"""Plays out the rest of the season and the playoffs many times to get each team's seeding and title chances."""
import math

import numpy as np

VERSION = "1"
EAST = {"ATL", "BOS", "BKN", "CHA", "CHI", "CLE", "DET", "IND", "MIA", "MIL", "NY", "NYK", "ORL", "PHI", "TOR",
        "WSH", "WAS"}
ROUNDS = ("First round", "Conference semifinals", "Conference finals")
HOME_GAMES, AWAY_GAMES = [0, 1, 4, 6], [2, 3, 5]      # 2-2-1-1-1 for the side with home court


def conference(abbr: str) -> str:
    return "East" if abbr in EAST else "West"


def _phi(x):
    """Normal CDF for arrays (Abramowitz-Stegun 7.1.26, good to about 1e-7)."""
    z = np.abs(x) / math.sqrt(2.0)
    t = 1.0 / (1.0 + 0.3275911 * z)
    erf = 1.0 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) \
        * t * np.exp(-z * z)
    return 0.5 * (1.0 + np.sign(x) * erf)


SD_START, SD_END = 2.0, 1.0     # doubt about a team's true strength (points): before any games, and after 82


def rating_sd(played: float, total: float = 82.0) -> float:
    """How far a team's true strength may sit from its rating: wide early, narrower as games pile up."""
    left = max(total - played, 0.0) / total
    return SD_END + (SD_START - SD_END) * left


class _Sim:
    def __init__(self, R, key, hca, sigma, rng):
        self.R, self.key, self.hca, self.sigma, self.rng = R, key, hca, sigma, rng
        self.rows = np.arange(R.shape[0])

    def game(self, home, away):
        d = self.R[self.rows, home] - self.R[self.rows, away] + self.hca
        return self.rng.random(len(self.rows)) < _phi(d / self.sigma)

    def series(self, a, b):
        """Best of seven; the side with the better record has home court. Returns the winners."""
        a_hi = self.key[self.rows, a] >= self.key[self.rows, b]
        hi, lo = np.where(a_hi, a, b), np.where(a_hi, b, a)
        d = self.R[self.rows, hi] - self.R[self.rows, lo]
        p_home, p_away = _phi((d + self.hca) / self.sigma), _phi((d - self.hca) / self.sigma)
        u = self.rng.random((len(self.rows), 7))
        won = (u[:, HOME_GAMES] < p_home[:, None]).sum(1) + (u[:, AWAY_GAMES] < p_away[:, None]).sum(1)
        return np.where(won >= 4, hi, lo)


def simulate(teams: dict, games, net: dict, hca: float, sigma: float, as_of: str | None = None,
             sims: int = 10000, seed: int = 7, chunk: int = 2000) -> dict | None:
    """teams: id -> row with abbr. games: this season's regular-season games. net: id -> rating in points."""
    ids = sorted(t for t in teams if t in net)
    if len(ids) < 20:
        return None
    ix = {t: i for i, t in enumerate(ids)}
    n = len(ids)
    conf = np.array([0 if conference(teams[t]["abbr"]) == "East" else 1 for t in ids])
    w0, l0 = np.zeros(n), np.zeros(n)
    rem_h, rem_a = [], []
    for g in games:
        h, a = ix.get(g["home_id"]), ix.get(g["away_id"])
        if h is None or a is None:
            continue
        done = g["completed"] and g["home_score"] is not None and g["home_score"] != g["away_score"]
        if done and (as_of is None or g["day"] < as_of):
            hw = g["home_score"] > g["away_score"]
            w0[h if hw else a] += 1
            l0[a if hw else h] += 1
        else:
            rem_h.append(h)
            rem_a.append(a)
    G = len(rem_h)
    rem_h, rem_a = np.array(rem_h, dtype=int), np.array(rem_a, dtype=int)
    Hm, Am = np.zeros((G, n), dtype=np.float32), np.zeros((G, n), dtype=np.float32)
    Hm[np.arange(G), rem_h] = 1
    Am[np.arange(G), rem_a] = 1
    base = np.array([net[t] for t in ids])
    sd = np.array([rating_sd(w0[i] + l0[i]) for i in range(n)])
    rng = np.random.default_rng(seed)

    tally = {k: np.zeros(n) for k in ("wins", "top6", "playin", "playoffs", "r2", "cf", "finals", "title", "seed")}
    win_sq = np.zeros(n)
    done_sims = 0
    while done_sims < sims:
        S = min(chunk, sims - done_sims)
        done_sims += S
        R = base[None, :] + rng.normal(0.0, 1.0, (S, n)) * sd[None, :]
        wins = np.tile(w0, (S, 1))
        if G:
            hw = (rng.random((S, G)) < _phi((R[:, rem_h] - R[:, rem_a] + hca) / sigma)).astype(np.float32)
            wins = wins + hw @ Hm + (1.0 - hw) @ Am
        tally["wins"] += wins.sum(0)
        win_sq += (wins ** 2).sum(0)
        key = wins + rng.random((S, n)) * 0.5           # ties are broken at random
        sim = _Sim(R, key, hca, sigma, rng)
        champs = []
        for c in (0, 1):
            members = np.where(conf == c)[0]
            order = members[np.argsort(-key[:, members], axis=1)]       # team index by seed, per simulation
            for k in range(order.shape[1]):
                np.add.at(tally["seed"], order[:, k], k + 1)
            for k in range(6):
                np.add.at(tally["top6"], order[:, k], 1)
            for k in range(6, 10):
                np.add.at(tally["playin"], order[:, k], 1)
            s7, s8, s9, s10 = order[:, 6], order[:, 7], order[:, 8], order[:, 9]
            a = sim.game(s7, s8)
            seed7, loser = np.where(a, s7, s8), np.where(a, s8, s7)
            b = sim.game(s9, s10)
            nine_ten = np.where(b, s9, s10)
            seed8 = np.where(sim.game(loser, nine_ten), loser, nine_ten)
            br = [order[:, 0], order[:, 1], order[:, 2], order[:, 3], order[:, 4], order[:, 5], seed7, seed8]
            for t in br:
                np.add.at(tally["playoffs"], t, 1)
            r1 = [sim.series(br[0], br[7]), sim.series(br[3], br[4]), sim.series(br[1], br[6]), sim.series(br[2], br[5])]
            for t in r1:
                np.add.at(tally["r2"], t, 1)
            r2 = [sim.series(r1[0], r1[1]), sim.series(r1[2], r1[3])]
            for t in r2:
                np.add.at(tally["cf"], t, 1)
            champ = sim.series(r2[0], r2[1])
            np.add.at(tally["finals"], champ, 1)
            champs.append(champ)
        np.add.at(tally["title"], sim.series(champs[0], champs[1]), 1)

    rows = []
    for i, t in enumerate(ids):
        mean = tally["wins"][i] / sims
        var = max(win_sq[i] / sims - mean * mean, 0.0)
        rows.append({
            "id": t, "abbr": teams[t]["abbr"], "name": teams[t]["name"], "logo": teams[t].get("logo"),
            "conf": conference(teams[t]["abbr"]), "w": int(w0[i]), "l": int(l0[i]), "net": round(float(base[i]), 1),
            "proj_w": round(mean, 1), "proj_sd": round(math.sqrt(var), 1), "avg_seed": round(tally["seed"][i] / sims, 1),
            **{k: round(float(tally[k][i]) / sims, 4) for k in ("top6", "playin", "playoffs", "r2", "cf", "finals", "title")},
        })
    return {"teams": rows, "games_left": G, "sims": sims,
            "bracket": _bracket(rows, ix, base, sd, hca, sigma, seed)}


def _bracket(rows, ix, base, sd, hca, sigma, seed, sims: int = 4000) -> dict:
    """The single most likely path: seeds by projected wins, the favourite advancing in every series."""
    rng = np.random.default_rng(seed + 1)
    R = base[None, :] + rng.normal(0.0, 1.0, (sims, len(base))) * sd[None, :]
    proj = {r["id"]: r["proj_w"] for r in rows}
    key = np.tile(np.array([proj[t] for t in sorted(ix, key=ix.get)]), (sims, 1))
    sim = _Sim(R, key, hca, sigma, rng)
    info = {r["id"]: r for r in rows}

    def play(a, b):
        ia, ib = ix[a["id"]], ix[b["id"]]
        won = sim.series(np.full(sims, ia), np.full(sims, ib))
        pa = float((won == ia).mean())
        hi, lo = (a, b) if proj[a["id"]] >= proj[b["id"]] else (b, a)
        p_hi = pa if hi is a else 1 - pa
        winner = hi if p_hi >= 0.5 else lo
        return {"hi": hi, "lo": lo, "p_hi": round(p_hi, 3), "winner": winner["id"]}, winner

    out, champs = {"conferences": []}, []
    for cname in ("East", "West"):
        seeds = sorted((r for r in rows if r["conf"] == cname), key=lambda r: -r["proj_w"])[:8]
        slots = [{"id": r["id"], "abbr": r["abbr"], "name": r["name"], "logo": r["logo"], "seed": i + 1}
                 for i, r in enumerate(seeds)]
        rounds, cur = [], [(slots[0], slots[7]), (slots[3], slots[4]), (slots[1], slots[6]), (slots[2], slots[5])]
        while cur:
            res = [play(a, b) for a, b in cur]
            rounds.append([m for m, _ in res])
            winners = [w for _, w in res]
            cur = [(winners[i], winners[i + 1]) for i in range(0, len(winners) - 1, 2)]
        out["conferences"].append({"name": cname, "rounds": rounds})
        champs.append(winners[0])
    final, champ = play(champs[0], champs[1])
    out["finals"] = final
    out["champion"] = {**champ, "title": info[champ["id"]]["title"]}
    return out


def season_outcome(data, season: int) -> dict | None:
    """The real champion and playoff field of a finished season."""
    po = sorted((g for g in data.games.values() if g["season"] == season and g["stype"] == 3 and g["completed"]
                 and g["home_score"] is not None), key=lambda g: g["date"])
    if len(po) < 60:
        return None
    last = po[-1]
    champ = last["home_id"] if last["home_score"] > last["away_score"] else last["away_id"]
    field = {g["home_id"] for g in po} | {g["away_id"] for g in po}
    return {"champion": champ, "field": field}


def history(data, P, teams, cur_season: int, month_day: str, builder_cls) -> list[dict]:
    """The same simulation run at this date of earlier seasons, against what then happened."""
    out = []
    for season in data.seasons:
        real = season_outcome(data, season) if season < cur_season else None
        if not real:
            continue
        day = f"{season - 1 if month_day >= '07-01' else season}-{month_day}"
        b = builder_cls(data, P)
        b.advance(day)
        ctx = b.snapshot(season, iters=30)
        net = {t: ctx.off[t] - ctx.dfa[t] for t in teams if t in ctx.off}
        games = [g for g in data.games.values() if g["season"] == season and g["stype"] == 2]
        res = simulate(teams, games, net, ctx.hca, P.sigma_margin, as_of=day, sims=6000)
        if not res or not res["games_left"]:
            continue
        ranked = sorted(res["teams"], key=lambda t: -t["title"])
        pos = next((i for i, t in enumerate(ranked) if t["id"] == real["champion"]), None)
        if pos is None:
            continue
        called = {t["id"] for t in sorted(res["teams"], key=lambda t: -t["playoffs"])[:16]}
        out.append({"season": season, "champion": ranked[pos]["name"], "chance": ranked[pos]["title"], "rank": pos + 1,
                    "favourite": ranked[0]["name"], "favourite_chance": ranked[0]["title"],
                    "field_called": len(called & real["field"]), "field": len(real["field"])})
    return out
