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
  ['lessons', 'Lessons', '📖'],
  ['latex', 'Write the paper', '📝'],
  ['system', 'System stats', '▤'],
  ['authkeys', 'authorized_keys', '⚿'],
];
const VIEW_TITLE = Object.fromEntries(VIEWS.map(v => [v[0], v[1]]));

/* ── URL routing ────────────────────────────────────────────────────────── */
// Map internal view IDs ↔ URL paths. The internal id "latex" is kept for
// back-compat with code paths; the URL prefers /write-paper because it's
// what the user sees in the burger menu.
const VIEW_TO_PATH = {
  dashboard: '/dashboard',
  analysis: '/analysis',
  lessons: '/lessons',
  latex: '/write-paper',
  system: '/system-stats',          // "System stats" — NOT "Settings"
  authkeys: '/authkeys',
};
const PATH_TO_VIEW = (() => {
  const m = {};
  for (const [v, p] of Object.entries(VIEW_TO_PATH)) m[p] = v;
  // aliases
  m['/'] = 'dashboard';
  m['/paper'] = 'latex';
  m['/writepaper'] = 'latex';
  m['/write_paper'] = 'latex';
  m['/system'] = 'system';
  m['/systemstats'] = 'system';
  m['/authorized-keys'] = 'authkeys';
  m['/authorized_keys'] = 'authkeys';
  // NOTE: /settings is intentionally NOT mapped — Settings is a modal
  // (openSettings()) that overlays on any view, not its own page.
  return m;
})();
function viewFromPath(pathname) {
  const p = (pathname || '/').replace(/\/+$/, '') || '/';
  return PATH_TO_VIEW[p] || null;
}
function pushView(viewId) {
  const path = VIEW_TO_PATH[viewId] || '/';
  if (window.location.pathname !== path) {
    try { history.pushState({ view: viewId }, '', path); } catch (e) {}
  }
}
// Public helper used everywhere a view changes — replaces S.view = X; render();
function setView(viewId) {
  if (S.view === viewId) return;
  S.view = viewId;
  pushView(viewId);
  render();
}
window.addEventListener('popstate', () => {
  const v = viewFromPath(window.location.pathname);
  if (v && S.view !== v) { S.view = v; render(); }
});

function render() {
  clearViewTimers(); stopTermPoll();
  const app = document.getElementById('app');
  app.innerHTML = '';
  // Write-the-paper in paper mode also gets the right rail so the user
  // can chat with the Author Agent while looking at the draft. Other
  // views (Analysis, Settings, Lessons, …) stay solo.
  const isPaperView = (S.view === 'latex'
    && S.mode && S.mode.mode === 'paper');
  if (S.view !== 'dashboard' && !isPaperView) {
    app.className = 'app solo';
    app.append(header(), viewPane());
    return;
  }
  app.className = 'app';
  if (isPaperView) {
    // Default the rail to the Author Agent on first visit to this view.
    if (!S._railTabSetForPaper) {
      S.railTab = 'author';
      S._railTabSetForPaper = true;
    }
    // viewPane() renders Write-the-paper into the LEFT column. We use
    // .paper-left (NOT .left) so the paper-wrap can own its own flex
    // layout — .left has a fixed 3-row grid that clipped the sub-tabs.
    const main = viewPane();
    main.classList.add('paper-left');
    app.append(header(), main, rail(), el('button', 'fab', '✉'));
  } else {
    app.append(header(), left(), rail(), el('button', 'fab', '✉'));
  }
  applyRailW();
  document.querySelector('.fab').onclick = () =>
    document.querySelector('.rail').classList.toggle('show');
  if (!isPaperView) { paintHero(); paintStats(); paintTable(); }
  paintRail();
  pollGpus();
  pollBlessStatus();
}

/* Pre-flight code-bless banner. Top-of-page banner that shows whether
 * the council has approved the codebase. Reasons for prominence:
 *   - pending  → user knows the agent is paused for review, not stuck
 *   - rejected → user sees exactly what the council asked the agent to
 *                fix, without digging into the Summary feed
 *   - approved → small green "✓ blessed" badge so it's visible but not
 *                in the way after the first time
 *   - not_requested → small grey "no review yet" badge
 */
async function pollBlessStatus() {
  let last = null;
  const tick = async () => {
    let s;
    try { s = await api('/council/bless/status'); } catch (e) { return; }
    if (!s) return;
    // dedupe: only repaint when state actually changes
    const sig = (s.status || '') + '|' + ((s.blockers || []).length);
    if (sig === last) return;
    last = sig;
    renderBlessBanner(s);
  };
  tick();
  addTimer(setInterval(tick, 6000));
}

function renderBlessBanner(s) {
  const existing = document.getElementById('bless-banner');
  if (existing) existing.remove();
  // Approved state — small badge in the header instead of a full banner.
  if (!s || s.status === 'approved') {
    const hdr = document.querySelector('.hdr');
    if (hdr && !hdr.querySelector('.bless-pill')) {
      const pill = el('span', 'bless-pill bless-pill-ok',
        '✓ code blessed');
      pill.title = (s && s.summary) || 'Council approved the codebase';
      hdr.appendChild(pill);
    }
    return;
  }
  // not_requested / pending / rejected — full banner under the header.
  const colorClass = ({
    pending: 'bless-pending',
    rejected: 'bless-rejected',
    not_requested: 'bless-waiting',
  })[s.status] || 'bless-waiting';
  const icon = ({
    pending: '⏳',
    rejected: '✗',
    not_requested: '·',
  })[s.status] || '·';
  const title = ({
    pending: 'Council is reviewing the codebase…',
    rejected: 'Council rejected the codebase — agent must fix before any '
      + 'run can launch',
    not_requested: 'Awaiting code review — the agent will request it after '
      + 'scaffolding the baseline',
  })[s.status] || s.summary || 'Code review';
  const blockersHtml = (s.blockers || []).length
    ? '<ul class="bless-blockers">' +
      s.blockers.slice(0, 12).map(b => `<li>${esc(b)}</li>`).join('') +
      (s.blockers.length > 12
        ? `<li>… and ${s.blockers.length - 12} more</li>` : '') +
      '</ul>'
    : '';
  const banner = el('div', `bless-banner ${colorClass}`);
  banner.id = 'bless-banner';
  banner.innerHTML =
    `<div class="bless-banner-row">` +
      `<div class="bless-icon">${icon}</div>` +
      `<div class="bless-text"><b>${esc(title)}</b>` +
      (s.summary && s.summary !== title
        ? `<div class="bless-summary">${esc(s.summary)}</div>` : '') +
      blockersHtml +
      '</div>' +
      (s.status === 'rejected'
        ? '<button class="btn xs" id="bless-clear" title="Mark as ' +
          'cleared — the agent will re-request review next">Clear &amp; ' +
          'await re-review</button>' : '') +
    '</div>';
  const app = document.getElementById('app');
  const hdr = app && app.querySelector('.hdr');
  if (hdr) hdr.insertAdjacentElement('afterend', banner);
  else if (app) app.insertBefore(banner, app.firstChild);
  const clr = document.getElementById('bless-clear');
  if (clr) clr.onclick = async () => {
    clr.disabled = true; clr.textContent = 'Clearing…';
    await post('/council/bless/reset', {});
    pollBlessStatus();
  };
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
  // (Mode toggle moved to the Write-the-paper page itself — not in the global
  // header anymore. The paper meta pill stays in the header for at-a-glance
  // status while you're on any view.)
  const mode = (S.mode && S.mode.mode) || 'research';
  const meta = (S.mode && S.mode.meta) || null;
  if (mode === 'paper' && meta) {
    const days = (S.mode && S.mode.days_till_deadline);
    h.append(el('div', 'paper-pill',
      `📝 ${esc(meta.venue || 'Paper')}` +
      (days != null ? ` · ${esc(days.toFixed ? days.toFixed(1) : days)}d` : '')));
  }
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

/* ── styled dialog helpers (replace native confirm/alert/prompt) ────── */
// Async, dark-theme dialogs that match the app's design. Each returns a
// Promise so we can `await aruiConfirm(...)`. Esc cancels; Enter submits.
function _aruiDialog({ title, message, kind = 'confirm', danger = false,
                       defaultValue = '', okText, cancelText } = {}) {
  return new Promise(resolve => {
    const sc = el('div', 'mscrim arui-dlg-scrim');
    const m  = el('div', 'modal arui-dlg' + (danger ? ' danger' : ''));
    const okLabel = okText || (kind === 'alert' ? 'OK'
                              : danger ? 'Delete' : 'Confirm');
    const cancelLabel = cancelText || 'Cancel';
    m.innerHTML =
      `<div class="arui-dlg-hd">${title ? esc(title) : 'Confirm'}</div>` +
      (message ? `<div class="arui-dlg-bd">${esc(message)
                  .replace(/\n/g, '<br>')}</div>` : '') +
      (kind === 'prompt'
        ? `<input class="arui-dlg-in" autocomplete="off" />` : '') +
      `<div class="arui-dlg-actions">` +
        (kind === 'alert' ? ''
          : `<button class="btn arui-dlg-cancel">${esc(cancelLabel)}</button>`) +
        `<button class="btn pri arui-dlg-ok${danger?' danger':''}">${esc(okLabel)}</button>` +
      `</div>`;
    sc.append(m); document.body.append(sc);
    const inp = m.querySelector('.arui-dlg-in');
    if (inp) { inp.value = defaultValue; setTimeout(() => inp.focus(), 0); }
    else setTimeout(() => m.querySelector('.arui-dlg-ok').focus(), 0);
    const close = (val) => {
      document.removeEventListener('keydown', onKey);
      sc.remove();
      resolve(val);
    };
    const onKey = e => {
      if (e.key === 'Escape') close(kind === 'prompt' ? null
                                  : kind === 'alert' ? undefined : false);
      else if (e.key === 'Enter' && (!inp || document.activeElement === inp
                                          || document.activeElement.tagName === 'BUTTON'))
        close(kind === 'prompt' ? inp.value
             : kind === 'alert' ? undefined : true);
    };
    document.addEventListener('keydown', onKey);
    sc.onclick = e => { if (e.target === sc) close(
      kind === 'prompt' ? null : kind === 'alert' ? undefined : false); };
    m.querySelector('.arui-dlg-ok').onclick = () => close(
      kind === 'prompt' ? (inp ? inp.value : '')
      : kind === 'alert' ? undefined : true);
    const cancel = m.querySelector('.arui-dlg-cancel');
    if (cancel) cancel.onclick = () => close(
      kind === 'prompt' ? null : false);
  });
}
const aruiConfirm = (msg, opts={}) =>
  _aruiDialog({ title: opts.title || 'Are you sure?', message: msg,
                kind: 'confirm', danger: opts.danger, okText: opts.okText });
const aruiAlert = (msg, opts={}) =>
  _aruiDialog({ title: opts.title || 'Heads up', message: msg,
                kind: 'alert', okText: opts.okText });
const aruiPrompt = (msg, opts={}) =>
  _aruiDialog({ title: opts.title || 'Input',
                message: msg, kind: 'prompt',
                defaultValue: opts.defaultValue || '',
                okText: opts.okText });

/* ── Paper Mode flip + proposal + onboard + revert ────────────────────── */

async function openPaperProposal() {
  const ok = await aruiConfirm(
    'I\'ll consult the council in the background — they\'ll be honest ' +
    'about whether this research is ready to write up. You can keep ' +
    'researching while they work; I\'ll notify you when the assessment ' +
    'is ready.',
    { title: 'Start a Paper Proposal?', okText: 'Start assessment' });
  if (!ok) return;
  // kick off the async proposal
  let resp;
  try { resp = await post('/paper/proposal/start', {}); }
  catch (e) { await aruiAlert('Could not start proposal: ' + e); return; }
  if (!resp || !resp.proposal_id) {
    await aruiAlert('Proposal not started.'); return;
  }
  // Show a watcher modal that polls until ready
  _watchPaperProposal(resp.proposal_id);
}

function _watchPaperProposal(pid) {
  const sc = el('div', 'mscrim');
  const m = el('div', 'modal');
  m.innerHTML =
    `<div class="modal-hd"><h2>Council is assessing the project</h2>` +
    `<button class="iconbtn" id="ppx">✕</button></div>` +
    `<div class="modal-sub">Reviewers run in parallel; this usually takes ` +
    `2-5 minutes. You can close this and keep working — the Summary feed ` +
    `will show when the proposal is ready.</div>` +
    `<div class="skel" style="height:80px;margin-top:14px"></div>`;
  sc.append(m); document.body.append(sc);
  sc.onclick = e => { if (e.target === sc) sc.remove(); };
  m.querySelector('#ppx').onclick = () => sc.remove();
  const poll = async () => {
    try {
      const p = await api('/paper/proposal/' + pid);
      if (p && p.status === 'ready') {
        sc.remove();
        _showPaperProposalResults(p);
        return;
      }
    } catch (e) { /* keep polling */ }
    setTimeout(poll, 4000);
  };
  setTimeout(poll, 3000);
}

function _showPaperProposalResults(p) {
  const sc = el('div', 'mscrim');
  const m = el('div', 'modal modal-proposal');
  const responses = p.council_responses || {};
  const reviewers = Object.keys(responses);
  const recCounts = {proceed_to_paper: 0, keep_researching: 0, pivot: 0};
  reviewers.forEach(r => {
    const rec = responses[r] && responses[r].recommendation;
    if (rec && recCounts[rec] !== undefined) recCounts[rec]++;
  });
  const proceed = recCounts.proceed_to_paper;
  const cols = reviewers.map(rev => {
    const x = responses[rev] || {};
    const rec = x.recommendation || 'unknown';
    const recCls = rec === 'proceed_to_paper' ? 'ok'
      : rec === 'pivot' ? 'warn' : 'bad';
    const claims = (x.claims || []).slice(0, 3).map(c =>
      `<div class="prop-claim"><b>${esc(c.title || '')}</b> ` +
      `<span class="prop-strength">${esc(c.evidence_strength || '')}</span>` +
      `<div class="prop-claim-sum">${esc(c.summary || '')}</div></div>`
    ).join('');
    const flags = (x.red_flags || []).slice(0, 3).map(f =>
      `<li>${esc(f)}</li>`).join('');
    return `<div class="prop-col">` +
      `<div class="prop-rev">${esc(rev)}</div>` +
      `<div class="prop-rec ${recCls}">${esc(rec.replace(/_/g,' '))}</div>` +
      `<div class="prop-section-h">Novelty</div>` +
      `<div>${esc(x.novelty || '—')}: ${esc(x.novelty_rationale || '')}</div>` +
      `<div class="prop-section-h">Claims</div>${claims}` +
      `<div class="prop-section-h">Red flags</div><ul>${flags}</ul>` +
      `<div class="prop-rationale">${esc(x.rationale_md || '')}</div>` +
      `</div>`;
  }).join('');
  m.innerHTML =
    `<div class="modal-hd"><h2>Paper Proposal — council assessment</h2>` +
    `<button class="iconbtn" id="ppx2">✕</button></div>` +
    `<div class="modal-sub"><b>${proceed}/${reviewers.length}</b> ` +
    `say <code>proceed_to_paper</code>. Read each reviewer below — ` +
    `dissent is shown intentionally.</div>` +
    `<div class="prop-grid">${cols}</div>` +
    `<div class="modal-actions">` +
    `<button class="btn" id="pp-keep">Keep researching</button>` +
    `<button class="btn pri" id="pp-proceed">Proceed to Paper Mode →</button>` +
    `</div>`;
  sc.append(m); document.body.append(sc);
  sc.onclick = e => { if (e.target === sc) sc.remove(); };
  m.querySelector('#ppx2').onclick = () => sc.remove();
  m.querySelector('#pp-keep').onclick = () => sc.remove();
  m.querySelector('#pp-proceed').onclick = () => {
    sc.remove();
    openPaperOnboard(p.id);
  };
}

async function openPaperOnboard(proposalId) {
  const sc = el('div', 'mscrim');
  const m = el('div', 'modal');
  // Sensible defaults: NeurIPS next, 30d out
  const t = new Date(); t.setDate(t.getDate() + 30);
  m.innerHTML =
    `<div class="modal-hd"><h2>Onboard the paper</h2>` +
    `<button class="iconbtn" id="onx">✕</button></div>` +
    `<div class="modal-sub">One-time setup so the Author Agent knows ` +
    `what kind of paper you're writing.</div>` +
    `<div class="onb-field"><label class="onb-lbl">Target venue</label>` +
    `<select class="onb-in" id="po-venue">` +
    `<option>NeurIPS 2026</option><option>ICML 2026</option>` +
    `<option>ICLR 2026</option><option>CVPR 2026</option>` +
    `<option>ACL 2026</option><option>EMNLP 2026</option>` +
    `<option>Workshop</option></select></div>` +
    `<div class="onb-field"><label class="onb-lbl">Submission deadline (UTC)</label>` +
    `<input class="onb-in" type="datetime-local" id="po-deadline" ` +
    `value="${t.toISOString().slice(0,16)}"/></div>` +
    `<div class="onb-field"><label class="onb-lbl">Author name</label>` +
    `<input class="onb-in" id="po-author-name" placeholder="Francois Chaubard"/></div>` +
    `<div class="onb-field"><label class="onb-lbl">Author affiliation</label>` +
    `<input class="onb-in" id="po-author-aff" placeholder="Stanford"/></div>` +
    `<div class="onb-field"><label class="onb-check"><input type="checkbox" id="po-anon" checked/> Anonymize for review</label></div>` +
    `<div class="onb-note" style="color:var(--muted);font-size:11px;margin:6px 0 10px">` +
      `The ablation runs needed are the ablation runs needed — we run until the paper is publishable. ` +
      `No GPU-hour cap.</div>` +
    `<div class="modal-actions">` +
    `<button class="btn" id="po-cancel">Cancel</button>` +
    `<button class="btn pri" id="po-go">Save & start →</button></div>`;
  sc.append(m); document.body.append(sc);
  sc.onclick = e => { if (e.target === sc) sc.remove(); };
  m.querySelector('#onx').onclick = () => sc.remove();
  m.querySelector('#po-cancel').onclick = () => sc.remove();
  m.querySelector('#po-go').onclick = async () => {
    const dl = m.querySelector('#po-deadline').value;
    const meta = {
      venue: m.querySelector('#po-venue').value,
      deadline_iso: dl ? new Date(dl + 'Z').toISOString() : '',
      anonymize: m.querySelector('#po-anon').checked,
      authors: [{ name: m.querySelector('#po-author-name').value,
                  affiliation: m.querySelector('#po-author-aff').value }],
    };
    const r = await post('/paper/enter', { meta, proposal_id: proposalId });
    if (r.status === 'entered_paper_mode' || r.status === 'already_in_paper_mode') {
      sc.remove();
      // Reload to pick up new mode + paper tab
      try { S.mode = await api('/mode'); } catch (e) {}
      setView('latex');
    } else {
      await aruiAlert('Could not enter paper mode: ' + (r.detail || ''));
    }
  };
}

async function openRevertModal() {
  const sc = el('div', 'mscrim');
  const m = el('div', 'modal');
  m.innerHTML =
    `<div class="modal-hd"><h2>Revert to research mode?</h2>` +
    `<button class="iconbtn" id="rvx">✕</button></div>` +
    `<div class="modal-sub">The Author Agent stops, in-flight paper runs ` +
    `pause, the paper draft is kept untouched. The research agent resumes. ` +
    `Tell us briefly why so the snapshot is meaningful — and so the ` +
    `research agent's resume prompt knows what happened.</div>` +
    `<textarea class="onb-in" id="rv-reason" rows="4" ` +
    `placeholder="e.g. 'Two of three ablations regressed; pivoting to a different objective.'"></textarea>` +
    `<div class="modal-actions">` +
    `<button class="btn" id="rv-cancel">Cancel</button>` +
    `<button class="btn pri danger" id="rv-go">Revert to research</button></div>`;
  sc.append(m); document.body.append(sc);
  sc.onclick = e => { if (e.target === sc) sc.remove(); };
  m.querySelector('#rvx').onclick = () => sc.remove();
  m.querySelector('#rv-cancel').onclick = () => sc.remove();
  m.querySelector('#rv-go').onclick = async () => {
    const reason = (m.querySelector('#rv-reason').value || '').trim();
    if (reason.length < 5) {
      await aruiAlert('A short reason is required (1+ sentence).'); return;
    }
    const r = await post('/paper/revert', { reason });
    if (r.status === 'reverted') {
      sc.remove();
      try { S.mode = await api('/mode'); } catch (e) {}
      setView('dashboard');
    } else {
      await aruiAlert('Could not revert: ' + (r.detail || ''));
    }
  };
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
    it.onclick = () => { closeMenu(); setView(k); };
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
  // Rail tab set is driven by the VIEW, not the mode:
  //   Dashboard view      → Research agent only (no Author agent)
  //   Write-the-paper view → Author agent only (no Research agent)
  // Other views (Analysis, Lessons, …) don't get a rail at all.
  const isPaperView = (S.view === 'latex');
  const TABS = isPaperView
    ? [['summary','Summary'], ['author','Author agent'], ['sessions','Sessions']]
    : [['summary','Summary'], ['live','Research agent'], ['sessions','Sessions']];
  // Keep S.railTab valid for the current view (so a stale 'live' on the
  // paper view, or stale 'author' on the dashboard, falls back to summary).
  if (!TABS.some(([k]) => k === S.railTab)) S.railTab = TABS[0][0];
  TABS.forEach(([k, lbl]) => {
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
  // Guard: when we're not on the dashboard view (e.g. user is on Write-the-paper,
  // Analysis, Settings, etc.), #tscroll doesn't exist and the old code would
  // throw "Cannot set properties of null (setting 'innerHTML')". SSE events fire
  // regardless of active view so this guard is required.
  if (!document.getElementById('tscroll')) return;
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
        aruiPrompt(
          'Why are you removing it? (sent to the agent so it learns your ' +
          'preference)',
          { title: 'Remove idea from queue', okText: 'Remove' })
          .then(why => { if (why !== null) deleteIdea(r.id, why); });
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
let _termHost = null;        // active xterm.js wrapper for the rail
function stopTermPoll() {
  if (_termTimer) { clearInterval(_termTimer); _termTimer = null; }
  if (_termHost) { try { _termHost.dispose(); } catch (e) {} _termHost = null; }
}

function paintRail() {
  document.querySelectorAll('.rail-tab').forEach(b =>
    b.classList.toggle('on', b.dataset.tab === S.railTab));
  const c = document.getElementById('railcontent');
  if (!c) return;
  if (S.railTab === 'author') {
    if (!document.getElementById('authorterm-host')) { stopTermPoll(); renderAuthorLive(c); }
  } else if (S.railTab === 'live') {
    if (!document.getElementById('term-host')) { stopTermPoll(); renderLive(c); }
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

/* ── xterm.js rail terminal ───────────────────────────────────────────── */
// One factory shared by the Research-Agent rail and the Author-Agent
// rail. Returns
//   { container, startStream(), stopStream(), writeText(text),
//     dispose(), fallback }.
//
// What the user gets:
//   - REAL terminal: server streams raw bytes (with ANSI escapes —
//     colors, cursor moves, in-place spinners) via /api/agent/raw and
//     xterm.js renders them natively. No more `t.reset() + t.write()`
//     polling that wiped selection + cursor every 2.5 s.
//   - native text selection (drag, even across line-wraps), Cmd/Ctrl-C
//     copies, double-click word, triple-click line
//   - URLs are clickable (Ctrl/Cmd-click opens in a new tab) via the
//     web-links addon
//   - every keystroke types directly into the agent's tmux via
//     POST /api/agent/keys, fired IMMEDIATELY (no 90 ms coalesce). Feels
//     like SSH into the box.
//   - F-keys + Cmd-shortcuts that the browser would normally swallow
//     are intercepted via attachCustomKeyEventHandler and passed
//     through to tmux.
//
// If xterm.js failed to load (CDN blocked, offline), we fall back to a
// plain <pre> so the rail still works, just with the old read-only UX.
function createRailTerm(session) {
  const container = el('div', 'xterm-wrap');
  container.dataset.session = session;
  if (!window.Terminal) {
    container.innerHTML =
      '<pre class="term" data-fallback="1">loading terminal…</pre>';
    const pre = container.firstChild;
    return { container, fallback: true,
             startStream: () => {},
             stopStream: () => {},
             writeText: (t) => { pre.textContent = t || ''; },
             dispose: () => {} };
  }
  const t = new window.Terminal({
    // Raw mode: backend pipes the program's actual bytes (ANSI codes
    // already include \r\n where appropriate). convertEol would
    // double-CR-LF those, so keep it OFF.
    convertEol: false,
    cursorBlink: true,
    fontFamily: "'SF Mono','Menlo','Consolas',monospace",
    fontSize: 12,
    lineHeight: 1.15,
    theme: {
      background: '#0a0c0f', foreground: '#cdd3da',
      cursor: '#cdd3da', cursorAccent: '#0a0c0f',
      selectionBackground: '#6366F155',
      black: '#000', red: '#F87171', green: '#34D399',
      yellow: '#FBBF24', blue: '#60A5FA', magenta: '#C084FC',
      cyan: '#22D3EE', white: '#E6E8EB',
      brightBlack: '#6b7280', brightRed: '#FCA5A5',
      brightGreen: '#86EFAC', brightYellow: '#FDE68A',
      brightBlue: '#93C5FD', brightMagenta: '#D8B4FE',
      brightCyan: '#67E8F9', brightWhite: '#F9FAFB',
    },
    scrollback: 8000,
    allowProposedApi: true,
  });
  const FitAddon = window.FitAddon && window.FitAddon.FitAddon;
  const WebLinksAddon =
    window.WebLinksAddon && window.WebLinksAddon.WebLinksAddon;
  const fitAddon = FitAddon ? new FitAddon() : null;
  if (fitAddon) t.loadAddon(fitAddon);
  if (WebLinksAddon) t.loadAddon(new WebLinksAddon((e, uri) =>
    window.open(uri, '_blank', 'noopener')));
  t.open(container);
  const refit = () => { try { fitAddon && fitAddon.fit(); } catch (e) {} };
  setTimeout(refit, 50);
  let ro = null;
  try {
    ro = new ResizeObserver(refit);
    ro.observe(container);
  } catch (e) { /* not all browsers */ }

  /* ── Tell the server to resize tmux to match xterm dimensions.
     Without this, Claude Code renders its UI at tmux's spawn size
     (210x52) but the browser's xterm is narrower, so every status
     line wraps mid-character and the terminal looks garbled.

     xterm fires onResize after FitAddon.fit() with the new (cols,
     rows). We debounce by 200ms so a drag-resize doesn't fire 60
     POSTs/sec, and POST to /api/agent/resize which calls
     `tmux resize-window` + sends Ctrl-L so Claude redraws clean. */
  let _resizeTimer = null, _lastSize = '';
  const pushSize = (cols, rows) => {
    const key = cols + 'x' + rows;
    if (key === _lastSize) return;
    _lastSize = key;
    if (_resizeTimer) clearTimeout(_resizeTimer);
    _resizeTimer = setTimeout(() => {
      _resizeTimer = null;
      post('/agent/resize', { session, cols, rows }).catch(() => {});
    }, 200);
  };
  t.onResize(({ cols, rows }) => pushSize(cols, rows));
  // After the initial fit (which fires onResize), Claude may already
  // have drawn at the old size. Force one more push + Ctrl-L just to
  // be sure the dimensions match before the user looks at the term.
  setTimeout(() => {
    if (t.cols && t.rows) pushSize(t.cols, t.rows);
  }, 600);

  /* ── Keystroke passthrough ───────────────────────────────────────────
     Every key the user types goes to /api/agent/keys IMMEDIATELY.
     - Single chars and special keys: one POST each.
     - Multi-char paste: one POST with the whole buffer (xterm onData
       already batches paste into a single call so we don't need to
       coalesce on our side).
     - No 90 ms delay. */
  const sendNow = (data) => {
    // Fire-and-forget: if the network coughs, the next keystroke goes
    // through anyway. Don't await — we want zero latency feel.
    post('/agent/keys', { session, data }).catch(() => {});
  };
  t.onData((data) => { sendNow(data); });

  /* Browser-intercepted keys: let F-keys + most Cmd/Ctrl shortcuts go
     through to tmux instead of triggering the browser. We deliberately
     allow Cmd/Ctrl-C (copy), Cmd/Ctrl-V (paste — xterm's paste handler
     will emit it via onData), Cmd-T (new tab), Cmd-W (close tab),
     Cmd-Q (quit), Cmd-R (reload), and the user's other browser
     shortcuts — pressing Cmd-T in a terminal SHOULD open a tab. */
  t.attachCustomKeyEventHandler((ev) => {
    if (ev.type !== 'keydown') return true;
    const k = ev.key, isMod = ev.metaKey || ev.ctrlKey;
    // Allow these to behave normally (copy, paste, new tab etc).
    if (isMod && /^[cvtnwqrlf]$/i.test(k)) return true;
    // F1-F12 → swallow browser default + forward as escape sequence.
    if (/^F([1-9]|1[0-2])$/.test(k)) {
      const fnum = parseInt(k.slice(1), 10);
      // xterm/vt220 F-key sequences
      const map = {
        1: '\x1bOP', 2: '\x1bOQ', 3: '\x1bOR', 4: '\x1bOS',
        5: '\x1b[15~', 6: '\x1b[17~', 7: '\x1b[18~', 8: '\x1b[19~',
        9: '\x1b[20~', 10: '\x1b[21~', 11: '\x1b[23~', 12: '\x1b[24~',
      };
      sendNow(map[fnum] || '');
      ev.preventDefault();
      return false;
    }
    return true;
  });

  /* ── Raw byte stream from server ─────────────────────────────────────
     Poll /api/agent/raw for new bytes since the last offset and pass
     them to xterm.write() AS-IS. The bytes already contain ANSI escape
     sequences for colors, cursor positioning, in-place spinner
     animation, etc — xterm.js re-parses them and the rendering matches
     what `tmux attach -t agent` would show.

     We use base64 transit because raw bytes in JSON would need
     escaping; decoding to Uint8Array preserves every byte.

     Poll interval: 250 ms when active, exponential back-off to 2 s
     when there's been no new bytes — keeps the network quiet while
     the agent thinks, snappy when it's emitting output. */
  let _offset = 0, _streamTimer = null, _idleMs = 250;
  const b64ToBytes = (b64) => {
    if (!b64) return new Uint8Array(0);
    try {
      const bin = atob(b64);
      const buf = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
      return buf;
    } catch (e) { return new Uint8Array(0); }
  };
  const tick = async () => {
    try {
      const d = await api(
        '/agent/raw?session=' + encodeURIComponent(session)
        + '&offset=' + _offset);
      if (d.rotated) {
        t.reset();
        _offset = 0;
        // Agent was restarted server-side. Our last dimensions cache
        // is invalidated — force a re-push so the fresh tmux pane
        // gets sized to our actual rail width instead of the 120x40
        // spawn default.
        _lastSize = '';
        if (t.cols && t.rows) pushSize(t.cols, t.rows);
      }
      const bytes = b64ToBytes(d.chunk);
      if (bytes.length) {
        t.write(bytes);
        _offset = d.offset || (_offset + bytes.length);
        _idleMs = 250;          // got data, stay snappy
      } else {
        // Make sure we know where the file ends even if we got 0 bytes
        // (avoids re-fetching the same offset forever).
        if (typeof d.offset === 'number') _offset = d.offset;
        _idleMs = Math.min(_idleMs * 1.5, 2000);
      }
    } catch (e) { _idleMs = Math.min(_idleMs * 1.5, 2000); }
    _streamTimer = setTimeout(tick, _idleMs);
  };
  const start = () => { if (!_streamTimer) tick(); };
  const stop  = () => {
    if (_streamTimer) { clearTimeout(_streamTimer); _streamTimer = null; }
  };

  return {
    container, fallback: false,
    startStream: start,
    stopStream:  stop,
    // Escape hatch — e.g. fallback message when stream is offline.
    writeText(text) { t.write(text || ''); },
    dispose() {
      stop();
      try { ro && ro.disconnect(); } catch (e) {}
      try { t.dispose(); } catch (e) {}
    },
  };
}

/* ── Author Agent rail (paper mode) ─────────────────────────────────────── */
function renderAuthorLive(c) {
  c.innerHTML =
    '<div class="author-rail-hd">' +
      '<span>📝 Author Agent</span>' +
      '<span class="author-rail-status" id="author-status">checking…</span>' +
      '<button class="btn xs" id="author-restart" title="Kill the author tmux and spawn a fresh one">↻ restart</button>' +
    '</div>' +
    '<div class="rail-agent-desc">' +
      '<b>What it does:</b> autonomous Claude Code loop that owns the paper ' +
      'end-to-end — plans cross-dataset ablations, queues runs (via ' +
      '<code>/paper/runs/queue</code>), kills divergers, integrates each ' +
      'finished run into <code>main.tex</code> automatically, maintains ' +
      '<code>claims.md</code> + <code>refs.bib</code>, recompiles the PDF. ' +
      'Files <i>strategic</i> decisions only — citations, kill_claim, ' +
      'approve_text, approve_figure. Ablation runs do NOT need your ' +
      'approval. The <b>PI agent</b> watches it every hour and nudges if it ' +
      'drifts. Click in the terminal below and type — keystrokes go ' +
      'straight to the Claude session.' +
    '</div>';
  const termHost = createRailTerm('author');
  termHost.container.id = 'authorterm-host';
  c.appendChild(termHost.container);
  if (_termHost) { try { _termHost.dispose(); } catch (e) {} }
  _termHost = termHost;
  const stat = document.getElementById('author-status');
  document.getElementById('author-restart').onclick = async () => {
    const ok = await aruiConfirm('Restart the Author Agent tmux? Anything mid-response will be lost.',
      { title: 'Restart Author Agent' });
    if (!ok) return;
    stat.textContent = 'restarting…';
    await post('/paper/author/restart', {});
    stat.textContent = 'restarted';
  };
  // Start streaming raw pane bytes (ANSI included) — this is what makes
  // the terminal feel real. The /paper/author/terminal full-text poll is
  // gone; instead we just hit /api/agent/raw incrementally inside
  // termHost.startStream().
  termHost.startStream();
  // Separate light status poll just for the "● running / ○ not running"
  // indicator. 4s is plenty — this isn't on the critical path of typing.
  const statusTick = async () => {
    try {
      const d = await api('/agent/raw?session=author&offset=999999999');
      const running = !!d.alive;
      stat.textContent = running ? '● running' : '○ not running';
      stat.style.color = running ? 'var(--ok)' : 'var(--muted)';
    } catch (e) { /* keep last */ }
  };
  statusTick();
  _termTimer = setInterval(statusTick, 4000);
}

function renderLive(c) {
  const isPaperMode = (S.mode && S.mode.mode === 'paper');
  const pausedBanner = isPaperMode ? (
    '<div class="rail-paused-banner">' +
      '⏸ <b>Research agent is paused</b> because you\'re in paper mode. ' +
      'Showing the last tmux scrollback below for reference. ' +
      'Flip to Research mode (toggle on Write-the-paper page) to resume.' +
    '</div>'
  ) : '';
  c.innerHTML =
    '<div class="author-rail-hd">' +
      '<span>🔬 Research Agent</span>' +
      '<span class="author-rail-status" id="agent-status">checking…</span>' +
      '<button class="btn xs" id="agent-restart" ' +
        'title="Kill the research tmux and spawn a fresh one — fixes a ' +
        'session stuck on the Claude Code consent prompt">↻ restart</button>' +
    '</div>' +
    '<div class="rail-agent-desc">' +
      '<b>What it does:</b> autonomous Claude Code loop that reads ' +
      '<code>ideas.md</code>, decides what to try next, launches training ' +
      'runs, kills divergers, and writes <code>lessons.md</code>. The ' +
      '<b>PI agent</b> watches it every hour and types short nudges into ' +
      'this tmux when GPUs go idle, runs diverge, or it ignores the ' +
      'council. Click the terminal and type — keystrokes go straight to ' +
      'the Claude session. <b>Job: discovery</b>; pauses automatically ' +
      'when you flip to paper mode.' +
    '</div>' +
    pausedBanner;
  const termHost = createRailTerm('agent');
  termHost.container.id = 'term-host';
  c.appendChild(termHost.container);
  if (_termHost) { try { _termHost.dispose(); } catch (e) {} }
  _termHost = termHost;
  const stat = document.getElementById('agent-status');
  document.getElementById('agent-restart').onclick = async () => {
    const ok = await aruiConfirm(
      'Restart the Research Agent tmux? Anything mid-response will be lost. ' +
      'Use this if the session got stuck on the Claude Code "Bypass ' +
      'Permissions" consent screen.',
      { title: 'Restart Research Agent' });
    if (!ok) return;
    stat.textContent = 'restarting…';
    const r = await post('/agent/restart', {});
    stat.textContent = (r && r.ok) ? 'restarted' : 'restart failed';
    if (r && !r.ok) {
      aruiAlert(r.error || 'unknown error',
        { title: 'Could not restart agent' });
    }
  };
  // Start streaming raw pane bytes — ANSI escapes included. xterm.js
  // renders them natively, so colors / spinners / cursor moves all
  // match what `tmux attach -t agent` would show.
  termHost.startStream();
  // Separate light status poll just for the "● running / ○ not running"
  // indicator. 4s is plenty.
  const statusTick = async () => {
    try {
      const d = await api('/agent/raw?session=agent&offset=999999999');
      const running = !!d.alive;
      if (stat) {
        stat.textContent = running ? '● running' : '○ not running';
        stat.style.color = running ? 'var(--ok)' : 'var(--muted)';
      }
    } catch (e) { /* keep last */ }
  };
  statusTick();
  _termTimer = setInterval(statusTick, 4000);
}

function renderSessions(c) {
  c.innerHTML =
    '<div class="rail-agent-desc">' +
      '<b>What this is:</b> live tmux output for any individual training run ' +
      '(research <code>diff_*</code> or paper <code>pr-*</code>). Pick a ' +
      'session above to tail its log — useful when a specific run is ' +
      'misbehaving and you want raw stdout/stderr instead of the aggregated ' +
      'metric view.' +
    '</div>' +
    '<div class="sess-wrap">' +
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
  // ── Paper-mode brief takes precedence when we're in paper mode ──────
  const inPaper = (S.mode && S.mode.mode === 'paper');
  if (inPaper) {
    const meta = (S.mode.meta) || {};
    const days = S.mode.days_till_deadline;
    const paperRuns = S.runs.filter(r => r.context === 'paper');
    const pQ = paperRuns.filter(r => r.status === 'queued').length;
    const pR = paperRuns.filter(r => r.status === 'running').length;
    const pD = paperRuns.filter(r =>
      ['kept','success','done'].includes(r.status)).length;
    const pX = paperRuns.filter(r =>
      ['crashed','failed','error'].includes(r.status)).length;
    let alert = '';
    if (pX && !pD && (pX >= paperRuns.length * 0.6)) {
      alert = `<div class="brief-alert">⚠ <b>${pX} of ${paperRuns.length} ablation runs crashed.</b> ` +
        `<a href="#" id="brief-rescaffold">Patch their commands & re-queue →</a></div>`;
    }
    let breakdown = '';
    if (paperRuns.length) {
      breakdown = `<p><span class="big">${pD}</span> done, ` +
        `<span class="big">${pR}</span> running, ` +
        `<span class="big">${pQ}</span> queued` +
        (pX ? `, <span class="big" style="color:var(--bad)">${pX}</span> crashed` : '') +
        ` <span style="color:var(--muted);font-size:11px">across ${paperRuns.length} paper ablations</span></p>`;
    } else {
      breakdown = `<p style="color:var(--muted);font-size:12px">No paper ablations queued yet. ` +
        `Open <b>Paper Plan</b> and click <b>Re-scaffold</b> to populate.</p>`;
    }
    brief.innerHTML =
      `<h3><span class="dot live" style="color:#A78BFA"></span>📝 Paper mode — ${esc(meta.venue || 'Paper')}</h3>` +
      `<p style="color:var(--text-2);font-size:12px;margin:2px 0 8px">` +
        `Switched from research to paper mode` +
        (days != null ? ` · <b>${days.toFixed ? days.toFixed(1) : days}d</b> till deadline` : '') +
        `.</p>` +
      breakdown +
      alert;
    const rs = document.getElementById('brief-rescaffold');
    if (rs) rs.onclick = async (e) => {
      e.preventDefault();
      const r = await post('/paper/scaffold', {});
      aruiAlert(`Patched & re-queued ${r.requeued_crashed || 0} crashed runs ` +
                `(${r.backfilled_cmd || 0} cmds backfilled).`,
                { title: 'Re-queued' });
    };
    return;
  }
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
  const inPaperMode = (S.mode && S.mode.mode === 'paper');
  const otherTab = inPaperMode ? '<b>Author agent</b>' : '<b>Research agent</b>';
  const desc = el('div', 'rail-agent-desc',
    '<b>What this is:</b> at-a-glance project status — current mode, ' +
    'frontier results, recent agent activity. Not an agent — no chat here. ' +
    `Use the ${otherTab} tab to steer the live agent, or the ` +
    '<b>Sessions</b> tab to inspect any single run\'s logs.');
  c.append(desc);
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
      const ok = await aruiConfirm(
        'Its tmux session will be terminated and the run marked crashed.',
        { title: 'Kill this run?', danger: true, okText: 'Kill run' });
      if (!ok) return;
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
  // Always-on multi-metric plot grid — used to be a "Show plots" button +
  // expand. The button-gating was unnecessary friction; the panels are
  // lazy-loaded via IntersectionObserver so off-screen plots don't fetch
  // anything until they scroll into view.
  bd.append(el('div', 'dr-h2', 'All plots'));
  const vapWrap = el('div', 'vap-wrap');
  bd.append(vapWrap);
  (async () => {
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
    // Default slots first (with '(not logged)' placeholders when missing)
    defaults.forEach(k => grid.append(_vapPanel(runId, k, runKeys.includes(k))));
    if (extras.length) {
      vapWrap.append(el('div', 'dr-h2', 'Other metrics'));
      const search = el('input', 'vap-search');
      search.placeholder = 'filter (e.g. token, mem)…';
      vapWrap.append(search);
      // Render every extra key directly; lazy IntersectionObserver inside
      // _vapPanel ensures only visible plots fetch data.
      const extraGrid = el('div', 'vap-grid'); vapWrap.append(extraGrid);
      const renderExtras = (filter) => {
        extraGrid.innerHTML = '';
        extras.filter(k => !filter
                          || k.toLowerCase().includes(filter.toLowerCase()))
              .forEach(k => extraGrid.append(_vapPanel(runId, k, true)));
      };
      renderExtras('');
      search.oninput = () => renderExtras(search.value);
    }
  })();
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
  // Route to the Author Agent when the rail is on its tab; otherwise to
  // the research agent (existing behavior).
  if (S.railTab === 'author') {
    const r = await post('/paper/author/send', { text });
    if (r && r.ok === false) {
      aruiAlert('Could not deliver to Author Agent: ' + (r.detail || 'unknown'),
        { title: 'Author Agent unreachable' });
    }
    return;
  }
  // Show the user's message in the conversation panel IMMEDIATELY — don't
  // wait for the SSE round-trip — so the user gets confirmation it went.
  const userMsg = { id: 'tmp-' + Date.now(), role: 'researcher',
    content: text, created_at: new Date().toISOString() };
  S.chat.push(userMsg);
  paintConvo();
  const r = await post('/agent/send', { text });
  if (r && r.ok === false) {
    S.chat.push({ role: 'agent', created_at: new Date().toISOString(),
      content: '⚠ could not deliver — ' + (r.error || 'no agent session') +
      ' · click "Restart agent" above to relaunch the Claude Code session.' });
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

// Tracks the last successful runs_changed (or polling-fallback) refresh.
// The polling fallback uses this to decide whether SSE is doing its job.
let _lastRunsRefreshAt = 0;

// Centralized refresh + repaint for runs/ideas/project/gpus/events.
// Called from both the SSE runs_changed handler and the polling fallback.
async function refreshDashboardLive(reason) {
  try {
    const [proj, runs, ideas, gpus] = await Promise.all([
      api('/project'), api('/runs'), api('/ideas'), api('/gpus')]);
    S.project = proj; S.runs = runs; S.ideas = ideas; S.gpus = gpus;
  } catch (e) { return false; }
  _lastRunsRefreshAt = Date.now();
  // Always repaint the parts that exist on the current view. If the user
  // happens to be on Analysis or Lessons we still keep the data fresh so
  // when they navigate back to Dashboard the row is already there.
  if (S.view === 'dashboard') {
    try { paintHero(); paintStats(); paintTable(); paintRail();
      document.querySelector('.hdr')?.replaceWith(header()); paintGpus();
    } catch (e) { /* ignore paint errors so the next tick can recover */ }
  } else if (S.view === 'analysis') {
    try { if (typeof paintAnalysisTable === 'function') paintAnalysisTable();
    } catch (e) {}
  }
  return true;
}

function streams() {
  const m = new EventSource('/api/stream/metrics');
  m.addEventListener('metrics_changed', e => {
    try {
      const { run_id } = JSON.parse(e.data);
      // Debounced: coalesce multiple metrics_changed events into one
      // panel refresh ~every 1.2s so running runs don't make the chart
      // flicker by rebuilding at 4Hz.
      if (typeof scheduleLiveRefresh === 'function') {
        scheduleLiveRefresh(run_id);
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
      await refreshDashboardLive('sse');
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

  // ──────────────────────────────────────────────────────────────────────
  // POLLING FALLBACK — covers every case where SSE silently fails:
  //   • cloudflared quick-tunnels buffer chunked responses for ~30s
  //   • proxies / corporate networks strip text/event-stream
  //   • the EventSource silently disconnects and reconnect storms it
  //   • the backend restarted mid-session so the SSE pipe is dead
  // Every 6s, if no SSE refresh has happened in the last 5s, do a fetch.
  // Adds ~0.5–1 KB / 6s of background traffic — negligible vs. having a
  // dashboard that doesn't update.
  // ──────────────────────────────────────────────────────────────────────
  const POLL_INTERVAL_MS = 6000;
  const STALE_THRESHOLD_MS = 5000;
  let _pollHidden = false;
  document.addEventListener('visibilitychange', () => {
    _pollHidden = (document.visibilityState === 'hidden');
    // On regaining visibility, do an immediate refresh — the user has
    // probably been away for a while and wants the latest state NOW.
    if (!_pollHidden) refreshDashboardLive('visible');
  });
  setInterval(() => {
    if (_pollHidden) return;                 // don't poll backgrounded tabs
    if (Date.now() - _lastRunsRefreshAt < STALE_THRESHOLD_MS) return;
    refreshDashboardLive('poll');
  }, POLL_INTERVAL_MS);

  // Log SSE health to console so it's easy to see in DevTools whether
  // EventSource is dropping (this is the #1 thing to check if the
  // "doesn't update" symptom comes back).
  [['metrics', m], ['events', ev], ['chat', ch]].forEach(([name, src]) => {
    src.addEventListener('open',
      () => console.log(`[sse] ${name} connected`));
    src.addEventListener('error',
      () => console.log(`[sse] ${name} disconnected — will auto-reconnect ` +
        '(polling fallback covers the gap)'));
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
  ['help', 'pi_help',
    'The PI ("Principal Investigator") is a small LLM that wakes up on a ' +
    'schedule, reads the project state, and types short nudges into the ' +
    'live agent\'s tmux when something looks off. In RESEARCH mode it ' +
    'nudges the research agent ("GPU 3 idle — launch the top idea", "kill ' +
    'pr-x42, it\'s diverging", "you\'re ignoring the council\'s rerank"). ' +
    'In PAPER mode it switches to nudging the AUTHOR agent ("5 runs ' +
    'finished but no commits — integrate them", "claim 2 has 0 queued ' +
    'runs", "you\'ve only used 1 dataset — top-tier papers need ≥3", ' +
    '"build is stale — recompile"). It does NOT run experiments, edit ' +
    'LaTeX, or kill runs itself; it just types messages. Be sparing on ' +
    'cadence — each nudge interrupts the agent.'],
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
    if (type === 'help') {
      // Static descriptive paragraph (no input). Useful for explaining
      // what a section / agent does in the onboarding + settings form.
      const help = el('div', 'onb-help', extra);
      form.append(help);
      return;
    }
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

function onboarding(initial = {}) {
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
  // Set explicit type="button" so this never tries to submit a parent form
  // (was defaulting to submit-button semantics in some browsers; combined
  // with the async defaults fetch this could swallow the first click).
  bpb.type = 'button';
  bp.append(bpa, bpb); wrap.append(bp);

  const { form, inp } = buildSettingsForm({ initial });
  wrap.append(form);

  // Attach the click handler IMMEDIATELY — before app.append + the async
  // /api/onboarding/defaults fetch. The previous order left a window
  // where the button was live in the DOM but had no handler yet, which
  // is what caused "first click does nothing, second click works".
  const parseAndFill = () => {
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
      // Notify any listeners (and prevent the async defaults fetch from
      // clobbering — `!inp[k].value` becomes false after this) by firing
      // both 'input' and 'change' events the way a real keystroke would.
      try { x.dispatchEvent(new Event('input', { bubbles: true })); } catch (e) {}
      try { x.dispatchEvent(new Event('change', { bubbles: true })); } catch (e) {}
      filled++;
    });
    bpb.textContent = filled
      ? `Filled ${filled} field${filled === 1 ? '' : 's'} ✓`
      : 'No matching keys';
    bpb.style.background = filled ? 'var(--ok)' : '';
    bpb.style.color = filled ? '#0b0d10' : '';
    setTimeout(() => {
      bpb.textContent = 'Parse & fill';
      bpb.style.background = '';
      bpb.style.color = '';
    }, 2200);
  };
  // Multi-event triggering: bind click + pointerdown + auto-on-paste so
  // the user gets a fill no matter which interaction Chrome decides to
  // honor first. The 200ms guard dedupes the cascade.
  let _lastFire = 0;
  const fireParseAndFill = (label) => {
    const now = Date.now();
    if (now - _lastFire < 200) return;
    _lastFire = now;
    console.log('[onb] parse&fill triggered via', label);
    parseAndFill();
  };
  // Capture phase + click — runs before any later-attached handler can
  // call stopPropagation. Defensive against future code that might
  // accidentally swallow clicks at a parent level.
  bpb.addEventListener('click', e => {
    e.preventDefault(); e.stopPropagation();
    fireParseAndFill('click');
  }, { capture: true });
  // pointerdown fires BEFORE click and before any focus-change drama —
  // works even if a textarea blur somehow eats the click event.
  bpb.addEventListener('pointerdown', e => {
    e.preventDefault();
    fireParseAndFill('pointerdown');
  });
  // AUTO-PARSE ON PASTE — most ergonomic of all: paste your .env block
  // into the textarea and the form fills itself ~250 ms later. No click
  // needed. We still keep the button for users who edit the textarea
  // line by line and want explicit control.
  bpa.addEventListener('paste', () => {
    setTimeout(() => fireParseAndFill('paste'), 250);
  });

  const foot = el('div', 'onb-foot');
  foot.append(el('div', 'onb-note',
    'This saves your project config — it does not show any demo data. The ' +
    'autonomous agent that researches your project (a real Claude Code agent ' +
    'on your GPUs) is the next milestone; until it is built the dashboard ' +
    'stays empty.'));
  const start = el('button', 'btn pri onb-start', 'Start research →');
  start.type = 'button';
  foot.append(start); wrap.append(foot);
  app.append(wrap);

  // Pre-fill editable defaults (agent_instructions etc.) from the backend.
  // Runs AFTER the Parse & fill click handler is attached, so an
  // overly-fast click is always handled correctly.
  api('/onboarding/defaults').then(defs => {
    Object.entries(defs || {}).forEach(([k, v]) => {
      if (inp[k] && !inp[k].value) inp[k].value = v;
    });
  }).catch(() => { /* keep the blank textarea */ });
  start.onclick = async () => {
    const cfg = {};
    Object.entries(inp).forEach(([k, x]) =>
      cfg[k] = x.type === 'checkbox' ? x.checked : x.value);
    // Cheap client-side sanity check — catches obvious mistakes (empty,
    // wrong-format, placeholder) before we waste 30 s of boot time only to
    // discover the token can't authenticate.
    const tok = (cfg.claude_token || '').trim();
    if (!tok || /^sk-ant-replace_?me$/i.test(tok) || tok.length < 25
        || !/^sk-ant-/i.test(tok)) {
      aruiAlert(
        "The Claude API token doesn't look right. It must start with " +
        "'sk-ant-' and be the full key from https://console.anthropic.com/" +
        " (paste the entire string, not a placeholder).",
        { title: 'Claude token looks invalid' });
      inp.claude_token?.focus();
      return;
    }
    // PRE-FLIGHT TOKEN VALIDATION. Probe every configured provider in
    // parallel and report results back. Required tokens (Claude) that
    // fail block the launch. Optional tokens (OpenAI/Gemini/Gmail/GitHub)
    // that fail prompt for "continue with this feature disabled" so the
    // user is never confused about why councils/emails go silent later.
    start.disabled = true; start.textContent = 'Checking tokens…';
    let results;
    try {
      results = await post('/onboarding/validate_tokens', cfg);
    } catch (e) {
      results = null;       // network blip → skip and proceed
    }
    if (results) {
      const proceed = await showTokenCheckResults(results);
      if (!proceed) {
        start.disabled = false; start.textContent = 'Start research →';
        return;
      }
    }
    start.textContent = 'Starting…';
    try {
      await post('/onboarding', cfg);
    } catch (e) {
      aruiAlert('Onboarding save failed: ' + e,
        { title: 'Could not save config' });
      start.disabled = false; start.textContent = 'Start research →';
      return;
    }
    // Don't slam-reload — the Claude Code agent takes 20–40 s to come up
    // (Claude Code splash → consent prompt → REPL → brief). Show a boot
    // overlay that polls the agent's tmux until we see real output, then
    // redirect. The user gets visible progress instead of a blank page.
    showAgentBootOverlay();
  };
}

/* Show the token-validation results in a modal. Returns a Promise that
 * resolves to true (user clicked Continue) or false (user clicked Fix). */
function showTokenCheckResults(results) {
  return new Promise(resolve => {
    // If everything passed (or was empty/skipped), no need to bother the user.
    const failed = Object.entries(results)
      .filter(([_, r]) => r && r.ok === false);
    if (failed.length === 0) { resolve(true); return; }
    // Claude is the only token that's actually required to launch the
    // research agent. If Claude failed, "continue" doesn't make sense.
    const claudeBad = (results.claude && results.claude.ok === false);
    const sc = el('div', 'mscrim');
    const m  = el('div', 'modal');
    const rowsHtml = Object.entries(results).map(([name, r]) => {
      const labelMap = { claude: 'Claude API token',
                         openai: 'OpenAI API token',
                         gemini: 'Gemini API token',
                         github: 'GitHub token',
                         gmail:  'Gmail app password' };
      const optional = name !== 'claude';
      let stChip, stColor;
      if (!r) { stChip = 'unknown'; stColor = '#5C636B'; }
      else if (r.skipped) { stChip = 'not configured'; stColor = '#5C636B'; }
      else if (r.ok) { stChip = 'valid ✓'; stColor = 'var(--ok)'; }
      else { stChip = 'failed ✗'; stColor = 'var(--bad)'; }
      const lat = r && r.latency_ms ? ` <span class="tc-lat">${r.latency_ms}ms</span>` : '';
      const detail = r && r.detail && !r.skipped
        ? `<div class="tc-detail">${esc(r.detail)}</div>` : '';
      const opt = optional
        ? '<span class="tc-opt">optional</span>' : '';
      return `<div class="tc-row tc-${r && r.ok ? (r.skipped ? 'skip' : 'ok') : 'bad'}">` +
        `<div class="tc-name">${esc(labelMap[name] || name)}${opt}</div>` +
        `<div class="tc-st" style="color:${stColor}">${stChip}${lat}</div>` +
        detail + '</div>';
    }).join('');
    m.innerHTML =
      '<div class="modal-hd"><h2>' +
      (claudeBad ? 'Claude token failed — fix it before continuing'
                 : 'Some optional tokens failed') +
      '</h2><button class="iconbtn" id="tc-x">✕</button></div>' +
      '<p class="modal-sub">' +
      (claudeBad
        ? 'The Research Agent needs a working Claude API token to start. ' +
          'Fix the token and try again.'
        : 'The required Claude token is good — but some optional providers ' +
          'failed. You can continue and those features (council reviews / ' +
          'emails / lit search) will be silently disabled, or fix them now.') +
      '</p>' +
      '<div class="tc-list">' + rowsHtml + '</div>' +
      '<div class="modal-actions">' +
      (claudeBad
        ? '<button class="btn pri" id="tc-fix" type="button">' +
          '← Back to fix Claude token</button>'
        : '<button class="btn pri" id="tc-cont" type="button">' +
          'Continue with failed features disabled</button>' +
          '<button class="btn" id="tc-fix" type="button">' +
          '← Back to fix</button>') +
      '</div>';
    sc.append(m); document.body.append(sc);
    const done = (proceed) => { sc.remove(); resolve(proceed); };
    m.querySelector('#tc-x').onclick = () => done(false);
    m.querySelector('#tc-fix').onclick = () => done(false);
    const cont = m.querySelector('#tc-cont');
    if (cont) cont.onclick = () => done(true);
    sc.onclick = e => { if (e.target === sc) done(false); };
  });
}

/* Full-screen "warming up" overlay shown between onboarding submission and
 * the dashboard opening. Polls /api/agent/terminal every 2 s, shows the
 * latest tmux tail + a status line that advances as we observe progress,
 * and redirects to the dashboard once the agent looks responsive.
 * A "Skip & open dashboard" button lets impatient users bail out. */
function showAgentBootOverlay() {
  const app = document.getElementById('app');
  app.className = 'onb';
  app.innerHTML =
    '<div class="boot-wrap">' +
      '<div class="boot-card">' +
        '<div class="boot-brand">autoresearcher<span>UI</span></div>' +
        '<div class="boot-spinner" aria-hidden="true"></div>' +
        '<h2 class="boot-title">Spinning up your research</h2>' +
        '<div class="boot-step" id="boot-step">Saving your config…</div>' +
        '<div class="boot-elapsed" id="boot-elapsed">0s elapsed</div>' +
        '<pre class="boot-term" id="boot-term">(waiting for the agent to ' +
        'come up…)</pre>' +
        '<div class="boot-actions">' +
          '<button class="btn" id="boot-skip" type="button">' +
          'Skip &amp; open dashboard</button>' +
        '</div>' +
        '<div class="boot-help">' +
          'The Research Agent is a real Claude Code session. First boot ' +
          'takes ~20–40&nbsp;seconds (splash screen → bypass-permissions ' +
          'consent → REPL → your research brief). Once it\'s up, you\'ll ' +
          'be dropped onto the dashboard.' +
        '</div>' +
      '</div>' +
    '</div>';
  const step = document.getElementById('boot-step');
  const term = document.getElementById('boot-term');
  const elapsed = document.getElementById('boot-elapsed');
  document.getElementById('boot-skip').onclick = () => location.reload();
  const t0 = Date.now();
  let stage = 0;
  const stages = [
    'Saving your config…',
    'Spawning the Research Agent tmux session…',
    'Booting Claude Code…',
    'Accepting bypass-permissions consent…',
    'Waiting for the Claude Code REPL to be ready…',
    'Handing the research brief to the agent…',
    'Agent is reading your purpose, seeds, and metric…',
    'Agent is scaffolding program.md, train.py, ideas.md…',
    'Agent is queuing the baseline run…',
  ];
  // Detect Claude Code OAuth fallback. On a fresh ~/.claude with no
  // ANTHROPIC_API_KEY honored, Claude Code prints an OAuth URL +
  // "Paste code here if prompted >". This regex is fuzzy — the
  // pane-captured text often has the URL hard-wrapped across multiple
  // terminal lines with stray \n's mid-token. We detect that an OAuth
  // flow is in progress here, then call /api/agent/oauth_url for the
  // un-wrapped URL (which uses tmux capture-pane -J to join wraps).
  const looksLikeOauth = (text) => {
    return /Paste\s+code\s+here/i.test(text)
        || /claude\.com\/cai\/oauth/i.test(text)
        || /response_type=code/i.test(text)
        || /code_challenge_method=S256/i.test(text);
  };
  // Best-effort stage detection from the actual tmux output.
  const detectStage = (text) => {
    const lc = text.toLowerCase();
    if (lc.includes('arui.init') || lc.includes('arui.log')
        || lc.includes('experiment') || /\bidea\s*\d/.test(lc)) return 8;
    if (lc.includes('program.md') || lc.includes('train.py')
        || lc.includes('ideas.md') || lc.includes('writing')
        || lc.includes('creating')) return 7;
    if (lc.includes('research') || lc.includes('purpose')
        || lc.includes('goal')) return 6;
    if (lc.includes('paste code here') || lc.includes('response_type=code')) {
      return 4;     // OAuth flow — stuck until user pastes the code back
    }
    if (lc.includes('how can i help') || lc.includes("what's your")
        || lc.includes('claude code') && lc.includes('ready')) return 5;
    if (lc.includes('yes, i accept') || lc.includes('bypass permissions')) {
      return 3;
    }
    if (lc.includes('welcome') || lc.length > 50) return 4;
    return Math.max(stage, 2);   // tmux is reachable → at least booting
  };
  let interval = null;
  let redirected = false;
  let errorShown = false;
  let sawAliveOnce = false;
  // Patterns that mean Claude Code died because the token is wrong /
  // expired / out of credit / rate-limited. Anything matching → switch
  // the overlay to error mode with a "Back to onboarding" button.
  const AUTH_ERROR_PATTERNS = [
    /invalid\s+api\s+key/i,
    /authentication\s+(failed|error)/i,
    /unauthor[iz]ed/i,
    /\b401\b/,
    /credit\s+balance/i,
    /(low|insufficient)\s+balance/i,
    /api\s+key.*(invalid|expired|revoked|not\s+found)/i,
    /please\s+(login|log\s+in|authenticate|sign\s+in)/i,
    /failed\s+to\s+authenticate/i,
    /your\s+api\s+key.*not.*valid/i,
  ];
  const RATE_LIMIT_PATTERNS = [
    /rate\s+limit/i,
    /\b429\b/,
    /too\s+many\s+requests/i,
  ];
  const matchesAny = (text, pats) => pats.some(re => re.test(text));
  const finish = (reason) => {
    if (redirected || errorShown) return; redirected = true;
    if (interval) clearInterval(interval);
    setTimeout(() => location.reload(), 500);
    step.textContent = reason || 'Ready — opening the dashboard…';
  };
  // Special recovery card for the Claude Code OAuth fallback.
  // 1. shows the OAuth URL as a clickable link (open in browser, sign in)
  // 2. provides a paste-back input → POST /api/agent/paste_oauth
  // 3. once typed, resumes polling (sawAliveOnce is already true so we
  //    won't show the "never started" error; the next tick should see
  //    real Claude Code REPL output).
  const showOauthRecovery = (url, ttext) => {
    if (errorShown || redirected) return;
    errorShown = true;
    if (interval) clearInterval(interval);
    const card = document.querySelector('.boot-card');
    if (!card) return;
    const urlRow = url
      ? '<div class="oauth-url-row">' +
          '<a class="btn pri oauth-url-btn" target="_blank" rel="noopener" ' +
          'href="' + esc(url) + '">Open Anthropic login →</a>' +
          '<button class="btn" id="oauth-copy" type="button">' +
          'Copy URL</button>' +
        '</div>' +
        '<div class="oauth-url-pre">' + esc(url) + '</div>'
      : '<div class="boot-err-body" style="color:#FCA5A5">' +
        'Could not extract the OAuth URL from the agent\'s pane. SSH ' +
        'into the pod and run <code>tmux attach -t agent</code> to see ' +
        'the full URL Claude printed.</div>';
    card.innerHTML =
      '<div class="boot-brand">autoresearcher<span>UI</span></div>' +
      '<div class="boot-err-icon" style="color:#A78BFA" aria-hidden="true">⚿</div>' +
      '<h2 class="boot-title">Claude Code wants you to log in</h2>' +
      '<div class="boot-err-body">' +
        'Claude Code started its OAuth flow instead of using the API key. ' +
        'Click the link below to sign in to your Anthropic account, then ' +
        'paste the code Claude gives you back into the box below.' +
      '</div>' +
      urlRow +
      '<input id="oauth-code" class="login-input" ' +
        'placeholder="Paste the code from your browser here…" ' +
        'autocomplete="off">' +
      '<div class="modal-actions">' +
        '<button class="btn pri" id="oauth-submit" type="button">' +
        'Send code to agent</button>' +
        '<button class="btn" id="oauth-retry" type="button">Skip &amp; ' +
        'retry agent</button>' +
      '</div>' +
      '<div class="boot-help">' +
        'Alternative (recommended): SSH into the pod, kill this session ' +
        'with <code>tmux kill-session -t agent</code>, run ' +
        '<code>IS_SANDBOX=1 claude --dangerously-skip-permissions</code> ' +
        'manually, finish OAuth in your browser + paste the code back, ' +
        'type <code>/exit</code> to close, then click <b>Skip &amp; retry ' +
        'agent</b> above. Subsequent restarts on this node will reuse the ' +
        'persisted credentials in <code>~/.claude/</code> and skip OAuth.' +
      '</div>';
    const copyBtn = document.getElementById('oauth-copy');
    if (copyBtn && url) copyBtn.onclick = () => {
      navigator.clipboard?.writeText(url);
      copyBtn.textContent = 'Copied ✓';
      setTimeout(() => copyBtn.textContent = 'Copy URL', 1400);
    };
    const submit = document.getElementById('oauth-submit');
    const inp = document.getElementById('oauth-code');
    if (inp) inp.focus();
    const doSubmit = async () => {
      const code = (inp && inp.value || '').trim();
      if (!code) { inp.focus(); return; }
      submit.disabled = true; submit.textContent = 'Sending…';
      try {
        const r = await post('/agent/paste_oauth', { code });
        if (r && r.ok === false) {
          submit.disabled = false; submit.textContent = 'Send code to agent';
          aruiAlert(r.error || 'Could not deliver to the agent.',
            { title: 'Paste failed' });
          return;
        }
        submit.textContent = 'Sent — resuming…';
      } catch (e) {
        submit.disabled = false; submit.textContent = 'Retry';
        return;
      }
      // Reset state and resume the spinner so we can detect "ready".
      errorShown = false; setTimeout(() => showAgentBootOverlay(), 800);
    };
    if (submit) submit.onclick = doSubmit;
    if (inp) inp.onkeydown = e => { if (e.key === 'Enter') doSubmit(); };
    const retry = document.getElementById('oauth-retry');
    if (retry) retry.onclick = async () => {
      retry.disabled = true; retry.textContent = 'Restarting…';
      try { await post('/agent/restart', {}); } catch (e) {}
      errorShown = false; setTimeout(() => showAgentBootOverlay(), 800);
    };
  };

  const showError = async (title, body, ttext) => {
    if (errorShown || redirected) return;
    errorShown = true;
    if (interval) clearInterval(interval);
    // Pull the user's saved config back so the form pre-fills when they
    // click "Back to onboarding". The settings endpoint returns the
    // onboarding row with secrets masked — that's fine, the user will
    // re-enter the broken token anyway.
    let saved = {};
    try { saved = await api('/settings'); } catch (e) {}
    const card = document.querySelector('.boot-card');
    if (card) {
      card.innerHTML =
        '<div class="boot-brand">autoresearcher<span>UI</span></div>' +
        '<div class="boot-err-icon" aria-hidden="true">⚠</div>' +
        '<h2 class="boot-title boot-err-title">' + esc(title) + '</h2>' +
        '<div class="boot-err-body">' + esc(body) + '</div>' +
        '<pre class="boot-term boot-term-err">' +
        esc((ttext || '').split('\n').slice(-14).join('\n') ||
        '(no agent output)') + '</pre>' +
        '<div class="boot-actions">' +
          '<button class="btn pri" id="boot-back" type="button">' +
          '← Back to onboarding</button>' +
          '<button class="btn" id="boot-retry" type="button">' +
          'Retry now</button>' +
        '</div>' +
        '<div class="boot-help">' +
          'Your config is saved — clicking <b>Back to onboarding</b> ' +
          'reopens the form with everything pre-filled so you only need ' +
          'to fix the broken field.' +
        '</div>';
      document.getElementById('boot-back').onclick = () =>
        onboarding(saved || {});
      document.getElementById('boot-retry').onclick = async () => {
        const btn = document.getElementById('boot-retry');
        btn.disabled = true; btn.textContent = 'Restarting agent…';
        try {
          await post('/agent/restart', {});
        } catch (e) {}
        // Reset overlay state and resume polling.
        errorShown = false;
        showAgentBootOverlay();
      };
    }
  };
  const tick = async () => {
    const sec = Math.floor((Date.now() - t0) / 1000);
    elapsed.textContent = sec + 's elapsed';
    let d = null;
    try { d = await api('/agent/terminal'); } catch (e) { /* try later */ }
    const txtRaw = (d && d.text) || '';
    const txt = txtRaw.replace(/[ \t]+$/gm, '').trim();
    if (d && d.alive) sawAliveOnce = true;
    if (txt && txt !== '(no agent session yet)') {
      const lines = txt.split('\n').slice(-12).join('\n');
      term.textContent = lines;
      term.scrollTop = term.scrollHeight;
      const detected = detectStage(txt);
      if (detected > stage) { stage = detected; step.textContent = stages[stage]; }
      // ── OAuth-prompt detection ──────────────────────────────────────
      // Claude Code on a fresh node falls back to OAuth when it can't
      // read ANTHROPIC_API_KEY. Surface a paste-back UI instead of
      // letting the user stare at a spinner forever. We use a separate
      // backend endpoint that captures the pane with line-joining,
      // because tmux wraps long URLs across multiple lines and the
      // regular /agent/terminal would give us a truncated URL.
      if (looksLikeOauth(txt)) {
        let urlResp = {};
        try { urlResp = await api('/agent/oauth_url'); } catch (e) {}
        const oauthUrl = (urlResp && urlResp.url) || '';
        if (oauthUrl) {
          return showOauthRecovery(oauthUrl, txt);
        }
        // Even if we couldn't extract the URL, surface the recovery
        // card so the user knows what's happening — they can SSH in.
        return showOauthRecovery('', txt);
      }
      // ── error detection on the live tmux text ───────────────────────
      if (matchesAny(txt, AUTH_ERROR_PATTERNS)) {
        return showError(
          'Claude token rejected',
          'Claude Code reported an authentication error. The token in ' +
          'your config is invalid, expired, revoked, or out of credit. ' +
          'Fix it in the onboarding form and try again.',
          txt);
      }
      if (matchesAny(txt, RATE_LIMIT_PATTERNS)) {
        return showError(
          'Anthropic rate-limited the launch',
          'The Anthropic API responded with a rate-limit error. Wait a ' +
          'minute or two, then click Retry. If it keeps happening, your ' +
          'workspace may be over its usage limit.',
          txt);
      }
    } else if (sec > 4) {
      if (stage < 2) { stage = 2; step.textContent = stages[stage]; }
    }
    // tmux died after we saw it alive at least once → Claude Code exited
    if (sawAliveOnce && d && d.alive === false && sec > 8) {
      return showError(
        'The Research Agent quit during startup',
        'The Claude Code tmux session is gone. Common causes: the API ' +
        'token is invalid or expired, the user pressed "No, exit" on the ' +
        'bypass-permissions consent, or Claude Code crashed. Check the ' +
        'output below, fix the cause, and retry.',
        txt);
    }
    // tmux NEVER came up at all → likely realrun failed to spawn.
    // Check for the specific "claude binary missing" Event first so we
    // give the user the actionable fix instead of a generic timeout.
    if (!sawAliveOnce && sec > 6) {
      try {
        const evs = await api('/events');
        const missing = (evs || []).find(e =>
          e && e.type === 'claude_code_missing');
        if (missing) {
          return showError(
            'Claude Code is not installed on this node',
            'The autonomous Research Agent runs as a real `claude` CLI ' +
            'session in tmux, but the binary isn\'t on the node\'s PATH. ' +
            'SSH in and run: ' +
            'npm install -g @anthropic-ai/claude-code ' +
            '(then click Retry). On a fresh node, the cleanest fix is to ' +
            '`git pull && bash setup.sh` — the installer now sets this up ' +
            'automatically.',
            txt || missing.message);
        }
      } catch (e) { /* keep polling */ }
    }
    if (!sawAliveOnce && sec > 25) {
      return showError(
        'The agent never started',
        'After 25 s the Research Agent tmux session still does not ' +
        'exist. Either the Claude Code binary is missing on this node, ' +
        'or the backend rejected the onboarding submission. Check the ' +
        'server log (`tmux attach -t arui`) for details.',
        txt);
    }
    // Heuristic finish: the agent has produced enough output AND we're
    // past the "scaffolding" stage. Realistically, by then there will be
    // a project name in /api/project and the dashboard will paint correctly.
    if (stage >= 7 && sec > 18) finish('Ready — opening the dashboard…');
    // Hard cap so we never strand the user.
    if (sec > 75) finish('Taking a while — opening the dashboard anyway…');
  };
  tick();
  interval = setInterval(tick, 2000);
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
  const ok = await aruiConfirm(
    'This deletes ALL experiments, runs, metrics, and config, and returns ' +
    'to the onboarding screen. This cannot be undone.',
    { title: 'Reset autoresearcherUI?', danger: true,
      okText: 'Delete everything' });
  if (!ok) return;
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
        setView('dashboard');
        setTimeout(() => openDrawer(run.id), 80);
      }
    };
  });
}

/* ── Write the paper tab ──────────────────────────────────────────────────
   Paper-mode dashboard. Default sub-tab is Today (the decision queue +
   overnight summary + cost trackers + section health). Other sub-tabs:
   Claim Coverage, Paper Plan, Critical Path, Related Work, Versions.
   In research mode, this view shows a CTA explaining how to enter paper
   mode without committing the user to anything. */
const PaperState = {
  state: null,         // full /api/paper/state payload
  today: null,         // /api/paper/today
  tab: 'today',        // today | coverage | plan | path | refs | versions
  loaded: false,
};

async function renderLatex(c) {
  const mode = (S.mode && S.mode.mode) || 'research';
  if (mode !== 'paper') {
    c.innerHTML =
      '<div class="latex-soon"><div class="latex-ic">📝</div>' +
      '<h2>Write the paper</h2>' +
      '<p>You\'re currently in <b>Research</b> mode — the autonomous research ' +
      'agent is exploring ideas. When the council says your work has enough ' +
      'evidence to write up, flip to <b>Paper</b> mode. This page then ' +
      'becomes a paper-writing mission-control: claims, ablations, figures, ' +
      'a Gantt of remaining runs, reviewer simulation, and the submission ' +
      'bundle.</p>' +
      '<div class="mode-flip-row">' +
        '<div class="mode-toggle big">' +
          '<button class="mode-btn on" data-m="research">Research</button>' +
          '<button class="mode-btn" data-m="paper">Paper</button>' +
        '</div>' +
        '<button class="btn pri" id="paper-cta">Start the Paper Proposal →</button>' +
      '</div>' +
      '</div>';
    const cta = c.querySelector('#paper-cta');
    if (cta) cta.onclick = openPaperProposal;
    c.querySelectorAll('.mode-btn').forEach(b => {
      b.onclick = () => {
        if (b.dataset.m === 'research') return;     // already here
        openPaperProposal();
      };
    });
    return;
  }
  c.innerHTML =
    '<div class="paper-wrap">' +
      '<div class="paper-hero">' +
        '<div class="paper-hero-bar">' +
          '<div class="mode-toggle small">' +
            '<button class="mode-btn" data-m="research" title="Revert to research mode">Research</button>' +
            '<button class="mode-btn on" data-m="paper">Paper</button>' +
          '</div>' +
          '<div class="paper-viewer-toggle">' +
            '<button class="pv-btn on" data-pv="pdf">PDF</button>' +
            '<button class="pv-btn" data-pv="tex">LaTeX</button>' +
          '</div>' +
          '<div class="paper-build" id="paper-build">build status…</div>' +
          '<button class="btn paper-rebuild" id="paper-rebuild">⟳ Rebuild</button>' +
          '<a class="btn paper-download" href="/api/paper/pdf" download="paper.pdf">⤓ PDF</a>' +
        '</div>' +
        '<div class="paper-viewer" id="paper-viewer">' +
          '<div class="paper-empty"><div class="paper-empty-ic">📄</div>' +
            '<div class="paper-empty-title">loading…</div></div>' +
        '</div>' +
      '</div>' +
      '<div class="paper-tabs">' +
        '<button class="paper-tab on" data-t="today">Today</button>' +
        '<button class="paper-tab" data-t="coverage">Claim Coverage</button>' +
        '<button class="paper-tab" data-t="plan">Paper Plan</button>' +
        '<button class="paper-tab" data-t="path">Critical Path</button>' +
        '<button class="paper-tab" data-t="refs">Related Work</button>' +
        '<button class="paper-tab" data-t="versions">Versions</button>' +
        '<button class="paper-tab" data-t="rebuttal" id="paper-tab-rebuttal" style="display:none">Rebuttal</button>' +
        '<button class="paper-tab" data-t="share" id="paper-tab-share">Share</button>' +
      '</div>' +
      '<div class="paper-tab-body" id="paper-tab-body">loading…</div>' +
    '</div>';
  // Wire viewer toggle
  c.querySelectorAll('.pv-btn').forEach(b => {
    b.onclick = () => {
      c.querySelectorAll('.pv-btn').forEach(x => x.classList.toggle(
        'on', x === b));
      paintPaperViewer(c, b.dataset.pv);
    };
  });
  // Wire the in-page mode toggle (flip back to research).
  c.querySelectorAll('.mode-toggle.small .mode-btn').forEach(b => {
    b.onclick = () => {
      if (b.dataset.m === 'paper') return;          // already here
      openRevertModal();
    };
  });
  c.querySelector('#paper-rebuild').onclick = async () => {
    const btn = c.querySelector('#paper-rebuild');
    btn.disabled = true; btn.textContent = '⟳ rebuilding…';
    try {
      await post('/paper/recompile', { force: true });
      await paintBuildStatus(c);
      const ifr = c.querySelector('#paper-pdf');
      if (ifr) ifr.src = '/api/paper/pdf?ts=' + Date.now();
    } catch (e) { /* ignore */ }
    btn.disabled = false; btn.textContent = '⟳ Rebuild';
  };
  // Sub-tabs
  c.querySelectorAll('.paper-tab').forEach(b => {
    b.onclick = () => {
      c.querySelectorAll('.paper-tab').forEach(x =>
        x.classList.toggle('on', x === b));
      PaperState.tab = b.dataset.t;
      paintPaperTab(c);
    };
  });
  // Load + paint
  await loadPaperState();
  paintBuildStatus(c);
  paintPaperViewer(c, 'pdf');   // resolves to iframe OR empty state
  paintPaperTab(c);
}

async function loadPaperState() {
  try {
    [PaperState.state, PaperState.today] = await Promise.all(
      [api('/paper/state'), api('/paper/today')]);
    PaperState.loaded = true;
  } catch (e) { /* keep stale */ }
}

async function paintBuildStatus(c) {
  const el_ = c.querySelector('#paper-build');
  if (!el_) return;
  try {
    const b = await api('/paper/build_log');
    if (!b) { el_.textContent = '—'; return; }
    // A PDF on disk after a backend restart has empty `at`. Don't lie to
    // the user by saying "never built" — the PDF the iframe is showing
    // came from somewhere. Just label it.
    if (!b.at) {
      if (b.pdf_exists) {
        el_.innerHTML = `<span style="color:var(--muted)">PDF on disk (no fresh build this session)</span>`;
      } else {
        el_.textContent = 'never built';
      }
      return;
    }
    const log = (b.log || '').toLowerCase();
    // "no main.tex yet" is the not-yet-scaffolded state, NOT a build failure.
    // Without this carve-out the header lies (says "build failed") while the
    // empty-state body says "Author Agent is scaffolding" — confusing.
    const scaffolding = !b.ok && (
      log.indexOf('no main.tex') >= 0 ||
      log.indexOf("hasn't scaffolded") >= 0);
    // Missing TeX Live is an environment problem, not a LaTeX compile error.
    const noTex = !b.ok && (
      log.indexOf('neither latexmk nor pdflatex') >= 0 ||
      log.indexOf('install tex live') >= 0);
    if (scaffolding) {
      el_.innerHTML = `<span style="color:var(--muted)">○ scaffolding…</span>`;
    } else if (noTex) {
      el_.innerHTML = `<span style="color:var(--warn)">⚠ TeX Live not installed</span>`;
    } else if (b.has_warnings && b.pdf_exists) {
      // PDF compiled but with unresolved refs/cites — still useful, just
      // warn the user instead of slapping "build failed" on the header.
      el_.innerHTML = `<span style="color:var(--warn)">⚠ compiled with warnings</span> ` +
        `<span style="color:var(--muted);font-size:10px">${b.elapsed_sec}s</span>`;
    } else if (!b.ok) {
      el_.innerHTML = `<span style="color:var(--bad)">⚠ build failed</span>`;
    } else if (b.stale) {
      el_.innerHTML = `<span style="color:var(--warn)">stale</span>`;
    } else {
      el_.innerHTML = `<span style="color:var(--ok)">PDF up to date</span> ` +
        `<span style="color:var(--muted);font-size:10px">${b.elapsed_sec}s</span>`;
    }
  } catch (e) { el_.textContent = '—'; }
}

async function paintPaperViewer(c, kind) {
  const v = c.querySelector('#paper-viewer');
  if (!v) return;
  if (kind === 'pdf') {
    // Check build status first — only render an iframe if there's actually
    // a PDF to show. Otherwise the iframe ends up displaying the raw
    // JSON error response, which looks broken.
    let bs = null;
    try { bs = await api('/paper/build_log'); } catch(e) {}
    // A PDF on disk is enough to render — latexmk routinely exits with
    // code 1 (unresolved \cite, undefined \ref) while still producing
    // a usable PDF. `pdf_exists` is the ground truth; `ok` is whether
    // the LaTeX build was warning-free.
    const havePdf = !!(bs && bs.pdf_exists);
    if (havePdf) {
      v.innerHTML = `<iframe id="paper-pdf" src="/api/paper/pdf?ts=${Date.now()}" frameborder="0"></iframe>`;
    } else {
      // Friendly empty state. Common cases:
      //   • Author Agent not yet spawned / scaffolded   (no main.tex)
      //   • LaTeX compile failure                       (build_status.ok==false)
      //   • Rebuild required after a change             (stale==true)
      const log = (bs && bs.log) || '';
      const lowLog = log.toLowerCase();
      let title = 'PDF not built yet';
      let body  = 'The Author Agent will scaffold main.tex and request the first build shortly.';
      if (lowLog.indexOf('no main.tex') >= 0 ||
          lowLog.indexOf("hasn't scaffolded") >= 0) {
        title = 'Author Agent is scaffolding';
        body  = 'The Author Agent is being spawned. As soon as it writes main.tex the PDF will appear here.';
      } else if (lowLog.indexOf('neither latexmk nor pdflatex') >= 0 ||
                 lowLog.indexOf('install tex live') >= 0) {
        title = 'TeX Live is not installed on this node';
        body  = 'Paper Mode needs latexmk/pdflatex to compile. SSH in and run: ' +
                'apt-get update && apt-get install -y texlive-latex-extra texlive-fonts-recommended latexmk';
      } else if (bs && bs.ok === false && bs.at) {
        title = 'Build failed';
        body  = (log || '').slice(-600) || 'See LaTeX log for details.';
      } else if (bs && bs.stale) {
        title = 'PDF is stale';
        body  = 'Click Rebuild to recompile the latest LaTeX.';
      }
      v.innerHTML =
        '<div class="paper-empty">' +
          '<div class="paper-empty-ic">📄</div>' +
          `<div class="paper-empty-title">${esc(title)}</div>` +
          `<div class="paper-empty-body">${esc(body)}</div>` +
          (log ? `<pre class="paper-empty-log">${esc(log.slice(-1200))}</pre>` : '') +
        '</div>';
    }
    // Always try a recompile so the next paint picks up new artifacts.
    try { await post('/paper/recompile', {}); paintBuildStatus(c); } catch(e) {}
  } else {
    v.innerHTML = '<div class="paper-tex"><div class="empty2">loading…</div></div>';
    try {
      const d = await api('/paper/tex');
      const files = d && d.files || [];
      if (!files.length) {
        v.querySelector('.paper-tex').innerHTML =
          '<div class="empty2">Author Agent hasn\'t written any LaTeX yet.</div>';
        return;
      }
      v.querySelector('.paper-tex').innerHTML = files.map(f =>
        `<div class="tex-file"><div class="tex-h">${esc(f.path)}` +
        (f.user_owned ? '<span class="tex-ow">USER OVERRIDE</span>' : '') +
        `</div><pre class="tex-body">${esc(f.content)}</pre></div>`).join('');
    } catch (e) {
      v.querySelector('.paper-tex').innerHTML =
        '<div class="empty2">Could not load LaTeX.</div>';
    }
  }
}

function paintPaperTab(c) {
  const body = c.querySelector('#paper-tab-body');
  if (!body) return;
  if (!PaperState.loaded) { body.innerHTML = '<div class="empty2">loading…</div>'; return; }
  const tab = PaperState.tab;
  if (tab === 'today') paintToday(body);
  else if (tab === 'coverage') paintCoverage(body);
  else if (tab === 'plan') paintPlan(body);
  else if (tab === 'path') paintCriticalPath(body);
  else if (tab === 'refs') paintRelatedWork(body);
  else if (tab === 'versions') paintVersions(body);
  else if (tab === 'rebuttal') paintRebuttal(body);
  else if (tab === 'share') paintShare(body);
  // Conditionally show Rebuttal tab based on phase.
  const meta = (PaperState.state && PaperState.state.meta) || {};
  const rebTab = document.getElementById('paper-tab-rebuttal');
  if (rebTab) {
    rebTab.style.display = (meta.phase === 'rebuttal' || meta.phase === 'submission') ? '' : 'none';
  }
}

/* ── Today view (the heartbeat) ──────────────────────────────────────── */
function paintToday(b) {
  const t = PaperState.today || {};
  const st = PaperState.state || {};
  const meta = st.meta || {};
  const bud = t.budget || {};
  const days = t.days_till_deadline;
  const decs = t.decisions || [];
  const running = t.running_runs || [];
  const recent = t.recent_runs || [];
  const sections = t.sections || [];
  const commits = t.commits || [];
  const gh = bud.gpu_hours_used || 0;
  const ghb = bud.gpu_hours_budget || 0;
  const ghPct = ghb ? Math.min(100, Math.round(gh/ghb*100)) : 0;
  // Twin progress bars: days-to-deadline + GPU-hours used.
  // Days bar fills as the deadline approaches — pinned at 100% past deadline.
  const totalProjDays = (() => {
    try {
      const d0 = new Date(meta.created_at || meta.updated_at || Date.now());
      const dDead = meta.deadline_iso ? new Date(meta.deadline_iso) : null;
      if (!dDead) return 0;
      return Math.max(1, (dDead - d0) / 86400000);
    } catch (e) { return 0; }
  })();
  const daysUsed = totalProjDays && days != null
    ? Math.max(0, totalProjDays - Math.max(0, days)) : 0;
  const daysPct = totalProjDays
    ? Math.min(100, Math.round(daysUsed / totalProjDays * 100)) : 0;
  // Overshoot warning: GPU burn rate > days burn rate by 10pp ⇒ on track to overrun.
  const overshoot = ghb && totalProjDays && ghPct > daysPct + 10 && days != null && days > 0;
  const claims = (st.claims || []).length;
  const figs = (st.figures || []).length;
  const queued = (st.paper_runs||[]).filter(r=>r.status==='queued').length;
  const doneN = (st.paper_runs||[]).filter(r=>['kept','success','done'].includes(r.status)).length;
  const ago_ = (iso) => iso ? ago(iso) : '—';
  b.innerHTML = `
    <div class="td-summary">
      <h2>Today · ${esc(meta.venue || 'Paper')}</h2>
      <div class="td-stats">
        <div class="td-stat"><b>${days != null ? (days.toFixed ? days.toFixed(1) : days) : '—'}</b><span>days till deadline</span><div class="td-bar td-bar-days"><i style="width:${daysPct}%"></i></div></div>
        <div class="td-stat"><b>${claims}</b><span>claims</span></div>
        <div class="td-stat"><b>${doneN}/${doneN+queued+running.length}</b><span>runs done</span><div class="td-bar td-bar-runs"><i style="width:${doneN+queued+running.length ? Math.round(100*doneN/(doneN+queued+running.length)) : 0}%"></i></div></div>
        <div class="td-stat"><b>${running.length}</b><span>running now</span></div>
      </div>
      ${(claims === 0) ? `<div class="td-warn td-empty-warn">
        ⚠ No claims yet. Click <button class="btn xs inline" id="td-scaffold-now">⚡ Scaffold now</button> to import them from the council's pre-flip assessment.
      </div>` : ''}
    </div>

    <div class="td-section-h">Roadmap to ship</div>
    <div class="td-roadmap" id="td-roadmap"></div>

    <div class="td-section-h">Decision queue <span class="td-h-sub">${decs.length} pending</span>
      ${decs.length ? `<div class="dq-bulk">
        <button class="btn xs dq-bulk-approve" data-kind="cite_paper" title="Approve all citation suggestions (j/k to navigate, Enter approves)">Approve all cites</button>
        <button class="btn xs dq-bulk-approve" data-kind="approve_text">Approve all text</button>
        <button class="btn xs dq-bulk-approve" data-kind="add_ablation">Approve all ablations</button>
        <button class="btn xs ghost dq-undo" disabled>Undo bulk</button>
        <span class="dq-kbd" title="j/k navigate · Enter approve · R reject · D defer">
          ⌨ <span class="mono">j/k · Enter · R · D</span>
        </span>
      </div>` : ''}
    </div>
    <div class="td-decisions" id="td-decisions" tabindex="0">${decs.length ? decs.map((d,i) => decisionCard(d, i===0)).join('')
      : '<div class="empty2">No decisions pending. The Author Agent will file new ones as ablations complete.</div>'}</div>

    <div class="td-section-h">Section health</div>
    <div class="td-sections">${sections.length ? sections.map(s => sectionPill(s)).join('')
      : '<div class="empty2">Author Agent hasn\'t scaffolded sections yet.</div>'}</div>

    <div class="td-section-h">Running now</div>
    <div class="td-running">${running.length ? running.map(r =>
      `<div class="td-run-row"><span class="chip s-running"><span class="dot"></span>running</span>` +
      ` <span class="mono">${esc(r.run_name||r.id)}</span> <span style="color:var(--muted);font-size:11px">` +
      `${r.paper_claim_id?'claim '+esc(r.paper_claim_id)+' · ':''}GPU ${r.gpu_index}</span></div>`
      ).join('') : '<div class="empty2">No runs in flight.</div>'}</div>

    <div class="td-section-h">Recent paper-mode commits</div>
    <div class="td-commits">${commits.length ? commits.map(c => `
      <div class="td-commit"><span class="mono td-sha">${esc(c.sha)}</span>
        <span class="td-commit-msg">${esc(c.subject)}</span>
        <span class="td-commit-au">${esc(c.author)} · ${esc(ago_(c.at))}</span></div>`).join('')
      : '<div class="empty2">No commits in paper/ yet.</div>'}</div>
  `;
  // Render the compact roadmap chips (Today's top section).
  const rm = b.querySelector('#td-roadmap');
  if (rm) {
    const pr = (st.paper_runs || []);
    const datasets = new Set();
    for (const r of pr) {
      const cfg = r.config || {};
      const d = cfg.dataset || (cfg.cmd || '').match(/--dataset[ =]([\w_.-]+)/)?.[1] || 'default';
      if (d) datasets.add(d);
    }
    const doneN_p = pr.filter(r => ['kept','success','done'].includes(r.status)).length;
    const claimsReady = (st.claims || []).filter(c => c.ready).length;
    const sectionsReady = sections.filter(s => s.status === 'ready').length;
    const cites = (st.citations || []).filter(c => c.user_approved_at).length;
    const milestones = [
      { name: 'Claims',     state: (st.claims||[]).length ? 'done' : 'todo',
        detail: `${claimsReady}/${(st.claims||[]).length} ready` },
      { name: 'Datasets',   state: datasets.size >= 3 ? 'done' : datasets.size ? 'wip' : 'todo',
        detail: `${datasets.size}/3 (${Array.from(datasets).join(', ') || '—'})` },
      { name: 'Ablations',  state: doneN_p ? 'wip' : 'todo',
        detail: `${doneN_p} done` },
      { name: 'Sections',   state: sections.length && sectionsReady === sections.length ? 'done' : sectionsReady ? 'wip' : 'todo',
        detail: `${sectionsReady}/${sections.length||0} ready` },
      { name: 'Citations',  state: cites >= 10 ? 'done' : cites ? 'wip' : 'todo',
        detail: `${cites}/10 cited` },
      { name: 'Reviewer sim', state: (st.reviewer_sims||[]).length ? 'done' : 'todo', detail: '' },
    ];
    rm.innerHTML = milestones.map(m => {
      const icon = m.state === 'done' ? '✓' : m.state === 'wip' ? '◷' : '☐';
      return `<div class="rm-chip rm-${m.state}" title="${esc(m.detail)}">
        <span class="rm-icon">${icon}</span>
        <span class="rm-name">${esc(m.name)}</span>
        ${m.detail ? `<span class="rm-detail">${esc(m.detail)}</span>` : ''}
      </div>`;
    }).join('');
  }
  // Inline "Scaffold now" CTA if there are no claims.
  const sb = b.querySelector('#td-scaffold-now');
  if (sb) sb.onclick = async () => {
    sb.disabled = true; sb.textContent = 'scaffolding…';
    const r = await post('/paper/scaffold', {});
    await loadPaperState();
    paintPaperTab(document.querySelector('.paper-wrap').parentElement);
    aruiAlert(`Scaffold: +${r.claims_added} claims, +${r.runs_added} runs queued.`,
              { title: 'Scaffolded' });
  };
  // Wire decision actions
  b.querySelectorAll('.dec-action').forEach(btn => {
    btn.onclick = async () => {
      const did = btn.dataset.did;
      const act = btn.dataset.act;
      await post('/paper/decisions/' + did + '/resolve', { action: act });
      await loadPaperState();
      paintPaperTab(document.querySelector('.paper-wrap').parentElement);
    };
  });
  // Bulk approve by kind. Stash the affected IDs into __dqUndo so a one-shot
  // undo can re-open them. Pattern matches the spec's safety net.
  b.querySelectorAll('.dq-bulk-approve').forEach(btn => {
    btn.onclick = async () => {
      const kind = btn.dataset.kind;
      const targets = decs.filter(d => d.kind === kind).map(d => d.id);
      if (!targets.length) { aruiAlert(`No pending decisions of kind "${kind}"`); return; }
      const ok = await aruiConfirm(
        `Approve all ${targets.length} pending decisions of kind "${kind}"?`,
        { title: 'Bulk approve' });
      if (!ok) return;
      btn.disabled = true; btn.textContent = `Approving ${targets.length}…`;
      for (const did of targets) {
        try { await post('/paper/decisions/' + did + '/resolve', { action: 'approve' }); }
        catch (e) { /* keep going */ }
      }
      window.__dqUndo = targets;
      await loadPaperState();
      paintPaperTab(document.querySelector('.paper-wrap').parentElement);
    };
  });
  const undoBtn = b.querySelector('.dq-undo');
  if (undoBtn && window.__dqUndo && window.__dqUndo.length) {
    undoBtn.disabled = false;
    undoBtn.textContent = `Undo bulk (${window.__dqUndo.length})`;
    undoBtn.onclick = async () => {
      const targets = window.__dqUndo || [];
      window.__dqUndo = null;
      for (const did of targets) {
        try { await post('/paper/decisions/' + did + '/resolve', { action: 'defer' }); }
        catch (e) {}
      }
      await loadPaperState();
      paintPaperTab(document.querySelector('.paper-wrap').parentElement);
    };
  }
  // Keyboard shortcuts: j/k navigate · Enter approve · R reject · D defer.
  // Bind on the #td-decisions container so we don't clash with global typing
  // in inputs/textareas. Focus on first card by default.
  const list = b.querySelector('#td-decisions');
  if (list && decs.length) {
    if (!list.dataset.kbBound) {
      list.dataset.kbBound = '1';
      window.__paperKb = true;  // for tests
      list.addEventListener('keydown', async (e) => {
        if (['INPUT','TEXTAREA'].includes(document.activeElement?.tagName)) return;
        const cards = Array.from(list.querySelectorAll('.dec-card'));
        let idx = cards.findIndex(c => c.classList.contains('dec-focus'));
        if (idx < 0) idx = 0;
        const setFocus = (i) => {
          cards.forEach((c, ci) => c.classList.toggle('dec-focus', ci === i));
          cards[i]?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        };
        if (e.key === 'j' || e.key === 'ArrowDown') {
          e.preventDefault(); setFocus(Math.min(cards.length - 1, idx + 1));
        } else if (e.key === 'k' || e.key === 'ArrowUp') {
          e.preventDefault(); setFocus(Math.max(0, idx - 1));
        } else if (e.key === 'Enter') {
          e.preventDefault();
          const did = cards[idx]?.dataset.did;
          if (did) { await post('/paper/decisions/' + did + '/resolve', { action: 'approve' });
            await loadPaperState();
            paintPaperTab(document.querySelector('.paper-wrap').parentElement); }
        } else if (e.key === 'r' || e.key === 'R') {
          e.preventDefault();
          const did = cards[idx]?.dataset.did;
          if (did) { await post('/paper/decisions/' + did + '/resolve', { action: 'reject' });
            await loadPaperState();
            paintPaperTab(document.querySelector('.paper-wrap').parentElement); }
        } else if (e.key === 'd' || e.key === 'D') {
          e.preventDefault();
          const did = cards[idx]?.dataset.did;
          if (did) { await post('/paper/decisions/' + did + '/resolve', { action: 'defer' });
            await loadPaperState();
            paintPaperTab(document.querySelector('.paper-wrap').parentElement); }
        }
      });
    }
    list.focus({ preventScroll: true });
  }
}

function decisionCard(d, isFocused) {
  const kindLabel = ({
    cite_paper: 'Cite paper', kill_claim: 'Drop claim',
    add_ablation: 'Add ablation', approve_text: 'Approve text rewrite',
    approve_figure: 'Approve figure', budget_overrun: 'Budget overrun',
  }[d.kind] || d.kind);
  const sourceLabel = ({
    lit: 'Lit Agent', council: 'Council', agent: 'Author Agent',
    reviewer_sim: 'Reviewer sim', system: 'System', user: 'You',
  }[d.source] || d.source);
  const isDefaultApprove = d.default_action === 'approve';
  // Color chips per spec — green for recommended approve, red for recommended
  // reject, grey when there is no opinion. Lets a researcher in flow mode
  // triage in 1 click without reading the body.
  const chipColor = isDefaultApprove ? 'rec-approve'
    : (d.default_action === 'reject' ? 'rec-reject' : 'rec-neutral');
  const focusedCls = isFocused ? ' dec-focus' : '';
  const cost = d.est_cost_md ? `<span class="dec-cost">${esc(d.est_cost_md)}</span>` : '';
  return `
    <div class="dec-card${focusedCls}" data-did="${esc(d.id)}" data-kind="${esc(d.kind||'')}">
      <div class="dec-hd">
        <span class="dec-kind">${esc(kindLabel)}</span>
        <span class="dec-source">via ${esc(sourceLabel)}</span>
        <span class="dec-chip ${chipColor}" title="Recommended: ${esc(d.default_action || 'no opinion')}">${
          isDefaultApprove ? '✓ approve'
            : (d.default_action === 'reject' ? '✗ reject' : '○ neutral')
        }</span>
        ${cost}
      </div>
      <div class="dec-title">${esc(d.title || '')}</div>
      <div class="dec-body">${esc(d.body_md || '').replace(/\n/g,'<br>')}</div>
      <div class="dec-actions">
        <button class="dec-action ${isDefaultApprove?'pri rec-approve':''}" data-did="${esc(d.id)}" data-act="approve">Approve <span class="kbd">↵</span></button>
        <button class="dec-action ${d.default_action==='reject'?'pri rec-reject':''}" data-did="${esc(d.id)}" data-act="reject">Reject <span class="kbd">R</span></button>
        <button class="dec-action" data-did="${esc(d.id)}" data-act="defer">Defer <span class="kbd">D</span></button>
      </div>
    </div>`;
}

function sectionPill(s) {
  const colors = { draft: '#A78BFA', writing: '#FBBF24',
    blocked: '#F87171', ready: '#34D399', needs_review: '#7DD3FC' };
  const col = colors[s.status] || '#9BA1A8';
  return `<div class="td-section"><span style="background:${col}" class="td-section-dot"></span>` +
    `<span class="td-section-name">${esc(s.title || s.slug)}</span>` +
    `<span class="td-section-status">${esc(s.status)}</span></div>`;
}

/* ── Claim Coverage ──────────────────────────────────────────────────── */
function paintCoverage(b) {
  const st = PaperState.state || {};
  const claims = st.claims || [];
  const runs = st.paper_runs || [];
  const figs = st.figures || [];
  if (!claims.length) {
    b.innerHTML = '<div class="empty2">No claims yet. The Author Agent will populate these from the council\'s pre-flip assessment.</div>';
    return;
  }
  const evidenceFor = (cid) => {
    const cr = runs.filter(r => r.paper_claim_id === cid);
    return {
      main: cr.filter(r => r.paper_role === 'main').length,
      mainDone: cr.filter(r => r.paper_role === 'main' && ['kept','success','done'].includes(r.status)).length,
      abl: cr.filter(r => r.paper_role === 'ablation').length,
      ablDone: cr.filter(r => r.paper_role === 'ablation' && ['kept','success','done'].includes(r.status)).length,
      scaling: cr.filter(r => r.paper_role === 'scaling').length,
      cross: cr.filter(r => r.paper_role === 'cross').length,
      baseline: cr.filter(r => r.paper_role === 'baseline').length,
      figs: figs.filter(f => f.claim_id === cid).length,
    };
  };
  const cell = (have, want) => {
    if (!want && !have) return '—';
    if (have >= want && have > 0) return `<span class="cov-ok">✓ ${have}</span>`;
    if (have > 0) return `<span class="cov-part">${have}/${want}</span>`;
    return `<span class="cov-missing">missing</span>`;
  };
  b.innerHTML = `
    <table class="cov-table">
      <thead><tr><th>★</th><th>Claim</th><th>main</th><th>ablations</th>
        <th>scaling</th><th>cross-dataset</th><th>baselines</th><th>figures</th>
        <th>strength</th></tr></thead>
      <tbody>${claims.map(c => {
        const e = evidenceFor(c.id);
        return `<tr><td><button class="cov-star ${c.ready?'on':''}" data-cid="${esc(c.id)}">★</button></td>` +
          `<td><div class="cov-title">${esc(c.title || c.id)}</div>` +
          `<div class="cov-summary">${esc(c.summary_md || '')}</div></td>` +
          `<td>${cell(e.mainDone, e.main || 1)}</td>` +
          `<td>${cell(e.ablDone, Math.max(e.abl,2))}</td>` +
          `<td>${cell(0, e.scaling)}</td>` +
          `<td>${cell(0, e.cross)}</td>` +
          `<td>${cell(0, e.baseline)}</td>` +
          `<td>${e.figs}</td>` +
          `<td><span class="cov-strength s-${c.evidence_strength}">${esc(c.evidence_strength || 'unclear')}</span></td>` +
          `</tr>`;
      }).join('')}</tbody>
    </table>`;
}

/* ── Paper Plan (ordered ablation checklist, grouped by claim) ───────── */
function paintPlan(b) {
  const st = PaperState.state || {};
  const claims = st.claims || [];
  const runs = st.paper_runs || [];
  const figs = st.figures || [];

  if (!claims.length && !runs.length && !figs.length) {
    b.innerHTML =
      '<div class="empty2" style="padding:18px 0">' +
      'Paper plan is empty. The council\'s claims should have been ' +
      'imported on paper-mode entry. If they weren\'t, click <b>Scaffold ' +
      'now</b> to populate claims and queue the default ablation set.' +
      '</div>' +
      '<button class="btn pri" id="plan-scaffold">⚡ Scaffold now</button>';
    b.querySelector('#plan-scaffold').onclick = async () => {
      const r = await post('/paper/scaffold', {});
      await loadPaperState();
      paintPaperTab(document.querySelector('.paper-wrap').parentElement);
      aruiAlert(`Scaffold complete — ${r.claims_added} new claims, ` +
                `${r.runs_added} new runs queued.`,
                { title: 'Scaffolded' });
    };
    return;
  }

  // Build the checklist: per-claim group with headline → ablations → seeds.
  const byClaim = {};
  for (const c of claims) byClaim[c.id] = { claim: c, runs: [] };
  byClaim['_orphan'] = { claim: { id: '_orphan',
    title: '(unassigned to a claim)' }, runs: [] };
  for (const r of runs) {
    const k = byClaim[r.paper_claim_id] ? r.paper_claim_id : '_orphan';
    byClaim[k].runs.push(r);
  }
  const groupHTML = Object.values(byClaim).map(grp => {
    if (!grp.runs.length && grp.claim.id !== '_orphan') {
      return `<div class="plan-group">
        <div class="plan-group-hd">
          <b>${esc(grp.claim.title)}</b>
          <span class="plan-group-counts">0 runs</span>
        </div>
        <div class="empty2" style="padding:10px 0">No ablations queued.
          <button class="btn xs plan-queue" data-cid="${esc(grp.claim.id)}">Queue default set</button>
        </div>
      </div>`;
    }
    if (!grp.runs.length) return '';
    // Bucket runs by status
    const order = { running: 0, queued: 1, kept: 2, success: 2, done: 2,
                    crashed: 3, failed: 3, discarded: 4 };
    grp.runs.sort((a,b) => (order[a.status] ?? 9) - (order[b.status] ?? 9) ||
                            (a.run_name || '').localeCompare(b.run_name||''));
    const queued = grp.runs.filter(r => r.status === 'queued').length;
    const running = grp.runs.filter(r => r.status === 'running').length;
    const done = grp.runs.filter(r => ['kept','success','done'].includes(r.status)).length;
    return `<div class="plan-group">
      <div class="plan-group-hd">
        <b>${esc(grp.claim.title)}</b>
        <span class="plan-group-counts">
          <span class="chip s-running">${running} running</span>
          <span class="chip s-queued">${queued} queued</span>
          <span class="chip s-kept">${done} done</span>
        </span>
      </div>
      <table class="plan-runs">
        <thead><tr><th>✓</th><th>Status</th><th>Run</th><th>Role</th>
          <th>Dataset</th><th>Seed</th><th>Metric</th><th>Started</th></tr></thead>
        <tbody>${grp.runs.map(r => {
          const cfg = r.config || {};
          const ds = cfg.dataset
            || (cfg.cmd || '').match(/--dataset[ =]([\w_.-]+)/)?.[1]
            || 'default';
          const m = r.headline_metric != null
            ? fmt(r.headline_metric, 4) : '—';
          const isDone = ['kept','success','done'].includes(r.status);
          return `<tr class="plan-run-row ${r.status}">
            <td>${isDone ? '☑' : (r.status==='running' ? '◷' : '☐')}</td>
            <td><span class="chip s-${r.status}">${esc(r.status)}</span></td>
            <td class="mono">${esc(r.run_name || r.id)}</td>
            <td>${esc(r.paper_role || cfg.role || '')}</td>
            <td><span class="ds-chip">${esc(ds)}</span></td>
            <td class="mono">${esc(String(cfg.seed ?? r.n_seeds ?? '—'))}</td>
            <td class="mono">${m}</td>
            <td class="mono" style="color:var(--muted);font-size:11px">${r.started_at ? esc(ago(r.started_at)) : '—'}</td>
          </tr>`;
        }).join('')}</tbody>
      </table>
    </div>`;
  }).join('');
  b.innerHTML = `
    <div class="plan-bar">
      <button class="btn xs" id="plan-rescaffold" title="Re-import claims from the council and queue any missing ablations">⚡ Re-scaffold</button>
      <span style="color:var(--muted);font-size:11px;margin-left:8px">
        ${runs.length} total runs · ${runs.filter(r=>r.status==='queued').length} queued · ${runs.filter(r=>r.status==='running').length} running · ${runs.filter(r=>['kept','success','done'].includes(r.status)).length} done
      </span>
    </div>
    ${groupHTML}`;
  b.querySelector('#plan-rescaffold').onclick = async () => {
    const r = await post('/paper/scaffold', {});
    await loadPaperState();
    paintPaperTab(document.querySelector('.paper-wrap').parentElement);
    aruiAlert(`Scaffold: +${r.claims_added} claims, +${r.runs_added} runs queued.`,
              { title: 'Re-scaffolded' });
  };
  b.querySelectorAll('.plan-queue').forEach(btn => {
    btn.onclick = async () => {
      await post('/paper/scaffold', {});
      await loadPaperState();
      paintPaperTab(document.querySelector('.paper-wrap').parentElement);
    };
  });
}

/* ── Critical Path: Milestones (primary) + per-run Gantt (collapsible) ── */
function paintCriticalPath(b) {
  const st = PaperState.state || {};
  const claims = st.claims || [];
  const runs = (st.paper_runs || []).slice();
  const sections = (PaperState.today || {}).sections || [];
  const citations = st.citations || [];
  const versions = st.versions || [];
  const meta = st.meta || {};
  const buildStatus = st.build_status || {};
  const days = (PaperState.today || {}).days_till_deadline;
  if (!claims.length && !runs.length) {
    b.innerHTML = '<div class="empty2">No claims yet — click <b>Re-scaffold</b> on Paper Plan to populate.</div>';
    return;
  }

  // Derive the "datasets we've used" set from run configs.
  const datasets = (() => {
    const seen = new Set();
    for (const r of runs) {
      const cfg = r.config || {};
      const d = cfg.dataset || (cfg.cmd || '').match(/--dataset[ =]([\w_.-]+)/)?.[1] || 'default';
      if (d) seen.add(d);
    }
    return Array.from(seen);
  })();

  // Build the milestone roadmap.
  const done = (n) => n > 0 ? '✓' : '☐';
  const inProgress = (q,r,d) => (r > 0 || (d > 0 && q > 0)) ? '◷' : (d > 0 ? '✓' : '☐');
  const totalRuns = runs.length;
  const runningRuns = runs.filter(r => r.status === 'running').length;
  const queuedRuns = runs.filter(r => r.status === 'queued').length;
  const doneRuns = runs.filter(r => ['kept','success','done'].includes(r.status)).length;
  const crashedRuns = runs.filter(r => ['crashed','failed','error'].includes(r.status)).length;
  const claimsReady = claims.filter(c => c.ready).length;
  const sectionsReady = sections.filter(s => s.status === 'ready').length;
  const approvedCites = citations.filter(c => c.user_approved_at).length;
  const versionsPinned = versions.length;
  const hasReviewerSim = (st.reviewer_sims || []).length > 0;

  // Per-dataset coverage per claim — the heart of "great paper" thinking.
  const datasetCoverage = claims.map(c => {
    const cRuns = runs.filter(r => r.paper_claim_id === c.id);
    const datasetsCovered = new Set();
    for (const r of cRuns) {
      if (['kept','success','done'].includes(r.status)) {
        const ds = (r.config || {}).dataset || 'default';
        datasetsCovered.add(ds);
      }
    }
    return {
      claim: c,
      datasets_done: Array.from(datasetsCovered),
      total_runs: cRuns.length,
      done: cRuns.filter(r => ['kept','success','done'].includes(r.status)).length,
      running: cRuns.filter(r => r.status === 'running').length,
      queued: cRuns.filter(r => r.status === 'queued').length,
    };
  });

  const milestones = [
    { name: 'Claims imported from council',
      status: claims.length ? 'done' : 'todo',
      detail: claims.length ? `${claims.length} active` : 'awaiting scaffold' },
    { name: 'Headline result per claim',
      status: claimsReady === claims.length && claims.length > 0 ? 'done' :
              doneRuns > 0 ? 'in_progress' : 'todo',
      detail: `${claimsReady}/${claims.length} claims marked ready` },
    { name: 'Cross-dataset validation (≥3 datasets)',
      status: datasets.length >= 3 ? 'done' :
              datasets.length > 0 ? 'in_progress' : 'todo',
      detail: datasets.length ? `${datasets.length} dataset${datasets.length>1?'s':''}: ${datasets.join(', ')}` : 'single-dataset only — author should expand' },
    { name: 'Ablation depth (≥3 seeds × ≥2 ablations per claim)',
      status: doneRuns >= claims.length * 6 ? 'done' :
              doneRuns > 0 ? 'in_progress' : 'todo',
      detail: `${doneRuns} done · ${runningRuns} running · ${queuedRuns} queued${crashedRuns?` · ${crashedRuns} crashed`:''}` },
    { name: 'Related Work — ≥10 cited papers',
      status: approvedCites >= 10 ? 'done' :
              approvedCites > 0 ? 'in_progress' : 'todo',
      detail: `${approvedCites} approved citations` },
    { name: 'Sections drafted',
      status: sectionsReady === sections.length && sections.length > 0 ? 'done' :
              sectionsReady > 0 ? 'in_progress' : 'todo',
      detail: `${sectionsReady}/${sections.length} sections ready` },
    { name: 'PDF compiles cleanly',
      status: buildStatus.pdf_exists ? (buildStatus.ok ? 'done' : 'in_progress') : 'todo',
      detail: buildStatus.pdf_exists
        ? (buildStatus.ok ? 'no warnings' : 'compiles with warnings')
        : 'no PDF yet' },
    { name: 'Reviewer simulation run',
      status: hasReviewerSim ? 'done' : 'todo',
      detail: hasReviewerSim ? 'simulator output saved' : 'run from Versions tab before submitting' },
    { name: 'Pin v-submitted',
      status: versionsPinned > 0 ? 'done' : 'todo',
      detail: versionsPinned ? `${versionsPinned} version(s) pinned` : 'no versions pinned' },
    { name: meta.deadline_iso ? `Deadline: ${meta.deadline_iso.slice(0,10)}` : 'No deadline set',
      status: (days != null && days < 7) ? 'urgent' :
              (days != null && days < 30) ? 'in_progress' : 'todo',
      detail: days != null ? `${days.toFixed(1)} days remaining` : '—' },
  ];

  // Render the milestone list.
  const milestoneHTML = milestones.map(m => {
    const icon = m.status === 'done' ? '✓'
              : m.status === 'urgent' ? '⚠'
              : m.status === 'in_progress' ? '◷' : '☐';
    return `<div class="ms-row ms-${m.status}">
      <span class="ms-icon">${icon}</span>
      <div class="ms-body">
        <div class="ms-name">${esc(m.name)}</div>
        <div class="ms-detail">${esc(m.detail)}</div>
      </div>
    </div>`;
  }).join('');

  // Per-claim cross-dataset coverage grid (compact).
  const coverageHTML = !claims.length ? '' : `
    <div class="cp-h" style="margin-top:18px">Per-claim cross-dataset coverage</div>
    <div class="empty2" style="margin-bottom:6px">
      ≥3 datasets per claim is the rough bar for a top-tier paper.
    </div>
    <table class="cov-table dataset-matrix">
      <thead><tr><th>Claim</th><th>Datasets done</th><th>Done / Running / Queued</th></tr></thead>
      <tbody>${datasetCoverage.map(dc => `
        <tr>
          <td><div class="cov-title">${esc((dc.claim.title||'').slice(0,80))}</div></td>
          <td>${dc.datasets_done.length
              ? dc.datasets_done.map(d => `<span class="ds-chip">${esc(d)}</span>`).join('')
              : '<span style="color:var(--bad)">no datasets done</span>'}
              <span style="color:var(--muted);font-size:11px">${dc.datasets_done.length}/3 target</span></td>
          <td class="mono">${dc.done} / ${dc.running} / ${dc.queued}</td>
        </tr>`).join('')}</tbody>
    </table>`;

  b.innerHTML = `
    <div class="cp-h">
      <b>Milestones to ship</b>
      <span style="color:var(--muted);font-size:12px;margin-left:8px">
        ${milestones.filter(m=>m.status==='done').length}/${milestones.length} complete
      </span>
    </div>
    <div class="ms-list">${milestoneHTML}</div>
    ${coverageHTML}
    <details class="cp-gantt-details" style="margin-top:22px">
      <summary>Per-run schedule (Gantt) — secondary view</summary>
      <div id="cp-gantt-inner"></div>
    </details>
  `;
  // Render the gantt inside the collapsible <details>.
  const gantt = b.querySelector('#cp-gantt-inner');
  if (gantt) _paintGantt(gantt, runs, claims);
}


function _paintGantt(b, runs, claims) {
  const gpuCount = Math.max(1, (S.gpus || []).length || 1);
  // Estimate per-run duration. Use the MEDIAN of historical completed
  // runs as the default — beats the old hardcoded 5400s which made
  // queued bars look 30× longer than actual ones.
  const NOW = Date.now();
  const HOUR = 3600000;
  const observedDurations = runs
    .filter(r => r.started_at && r.ended_at &&
                 ['kept','success','done'].includes(r.status))
    .map(r => (+new Date(r.ended_at) - +new Date(r.started_at)) / 1000)
    .filter(d => d > 0 && d < 86400)
    .sort((a, b) => a - b);
  const medianSec = observedDurations.length
    ? observedDurations[Math.floor(observedDurations.length / 2)]
    : 600;     // 10min fallback only if zero historical data
  const estSec = r => r.est_time_sec || (r.config && r.config.est_time_sec) || medianSec;
  // 1) Place completed/running runs at their real time.
  const placed = [];
  for (const r of runs) {
    if (['running','kept','success','done','crashed','failed'].includes(r.status) && r.started_at) {
      const start = +new Date(r.started_at);
      const end = r.ended_at ? +new Date(r.ended_at)
        : (r.status === 'running' ? NOW + estSec(r) * 1000 * 0.5 : NOW);
      placed.push({ run: r, start, end });
    }
  }
  // 2) Bin-pack queued runs onto N GPU lanes after now.
  const laneEnds = Array(gpuCount).fill(NOW);
  for (const r of runs) {
    if (r.status === 'queued') {
      const lane = laneEnds.indexOf(Math.min(...laneEnds));
      const start = laneEnds[lane];
      const end = start + estSec(r) * 1000;
      laneEnds[lane] = end;
      placed.push({ run: r, start, end, queued: true, lane });
    }
  }
  if (!placed.length) {
    b.innerHTML = '<div class="empty2">No paper runs to schedule. Click <b>Re-scaffold</b> on Paper Plan.</div>';
    return;
  }
  const minT = Math.min(NOW - 2*HOUR, ...placed.map(p => p.start));
  const maxT = Math.max(NOW + 4*HOUR, ...placed.map(p => p.end));
  const span = Math.max(HOUR, maxT - minT);
  // Build per-claim rows (each claim is a horizontal row containing its bars).
  const claimsWithRuns = claims.filter(c =>
    placed.some(p => p.run.paper_claim_id === c.id));
  const rows = (claimsWithRuns.length ? claimsWithRuns : claims).concat(
    placed.some(p => !p.run.paper_claim_id) ?
      [{ id: '_orphan', title: '(orphan runs)' }] : []);
  if (!rows.length) {
    b.innerHTML = '<div class="empty2">No scheduled runs to chart.</div>';
    return;
  }
  const ROW_H = 38;
  const LBL_W = 220;
  const WIDTH = 1100;
  const chartW = WIDTH - LBL_W - 24;
  const xs = t => LBL_W + (t - minT) / span * chartW;
  // Render
  const ticks = [];
  for (let t = minT; t <= maxT; t += Math.max(HOUR, span/8)) {
    ticks.push(t);
  }
  // build SVG
  const nowX = xs(NOW);
  const totalH = rows.length * ROW_H + 60;
  const colorFor = st => ({
    running: '#FBBF24', queued: '#7DD3FC',
    kept: '#34D399', success: '#34D399', done: '#34D399',
    crashed: '#F87171', failed: '#F87171', discarded: '#9BA1A8',
  }[st] || '#A78BFA');
  let svg = `<svg viewBox="0 0 ${WIDTH} ${totalH}" class="cp-gantt"
    xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMinYMin meet">`;
  // gridlines + tick labels
  svg += `<g class="cp-grid">`;
  for (const t of ticks) {
    const x = xs(t);
    const lbl = new Date(t);
    const ms = (t - NOW) / 3600000;
    const txt = (Math.abs(ms) < 0.5 ? 'now'
      : (ms > 0 ? `+${ms.toFixed(1)}h` : `${ms.toFixed(1)}h`));
    svg += `<line x1="${x}" y1="20" x2="${x}" y2="${totalH-10}" stroke="rgba(255,255,255,.06)"/>`;
    svg += `<text x="${x}" y="14" text-anchor="middle" fill="#9BA1A8" font-size="10" font-family="ui-monospace,monospace">${txt}</text>`;
  }
  svg += `</g>`;
  // now line
  svg += `<line x1="${nowX}" y1="20" x2="${nowX}" y2="${totalH-10}" stroke="#6366F1" stroke-width="1.5" stroke-dasharray="3 3"/>`;
  // rows
  rows.forEach((row, i) => {
    const y = 30 + i * ROW_H;
    svg += `<rect x="0" y="${y-2}" width="${WIDTH}" height="${ROW_H}" fill="${i%2?'rgba(255,255,255,.02)':'transparent'}"/>`;
    const tit = (row.title || row.id || '').slice(0, 30);
    svg += `<text x="8" y="${y + ROW_H/2 + 3}" fill="#E5E7EB" font-size="12" font-weight="600">${esc(tit)}${row.ready?' ★':''}</text>`;
    // bars
    placed.filter(p => (p.run.paper_claim_id || '_orphan') === row.id).forEach(p => {
      const x0 = Math.max(LBL_W, xs(p.start));
      const x1 = Math.max(x0 + 4, xs(p.end));
      const w = x1 - x0;
      const color = colorFor(p.run.status);
      const alpha = p.queued ? .55 : .9;
      svg += `<g class="cp-bar"><title>${esc(p.run.run_name || p.run.id)} · ${esc(p.run.status)}</title>` +
        `<rect x="${x0}" y="${y + 6}" width="${w}" height="${ROW_H-12}" rx="3" fill="${color}" opacity="${alpha}"/>` +
        (w > 60 ? `<text x="${x0 + 6}" y="${y + ROW_H/2 + 4}" font-size="10" fill="#0B0D10" font-family="ui-monospace,monospace">${esc((p.run.run_name || p.run.id).slice(0, Math.max(4, w/7)))}</text>` : '') +
        `</g>`;
    });
  });
  svg += `</svg>`;
  // ETA summary
  const allEnd = Math.max(...placed.map(p => p.end));
  const etaHrs = Math.max(0, (allEnd - NOW) / HOUR);
  b.innerHTML =
    `<div class="cp-h">
      <b>Critical Path</b>
      <span style="color:var(--muted);font-size:12px;margin-left:8px">
        ${gpuCount} GPU lane${gpuCount>1?'s':''} ·
        projected completion in <b>${etaHrs.toFixed(1)}h</b>
        (${rows.length} claim${rows.length>1?'s':''}, ${placed.length} bar${placed.length>1?'s':''})
      </span>
    </div>
    <div class="cp-legend">
      <span><i style="background:#FBBF24"></i>running</span>
      <span><i style="background:#7DD3FC"></i>queued</span>
      <span><i style="background:#34D399"></i>done</span>
      <span><i style="background:#F87171"></i>crashed</span>
    </div>
    <div class="cp-svg-wrap">${svg}</div>`;
}

/* ── one citation card (compact, expandable, with relevance + Approve) ── */
function citationCard(c, approved) {
  // Truncate the author list — research papers can have 1000+ authors.
  const authors = (c.authors || '').split(',').map(s => s.trim()).filter(Boolean);
  const shortA = authors.slice(0, 3).join(', ');
  const moreA = authors.length > 3 ? `, +${authors.length - 3} more` : '';
  // Source link (prefer arxiv, then DOI, then Semantic Scholar)
  let link = '';
  if (c.arxiv_id) link = `https://arxiv.org/abs/${esc(c.arxiv_id)}`;
  else if (c.doi) link = `https://doi.org/${esc(c.doi)}`;
  else if (c.semantic_scholar_id)
    link = `https://www.semanticscholar.org/paper/${esc(c.semantic_scholar_id)}`;
  const abs = (c.abstract_md || '').trim();
  const relev = (c.relevance_md || '').trim();
  return `<div class="rw-card${approved ? ' rw-approved' : ''}">
    <div class="rw-card-hd">
      <a class="rw-title" ${link ? `href="${link}" target="_blank" rel="noopener"` : ''}>${esc(c.title || c.key || '(untitled)')}</a>
      <span class="rw-year">${esc(c.year || '')}</span>
    </div>
    <div class="rw-authors">${esc(shortA)}<span class="rw-more-au">${esc(moreA)}</span></div>
    ${relev ? `<div class="rw-relev">${esc(relev)}</div>` : ''}
    ${abs ? `<div class="rw-abs"><div class="rw-abs-text">${esc(abs)}</div></div>` : ''}
    <div class="rw-actions">
      ${abs.length > 240 ? `<button class="btn xs ghost rw-toggle">more</button>` : ''}
      ${link ? `<a class="btn xs ghost" href="${link}" target="_blank" rel="noopener">↗ open</a>` : ''}
      ${approved ? `<span class="rw-approved-mark">✓ cited</span>`
                  : `<button class="btn xs pri rw-approve" data-key="${esc(c.key)}">✓ Cite</button>`}
    </div>
  </div>`;
}


/* ── Related Work ──────────────────────────────────────────────────────── */
function paintRelatedWork(b) {
  const st = PaperState.state || {};
  const cites = st.citations || [];
  const approved = cites.filter(c => c.user_approved_at);
  const discovered = cites.filter(c => !c.user_approved_at);
  // Auto-kick lit discovery once if nothing has been found yet. Idempotent
  // on the backend (skips if ≥5 citations already exist).
  if (!cites.length && !window.__litAutoFired) {
    window.__litAutoFired = true;
    post('/paper/lit/auto_discover', {}).then(async () => {
      await loadPaperState();
      paintPaperTab(document.querySelector('.paper-wrap').parentElement);
    }).catch(() => {});
  }
  b.innerHTML = `
    <div class="rw-search">
      <input id="rw-q" placeholder="search arxiv (e.g. diffusion ensembles for language)"/>
      <button id="rw-go" class="btn">Search</button>
      <button id="rw-auto" class="btn">Auto-discover for claims</button>
    </div>
    <div class="td-section-h">Approved citations (${approved.length})</div>
    <div class="rw-list">${approved.length ? approved.map(c =>
      citationCard(c, /*approved=*/true)).join('')
      : '<div class="empty2">No approved citations yet.</div>'}</div>
    <div class="td-section-h">Discovered (Lit Agent — awaiting your approval)</div>
    <div class="rw-list">${discovered.slice(0,25).map(c =>
      citationCard(c, /*approved=*/false)).join('')
      || '<div class="empty2">No discovered citations yet — try the search box or auto-discover.</div>'}</div>
    <div class="td-section-h">Differentiation matrix</div>
    <div class="empty2" style="margin-bottom:8px">
      For each approved citation, the agent should note: <i>what's the same / different vs our paper?</i>
      This becomes the heart of the Related Work section.
    </div>
    ${approved.length ? `
      <table class="diff-matrix">
        <thead><tr><th>Paper</th><th>Same as ours</th><th>Different / our contribution</th></tr></thead>
        <tbody>${approved.map(c => `
          <tr>
            <td><b>${esc((c.title || c.key || '').slice(0,80))}</b><br>
                <span style="color:var(--muted);font-size:11px">${esc(c.authors || '')} · ${esc(c.year||'')}</span></td>
            <td><textarea class="diff-cell" data-key="${esc(c.key)}" data-fld="same" placeholder="(empty)">${esc(c.same_md||'')}</textarea></td>
            <td><textarea class="diff-cell" data-key="${esc(c.key)}" data-fld="diff" placeholder="(empty)">${esc(c.diff_md||'')}</textarea></td>
          </tr>`).join('')}
        </tbody>
      </table>
    ` : '<div class="empty2">Approve some citations first to populate the matrix.</div>'}
  `;
  // Wire per-citation Approve / Ignore.
  b.querySelectorAll('.rw-approve').forEach(btn => {
    btn.onclick = async () => {
      btn.disabled = true; btn.textContent = '…';
      await post('/paper/citations/' + encodeURIComponent(btn.dataset.key) + '/approve', {});
      await loadPaperState();
      paintPaperTab(document.querySelector('.paper-wrap').parentElement);
    };
  });
  b.querySelectorAll('.rw-toggle').forEach(btn => {
    btn.onclick = () => {
      const card = btn.closest('.rw-card');
      card.classList.toggle('expanded');
      btn.textContent = card.classList.contains('expanded') ? 'less' : 'more';
    };
  });
  b.querySelector('#rw-go').onclick = async () => {
    const q = b.querySelector('#rw-q').value;
    if (!q) return;
    const r = await post('/paper/lit/search', { query: q });
    const list = (r && r.results) || [];
    b.querySelector('.rw-list:last-child').innerHTML = list.map(p =>
      `<div class="rw-row"><div><b>${esc(p.title)}</b> <span style="color:var(--muted)">${esc(p.year||'')}</span></div>` +
      `<div style="color:var(--text-2);font-size:11px">${esc(p.authors || '')}</div>` +
      `<div style="color:var(--muted);font-size:11px">${esc((p.abstract||'').slice(0,240))}…</div></div>`
    ).join('') || '<div class="empty2">No results.</div>';
  };
  b.querySelector('#rw-auto').onclick = async () => {
    await post('/paper/lit/auto_discover', {});
    await loadPaperState();
    paintPaperTab(document.querySelector('.paper-wrap').parentElement);
  };
}

/* ── Versions + Reviewer Sim + Submit ─────────────────────────────────── */
function paintVersions(b) {
  const st = PaperState.state || {};
  const vs = st.versions || [];
  b.innerHTML = `
    <div class="versions-bar">
      <button class="btn pri" id="ver-pin">Pin current version…</button>
      <button class="btn" id="ver-revsim">🎓 Simulate reviewers</button>
      <button class="btn" id="ver-submit">📦 Submission helper</button>
    </div>
    <div class="td-section-h">Pinned versions (${vs.length})</div>
    <div class="versions-list">${vs.length ? vs.map(v =>
      `<div class="ver-row" data-vid="${esc(v.id)}">
        <div><b>${esc(v.label)}</b> <span class="mono" style="color:var(--accent)">${esc((v.latex_commit_sha||'').slice(0,8))}</span></div>
        <div style="color:var(--muted);font-size:11px">${esc(ago(v.created_at))} · ${
          (v.snapshot_json && v.snapshot_json.claims) ? v.snapshot_json.claims.length : 0
        } claims · ${(v.snapshot_json && v.snapshot_json.figures)
          ? v.snapshot_json.figures.length : 0} figures</div>
        <div class="ver-actions">
          <button class="btn xs ver-diff" data-vid="${esc(v.id)}">Diff vs…</button>
        </div>
      </div>`).join('')
      : '<div class="empty2">No versions pinned yet. Pin one when you reach a milestone (v0 draft, v1 internal review, v2 submitted, etc.).</div>'}</div>
    <div id="ver-revsim-result"></div>
  `;
  b.querySelector('#ver-pin').onclick = async () => {
    const label = await aruiPrompt(
      'Tag this version with a label (e.g. v0-draft, v1-internal, v2-submitted)',
      { title: 'Pin a paper version', defaultValue: 'v0-draft' });
    if (!label) return;
    await post('/paper/versions/pin', { label });
    await loadPaperState();
    paintPaperTab(document.querySelector('.paper-wrap').parentElement);
  };
  b.querySelector('#ver-revsim').onclick = async () => {
    const ok = await aruiConfirm(
      'The council will read the current paper and write three independent ' +
      'NeurIPS-style reviews. They are instructed to be brutal — strengths, ' +
      'weaknesses, missing experiments, score. Each weakness flagged becomes ' +
      'an approvable add_ablation decision in your queue. Takes ~5 min.',
      { title: 'Run reviewer simulation?', okText: 'Run simulation' });
    if (!ok) return;
    const r = await post('/paper/reviewer_sim/run', {});
    document.getElementById('ver-revsim-result').innerHTML =
      '<div class="empty2" style="margin-top:14px">Reviewer simulation started. ' +
      'Results will appear in the Decision Queue as add_ablation rows over the ' +
      'next 2-5 minutes.</div>';
  };
  b.querySelector('#ver-submit').onclick = openSubmitHelper;
  // Wire per-row diff buttons.
  b.querySelectorAll('.ver-diff').forEach(btn => {
    btn.onclick = () => openVersionDiffModal(btn.dataset.vid, vs);
  });
}


async function openVersionDiffModal(vid, allVersions) {
  // Pick the "against" version.
  const others = allVersions.filter(v => v.id !== vid);
  if (!others.length) {
    return aruiAlert('Need at least 2 pinned versions to diff. Pin another version first.');
  }
  const sc = el('div', 'mscrim');
  const m = el('div', 'modal modal-wide');
  m.innerHTML =
    `<div class="modal-hd"><h2>Version diff</h2>` +
    `<button class="iconbtn" id="vdx">✕</button></div>` +
    `<div class="modal-sub">A: <b>${esc(allVersions.find(v=>v.id===vid)?.label||vid)}</b>  vs.  ` +
    `B: <select id="vd-against" style="margin-left:6px">` +
    others.map(o => `<option value="${esc(o.id)}">${esc(o.label)}</option>`).join('') +
    `</select> <span style="color:var(--muted);font-size:11px;margin-left:6px">(or HEAD if nothing selected)</span>` +
    `<button class="btn xs" id="vd-go" style="margin-left:10px">Diff</button></div>` +
    `<div id="vd-out" class="vd-out"><div class="empty2">Pick a version, then Diff.</div></div>` +
    `<div class="modal-actions"><button class="btn" id="vd-close">Close</button></div>`;
  sc.append(m); document.body.append(sc);
  sc.onclick = e => { if (e.target === sc) sc.remove(); };
  m.querySelector('#vdx').onclick = () => sc.remove();
  m.querySelector('#vd-close').onclick = () => sc.remove();
  m.querySelector('#vd-go').onclick = async () => {
    const out = m.querySelector('#vd-out');
    out.innerHTML = '<div class="empty2">computing diff…</div>';
    const against = m.querySelector('#vd-against').value;
    const r = await api(`/paper/versions/${vid}/diff?against=${against}`);
    if (!r.ok) { out.innerHTML = `<div class="empty2">Diff failed: ${esc(r.detail||'?')}</div>`; return; }
    if (!r.files.length) { out.innerHTML = '<div class="empty2">No changes between these versions.</div>'; return; }
    out.innerHTML = r.files.map(f =>
      `<div class="vd-file"><div class="vd-file-h">${esc(f.path)}</div>` +
      `<pre class="vd-file-body">${esc(f.diff).split('\n').map(ln => {
        const col = ln.startsWith('+') ? 'var(--ok)'
                    : ln.startsWith('-') ? 'var(--bad)'
                    : ln.startsWith('@@') ? 'var(--accent)' : 'var(--text-2)';
        return `<span style="color:${col}">${ln}</span>`;
      }).join('\n')}</pre></div>`).join('');
  };
}


/* ── Rebuttal sub-tab (Phase 7) ──────────────────────────────────────────── */
function paintRebuttal(b) {
  const meta = (PaperState.state && PaperState.state.meta) || {};
  if (meta.phase !== 'rebuttal' && meta.phase !== 'submission') {
    b.innerHTML =
      `<div class="empty2" style="padding:20px 0">` +
      `The Rebuttal sub-tab unlocks after you've submitted the paper. ` +
      `Use the <b>Submission helper</b> in Versions to flip the phase, ` +
      `OR click below to enter rebuttal mode manually.</div>` +
      `<div style="margin-top:14px"><button class="btn pri" id="reb-start">Start rebuttal mode</button></div>`;
    b.querySelector('#reb-start').onclick = async () => {
      const ok = await aruiConfirm(
        'Switch paper_phase to "rebuttal"? This unlocks the rebuttal tools (paste reviews → ' +
        'auto-file decisions → rebuttal.tex draft). You can switch back any time.',
        { title: 'Enter rebuttal mode' });
      if (!ok) return;
      await post('/paper/rebuttal/start', {});
      await loadPaperState();
      paintPaperTab(document.querySelector('.paper-wrap').parentElement);
    };
    return;
  }
  b.innerHTML = `
    <div class="td-section-h">Paste reviewer reviews</div>
    <div class="empty2" style="margin-bottom:8px">
      One textarea per reviewer. The council reads each and files one decision
      per actionable concern (max 6 per reviewer).
    </div>
    <div class="reb-list" id="reb-list">
      <textarea class="reb-input" placeholder="Reviewer 1 — paste the full review here"></textarea>
      <textarea class="reb-input" placeholder="Reviewer 2 — paste the full review here"></textarea>
      <textarea class="reb-input" placeholder="Reviewer 3 — paste the full review here"></textarea>
    </div>
    <div style="margin-top:10px">
      <button class="btn" id="reb-add">+ another reviewer</button>
      <button class="btn pri" id="reb-parse">Parse with council → file decisions</button>
    </div>
    <div id="reb-result" style="margin-top:14px"></div>
  `;
  b.querySelector('#reb-add').onclick = () => {
    const ta = document.createElement('textarea');
    ta.className = 'reb-input';
    ta.placeholder = 'Reviewer N — paste the full review here';
    b.querySelector('#reb-list').appendChild(ta);
  };
  b.querySelector('#reb-parse').onclick = async () => {
    const reviews = Array.from(b.querySelectorAll('.reb-input'))
      .map(t => t.value.trim()).filter(s => s.length > 30);
    if (!reviews.length) {
      return aruiAlert('Paste at least one substantive review (>30 chars).');
    }
    const out = b.querySelector('#reb-result');
    out.innerHTML = '<div class="empty2">Parsing — this may take ~30s per reviewer…</div>';
    const r = await post('/paper/rebuttal/parse', { reviews });
    if (r.ok) {
      out.innerHTML = `<div style="color:var(--ok)">Filed ${r.filed} decisions ` +
        `from ${reviews.length} reviews. Open the Today tab to triage.</div>`;
    } else {
      out.innerHTML = `<div style="color:var(--bad)">Failed: ${esc(r.detail || '?')}</div>`;
    }
  };
}


/* ── Share sub-tab (read-only collaborator link) ─────────────────────────── */
async function paintShare(b) {
  b.innerHTML = `
    <div class="td-section-h">Read-only share link</div>
    <div class="empty2" style="margin-bottom:10px">
      Generates a token-gated URL your advisor can open without an account.
      Shows the current PDF + Claim Coverage + Decision Queue (read-only).
      Anyone with the URL can read; rotate the token to revoke.
    </div>
    <div class="share-row">
      <input id="share-url" class="share-url mono" readonly value="(no token yet)" />
      <button class="btn" id="share-copy">Copy</button>
      <button class="btn pri" id="share-gen">Generate / rotate</button>
      <button class="btn ghost" id="share-revoke">Revoke</button>
    </div>
    <div id="share-status" class="empty2" style="margin-top:10px"></div>
  `;
  b.querySelector('#share-gen').onclick = async () => {
    const r = await post('/paper/share/token', {});
    if (r.token) {
      const full = window.location.origin + r.url;
      b.querySelector('#share-url').value = full;
      b.querySelector('#share-status').innerHTML =
        `<span style="color:var(--ok)">✓ token generated. Share this URL.</span>`;
    }
  };
  b.querySelector('#share-copy').onclick = () => {
    const u = b.querySelector('#share-url').value;
    if (!u || u.startsWith('(')) { aruiAlert('Generate a token first.'); return; }
    navigator.clipboard.writeText(u);
    b.querySelector('#share-status').innerHTML =
      `<span style="color:var(--ok)">✓ copied to clipboard.</span>`;
  };
  b.querySelector('#share-revoke').onclick = async () => {
    const ok = await aruiConfirm('Revoke the share link? Existing URL will stop working.',
      { title: 'Revoke share token', danger: true });
    if (!ok) return;
    await fetch('/api/paper/share/token', { method: 'DELETE' });
    b.querySelector('#share-url').value = '(no token yet)';
    b.querySelector('#share-status').innerHTML =
      `<span style="color:var(--muted)">Token revoked.</span>`;
  };
}

async function openSubmitHelper() {
  const sc = el('div', 'mscrim');
  const m = el('div', 'modal submit-helper');
  m.innerHTML =
    `<div class="modal-hd"><h2>Submission helper</h2>` +
    `<button class="iconbtn" id="sbx">✕</button></div>` +
    `<div class="modal-sub">Pre-submission checks before you upload to ` +
    `OpenReview / CMT. arXiv upload is manual for now.</div>` +
    `<div class="sb-check" id="sb-anon">` +
      `<div class="sb-check-h"><span class="sb-num">1</span><b>Anonymization scan</b>` +
        `<span class="sb-status" id="sb-anon-st">checking…</span></div>` +
      `<div class="sb-check-bd" id="sb-anon-bd">scanning paper/ for author names, ` +
        `affiliations, GitHub URLs, \\thanks blocks, ORCID…</div>` +
    `</div>` +
    `<div class="sb-check" id="sb-pages">` +
      `<div class="sb-check-h"><span class="sb-num">2</span><b>Page count</b>` +
        `<span class="sb-status" id="sb-pages-st">checking…</span></div>` +
      `<div class="sb-check-bd" id="sb-pages-bd">counting pages in current PDF…</div>` +
    `</div>` +
    `<div class="sb-check" id="sb-pdf">` +
      `<div class="sb-check-h"><span class="sb-num">3</span><b>PDF compiled?</b>` +
        `<span class="sb-status" id="sb-pdf-st">checking…</span></div>` +
      `<div class="sb-check-bd" id="sb-pdf-bd">…</div>` +
    `</div>` +
    `<div class="modal-actions">` +
      `<button class="btn" id="sb-cancel">Close</button>` +
      `<button class="btn pri" id="sb-bundle" disabled>Bundle .zip & pin v-submitted</button>` +
    `</div>`;
  sc.append(m); document.body.append(sc);
  sc.onclick = e => { if (e.target === sc) sc.remove(); };
  m.querySelector('#sbx').onclick = () => sc.remove();
  m.querySelector('#sb-cancel').onclick = () => sc.remove();
  // Fire the three checks in parallel.
  const setCheck = (id, status, text) => {
    const st = m.querySelector(`#${id}-st`); const bd = m.querySelector(`#${id}-bd`);
    if (st) st.className = `sb-status ${status}`;
    if (st) st.textContent = { ok: '✓ pass', warn: '⚠ warn', fail: '✗ fail', running: '…' }[status] || status;
    if (bd) bd.innerHTML = text;
  };
  const blocking = { anon: true, pages: false, pdf: true };  // bundle gated by these
  const results = {};
  await Promise.all([
    (async () => {
      try {
        const r = await post('/paper/submit/anonymize_check', {});
        results.anon = r;
        if (r.ok) setCheck('sb-anon', 'ok', `No de-anon findings (${r.files_scanned||0} files scanned).`);
        else setCheck('sb-anon', 'fail',
          `<b>${r.findings.length} de-anon findings</b> across ${r.files_scanned} files. ` +
          `<details><summary>Show first 12</summary>` +
          r.findings.slice(0,12).map(f => `<div class="mono" style="font-size:11px;color:var(--text-2)">${esc(f.path)}:${f.line} — ${esc(f.kind)} → <span style="color:var(--bad)">${esc(f.match)}</span></div>`).join('') +
          `</details>`);
      } catch (e) { results.anon = {ok:false}; setCheck('sb-anon','fail','anonymize_check failed: '+e); }
    })(),
    (async () => {
      try {
        const r = await api('/paper/submit/page_count');
        results.pages = r;
        const n = r.pages || 0;
        if (!n) setCheck('sb-pages','warn','Could not determine page count (pdfinfo missing?).');
        else if (n > 12) setCheck('sb-pages','warn',`${n} pages — most venues require 9-10 main + refs.`);
        else setCheck('sb-pages','ok',`${n} pages.`);
      } catch (e) { setCheck('sb-pages','warn','page count check skipped'); }
    })(),
    (async () => {
      try {
        const r = await api('/paper/build_log');
        results.pdf = r;
        if (r && r.pdf_exists) setCheck('sb-pdf','ok','PDF on disk and ready.');
        else setCheck('sb-pdf','fail','No PDF compiled. Click Rebuild on the viewer first.');
      } catch (e) { setCheck('sb-pdf','fail','/paper/build_log failed'); }
    })(),
  ]);
  const canBundle = (results.anon && results.anon.ok) && (results.pdf && results.pdf.pdf_exists);
  const bundleBtn = m.querySelector('#sb-bundle');
  bundleBtn.disabled = !canBundle;
  bundleBtn.title = canBundle ? '' : 'Fix the failing checks first.';
  bundleBtn.onclick = async () => {
    bundleBtn.disabled = true; bundleBtn.textContent = 'Bundling…';
    try {
      const r = await post('/paper/submit/bundle', {});
      if (r.ok) {
        await aruiAlert(
          `Bundle written to ${r.zip} (${(r.size_bytes/1024).toFixed(0)} KB). ` +
          `Version pinned: ${r.version_id}. Download from the node: paper/${r.zip}`,
          { title: 'Submission bundled' });
        sc.remove();
      } else {
        bundleBtn.disabled = false; bundleBtn.textContent = 'Bundle .zip & pin v-submitted';
        aruiAlert('Bundle failed: ' + (r.detail || 'unknown'), { title: 'Bundle error' });
      }
    } catch (e) {
      bundleBtn.disabled = false; bundleBtn.textContent = 'Bundle .zip & pin v-submitted';
      aruiAlert('Bundle failed: ' + e, { title: 'Bundle error' });
    }
  };
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
    const warns = d.warnings || [];
    const warnsHtml = warns.length
      ? '<div class="sys-warns">' + warns.map(w =>
          `<div class="sys-warn sys-warn-${esc(w.severity)}">` +
          `<b>${esc(w.severity)}</b> ${esc(w.msg)}</div>`).join('') +
        '</div>'
      : '';
    c.querySelector('.sys-wrap').innerHTML =
      warnsHtml +
      `<div class="sys-sec">GPUs (${gpus.length})</div>` +
      `<div class="sys-gpus">${gpuCards ||
        '<div class="empty2">no GPU data</div>'}</div>` +
      `<div class="sys-sec">Host</div><div class="sys-cards">` +
      cards.map(([k, v]) => `<div class="sys-card"><div class="k">${k}` +
        `</div><div class="v">${esc(v)}</div></div>`).join('') + '</div>' +
      '<div class="sys-sec">Maintenance</div>' +
      '<div class="sys-maint">' +
      '<p class="sys-maint-help">' +
      'Reclaim disk by deleting stdout/stderr logs and checkpoints for runs ' +
      'older than 2 days AND in the bottom half by your metric. Run rows, ' +
      'headline metrics, and council reviews stay intact.</p>' +
      '<button class="btn pri" id="sys-purge">Purge old run logs…</button>' +
      '<span class="sys-purge-status"></span>' +
      '</div>' +
      '<div class="sys-maint">' +
      '<p class="sys-maint-help">' +
      '<b>Aggressive:</b> keep only the SOTA (best-metric) run\'s ' +
      'checkpoint, delete every other run\'s artifacts (logs + .pt files). ' +
      'Run rows + headline metrics + reviews stay — only the bulky ' +
      'on-disk state goes away. Use when disk is critical.</p>' +
      '<button class="btn pri" id="sys-purge-sota">Keep SOTA only…</button>' +
      '<span class="sys-purge-sota-status"></span>' +
      '</div>';
    const pb = c.querySelector('#sys-purge');
    if (pb) pb.onclick = () => openPurgeOldRunLogs(c);
    const pbs = c.querySelector('#sys-purge-sota');
    if (pbs) pbs.onclick = () => openPurgeKeepSotaOnly(c);
  };
  tick();
  addTimer(setInterval(tick, 4000));
}

/* Modal: preview which runs would be purged, confirm, then run cleanup.
 * Lives on the System Stats page since that's where the user notices the
 * disk-full pain first. The age + bottom-percent sliders let the user
 * relax the filter when the default 2-day rule frees nothing because all
 * their runs are recent. */
async function openPurgeOldRunLogs(sysContainer) {
  const sc = el('div', 'mscrim');
  const m  = el('div', 'modal');
  m.innerHTML = '<div class="skel" style="height:240px"></div>';
  sc.append(m); document.body.append(sc);
  sc.onclick = e => { if (e.target === sc) sc.remove(); };
  const state = { age: 2, pct: 0.5, preview: null };
  const mb = b => (b >= 1e9 ? (b / 1e9).toFixed(2) + ' GB'
    : (b / 1e6).toFixed(1) + ' MB');
  const render = () => {
    const p = state.preview;
    const rows = !p ? '' : (p.runs || []).slice(0, 30).map(r =>
      `<tr><td class="mono">${esc(r.name)}</td>` +
      `<td>${esc(r.status)}</td>` +
      `<td class="r">${r.headline_metric != null
        ? r.headline_metric.toFixed(4) : '—'}</td>` +
      `<td class="r mono">${mb(r.log_bytes)}</td></tr>`).join('');
    const more = p && (p.runs || []).length > 30
      ? `<p class="modal-sub">…and ${(p.runs || []).length - 30} more.</p>`
      : '';
    const tbl = !p
      ? '<div class="skel" style="height:120px"></div>'
      : (rows
          ? `<table class="modal-tbl">` +
            `<thead><tr><th>Run</th><th>Status</th><th>Metric</th>` +
            `<th>Reclaim</th></tr></thead>` +
            `<tbody>${rows}</tbody></table>` + more
          : '<div class="empty2">Nothing eligible at these thresholds. ' +
            'Try lowering the days threshold.</div>');
    const headline = !p
      ? '<p class="modal-sub">computing…</p>'
      : `<p class="modal-sub">Would delete artifacts for <b>${p.eligible}</b> ` +
        `run${p.eligible === 1 ? '' : 's'}, freeing about ` +
        `<b>${mb(p.bytes_freeable)}</b>. ` +
        'Run rows, headline metrics, and council reviews are KEPT — only ' +
        'on-disk artifacts (stdout, checkpoints, sample dirs) are removed.</p>';
    m.innerHTML =
      '<div class="modal-hd"><h2>Purge old run logs &amp; checkpoints</h2>' +
      '<button class="iconbtn" id="pp-x">✕</button></div>' +
      headline +
      '<div class="pp-controls">' +
        '<label class="pp-ctrl">Min age: ' +
        `<select id="pp-age">` +
          [0.5, 1, 2, 3, 7, 14, 30].map(v =>
            `<option value="${v}"${v === state.age ? ' selected' : ''}>` +
            `${v < 1 ? (v * 24) + 'h' : v + 'd'}</option>`).join('') +
        '</select></label>' +
        '<label class="pp-ctrl">Bottom: ' +
        `<select id="pp-pct">` +
          [0.25, 0.5, 0.75, 0.9].map(v =>
            `<option value="${v}"${v === state.pct ? ' selected' : ''}>` +
            `${Math.round(v * 100)}%</option>`).join('') +
        '</select></label>' +
      '</div>' +
      tbl +
      '<div class="modal-actions">' +
      `<button class="btn pri" id="pp-go"${p && p.eligible ? '' : ' disabled'}>` +
      (p ? `Delete ${p.eligible} run${p.eligible === 1 ? '' : 's'} · ` +
            `${mb(p.bytes_freeable)}` : 'Delete') + '</button>' +
      '<button class="btn" id="pp-cancel">Cancel</button>' +
      '<span class="set-status" id="pp-st"></span>' +
      '</div>';
    m.querySelector('#pp-x').onclick      = () => sc.remove();
    m.querySelector('#pp-cancel').onclick = () => sc.remove();
    m.querySelector('#pp-age').onchange   = e => {
      state.age = parseFloat(e.target.value); loadPreview(); };
    m.querySelector('#pp-pct').onchange   = e => {
      state.pct = parseFloat(e.target.value); loadPreview(); };
    const go = m.querySelector('#pp-go');
    if (go && !go.disabled) {
      go.onclick = async () => {
        const confirmed = await aruiConfirm(
          `Permanently delete on-disk artifacts (logs + checkpoints) for ` +
          `${p.eligible} runs? Reclaims about ${mb(p.bytes_freeable)}. ` +
          `Headline metrics and reviews are kept.`,
          { title: 'Purge old run artifacts?', danger: true,
            okText: `Delete ${p.eligible} runs` });
        if (!confirmed) return;
        go.disabled = true; go.textContent = 'Deleting…';
        const st = m.querySelector('#pp-st');
        try {
          const r = await post('/runs/cleanup',
            { min_age_days: state.age, bottom_pct: state.pct });
          st.textContent = `  freed ${mb(r.bytes_freed || 0)} ` +
            `(${r.deleted} run${r.deleted === 1 ? '' : 's'})  ✓`;
          st.style.color = 'var(--ok)';
          go.textContent = 'Done';
          const sps = sysContainer
            && sysContainer.querySelector('.sys-purge-status');
          if (sps) {
            sps.textContent = `  freed ${mb(r.bytes_freed || 0)}`;
            sps.style.color = 'var(--ok)';
          }
          setTimeout(() => sc.remove(), 1700);
        } catch (e) {
          st.textContent = '  failed: ' + esc(e);
          st.style.color = 'var(--bad)';
          go.disabled = false; go.textContent = 'Retry';
        }
      };
    }
  };
  const loadPreview = async () => {
    state.preview = null;
    render();
    try {
      state.preview = await api(
        `/runs/cleanup/preview?min_age_days=${state.age}` +
        `&bottom_pct=${state.pct}`);
    } catch (e) {
      m.innerHTML = '<p>Preview failed: ' + esc(e) + '</p>';
      return;
    }
    render();
  };
  loadPreview();
}

/* Modal: keep ONLY the SOTA (best-metric) run's checkpoint, blow away
 * every other run's on-disk state. The DB row + metric + reviews stay,
 * so Analysis / Lessons keep working. This is the "I'm out of disk
 * NOW" button. */
async function openPurgeKeepSotaOnly(sysContainer) {
  const sc = el('div', 'mscrim');
  const m  = el('div', 'modal');
  m.innerHTML = '<div class="skel" style="height:240px"></div>';
  sc.append(m); document.body.append(sc);
  sc.onclick = e => { if (e.target === sc) sc.remove(); };
  const mb = b => (b >= 1e9 ? (b / 1e9).toFixed(2) + ' GB'
    : (b / 1e6).toFixed(1) + ' MB');
  let p;
  try { p = await api('/runs/cleanup/preview_sota'); }
  catch (e) { m.innerHTML = '<p>Preview failed: ' + esc(e) + '</p>'; return; }
  const kept = (p.kept_run_ids || []).slice(0, 4).join(', ') +
               (p.kept_run_ids && p.kept_run_ids.length > 4
                 ? ', …' : '');
  const rows = (p.runs || []).slice(0, 30).map(r =>
    `<tr><td class="mono">${esc(r.name)}</td>` +
    `<td>${esc(r.status)}</td>` +
    `<td class="r">${r.headline_metric != null
      ? r.headline_metric.toFixed(4) : '—'}</td>` +
    `<td class="r mono">${mb(r.log_bytes)}</td></tr>`).join('');
  const more = (p.runs || []).length > 30
    ? `<p class="modal-sub">…and ${(p.runs || []).length - 30} more.</p>` : '';
  m.innerHTML =
    '<div class="modal-hd"><h2>Keep SOTA only · purge the rest</h2>' +
    '<button class="iconbtn" id="ps-x">✕</button></div>' +
    `<p class="modal-sub">Would delete artifacts for <b>${p.eligible}</b> ` +
    `run${p.eligible === 1 ? '' : 's'}, freeing about ` +
    `<b>${mb(p.bytes_freeable)}</b>. ` +
    'Run rows, metrics, and reviews are kept — only on-disk artifacts ' +
    '(stdout, .pt checkpoints, output dirs) are removed. ' +
    `Kept runs: <code>${esc(kept || 'none yet')}</code>.</p>` +
    (rows ? `<table class="modal-tbl">` +
      `<thead><tr><th>Run</th><th>Status</th><th>Metric</th>` +
      `<th>Reclaim</th></tr></thead>` +
      `<tbody>${rows}</tbody></table>` + more
      : '<div class="empty2">Nothing on disk except the SOTA. Trim!</div>') +
    '<div class="modal-actions">' +
    `<button class="btn pri" id="ps-go"${p.eligible ? '' : ' disabled'}>` +
    `Delete ${p.eligible} run${p.eligible === 1 ? '' : 's'} · ` +
    `${mb(p.bytes_freeable)}</button>` +
    '<button class="btn" id="ps-cancel">Cancel</button>' +
    '<span class="set-status" id="ps-st"></span>' +
    '</div>';
  m.querySelector('#ps-x').onclick      = () => sc.remove();
  m.querySelector('#ps-cancel').onclick = () => sc.remove();
  const go = m.querySelector('#ps-go');
  if (go && !go.disabled) {
    go.onclick = async () => {
      const confirmed = await aruiConfirm(
        `Permanently delete on-disk artifacts (logs + checkpoints) for ` +
        `${p.eligible} runs, keeping only the SOTA? Reclaims about ` +
        `${mb(p.bytes_freeable)}. Metrics and reviews are preserved.`,
        { title: 'Keep SOTA only?', danger: true,
          okText: `Yes, purge ${p.eligible} runs` });
      if (!confirmed) return;
      go.disabled = true; go.textContent = 'Deleting…';
      const st = m.querySelector('#ps-st');
      try {
        const r = await post('/runs/cleanup_sota', {});
        st.textContent = `  freed ${mb(r.bytes_freed || 0)} ` +
          `(${r.deleted} run${r.deleted === 1 ? '' : 's'})  ✓`;
        st.style.color = 'var(--ok)';
        go.textContent = 'Done';
        const sps = sysContainer
          && sysContainer.querySelector('.sys-purge-sota-status');
        if (sps) {
          sps.textContent = `  freed ${mb(r.bytes_freed || 0)}`;
          sps.style.color = 'var(--ok)';
        }
        setTimeout(() => sc.remove(), 1700);
      } catch (e) {
        st.textContent = '  failed: ' + esc(e);
        st.style.color = 'var(--bad)';
        go.disabled = false; go.textContent = 'Retry';
      }
    };
  }
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
        const ok = await aruiConfirm(
          'This key will no longer be able to SSH into the node. Make sure ' +
          'you have another working key before deleting.',
          { title: 'Delete this SSH key?', danger: true,
            okText: 'Delete key' });
        if (!ok) return;
        const r = await post('/authkeys/delete', { fingerprint: b.dataset.fp });
        if (!r.ok) { await aruiAlert(r.error || 'Delete failed.',
                                     { title: 'Could not delete key' }); }
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

// Two-way "active run hover" — chart line ↔ table row. Hovering a line in
// any panel emphasizes that line and scrolls/highlights the matching row
// in the runs table on the left. Hovering a row emphasizes that run's
// lines in every panel.
const hoverBus = {
  runId: null, frame: 0, listeners: new Set(),
  set(rid) {
    if (rid === this.runId) return;
    this.runId = rid;
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = 0;
      this.listeners.forEach(fn => fn(this.runId));
    });
  },
};
// Side-effect: highlight + scroll the matching row in the table.
hoverBus.listeners.add(rid => {
  document.querySelectorAll('.anav2-table tbody tr').forEach(tr => {
    const isHover = !!rid && tr.dataset.rid === rid;
    tr.classList.toggle('hover', isHover);
  });
  if (rid) {
    const tr = document.querySelector(
      `.anav2-table tbody tr[data-rid="${CSS.escape(rid)}"]`);
    if (tr) {
      const rect = tr.getBoundingClientRect();
      const tw = tr.parentElement.parentElement.parentElement; // tablewrap
      if (tw && tw.scrollHeight > tw.clientHeight) {
        const twRect = tw.getBoundingClientRect();
        if (rect.top < twRect.top || rect.bottom > twRect.bottom) {
          tr.scrollIntoView({ block: 'center', behavior: 'smooth' });
        }
      }
    }
  }
  // Tell every panel to redraw with the new hover emphasis.
  if (AnaState && AnaState.charts) {
    AnaState.charts.forEach(ch => ch.draw && ch.draw());
  }
});

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
      cursorBus.clear();
      hoverBus.set(null);
      this._dragStart = null;
      this._drawOverlay(null);
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
    // series — emphasize the line whose run is currently hovered. Subtle
    // dim (0.55) on inactive lines so context isn't lost; thicken active.
    // Draw active line LAST so it's on top.
    const activeRun = (typeof hoverBus !== 'undefined') ? hoverBus.runId : null;
    const hasMany = ser.length > 1;
    // Bridge short null gaps (<= MAX_GAP buckets) so a single missing
    // sample doesn't visually break the line. Longer gaps stay broken
    // so real data dropouts remain truthful to the user.
    const MAX_GAP = 8;   // bridge cosmetic dropouts ~1.6% of x range
    const drawSeries = (s, idx) => {
      const ys = smoothed[idx];
      const isActive = activeRun && s.run_id === activeRun;
      c.globalAlpha = (!activeRun || !hasMany) ? 1.0
        : (isActive ? 1.0 : 0.55);
      c.strokeStyle = s.color || PALETTE_BIG[idx % PALETTE_BIG.length];
      c.lineWidth = isActive ? 2.8 : (s.dashed ? 1.4 : 1.8);
      if (s.dashed) c.setLineDash([4, 3]); else c.setLineDash([]);
      let pen = false;
      let gapLen = 0;     // consecutive nulls since last drawn point
      let lastX = null, lastY = null;
      c.beginPath();
      for (let i = 0; i < s.x.length; i++) {
        const xi = s.x[i], yi = ys[i];
        if (xi == null || yi == null || isNaN(yi)) {
          gapLen++;
          continue;
        }
        const px = X(xi), py = Y(yi);
        if (!pen) {
          c.moveTo(px, py); pen = true;
        } else if (gapLen > MAX_GAP) {
          // long gap — drop the pen, start a new segment so the gap is honest
          c.moveTo(px, py);
        } else {
          // short or zero-length gap — connect through to keep the line smooth
          c.lineTo(px, py);
        }
        gapLen = 0; lastX = px; lastY = py;
      }
      c.stroke(); c.setLineDash([]);
    };
    // Draw inactive lines first, active line last (so it's on top)
    const inactiveIdx = [], activeIdx = [];
    ser.forEach((s, i) => (activeRun && s.run_id === activeRun ? activeIdx : inactiveIdx).push(i));
    inactiveIdx.forEach(i => drawSeries(ser[i], i));
    activeIdx.forEach(i => drawSeries(ser[i], i));
    c.globalAlpha = 1.0;
    // Wipe overlay so we don't leave a stale crosshair
    this._drawOverlay(cursorBus.x);
  }
  _onMove(e) {
    const r = this.overlay.getBoundingClientRect();
    const mx = e.clientX - r.left;
    const my = e.clientY - r.top;
    if (!this._lastDraw) return;
    const { xlo, xhi, pad, w, ser, smoothed, Ylo, Yhi, log, h } = this._lastDraw;
    const xv = xlo + (mx - pad.l) / (w - pad.l - pad.r) * (xhi - xlo);
    cursorBus.set(xv, this.xKey);
    // Pick the series whose nearest point is closest to (mx, my) in
    // screen pixels — that's the line the user is "hovering over".
    const tf = v => log ? Math.log10(v) : v;
    const Y = v => pad.t + (1 - (tf(v) - Ylo) / (Yhi - Ylo))
      * (h - pad.t - pad.b);
    const X = v => pad.l + (v - xlo) / (xhi - xlo) * (w - pad.l - pad.r);
    let bestRun = null, bestDist = Infinity;
    ser.forEach((s, idx) => {
      const ys = smoothed[idx];
      for (let i = 0; i < s.x.length; i++) {
        if (s.x[i] == null || ys[i] == null || isNaN(ys[i])) continue;
        const px = X(s.x[i]);
        const py = Y(ys[i]);
        const dx = px - mx, dy = py - my;
        const d = dx*dx + dy*dy;
        if (d < bestDist) { bestDist = d; bestRun = s.run_id; }
      }
    });
    // Only treat as "hovered" within 60 px of a real point (so empty
    // chart area doesn't emit spurious hovers).
    if (bestDist <= 60*60) hoverBus.set(bestRun);
    else hoverBus.set(null);
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
  AnaState.container = c;          // module-wide reference so other fns can use it
  // Add fallback class so the layout works even if the browser doesn't
  // support :has() (CSS rule .viewpane:has(.anav2){padding:0}).
  c.classList.add('full-bleed');
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
    const ok = await aruiConfirm(
      'This replaces your current panel set with the project-aware ' +
      'defaults (7 panels: project metric, train/val loss, train/val acc, ' +
      'learning rate, time per step).',
      { title: 'Reset panels to defaults?', okText: 'Reset panels' });
    if (!ok) return;
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
    tr.dataset.rid = r.id;
    tr.onmouseenter = () => hoverBus.set(r.id);
    tr.onmouseleave = () => hoverBus.set(null);
    // Click anywhere on the row opens the drawer — same UX as the
    // Dashboard runs table. Buttons (eye, star, solo) keep their own
    // handlers; we ignore clicks that target a button.
    tr.onclick = (e) => {
      if (e.target.closest('button')) return;
      openDrawer(r.id);
    };
    tr.style.cursor = 'pointer';
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
  const pop = el('div', 'anav2-popover anav2-bulkpop');
  // Per-status counts so labels say "Show kept (12)"
  const byStatus = {};
  (S.runs || []).forEach(r => {
    byStatus[r.status] = (byStatus[r.status] || 0) + 1;
  });
  const statusBtn = (st, label, dot) => {
    const n = byStatus[st] || 0;
    return `<button class="anav2-pop-it anav2-pop-status" data-a="status:${st}"` +
           ` ${n?'':'disabled'} title="Show only ${st} runs">` +
           `<span class="anav2-pop-dot s-${st}"></span>` +
           `<span class="anav2-pop-lbl">Show ${esc(label)}</span>` +
           `<span class="anav2-pop-n">${n}</span></button>`;
  };
  pop.innerHTML =
    `<div class="anav2-pop-h">Bulk actions</div>` +
    `<button class="anav2-pop-it" data-a="all">Make all visible` +
      `<span class="anav2-pop-n">${(S.runs||[]).length}</span></button>` +
    `<button class="anav2-pop-it" data-a="none">Make all hidden</button>` +
    `<button class="anav2-pop-it" data-a="visible">` +
      `Make filtered visible</button>` +
    `<button class="anav2-pop-it" data-a="invert">Invert</button>` +
    `<div class="anav2-pop-sep"></div>` +
    `<div class="anav2-pop-h">Show only…</div>` +
    statusBtn('kept', 'kept', '#34D399') +
    statusBtn('running', 'running', '#FBBF24') +
    statusBtn('crashed', 'crashed', '#F43F5E') +
    statusBtn('discarded', 'discarded', '#F87171') +
    `<div class="anav2-pop-sep"></div>` +
    `<button class="anav2-pop-it" data-a="add-best">` +
      `Add top-5 by metric (best on frontier)</button>` +
    `<button class="anav2-pop-it" data-a="add-recent">` +
      `Add 5 most-recent kept</button>`;
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
  pop.querySelectorAll('button[data-a]').forEach(b => b.onclick = (ev) => {
    const filtered = _sortedAnaRuns();
    const act = b.dataset.a;
    const runs = S.runs || [];
    // Shift-click adds to current selection; plain click replaces (per
    // W&B convention for these "show only X" actions).
    const add = ev.shiftKey;
    if (act === 'all') runs.forEach(r => AnaState.selected.add(r.id));
    else if (act === 'none') AnaState.selected.clear();
    else if (act === 'visible') {
      if (!add) AnaState.selected.clear();
      filtered.forEach(r => AnaState.selected.add(r.id));
    }
    else if (act === 'invert') {
      runs.forEach(r => {
        if (AnaState.selected.has(r.id)) AnaState.selected.delete(r.id);
        else AnaState.selected.add(r.id);
      });
    }
    else if (act && act.startsWith('status:')) {
      const want = act.slice('status:'.length);
      if (!add) AnaState.selected.clear();
      runs.filter(r => r.status === want)
          .forEach(r => AnaState.selected.add(r.id));
    }
    else if (act === 'add-best') {
      // Top-5 kept by direction-aware metric, ignoring null metrics.
      const dir = (S.project && S.project.metric_direction === 'minimize')
                ? 1 : -1;
      const kept = runs
        .filter(r => r.headline_metric != null
                  && (r.status === 'kept' || r.status === 'success'))
        .sort((a,b) => dir * (a.headline_metric - b.headline_metric))
        .slice(0, 5);
      kept.forEach(r => AnaState.selected.add(r.id));
    }
    else if (act === 'add-recent') {
      const recent = runs
        .filter(r => r.status === 'kept' || r.status === 'success')
        .sort((a,b) => (b.ended_at||b.created_at||'')
                       .localeCompare(a.ended_at||a.created_at||''))
        .slice(0, 5);
      recent.forEach(r => AnaState.selected.add(r.id));
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
  // When a panel is expanded, hide the rest so it fills the grid area.
  // The left runs table is unaffected.
  if (AnaState.expandedPanel) {
    const target = AnaState.panels.find(p => p.id === AnaState.expandedPanel);
    if (target) {
      grid.classList.add('expanded');
      grid.append(buildPanel(c, target));
      refreshAllPanels();
      return;
    }
    // expanded panel no longer exists — reset
    AnaState.expandedPanel = null;
  }
  grid.classList.remove('expanded');
  AnaState.panels.forEach(p => grid.append(buildPanel(c, p)));
  refreshAllPanels();
}

function buildPanel(c, p) {
  const card = el('div', 'anav2-panel' + (p.width === 'full' ? ' full' : ''));
  card.dataset.pid = p.id;
  // Two-row header: title on its own row (so it's never squeezed), then
  // a control row below it.
  const isExpanded = AnaState.expandedPanel === p.id;
  const titleRow = el('div', 'anav2-panel-titlerow');
  titleRow.innerHTML =
    `<div class="anav2-panel-title">${esc(p.title)}</div>` +
    `<div class="anav2-panel-keys mono">${esc((p.y_keys||[]).join(' · '))}</div>` +
    `<div class="anav2-panel-ctrls-r">` +
      `<button class="anav2-ctrl-btn anav2-expand${isExpanded?' on':''}" title="${isExpanded?'Minimize panel':'Expand panel'}">${isExpanded?'⤡':'⤢'}</button>` +
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
  card.querySelector('.anav2-expand').onclick = () => {
    AnaState.expandedPanel = (AnaState.expandedPanel === p.id) ? null : p.id;
    renderAnaPanels(c);
  };
  card.querySelector('.anav2-edit').onclick = () => openEditPanelModal(c, p);
  card.querySelector('.anav2-rm').onclick = () => {
    AnaState.panels = AnaState.panels.filter(x => x.id !== p.id);
    if (AnaState.expandedPanel === p.id) AnaState.expandedPanel = null;
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
      _renderEmptyPanelLegend(legend, p, runIds, AnaState.container);
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

// When a panel has no data, surface WHY and offer a click-to-fix where
// possible. Three cases:
//   A) The key isn't logged anywhere in the project — agent isn't tracking it.
//   B) The key IS logged by other runs — offer to add them.
//   C) Selected runs are all still running and just haven't logged yet.
async function _renderEmptyPanelLegend(legend, p, runIds, c) {
  const keys = p.y_keys || [];
  if (!keys.length) {
    legend.innerHTML = '<span class="anav2-legend-empty">' +
      'Click ✎ to pick metrics for this panel.</span>';
    return;
  }
  const projectKeys = new Set(AnaState.keys || []);
  const missing = keys.filter(k => !projectKeys.has(k));
  if (missing.length === keys.length) {
    // Case A — none of the panel's keys exist in the project.
    legend.innerHTML = '<span class="anav2-legend-empty">' +
      `<code>${esc(missing.join(', '))}</code> ` +
      `${missing.length>1?'aren\'t':'isn\'t'} logged by any run in this ` +
      `project. The agent may not be tracking ${missing.length>1?'them':'it'} ` +
      `yet.</span>`;
    return;
  }
  // Some keys exist — find runs that DO log the first key, excluding the
  // currently-selected set.
  const k0 = keys.find(k => projectKeys.has(k));
  let coverage = [];
  try {
    const d = await api('/metrics/key_coverage?key=' +
      encodeURIComponent(k0) + '&limit=50');
    coverage = (d && d.run_ids) || [];
  } catch (e) { coverage = []; }
  const candidates = coverage.filter(r => !AnaState.selected.has(r.id));
  if (!candidates.length) {
    // The selected runs DO log this metric — just don't have data yet.
    // Most likely: they're running and haven't reached eval.
    const allRunning = runIds.every(rid => {
      const r = (S.runs || []).find(x => x.id === rid);
      return r && r.status === 'running';
    });
    legend.innerHTML = '<span class="anav2-legend-empty">' +
      (allRunning
        ? `Selected runs are still training — <code>${esc(k0)}</code> ` +
          `usually only logs at evaluation. Wait or pick a finished run.`
        : `No data for <code>${esc(k0)}</code> in the selected runs yet.`) +
      '</span>';
    return;
  }
  // Case B — other runs DO log this; offer to add them.
  legend.innerHTML =
    `<span class="anav2-legend-empty">${candidates.length} other run` +
    `${candidates.length===1?'':'s'} logged <code>${esc(k0)}</code> — ` +
    `<button class="anav2-legend-action" data-action="add-top">` +
    `add the latest ${Math.min(5, candidates.length)}</button> · ` +
    `<button class="anav2-legend-action" data-action="swap">` +
    `swap to them</button></span>`;
  legend.querySelectorAll('.anav2-legend-action').forEach(btn => {
    btn.onclick = () => {
      const top = candidates.slice(0, 5).map(r => r.id);
      if (btn.dataset.action === 'swap') AnaState.selected.clear();
      top.forEach(rid => AnaState.selected.add(rid));
      renderAnaTable(c); refreshAllPanels(); syncUrl();
    };
  });
}

function refreshAllPanels() {
  AnaState.panels.forEach(p => refreshPanel(p));
}

// Debounced refresh for SSE-driven updates. Multiple metrics_changed events
// (e.g. 2 running runs alternating at 2Hz each = 4Hz total) used to fire
// 4 full refreshAllPanels per second, which made the chart lines visibly
// flicker as new data arrived and x ranges shifted. Coalesce all dirty
// runs into a single refresh at most once every 1.2s.
const _dirtyRuns = new Set();
let _refreshTimer = null;
function scheduleLiveRefresh(run_id) {
  if (run_id) _dirtyRuns.add(run_id);
  if (_refreshTimer) return;
  _refreshTimer = setTimeout(() => {
    _refreshTimer = null;
    const runs = Array.from(_dirtyRuns);
    _dirtyRuns.clear();
    // Only invalidate cache for the runs that actually changed.
    runs.forEach(r => invalidateBucketsForRun(r));
    if (S.view !== 'analysis' || !AnaState || !AnaState.charts) return;
    // Repaint only if at least one dirty run is currently visualized.
    const anySelected = runs.some(r => AnaState.selected.has(r));
    if (anySelected) refreshAllPanels();
  }, 1200);
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

/* Read-only paper share viewer. Rendered when the URL is /p/<token>. */
async function renderShareViewer(token) {
  const app = document.getElementById('app');
  app.innerHTML = '<div class="skel" style="margin:20px;height:80vh"></div>';
  let d;
  try { d = await fetch('/api/paper/share/' + encodeURIComponent(token))
    .then(r => r.json()); }
  catch (e) { app.innerHTML =
    '<div class="empty2" style="margin:40px">Share viewer error: ' +
    esc(e) + '</div>'; return; }
  if (!d || !d.ok) {
    app.innerHTML =
      '<div class="share-wrap"><div class="share-hd">' +
      '<div class="share-brand">autoresearcher<span>UI</span></div>' +
      '<div class="share-sub">read-only paper share</div></div>' +
      '<div class="empty2" style="margin:30px 18px">' +
      esc((d && d.detail) || 'invalid or revoked share link') + '</div></div>';
    return;
  }
  const claimsHtml = (d.claims || []).map(c =>
    '<div class="share-claim"><div class="share-claim-hd">' +
    '<span class="share-claim-st share-claim-st-' + esc(c.status) + '">' +
    esc(c.status) + '</span><b>' + esc(c.title) + '</b></div>' +
    (c.summary_md
      ? '<div class="share-claim-body">' + esc(c.summary_md) + '</div>'
      : '') + '</div>').join('') ||
    '<div class="empty2">no claims yet</div>';
  const decsHtml = (d.decisions || []).slice(0, 12).map(x =>
    '<div class="share-dec"><span class="share-dec-kind">' +
    esc((x.kind || '').replace(/_/g, ' ')) + '</span>' +
    '<span class="share-dec-src">' + esc(x.source || '') + '</span>' +
    '<div class="share-dec-title">' + esc(x.title || '') + '</div></div>'
  ).join('') || '<div class="empty2">inbox zero — author is working</div>';
  const secsHtml = (d.sections || []).map(s =>
    '<div class="share-sec"><span class="share-sec-st share-sec-st-' +
    esc(s.status) + '">' + esc(s.status) + '</span>' +
    esc(s.title || s.slug) + '</div>').join('') ||
    '<div class="empty2">no sections drafted yet</div>';
  app.innerHTML =
    '<div class="share-wrap">' +
    '<div class="share-hd">' +
    '<div class="share-brand">autoresearcher<span>UI</span></div>' +
    '<div class="share-sub">' + esc(d.venue || 'paper share') +
    (d.days_till_deadline != null
      ? ' · ' + d.days_till_deadline.toFixed(1) + ' days to deadline'
      : '') + '</div>' +
    '</div>' +
    (d.has_pdf
      ? '<div class="share-pdf"><iframe src="/api/paper/share/' +
        encodeURIComponent(token) + '/pdf" frameborder="0"></iframe>' +
        '<a class="btn pri" href="/api/paper/share/' +
        encodeURIComponent(token) + '/pdf" download>Download PDF</a></div>'
      : '<div class="empty2" style="margin:20px 0">PDF not built yet</div>') +
    '<div class="share-sec-hd">Claims</div>' + claimsHtml +
    '<div class="share-sec-hd">Decisions waiting on the author</div>' +
    decsHtml +
    '<div class="share-sec-hd">Section status</div>' + secsHtml +
    '<div class="share-foot">read-only · powered by ' +
    '<b>autoresearcher<span style="color:#6366F1">UI</span></b></div>' +
    '</div>';
}

/* Login screen — rendered when the backend says a passcode is set
 * and the current request is not authenticated. */
function renderLogin() {
  const app = document.getElementById('app');
  app.innerHTML =
    '<div class="login-wrap">' +
    '<div class="login-card">' +
    '<div class="login-brand">autoresearcher<span>UI</span></div>' +
    '<div class="login-sub">enter your passcode</div>' +
    '<input id="login-pc" type="password" class="login-input" ' +
    'autocomplete="current-password" placeholder="passcode">' +
    '<button class="btn pri" id="login-go">Unlock</button>' +
    '<div class="login-err" id="login-err"></div>' +
    '</div></div>';
  const inp = app.querySelector('#login-pc');
  const err = app.querySelector('#login-err');
  const go  = app.querySelector('#login-go');
  inp.focus();
  const submit = async () => {
    err.textContent = '';
    go.disabled = true; go.textContent = 'Checking…';
    try {
      const r = await fetch('/api/passcode/login', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ passcode: inp.value }) }).then(r => r.json());
      if (r && r.ok) { location.reload(); return; }
      err.textContent = (r && r.detail) || 'wrong passcode';
    } catch (e) { err.textContent = String(e); }
    go.disabled = false; go.textContent = 'Unlock';
  };
  go.onclick = submit;
  inp.onkeydown = e => { if (e.key === 'Enter') submit(); };
}

async function boot() {
  document.getElementById('app').innerHTML =
    '<div class="skel" style="margin:20px;height:90vh"></div>';
  // Share-viewer URL takes precedence over everything else (no login).
  if (window.location.pathname.startsWith('/p/')) {
    const tok = window.location.pathname.split('/').filter(Boolean)[1] || '';
    renderShareViewer(tok); return;
  }
  // Passcode gate
  try {
    const pc = await fetch('/api/passcode/check').then(r => r.json());
    if (pc && pc.enabled && !pc.authed) { renderLogin(); return; }
  } catch (e) { /* gate down → fall through */ }
  const project = await api('/project');
  if (!project || !project.name) { onboarding(); return; }
  const [runs, ideas, events, chat, gpus, mode] = await Promise.all([
    api('/runs'), api('/ideas'), api('/events'), api('/chat'),
    api('/gpus'), api('/mode').catch(() => ({ mode: 'research' }))]);
  Object.assign(S, { project, runs, ideas, events, chat, gpus, mode });
  // Honor the URL on first paint: /write-paper → 'latex', etc.
  const startView = viewFromPath(window.location.pathname);
  if (startView) S.view = startView;
  // Make sure the URL matches the view we ended up on (so back-button works).
  try { history.replaceState({ view: S.view }, '',
    VIEW_TO_PATH[S.view] || '/'); } catch (e) {}
  render(); streams();
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeDrawer();
  });
}
boot();
