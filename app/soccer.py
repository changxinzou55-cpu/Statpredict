"""Football (soccer): results and squads from ESPN, a Dixon-Coles style goals model, player scoring chances."""
import logging
import math
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import db, espn

log = logging.getLogger("statpredict.soccer")
LEAGUES = {
    "eng.1": "Premier League",
    "esp.1": "La Liga",
    "ita.1": "Serie A",
    "ger.1": "Bundesliga",
    "fra.1": "Ligue 1",
}
HALF_LIFE_DAYS = 300.0
SHRINK = 3.0
MAX_GOALS = 8
POS_GOAL_PRIOR = {"F": 0.30, "M": 0.10, "D": 0.04, "G": 0.0}
POS_ASSIST_PRIOR = {"F": 0.15, "M": 0.12, "D": 0.05, "G": 0.005}


# ---------- ingestion ----------

def sync_league(lg: str, years: list[int]) -> None:
    c = db.conn()
    data = espn.get(f"soccer/{lg}/teams", {"limit": 100})
    for t in data["sports"][0]["leagues"][0]["teams"]:
        t = t["team"]
        c.execute(
            "INSERT OR REPLACE INTO soc_teams(id,league,name,short,abbr,logo,color) VALUES(?,?,?,?,?,?,?)",
            (t["id"], lg, t.get("displayName"), t.get("shortDisplayName"), t.get("abbreviation"),
             (t.get("logos") or [{}])[0].get("href"), t.get("color")),
        )
    for y in years:
        data = espn.get(f"soccer/{lg}/scoreboard", {"dates": str(y), "limit": 1000})
        for e in data.get("events", []):
            comp = e["competitions"][0]
            home = next((x for x in comp["competitors"] if x["homeAway"] == "home"), None)
            away = next((x for x in comp["competitors"] if x["homeAway"] == "away"), None)
            if not home or not away:
                continue
            st = comp["status"]["type"]
            done = bool(st.get("completed")) and st.get("name") == "STATUS_FULL_TIME"
            for side in (home, away):
                t = side["team"]
                c.execute(
                    "INSERT OR IGNORE INTO soc_teams(id,league,name,short,abbr,logo,color) VALUES(?,?,?,?,?,?,?)",
                    (t["id"], lg, t.get("displayName"), t.get("shortDisplayName"), t.get("abbreviation"),
                     t.get("logo"), t.get("color")),
                )
            c.execute(
                "INSERT OR REPLACE INTO soc_matches(id,league,date,season,state,completed,status_detail,home_id,"
                "away_id,home_goals,away_goals,venue) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (e["id"], lg, e["date"], e["season"]["year"], st["state"], 1 if done else 0,
                 st.get("shortDetail"), home["team"]["id"], away["team"]["id"],
                 int(home.get("score") or 0) if st["state"] != "pre" else None,
                 int(away.get("score") or 0) if st["state"] != "pre" else None,
                 (comp.get("venue") or {}).get("fullName")),
            )
    c.commit()


def _stat(athlete: dict, name: str) -> float:
    for cat in ((athlete.get("statistics") or {}).get("splits") or {}).get("categories", []):
        for s in cat.get("stats", []):
            if s.get("name") == name:
                return float(s.get("value") or 0)
    return 0.0


def sync_squads(lg: str) -> None:
    c = db.conn()
    season = c.execute("SELECT MAX(season) s FROM soc_matches WHERE league=?", (lg,)).fetchone()["s"]
    teams = [r["t"] for r in c.execute(
        "SELECT DISTINCT home_id t FROM soc_matches WHERE league=? AND season=?", (lg, season))]

    def one(tid):
        try:
            return tid, espn.get(f"soccer/{lg}/teams/{tid}/roster")
        except Exception as ex:
            log.warning("squad %s/%s failed: %s", lg, tid, ex)
            return tid, None

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(one, teams))
    for tid, data in results:
        if not data:
            continue
        c.execute("DELETE FROM soc_players WHERE team_id=? AND league=?", (tid, lg))
        for a in data.get("athletes", []):
            c.execute(
                "INSERT OR REPLACE INTO soc_players(id,team_id,league,name,pos,jersey,apps,sub_ins,goals,assists,"
                "shots,sot,injured) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (a["id"], tid, lg, a.get("displayName"), (a.get("position") or {}).get("abbreviation"),
                 a.get("jersey"), _stat(a, "appearances"), _stat(a, "subIns"), _stat(a, "totalGoals"),
                 _stat(a, "goalAssists"), _stat(a, "totalShots"), _stat(a, "shotsOnTarget"),
                 1 if a.get("injuries") else 0),
            )
    c.commit()


def refresh() -> None:
    db.init()
    now = datetime.now(timezone.utc)
    years = sorted({now.year, (now + timedelta(days=30)).year})
    for lg in LEAGUES:
        try:
            ys = list(years)
            if not db.get_meta(f"soc_hist_{lg}_{now.year - 1}"):
                ys.insert(0, now.year - 1)
            sync_league(lg, ys)
            db.set_meta(f"soc_hist_{lg}_{now.year - 1}", True)
            if time.time() - (db.get_meta(f"soc_squads_{lg}") or 0) > 12 * 3600:
                sync_squads(lg)
                db.set_meta(f"soc_squads_{lg}", time.time())
        except Exception as ex:
            log.warning("soccer refresh %s failed: %s", lg, ex)
    db.set_meta("soc_refreshed_at", time.time())


# ---------- model ----------

def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def fit(matches: list[dict], as_of: float) -> dict | None:
    """matches: completed games before `as_of`. Returns attack/defence strengths, base rates and rho."""
    rows = []
    for m in matches:
        age = (as_of - m["ts"]) / 86400.0
        if age < 0 or age > 800:
            continue
        rows.append((0.5 ** (age / HALF_LIFE_DAYS), m["home_id"], m["away_id"], m["home_goals"], m["away_goals"]))
    if len(rows) < 30:
        return None
    sw = sum(r[0] for r in rows)
    mu_h = sum(r[0] * r[3] for r in rows) / sw
    mu_a = sum(r[0] * r[4] for r in rows) / sw
    att, dfc = defaultdict(lambda: 1.0), defaultdict(lambda: 1.0)
    for _ in range(25):
        gs, ex = defaultdict(float), defaultdict(float)
        for w, h, a, hg, ag in rows:
            gs[h] += w * hg; ex[h] += w * mu_h * dfc[a]
            gs[a] += w * ag; ex[a] += w * mu_a * dfc[h]
        for t in gs:
            att[t] = (gs[t] + SHRINK) / (ex[t] + SHRINK)
        gc, ex = defaultdict(float), defaultdict(float)
        for w, h, a, hg, ag in rows:
            gc[a] += w * hg; ex[a] += w * mu_h * att[h]
            gc[h] += w * ag; ex[h] += w * mu_a * att[a]
        for t in gc:
            dfc[t] = (gc[t] + SHRINK) / (ex[t] + SHRINK)
    best_rho, best_ll = 0.0, -1e18
    for r10 in range(-16, 7, 2):
        rho, ll, ok = r10 / 100.0, 0.0, True
        for w, h, a, hg, ag in rows:
            if hg > 1 or ag > 1:
                continue
            t = _tau(hg, ag, mu_h * att[h] * dfc[a], mu_a * att[a] * dfc[h], rho)
            if t <= 0:
                ok = False
                break
            ll += w * math.log(t)
        if ok and ll > best_ll:
            best_rho, best_ll = rho, ll
    return {"att": dict(att), "dfc": dict(dfc), "mu_h": mu_h, "mu_a": mu_a, "rho": best_rho}


def _tau(x: int, y: int, lh: float, la: float, rho: float) -> float:
    if x == 0 and y == 0:
        return 1 - lh * la * rho
    if x == 0 and y == 1:
        return 1 + lh * rho
    if x == 1 and y == 0:
        return 1 + la * rho
    if x == 1 and y == 1:
        return 1 - rho
    return 1.0


def predict(model: dict, home: str, away: str) -> dict:
    lh = model["mu_h"] * model["att"].get(home, 0.85) * model["dfc"].get(away, 1.15)
    la = model["mu_a"] * model["att"].get(away, 0.85) * model["dfc"].get(home, 1.15)
    ph = [math.exp(-lh) * lh ** k / math.factorial(k) for k in range(MAX_GOALS + 1)]
    pa = [math.exp(-la) * la ** k / math.factorial(k) for k in range(MAX_GOALS + 1)]
    grid, tot = [], 0.0
    for x in range(MAX_GOALS + 1):
        row = []
        for y in range(MAX_GOALS + 1):
            p = ph[x] * pa[y] * _tau(x, y, lh, la, model["rho"])
            row.append(p)
            tot += p
        grid.append(row)
    p_home = p_draw = p_away = over25 = btts = 0.0
    scores = []
    for x in range(MAX_GOALS + 1):
        for y in range(MAX_GOALS + 1):
            p = grid[x][y] / tot
            if x > y:
                p_home += p
            elif x == y:
                p_draw += p
            else:
                p_away += p
            if x + y > 2:
                over25 += p
            if x and y:
                btts += p
            scores.append((p, x, y))
    scores.sort(reverse=True)
    return {
        "xg_home": round(lh, 2), "xg_away": round(la, 2),
        "p_home": round(p_home, 3), "p_draw": round(p_draw, 3), "p_away": round(p_away, 3),
        "over25": round(over25, 3), "btts": round(btts, 3),
        "scores": [{"home": x, "away": y, "p": round(p, 3)} for p, x, y in scores[:5]],
    }


def player_chances(players: list[dict], team_matches: int, xg: float, team_gpg: float, conv: float) -> list[dict]:
    """Chance each squad player scores or assists, scaled to the team's expected goals in this match."""
    out, lam_sum, ast_sum = [], 0.0, 0.0
    n = max(team_matches, 1)
    factor = xg / max(team_gpg, 0.4)
    for p in players:
        starts = max(p["apps"] - p["sub_ins"], 0.0)
        eff = starts + 0.35 * p["sub_ins"]
        if eff <= 0 or p["injured"]:
            continue
        pos = (p["pos"] or "M")[0]
        g_hat = 0.5 * p["goals"] + 0.5 * p["shots"] * conv
        rate = (g_hat + 2.0 * POS_GOAL_PRIOR.get(pos, 0.1)) / (eff + 2.0)
        arate = (p["assists"] + 2.0 * POS_ASSIST_PRIOR.get(pos, 0.08)) / (eff + 2.0)
        play = min(eff / n, 1.0)
        lam, al = rate * play * factor, arate * play * factor
        lam_sum += lam
        ast_sum += al
        out.append({"id": p["id"], "name": p["name"], "pos": p["pos"], "apps": int(p["apps"]),
                    "goals": int(p["goals"]), "assists": int(p["assists"]), "shots": int(p["shots"]),
                    "lam": lam, "al": al})
    gs = min(max(0.97 * xg / lam_sum, 0.6), 1.6) if lam_sum else 1.0
    as_ = min(max(0.75 * xg / ast_sum, 0.6), 1.6) if ast_sum else 1.0
    for o in out:
        o["xg"] = round(o["lam"] * gs, 2)
        o["p_goal"] = round(1 - math.exp(-o.pop("lam") * gs), 3)
        o["p_assist"] = round(1 - math.exp(-o.pop("al") * as_), 3)
    out.sort(key=lambda o: -o["p_goal"])
    return out


class SoccerState:
    """Everything the API needs for football, rebuilt after each refresh."""

    def __init__(self, conn, cache: dict | None = None):
        now = time.time()
        self.leagues = {}
        self.match_index = {}
        cache = cache or {}
        self.cache = {}
        for lg, name in LEAGUES.items():
            ms = [dict(r) for r in conn.execute("SELECT * FROM soc_matches WHERE league=? ORDER BY date", (lg,))]
            if not ms:
                continue
            for m in ms:
                m["ts"] = _ts(m["date"])
            teams = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM soc_teams WHERE league=?", (lg,))}
            done = [m for m in ms if m["completed"]]
            season = max((m["season"] for m in ms if m["ts"] < now + 30 * 86400), default=None)
            model = fit(done, now)
            if not model:
                continue
            cur_done = [m for m in done if m["season"] == season]
            # Walk-forward check on this season's matches; earlier results are reused between refreshes.
            bt = dict(cache.get(lg, {}))
            by_day = defaultdict(list)
            for m in cur_done:
                if m["id"] not in bt:
                    by_day[m["date"][:10]].append(m)
            for day, group in by_day.items():
                cutoff = min(m["ts"] for m in group)
                mdl = fit([m for m in done if m["ts"] < cutoff], cutoff)
                if not mdl:
                    continue
                for m in group:
                    bt[m["id"]] = predict(mdl, m["home_id"], m["away_id"])
            self.cache[lg] = bt
            table = self._table(cur_done, teams, model)
            players = defaultdict(list)
            for r in conn.execute("SELECT * FROM soc_players WHERE league=?", (lg,)):
                players[r["team_id"]].append(dict(r))
            tg = sum(p["goals"] for ps in players.values() for p in ps)
            ts_ = sum(p["shots"] for ps in players.values() for p in ps)
            self.leagues[lg] = {
                "name": name, "season": season, "teams": teams, "model": model, "matches": ms,
                "table": table, "players": players, "conv": tg / ts_ if ts_ else 0.11, "backtest": bt,
                "accuracy": self._accuracy(cur_done, bt),
            }
            for m in ms:
                self.match_index[m["id"]] = lg

    @staticmethod
    def _table(done, teams, model):
        t = defaultdict(lambda: {"p": 0, "w": 0, "d": 0, "l": 0, "gf": 0, "ga": 0, "pts": 0})
        for m in done:
            for tid, gf, ga in ((m["home_id"], m["home_goals"], m["away_goals"]),
                                (m["away_id"], m["away_goals"], m["home_goals"])):
                r = t[tid]
                r["p"] += 1; r["gf"] += gf; r["ga"] += ga
                if gf > ga:
                    r["w"] += 1; r["pts"] += 3
                elif gf == ga:
                    r["d"] += 1; r["pts"] += 1
                else:
                    r["l"] += 1
        rows = []
        for tid, r in t.items():
            tm = teams.get(tid, {})
            rows.append({"id": tid, "name": tm.get("short") or tm.get("name"), "logo": tm.get("logo"), **r,
                         "att": round(model["att"].get(tid, 1.0), 2), "dfc": round(model["dfc"].get(tid, 1.0), 2)})
        rows.sort(key=lambda r: (-r["pts"], -(r["gf"] - r["ga"]), -r["gf"]))
        return rows

    @staticmethod
    def _accuracy(done, bt):
        n = hit = home = 0
        brier = base = 0.0
        outcomes = [0, 0, 0]
        rows = []
        for m in done:
            p = bt.get(m["id"])
            if not p:
                continue
            o = 0 if m["home_goals"] > m["away_goals"] else (1 if m["home_goals"] == m["away_goals"] else 2)
            rows.append((p, o))
            outcomes[o] += 1
        n = len(rows)
        if not n:
            return {"matches": 0}
        freq = [x / n for x in outcomes]
        for p, o in rows:
            probs = [p["p_home"], p["p_draw"], p["p_away"]]
            hit += probs.index(max(probs)) == o
            home += o == 0
            brier += sum((probs[i] - (1.0 if i == o else 0.0)) ** 2 for i in range(3))
            base += sum((freq[i] - (1.0 if i == o else 0.0)) ** 2 for i in range(3))
        return {"matches": n, "pick": round(hit / n, 3), "home_always": round(home / n, 3),
                "brier": round(brier / n, 3), "brier_base_rates": round(base / n, 3)}

    def _team(self, L, tid):
        t = L["teams"].get(tid, {})
        return {"id": tid, "name": t.get("short") or t.get("name"), "abbr": t.get("abbr"), "logo": t.get("logo")}

    def _match(self, L, m, with_pred=True):
        out = {"id": m["id"], "date": m["date"], "state": m["state"], "completed": bool(m["completed"]),
               "status": m["status_detail"], "home": self._team(L, m["home_id"]), "away": self._team(L, m["away_id"]),
               "score": None if m["home_goals"] is None else [m["home_goals"], m["away_goals"]], "venue": m["venue"]}
        if with_pred:
            if m["completed"]:
                out["pred"], out["source"] = L["backtest"].get(m["id"]), "backtest"
            else:
                out["pred"], out["source"] = predict(L["model"], m["home_id"], m["away_id"]), "live"
        return out

    def league_view(self, lg: str) -> dict | None:
        L = self.leagues.get(lg)
        if not L:
            return None
        now = time.time()
        upcoming = [m for m in L["matches"] if not m["completed"] and now - 4 * 3600 < m["ts"] < now + 21 * 86400]
        recent = [m for m in L["matches"] if m["completed"] and m["ts"] > now - 21 * 86400 and m["season"] == L["season"]]
        return {
            "league": lg, "name": L["name"], "season": L["season"],
            "upcoming": [self._match(L, m) for m in upcoming[:40]],
            "recent": [self._match(L, m) for m in reversed(recent[-30:])],
            "table": L["table"], "accuracy": L["accuracy"],
            "home_goals_avg": round(L["model"]["mu_h"], 2), "away_goals_avg": round(L["model"]["mu_a"], 2),
        }

    def match_view(self, mid: str) -> dict | None:
        lg = self.match_index.get(mid)
        L = self.leagues.get(lg) if lg else None
        if not L:
            return None
        m = next(x for x in L["matches"] if x["id"] == mid)
        out = self._match(L, m)
        out["league"], out["league_name"] = lg, L["name"]
        pred = out.get("pred")
        tbl = {r["id"]: r for r in L["table"]}
        out["form"] = {}
        for side, tid in (("home", m["home_id"]), ("away", m["away_id"])):
            last = [x for x in L["matches"] if x["completed"] and x["ts"] < m["ts"] and tid in (x["home_id"], x["away_id"])][-5:]
            out["form"][side] = [{
                "date": x["date"], "opp": self._team(L, x["away_id"] if x["home_id"] == tid else x["home_id"])["name"],
                "home": x["home_id"] == tid,
                "gf": x["home_goals"] if x["home_id"] == tid else x["away_goals"],
                "ga": x["away_goals"] if x["home_id"] == tid else x["home_goals"]} for x in reversed(last)]
            out[side]["row"] = tbl.get(tid)
        if pred and not m["completed"]:
            out["players"] = {}
            for side, tid, xg in (("home", m["home_id"], pred["xg_home"]), ("away", m["away_id"], pred["xg_away"])):
                row = tbl.get(tid) or {"p": 0, "gf": 0}
                gpg = (row["gf"] + 3 * 1.35) / (row["p"] + 3)
                out["players"][side] = player_chances(L["players"].get(tid, []), row["p"], xg, gpg, L["conv"])[:12]
                out.setdefault("injured", {})[side] = [p["name"] for p in L["players"].get(tid, []) if p["injured"]]
        return out
