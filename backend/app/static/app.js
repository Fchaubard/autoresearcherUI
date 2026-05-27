/* autoresearcherUI v0.3 — single-page mission control (vanilla JS, no build) */
'use strict';

const S = {
  project: null, runs: [], ideas: [], events: [], chat: [], gpus: [],
  metrics: {}, filter: 'all', search: '', sel: null, ylog: false,
  railTab: 'summary', sessTab: null, view: 'dashboard', cmp: [], cmpMetric: '',
  sortKey: 'time', sortAsc: true,    // main table sort — default ASC by time
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
  constructor(host, color) {
    this.host = host; this.color = color || '#6366F1';
    host.classList.add('lc-host');
    this.canvas = el('canvas'); host.append(this.canvas);
    // Hover tooltip element (one per chart, positioned absolutely in host).
    this.tip = el('div', 'lc-tip'); this.tip.style.display = 'none';
    host.append(this.tip);
    new ResizeObserver(() => this.draw()).observe(host);
    this.canvas.addEventListener('mousemove', e => this._onHover(e));
    this.canvas.addEventListener('mouseleave', () => this._hideTip());
  }
  setData(d) { this.data = d || []; this.draw(); }
  _format(v, digits) {
    if (v == null || isNaN(v)) return '—';
    const a = Math.abs(v);
    if (a !== 0 && (a < 1e-3 || a >= 1e5)) return (+v).toExponential(2);
    return (+v).toFixed(digits != null ? digits : (a < 1 ? 4 : 3));
  }
  _onHover(e) {
    if (!this.data || this.data.length < 2) return;
    const rect = this.canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    // find nearest data point in screen X
    const { _X, _Y, _pad, _w, _h } = this;
    if (!_X) return;
    let best = -1, bestDist = Infinity;
    this.data.forEach((p, i) => {
      const dx = Math.abs(_X(p[0]) - mx);
      if (dx < bestDist) { bestDist = dx; best = i; }
    });
    if (best < 0 || bestDist > 50) { this._hideTip(); return; }
    const [px, py] = [this.data[best][0], this.data[best][1]];
    const tx = _X(px), ty = _Y(py);
    // crosshair + dot
    this.draw();
    const c = this.canvas.getContext('2d');
    c.setTransform(devicePixelRatio || 1, 0, 0, devicePixelRatio || 1, 0, 0);
    c.strokeStyle = '#5C636B66'; c.lineWidth = 1;
    c.beginPath(); c.moveTo(tx, _pad.t); c.lineTo(tx, _h - _pad.b); c.stroke();
    c.fillStyle = this.color;
    c.beginPath(); c.arc(tx, ty, 3.5, 0, 6.283); c.fill();
    this.tip.style.display = 'block';
    this.tip.innerHTML = `<b>step ${px}</b><br>${this._format(py)}`;
    // position tip; flip side if near right edge
    const tipW = 100;
    const left = (tx + tipW + 10 > _w) ? tx - tipW - 8 : tx + 8;
    const top = Math.max(4, ty - 28);
    this.tip.style.left = left + 'px';
    this.tip.style.top = top + 'px';
  }
  _hideTip() {
    if (this.tip) this.tip.style.display = 'none';
    this.draw();
  }
  draw() {
    const d = this.data || [], w = this.host.clientWidth || 300,
      h = this.host.clientHeight || 150, dpr = devicePixelRatio || 1;
    this.canvas.width = w * dpr; this.canvas.height = h * dpr;
    this.canvas.style.width = w + 'px'; this.canvas.style.height = h + 'px';
    const c = this.canvas.getContext('2d');
    c.setTransform(dpr, 0, 0, dpr, 0, 0); c.clearRect(0, 0, w, h);
    if (d.length < 2) { c.fillStyle = '#5C636B'; c.font = '11px sans-serif';
      c.textAlign = 'center'; c.fillText('no series', w / 2, h / 2); return; }
    const pad = { l: 44, r: 10, t: 10, b: 28 };
    const xs = d.map(p => p[0]), ys = d.map(p => p[1]);
    let xlo = Math.min(...xs), xhi = Math.max(...xs);
    let ylo = Math.min(...ys), yhi = Math.max(...ys);
    const yp = (yhi - ylo) * 0.1 || 1; ylo -= yp; yhi += yp;
    if (xhi === xlo) xhi++;
    const X = v => pad.l + (v - xlo) / (xhi - xlo) * (w - pad.l - pad.r);
    const Y = v => pad.t + (1 - (v - ylo) / (yhi - ylo)) * (h - pad.t - pad.b);
    // expose for hover
    this._X = X; this._Y = Y; this._pad = pad; this._w = w; this._h = h;
    c.font = '9px ' + MONO; c.fillStyle = '#5C636B';
    c.textBaseline = 'middle';
    // y-axis gridlines + labels
    c.textAlign = 'right';
    for (let i = 0; i <= 3; i++) {
      const yy = pad.t + i / 3 * (h - pad.t - pad.b);
      c.strokeStyle = '#1b1f25'; c.beginPath();
      c.moveTo(pad.l, yy); c.lineTo(w - pad.r, yy); c.stroke();
      c.fillText(this._format(yhi - i / 3 * (yhi - ylo), 2), pad.l - 6, yy);
    }
    // x-axis ticks: 5 ticks evenly across the visible x range
    c.textAlign = 'center'; c.textBaseline = 'top';
    const nTicks = 5;
    for (let i = 0; i <= nTicks; i++) {
      const xv = xlo + (i / nTicks) * (xhi - xlo);
      const xx = X(xv);
      c.strokeStyle = '#1b1f2588'; c.beginPath();
      c.moveTo(xx, h - pad.b); c.lineTo(xx, h - pad.b + 4); c.stroke();
      const lbl = (xhi - xlo > 10) ? Math.round(xv).toString()
        : (+xv).toFixed(2);
      c.fillText(lbl, xx, h - pad.b + 7);
    }
    // axis line at the bottom
    c.strokeStyle = '#2a2f37'; c.beginPath();
    c.moveTo(pad.l, h - pad.b); c.lineTo(w - pad.r, h - pad.b); c.stroke();
    // series
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
  ['lessons', 'Lessons', '📖'], ['latex', 'Latex', '∑'],
  ['system', 'System stats', '▤'],
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
  const set = el('button', 'menu-item',
    `<span class="menu-ic">⚙</span><span>Settings</span>`);
  set.onclick = () => { closeMenu(); openSettings(); };
  const arc = el('button', 'menu-item',
    `<span class="menu-ic">⤓</span><span>Archive</span>`);
  arc.onclick = () => { closeMenu(); openArchive(); };
  const rst = el('button', 'menu-item danger',
    `<span class="menu-ic">⟲</span><span>Reset</span>`);
  rst.onclick = () => { closeMenu(); resetAll(); };
  m.append(set, arc, rst);
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
  [['summary', 'Summary'], ['live', 'Research agent'],
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
    exp: '#' + r.exp, _exp_num: r.exp, id: r.id, kind: 'run',
    name: r.run_name, desc: ideaDesc(r.idea_id), status: r.status,
    metric: r.headline_metric, delta: r.baseline_delta,
    gpu: r.gpu_index, started: r.started_at, ended: r.ended_at,
  }));
  S.ideas.filter(i => i.status === 'not_implemented').forEach(i => {
    rows.push({ exp: '—', _exp_num: Number.POSITIVE_INFINITY,
      id: i.id, kind: 'idea', name: i.idea_id,
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
  // Sort: default key 'time' (started_at), ascending. Click any column header
  // to toggle the sort. Queued rows (no time) sink to the end either way.
  const sortBy = S.sortKey || 'time';
  const asc = S.sortAsc !== false;
  const getVal = r => {
    switch (sortBy) {
      case 'exp': return r._exp_num;
      case 'status': return r.status || '';
      case 'name': return (r.name || '').toLowerCase();
      case 'metric': return r.metric == null ? null : +r.metric;
      case 'gpu': return r.gpu == null ? -1 : r.gpu;
      case 'duration':
        if (!r.started) return null;
        return ((r.ended ? +new Date(r.ended) : Date.now())
                - +new Date(r.started));
      case 'time':
      default:
        return r.started || r.ended || '';
    }
  };
  rows = rows.slice().sort((a, b) => {
    const va = getVal(a), vb = getVal(b);
    const ea = va == null || va === '' || va === -1;
    const eb = vb == null || vb === '' || vb === -1;
    if (ea && eb) return 0;
    if (ea) return 1;           // empties sink
    if (eb) return -1;
    if (typeof va === 'number' && typeof vb === 'number')
      return asc ? va - vb : vb - va;
    return asc ? String(va).localeCompare(String(vb))
                : String(vb).localeCompare(String(va));
  });
  const deltas = rows.map(r => r.delta).filter(d => d != null && isFinite(d));
  const mx = Math.max(0.001, ...deltas.map(Math.abs));
  const ts = document.getElementById('tscroll');
  const tbl = el('table', 'runs');
  const COLS = [
    ['exp',      '#'],
    ['status',   'Status'],
    ['name',     'Idea'],
    ['metric',   'Result vs baseline'],
    ['gpu',      'GPU'],
    ['duration', 'Duration'],
    ['time',     'Started'],
  ];
  const hrow = COLS.map(([k, label]) => {
    const on = (S.sortKey || 'time') === k;
    const arrow = on ? (S.sortAsc !== false ? ' ▲' : ' ▼') : '';
    return `<th data-sort="${k}" class="th-sort${on ? ' on' : ''}">`
      + esc(label) + `<span class="th-arrow">${arrow}</span></th>`;
  }).join('');
  tbl.innerHTML = `<thead><tr>${hrow}</tr></thead>`;
  // wire header clicks
  setTimeout(() => {
    tbl.querySelectorAll('thead th[data-sort]').forEach(th => {
      th.onclick = () => {
        const k = th.dataset.sort;
        if (S.sortKey === k) S.sortAsc = !S.sortAsc;
        else { S.sortKey = k; S.sortAsc = true; }
        paintTable();
      };
    });
  }, 0);
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
    const tcell = r.started ? ago(r.started)
                : r.ended ? ago(r.ended) : '—';
    tr.innerHTML =
      `<td class="mono" style="color:#5C636B">${r.exp}</td>` +
      `<td><span class="chip s-${r.status}"><span class="dot"></span>` +
      `${r.status}</span></td>` +
      `<td><div class="idea-name">${esc(r.name)}</div>` +
      `<div class="idea-desc">${esc(r.desc)}</div></td>` +
      `<td>${mc}</td>` +
      `<td class="mono">${r.gpu >= 0 ? r.gpu : '—'}</td>` +
      `<td class="mono">${r.started ? dur(r.started, r.ended) : '—'}</td>` +
      `<td class="mono" title="${esc(r.started || r.ended || '')}">` +
        `${esc(tcell)}</td>`;
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

function _runTime(r) {
  // Best-effort 'when did this experiment finish/start' for sort + display.
  return r.ended_at || r.started_at || r.created_at || '';
}

function _reviewerLabel(rev) {
  if (!rev) return { lbl: '?', color: '#9CA3AF' };
  for (const k of Object.keys(REVIEWER_META)) {
    if (String(rev).toLowerCase().includes(k)) return REVIEWER_META[k];
  }
  return { lbl: rev, color: '#A78BFA' };
}

function ideaCard(run) {
  const sm = STATUS_META[run.status] || STATUS_META.queued;
  const cfg = run.config || {};
  const what = (cfg.what || cfg.description || cfg.hypothesis || '').toString().trim();
  const why = (cfg.why || '').toString().trim();
  const review = (cfg.review && typeof cfg.review === 'object') ? cfg.review : null;
  const reviews = (cfg.reviews && typeof cfg.reviews === 'object') ? cfg.reviews : null;
  const metric = run.headline_metric;
  const delta = run.baseline_delta;
  const isBaseline = run.is_baseline;
  const t = _runTime(run);
  const card = el('div', 'icard');
  card.dataset.runId = run.id;
  // header row: icon + title + chips
  const hd = el('div', 'icard-hd');
  hd.innerHTML =
    `<div class="icard-ic ${sm.cls}">${sm.ic}</div>` +
    `<div class="icard-ti">` +
      `<div class="icard-name">${esc(run.run_name || run.id)}</div>` +
      `<div class="icard-sub" title="${esc(t)}">` +
        `${esc(t ? ago(t) : '—')}` +
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
  // council learning(s) — show every round-0 reviewer + tiebreaker if any
  let panels = [];
  if (reviews && Array.isArray(reviews.rounds) && reviews.rounds.length) {
    const r0 = reviews.rounds[0].positions || {};
    Object.entries(r0).forEach(([rev, pos]) => {
      if (pos && (pos.learning || '').trim()) {
        panels.push({ reviewer: rev, learning: pos.learning,
                       new_ideas: pos.new_ideas, role: 'reviewer' });
      }
    });
    if (reviews.agreement === false && reviews.reviewer
        && /tiebreaker|claude/i.test(reviews.reviewer)) {
      panels.push({ reviewer: 'claude (tiebreaker)',
                     learning: reviews.learning, new_ideas: reviews.new_ideas,
                     role: 'tiebreaker' });
    }
  } else if (review && (review.learning || '').trim()) {
    panels.push({ reviewer: review.reviewer, learning: review.learning,
                   new_ideas: review.new_ideas, role: 'reviewer' });
  }
  if (panels.length) {
    panels.forEach(p => {
      const rm = _reviewerLabel(p.reviewer);
      const rv = el('div', 'icard-review' + (p.role === 'tiebreaker'
        ? ' tiebreak' : ''));
      rv.style.borderLeftColor = rm.color;
      rv.innerHTML =
        `<div class="icard-rv-tag" style="color:${rm.color}">` +
          `★ ${p.role === 'tiebreaker' ? 'Tiebreaker' : 'Council'} · ` +
          `${esc(rm.lbl)}</div>` +
        `<div class="icard-rv-bd">${esc((p.learning || '').trim())}</div>` +
        (Array.isArray(p.new_ideas) && p.new_ideas.length
          ? `<div class="icard-rv-new">Proposed: ` +
            p.new_ideas.slice(0, 3).map(ni =>
              `<code>${esc(ni.idea_id || '?')}</code>`).join(' ') + `</div>`
          : '');
      card.append(rv);
    });
  } else if (run.status !== 'running') {
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

  // Scrolling container that holds the completed-ideas cards + activity feed
  // below. Sort is ascending (oldest top, newest at the bottom) — chat-feed
  // style — so 'now' is always at the bottom. On first paint we scroll all
  // the way down. A floating 'jump to latest' button appears when the user
  // is scrolled up.
  const scroll = el('div', 'rail-scroll'); scroll.id = 'rail-scroll';
  c.append(scroll);

  // Visible conversation panel — last 12 messages between user, agent, PI.
  scroll.append(el('div', 'rail-h', 'Conversation with agent'));
  const convo = el('div', 'convo'); convo.id = 'convo';
  scroll.append(convo);

  scroll.append(el('div', 'rail-h', 'Completed experiments'));
  const wrap = el('div', 'icards'); wrap.id = 'icards';
  scroll.append(wrap);

  const compact = el('details', 'feed-compact');
  compact.innerHTML = '<summary>Activity feed</summary>';
  const feed = el('div', 'feed'); feed.id = 'feed';
  compact.append(feed);
  scroll.append(compact);

  // floating 'jump to latest' button (only when user has scrolled up)
  const jump = el('button', 'jump-latest', '↓ Latest');
  jump.style.display = 'none';
  jump.onclick = () => {
    scroll.scrollTo({ top: scroll.scrollHeight, behavior: 'smooth' });
  };
  c.append(jump);

  paintConvo();
  paintCompletedCards();

  // activity feed (kept for chronological events + chat)
  const items = [
    ...S.events.map(e => ({ t: e.created_at, kind: 'event', d: e })),
    ...S.chat.map(m => ({ t: m.created_at, kind: 'chat', d: m })),
  ].sort((a, b) => (a.t || '').localeCompare(b.t || ''));
  items.forEach(it => feed.append(feedItemEl(it)));
  S._feedLoading = false;

  // Scroll-to-bottom on first paint; show jump button when away from bottom.
  const stickToBottom = () => {
    scroll.scrollTop = scroll.scrollHeight;
  };
  requestAnimationFrame(stickToBottom);
  setTimeout(stickToBottom, 50);

  scroll.onscroll = () => {
    const fromBottom = scroll.scrollHeight - scroll.scrollTop
      - scroll.clientHeight;
    jump.style.display = fromBottom > 280 ? 'block' : 'none';
    if (scroll.scrollTop < 60) loadOlderEvents();
  };
}

function paintCompletedCards() {
  const wrap = document.getElementById('icards');
  if (!wrap) return;
  const scroll = document.getElementById('rail-scroll');
  // Stay-pinned-to-bottom behaviour: if the user is at the bottom, we keep
  // them there after a repaint. Otherwise we preserve their scroll position.
  let stickToBottom = true;
  let priorScrollFromBottom = 0;
  if (scroll) {
    const fromBottom = scroll.scrollHeight - scroll.scrollTop
      - scroll.clientHeight;
    stickToBottom = fromBottom < 90;
    priorScrollFromBottom = fromBottom;
  }
  // Ascending sort (oldest top, newest bottom) — chat-feed style.
  const ranked = (S.runs || []).slice().sort((a, b) =>
    (_runTime(a) || '').localeCompare(_runTime(b) || ''));
  wrap.innerHTML = '';
  if (!ranked.length) {
    wrap.append(el('div', 'icards-empty',
      'No experiments yet — the agent will start running ideas from the queue soon.'));
    return;
  }
  ranked.forEach(r => wrap.append(ideaCard(r)));
  if (scroll) {
    if (stickToBottom) {
      scroll.scrollTop = scroll.scrollHeight;
    } else {
      // restore approximate scroll position so the user doesn't get yanked
      scroll.scrollTop = scroll.scrollHeight - scroll.clientHeight
        - priorScrollFromBottom;
    }
  }
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
  const baseline = (S.project && S.project.baseline_metric != null)
    ? S.project.baseline_metric : null;
  // Compute a vs-baseline string even when baseline_delta isn't populated.
  let vsBaseline = '—';
  if (run.headline_metric != null && baseline != null) {
    const dabs = run.headline_metric - baseline;
    const mins = minimize();
    const better = mins ? dabs < 0 : dabs > 0;
    vsBaseline =
      `<span class="${better ? 'up' : 'down'}">` +
      (dabs >= 0 ? '+' : '−') + fmt(Math.abs(dabs)) +
      ` (${better ? 'better' : 'worse'})</span>`;
  } else if (delta != null) {
    const better = (S.project?.metric_direction === 'minimize')
      ? (delta > 0) : (delta < 0);
    vsBaseline = `<span class="${better ? 'up' : 'down'}">` +
      (delta >= 0 ? '−' : '+') + fmt(Math.abs(delta)) + '</span>';
  }
  // Build result rows, dropping any row whose value is unknown/missing.
  const rows = [];
  rows.push(['final ' + (S.project.validation_metric),
             run.status === 'crashed' ? 'diverged'
                                       : fmt(run.headline_metric)]);
  rows.push(['vs baseline', vsBaseline]);
  if (run.gpu_index != null && run.gpu_index >= 0) {
    rows.push(['GPU', '#' + run.gpu_index]);
  }
  rows.push(['duration', dur(run.started_at, run.ended_at)]);
  if (run.tmux_session && run.status === 'running') {
    rows.push(['tmux session', run.tmux_session]);
  }
  if (run.git_commit) rows.push(['commit', run.git_commit.slice(0, 10)]);
  if (run.status) rows.push(['status', run.status]);
  rows.forEach(([k, v]) => {
    if (v == null || v === '' || v === '—') return;
    dl.innerHTML += `<dt>${k}</dt><dd>${v}</dd>`;
  });
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
  // "View all plots" — lazy-loaded multi-metric plot grid for this run.
  // Defaults to a configurable mandatory list; below that a searchable
  // list of remaining keys with "tap to plot" chips (per the v2 spec).
  bd.append(el('div', 'dr-h2', 'View all plots'));
  const vapBtn = el('button', 'btn', 'Show plots');
  const vapWrap = el('div', 'vap-wrap'); vapWrap.style.display = 'none';
  bd.append(vapBtn, vapWrap);
  vapBtn.onclick = async () => {
    vapBtn.style.display = 'none';
    vapWrap.style.display = '';
    vapWrap.innerHTML = '<div class="skel" style="height:200px"></div>';
    // Fetch this run's logged keys
    let runKeys = [];
    try {
      const k = await api('/runs/' + encodeURIComponent(runId) + '/metric_keys');
      runKeys = (k && k.keys) || [];
    } catch (e) { runKeys = []; }
    const defaults = ['val_loss', 'val_acc', 'lr', 'train_loss',
      'train_acc', 'time_per_step', 'samples_per_sec'];
    const extras = runKeys.filter(k => !defaults.includes(k));
    vapWrap.innerHTML = '';
    const grid = el('div', 'vap-grid'); vapWrap.append(grid);
    // Default panels — slot for each (placeholder if not logged)
    defaults.forEach(k => grid.append(_vapPanel(runId, k, runKeys.includes(k))));
    if (extras.length) {
      vapWrap.append(el('div', 'dr-h2', 'Other metrics'));
      const search = el('input', 'vap-search');
      search.placeholder = 'filter (e.g. token, mem)…';
      vapWrap.append(search);
      const chips = el('div', 'vap-chips'); vapWrap.append(chips);
      const extraGrid = el('div', 'vap-grid'); vapWrap.append(extraGrid);
      const renderChips = (filter) => {
        chips.innerHTML = '';
        extras.filter(k => !filter
                          || k.toLowerCase().includes(filter.toLowerCase()))
              .forEach(k => {
          const b = el('button', 'evchip', '+ ' + k);
          b.onclick = () => {
            extraGrid.append(_vapPanel(runId, k, true));
            b.remove();
          };
          chips.append(b);
        });
      };
      renderChips('');
      search.oninput = () => renderChips(search.value);
    }
  };
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
  // Show the user's message in the conversation panel IMMEDIATELY — don't
  // wait for the SSE round-trip — so the user gets confirmation it went.
  const userMsg = { id: 'tmp-' + Date.now(), role: 'researcher',
    content: text, created_at: new Date().toISOString() };
  S.chat.push(userMsg);
  paintConvo();
  const r = await post('/agent/send', { text });
  if (r && r.ok === false) {
    S.chat.push({ role: 'agent', created_at: new Date().toISOString(),
      content: '⚠ could not deliver — ' + (r.error || 'no agent session') });
    paintConvo();
    return;
  }
  // Watch the research agent's tmux output for ~30s after the send and
  // surface the next non-trivial line(s) as a synthesised agent bubble.
  watchAgentForReply();
}

// Capture an "agent reply" by diffing the tmux output before/after the send.
let _agentReplyTimer = null;
async function watchAgentForReply() {
  if (_agentReplyTimer) return;          // already watching
  const before = (await api('/agent/terminal').catch(() => ({}))).text || '';
  let elapsed = 0;
  _agentReplyTimer = setInterval(async () => {
    elapsed += 2200;
    let d;
    try { d = await api('/agent/terminal'); } catch (e) { return; }
    const after = (d && d.text) || '';
    if (after.length > before.length) {
      const fresh = after.slice(before.length).trim();
      // grab the last few content lines (skip blank + box-drawing decoration)
      const lines = fresh.split('\n').map(l => l.replace(/\s+$/, ''))
        .filter(l => l && !/^[─━│┃┌┐└┘├┤┬┴┼ ▶▸>›·•]+$/.test(l));
      if (lines.length) {
        const reply = lines.slice(-6).join('\n').slice(0, 800);
        S.chat.push({ role: 'agent', created_at: new Date().toISOString(),
          content: reply });
        paintConvo();
        clearInterval(_agentReplyTimer); _agentReplyTimer = null;
        return;
      }
    }
    if (elapsed > 30000) {
      clearInterval(_agentReplyTimer); _agentReplyTimer = null;
    }
  }, 2200);
}

function paintConvo() {
  const c = document.getElementById('convo');
  if (!c) return;
  c.innerHTML = '';
  const items = (S.chat || []).slice(-12);
  if (!items.length) {
    c.append(el('div', 'convo-empty',
      'Send the agent a message in the box below — it answers from its ' +
      'research-agent terminal.'));
    return;
  }
  items.forEach(m => {
    const isPI = /\[PI\b/.test(m.content || '');
    const cls = isPI ? 'bub pi'
      : (m.role === 'researcher' ? 'bub researcher' : 'bub agent');
    const b = el('div', cls);
    b.textContent = m.content || '';
    c.append(b);
  });
  // auto-scroll to the bottom of the convo panel itself
  c.scrollTop = c.scrollHeight;
}

/* ── SSE ──────────────────────────────────────────────────────────────── */
function streams() {
  const m = new EventSource('/api/stream/metrics');
  m.addEventListener('metrics_changed', e => {
    try {
      const { run_id } = JSON.parse(e.data);
      // Invalidate Analysis tab bucket cache for this run; if we're on
      // Analysis right now and this run is selected, repaint.
      if (typeof invalidateBucketsForRun === 'function') {
        invalidateBucketsForRun(run_id);
      }
      if (S.view === 'analysis' && AnaState && AnaState.selected
          && AnaState.selected.has(run_id)) {
        if (typeof refreshAllPanels === 'function') refreshAllPanels();
      }
    } catch (e) { /* ignore */ }
  });
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
    // Skip if we already showed this (e.g., user message echoed back via SSE
    // after we already added it locally).
    if (msg.id && S.chat.some(x => x.id === msg.id)) return;
    // Replace any temp message with the canonical server copy if content matches.
    const tmpIdx = S.chat.findIndex(x =>
      x.id && x.id.startsWith('tmp-') && x.content === msg.content);
    if (tmpIdx >= 0) S.chat[tmpIdx] = msg;
    else S.chat.push(msg);
    paintConvo();
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
  ['research_agent_model', 'Research agent model (Claude variant)', 'select',
    'claude-opus-4-6|claude-sonnet-4-6|claude-haiku-4-5|claude-opus-4-1|'
    + 'claude-sonnet-4-5'],
  ['sec', 'Review council — runs after every experiment'],
  ['council_enable_gemini', 'Enable Gemini in council', 'check', ''],
  ['council_gemini_model', 'Council — Gemini model', 'select',
    'gemini-2.5-pro|gemini-2.5-flash|gemini-2.0-flash'],
  ['council_enable_openai', 'Enable OpenAI in council', 'check', ''],
  ['council_openai_model', 'Council — OpenAI model', 'select',
    'gpt-5|gpt-5-mini|gpt-5-nano|o3|o3-mini|o4-mini|o3-pro'],
  ['council_openai_effort', 'Council — OpenAI reasoning effort', 'select',
    'high|medium|low|minimal'],
  ['council_enable_claude_tiebreaker',
    'Enable Claude tiebreaker (only used when reviewers disagree)',
    'check', ''],
  ['council_claude_model', 'Council — Claude tiebreaker model', 'select',
    'claude-opus-4-6|claude-sonnet-4-6|claude-haiku-4-5'],
  ['run_debate', 'Run debate between reviewers (per-run reviews only)',
    'check', ''],
  ['debate_max_rounds', 'Debate max rounds (before tiebreaker)', 'select',
    '3|2|1|4|5'],
  ['council_per_run_enabled',
    'Per-run review (NOISY — default off; the strategic review handles '
    + 'most of the work)', 'check', ''],
  ['strategic_review_enabled',
    'Strategic batch review every N runs (recommended)', 'check', ''],
  ['strategic_review_batch_n',
    'Strategic batch size N (0 = auto = GPU count)', 'select',
    '0|1|2|4|8|16'],
  ['sec', 'This node — SSH access (auto-detected, override if wrong)'],
  ['node_ssh_user', 'SSH user', 'text', 'root'],
  ['node_ssh_host', 'SSH host / public IP (blank = auto-detect)', 'text', ''],
  ['node_ssh_port', 'SSH port (blank = auto-detect)', 'text', '22'],
  ['sec', 'Extra GPU nodes (optional)'],
  ['extra_gpu_nodes',
    'One SSH target per line — e.g. `root@10.0.0.5:22` or `user@host`. '
    + 'The autoresearcher can ssh into these to launch experiments on '
    + 'their GPUs (paste this node\'s pub key into their authorized_keys '
    + 'first — see authorized_keys panel).',
    'area', 'root@gpu-node-2:22\nuser@10.0.0.7'],
  ['sec', 'PI agent — periodic oversight'],
  ['pi_agent_enabled', 'Run the PI agent on a schedule', 'check', ''],
  ['pi_agent_model', 'PI agent model', 'select',
    'gemini-2.5-pro|gemini-2.5-flash|gpt-5|gpt-5-mini|claude-opus-4-6|'
    + 'claude-sonnet-4-6'],
  ['pi_cadence_minutes', 'PI cadence (minutes between checks)', 'select',
    '60|15|30|120|240'],
  ['sec', 'Email alerts — optional (leave the app password blank for none)'],
  ['cadence', 'Cadence', 'select', 'off|immediate|1h|4h|12h|24h'],
  ['email_recipients', 'Recipients (comma-separated)', 'text',
    'you@example.com, teammate@example.com'],
  ['gmail_app_pw', 'Gmail app password (for the sender email above)',
    'password', ''],
  ['sec', 'Access'],
  ['passcode', 'Dashboard passcode (blank = open)', 'text', ''],
];

/* Shared form-builder used by both onboarding() and openSettings(). Returns
   {form, inp} where inp is a map of field-key -> input element. Pre-fills
   from /api/onboarding/defaults and (optionally) overrides from `initial`. */
function buildSettingsForm({ initial = {}, hideFields = [] } = {}) {
  const hide = new Set(hideFields);
  const form = el('div', 'onb-card');
  const inp = {};
  OB_FIELDS.forEach(f => {
    if (f[0] === 'sec') { form.append(el('div', 'onb-sec', f[1])); return; }
    const [k, label, type, extra] = f;
    if (hide.has(k)) return;
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
      // Mitigate Chrome's "deceptive site / you entered a password"
      // heuristic on the *.trycloudflare.com tunnel. autocomplete +
      // data-form-type tell Chrome these aren't login passwords.
      if (type === 'password' || type === 'email') {
        x.setAttribute('autocomplete', 'new-password');
        x.setAttribute('data-form-type', 'other');
        x.setAttribute('data-1p-ignore', 'true');     // 1Password
        x.setAttribute('data-lpignore', 'true');      // LastPass
      } else if (type === 'text') {
        x.setAttribute('autocomplete', 'off');
      }
      inp[k] = x; row.append(x);
    }
    form.append(row);
  });
  // Apply initial overrides immediately so checkbox defaults are correct.
  Object.entries(initial || {}).forEach(([k, v]) => {
    if (inp[k] == null) return;
    if (inp[k].type === 'checkbox') {
      inp[k].checked = !!v && v !== 'false' && v !== 'False';
    } else if (v !== undefined && v !== null) {
      inp[k].value = v;
    }
  });
  return { form, inp };
}

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

  const { form, inp } = buildSettingsForm();
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

/* ── settings (post-onboarding edit of EVERY onboarding field) ────────── */
async function openSettings() {
  const sc = el('div', 'mscrim');
  const m = el('div', 'modal modal-settings');
  m.innerHTML = '<div class="skel" style="height:300px"></div>';
  sc.append(m); document.body.append(sc);
  sc.onclick = e => { if (e.target === sc) sc.remove(); };
  let cur = {}, defs = {};
  try { [cur, defs] = await Promise.all(
    [api('/settings'), api('/onboarding/defaults')]); }
  catch (e) { m.innerHTML = '<p>Could not load settings.</p>'; return; }
  const initial = { ...defs, ...cur };
  const { form, inp } = buildSettingsForm({ initial });
  m.innerHTML = '';
  const hd = el('div', 'modal-hd');
  hd.append(el('h2', '', 'Settings'));
  const xb = el('button', 'iconbtn', '✕');
  xb.onclick = () => sc.remove();
  hd.append(xb);
  m.append(hd);
  m.append(el('p', 'modal-sub',
    'Every field from onboarding is editable here. Tokens and passwords show ' +
    'as •••• — leave them blank to keep the saved value, or paste a new value ' +
    'to replace it. Most changes take effect on the next council / PI cycle; ' +
    'changes to the Research-agent model only apply on the next research run.'));
  m.append(form);
  const actions = el('div', 'modal-actions');
  const status = el('div', 'set-status');
  const save = el('button', 'btn pri', 'Save settings');
  save.onclick = async () => {
    save.disabled = true; save.textContent = 'Saving…';
    const upd = {};
    Object.entries(inp).forEach(([k, x]) => {
      upd[k] = x.type === 'checkbox' ? x.checked : x.value;
    });
    try {
      const r = await fetch('/api/settings', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(upd),
      }).then(r => r.json());
      if (r && r.status === 'ok') {
        status.textContent = 'Saved ✓';
        save.textContent = 'Saved ✓';
        setTimeout(() => sc.remove(), 800);
      } else {
        status.textContent = 'Save failed: ' + ((r && r.detail) || '?');
        save.disabled = false; save.textContent = 'Save settings';
      }
    } catch (e) {
      status.textContent = 'Save failed: ' + e;
      save.disabled = false; save.textContent = 'Save settings';
    }
  };
  actions.append(save, status);
  m.append(actions);
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
  else if (S.view === 'lessons') renderLessons(c);
  else renderLatex(c);
  return c;
}

/* ── Lessons (the council's running notebook, parsed from lessons.md) ─── */
async function renderLessons(c) {
  c.innerHTML = '<div class="lessons-wrap"><div class="empty2">loading…</div></div>';
  let d;
  try { d = await api('/lessons'); }
  catch (e) { c.innerHTML = '<p>Could not load lessons.</p>'; return; }
  const items = d.lessons || [];
  const wrap = c.querySelector('.lessons-wrap');
  if (!items.length) {
    wrap.innerHTML =
      '<h2 style="margin:0 0 8px">Lessons learned</h2>' +
      `<p style="color:var(--muted)">No lessons yet. As the council ` +
      `reviews completed experiments, its findings are appended to ` +
      `<code>${esc(d.path || 'lessons.md')}</code> and shown here. Each ` +
      `entry also gets fed back into every future review, so insights ` +
      `compound over time.</p>`;
    return;
  }
  // newest first
  const rows = items.slice().reverse().map(L => {
    const evidence = (L.evidence || []).map(r =>
      `<button class="evchip" data-run="${esc(r)}">${esc(r)}</button>`
    ).join('');
    return `<div class="lesson">
      <div class="lesson-hd">
        <span class="lesson-ts mono">${esc(L.ts)}</span>
        <span class="lesson-rev">${esc(L.reviewer)}</span>
        <button class="evchip primary" data-run="${esc(L.supporting_run)}">
          ${esc(L.supporting_run)}</button>
      </div>
      <div class="lesson-bd">${esc(L.text)}</div>
      ${evidence ? `<div class="lesson-ev">also referenced: ${evidence}</div>` : ''}
    </div>`;
  }).join('');
  wrap.innerHTML =
    `<h2 style="margin:0 0 4px">Lessons learned <span style="color:var(--muted);font-size:12px;font-weight:500">(${items.length})</span></h2>` +
    `<p style="color:var(--muted);margin:0 0 18px;font-size:12px">` +
    `Auto-written by the council after each review. File: ` +
    `<code class="mono">${esc(d.path || '')}</code>. Click a run chip to ` +
    `open that experiment in the drawer.</p>` +
    `<div class="lessons-list">${rows}</div>`;
  // wire run chips → open drawer
  wrap.querySelectorAll('.evchip').forEach(b => {
    b.onclick = () => {
      const target = b.dataset.run;
      // Find the run by id OR run_name match
      const run = (S.runs || []).find(r =>
        r.id === target || r.run_name === target);
      if (run) {
        // we need to navigate back to dashboard for the drawer to render over it
        S.view = 'dashboard'; render();
        setTimeout(() => openDrawer(run.id), 80);
      }
    };
  });
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
    let d, pk;
    try { [d, pk] = await Promise.all(
      [api('/authkeys'), api('/authkeys/pubkey')]); } catch (e) { return; }
    const keys = d.keys || [];
    const rows = keys.map(k =>
      `<div class="ak-row"><div class="ak-info">` +
      `<div class="ak-fp mono">${esc(k.fingerprint || k.type || 'key')}` +
      `</div><div class="ak-cmt">${esc(k.comment || k.type || '')}</div>` +
      `</div><button class="ak-del" data-fp="${esc(k.fingerprint || '')}">` +
      `Delete</button></div>`).join('')
      || '<div class="empty2">No authorized keys.</div>';
    // This node's own SSH pub key block — for copying to OTHER GPU servers
    // so this autoresearcher can SSH into them.
    const pubBlock = pk && pk.ok
      ? '<div class="sys-sec">This node\'s SSH public key</div>' +
        '<p class="ak-help">Paste this into ~/.ssh/authorized_keys on ' +
        'another GPU server so this autoresearcher can SSH into it. ' +
        'Useful when attaching additional GPU nodes.</p>' +
        `<code class="ak-ssh" id="ak-pub">${esc(pk.pubkey)}</code>` +
        '<div class="ak-pub-row">' +
        '<button class="btn ak-copy-pub">Copy public key</button> ' +
        '<button class="btn ak-copy-oneliner">Copy install one-liner</button>' +
        `<span class="ak-cmt mono" style="margin-left:8px">${esc(pk.fingerprint || '')}</span>` +
        '</div>'
      : '<div class="sys-sec">This node\'s SSH public key</div>' +
        '<div class="empty2">' + esc((pk && pk.error) || 'not available') + '</div>';
    c.querySelector('.ak-wrap').innerHTML =
      '<div class="ak-warn">⚠ These keys control SSH access to the node. ' +
      'After adding a key, test the new login in another terminal before ' +
      'deleting any old key.</div>' +
      '<div class="sys-sec">SSH into the node</div>' +
      `<code class="ak-ssh">${esc(d.ssh || '')}</code>` +
      (d.ssh_hint
        ? `<div class="ak-help" style="color:var(--warn)">${esc(d.ssh_hint)}</div>`
        : '') +
      pubBlock +
      `<div class="sys-sec">Authorized keys (${keys.length})</div>` +
      `<div class="ak-list">${rows}</div>` +
      '<div class="sys-sec">Add a public key</div>' +
      '<textarea class="ak-add" placeholder="ssh-ed25519 AAAA… you@host">' +
      '</textarea><button class="btn ak-addbtn">Add key</button>' +
      '<span class="ak-status"></span>';
    const cpb = c.querySelector('.ak-copy-pub');
    if (cpb) cpb.onclick = () => {
      navigator.clipboard?.writeText(pk.pubkey);
      cpb.textContent = 'Copied ✓';
      setTimeout(() => cpb.textContent = 'Copy public key', 1400);
    };
    const cob = c.querySelector('.ak-copy-oneliner');
    if (cob) cob.onclick = () => {
      navigator.clipboard?.writeText(pk.install_one_liner || '');
      cob.textContent = 'Copied ✓';
      setTimeout(() => cob.textContent = 'Copy install one-liner', 1400);
    };
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
/* ── Analysis v2 — W&B-style multi-panel dashboard ─────────────────────────
   Architecture (see docs/12-analysis-v2-spec-final.md):
   - Runs table (left) with sort, regex search, status filter, baseline tag
     stored in localStorage, solo button, multi-select via checkboxes.
   - Panel grid (right) — each panel is a BucketChart that fetches
     server-bucketed data via POST /api/metrics/batch. Smoothing applied
     client-side; log-y toggleable; shared crosshair across panels with
     the same x_key via cursorBus; zoom via drag-select.
   - Baseline run is included automatically on every panel where
     include_baseline is true; rendered as a dashed grey line.
   - Panel set persists server-side at GET/PUT /api/analysis/panels. */

// Tiny pub-sub for shared crosshair, scoped by x_key group.
const cursorBus = {
  x: null, group: null, frame: 0,
  listeners: new Set(),
  set(x, group) {
    this.x = x; this.group = group;
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = 0;
      this.listeners.forEach(fn => fn(this.x, this.group));
    });
  },
  clear() { this.set(null, null); },
};

// Apply client-side EMA smoothing. Operates on Number|null arrays;
// NaN/null breaks the EMA so curves don't bridge across gaps.
function _smooth(ys, alpha) {
  if (!alpha || alpha <= 0) return ys;
  const out = new Array(ys.length);
  let s = null;
  for (let i = 0; i < ys.length; i++) {
    const v = ys[i];
    if (v == null || isNaN(v)) { out[i] = null; s = null; continue; }
    s = (s == null) ? v : (alpha * s + (1 - alpha) * v);
    out[i] = s;
  }
  return out;
}

const PALETTE_BIG = [
  '#6366F1','#34D399','#F59E0B','#F87171','#A78BFA','#22D3EE','#FBBF24',
  '#FB7185','#10B981','#60A5FA','#F97316','#C084FC','#14B8A6','#E879F9',
];
const BASELINE_COLOR = '#9BA1A8';

// Read/write the user's baseline run id (per-browser).
function getBaseline() {
  try { return localStorage.getItem('arui:baseline') || null; }
  catch (e) { return null; }
}
function setBaseline(rid) {
  try {
    if (rid) localStorage.setItem('arui:baseline', rid);
    else localStorage.removeItem('arui:baseline');
  } catch (e) { /* ignore */ }
}

// === BucketChart =========================================================
// Renders one panel of multi-run, multi-series bucketed data.
class BucketChart {
  constructor(host) {
    this.host = host; this.host.classList.add('lc-host');
    this.canvas = el('canvas', 'bc-canvas');
    this.overlay = el('canvas', 'bc-overlay');
    this.tip = el('div', 'lc-tip'); this.tip.style.display = 'none';
    host.append(this.canvas, this.overlay, this.tip);
    this.series = [];        // [{run_id, name, color, dashed, x, y}]
    this.smoothing = 0;
    this.log = false;
    this.xKey = 'step';
    this.zoom = null;        // {x_min, x_max} | null
    this._dragStart = null;
    this._lastDraw = null;
    new ResizeObserver(() => this.draw()).observe(host);
    this.overlay.addEventListener('mousemove', e => this._onMove(e));
    this.overlay.addEventListener('mouseleave', () => {
      cursorBus.clear(); this._dragStart = null; this._drawOverlay(null);
    });
    this.overlay.addEventListener('mousedown', e => {
      const r = this.overlay.getBoundingClientRect();
      this._dragStart = e.clientX - r.left;
    });
    this.overlay.addEventListener('mouseup', e => {
      if (this._dragStart == null) return;
      const r = this.overlay.getBoundingClientRect();
      const xEnd = e.clientX - r.left;
      if (Math.abs(xEnd - this._dragStart) > 10 && this._lastDraw) {
        const { xlo, xhi, pad, w } = this._lastDraw;
        const a = this._dragStart, b = xEnd;
        const sx = Math.min(a, b), ex = Math.max(a, b);
        const xv1 = xlo + (sx - pad.l) / (w - pad.l - pad.r) * (xhi - xlo);
        const xv2 = xlo + (ex - pad.l) / (w - pad.l - pad.r) * (xhi - xlo);
        if (this.onZoom) this.onZoom(xv1, xv2);
      }
      this._dragStart = null;
    });
    this.overlay.addEventListener('dblclick', () => {
      if (this.onZoom) this.onZoom(null, null);
    });
    // subscribe to the cursor bus
    this._busFn = (x, group) => this._drawOverlay(group === this.xKey ? x : null);
    cursorBus.listeners.add(this._busFn);
  }
  destroy() { cursorBus.listeners.delete(this._busFn); }
  setData(series) { this.series = series || []; this.draw(); }
  setSmoothing(a) { this.smoothing = a; this.draw(); }
  setLog(v) { this.log = !!v; this.draw(); }
  setXKey(k) { this.xKey = k; }
  // Compute the smoothed y for one series (with NaN-aware EMA).
  _ySmoothed(s) { return _smooth(s.y, this.smoothing); }
  draw() {
    const w = this.host.clientWidth || 360, h = this.host.clientHeight || 220;
    const dpr = devicePixelRatio || 1;
    for (const cv of [this.canvas, this.overlay]) {
      cv.width = w * dpr; cv.height = h * dpr;
      cv.style.width = w + 'px'; cv.style.height = h + 'px';
    }
    const c = this.canvas.getContext('2d');
    c.setTransform(dpr, 0, 0, dpr, 0, 0); c.clearRect(0, 0, w, h);
    const ser = (this.series || []).filter(s => s.y && s.y.some(v => v != null));
    if (!ser.length) {
      c.fillStyle = '#5C636B'; c.font = '11px sans-serif';
      c.textAlign = 'center';
      c.fillText('no data', w / 2, h / 2);
      return;
    }
    const pad = { l: 50, r: 12, t: 8, b: 26 };
    // Determine bounds. Smoothed y is what we plot.
    let xlo = Infinity, xhi = -Infinity, ylo = Infinity, yhi = -Infinity;
    const smoothed = ser.map(s => {
      const ys = this._ySmoothed(s);
      for (let i = 0; i < s.x.length; i++) {
        const xi = s.x[i]; const yi = ys[i];
        if (xi != null) {
          if (xi < xlo) xlo = xi;
          if (xi > xhi) xhi = xi;
        }
        if (yi != null && !isNaN(yi)) {
          if (yi < ylo) ylo = yi;
          if (yi > yhi) yhi = yi;
        }
      }
      return ys;
    });
    if (!isFinite(xlo) || !isFinite(yhi)) {
      c.fillStyle = '#5C636B'; c.font = '11px sans-serif';
      c.textAlign = 'center';
      c.fillText('no data', w / 2, h / 2); return;
    }
    const log = this.log && ylo > 0;
    const tf = v => log ? Math.log10(v) : v;
    let Ylo = tf(ylo), Yhi = tf(yhi);
    const yp = (Yhi - Ylo) * 0.08 || Math.abs(Yhi) * 0.1 || 1;
    Ylo -= yp; Yhi += yp;
    if (xhi === xlo) xhi = xlo + 1;
    const X = v => pad.l + (v - xlo) / (xhi - xlo) * (w - pad.l - pad.r);
    const Y = v => pad.t + (1 - (tf(v) - Ylo) / (Yhi - Ylo))
      * (h - pad.t - pad.b);
    this._lastDraw = { xlo, xhi, Ylo, Yhi, log, pad, w, h, smoothed, ser };
    // Y gridlines
    c.font = '9.5px ' + MONO; c.fillStyle = '#5C636B'; c.textBaseline = 'middle';
    c.textAlign = 'right';
    for (let i = 0; i <= 4; i++) {
      const yy = pad.t + i / 4 * (h - pad.t - pad.b);
      c.strokeStyle = '#1b1f25'; c.beginPath();
      c.moveTo(pad.l, yy); c.lineTo(w - pad.r, yy); c.stroke();
      const val = log ? 10 ** (Yhi - i / 4 * (Yhi - Ylo))
        : Yhi - i / 4 * (Yhi - Ylo);
      const label = Math.abs(val) >= 1000 || (val !== 0 && Math.abs(val) < 1e-3)
        ? val.toExponential(1) : val.toFixed(3);
      c.fillText(label, pad.l - 6, yy);
    }
    // X ticks
    c.textAlign = 'center'; c.textBaseline = 'top';
    for (let i = 0; i <= 5; i++) {
      const xv = xlo + (i / 5) * (xhi - xlo);
      const xx = X(xv);
      c.strokeStyle = '#1b1f2588'; c.beginPath();
      c.moveTo(xx, h - pad.b); c.lineTo(xx, h - pad.b + 4); c.stroke();
      const lbl = (xhi - xlo > 10) ? Math.round(xv).toString()
        : (+xv).toFixed(2);
      c.fillText(lbl, xx, h - pad.b + 6);
    }
    c.strokeStyle = '#2a2f37'; c.beginPath();
    c.moveTo(pad.l, h - pad.b); c.lineTo(w - pad.r, h - pad.b); c.stroke();
    // series
    ser.forEach((s, idx) => {
      const ys = smoothed[idx];
      c.strokeStyle = s.color || PALETTE_BIG[idx % PALETTE_BIG.length];
      c.lineWidth = s.dashed ? 1.4 : 1.8;
      if (s.dashed) c.setLineDash([4, 3]); else c.setLineDash([]);
      let pen = false;
      c.beginPath();
      for (let i = 0; i < s.x.length; i++) {
        const xi = s.x[i], yi = ys[i];
        if (xi == null || yi == null || isNaN(yi)) { pen = false; continue; }
        const px = X(xi), py = Y(yi);
        if (!pen) { c.moveTo(px, py); pen = true; } else c.lineTo(px, py);
      }
      c.stroke(); c.setLineDash([]);
    });
    // Wipe overlay so we don't leave a stale crosshair
    this._drawOverlay(cursorBus.x);
  }
  _onMove(e) {
    const r = this.overlay.getBoundingClientRect();
    const mx = e.clientX - r.left;
    if (!this._lastDraw) return;
    const { xlo, xhi, pad, w } = this._lastDraw;
    const xv = xlo + (mx - pad.l) / (w - pad.l - pad.r) * (xhi - xlo);
    cursorBus.set(xv, this.xKey);
  }
  _drawOverlay(xv) {
    const o = this.overlay.getContext('2d');
    const dpr = devicePixelRatio || 1;
    o.setTransform(dpr, 0, 0, dpr, 0, 0);
    o.clearRect(0, 0, this.overlay.width / dpr, this.overlay.height / dpr);
    this.tip.style.display = 'none';
    if (xv == null || !this._lastDraw) return;
    const { xlo, xhi, pad, w, h, smoothed, ser } = this._lastDraw;
    if (xv < xlo || xv > xhi) return;
    const mx = pad.l + (xv - xlo) / (xhi - xlo) * (w - pad.l - pad.r);
    // drag-zoom band
    if (this._dragStart != null) {
      o.fillStyle = 'rgba(99,102,241,0.15)';
      o.fillRect(Math.min(this._dragStart, mx), pad.t,
                 Math.abs(mx - this._dragStart), h - pad.t - pad.b);
    }
    // crosshair line
    o.strokeStyle = '#5C636B66'; o.lineWidth = 1; o.setLineDash([3, 3]);
    o.beginPath(); o.moveTo(mx, pad.t); o.lineTo(mx, h - pad.b); o.stroke();
    o.setLineDash([]);
    // nearest point per series
    const Y = v => {
      const log = this._lastDraw.log;
      const tf = z => log ? Math.log10(z) : z;
      return pad.t + (1 - (tf(v) - this._lastDraw.Ylo)
        / (this._lastDraw.Yhi - this._lastDraw.Ylo))
        * (h - pad.t - pad.b);
    };
    const rows = [];
    ser.forEach((s, idx) => {
      const ys = smoothed[idx];
      let best = -1, bd = Infinity;
      for (let i = 0; i < s.x.length; i++) {
        if (s.x[i] == null || ys[i] == null || isNaN(ys[i])) continue;
        const d = Math.abs(s.x[i] - xv);
        if (d < bd) { bd = d; best = i; }
      }
      if (best >= 0) {
        const px = pad.l + (s.x[best] - xlo) / (xhi - xlo) * (w - pad.l - pad.r);
        const py = Y(ys[best]);
        o.fillStyle = s.color || PALETTE_BIG[idx % PALETTE_BIG.length];
        o.beginPath(); o.arc(px, py, 3.2, 0, 6.283); o.fill();
        rows.push({ s, x: s.x[best], y: ys[best], color: o.fillStyle });
      }
    });
    if (rows.length) {
      this.tip.innerHTML =
        `<div style="color:#9BA1A8;margin-bottom:3px">${this.xKey} ` +
        `${(+rows[0].x).toFixed(rows[0].x < 1 ? 4 : 0)}</div>` +
        rows.slice(0, 8).map(r =>
          `<div style="white-space:nowrap"><span style="display:` +
          `inline-block;width:8px;height:8px;border-radius:2px;` +
          `background:${r.color};margin-right:5px"></span>` +
          `${esc(r.s.name)} <b>${fmt(r.y)}</b></div>`).join('') +
        (rows.length > 8 ? `<div style="color:#5C636B">+${rows.length-8} more</div>` : '');
      this.tip.style.display = 'block';
      let tx = mx + 12;
      if (tx > w - 200) tx = mx - 204;
      this.tip.style.left = Math.max(4, tx) + 'px';
      this.tip.style.top = (pad.t + 4) + 'px';
    }
  }
}

// Bucketed-batch fetch with per-(run,key,zoom) caching.
const _bucketCache = new Map();
function _bucketKey(rid, k, xKey, xMin, xMax, bc) {
  return `${rid}|${k}|${xKey}|${xMin}|${xMax}|${bc}`;
}
async function fetchBuckets(runIds, keys, opts = {}) {
  const xKey = opts.x_key || 'step';
  const xMin = opts.x_min ?? null;
  const xMax = opts.x_max ?? null;
  const bc = opts.bucket_count || 500;
  // Look up cache; collect missing.
  const missing_runs = new Set();
  const missing_keys = new Set();
  runIds.forEach(rid => keys.forEach(k => {
    if (!_bucketCache.has(_bucketKey(rid, k, xKey, xMin, xMax, bc))) {
      missing_runs.add(rid); missing_keys.add(k);
    }
  }));
  if (missing_runs.size) {
    try {
      const resp = await fetch('/api/metrics/batch', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          run_ids: Array.from(missing_runs),
          keys: Array.from(missing_keys),
          x_key: xKey, x_min: xMin, x_max: xMax, bucket_count: bc,
        }),
      }).then(r => r.json());
      (resp.series || []).forEach(s => {
        _bucketCache.set(
          _bucketKey(s.run_id, s.key, xKey, xMin, xMax, bc), s);
      });
    } catch (e) { /* leave cache as is */ }
  }
  // Assemble result in input order.
  const out = [];
  runIds.forEach(rid => keys.forEach(k => {
    const s = _bucketCache.get(_bucketKey(rid, k, xKey, xMin, xMax, bc));
    if (s) out.push(s);
  }));
  return out;
}
// Invalidate cache for one run (SSE metrics_changed handler will use this).
function invalidateBucketsForRun(rid) {
  for (const k of Array.from(_bucketCache.keys())) {
    if (k.startsWith(rid + '|')) _bucketCache.delete(k);
  }
}

// Build a tiny panel for the drawer's "View all plots" grid. Lazy-loads
// the data via IntersectionObserver — many panels can fit on screen but
// only the visible ones fetch.
function _vapPanel(runId, key, hasData) {
  const card = el('div', 'vap-panel');
  const hd = el('div', 'vap-hd');
  hd.innerHTML = `<span class="vap-key mono">${esc(key)}</span>` +
    (hasData ? '' : '<span class="vap-no">(not logged)</span>');
  card.append(hd);
  const body = el('div', 'vap-body'); card.append(body);
  if (!hasData) return card;
  let loaded = false;
  const io = new IntersectionObserver(async entries => {
    if (loaded) return;
    if (!entries[0].isIntersecting) return;
    loaded = true;
    io.disconnect();
    const ch = new BucketChart(body);
    ch.setXKey('step');
    const series = await fetchBuckets([runId], [key], {
      x_key: 'step', bucket_count: 300,
    });
    ch.setData(series.map(s => ({
      run_id: s.run_id, key: s.key,
      name: key, color: PALETTE_BIG[0], dashed: false,
      x: s.x, y: s.y,
    })));
  }, { rootMargin: '120px' });
  io.observe(card);
  return card;
}

// Legacy MultiChart kept as a no-op shim so any leftover callers don't break.
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

/* W&B-style Analysis tab — two-pane: runs table left, panel grid right. */
const AnaState = {
  selected: new Set(),     // run_ids currently VISUALIZED (plotted)
  panels: [],              // [{id, title, y_keys, x_key, smoothing, log, include_baseline, show_band, zoom: null|{x_min,x_max}}]
  keys: [],                // all known metric keys
  search: '', regex: false,
  // Multi-condition filter modal — clauses are { join: 'AND'|'OR'|'WHERE',
  //   field: 'status'|'name'|'metric'|'started_at', op: '=|!=|contains|>|<',
  //   value: string }.
  filters: [],
  hideCrashed: false,
  sortKey: 'started', sortAsc: false,
  charts: new Map(),       // panel_id -> BucketChart instance
  baseline: null,
  panelsLoaded: false,
};

async function renderAnalysis(c) {
  AnaState.baseline = getBaseline();
  c.innerHTML = `
    <div class="anav2">
      <div class="anav2-side">
        <div class="anav2-side-hd">
          <input class="anav2-search" placeholder="search runs…" autocomplete="off" />
          <label class="anav2-rx"><input type="checkbox" /> .*</label>
        </div>
        <div class="anav2-toolbar">
          <button class="anav2-tb anav2-filter-btn" title="Filter">
            <span class="anav2-tb-ic">≡</span>
            <span class="anav2-tb-lbl">Filter</span>
            <span class="anav2-filter-count"></span>
          </button>
          <button class="anav2-tb anav2-sort-btn" title="Sort">
            <span class="anav2-tb-ic">↕</span>
            <span class="anav2-tb-lbl">Sort</span>
          </button>
          <button class="anav2-tb anav2-bulk-btn" title="Show / hide all">
            <span class="anav2-tb-ic">👁</span>
          </button>
          <span class="anav2-visualized"></span>
        </div>
        <div class="anav2-tablewrap"><table class="anav2-table"></table></div>
        <div class="anav2-side-ft">
          <button class="anav2-clear">clear selection</button>
          <span class="anav2-count"></span>
        </div>
      </div>
      <div class="anav2-main">
        <div class="anav2-bar">
          <div class="anav2-bar-l">
            <button class="anav2-add">+ Add panel</button>
            <button class="anav2-reset">Reset to defaults</button>
            <span class="anav2-baseline-tag" style="display:none">
              <span class="anav2-base-dot">★</span>
              <span class="anav2-base-name"></span>
              <button class="anav2-base-clear" title="Clear baseline">✕</button>
            </span>
          </div>
          <div class="anav2-hint">drag-select on a panel to zoom · double-click to reset</div>
        </div>
        <div class="anav2-grid"></div>
      </div>
    </div>`;
  // Wire search + toolbar buttons
  const searchEl = c.querySelector('.anav2-search');
  const rxEl = c.querySelector('.anav2-rx input');
  searchEl.value = AnaState.search;
  rxEl.checked = AnaState.regex;
  searchEl.oninput = () => { AnaState.search = searchEl.value; renderAnaTable(c); };
  rxEl.onchange = () => { AnaState.regex = rxEl.checked; renderAnaTable(c); };
  c.querySelector('.anav2-filter-btn').onclick = () => openFilterModal(c);
  c.querySelector('.anav2-sort-btn').onclick = e =>
    openSortMenu(c, e.currentTarget);
  c.querySelector('.anav2-bulk-btn').onclick = e =>
    openBulkMenu(c, e.currentTarget);
  c.querySelector('.anav2-clear').onclick = () => {
    AnaState.selected.clear(); renderAnaTable(c); refreshAllPanels();
    syncUrl();
  };
  c.querySelector('.anav2-add').onclick = () => openAddPanelModal(c);
  c.querySelector('.anav2-reset').onclick = async () => {
    if (!confirm('Reset to the default 4 panels (train/val loss, val_acc, lr)?')) return;
    AnaState.panels = [];
    // Pull defaults from the backend (which provides them when saved is empty)
    try {
      await fetch('/api/analysis/panels', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ panels: [] }),
      });
      const p = await api('/analysis/panels');
      AnaState.panels = (p && p.panels) || [];
    } catch (e) { /* keep empty */ }
    renderAnaPanels(c);
  };
  // Load keys + panels + initial selection from URL
  try {
    const k = await api('/metrics/keys');
    AnaState.keys = (k && k.keys) || [];
  } catch (e) { AnaState.keys = []; }
  if (!AnaState.panelsLoaded) {
    try {
      const p = await api('/analysis/panels');
      AnaState.panels = (p && p.panels) || [];
      AnaState.panelsLoaded = true;
    } catch (e) { AnaState.panels = []; }
    // Hydrate selection + baseline from URL
    const u = new URL(location.href);
    const sel = u.searchParams.get('runs');
    if (sel) sel.split(',').filter(Boolean).forEach(r => AnaState.selected.add(r));
    const base = u.searchParams.get('base');
    if (base) { setBaseline(base); AnaState.baseline = base; }
  }
  renderAnaBaselineTag(c);
  renderAnaTable(c);
  renderAnaPanels(c);
}

function renderAnaBaselineTag(c) {
  const tag = c.querySelector('.anav2-baseline-tag');
  if (!tag) return;
  if (!AnaState.baseline) { tag.style.display = 'none'; return; }
  const r = (S.runs || []).find(r => r.id === AnaState.baseline);
  tag.querySelector('.anav2-base-name').textContent =
    (r && r.run_name) || AnaState.baseline;
  tag.style.display = '';
  tag.querySelector('.anav2-base-clear').onclick = () => {
    setBaseline(null); AnaState.baseline = null;
    renderAnaBaselineTag(c); refreshAllPanels(); syncUrl();
    renderAnaTable(c);
  };
}

function _runFieldValue(r, field) {
  switch (field) {
    case 'status': return (r.status || '').toLowerCase();
    case 'name': return (r.run_name || r.id || '').toLowerCase();
    case 'metric': return r.headline_metric ?? null;
    case 'started_at': return r.started_at || r.created_at || '';
    case 'gpu': return r.gpu_index;
    default: return '';
  }
}

function _matchesClause(r, clause) {
  const v = _runFieldValue(r, clause.field);
  const tv = (clause.value || '').toString().toLowerCase();
  const num = parseFloat(clause.value);
  switch (clause.op) {
    case '=':   return String(v).toLowerCase() === tv;
    case '!=':  return String(v).toLowerCase() !== tv;
    case 'contains':
      return String(v).toLowerCase().includes(tv);
    case '!contains':
      return !String(v).toLowerCase().includes(tv);
    case '>':   return typeof v === 'number' && v > num;
    case '<':   return typeof v === 'number' && v < num;
    case '>=':  return typeof v === 'number' && v >= num;
    case '<=':  return typeof v === 'number' && v <= num;
    default: return true;
  }
}

function _matchesFilters(r) {
  const fs = AnaState.filters || [];
  if (!fs.length) return true;
  // Evaluate left-to-right honoring AND/OR. First clause's join is ignored
  // ('WHERE'). Each subsequent join combines with the running value.
  let acc = _matchesClause(r, fs[0]);
  for (let i = 1; i < fs.length; i++) {
    const ok = _matchesClause(r, fs[i]);
    if (fs[i].join === 'OR') acc = acc || ok;
    else acc = acc && ok;
  }
  return acc;
}

function _matchesRun(r) {
  if (AnaState.hideCrashed && r.status === 'crashed') return false;
  if (!_matchesFilters(r)) return false;
  const q = (AnaState.search || '').trim();
  if (!q) return true;
  const hay = (r.run_name || '') + ' ' + (r.id || '');
  if (AnaState.regex) {
    try { return new RegExp(q, 'i').test(hay); }
    catch (e) { return false; }
  }
  return hay.toLowerCase().includes(q.toLowerCase());
}

function _sortedAnaRuns() {
  const runs = (S.runs || []).filter(_matchesRun);
  const k = AnaState.sortKey;
  const asc = AnaState.sortAsc;
  runs.sort((a, b) => {
    let va, vb;
    if (k === 'name') { va = (a.run_name||'').toLowerCase(); vb = (b.run_name||'').toLowerCase(); }
    else if (k === 'status') { va = a.status||''; vb = b.status||''; }
    else if (k === 'metric') { va = a.headline_metric ?? Infinity; vb = b.headline_metric ?? Infinity; }
    else { va = a.started_at||a.created_at||''; vb = b.started_at||b.created_at||''; }
    if (typeof va === 'number' && typeof vb === 'number') return asc ? va - vb : vb - va;
    return asc ? String(va).localeCompare(String(vb)) : String(vb).localeCompare(String(va));
  });
  return runs;
}

function renderAnaTable(c) {
  const tbl = c.querySelector('.anav2-table');
  if (!tbl) return;
  const runs = _sortedAnaRuns();
  const COLS = [['name', 'name'], ['status', 'status'],
    ['metric', 'metric'], ['started', 'started']];
  const hrow = COLS.map(([k, lbl]) => {
    const on = AnaState.sortKey === k;
    const arrow = on ? (AnaState.sortAsc ? ' ▲' : ' ▼') : '';
    return `<th data-s="${k}" class="th-sort${on?' on':''}">${esc(lbl)}<span class="th-arrow">${arrow}</span></th>`;
  }).join('');
  tbl.innerHTML = `<thead><tr><th></th><th></th>${hrow}<th></th></tr></thead><tbody></tbody>`;
  tbl.querySelectorAll('thead th[data-s]').forEach(th => {
    th.onclick = () => {
      const k = th.dataset.s;
      if (AnaState.sortKey === k) AnaState.sortAsc = !AnaState.sortAsc;
      else { AnaState.sortKey = k; AnaState.sortAsc = k === 'name'; }
      renderAnaTable(c);
    };
  });
  const tb = tbl.querySelector('tbody');
  runs.slice(0, 1000).forEach(r => {
    const isViz = AnaState.selected.has(r.id);
    const tr = el('tr', isViz ? 'sel' : '');
    const isBase = r.id === AnaState.baseline;
    const m = r.headline_metric == null ? '—' : fmt(r.headline_metric, 4);
    // Each row gets a stable color so the eye dot matches the plot line.
    const idx = Array.from(AnaState.selected).indexOf(r.id);
    const color = isViz && idx >= 0
      ? PALETTE_BIG[idx % PALETTE_BIG.length] : 'transparent';
    tr.innerHTML =
      `<td class="anav2-eyecell"><button class="anav2-eye${isViz?' on':''}" title="${isViz?'Hide':'Visualize'}">${isViz?'👁':'👁'}</button>` +
        `<span class="anav2-eyedot" style="background:${color}"></span></td>` +
      `<td class="anav2-star${isBase?' on':''}" title="Set as baseline">★</td>` +
      `<td class="anav2-name mono">${esc(r.run_name||r.id)}</td>` +
      `<td><span class="chip s-${r.status}"><span class="dot"></span>${esc(r.status||'')}</span></td>` +
      `<td class="mono">${m}</td>` +
      `<td class="mono">${esc(ago(r.started_at||r.created_at||''))}</td>` +
      `<td><button class="anav2-solo" title="Solo this run">↗</button></td>`;
    tr.querySelector('.anav2-eye').onclick = () => {
      if (AnaState.selected.has(r.id)) AnaState.selected.delete(r.id);
      else AnaState.selected.add(r.id);
      renderAnaTable(c); refreshAllPanels(); syncUrl();
    };
    tr.querySelector('.anav2-star').onclick = e => {
      e.stopPropagation();
      const newBase = isBase ? null : r.id;
      setBaseline(newBase); AnaState.baseline = newBase;
      renderAnaTable(c); renderAnaBaselineTag(c);
      refreshAllPanels(); syncUrl();
    };
    tr.querySelector('.anav2-solo').onclick = e => {
      e.stopPropagation();
      AnaState.selected.clear(); AnaState.selected.add(r.id);
      renderAnaTable(c); refreshAllPanels(); syncUrl();
    };
    tb.append(tr);
  });
  c.querySelector('.anav2-count').textContent =
    `${AnaState.selected.size} visualized · ${runs.length} runs`;
  const visEl = c.querySelector('.anav2-visualized');
  if (visEl) visEl.textContent =
    `${AnaState.selected.size} visualized`;
  const filterCount = c.querySelector('.anav2-filter-count');
  if (filterCount) {
    const n = AnaState.filters.length + (AnaState.hideCrashed ? 1 : 0);
    filterCount.textContent = n ? `(${n})` : '';
  }
}

// === Filter modal ========================================================
function openFilterModal(c) {
  const sc = el('div', 'mscrim');
  const m = el('div', 'modal modal-filter');
  m.innerHTML = `
    <div class="modal-hd"><h2>Filter runs</h2>
      <button class="iconbtn" id="fmx">✕</button></div>
    <div class="fm-rows" id="fm-rows"></div>
    <div class="fm-add-row">
      <button class="btn" id="fm-add">+ New filter</button>
    </div>
    <div class="fm-toggles">
      <label><input type="checkbox" id="fm-hide-crashed" />
        <span>Hide crashed runs</span></label>
    </div>
    <div class="modal-actions">
      <button class="btn" id="fm-clear">Clear all filters</button>
      <button class="btn pri" id="fm-apply">Apply</button>
    </div>`;
  sc.append(m); document.body.append(sc);
  sc.onclick = e => { if (e.target === sc) sc.remove(); };
  m.querySelector('#fmx').onclick = () => sc.remove();
  // Working copy
  let rows = (AnaState.filters || []).map(c => ({ ...c }));
  if (!rows.length) {
    rows.push({ join: 'WHERE', field: 'status', op: '=', value: '' });
  }
  const fields = [
    ['status','status'], ['name','name'], ['metric','headline metric'],
    ['started_at','started_at'], ['gpu','GPU index'],
  ];
  const ops = [
    ['=','='], ['!=','!='], ['contains','contains'], ['!contains','does not contain'],
    ['>','>'], ['<','<'], ['>=','>='], ['<=','<='],
  ];
  const rowsEl = m.querySelector('#fm-rows');
  function paint() {
    rowsEl.innerHTML = '';
    rows.forEach((cl, i) => {
      const r = el('div', 'fm-row');
      const join = (i === 0)
        ? `<div class="fm-join fm-where">WHERE</div>`
        : `<select class="fm-join-sel">
             <option value="AND"${cl.join==='AND'?' selected':''}>AND</option>
             <option value="OR"${cl.join==='OR'?' selected':''}>OR</option>
           </select>`;
      const fOpts = fields.map(([v,l]) =>
        `<option value="${v}"${cl.field===v?' selected':''}>${esc(l)}</option>`).join('');
      const oOpts = ops.map(([v,l]) =>
        `<option value="${v}"${cl.op===v?' selected':''}>${esc(l)}</option>`).join('');
      r.innerHTML =
        `<div class="fm-cell">${join}</div>` +
        `<select class="fm-field">${fOpts}</select>` +
        `<select class="fm-op">${oOpts}</select>` +
        `<input class="fm-val" value="${esc(cl.value||'')}" autocomplete="off"/>` +
        `<button class="fm-del" title="Remove">✕</button>`;
      r.querySelector('.fm-field').onchange = e => { rows[i].field = e.target.value; };
      r.querySelector('.fm-op').onchange = e => { rows[i].op = e.target.value; };
      r.querySelector('.fm-val').oninput = e => { rows[i].value = e.target.value; };
      if (i > 0) {
        r.querySelector('.fm-join-sel').onchange = e => { rows[i].join = e.target.value; };
      }
      r.querySelector('.fm-del').onclick = () => { rows.splice(i,1); paint(); };
      rowsEl.append(r);
    });
  }
  paint();
  m.querySelector('#fm-hide-crashed').checked = !!AnaState.hideCrashed;
  m.querySelector('#fm-add').onclick = () => {
    rows.push({ join: 'AND', field: 'name', op: 'contains', value: '' });
    paint();
  };
  m.querySelector('#fm-clear').onclick = () => {
    rows.length = 0; paint();
  };
  m.querySelector('#fm-apply').onclick = () => {
    AnaState.filters = rows.filter(r =>
      r.field && r.op && (r.value !== '' || r.field === 'status'));
    AnaState.hideCrashed = m.querySelector('#fm-hide-crashed').checked;
    sc.remove();
    renderAnaTable(c);
  };
}

// === Sort menu ===========================================================
function openSortMenu(c, anchor) {
  document.querySelectorAll('.anav2-popover').forEach(p => p.remove());
  const pop = el('div', 'anav2-popover');
  const opts = [
    ['started', 'started time'], ['name', 'name'], ['status', 'status'],
    ['metric', 'metric'],
  ];
  pop.innerHTML = opts.map(([k,l]) =>
    `<button data-k="${k}" class="anav2-pop-it${AnaState.sortKey===k?' on':''}">` +
    `${esc(l)} ${AnaState.sortKey===k ? (AnaState.sortAsc?'▲':'▼') : ''}</button>`
  ).join('') +
    `<div class="anav2-pop-sep"></div>` +
    `<button class="anav2-pop-it" data-dir>${AnaState.sortAsc?'ascending':'descending'}</button>`;
  const r = anchor.getBoundingClientRect();
  pop.style.left = r.left + 'px';
  pop.style.top = (r.bottom + 4) + 'px';
  document.body.append(pop);
  setTimeout(() => {
    const off = (e) => {
      if (pop.contains(e.target)) return;
      pop.remove(); document.removeEventListener('mousedown', off);
    };
    document.addEventListener('mousedown', off);
  }, 0);
  pop.querySelectorAll('button').forEach(b => b.onclick = () => {
    if (b.dataset.k) {
      if (AnaState.sortKey === b.dataset.k) AnaState.sortAsc = !AnaState.sortAsc;
      else { AnaState.sortKey = b.dataset.k; AnaState.sortAsc = (b.dataset.k === 'name'); }
    } else {
      AnaState.sortAsc = !AnaState.sortAsc;
    }
    pop.remove();
    renderAnaTable(c);
  });
}

// === Bulk visualize menu =================================================
function openBulkMenu(c, anchor) {
  document.querySelectorAll('.anav2-popover').forEach(p => p.remove());
  const pop = el('div', 'anav2-popover');
  pop.innerHTML =
    `<button class="anav2-pop-it" data-a="all">Make all visible</button>` +
    `<button class="anav2-pop-it" data-a="none">Make all hidden</button>` +
    `<div class="anav2-pop-sep"></div>` +
    `<button class="anav2-pop-it" data-a="visible">Make filtered visible</button>` +
    `<button class="anav2-pop-it" data-a="invert">Invert</button>`;
  const r = anchor.getBoundingClientRect();
  pop.style.left = r.left + 'px';
  pop.style.top = (r.bottom + 4) + 'px';
  document.body.append(pop);
  setTimeout(() => {
    const off = (e) => {
      if (pop.contains(e.target)) return;
      pop.remove(); document.removeEventListener('mousedown', off);
    };
    document.addEventListener('mousedown', off);
  }, 0);
  pop.querySelectorAll('button').forEach(b => b.onclick = () => {
    const filtered = _sortedAnaRuns();
    const act = b.dataset.a;
    if (act === 'all') (S.runs || []).forEach(r => AnaState.selected.add(r.id));
    else if (act === 'none') AnaState.selected.clear();
    else if (act === 'visible') filtered.forEach(r => AnaState.selected.add(r.id));
    else if (act === 'invert') {
      (S.runs || []).forEach(r => {
        if (AnaState.selected.has(r.id)) AnaState.selected.delete(r.id);
        else AnaState.selected.add(r.id);
      });
    }
    pop.remove();
    renderAnaTable(c); refreshAllPanels(); syncUrl();
  });
}

function renderAnaPanels(c) {
  const grid = c.querySelector('.anav2-grid');
  if (!grid) return;
  // Tear down old charts
  AnaState.charts.forEach(ch => ch.destroy && ch.destroy());
  AnaState.charts.clear();
  grid.innerHTML = '';
  if (!AnaState.panels.length) {
    const empty = el('div', 'anav2-grid-empty');
    empty.innerHTML =
      '<div class="anav2-grid-empty-icon">📊</div>' +
      '<h2>No panels</h2>' +
      '<p>Add a panel to start plotting selected runs, or reset to the ' +
      'default set (train/val loss, val accuracy, learning rate).</p>' +
      '<div class="anav2-grid-empty-actions">' +
      '<button class="btn pri anav2-emp-add">+ Add panel</button>' +
      '<button class="btn anav2-emp-reset">Reset to defaults</button>' +
      '</div>';
    grid.append(empty);
    empty.querySelector('.anav2-emp-add').onclick = () => openAddPanelModal(c);
    empty.querySelector('.anav2-emp-reset').onclick =
      () => c.querySelector('.anav2-reset').click();
    return;
  }
  AnaState.panels.forEach(p => grid.append(buildPanel(c, p)));
  refreshAllPanels();
}

function buildPanel(c, p) {
  const card = el('div', 'anav2-panel' + (p.width === 'full' ? ' full' : ''));
  card.dataset.pid = p.id;
  // Two-row header: title on its own row (so it's never squeezed), then
  // a control row below it.
  const titleRow = el('div', 'anav2-panel-titlerow');
  titleRow.innerHTML =
    `<div class="anav2-panel-title">${esc(p.title)}</div>` +
    `<div class="anav2-panel-keys mono">${esc((p.y_keys||[]).join(' · '))}</div>` +
    `<div class="anav2-panel-ctrls-r">` +
      `<button class="anav2-ctrl-btn anav2-edit" title="edit panel">✎</button>` +
      `<button class="anav2-ctrl-btn anav2-rm" title="remove panel">✕</button>` +
    `</div>`;
  card.append(titleRow);
  const hd = el('div', 'anav2-panel-hd');
  hd.innerHTML =
    `<label class="anav2-ctrl"><span>smoothing</span>` +
      `<input type="range" min="0" max="0.99" step="0.01" value="${p.smoothing||0}" class="anav2-smooth"/>` +
      `<span class="anav2-smooth-val">${(+(p.smoothing||0)).toFixed(2)}</span></label>` +
    `<button class="anav2-ctrl-btn anav2-logy${p.y_log?' on':''}" title="log y">log y</button>` +
    `<button class="anav2-ctrl-btn anav2-baseinc${p.include_baseline?' on':''}" title="include baseline">★</button>`;
  card.append(hd);
  // Body host
  const body = el('div', 'anav2-panel-body');
  card.append(body);
  // Legend strip (color dot + run name) — populated by refreshPanel
  const legend = el('div', 'anav2-panel-legend');
  card.append(legend);
  // Chart
  const chart = new BucketChart(body);
  chart.setLog(!!p.y_log);
  chart.setSmoothing(+(p.smoothing || 0));
  chart.setXKey(p.x_key || 'step');
  chart.onZoom = (a, b) => {
    if (a == null) p.zoom = null;
    else p.zoom = { x_min: Math.min(a, b), x_max: Math.max(a, b) };
    refreshPanel(p);
  };
  AnaState.charts.set(p.id, chart);
  // Wire controls
  const smInput = hd.querySelector('.anav2-smooth');
  const smVal = hd.querySelector('.anav2-smooth-val');
  smInput.oninput = () => {
    p.smoothing = parseFloat(smInput.value);
    smVal.textContent = p.smoothing.toFixed(2);
    chart.setSmoothing(p.smoothing);
    savePanelsDebounced();
  };
  // Use card.querySelector since some buttons live in the title row and
  // others live in the control row. Querying from the right ancestor.
  card.querySelector('.anav2-logy').onclick = e => {
    p.y_log = !p.y_log;
    e.currentTarget.classList.toggle('on', p.y_log);
    chart.setLog(p.y_log); savePanelsDebounced();
  };
  card.querySelector('.anav2-baseinc').onclick = e => {
    p.include_baseline = !p.include_baseline;
    e.currentTarget.classList.toggle('on', p.include_baseline);
    refreshPanel(p); savePanelsDebounced();
  };
  card.querySelector('.anav2-edit').onclick = () => openEditPanelModal(c, p);
  card.querySelector('.anav2-rm').onclick = () => {
    AnaState.panels = AnaState.panels.filter(x => x.id !== p.id);
    renderAnaPanels(c); savePanelsDebounced();
  };
  return card;
}

async function refreshPanel(p) {
  const chart = AnaState.charts.get(p.id);
  if (!chart) return;
  const card = document.querySelector(`.anav2-panel[data-pid="${p.id}"]`);
  const legend = card && card.querySelector('.anav2-panel-legend');
  const body = card && card.querySelector('.anav2-panel-body');
  const runIds = Array.from(AnaState.selected);
  if (AnaState.baseline && p.include_baseline
      && !runIds.includes(AnaState.baseline)) {
    runIds.push(AnaState.baseline);
  }
  if (!runIds.length) {
    chart.setData([]);
    if (legend) legend.innerHTML =
      '<span class="anav2-legend-empty">Tick runs on the left to plot.</span>';
    return;
  }
  if (!(p.y_keys || []).length) {
    chart.setData([]);
    if (legend) legend.innerHTML =
      '<span class="anav2-legend-empty">Click ✎ to pick metrics for this panel.</span>';
    return;
  }
  const opts = { x_key: p.x_key || 'step', bucket_count: 500 };
  if (p.zoom) { opts.x_min = p.zoom.x_min; opts.x_max = p.zoom.x_max; }
  const series = await fetchBuckets(runIds, p.y_keys, opts);
  // Color assignment: stable per-run via a map; baseline always grey/dashed.
  const colorOf = new Map();
  let idx = 0;
  Array.from(AnaState.selected).forEach(rid => {
    colorOf.set(rid, PALETTE_BIG[idx++ % PALETTE_BIG.length]);
  });
  // Series with any non-null y are 'real'; others got filtered server-side.
  const rendered = series.filter(s =>
    s.y && s.y.some(v => v != null)).map(s => {
    const isBase = (s.run_id === AnaState.baseline);
    const run = (S.runs || []).find(r => r.id === s.run_id);
    return {
      run_id: s.run_id, key: s.key,
      name: ((run && run.run_name) || s.run_id)
        + (s.key && p.y_keys.length > 1 ? ' · ' + s.key : ''),
      color: isBase ? BASELINE_COLOR
        : (colorOf.get(s.run_id) || PALETTE_BIG[0]),
      dashed: isBase,
      x: s.x, y: s.y,
    };
  });
  chart.setData(rendered);
  // Legend
  if (legend) {
    if (!rendered.length) {
      // Show which runs were selected but didn't log this metric
      const missing = runIds.map(rid => {
        const r = (S.runs || []).find(r => r.id === rid);
        return (r && r.run_name) || rid;
      });
      legend.innerHTML =
        `<span class="anav2-legend-empty">None of the selected run(s) ` +
        `logged <code>${esc((p.y_keys||[]).join(', '))}</code>.</span>`;
    } else {
      legend.innerHTML = rendered.slice(0, 10).map(s =>
        `<span class="anav2-legend-item" title="${esc(s.name)}">` +
        `<span class="anav2-legend-dot" style="background:${s.color}` +
        `${s.dashed?';outline:1px dashed #fff3':''}"></span>` +
        `<span class="anav2-legend-name mono">${esc(s.name)}</span>` +
        `</span>`).join('') +
        (rendered.length > 10
          ? `<span class="anav2-legend-empty">+${rendered.length-10} more</span>`
          : '');
    }
  }
}

function refreshAllPanels() {
  AnaState.panels.forEach(p => refreshPanel(p));
}

let _saveTimer = null;
function savePanelsDebounced() {
  if (_saveTimer) clearTimeout(_saveTimer);
  _saveTimer = setTimeout(async () => {
    try {
      await fetch('/api/analysis/panels', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ panels: AnaState.panels }),
      });
    } catch (e) { /* ignore */ }
  }, 500);
}

function syncUrl() {
  const u = new URL(location.href);
  const sel = Array.from(AnaState.selected);
  if (sel.length) u.searchParams.set('runs', sel.join(','));
  else u.searchParams.delete('runs');
  if (AnaState.baseline) u.searchParams.set('base', AnaState.baseline);
  else u.searchParams.delete('base');
  history.replaceState({}, '', u);
}

function openAddPanelModal(c) {
  const sc = el('div', 'mscrim');
  const m = el('div', 'modal');
  m.innerHTML = `
    <div class="modal-hd"><h2>Add panel</h2>
      <button class="iconbtn" id="apx">✕</button></div>
    <div style="display:flex;flex-direction:column;gap:10px;margin-top:8px">
      <label>Title <input class="onb-in" id="ap-title" placeholder="Panel title"/></label>
      <label>Y metric(s)
        <select class="onb-in" id="ap-y" multiple size="8" style="height:auto"></select>
        <div style="font-size:11px;color:var(--muted)">cmd/ctrl-click to multi-select</div>
      </label>
      <label>X axis
        <select class="onb-in" id="ap-x">
          <option value="step">step</option>
          <option value="wall_time">wall_time</option>
        </select>
      </label>
    </div>
    <div class="modal-actions">
      <button class="btn pri" id="ap-add">Add panel</button>
    </div>`;
  sc.append(m); document.body.append(sc);
  sc.onclick = e => { if (e.target === sc) sc.remove(); };
  m.querySelector('#apx').onclick = () => sc.remove();
  const ysel = m.querySelector('#ap-y');
  AnaState.keys.forEach(k => {
    const o = el('option'); o.value = k; o.textContent = k; ysel.append(o);
  });
  m.querySelector('#ap-add').onclick = () => {
    const yks = Array.from(ysel.selectedOptions).map(o => o.value);
    if (!yks.length) return;
    const title = m.querySelector('#ap-title').value.trim() || yks.join(', ');
    const xk = m.querySelector('#ap-x').value || 'step';
    const id = 'p' + Date.now().toString(36);
    AnaState.panels.push({
      id, title, y_keys: yks, x_key: xk,
      smoothing: 0, y_log: false, include_baseline: true,
      show_band: false, width: 'half',
    });
    sc.remove();
    renderAnaPanels(c); savePanelsDebounced();
  };
}

function openEditPanelModal(c, p) {
  const sc = el('div', 'mscrim');
  const m = el('div', 'modal');
  m.innerHTML = `
    <div class="modal-hd"><h2>Edit panel</h2>
      <button class="iconbtn" id="epx">✕</button></div>
    <div style="display:flex;flex-direction:column;gap:10px;margin-top:8px">
      <label>Title <input class="onb-in" id="ep-title" value="${esc(p.title)}"/></label>
      <label>Y metric(s)
        <select class="onb-in" id="ep-y" multiple size="8" style="height:auto"></select>
      </label>
      <label>X axis
        <select class="onb-in" id="ep-x">
          <option value="step">step</option>
          <option value="wall_time">wall_time</option>
        </select>
      </label>
      <label>Width
        <select class="onb-in" id="ep-w">
          <option value="half">half-width</option>
          <option value="full">full-width</option>
        </select>
      </label>
    </div>
    <div class="modal-actions">
      <button class="btn pri" id="ep-save">Save</button>
    </div>`;
  sc.append(m); document.body.append(sc);
  sc.onclick = e => { if (e.target === sc) sc.remove(); };
  m.querySelector('#epx').onclick = () => sc.remove();
  const ysel = m.querySelector('#ep-y');
  AnaState.keys.forEach(k => {
    const o = el('option'); o.value = k; o.textContent = k;
    if ((p.y_keys||[]).includes(k)) o.selected = true; ysel.append(o);
  });
  m.querySelector('#ep-x').value = p.x_key || 'step';
  m.querySelector('#ep-w').value = p.width || 'half';
  m.querySelector('#ep-save').onclick = () => {
    p.title = m.querySelector('#ep-title').value.trim() || p.title;
    p.y_keys = Array.from(ysel.selectedOptions).map(o => o.value);
    p.x_key = m.querySelector('#ep-x').value;
    p.width = m.querySelector('#ep-w').value;
    sc.remove();
    renderAnaPanels(c); savePanelsDebounced();
  };
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
