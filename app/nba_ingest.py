"""Pull NBA schedule, box scores, rosters and injuries from ESPN into SQLite."""
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db, espn

log = logging.getLogger("statpredict.nba")
ET = ZoneInfo("America/New_York")
LEAGUE = "basketball/nba"
# Regular season, playoffs and play-in. Preseason and All-Star are left out of the model.
MODEL_STYPES = (2, 3, 5)
PROGRESS = {"stage": "idle", "done": 0, "total": 0}


def _to_day(iso: str) -> str:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return dt.astimezone(ET).strftime("%Y-%m-%d")


def _num(v, default=0.0):
    try:
        return float(str(v).replace("+", ""))
    except (TypeError, ValueError):
        return default


def _pair(v):
    try:
        a, b = str(v).split("-")
        return float(a), float(b)
    except ValueError:
        return 0.0, 0.0


def _home_line(odds: dict, home_abbr: str, away_abbr: str):
    """Point spread from the home team's side (negative means the home team is favoured)."""
    details = (odds or {}).get("details") or ""
    m = re.match(r"^([A-Z]+)\s+([+-]?\d+(?:\.\d+)?)$", details.strip())
    if m:
        line = -abs(float(m.group(2)))
        if m.group(1) == home_abbr:
            return line
        if m.group(1) == away_abbr:
            return -line
    if details.strip().upper() in ("EVEN", "PK", "PICK"):
        return 0.0
    return None


def sync_teams() -> None:
    data = espn.get(f"{LEAGUE}/teams", {"limit": 50})
    c = db.conn()
    for t in data["sports"][0]["leagues"][0]["teams"]:
        t = t["team"]
        logo = (t.get("logos") or [{}])[0].get("href")
        c.execute(
            "INSERT INTO nba_teams(id,abbr,name,short,color,logo) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET abbr=excluded.abbr,name=excluded.name,short=excluded.short,"
            "color=excluded.color,logo=excluded.logo",
            (t["id"], t["abbreviation"], t["displayName"], t.get("shortDisplayName"), t.get("color"), logo),
        )
    c.commit()


def sync_month(yyyymm: str) -> int:
    data = espn.get(f"{LEAGUE}/scoreboard", {"dates": yyyymm, "limit": 600})
    c = db.conn()
    team_ids = {r["id"] for r in c.execute("SELECT id FROM nba_teams")}
    n = 0
    for e in data.get("events", []):
        comp = e["competitions"][0]
        home = next((x for x in comp["competitors"] if x["homeAway"] == "home"), None)
        away = next((x for x in comp["competitors"] if x["homeAway"] == "away"), None)
        if not home or not away:
            continue
        if home["team"]["id"] not in team_ids or away["team"]["id"] not in team_ids:
            continue  # All-Star and exhibition sides
        st = comp["status"]["type"]
        odds = (comp.get("odds") or [None])[0]
        spread = _home_line(odds, home["team"].get("abbreviation"), away["team"].get("abbreviation"))
        ou = (odds or {}).get("overUnder")
        hs = int(_num(home.get("score"))) if st["state"] != "pre" else None
        as_ = int(_num(away.get("score"))) if st["state"] != "pre" else None
        c.execute(
            "INSERT INTO nba_games(id,date,day,season,stype,state,completed,status_detail,home_id,away_id,"
            "home_score,away_score,spread,over_under,venue) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET date=excluded.date,day=excluded.day,state=excluded.state,"
            "completed=excluded.completed,status_detail=excluded.status_detail,home_score=excluded.home_score,"
            "away_score=excluded.away_score,spread=COALESCE(excluded.spread,nba_games.spread),"
            "over_under=COALESCE(excluded.over_under,nba_games.over_under),venue=excluded.venue",
            (
                e["id"], e["date"], _to_day(e["date"]), e["season"]["year"], e["season"]["type"],
                st["state"], 1 if st.get("completed") else 0, st.get("shortDetail"),
                home["team"]["id"], away["team"]["id"], hs, as_, spread, ou,
                (comp.get("venue") or {}).get("fullName"),
            ),
        )
        for side in (home, away):
            rec = next((r.get("summary") for r in side.get("records", []) if r.get("type") == "total"), None)
            if rec and e["season"]["year"] == data["leagues"][0]["season"]["year"] and st["state"] != "post":
                c.execute("UPDATE nba_teams SET record=? WHERE id=?", (rec, side["team"]["id"]))
        n += 1
    c.commit()
    return n


def _parse_box(game_id: str, data: dict) -> tuple[list[tuple], dict]:
    rows = []
    box = data.get("boxscore") or {}
    teams = [t["team"]["id"] for t in box.get("players", [])]
    for t in box.get("players", []):
        tid = t["team"]["id"]
        opp = next((x for x in teams if x != tid), None)
        for block in t.get("statistics", []):
            labels = block.get("labels") or []
            idx = {lab: i for i, lab in enumerate(labels)}
            for a in block.get("athletes", []):
                st = a.get("stats") or []

                def g(label):
                    i = idx.get(label)
                    return st[i] if i is not None and i < len(st) else None

                mins = _num(g("MIN"))
                fgm, fga = _pair(g("FG"))
                f3m, f3a = _pair(g("3PT"))
                ftm, fta = _pair(g("FT"))
                ath = a.get("athlete") or {}
                if not ath.get("id"):
                    continue
                rows.append((
                    game_id, ath["id"], tid, opp, ath.get("displayName"),
                    (ath.get("position") or {}).get("abbreviation"),
                    1 if a.get("starter") else 0, 1 if mins > 0 else 0, mins,
                    _num(g("PTS")), _num(g("REB")), _num(g("AST")), _num(g("STL")), _num(g("BLK")),
                    _num(g("TO")), f3m, f3a, fgm, fga, ftm, fta, _num(g("+/-")),
                ))
    pick = (data.get("pickcenter") or [None])[0] or {}
    odds = {"spread": pick.get("spread"), "over_under": pick.get("overUnder")}
    return rows, odds


def _parse_shots(game_id: str, data: dict) -> list[tuple]:
    """Field-goal attempts with court positions. x runs 0-50 ft across the court, y is feet out from the hoop."""
    out = []
    for p in data.get("plays") or []:
        if not p.get("shootingPlay") or p.get("pointsAttempted") not in (2, 3):
            continue
        co = p.get("coordinate") or {}
        x, y = co.get("x"), co.get("y")
        if x is None or y is None or abs(x) > 200 or abs(y) > 200:
            continue
        who = p.get("participants") or []
        pid = ((who[0] if who else {}).get("athlete") or {}).get("id")
        if not pid:
            continue
        out.append((
            game_id, str(p.get("sequenceNumber") or p.get("id")), pid, (p.get("team") or {}).get("id"),
            float(x), float(y), 1 if p.get("scoringPlay") else 0, int(p["pointsAttempted"]),
            (p.get("period") or {}).get("number"), (p.get("type") or {}).get("text"),
            1 if len(who) > 1 and p.get("scoringPlay") else 0,
        ))
    return out


def _store_shots(c, game_id: str, data: dict) -> None:
    shots = _parse_shots(game_id, data)
    c.execute("DELETE FROM nba_shots WHERE game_id=?", (game_id,))
    c.executemany("INSERT OR REPLACE INTO nba_shots VALUES(?,?,?,?,?,?,?,?,?,?,?)", shots)
    c.execute("UPDATE nba_games SET shots=? WHERE id=?", (1 if shots else -1, game_id))


def backfill_shots(min_season: int, workers: int = 8) -> int:
    """Shot locations for games stored before shot charts existed."""
    c = db.conn()
    ids = [r["id"] for r in c.execute(
        "SELECT id FROM nba_games WHERE box=1 AND COALESCE(shots,0)=0 AND season>=? ORDER BY date DESC", (min_season,))]
    PROGRESS.update(stage="shot charts", done=0, total=len(ids))

    def one(gid):
        try:
            return gid, espn.get(f"{LEAGUE}/summary", {"event": gid})
        except Exception as ex:
            log.warning("shots %s failed: %s", gid, ex)
            return gid, None

    n = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for gid, data in pool.map(one, ids):
            PROGRESS["done"] += 1
            if data:
                _store_shots(c, gid, data)
                n += 1
                if n % 40 == 0:
                    c.commit()
    c.commit()
    return n


def sync_box(game_id: str) -> bool:
    data = espn.get(f"{LEAGUE}/summary", {"event": game_id})
    rows, odds = _parse_box(game_id, data)
    if not any(r[7] for r in rows):
        return False
    c = db.conn()
    c.executemany(
        "INSERT OR REPLACE INTO nba_player_games(game_id,player_id,team_id,opp_id,name,pos,starter,played,min,"
        "pts,reb,ast,stl,blk,tov,fg3m,fg3a,fgm,fga,ftm,fta,pm) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    c.execute(
        "UPDATE nba_games SET box=1, spread=COALESCE(?,spread), over_under=COALESCE(?,over_under) WHERE id=?",
        (odds["spread"], odds["over_under"], game_id),
    )
    _store_shots(c, game_id, data)
    c.commit()
    return True


def sync_boxes(workers: int = 8) -> int:
    c = db.conn()
    ids = [r["id"] for r in c.execute(
        f"SELECT id FROM nba_games WHERE completed=1 AND box=0 AND stype IN ({','.join(map(str, MODEL_STYPES))}) "
        "ORDER BY date")]
    PROGRESS.update(stage="box scores", done=0, total=len(ids))
    ok = 0

    def one(gid):
        try:
            return sync_box(gid)
        except Exception as ex:  # one bad game must not stop the rest
            log.warning("box %s failed: %s", gid, ex)
            if "HTTP 404" in str(ex):
                # ESPN has no box score for a few old games; stop asking once the game is a week old.
                db.conn().execute(
                    "UPDATE nba_games SET box=-1 WHERE id=? AND date < ?",
                    (gid, (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%MZ")))
                db.conn().commit()
            return False

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for res in pool.map(one, ids):
            PROGRESS["done"] += 1
            ok += 1 if res else 0
    return ok


def sync_rosters() -> None:
    c = db.conn()
    teams = [r["id"] for r in c.execute("SELECT id FROM nba_teams")]

    def one(tid):
        try:
            return tid, espn.get(f"{LEAGUE}/teams/{tid}/roster")
        except Exception as ex:
            log.warning("roster %s failed: %s", tid, ex)
            return tid, None

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(one, teams))
    if sum(1 for _, d in results if d) < len(teams) * 0.8:
        return
    for tid, data in results:
        if not data:
            continue
        c.execute("UPDATE nba_players SET team_id=NULL WHERE team_id=?", (tid,))
        for a in data.get("athletes", []):
            c.execute(
                "INSERT INTO nba_players(id,name,short,pos,team_id,jersey,headshot,age,height,weight,experience) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,short=excluded.short,"
                "pos=excluded.pos,team_id=excluded.team_id,jersey=excluded.jersey,headshot=excluded.headshot,"
                "age=excluded.age,height=excluded.height,weight=excluded.weight,experience=excluded.experience",
                (
                    a["id"], a.get("displayName"), a.get("shortName"),
                    (a.get("position") or {}).get("abbreviation"), tid, a.get("jersey"),
                    (a.get("headshot") or {}).get("href"), a.get("age"), a.get("displayHeight"),
                    a.get("displayWeight"), (a.get("experience") or {}).get("years"),
                ),
            )
        team = data.get("team") or {}
        c.execute("UPDATE nba_teams SET record=COALESCE(?,record), standing=? WHERE id=?",
                  (team.get("recordSummary"), team.get("standingSummary"), tid))
    c.commit()
    db.set_meta("nba_rosters_at", time.time())


def sync_injuries() -> None:
    data = espn.get(f"{LEAGUE}/injuries")
    rows = []
    for t in data.get("injuries", []):
        for x in t.get("injuries", []):
            ath = x.get("athlete") or {}
            href = next((l.get("href", "") for l in ath.get("links", [])), "")
            m = re.search(r"/id/(\d+)", href)
            if not m:
                continue
            det = x.get("details") or {}
            kind = " ".join(p for p in (det.get("side"), det.get("type"), det.get("detail")) if p and p != "Not Specified")
            rows.append((
                m.group(1), t.get("id"), ath.get("displayName"), x.get("status"),
                (det.get("fantasyStatus") or {}).get("abbreviation"), kind,
                x.get("shortComment"), det.get("returnDate"), x.get("date"),
            ))
    c = db.conn()
    c.execute("DELETE FROM nba_injuries")
    c.executemany("INSERT OR REPLACE INTO nba_injuries VALUES(?,?,?,?,?,?,?,?,?)", rows)
    c.commit()
    db.set_meta("nba_injuries_at", time.time())


def _months(start: datetime, end: datetime) -> list[str]:
    out, cur = [], datetime(start.year, start.month, 1)
    while cur <= end:
        out.append(cur.strftime("%Y%m"))
        cur = datetime(cur.year + (cur.month == 12), cur.month % 12 + 1, 1)
    return out


def current_season() -> int:
    data = espn.get(f"{LEAGUE}/scoreboard")
    year = data["leagues"][0]["season"]["year"]
    db.set_meta("nba_season", year)
    return year


def refresh(history_seasons: int = 2) -> dict:
    """Incremental refresh. The first run also backfills earlier seasons, which the model uses as a prior."""
    db.init()
    PROGRESS.update(stage="schedule", done=0, total=0)
    sync_teams()
    season = current_season()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for back in range(history_seasons, 0, -1):
        yr = season - back
        if not db.get_meta(f"nba_hist_{yr}"):
            for m in _months(datetime(yr - 1, 10, 1), datetime(yr, 6, 30)):
                sync_month(m)
            db.set_meta(f"nba_hist_{yr}", True)
    if not db.get_meta(f"nba_sched_{season}"):
        months = _months(datetime(season - 1, 10, 1), datetime(season, 6, 30))
        db.set_meta(f"nba_sched_{season}", True)
    else:
        months = _months(now - timedelta(days=3), now + timedelta(days=12))
    for m in months:
        sync_month(m)
    new_boxes = sync_boxes()
    PROGRESS.update(stage="rosters and injuries", done=0, total=0)
    if time.time() - (db.get_meta("nba_rosters_at") or 0) > 3 * 3600:
        sync_rosters()
    sync_injuries()
    db.set_meta("nba_refreshed_at", time.time())
    PROGRESS.update(stage="idle")
    return {"season": season, "new_boxes": new_boxes}
