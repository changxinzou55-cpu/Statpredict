"""NBA projection model.

Player lines: projected minutes x per-minute production, both weighted toward recent games,
with last season as a prior, adjusted for the opponent, home court and who is available.
Game lines: opponent-adjusted team ratings blended with the sum of the player projections.
Everything is computed "as of" a day, using only games played before it, so the same code
drives live predictions and the walk-forward accuracy check.
"""
import math
from bisect import bisect_left
from datetime import date
from collections import defaultdict
from dataclasses import dataclass, field

STATS = ("pts", "reb", "ast", "fg3m", "stl", "blk", "tov")
NS = len(STATS)
MODEL_VERSION = "2"


@dataclass
class Params:
    h_rates: tuple = (12.0,) * 7  # half-life (games) for per-minute production, one per stat
    h_min: float = 5.0          # half-life (games) for minutes
    prior_mult: float = 0.4     # weight of a previous-season game for production
    prior_mult_min: float = 0.25  # ... and for minutes
    kappa: float = 40.0         # minutes of league-average play mixed into thin samples
    beta: float = 0.5           # how much of the opponent's defensive profile to apply
    gamma: float = 0.5          # pull of player sums toward the team's usual totals
    k_opp: float = 10.0
    k_team: float = 6.0
    blend: float = 0.3          # share of the game line taken from summed player points
    rating_prev_w: float = 0.35
    rating_ridge: float = 4.0
    sigma_margin: float = 12.5
    match_score: bool = True
    decomp: float = 0.5         # share of the points rate built from shot volume x regressed accuracy
    decomp3: float = 0.5        # same for threes made
    pct_k: tuple = (60.0, 150.0, 40.0)  # league-average attempts mixed into 2pt%, 3pt% and FT%
    pct_prior: float = 0.7      # weight of a previous-season game for shooting accuracy
    blow_k: float = 0.0         # minutes a 34-minute player loses per point of expected margin beyond 6
    b2b_min: float = 0.0        # share of minutes regulars lose on the second night of a back-to-back
    rest_pen: float = 0.0       # points a team gives up on the second night of a back-to-back
    rating_half: float = 0.0    # half-life (days) of a result in the team ratings; 0 = no decay
    playoff_total: float = 1.0  # scoring multiplier in the play-in and playoffs
    w_mkt_margin: float = 0.0   # weight of the betting line in the final margin, when a line exists
    w_mkt_total: float = 0.0    # ... and in the final total
    star_hold: float = 1.0      # how firmly heavy-minute players keep their minutes when the team is rebalanced
    home_adj: tuple = (1.01, 1.0, 1.01, 1.0, 1.0, 1.0, 1.0)
    var_phi: tuple = (1.6, 1.3, 1.3, 1.1, 1.0, 1.0, 1.1)
    var_c: tuple = (0.04, 0.03, 0.04, 0.02, 0.0, 0.0, 0.0)


class Row:
    __slots__ = ("gid", "pid", "tid", "opp", "day", "season", "home", "starter", "played", "min", "v", "sh",
                 "name", "pos")


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def count_pmf(mean: float, var: float, kmax: int) -> list[float]:
    """Negative binomial (or Poisson when not over-dispersed) probabilities for 0..kmax."""
    mean = max(mean, 1e-6)
    out = []
    if var <= mean * 1.02:
        p = math.exp(-mean)
        for k in range(kmax + 1):
            out.append(p)
            p *= mean / (k + 1)
    else:
        r = mean * mean / (var - mean)
        q = mean / (r + mean)
        p = math.exp(r * math.log(r / (r + mean)))
        for k in range(kmax + 1):
            out.append(p)
            p *= (k + r) / (k + 1) * q
    return out


def count_range(mean: float, var: float, lo_q: float = 0.1, hi_q: float = 0.9) -> tuple[int, int]:
    kmax = int(mean + 6 * math.sqrt(max(var, 1.0)) + 6)
    acc, lo, hi = 0.0, 0, kmax
    found_lo = False
    for k, p in enumerate(count_pmf(mean, var, kmax)):
        acc += p
        if not found_lo and acc >= lo_q:
            lo, found_lo = k, True
        if acc >= hi_q:
            hi = k
            break
    return lo, hi


def variance(P: Params, i: int, mean: float) -> float:
    return max(mean * P.var_phi[i] + P.var_c[i] * mean * mean, 0.05)


class NBAData:
    """All games and box-score rows held in memory."""

    def __init__(self, conn):
        self.games = {}
        for r in conn.execute("SELECT * FROM nba_games WHERE stype IN (2,3,5)"):
            self.games[r["id"]] = dict(r)
        self.rows_by_player = defaultdict(list)
        self.box = defaultdict(list)
        q = ("SELECT p.*, g.day AS day, g.date AS date, g.season AS season, g.home_id AS home_id "
             "FROM nba_player_games p JOIN nba_games g ON g.id=p.game_id "
             "WHERE g.box=1 AND g.stype IN (2,3,5) ORDER BY g.date")
        for r in conn.execute(q):
            row = Row()
            row.gid, row.pid, row.tid, row.opp = r["game_id"], r["player_id"], r["team_id"], r["opp_id"]
            row.day, row.season = r["day"], r["season"]
            row.home = r["home_id"] == r["team_id"]
            row.starter, row.played, row.min = bool(r["starter"]), bool(r["played"]), r["min"] or 0.0
            row.v = (r["pts"], r["reb"], r["ast"], r["fg3m"], r["stl"], r["blk"], r["tov"])
            row.sh = ((r["fgm"] or 0) - (r["fg3m"] or 0), (r["fga"] or 0) - (r["fg3a"] or 0),
                      r["fg3m"] or 0, r["fg3a"] or 0, r["ftm"] or 0, r["fta"] or 0)
            row.name, row.pos = r["name"], r["pos"]
            self.rows_by_player[row.pid].append(row)
            self.box[(row.gid, row.tid)].append(row)
        self.days_by_player = {pid: [x.day for x in rows] for pid, rows in self.rows_by_player.items()}
        self.completed = sorted(
            (g for g in self.games.values() if g["box"] == 1 and (g["id"], g["home_id"]) in self.box),
            key=lambda g: g["date"])
        self.team_tot = {}
        for key, rows in self.box.items():
            self.team_tot[key] = tuple(sum(x.v[i] for x in rows) for i in range(NS))
        self.seasons = sorted({g["season"] for g in self.games.values()})
        # League shooting by season: attempts per team-game and accuracy for twos, threes and free throws.
        tot, ngames = defaultdict(lambda: [0.0] * 6), defaultdict(int)
        for (gid, _tid), rows in self.box.items():
            season = self.games[gid]["season"]
            ngames[season] += 1
            t = tot[season]
            for x in rows:
                for i in range(6):
                    t[i] += x.sh[i]
        self.lg_shoot = {}
        for season, t in tot.items():
            n = max(ngames[season], 1)
            self.lg_shoot[season] = (t[1] / n, t[3] / n, t[5] / n,
                                     t[0] / max(t[1], 1), t[2] / max(t[3], 1), t[4] / max(t[5], 1))

    def shoot_prior(self, season: int):
        """League shooting to regress toward: the season before, so nothing from the future leaks in."""
        for s in (season - 1, season):
            if s in self.lg_shoot:
                return self.lg_shoot[s]
        return (52.0, 37.0, 22.0, 0.545, 0.36, 0.78)


class Context:
    """League state as of a day: opponent factors, team norms, ratings, rest."""

    def __init__(self):
        self.lg = (0.0,) * NS
        self.oppf = {}
        self.teamfor = {}
        self.off = {}
        self.dfa = {}
        self.mu = 112.0
        self.hca = 2.5
        self.last_day = {}
        self.record = {}
        self.recent_days = {}


class ContextBuilder:
    """Feeds completed games in date order; snapshot() gives the state before a given day."""

    def __init__(self, data: NBAData, P: Params):
        self.data, self.P = data, P
        self.i = 0
        self.n = defaultdict(int)                      # (season, tid) -> games
        self.fr = defaultdict(lambda: [0.0] * NS)      # (season, tid) -> totals for
        self.al = defaultdict(lambda: [0.0] * NS)      # (season, tid) -> totals allowed
        self.lg_n = defaultdict(int)
        self.lg_tot = defaultdict(lambda: [0.0] * NS)
        self.rec = defaultdict(lambda: [0, 0])
        self.last_day = {}
        self.recent_days = defaultdict(list)
        self.rgames = []                               # (season, home, away, hs, as, day ordinal)
        self.asof = 0
        self.off, self.dfa = defaultdict(float), defaultdict(float)

    def advance(self, day: str) -> None:
        comp, d = self.data.completed, self.data
        if day < "9999":
            self.asof = date.fromisoformat(day).toordinal()
        while self.i < len(comp) and comp[self.i]["day"] < day:
            g = comp[self.i]
            self.i += 1
            s, h, a = g["season"], g["home_id"], g["away_id"]
            th, ta = d.team_tot.get((g["id"], h)), d.team_tot.get((g["id"], a))
            if not th or not ta:
                continue
            for t, mine, theirs in ((h, th, ta), (a, ta, th)):
                self.n[(s, t)] += 1
                fr, al = self.fr[(s, t)], self.al[(s, t)]
                lg = self.lg_tot[s]
                for j in range(NS):
                    fr[j] += mine[j]
                    al[j] += theirs[j]
                    lg[j] += mine[j]
                self.lg_n[s] += 1
                self.last_day[t] = g["day"]
                self.recent_days[t].append(g["day"])
            hs, as_ = g["home_score"] or th[0], g["away_score"] or ta[0]
            self.rec[(s, h)][0 if hs > as_ else 1] += 1
            self.rec[(s, a)][0 if as_ > hs else 1] += 1
            self.rgames.append((s, h, a, hs, as_, date.fromisoformat(g["day"]).toordinal()))

    def _fit_ratings(self, season: int, iters: int) -> tuple[float, float]:
        P = self.P
        cur_n = self.lg_n.get(season, 0) / 30.0
        w_prev = P.rating_prev_w * 0.5 ** (cur_n / 20.0)
        asof = self.asof or (self.rgames[-1][5] + 1 if self.rgames else 0)
        half = P.rating_half

        def wt(s, o):
            w = 1.0 if s == season else w_prev
            return w * 0.5 ** ((asof - o) / half) if half > 0 else w

        games = [(wt(s, o), h, a, hs, as_) for s, h, a, hs, as_, o in self.rgames if s >= season - 1]
        if not games:
            return 112.0, 2.5
        sw = sum(g[0] for g in games)
        mu = sum(g[0] * (g[3] + g[4]) for g in games) / (2 * sw)
        hca = sum(g[0] * (g[3] - g[4]) for g in games) / sw
        off, dfa = self.off, self.dfa
        for _ in range(iters):
            num, den = defaultdict(float), defaultdict(float)
            for w, h, a, hs, as_ in games:
                num[h] += w * (hs - mu - hca / 2 - dfa[a]); den[h] += w
                num[a] += w * (as_ - mu + hca / 2 - dfa[h]); den[a] += w
            for t in den:
                off[t] = num[t] / (den[t] + P.rating_ridge)
            num, den = defaultdict(float), defaultdict(float)
            for w, h, a, hs, as_ in games:
                num[a] += w * (hs - mu - hca / 2 - off[h]); den[a] += w
                num[h] += w * (as_ - mu + hca / 2 - off[a]); den[h] += w
            for t in den:
                dfa[t] = num[t] / (den[t] + P.rating_ridge)
        return mu, hca

    def snapshot(self, season: int, iters: int = 5) -> Context:
        P, c = self.P, Context()
        n_cur, n_prev = self.lg_n.get(season, 0), self.lg_n.get(season - 1, 0)
        prev_avg = [x / n_prev for x in self.lg_tot[season - 1]] if n_prev else None
        if n_cur or prev_avg:
            k = 60.0 if prev_avg else 0.0
            base = prev_avg or [0.0] * NS
            c.lg = tuple((self.lg_tot[season][j] + k * base[j]) / (n_cur + k) for j in range(NS))
        else:
            c.lg = (113.0, 44.0, 26.0, 13.0, 8.0, 5.0, 14.0)
        teams = {t for (s, t) in self.n if s >= season - 1}
        for t in teams:
            n = self.n.get((season, t), 0)
            npv = self.n.get((season - 1, t), 0)
            of, tf = [], []
            for j in range(NS):
                lg = c.lg[j] or 1.0
                pa = pf = lg
                if npv and prev_avg and prev_avg[j]:
                    pa = lg * (1 + 0.5 * (self.al[(season - 1, t)][j] / npv / prev_avg[j] - 1))
                    pf = lg * (1 + 0.5 * (self.fr[(season - 1, t)][j] / npv / prev_avg[j] - 1))
                of.append(((self.al[(season, t)][j] if n else 0.0) + P.k_opp * pa) / (n + P.k_opp) / lg)
                tf.append(((self.fr[(season, t)][j] if n else 0.0) + P.k_team * pf) / (n + P.k_team))
            c.oppf[t], c.teamfor[t] = of, tf
            c.record[t] = tuple(self.rec.get((season, t), (0, 0)))
        c.mu, c.hca = self._fit_ratings(season, iters)
        c.off, c.dfa = dict(self.off), dict(self.dfa)
        c.last_day = dict(self.last_day)
        c.recent_days = {t: d[-6:] for t, d in self.recent_days.items()}
        return c


def player_base(data: NBAData, ctx: Context, P: Params, pid: str, day: str, season: int):
    """Weighted history for one player using only games before `day`."""
    rows = data.rows_by_player.get(pid)
    if not rows:
        return None
    idx = bisect_left(data.days_by_player[pid], day)
    if idx == 0:
        return None
    k = 0
    swm = wm = 0.0
    ws = [0.0] * NS
    sw = [0.0] * NS
    att = [0.0] * 3          # recency-weighted 2pt, 3pt and free-throw attempts
    mk = [0.0] * 3           # slow-moving makes and attempts, for accuracy
    at = [0.0] * 3
    dressed = played8 = 0
    gp = 0
    cur_sum = [0.0] * (NS + 1)
    l5 = [0.0] * (NS + 1)
    l5n = 0
    any_sum = [0.0] * (NS + 1)
    any_n = 0
    last_played = None
    last_dressed = rows[idx - 1].day
    dr = [0.5 ** (1.0 / h) for h in P.h_rates]
    dm = 0.5 ** (1.0 / P.h_min)
    wr, wmn, wp = [1.0] * NS, 1.0, 1.0
    for j in range(idx - 1, max(idx - 90, -1), -1):
        r = rows[j]
        if dressed < 8:
            dressed += 1
            played8 += 1 if r.played else 0
        if not r.played:
            continue
        if last_played is None:
            last_played = r.day
        cur = r.season == season
        pm = 1.0 if cur else P.prior_mult
        b = wmn * (1.0 if cur else P.prior_mult_min)
        swm += b
        wm += b * r.min
        v = r.v
        for i in range(NS):
            a = wr[i] * pm
            ws[i] += a * v[i]
            sw[i] += a * r.min
            wr[i] *= dr[i]
        sh = r.sh
        a0, a3 = wr[0] / dr[0] * pm, wr[3] / dr[3] * pm
        att[0] += a0 * sh[1]
        att[1] += a3 * sh[3]
        att[2] += a0 * sh[5]
        c = wp * (1.0 if cur else P.pct_prior)
        for i in range(3):
            mk[i] += c * sh[2 * i]
            at[i] += c * sh[2 * i + 1]
        wp *= 0.985
        if cur:
            gp += 1
            cur_sum[NS] += r.min
            for i in range(NS):
                cur_sum[i] += v[i]
        if l5n < 5:
            l5n += 1
            l5[NS] += r.min
            for i in range(NS):
                l5[i] += v[i]
        if any_n < 20:
            any_n += 1
            any_sum[NS] += r.min
            for i in range(NS):
                any_sum[i] += v[i]
        k += 1
        wmn *= dm
        if k >= 50:
            break
    if k == 0:
        return None
    rates = [(ws[i] + P.kappa * 0.9 * ctx.lg[i] / 240.0) / (sw[i] + P.kappa) for i in range(NS)]
    if P.decomp or P.decomp3:
        lg = data.shoot_prior(season)
        pct = [(mk[i] + P.pct_k[i] * lg[3 + i]) / (at[i] + P.pct_k[i]) for i in range(3)]
        vol = [(att[0] + P.kappa * 0.9 * lg[0] / 240.0) / (sw[0] + P.kappa),
               (att[1] + P.kappa * 0.9 * lg[1] / 240.0) / (sw[3] + P.kappa),
               (att[2] + P.kappa * 0.9 * lg[2] / 240.0) / (sw[0] + P.kappa)]
        built = 2 * vol[0] * pct[0] + 3 * vol[1] * pct[1] + vol[2] * pct[2]
        rates[0] = (1 - P.decomp) * rates[0] + P.decomp * built
        rates[3] = (1 - P.decomp3) * rates[3] + P.decomp3 * vol[1] * pct[1]
    season_avg = [x / gp for x in cur_sum] if gp >= 3 else [x / any_n for x in any_sum]
    return {
        "min": wm / swm, "p_play": (played8 + 0.3) / (dressed + 0.3), "rates": rates, "n": k, "gp": gp,
        "avg": season_avg, "l5": [x / l5n for x in l5], "last_played": last_played, "last_dressed": last_dressed,
    }


def project_team(data, ctx, P, tid, opp, is_home, cands, day, season, known_dressed, exp_margin=0.0, rest=None):
    """cands: [{'pid','name','pos','flag'}]. Returns (player lines, team totals)."""
    blow = P.blow_k * max(abs(exp_margin) - 6.0, 0.0)
    recent = ctx.recent_days.get(tid, [])
    cutoff = recent[-5] if len(recent) >= 5 else None
    out = []
    for c in cands:
        b = player_base(data, ctx, P, c["pid"], day, season)
        if not b:
            continue
        p_play = b["p_play"]
        note = c.get("flag")
        if not known_dressed:
            # Live: the injury report says who is doubtful, so a rotation regular who is not on it
            # is assumed to play. Fringe players keep their coach's-decision odds.
            if note == "questionable":
                p_play = min(p_play, 0.65)
            else:
                p_play += (1.0 - p_play) * 0.9 * min(max((b["min"] - 12.0) / 12.0, 0.0), 1.0)
        m = p_play * b["min"]
        if cutoff and b["last_dressed"] < cutoff:
            # Has not suited up in the team's last five games.
            m = b["min"] * (0.85 if known_dressed else (0.6 if b["min"] >= 24 else 0.25))
            note = note or "returning"
        if m > 20.0:
            # Regulars sit earlier in lopsided games and play a little less on no rest.
            m -= blow * (m - 20.0) / 14.0
            if rest == 0:
                m -= P.b2b_min * (m - 20.0)
        out.append({"pid": c["pid"], "name": c.get("name"), "pos": c.get("pos"), "flag": note, "b": b, "m": m})
    # Fit the team to 240 minutes. Heavy-minute players move least; the bench absorbs most of the change.
    for _ in range(3):
        gap = 240.0 - sum(p["m"] for p in out)
        if abs(gap) < 0.5:
            break
        w = [p["m"] * max(1.0 - P.star_hold * p["m"] / 38.0, 0.1) for p in out]
        sw = sum(w)
        if sw <= 0:
            break
        gap = min(max(gap, -60.0), 60.0)
        for p, wi in zip(out, w):
            p["m"] = min(max(p["m"] + gap * wi / sw, 0.0), 40.0)
    oppf = ctx.oppf.get(opp, [1.0] * NS)
    tfor = ctx.teamfor.get(tid, ctx.lg)
    lines, totals = [], [0.0] * NS
    adj = [(1 + P.beta * (oppf[i] - 1)) * (P.home_adj[i] if is_home else 1.0 / P.home_adj[i]) for i in range(NS)]
    raw_sum = [sum(p["m"] * p["b"]["rates"][i] for p in out) * adj[i] for i in range(NS)]
    rec = []
    for i in range(NS):
        target = tfor[i] * adj[i]
        f = 1 + P.gamma * (target / raw_sum[i] - 1) if raw_sum[i] > 0 else 1.0
        rec.append(min(max(f, 0.85), 1.2))
    for p in out:
        b = p["b"]
        vals = [p["m"] * b["rates"][i] * adj[i] * rec[i] for i in range(NS)]
        for i in range(NS):
            totals[i] += vals[i]
        lines.append({
            "pid": p["pid"], "name": p["name"], "pos": p["pos"], "flag": p["flag"],
            "min": round(p["m"], 1), "proj": [round(x, 2) for x in vals],
            "avg": [round(x, 2) for x in b["avg"]], "l5": [round(x, 2) for x in b["l5"]],
            "gp": b["gp"], "last_played": b["last_played"],
        })
    lines.sort(key=lambda x: -x["min"])
    return lines, totals


def _rest(ctx, tid, day):
    last = ctx.last_day.get(tid)
    if not last:
        return None
    from datetime import date
    return (date.fromisoformat(day) - date.fromisoformat(last)).days - 1


def project_game(data, ctx, P, game, cands_home, cands_away, known_dressed=False):
    h, a, day, season = game["home_id"], game["away_id"], game["day"], game["season"]
    rest_h, rest_a = _rest(ctx, h, day), _rest(ctx, a, day)
    rh = ctx.mu + ctx.hca / 2 + ctx.off.get(h, 0.0) + ctx.dfa.get(a, 0.0)
    ra = ctx.mu - ctx.hca / 2 + ctx.off.get(a, 0.0) + ctx.dfa.get(h, 0.0)
    shift = (P.rest_pen if rest_a == 0 else 0.0) - (P.rest_pen if rest_h == 0 else 0.0)
    rh, ra = rh + shift / 2, ra - shift / 2
    spread, ou = game.get("spread"), game.get("over_under")
    wm = P.w_mkt_margin if spread is not None else 0.0
    wt = P.w_mkt_total if ou else 0.0
    exp_margin = (1 - wm) * (rh - ra) + wm * (-(spread or 0.0))
    hl, ht = project_team(data, ctx, P, h, a, True, cands_home, day, season, known_dressed, exp_margin, rest_h)
    al, at = project_team(data, ctx, P, a, h, False, cands_away, day, season, known_dressed, -exp_margin, rest_a)
    w = P.blend if ht[0] > 60 and at[0] > 60 else 0.0
    mh, ma = (1 - w) * rh + w * ht[0], (1 - w) * ra + w * at[0]
    if game.get("stype") in (3, 5):
        mh, ma = mh * P.playoff_total, ma * P.playoff_total
    model_margin, model_total = mh - ma, mh + ma
    margin = (1 - wm) * model_margin + wm * (-(spread or 0.0))
    total = (1 - wt) * model_total + wt * (ou or 0.0)
    ph, pa = (total + margin) / 2, (total - margin) / 2
    if w and P.match_score:
        # Make each side's player points add up to its predicted score.
        for lines, tot, target in ((hl, ht[0], ph), (al, at[0], pa)):
            f = min(max(target / tot, 0.9), 1.12)
            for line in lines:
                line["proj"][0] = round(line["proj"][0] * f, 2)
    return {
        "home_pts": round(ph, 1), "away_pts": round(pa, 1), "margin": round(margin, 1),
        "total": round(ph + pa, 1), "p_home": round(_phi(margin / P.sigma_margin), 3),
        "model_margin": round(model_margin, 1), "model_total": round(model_total, 1),
        "line_used": bool(wm or wt),
        "rating_margin": round(rh - ra, 1),
        "rest": {"home": rest_h, "away": rest_a},
        "oppf": {"home": [round(x, 3) for x in ctx.oppf.get(h, [1.0] * NS)],
                 "away": [round(x, 3) for x in ctx.oppf.get(a, [1.0] * NS)]},
        "record": {"home": ctx.record.get(h, (0, 0)), "away": ctx.record.get(a, (0, 0))},
        "players": {"home": hl, "away": al},
    }


def attach_actuals(data, game, pred):
    """Adds what actually happened to each projected player line."""
    for side, tid in (("home", game["home_id"]), ("away", game["away_id"])):
        actual = {r.pid: r for r in data.box.get((game["id"], tid), [])}
        seen = set()
        for line in pred["players"][side]:
            r = actual.get(line["pid"])
            seen.add(line["pid"])
            if r is not None:
                line["act"] = [r.v[i] for i in range(NS)] if r.played else None
                line["act_min"] = r.min if r.played else 0
                line["dressed"] = True
        for pid, r in actual.items():
            if pid not in seen and r.played:
                pred["players"][side].append({
                    "pid": pid, "name": r.name, "pos": r.pos, "flag": "unprojected", "min": 0.0,
                    "proj": [0.0] * NS, "avg": [0.0] * (NS + 1), "l5": [0.0] * (NS + 1), "gp": 0,
                    "act": list(r.v), "act_min": r.min, "dressed": True})
    pred["actual"] = {"home_pts": game["home_score"], "away_pts": game["away_score"]}
    return pred


def backtest(data: NBAData, P: Params, seasons, skip_ids=(), progress=None):
    """Walk forward through completed games, predicting each from earlier games only."""
    builder = ContextBuilder(data, P)
    out = {}
    ctx, ctx_day = None, None
    for g in data.completed:
        if g["season"] not in seasons:
            continue
        if g["day"] != ctx_day:
            builder.advance(g["day"])
            ctx, ctx_day = None, g["day"]
        if g["id"] in skip_ids:
            continue
        if ctx is None:
            ctx = builder.snapshot(g["season"])
        cands = {}
        for side, tid in (("home", g["home_id"]), ("away", g["away_id"])):
            cands[side] = [{"pid": r.pid, "name": r.name, "pos": r.pos}
                           for r in data.box.get((g["id"], tid), [])]
        pred = project_game(data, ctx, P, g, cands["home"], cands["away"], known_dressed=True)
        out[g["id"]] = attach_actuals(data, g, pred)
        if progress:
            progress(len(out))
    return out


def favorite_probability_band(p_home: float) -> str:
    # The model stores probabilities to three decimals. Use integers so exact
    # 60%, 70% and 80% forecasts land at the bottom of the next band.
    home = round(p_home * 1000)
    lower = 50 + 10 * min((max(home, 1000 - home) - 500) // 100, 4)
    return f"{lower}-{lower + 10}%"


def summarize(data: NBAData, P: Params, preds: dict) -> dict:
    """Accuracy figures from walk-forward (or locked) predictions."""
    n_stat = 0
    err = [0.0] * NS
    err_avg = [0.0] * NS
    err_l5 = [0.0] * NS
    in_range = [0] * NS
    bias = [0.0] * NS
    min_err = 0.0
    games = correct = home_pick = rec_pick = rec_n = 0
    mkt_n = mkt_correct = model_on_mkt = 0
    m_err = t_err = mk_m_err = mk_t_err = 0.0
    mk_t_n = 0
    sq_margin = 0.0
    so_correct = 0
    so_m = so_t = 0.0
    buckets = defaultdict(lambda: [0, 0, 0.0])
    brier = 0.0
    for gid, p in preds.items():
        g = data.games.get(gid)
        if not g or g["home_score"] is None:
            continue
        hs, as_ = g["home_score"], g["away_score"]
        if hs == as_:
            continue
        games += 1
        home_won = hs > as_
        pick_home = p["p_home"] >= 0.5
        correct += pick_home == home_won
        home_pick += home_won
        brier += (p["p_home"] - (1.0 if home_won else 0.0)) ** 2
        m_err += abs(p["margin"] - (hs - as_))
        so_correct += (p.get("model_margin", p["margin"]) >= 0) == home_won
        so_m += abs(p.get("model_margin", p["margin"]) - (hs - as_))
        so_t += abs(p.get("model_total", p["total"]) - (hs + as_))
        sq_margin += (p["margin"] - (hs - as_)) ** 2
        t_err += abs(p["total"] - (hs + as_))
        conf = max(p["p_home"], 1 - p["p_home"])
        b = favorite_probability_band(p["p_home"])
        buckets[b][0] += 1
        buckets[b][1] += pick_home == home_won
        buckets[b][2] += conf
        rh, ra = p["record"]["home"], p["record"]["away"]
        if sum(rh) and sum(ra):
            wh, wa = rh[0] / sum(rh), ra[0] / sum(ra)
            if wh != wa:
                rec_n += 1
                rec_pick += (wh > wa) == home_won
        if g["spread"] is not None:
            if g["spread"] != 0:
                mkt_n += 1
                mkt_correct += (g["spread"] < 0) == home_won
                model_on_mkt += pick_home == home_won
            mk_m_err += abs(-g["spread"] - (hs - as_))
        if g["over_under"]:
            mk_t_n += 1
            mk_t_err += abs(g["over_under"] - (hs + as_))
        for side in ("home", "away"):
            for line in p["players"][side]:
                if line.get("act") is None or line["min"] < 12:
                    continue
                n_stat += 1
                min_err += abs(line["min"] - line["act_min"])
                for i in range(NS):
                    a, m = line["act"][i], line["proj"][i]
                    err[i] += abs(m - a)
                    bias[i] += m - a
                    err_avg[i] += abs(line["avg"][i] - a)
                    err_l5[i] += abs(line["l5"][i] - a)
                    lo, hi = count_range(m, variance(P, i, m))
                    in_range[i] += lo <= a <= hi
    if not games or not n_stat:
        return {"games": 0}
    mkt_games = sum(1 for gid in preds if (data.games.get(gid) or {}).get("spread") is not None)
    return {
        "games": games, "player_lines": n_stat,
        "winner": {
            "model": round(correct / games, 4), "home": round(home_pick / games, 4),
            "record": round(rec_pick / rec_n, 4) if rec_n else None,
            "market": round(mkt_correct / mkt_n, 4) if mkt_n else None,
            "model_on_market_games": round(model_on_mkt / mkt_n, 4) if mkt_n else None,
            "market_games": mkt_n, "brier": round(brier / games, 4),
        },
        "margin_mae": round(m_err / games, 2), "total_mae": round(t_err / games, 2),
        "stats_only": {"winner": round(so_correct / games, 4), "margin_mae": round(so_m / games, 2),
                       "total_mae": round(so_t / games, 2)},
        "margin_rmse": round(math.sqrt(sq_margin / games), 2),
        "market_margin_mae": round(mk_m_err / mkt_games, 2) if mkt_games else None,
        "market_total_mae": round(mk_t_err / mk_t_n, 2) if mk_t_n else None,
        "minutes_mae": round(min_err / n_stat, 2),
        "stats": [{
            "stat": STATS[i], "mae": round(err[i] / n_stat, 3), "season_avg_mae": round(err_avg[i] / n_stat, 3),
            "last5_mae": round(err_l5[i] / n_stat, 3), "bias": round(bias[i] / n_stat, 3),
            "in_range": round(in_range[i] / n_stat, 3),
        } for i in range(NS)],
        "calibration": [{
            "bucket": b, "games": v[0],
            "predicted": round(v[2] / v[0], 3), "actual": round(v[1] / v[0], 3),
        } for b, v in sorted(buckets.items())],
    }
