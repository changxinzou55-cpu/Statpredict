import itertools
import os
import tempfile
from types import SimpleNamespace

os.environ.setdefault("STATPREDICT_DATA_DIR", tempfile.mkdtemp())

from app import playoffs, service  # noqa: E402

EAST = ["ATL", "BOS", "BKN", "CHA", "CHI", "CLE", "DET", "IND", "MIA", "MIL", "NY", "ORL", "PHI", "TOR", "WSH"]
WEST = ["DAL", "DEN", "GS", "HOU", "LAC", "LAL", "MEM", "MIN", "NO", "OKC", "PHX", "POR", "SAC", "SA", "UTAH"]


def league(played_rounds=1):
    """30 teams; team i is rated 14 - i within its conference, so BOS-like index 0 is the best."""
    teams, net = {}, {}
    for conf in (EAST, WEST):
        for i, ab in enumerate(conf):
            teams[ab] = {"abbr": ab, "name": ab, "logo": None}
            net[ab] = 7.0 - i
    games, ids = [], list(teams)
    for rnd in range(3):
        for h, a in itertools.combinations(ids, 2):
            if rnd % 2:
                h, a = a, h
            done = rnd < played_rounds
            hw = net[h] + 2 > net[a]
            games.append({"home_id": h, "away_id": a, "day": f"2026-1{rnd}-01", "completed": done,
                          "home_score": (110 if hw else 100) if done else None,
                          "away_score": (100 if hw else 110) if done else None})
    return teams, games, net


def run(**kw):
    teams, games, net = league()
    return playoffs.simulate(teams, games, net, 2.0, 12.0, sims=2000, **kw)


def test_conferences_split_evenly():
    assert sum(playoffs.conference(a) == "East" for a in EAST + WEST) == 15
    assert all(playoffs.conference(a) == "West" for a in WEST)


def test_chances_add_up():
    res = run()
    t = res["teams"]
    assert len(t) == 30 and res["games_left"] == 2 * 435
    total = lambda k: sum(r[k] for r in t)
    assert abs(total("title") - 1) < 1e-3
    assert abs(total("finals") - 2) < 1e-3
    assert abs(total("cf") - 4) < 1e-3
    assert abs(total("r2") - 8) < 1e-3
    assert abs(total("playoffs") - 16) < 1e-3
    assert abs(total("top6") - 12) < 1e-3 and abs(total("playin") - 8) < 1e-3
    for conf in ("East", "West"):
        assert abs(sum(r["finals"] for r in t if r["conf"] == conf) - 1) < 1e-3
    for r in t:
        assert r["title"] <= r["finals"] <= r["cf"] <= r["r2"] <= r["playoffs"] <= r["top6"] + r["playin"] + 1e-9
    # every team plays 87 games in this made-up schedule
    assert abs(sum(r["proj_w"] for r in t) - 30 * 87 / 2) < 1


def test_better_teams_get_better_chances():
    t = {r["id"]: r for r in run()["teams"]}
    assert t["ATL"]["title"] > t["CHI"]["title"] > t["WSH"]["title"]
    assert t["ATL"]["proj_w"] > t["IND"]["proj_w"] > t["WSH"]["proj_w"]
    assert t["ATL"]["playoffs"] > 0.99 and t["WSH"]["playoffs"] < 0.01


def test_same_seed_same_answer():
    a, b = run(seed=3), run(seed=3)
    assert a == b
    assert run(seed=4)["teams"] != a["teams"]


def test_as_of_ignores_later_results():
    teams, games, net = league(played_rounds=2)
    early = playoffs.simulate(teams, games, net, 2.0, 12.0, as_of="2026-11-01", sims=500)
    assert early["games_left"] == 2 * 435
    assert all(r["w"] + r["l"] == 29 for r in early["teams"])


def test_bracket_shape():
    b = run()["bracket"]
    assert [c["name"] for c in b["conferences"]] == ["East", "West"]
    for c in b["conferences"]:
        assert [len(r) for r in c["rounds"]] == [4, 2, 1]
        assert [(m["hi"]["seed"], m["lo"]["seed"]) for m in c["rounds"][0]] == [(1, 8), (4, 5), (2, 7), (3, 6)]
        for prev, nxt in zip(c["rounds"], c["rounds"][1:]):
            winners = [m["winner"] for m in prev]
            for k, m in enumerate(nxt):
                assert {m["hi"]["id"], m["lo"]["id"]} == set(winners[2 * k:2 * k + 2])
        for m in itertools.chain(*c["rounds"]):
            assert m["winner"] == (m["hi"] if m["p_hi"] >= 0.5 else m["lo"])["id"]
    assert b["champion"]["id"] == b["finals"]["winner"]


def test_series_favours_the_stronger_side_more_than_one_game():
    import numpy as np
    n = 20000
    R = np.tile(np.array([4.0, 0.0]), (n, 1))
    key = np.tile(np.array([50.0, 40.0]), (n, 1))
    sim = playoffs._Sim(R, key, 2.0, 12.0, np.random.default_rng(1))
    a, b = np.zeros(n, dtype=int), np.ones(n, dtype=int)
    one = sim.game(a, b).mean()
    seven = (sim.series(a, b) == 0).mean()
    assert 0.65 < one < 0.73 and seven > one + 0.05
    assert abs((sim.series(b, a) == 0).mean() - seven) < 0.02     # order of the arguments does not matter


def test_player_accuracy_averages():
    line = lambda proj, act, mins=30: {"name": "P", "proj": proj, "act": act, "min": mins}
    state = SimpleNamespace(
        players={"1": {"name": "Ann", "team_id": "T"}}, teams={"T": {"abbr": "TT"}},
        by_player={
            "1": [("g1", line([20, 5, 4], [30, 5, 2])), ("g2", line([24, 7, 6], [20, 9, 6])),
                  ("g3", line([10, 1, 1], None)), ("g4", line([9, 9, 9], [0, 0, 0], mins=0))],
            "2": [("g1", line([8, 2, 1], [9, 2, 1]))],
        })
    rows = service.NBAState.player_accuracy(state, min_games=2)
    assert len(rows) == 1
    r = rows[0]
    assert (r["name"], r["team"], r["gp"]) == ("Ann", "TT", 2)
    assert r["pts"] == {"pred": 22.0, "act": 25.0, "miss": 7.0}
    assert r["reb"] == {"pred": 6.0, "act": 7.0, "miss": 1.0}
    assert r["ast"] == {"pred": 5.0, "act": 4.0, "miss": 1.0}
