"""StatPredict web app: JSON API plus the static front end."""
import datetime as dt
import hashlib
import json
import logging
import os
import threading
import time
from urllib.parse import urlparse

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from . import awards, db, nba_ingest, nba_model as M, soccer
from .service import NBAState, today_et

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("statpredict")

ROOT = os.path.dirname(os.path.dirname(__file__))
STATIC = os.path.join(ROOT, "static")
IMG_DIR = os.path.join(db.DATA_DIR, "img")
REFRESH_SECONDS = int(os.environ.get("STATPREDICT_REFRESH_SECONDS", "300"))
SOCCER_EVERY = 6  # refresh football every 6th cycle (30 minutes)
SESSION_URL = os.environ.get("STATPREDICT_SESSION_URL", "")

app = FastAPI(title="StatPredict", docs_url=None, redoc_url=None)
app.add_middleware(GZipMiddleware, minimum_size=800)


class State:
    nba = None      # NBAState once the first build finishes
    soccer = None   # soccer.SoccerState
    error: str | None = None
    building = False
    progress = 0


STATE = State()


def asset_version() -> str:
    """Changes whenever the front end on disk changes, so open pages know to reload themselves."""
    return "-".join(str(int(os.stat(os.path.join(STATIC, n)).st_mtime)) for n in ("index.html", "app.js", "styles.css"))
_refresh_lock = threading.Lock()


def _build_nba():
    STATE.building = True
    try:
        def prog(n):
            STATE.progress = n
        STATE.nba = NBAState(progress=prog)
    finally:
        STATE.building = False


def _build_soccer():
    prev = STATE.soccer.cache if STATE.soccer else None
    STATE.soccer = soccer.SoccerState(db.conn(), cache=prev)


def _has(table: str) -> bool:
    return db.conn().execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None


def refresh_once(cycle: int = 0) -> None:
    with _refresh_lock:
        try:
            nba_ingest.refresh()
            _build_nba()
            STATE.error = None
        except Exception as ex:
            log.exception("NBA refresh failed")
            STATE.error = f"NBA refresh failed: {ex}"
        if cycle % SOCCER_EVERY == 0 or STATE.soccer is None:
            try:
                soccer.refresh()
                _build_soccer()
            except Exception:
                log.exception("football refresh failed")


def _loop():
    db.init()
    try:  # serve what is already stored straight away, then refresh
        if _has("nba_player_games"):
            _build_nba()
        if _has("soc_matches"):
            _build_soccer()
    except Exception:
        log.exception("initial build failed")
    cycle, last = 0, 0.0
    while True:
        # Wall-clock check on a short tick: after the machine wakes from sleep the data refreshes at once.
        if time.time() - last >= REFRESH_SECONDS:
            refresh_once(cycle)
            last = time.time()
            cycle += 1
            if cycle == 1:
                try:
                    if nba_ingest.backfill_shots((db.get_meta("nba_season") or 0) - 1):
                        _build_nba()
                except Exception:
                    log.exception("shot backfill failed")
        time.sleep(10)


@app.on_event("startup")
def _startup():
    if os.environ.get("STATPREDICT_NO_REFRESH"):
        db.init()
        _build_nba()
        _build_soccer()
        return
    threading.Thread(target=_loop, daemon=True, name="refresh").start()


def nba() -> NBAState:
    if STATE.nba is None:
        raise HTTPException(503, "Data is still loading. This takes a few minutes on first start.")
    return STATE.nba


# ---------- serialisation ----------

def team_out(S: NBAState, tid: str) -> dict:
    t = S.teams.get(tid) or {}
    return {"id": tid, "abbr": t.get("abbr"), "name": t.get("name"), "short": t.get("short"),
            "logo": t.get("logo"), "record": t.get("record")}


def line_out(S: NBAState, line: dict) -> dict:
    stats = {}
    played = line.get("act") is not None
    for i, s in enumerate(M.STATS):
        m = line["proj"][i]
        lo, hi = M.count_range(m, M.variance(S.P, i, m))
        d = {"p": round(m, 1), "lo": lo, "hi": hi}
        if played:
            d["a"] = line["act"][i]
        stats[s] = d
    out = {"pid": line["pid"], "name": line["name"], "pos": line["pos"], "flag": line.get("flag"),
           "min": line["min"], "stats": stats, "gp": line.get("gp", 0),
           "avg": {s: round(line["avg"][i], 1) for i, s in enumerate(M.STATS)},
           "l5": {s: round(line["l5"][i], 1) for i, s in enumerate(M.STATS)}}
    if "dressed" in line:
        out["played"] = played
        out["act_min"] = line.get("act_min")
    return out


def game_out(S: NBAState, g: dict, top: int = 2) -> dict:
    pred, source, locked_at = S.prediction(g["id"])
    d = {
        "id": g["id"], "date": g["date"], "day": g["day"], "state": g["state"],
        "completed": bool(g["completed"]), "status": g["status_detail"], "venue": g["venue"],
        "home": team_out(S, g["home_id"]), "away": team_out(S, g["away_id"]),
        "score": None if g["home_score"] is None else {"home": g["home_score"], "away": g["away_score"]},
        "market": {"spread": g["spread"], "over_under": g["over_under"]},
        "source": source, "locked_at": locked_at, "pred": None,
    }
    if pred:
        d["pred"] = {k: pred[k] for k in ("home_pts", "away_pts", "margin", "total", "p_home")}
        d["top"] = {side: [{"pid": l["pid"], "name": l["name"], "pts": round(l["proj"][0], 1)}
                           for l in sorted(pred["players"][side], key=lambda l: -l["proj"][0])[:top]]
                    for side in ("home", "away")}
    return d


# ---------- API ----------

@app.get("/api/status")
def status():
    S = STATE.nba
    return {
        "ready": S is not None, "building": STATE.building, "error": STATE.error,
        "ingest": nba_ingest.PROGRESS, "progress": STATE.progress,
        "built_at": None if S is None else S.built_at,
        "nba": None if S is None else {
            "season": S.season, "refreshed_at": S.refreshed_at, "built_at": S.built_at,
            "games_completed": len(S.backtests), "today": today_et(),
        },
        "football": None if STATE.soccer is None else {"refreshed_at": db.get_meta("soc_refreshed_at")},
        "session_url": SESSION_URL, "version": asset_version(), "refresh_seconds": REFRESH_SECONDS,
    }


def game_full(S: NBAState, g: dict) -> dict:
    out = game_out(S, g, top=3)
    pred, _, _ = S.prediction(g["id"])
    if pred:
        out["pred"]["model_margin"] = pred.get("model_margin")
        out["pred"]["line_used"] = pred.get("line_used", False)
        out["detail"] = {
            "rest": pred.get("rest"), "record": pred.get("record"),
            "opp_factors": {side: dict(zip(M.STATS, pred["oppf"][side])) for side in ("home", "away")},
            "players": {side: [line_out(S, l) for l in pred["players"][side]] for side in ("home", "away")},
            "out": pred.get("out") or {"home": [], "away": []},
        }
    return out


def day_bundle(S: NBAState, day: str | None) -> dict:
    """Everything for one date in a single response: the date strip, games and every player line."""
    today = today_et()
    days = [{"day": d, "games": v[0], "completed": v[1]} for d, v in sorted(S.days.items())]
    upcoming = [d["day"] for d in days if d["day"] >= today and d["completed"] < d["games"]]
    default = upcoming[0] if upcoming else (days[-1]["day"] if days else today)
    try:
        day = dt.date.fromisoformat(day or "").isoformat()
    except ValueError:      # missing or malformed: fall back to the next date with games
        day = default
    games = sorted((g for g in S.data.games.values() if g["day"] == day and g["season"] == S.season),
                   key=lambda g: (g["date"], g["id"]))
    return {"day": day, "days": days, "default": default, "today": today, "season": S.season,
            "games": [game_full(S, g) for g in games]}


@app.get("/api/nba/day")
def nba_day(day: str | None = None):
    return day_bundle(nba(), day)


@app.get("/api/nba/game/{gid}")
def nba_game(gid: str):
    S = nba()
    g = S.data.games.get(gid)
    if not g:
        raise HTTPException(404, "Game not found")
    return game_full(S, g)


@app.get("/api/nba/players")
def nba_player_index():
    S = nba()
    seen, rows = set(), []
    for p in S.players.values():
        seen.add(p["id"])
        rows.append({"id": p["id"], "name": p["name"], "pos": p["pos"],
                     "team": (S.teams.get(p["team_id"]) or {}).get("abbr")})
    for pid, hist in S.data.rows_by_player.items():
        if pid not in seen and hist and hist[-1].season == S.season:
            rows.append({"id": pid, "name": hist[-1].name, "pos": hist[-1].pos,
                         "team": (S.teams.get(hist[-1].tid) or {}).get("abbr")})
    rows.sort(key=lambda r: r["name"] or "")
    return {"players": rows}


def _avg(rows) -> dict | None:
    rows = [r for r in rows if r.played]
    if not rows:
        return None
    n = len(rows)
    out = {s: round(sum(r.v[i] for r in rows) / n, 1) for i, s in enumerate(M.STATS)}
    out["min"] = round(sum(r.min for r in rows) / n, 1)
    out["gp"] = n
    return out


@app.get("/api/nba/player/{pid}")
def nba_player(pid: str):
    S = nba()
    info = S.players.get(pid)
    hist = S.data.rows_by_player.get(pid, [])
    if not info and not hist:
        raise HTTPException(404, "Player not found")
    tid = (info or {}).get("team_id") or (hist[-1].tid if hist else None)
    cur = [r for r in hist if r.season == S.season]
    prev = [r for r in hist if r.season == S.season - 1]
    played = [r for r in cur if r.played]
    proj_by_game = {gid: line for gid, line in S.by_player.get(pid, [])}

    def log_row(r):
        g = S.data.games[r.gid]
        line = proj_by_game.get(r.gid)
        return {
            "game_id": r.gid, "day": r.day, "season": r.season, "opp": (S.teams.get(r.opp) or {}).get("abbr"),
            "home": r.home, "played": r.played, "starter": r.starter, "min": r.min,
            "stats": dict(zip(M.STATS, r.v)),
            "won": (g["home_score"] > g["away_score"]) == r.home if g["home_score"] is not None else None,
            "proj": None if not line or line["min"] < 1 else {s: round(line["proj"][i], 1) for i, s in enumerate(M.STATS)},
            "proj_min": line["min"] if line else None,
        }

    log_rows = [log_row(r) for r in reversed(hist[-45:])]
    played_history = [r for r in hist if r.played]
    profile_season = played_history[-1].season if played_history else S.season
    profile_games = [r for r in played_history if r.season == profile_season]
    compare = []
    for r in cur:
        if not r.played:
            continue
        pred, src, _ = S.prediction(r.gid)
        line = next((l for side in ("home", "away") for l in pred["players"][side] if l["pid"] == pid), None) if pred else None
        if not line or line["min"] < 1:
            continue
        rng = [M.count_range(line["proj"][i], M.variance(S.P, i, line["proj"][i])) for i in range(len(M.STATS))]
        compare.append({
            "game_id": r.gid, "day": r.day, "opp": (S.teams.get(r.opp) or {}).get("abbr"), "home": r.home,
            "locked": src == "locked", "proj": [round(x, 1) for x in line["proj"]], "act": list(r.v),
            "lo": [x[0] for x in rng], "hi": [x[1] for x in rng],
        })
    nxt = None
    upcoming = sorted((g for g in S.data.games.values()
                       if g["id"] in S.live and tid in (g["home_id"], g["away_id"])), key=lambda g: g["date"])
    if upcoming:
        g = upcoming[0]
        side = "home" if g["home_id"] == tid else "away"
        other = "away" if side == "home" else "home"
        pred = S.live[g["id"]]
        line = next((l for l in pred["players"][side] if l["pid"] == pid), None)
        nxt = {
            "game": game_out(S, g, top=0), "home": side == "home", "opp": team_out(S, g[f"{other}_id"]),
            "line": line_out(S, line) if line else None,
            "variance": None if not line else {s: round(M.variance(S.P, i, line["proj"][i]), 3)
                                               for i, s in enumerate(M.STATS)},
            "opp_factors": dict(zip(M.STATS, pred["oppf"][other])),
            "rest": (pred.get("rest") or {}).get(side),
        }
    inj = S.injuries.get(pid)
    return {
        "id": pid, "name": (info or {}).get("name") or (hist[-1].name if hist else None),
        "pos": (info or {}).get("pos") or (hist[-1].pos if hist else None),
        "jersey": (info or {}).get("jersey"), "age": (info or {}).get("age"),
        "height": (info or {}).get("height"), "weight": (info or {}).get("weight"),
        "experience": (info or {}).get("experience"), "headshot": (info or {}).get("headshot"),
        "team": team_out(S, tid) if tid else None, "season": S.season,
        "injury": None if not inj else {k: inj[k] for k in ("status", "kind", "comment", "return_date")},
        "averages": {
            "season": _avg(cur), "last5": _avg(played[-5:]), "last10": _avg(played[-10:]),
            "home": _avg([r for r in cur if r.home]), "away": _avg([r for r in cur if not r.home]),
            "previous": _avg([r for r in prev if S.data.games[r.gid]["stype"] == 2]),
            "playoffs": _avg([r for r in prev if S.data.games[r.gid]["stype"] == 3]),
        },
        "next": nxt, "log": log_rows, "compare": compare,
        "trend": {"season": profile_season, "average": _avg(profile_games),
                  "games": [log_row(r) for r in reversed(profile_games[-20:])]},
        "shooting": S.shots(pid, profile_season),
    }


@app.get("/api/nba/awards")
def nba_awards():
    S = nba()
    return {"season": S.season, "played": S.awards["played"], "awards": S.awards["awards"],
            "names": dict(awards.AWARDS), "history": S.awards_history}


@app.get("/api/nba/playoffs")
def nba_playoffs():
    S = nba()
    if not S.playoffs:
        raise HTTPException(503, "The season simulation is not ready yet.")
    return {"season": S.season, **S.playoffs, "history": S.playoffs_history,
            "home_edge": round(S.ctx.hca, 1)}


@app.get("/api/nba/teams")
def nba_teams():
    S = nba()
    return {"teams": S.team_ratings(), "league_avg": round(S.ctx.mu, 1), "home_edge": round(S.ctx.hca, 1),
            "stats": list(M.STATS)}


@app.get("/api/nba/accuracy")
def nba_accuracy():
    S = nba()
    tuned_on = None
    try:
        tuned_on = json.load(open(os.path.join(os.path.dirname(__file__), "params.json"))).get("tuned_on")
    except (OSError, ValueError):
        pass
    return {"season": S.season, "summary": S.summary,
            "previous": {"season": S.season - 1, "summary": S.prev_summary} if S.prev_summary else None,
            "locked": S.locked_record, "locked_games": S.locked_games(), "tuned_on": tuned_on,
            "line_weight": S.P.w_mkt_margin,
            "players": S.player_acc,
            }


def soc() -> soccer.SoccerState:
    if STATE.soccer is None:
        raise HTTPException(503, "Football data is still loading.")
    return STATE.soccer


@app.get("/api/football/leagues")
def football_leagues():
    S = soc()
    return {"leagues": [{"id": lg, "name": L["name"], "accuracy": L["accuracy"]} for lg, L in S.leagues.items()]}


@app.get("/api/football/league/{lg}")
def football_league(lg: str):
    v = soc().league_view(lg)
    if not v:
        raise HTTPException(404, "League not found")
    return v


@app.get("/api/football/match/{mid}")
def football_match(mid: str):
    v = soc().match_view(mid)
    if not v:
        raise HTTPException(404, "Match not found")
    return v


@app.get("/img")
def img(u: str = Query(..., max_length=400)):
    """Caches ESPN logos and headshots so the app does not depend on the viewer reaching ESPN's CDN."""
    parsed = urlparse(u)
    if parsed.scheme != "https" or parsed.hostname != "a.espncdn.com":
        raise HTTPException(400, "Unsupported image host")
    os.makedirs(IMG_DIR, exist_ok=True)
    path = os.path.join(IMG_DIR, hashlib.sha1(u.encode()).hexdigest())
    if not os.path.exists(path):
        try:
            r = requests.get(u, timeout=15)
        except requests.RequestException:
            raise HTTPException(502, "Image fetch failed")
        if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/"):
            raise HTTPException(404, "No image")
        tmp = f"{path}.{threading.get_ident()}.tmp"
        with open(tmp, "wb") as f:
            f.write(r.content)
        os.replace(tmp, path)
    with open(path, "rb") as f:
        body = f.read()
    kind = "image/png" if body[:4] == b"\x89PNG" else ("image/jpeg" if body[:2] == b"\xff\xd8" else "image/webp")
    return Response(body, media_type=kind, headers={"Cache-Control": "public, max-age=31536000, immutable"})


class NoCacheStatic(StaticFiles):
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/static", NoCacheStatic(directory=STATIC), name="static")


@app.get("/", response_class=HTMLResponse)
def index():
    """One response carries the page, its styles, its script and today's data, so first paint needs no second trip."""
    def read(name):
        return open(os.path.join(STATIC, name), encoding="utf-8").read()

    boot = {"session_url": SESSION_URL, "version": asset_version(), "refresh_seconds": REFRESH_SECONDS}
    if STATE.nba is not None:
        boot["day"] = day_bundle(STATE.nba, None)
        boot["refreshed_at"] = STATE.nba.refreshed_at
        boot["built_at"] = STATE.nba.built_at
    data = json.dumps(boot, separators=(",", ":")).replace("</", "<\\/")
    html = (read("index.html").replace("/*__CSS__*/", read("styles.css"))
            .replace("/*__BOOT__*/", "window.__BOOT__=" + data + ";")
            .replace("/*__JS__*/", read("app.js").replace("</script", "<\\/script")))
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/favicon.ico")
def favicon():
    return FileResponse(os.path.join(STATIC, "icon.svg"), media_type="image/svg+xml")
