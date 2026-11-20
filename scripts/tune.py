"""Tune model parameters on a past season (walk-forward) and write app/params.json.

Usage: python3 -m scripts.tune [season]   (default: the season before the current one)
"""
import json
import math
import sys
from dataclasses import asdict, replace

from app import db, nba_model as M

GRID = {
    "h_min": [3, 5, 8, 12],
    "prior_mult": [0.2, 0.4, 0.6, 0.9],
    "prior_mult_min": [0.1, 0.25, 0.5],
    "kappa": [20, 40, 80],
    "beta": [0.0, 0.5, 1.0],
    "gamma": [0.0, 0.5, 1.0],
    "k_opp": [5, 10, 20],
    "blend": [0.0, 0.3, 0.5, 0.7],
    "rating_prev_w": [0.2, 0.35, 0.6],
    "rating_ridge": [2, 4, 8],
    "rating_half": [0, 60, 120, 240],
    "rest_pen": [0.0, 1.0, 2.0, 3.0],
    "decomp": [0.0, 0.5, 1.0],
    "decomp3": [0.0, 0.5, 1.0],
    "pct_prior": [0.4, 0.7, 1.0],
    "blow_k": [0.0, 0.1, 0.2, 0.3],
    "b2b_min": [0.0, 0.05, 0.1, 0.2],
}
H_GRID = [8, 15, 25, 40]          # per-stat half-lives, tuned one stat at a time
MARKET_GRID = [0.0, 0.25, 0.5, 0.65, 0.8]
WEIGHTS = (3, 1.5, 1.5, 1, 0.5, 0.5, 0.5)


def score(data, P, season):
    preds = M.backtest(data, P, {season})
    s = M.summarize(data, P, preds)
    stat = sum(w * x["mae"] / x["season_avg_mae"] for w, x in zip(WEIGHTS, s["stats"])) / sum(WEIGHTS)
    so = s["stats_only"]
    return stat + 0.5 * so["margin_mae"] / 11.5 + 0.15 * so["total_mae"] / 15.0, s, preds


def main():
    c = db.conn()
    data = M.NBAData(c)
    season = int(sys.argv[1]) if len(sys.argv) > 1 else data.seasons[-2]
    P = M.Params()

    # Home/away multipliers straight from the data.
    hs, as_ = [0.0] * M.NS, [0.0] * M.NS
    for g in data.completed:
        th, ta = data.team_tot.get((g["id"], g["home_id"])), data.team_tot.get((g["id"], g["away_id"]))
        if th and ta:
            for i in range(M.NS):
                hs[i] += th[i]
                as_[i] += ta[i]
    P = replace(P, home_adj=tuple(round(math.sqrt(hs[i] / as_[i]), 4) for i in range(M.NS)))
    print("home_adj", P.home_adj, flush=True)

    # Stage 1: the stats model on its own, with the betting line switched off.
    P = replace(P, w_mkt_margin=0.0, w_mkt_total=0.0)
    best, summary, preds = score(data, P, season)
    print("start", round(best, 5), flush=True)
    for rnd in range(2):
        changed = False
        trials = [(name, v, {name: v}) for name, values in GRID.items() for v in values]
        for i in range(M.NS):
            for h in H_GRID:
                trials.append((f"h_rates[{i}]", h, None))
        for name, v, kw in trials:
            if kw is None:
                i = int(name[8])
                if P.h_rates[i] == v:
                    continue
                hs = list(P.h_rates)
                hs[i] = v
                cand = replace(P, h_rates=tuple(hs))
            else:
                if getattr(P, name) == v:
                    continue
                cand = replace(P, **kw)
            sc, sm, pr = score(data, cand, season)
            if sc < best - 1e-5:
                best, P, summary, preds, changed = sc, cand, sm, pr, True
                print(f"  {name}={v} -> {best:.5f}", flush=True)
        if not changed:
            break

    # Scoring runs lower in the playoffs than the regular-season ratings expect.
    num = den = 0.0
    for gid, p in preds.items():
        g = data.games[gid]
        if g["stype"] in (3, 5):
            num += g["home_score"] + g["away_score"]
            den += p["model_total"]
    if den:
        P = replace(P, playoff_total=round(min(max(0.5 + 0.5 * num / den, 0.94), 1.0), 3))
    print("playoff_total", P.playoff_total, flush=True)

    # Stage 2: how much of the betting line to mix in when one exists.
    for field, key in (("w_mkt_margin", "margin_mae"), ("w_mkt_total", "total_mae")):
        best_w, best_e = 0.0, 1e9
        for w in MARKET_GRID:
            sm = M.summarize(data, replace(P, **{field: w}), M.backtest(data, replace(P, **{field: w}), {season}))
            print(f"  {field}={w}: {key} {sm[key]}", flush=True)
            if sm[key] < best_e - 0.005:
                best_w, best_e = w, sm[key]
        P = replace(P, **{field: best_w})
    preds = M.backtest(data, P, {season})

    # Spread of outcomes around each projection: var = phi*m + c*m^2.
    phi, cc = [], []
    for i in range(M.NS):
        s11 = s12 = s22 = b1 = b2 = 0.0
        for p in preds.values():
            for side in ("home", "away"):
                for line in p["players"][side]:
                    if line.get("act") is None or line["min"] < 12:
                        continue
                    m, e2 = line["proj"][i], (line["proj"][i] - line["act"][i]) ** 2
                    wt = 1.0 / max(m, 0.3) ** 2
                    s11 += wt * m * m; s12 += wt * m ** 3; s22 += wt * m ** 4
                    b1 += wt * m * e2; b2 += wt * m * m * e2
        det = s11 * s22 - s12 * s12
        a, b = (b1 * s22 - b2 * s12) / det, (b2 * s11 - b1 * s12) / det
        if b < 0:
            a, b = b1 / s11, 0.0
        if a < 1:
            a, b = 1.0, max((b2 - s12) / s22, 0.0)
        phi.append(round(a, 3)); cc.append(round(b, 4))
    P = replace(P, var_phi=tuple(phi), var_c=tuple(cc))

    # Margin spread that makes win probabilities best calibrated.
    best_s, best_ll = P.sigma_margin, 1e9
    for s10 in range(90, 181, 5):
        sg, ll = s10 / 10.0, 0.0
        for gid, p in preds.items():
            g = data.games[gid]
            ph = min(max(M._phi(p["margin"] / sg), 1e-4), 1 - 1e-4)
            ll -= math.log(ph if g["home_score"] > g["away_score"] else 1 - ph)
        if ll < best_ll:
            best_s, best_ll = sg, ll
    P = replace(P, sigma_margin=best_s)

    final = M.summarize(data, P, M.backtest(data, P, {season}))
    print(json.dumps(final, indent=1))
    with open("app/params.json", "w") as f:
        json.dump({"tuned_on": season, **asdict(P)}, f, indent=1)
    print("written", asdict(P))


if __name__ == "__main__":
    main()
