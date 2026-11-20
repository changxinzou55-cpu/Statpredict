const $ = (sel, el = document) => el.querySelector(sel);
const main = $('#main');
const STATS = ['pts', 'reb', 'ast', 'fg3m', 'stl', 'blk', 'tov'];
const LABEL = { pts: 'PTS', reb: 'REB', ast: 'AST', fg3m: '3PM', stl: 'STL', blk: 'BLK', tov: 'TO', min: 'MIN' };
const LONG = { pts: 'Points', reb: 'Rebounds', ast: 'Assists', fg3m: 'Threes made', stl: 'Steals', blk: 'Blocks', tov: 'Turnovers' };

const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const f1 = (v) => (v == null ? '–' : Number(v).toFixed(1));
const pct = (v, d = 0) => (v == null ? '–' : (v * 100).toFixed(d) + '%');
const signed = (v) => (v > 0 ? '+' : '') + Number(v).toFixed(1);

function img(u, w, h) {
  if (!u) return '';
  let src = u;
  const m = /^https:\/\/a\.espncdn\.com(\/i\/.+)$/.exec(u);
  if (m && w) src = `https://a.espncdn.com/combiner/i?img=${m[1]}&w=${w}&h=${h || w}`;
  return 'img?u=' + encodeURIComponent(src);
}
const logo = (t, size = 28) => (t && t.logo ? `<img class="logo" src="${img(t.logo, size * 2)}" alt="" width="${size}" height="${size}" loading="lazy">` : '');

// ---------- data: one in-memory cache, shown instantly and refreshed quietly ----------
const cache = new Map();
const FRESH_MS = 60000;
const BOOT = window.__BOOT__ || {};
let builtAt = BOOT.built_at || 0;

async function load(path) {
  const r = await fetch('api/' + path);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch { /* keep status text */ }
    const e = new Error(msg);
    e.status = r.status;
    throw e;
  }
  const data = await r.json();
  cache.set(path, { data, at: Date.now(), pending: null });
  if (path.startsWith('nba/day')) seedDay(data);
  return data;
}
function request(path) {
  const hit = cache.get(path);
  if (hit && hit.pending) return hit.pending;
  const pending = load(path).finally(() => { const c = cache.get(path); if (c) c.pending = null; });
  cache.set(path, { ...(hit || { at: 0 }), pending });
  return pending;
}
// Returns cached data at once when there is any; stale entries are refreshed in the background.
function api(path) {
  const hit = cache.get(path);
  if (hit && hit.data) {
    if (Date.now() - hit.at > FRESH_MS) request(path).then(() => rerender(path)).catch(() => {});
    return Promise.resolve(hit.data);
  }
  return request(path);
}
function prefetch(path) {
  const hit = cache.get(path);
  if (!(hit && (hit.data || hit.pending))) request(path).catch(() => {});
}
function seedDay(d) {
  const now = Date.now();
  cache.set('nba/day?day=' + d.day, { data: d, at: now, pending: null });
  d.games.forEach((g) => cache.set('nba/game/' + g.id, { data: g, at: now, pending: null }));
}
if (BOOT.day) { cache.set('nba/day', { data: BOOT.day, at: Date.now(), pending: null }); seedDay(BOOT.day); }
let currentPaths = [];
function rerender(path) { if (currentPaths.includes(path)) route(true); }
function hrefToApi(href) {
  const h = (href || '').replace(/^.*#/, '');
  let m;
  if ((m = /^\/nba\/game\/(\w+)/.exec(h))) return 'nba/game/' + m[1];
  if ((m = /^\/nba\/player\/(\w+)/.exec(h))) return 'nba/player/' + m[1];
  if ((m = /^\/football\/match\/(\w+)/.exec(h))) return 'football/match/' + m[1];
  if ((m = /^\/football\?league=([\w.]+)/.exec(h))) return 'football/league/' + m[1];
  if ((m = /^\/nba\/(?:games|players)\?day=([\d-]+)/.exec(h))) return 'nba/day?day=' + m[1];
  return { '/nba/teams': 'nba/teams', '/nba/playoffs': 'nba/playoffs', '/nba/accuracy': 'nba/accuracy', '/nba/awards': 'nba/awards', '/football': 'football/league/eng.1', '/nba/players': 'nba/players' }[h];
}
// Start loading a page as soon as the pointer or finger reaches its link.
['pointerover', 'touchstart', 'focusin'].forEach((ev) => document.addEventListener(ev, (e) => {
  const a = e.target.closest && e.target.closest('a[href^="#/"], button[data-day], button[data-lg]');
  if (!a) return;
  const path = a.dataset.day ? 'nba/day?day=' + a.dataset.day : a.dataset.lg ? 'football/league/' + a.dataset.lg : hrefToApi(a.getAttribute('href'));
  if (path) prefetch(path);
}, { passive: true }));

const dayFmt = (day, opts) => new Date(day + 'T12:00:00Z').toLocaleDateString('en-US', { timeZone: 'UTC', ...opts });
const dateTimeFmt = (iso) => new Date(iso).toLocaleString(undefined, { weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
const shortDate = (iso) => new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
const localTipoff = new Intl.DateTimeFormat('en-US', { weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short' });

// ---------- probability helpers (same formulas as the server) ----------
function pmf(mean, variance, kmax) {
  mean = Math.max(mean, 1e-6);
  const out = [];
  if (variance <= mean * 1.02) {
    let p = Math.exp(-mean);
    for (let k = 0; k <= kmax; k++) { out.push(p); p *= mean / (k + 1); }
  } else {
    const r = (mean * mean) / (variance - mean), q = mean / (r + mean);
    let p = Math.exp(r * Math.log(r / (r + mean)));
    for (let k = 0; k <= kmax; k++) { out.push(p); p *= ((k + r) / (k + 1)) * q; }
  }
  return out;
}
function probAtLeast(mean, variance, n) {
  if (n <= 0) return 1;
  const p = pmf(mean, variance, n - 1);
  return Math.max(0, 1 - p.reduce((a, b) => a + b, 0));
}

// ---------- shared pieces ----------
function sourceNote(g) {
  if (!g.pred) return '';
  if (g.source === 'live') return 'Prediction updates until tip-off as injuries and results come in.';
  if (g.source === 'locked') return `Prediction locked before tip-off (${dateTimeFmt(g.locked_at)}).`;
  return 'Back-tested: rebuilt using only games played before this one.';
}

function probBar(pHome, away, home) {
  const pa = 1 - pHome;
  return `<div class="prob">
    <div class="bar" role="img" aria-label="${esc(away)} ${pct(pa)}, ${esc(home)} ${pct(pHome)}">
      <i class="${pa >= 0.5 ? 'on' : ''}" style="width:${pa * 100}%"></i><i class="${pHome > 0.5 ? 'on' : ''}" style="width:${pHome * 100}%"></i>
    </div>
    <div class="prob-l"><span>${esc(away)} <b>${pct(pa)}</b></span><span><b>${pct(pHome)}</b> ${esc(home)}</span></div>
  </div>`;
}

function marketLine(g) {
  const m = g.market || {};
  if (m.spread == null && !m.over_under) return null;
  const parts = [];
  if (m.spread != null) parts.push(m.spread === 0 ? 'even' : `${esc(m.spread < 0 ? g.home.abbr : g.away.abbr)} by ${Math.abs(m.spread)}`);
  if (m.over_under) parts.push(`total ${m.over_under}`);
  return parts.join(', ');
}

function gameCard(g) {
  const p = g.pred, done = g.completed, live = g.state === 'in';
  const hasScore = done || live;
  const favHome = p ? p.p_home >= 0.5 : null;
  const leader = g.score && g.score.away !== g.score.home ? (g.score.home > g.score.away ? 'home' : 'away') : null;
  const row = (side) => {
    const t = g[side], isFav = favHome != null && (side === 'home') === favHome;
    const sc = g.score ? g.score[side] : null;
    return `<tr class="${(hasScore ? leader === side : isFav) ? 'fav' : ''}">
      <th scope="row"><div class="team"><span class="team-mark" aria-hidden="true"><span>${esc(t.abbr)}</span>${logo(t, 24)}</span>
        <span class="team-info"><span class="tname">${esc(t.name)}</span>${t.record ? `<span class="rec">${esc(t.record)}</span>` : ''}</span></div></th>
      ${hasScore ? `<td class="n actual-score">${sc ?? '–'}</td>` : ''}
      <td class="n ${hasScore ? 'dim' : ''}">${p ? f1(p[side + '_pts']) : '–'}</td>
    </tr>`;
  };
  let verdict = '';
  if (done && p && g.score) {
    const homeWon = g.score.home > g.score.away;
    verdict = favHome === homeWon ? '<span class="good">Winner called</span>' : '<span class="bad">Winner missed</span>';
  }
  const top = g.top ? ['away', 'home'].flatMap((s) => g.top[s].slice(0, 1).map((x) => `${esc(x.name)} ${f1(x.pts)}`)).join(' · ') : '';
  const mk = marketLine(g);
  const tipoff = g.date && new Date(g.date);
  const when = done ? '<span class="game-status final">Final</span>'
    : live ? `<span class="game-status live">Live</span>${g.status ? `<span>${esc(g.status)}</span>` : ''}`
    : tipoff && !Number.isNaN(tipoff.getTime())
      ? `<time class="when" datetime="${esc(g.date)}">${localTipoff.format(tipoff)}</time>`
      : '<span class="when">Tip-off time TBD</span>';
  return `<li><a class="game" href="#/nba/game/${g.id}">
    <div class="game-top"><div class="game-clock">${when}</div><span class="game-note">${verdict || esc(g.venue || '')}</span></div>
    <table class="sides"><thead><tr><th scope="col"><span class="skip">Team</span></th>${hasScore ? '<th scope="col">Score</th>' : ''}<th scope="col">Predicted</th></tr></thead>
      <tbody>${row('away')}${row('home')}</tbody></table>
    ${p ? probBar(p.p_home, g.away.abbr, g.home.abbr) : '<p class="muted small">No prediction for this game.</p>'}
    ${p || mk || top ? `<dl class="lines">
      ${p ? `<dt>Model</dt><dd>${esc(p.margin >= 0 ? g.home.abbr : g.away.abbr)} by ${f1(Math.abs(p.margin))}, total ${f1(p.total)}</dd>` : ''}
      ${mk ? `<dt>Betting line</dt><dd>${mk}</dd>` : ''}
      ${top ? `<dt>Top scorers</dt><dd>${top}</dd>` : ''}
    </dl>` : ''}
  </a></li>`;
}

// Keep the abbreviation visible until a team logo has loaded successfully.
main.addEventListener('load', (e) => {
  if (e.target.matches('.team-mark img')) e.target.classList.add('loaded');
}, true);

// ---------- sortable tables ----------
function sortable(table, rows, render, initial) {
  let key = initial.key, dir = initial.dir;
  const body = $('tbody', table);
  const draw = () => {
    const get = (r) => { const v = r._sort[key]; return v == null ? -Infinity : v; };
    const sorted = [...rows].sort((a, b) => {
      const x = get(a), y = get(b);
      if (typeof x === 'string' || typeof y === 'string') return dir * String(x).localeCompare(String(y));
      return dir * (x - y);
    });
    body.innerHTML = sorted.map(render).join('') || '<tr><td class="l muted" colspan="20">Nothing to show.</td></tr>';
    table.querySelectorAll('thead th[data-k]').forEach((th) => {
      if (th.dataset.k === key) th.setAttribute('aria-sort', dir < 0 ? 'descending' : 'ascending');
      else th.removeAttribute('aria-sort');
    });
  };
  table.querySelectorAll('thead th[data-k]').forEach((th) => {
    th.innerHTML = `<button type="button">${th.innerHTML}</button>`;
    $('button', th).addEventListener('click', () => {
      const k = th.dataset.k;
      if (k === key) dir = -dir; else { key = k; dir = th.dataset.asc ? 1 : -1; }
      draw();
    });
  });
  draw();
  return { setRows(r) { rows = r; draw(); } };
}

// ---------- NBA: games ----------
async function viewGames(q, token) {
  const want = q.get('day');
  const data = await api(want ? 'nba/day?day=' + encodeURIComponent(want) : 'nba/day');
  if (token !== navToken) return;
  const { day, days, games, today } = data;
  const live = games.filter((g) => !g.completed && g.state === 'in').length;
  const final = games.filter((g) => g.completed).length;
  const evaluated = games.filter((g) => g.completed && g.pred && g.score);
  const hits = evaluated.filter((g) => (g.pred.p_home >= 0.5) === (g.score.home > g.score.away)).length;
  const upcoming = games.length - final - live;
  const counts = [
    live && `${live} live`,
    upcoming && `${upcoming} upcoming`,
    final && `${final} final`,
    evaluated.length && `${hits}/${evaluated.length} winners correct`,
  ].filter(Boolean).join(' · ');
  const index = days.findIndex((d) => d.day >= day);
  const nearest = index < 0 ? days.length : index;
  const selected = days[nearest]?.day === day;
  const previous = days[nearest - 1];
  const next = days[nearest + Number(selected)];
  const start = Math.max(0, Math.min(nearest - 3, days.length - 7));
  const visibleDays = days.slice(start, start + 7);
  const active = document.activeElement;
  const restoreFocus = active?.closest('.schedule-nav') ? active.id || 'selected' : '';
  main.innerHTML = `
    <header class="schedule-head"><div>
      <p class="schedule-league">NBA schedule</p>
      <h1><time datetime="${day}">${dayFmt(day, { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' })}</time></h1>
      <p class="schedule-zone">Schedule dates are in Eastern Time. Tip-off times are local.</p>
      <p class="schedule-counts">${counts || 'No games scheduled'}</p>
    </div><a href="#/nba/players?day=${day}" class="schedule-projections">Player projections <span aria-hidden="true">↗</span></a></header>
    <section class="schedule-nav" aria-label="NBA schedule navigation">
      <div class="schedule-toolbar">
        <div class="schedule-actions">
          <button type="button" id="previous-day" ${previous ? `data-day="${previous.day}"` : 'disabled'} aria-label="Previous game day">‹ <span>Previous</span></button>
          <button type="button" id="current-day" data-day="${today}" aria-label="Today in Eastern Time" ${today === day ? 'disabled' : ''}>Today (ET)</button>
          <button type="button" id="next-day" ${next ? `data-day="${next.day}"` : 'disabled'} aria-label="Next game day"><span>Next</span> ›</button>
        </div>
        <label for="schedule-date">Jump to date <input type="date" id="schedule-date" value="${day}"></label>
      </div>
      ${visibleDays.length ? `<div class="days" role="group" aria-label="Game dates in Eastern Time">${visibleDays.map((d) => `
      <button type="button" data-day="${d.day}" aria-pressed="${d.day === day}" title="${d.games} games">
        <span class="dow ${d.day === today ? 'today-mark' : 'muted'}">${d.day === today ? 'Today (ET)' : dayFmt(d.day, { weekday: 'short' })}</span>
        <span class="dnum">${dayFmt(d.day, { month: 'short', day: 'numeric' })}</span>
      </button>`).join('')}</div>` : ''}
    </section>
    ${games.length ? `<ul class="games">${games.map(gameCard).join('')}</ul>` : '<p class="empty">No games on this date. Choose another date to see its matchups.</p>'}
    <div class="cols" id="home-charts"></div>`;
  Promise.all([api('nba/playoffs').catch(() => null), api('nba/accuracy').catch(() => null)]).then(([po, acc]) => {
    const box = $('#home-charts');
    if (token !== navToken || !box) return;
    box.innerHTML = (po ? `<section><h2 style="margin-top:0">Who wins the title?</h2>${titleBars(po.teams, 6)}<p style="margin-top:8px"><a href="#/nba/playoffs">Playoff picture, bracket and all 30 teams</a></p></section>` : '')
      + (acc && acc.players && acc.players.length ? `<section><h2 style="margin-top:0">Top scorers: predicted against actual</h2>${scorerBars(acc.players, 6)}<p style="margin-top:8px"><a href="#/nba/accuracy">All players and prediction accuracy</a></p></section>` : '');
  });
  const nav = $('.schedule-nav', main);
  const go = (target) => {
    if (target && target !== day) location.hash = '#/nba/games?day=' + target;
  };
  nav.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-day]');
    if (b) go(b.dataset.day);
  });
  $('#schedule-date', nav).addEventListener('change', (e) => go(e.target.value));
  const strip = $('.days', nav);
  const cur = strip && $('button[aria-pressed="true"]', strip);
  if (cur) strip.scrollLeft = cur.offsetLeft - (strip.clientWidth - cur.offsetWidth) / 2;
  if (restoreFocus) {
    const target = restoreFocus === 'selected' ? cur : document.getElementById(restoreFocus);
    (target && !target.disabled ? target : cur || $('#schedule-date', nav)).focus({ preventScroll: true });
  }
}

// ---------- NBA: game detail ----------
function statCell(s, done, dnp) {
  if (done) {
    if (dnp) return '<td>–</td>';
    return `<td><span class="main">${s.a}</span><span class="sub">${f1(s.p)}</span></td>`;
  }
  return `<td><span class="main">${f1(s.p)}</span><span class="sub">${s.lo}–${s.hi}</span></td>`;
}

function boxTable(team, lines, done) {
  const shown = lines.filter((l) => l.min >= 1 || l.played);
  const rows = shown.map((l) => {
    const dnp = done && !l.played;
    const flag = l.flag === 'questionable' ? '<span class="tag">Questionable</span>' : l.flag === 'returning' ? '<span class="tag">Returning</span>' : '';
    return `<tr class="${dnp ? 'dnp' : ''}">
      <th scope="row" class="sticky"><a href="#/nba/player/${l.pid}">${esc(l.name)}</a> <span class="muted small">${esc(l.pos || '')}</span>${done ? '' : flag}${dnp ? '<span class="sub">Did not play</span>' : ''}</th>
      <td>${done ? `<span class="main">${dnp ? '–' : f1(l.act_min)}</span><span class="sub">${f1(l.min)}</span>` : `<span class="main">${f1(l.min)}</span>`}</td>
      ${STATS.map((k) => statCell(l.stats[k], done, dnp)).join('')}
    </tr>`;
  }).join('');
  const tot = (k) => shown.reduce((a, l) => a + l.stats[k].p, 0);
  const act = (k) => shown.reduce((a, l) => a + (l.played ? l.stats[k].a : 0), 0);
  return `<h2>${esc(team.name)}</h2>
  <div class="scroll"><table class="t">
    <caption class="skip">${esc(team.name)} player ${done ? 'results against projections' : 'projections'}</caption>
    <thead><tr><th scope="col" class="sticky">Player</th><th scope="col">MIN</th>${STATS.map((k) => `<th scope="col"><abbr title="${LONG[k]}">${LABEL[k]}</abbr></th>`).join('')}</tr></thead>
    <tbody>${rows}
      <tr class="total"><th scope="row" class="sticky">Team</th><td></td>${STATS.map((k) => (done
        ? `<td><span class="main">${act(k)}</span><span class="sub">${f1(tot(k))}</span></td>` : `<td>${f1(tot(k))}</td>`)).join('')}</tr>
    </tbody></table></div>`;
}

async function viewGame(id, token) {
  const g = await api('nba/game/' + id);
  if (token !== navToken) return;
  const p = g.pred, d = g.detail, done = g.completed;
  const favHome = p && p.p_home >= 0.5;
  const fav = p ? (favHome ? g.home : g.away) : null;
  const rest = (n) => (n == null ? 'season opener' : n === 0 ? 'second night of a back-to-back' : `${n} day${n > 1 ? 's' : ''} of rest`);
  const outList = (side) => {
    const rows = (d && d.out[side]) || [];
    if (!rows.length) return '<span class="muted">No one listed</span>';
    return '<ul class="plain">' + rows.map((r) => `<li><a href="#/nba/player/${r.pid}">${esc(r.name)}</a> – ${r.out ? 'out' : 'questionable'}${r.kind ? `, ${esc(r.kind.toLowerCase())}` : ''}</li>`).join('') + '</ul>';
  };
  const softest = (side) => {
    const f = d.opp_factors[side];
    const k = ['pts', 'reb', 'ast', 'fg3m'].sort((a, b) => Math.abs(f[b] - 1) - Math.abs(f[a] - 1))[0];
    const diff = (f[k] - 1) * 100;
    return `${esc(g[side].abbr)} allow ${Math.abs(diff).toFixed(0)}% ${diff >= 0 ? 'more' : 'fewer'} ${LONG[k].toLowerCase()} than average`;
  };
  const mk = marketLine(g);
  main.innerHTML = `
    <a class="crumb" href="#/nba/games?day=${g.day}">← ${dayFmt(g.day, { weekday: 'long', month: 'short', day: 'numeric' })}</a>
    <h1 class="skip">${esc(g.away.name)} at ${esc(g.home.name)}</h1>
    <section class="matchup" aria-label="Matchup">
      <div class="mu-grid">
        <div class="mu-team">${logo(g.away, 56)}<span class="nm">${esc(g.away.name)}</span><span class="muted small">${esc(g.away.record || '')} · away</span></div>
        <div class="mu-score">
          <div class="lbl">${p ? 'Predicted score' : esc(g.status || '')}</div>
          <div class="big">${p ? `${Math.round(p.away_pts)} – ${Math.round(p.home_pts)}` : '–'}</div>
          ${g.score ? `<div class="act">${done ? 'Final' : esc(g.status)}: <b>${g.score.away} – ${g.score.home}</b></div>` : `<div class="act muted">${dateTimeFmt(g.date)}</div>`}
        </div>
        <div class="mu-team">${logo(g.home, 56)}<span class="nm">${esc(g.home.name)}</span><span class="muted small">${esc(g.home.record || '')} · home</span></div>
      </div>
      ${p ? probBar(p.p_home, g.away.name, g.home.name) : ''}
      ${p && d ? `<dl class="facts">
        <div><dt>Model pick</dt><dd><b>${esc(fav.name)}</b> by ${f1(Math.abs(p.margin))} (${pct(Math.max(p.p_home, 1 - p.p_home))}), total ${f1(p.total)}</dd></div>
        ${mk ? `<div><dt>Betting line</dt><dd>${mk}</dd></div>` : ''}
        ${p.line_used ? `<div><dt>Stats model alone</dt><dd>${esc(p.model_margin >= 0 ? g.home.abbr : g.away.abbr)} by ${f1(Math.abs(p.model_margin))}</dd></div>` : ''}
        <div><dt>Rest</dt><dd>${esc(g.away.abbr)}: ${rest(d.rest.away)}<br>${esc(g.home.abbr)}: ${rest(d.rest.home)}</dd></div>
        <div><dt>Defence</dt><dd>${softest('away')}<br>${softest('home')}</dd></div>
        ${!done ? `<div class="wide"><dt>${esc(g.away.abbr)} injuries</dt><dd>${outList('away')}</dd></div><div class="wide"><dt>${esc(g.home.abbr)} injuries</dt><dd>${outList('home')}</dd></div>` : ''}
      </dl>` : ''}
    </section>
    <p class="muted small" style="margin-top:8px">${sourceNote(g)} ${done ? 'Each cell shows the actual figure with the projection beneath it.' : 'Each cell shows the projection with the likely range (8 in 10 games) beneath it.'}</p>
    ${d ? boxTable(g.away, d.players.away, done) + boxTable(g.home, d.players.home, done) : '<p class="empty">No player projections for this game.</p>'}`;
}

// ---------- NBA: players (daily projections + search) ----------
async function viewPlayers(q, token) {
  const want = q.get('day');
  const [meta, idx] = await Promise.all([api(want ? 'nba/day?day=' + encodeURIComponent(want) : 'nba/day'), api('nba/players')]);
  if (token !== navToken) return;
  const day = meta.day;
  const data = { players: [] };
  meta.games.forEach((g) => {
    if (!g.detail) return;
    [['home', 'away'], ['away', 'home']].forEach(([side, other]) => g.detail.players[side].forEach((l) => {
      if (l.min >= 1) data.players.push({ ...l, game_id: g.id, team: g[side].abbr, opp: g[other].abbr, home: side === 'home' });
    }));
  });
  const withGames = meta.days.filter((d) => d.day >= meta.today).slice(0, 8);
  if (!withGames.some((d) => d.day === day)) withGames.unshift({ day });
  const anyActual = data.players.some((p) => p.played != null);
  const rows = data.players.map((p) => ({
    ...p,
    _sort: { name: p.name, team: p.team, min: p.min, ...Object.fromEntries(STATS.map((k) => [k, p.stats[k].p])), pra: p.stats.pts.p + p.stats.reb.p + p.stats.ast.p },
  }));
  const teams = [...new Set(rows.map((r) => r.team))].sort();
  main.innerHTML = `
    <div class="head"><div><h1>Player projections</h1><p class="muted">${dayFmt(day, { weekday: 'long', month: 'long', day: 'numeric' })} · ${rows.length} players${anyActual ? ' · games already played show the pre-game projection' : ''}</p></div></div>
    <div class="controls">
      <label class="f">Date<select id="day">${withGames.map((d) => `<option value="${d.day}" ${d.day === day ? 'selected' : ''}>${d.day === meta.today ? 'Today, ' : ''}${dayFmt(d.day, { weekday: 'short', month: 'short', day: 'numeric' })}</option>`).join('')}</select></label>
      <label class="f">Team<select id="team"><option value="">All teams</option>${teams.map((t) => `<option>${esc(t)}</option>`).join('')}</select></label>
      <label class="f">Find any player<input type="search" id="find" placeholder="Type a name" autocomplete="off"></label>
    </div>
    <ul class="results" id="results" hidden></ul>
    <div class="scroll"><table class="t" id="proj">
      <caption class="skip">Projected stat lines, sortable by column</caption>
      <thead><tr><th scope="col" data-k="name" data-asc="1" class="sticky">Player</th><th scope="col" data-k="team" data-asc="1" class="l hide-sm">Game</th>
        <th scope="col" data-k="min">MIN</th>${STATS.map((k) => `<th scope="col" data-k="${k}"><abbr title="${LONG[k]}">${LABEL[k]}</abbr></th>`).join('')}
        <th scope="col" data-k="pra"><abbr title="Points + rebounds + assists">PRA</abbr></th></tr></thead>
      <tbody></tbody></table></div>
    <p class="muted small" style="margin-top:8px">The small figures are the likely range: about 8 in 10 games land inside it. Select a column heading to sort.</p>`;
  const render = (p) => `<tr>
    <th scope="row" class="sticky"><a href="#/nba/player/${p.pid}">${esc(p.name)}</a> <span class="muted small">${esc(p.pos || '')}</span>${p.flag === 'questionable' ? '<span class="tag">Q</span>' : ''}<span class="sub show-sm">${esc(p.team)} ${p.home ? 'vs' : '@'} ${esc(p.opp)}</span></th>
    <td class="l hide-sm"><a href="#/nba/game/${p.game_id}">${esc(p.team)} ${p.home ? 'vs' : '@'} ${esc(p.opp)}</a></td>
    <td>${f1(p.min)}</td>
    ${STATS.map((k) => `<td><span class="main">${f1(p.stats[k].p)}</span><span class="sub">${p.stats[k].lo}–${p.stats[k].hi}</span></td>`).join('')}
    <td>${f1(p._sort.pra)}</td></tr>`;
  const tbl = sortable($('#proj'), rows, render, { key: 'pts', dir: -1 });
  currentPaths.push('nba/players');
  $('#day').addEventListener('change', (e) => { location.hash = '#/nba/players?day=' + e.target.value; });
  $('#team').addEventListener('change', (e) => tbl.setRows(e.target.value ? rows.filter((r) => r.team === e.target.value) : rows));
  const res = $('#results');
  $('#find').addEventListener('input', (e) => {
    const s = e.target.value.trim().toLowerCase();
    if (s.length < 2) { res.hidden = true; return; }
    const hits = idx.players.filter((p) => (p.name || '').toLowerCase().includes(s)).slice(0, 12);
    res.hidden = false;
    res.innerHTML = hits.length ? hits.map((p) => `<li><a href="#/nba/player/${p.id}"><span>${esc(p.name)}</span><span class="muted">${esc(p.team || 'No team')} · ${esc(p.pos || '')}</span></a></li>`).join('')
      : '<li><a><span class="muted">No player matches that name.</span></a></li>';
  });
}

// ---------- NBA: player detail ----------
function historyChart(log, stat, seasonAvg) {
  const games = log.filter((r) => r.played).slice(0, 20).reverse();
  if (!games.length) return '<p class="muted">No games played yet.</p>';
  const W = 720, H = 220, L = 30, B = 34, T = 14;
  const max = Math.max(4, ...games.map((r) => Math.max(r.stats[stat], r.proj ? r.proj[stat] : 0))) * 1.12;
  const bw = (W - L - 8) / games.length;
  const y = (v) => T + (H - T - B) * (1 - v / max);
  const step = max > 40 ? 10 : max > 16 ? 5 : max > 8 ? 2 : 1;
  let grid = '';
  for (let v = 0; v <= max; v += step) grid += `<line class="grid" x1="${L}" x2="${W}" y1="${y(v)}" y2="${y(v)}"/><text x="${L - 5}" y="${y(v) + 3}" text-anchor="end">${v}</text>`;
  const bars = games.map((r, i) => {
    const x = L + 4 + i * bw, v = r.stats[stat], w = Math.max(bw - 6, 3);
    const pr = r.proj ? `<line class="proj" x1="${x - 1}" x2="${x + w + 1}" y1="${y(r.proj[stat])}" y2="${y(r.proj[stat])}"/>` : '';
    return `<g><title>${shortDate(r.day + 'T12:00:00Z')} ${r.home ? 'vs' : '@'} ${r.opp}: ${v}${r.proj ? `, projected ${r.proj[stat]}` : ''}</title>
      <rect class="bar-r ${r.won ? 'win' : ''}" x="${x}" y="${y(v)}" width="${w}" height="${Math.max(y(0) - y(v), 0)}"/>${pr}
      <text class="val" x="${x + w / 2}" y="${Math.min(y(v), r.proj ? y(r.proj[stat]) : H) - 4}" text-anchor="middle">${v}</text>
      <text x="${x + w / 2}" y="${H - B + 13}" text-anchor="middle">${esc(r.opp || '')}</text>
      <text x="${x + w / 2}" y="${H - B + 25}" text-anchor="middle">${new Date(r.day + 'T12:00:00Z').toLocaleDateString(undefined, { timeZone: 'UTC', month: 'numeric', day: 'numeric' })}</text></g>`;
  }).join('');
  const avg = seasonAvg != null ? `<line class="avg" x1="${L}" x2="${W}" y1="${y(seasonAvg)}" y2="${y(seasonAvg)}"/>` : '';
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${LONG[stat]} in the last ${games.length} games, with the pre-game projection for each">${grid}${bars}${avg}</svg>
    <div class="legend"><span><i class="b"></i>Actual (darker = team won)</span><span><i></i>Projected before the game</span><span><i class="a"></i>Season average</span></div>`;
}

// Whole season: what was projected before each game against what the player did, with the likely range.
function compareChart(rows, i, stat) {
  const narrow = window.innerWidth < 560;
  const W = narrow ? 380 : 720, H = 230, L = 30, B = 22, T = 12, R = 10;
  const max = Math.max(4, ...rows.map((r) => Math.max(r.act[i], r.hi[i], r.proj[i]))) * 1.08;
  const x = (k) => (rows.length === 1 ? (L + W - R) / 2 : L + 6 + k * (W - L - R - 12) / (rows.length - 1));
  const y = (v) => T + (H - T - B) * (1 - v / max);
  const step = max > 40 ? 10 : max > 16 ? 5 : max > 8 ? 2 : 1;
  let grid = '';
  for (let v = 0; v <= max; v += step) grid += `<line class="grid" x1="${L}" x2="${W}" y1="${y(v)}" y2="${y(v)}"/><text x="${L - 5}" y="${y(v) + 3}" text-anchor="end">${v}</text>`;
  const pts = (f) => rows.map((r, k) => `${x(k).toFixed(1)},${y(f(r)).toFixed(1)}`);
  const band = [...pts((r) => r.hi[i]), ...pts((r) => r.lo[i]).reverse()].join(' ');
  const every = Math.ceil(rows.length / (narrow ? 6 : 12));
  const marks = rows.map((r, k) => `<g><title>${shortDate(r.day + 'T12:00:00Z')} ${r.home ? 'vs' : '@'} ${r.opp}: predicted ${r.proj[i]}, actual ${r.act[i]}</title>
    <circle class="act-dot ${r.act[i] < r.lo[i] || r.act[i] > r.hi[i] ? 'out' : ''}" cx="${x(k)}" cy="${y(r.act[i])}" r="3.5"/>
    ${k % every === 0 ? `<text x="${x(k)}" y="${H - 6}" text-anchor="middle">${new Date(r.day + 'T12:00:00Z').toLocaleDateString(undefined, { timeZone: 'UTC', month: 'numeric', day: 'numeric' })}</text>` : ''}</g>`).join('');
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${LONG[stat]}: predicted and actual in each of ${rows.length} games this season">${grid}
    ${rows.length > 1 ? `<polygon class="band" points="${band}"/><polyline class="act-line" points="${pts((r) => r.act[i]).join(' ')}"/><polyline class="proj-line" points="${pts((r) => r.proj[i]).join(' ')}"/>` : ''}${marks}</svg>
    <div class="legend"><span><i class="b"></i>Actual</span><span><i></i>Predicted before the game</span><span><i class="band"></i>Likely range</span><span><i class="dot"></i>Outside the range</span></div>`;
}
function compareBlock(rows, stat) {
  const i = STATS.indexOf(stat), n = rows.length;
  if (!n) return '<p class="muted">No finished games with a prediction yet this season.</p>';
  const mean = (f) => rows.reduce((t, r) => t + f(r), 0) / n;
  const pred = mean((r) => r.proj[i]), act = mean((r) => r.act[i]), miss = mean((r) => Math.abs(r.proj[i] - r.act[i]));
  const inside = rows.filter((r) => r.act[i] >= r.lo[i] && r.act[i] <= r.hi[i]).length;
  return `<div class="tiles">
      <div class="tile"><div class="k">Games compared</div><div class="v">${n}</div><div class="s">this season</div></div>
      <div class="tile"><div class="k">Predicted average</div><div class="v">${pred.toFixed(1)}</div><div class="s">${LONG[stat].toLowerCase()} per game</div></div>
      <div class="tile"><div class="k">Actual average</div><div class="v">${act.toFixed(1)}</div><div class="s">${signed(act - pred)} against prediction</div></div>
      <div class="tile"><div class="k">Average miss</div><div class="v">${miss.toFixed(1)}</div><div class="s">per game, either way</div></div>
      <div class="tile"><div class="k">Inside the range</div><div class="v">${inside} of ${n}</div><div class="s">aim is about 8 in 10</div></div>
    </div>
    <div class="chart">${compareChart(rows, i, stat)}</div>
    <div class="scroll" style="margin-top:12px;max-height:420px;overflow-y:auto"><table class="t wrap-sm"><caption class="skip">${LONG[stat]}: predicted against actual, game by game</caption>
      <thead><tr><th scope="col">Date</th><th scope="col" class="l">Opponent</th><th scope="col">Predicted</th><th scope="col" class="hide-sm">Likely range</th><th scope="col">Actual</th><th scope="col"><abbr title="Actual minus predicted">Diff</abbr></th></tr></thead>
      <tbody>${[...rows].reverse().map((r) => { const d = r.act[i] - r.proj[i], out = r.act[i] < r.lo[i] || r.act[i] > r.hi[i];
        return `<tr><th scope="row"><a href="#/nba/game/${r.game_id}">${shortDate(r.day + 'T12:00:00Z')}</a></th><td class="l">${r.home ? 'vs' : '@'} ${esc(r.opp || '')}</td><td>${f1(r.proj[i])}</td><td class="hide-sm">${r.lo[i]}–${r.hi[i]}</td><td class="main">${r.act[i]}</td><td class="${out ? 'warn' : ''}">${signed(d)}${out ? ' <span class="small hide-sm">outside</span>' : ''}</td></tr>`; }).join('')}</tbody></table></div>
    <p class="muted small" style="margin-top:8px">${rows.some((r) => r.locked) ? 'Games where a prediction was frozen before tip-off use that one. The others' : 'Predictions'} are rebuilt from earlier games only, knowing who was in uniform.</p>`;
}

function shotChart(sh) {
  if (!sh || !sh.shots.length) return '<p class="muted">No shots recorded this season.</p>';
  // Court in feet: x 0-50 across, hoop at (25, 0), baseline 5.25 ft behind it. Drawn with the hoop at the top.
  const X = (x) => x * 10, Y = (y) => (Math.min(y, 33) + 5.25) * 10;
  const arcY = Math.sqrt(23.75 ** 2 - 22 ** 2);
  const court = `<rect class="ln" x="0" y="0" width="500" height="390"/>
    <rect class="ln" x="170" y="0" width="160" height="190"/>
    <path class="ln" d="M190 190 A60 60 0 0 0 310 190"/>
    <path class="ln" d="M30 0 V${Y(arcY)} A237.5 237.5 0 0 0 470 ${Y(arcY)} V0"/>
    <path class="ln" d="M210 ${Y(0)} A40 40 0 0 0 290 ${Y(0)}"/>
    <line class="ln" x1="220" x2="280" y1="40" y2="40"/><circle class="ln" cx="250" cy="${Y(0)}" r="7.5"/>`;
  const dots = sh.shots.map(([x, y, made]) => (made
    ? `<circle class="mk" cx="${X(x)}" cy="${Y(y)}" r="4.2"/>`
    : `<path class="ms" d="M${X(x) - 3.4} ${Y(y) - 3.4}l6.8 6.8m0-6.8l-6.8 6.8"/>`)).join('');
  const made = sh.shots.filter((x) => x[2]).length;
  const rows = sh.zones.map((z) => {
    const diff = z.pct != null && z.league != null && z.att >= 8 ? z.pct - z.league : null;
    return `<tr><th scope="row" class="l">${esc(z.label)}</th><td>${z.made}/${z.att}</td><td class="main">${pct(z.pct, 1)}</td><td>${pct(z.league, 1)}</td>
      <td class="${diff == null ? 'muted' : diff > 0.02 ? 'good' : diff < -0.02 ? 'bad' : ''}">${diff == null ? '–' : (diff > 0 ? '+' : '') + (diff * 100).toFixed(1)}</td><td class="hide-sm">${pct(z.share)}</td>
      <td class="hide-sm">${z.pct == null ? '–' : (z.pct * z.value).toFixed(2)}</td></tr>`;
  }).join('');
  return `<div class="cols shotwrap">
    <div class="chart court"><svg viewBox="-2 -2 504 394" role="img" aria-label="Shot chart: ${made} made of ${sh.shots.length} field-goal attempts in ${sh.games} games this season">${court}${dots}</svg>
      <div class="legend"><span><i class="dot"></i>Made</span><span><b class="x">×</b> Missed</span><span>${made} of ${sh.shots.length} (${pct(made / sh.shots.length, 1)}) in ${sh.games} games</span></div></div>
    <div><div class="scroll"><table class="t wrap-sm"><caption class="skip">Shooting by court zone against the league average</caption>
      <thead><tr><th scope="col">Zone</th><th scope="col">Made</th><th scope="col">FG%</th><th scope="col">League</th><th scope="col">+/-</th><th scope="col" class="hide-sm"><abbr title="Share of the player's shots taken here">Share</abbr></th><th scope="col" class="hide-sm"><abbr title="Points per shot">PPS</abbr></th></tr></thead>
      <tbody>${rows}</tbody></table></div>
      <p class="muted small" style="margin-top:6px">This season's field-goal attempts (free throws excluded). +/- is percentage points against the league in that zone, shown from 8 attempts.</p></div>
  </div>`;
}

async function viewPlayer(id, token) {
  const p = await api('nba/player/' + id);
  if (token !== navToken) return;
  const a = p.averages, nx = p.next, line = nx && nx.line;
  const bits = [p.team && p.team.name, p.pos, p.jersey && '#' + p.jersey, p.height, p.age && p.age + ' yrs'].filter(Boolean).map(esc).join(' · ');
  const avgRow = (label, r) => (r ? `<tr><th scope="row">${label}</th><td>${r.gp}</td><td>${f1(r.min)}</td>${STATS.map((k) => `<td>${f1(r[k])}</td>`).join('')}</tr>` : '');
  let nextHtml = '<p class="notice">No upcoming game in the next week for this player\'s team.</p>';
  if (nx) {
    const g = nx.game;
    const where = `${nx.home ? 'vs' : 'at'} ${esc(nx.opp.name)}, ${dateTimeFmt(g.date)}`;
    if (line) {
      const of = nx.opp_factors;
      const notes = [];
      const dp = (of.pts - 1) * 100;
      if (Math.abs(dp) >= 1.5) notes.push(`${esc(nx.opp.abbr)} allow ${Math.abs(dp).toFixed(0)}% ${dp > 0 ? 'more' : 'fewer'} points than average`);
      notes.push(nx.home ? 'home game' : 'road game');
      if (nx.rest === 0) notes.push('second night of a back-to-back');
      if (line.flag === 'questionable') notes.push('listed as questionable, so minutes are uncertain');
      if (line.flag === 'returning') notes.push('coming back from an absence, so minutes are reduced');
      nextHtml = `<p>${where} · <a href="#/nba/game/${g.id}">open game</a><br><span class="muted">${notes.join(' · ')}</span></p>
        <div class="tiles">
          <div class="tile"><div class="k">Minutes</div><div class="v">${f1(line.min)}</div><div class="s">season ${a.season ? f1(a.season.min) : '–'}</div></div>
          ${STATS.map((k) => `<div class="tile"><div class="k">${LONG[k]}</div><div class="v">${f1(line.stats[k].p)}</div><div class="s">range ${line.stats[k].lo}–${line.stats[k].hi} · avg ${f1(line.avg[k])}</div></div>`).join('')}
        </div>
        <div class="calc">
          <label class="f">Stat<select id="c-stat">${STATS.map((k) => `<option value="${k}">${LONG[k]}</option>`).join('')}</select></label>
          <label class="f">Line<input type="number" id="c-line" step="0.5" min="0" inputmode="decimal"></label>
          <div><div class="muted small">Chance of going over</div><output id="c-out" for="c-stat c-line">–</output></div>
        </div>`;
    } else {
      const why = p.injury ? `listed as ${esc(p.injury.status.toLowerCase())}` : 'not expected to play';
      nextHtml = `<p>${where} · <a href="#/nba/game/${g.id}">open game</a></p><p class="notice">No projection: ${why}.</p>`;
    }
  }
  main.innerHTML = `
    <a class="crumb" href="#/nba/players">← Player projections</a>
    <div class="phead">
      ${p.headshot ? `<img class="shot" src="${img(p.headshot, 240, 176)}" alt="" width="120" height="88">` : ''}
      <div><h1>${esc(p.name)}</h1><p class="muted" style="margin:4px 0 0">${bits}</p></div>
    </div>
    ${p.injury ? `<p class="notice"><b>${esc(p.injury.status)}</b>${p.injury.kind ? ` (${esc(p.injury.kind.toLowerCase())})` : ''}. ${esc(p.injury.comment || '')}</p>` : ''}
    <h2>Next game</h2>${nextHtml}
    <h2>Predicted against actual</h2>
    <div class="controls"><div class="seg" role="group" aria-label="Stat to compare">${STATS.map((k, i) => `<button type="button" data-stat="${k}" aria-pressed="${i === 0}">${LABEL[k]}</button>`).join('')}</div></div>
    <div id="compare"></div>
    <h3 style="margin:18px 0 8px">Last 20 games</h3>
    <div class="chart" id="chart"></div>
    <h2>Shot chart</h2>
    ${shotChart(p.shooting)}
    <h2>Averages</h2>
    <div class="scroll"><table class="t"><thead><tr><th scope="col">Split</th><th scope="col">GP</th><th scope="col">MIN</th>${STATS.map((k) => `<th scope="col">${LABEL[k]}</th>`).join('')}</tr></thead>
      <tbody>${avgRow('This season', a.season)}${avgRow('Last 5', a.last5)}${avgRow('Last 10', a.last10)}${avgRow('Home', a.home)}${avgRow('Away', a.away)}${avgRow('Last season', a.previous)}${avgRow('Last playoffs', a.playoffs)}</tbody></table></div>
    <h2>Game log</h2>
    <div class="scroll"><table class="t"><caption class="skip">Game log with pre-game projections</caption>
      <thead><tr><th scope="col">Date</th><th scope="col" class="l">Opponent</th><th scope="col">MIN</th>${STATS.map((k) => `<th scope="col">${LABEL[k]}</th>`).join('')}</tr></thead>
      <tbody>${p.log.map((r) => `<tr class="${r.played ? '' : 'dnp'}">
        <th scope="row"><a href="#/nba/game/${r.game_id}">${shortDate(r.day + 'T12:00:00Z')}</a></th>
        <td class="l">${r.home ? 'vs' : '@'} ${esc(r.opp || '')} ${r.won == null ? '' : r.won ? '<span class="good">W</span>' : '<span class="bad">L</span>'}</td>
        ${r.played ? `<td>${f1(r.min)}</td>${STATS.map((k) => `<td><span class="main">${r.stats[k]}</span>${r.proj ? `<span class="sub">${f1(r.proj[k])}</span>` : ''}</td>`).join('')}` : `<td class="l" colspan="${STATS.length + 1}">Did not play</td>`}
      </tr>`).join('') || '<tr><td class="l muted" colspan="10">No games on record.</td></tr>'}</tbody></table></div>
    <p class="muted small" style="margin-top:8px">Under each figure is what the model projected before that game (this season only).</p>`;
  const drawChart = (k) => {
    $('#chart').innerHTML = historyChart(p.log.filter((r) => r.season === p.season || !a.season), k, a.season ? a.season[k] : null);
    $('#compare').innerHTML = compareBlock(p.compare || [], k);
  };
  drawChart('pts');
  $('.seg').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-stat]');
    if (!b) return;
    $('.seg').querySelectorAll('button').forEach((x) => x.setAttribute('aria-pressed', x === b));
    drawChart(b.dataset.stat);
  });
  if (line) {
    const st = $('#c-stat'), ln = $('#c-line'), out = $('#c-out');
    const calc = () => {
      const k = st.value, v = parseFloat(ln.value);
      if (Number.isNaN(v) || v < 0) { out.textContent = '–'; return; }
      const need = Math.floor(v) + 1;
      out.textContent = `${pct(probAtLeast(line.stats[k].p, nx.variance[k], need))} for ${need}+`;
    };
    const reset = () => { ln.value = Math.max(Math.round(line.stats[st.value].p) - 0.5, 0.5); calc(); };
    st.addEventListener('change', reset);
    ln.addEventListener('input', calc);
    reset();
  }
}

// ---------- NBA: teams ----------
async function viewTeams(token) {
  const d = await api('nba/teams');
  if (token !== navToken) return;
  const rows = d.teams.map((t) => ({ ...t, _sort: { name: t.name, w: t.w / Math.max(t.w + t.l, 1), net: t.net, off: t.off, def: t.def, ppg: t.ppg, apts: -t.allowed[0], areb: -t.allowed[1], aast: -t.allowed[2], a3: -t.allowed[3] } }));
  const rel = (v) => { const x = (v - 1) * 100; return `<span class="${x > 2 ? 'bad' : x < -2 ? 'good' : ''}">${x > 0 ? '+' : ''}${x.toFixed(1)}%</span>`; };
  main.innerHTML = `
    <div class="head"><div><h1>Team ratings</h1><p class="muted">Points per game better or worse than an average team, adjusted for opponents. League average ${d.league_avg} points; home court is worth ${d.home_edge}.</p></div></div>
    <div class="scroll"><table class="t" id="teams"><caption class="skip">Team ratings, sortable</caption>
      <thead><tr><th scope="col" data-k="name" data-asc="1" class="sticky">Team</th><th scope="col" data-k="w">Record</th><th scope="col" data-k="net">Net</th><th scope="col" data-k="off">Offence</th><th scope="col" data-k="def">Defence</th><th scope="col" data-k="ppg">PPG</th>
        <th scope="col" data-k="apts"><abbr title="Points allowed versus league average">Opp PTS</abbr></th><th scope="col" data-k="areb"><abbr title="Rebounds allowed versus league average">Opp REB</abbr></th><th scope="col" data-k="aast"><abbr title="Assists allowed versus league average">Opp AST</abbr></th><th scope="col" data-k="a3"><abbr title="Threes allowed versus league average">Opp 3PM</abbr></th></tr></thead>
      <tbody></tbody></table></div>
    <p class="muted small" style="margin-top:8px">The "Opp" columns show what each defence gives up compared with the league. Player projections are moved by these.</p>`;
  sortable($('#teams'), rows, (t) => `<tr><th scope="row" class="sticky"><div class="team">${logo(t, 22)}<span>${esc(t.name)}</span></div></th>
    <td>${t.w}-${t.l}</td><td class="main">${signed(t.net)}</td><td>${signed(t.off)}</td><td>${signed(t.def)}</td><td>${f1(t.ppg)}</td>
    <td>${rel(t.allowed[0])}</td><td>${rel(t.allowed[1])}</td><td>${rel(t.allowed[2])}</td><td>${rel(t.allowed[3])}</td></tr>`, { key: 'net', dir: -1 });
}

// ---------- NBA: awards ----------
async function viewAwards(token) {
  const d = await api('nba/awards');
  if (token !== navToken) return;
  const yr = (s) => `${s - 1}-${String(s).slice(2)}`;
  const COLS = {
    mvp: [['PTS', 'pts'], ['REB', 'reb'], ['AST', 'ast']],
    roy: [['PTS', 'pts'], ['REB', 'reb'], ['AST', 'ast']],
    dpoy: [['REB', 'reb'], ['STL', 'stl'], ['BLK', 'blk']],
    smoy: [['PTS', 'pts'], ['REB', 'reb'], ['AST', 'ast']],
    mip: [['PTS', 'pts'], ['<abbr title="Points per game last season">Was</abbr>', 'prev_pts'], ['AST', 'ast']],
  };
  const block = (key) => {
    const rows = d.awards[key] || [];
    if (!rows.length) return '';
    const lead = rows[0], cols = COLS[key] || COLS.mvp;
    return `<section><h2>${esc(d.names[key])}</h2>
      <p class="muted" style="margin-top:-4px">Front-runner: <b style="color:var(--text)">${esc(lead.name)}</b> (${esc(lead.team)}), ${pct(lead.chance)} chance.</p>
      <div class="scroll"><table class="t award"><caption class="skip">${esc(d.names[key])} candidates</caption>
        <thead><tr><th scope="col">Player</th><th scope="col">Chance</th><th scope="col" class="hide-sm"><abbr title="Games played">GP</abbr></th>${cols.map((c) => `<th scope="col">${c[0]}</th>`).join('')}<th scope="col" class="hide-sm"><abbr title="Team record, and wins projected over 82 games">Team</abbr></th></tr></thead>
        <tbody>${rows.map((r, i) => `<tr><th scope="row"><span class="rk">${i + 1}</span><a href="#/nba/player/${r.pid}">${esc(r.name)}</a> <span class="muted tm">${esc(r.team)}${r.at_risk ? ' <span class="tag" title="On pace to miss the 65-game minimum">at risk</span>' : ''}</span></th>
          <td class="main"><span class="meter"><i style="width:${Math.round(r.chance / rows[0].chance * 100)}%"></i></span>${pct(r.chance)}</td><td class="hide-sm">${r.gp}</td>${cols.map((c) => `<td>${f1(r[c[1]])}</td>`).join('')}<td class="hide-sm">${r.w}-${r.l} <span class="muted small">→ ${r.proj_w}</span></td></tr>`).join('')}</tbody></table></div></section>`;
  };
  const hist = d.history || [];
  const top3 = hist.filter((h) => h.rank_now && h.rank_now <= 3).length, top3end = hist.filter((h) => h.rank_end && h.rank_end <= 3).length;
  main.innerHTML = `
    <div class="head"><div><h1>Award predictions</h1><p class="muted">${yr(d.season)} season, ${pct(d.played)} played. Chances are rough and will move a lot this early.</p></div></div>
    <div class="cols award-cols">${Object.keys(d.names).map(block).join('')}</div>
    <h2>Would this have worked before?</h2>
    <p class="muted prose">The same method replayed on earlier seasons. At this date the eventual winner was in the model's top three in ${top3} of ${hist.length} cases; by the end of the season, ${top3end} of ${hist.length}. Awards are decided by voters, so defence, story and reputation count in ways box scores do not capture.</p>
    <div class="scroll" style="max-width:860px"><table class="t wrap-sm"><caption class="skip">Where past winners ranked in this model</caption>
      <thead><tr><th scope="col" class="hide-sm">Season</th><th scope="col" class="l">Award</th><th scope="col" class="l">Winner</th><th scope="col"><abbr title="Where this model ranked the eventual winner at this point of that season">At this date</abbr></th><th scope="col"><abbr title="Where this model ranked the eventual winner when that season ended">At season end</abbr></th></tr></thead>
      <tbody>${hist.map((h) => `<tr><th scope="row" class="hide-sm">${yr(h.season)}</th><td class="l">${esc(d.names[h.award])}<span class="sub show-sm">${yr(h.season)}</span></td><td class="l">${esc(h.winner)}</td><td>${h.rank_now || 'outside 25'}</td><td class="main">${h.rank_end || 'outside 25'}</td></tr>`).join('')}</tbody></table></div>
    <div class="prose" style="margin-top:14px"><p class="muted">How it scores: MVP is all-round production scaled by the team's projected wins. Rookie is production among first-year players. Defensive Player weighs blocks, steals, rebounds and the team's defence. Sixth Man is scoring by players who mostly come off the bench. Most Improved is the jump from last season. Players on pace to miss the 65-game minimum are marked "at risk" or left out.</p></div>`;
}

// ---------- NBA: accuracy ----------
function accuracyBlock(s, label) {
  if (!s || !s.games) return `<p class="notice">No finished games to score yet for ${label}.</p>`;
  const w = s.winner;
  const best = (a, b) => (a < b ? 'good' : a > b ? 'bad' : '');
  return `
    <div class="band">
      <div><div class="k">Winners picked</div><div class="v">${pct(w.model, 1)}</div><div class="s">${s.games} games${s.stats_only ? `; stats model alone ${pct(s.stats_only.winner, 1)}` : ''}</div></div>
      <div><div class="k">Win-probability error</div><div class="v">${w.brier?.toFixed(4) ?? '–'}</div><div class="s">Brier score · lower is better</div></div>
      <div><div class="k">Betting favourite</div><div class="v">${pct(w.market, 1)}</div><div class="s">${w.market_games} games with a line</div></div>
      <div><div class="k">Better record</div><div class="v">${pct(w.record, 1)}</div><div class="s">simple rule</div></div>
      <div><div class="k">Always home team</div><div class="v">${pct(w.home, 1)}</div><div class="s">simple rule</div></div>
      <div><div class="k">Margin error</div><div class="v">${s.margin_mae}</div><div class="s">points; line ${s.market_margin_mae ?? '–'}${s.stats_only ? `, stats alone ${s.stats_only.margin_mae}` : ''}</div></div>
      <div><div class="k">Total error</div><div class="v">${s.total_mae}</div><div class="s">points; line ${s.market_total_mae ?? '–'}${s.stats_only ? `, stats alone ${s.stats_only.total_mae}` : ''}</div></div>
    </div>
    <div class="cols">
      <div><h3 style="margin:14px 0 8px">Player stat lines</h3>
        <div class="scroll"><table class="t wrap-sm"><caption class="skip">Average miss per player per game, model against simple baselines</caption>
          <thead><tr><th scope="col">Stat</th><th scope="col">Model</th><th scope="col">Season avg</th><th scope="col">Last 5 avg</th><th scope="col">In range</th></tr></thead>
          <tbody>${s.stats.map((x) => `<tr><th scope="row">${LONG[x.stat]}</th><td class="main ${best(x.mae, Math.min(x.season_avg_mae, x.last5_mae))}">${x.mae.toFixed(2)}</td><td>${x.season_avg_mae.toFixed(2)}</td><td>${x.last5_mae.toFixed(2)}</td><td>${pct(x.in_range)}</td></tr>`).join('')}</tbody></table></div>
        <p class="muted small" style="margin-top:6px">Average miss per player per game (lower is better) over ${s.player_lines.toLocaleString()} lines for players projected 12+ minutes. Minutes miss by ${s.minutes_mae} on average. "In range" is how often the real figure fell inside the range shown; ranges are built to cover at least 80%, and more for low-count stats because they can only be whole numbers.</p></div>
      <div><h3 style="margin:14px 0 8px">Are the win chances honest?</h3>
        <div class="scroll"><table class="t wrap-sm"><caption class="skip">Predicted win chance against how often that side won</caption>
          <thead><tr><th scope="col">Model said</th><th scope="col">Games</th><th scope="col">Average chance</th><th scope="col">Actually won</th></tr></thead>
          <tbody>${s.calibration.map((c) => `<tr><th scope="row">${c.bucket}</th><td>${c.games}</td><td>${pct(c.predicted)}</td><td class="main">${pct(c.actual)}</td></tr>`).join('')}</tbody></table></div>
        <p class="muted small" style="margin-top:6px">When the model gives a side 70-80%, that side should win about that often.</p></div>
    </div>`;
}

async function viewAccuracy(token) {
  const d = await api('nba/accuracy');
  if (token !== navToken) return;
  const yr = (s) => `${s - 1}-${String(s).slice(2)}`;
  const lk = d.locked;
  main.innerHTML = `
    <div class="head"><div><h1>How accurate is it?</h1><p class="muted">Every finished game is re-predicted using only what was known before it, then compared with what happened.</p></div></div>
    <h2 style="margin-top:8px">${yr(d.season)} season so far</h2>
    ${accuracyBlock(d.summary, 'this season')}
    <h2>Locked predictions against real scores</h2>
    <p class="muted">Frozen before tip-off, then compared with the final score. ${lk.games ? `<b style="color:var(--text)">${lk.correct} of ${lk.games}</b> winners correct so far${lk.line_games ? `; the betting favourite won ${lk.line_correct} of ${lk.line_games} of the same games` : ''}.` : 'None has finished yet.'}</p>
    ${d.locked_games.length ? `<div class="scroll"><table class="t wrap-sm"><caption class="skip">Locked predictions and results</caption>
      <thead><tr><th scope="col" class="hide-sm">Date</th><th scope="col" class="l">Game</th><th scope="col">Predicted</th><th scope="col">Actual</th><th scope="col" class="hide-sm"><abbr title="Error in the predicted margin, in points">Margin miss</abbr></th><th scope="col">Winner</th></tr></thead>
      <tbody>${d.locked_games.map((g) => { const miss = Math.abs((g.pred[1] - g.pred[0]) - (g.actual[1] - g.actual[0]));
        return `<tr><th scope="row" class="hide-sm">${shortDate(g.day + 'T12:00:00Z')}</th><td class="l"><a href="#/nba/game/${g.id}">${esc(g.away)} @ ${esc(g.home)}</a><span class="sub show-sm">${shortDate(g.day + 'T12:00:00Z')}</span></td><td>${Math.round(g.pred[0])}–${Math.round(g.pred[1])}</td><td class="main">${g.actual[0]}–${g.actual[1]}</td><td class="hide-sm">${miss.toFixed(1)}</td><td class="${g.hit ? 'good' : 'bad'}">${g.hit ? 'called' : 'missed'}</td></tr>`; }).join('')}</tbody></table></div>` : ''}
    ${d.players && d.players.length ? `<h2>Player predictions against actual</h2>
    <p class="muted">Per-game averages this season for the ${d.players.length} highest scorers with five or more games: what was predicted before each game, what happened, and the average miss.</p>
    <div style="max-width:860px;margin-bottom:12px">${scorerBars(d.players, 12)}</div>
    <div class="scroll" style="max-height:520px;overflow-y:auto"><table class="t" id="pacc"><caption class="skip">Predicted and actual averages by player, sortable</caption>
      <thead><tr><th scope="col" data-k="name" data-asc="1" class="sticky">Player</th><th scope="col" data-k="gp"><abbr title="Games compared">GP</abbr></th>
        <th scope="col" data-k="ppred"><abbr title="Points predicted per game">PTS pred</abbr></th><th scope="col" data-k="pact"><abbr title="Points scored per game">PTS actual</abbr></th><th scope="col" data-k="pdiff"><abbr title="Actual minus predicted">Diff</abbr></th><th scope="col" data-k="pmiss"><abbr title="Average miss per game, either way">Miss</abbr></th>
        <th scope="col" data-k="rpred">REB pred</th><th scope="col" data-k="ract">REB actual</th><th scope="col" data-k="apred">AST pred</th><th scope="col" data-k="aact">AST actual</th></tr></thead>
      <tbody></tbody></table></div>` : ''}
    ${d.previous ? `<h2>${yr(d.previous.season)} season</h2>${accuracyBlock(d.previous.summary, 'last season')}` : ''}
    <h2>How the predictions are made</h2>
    <div class="prose">
      <ul>
        <li><b>Minutes first.</b> Each player's expected minutes come from recent games (the last few count most), how often he has actually played lately, and the injury report. Players ruled out are removed and their minutes go to team-mates.</li>
        <li><b>Production per minute.</b> Points, rebounds, assists and the rest are measured per minute, weighted toward recent games, with last season as a starting point early in the year.</li>
        <li><b>Opponent and venue.</b> Lines move up or down by how much the opposing defence allows in each stat, and slightly for home or away.</li>
        <li><b>Game score.</b> Team strength (offence and defence, adjusted for schedule and rest) is blended with the sum of the player projections. ${d.line_weight ? `When a market spread is available, it supplies ${Math.round(d.line_weight * 100)}% of the projected margin. The projected total is blended separately. "Stats model alone" shows performance without market data.` : ''}</li>
        <li><b>Shooting.</b> Points and threes are built from how many shots a player takes (which is steady) and how well he shoots them (pulled toward his long-run and the league rate, because hot and cold streaks mostly fade).</li>
        <li><b>Ranges.</b> Each projection comes with a range that should hold about 8 games in 10. Single games swing a lot; the range is the honest part of the answer.</li>
      </ul>
      <p class="muted">Limits worth knowing: the betting line is the benchmark and the stats model alone does not beat it. The back-test knows each team's game-day roster but nothing else about the game. Games more than a day or two out have no line yet, so they rest on the stats model alone. Settings were tuned on the ${d.tuned_on ? yr(d.tuned_on) : 'previous'} season, so this season's figures are the fairer test. Trades, rest days announced late and blowouts are not anticipated.</p>
    </div>`;
  if ($('#pacc')) {
    const rows = d.players.map((r) => ({ ...r, _sort: { name: r.name, gp: r.gp, ppred: r.pts.pred, pact: r.pts.act, pdiff: r.pts.act - r.pts.pred, pmiss: r.pts.miss, rpred: r.reb.pred, ract: r.reb.act, apred: r.ast.pred, aact: r.ast.act } }));
    sortable($('#pacc'), rows, (r) => { const df = r.pts.act - r.pts.pred;
      return `<tr><th scope="row" class="sticky"><a href="#/nba/player/${r.pid}">${esc(r.name)}</a> <span class="muted small">${esc(r.team || '')}</span></th><td>${r.gp}</td>
        <td>${f1(r.pts.pred)}</td><td class="main">${f1(r.pts.act)}</td><td class="${Math.abs(df) >= 3 ? 'warn' : ''}">${signed(df)}</td><td>${r.pts.miss.toFixed(1)}</td>
        <td>${f1(r.reb.pred)}</td><td class="main">${f1(r.reb.act)}</td><td>${f1(r.ast.pred)}</td><td class="main">${f1(r.ast.act)}</td></tr>`; }, { key: 'pact', dir: -1 });
  }
}

// ---------- small charts shared by several pages ----------
const p1 = (v) => (v > 0 && v < 0.001 ? '<0.1%' : pct(v, v < 0.1 ? 1 : 0));
function titleBars(teams, n) {
  const top = [...teams].sort((a, b) => b.title - a.title).slice(0, n);
  return `<div class="chart hbars" role="img" aria-label="Chance of winning the title, top ${top.length} teams">${top.map((t) => `<div class="hb"><div class="team">${logo(t, 20)}<span class="hide-sm">${esc(t.name)}</span><span class="show-in">${esc(t.abbr)}</span></div>
    <div class="hb-bar"><i style="width:${(t.title / top[0].title * 100).toFixed(1)}%"></i></div><b>${p1(t.title)}</b></div>`).join('')}</div>`;
}
// Two bars per player: points predicted before the games, and points actually scored.
function scorerBars(players, n) {
  const top = [...players].sort((a, b) => b.pts.act - a.pts.act).slice(0, n);
  const max = Math.max(...top.map((r) => Math.max(r.pts.act, r.pts.pred)));
  return `<div class="chart hbars" role="img" aria-label="Points per game, predicted and actual, top ${top.length} scorers">${top.map((r) => `<div class="hb two"><a href="#/nba/player/${r.pid}">${esc(r.name)}</a>
      <div class="pair"><div class="hb-bar"><i class="pred" style="width:${(r.pts.pred / max * 100).toFixed(1)}%"></i></div><div class="hb-bar"><i style="width:${(r.pts.act / max * 100).toFixed(1)}%"></i></div></div>
      <div class="nums"><span class="p">${f1(r.pts.pred)}</span><b>${f1(r.pts.act)}</b></div></div>`).join('')}
    <div class="legend"><span><i></i>Predicted points per game</span><span><i class="b"></i>Actual</span></div></div>`;
}

// ---------- NBA: playoffs ----------
async function viewPlayoffs(token) {
  const d = await api('nba/playoffs');
  if (token !== navToken) return;
  const yr = (s) => `${s - 1}-${String(s).slice(2)}`;
  const byTitle = [...d.teams].sort((a, b) => b.title - a.title);
  const fav = byTitle[0], br = d.bracket, bc = br.champion;
  const confFav = (c) => [...d.teams].filter((t) => t.conf === c).sort((a, b) => b.finals - a.finals)[0];
  const ef = confFav('East'), wf = confFav('West');
  const slot = (t, m) => `<div class="bslot ${m.winner === t.id ? 'win' : ''}"><span class="seed">${t.seed}</span>${logo(t, 18)}<span class="nm">${esc(t.abbr)}</span><span class="pc">${pct(m.hi.id === t.id ? m.p_hi : 1 - m.p_hi)}</span></div>`;
  const match = (m) => `<div class="bmatch">${slot(m.hi, m)}${slot(m.lo, m)}</div>`;
  const ROUND = ['First round', 'Semifinals', 'Conference finals'];
  const conf = (c) => `<section><h3 style="margin:0 0 8px">${c.name}</h3><div class="scroll bwrap"><div class="bracket">${c.rounds.map((r, k) => `<div class="bround"><div class="bhead">${ROUND[k]}</div>${r.map(match).join('')}</div>`).join('')}</div></div></section>`;
  const table = (c) => {
    const rows = d.teams.filter((t) => t.conf === c).sort((a, b) => b.proj_w - a.proj_w);
    return `<section><h3 style="margin:0 0 8px">${c}</h3><div class="scroll"><table class="t wrap-sm seeds"><caption class="skip">${c}: projected seeding and playoff chances</caption>
      <thead><tr><th scope="col">Team</th><th scope="col">W-L</th><th scope="col"><abbr title="Projected wins after 82 games">Proj W</abbr></th><th scope="col"><abbr title="Chance of reaching the playoffs, through a top-six finish or the play-in">Playoffs</abbr></th><th scope="col" class="hide-sm hide-md"><abbr title="Chance of reaching the conference finals">Conf finals</abbr></th><th scope="col" class="hide-sm"><abbr title="Chance of reaching the NBA Finals">Finals</abbr></th><th scope="col">Title</th></tr></thead>
      <tbody>${rows.map((t, k) => `<tr class="${k === 5 ? 'cut' : k === 9 ? 'cut2' : ''}"><th scope="row"><div class="team"><span class="rk">${k + 1}</span>${logo(t, 20)}<span class="hide-sm">${esc(t.name)}</span><span class="show-in">${esc(t.abbr)}</span></div></th>
        <td>${t.w}-${t.l}</td><td class="main">${Math.round(t.proj_w)}<span class="muted small">-${82 - Math.round(t.proj_w)}</span></td><td>${p1(t.playoffs)}</td><td class="hide-sm hide-md">${p1(t.cf)}</td><td class="hide-sm">${p1(t.finals)}</td><td class="main">${p1(t.title)}</td></tr>`).join('')}</tbody></table></div></section>`;
  };
  const hist = d.history || [];
  main.innerHTML = `
    <div class="head"><div><h1>Playoff picture and title odds</h1><p class="muted">${yr(d.season)} season, ${d.games_left} regular-season games left. The rest of the schedule, the play-in and the playoffs are played out ${d.sims.toLocaleString()} times from the team ratings.</p></div></div>
    ${d.postseason ? '<p class="notice">The play-in or playoffs have started. These chances are still built from the regular-season standings and do not yet follow the real series results.</p>' : ''}
    <div class="tiles names">
      <div class="tile"><div class="k">Predicted champion</div><div class="v">${esc(fav.name)}</div><div class="s">${p1(fav.title)} chance of the title</div></div>
      <div class="tile"><div class="k">East favourite</div><div class="v">${esc(ef.name)}</div><div class="s">${p1(ef.finals)} to reach the Finals</div></div>
      <div class="tile"><div class="k">West favourite</div><div class="v">${esc(wf.name)}</div><div class="s">${p1(wf.finals)} to reach the Finals</div></div>
    </div>
    <h2>Title chances</h2>
    ${titleBars(d.teams, 12)}
    <p class="muted small" style="margin-top:8px">Even the favourite is far more likely to lose than win at this stage; ${byTitle.filter((t) => t.title >= 0.01).length} teams have at least a 1% chance.</p>
    <h2>Most likely bracket</h2>
    <p class="muted prose">Seeds by projected wins, with the favourite advancing in each best-of-seven series. Percentages are each side's chance of winning that series. Seeds 7 and 8 are shown as projected; in reality seeds 7 to 10 play in for them.</p>
    <div class="cols">${br.conferences.map(conf).join('')}</div>
    <div class="bfinal"><div class="bhead">NBA Finals</div>${match(br.finals)}
      <p class="muted small" style="margin:6px 0 0">${bc.id === fav.id ? `${esc(bc.name)} win this bracket and also have the best overall title chance.` : `${esc(bc.name)} would be favoured in this final, but ${esc(fav.name)} have the better overall title chance (${p1(fav.title)} against ${p1(bc.title)}) because their road to the Finals is easier.`}</p></div>
    <h2>Projected seeding and chances</h2>
    <p class="muted">Ordered by projected wins. The top six in each conference qualify directly (first line); seeds 7 to 10 go to the play-in (second line).</p>
    <div class="cols">${table('East')}${table('West')}</div>
    ${hist.length ? `<h2>Would this have worked before?</h2>
    <p class="muted prose">The same simulation run at this date in earlier seasons. Two seasons is far too few to judge it, so treat the chances as rough.</p>
    <div class="scroll" style="max-width:860px"><table class="t wrap-sm"><caption class="skip">Past champions in this simulation</caption>
      <thead><tr><th scope="col">Season</th><th scope="col" class="l">Champion</th><th scope="col"><abbr title="Title chance this simulation gave the eventual champion at this date">Chance then</abbr></th><th scope="col"><abbr title="Where the eventual champion ranked by title chance at this date">Rank then</abbr></th><th scope="col" class="l hide-sm">Favourite then</th><th scope="col"><abbr title="How many of the real playoff teams were among the 16 this simulation rated most likely">Playoff teams called</abbr></th></tr></thead>
      <tbody>${hist.map((h) => `<tr><th scope="row">${yr(h.season)}</th><td class="l">${esc(h.champion)}</td><td>${p1(h.chance)}</td><td class="main">${h.rank}</td><td class="l hide-sm">${esc(h.favourite)} (${p1(h.favourite_chance)})</td><td>${h.field_called} of ${h.field}</td></tr>`).join('')}</tbody></table></div>` : ''}
    <div class="prose" style="margin-top:14px"><p class="muted">How it works: each team's rating (the Net column on the Teams page) sets its chance in every remaining game, with home court worth ${d.home_edge} points. Each run also nudges every rating up or down a little, because early-season ratings are uncertain. Limits: injuries, trades and teams resting players late in the season are not anticipated, tie-breaks are random, and playoff games are treated like regular-season games.</p></div>`;
}

// ---------- football ----------
function fxRow(m) {
  const p = m.pred, sc = m.score;
  const fav = p ? (p.p_home >= p.p_away && p.p_home >= p.p_draw ? 'home' : p.p_away >= p.p_draw ? 'away' : 'draw') : null;
  let right = '';
  if (m.completed && sc) {
    const o = sc[0] > sc[1] ? 'home' : sc[0] < sc[1] ? 'away' : 'draw';
    right = `<b>${sc[0]} – ${sc[1]}</b>${p ? `<br><span class="small ${o === fav ? 'good' : 'bad'}">${o === fav ? 'called' : 'missed'}</span>` : ''}`;
  } else if (p) right = `<b>${p.scores[0].home} – ${p.scores[0].away}</b><br><span class="small muted">most likely</span>`;
  const tm = (t, side) => `<span class="${fav === side ? 'fav' : ''}">${t.logo ? `<img src="${img(t.logo, 40)}" alt="" width="18" height="18" loading="lazy">` : ''}${esc(t.name)}</span>`;
  return `<li><a href="#/football/match/${m.id}">
    <span class="when muted small">${m.state === 'pre' ? dateTimeFmt(m.date) : shortDate(m.date) + ' · ' + esc(m.status || '')}</span>
    <span class="tm">${tm(m.home, 'home')}${tm(m.away, 'away')}</span>
    <span class="pb">${p ? `<div class="bar" role="img" aria-label="Home ${pct(p.p_home)}, draw ${pct(p.p_draw)}, away ${pct(p.p_away)}"><i class="${fav === 'home' ? 'on' : ''}" style="width:${p.p_home * 100}%"></i><i class="mid" style="width:${p.p_draw * 100}%"></i><i class="${fav === 'away' ? 'on' : ''}" style="width:${p.p_away * 100}%"></i></div>
      <div class="prob-l"><span>1 <b>${pct(p.p_home)}</b></span><span>X <b>${pct(p.p_draw)}</b></span><span>2 <b>${pct(p.p_away)}</b></span></div>` : '<span class="muted small">Not enough history</span>'}</span>
    <span class="r">${right}</span></a></li>`;
}

async function viewFootball(q, token) {
  const lg = q.get('league') || 'eng.1';
  const [list, d] = await Promise.all([api('football/leagues'), api('football/league/' + lg)]);
  if (token !== navToken) return;
  const a = d.accuracy;
  main.innerHTML = `
    <div class="head"><div><h1>Football</h1><p class="muted">Match odds and likely scorers for Europe's top five leagues. Kick-off times are in your local time.</p></div></div>
    <div class="controls"><div class="seg" role="group" aria-label="League">${list.leagues.map((l) => `<button type="button" data-lg="${l.id}" aria-pressed="${l.id === lg}">${esc(l.name)}</button>`).join('')}</div></div>
    ${a.matches ? `<div class="band">
      <div><div class="k">Results called</div><div class="v">${pct(a.pick, 1)}</div><div class="s">${a.matches} matches this season</div></div>
      <div><div class="k">Always home win</div><div class="v">${pct(a.home_always, 1)}</div><div class="s">simple rule</div></div>
      <div><div class="k">Probability score</div><div class="v">${a.brier}</div><div class="s">lower is better; guessing by base rates ${a.brier_base_rates}</div></div>
      <div><div class="k">Goals per match</div><div class="v">${(d.home_goals_avg + d.away_goals_avg).toFixed(2)}</div><div class="s">home ${d.home_goals_avg}, away ${d.away_goals_avg}</div></div>
    </div>` : ''}
    <h2>Upcoming matches</h2>
    ${d.upcoming.length ? `<ul class="fx">${d.upcoming.map(fxRow).join('')}</ul>` : '<p class="empty">No matches scheduled in the next three weeks.</p>'}
    <p class="muted small" style="margin-top:6px">1 = home win, X = draw, 2 = away win.</p>
    <div class="cols">
      <div><h2>Recent results</h2>${d.recent.length ? `<ul class="fx">${d.recent.slice(0, 12).map(fxRow).join('')}</ul>` : '<p class="muted">None in the last three weeks.</p>'}</div>
      <div><h2>Table and strength</h2>
        <div class="scroll"><table class="t wrap-sm"><caption class="skip">League table with model strength ratings</caption>
          <thead><tr><th scope="col">Team</th><th scope="col"><abbr title="Played">P</abbr></th><th scope="col" class="hide-sm">W</th><th scope="col" class="hide-sm">D</th><th scope="col" class="hide-sm">L</th><th scope="col"><abbr title="Goal difference">GD</abbr></th><th scope="col">Pts</th><th scope="col"><abbr title="Attack strength: 1.00 is league average, higher is better">Att</abbr></th><th scope="col"><abbr title="Defence: goals conceded versus average, lower is better">Def</abbr></th></tr></thead>
          <tbody>${d.table.map((t, i) => `<tr><th scope="row"><div class="team"><span class="muted" style="width:18px">${i + 1}</span>${t.logo ? `<img src="${img(t.logo, 40)}" alt="" width="18" height="18" style="width:18px;height:18px" loading="lazy">` : ''}<span>${esc(t.name)}</span></div></th><td>${t.p}</td><td class="hide-sm">${t.w}</td><td class="hide-sm">${t.d}</td><td class="hide-sm">${t.l}</td><td>${t.gf - t.ga > 0 ? '+' : ''}${t.gf - t.ga}</td><td class="main">${t.pts}</td><td>${t.att.toFixed(2)}</td><td>${t.dfc.toFixed(2)}</td></tr>`).join('')}</tbody></table></div></div>
    </div>`;
  $('.seg').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-lg]');
    if (b) location.hash = '#/football?league=' + b.dataset.lg;
  });
}

async function viewMatch(id, token) {
  const m = await api('football/match/' + id);
  if (token !== navToken) return;
  const p = m.pred, sc = m.score;
  const side = (s) => {
    const t = m[s], r = t.row;
    return `<div class="mu-team">${t.logo ? `<img src="${img(t.logo, 112)}" alt="" width="56" height="56">` : ''}<span class="nm">${esc(t.name)}</span><span class="muted small">${r ? `${r.w}-${r.d}-${r.l} · ${r.pts} pts` : ''} · ${s}</span></div>`;
  };
  const form = (s) => `<div><h3 style="margin:14px 0 8px">${esc(m[s].name)}: last five</h3><div class="scroll"><table class="t"><thead><tr><th scope="col">Date</th><th scope="col" class="l">Opponent</th><th scope="col">Score</th></tr></thead>
    <tbody>${m.form[s].map((x) => `<tr><th scope="row">${shortDate(x.date)}</th><td class="l">${x.home ? 'vs' : '@'} ${esc(x.opp)}</td><td><span class="${x.gf > x.ga ? 'good' : x.gf < x.ga ? 'bad' : ''}">${x.gf > x.ga ? 'W' : x.gf < x.ga ? 'L' : 'D'}</span> ${x.gf}–${x.ga}</td></tr>`).join('') || '<tr><td class="l muted" colspan="3">No earlier matches.</td></tr>'}</tbody></table></div></div>`;
  const scorers = (s) => {
    const rows = (m.players && m.players[s]) || [];
    return `<div><h3 style="margin:14px 0 8px">${esc(m[s].name)}</h3><div class="scroll"><table class="t"><caption class="skip">Chance to score or assist</caption>
      <thead><tr><th scope="col">Player</th><th scope="col">To score</th><th scope="col">To assist</th><th scope="col"><abbr title="Season goals and assists">G/A</abbr></th><th scope="col"><abbr title="Appearances">App</abbr></th></tr></thead>
      <tbody>${rows.map((x) => `<tr><th scope="row">${esc(x.name)} <span class="muted small">${esc(x.pos || '')}</span></th><td class="main">${pct(x.p_goal)}</td><td>${pct(x.p_assist)}</td><td>${x.goals}/${x.assists}</td><td>${x.apps}</td></tr>`).join('') || '<tr><td class="l muted" colspan="5">No squad data.</td></tr>'}</tbody></table></div>
      ${m.injured && m.injured[s].length ? `<p class="muted small" style="margin-top:6px">Left out as injured: ${m.injured[s].map(esc).join(', ')}.</p>` : ''}</div>`;
  };
  main.innerHTML = `
    <a class="crumb" href="#/football?league=${m.league}">← ${esc(m.league_name)}</a>
    <h1 class="skip">${esc(m.home.name)} v ${esc(m.away.name)}</h1>
    <section class="matchup" aria-label="Match">
      <div class="mu-grid">${side('home')}
        <div class="mu-score"><div class="lbl">${p ? 'Expected goals' : ''}</div><div class="big">${p ? `${p.xg_home.toFixed(1)} – ${p.xg_away.toFixed(1)}` : '–'}</div>
          ${sc ? `<div class="act">${esc(m.status || '')}: <b>${sc[0]} – ${sc[1]}</b></div>` : `<div class="act muted">${dateTimeFmt(m.date)}</div>`}</div>
        ${side('away')}</div>
      ${p ? `<div class="prob"><div class="bar" role="img" aria-label="Home ${pct(p.p_home)}, draw ${pct(p.p_draw)}, away ${pct(p.p_away)}"><i class="on" style="width:${p.p_home * 100}%"></i><i class="mid" style="width:${p.p_draw * 100}%"></i><i style="width:${p.p_away * 100}%"></i></div>
        <div class="prob-l"><span>${esc(m.home.name)} <b>${pct(p.p_home)}</b></span><span>Draw <b>${pct(p.p_draw)}</b></span><span><b>${pct(p.p_away)}</b> ${esc(m.away.name)}</span></div></div>
      <dl class="facts">
        <div><dt>Most likely scores</dt><dd>${p.scores.map((s) => `${s.home}–${s.away} (${pct(s.p)})`).join(', ')}</dd></div>
        <div><dt>Over 2.5 goals</dt><dd>${pct(p.over25)}</dd></div>
        <div><dt>Both teams score</dt><dd>${pct(p.btts)}</dd></div>
        ${m.venue ? `<div><dt>Venue</dt><dd>${esc(m.venue)}</dd></div>` : ''}
      </dl>` : '<p class="muted">Not enough history to predict this match.</p>'}
    </section>
    <p class="muted small" style="margin-top:8px">${m.completed ? 'Back-tested: rebuilt using only matches played before this one.' : 'Based on results over the last two seasons, with recent matches counting most.'}</p>
    ${m.players ? `<h2>Who scores?</h2><p class="muted">Each player's share of his team's expected goals, from his goals, shots and playing time this season. Line-ups are not known in advance, so regular starters are assumed to play.</p><div class="cols">${scorers('home')}${scorers('away')}</div>` : ''}
    <h2>Form</h2><div class="cols">${form('home')}${form('away')}</div>`;
}

// ---------- router ----------
const routes = [
  [/^\/nba\/games$/, (m, q, token) => viewGames(q, token), 'games', 'Games'],
  [/^\/nba\/game\/(\w+)$/, (m, q, token) => viewGame(m[1], token), 'games', 'Game'],
  [/^\/nba\/players$/, (m, q, token) => viewPlayers(q, token), 'players', 'Player projections'],
  [/^\/nba\/player\/(\w+)$/, (m, q, token) => viewPlayer(m[1], token), 'players', 'Player'],
  [/^\/nba\/teams$/, (m, q, token) => viewTeams(token), 'teams', 'Team ratings'],
  [/^\/nba\/playoffs$/, (m, q, token) => viewPlayoffs(token), 'playoffs', 'Playoffs'],
  [/^\/nba\/awards$/, (m, q, token) => viewAwards(token), 'awards', 'Awards'],
  [/^\/nba\/accuracy$/, (m, q, token) => viewAccuracy(token), 'accuracy', 'Accuracy'],
  [/^\/football$/, (m, q, token) => viewFootball(q, token), 'football', 'Football'],
  [/^\/football\/match\/(\w+)$/, (m, q, token) => viewMatch(m[1], token), 'football', 'Match'],
];
let navToken = 0, retryTimer = null;

const bar = $('#bar');
async function route(quiet) {
  clearTimeout(retryTimer);
  const token = ++navToken;
  const raw = location.hash.replace(/^#/, '') || '/nba/games';
  const [path, qs] = raw.split('?');
  const q = new URLSearchParams(qs || '');
  const hit = routes.find((r) => r[0].test(path));
  if (!hit) { location.hash = '#/nba/games'; return; }
  document.querySelectorAll('.nav a').forEach((a) => (a.dataset.nav === hit[2] ? a.setAttribute('aria-current', 'page') : a.removeAttribute('aria-current')));
  if (!main.innerHTML) main.innerHTML = '<p class="empty">Loading…</p>';
  const keepScroll = quiet === true ? window.scrollY : null;
  const slow = setTimeout(() => { bar.hidden = false; }, 120);
  currentPaths = [hrefToApi('#' + raw) || (path === '/nba/games' ? 'nba/day' : '')];
  if (path === '/nba/games' && !q.get('day')) currentPaths = ['nba/day'];
  try {
    await hit[1](hit[0].exec(path), q, token);
    if (token !== navToken) return;
    const h1 = $('h1', main);
    document.title = (h1 ? h1.textContent : hit[3]) + ' · StatPredict';
    window.scrollTo(0, keepScroll ?? 0);
  } catch (e) {
    if (token !== navToken) return;
    if (e.status === 503) {
      let extra = '';
      try {
        const s = await (await fetch('api/status')).json();
        if (s.ingest && s.ingest.total) extra = ` Fetching ${s.ingest.stage}: ${s.ingest.done} of ${s.ingest.total}.`;
        else if (s.building) extra = ` Scoring past games: ${s.progress} done.`;
        if (s.error) extra += ' ' + s.error;
      } catch { /* status is optional */ }
      if (token !== navToken) return;
      main.innerHTML = `<p class="empty">Loading game data from ESPN. The first load takes a few minutes.${esc(extra)}</p>`;
      retryTimer = setTimeout(route, 4000);
    } else {
      main.innerHTML = `<div class="empty"><p>${e.status === 404 ? esc(e.message) + '.' : 'Could not load this page: ' + esc(e.message)}</p><p><a href="#/nba/games">Back to games</a></p></div>`;
    }
  } finally {
    clearTimeout(slow);
    if (token === navToken) bar.hidden = true;
  }
}

const EVERY = BOOT.refresh_seconds || 300;
let refreshedAt = BOOT.refreshed_at || 0;
function stamp(t) {
  if (t) refreshedAt = t;
  const el = $('#updated');
  if (!refreshedAt) { el.textContent = ''; return; }
  const when = new Date(refreshedAt * 1000).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  const behind = Date.now() / 1000 - refreshedAt > EVERY * 3;
  el.textContent = behind ? `Catching up with the latest games (last update ${when}); this page will redraw by itself.`
    : `Updates itself every ${Math.max(1, Math.round(EVERY / 60))} minutes, no reload needed. Last update ${when}.`;
  el.classList.toggle('warn', behind);
}
let polling = false;
async function footer() {
  if (polling) return;
  polling = true;
  try {
    const s = await (await fetch('api/status', { cache: 'no-store' })).json();
    // A newer version of the app was published: load it, once.
    if (s.version && BOOT.version && s.version !== BOOT.version && sessionStorage.getItem('sp-reloaded') !== s.version) {
      sessionStorage.setItem('sp-reloaded', s.version);
      location.reload();
      return;
    }
    stamp(s.nba && s.nba.refreshed_at);
    if (s.built_at && builtAt && s.built_at !== builtAt) {
      // New data on the server: drop what we hold and redraw the page in place.
      cache.forEach((v) => { v.at = 0; });
      route(true);
    }
    if (s.built_at) builtAt = s.built_at;
  } catch { /* footer is optional */ } finally { polling = false; }
}
// Poll every minute; every 5 seconds while the server is catching up after a sleep.
function tick() {
  const behind = refreshedAt && Date.now() / 1000 - refreshedAt > EVERY * 3;
  if (!document.hidden) footer();
  setTimeout(tick, behind ? 5000 : 60000);
}
if (BOOT.session_url) $('#session-link').href = BOOT.session_url;
else $('#session-link').parentElement.hidden = true;
stamp(BOOT.refreshed_at);
$('a.skip')?.addEventListener('click', (e) => {
  e.preventDefault();
  window.scrollTo(0, 0);
  main.focus({ preventScroll: true });
});
window.addEventListener('hashchange', () => route());
route();
// Coming back to the tab (or back online) checks at once instead of waiting for the next tick.
document.addEventListener('visibilitychange', () => { if (!document.hidden) footer(); });
window.addEventListener('online', footer);
window.addEventListener('pageshow', (e) => { if (e.persisted) footer(); });
if (!BOOT.day) footer();
setTimeout(tick, refreshedAt && Date.now() / 1000 - refreshedAt > EVERY * 3 ? 3000 : 60000);
