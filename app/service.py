"""Builds the in-memory view the API serves: live predictions, locked snapshots, accuracy."""
import hashlib
import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import asdict, fields
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import awards, db, nba_model as M, playoffs

log = logging.getLogger("statpredict.service")
ET = ZoneInfo("America/New_York")
PARAMS_FILE = os.path.join(os.path.dirname(__file__), "params.json")
OUT_STATUSES = {"OUT", "OFS"}


def load_params() -> M.Params:
    P = M.Params()
    if os.path.exists(PARAMS_FILE):
        raw = json.load(open(PARAMS_FILE))
        names = {f.name for f in fields(M.Params)}
        vals = {k: (tuple(v) if isinstance(v, list) else v) for k, v in raw.items() if k in names}
        P = M.Params(**vals)
    return P


def params_version(P: M.Params) -> str:
    h = hashlib.sha1(json.dumps(asdict(P), sort_keys=True).encode()).hexdigest()[:8]
    return f"{M.MODEL_VERSION}-{h}"


ZONES = {"rim": "At the rim", "paint": "Paint (non-rim)", "mid": "Mid-range",
         "corner3": "Corner three", "above3": "Above-the-break three"}


def zone_of(x: float, y: float, value: int) -> str:
    """x is feet across the court (hoop at 25), y is feet out from the hoop."""
    if value == 3:
        return "corner3" if y <= 9 else "above3"
    if (x - 25) ** 2 + y * y <= 16:
        return "rim"
    return "paint" if abs(x - 25) <= 8 and y <= 13.75 else "mid"


def today_et() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")


class NBAState:
    def __init__(self, progress=None):
        c = db.conn()
        self.P = load_params()
        self.version = params_version(self.P)
        self.data = data = M.NBAData(c)
        self.season = db.get_meta("nba_season") or (data.seasons[-1] if data.seasons else None)
        self.teams = {r["id"]: dict(r) for r in c.execute("SELECT * FROM nba_teams")}
        self.players = {r["id"]: dict(r) for r in c.execute("SELECT * FROM nba_players")}
        self.injuries = {r["player_id"]: dict(r) for r in c.execute("SELECT * FROM nba_injuries")}
        self.refreshed_at = db.get_meta("nba_refreshed_at")
        self.built_at = time.time()
        self.roster = defaultdict(list)
        for p in self.players.values():
            if p["team_id"]:
                self.roster[p["team_id"]].append(p)

        # Walk-forward predictions for every finished game this season (stored, so only new games are computed).
        season_ids = {g["id"] for g in data.completed if g["season"] == self.season}
        self.backtests = {}
        for r in c.execute("SELECT game_id, payload FROM nba_backtests WHERE version=?", (self.version,)):
            if r["game_id"] in season_ids:
                self.backtests[r["game_id"]] = json.loads(r["payload"])
        if season_ids - set(self.backtests):
            new = M.backtest(data, self.P, {self.season}, skip_ids=set(self.backtests), progress=progress)
            c.executemany("INSERT OR REPLACE INTO nba_backtests(game_id,version,payload) VALUES(?,?,?)",
                          [(gid, self.version, json.dumps(p)) for gid, p in new.items()])
            c.execute("DELETE FROM nba_backtests WHERE version<>?", (self.version,))
            c.commit()
            self.backtests.update(new)
        self.summary = M.summarize(data, self.P, self.backtests)
        key = f"nba_summary_{self.season - 1}_{self.version}" if self.season else None
        self.prev_summary = db.get_meta(key) if key else None
        if key and self.prev_summary is None and (self.season - 1) in data.seasons:
            self.prev_summary = M.summarize(data, self.P, M.backtest(data, self.P, {self.season - 1}))
            db.set_meta(key, self.prev_summary)

        # Live predictions for games that have not finished.
        builder = M.ContextBuilder(data, self.P)
        builder.advance("9999-12-31")
        self.ctx = builder.snapshot(self.season, iters=30) if self.season else None
        self.snapshots = {r["game_id"]: (r["created_at"], r["payload"])
                          for r in c.execute("SELECT * FROM nba_snapshots")}
        self.live = {}
        today = today_et()
        horizon = (datetime.now(ET) + timedelta(days=8)).strftime("%Y-%m-%d")
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        soon = (datetime.now(timezone.utc) + timedelta(hours=48)).strftime("%Y-%m-%dT%H:%MZ")
        for g in data.games.values():
            if g["season"] != self.season or g["completed"] or g["box"] == 1:
                continue
            if g["state"] == "post" or not (today <= g["day"] <= horizon or g["state"] == "in"):
                continue
            if g["state"] == "in" and g["id"] in self.snapshots:
                continue
            pred = M.project_game(data, self.ctx, self.P, g, self._cands(g["home_id"], g["day"]),
                                  self._cands(g["away_id"], g["day"]))
            pred["out"] = {"home": self._out(g["home_id"], g["day"]), "away": self._out(g["away_id"], g["day"])}
            self.live[g["id"]] = pred
            if g["state"] == "pre" and g["date"] <= soon:
                payload = json.dumps(pred)
                c.execute("INSERT OR REPLACE INTO nba_snapshots(game_id,created_at,payload) VALUES(?,?,?)",
                          (g["id"], now_iso, payload))
                self.snapshots[g["id"]] = (now_iso, payload)
        c.commit()
        self._snap_cache = {}
        self._build_awards()
        self._build_playoffs()
        self._build_zones(c)

        self.by_player = defaultdict(list)
        for gid, p in self.backtests.items():
            for side in ("home", "away"):
                for line in p["players"][side]:
                    self.by_player[line["pid"]].append((gid, line))
        self.days = defaultdict(lambda: [0, 0])
        for g in data.games.values():
            if g["season"] == self.season:
                self.days[g["day"]][0] += 1
                self.days[g["day"]][1] += 1 if g["completed"] else 0
        self.locked_record = self._locked_record()
        self.player_acc = self.player_accuracy()

    def _build_awards(self) -> None:
        self.awards = awards.compute(self.data, self.P, self.season, self.teams, self.players)
        today = today_et()
        key = f"nba_awards_hist_{awards.VERSION}_{self.version}_{today}"
        self.awards_history = db.get_meta(key)
        if self.awards_history is None:
            self.awards_history = awards.history(self.data, self.P, self.teams, self.players, self.season, today[5:])
            db.conn().execute("DELETE FROM meta WHERE key LIKE 'nba_awards_hist_%'")
            db.set_meta(key, self.awards_history)

    def _build_playoffs(self) -> None:
        self.playoffs, self.playoffs_history = None, []
        if not self.season or not self.ctx:
            return
        games = [g for g in self.data.games.values() if g["season"] == self.season and g["stype"] == 2]
        played = sum(1 for g in games if g["completed"])
        today = today_et()
        key = f"nba_playoffs_{playoffs.VERSION}_{self.version}_{today}_{played}"
        hkey = f"nba_playoffs_hist_{playoffs.VERSION}_{self.version}_{today}"
        self.playoffs, self.playoffs_history = db.get_meta(key), db.get_meta(hkey)
        if self.playoffs is None:
            net = {t: self.ctx.off[t] - self.ctx.dfa[t] for t in self.teams if t in self.ctx.off}
            self.playoffs = playoffs.simulate(self.teams, games, net, self.ctx.hca, self.P.sigma_margin)
            if self.playoffs:
                self.playoffs["postseason"] = any(
                    g["season"] == self.season and g["stype"] in (3, 5) for g in self.data.games.values())
            db.conn().execute("DELETE FROM meta WHERE key LIKE 'nba_playoffs_%' AND key NOT LIKE 'nba_playoffs_hist_%'")
            db.set_meta(key, self.playoffs)
        if self.playoffs_history is None:
            self.playoffs_history = playoffs.history(self.data, self.P, self.teams, self.season, today[5:],
                                                     M.ContextBuilder)
            db.conn().execute("DELETE FROM meta WHERE key LIKE 'nba_playoffs_hist_%'")
            db.set_meta(hkey, self.playoffs_history)

    def player_accuracy(self, min_games: int = 5, top: int = 150) -> list[dict]:
        """Projected against actual points for each regular, from the walk-forward predictions."""
        rows = []
        for pid, lines in self.by_player.items():
            both = [(l["proj"], l["act"]) for _, l in lines if l.get("act") and l["min"] >= 1]
            if len(both) < min_games:
                continue
            n = len(both)
            info = self.players.get(pid) or {}
            name = info.get("name") or next((l["name"] for _, l in lines), None)
            row = {"pid": pid, "name": name, "team": (self.teams.get(info.get("team_id")) or {}).get("abbr"), "gp": n}
            for k, i in (("pts", 0), ("reb", 1), ("ast", 2)):
                row[k] = {"pred": round(sum(p[i] for p, _ in both) / n, 1), "act": round(sum(a[i] for _, a in both) / n, 1),
                          "miss": round(sum(abs(p[i] - a[i]) for p, a in both) / n, 2)}
            rows.append(row)
        rows.sort(key=lambda r: -r["pts"]["act"])
        return rows[:top]

    def _build_zones(self, c) -> None:
        """League accuracy by court zone this season, to compare each player against."""
        n = c.execute("SELECT COUNT(*) n FROM nba_shots s JOIN nba_games g ON g.id=s.game_id WHERE g.season=?",
                      (self.season,)).fetchone()["n"]
        cached = db.get_meta("nba_zone_avg")
        if cached and cached.get("n") == n:
            self.zone_avg = cached["zones"]
            return
        tot = {z: [0, 0] for z in ZONES}
        for r in c.execute("SELECT s.x, s.y, s.made, s.value FROM nba_shots s JOIN nba_games g ON g.id=s.game_id "
                           "WHERE g.season=?", (self.season,)):
            z = tot[zone_of(r["x"], r["y"], r["value"])]
            z[0] += r["made"]
            z[1] += 1
        self.zone_avg = {z: round(v[0] / v[1], 4) if v[1] else None for z, v in tot.items()}
        db.set_meta("nba_zone_avg", {"n": n, "zones": self.zone_avg})

    def shots(self, pid: str) -> dict:
        rows = db.conn().execute(
            "SELECT s.x, s.y, s.made, s.value, g.day FROM nba_shots s JOIN nba_games g ON g.id=s.game_id "
            "WHERE s.player_id=? AND g.season=? ORDER BY g.date", (pid, self.season)).fetchall()
        zones = {z: [0, 0] for z in ZONES}
        pts = []
        for r in rows:
            z = zones[zone_of(r["x"], r["y"], r["value"])]
            z[0] += r["made"]
            z[1] += 1
            pts.append([r["x"], r["y"], r["made"], r["value"]])
        total = len(pts)
        return {
            "shots": pts, "games": len({r["day"] for r in rows}),
            "zones": [{"zone": z, "label": ZONES[z], "made": v[0], "att": v[1],
                       "pct": round(v[0] / v[1], 3) if v[1] else None,
                       "share": round(v[1] / total, 3) if total else 0,
                       "league": self.zone_avg.get(z), "value": 3 if z in ("corner3", "above3") else 2}
                      for z, v in zones.items()],
        }

    # ----- availability -----

    def _is_out(self, pid: str, day: str) -> bool:
        inj = self.injuries.get(pid)
        if not inj:
            return False
        out = (inj.get("fstatus") or "").upper() in OUT_STATUSES or inj.get("status") in ("Out", "Suspension")
        if out and inj.get("return_date") and inj["return_date"] <= day:
            return False
        return out

    def _cands(self, tid: str, day: str) -> list[dict]:
        out = []
        for p in self.roster.get(tid, []):
            if self._is_out(p["id"], day):
                continue
            inj = self.injuries.get(p["id"])
            out.append({"pid": p["id"], "name": p["name"], "pos": p["pos"],
                        "flag": "questionable" if inj else None})
        return out

    def _out(self, tid: str, day: str) -> list[dict]:
        rows = []
        for p in self.roster.get(tid, []):
            inj = self.injuries.get(p["id"])
            if not inj:
                continue
            rows.append({"pid": p["id"], "name": p["name"], "out": self._is_out(p["id"], day),
                         "status": inj["status"], "kind": inj["kind"], "comment": inj["comment"],
                         "return_date": inj["return_date"]})
        rows.sort(key=lambda r: (not r["out"], r["name"]))
        return rows

    # ----- lookups -----

    def prediction(self, gid: str):
        """Returns (prediction, source, locked_at). Source: live, locked or backtest."""
        g = self.data.games.get(gid)
        if not g:
            return None, None, None
        if gid in self.live and g["state"] == "pre":
            return self.live[gid], "live", None
        if gid in self.snapshots:
            if gid not in self._snap_cache:
                pred = json.loads(self.snapshots[gid][1])
                if g["box"] == 1:
                    M.attach_actuals(self.data, g, pred)
                self._snap_cache[gid] = pred
            return self._snap_cache[gid], "locked", self.snapshots[gid][0]
        if gid in self.backtests:
            return self.backtests[gid], "backtest", None
        if gid in self.live:
            return self.live[gid], "live", None
        return None, None, None

    def _locked_record(self) -> dict:
        n = hit = ln = lhit = 0
        for gid in self.snapshots:
            g = self.data.games.get(gid)
            if not g or not g["completed"] or g["home_score"] is None or g["home_score"] == g["away_score"]:
                continue
            pred, _, _ = self.prediction(gid)
            home_won = g["home_score"] > g["away_score"]
            n += 1
            hit += (pred["p_home"] >= 0.5) == home_won
            if g["spread"]:     # negative means the home team is favoured; a pick'em has no favourite
                ln += 1
                lhit += (g["spread"] < 0) == home_won
        return {"games": n, "correct": hit, "line_games": ln, "line_correct": lhit}

    def locked_games(self, limit: int = 40) -> list[dict]:
        rows = []
        for gid in self.snapshots:
            g = self.data.games.get(gid)
            if not g or not g["completed"] or g["home_score"] is None:
                continue
            pred, _, _ = self.prediction(gid)
            rows.append({
                "id": gid, "day": g["day"], "date": g["date"],
                "home": self.teams.get(g["home_id"], {}).get("abbr"), "away": self.teams.get(g["away_id"], {}).get("abbr"),
                "pred": [pred["away_pts"], pred["home_pts"]], "actual": [g["away_score"], g["home_score"]],
                "p_home": pred["p_home"], "spread": g["spread"],
                "hit": (pred["p_home"] >= 0.5) == (g["home_score"] > g["away_score"]),
            })
        rows.sort(key=lambda r: r["date"], reverse=True)
        return rows[:limit]

    def team_ratings(self) -> list[dict]:
        rows = []
        ctx = self.ctx
        for tid, t in self.teams.items():
            if tid not in ctx.off:
                continue
            rec = ctx.record.get(tid, (0, 0))
            rows.append({
                "id": tid, "abbr": t["abbr"], "name": t["name"], "logo": t["logo"],
                "w": rec[0], "l": rec[1], "off": round(ctx.off[tid], 1), "def": round(-ctx.dfa[tid], 1),
                "net": round(ctx.off[tid] - ctx.dfa[tid], 1),
                "ppg": round(ctx.teamfor[tid][0], 1) if tid in ctx.teamfor else None,
                "allowed": [round(x, 3) for x in ctx.oppf.get(tid, [])],
            })
        rows.sort(key=lambda r: -r["net"])
        return rows
