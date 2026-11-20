"""SQLite storage. The database lives outside the code directory so redeploys keep it."""
import gzip
import json
import os
import shutil
import sqlite3
import threading

DATA_DIR = os.environ.get("STATPREDICT_DATA_DIR", os.path.expanduser("~/statpredict-data"))
DB_PATH = os.path.join(DATA_DIR, "statpredict.db")
SEED_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "seed", "statpredict.db.gz")
_seed_lock = threading.Lock()

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS nba_teams (
  id TEXT PRIMARY KEY, abbr TEXT, name TEXT, short TEXT, color TEXT, logo TEXT, record TEXT, standing TEXT
);
CREATE TABLE IF NOT EXISTS nba_games (
  id TEXT PRIMARY KEY, date TEXT, day TEXT, season INTEGER, stype INTEGER,
  state TEXT, completed INTEGER, status_detail TEXT,
  home_id TEXT, away_id TEXT, home_score INTEGER, away_score INTEGER,
  spread REAL, over_under REAL, venue TEXT, box INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS nba_games_day ON nba_games(day);
CREATE TABLE IF NOT EXISTS nba_player_games (
  game_id TEXT, player_id TEXT, team_id TEXT, opp_id TEXT, name TEXT, pos TEXT,
  starter INTEGER, played INTEGER, min REAL, pts REAL, reb REAL, ast REAL, stl REAL, blk REAL,
  tov REAL, fg3m REAL, fg3a REAL, fgm REAL, fga REAL, ftm REAL, fta REAL, pm REAL,
  PRIMARY KEY (game_id, player_id)
);
CREATE INDEX IF NOT EXISTS nba_pg_player ON nba_player_games(player_id);
CREATE TABLE IF NOT EXISTS nba_players (
  id TEXT PRIMARY KEY, name TEXT, short TEXT, pos TEXT, team_id TEXT, jersey TEXT,
  headshot TEXT, age INTEGER, height TEXT, weight TEXT, experience INTEGER
);
CREATE TABLE IF NOT EXISTS nba_injuries (
  player_id TEXT PRIMARY KEY, team_id TEXT, name TEXT, status TEXT, fstatus TEXT,
  kind TEXT, comment TEXT, return_date TEXT, date TEXT
);
CREATE TABLE IF NOT EXISTS nba_snapshots (
  game_id TEXT PRIMARY KEY, created_at TEXT, payload TEXT
);
CREATE TABLE IF NOT EXISTS nba_backtests (
  game_id TEXT PRIMARY KEY, version TEXT, payload TEXT
);

CREATE TABLE IF NOT EXISTS nba_shots (
  game_id TEXT, seq TEXT, player_id TEXT, team_id TEXT, x REAL, y REAL, made INTEGER, value INTEGER,
  period INTEGER, kind TEXT, assisted INTEGER,
  PRIMARY KEY (game_id, seq)
);
CREATE INDEX IF NOT EXISTS nba_shots_player ON nba_shots(player_id);

CREATE TABLE IF NOT EXISTS soc_teams (
  id TEXT, league TEXT, name TEXT, short TEXT, abbr TEXT, logo TEXT, color TEXT,
  PRIMARY KEY (id, league)
);
CREATE TABLE IF NOT EXISTS soc_matches (
  id TEXT PRIMARY KEY, league TEXT, date TEXT, season INTEGER, state TEXT, completed INTEGER,
  status_detail TEXT, home_id TEXT, away_id TEXT, home_goals INTEGER, away_goals INTEGER, venue TEXT
);
CREATE INDEX IF NOT EXISTS soc_matches_league ON soc_matches(league, date);
CREATE TABLE IF NOT EXISTS soc_players (
  id TEXT, team_id TEXT, league TEXT, name TEXT, pos TEXT, jersey TEXT,
  apps REAL, sub_ins REAL, goals REAL, assists REAL, shots REAL, sot REAL, injured INTEGER,
  PRIMARY KEY (id, team_id, league)
);
"""


def _seed() -> None:
    """On a host with no stored data, start from the bundled snapshot instead of a long backfill."""
    with _seed_lock:
        if os.path.exists(DB_PATH) or not os.path.exists(SEED_PATH):
            return
        tmp = DB_PATH + ".seed"
        with gzip.open(SEED_PATH, "rb") as src, open(tmp, "wb") as dst:
            shutil.copyfileobj(src, dst)
        c = sqlite3.connect(tmp)
        c.execute("DELETE FROM meta WHERE key LIKE 'nba_sched_%'")   # re-sync the season since the snapshot
        c.commit()
        c.close()
        os.replace(tmp, DB_PATH)


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        _seed()
        c = sqlite3.connect(DB_PATH, timeout=60)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        _local.conn = c
    return c


def _add_column(c, table: str, column: str, decl: str) -> None:
    if column not in {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init() -> None:
    c = conn()
    c.executescript(SCHEMA)
    _add_column(c, "nba_games", "shots", "INTEGER DEFAULT 0")
    c.commit()


def get_meta(key: str, default=None):
    row = conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_meta(key: str, value) -> None:
    c = conn()
    c.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )
    c.commit()
