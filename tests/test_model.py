import os
import sqlite3
import tempfile

os.environ.setdefault("STATPREDICT_DATA_DIR", tempfile.mkdtemp())

from app import db, nba_ingest, nba_model as M, soccer  # noqa: E402


def make_db(tweak_last=None):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(db.SCHEMA)
    teams = ["A", "B", "C", "D"]
    gid = 0
    for rnd in range(12):
        day = f"2026-11-{rnd + 1:02d}"
        for h, a in ((teams[rnd % 4], teams[(rnd + 1) % 4]), (teams[(rnd + 2) % 4], teams[(rnd + 3) % 4])):
            gid += 1
            c.execute(
                "INSERT INTO nba_games(id,date,day,season,stype,state,completed,home_id,away_id,home_score,away_score,box)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,1)",
                (str(gid), day + "T23:00Z", day, 2027, 2, "post", 1, h, a, 0, 0))
            score = {}
            for t, o in ((h, a), (a, h)):
                tot = 0
                for k in range(8):
                    pts = 6 + 3 * k + (gid * (k + 3) + ord(t)) % 7
                    if tweak_last and str(gid) == tweak_last:
                        pts += 20
                    tot += pts
                    c.execute(
                        "INSERT INTO nba_player_games VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (str(gid), f"{t}{k}", t, o, f"{t}{k}", "G", int(k >= 3), 1, 30.0, pts, 4, 3, 1, 0, 2,
                         1, 3, 5, 10, 2, 2, 0))
                score[t] = tot
            c.execute("UPDATE nba_games SET home_score=?, away_score=? WHERE id=?", (score[h], score[a], str(gid)))
    return c


def test_count_distribution_sums_to_one_and_range_brackets_mean():
    for mean, var in ((0.4, 0.5), (6.0, 9.0), (24.0, 80.0), (3.0, 3.0)):
        assert abs(sum(M.count_pmf(mean, var, 200)) - 1) < 1e-6
        lo, hi = M.count_range(mean, var)
        assert lo <= mean <= hi + 1


def test_backtest_uses_only_earlier_games():
    P = M.Params()
    base = M.backtest(M.NBAData(make_db()), P, {2027})
    changed = M.backtest(M.NBAData(make_db(tweak_last="24")), P, {2027})
    # Changing what happened in the final game must not change what was predicted for it.
    assert base["24"]["home_pts"] == changed["24"]["home_pts"]
    assert [l["proj"] for l in base["24"]["players"]["home"]] == [l["proj"] for l in changed["24"]["players"]["home"]]
    assert changed["24"]["actual"]["home_pts"] != base["24"]["actual"]["home_pts"]


def test_team_minutes_sum_to_240_and_summary_runs():
    data = M.NBAData(make_db())
    P = M.Params()
    preds = M.backtest(data, P, {2027})
    last = preds["24"]
    for side in ("home", "away"):
        assert abs(sum(l["min"] for l in last["players"][side]) - 240) < 1.5
    s = M.summarize(data, P, preds)
    assert s["games"] > 0 and len(s["stats"]) == len(M.STATS)


def test_home_spread_parsing():
    assert nba_ingest._home_line({"details": "PHI -7.5"}, "PHI", "TOR") == -7.5
    assert nba_ingest._home_line({"details": "TOR -3"}, "PHI", "TOR") == 3.0
    assert nba_ingest._home_line({"details": "EVEN"}, "PHI", "TOR") == 0.0
    assert nba_ingest._home_line(None, "PHI", "TOR") is None


def test_football_probabilities_are_consistent():
    ms = []
    for i in range(60):
        h, a = f"T{i % 6}", f"T{(i + 1 + i // 6) % 6}"
        if h == a:
            continue
        ms.append({"ts": 1000.0 + i * 86400, "home_id": h, "away_id": a,
                   "home_goals": 3 if h == "T0" else i % 3, "away_goals": 0 if h == "T0" else (i + 1) % 2})
    model = soccer.fit(ms, 1000.0 + 61 * 86400)
    p = soccer.predict(model, "T0", "T3")
    assert abs(p["p_home"] + p["p_draw"] + p["p_away"] - 1) < 0.01
    assert p["p_home"] > p["p_away"]
    assert model["att"]["T0"] > 1


def test_shots_skip_free_throws_and_bad_coordinates():
    from app.nba_ingest import _parse_shots
    who = [{"athlete": {"id": "7"}}, {"athlete": {"id": "8"}}]
    base = {"shootingPlay": True, "team": {"id": "1"}, "period": {"number": 1}, "type": {"text": "Jump Shot"}}
    plays = [
        {**base, "sequenceNumber": "1", "pointsAttempted": 3, "scoringPlay": True, "participants": who,
         "coordinate": {"x": 3, "y": 2}},
        {**base, "sequenceNumber": "2", "pointsAttempted": 2, "scoringPlay": False, "participants": who[:1],
         "coordinate": {"x": 25, "y": 1}},
        {**base, "sequenceNumber": "3", "pointsAttempted": 1, "scoringPlay": True, "participants": who[:1],
         "coordinate": {"x": -214748340, "y": -214748365}},
        {**base, "sequenceNumber": "4", "pointsAttempted": 2, "scoringPlay": True, "participants": who[:1],
         "coordinate": {"x": -214748340, "y": -214748365}},
    ]
    shots = _parse_shots("g", {"plays": plays})
    assert [s[1] for s in shots] == ["1", "2"]
    assert shots[0][6:8] == (1, 3) and shots[0][10] == 1      # made three, assisted
    assert shots[1][6:8] == (0, 2) and shots[1][10] == 0      # missed two


def test_court_zones():
    from app.service import zone_of
    assert zone_of(25, 1, 2) == "rim"
    assert zone_of(30, 8, 2) == "paint"
    assert zone_of(10, 12, 2) == "mid"
    assert zone_of(2, 3, 3) == "corner3"
    assert zone_of(25, 25, 3) == "above3"
