/* app.js — main controller for tracesage UI. ES module.
 *
 * Owns: run list, timeline, drawer, header stats, theme, keyboard shortcuts,
 * WebSocket lifecycle (global runs feed + per-run trace) with exponential
 * backoff reconnect.  Delegates the graph entirely to GraphView (graph.js).
 *
 * No build step.  Imports are relative ES module paths.
 */

import {
  GraphView,
  nodeIdForEvent,
  escapeHtml,
  formatMs,
  formatRelTime,
  mcpServerColor,
} from './graph.js';

/* ============================================================
 * Constants & state
 * ============================================================ */

const STORAGE_THEME = 'tracesage.theme';
const STORAGE_TOKEN = 'tracesage.auth_token';
const STORAGE_PANES = 'tracesage.panes';
const STORAGE_TOOLS_POS = 'tracesage.tools_panel_pos';
const TIMELINE_MAX = 500;
const STATS_POLL_MS = 5000;
const RUNS_POLL_MS = 30000;
const WS_BACKOFF = [1000, 2000, 4000, 8000, 16000, 30000];

// Short, portable uppercase text tags. Avoids mojibake / replacement glyphs
// and renders identically across all platforms and fonts.
const EVENT_ICONS = {
  chain_start: 'CHAIN', chain_end: 'CHAIN', chain_error: 'ERR',
  agent_action: 'AGENT', agent_finish: 'AGENT',
  tool_start: 'TOOL', tool_end: 'TOOL', tool_error: 'ERR',
  llm_start: 'LLM', llm_end: 'LLM', llm_error: 'ERR',
  chat_model_start: 'LLM',
  retriever_start: 'RETR', retriever_end: 'RETR', retriever_error: 'ERR',
  run_start: 'RUN', run_end: 'RUN',
};

/** Single source of truth for everything that lives in memory. */
const state = {
  authToken: localStorage.getItem(STORAGE_TOKEN) || '',
  theme: localStorage.getItem(STORAGE_THEME) || 'dark',
  runs: [],                    // Run[]
  runsById: new Map(),         // run_id -> Run
  filterStatus: 'all',
  searchQuery: '',
  selectedRunId: null,
  topology: { nodes: [], edges: [] },
  journey: [],                 // StoredEvent[] for selected run
  newEventCount: 0,
  autoScrollTimeline: true,
  graphMode: 'topology',       // 'topology' | 'trace'
  topologyScope: 'run',        // 'run' (selected/latest) | 'lastn' | 'all'
  topologyScopeN: 5,           // N for the 'lastn' scope
  toolsCollapsed: true,        // "Tools by source" panel starts minimized
  layoutDir: 'LR',
  evRateWindow: [],            // sliding window of timestamps for ev/s
  graphRenderTimer: null,
  pendingTopologyUpdate: false,
  serverVersion: '?',
  // event_id set so we never duplicate when both REST catchup and WS catchup land.
  knownEventIds: new Set(),
  // Replay
  replayMode: 'auto',          // 'auto' | 'manual'
  replaySteps: [],             // [{source, target, eventId}, ...]
  manualStepIdx: -1,           // -1 = nothing yet selected, 0..N-1 = current step
  autoReplayInProgress: false, // true while graph.playRun is running
  currentDrawerEventId: null,  // guards async blob fetch against a re-opened drawer
  timelineFilter: '',          // within-run timeline search query (lowercased on use)
};

// WebSocket holders.
let wsRuns = null;
let wsTrace = null;
let wsRunsBackoff = 0;
let wsTraceBackoff = 0;
let wsRunsCloseRequested = false;
let wsTraceCloseRequested = false;

// Graph view instance — created after DOM ready.
let graph;

/* ============================================================
 * REST helpers
 * ============================================================ */

function buildHeaders() {
  const h = { 'Content-Type': 'application/json' };
  if (state.authToken) h['Authorization'] = `Bearer ${state.authToken}`;
  return h;
}

async function apiGet(path) {
  const r = await fetch(`/api${path}`, { headers: buildHeaders() });
  if (!r.ok) {
    const detail = await r.text().catch(() => r.statusText);
    throw new ApiError(r.status, detail || r.statusText);
  }
  return r.json();
}

class ApiError extends Error {
  constructor(status, detail) {
    super(`HTTP ${status}: ${detail}`);
    this.status = status;
    this.detail = detail;
  }
}

/* ============================================================
 * Run actions — export (JSONL download) + delete
 * ============================================================ */

/** Download the run's JSONL export. Uses fetch + blob with the Bearer header
 *  (buildHeaders) so it works when an auth token is configured, and keeps the
 *  token out of the URL / server access logs (an anchor navigation can't send
 *  an Authorization header, and the HTTP middleware ignores ?token=). */
async function exportRun(runId) {
  try {
    const r = await fetch(
      `/api/runs/${encodeURIComponent(runId)}/export?format=jsonl`,
      { headers: buildHeaders() },
    );
    if (!r.ok) {
      const detail = await r.text().catch(() => r.statusText);
      throw new ApiError(r.status, detail || r.statusText);
    }
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${runId}.jsonl`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    if (e.status === 401) {
      toast('Authentication failed. Open settings to set token.', 'error');
    } else {
      toast(`Could not export run: ${e.message}`, 'error');
    }
  }
}

/** Confirm, then DELETE the run via the REST API (Bearer auth, mirroring the
 *  other authed fetches), then refresh the run list. */
async function deleteRun(runId) {
  if (!window.confirm(`Delete run ${runId}? This cannot be undone.`)) return;
  try {
    const r = await fetch(`/api/runs/${encodeURIComponent(runId)}`, {
      method: 'DELETE',
      headers: buildHeaders(),
    });
    if (!r.ok) {
      const detail = await r.text().catch(() => r.statusText);
      throw new ApiError(r.status, detail || r.statusText);
    }
    // If the deleted run was selected, clear selection + its trace WS.
    if (state.selectedRunId === runId) {
      state.selectedRunId = null;
      closeTraceWS();
      renderTimeline();
    }
    toast('Run deleted', 'success', 1500);
    await loadRuns();
  } catch (e) {
    if (e.status === 401) {
      toast('Authentication failed. Open settings to set token.', 'error');
    } else {
      toast(`Could not delete run: ${e.message}`, 'error');
    }
  }
}

/* ============================================================
 * Theme
 * ============================================================ */

function applyTheme(theme) {
  state.theme = theme;
  document.documentElement.setAttribute('data-theme', theme);
  localStorage.setItem(STORAGE_THEME, theme);
  // Cytoscape colors come from CSS vars — re-apply stylesheet so it picks up changes.
  if (graph?.isReady()) graph.refreshStyles();
}

function toggleTheme() {
  applyTheme(state.theme === 'dark' ? 'light' : 'dark');
}

/* ============================================================
 * Toasts
 * ============================================================ */

function toast(message, level = 'info', timeoutMs = 4000) {
  const host = document.getElementById('toast-host');
  if (!host) return;
  const el = document.createElement('div');
  el.className = `toast ${level}`;
  el.textContent = message;
  host.appendChild(el);
  setTimeout(() => {
    el.classList.add('fade-out');
    setTimeout(() => el.remove(), 220);
  }, timeoutMs);
}

/* ============================================================
 * Run list
 * ============================================================ */

async function loadRuns() {
  try {
    const data = await apiGet(`/runs?limit=50`);
    state.runs = data.runs;
    state.runsById = new Map(data.runs.map((r) => [r.run_id, r]));
    renderRunList();
    renderEmptyState();
  } catch (e) {
    if (e.status === 401) {
      toast('Authentication failed. Open settings to set token.', 'error');
    } else {
      toast(`Could not load runs: ${e.message}`, 'error');
    }
    document.getElementById('run-list').innerHTML =
      `<div class="empty-runs">Failed to load runs.<br><small>${escapeHtml(e.message)}</small></div>`;
  }
}

function renderRunList() {
  const container = document.getElementById('run-list');
  const filtered = filteredRuns();

  if (filtered.length === 0) {
    container.innerHTML = `<div class="empty-runs">No runs match the current filter.</div>`;
    document.getElementById('run-count').textContent = `0 of ${state.runs.length} runs`;
    return;
  }

  // Build via a doc fragment so we don't lose scroll on partial re-render.
  const frag = document.createDocumentFragment();
  for (const run of filtered) {
    frag.appendChild(buildRunRow(run));
  }
  container.innerHTML = '';
  container.appendChild(frag);
  document.getElementById('run-count').textContent =
    state.runs.length === filtered.length
      ? `${state.runs.length} runs`
      : `${filtered.length} of ${state.runs.length} runs`;
}

function buildRunRow(run) {
  const row = document.createElement('div');
  row.className = 'run-row';
  if (state.selectedRunId === run.run_id) row.classList.add('active');
  row.dataset.runId = run.run_id;
  row.setAttribute('role', 'listitem');
  row.setAttribute('tabindex', '0');
  row.setAttribute('aria-label', `Run ${run.run_id} — ${run.status}`);

  const tags = (run.tags || []).slice(0, 3)
    .map((t) => `<span class="tag-chip">${escapeHtml(t)}</span>`)
    .join('');

  row.innerHTML = `
    <div class="run-row-top">
      <span class="status-badge ${escapeHtml(run.status)}" aria-label="${escapeHtml(run.status)}"></span>
      <span class="run-id" title="${escapeHtml(run.run_id)}">${escapeHtml(run.run_id.slice(0, 12))}…</span>
      <span class="run-row-actions">
        <button class="run-action export" data-run-action="export" data-run-id="${escapeHtml(run.run_id)}" type="button" title="Export run as JSONL" aria-label="Export run ${escapeHtml(run.run_id)} as JSONL">Export</button>
        <button class="run-action delete" data-run-action="delete" data-run-id="${escapeHtml(run.run_id)}" type="button" title="Delete run" aria-label="Delete run ${escapeHtml(run.run_id)}">Delete</button>
      </span>
    </div>
    <div class="run-row-bottom">
      <span class="meta-step">${run.total_steps} step${run.total_steps === 1 ? '' : 's'}</span>
      <span>·</span>
      <span title="${escapeHtml(run.started_at)}">${formatRelTime(run.started_at)}</span>
      ${tags}
    </div>
  `;
  row.addEventListener('click', () => selectRun(run.run_id));
  row.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      selectRun(run.run_id);
    }
  });
  // Run-level actions: stop propagation so clicking them doesn't select the row.
  const exportBtn = row.querySelector('[data-run-action="export"]');
  if (exportBtn) {
    exportBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      exportRun(run.run_id);
    });
  }
  const deleteBtn = row.querySelector('[data-run-action="delete"]');
  if (deleteBtn) {
    deleteBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      deleteRun(run.run_id);
    });
  }
  return row;
}

function filteredRuns() {
  const q = state.searchQuery.toLowerCase();
  return state.runs.filter((r) => {
    if (state.filterStatus !== 'all' && r.status !== state.filterStatus) return false;
    if (q) {
      const hay = `${r.run_id} ${(r.tags || []).join(' ')}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

function upsertRun(run) {
  const existing = state.runsById.get(run.run_id);
  if (existing) {
    Object.assign(existing, run);
  } else {
    state.runs.unshift(run);
    state.runsById.set(run.run_id, run);
  }
  renderRunList();
  renderEmptyState();
}

/* ============================================================
 * Run selection — load journey + open trace WS
 * ============================================================ */

async function selectRun(runId) {
  if (state.selectedRunId === runId) return;
  state.selectedRunId = runId;
  state.journey = [];
  state.newEventCount = 0;
  state.autoScrollTimeline = true;
  state.knownEventIds = new Set();
  setHash(runId);
  renderRunList();
  setGraphModeAvailable(true);
  document.getElementById('replay-controls').classList.remove('hidden');

  const tlMeta = document.getElementById('timeline-meta');
  tlMeta.textContent = runId.slice(0, 12) + '…';

  showTimelineSkeleton();
  try {
    const data = await apiGet(`/runs/${encodeURIComponent(runId)}/journey`);
    state.journey = data.steps || [];
    state.journey.forEach((s) => state.knownEventIds.add(s.event_id));
  } catch (e) {
    toast(`Could not load journey: ${e.message}`, 'error');
    state.journey = [];
  }
  renderTimeline();
  applyRunTraceToGraph();
  // When the topology is scoped to "this run", refresh it for the new selection.
  if (state.topologyScope === 'run') loadTopology();

  // Reset manual replay state for the new run; if user is in manual mode,
  // refresh the step list against the new journey.
  state.replaySteps = buildReplaySteps();
  state.manualStepIdx = -1;
  if (state.replayMode === 'manual') {
    updateManualStepUI();
    if (graph?.isReady()) graph.clearStepHighlight();
    clearTimelineEventHighlight();
  }

  openTraceWS(runId);
  setGraphMode('trace');
}

function setHash(runId) {
  const params = new URLSearchParams();
  if (runId) params.set('run', runId);
  if (state.filterStatus !== 'all') params.set('status', state.filterStatus);
  const hash = params.toString();
  if (hash) location.hash = `#${hash}`;
  else if (location.hash) history.replaceState(null, '', location.pathname + location.search);
}

function readHash() {
  const hash = location.hash.replace(/^#/, '');
  if (!hash) return {};
  const params = new URLSearchParams(hash);
  return Object.fromEntries(params.entries());
}

/* ============================================================
 * Timeline rendering
 * ============================================================ */

function showTimelineSkeleton() {
  const tl = document.getElementById('timeline');
  tl.innerHTML = `
    <div class="skeleton-list" style="padding:6px 4px;">
      <div class="skeleton-row" style="height:48px;"></div>
      <div class="skeleton-row" style="height:48px;"></div>
      <div class="skeleton-row" style="height:48px;"></div>
    </div>`;
}

function renderTimeline() {
  const tl = document.getElementById('timeline');
  const events = state.journey;
  if (!state.selectedRunId) {
    tl.innerHTML = `<div class="timeline-empty"><p>Select a run to inspect its timeline.</p></div>`;
    return;
  }
  if (events.length === 0) {
    tl.innerHTML = `<div class="timeline-empty"><p>Waiting for events…</p></div>`;
    return;
  }

  // Truncate to last TIMELINE_MAX, plus a "load earlier" stub if we cut.
  const start = Math.max(0, events.length - TIMELINE_MAX);
  const visible = events.slice(start);
  const frag = document.createDocumentFragment();
  if (start > 0) {
    const btn = document.createElement('button');
    btn.className = 'load-earlier';
    btn.textContent = `Load earlier (${start} hidden) — render all`;
    btn.addEventListener('click', () => {
      // For now, expanding shows everything.  In huge runs this still gates re-render
      // to a single click rather than continuous DOM growth on scroll.
      const allFrag = document.createDocumentFragment();
      events.forEach((e) => allFrag.appendChild(buildStepCard(e)));
      tl.innerHTML = '';
      tl.appendChild(allFrag);
    });
    frag.appendChild(btn);
  }
  visible.forEach((e) => frag.appendChild(buildStepCard(e)));
  tl.innerHTML = '';
  tl.appendChild(frag);

  if (state.autoScrollTimeline) tl.scrollTop = tl.scrollHeight;
  hideNewestPill();
  applyTimelineFilter();
}

/** Show/hide timeline step cards based on `state.timelineFilter`. Matches against
 *  each card's pre-computed `data-search` haystack (kind/name/summary/error). */
function applyTimelineFilter() {
  const q = (state.timelineFilter || '').trim().toLowerCase();
  const tl = document.getElementById('timeline');
  if (!tl) return;
  const cards = tl.querySelectorAll('.step-card');
  const countEl = document.getElementById('timeline-search-count');
  if (!q) {
    cards.forEach((c) => c.classList.remove('filtered-out'));
    if (countEl) countEl.textContent = '';
    return;
  }
  let shown = 0;
  cards.forEach((c) => {
    const hit = (c.dataset.search || '').includes(q);
    c.classList.toggle('filtered-out', !hit);
    if (hit) shown += 1;
  });
  if (countEl) countEl.textContent = `${shown} / ${cards.length}`;
}

function buildStepCard(ev) {
  const card = document.createElement('article');
  card.className = 'step-card';
  if (ev.error_message) card.classList.add('error');
  card.dataset.eventId = ev.event_id;
  card.setAttribute('role', 'listitem');

  const icon = EVENT_ICONS[ev.event_type] || '•';
  const ts = formatTs(ev.timestamp);
  const name = ev.tool_name || ev.agent_name || ev.event_type;
  const summary = ev.summary || '';
  // Pre-computed haystack for the timeline filter (kind/name/summary/error).
  card.dataset.search = `${ev.event_type} ${name} ${summary} ${ev.error_message || ''}`.toLowerCase();
  const dur = ev.duration_ms != null ? `<span class="badge duration">${formatMs(ev.duration_ms)}</span>` : '';
  const tokens = (ev.token_input != null || ev.token_output != null)
    ? `<span class="badge tokens">↑${ev.token_input || 0} ↓${ev.token_output || 0}</span>`
    : '';
  const errorBadge = ev.error_message ? `<span class="badge error">error</span>` : '';
  const openBtn = ev.blob_path ? `<button class="open-btn" data-event-id="${escapeHtml(ev.event_id)}" aria-label="Open step details">Open</button>` : '';

  card.innerHTML = `
    <div class="row-1">
      <span class="icon ${ev.error_message ? 'error' : ''}" aria-hidden="true">${icon}</span>
      <span class="ts">${ts}</span>
      <span class="name" title="${escapeHtml(name)}">${escapeHtml(name)}</span>
      ${openBtn}
    </div>
    ${summary ? `<div class="summary" title="${escapeHtml(summary)}">${escapeHtml(summary)}</div>` : ''}
    ${(dur || tokens || errorBadge) ? `<div class="badges">${dur}${tokens}${errorBadge}</div>` : ''}
  `;

  if (ev.blob_path) {
    card.querySelector('.open-btn').addEventListener('click', (e) => {
      e.stopPropagation();
      openStepDrawer(ev);
    });
  }
  card.addEventListener('click', () => openStepDrawer(ev));
  return card;
}

function appendStepCard(ev) {
  if (state.knownEventIds.has(ev.event_id)) return;
  state.knownEventIds.add(ev.event_id);
  state.journey.push(ev);

  const tl = document.getElementById('timeline');
  // If we're showing the empty/skeleton placeholder, full re-render.
  if (!tl.querySelector('.step-card') && !tl.querySelector('.load-earlier')) {
    renderTimeline();
    return;
  }
  // If we're at TIMELINE_MAX cap, drop the oldest visible card.
  const cards = tl.querySelectorAll('.step-card');
  if (cards.length >= TIMELINE_MAX) {
    cards[0].remove();
  }

  tl.appendChild(buildStepCard(ev));
  if (state.timelineFilter) applyTimelineFilter();

  if (state.autoScrollTimeline) {
    tl.scrollTop = tl.scrollHeight;
  } else {
    state.newEventCount += 1;
    showNewestPill();
  }

  // Pulse graph node + flash edge from previous event's node.
  pulseGraphForEvent(ev);

  // Keep the manual-replay step list current as live events stream in, so the
  // "Step X / N" total and prev/next reach later-arriving events without the
  // user re-selecting the run. manualStepIdx is preserved.
  if (state.replayMode === 'manual') {
    state.replaySteps = buildReplaySteps();
    updateManualStepUI();
  }
}

function showNewestPill() {
  const pill = document.getElementById('newest-pill');
  pill.classList.remove('hidden');
  document.getElementById('newest-count').textContent = state.newEventCount.toString();
}
function hideNewestPill() {
  state.newEventCount = 0;
  document.getElementById('newest-pill').classList.add('hidden');
}

function formatTs(iso) {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '—';
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  const ss = String(d.getSeconds()).padStart(2, '0');
  const ms = String(d.getMilliseconds()).padStart(3, '0');
  return `${hh}:${mm}:${ss}.${ms}`;
}

/* ============================================================
 * Drawer (step details / node details)
 * ============================================================ */

/** Which half of a logical step an event represents: the request (inputs) or the
 *  response (outputs / error). Used to pair a step's two events by run_id. */
function stepPhase(eventType) {
  if (/_(end|finish|error)$/.test(eventType)) return 'response';
  if (/_start$/.test(eventType) || eventType === 'agent_action' || eventType === 'run_start') return 'request';
  return null;
}

function openStepDrawer(ev) {
  const drawer = document.getElementById('drawer');
  const body = document.getElementById('drawer-body');
  // Token for the async blob fetch below: if the user opens another step before
  // the fetch resolves, this changes and the stale response is discarded rather
  // than written into the now-different drawer.
  state.currentDrawerEventId = ev.event_id;
  document.getElementById('drawer-title').textContent =
    `${(EVENT_ICONS[ev.event_type] || '•')} ${ev.event_type}`;

  const meta = [
    ['agent', ev.agent_name],
    ['tool', ev.tool_name],
    ['run_id', ev.run_id],
    ['event_id', ev.event_id],
    ['parent_run_id', ev.parent_run_id],
    ['timestamp', ev.timestamp],
    ['duration', ev.duration_ms != null ? formatMs(ev.duration_ms) : null],
    ['tokens (in/out)', (ev.token_input != null || ev.token_output != null)
      ? `${ev.token_input || 0} / ${ev.token_output || 0}` : null],
    ['error', ev.error_message],
  ].filter(([, v]) => v != null && v !== '');

  // A logical step is a request (*_start) + a response (*_end/_error) sharing the
  // same run_id. Find both halves so the drawer can show the full REQUEST and
  // RESPONSE payloads together, no matter which card was clicked.
  const siblings = (state.journey || []).filter((e) => e.run_id === ev.run_id);
  const pickWithBlob = (phase) =>
    siblings.find((e) => stepPhase(e.event_type) === phase && e.blob_path)
    || (stepPhase(ev.event_type) === phase && ev.blob_path ? ev : null);
  const reqEv = pickWithBlob('request');
  const resEv = pickWithBlob('response');

  const payloadSection = (id, title, sourceEv) => sourceEv ? `
    <section class="drawer-section">
      <h4>${title}</h4>
      <div id="${id}-status" class="dim">Loading…</div>
      <pre id="${id}" class="json-pre hidden"></pre>
    </section>` : '';

  body.innerHTML = `
    <section class="drawer-section">
      <h4>Metadata</h4>
      ${meta.map(([k, v]) => `
        <div class="kv">
          <span class="kv-key">${escapeHtml(k)}</span>
          <span class="kv-val">${escapeHtml(v)}</span>
        </div>`).join('')}
    </section>
    <section class="drawer-section" id="drawer-summary-section">
      <h4>Summary <span style="font-weight:400;font-size:11px;color:var(--text-dim);">— short one-line preview; full request/response below</span></h4>
      <div style="white-space:pre-wrap; word-break:break-word; color:var(--text);">${escapeHtml(ev.summary || '(none)')}</div>
    </section>
    ${payloadSection('drawer-request', 'Request payload', reqEv)}
    ${payloadSection('drawer-response', 'Response payload', resEv)}
    ${(!reqEv && !resEv) ? `
      <section class="drawer-section">
        <div class="dim">No full payload captured for this step. (Request/response payloads
        are recorded for runs traced after this feature was enabled — re-run to see them.)</div>
      </section>` : ''}
  `;

  showDrawer();

  const token = ev.event_id;
  const loadInto = (sourceEv, preId) => {
    if (!sourceEv) return;
    apiGet(`/runs/${encodeURIComponent(sourceEv.run_id)}/steps/${encodeURIComponent(sourceEv.event_id)}/full`)
      .then((data) => {
        // Drawer moved on (or closed) while we were fetching — drop the result.
        if (state.currentDrawerEventId !== token) return;
        const status = document.getElementById(`${preId}-status`);
        const pre = document.getElementById(preId);
        if (!status || !pre) return;
        status.classList.add('hidden');
        pre.classList.remove('hidden');
        pre.innerHTML = highlightJson(data.full_payload);
      })
      .catch((err) => {
        if (state.currentDrawerEventId !== token) return;
        const status = document.getElementById(`${preId}-status`);
        if (status) status.textContent = `Failed: ${err.message}`;
      });
  };
  loadInto(reqEv, 'drawer-request');
  loadInto(resEv, 'drawer-response');
}

/** Inline icons + labels for each topology node kind. Used in the drawer's
 *  connections list and elsewhere where a quick visual signifier helps. */
const TYPE_ICONS = {
  agent: '⬡',
  mcp: '▥',
  tool: '▭',
  llm: '◯',
  retriever: '⌭',
  chain: '◇',
};
const TYPE_LABEL = {
  agent: 'Agent',
  mcp: 'MCP server',
  tool: 'Tool',
  llm: 'LLM',
  retriever: 'Retriever',
  chain: 'Chain',
};

/** Find every topology edge involving `nodeId`; split into outgoing ('calls')
 *  and incoming ('calledBy'). Each entry carries the other end's full node
 *  object plus the edge count. */
function getNodeConnections(nodeId) {
  const topology = state.topology || { nodes: [], edges: [] };
  const byId = new Map((topology.nodes || []).map((n) => [n.id, n]));
  const calls = [];
  const calledBy = [];
  for (const e of topology.edges || []) {
    if (e.source === nodeId) {
      const tgt = byId.get(e.target);
      if (tgt) calls.push({ node: tgt, count: e.count || 0, lastSeen: e.last_seen });
    } else if (e.target === nodeId) {
      const src = byId.get(e.source);
      if (src) calledBy.push({ node: src, count: e.count || 0, lastSeen: e.last_seen });
    }
  }
  // Most-used first.
  const sortFn = (a, b) =>
    (b.count - a.count) || a.node.name.localeCompare(b.node.name);
  calls.sort(sortFn);
  calledBy.sort(sortFn);
  return { calls, calledBy };
}

/** Render a list of connection rows (each one is a clickable row). */
function renderConnectionsList(connections) {
  if (connections.length === 0) {
    return '<div class="conn-empty">No connections recorded.</div>';
  }
  return `<div class="connections-list">${connections.map((c) => `
    <button class="connection-item" data-node-id="${escapeHtml(c.node.id)}" type="button" aria-label="${escapeHtml(c.node.name)}">
      <span class="conn-icon conn-icon-${c.node.type}" aria-hidden="true">${TYPE_ICONS[c.node.type] || '?'}</span>
      <span class="conn-text">
        <span class="conn-name">${escapeHtml(c.node.name)}</span>
        <span class="conn-type">${escapeHtml(TYPE_LABEL[c.node.type] || c.node.type)}</span>
      </span>
      <span class="conn-count" title="invocations across all runs">×${c.count}</span>
      <span class="conn-arrow" aria-hidden="true">›</span>
    </button>`).join('')}</div>`;
}

/** Render the "Calls" area of a node drawer. For an MCP server it lists the tools
 *  it provides (called or not); for an agent it groups what it calls into MCP
 *  servers vs in-code (local) tools vs everything else; otherwise a flat list. */
function renderCallsSection(nodeData, calls) {
  const block = (title, items) => `
    <section class="drawer-section">
      <h4>${escapeHtml(title)} <span class="count-pill">${items.length}</span></h4>
      ${renderConnectionsList(items)}
    </section>`;

  if (nodeData.type === 'mcp') {
    return block('Provides tools', calls);
  }
  if (nodeData.type === 'agent') {
    const servers = calls.filter((c) => c.node.type === 'mcp');
    const localTools = calls.filter((c) => c.node.type === 'tool' && !c.node.source);
    // MCP tools (tool with a source) are reachable by drilling into their server,
    // so they're not repeated here — keeps the agent view to servers + local tools.
    const other = calls.filter(
      (c) => c.node.type !== 'mcp' && !(c.node.type === 'tool'),
    );
    let html = '';
    if (servers.length) html += block('MCP servers', servers);
    if (localTools.length) html += block('In-code tools', localTools);
    if (other.length) html += block('Also calls', other);
    if (!html) html = block('Calls', []);
    return html;
  }
  return block('Calls', calls);
}

/** Navigate to a node: highlight in graph + reopen drawer with that node's
 *  details. Used when the user clicks a connection row. */
function navigateToNode(nodeId) {
  const topology = state.topology || { nodes: [] };
  const target = (topology.nodes || []).find((n) => n.id === nodeId);
  if (!target) return;
  if (graph?.isReady()) graph.setFocusedNode(nodeId);
  openNodeDrawer({
    id: target.id,
    type: target.type,
    label: target.name,
    source: target.source,
    invocations: target.invocation_count,
    errors: target.error_count,
    avgMs: target.avg_duration_ms,
    p99Ms: target.p99_duration_ms,
    lastSeen: target.last_seen,
  });
}

/** Match an event to a topology node (must mirror nodeIdForEvent in graph.js). */
function eventMatchesNode(ev, nodeData) {
  const t = ev.event_type || '';
  const isLlmEvt =
    t === 'chat_model_start' || t === 'llm_start' || t === 'llm_end' || t === 'llm_error';
  const isRetEvt =
    t === 'retriever_start' || t === 'retriever_end' || t === 'retriever_error';
  const isToolEvt = !!ev.tool_name && !isLlmEvt && !isRetEvt;
  if (nodeData.type === 'llm') return isLlmEvt && ev.agent_name === nodeData.label;
  if (nodeData.type === 'retriever') return isRetEvt && ev.agent_name === nodeData.label;
  if (nodeData.type === 'tool') return isToolEvt && ev.tool_name === nodeData.label;
  // agent / chain
  return ev.agent_name === nodeData.label && !isLlmEvt && !isRetEvt;
}

/** Render one rich event card with inline input/output text for the drawer. */
function renderEventCard(ev) {
  const dur = ev.duration_ms != null
    ? `<span class="badge">${formatMs(ev.duration_ms)}</span>`
    : '';
  const tok = (ev.token_input != null || ev.token_output != null)
    ? `<span class="badge">↑${ev.token_input ?? 0} ↓${ev.token_output ?? 0}</span>`
    : '';
  const errCls = ev.error_message ? ' error' : '';
  const blobBtn = ev.blob_path
    ? `<button class="btn-link" data-event-full="${escapeHtml(ev.event_id)}">view full ›</button>`
    : '';
  return `
    <article class="node-event-card${errCls}" data-event-id="${escapeHtml(ev.event_id)}">
      <header class="node-event-head">
        <span class="node-event-type">${escapeHtml(ev.event_type)}</span>
        <span class="node-event-time">${formatTs(ev.timestamp)}</span>
      </header>
      <div class="node-event-body">${escapeHtml(ev.summary || '(no summary)')}</div>
      <footer class="node-event-foot">
        ${dur}${tok}
        ${ev.error_message ? `<span class="badge error">${escapeHtml(ev.error_message).slice(0, 80)}</span>` : ''}
        ${blobBtn}
      </footer>
    </article>`;
}

function openNodeDrawer(nodeData) {
  const body = document.getElementById('drawer-body');
  const labelByType = {
    agent: 'Agent',
    mcp: 'MCP server',
    tool: 'Tool',
    llm: 'LLM',
    retriever: 'Retriever',
    chain: 'Chain',
  };
  const titlePrefix = labelByType[nodeData.type] || 'Node';
  document.getElementById('drawer-title').textContent = `${titlePrefix}: ${nodeData.label}`;

  const matching = (state.journey || []).filter((ev) => eventMatchesNode(ev, nodeData));
  matching.sort((a, b) => (a.timestamp || '').localeCompare(b.timestamp || ''));
  const visible = matching.slice(-12).reverse();
  const ctxLabel = state.selectedRunId
    ? `Invocations in selected run (${matching.length})`
    : 'Select a run to see invocations';

  // Connections derived from topology — what does this node call, and who calls it.
  const { calls, calledBy } = getNodeConnections(nodeData.id);

  // Tool-source provenance (MCP server vs local), looked up from topology by id.
  const topoNode = (state.topology?.nodes || []).find((n) => n.id === nodeData.id);
  const toolSource = topoNode?.source;
  const sourceFoot = nodeData.type === 'tool'
    ? (toolSource
        ? ` · Source: <strong>MCP ${escapeHtml(toolSource)}</strong>`
        : ' · Source: <strong>local</strong>')
    : '';

  body.innerHTML = `
    <section class="drawer-section drawer-hero">
      <div class="hero-row">
        <span class="hero-icon hero-icon-${escapeHtml(nodeData.type)}" aria-hidden="true">
          ${TYPE_ICONS[nodeData.type] || '?'}
        </span>
        <div class="hero-text">
          <div class="hero-kind">${escapeHtml(TYPE_LABEL[nodeData.type] || titlePrefix)}</div>
          <div class="hero-name">${escapeHtml(nodeData.label)}</div>
        </div>
      </div>
      <div class="stats-grid">
        <div class="stat">
          <span class="stat-label">Invocations</span>
          <span class="stat-value">${nodeData.invocations || 0}</span>
        </div>
        <div class="stat">
          <span class="stat-label">Errors</span>
          <span class="stat-value ${(nodeData.errors || 0) > 0 ? 'is-error' : ''}">${nodeData.errors || 0}</span>
        </div>
        <div class="stat">
          <span class="stat-label">Avg duration</span>
          <span class="stat-value">${formatMs(nodeData.avgMs)}</span>
        </div>
        <div class="stat">
          <span class="stat-label">P99 duration</span>
          <span class="stat-value">${formatMs(nodeData.p99Ms)}</span>
        </div>
      </div>
      <div class="hero-foot">Last seen: ${formatRelTime(nodeData.lastSeen)}${sourceFoot}</div>
    </section>

    ${renderCallsSection(nodeData, calls)}

    <section class="drawer-section">
      <h4>${nodeData.type === 'mcp' ? 'Used by' : 'Called by'} <span class="count-pill">${calledBy.length}</span></h4>
      ${renderConnectionsList(calledBy)}
    </section>

    <section class="drawer-section">
      <h4>${escapeHtml(ctxLabel)} ${state.selectedRunId ? `<span class="count-pill">${matching.length}</span>` : ''}</h4>
      <div class="node-events">
        ${visible.length
          ? visible.map(renderEventCard).join('')
          : `<div class="dim">No invocations in the loaded journey.${state.selectedRunId ? '' : ' Click a run on the left first.'}</div>`}
      </div>
    </section>
  `;
  // Connection-row click → navigate to that node (graph focus + drawer update).
  body.querySelectorAll('.connection-item').forEach((el) => {
    el.addEventListener('click', () => navigateToNode(el.dataset.nodeId));
  });
  // Clicking a card body opens the step drawer with the full payload.
  body.querySelectorAll('.node-event-card').forEach((el) => {
    el.addEventListener('click', (e) => {
      if (e.target.closest('[data-event-full]')) return;
      const id = el.dataset.eventId;
      const ev = state.journey.find((x) => x.event_id === id);
      if (ev) openStepDrawer(ev);
    });
  });
  body.querySelectorAll('[data-event-full]').forEach((el) => {
    el.addEventListener('click', (e) => {
      e.stopPropagation();
      const id = el.dataset.eventFull;
      const ev = state.journey.find((x) => x.event_id === id);
      if (ev) openStepDrawer(ev);
    });
  });
  showDrawer();
}

function openEdgeDrawer(edgeData) {
  const body = document.getElementById('drawer-body');
  document.getElementById('drawer-title').textContent = `Edge: ${edgeData.source} → ${edgeData.target}`;

  // Build a synthetic node descriptor for the target, then reuse the matcher.
  // Node ids are `${kind}:${name}` — split on the FIRST colon only so names
  // that themselves contain a colon (e.g. `llama3:8b`, `openai:gpt-4`) survive.
  const tgt = edgeData.target || ':';
  const ci = tgt.indexOf(':');
  const tgtType = ci >= 0 ? tgt.slice(0, ci) : '';
  const tgtName = ci >= 0 ? tgt.slice(ci + 1) : tgt;
  const targetNode = { type: tgtType, label: tgtName };
  const matching = (state.journey || []).filter((ev) => eventMatchesNode(ev, targetNode));
  matching.sort((a, b) => (a.timestamp || '').localeCompare(b.timestamp || ''));
  const visible = matching.slice(-10).reverse();

  body.innerHTML = `
    <section class="drawer-section">
      <h4>Edge</h4>
      <div class="kv"><span class="kv-key">source</span><span class="kv-val">${escapeHtml(edgeData.source)}</span></div>
      <div class="kv"><span class="kv-key">target</span><span class="kv-val">${escapeHtml(edgeData.target)}</span></div>
      <div class="kv"><span class="kv-key">traversals (all runs)</span><span class="kv-val">${edgeData.count || 0}</span></div>
      <div class="kv"><span class="kv-key">last seen</span><span class="kv-val">${formatRelTime(edgeData.lastSeen)}</span></div>
    </section>
    <section class="drawer-section">
      <h4>Invocations of target in selected run (${matching.length})</h4>
      <div class="node-events">
        ${visible.length
          ? visible.map(renderEventCard).join('')
          : `<div class="dim">No invocations in this run's journey.</div>`}
      </div>
    </section>
  `;
  body.querySelectorAll('.node-event-card').forEach((el) => {
    el.addEventListener('click', (e) => {
      if (e.target.closest('[data-event-full]')) return;
      const id = el.dataset.eventId;
      const ev = state.journey.find((x) => x.event_id === id);
      if (ev) openStepDrawer(ev);
    });
  });
  body.querySelectorAll('[data-event-full]').forEach((el) => {
    el.addEventListener('click', (e) => {
      e.stopPropagation();
      const id = el.dataset.eventFull;
      const ev = state.journey.find((x) => x.event_id === id);
      if (ev) openStepDrawer(ev);
    });
  });
  showDrawer();
}

function showDrawer() {
  document.getElementById('drawer').classList.add('open');
  document.getElementById('drawer-backdrop').classList.remove('hidden');
  requestAnimationFrame(() => document.getElementById('drawer-backdrop').classList.add('visible'));
  document.getElementById('drawer').setAttribute('aria-hidden', 'false');
}

function closeDrawer() {
  document.getElementById('drawer').classList.remove('open');
  document.getElementById('drawer-backdrop').classList.remove('visible');
  setTimeout(() => document.getElementById('drawer-backdrop').classList.add('hidden'), 220);
  document.getElementById('drawer').setAttribute('aria-hidden', 'true');
  // Invalidate any in-flight blob fetch so it can't write into a reopened drawer.
  state.currentDrawerEventId = null;
}

/* ============================================================
 * Lightweight JSON syntax highlighter (~30 LOC, no deps)
 * ============================================================ */

function highlightJson(value) {
  // Guard FIRST: null/undefined (and anything that stringifies to undefined,
  // e.g. a bare `undefined` payload) must short-circuit before any .replace,
  // otherwise we'd call String.prototype.replace on `undefined` and throw.
  if (value == null) return '';
  const json = JSON.stringify(value, null, 2);
  if (typeof json !== 'string') return '';
  // Escape HTML once; then use class-tagged spans for tokens.
  const escaped = json
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
  // Match strings, then numbers, then bool/null. Strings include the trailing
  // optional ":" so we can mark them as keys.
  return escaped.replace(
    /("(\\u[a-fA-F0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g,
    (match) => {
      let cls = 'tok-num';
      if (/^"/.test(match)) {
        cls = /:\s*$/.test(match) ? 'tok-key' : 'tok-str';
      } else if (/^(true|false)$/.test(match)) {
        cls = 'tok-bool';
      } else if (/^null$/.test(match)) {
        cls = 'tok-null';
      }
      return `<span class="${cls}">${match}</span>`;
    },
  );
}

/* ============================================================
 * WebSocket — global runs feed
 * ============================================================ */

function openRunsWS() {
  if (wsRuns) try { wsRuns.close(); } catch { /* noop */ }
  wsRunsCloseRequested = false;

  const url = wsUrl('/ws/runs');
  setConnState('connecting');

  let ws;
  try {
    ws = new WebSocket(url);
  } catch (e) {
    setConnState('disconnected');
    scheduleRunsReconnect();
    return;
  }
  wsRuns = ws;

  ws.onopen = () => {
    wsRunsBackoff = 0;
    setConnState('connected');
  };
  ws.onerror = () => { /* onclose will fire next */ };
  ws.onclose = () => {
    setConnState('disconnected');
    if (!wsRunsCloseRequested) scheduleRunsReconnect();
  };
  ws.onmessage = (m) => {
    try {
      const msg = JSON.parse(m.data);
      handleRunsWsMessage(msg);
    } catch (e) {
      console.warn('bad ws msg', e);
    }
  };
}

function scheduleRunsReconnect() {
  const delay = WS_BACKOFF[Math.min(wsRunsBackoff, WS_BACKOFF.length - 1)];
  wsRunsBackoff += 1;
  toast(`Disconnected. Reconnecting in ${Math.round(delay/1000)}s…`, 'warning', 2500);
  setTimeout(() => {
    if (!wsRunsCloseRequested) openRunsWS();
  }, delay);
}

function handleRunsWsMessage(msg) {
  if (!msg || !msg.msg_type) return;
  if (msg.msg_type === 'run_update' && msg.payload?.run) {
    upsertRun(msg.payload.run);
  } else if (msg.msg_type === 'event' && msg.payload?.event_id) {
    // The global feed may not carry per-run events depending on backend wiring;
    // keep a defensive path that bumps run.total_steps when we see events.
    // (StoredEvent fields are flat in payload — gate on event_id, not .event.)
    const run = state.runsById.get(msg.run_id);
    if (run) {
      run.total_steps += 1;
      renderRunList();
    }
    addEventToRateWindow();
  }
}

/* ============================================================
 * WebSocket — per-run trace
 * ============================================================ */

function openTraceWS(runId) {
  closeTraceWS();
  wsTraceCloseRequested = false;
  const url = wsUrl(`/ws/trace/${encodeURIComponent(runId)}`);
  let ws;
  try {
    ws = new WebSocket(url);
  } catch {
    scheduleTraceReconnect(runId);
    return;
  }
  wsTrace = ws;

  ws.onopen = () => { wsTraceBackoff = 0; };
  ws.onerror = () => { /* onclose will fire next */ };
  ws.onclose = () => {
    if (state.selectedRunId === runId && !wsTraceCloseRequested) {
      // Refetch journey via REST to fill any gap, then reconnect WS.
      apiGet(`/runs/${encodeURIComponent(runId)}/journey`)
        .then((data) => {
          (data.steps || []).forEach((s) => {
            if (!state.knownEventIds.has(s.event_id)) {
              state.knownEventIds.add(s.event_id);
              state.journey.push(s);
            }
          });
          renderTimeline();
        })
        .catch((err) => console.warn('catchup refetch failed', err));
      scheduleTraceReconnect(runId);
    }
  };

  ws.onmessage = (m) => {
    try {
      const msg = JSON.parse(m.data);
      handleTraceWsMessage(msg);
    } catch (e) {
      console.warn('bad trace ws msg', e);
    }
  };
}

function closeTraceWS() {
  if (wsTrace) {
    wsTraceCloseRequested = true;
    try { wsTrace.close(); } catch { /* noop */ }
    wsTrace = null;
  }
}

function scheduleTraceReconnect(runId) {
  const delay = WS_BACKOFF[Math.min(wsTraceBackoff, WS_BACKOFF.length - 1)];
  wsTraceBackoff += 1;
  setTimeout(() => {
    if (state.selectedRunId === runId) openTraceWS(runId);
  }, delay);
}

function handleTraceWsMessage(msg) {
  if (!msg || msg.run_id !== state.selectedRunId) return;
  if (msg.msg_type === 'catchup' && Array.isArray(msg.payload?.steps)) {
    msg.payload.steps.forEach((s) => {
      if (!state.knownEventIds.has(s.event_id)) {
        state.knownEventIds.add(s.event_id);
        state.journey.push(s);
      }
    });
    renderTimeline();
    applyRunTraceToGraph();
  } else if (msg.msg_type === 'event' && msg.payload?.event_id) {
    // The worker broadcasts the StoredEvent fields flat in `payload` (no nested
    // `event` key), so the live step is `msg.payload` itself.
    appendStepCard(msg.payload);
    addEventToRateWindow();
    scheduleGraphTopologyRefresh();
  } else if (msg.msg_type === 'run_update' && msg.payload?.run) {
    upsertRun(msg.payload.run);
  }
}

/* ============================================================
 * Stats poll + ev/s rate
 * ============================================================ */

async function pollStats() {
  try {
    const stats = await apiGet('/stats');
    document.getElementById('stat-running').querySelector('.stat-value').textContent =
      String(stats.runs_active ?? state.runs.filter((r) => r.status === 'running').length);
    const droppedEl = document.getElementById('stat-dropped');
    droppedEl.querySelector('.stat-value').textContent = String(stats.events_dropped || 0);
    droppedEl.classList.toggle('alert', (stats.events_dropped || 0) > 0);
  } catch (e) {
    /* keep the display; toast on first failure only handled elsewhere */
  } finally {
    setTimeout(pollStats, STATS_POLL_MS);
  }
}

function addEventToRateWindow() {
  const now = Date.now();
  state.evRateWindow.push(now);
  // 60-second window
  while (state.evRateWindow.length && state.evRateWindow[0] < now - 60_000) {
    state.evRateWindow.shift();
  }
  document.getElementById('stat-rate').querySelector('.stat-value').textContent =
    (state.evRateWindow.length / 60).toFixed(1);
}

async function pollHealth() {
  try {
    const h = await apiGet('/health');
    state.serverVersion = h.version || '?';
    document.getElementById('diag-version').textContent = h.version || '?';
    document.getElementById('diag-rest').textContent = 'ok';
    applyProjectName(h.project_name);
  } catch (e) {
    document.getElementById('diag-rest').textContent = 'unreachable';
  }
}

/** Show the optional project label (TRACESAGE_PROJECT_NAME) in the header + tab
 *  title so multiple apps' UIs are distinguishable. Hidden when unset. */
function applyProjectName(name) {
  const el = document.getElementById('project-name');
  const clean = (name || '').trim();
  if (el) {
    el.textContent = clean;
    el.classList.toggle('hidden', !clean);
  }
  document.title = clean ? `${clean} · tracesage` : 'tracesage';
}

/* ============================================================
 * Graph wiring
 * ============================================================ */

/** The `?scope=` value for topology/tools. Default 'run' scopes to the selected run
 *  (exact current structure — no stale nodes); with no run selected it falls back to
 *  the latest run. 'last10'/'all' are explicit opt-in aggregates. */
function topologyScopeParam() {
  const s = state.topologyScope || 'run';
  if (s === 'all') return 'all';
  if (s === 'lastn') return `last_n:${Math.max(1, state.topologyScopeN || 5)}`;
  return state.selectedRunId ? `run:${state.selectedRunId}` : 'last_n:1';
}

async function loadTopology() {
  try {
    const scope = encodeURIComponent(topologyScopeParam());
    state.topology = await apiGet(`/topology?scope=${scope}`);
    if (graph?.isReady()) graph.setTopology(state.topology);
    renderMcpLegend();
  } catch (e) {
    toast(`Could not load topology: ${e.message}`, 'error', 3000);
  }
  // Tools-by-source derives from the same events; refresh it alongside topology.
  loadTools();
}

async function loadTools() {
  try {
    const scope = encodeURIComponent(topologyScopeParam());
    renderToolsPanel(await apiGet(`/tools?scope=${scope}`));
  } catch {
    /* non-fatal: leave the panel showing its last state */
  }
}

function renderToolsPanel(data) {
  const body = document.getElementById('tools-panel-body');
  if (!body) return;
  const sources = (data && data.sources) || [];
  if (!sources.length) {
    body.innerHTML = '<div class="tools-panel-empty">No tools yet</div>';
    applyToolsCollapsed();
    return;
  }
  body.innerHTML = sources.map((s) => {
    const isMcp = s.kind === 'mcp';
    // Per-server colour shared with the graph rings + legend (mcpServerColor).
    const color = isMcp ? mcpServerColor(s.source) : 'var(--text-dim)';
    const label = isMcp ? escapeHtml(s.source) : 'Local';
    const badge = isMcp ? 'MCP' : 'LOCAL';
    const count = `${s.tool_count}`;
    const rows = (s.tools || []).map((t) => `
      <div class="tool-row">
        <span class="tool-dot" style="background:${color}"></span>
        <span class="tool-name" title="${escapeHtml(t.name)}">${escapeHtml(t.name)}</span>
        <span class="tool-invocations" title="invocations">${Number(t.invocations) || 0}×</span>
        ${t.errors ? `<span class="tool-errors" title="errors">${Number(t.errors)} err</span>` : ''}
      </div>`).join('');
    return `
      <div class="tool-source-group" style="border-left-color:${color}">
        <div class="tool-source-head">
          <span class="tool-source-badge ${isMcp ? 'mcp' : 'local'}" style="${isMcp ? `background:${color}1f;color:${color};border-color:${color}` : ''}">${badge}</span>
          <span class="tool-source-name">${label}</span>
          <span class="tool-source-count" title="tools">${count}</span>
        </div>
        ${rows}
      </div>`;
  }).join('');
  applyToolsCollapsed();
}

/** Rebuild the dynamic "MCP servers" legend section from the current topology,
 *  using the same colours as the graph rings + the panel. Hidden when none. */
function renderMcpLegend() {
  const container = document.getElementById('legend-mcp-servers');
  if (!container) return;
  const servers = [
    ...new Set(
      (state.topology?.nodes || [])
        .filter((n) => n.type === 'tool' && n.source)
        .map((n) => n.source),
    ),
  ].sort();
  if (!servers.length) {
    container.innerHTML = '';
    container.classList.add('hidden');
    return;
  }
  container.classList.remove('hidden');
  container.innerHTML =
    '<div class="legend-section-title">MCP servers</div>' +
    servers.map((s) =>
      `<div class="legend-item"><span class="legend-mcp-dot" style="background:${mcpServerColor(s)}"></span>${escapeHtml(s)}</div>`
    ).join('');
}

/** Apply the persisted collapsed state to the panel (class + toggle glyph). Called
 *  on toggle AND after every renderToolsPanel so the two never desync. */
function applyToolsCollapsed() {
  const panel = document.getElementById('tools-panel');
  const toggle = document.getElementById('tools-panel-toggle');
  if (!panel) return;
  panel.classList.toggle('collapsed', state.toolsCollapsed);
  if (toggle) {
    toggle.setAttribute('aria-expanded', state.toolsCollapsed ? 'false' : 'true');
    toggle.innerHTML = state.toolsCollapsed ? '&#x2b;' : '&#x2212;';
  }
  // Expanding grows the panel height; if it was parked near the bottom edge the
  // body could spill out of the clipped pane. Re-clamp when a position is set.
  if (panel.style.left) clampToolsPanel(panel);
}

/** Clamp the panel so the WHOLE of it stays inside the graph pane's visible
 *  box. The pane has overflow:hidden, so any part pushed past an edge would be
 *  clipped ("hidden in the sidebar"); keeping the full width AND height in view
 *  prevents that. Safe to call any time the panel has an explicit left/top. */
function clampToolsPanel(panel) {
  const pane = panel.parentElement;
  if (!pane) return;
  const pb = pane.getBoundingClientRect();
  const pr = panel.getBoundingClientRect();
  // Current offset of the panel within the pane.
  let left = pr.left - pb.left;
  let top = pr.top - pb.top;
  const maxLeft = Math.max(4, pb.width - panel.offsetWidth - 4);
  const maxTop = Math.max(4, pb.height - panel.offsetHeight - 4);
  left = Math.max(4, Math.min(left, maxLeft));
  top = Math.max(4, Math.min(top, maxTop));
  panel.style.left = `${left}px`;
  panel.style.top = `${top}px`;
  panel.style.right = 'auto';
}

function wireToolsPanel() {
  const panel = document.getElementById('tools-panel');
  const header = panel && panel.querySelector('.tools-panel-header');
  if (!panel || !header) return;

  // Restore a saved position so the panel stays where the user parked it (out of
  // the way of the llm/retriever columns on the right). Clamp it afterwards: the
  // pane may now be narrower (window resize, expanded side pane) than when saved,
  // and an un-clamped position would leave the panel clipped/off-screen.
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_TOOLS_POS) || 'null');
    if (saved && Number.isFinite(saved.left) && Number.isFinite(saved.top)) {
      panel.style.left = `${saved.left}px`;
      panel.style.top = `${saved.top}px`;
      panel.style.right = 'auto';
      clampToolsPanel(panel);
    }
  } catch { /* ignore bad saved value */ }

  // Keep the panel on-screen whenever the graph pane changes size — window
  // resize AND (crucially) a side pane being expanded/collapsed, which shrinks
  // the graph pane via a grid-column change rather than a window resize. A
  // ResizeObserver catches all of these (and fires during the 240ms pane
  // transition) so a panel parked at the right edge slides left to stay visible
  // instead of being covered by the opening sidebar.
  const pane = panel.parentElement;
  if (pane && 'ResizeObserver' in window) {
    const ro = new ResizeObserver(() => {
      if (panel.style.left) clampToolsPanel(panel);
    });
    ro.observe(pane);
  } else {
    window.addEventListener('resize', () => {
      if (panel.style.left) clampToolsPanel(panel);
    });
  }

  // Drag the header to MOVE the panel; a plain click (no drag) toggles collapse.
  let startX = 0, startY = 0, origLeft = 0, origTop = 0, pressed = false, dragging = false;

  const onMove = (e) => {
    if (!pressed) return;
    const dx = e.clientX - startX;
    const dy = e.clientY - startY;
    if (!dragging && Math.hypot(dx, dy) < 4) return;   // below threshold = still a click
    dragging = true;
    const pane = panel.parentElement;
    const pb = pane.getBoundingClientRect();
    // Clamp the WHOLE panel (width and height) inside the pane so it can never be
    // dragged partially under the clipped edge.
    const maxLeft = Math.max(4, pb.width - panel.offsetWidth - 4);
    const maxTop = Math.max(4, pb.height - panel.offsetHeight - 4);
    const left = Math.max(4, Math.min(origLeft + dx, maxLeft));
    const top = Math.max(4, Math.min(origTop + dy, maxTop));
    panel.style.left = `${left}px`;
    panel.style.top = `${top}px`;
    panel.style.right = 'auto';
  };

  const onUp = () => {
    if (!pressed) return;
    pressed = false;
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
    if (dragging) {
      try {
        localStorage.setItem(STORAGE_TOOLS_POS, JSON.stringify({
          left: parseFloat(panel.style.left),
          top: parseFloat(panel.style.top),
        }));
      } catch { /* ignore */ }
    } else {
      state.toolsCollapsed = !state.toolsCollapsed;
      applyToolsCollapsed();
    }
    dragging = false;
  };

  header.addEventListener('mousedown', (e) => {
    if (e.button !== 0) return;
    pressed = true;
    dragging = false;
    startX = e.clientX;
    startY = e.clientY;
    const pane = panel.parentElement;
    const pr = panel.getBoundingClientRect();
    const pb = pane.getBoundingClientRect();
    origLeft = pr.left - pb.left;
    origTop = pr.top - pb.top;
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
    e.preventDefault();   // prevent text selection while dragging
  });

  applyToolsCollapsed();
}

function setGraphMode(mode) {
  state.graphMode = mode;
  // The topology-scope selector only applies to the Topology view.
  const scopeWrap = document.getElementById('topology-scope-wrap');
  if (scopeWrap) scopeWrap.classList.toggle('hidden', mode !== 'topology');
  document.querySelectorAll('.seg-btn').forEach((b) => {
    const isActive = b.dataset.mode === mode;
    b.classList.toggle('active', isActive);
    b.setAttribute('aria-selected', isActive ? 'true' : 'false');
  });
  if (mode === 'topology') {
    // Stop any in-flight replay (auto or manual) so the graph doesn't keep
    // animating after the user has explicitly asked to leave trace view.
    if (state.autoReplayInProgress && graph?.isReady()) {
      graph.cancelReplay();
      state.autoReplayInProgress = false;
    }
    hideReplayStepBadge();
    state.manualStepIdx = -1;
    if (typeof updateManualStepUI === 'function') updateManualStepUI();
    clearTimelineEventHighlight();
    // Hide the replay controls bar entirely while in pure topology — there's
    // nothing to replay when the user is browsing the system architecture.
    document.getElementById('replay-controls').classList.add('hidden');
    if (graph?.isReady()) {
      graph.clearStepHighlight();
      graph.clearRunTrace();   // resets to column layout, drops all run state
    }
  } else {
    document.getElementById('replay-controls').classList.remove('hidden');
    applyRunTraceToGraph();
  }
}

function setGraphModeAvailable(available) {
  document.querySelector('.seg-btn[data-mode="trace"]').disabled = !available;
}

function applyRunTraceToGraph() {
  if (!graph?.isReady() || state.graphMode !== 'trace') return;
  const nodeIds = new Set();
  const edges = [];
  let prev = null;
  for (const ev of state.journey) {
    const id = nodeIdForEvent(ev);
    if (!id) continue;
    nodeIds.add(id);
    if (prev && prev !== id) edges.push({ source: prev, target: id });
    prev = id;
  }
  // Pass the ordered journey so the graph can switch to a sequential trace
  // layout instead of the column layout used in topology mode.
  graph.setRunTrace([...nodeIds], edges, state.journey);
}

/** Pulse on graph for a single new event. */
function pulseGraphForEvent(ev) {
  if (!graph?.isReady()) return;
  const nodeId = nodeIdForEvent(ev);
  if (!nodeId) return;
  graph.pulseNode(nodeId);

  // Find previous event for edge dot.
  const idx = state.journey.findIndex((e) => e.event_id === ev.event_id);
  if (idx > 0) {
    for (let i = idx - 1; i >= 0; i--) {
      const prevId = nodeIdForEvent(state.journey[i]);
      if (prevId && prevId !== nodeId) {
        graph.flashEdge(prevId, nodeId);
        break;
      }
    }
  }
}

/** Debounced re-render of topology — bursts of WS events shouldn't cause one
 *  Cytoscape mutation per event. */
function scheduleGraphTopologyRefresh() {
  if (state.pendingTopologyUpdate) return;
  state.pendingTopologyUpdate = true;
  if (state.graphRenderTimer) clearTimeout(state.graphRenderTimer);
  state.graphRenderTimer = setTimeout(async () => {
    state.pendingTopologyUpdate = false;
    // An in-flight auto-replay holds references to the current node/edge
    // elements; rebuilding the graph would orphan them and drop the moving
    // dot mid-step. Skip — a live run keeps emitting events that retrigger
    // this, and a finished run's topology is already static.
    if (state.autoReplayInProgress) return;
    try {
      const topo = await apiGet('/topology');
      state.topology = topo;
      if (graph?.isReady()) graph.setTopology(topo);
      // setTopology rebuilds the node/edge elements, dropping all step-*
      // highlight classes — re-apply whatever should currently be showing.
      if (state.graphMode === 'trace') applyRunTraceToGraph();
      if (state.replayMode === 'manual' && state.manualStepIdx >= 0 && graph?.isReady()) {
        const step = state.replaySteps[state.manualStepIdx];
        if (step) graph.highlightStep(step.source, step.target, { animate: false, pulse: false });
      }
    } catch { /* swallow */ }
  }, 300);
}

/* ============================================================
 * Empty state + replay
 * ============================================================ */

function renderEmptyState() {
  const empty = document.getElementById('graph-empty');
  if (state.runs.length === 0) {
    empty.classList.remove('hidden');
  } else {
    empty.classList.add('hidden');
  }
}

/* ---- Replay: shared helpers ---- */

/** Build the ordered list of replay steps from the current journey. Each step
 *  is an edge transition between two distinct topology node ids; we also keep
 *  the event_id of the event that landed us at the target so the UI can show
 *  that event's full details when the user navigates to that step. */
function buildReplaySteps() {
  const steps = [];
  let prevId = null;
  for (const ev of state.journey) {
    const id = nodeIdForEvent(ev);
    if (!id) continue;
    if (prevId && prevId !== id) {
      steps.push({ source: prevId, target: id, eventId: ev.event_id });
    }
    prevId = id;
  }
  return steps;
}

/* ---- Replay: auto mode ---- */

async function startReplay() {
  if (!state.selectedRunId || !graph?.isReady()) return;
  if (state.autoReplayInProgress) return; // ignore re-entry while playing
  const speed = parseFloat(document.getElementById('replay-speed').value || '1');
  const edges = buildReplaySteps();
  if (edges.length === 0) {
    toast('Nothing to replay yet — no traversed edges.', 'info', 2500);
    return;
  }
  state.autoReplayInProgress = true;
  toast(`Replaying ${edges.length} step${edges.length === 1 ? '' : 's'} at ${speed}x`, 'info', 2000);
  showReplayStepBadge(0, edges.length, '');
  try {
    await graph.playRun(edges, speed);
  } finally {
    state.autoReplayInProgress = false;
    hideReplayStepBadge();
  }
}

/* ---- Replay: manual mode ---- */

/** Switch between 'auto' and 'manual' replay UIs. If the user switches to
 *  manual while autoplay is in flight, we stop autoplay cleanly: cancel the
 *  loop in graph.playRun, drop any in-flight dot, hide the step badge. */
function setReplayMode(mode) {
  // If autoplay is running, stop it before swapping UIs.
  if (state.autoReplayInProgress && graph?.isReady()) {
    graph.cancelReplay();
    state.autoReplayInProgress = false;
    hideReplayStepBadge();
  }

  state.replayMode = mode;
  // Tab buttons
  document.querySelectorAll('.replay-mode-btn').forEach((b) => {
    const isActive = b.dataset.replayMode === mode;
    b.classList.toggle('active', isActive);
    b.setAttribute('aria-selected', isActive ? 'true' : 'false');
  });
  // Show/hide control groups
  document.getElementById('replay-controls-auto').classList.toggle('hidden', mode !== 'auto');
  document.getElementById('replay-controls-manual').classList.toggle('hidden', mode !== 'manual');
  if (mode === 'manual') {
    state.replaySteps = buildReplaySteps();
    state.manualStepIdx = -1;
    updateManualStepUI();
    if (graph?.isReady()) graph.clearStepHighlight();
    clearTimelineEventHighlight();
  } else {
    // Leaving manual mode — clear any per-step highlights so the user
    // sees the calm trace view before pressing Play.
    if (graph?.isReady()) graph.clearStepHighlight();
    clearTimelineEventHighlight();
  }
}

/** Move forward by one manual step. */
function manualNext() {
  if (state.replayMode !== 'manual') return;
  if (state.replaySteps.length === 0) state.replaySteps = buildReplaySteps();
  const next = state.manualStepIdx + 1;
  if (next >= state.replaySteps.length) return; // already at end
  applyManualStep(next, { reverse: false });
}

/** Move backward by one manual step. */
function manualPrev() {
  if (state.replayMode !== 'manual') return;
  if (state.manualStepIdx <= 0) return; // already at start
  applyManualStep(state.manualStepIdx - 1, { reverse: true });
}

/** Render manual step idx: highlight on graph + corresponding card in timeline.
 *  We animate the moving dot so the user can SEE the transition, but skip
 *  the target-node pulse so rapid prev/next clicks don't stack glow effects.
 *  The dot animation is cancellable in graph.js, so clicking fast never piles
 *  up multiple in-flight dots. Reverse=true makes the dot run target->source
 *  for the 'previous' action, which matches the user's expected direction. */
function applyManualStep(idx, opts = {}) {
  state.manualStepIdx = idx;
  const step = state.replaySteps[idx];
  if (!step) return;
  if (graph?.isReady()) {
    graph.highlightStep(step.source, step.target, {
      animate: true,
      pulse: false,
      reverse: !!opts.reverse,
    });
  }
  highlightTimelineEvent(step.eventId);
  updateManualStepUI();
}

/** Update the prev/next disabled state and the step counter text. */
function updateManualStepUI() {
  const total = state.replaySteps.length;
  const idx = state.manualStepIdx;
  document.getElementById('manual-step-current').textContent = String(Math.max(0, idx + 1));
  document.getElementById('manual-step-total').textContent = String(total);
  // Previous disabled at start (idx 0 or before).
  document.getElementById('replay-prev').disabled = idx <= 0;
  // Next disabled at end (last step is idx total-1).
  document.getElementById('replay-next').disabled = total === 0 || idx >= total - 1;
}

/** Apply 'current-step' highlight to the timeline card for this event id and
 *  scroll it into view. Removes the highlight from any previously-current card. */
function highlightTimelineEvent(eventId) {
  const tl = document.getElementById('timeline');
  if (!tl) return;
  tl.querySelectorAll('.step-card.current-step').forEach((el) => el.classList.remove('current-step'));
  if (!eventId) return;
  const card = tl.querySelector(`.step-card[data-event-id="${cssEscapeAttr(eventId)}"]`);
  if (card) {
    card.classList.add('current-step');
    card.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
}

function clearTimelineEventHighlight() {
  const tl = document.getElementById('timeline');
  if (!tl) return;
  tl.querySelectorAll('.step-card.current-step').forEach((el) => el.classList.remove('current-step'));
}

/** Escape an event id (uuid) for use in an attribute selector. UUIDs are
 *  hex+hyphens so naive escaping is fine, but use CSS.escape when available. */
function cssEscapeAttr(s) {
  if (window.CSS && CSS.escape) return CSS.escape(s);
  return String(s).replace(/"/g, '\\"');
}

/* ---- Pane collapse / expand ---- */

function getPaneState() {
  try { return JSON.parse(localStorage.getItem(STORAGE_PANES)) || {}; }
  catch { return {}; }
}

function savePaneState(s) {
  try { localStorage.setItem(STORAGE_PANES, JSON.stringify(s)); } catch { /* full storage */ }
}

function applyPaneState() {
  const s = getPaneState();
  const runsPane = document.querySelector('.pane-runs');
  const timelinePane = document.querySelector('.pane-timeline');
  if (runsPane) runsPane.classList.toggle('collapsed', !!s.runsCollapsed);
  if (timelinePane) timelinePane.classList.toggle('collapsed', !!s.timelineCollapsed);
}

function togglePane(which) {
  const sel = which === 'runs' ? '.pane-runs' : '.pane-timeline';
  const pane = document.querySelector(sel);
  if (!pane) return;
  pane.classList.toggle('collapsed');
  const isCollapsed = pane.classList.contains('collapsed');
  const s = getPaneState();
  s[which === 'runs' ? 'runsCollapsed' : 'timelineCollapsed'] = isCollapsed;
  savePaneState(s);
}

function showReplayStepBadge(current, total, detail) {
  const badge = document.getElementById('replay-step-badge');
  if (!badge) return;
  document.getElementById('replay-step-current').textContent = String(current);
  document.getElementById('replay-step-total').textContent = String(total);
  document.getElementById('replay-step-detail').textContent = detail || '';
  badge.classList.add('visible');
}

function hideReplayStepBadge() {
  const badge = document.getElementById('replay-step-badge');
  if (badge) badge.classList.remove('visible');
}

/* ============================================================
 * Connection-state UI
 * ============================================================ */

function setConnState(state_) {
  const el = document.querySelector('.conn-indicator');
  const label = document.getElementById('stat-conn-label');
  const diagWs = document.getElementById('diag-ws');
  if (el) el.dataset.state = state_;
  if (label) label.textContent = state_;
  if (diagWs) diagWs.textContent = state_;
}

/* ============================================================
 * Modals / shortcuts / wiring
 * ============================================================ */

function openModal(id) {
  const m = document.getElementById(id);
  if (!m) return;
  m.classList.remove('hidden');
  m.setAttribute('aria-hidden', 'false');
  const focusTarget = m.querySelector('input, button');
  if (focusTarget) setTimeout(() => focusTarget.focus(), 50);
}

function closeModal(id) {
  const m = document.getElementById(id);
  if (!m) return;
  m.classList.add('hidden');
  m.setAttribute('aria-hidden', 'true');
}

function closeAllModals() {
  document.querySelectorAll('.modal').forEach((m) => {
    m.classList.add('hidden');
    m.setAttribute('aria-hidden', 'true');
  });
}

function wireKeyboard() {
  document.addEventListener('keydown', (e) => {
    const tag = (e.target?.tagName || '').toLowerCase();
    const inField = tag === 'input' || tag === 'textarea' || tag === 'select';

    if (e.key === 'Escape') {
      closeDrawer();
      closeAllModals();
      return;
    }
    if (inField) return;

    if (e.key === '/') {
      e.preventDefault();
      document.getElementById('search-input').focus();
    } else if (e.key === '?') {
      openModal('modal-help');
    } else if (e.key === 't') {
      toggleTheme();
    } else if (e.key === 'j' || e.key === 'k') {
      e.preventDefault();
      navigateRuns(e.key === 'j' ? 1 : -1);
    }
  });
}

function navigateRuns(delta) {
  const list = filteredRuns();
  if (list.length === 0) return;
  const idx = list.findIndex((r) => r.run_id === state.selectedRunId);
  const next = idx === -1
    ? (delta > 0 ? 0 : list.length - 1)
    : Math.max(0, Math.min(list.length - 1, idx + delta));
  selectRun(list[next].run_id);
  // Scroll to it.
  const row = document.querySelector(`.run-row[data-run-id="${CSS.escape(list[next].run_id)}"]`);
  if (row) row.scrollIntoView({ block: 'nearest' });
}

function wireUI() {
  document.getElementById('theme-toggle').addEventListener('click', toggleTheme);
  document.getElementById('settings-btn').addEventListener('click', () => {
    document.getElementById('auth-token-input').value = state.authToken;
    openModal('modal-settings');
  });
  document.getElementById('help-btn').addEventListener('click', () => openModal('modal-help'));
  document.getElementById('settings-save').addEventListener('click', () => {
    const v = document.getElementById('auth-token-input').value || '';
    state.authToken = v;
    if (v) localStorage.setItem(STORAGE_TOKEN, v);
    else localStorage.removeItem(STORAGE_TOKEN);
    closeModal('modal-settings');
    toast('Settings saved. Reconnecting…', 'success', 1500);
    // Reset connections so new token takes effect.
    wsRunsCloseRequested = true;
    if (wsRuns) try { wsRuns.close(); } catch { /* noop */ }
    closeTraceWS();
    setTimeout(() => {
      wsRunsCloseRequested = false;
      openRunsWS();
      if (state.selectedRunId) openTraceWS(state.selectedRunId);
      loadRuns();
      loadTopology();
    }, 200);
  });
  document.querySelectorAll('[data-modal-close]').forEach((b) => {
    b.addEventListener('click', () => {
      const m = b.closest('.modal');
      if (m) closeModal(m.id);
    });
  });
  document.querySelectorAll('.modal').forEach((m) => {
    m.addEventListener('click', (e) => {
      if (e.target === m) closeModal(m.id);
    });
  });

  document.getElementById('drawer-close').addEventListener('click', closeDrawer);
  document.getElementById('drawer-backdrop').addEventListener('click', closeDrawer);

  document.getElementById('status-filter').addEventListener('change', (e) => {
    state.filterStatus = e.target.value;
    setHash(state.selectedRunId);
    renderRunList();
  });
  document.getElementById('search-input').addEventListener('input', (e) => {
    state.searchQuery = e.target.value;
    renderRunList();
  });
  document.getElementById('timeline-search').addEventListener('input', (e) => {
    state.timelineFilter = e.target.value;
    applyTimelineFilter();
  });

  document.querySelectorAll('.seg-btn').forEach((b) => {
    b.addEventListener('click', () => {
      if (b.disabled) return;
      setGraphMode(b.dataset.mode);
    });
  });

  document.getElementById('zoom-in').addEventListener('click', () => graph?.zoomIn());
  document.getElementById('zoom-out').addEventListener('click', () => graph?.zoomOut());
  document.getElementById('zoom-fit').addEventListener('click', () => graph?.fit());
  const scopeSel = document.getElementById('topology-scope');
  const scopeN = document.getElementById('topology-scope-n');
  if (scopeSel) {
    scopeSel.value = state.topologyScope;
    if (scopeN) {
      scopeN.value = String(state.topologyScopeN);
      scopeN.classList.toggle('hidden', state.topologyScope !== 'lastn');
    }
    scopeSel.addEventListener('change', (e) => {
      state.topologyScope = e.target.value;
      if (scopeN) scopeN.classList.toggle('hidden', state.topologyScope !== 'lastn');
      loadTopology();   // re-fetch topology + tools at the new scope
    });
  }
  if (scopeN) {
    scopeN.addEventListener('change', (e) => {
      const n = parseInt(e.target.value, 10);
      state.topologyScopeN = Number.isFinite(n) && n > 0 ? n : 5;
      e.target.value = String(state.topologyScopeN);
      if (state.topologyScope === 'lastn') loadTopology();
    });
  }
  document.getElementById('layout-toggle').addEventListener('click', () => {
    graph?.toggleLayoutDir();
    state.layoutDir = state.layoutDir === 'LR' ? 'TD' : 'LR';
  });

  document.getElementById('replay-btn').addEventListener('click', startReplay);
  document.querySelectorAll('.replay-mode-btn').forEach((b) => {
    b.addEventListener('click', () => setReplayMode(b.dataset.replayMode));
  });
  document.getElementById('replay-prev').addEventListener('click', manualPrev);
  document.getElementById('replay-next').addEventListener('click', manualNext);

  // Pane collapse / expand. State persists in localStorage so the user's
  // preferred layout is remembered across reloads.
  document.getElementById('collapse-runs').addEventListener('click', () => togglePane('runs'));
  document.getElementById('expand-runs').addEventListener('click', () => togglePane('runs'));
  document.getElementById('collapse-timeline').addEventListener('click', () => togglePane('timeline'));
  document.getElementById('expand-timeline').addEventListener('click', () => togglePane('timeline'));

  document.getElementById('newest-pill').addEventListener('click', () => {
    state.autoScrollTimeline = true;
    hideNewestPill();
    const tl = document.getElementById('timeline');
    tl.scrollTop = tl.scrollHeight;
  });

  document.getElementById('timeline').addEventListener('scroll', () => {
    const tl = document.getElementById('timeline');
    const atBottom = tl.scrollHeight - tl.scrollTop - tl.clientHeight < 30;
    state.autoScrollTimeline = atBottom;
    if (atBottom) hideNewestPill();
  });

  document.getElementById('copy-snippet').addEventListener('click', async () => {
    const text = document.getElementById('empty-snippet').textContent || '';
    try {
      await navigator.clipboard.writeText(text);
      toast('Snippet copied', 'success', 1500);
    } catch {
      toast('Copy failed — select and copy manually', 'warning', 2500);
    }
  });
  document.getElementById('reload-empty').addEventListener('click', () => {
    loadRuns();
    loadTopology();
  });

  // Hash routing: load run from #run=…
  window.addEventListener('hashchange', () => {
    const params = readHash();
    if (params.status) {
      state.filterStatus = params.status;
      document.getElementById('status-filter').value = params.status;
    }
    if (params.run && params.run !== state.selectedRunId) {
      selectRun(params.run);
    }
  });
}

function wsUrl(path) {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const tokenQp = state.authToken ? `?token=${encodeURIComponent(state.authToken)}` : '';
  return `${proto}//${location.host}${path}${tokenQp}`;
}

/* ============================================================
 * Bootstrap
 * ============================================================ */

async function main() {
  applyTheme(state.theme);

  // Construct GraphView after DOM ready.
  graph = new GraphView(
    document.getElementById('graph-container'),
    {
      onNodeClick: openNodeDrawer,
      onEdgeClick: openEdgeDrawer,
      onPlayDone: () => {
        hideReplayStepBadge();
        toast('Replay complete', 'success', 1500);
      },
      onPlayStep: (s) => {
        showReplayStepBadge(
          s.index + 1,
          s.total,
          `${s.sourceLabel} → ${s.targetLabel}`,
        );
        // Highlight the corresponding event card in the timeline so the user
        // can follow the run on the right side simultaneously with the graph.
        if (s.eventId) highlightTimelineEvent(s.eventId);
      },
    },
  );

  wireUI();
  wireKeyboard();
  wireToolsPanel();
  applyPaneState();

  // Read initial hash before first load.
  const initial = readHash();
  if (initial.status) {
    state.filterStatus = initial.status;
    document.getElementById('status-filter').value = initial.status;
  }

  await Promise.all([loadRuns(), loadTopology(), pollHealth()]);

  // Auto-select run from hash, if any.
  if (initial.run && state.runsById.has(initial.run)) {
    selectRun(initial.run);
  }

  openRunsWS();

  // Background pollers.
  setTimeout(pollStats, STATS_POLL_MS);
  setInterval(loadRuns, RUNS_POLL_MS);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', main);
} else {
  main();
}
