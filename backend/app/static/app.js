/* autoresearcherUI v0.3 — single-page mission control (vanilla JS, no build) */
'use strict';

const S = {
  project: null, runs: [], ideas: [], events: [], chat: [], gpus: [],
  metrics: {}, filter: 'all', search: '', sel: null, ylog: false,
  railTab: 'summary', sessTab: null, view: 'dashboard', cmp: [], cmpMetric: '',
};

const api = (p) => fetch('/api' + p).then(r => r.json());
const post = (p, b) => fetch('/api' + p, {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(b || {}),
}).then(r => r.json());

/* report any client-side JS error to the backend so crashes are debuggable */
function _report(msg, extra) {
  try {
    fetch('/api/clientlog', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(Object.assign({ msg: String(msg) }, extra || {})),
    });
  } catch (e) { /* ignore */ }
}
window.addEventListener('error', e => _report(e.message,
  { src: e.filename, line: e.lineno,
    stack: String((e.error && e.error.stack) || '').slice(0, 700) }));
window.addEventListener('unhandledrejection', e => _report(
  'unhandledrejection',
  { stack: String((e.reason && e.reason.stack) || e.reason || '')
    .slice(0, 700) }));

const el = (t, c, h) => { const n = document.createElement(t);
  if (c) n.className = c; if (h != null) n.innerHTML = h; return n; };
const esc = (s) => String(s == null ? '' : s).replace(/[&<>]/g,
  m => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[m]));
const fmt = (v, d = 4) => (v == null || isNaN(v)) ? '—' : (+v).toFixed(d);
const MONO = "'SF Mono',Menlo,monospace";
const ago = (iso) => { if (!iso) return ''; const s = (Date.now() - +new Date(iso)) / 1000;
  if (s < 60) return Math.max(1, s | 0) + 's ago';
  if (s < 3600) return (s / 60 | 0) + 'm ago';
  if (s < 86400) return (s / 3600 | 0) + 'h ago'; return (s / 86400 | 0) + 'd ago'; };
const dur = (a, b) => { if (!a) return '—';
  const s = ((b ? +new Date(b) : Date.now()) - +new Date(a)) / 1000;
  return s < 90 ? Math.max(1, s | 0) + 's' : s < 5400 ? (s / 60 | 0) + 'm'
    : (s / 3600).toFixed(1) + 'h'; };

/* ── derived data ───────────────────────────────────────────────────────── */
function expRuns() {                       // runs that have started, in order
  return S.runs.filter(r => r.started_at)
    .sort((a, b) => (a.started_at || '').localeCompare(b.started_at || '')
      || (a.created_at || '').localeCompare(b.created_at || ''))
    .map((r, i) => ({ ...r, exp: i }));
}
const minimize = () => (S.project?.metric_direction || 'minimize') === 'minimize';
const better = (a, b) => minimize() ? a < b : a > b;

function frontier(runs) {                  // mark running-best improvements
  let best = null;
  for (const r of runs) {
    r._finite = r.status !== 'crashed' && r.headline_metric != null
      && isFinite(r.headline_metric) && r.headline_metric < 5e4;
    r._frontier = false;
    if (r._finite) {
      if (best === null || better(r.headline_metric, best)) {
        best = r.headline_metric; r._frontier = true;
      }
      r._best = best;
    }
  }
  return runs;
}

/* ════════════════════════ PROGRESS CHART (hero) ═══════════════════════════ */
class ProgressChart {
  constructor(host) {
    this.host = host;
    this.canvas = el('canvas'); this.tip = el('div', 'tip');
    host.append(this.canvas, this.tip);
    this.canvas.addEventListener('mousemove', e => this.hover(e));
    this.canvas.addEventListener('mouseleave', () => {
      this.hx = null; this.tip.style.opacity = 0; this.draw(); });
    this.canvas.addEventListener('click', () => {
      if (this.hit) openDrawer(this.hit.id); });
    new ResizeObserver(() => this.draw()).observe(host);
  }
  setData(runs) { this.runs = frontier(runs); this.draw(); }
  draw() {
    const runs = this.runs || [];
    const w = this.host.clientWidth || 700, h = this.host.clientHeight || 320;
    const dpr = devicePixelRatio || 1;
    this.canvas.width = w * dpr; this.canvas.height = h * dpr;
    this.canvas.style.width = w + 'px'; this.canvas.style.height = h + 'px';
    const c = this.canvas.getContext('2d');
    c.setTransform(dpr, 0, 0, dpr, 0, 0); c.clearRect(0, 0, w, h);
    const pad = { l: 56, r: 18, t: 14, b: 30 }, fl = 22;   // fl = failure lane
    const base = S.project?.baseline_metric;
    const fin = runs.filter(r => r._finite).map(r => r.headline_metric);
    if (base != null) fin.push(base);
    if (!fin.length) {
      c.fillStyle = '#5C636B'; c.font = '12px sans-serif'; c.textAlign = 'center';
      c.fillText('No experiments yet — the research agent has not started.',
                 w / 2, h / 2);
      return;
    }
    let lo = Math.min(...fin), hi = Math.max(...fin);
    const log = S.ylog && lo > 0;
    const tf = v => log ? Math.log10(v) : v;
    let ylo = tf(lo), yhi = tf(hi), pd = (yhi - ylo) * 0.12 || Math.abs(ylo) * .1 || 1;
    ylo -= pd; yhi += pd;
    const n = Math.max(runs.length, 8);
    const X = i => pad.l + (i / (n - 1)) * (w - pad.l - pad.r);
    const Y = v => pad.t + fl + (1 - (tf(v) - ylo) / (yhi - ylo))
      * (h - pad.t - pad.b - fl);
    this._geo = { X, Y, w, pad, n };

    // grid + y labels
    c.font = '10px ' + MONO; c.textAlign = 'right'; c.textBaseline = 'middle';
    for (let i = 0; i <= 4; i++) {
      const yy = pad.t + fl + i / 4 * (h - pad.t - pad.b - fl);
      const val = log ? 10 ** (yhi - i / 4 * (yhi - ylo)) : yhi - i / 4 * (yhi - ylo);
      c.strokeStyle = '#1b1f25'; c.lineWidth = 1;
      c.beginPath(); c.moveTo(pad.l, yy); c.lineTo(w - pad.r, yy); c.stroke();
      c.fillStyle = '#5C636B'; c.fillText(val < 100 ? val.toFixed(2) : val.toExponential(0), pad.l - 8, yy);
    }
    // x labels
    c.textAlign = 'center'; c.textBaseline = 'top';
    for (let i = 0; i < n; i += Math.ceil(n / 9)) {
      c.fillStyle = '#5C636B'; c.fillText('#' + i, X(i), h - pad.b + 8);
    }
    // baseline line
    if (base != null) {
      c.strokeStyle = '#9BA1A8'; c.lineWidth = 1; c.setLineDash([4, 4]);
      c.beginPath(); c.moveTo(pad.l, Y(base)); c.lineTo(w - pad.r, Y(base)); c.stroke();
      c.setLineDash([]); c.fillStyle = '#9BA1A8'; c.textAlign = 'left';
      c.font = '9.5px ' + MONO;
      c.fillText('baseline ' + fmt(base, 3), pad.l + 4, Y(base) - 8);
    }
    // running-best step line
    c.strokeStyle = '#34D399'; c.lineWidth = 2; c.beginPath();
    let started = false, prevY = 0;
    for (const r of runs) {
      if (!r._finite) continue;
      const x = X(r.exp), y = Y(r._best);
      if (!started) { c.moveTo(x, y); started = true; }
      else { c.lineTo(x, prevY); c.lineTo(x, y); }
      prevY = y;
    }
    if (started) c.lineTo(w - pad.r, prevY);
    c.stroke();
    // dots
    for (const r of runs) {
      const x = X(r.exp);
      if (r.status === 'crashed' || (!r._finite && r.status !== 'running')) {
        c.strokeStyle = '#F43F5E'; c.lineWidth = 1.6;
        c.beginPath(); c.moveTo(x - 4, pad.t + fl / 2 - 4); c.lineTo(x + 4, pad.t + fl / 2 + 4);
        c.moveTo(x + 4, pad.t + fl / 2 - 4); c.lineTo(x - 4, pad.t + fl / 2 + 4); c.stroke();
      } else if (r.status === 'running') {
        c.fillStyle = '#FBBF24';
        c.beginPath(); c.arc(x, pad.t + fl / 2, 4, 0, 7); c.fill();
      } else if (r._frontier) {
        c.fillStyle = '#34D399';
        c.beginPath(); c.arc(x, Y(r.headline_metric), 4.5, 0, 7); c.fill();
        c.strokeStyle = '#0B0D10'; c.lineWidth = 1.5; c.stroke();
      } else {
        c.fillStyle = '#39414d';
        c.beginPath(); c.arc(x, Y(r.headline_metric), 3, 0, 7); c.fill();
      }
    }
    // frontier labels
    c.font = '9.5px ' + MONO; c.fillStyle = '#7d8590'; c.textBaseline = 'bottom';
    let lastLx = -99;
    for (const r of runs) {
      if (!r._frontier) continue;
      const x = X(r.exp);
      if (x - lastLx < 46) continue; lastLx = x;
      c.textAlign = x > w - 110 ? 'right' : 'left';
      c.fillText(r.run_name.slice(0, 18), x + (x > w - 110 ? -6 : 6),
        Y(r.headline_metric) - 9);
    }
    // crosshair + hover dot
    if (this.hx != null && this.hit) {
      const x = X(this.hit.exp);
      c.strokeStyle = '#3a4150'; c.setLineDash([3, 3]); c.lineWidth = 1;
      c.beginPath(); c.moveTo(x, pad.t); c.lineTo(x, h - pad.b); c.stroke();
      c.setLineDash([]);
      if (this.hit._finite) {
        c.strokeStyle = '#fff'; c.lineWidth = 2;
        c.beginPath(); c.arc(x, Y(this.hit.headline_metric), 6, 0, 7); c.stroke();
      }
    }
  }
  hover(e) {
    if (!this._geo || !this.runs) return;
    const r = this.canvas.getBoundingClientRect();
    const mx = e.clientX - r.left;
    let best = null, bd = 1e9;
    for (const run of this.runs) {
      const d = Math.abs(this._geo.X(run.exp) - mx);
      if (d < bd) { bd = d; best = run; }
    }
    if (!best || bd > 40) { this.hit = null; this.hx = null;
      this.tip.style.opacity = 0; this.draw(); return; }
    this.hit = best; this.hx = mx;
    const dlt = best.baseline_delta;
    this.tip.innerHTML =
      `<b>#${best.exp} ${esc(best.run_name)}</b><br>` +
      `${S.project.validation_metric}: ${best.status === 'crashed'
        ? 'diverged' : fmt(best.headline_metric)}<br>` +
      `Δ vs baseline: ${dlt == null ? '—'
        : (dlt >= 0 ? '+' : '') + fmt(dlt)}<br>` +
      `status: ${best.status}`;
    this.tip.style.opacity = 1;
    this.tip.style.left = Math.min(mx + 14, r.width - 180) + 'px';
    this.tip.style.top = '34px';
    this.canvas.style.cursor = 'pointer';
    this.draw();
  }
}

/* ════════════════════════ LINE CHART (drawer) ═════════════════════════════ */
class LineChart {
  constructor(host, color) { this.host = host; this.color = color || '#6366F1';
    this.canvas = el('canvas'); host.append(this.canvas);
    new ResizeObserver(() => this.draw()).observe(host); }
  setData(d) { this.data = d || []; this.draw(); }
  draw() {
    const d = this.data || [], w = this.host.clientWidth || 300,
      h = this.host.clientHeight || 150, dpr = devicePixelRatio || 1;
    this.canvas.width = w * dpr; this.canvas.height = h * dpr;
    this.canvas.style.width = w + 'px'; this.canvas.style.height = h + 'px';
    const c = this.canvas.getContext('2d');
    c.setTransform(dpr, 0, 0, dpr, 0, 0); c.clearRect(0, 0, w, h);
    if (d.length < 2) { c.fillStyle = '#5C636B'; c.font = '11px sans-serif';
      c.textAlign = 'center'; c.fillText('no series', w / 2, h / 2); return; }
    const pad = { l: 44, r: 10, t: 10, b: 18 };
    const xs = d.map(p => p[0]), ys = d.map(p => p[1]);
    let xlo = Math.min(...xs), xhi = Math.max(...xs);
    let ylo = Math.min(...ys), yhi = Math.max(...ys);
    const yp = (yhi - ylo) * 0.1 || 1; ylo -= yp; yhi += yp;
    if (xhi === xlo) xhi++;
    const X = v => pad.l + (v - xlo) / (xhi - xlo) * (w - pad.l - pad.r);
    const Y = v => pad.t + (1 - (v - ylo) / (yhi - ylo)) * (h - pad.t - pad.b);
    c.font = '9px ' + MONO; c.fillStyle = '#5C636B'; c.textAlign = 'right';
    c.textBaseline = 'middle';
    for (let i = 0; i <= 3; i++) {
      const yy = pad.t + i / 3 * (h - pad.t - pad.b);
      c.strokeStyle = '#1b1f25'; c.beginPath();
      c.moveTo(pad.l, yy); c.lineTo(w - pad.r, yy); c.stroke();
      c.fillText((yhi - i / 3 * (yhi - ylo)).toFixed(2), pad.l - 6, yy);
    }
    c.strokeStyle = this.color; c.lineWidth = 1.8; c.beginPath();
    d.forEach(([x, y], i) => { const px = X(x), py = Y(y);
      i ? c.lineTo(px, py) : c.moveTo(px, py); });
    c.stroke();
  }
}

/* ════════════════════════ RENDER ══════════════════════════════════════════ */
let _viewTimers = [];
const addTimer = (id) => { _viewTimers.push(id); return id; };
function clearViewTimers() {
  _viewTimers.forEach(clearInterval); _viewTimers = [];
}

const VIEWS = [
  ['dashboard', 'Dashboard', '▦'], ['analysis', 'Analysis', '◫'],
  ['latex', 'Latex', '∑'], ['system', 'System stats', '▤'],
  ['authkeys', 'authorized_keys', '⚿'],
];
const VIEW_TITLE = Object.fromEntries(VIEWS.map(v => [v[0], v[1]]));

function render() {
  clearViewTimers(); stopTermPoll();
  const app = document.getElementById('app');
  app.innerHTML = '';
  if (S.view !== 'dashboard') {
    app.className = 'app solo';
    app.append(header(), viewPane());
    return;
  }
  app.className = 'app';
  app.append(header(), left(), rail(), el('button', 'fab', '✉'));
  applyRailW();
  document.querySelector('.fab').onclick = () =>
    document.querySelector('.rail').classList.toggle('show');
  paintHero(); paintStats(); paintTable(); paintRail();
  pollGpus();
}

function header() {
  const p = S.project || {};
  const h = el('div', 'hdr');
  const burger = el('button', 'burger', '☰');
  burger.onclick = toggleMenu;
  h.append(burger);
  h.append(el('div', 'brand',
    `<div class="brand-mark">a</div><b>autoresearcher<span>UI</span></b>`));
  if (S.view !== 'dashboard') {
    h.append(el('div', 'proj', esc(VIEW_TITLE[S.view] || '')));
    h.append(el('div', 'spacer'));
    return h;
  }
  const dir = (p.metric_direction === 'minimize') ? '↓' : '↑';
  h.append(el('div', 'proj', esc(p.name || 'project')));
  h.append(el('div', 'metric', `${esc(p.validation_metric || '')} ${dir}`));
  const running = (p.status === 'running' || p.status === 'bootstrapping');
  h.append(el('div', 'pill' + (running ? '' : ' done'),
    `<span class="dot ${running ? 'live' : ''}"></span>${esc(p.status || '—')}`));
  h.append(el('div', 'spacer'));
  const strip = el('div', 'gpu-strip'); strip.id = 'gpus';
  h.append(strip);
  const runs = S.runs.filter(r => r.status === 'running').length;
  const q = S.ideas.filter(i => i.status === 'not_implemented').length;
  const done = S.runs.filter(r => ['kept', 'discarded'].includes(r.status)).length;
  const fail = S.runs.filter(r => r.status === 'crashed').length;
  h.append(el('div', 'counts',
    `<span class="c-run">running <b>${runs}</b></span>` +
    `<span>queued <b>${q}</b></span><span>done <b>${done}</b></span>` +
    `<span class="c-fail">failed <b>${fail}</b></span>`));
  return h;
}

/* ── side menu ────────────────────────────────────────────────────────── */
function closeMenu() {
  document.querySelector('.sidemenu')?.remove();
  document.querySelector('.menuscrim')?.remove();
}
function toggleMenu() {
  if (document.querySelector('.sidemenu')) { closeMenu(); return; }
  const scrim = el('div', 'menuscrim');
  scrim.onclick = closeMenu;
  const m = el('div', 'sidemenu');
  m.append(el('div', 'menu-hd',
    `<div class="brand-mark">a</div><b>autoresearcher<span>UI</span></b>`));
  VIEWS.forEach(([k, label, ic]) => {
    const it = el('button', 'menu-item' + (S.view === k ? ' on' : ''),
      `<span class="menu-ic">${ic}</span><span>${label}</span>`);
    it.onclick = () => { closeMenu(); if (S.view !== k) { S.view = k; render(); } };
    m.append(it);
  });
  m.append(el('div', 'menu-spacer'));
  const arc = el('button', 'menu-item',
    `<span class="menu-ic">⤓</span><span>Archive</span>`);
  arc.onclick = () => { closeMenu(); openArchive(); };
  const rst = el('button', 'menu-item danger',
    `<span class="menu-ic">⟲</span><span>Reset</span>`);
  rst.onclick = () => { closeMenu(); resetAll(); };
  m.append(arc, rst);
  document.body.append(scrim, m);
  requestAnimationFrame(() => {
    scrim.classList.add('open'); m.classList.add('open');
  });
}

function left() {
  const L = el('div', 'left');
  // hero
  const hero = el('div', 'hero');
  const top = el('div', 'hero-top');
  top.id = 'herotop';
  hero.append(top);
  const cw = el('div', 'chart-wrap'); cw.id = 'cw';
  hero.append(cw);
  L.append(hero);
  // stats
  const st = el('div', 'stats'); st.id = 'stats';
  L.append(st);
  // table
  const tw = el('div', 'tbl-wrap');
  const bar = el('div', 'tbl-bar');
  bar.append(el('div', 'lbl', 'Experiments'));
  const seg = el('div', 'seg');
  [['all', 'All'], ['running', 'Running'], ['kept', 'Kept'],
   ['discarded', 'Discarded'], ['crashed', 'Failed'], ['queued', 'Queued']]
    .forEach(([k, lbl]) => {
      const b = el('button', S.filter === k ? 'on' : '', lbl);
      b.onclick = () => { S.filter = k; render(); };
      seg.append(b);
    });
  bar.append(seg);
  const srch = el('input', 'search'); srch.placeholder = 'Search ideas…';
  srch.value = S.search;
  srch.oninput = () => { S.search = srch.value; paintTable(); };
  bar.append(srch);
  tw.append(bar);
  const ts = el('div', 'tbl-scroll'); ts.id = 'tscroll';
  tw.append(ts);
  L.append(tw);
  return L;
}

/* ── resizable rail ───────────────────────────────────────────────────── */
const RAIL_MIN = 320;
function railMax() {
  return Math.max(RAIL_MIN, Math.min(960, window.innerWidth - 440));
}
function setRailW(w) {
  const app = document.querySelector('.app');
  if (app) app.style.gridTemplateColumns = '1fr ' + Math.round(w) + 'px';
}
function applyRailW() {
  if (window.innerWidth <= 880) return;          // mobile: rail is an overlay
  let w;
  try { w = parseInt(localStorage.getItem('arui.railW'), 10); } catch (e) {}
  if (w && isFinite(w)) setRailW(Math.max(RAIL_MIN, Math.min(w, railMax())));
}
function mountRailResize(grip) {
  let startX = 0, startW = 0;
  const move = e => setRailW(
    Math.max(RAIL_MIN, Math.min(railMax(), startW + (startX - e.clientX))));
  const up = () => {
    document.removeEventListener('mousemove', move);
    document.removeEventListener('mouseup', up);
    document.body.classList.remove('resizing');
    grip.classList.remove('drag');
    const rail = document.querySelector('.rail');
    if (rail) {
      try {
        localStorage.setItem('arui.railW',
          Math.round(rail.getBoundingClientRect().width));
      } catch (e) { /* ignore */ }
    }
  };
  grip.addEventListener('mousedown', e => {
    if (window.innerWidth <= 880) return;
    e.preventDefault();
    const rail = document.querySelector('.rail');
    startX = e.clientX;
    startW = rail ? rail.getBoundingClientRect().width : 372;
    document.body.classList.add('resizing');
    grip.classList.add('drag');
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
  });
}
window.addEventListener('resize', () => {
  const app = document.querySelector('.app');
  if (!app) return;
  if (window.innerWidth <= 880) app.style.gridTemplateColumns = '';
  else applyRailW();
});

function rail() {
  const r = el('div', 'rail');
  const grip = el('div', 'rail-grip');
  grip.title = 'Drag to resize';
  mountRailResize(grip);
  r.append(grip);
  const tabs = el('div', 'rail-tabs');
  [['summary', 'Summary'], ['live', 'Live'],
   ['sessions', 'Sessions']].forEach(([k, lbl]) => {
    const b = el('button', 'rail-tab' + (S.railTab === k ? ' on' : ''), lbl);
    b.dataset.tab = k;
    b.onclick = () => { S.railTab = k; paintRail(); };
    tabs.append(b);
  });
  r.append(tabs);
  const content = el('div', 'rail-content'); content.id = 'railcontent';
  r.append(content);
  const comp = el('div', 'composer');
  const chips = el('div', 'qchips');
  ["what's your status?", "what are you running?", "best result so far?"]
    .forEach(t => {
      const ch = el('button', 'qchip', t);
      ch.onclick = () => sendChat(t); chips.append(ch);
    });
  comp.append(chips);
  const row = el('div', 'row');
  const inp = el('input'); inp.id = 'chatin';
  inp.placeholder = 'Message the agent — typed into its session…';
  inp.onkeydown = e => { if (e.key === 'Enter') sendChat(inp.value); };
  const snd = el('button', '', 'Send'); snd.onclick = () => sendChat(inp.value);
  row.append(inp, snd); comp.append(row);
  r.append(comp);
  return r;
}

/* ── painters ─────────────────────────────────────────────────────────── */
let HERO;
function paintHero() {
  const runs = frontier(expRuns());
  const kept = runs.filter(r => r._frontier).length;
  document.getElementById('herotop').innerHTML =
    `<h1>Autoresearch progress</h1>` +
    `<span class="sub">${runs.length} experiments · ${kept} kept ` +
    `improvements</span>` +
    `<span class="hero-ctl">` +
    `<button class="tg${S.ylog ? ' on' : ''}" id="ylog">log y</button></span>`;
  document.getElementById('ylog').onclick = () => { S.ylog = !S.ylog; paintHero(); };
  const cw = document.getElementById('cw');
  if (!HERO || cw.childElementCount === 0) { cw.innerHTML = ''; HERO = new ProgressChart(cw); }
  HERO.setData(runs);
}

function paintStats() {
  const p = S.project || {};
  const runs = S.runs;
  const done = runs.filter(r => ['kept', 'discarded'].includes(r.status));
  const fail = runs.filter(r => r.status === 'crashed').length;
  const best = p.best_metric, base = p.baseline_metric;
  const delta = (best != null && base != null) ? base - best : null;
  const cards = [
    ['baseline', base == null ? '—' : fmt(base, 3), '', ''],
    ['incumbent', best == null ? '—' : fmt(best, 3), 'up',
      delta == null ? '' : `best so far`],
    ['improvement', delta == null ? '—' :
      ((delta >= 0 ? '−' : '+') + fmt(Math.abs(delta), 3)),
      delta >= 0 ? 'up' : 'down', 'vs baseline'],
    ['experiments', String(done.length + fail), '', `of ${S.ideas.length} ideas`],
    ['failures', String(fail), fail ? 'down' : '',
      done.length + fail ? Math.round(fail / (done.length + fail) * 100) + '% rate' : ''],
    ['running', String(runs.filter(r => r.status === 'running').length), '', 'GPUs active'],
  ];
  const st = document.getElementById('stats'); st.innerHTML = '';
  cards.forEach(([k, v, d, sub]) => {
    st.append(el('div', 'stat',
      `<div class="k">${k}</div><div class="v">${v}</div>` +
      `<div class="d ${d}">${sub}</div>`));
  });
}

function tableRows() {
  const runs = frontier(expRuns());
  const byId = {}; runs.forEach(r => byId[r.idea_id] = r);
  let rows = runs.map(r => ({
    exp: '#' + r.exp, id: r.id, kind: 'run', name: r.run_name,
    desc: ideaDesc(r.idea_id), status: r.status,
    metric: r.headline_metric, delta: r.baseline_delta,
    gpu: r.gpu_index, started: r.started_at, ended: r.ended_at,
  }));
  S.ideas.filter(i => i.status === 'not_implemented').forEach(i => {
    rows.push({ exp: '—', id: i.id, kind: 'idea', name: i.idea_id,
      desc: i.description, status: 'queued', metric: null, delta: null,
      gpu: -1, started: '', ended: '' });
  });
  return rows;
}
function ideaDesc(ideaRowId) {
  const i = S.ideas.find(x => x.id === ideaRowId);
  return i ? i.description : '';
}

function paintTable() {
  let rows = tableRows();
  if (S.filter !== 'all') {
    const f = S.filter;
    rows = rows.filter(r => f === 'queued' ? r.status === 'queued'
      : f === 'kept' ? r.status === 'kept'
      : f === 'discarded' ? r.status === 'discarded'
      : f === 'crashed' ? r.status === 'crashed'
      : f === 'running' ? r.status === 'running' : true);
  }
  if (S.search) {
    const q = S.search.toLowerCase();
    rows = rows.filter(r => (r.name + ' ' + r.desc).toLowerCase().includes(q));
  }
  const deltas = rows.map(r => r.delta).filter(d => d != null && isFinite(d));
  const mx = Math.max(0.001, ...deltas.map(Math.abs));
  const ts = document.getElementById('tscroll');
  const tbl = el('table', 'runs');
  tbl.innerHTML = `<thead><tr><th>#</th><th>Status</th><th>Idea</th>` +
    `<th>Result vs baseline</th><th>GPU</th><th>Duration</th></tr></thead>`;
  const tb = el('tbody');
  rows.forEach(r => {
    const tr = el('tr', S.sel === r.id ? 'sel' : '');
    let mc = '<span style="color:#5C636B">—</span>';
    if (r.status === 'crashed') mc = '<span class="chip s-crashed">diverged</span>';
    else if (r.metric != null && isFinite(r.metric)) {
      const d = r.delta || 0, t = Math.max(-1, Math.min(1, d / mx));
      const bg = d >= 0 ? `rgba(52,211,153,${.1 + .3 * t})`
        : `rgba(248,113,113,${.1 + .3 * -t})`;
      mc = `<span class="heat" style="background:${bg}">${fmt(r.metric, 4)}` +
        `<span style="opacity:.7"> ${d >= 0 ? '−' : '+'}` +
        `${fmt(Math.abs(d), 4)}</span></span>`;
    }
    tr.innerHTML =
      `<td class="mono" style="color:#5C636B">${r.exp}</td>` +
      `<td><span class="chip s-${r.status}"><span class="dot"></span>` +
      `${r.status}</span></td>` +
      `<td><div class="idea-name">${esc(r.name)}</div>` +
      `<div class="idea-desc">${esc(r.desc)}</div></td>` +
      `<td>${mc}</td>` +
      `<td class="mono">${r.gpu >= 0 ? r.gpu : '—'}</td>` +
      `<td class="mono">${r.started ? dur(r.started, r.ended) : '—'}</td>`;
    if (r.kind === 'run') {
      tr.onclick = () => openDrawer(r.id);
    } else if (r.status === 'queued') {
      tr.classList.add('q-row');
      tr.draggable = true;
      tr.ondragstart = () => { _dragIdea = r.id; tr.classList.add('dragging'); };
      tr.ondragend = () => {
        tr.classList.remove('dragging');
        document.querySelectorAll('.drop-tgt').forEach(
          x => x.classList.remove('drop-tgt'));
      };
      tr.ondragover = e => { e.preventDefault(); tr.classList.add('drop-tgt'); };
      tr.ondragleave = () => tr.classList.remove('drop-tgt');
      tr.ondrop = e => {
        e.preventDefault(); tr.classList.remove('drop-tgt');
        if (_dragIdea && _dragIdea !== r.id) reorderIdea(_dragIdea, r.id);
      };
      const tds = tr.querySelectorAll('td');
      const last = tds[tds.length - 1];
      last.innerHTML = '<button class="idel" title="Remove idea">✕</button>';
      last.querySelector('.idel').onclick = e => {
        e.stopPropagation();
        const why = prompt('Remove this idea from the queue.\n\n' +
          'Why? (sent to the agent so it learns your preference)');
        if (why === null) return;
        deleteIdea(r.id, why);
      };
    }
    tb.append(tr);
  });
  tbl.append(tb);
  ts.innerHTML = ''; ts.append(tbl);
  if (!rows.length) ts.append(el('div', 'empty', S.runs.length
    ? 'No experiments match this filter.'
    : 'No experiments yet — nothing has run.'));
}

/* ── idea queue: drag-to-rerank + delete ──────────────────────────────── */
let _dragIdea = null;
let _drawerCharts = {};      // {metric_key: LineChart} — live-refreshed by SSE
let _drawerRunId = null;     // run id of the currently-open drawer, if any
function queuedOrder() {
  return tableRows().filter(r => r.status === 'queued').map(r => r.id);
}
async function reorderIdea(dragId, targetId) {
  let order = queuedOrder().filter(x => x !== dragId);
  const ti = order.indexOf(targetId);
  order.splice(ti < 0 ? order.length : ti, 0, dragId);
  await post('/ideas/reorder', { order });
  try { S.ideas = await api('/ideas'); } catch (e) { /* keep */ }
  paintTable();
}
async function deleteIdea(id, reason) {
  await post('/ideas/delete', { idea_id: id, reason: reason });
  try { S.ideas = await api('/ideas'); } catch (e) { /* keep */ }
  paintTable();
}

let _termTimer = null;
function stopTermPoll() {
  if (_termTimer) { clearInterval(_termTimer); _termTimer = null; }
}

function paintRail() {
  document.querySelectorAll('.rail-tab').forEach(b =>
    b.classList.toggle('on', b.dataset.tab === S.railTab));
  const c = document.getElementById('railcontent');
  if (!c) return;
  if (S.railTab === 'live') {
    if (!document.getElementById('term')) { stopTermPoll(); renderLive(c); }
  } else if (S.railTab === 'sessions') {
    if (!c.querySelector('.sess-wrap')) { stopTermPoll(); renderSessions(c); }
  } else {
    stopTermPoll();
    if (document.getElementById('icards')) {
      // live-update: cards + brief without rebuilding the activity feed
      updateBrief();
      paintCompletedCards();
    } else {
      renderSummary(c);
    }
  }
}

function renderLive(c) {
  c.innerHTML = '<pre class="term" id="term">connecting to the agent ' +
    'session…</pre>';
  const term = document.getElementById('term');
  const poll = async () => {
    try {
      const d = await api('/agent/terminal');
      const atBottom = term.scrollHeight - term.scrollTop - term.clientHeight
        < 80;
      term.textContent = ((d.text || '').replace(/[ \t\r\n]+$/, '')) ||
        '(no output)';
      if (atBottom) term.scrollTop = term.scrollHeight;
    } catch (e) { /* keep last frame */ }
  };
  poll();
  _termTimer = setInterval(poll, 2500);
}

function renderSessions(c) {
  c.innerHTML = '<div class="sess-wrap">' +
    '<div class="sess-tabs" id="sesstabs"></div>' +
    '<pre class="term" id="sessterm">loading run sessions…</pre></div>';
  const tabsEl = c.querySelector('#sesstabs');
  const term = c.querySelector('#sessterm');
  let known = '';
  const buildTabs = (names) => {
    tabsEl.innerHTML = '';
    if (!names.length) {
      tabsEl.innerHTML = '<span class="sess-empty">no run sessions yet</span>';
      return;
    }
    if (!names.includes(S.sessTab)) S.sessTab = names[0];
    names.forEach(n => {
      const b = el('button', 'sess-tab' + (n === S.sessTab ? ' on' : ''), n);
      b.onclick = () => {
        S.sessTab = n;
        tabsEl.querySelectorAll('.sess-tab').forEach(x =>
          x.classList.toggle('on', x === b));
        poll();
      };
      tabsEl.append(b);
    });
  };
  const poll = async () => {
    try {
      const s = await api('/sessions');
      const names = s.sessions || [];
      const sig = names.join('|');
      if (sig !== known) { known = sig; buildTabs(names); }
      if (!names.length) {
        term.textContent = 'No run sessions yet.\n\nThe agent launches each ' +
          'training run in its own tmux session — each one will appear above ' +
          'as a clickable tab showing that run’s live output.';
        return;
      }
      const d = await api('/sessions/' + encodeURIComponent(S.sessTab));
      const atBottom = term.scrollHeight - term.scrollTop
        - term.clientHeight < 80;
      term.textContent = ((d.text || '').replace(/[ \t\r\n]+$/, ''))
        || (d.alive ? '(no output yet)' : '(session ended)');
      if (atBottom) term.scrollTop = term.scrollHeight;
    } catch (e) { /* keep last frame */ }
  };
  poll();
  _termTimer = setInterval(poll, 2500);
}

/* Summary feed — incrementally appended, smart-scrolled, lazy-loaded.
   The feed is built ONCE on tab open; SSE events APPEND new rows (no
   re-render, no scroll yank), and scrolling near the top fetches older
   events from the backend and prepends them. */

function feedItemEl(it) {
  if (it.kind === 'chat') {
    return el('div', 'bub ' + it.d.role, esc(it.d.content));
  }
  const e = it.d;
  const ic = e.type === 'breakthrough' ? ['win', '★']
    : e.type === 'run_finished' && e.severity === 'warning' ? ['fail', '✕']
    : e.type === 'run_finished' ? ['', '✓']
    : e.type === 'run_started' ? ['', '▶']
    : e.type === 'idea_added' ? ['', '✎'] : ['', '•'];
  const row = el('div', 'ev');
  row.innerHTML = `<div class="ev-ic ${ic[0]}">${ic[1]}</div>` +
    `<div class="ev-bd"><div class="ev-msg">${esc(e.message)}</div>` +
    `<div class="ev-tm">${esc(e.actor)} · ${ago(e.created_at)}</div></div>`;
  return row;
}

function updateBrief() {
  const brief = document.getElementById('brief');
  if (!brief) return;
  const p = S.project || {};
  const runs = frontier(expRuns());
  const kept = runs.filter(r => r._frontier);
  const fail = S.runs.filter(r => r.status === 'crashed').length;
  const top = kept[kept.length - 1];
  if (!runs.length) {
    brief.innerHTML = `<h3><span class="dot" style="color:#6366F1"></span>` +
      `Research brief</h3>` +
      `<p>Project <span class="big">${esc(p.name || '')}</span> is ` +
      `configured. No experiments have run yet — switch to the <b>Live</b> ` +
      `tab to watch the agent set up.</p>`;
  } else {
    brief.innerHTML = `<h3><span class="dot live" style="color:#6366F1">` +
      `</span>Research brief</h3>` +
      `<p><span class="big">${runs.length}</span> experiments run, ` +
      `<span class="big">${kept.length}</span> kept improvements, ` +
      `<span class="big">${fail}</span> diverged. ` +
      (top ? `Incumbent: <span class="big">${esc(top.run_name)}</span> at ` +
        `${fmt(top.headline_metric, 4)}` +
        (p.baseline_metric != null
          ? ` (−${fmt(p.baseline_metric - top.headline_metric, 3)} vs ` +
            `baseline)` : '') + '.' : '') + `</p>`;
  }
}

function appendFeedItem(it) {
  const feed = document.getElementById('feed');
  if (!feed) return;
  const atBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80;
  feed.append(feedItemEl(it));
  if (atBottom) feed.scrollTop = feed.scrollHeight;
}

async function loadOlderEvents() {
  const feed = document.getElementById('feed');
  if (!feed || S._feedLoading) return;
  const oldest = S.events.reduce(
    (a, b) => (a && a.created_at < b.created_at) ? a : b, null);
  if (!oldest) return;
  S._feedLoading = true;
  try {
    const older = await api('/events?limit=60&before='
      + encodeURIComponent(oldest.created_at));
    if (older && older.length) {
      const seen = new Set(S.events.map(e => e.id));
      const fresh = older.filter(e => e.id && !seen.has(e.id));
      if (fresh.length) {
        S.events = fresh.concat(S.events);
        if (S.events.length > 2000) S.events = S.events.slice(-2000);
        const distFromBottom = feed.scrollHeight - feed.scrollTop;
        const frag = document.createDocumentFragment();
        fresh.sort((a, b) => (a.created_at || '').localeCompare(
            b.created_at || ''))
          .forEach(e => frag.append(feedItemEl(
            { t: e.created_at, kind: 'event', d: e })));
        feed.prepend(frag);
        feed.scrollTop = feed.scrollHeight - distFromBottom;
      }
    }
  } catch (e) { /* ignore */ }
  S._feedLoading = false;
}

/* ── completed-ideas card view ─────────────────────────────────────────────
   Replaces the old "feed of checkmarks". One card per completed experiment,
   newest first. Each card shows: status icon · run name · what/why · result
   chip · the council's learning (color-coded by reviewer). Clicking the card
   opens the run drawer. The activity feed is still available below in a
   compact, collapsed-by-default panel. */

const STATUS_META = {
  kept:        { ic: '✓', cls: 's-kept',      lbl: 'kept' },
  success:     { ic: '✓', cls: 's-kept',      lbl: 'kept' },
  discarded:   { ic: '◯', cls: 's-discarded', lbl: 'discarded' },
  failed:      { ic: '◯', cls: 's-discarded', lbl: 'discarded' },
  crashed:     { ic: '✕', cls: 's-crashed',   lbl: 'crashed' },
  running:     { ic: '▶', cls: 's-running',   lbl: 'running' },
  queued:      { ic: '·', cls: 's-running',   lbl: 'queued' },
};

const REVIEWER_META = {
  gemini: { lbl: 'Gemini',  color: '#4285F4' },
  openai: { lbl: 'GPT-5.5', color: '#10A37F' },
  claude: { lbl: 'Claude',  color: '#D97757' },
};

function ideaCard(run) {
  const sm = STATUS_META[run.status] || STATUS_META.queued;
  const cfg = run.config || {};
  const what = (cfg.what || cfg.description || cfg.hypothesis || '').toString().trim();
  const why = (cfg.why || '').toString().trim();
  const review = (cfg.review && typeof cfg.review === 'object') ? cfg.review : null;
  const metric = run.headline_metric;
  const delta = run.baseline_delta;
  const isBaseline = run.is_baseline;
  const card = el('div', 'icard');
  // header row: icon + title + chips
  const hd = el('div', 'icard-hd');
  hd.innerHTML =
    `<div class="icard-ic ${sm.cls}">${sm.ic}</div>` +
    `<div class="icard-ti">` +
      `<div class="icard-name">${esc(run.run_name || run.id)}</div>` +
      `<div class="icard-sub">${esc(ago(run.ended_at || run.created_at))}` +
        (isBaseline ? ' · <b>baseline</b>' : '') + `</div>` +
    `</div>` +
    `<div class="icard-mt">` +
      (run.status === 'crashed'
        ? `<span class="chip s-crashed">diverged</span>`
        : (metric == null ? ''
          : `<span class="chip ${sm.cls}">${fmt(metric)}</span>` +
            (delta == null ? ''
              : `<div class="icard-delta">${
                  delta >= 0 ? '−' + fmt(Math.abs(delta), 3)
                             : '+' + fmt(Math.abs(delta), 3)
                } vs base</div>`))) +
    `</div>`;
  card.append(hd);
  // what / why
  if (what || why) {
    const bd = el('div', 'icard-bd');
    if (what) bd.append(el('div', 'icard-what', esc(what)));
    if (why)  bd.append(el('div', 'icard-why',  '<i>why:</i> ' + esc(why)));
    card.append(bd);
  }
  // council learning, if any
  if (review && (review.learning || '').trim()) {
    const rm = REVIEWER_META[review.reviewer] || { lbl: review.reviewer || '?', color: '#9CA3AF' };
    const rv = el('div', 'icard-review');
    rv.innerHTML =
      `<div class="icard-rv-tag" style="color:${rm.color}">` +
        `★ Council · ${esc(rm.lbl)}</div>` +
      `<div class="icard-rv-bd">${esc(review.learning.trim())}</div>` +
      (Array.isArray(review.new_ideas) && review.new_ideas.length
        ? `<div class="icard-rv-new">Proposed: ` +
          review.new_ideas.slice(0, 3).map(ni =>
            `<code>${esc(ni.idea_id || '?')}</code>`).join(' ') + `</div>`
        : '');
    card.append(rv);
  } else if (run.status !== 'running') {
    // placeholder while we wait for the council (only if council is on)
    const rv = el('div', 'icard-review pending');
    rv.innerHTML = `<div class="icard-rv-tag" style="color:#94A3B8">` +
      `★ Council · pending…</div>`;
    card.append(rv);
  }
  card.onclick = () => openDrawer(run.id);
  return card;
}

function renderSummary(c) {
  c.innerHTML = '';
  const brief = el('div', 'brief'); brief.id = 'brief';
  c.append(brief);
  updateBrief();

  // Completed experiments — newest first
  const wrap = el('div', 'icards'); wrap.id = 'icards';
  c.append(el('div', 'rail-h', 'Completed experiments'));
  c.append(wrap);

  const compact = el('details', 'feed-compact');
  compact.innerHTML = '<summary>Activity feed</summary>';
  const feed = el('div', 'feed'); feed.id = 'feed';
  compact.append(feed);
  c.append(compact);

  paintCompletedCards();

  // activity feed (kept for chronological events + chat)
  const items = [
    ...S.events.map(e => ({ t: e.created_at, kind: 'event', d: e })),
    ...S.chat.map(m => ({ t: m.created_at, kind: 'chat', d: m })),
  ].sort((a, b) => (a.t || '').localeCompare(b.t || ''));
  items.forEach(it => feed.append(feedItemEl(it)));
  feed.scrollTop = feed.scrollHeight;
  S._feedLoading = false;
  feed.onscroll = () => {
    if (feed.scrollTop < 60) loadOlderEvents();
  };
}

function paintCompletedCards() {
  const wrap = document.getElementById('icards');
  if (!wrap) return;
  // include both finished runs (kept/discarded/crashed) and currently-running
  // so the user sees the queue's progress live; running runs sort to the top.
  const ranked = (S.runs || []).slice().sort((a, b) => {
    const sa = a.status === 'running' ? 0 : 1;
    const sb = b.status === 'running' ? 0 : 1;
    if (sa !== sb) return sa - sb;
    return (b.ended_at || b.created_at || '')
      .localeCompare(a.ended_at || a.created_at || '');
  });
  wrap.innerHTML = '';
  if (!ranked.length) {
    wrap.append(el('div', 'icards-empty',
      'No experiments yet — the agent will start running ideas from the queue soon.'));
    return;
  }
  ranked.forEach(r => wrap.append(ideaCard(r)));
}

function paintGpus() {
  const strip = document.getElementById('gpus'); if (!strip) return;
  const gpus = (S.gpus || []).slice().sort((a, b) => a.index - b.index);
  strip.innerHTML = '';
  if (!gpus.length) { strip.append(el('span', 'gpu-none', 'GPUs —')); return; }
  let active = 0;
  gpus.forEach(g => {
    const util = Math.max(0, Math.min(100, g.util_pct || 0));
    const vram = g.vram_used_mb || 0;
    const busy = util > 5 || vram > 600;
    if (busy) active++;
    const b = el('div', 'gpu-bar' + (busy ? '' : ' idle'));
    b.title = `GPU ${g.index} · ${util.toFixed(0)}% · ` +
      `${(vram / 1024).toFixed(1)}/${Math.round((g.total_vram_mb || 0) / 1024)}` +
      `GB` + (g.temp_c ? ` · ${Math.round(g.temp_c)}°C` : '');
    const i = el('i'); i.style.height = Math.max(6, util) + '%';
    b.append(i);
    strip.append(b);
  });
  strip.append(el('span', 'gpu-count', `${active}/${gpus.length} active`));
}

function pollGpus() {
  const tick = async () => {
    try { S.gpus = await api('/gpus'); paintGpus(); } catch (e) { /* keep */ }
  };
  tick();
  addTimer(setInterval(tick, 4000));
}

/* ── drawer ───────────────────────────────────────────────────────────── */
async function openDrawer(runId) {
  S.sel = runId;
  let d = document.querySelector('.drawer'), sc = document.querySelector('.scrim');
  if (!d) { sc = el('div', 'scrim'); d = el('div', 'drawer');
    sc.onclick = closeDrawer; document.body.append(sc, d); }
  sc.classList.add('open'); d.classList.add('open');
  d.innerHTML = '<div class="dr-bd"><div class="skel" style="height:240px">' +
    '</div></div>';
  const run = await api('/runs/' + runId);
  if (!S.metrics[runId]) S.metrics[runId] = await api('/runs/' + runId + '/metrics');
  const idea = run.idea || {};
  const m = S.metrics[runId] || {};
  d.innerHTML = '';
  const hd = el('div', 'dr-hd');
  hd.innerHTML = `<div><div style="font-size:16px;font-weight:700">` +
    `${esc(run.run_name)}</div><div style="margin-top:6px"><span class="chip ` +
    `s-${run.status}"><span class="dot"></span>${run.status}</span></div></div>`;
  if (run.status === 'running') {
    const kb = el('button', 'dr-kill', 'Kill run');
    kb.onclick = async () => {
      if (!confirm('Kill this run? Its tmux session will be terminated.'))
        return;
      await post('/runs/' + encodeURIComponent(runId) + '/kill', {});
      kb.textContent = 'Killed'; kb.disabled = true;
    };
    hd.append(kb);
  }
  const x = el('button', 'iconbtn', '✕'); x.onclick = closeDrawer;
  hd.append(x); d.append(hd);
  const bd = el('div', 'dr-bd');
  if ((idea.description || '').trim()) {
    bd.append(el('div', 'dr-h2', 'Hypothesis'));
    bd.append(el('div', 'prose', esc(idea.description)));
  }
  if (idea.why) {
    bd.append(el('div', 'dr-h2', 'Why'));
    bd.append(el('div', 'prose', esc(idea.why)));
  }
  bd.append(el('div', 'dr-h2', 'Result'));
  const dl = el('dl', 'kv');
  const delta = run.baseline_delta;
  [['final ' + (S.project.validation_metric), run.status === 'crashed'
      ? 'diverged' : fmt(run.headline_metric)],
   ['vs baseline', delta == null ? '—'
      : (delta >= 0 ? '−' : '+') + fmt(Math.abs(delta))],
   ['GPU', run.gpu_index], ['duration', dur(run.started_at, run.ended_at)],
   ['commit', run.git_commit || '—']]
    .forEach(([k, v]) => dl.innerHTML += `<dt>${k}</dt><dd>${esc(v)}</dd>`);
  bd.append(dl);
  // metrics — curves for time-series, a value list for single points
  const have = Object.keys(m).filter(k => (m[k] || []).length);
  const curveKeys = have.filter(k => m[k].length > 1);
  const pointKeys = have.filter(k => m[k].length === 1);
  // live charts: SSE pushes new metric points into m[k] (same reference),
  // and the SSE handler calls .draw() on each chart whose run is open
  _drawerCharts = {};
  _drawerRunId = runId;
  if (curveKeys.length) {
    bd.append(el('div', 'dr-h2',
      'Training curves' + (run.status === 'running' ? ' · live' : '')));
    curveKeys.forEach(k => {
      bd.append(el('div', '', `<div style="font-size:10.5px;color:#5C636B;` +
        `font-family:${MONO}">${esc(k)}</div>`));
      const box = el('div', 'mini'); bd.append(box);
      const chart = new LineChart(box,
        k.includes('loss') ? '#FBBF24' : '#34D399');
      chart.setData(m[k]);
      _drawerCharts[k] = chart;
    });
  }
  if (pointKeys.length) {
    bd.append(el('div', 'dr-h2', 'Logged values'));
    const pdl = el('dl', 'kv');
    pointKeys.forEach(k => { pdl.innerHTML +=
      `<dt>${esc(k)}</dt><dd>${fmt(m[k][0][1])}</dd>`; });
    bd.append(pdl);
  }
  if (!have.length) {
    bd.append(el('div', 'dr-h2', 'Metrics'));
    bd.append(el('div', 'prose', 'This run logged no metric time-series.'));
  }
  // config = "what changed"
  if (run.config && Object.keys(run.config).length) {
    bd.append(el('div', 'dr-h2', 'Config — what changed'));
    const cdl = el('dl', 'kv');
    Object.entries(run.config).forEach(([k, v]) =>
      cdl.innerHTML += `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`);
    bd.append(cdl);
    const cp = el('button', 'btn', 'Copy repro command');
    cp.style.marginTop = '8px';
    cp.onclick = () => { navigator.clipboard?.writeText(
      `ARUI_RUN_NAME=${run.run_name} ARUI_CONFIG='${JSON.stringify(run.config)}'` +
      ` python train.py`); cp.textContent = 'Copied ✓'; };
    bd.append(cp);
  }
  if (idea.analysis) {
    bd.append(el('div', 'dr-h2', 'Agent analysis'));
    bd.append(el('div', 'prose', esc(idea.analysis)));
  }
  // Council review (if any) — what the external LLM panel made of this run
  const _review = (run.config && run.config.review) || null;
  if (_review && (_review.learning || '').trim()) {
    const rm = REVIEWER_META[_review.reviewer] || { lbl: _review.reviewer || '?', color: '#9CA3AF' };
    bd.append(el('div', 'dr-h2', `Council review · ${rm.lbl}`));
    const wrap = el('div', 'icard-review');
    wrap.style.borderLeftColor = rm.color;
    wrap.innerHTML =
      `<div class="icard-rv-bd">${esc(_review.learning.trim())}</div>` +
      (Array.isArray(_review.new_ideas) && _review.new_ideas.length
        ? `<div class="icard-rv-new" style="margin-top:8px"><b>Proposed next:</b> ` +
          _review.new_ideas.slice(0, 3).map(ni =>
            `<code>${esc(ni.idea_id || '?')}</code> ${esc(ni.what || '')}`)
            .join('<br>') + `</div>` : '') +
      (Array.isArray(_review.veto) && _review.veto.length
        ? `<div class="icard-rv-new" style="margin-top:6px;color:var(--bad)">` +
          `<b>Vetoed:</b> ` + _review.veto.slice(0, 5).map(v =>
            `<code>${esc(v)}</code>`).join(' ') + `</div>` : '');
    bd.append(wrap);
  }
  // run logs — captured to disk, so they persist after the run finishes
  bd.append(el('div', 'dr-h2', 'Logs'));
  const logBox = el('pre', 'dr-logs', 'loading logs…');
  bd.append(logBox);
  d.append(bd);
  paintTable();
  try {
    const lg = await api('/runs/' + encodeURIComponent(runId) +
      '/logs?tail=700');
    logBox.textContent = (lg.text || '').trim()
      || (lg.alive ? '(no output captured yet)'
        : '(no logs — this run finished before log capture was enabled, or '
          + 'produced no output)');
    logBox.scrollTop = logBox.scrollHeight;
  } catch (e) {
    logBox.textContent = '(could not load logs)';
  }
}
function closeDrawer() {
  S.sel = null;
  _drawerCharts = {}; _drawerRunId = null;
  document.querySelector('.scrim')?.classList.remove('open');
  document.querySelector('.drawer')?.classList.remove('open');
  paintTable();
}

/* ── chat ─────────────────────────────────────────────────────────────── */
async function sendChat(text) {
  text = (text || '').trim(); if (!text) return;
  const inp = document.getElementById('chatin'); if (inp) inp.value = '';
  const r = await post('/agent/send', { text });   // typed into the tmux session
  if (r && r.ok === false) {
    S.chat.push({ role: 'agent', created_at: new Date().toISOString(),
      content: '⚠ could not deliver — ' + (r.error || 'no agent session') });
    if (S.railTab === 'summary') paintRail();
  }
}

/* ── SSE ──────────────────────────────────────────────────────────────── */
function streams() {
  const m = new EventSource('/api/stream/metrics');
  m.addEventListener('metric', e => {
    const { run_id, points } = JSON.parse(e.data);
    const md = S.metrics[run_id] || (S.metrics[run_id] = {});
    points.forEach(p => (md[p.key] || (md[p.key] = [])).push([p.step, p.value]));
    if (run_id === _drawerRunId) {
      const fresh = new Set(points.map(p => p.key));
      fresh.forEach(k => _drawerCharts[k] && _drawerCharts[k].draw());
    }
  });
  const ev = new EventSource('/api/stream/events');
  ev.addEventListener('event', e => {
    const evt = JSON.parse(e.data);
    if (!evt.id || S.events.find(x => x.id === evt.id)) return;
    S.events.push(evt);
    if (S.events.length > 2000) S.events = S.events.slice(-2000);
    appendFeedItem({ t: evt.created_at, kind: 'event', d: evt });
  });
  // The agent can fire runs_changed several times per second (every run
  // start/finish), and each one triggers 3 fetches + a full repaint, which
  // wedges the browser. Coalesce a burst into one update.
  let _rcTimer = null, _rcPending = false;
  ev.addEventListener('runs_changed', () => {
    _rcPending = true;
    if (_rcTimer) return;
    _rcTimer = setTimeout(async () => {
      _rcTimer = null;
      if (!_rcPending) return;
      _rcPending = false;
      try {
        [S.project, S.runs, S.ideas] = await Promise.all(
          [api('/project'), api('/runs'), api('/ideas')]);
      } catch (e) { return; }                // network blip: try again later
      if (S.view !== 'dashboard') return;
      paintHero(); paintStats(); paintTable(); paintRail();
      document.querySelector('.hdr')?.replaceWith(header()); paintGpus();
    }, 800);
  });
  const ch = new EventSource('/api/stream/chat');
  ch.addEventListener('chat', e => {
    const msg = JSON.parse(e.data);
    S.chat.push(msg);
    appendFeedItem({ t: msg.created_at, kind: 'chat', d: msg });
  });
}

/* ── onboarding ───────────────────────────────────────────────────────── */
const OB_FIELDS = [
  ['sec', 'Researcher'],
  ['email', 'Your email (sender address for alerts)', 'email',
    'you@example.com'],
  ['sec', 'GitHub'],
  ['github_token', 'GitHub token', 'password', 'ghp_…'],
  ['github_username', 'GitHub username', 'text', 'octocat'],
  ['github_email', 'GitHub email', 'email', 'you@example.com'],
  ['repo_name', 'New repo name', 'text', 'my-research'],
  ['sec', 'Models'],
  ['claude_token', 'Claude API token', 'password', 'sk-ant-…'],
  ['gemini_token', 'Gemini token (optional)', 'password', ''],
  ['openai_token', 'OpenAI token (optional)', 'password', ''],
  ['skip_perms', 'Run the agent with --dangerously-skip-permissions',
    'check', ''],
  ['sec', 'Research'],
  ['purpose', 'Purpose — what are we researching, and why?', 'area',
    'e.g. Take TRM (Tiny Recursive Model) as a baseline and improve on it for ARC-AGI-2…'],
  ['seed_ideas', 'Seed ideas', 'area', 'One idea per line…'],
  ['eval', 'Evaluation function / validation set', 'area', ''],
  ['metric', 'Validation metric', 'select',
    'val_loss|perplexity|accuracy|f1|rmse|mse|fid|bpb|arc_score|custom'],
  ['baseline', 'Baseline method(s) to run first', 'area',
    'e.g. TRM (Tiny Recursive Model)'],
  ['sec', 'Agent (advanced — leave the textarea alone for sensible defaults)'],
  ['agent_instructions',
    'How the agent should work (logging rules, GPU saturation, ideas.md '
    + 'format, …). Edit to customise; blank uses the default.', 'area', ''],
  ['sec', 'Email alerts — optional (leave the app password blank for none)'],
  ['cadence', 'Cadence', 'select', 'off|immediate|1h|4h|12h|24h'],
  ['email_recipients', 'Recipients (comma-separated)', 'text',
    'you@example.com, teammate@example.com'],
  ['gmail_app_pw', 'Gmail app password (for the sender email above)',
    'password', ''],
  ['sec', 'Access'],
  ['passcode', 'Dashboard passcode (blank = open)', 'text', ''],
];

function onboarding() {
  const app = document.getElementById('app');
  app.className = 'onb'; app.innerHTML = '';
  const wrap = el('div', 'onb-wrap');
  wrap.append(el('div', 'onb-head',
    `<div class="brand" style="justify-content:center">` +
    `<div class="brand-mark">a</div><b>autoresearcher<span>UI</span></b></div>` +
    `<h1>Set up your research</h1>` +
    `<p>Fill this in — or paste a config block — and the autoresearcher starts.</p>`));

  // resume-from-archive
  const rc = el('div', 'onb-card onb-restore');
  rc.append(el('div', 'onb-sec', 'Resume from an archive'));
  rc.append(el('p', 'onb-restore-p',
    'Moving servers? Upload a .tar.gz archive saved from a previous ' +
    'autoresearcherUI server to restore the project, runs, metrics and ' +
    'checkpoints — the agent picks the research back up where it left off.'));
  const fi = el('input'); fi.type = 'file';
  fi.accept = '.gz,.tgz,.tar'; fi.className = 'onb-file';
  const fb = el('button', 'btn', 'Restore & resume');
  const fst = el('div', 'onb-restore-status');
  fb.onclick = async () => {
    if (!fi.files || !fi.files[0]) { fst.textContent = 'Choose a file first.';
      return; }
    fb.disabled = true;
    fst.textContent = 'Uploading & restoring — large archives take a while…';
    try {
      const fd = new FormData(); fd.append('file', fi.files[0]);
      const r = await fetch('/api/restore', { method: 'POST', body: fd })
        .then(x => x.json());
      if (r && r.status === 'restored') {
        fst.textContent = `Restored — ${r.runs} runs, ${r.interrupted} ` +
          `interrupted. Loading dashboard…`;
        setTimeout(() => location.reload(), 1400);
      } else {
        fb.disabled = false;
        fst.textContent = 'Restore failed: ' + ((r && r.detail) || 'bad archive');
      }
    } catch (e) {
      fb.disabled = false; fst.textContent = 'Restore failed: ' + e;
    }
  };
  rc.append(fi, fb, fst);
  wrap.append(rc);

  const bp = el('div', 'onb-card');
  bp.append(el('div', 'onb-sec', 'Quick paste'));
  const bpa = el('textarea', 'onb-bulk');
  bpa.placeholder = 'Paste a .env (KEY=value) block, then click Parse & fill.';
  bpa.value =
`# Edit the values, then click "Parse & fill" (or just edit the form below).
EMAIL=you@example.com
GITHUB_TOKEN=ghp_REPLACE_ME
GITHUB_USERNAME=your-github-username
GITHUB_EMAIL=you@example.com
REPO_NAME=arc-agi-trm
CLAUDE_TOKEN=sk-ant-REPLACE_ME
GEMINI_TOKEN=
OPENAI_TOKEN=
SKIP_PERMS=true
PURPOSE=Take TRM (Tiny Recursive Model) as the baseline and discover architecture and training changes that improve its ARC-AGI-2 score.
SEED_IDEAS=Deeper recursion depth; larger latent state; learned adaptive halting; deep supervision across recursion steps; ARC grid-symmetry augmentation.
EVAL=Official ARC-AGI-2 public evaluation set.
METRIC=arc_score
BASELINE=TRM (Tiny Recursive Model).
CADENCE=1h
EMAIL_RECIPIENTS=you@example.com
GMAIL_APP_PW=
PASSCODE=`;
  const bpb = el('button', 'btn', 'Parse & fill');
  bp.append(bpa, bpb); wrap.append(bp);

  const form = el('div', 'onb-card');
  const inp = {};
  OB_FIELDS.forEach(f => {
    if (f[0] === 'sec') { form.append(el('div', 'onb-sec', f[1])); return; }
    const [k, label, type, extra] = f;
    const row = el('div', 'onb-field');
    if (type === 'check') {
      const cb = el('input'); cb.type = 'checkbox'; cb.checked = true;
      inp[k] = cb;
      const lab = el('label', 'onb-check');
      lab.append(cb, document.createTextNode(' ' + label));
      row.append(lab);
    } else {
      row.append(el('label', 'onb-lbl', label));
      let x;
      if (type === 'area') { x = el('textarea', 'onb-in');
        x.rows = (k === 'agent_instructions' ? 14 : 3); }
      else if (type === 'select') {
        x = el('select', 'onb-in');
        extra.split('|').forEach(o => {
          const op = el('option'); op.value = o; op.textContent = o; x.append(op);
        });
      } else { x = el('input', 'onb-in'); x.type = type; }
      if (extra && type !== 'select') x.placeholder = extra;
      inp[k] = x; row.append(x);
    }
    form.append(row);
  });
  wrap.append(form);

  const foot = el('div', 'onb-foot');
  foot.append(el('div', 'onb-note',
    'This saves your project config — it does not show any demo data. The ' +
    'autonomous agent that researches your project (a real Claude Code agent ' +
    'on your GPUs) is the next milestone; until it is built the dashboard ' +
    'stays empty.'));
  const start = el('button', 'btn pri onb-start', 'Start research →');
  foot.append(start); wrap.append(foot);
  app.append(wrap);

  // pre-fill editable defaults (agent_instructions etc.) from the backend
  api('/onboarding/defaults').then(defs => {
    Object.entries(defs || {}).forEach(([k, v]) => {
      if (inp[k] && !inp[k].value) inp[k].value = v;
    });
  }).catch(() => { /* keep the blank textarea */ });

  bpb.onclick = () => {
    const keymap = {};                       // case-insensitive lookup
    Object.keys(inp).forEach(k => keymap[k.toLowerCase()] = inp[k]);
    let filled = 0;
    bpa.value.split('\n').forEach(line => {
      line = line.trim();
      if (!line || line.startsWith('#')) return;
      const i = line.search(/[:=]/);          // accept  key: value  or  key=value
      if (i < 1) return;
      const x = keymap[line.slice(0, i).trim().toLowerCase()];
      if (!x) return;
      let v = line.slice(i + 1).trim();
      if (v.length > 1 && /^(['"]).*\1$/.test(v)) v = v.slice(1, -1);
      if (x.type === 'checkbox') x.checked = /^(true|yes|1|on)$/i.test(v);
      else x.value = v;
      filled++;
    });
    bpb.textContent = filled ? `Filled ${filled} fields ✓` : 'No matching keys';
    setTimeout(() => bpb.textContent = 'Parse & fill', 2000);
  };
  start.onclick = async () => {
    start.disabled = true; start.textContent = 'Starting…';
    const cfg = {};
    Object.entries(inp).forEach(([k, x]) =>
      cfg[k] = x.type === 'checkbox' ? x.checked : x.value);
    await post('/onboarding', cfg);
    setTimeout(() => location.reload(), 600);
  };
}

/* ── archive ──────────────────────────────────────────────────────────── */
const _mb = b => (b >= 1e9 ? (b / 1e9).toFixed(2) + ' GB'
  : (b / 1e6).toFixed(0) + ' MB');

async function openArchive() {
  const sc = el('div', 'mscrim');
  const m = el('div', 'modal');
  m.innerHTML = '<div class="skel" style="height:200px"></div>';
  sc.append(m); document.body.append(sc);
  sc.onclick = e => { if (e.target === sc) sc.remove(); };
  let info;
  try { info = await api('/archive/info'); }
  catch (e) { m.innerHTML = '<p>Could not read archive info.</p>'; return; }
  const rows = Object.entries(info.categories || {}).map(([k, v]) =>
    `<tr><td>${esc(k)}</td><td>${_mb(v)}</td></tr>`).join('');
  m.innerHTML =
    `<div class="modal-hd"><h2>Archive research state</h2>` +
    `<button class="iconbtn" id="arc-x">✕</button></div>` +
    `<p class="modal-sub">Save everything — code, logs, databases, ` +
    `checkpoints — so you can spin up a new server and resume exactly ` +
    `where you left off.</p>` +
    `<table class="modal-tbl">${rows}<tr class="tot"><td>full archive</td>` +
    `<td>${_mb(info.full_bytes)}</td></tr></table>` +
    `<div class="modal-actions">` +
    `<a class="btn pri" href="/api/archive?profile=full" download>` +
    `Download full · ${_mb(info.full_bytes)}</a>` +
    `<a class="btn" href="/api/archive?profile=slim" download>` +
    `Download slim · ${_mb(info.slim_bytes)}</a></div>` +
    `<div class="modal-rsync"><div class="modal-lbl">Server-to-server ` +
    `(best for large state — run this on the NEW box, click to copy):</div>` +
    `<code id="arc-rsync">${esc(info.rsync)}</code></div>` +
    `<div class="modal-actions"><button class="btn" id="arc-email">` +
    `Email me the instructions</button></div>` +
    `<p class="modal-hint">To resume: install autoresearcherUI on the new ` +
    `server and pick “Resume from archive” on the onboarding screen.</p>`;
  m.querySelector('#arc-x').onclick = () => sc.remove();
  const rs = m.querySelector('#arc-rsync');
  rs.onclick = () => {
    navigator.clipboard && navigator.clipboard.writeText(info.rsync);
    rs.classList.add('copied');
  };
  const eb = m.querySelector('#arc-email');
  eb.onclick = async () => {
    eb.disabled = true; eb.textContent = 'Sending…';
    const r = await post('/archive/email', {});
    eb.textContent = (r && r.sent) ? 'Emailed ✓' : 'Email not configured';
  };
}

async function resetAll() {
  if (!confirm('Reset autoresearcherUI?\n\nThis deletes all experiments, runs, '
    + 'metrics and config, and returns to the onboarding screen.')) return;
  await post('/reset', {});
  setTimeout(() => location.reload(), 300);
}

/* ════════════════════════ VIEWS (side-menu) ═══════════════════════════════ */
const PALETTE = ['#6366F1', '#34D399', '#FBBF24', '#F87171', '#7DD3FC',
  '#F472B6', '#A78BFA', '#FB923C', '#4ADE80', '#22D3EE', '#E879F9', '#FACC15'];

function fmtUptime(s) {
  s = s | 0;
  const d = s / 86400 | 0, h = (s % 86400) / 3600 | 0, m = (s % 3600) / 60 | 0;
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}

function viewPane() {
  const c = el('div', 'viewpane');
  if (S.view === 'analysis') renderAnalysis(c);
  else if (S.view === 'system') renderSystem(c);
  else if (S.view === 'authkeys') renderAuthkeys(c);
  else renderLatex(c);
  return c;
}

function renderLatex(c) {
  c.innerHTML = '<div class="latex-soon"><div class="latex-ic">∑</div>' +
    '<h2>LaTeX export</h2><p>Auto-generated paper drafts from your research ' +
    'runs — coming soon.</p></div>';
}

/* ── system stats ─────────────────────────────────────────────────────── */
function renderSystem(c) {
  c.innerHTML = '<div class="sys-wrap"><div class="empty2">loading…</div></div>';
  const tick = async () => {
    const t0 = performance.now();
    let d;
    try { d = await api('/system'); } catch (e) { return; }
    const latency = Math.round(performance.now() - t0);
    const gpus = d.gpus || [];
    const gpuCards = gpus.map(g => {
      const util = Math.round(g.util_pct || 0);
      const vu = ((g.vram_used_mb || 0) / 1024).toFixed(1);
      const vt = Math.round((g.total_vram_mb || 0) / 1024);
      return `<div class="sys-gpu"><div class="sys-gpu-hd"><b>GPU ${g.index}` +
        `</b><span>${util}%</span></div>` +
        `<div class="sys-bar"><i style="width:${util}%"></i></div>` +
        `<div class="sys-gpu-ft">${vu}/${vt} GB · ` +
        `${Math.round(g.temp_c || 0)}°C</div></div>`;
    }).join('');
    const ram = d.ram || {}, disk = d.disk || {};
    const cards = [
      ['CPU', d.cpu_percent != null ? Math.round(d.cpu_percent) + '%' : '—'],
      ['Load avg', (d.loadavg || []).join('  ') || '—'],
      ['RAM', ram.total_gb ? `${ram.used_gb} / ${ram.total_gb} GB` : '—'],
      ['Disk', disk.total_gb ? `${disk.used_gb} / ${disk.total_gb} GB` : '—'],
      ['Disk free', disk.free_gb != null ? disk.free_gb + ' GB' : '—'],
      ['Uptime', d.uptime_sec != null ? fmtUptime(d.uptime_sec) : '—'],
      ['API latency', latency + ' ms'],
    ];
    c.querySelector('.sys-wrap').innerHTML =
      `<div class="sys-sec">GPUs (${gpus.length})</div>` +
      `<div class="sys-gpus">${gpuCards ||
        '<div class="empty2">no GPU data</div>'}</div>` +
      `<div class="sys-sec">Host</div><div class="sys-cards">` +
      cards.map(([k, v]) => `<div class="sys-card"><div class="k">${k}` +
        `</div><div class="v">${esc(v)}</div></div>`).join('') + '</div>';
  };
  tick();
  addTimer(setInterval(tick, 4000));
}

/* ── authorized_keys ──────────────────────────────────────────────────── */
function renderAuthkeys(c) {
  c.innerHTML = '<div class="ak-wrap"><div class="empty2">loading…</div></div>';
  const load = async () => {
    let d;
    try { d = await api('/authkeys'); } catch (e) { return; }
    const keys = d.keys || [];
    const rows = keys.map(k =>
      `<div class="ak-row"><div class="ak-info">` +
      `<div class="ak-fp mono">${esc(k.fingerprint || k.type || 'key')}` +
      `</div><div class="ak-cmt">${esc(k.comment || k.type || '')}</div>` +
      `</div><button class="ak-del" data-fp="${esc(k.fingerprint || '')}">` +
      `Delete</button></div>`).join('')
      || '<div class="empty2">No authorized keys.</div>';
    c.querySelector('.ak-wrap').innerHTML =
      '<div class="ak-warn">⚠ These keys control SSH access to the node. ' +
      'After adding a key, test the new login in another terminal before ' +
      'deleting any old key.</div>' +
      '<div class="sys-sec">SSH into the node</div>' +
      `<code class="ak-ssh">${esc(d.ssh || '')}</code>` +
      `<div class="sys-sec">Authorized keys (${keys.length})</div>` +
      `<div class="ak-list">${rows}</div>` +
      '<div class="sys-sec">Add a public key</div>' +
      '<textarea class="ak-add" placeholder="ssh-ed25519 AAAA… you@host">' +
      '</textarea><button class="btn ak-addbtn">Add key</button>' +
      '<span class="ak-status"></span>';
    c.querySelectorAll('.ak-del').forEach(b => {
      b.onclick = async () => {
        if (!confirm('Delete this SSH key from the node?')) return;
        const r = await post('/authkeys/delete', { fingerprint: b.dataset.fp });
        if (!r.ok) { alert(r.error || 'failed'); }
        load();
      };
    });
    c.querySelector('.ak-addbtn').onclick = async () => {
      const ta = c.querySelector('.ak-add');
      const st = c.querySelector('.ak-status');
      const r = await post('/authkeys', { key: ta.value });
      st.textContent = r.ok ? '  added ✓' : '  ' + (r.error || 'failed');
      st.style.color = r.ok ? 'var(--ok)' : 'var(--bad)';
      if (r.ok) { ta.value = ''; load(); }
    };
  };
  load();
}

/* ── analysis: multi-run comparison ───────────────────────────────────── */
class MultiChart {
  constructor(host) {
    this.host = host; this.series = []; this.log = false; this.hx = null;
    this.canvas = el('canvas'); this.tip = el('div', 'tip');
    host.append(this.canvas, this.tip);
    this.canvas.addEventListener('mousemove', e => {
      const r = this.canvas.getBoundingClientRect();
      this.hx = e.clientX - r.left; this.draw();
    });
    this.canvas.addEventListener('mouseleave', () => {
      this.hx = null; this.tip.style.opacity = 0; this.draw();
    });
    new ResizeObserver(() => this.draw()).observe(host);
  }
  setData(series) { this.series = series || []; this.draw(); }
  draw() {
    const ser = this.series.filter(s => s.points && s.points.length);
    const w = this.host.clientWidth || 600, h = this.host.clientHeight || 340;
    const dpr = devicePixelRatio || 1;
    this.canvas.width = w * dpr; this.canvas.height = h * dpr;
    this.canvas.style.width = w + 'px'; this.canvas.style.height = h + 'px';
    const c = this.canvas.getContext('2d');
    c.setTransform(dpr, 0, 0, dpr, 0, 0); c.clearRect(0, 0, w, h);
    if (!ser.length) {
      c.fillStyle = '#5C636B'; c.font = '12px sans-serif';
      c.textAlign = 'center';
      c.fillText('Tick runs on the left and pick a metric to compare.',
        w / 2, h / 2);
      return;
    }
    const pad = { l: 60, r: 16, t: 14, b: 26 };
    let xs = [], ys = [];
    ser.forEach(s => s.points.forEach(p => { xs.push(p[0]); ys.push(p[1]); }));
    let xlo = Math.min(...xs), xhi = Math.max(...xs);
    let ylo = Math.min(...ys), yhi = Math.max(...ys);
    const log = this.log && ylo > 0;
    const tf = v => log ? Math.log10(v) : v;
    let Ylo = tf(ylo), Yhi = tf(yhi);
    const yp = (Yhi - Ylo) * 0.08 || Math.abs(Yhi) * 0.1 || 1;
    Ylo -= yp; Yhi += yp;
    if (xhi === xlo) xhi++;
    const X = v => pad.l + (v - xlo) / (xhi - xlo) * (w - pad.l - pad.r);
    const Y = v => pad.t + (1 - (tf(v) - Ylo) / (Yhi - Ylo))
      * (h - pad.t - pad.b);
    c.font = '10px ' + MONO; c.textAlign = 'right'; c.textBaseline = 'middle';
    for (let i = 0; i <= 4; i++) {
      const yy = pad.t + i / 4 * (h - pad.t - pad.b);
      const val = log ? 10 ** (Yhi - i / 4 * (Yhi - Ylo))
        : Yhi - i / 4 * (Yhi - Ylo);
      c.strokeStyle = '#1b1f25'; c.beginPath();
      c.moveTo(pad.l, yy); c.lineTo(w - pad.r, yy); c.stroke();
      c.fillStyle = '#5C636B';
      c.fillText(Math.abs(val) < 1000 && Math.abs(val) >= 0.001
        ? val.toFixed(3) : val.toExponential(1), pad.l - 8, yy);
    }
    ser.forEach(s => {
      c.strokeStyle = s.color; c.lineWidth = 1.8; c.beginPath();
      s.points.forEach(([x, y], i) => {
        const px = X(x), py = Y(y);
        i ? c.lineTo(px, py) : c.moveTo(px, py);
      });
      c.stroke();
    });
    // hover crosshair + tooltip
    if (this.hx != null && this.hx >= pad.l && this.hx <= w - pad.r) {
      const dx = xlo + (this.hx - pad.l) / (w - pad.l - pad.r) * (xhi - xlo);
      c.strokeStyle = '#3a4150'; c.setLineDash([3, 3]); c.lineWidth = 1;
      c.beginPath(); c.moveTo(this.hx, pad.t);
      c.lineTo(this.hx, h - pad.b); c.stroke(); c.setLineDash([]);
      const rows = [];
      ser.forEach(s => {
        let best = null, bd2 = Infinity;
        for (const p of s.points) {
          const d2 = Math.abs(p[0] - dx);
          if (d2 < bd2) { bd2 = d2; best = p; }
        }
        if (best) {
          rows.push({ s, p: best });
          c.fillStyle = s.color;
          c.beginPath(); c.arc(X(best[0]), Y(best[1]), 3.6, 0, 7); c.fill();
          c.strokeStyle = '#0B0D10'; c.lineWidth = 1.4; c.stroke();
        }
      });
      if (rows.length) {
        this.tip.innerHTML =
          `<div style="color:#9BA1A8;margin-bottom:3px">step ` +
          `${rows[0].p[0]}</div>` + rows.map(({ s, p }) =>
            `<div style="white-space:nowrap"><span style="display:` +
            `inline-block;width:8px;height:8px;border-radius:2px;` +
            `background:${s.color};margin-right:5px"></span>` +
            `${esc(s.name)} <b>${fmt(p[1])}</b></div>`).join('');
        this.tip.style.opacity = 1;
        let tx = this.hx + 14;
        if (tx > w - 180) tx = this.hx - 184;
        this.tip.style.left = Math.max(4, tx) + 'px';
        this.tip.style.top = (pad.t + 4) + 'px';
      }
    }
  }
}

async function renderAnalysis(c) {
  c.innerHTML =
    '<div class="ana-wrap"><div class="ana-side"></div>' +
    '<div class="ana-main"><div class="ana-bar"></div>' +
    '<div class="ana-chart"></div><div class="ana-legend"></div></div></div>';
  const side = c.querySelector('.ana-side');
  const bar = c.querySelector('.ana-bar');
  const legend = c.querySelector('.ana-legend');
  let names = [];
  try { names = (await api('/metrics/names')).metrics || []; } catch (e) { }
  if (!names.length) names = ['train_loss'];
  if (!names.includes(S.cmpMetric)) {
    S.cmpMetric = names.includes('train_loss') ? 'train_loss' : names[0];
  }
  const runs = expRuns().slice().reverse();
  side.innerHTML = '<div class="ana-h">Runs to compare</div>';
  runs.forEach(r => {
    const lab = el('label', 'ana-run');
    const cb = el('input'); cb.type = 'checkbox';
    cb.checked = S.cmp.includes(r.id);
    cb.onchange = () => {
      if (cb.checked) { if (!S.cmp.includes(r.id)) S.cmp.push(r.id); }
      else S.cmp = S.cmp.filter(x => x !== r.id);
      draw();
    };
    lab.append(cb, document.createTextNode(' ' + r.run_name));
    side.append(lab);
  });
  const sel = el('select', 'ana-select');
  names.forEach(m => {
    const o = el('option'); o.value = m; o.textContent = m;
    if (m === S.cmpMetric) o.selected = true; sel.append(o);
  });
  sel.onchange = () => { S.cmpMetric = sel.value; draw(); };
  const logb = el('button', 'tg' + (S.cmpLog ? ' on' : ''), 'log y');
  logb.onclick = () => {
    S.cmpLog = !S.cmpLog; logb.classList.toggle('on');
    chart.log = S.cmpLog; chart.draw();
  };
  bar.append(el('span', 'ana-lbl', 'Metric'), sel, logb,
    el('span', 'ana-note', 'pick up to 12 runs'));
  const chart = new MultiChart(c.querySelector('.ana-chart'));
  chart.log = !!S.cmpLog;
  async function draw() {
    const pick = S.cmp.slice(0, 12);
    const series = [];
    for (let i = 0; i < pick.length; i++) {
      try {
        const m = await api('/runs/' + encodeURIComponent(pick[i]) +
          '/metrics');
        const run = S.runs.find(r => r.id === pick[i]);
        series.push({
          name: (run && run.run_name) || pick[i],
          color: PALETTE[i % PALETTE.length],
          points: m[S.cmpMetric] || [],
        });
      } catch (e) { /* skip */ }
    }
    chart.setData(series);
    legend.innerHTML = series.map(s =>
      `<span class="lg-item"><span class="lg-dot" style="background:` +
      `${s.color}"></span>${esc(s.name)}` +
      `${s.points.length ? '' : ' (no data)'}</span>`).join('')
      || '<span class="ana-note">no runs selected</span>';
  }
  draw();
}

/* ── boot ─────────────────────────────────────────────────────────────── */
async function boot() {
  document.getElementById('app').innerHTML =
    '<div class="skel" style="margin:20px;height:90vh"></div>';
  const project = await api('/project');
  if (!project || !project.name) { onboarding(); return; }
  const [runs, ideas, events, chat, gpus] = await Promise.all([
    api('/runs'), api('/ideas'), api('/events'), api('/chat'), api('/gpus')]);
  Object.assign(S, { project, runs, ideas, events, chat, gpus });
  render(); streams();
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeDrawer();
  });
}
boot();
