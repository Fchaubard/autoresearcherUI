/* autoresearcherUI - dashboard (scaffold, vanilla JS, no build step) */
'use strict';

// ── palette ───────────────────────────────────────────────────────────────
const SERIES = ['#6366F1','#22C55E','#F59E0B','#38BDF8','#EC4899','#A855F7',
                '#14B8A6','#F43F5E','#84CC16','#FB923C'];
const colorFor = (i) => SERIES[i % SERIES.length];

// ── state ─────────────────────────────────────────────────────────────────
const S = {
  view: 'overview',
  project: null, ideas: [], runs: [], gpus: [], events: [], chat: [],
  journal: [],
  metrics: {},          // run_id -> { key: [[step,val],...] }
  charts: [],           // mounted LineChart instances
  drawerRun: null,
};

// ── api ───────────────────────────────────────────────────────────────────
const api  = (p) => fetch('/api' + p).then(r => r.json());
const post = (p, b) => fetch('/api' + p, {
  method: 'POST', headers: {'Content-Type': 'application/json'},
  body: JSON.stringify(b || {}),
}).then(r => r.json());

// ── helpers ───────────────────────────────────────────────────────────────
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html != null) n.innerHTML = html;
  return n;
};
const fmt = (v, d = 3) => (v == null || isNaN(v)) ? '—' : Number(v).toFixed(d);
const ago = (iso) => {
  if (!iso) return '';
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return Math.max(1, s | 0) + 's ago';
  if (s < 3600) return (s / 60 | 0) + 'm ago';
  if (s < 86400) return (s / 3600 | 0) + 'h ago';
  return (s / 86400 | 0) + 'd ago';
};
const dur = (a, b) => {
  if (!a) return '—';
  const s = ((b ? new Date(b) : new Date()) - new Date(a)) / 1000;
  return s < 3600 ? (s / 60 | 0) + 'm' : (s / 3600).toFixed(1) + 'h';
};
const STATUS_LABEL = {
  kept: 'success', success: 'success', discarded: 'failed', failed: 'failed',
  crashed: 'failed', running: 'running', unclear: 'unclear', queued: 'queued',
  not_implemented: 'queued', implemented: 'queued',
};
const chip = (st) => {
  const k = STATUS_LABEL[st] || 'queued';
  return `<span class="chip ${k}"><span class="dot"></span>${st}</span>`;
};

// ── canvas line chart ─────────────────────────────────────────────────────
class LineChart {
  constructor(host, opts = {}) {
    this.h = opts.height || 230;
    this.series = [];
    this.wrap = el('div', 'chart-wrap');
    this.canvas = el('canvas');
    this.tip = el('div', 'chart-tip');
    this.wrap.append(this.canvas, this.tip);
    host.append(this.wrap);
    this.legendHost = opts.legend ? el('div', 'chart-legend') : null;
    if (this.legendHost) host.append(this.legendHost);
    this.canvas.addEventListener('mousemove', (e) => this._hover(e));
    this.canvas.addEventListener('mouseleave', () => {
      this.tip.style.opacity = 0; this.hoverX = null; this.draw();
    });
    this._ro = new ResizeObserver(() => this.draw());
    this._ro.observe(this.wrap);
    S.charts.push(this);
  }
  setSeries(s) { this.series = s; this.draw(); }
  draw() {
    const w = this.wrap.clientWidth || 600, h = this.h;
    const dpr = window.devicePixelRatio || 1;
    this.canvas.width = w * dpr; this.canvas.height = h * dpr;
    this.canvas.style.height = h + 'px';
    const c = this.canvas.getContext('2d');
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    c.clearRect(0, 0, w, h);
    const pad = {l: 46, r: 14, t: 12, b: 24};
    const vis = this.series.filter(s => s.data && s.data.length);
    if (!vis.length) {
      c.fillStyle = '#5C636B'; c.font = '12px sans-serif';
      c.textAlign = 'center';
      c.fillText('waiting for metrics…', w / 2, h / 2);
      return;
    }
    let xmin = Infinity, xmax = -Infinity, ymin = Infinity, ymax = -Infinity;
    for (const s of vis) for (const [x, y] of s.data) {
      if (x < xmin) xmin = x; if (x > xmax) xmax = x;
      if (y < ymin) ymin = y; if (y > ymax) ymax = y;
    }
    const yp = (ymax - ymin) * 0.12 || 1;
    ymin -= yp; ymax += yp;
    if (xmax === xmin) xmax = xmin + 1;
    const px = (x) => pad.l + (x - xmin) / (xmax - xmin) * (w - pad.l - pad.r);
    const py = (y) => pad.t + (1 - (y - ymin) / (ymax - ymin)) * (h - pad.t - pad.b);
    this._geo = {px, py, xmin, xmax, w, pad};
    // grid + y labels
    c.font = '10px ' + getMono(); c.textAlign = 'right';
    for (let i = 0; i <= 4; i++) {
      const y = pad.t + i / 4 * (h - pad.t - pad.b);
      const val = ymax - i / 4 * (ymax - ymin);
      c.strokeStyle = '#1c2026'; c.lineWidth = 1;
      c.beginPath(); c.moveTo(pad.l, y); c.lineTo(w - pad.r, y); c.stroke();
      c.fillStyle = '#5C636B';
      c.fillText(val.toFixed(2), pad.l - 7, y + 3);
    }
    // crosshair
    if (this.hoverX != null) {
      c.strokeStyle = '#3a4150'; c.lineWidth = 1;
      c.beginPath(); c.moveTo(this.hoverX, pad.t);
      c.lineTo(this.hoverX, h - pad.b); c.stroke();
    }
    // lines
    for (const s of vis) {
      c.strokeStyle = s.color; c.lineWidth = s.baseline ? 1.4 : 2;
      if (s.baseline) c.setLineDash([5, 4]); else c.setLineDash([]);
      c.beginPath();
      s.data.forEach(([x, y], i) => {
        const X = px(x), Y = py(y);
        i ? c.lineTo(X, Y) : c.moveTo(X, Y);
      });
      c.stroke();
      // last-point dot
      const last = s.data[s.data.length - 1];
      c.setLineDash([]); c.fillStyle = s.color;
      c.beginPath(); c.arc(px(last[0]), py(last[1]), 2.6, 0, 7); c.fill();
    }
    c.setLineDash([]);
    if (this.legendHost) {
      this.legendHost.innerHTML = vis.map(s =>
        `<span class="leg"><i style="background:${s.color}"></i>${s.name}</span>`
      ).join('');
    }
  }
  _hover(e) {
    if (!this._geo) return;
    const r = this.canvas.getBoundingClientRect();
    const mx = e.clientX - r.left;
    const {px, xmin, xmax, w, pad} = this._geo;
    const frac = (mx - pad.l) / (w - pad.l - pad.r);
    const xval = xmin + Math.max(0, Math.min(1, frac)) * (xmax - xmin);
    this.hoverX = px(xval);
    const rows = [];
    for (const s of this.series) {
      if (!s.data || !s.data.length) continue;
      let best = s.data[0];
      for (const p of s.data)
        if (Math.abs(p[0] - xval) < Math.abs(best[0] - xval)) best = p;
      rows.push(`<div style="color:${s.color}">${s.name}: ${best[1].toFixed(3)}</div>`);
    }
    this.tip.innerHTML = `<div style="color:#9BA1A8">step ${xval | 0}</div>` +
      rows.join('');
    this.tip.style.opacity = 1;
    this.tip.style.left = Math.min(mx + 14, r.width - 150) + 'px';
    this.tip.style.top = '12px';
    this.draw();
  }
}
const getMono = () => "'SF Mono',Menlo,monospace";

// ── SSE wiring (doc 11 D1) ────────────────────────────────────────────────
function connectStreams() {
  const m = new EventSource('/api/stream/metrics');
  m.addEventListener('metric', (e) => {
    const {run_id, points} = JSON.parse(e.data);
    const md = S.metrics[run_id] || (S.metrics[run_id] = {});
    for (const p of points) {
      (md[p.key] || (md[p.key] = [])).push([p.step, p.value]);
    }
    if (S.view === 'overview' || S.view === 'graphs') refreshCharts();
  });

  const ev = new EventSource('/api/stream/events');
  ev.addEventListener('event', (e) => {
    S.events.unshift(JSON.parse(e.data));
    S.events = S.events.slice(0, 60);
    if (S.view === 'overview') render();
  });
  ev.addEventListener('runs_changed', async () => {
    [S.runs, S.ideas] = await Promise.all([api('/runs'), api('/ideas')]);
    if (S.view === 'experiments' || S.view === 'overview') render();
  });

  const g = new EventSource('/api/stream/gpus');
  g.addEventListener('gpus', (e) => {
    S.gpus = JSON.parse(e.data).gpus;
    paintGpuStrip();
    if (S.view === 'overview') paintGpuPanel();
  });

  const ch = new EventSource('/api/stream/chat');
  ch.addEventListener('chat', (e) => {
    S.chat.push(JSON.parse(e.data));
    if (S.view === 'chat') { renderChat(); scrollChat(); }
  });
}

// ── shell ─────────────────────────────────────────────────────────────────
const NAV = [
  ['overview', 'Overview', '◳'],
  ['experiments', 'Experiments', '☷'],
  ['graphs', 'Live Graphs', '∿'],
  ['journal', 'Journal', '✎'],
  ['chat', 'Agent Chat', '✦'],
];

function shell() {
  const app = document.getElementById('app');
  app.innerHTML = '';
  const wrap = el('div', 'app-shell');

  const side = el('div', 'sidebar');
  side.append(el('div', 'brand',
    `<div class="brand-mark">a</div>
     <div class="brand-name">autoresearcher<span>UI</span></div>`));
  NAV.forEach(([id, label, ico]) => {
    const b = el('button', 'nav-item' + (S.view === id ? ' active' : ''),
      `<span class="ico">${ico}</span>${label}`);
    b.onclick = () => go(id);
    side.append(b);
  });
  side.append(el('div', 'nav-spacer'));
  side.append(el('div', 'nav-foot', 'v0.2 · demo mode<br>1 node · live'));

  const main = el('div', 'main');
  main.append(topbar());
  const content = el('div', 'content');
  content.id = 'content';
  main.append(content);

  wrap.append(side, main);
  app.append(wrap, tabbar(), scrim(), drawer());
}

function topbar() {
  const t = el('div', 'topbar');
  const p = S.project || {};
  t.append(el('div', 'proj',
    `${p.name || 'project'} <small>${p.validation_metric || ''}</small>`));
  t.append(el('div', 'loop-pill',
    `<span class="dot live"></span>${p.status || 'running'}`));
  const strip = el('div', 'gpu-strip'); strip.id = 'gpustrip';
  t.append(strip);
  t.append(el('div', 'count', '', ));
  const c = el('div', 'count'); c.id = 'topcount';
  t.append(c);
  return t;
}

function tabbar() {
  const t = el('div', 'tabbar mobile-only');
  NAV.forEach(([id, label, ico]) => {
    const b = el('button', S.view === id ? 'active' : '',
      `<span class="ico">${ico}</span>${label.split(' ')[0]}`);
    b.onclick = () => go(id);
    t.append(b);
  });
  return t;
}

function go(view) { S.view = view; S.charts = []; shell(); render(); }

// ── topbar widgets ────────────────────────────────────────────────────────
function paintGpuStrip() {
  const host = document.getElementById('gpustrip');
  if (!host) return;
  host.innerHTML = '';
  S.gpus.forEach(g => {
    const cell = el('div', 'gpu-cell', `<span>${g.index}</span>`);
    const fill = el('div', 'fill');
    fill.style.height = (g.util_pct || 0) + '%';
    cell.title = `GPU ${g.index} · ${g.util_pct}% · ${(g.vram_used_mb/1024).toFixed(1)}GB`;
    cell.prepend(fill);
    host.append(cell);
  });
  const tc = document.getElementById('topcount');
  if (tc) {
    const run = S.runs.filter(r => r.status === 'running').length;
    const q = S.ideas.filter(i => i.status === 'not_implemented').length;
    tc.innerHTML = `<b>${run}</b> running · <b>${q}</b> queued`;
  }
}

// ── router ────────────────────────────────────────────────────────────────
function render() {
  const c = document.getElementById('content');
  if (!c) return;
  c.innerHTML = '';
  S.charts = [];
  ({overview: viewOverview, experiments: viewExperiments, graphs: viewGraphs,
    journal: viewJournal, chat: viewChat}[S.view] || viewOverview)(c);
  paintGpuStrip();
}

// ── OVERVIEW ──────────────────────────────────────────────────────────────
function viewOverview(c) {
  const p = S.project || {};
  c.append(el('div', 'view-title', 'Overview'));
  c.append(el('div', 'view-sub',
    'Autonomous research — live. ' + (p.purpose || '').slice(0, 110) + '…'));

  const dir = p.metric_direction === 'maximize';
  const delta = (p.best_metric != null)
    ? (p.best_metric - (p.baseline_metric || 0)) : 0;
  const stats = el('div', 'grid stat-row');
  stats.append(statCard('Best ' + (p.validation_metric || 'metric'),
    fmt(p.best_metric), (delta >= 0 ? 'up' : 'down'),
    `${delta >= 0 ? '+' : ''}${fmt(delta)} vs baseline`));
  stats.append(statCard('Experiments done', p.experiments_done ?? 0, 'up',
    `${p.experiments_total ?? 0} total ideas`));
  stats.append(statCard('Success rate',
    Math.round((p.success_rate || 0) * 100) + '%', 'up', 'of completed runs'));
  const busy = S.gpus.filter(g => g.current_run_id).length;
  stats.append(statCard('GPUs in use', `${busy}/${S.gpus.length}`,
    busy === S.gpus.length ? 'up' : 'down',
    busy === S.gpus.length ? 'fully utilized' : 'idle capacity'));
  c.append(stats);

  const cols = el('div', 'grid two-col');
  cols.style.gridTemplateColumns = '1fr 340px';

  const left = el('div', 'grid');
  const cp = el('div', 'panel');
  cp.append(el('div', 'panel-head',
    `<h3>Validation metric — live</h3>
     <span class="hint">running runs vs baseline</span>`));
  const cb = el('div', 'panel-body');
  cp.append(cb); left.append(cp);
  const acc = new LineChart(cb, {height: 250, legend: true});

  const lp = el('div', 'panel');
  lp.append(el('div', 'panel-head', `<h3>Training loss — live</h3>`));
  const lb = el('div', 'panel-body'); lp.append(lb); left.append(lp);
  const loss = new LineChart(lb, {height: 190, legend: true});
  c._ovCharts = {acc, loss};

  const right = el('div', 'grid');
  const gp = el('div', 'panel');
  gp.append(el('div', 'panel-head', `<h3>GPUs</h3>`));
  const gb = el('div', 'panel-body'); gb.id = 'gpupanel';
  gp.append(gb); right.append(gp);

  const qp = el('div', 'panel');
  qp.append(el('div', 'panel-head',
    `<h3>Up next</h3><span class="hint">by EV</span>`));
  const qb = el('div', 'panel-body');
  S.ideas.filter(i => i.status === 'not_implemented')
    .slice(0, 4).forEach((i, n) => qb.append(queueItem(i, n + 1)));
  qp.append(qb); right.append(qp);

  cols.append(left, right);
  c.append(cols);

  const tp = el('div', 'panel'); tp.style.marginTop = '14px';
  tp.append(el('div', 'panel-head', `<h3>Activity</h3>`));
  const tb = el('div', 'panel-body');
  S.events.slice(0, 8).forEach(e => tb.append(timelineItem(e)));
  tp.append(tb); c.append(tp);

  paintGpuPanel();
  refreshCharts();
}

function statCard(label, value, dir, sub) {
  const card = el('div', 'card stat-card');
  card.append(el('div', 'stat-label', label));
  const v = el('div', 'stat-value', String(value));
  card.append(v);
  card.append(el('div', 'stat-delta ' + dir, sub));
  return card;
}

function paintGpuPanel() {
  const host = document.getElementById('gpupanel');
  if (!host) return;
  host.innerHTML = '';
  S.gpus.forEach(g => {
    const row = el('div');
    row.style.cssText = 'margin-bottom:11px';
    const busy = !!g.current_run_id;
    row.innerHTML =
      `<div style="display:flex;justify-content:space-between;font-size:12px">
         <span class="cell-mono">GPU ${g.index} · ${g.model.replace('NVIDIA ','')}</span>
         <span style="color:${busy ? '#fbbf24' : '#5C636B'}">${g.util_pct}%</span>
       </div>
       <div class="ev-bar" style="max-width:none;margin-top:4px">
         <i style="width:${g.util_pct}%;background:${busy
           ? 'linear-gradient(90deg,#f59e0b,#fbbf24)'
           : '#2a2f37'}"></i>
       </div>
       <div style="font-size:11px;color:#5C636B;margin-top:3px">
         ${busy ? g.current_run_id : 'idle'} ·
         ${(g.vram_used_mb/1024).toFixed(1)}/${(g.total_vram_mb/1024).toFixed(0)}GB ·
         ${g.temp_c}°C</div>`;
    host.append(row);
  });
}

function timelineItem(e) {
  const ic = {breakthrough: '★', run_finished: '✓', run_started: '▶',
              idea_added: '✎', gpu_idle: '⚠', agent_down: '✕'}[e.type] || '•';
  const it = el('div', 'timeline-item');
  it.innerHTML =
    `<div class="tl-ico">${ic}</div>
     <div><div class="tl-msg">${e.message}</div>
     <div class="tl-time">${e.actor} · ${ago(e.created_at)}</div></div>`;
  return it;
}

function queueItem(idea, rank) {
  const it = el('div', 'queue-item');
  it.innerHTML =
    `<span class="queue-rank">#${rank}</span>
     <div style="flex:1;min-width:0">
       <div style="font-size:12.5px;font-weight:600;white-space:nowrap;
            overflow:hidden;text-overflow:ellipsis">${idea.idea_id}</div>
       <div style="font-size:11px;color:#5C636B">EV ${fmt(idea.ev, 2)}</div>
     </div>
     <div class="ev-bar"><i style="width:${Math.min(100, idea.ev*200)}%"></i></div>`;
  return it;
}

// ── EXPERIMENTS ───────────────────────────────────────────────────────────
const EXP = {q: '', sort: 'date', filter: 'all'};

function viewExperiments(c) {
  c.append(el('div', 'view-title', 'Experiments'));
  c.append(el('div', 'view-sub',
    'Everything tried, running, and queued — sorted by expected value.'));

  const bar = el('div', 'toolbar');
  const search = el('input', 'input');
  search.placeholder = 'Search ideas…'; search.value = EXP.q;
  search.oninput = () => { EXP.q = search.value; paintExpTable(); };
  bar.append(search);
  const seg = el('div', 'seg');
  [['all', 'All'], ['running', 'Running'], ['success', 'Success'],
   ['failed', 'Failed']].forEach(([k, lbl]) => {
    const b = el('button', EXP.filter === k ? 'on' : '', lbl);
    b.onclick = () => { EXP.filter = k; viewExperiments(c.replaceChildren() || c); };
    seg.append(b);
  });
  bar.append(seg);
  c.append(bar);

  const host = el('div'); host.id = 'exptable';
  c.append(host);
  paintExpTable();
}

function rowsForExperiments() {
  // join runs + ideas into a unified row set
  const ideaById = {};
  S.ideas.forEach(i => ideaById[i.id] = i);
  const rows = [];
  S.runs.forEach(r => {
    const idea = ideaById[r.idea_id] || {};
    rows.push({
      kind: r.status === 'running' ? 'running' : 'completed',
      id: r.id, name: r.run_name || r.id,
      desc: idea.description || '', status: r.status,
      metric: r.headline_metric, delta: r.baseline_delta,
      ev: idea.ev || 0, gpu: r.gpu_index, vram: r.peak_vram_mb,
      started: r.started_at, ended: r.ended_at, commit: r.git_commit,
    });
  });
  S.ideas.filter(i => i.status === 'not_implemented').forEach(i => {
    rows.push({
      kind: 'upcoming', id: i.id, name: i.idea_id, desc: i.description,
      status: 'queued', metric: null, delta: null, ev: i.ev || 0,
      gpu: -1, vram: null, started: '', ended: '', commit: '',
    });
  });
  return rows;
}

function paintExpTable() {
  const host = document.getElementById('exptable');
  if (!host) return;
  host.innerHTML = '';
  let rows = rowsForExperiments();
  if (EXP.q) {
    const q = EXP.q.toLowerCase();
    rows = rows.filter(r => (r.name + r.desc).toLowerCase().includes(q));
  }
  if (EXP.filter === 'running') rows = rows.filter(r => r.kind === 'running');
  if (EXP.filter === 'success')
    rows = rows.filter(r => STATUS_LABEL[r.status] === 'success');
  if (EXP.filter === 'failed')
    rows = rows.filter(r => STATUS_LABEL[r.status] === 'failed');

  const groups = [
    ['running', 'Running'], ['upcoming', 'Up next — ranked by EV'],
    ['completed', 'Completed'],
  ];
  const deltas = rows.map(r => r.delta).filter(d => d != null);
  const maxAbs = Math.max(0.01, ...deltas.map(Math.abs));

  groups.forEach(([k, label]) => {
    let g = rows.filter(r => r.kind === k);
    if (!g.length) return;
    if (k === 'upcoming') g.sort((a, b) => b.ev - a.ev);
    else g.sort((a, b) => (b.started || '').localeCompare(a.started || ''));
    const gl = el('div', 'group-label',
      `${label} <span class="n">${g.length}</span>`);
    host.append(gl);
    const table = el('table', 'xtable');
    table.innerHTML =
      `<thead><tr>
         <th>Status</th><th>Idea</th><th>Result vs baseline</th>
         <th>EV</th><th>GPU</th><th>Duration</th><th>VRAM</th><th>Commit</th>
       </tr></thead>`;
    const tb = el('tbody');
    g.forEach(r => {
      const tr = el('tr');
      let metricCell = '<span style="color:#5C636B">—</span>';
      if (r.metric != null) {
        const d = r.delta || 0;
        const t = Math.max(-1, Math.min(1, d / maxAbs));
        const bg = d >= 0
          ? `rgba(34,197,94,${0.12 + 0.33 * t})`
          : `rgba(239,68,68,${0.12 + 0.33 * -t})`;
        metricCell = `<span class="heat" style="background:${bg}">
          ${fmt(r.metric)} <span style="opacity:.7">
          ${d >= 0 ? '+' : ''}${fmt(d)}</span></span>`;
      }
      tr.innerHTML =
        `<td>${chip(r.status)}</td>
         <td><div style="font-weight:600">${r.name}</div>
           <div style="font-size:11.5px;color:#5C636B;max-width:340px;
             overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
             ${r.desc}</div></td>
         <td>${metricCell}</td>
         <td class="cell-mono">${fmt(r.ev, 2)}</td>
         <td class="cell-mono">${r.gpu >= 0 ? r.gpu : '—'}</td>
         <td class="cell-mono">${r.started ? dur(r.started, r.ended) : '—'}</td>
         <td class="cell-mono">${r.vram ? (r.vram/1024).toFixed(1)+'G' : '—'}</td>
         <td class="cell-mono" style="color:#5C636B">${r.commit || '—'}</td>`;
      tr.onclick = () => openDrawer(r.id, r.kind);
      tb.append(tr);
    });
    table.append(tb);
    host.append(table);
  });
  if (!rows.length)
    host.append(el('div', 'card', 'No experiments match your filter.'));
}

// ── LIVE GRAPHS ───────────────────────────────────────────────────────────
async function viewGraphs(c) {
  c.append(el('div', 'view-title', 'Live Graphs'));
  c.append(el('div', 'view-sub',
    'Every run, overlaid. Charts share a cursor — hover one to read all.'));
  await ensureMetrics();
  ['at5_acc', 'train_loss'].forEach(key => {
    const p = el('div', 'panel'); p.style.marginBottom = '14px';
    p.append(el('div', 'panel-head',
      `<h3>${key}</h3><span class="hint">${runsWithMetric(key).length} runs</span>`));
    const b = el('div', 'panel-body');
    p.append(b); c.append(p);
    const ch = new LineChart(b, {height: 300, legend: true});
    ch.metricKey = key;
  });
  refreshCharts();
}

const runsWithMetric = (key) =>
  Object.keys(S.metrics).filter(rid => S.metrics[rid][key]);

async function ensureMetrics() {
  const need = S.runs.filter(r => !S.metrics[r.id]);
  await Promise.all(need.map(async r => {
    S.metrics[r.id] = await api(`/runs/${r.id}/metrics`);
  }));
}

function seriesFor(key) {
  const out = [];
  S.runs.forEach((r, i) => {
    const d = S.metrics[r.id] && S.metrics[r.id][key];
    if (d && d.length) out.push({
      name: r.run_name || r.id, color: colorFor(i), data: d,
      baseline: r.is_baseline,
    });
  });
  return out;
}

function refreshCharts() {
  S.charts.forEach(ch => {
    if (ch.metricKey) ch.setSeries(seriesFor(ch.metricKey));
  });
  const c = document.getElementById('content');
  if (c && c._ovCharts) {
    c._ovCharts.acc.setSeries(seriesFor('at5_acc')
      .filter(s => s.baseline || hasRunning(s.name)));
    c._ovCharts.loss.setSeries(seriesFor('train_loss')
      .filter(s => hasRunning(s.name)));
  }
}
const hasRunning = (name) =>
  S.runs.some(r => (r.run_name || r.id) === name && r.status === 'running');

// ── JOURNAL ───────────────────────────────────────────────────────────────
function viewJournal(c) {
  c.append(el('div', 'view-title', 'Research Journal'));
  c.append(el('div', 'view-sub',
    'Auto-written narrative of the project — the story behind the runs.'));
  const p = el('div', 'panel');
  const b = el('div', 'panel-body');
  S.journal.forEach(j => {
    const e = el('div', 'journal-entry');
    e.innerHTML =
      `<div class="jdate">${j.date} · ${ago(j.created_at)}</div>
       <h4>${j.title}</h4><p>${j.body}</p>`;
    b.append(e);
  });
  if (!S.journal.length) b.append(el('div', '', 'No entries yet.'));
  p.append(b); c.append(p);
}

// ── AGENT CHAT ────────────────────────────────────────────────────────────
function viewChat(c) {
  c.append(el('div', 'view-title', 'Agent Chat'));
  c.append(el('div', 'view-sub',
    'Talk to the Principal Researcher running your experiments.'));
  const chips = el('div', 'chips-row');
  ['Status update', 'What are you working on?', 'Pause the loop',
   'Skip the current idea'].forEach(t => {
    const b = el('button', 'qchip', t);
    b.onclick = () => sendChat(t);
    chips.append(b);
  });
  c.append(chips);
  const box = el('div', 'chat');
  const log = el('div', 'chat-log'); log.id = 'chatlog';
  box.append(log);
  const comp = el('div', 'composer');
  const inp = el('input', 'input');
  inp.id = 'chatinput'; inp.placeholder = 'Message the researcher…';
  inp.onkeydown = (e) => { if (e.key === 'Enter') sendChat(inp.value); };
  const send = el('button', 'btn', 'Send');
  send.onclick = () => sendChat(inp.value);
  comp.append(inp, send);
  box.append(comp);
  c.append(box);
  renderChat(); scrollChat();
}

function renderChat() {
  const log = document.getElementById('chatlog');
  if (!log) return;
  log.innerHTML = '';
  S.chat.forEach(m => {
    const b = el('div', 'bubble ' + m.role);
    b.innerHTML = `<div class="who">${m.role}</div>${m.content}`;
    log.append(b);
  });
}
const scrollChat = () => {
  const l = document.getElementById('chatlog');
  if (l) l.scrollTop = l.scrollHeight;
};
async function sendChat(text) {
  text = (text || '').trim();
  if (!text) return;
  const inp = document.getElementById('chatinput');
  if (inp) inp.value = '';
  await post('/chat', {content: text});
}

// ── DRAWER (experiment detail) ────────────────────────────────────────────
function scrim() {
  const s = el('div', 'scrim'); s.id = 'scrim';
  s.onclick = closeDrawer;
  return s;
}
function drawer() {
  const d = el('div', 'drawer'); d.id = 'drawer';
  return d;
}
async function openDrawer(id, kind) {
  const d = document.getElementById('drawer');
  d.innerHTML = '<div class="drawer-body"><div class="skeleton" ' +
    'style="height:200px"></div></div>';
  document.getElementById('scrim').classList.add('open');
  d.classList.add('open');
  let run = null, idea = null;
  if (kind === 'upcoming') {
    idea = S.ideas.find(i => i.id === id);
  } else {
    run = await api(`/runs/${id}`);
    idea = run.idea;
    if (!S.metrics[id]) S.metrics[id] = await api(`/runs/${id}/metrics`);
  }
  d.innerHTML = '';
  const head = el('div', 'drawer-head');
  head.innerHTML =
    `<div><div style="font-size:17px;font-weight:700">
       ${(run && run.run_name) || (idea && idea.idea_id) || id}</div>
     <div style="margin-top:5px">${chip((run && run.status) ||
       (idea && idea.status) || 'queued')}</div></div>`;
  const x = el('button', 'iconbtn', '✕');
  x.onclick = closeDrawer;
  head.append(x);
  d.append(head);

  const body = el('div', 'drawer-body');
  if (idea) {
    body.append(el('div', 'section-h', 'Idea'));
    body.append(el('div', 'prose', idea.description || ''));
    if (idea.why) {
      body.append(el('div', 'section-h', 'Why'));
      body.append(el('div', 'prose', idea.why));
    }
  }
  if (run && run.id) {
    body.append(el('div', 'section-h', 'Run'));
    const kv = el('dl', 'kv');
    const rows = [
      ['Status', run.status], ['GPU', run.gpu_index],
      ['tmux', run.tmux_session], ['Commit', run.git_commit],
      ['Headline', fmt(run.headline_metric)],
      ['vs baseline', run.baseline_delta != null
        ? (run.baseline_delta >= 0 ? '+' : '') + fmt(run.baseline_delta) : '—'],
      ['Peak VRAM', run.peak_vram_mb
        ? (run.peak_vram_mb/1024).toFixed(1) + ' GB' : '—'],
      ['Duration', dur(run.started_at, run.ended_at)],
    ];
    rows.forEach(([k, v]) =>
      kv.innerHTML += `<dt>${k}</dt><dd>${v}</dd>`);
    body.append(kv);
    if (run.config && Object.keys(run.config).length) {
      body.append(el('div', 'section-h', 'Config / HPPs'));
      const cfg = el('dl', 'kv');
      Object.entries(run.config).forEach(([k, v]) =>
        cfg.innerHTML += `<dt>${k}</dt><dd>${v}</dd>`);
      body.append(cfg);
    }
    body.append(el('div', 'section-h', 'Metrics'));
    const cw = el('div');
    body.append(cw);
    const ch = new LineChart(cw, {height: 200, legend: true});
    const md = S.metrics[id] || {};
    ch.setSeries(Object.keys(md).map((k, i) => ({
      name: k, color: colorFor(i), data: md[k],
    })));
  }
  if (idea && idea.analysis) {
    body.append(el('div', 'section-h', 'Agent analysis'));
    body.append(el('div', 'prose', idea.analysis));
  }
  if (idea && idea.conclusion) {
    body.append(el('div', 'section-h', 'Conclusion'));
    body.append(el('div', 'prose', idea.conclusion));
  }
  d.append(body);
}
function closeDrawer() {
  document.getElementById('scrim').classList.remove('open');
  document.getElementById('drawer').classList.remove('open');
}

// ── boot ──────────────────────────────────────────────────────────────────
async function boot() {
  shell();
  const c = document.getElementById('content');
  c.innerHTML = '<div class="skeleton" style="height:60vh"></div>';
  const [project, ideas, runs, gpus, events, chat, journal] =
    await Promise.all([
      api('/project'), api('/ideas'), api('/runs'), api('/gpus'),
      api('/events'), api('/chat'), api('/journal'),
    ]);
  Object.assign(S, {project, ideas, runs, gpus, events, chat, journal});
  shell();
  render();
  connectStreams();
}
boot();
