"""League award predictions from season-to-date production, team success and availability.

Each award has a transparent score. Scores turn into rough chances with a softmax that is
flatter early in the season, when a lot can still change. `as_of` lets the same code be
replayed on past seasons to check where the eventual winners ranked.
"""
import math
from collections import defaultdict

from . import nba_model as M

VERSION = "2"   # bump when a scoring rule changes, so cached history is rebuilt
AWARDS = [
    ("mvp", "Most Valuable Player"), ("roy", "Rookie of the Year"), ("dpoy", "Defensive Player of the Year"),
    ("smoy", "Sixth Man of the Year"), ("mip", "Most Improved Player"),
]
TEMP = {"mvp": 2.2, "roy": 2.5, "dpoy": 0.55, "smoy": 1.6, "mip": 2.2}
# Actual winners, used only to check the method against history.
PAST_WINNERS = {
    2025: {"mvp": "Shai Gilgeous-Alexander", "roy": "Stephon Castle", "dpoy": "Evan Mobley",
           "smoy": "Payton Pritchard", "mip": "Dyson Daniels"},
    2026: {"mvp": "Shai Gilgeous-Alexander", "roy": "Cooper Flagg", "dpoy": "Victor Wembanyama",
           "smoy": "Keldon Johnson", "mip": "Nickeil Alexander-Walker"},
}


def _agg(rows):
    g = [r for r in rows if r.played]
    n = len(g)
    if not n:
        return None
    s = [sum(r.v[i] for r in g) / n for i in range(M.NS)]
    sh = [sum(r.sh[i] for r in g) / n for i in range(6)]
    fgm, fga = sh[0] + sh[2], sh[1] + sh[3]
    gmsc = s[0] + 0.4 * fgm - 0.7 * fga - 0.4 * (sh[5] - sh[4]) + 0.4 * s[1] + s[4] + 0.7 * s[2] + 0.7 * s[5] - s[6]
    return {"gp": n, "starts": sum(1 for r in g if r.starter), "min": sum(r.min for r in g) / n,
            "pts": s[0], "reb": s[1], "ast": s[2], "stl": s[4], "blk": s[5], "gmsc": gmsc}


def compute(data, P, season, teams, players, as_of=None, cur_season=None, top=8):
    """teams: id -> {abbr,name}; players: id -> {experience,...} from current rosters."""
    cur_season = cur_season or season
    as_of = as_of or "9999-12-31"
    builder = M.ContextBuilder(data, P)
    builder.advance(as_of)
    ctx = builder.snapshot(season, iters=20)
    sched = [g for g in data.games.values() if g["season"] == season and g["stype"] == 2]
    left = defaultdict(float)
    n_left = defaultdict(int)
    rec = defaultdict(lambda: [0, 0])
    for g in sched:
        if g["day"] < as_of and g["completed"]:
            if g["home_score"] is not None and g["home_score"] != g["away_score"]:
                hw = g["home_score"] > g["away_score"]
                rec[g["home_id"]][0 if hw else 1] += 1
                rec[g["away_id"]][1 if hw else 0] += 1
            continue
        h, a = g["home_id"], g["away_id"]
        m = ctx.hca + ctx.off.get(h, 0) - ctx.dfa.get(h, 0) - ctx.off.get(a, 0) + ctx.dfa.get(a, 0)
        p = M._phi(m / P.sigma_margin)
        left[h] += p
        left[a] += 1 - p
        n_left[h] += 1
        n_left[a] += 1
    team = {}
    for tid in teams:
        w, l = rec[tid]
        tot = w + l + n_left[tid]
        team[tid] = {"w": w, "l": l, "proj_w": w + left[tid], "games": tot or 82,
                     "pct": (w + left[tid]) / tot if tot else 0.5}
    played_frac = sum(t["w"] + t["l"] for t in team.values()) / max(sum(t["games"] for t in team.values()), 1)
    first_season = data.seasons[0] if data.seasons else season
    cands = []
    for pid, rows in data.rows_by_player.items():
        cur = [r for r in rows if r.season == season and r.day < as_of and data.games[r.gid]["stype"] == 2]
        a = _agg(cur)
        if not a:
            continue
        tid = cur[-1].tid
        t = team.get(tid)
        if not t:
            continue
        b = _agg([r for r in rows if r.season == season - 1 and data.games[r.gid]["stype"] == 2])
        tg = t["w"] + t["l"]
        k = 8.0 if b and b["gp"] >= 20 else 0.0     # early on, lean on last season
        v = {x: (a[x] * a["gp"] + k * b[x]) / (a["gp"] + k) if k else a[x]
             for x in ("pts", "reb", "ast", "stl", "blk", "gmsc", "min")}
        proj_gp = a["gp"] + (t["games"] - tg) * min(a["gp"] / tg, 1.0) if tg else a["gp"]
        exp = (players.get(pid) or {}).get("experience")
        seen_before = rows[0].season < season
        # ESPN counts a rookie as 0 years and a second-year player as 2.
        rookie_exp = 0 if season == cur_season else cur_season - season + 1
        rookie = exp == rookie_exp if exp is not None else (not seen_before and season > first_season)
        cands.append({
            "pid": pid, "name": cur[-1].name, "tid": tid, "gp": a["gp"], "tg": tg, "v": v, "raw": a, "prev": b,
            "proj_gp": proj_gp, "eligible": proj_gp >= 60 * t["games"] / 82,
            "at_risk": proj_gp < 65 * t["games"] / 82,
            "rookie": rookie,
            "bench": a["starts"] < a["gp"] / 2, "avail": min(a["gp"] / tg, 1.0) if tg else 1.0,
        })

    def rank(key, pool, score):
        scored = sorted(((score(c), c) for c in pool), key=lambda x: -x[0])[:25]
        if not scored:
            return []
        temp = TEMP[key] * (1 + 1.5 * (1 - played_frac))
        mx = scored[0][0]
        ws = [math.exp((s - mx) / temp) for s, _ in scored]
        tot = sum(ws)
        out = []
        for (s, c), w in zip(scored[:top], ws):
            t = teams.get(c["tid"], {})
            row = {"pid": c.get("pid"), "name": c["name"], "team": t.get("abbr"), "tid": c["tid"],
                   "chance": round(w / tot, 3), "score": round(s, 2)}
            if "v" in c:
                row.update(gp=c["gp"], pts=round(c["raw"]["pts"], 1), reb=round(c["raw"]["reb"], 1),
                           ast=round(c["raw"]["ast"], 1), stl=round(c["raw"]["stl"], 1),
                           blk=round(c["raw"]["blk"], 1), at_risk=c["at_risk"],
                           prev_pts=round(c["prev"]["pts"], 1) if c["prev"] else None)
            tm = team[c["tid"]]
            row.update(w=tm["w"], l=tm["l"], proj_w=round(tm["proj_w"] * 82 / tm["games"]))
            out.append(row)
        return out

    reg = [c for c in cands if c["avail"] >= 0.6 and c["eligible"]]
    res = {}
    res["mvp"] = rank("mvp", [c for c in reg if c["v"]["min"] >= 28],
                      lambda c: c["v"]["gmsc"] * (0.35 + 1.3 * team[c["tid"]]["pct"]))
    res["roy"] = rank("roy", [c for c in cands if c["rookie"] and c["avail"] >= 0.5],
                      lambda c: c["raw"]["gmsc"] + 0.5 * c["raw"]["pts"])
    res["dpoy"] = rank("dpoy", [c for c in reg if c["v"]["min"] >= 26],
                       lambda c: 1.3 * c["v"]["blk"] + 0.8 * c["v"]["stl"] + 0.1 * c["v"]["reb"]
                       - 0.15 * ctx.dfa.get(c["tid"], 0.0))
    res["smoy"] = rank("smoy", [c for c in cands if c["bench"] and c["avail"] >= 0.6],
                       lambda c: c["v"]["pts"] + 0.3 * (c["v"]["reb"] + c["v"]["ast"]) + 6 * team[c["tid"]]["pct"])
    res["mip"] = rank("mip", [c for c in reg if c["prev"] and c["prev"]["gp"] >= 20 and not c["rookie"]
                              and c["v"]["pts"] >= 12 and c["v"]["min"] >= 26
                              and c["prev"]["pts"] < 22],     # voters do not give this to established stars
                      lambda c: (c["v"]["gmsc"] - c["prev"]["gmsc"]) + 0.6 * (c["v"]["pts"] - c["prev"]["pts"]))
    return {"season": season, "played": round(played_frac, 3), "awards": res}


def winner_rank(result, key, winner):
    for i, r in enumerate(result["awards"].get(key, []), 1):
        if r["name"] == winner:
            return i
    return None


def history(data, P, teams, players, cur_season, month_day):
    """Where the eventual winners ranked at this point of earlier seasons, and at season's end."""
    out = []
    for season, winners in PAST_WINNERS.items():
        if season not in data.seasons or season >= cur_season:
            continue
        year = season - 1 if month_day >= "07-01" else season
        now = compute(data, P, season, teams, players, as_of=f"{year}-{month_day}", cur_season=cur_season, top=25)
        end = compute(data, P, season, teams, players, cur_season=cur_season, top=25)
        for key, name in AWARDS:
            if key == "mip" and (season - 1) not in data.seasons:
                continue
            w = winners[key]
            out.append({"season": season, "award": key, "winner": w,
                        "rank_now": winner_rank(now, key, w), "rank_end": winner_rank(end, key, w)})
    return out
