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
import { renderWaterfall } from './waterfall.js';

/* ============================================================
 * Constants & state
 * ============================================================ */

const STORAGE_THEME = 'tracesage.theme';
const STORAGE_TOKEN = 'tracesage.auth_token';
const STORAGE_PANES = 'tracesage.panes';
const STORAGE_RIGHT_TAB = 'tracesage.right_tab';
const STORAGE_PANE_WIDTHS = 'tracesage.pane_widths';
const TIMELINE_MAX = 500;
const STATS_POLL_MS = 5000;
const RUNS_POLL_MS = 30000;
const THREAD_TURNS = 30;   // turns loaded for "whole chat"; long chats show the latest
const WS_BACKOFF = [1000, 2000, 4000, 8000, 16000, 30000];

// Short, portable uppercase text tags. Avoids mojibake / replacement glyphs
// and renders identically across all platforms and fonts.
const EVENT_ICONS = {
  chain_start: 'CHAIN', chain_end: 'CHAIN', chain_error: 'ERR', chain_interrupt: 'PAUSE',
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
  runsSort: 'recent',
  runsTotal: 0,
  threadsSort: 'recent',
  minTurns: 1,
  searchQuery: '',
  source: 'all',
  listMode: 'runs',            // 'runs' | 'threads'
  selectedThreadId: null,
  rightTab: localStorage.getItem(STORAGE_RIGHT_TAB) === 'tools' ? 'tools' : 'timeline',
  runsGraphMode: null,         // graph tab to restore when switching back to Runs
  searchHits: [],
  searchTimer: null,
  selectedRunId: null,
  drawerRunId: null,           // run the open detail drawer belongs to
  topology: { nodes: [], edges: [] },
  journey: [],                 // StoredEvent[] for selected run
  newEventCount: 0,
  autoScrollTimeline: true,
  graphMode: 'topology',       // 'topology' | 'trace'
  topologyScope: 'run',        // 'run' (selected/latest) | 'lastn' | 'all'
  topologyScopeN: 5,           // N for the 'lastn' scope
  layoutDir: 'LR',
  evRateWindow: [],            // sliding window of timestamps for ev/s
  graphRenderTimer: null,
  pendingTopologyUpdate: false,
  serverVersion: '?',
  // event_id set so we never duplicate when both REST catchup and WS catchup land.
  knownEventIds: new Set(),
  // Replay / playback
  playback: 'idle',            // 'idle' | 'playing' | 'paused'
  replayGen: 0,                // generation guard so a restarted run's finally can't clobber
  replaySteps: [],             // [{source, target, eventId}, ...]
  autoReplayInProgress: false, // true while graph.playRun is alive (playing OR paused)
  // Timeline: which invocation rows are expanded (by run_id) + a cache of fetched
  // payload HTML (by event_id) so re-renders don't re-fetch/flicker.
  expandedInvocations: new Set(),
  payloadCache: new Map(),
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
let wsRunsReconnectTimer = null;
let wsTraceReconnectTimer = null;
// Bumped per request so a slow response for an older selection/scope is ignored.
let selectRunGen = 0;
let topologyGen = 0;

// Liveness: the server pings /ws/runs every ~15s. If we hear nothing (ping or
// real traffic) for this long, treat the link as dead — covers cases where the
// socket never fires `onclose` (half-open connection, hung/suspended server,
// dropped network) so the header doesn't get stuck on "connected".
const WS_RUNS_LIVENESS_MS = 35000;
let wsRunsLastMsgAt = 0;
let wsRunsLivenessTimer = null;

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

function formatCost(value) {
  if (value == null) return '';
  const n = Number(value);
  if (!Number.isFinite(n)) return '';
  return n < 0.01 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`;
}

/** Source filter as query params: an app tag, or "no source tag" for your own agents. */
function applySourceParams(params) {
  if (state.source === 'yours') params.set('no_source', 'true');
  else if (state.source && state.source !== 'all') params.set('tag', `source:${state.source}`);
}

function runsQuery() {
  const params = new URLSearchParams({ limit: '50' });
  if (state.filterStatus && state.filterStatus !== 'all') params.set('status', state.filterStatus);
  applySourceParams(params);
  if ((state.searchQuery || '').trim().length >= 2) params.set('q', state.searchQuery.trim());
  if (state.runsSort !== 'recent') params.set('sort', state.runsSort);
  return params.toString();
}

const APP_SOURCES = ['claude-code', 'claude-desktop', 'cursor', 'codex'];
const SOURCE_LABELS = {
  'claude-code': 'Claude Code', 'claude-desktop': 'Claude Desktop', cursor: 'Cursor', codex: 'Codex',
};

function setSource(source) {
  state.source = source || 'all';
  const select = document.getElementById('source-filter');
  select.value = state.source;
  select.dataset.source = state.source;
}

/** Counts per source for the open tab (runs or threads); apps with none are left
 *  out of the menu. */
async function loadSourceCounts() {
  const mode = state.listMode;
  const endpoint = mode === 'threads' ? '/threads' : '/runs';
  const count = async (extra) => {
    const params = new URLSearchParams({ limit: '1', ...extra });
    return (await apiGet(`${endpoint}?${params}`)).total || 0;
  };
  try {
    const [all, yours, ...apps] = await Promise.all([
      count({}),
      count({ no_source: 'true' }),
      ...APP_SOURCES.map((src) => count({ tag: `source:${src}` })),
    ]);
    if (state.listMode !== mode) return;
    const counts = { all, yours };
    APP_SOURCES.forEach((src, i) => { counts[src] = apps[i]; });
    document.querySelectorAll('#source-filter option').forEach((opt) => {
      const n = counts[opt.value] ?? 0;
      const label = opt.value === 'all' ? 'All' : opt.value === 'yours' ? 'Your agents' : SOURCE_LABELS[opt.value];
      opt.textContent = `${label} · ${n.toLocaleString()}`;
      opt.hidden = n === 0 && opt.value !== 'all' && opt.value !== state.source;
    });
  } catch {
    // Counts are decoration; the list itself reports load errors.
  }
}

function sourceOf(tags) {
  const tag = (tags || []).find((t) => String(t).startsWith('source:'));
  return tag ? String(tag).slice('source:'.length) : null;
}

async function loadThreads() {
  const params = new URLSearchParams({ limit: '200' });
  applySourceParams(params);
  if ((state.searchQuery || '').trim()) params.set('q', state.searchQuery.trim());
  if (state.threadsSort !== 'recent') params.set('sort', state.threadsSort);
  if (state.minTurns > 1) params.set('min_turns', String(state.minTurns));
  const container = document.getElementById('run-list');
  const gen = ++listLoadGen;
  try {
    const data = await apiGet(`/threads?${params}`);
    if (gen !== listLoadGen || state.listMode !== 'threads') return;
    const threads = data.threads || [];
    container.innerHTML = threads.map((t) => {
      const source = sourceOf(t.tags);
      const label = (t.label || t.thread_id).replace(/^[\w.-]+:\s*/, '');
      return `
        <div class="run-row thread-row${t.thread_id === state.selectedThreadId ? ' active' : ''}"
             data-thread="${escapeHtml(t.thread_id)}" data-last-run="${escapeHtml(t.last_run_id || '')}"
             role="listitem" tabindex="0" title="${escapeHtml(t.thread_id)}">
          <div class="run-row-top"><span class="thread-label">${escapeHtml(label)}</span></div>
          <div class="run-row-bottom">
            ${source ? `<span class="source-tag" data-source="${escapeHtml(source)}"><span class="chip-dot"></span>${escapeHtml(SOURCE_LABELS[source] || source)}</span>` : ''}
            <span class="status-pill ${escapeHtml(t.last_status)}"><span class="status-badge ${escapeHtml(t.last_status)}" aria-hidden="true"></span>${escapeHtml(t.last_status)}</span>
            <span class="meta-step">${t.run_count} turn${t.run_count === 1 ? '' : 's'}</span>
            <span>·</span>
            <span title="${escapeHtml(t.last_at || '')}">${formatRelTime(t.last_at)}</span>
          </div>
        </div>`;
    }).join('') || '<div class="empty-runs">No threads match the current filter.</div>';
    document.getElementById('run-count').textContent =
      `${threads.length}${data.total > threads.length ? ` of ${data.total}` : ''} threads`;
    container.querySelectorAll('[data-thread]').forEach((row) => {
      const open = () => openThread(row.dataset.thread);
      row.addEventListener('click', open);
      row.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
      });
    });
  } catch (e) {
    if (gen !== listLoadGen) return;
    container.innerHTML =
      `<div class="empty-runs">Failed to load threads.<br><small>${escapeHtml(e.message)}</small></div>`;
  }
}

/** Threads mode shows whole chats: no per-turn trace, waterfall or timeline. */
function threadView() {
  return state.listMode === 'threads';
}

/** Cover the centre pane while the next run/thread loads, or prompt for a pick,
 *  so the previous entity is never left on screen under the new selection. */
function setCenterOverlay(kind, text) {
  const overlay = document.getElementById('center-overlay');
  if (!overlay) return;
  overlay.classList.toggle('hidden', kind === 'none');
  overlay.classList.toggle('prompt', kind === 'prompt');
  document.getElementById('center-overlay-text').textContent =
    text || (kind === 'loading' ? 'Loading…' : '');
}

function listSkeleton() {
  const row = '<div class="skeleton-row" style="height:48px;"></div>';
  return `<div class="skeleton-list" aria-hidden="true">${row.repeat(6)}</div>`;
}

function setRightTab(tab) {
  state.rightTab = tab;
  try { localStorage.setItem(STORAGE_RIGHT_TAB, tab); } catch { /* full storage */ }
  applyRightPane();
}

/** Runs: Timeline | Tools tabs. Threads: Tools only (a timeline is per turn). */
function applyRightPane() {
  const tab = threadView() ? 'tools' : state.rightTab;
  document.querySelectorAll('.list-toggle-btn[data-right]').forEach((b) => {
    const on = b.dataset.right === tab;
    b.classList.toggle('active', on);
    b.setAttribute('aria-selected', String(on));
  });
  document.getElementById('timeline-view').classList.toggle('hidden', tab !== 'timeline');
  document.getElementById('tools-view').classList.toggle('hidden', tab !== 'tools');
  document.getElementById('timeline-meta').classList.toggle('hidden', tab !== 'timeline');
  document.getElementById('expand-timeline-label').textContent = tab === 'tools' ? 'Tools' : 'Timeline';
  document.getElementById('tools-view-scope').textContent = threadView()
    ? 'Tools used across this thread, grouped by MCP server.'
    : state.selectedRunId
      ? 'Tools used in this run, grouped by MCP server.'
      : 'Tools used in the latest run, grouped by MCP server.';
}

/** A run on the Runs screen opens beside its timeline, pane expanded. */
function showRunTimeline() {
  if (threadView()) return;
  setRightTab('timeline');
  const pane = document.querySelector('.pane-timeline');
  if (!pane || !pane.classList.contains('collapsed')) return;
  pane.classList.remove('collapsed');
  const saved = getPaneState();
  saved.timelineCollapsed = false;
  savePaneState(saved);
}

async function setListMode(mode, { restore = true } = {}) {
  if (mode === state.listMode) return;
  if (mode === 'threads') state.runsGraphMode = state.graphMode;
  state.listMode = mode;
  document.body.classList.toggle('threads-mode', mode === 'threads');
  document.querySelectorAll('.list-toggle-btn[data-list]').forEach((b) => {
    const on = b.dataset.list === mode;
    b.classList.toggle('active', on);
    b.setAttribute('aria-selected', String(on));
  });
  const list = document.getElementById('run-list');
  list.setAttribute('aria-label', mode === 'threads' ? 'Threads' : 'Runs');
  const search = document.getElementById('search-input');
  search.placeholder = mode === 'threads' ? 'Search threads…' : 'Search runs…';
  search.setAttribute('aria-label', mode === 'threads' ? 'Search threads' : 'Search runs');
  list.scrollTop = 0;
  list.innerHTML = listSkeleton();
  document.getElementById('run-count').textContent = 'Loading…';
  applyPaneState();
  applyRightPane();
  syncSelectionHints();
  if (isDrawerOpen()) closeDrawer();
  if (graph?.isReady() && graph.cancelReplay) graph.cancelReplay();
  state.autoReplayInProgress = false;
  state.playback = 'idle';
  updatePlaybackUI();
  hideReplayStepBadge();
  const listed = loadRuns();
  loadSourceCounts();
  if (mode === 'threads') {
    if (state.selectedThreadId) await showThread();
    else showThreadPrompt();
  } else if (restore) {
    await showRunViews();
  }
  await listed;
}

function showThreadPrompt() {
  setCenterOverlay('prompt', 'Pick a thread to read the whole conversation.');
  setGraphModeAvailable(false);
  setGraphMode('topology');
  loadTopology();
}

/** Back in Runs mode, put the selected run (or the latest-run topology) back. */
async function showRunViews() {
  setCenterOverlay('loading');
  const hasRun = !!state.selectedRunId;
  setGraphModeAvailable(hasRun);
  const back = state.runsGraphMode;
  const mode = hasRun && back && back !== 'topology' ? back : hasRun ? 'trace' : 'topology';
  await loadTopology();
  if (threadView()) return;
  setGraphMode(mode);
  if (hasRun) showRunTimeline();
  if (mode !== 'conversation') setCenterOverlay('none');
}

async function openThread(threadId) {
  if (state.selectedThreadId === threadId) {
    state.selectedThreadId = null;
    document.querySelectorAll('.thread-row.active').forEach((r) => r.classList.remove('active'));
    showThreadPrompt();
    return;
  }
  state.selectedThreadId = threadId;
  document.querySelectorAll('.thread-row').forEach((r) => {
    r.classList.toggle('active', r.dataset.thread === threadId);
  });
  await showThread();
}

async function showThread() {
  setCenterOverlay('loading');
  setGraphModeAvailable(true);
  const threadId = state.selectedThreadId;
  await loadTopology();
  if (!threadView() || state.selectedThreadId !== threadId) return;
  setGraphMode('conversation');
}

/** "Open run" on a turn of the whole chat: show that turn as a run. */
async function openRunFromThread(runId) {
  setCenterOverlay('loading');
  if (!state.runsById.has(runId)) {
    try {
      state.runsById.set(runId, await apiGet(`/runs/${encodeURIComponent(runId)}`));
    } catch (e) {
      toast(`Could not open run: ${e.message}`, 'error');
      setCenterOverlay('none');
      return;
    }
  }
  state.runsGraphMode = 'trace';
  const same = state.selectedRunId === runId;
  await setListMode('runs', { restore: false });
  if (same) await showRunViews();
  else await selectRun(runId, { mode: 'trace' });
}

// Each list load bumps this; a response that is no longer the newest is dropped,
// so quick filter changes or a tab switch can't show an older result last.
let listLoadGen = 0;

async function loadRuns() {
  if (state.listMode === 'threads') return loadThreads();
  const gen = ++listLoadGen;
  try {
    const data = await apiGet(`/runs?${runsQuery()}`);
    if (gen !== listLoadGen) return;
    // The open run stays known even when the current filter hides it, so its
    // conversation and thread still resolve.
    const open = state.runsById.get(state.selectedRunId);
    state.runs = data.runs;
    state.runsTotal = data.total;
    state.runsById = new Map(data.runs.map((r) => [r.run_id, r]));
    if (open && !state.runsById.has(open.run_id)) state.runsById.set(open.run_id, open);
    renderRunList();
    renderEmptyState();
  } catch (e) {
    if (gen !== listLoadGen) return;
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
  if (state.listMode === 'threads') return;
  const container = document.getElementById('run-list');
  const filtered = filteredRuns();
  // A filter that hides the drawer's run would leave its details floating next
  // to a list of other runs.
  if (state.drawerRunId && isDrawerOpen() && !filtered.some((r) => r.run_id === state.drawerRunId)) {
    closeDrawer();
  }

  if (filtered.length === 0) {
    container.innerHTML = `<div class="empty-runs">No runs match the current filter.</div>`;
    document.getElementById('run-count').textContent = '0 runs';
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
    state.runsTotal > filtered.length
      ? `${filtered.length} of ${state.runsTotal} runs`
      : `${filtered.length} run${filtered.length === 1 ? '' : 's'}`;
}

function buildRunRow(run) {
  const row = document.createElement('div');
  row.className = 'run-row';
  if (state.selectedRunId === run.run_id) row.classList.add('active');
  row.dataset.runId = run.run_id;
  row.setAttribute('role', 'listitem');
  row.setAttribute('tabindex', '0');
  row.setAttribute('aria-label', `Run ${run.run_id} — ${run.status}`);
  row.title = run.run_id;   // hovering anywhere on the row shows the full run id

  const tags = (run.tags || []).slice(0, 3)
    .map((t) => `<span class="tag-chip">${escapeHtml(t)}</span>`)
    .join('');
  const thread = run.thread_id
    ? `<span class="tag-chip thread-chip" data-thread-id="${escapeHtml(run.thread_id)}" title="Thread ${escapeHtml(run.thread_id)}: click to show this thread's runs">thread:${escapeHtml(run.thread_id.slice(0, 12))}</span>`
    : '';

  row.innerHTML = `
    <div class="run-row-top">
      <span class="run-id" title="${escapeHtml(run.run_id)}">${escapeHtml(run.run_id.slice(0, 12))}…</span>
      <span class="run-row-actions">
        <button class="run-action export" data-run-action="export" data-run-id="${escapeHtml(run.run_id)}" type="button" title="Export run as JSONL" aria-label="Export run ${escapeHtml(run.run_id)} as JSONL">Export</button>
        <button class="run-action delete" data-run-action="delete" data-run-id="${escapeHtml(run.run_id)}" type="button" title="Delete run" aria-label="Delete run ${escapeHtml(run.run_id)}">Delete</button>
      </span>
    </div>
    <div class="run-row-bottom">
      <span class="status-pill ${escapeHtml(run.status)}"><span class="status-badge ${escapeHtml(run.status)}" aria-hidden="true"></span>${escapeHtml(run.status)}</span>
      <span class="meta-step">${run.total_steps} step${run.total_steps === 1 ? '' : 's'}</span>
      <span class="meta-cost" title="tokens and cost">${(run.total_tokens_input || 0) + (run.total_tokens_output || 0)} tok${formatCost(run.total_cost_usd) ? ' · ' + formatCost(run.total_cost_usd) : ''}</span>
      <span>·</span>
      <span title="${escapeHtml(run.started_at)}">${formatRelTime(run.started_at)}</span>
      ${thread}
      ${tags}
      ${run._snippet ? `<div class="run-snippet" data-hit-event="${escapeHtml(run._hitEventId || '')}">${escapeHtml(run._snippet)}</div>` : ''}
    </div>
  `;
  const toggle = () => (state.selectedRunId === run.run_id ? deselectRun() : selectRun(run.run_id));
  row.addEventListener('click', toggle);
  const snippet = row.querySelector('.run-snippet');
  if (snippet) {
    snippet.addEventListener('click', async (e) => {
      e.stopPropagation();
      const eventId = run._hitEventId;
      if (state.selectedRunId !== run.run_id) await selectRun(run.run_id);
      const ev = (state.journey || []).find((step) => step.event_id === eventId);
      if (ev) openStepDrawer(ev);
    });
  }
  const threadChip = row.querySelector('.thread-chip');
  if (threadChip) {
    threadChip.addEventListener('click', (e) => {
      e.stopPropagation();
      const input = document.getElementById('search-input');
      input.value = run.thread_id;
      state.searchQuery = run.thread_id;
      renderRunList();
    });
  }
  row.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      toggle();
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
  const server = q.trim().length >= 2;
  return state.runs.filter((r) => {
    if (state.filterStatus !== 'all' && r.status !== state.filterStatus) return false;
    if (state.source === 'yours' && (r.tags || []).some((t) => String(t).startsWith('source:'))) return false;
    if (!server && q) {
      const hay = `${r.run_id} ${r.thread_id || ''} ${(r.tags || []).join(' ')}`.toLowerCase();
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
    if (state.source === 'yours' ? sourceOf(run.tags) : state.source !== 'all' && sourceOf(run.tags) !== state.source) return;
    if (state.filterStatus !== 'all' && run.status !== state.filterStatus) return;
    // Other orders depend on totals that are still changing; the poller places them.
    if (state.runsSort !== 'recent') return;
    // Imported chats arrive out of order; keep the list sorted by trace start.
    const at = state.runs.findIndex((r) => String(r.started_at) < String(run.started_at));
    if (at === -1 && state.runs.length >= 50) return;
    state.runs.splice(at === -1 ? state.runs.length : at, 0, run);
    state.runsTotal += 1;
    state.runsById.set(run.run_id, run);
  }
  renderRunList();
  renderEmptyState();
}

/* ============================================================
 * Run selection — load journey + open trace WS
 * ============================================================ */

function deselectRun() {
  if (!state.selectedRunId) return;
  selectRunGen += 1;
  state.selectedRunId = null;
  state.journey = [];
  state.knownEventIds = new Set();
  if (isDrawerOpen()) closeDrawer();
  if (graph?.isReady() && graph.cancelReplay) graph.cancelReplay();
  closeTraceWS();
  setHash(null);
  const meta = document.getElementById('timeline-meta');
  meta.textContent = 'no run selected';
  meta.title = '';
  renderTimeline();
  document.querySelectorAll('.run-row.active').forEach((r) => r.classList.remove('active'));
  setCenterOverlay('none');
  setGraphMode('topology');
  setGraphModeAvailable(false);
  applyRightPane();
  if (state.topologyScope === 'run') loadTopology();
}

async function selectRun(runId, { mode = null } = {}) {
  if (threadView()) await setListMode('runs', { restore: false });
  if (state.selectedRunId === runId) {
    // Re-clicking the already-selected run: the user likely switched to Topology
    // and wants its run-trace back.
    if (state.graphMode !== 'trace') {
      applyRunTraceToGraph();
      setGraphMode('trace');
    }
    showRunTimeline();
    return;
  }
  state.selectedRunId = runId;
  setCenterOverlay('loading');
  // The drawer shows a step/node of the previous run; don't leave it open over
  // the new one.
  if (isDrawerOpen()) closeDrawer();
  state.journey = [];
  state.newEventCount = 0;
  state.expandedInvocations.clear();   // a new run starts with all rows collapsed
  // Open at the FIRST step (top), not the latest. Live tailing re-enables itself
  // when the user scrolls to the bottom (see the timeline scroll listener).
  state.autoScrollTimeline = false;
  state.knownEventIds = new Set();
  setHash(runId);
  renderRunList();
  showRunTimeline();
  setGraphModeAvailable(true);
  document.getElementById('replay-controls').classList.remove('hidden');

  const tlMeta = document.getElementById('timeline-meta');
  // Full run id in brackets right after the "Timeline" heading (also on hover).
  tlMeta.textContent = `(${runId})`;
  tlMeta.title = runId;

  showTimelineSkeleton();
  const gen = ++selectRunGen;
  let steps = [];
  try {
    const data = await apiGet(`/runs/${encodeURIComponent(runId)}/journey`);
    steps = data.steps || [];
    if (data.truncated && gen === selectRunGen) {
      toast(`Large run: showing the latest ${steps.length} steps. Export for the full trace.`, 'warning', 6000);
    }
  } catch (e) {
    if (gen === selectRunGen) toast(`Could not load journey: ${e.message}`, 'error');
  }
  // A newer selection started while this fetch was in flight; its own call owns
  // the timeline/graph now, so applying this (older) journey would mislabel it.
  if (gen !== selectRunGen) return;
  state.journey = steps;
  state.journey.forEach((s) => state.knownEventIds.add(s.event_id));
  renderTimeline();
  loadNotes(runId);
  // Point the timeline at the first entry on open.
  const tlEl = document.getElementById('timeline');
  if (tlEl) tlEl.scrollTop = 0;
  applyRunTraceToGraph();
  // When the topology is scoped to "this run", refresh it for the new selection.
  if (state.topologyScope === 'run') await loadTopology();
  if (gen !== selectRunGen) return;

  // Reset replay/playback for the new run: cancel any in-flight replay, rebuild
  // the step list against the new journey, and reset the controls to idle.
  if (graph?.isReady() && graph.cancelReplay) graph.cancelReplay();
  state.autoReplayInProgress = false;
  state.playback = 'idle';
  state.replaySteps = buildReplaySteps();
  if (graph?.isReady()) graph.clearStepHighlight();
  clearTimelineEventHighlight();
  updatePlaybackUI();

  openTraceWS(runId);
  const next = mode || 'trace';
  setGraphMode(next);
  if (next !== 'conversation') setCenterOverlay('none');
}

function setHash(runId) {
  const params = new URLSearchParams();
  if (runId) params.set('run', runId);
  if (state.filterStatus !== 'all') params.set('status', state.filterStatus);
  if (state.source && state.source !== 'all') params.set('source', state.source);
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

  // One card per raw event (detailed view): truncate to the last TIMELINE_MAX,
  // with a "load earlier" stub if we cut.
  const start = Math.max(0, events.length - TIMELINE_MAX);
  const visible = events.slice(start);
  const frag = document.createDocumentFragment();
  if (start > 0) {
    const btn = document.createElement('button');
    btn.className = 'load-earlier';
    btn.textContent = `Load earlier (${start} hidden) — render all`;
    btn.addEventListener('click', () => {
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

/** One detailed timeline card per event (icon, timestamp, name, summary, badges).
 *  Click opens the full step drawer (request + response payloads). */
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
  card.dataset.search = `${ev.event_type} ${name} ${summary} ${ev.error_message || ''}`.toLowerCase();
  const dur = ev.duration_ms != null ? `<span class="badge duration">${formatMs(ev.duration_ms)}</span>` : '';
  const tokens = (ev.token_input != null || ev.token_output != null)
    ? `<span class="badge tokens">↑${ev.token_input || 0} ↓${ev.token_output || 0}${formatCost(ev.cost_usd) ? ' ' + formatCost(ev.cost_usd) : ''}</span>` : '';
  if (state.searchHits.some((h) => h.event_id === ev.event_id)) card.classList.add('search-hit');
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
      seekReplayToEvent(ev.event_id);
      openStepDrawer(ev);
    });
  }
  card.addEventListener('click', () => {
    // While replaying, clicking a timeline step stops the run there and jumps the
    // graph trace to that step — no matter where playback currently is.
    seekReplayToEvent(ev.event_id);
    openStepDrawer(ev);
  });
  return card;
}

/** Group an ordered event list into invocations keyed by run_id (preserving
 *  first-seen order), merging each step's start (request) and end (response).
 *  Used by the node drawer's invocations dropdown. */
function groupInvocations(events) {
  const order = [];
  const byRun = new Map();
  for (const ev of events) {
    const rid = ev.run_id || ev.event_id;
    let inv = byRun.get(rid);
    if (!inv) { inv = { runId: rid, events: [], eventIds: [] }; byRun.set(rid, inv); order.push(inv); }
    inv.events.push(ev);
    inv.eventIds.push(ev.event_id);
  }
  for (const inv of order) finalizeInvocation(inv);
  return order;
}

function finalizeInvocation(inv) {
  const evs = inv.events;
  const startEv = evs.find((e) => stepPhase(e.event_type) === 'request') || evs[0];
  const endEv = [...evs].reverse().find((e) => stepPhase(e.event_type) === 'response');
  const errEv = evs.find((e) => /_error$/.test(e.event_type) || e.error_message);
  const ref = startEv || evs[0];
  inv.name = ref.tool_name || ref.agent_name || ref.event_type;
  inv.eventType = ref.event_type;
  inv.icon = EVENT_ICONS[ref.event_type] || '•';
  inv.source = (evs.find((e) => e.mcp_server) || {}).mcp_server || null;
  inv.timestamp = (startEv || ref).timestamp;
  inv.durationMs = (endEv && endEv.duration_ms != null)
    ? endEv.duration_ms
    : ((evs.find((e) => e.duration_ms != null) || {}).duration_ms ?? null);
  const tokEv = (endEv && (endEv.token_input != null || endEv.token_output != null))
    ? endEv : evs.find((e) => e.token_input != null || e.token_output != null);
  inv.tokenIn = tokEv ? (tokEv.token_input || 0) : null;
  inv.tokenOut = tokEv ? (tokEv.token_output || 0) : null;
  inv.error = errEv ? (errEv.error_message || 'error') : null;
  inv.status = errEv ? 'error' : (endEv ? 'done' : 'running');
  inv.summary = (endEv && endEv.summary) || (startEv && startEv.summary) || ref.summary || '';
  inv.startEv = (stepPhase(startEv?.event_type) === 'request' && startEv?.blob_path)
    ? startEv : (evs.find((e) => stepPhase(e.event_type) === 'request' && e.blob_path) || null);
  inv.endEv = (endEv && endEv.blob_path)
    ? endEv : (evs.find((e) => stepPhase(e.event_type) === 'response' && e.blob_path) || null);
  inv.search = `${inv.eventType} ${inv.name} ${inv.summary} ${inv.error || ''}`.toLowerCase();
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

function appendStepCard(ev) {
  if (state.knownEventIds.has(ev.event_id)) return;
  state.knownEventIds.add(ev.event_id);
  state.journey.push(ev);

  const tl = document.getElementById('timeline');
  // If we're showing the empty/skeleton placeholder, do a full re-render.
  if (!tl.querySelector('.step-card') && !tl.querySelector('.load-earlier')) {
    renderTimeline();
  } else {
    // At the cap, drop the oldest visible card, then append the new one.
    const cards = tl.querySelectorAll('.step-card');
    if (cards.length >= TIMELINE_MAX) cards[0].remove();
    tl.appendChild(buildStepCard(ev));
    if (state.timelineFilter) applyTimelineFilter();
    if (state.autoScrollTimeline) {
      tl.scrollTop = tl.scrollHeight;
    } else {
      state.newEventCount += 1;
      showNewestPill();
    }
  }

  // Pulse graph node + flash edge from previous event's node.
  pulseGraphForEvent(ev);

  // Keep the replay step list current as live events stream in, so step-back/
  // forward reach later-arriving events — but only while idle (don't mutate the
  // edge list under an in-flight or paused replay).
  if (state.playback === 'idle') {
    state.replaySteps = buildReplaySteps();
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
  // same run_id. Find both halves by run_id.
  const siblings = (state.journey || []).filter((e) => e.run_id === ev.run_id);
  const pickWithBlob = (phase) =>
    siblings.find((e) => stepPhase(e.event_type) === phase && e.blob_path)
    || (stepPhase(ev.event_type) === phase && ev.blob_path ? ev : null);
  const reqEv = pickWithBlob('request');
  // A *_start card is a REQUEST — show only the request, never the response (the
  // response belongs to the *_end half, and conflating them misleads). A *_end /
  // *_error card shows request (context) + response: the full completed call,
  // which is also what the merged invocation cards open.
  const resEv = stepPhase(ev.event_type) === 'request' ? null : pickWithBlob('response');

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
      <h4>Summary <span style="font-weight:400;font-size:11px;color:var(--text-dim);">— short one-line preview; full payload below</span></h4>
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

/** Render one LOGICAL invocation — a call's start (request) + end (response)
 *  merged into a single card. Tokens come from the end event only (set by
 *  finalizeInvocation), so a call is shown and counted exactly once. Clicking
 *  opens the step drawer, which pairs the same run_id to show full request +
 *  response. */
function renderInvocationCard(inv) {
  const dur = inv.durationMs != null ? `<span class="badge">${formatMs(inv.durationMs)}</span>` : '';
  const tok = (inv.tokenIn != null || inv.tokenOut != null)
    ? `<span class="badge">↑${inv.tokenIn ?? 0} ↓${inv.tokenOut ?? 0}</span>` : '';
  const errCls = inv.status === 'error' ? ' error' : '';
  const running = inv.status === 'running' ? '<span class="badge">running…</span>' : '';
  // Open the drawer on the end event when present so its token line resolves;
  // openStepDrawer re-pairs by run_id either way.
  const clickId = inv.eventIds[inv.eventIds.length - 1] || inv.eventIds[0];
  return `
    <article class="node-event-card${errCls}" data-event-id="${escapeHtml(clickId)}">
      <header class="node-event-head">
        <span class="node-event-type">${escapeHtml(inv.eventType)}</span>
        <span class="node-event-time">${formatTs(inv.timestamp)}</span>
      </header>
      <div class="node-event-body">${escapeHtml(inv.summary || '(no summary)')}</div>
      <footer class="node-event-foot">
        ${dur}${tok}${running}
        ${inv.error ? `<span class="badge error">${escapeHtml(inv.error).slice(0, 80)}</span>` : ''}
      </footer>
    </article>`;
}

function openNodeDrawer(nodeData) {
  // Inspecting a node mid-replay pauses the run so you can study it; Continue
  // resumes from where it stopped.
  if (state.playback === 'playing') pauseReplay();
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
  // Collapse each call's start (request) + end (response) into ONE logical
  // invocation, keyed by run_id. Without this a single call shows up as two
  // rows (a *_start card and a *_end card) and doubles the count.
  const invocations = groupInvocations(matching);
  const visible = invocations.slice(-12).reverse();

  // Aggregate token usage across this node's invocations. finalizeInvocation
  // takes tokens from the END event only, so summing per-invocation counts each
  // call exactly once — no start/end double counting.
  const tokenInvs = invocations.filter((inv) => inv.tokenIn != null || inv.tokenOut != null);
  const tokenIn = tokenInvs.reduce((s, inv) => s + (inv.tokenIn || 0), 0);
  const tokenOut = tokenInvs.reduce((s, inv) => s + (inv.tokenOut || 0), 0);
  const showTokens = nodeData.type === 'llm' && tokenInvs.length > 0;
  const ctxLabel = state.selectedRunId
    ? `Invocations in selected run (${invocations.length})`
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
        ${showTokens ? `
        <div class="stat">
          <span class="stat-label">Tokens in</span>
          <span class="stat-value">${tokenIn.toLocaleString()}</span>
        </div>
        <div class="stat">
          <span class="stat-label">Tokens out</span>
          <span class="stat-value">${tokenOut.toLocaleString()}</span>
        </div>` : ''}
      </div>
      <div class="hero-foot">Last seen: ${formatRelTime(nodeData.lastSeen)}${sourceFoot}${showTokens ? ` · Total tokens: <strong>${(tokenIn + tokenOut).toLocaleString()}</strong> across ${tokenInvs.length} call${tokenInvs.length === 1 ? '' : 's'}` : ''}</div>
    </section>

    ${renderCallsSection(nodeData, calls)}

    <section class="drawer-section">
      <h4>${nodeData.type === 'mcp' ? 'Used by' : 'Called by'} <span class="count-pill">${calledBy.length}</span></h4>
      ${renderConnectionsList(calledBy)}
    </section>

    <section class="drawer-section">
      <button class="drawer-collapse-head" id="invocations-toggle" type="button" aria-expanded="false">
        <span class="drawer-collapse-chevron" aria-hidden="true">▸</span>
        <h4>${escapeHtml(ctxLabel)} ${state.selectedRunId ? `<span class="count-pill">${invocations.length}</span>` : ''}</h4>
      </button>
      <div class="node-events drawer-collapse-body hidden" id="invocations-body">
        ${visible.length
          ? visible.map(renderInvocationCard).join('')
          : `<div class="dim">No invocations in the loaded journey.${state.selectedRunId ? '' : ' Click a run on the left first.'}</div>`}
      </div>
    </section>
  `;

  // The invocations list is a collapsed dropdown — click the header to expand.
  const invToggle = document.getElementById('invocations-toggle');
  const invBody = document.getElementById('invocations-body');
  if (invToggle && invBody) {
    invToggle.addEventListener('click', () => {
      const open = invBody.classList.toggle('hidden') === false;
      invToggle.setAttribute('aria-expanded', String(open));
      invToggle.querySelector('.drawer-collapse-chevron').textContent = open ? '▾' : '▸';
    });
  }
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
  if (state.playback === 'playing') pauseReplay();
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
  // One entry per logical call (start+end merged), so counts aren't doubled.
  const invocations = groupInvocations(matching);
  const visible = invocations.slice(-10).reverse();

  body.innerHTML = `
    <section class="drawer-section">
      <h4>Edge</h4>
      <div class="kv"><span class="kv-key">source</span><span class="kv-val">${escapeHtml(edgeData.source)}</span></div>
      <div class="kv"><span class="kv-key">target</span><span class="kv-val">${escapeHtml(edgeData.target)}</span></div>
      <div class="kv"><span class="kv-key">traversals (all runs)</span><span class="kv-val">${edgeData.count || 0}</span></div>
      <div class="kv"><span class="kv-key">last seen</span><span class="kv-val">${formatRelTime(edgeData.lastSeen)}</span></div>
    </section>
    <section class="drawer-section">
      <h4>Invocations of target in selected run (${invocations.length})</h4>
      <div class="node-events">
        ${visible.length
          ? visible.map(renderInvocationCard).join('')
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

function isDrawerOpen() {
  return !!document.getElementById('drawer')?.classList.contains('open');
}

function showDrawer() {
  state.drawerRunId = state.selectedRunId;
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
  state.drawerRunId = null;
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
  clearTimeout(wsRunsReconnectTimer);
  wsRunsReconnectTimer = null;
  stopRunsLiveness();
  detachSocket(wsRuns);
  wsRuns = null;
  wsRunsCloseRequested = false;

  const url = wsUrl('/ws/runs');
  setConnState('connecting');

  let ws;
  try {
    ws = new WebSocket(url, wsProtocols());
  } catch (e) {
    setConnState('disconnected');
    scheduleRunsReconnect();
    return;
  }
  wsRuns = ws;

  ws.onopen = () => {
    wsRunsBackoff = 0;
    wsRunsLastMsgAt = Date.now();
    setConnState('connected');
    startRunsLiveness();
  };
  ws.onerror = () => { /* onclose will fire next */ };
  ws.onclose = () => {
    stopRunsLiveness();
    setConnState('disconnected');
    if (!wsRunsCloseRequested) scheduleRunsReconnect();
  };
  ws.onmessage = (m) => {
    wsRunsLastMsgAt = Date.now();   // any frame (incl. heartbeat ping) = alive
    try {
      const msg = JSON.parse(m.data);
      if (msg && msg.msg_type === 'ping') return;   // heartbeat only — nothing to render
      handleRunsWsMessage(msg);
    } catch (e) {
      console.warn('bad ws msg', e);
    }
  };
}

/** Poll the time since the last /ws/runs frame; if the link has gone silent past
 *  the liveness window, force it closed (which flips the header to disconnected
 *  and schedules a reconnect) even though no `onclose` arrived. */
function startRunsLiveness() {
  stopRunsLiveness();
  wsRunsLivenessTimer = setInterval(() => {
    if (wsRunsCloseRequested) return;
    if (Date.now() - wsRunsLastMsgAt <= WS_RUNS_LIVENESS_MS) return;
    stopRunsLiveness();
    // Detach the stale socket so its (possibly never-arriving, possibly late)
    // onclose can't double-schedule a reconnect, then reconnect ourselves.
    detachSocket(wsRuns);
    wsRuns = null;
    setConnState('disconnected');
    if (!wsRunsCloseRequested) scheduleRunsReconnect();
  }, 5000);
}

function stopRunsLiveness() {
  if (wsRunsLivenessTimer) { clearInterval(wsRunsLivenessTimer); wsRunsLivenessTimer = null; }
}

function scheduleRunsReconnect() {
  if (wsRunsReconnectTimer) return;
  const delay = WS_BACKOFF[Math.min(wsRunsBackoff, WS_BACKOFF.length - 1)];
  wsRunsBackoff += 1;
  toast(`Disconnected. Reconnecting in ${Math.round(delay/1000)}s…`, 'warning', 2500);
  wsRunsReconnectTimer = setTimeout(() => {
    wsRunsReconnectTimer = null;
    if (!wsRunsCloseRequested) openRunsWS();
  }, delay);
}

function handleRunsWsMessage(msg) {
  if (!msg || !msg.msg_type) return;
  if (msg.msg_type === 'run_update' && msg.payload?.run) {
    const incoming = msg.payload.run;
    // Capture the prior status as a STRING before upsert — upsertRun does
    // Object.assign(existing, run) (mutates in place), so holding the object
    // reference would see the already-overwritten status. Toast on a
    // running -> completed/failed transition so you don't have to watch the list.
    const prevStatus = state.runsById.get(incoming.run_id)?.status;
    upsertRun(incoming);
    // Turns imported from AI apps (including history backfills) don't toast.
    if (sourceOf(incoming.tags)) return;
    if (prevStatus === 'running' && incoming.status === 'completed') {
      toast('Run completed', 'success', 3000);
    } else if (prevStatus === 'running' && incoming.status === 'failed') {
      const detail = incoming.error_message ? `: ${incoming.error_message.slice(0, 60)}` : '';
      toast(`Run failed${detail}`, 'error', 5000);
    } else if (prevStatus === 'running' && incoming.status === 'interrupted') {
      toast('Run interrupted: waiting for human input', 'warning', 4000);
    }
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
    ws = new WebSocket(url, wsProtocols());
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
  clearTimeout(wsTraceReconnectTimer);
  wsTraceReconnectTimer = null;
  if (wsTrace) {
    wsTraceCloseRequested = true;
    detachSocket(wsTrace);
    wsTrace = null;
  }
}

function scheduleTraceReconnect(runId) {
  clearTimeout(wsTraceReconnectTimer);
  const delay = WS_BACKOFF[Math.min(wsTraceBackoff, WS_BACKOFF.length - 1)];
  wsTraceBackoff += 1;
  wsTraceReconnectTimer = setTimeout(() => {
    wsTraceReconnectTimer = null;
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
  // "This run" follows the selection: a thread covers every turn of that chat,
  // a run covers that one turn. Threads mode with nothing picked shows nothing.
  if (threadView()) return state.selectedThreadId ? `thread:${state.selectedThreadId}` : null;
  return state.selectedRunId ? `run:${state.selectedRunId}` : 'last_n:1';
}

async function loadTopology() {
  const gen = ++topologyGen;
  if (topologyScopeParam() === null) {
    state.topology = { nodes: [], edges: [] };
    if (graph?.isReady()) graph.setTopology(state.topology);
    renderMcpLegend();
    renderToolsPanel(null);
    return;
  }
  try {
    const scope = encodeURIComponent(topologyScopeParam());
    const topo = await apiGet(`/topology?scope=${scope}`);
    if (gen !== topologyGen) return;
    state.topology = topo;
    if (graph?.isReady()) graph.setTopology(state.topology);
    // Event -> node ids resolve against the current topology (leaf nodes are
    // 'chain:', not 'agent:'), so a trace applied before this run's topology
    // arrived must be recomputed now.
    if (state.graphMode === 'trace' && state.journey.length) applyRunTraceToGraph();
    renderMcpLegend();
  } catch (e) {
    toast(`Could not load topology: ${e.message}`, 'error', 3000);
  }
  // Tools-by-source derives from the same events; refresh it alongside topology.
  if (gen === topologyGen) await loadTools();
}

async function loadTools() {
  if (topologyScopeParam() === null) return renderToolsPanel(null);
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

/** Topology's "This run" option covers the whole chat in Threads mode. */
function syncSelectionHints() {
  const option = document.querySelector('#topology-scope option[value="run"]');
  if (option) option.textContent = threadView() ? 'This thread' : 'This run';
}

function setGraphMode(mode) {
  if (threadView() && (mode === 'trace' || mode === 'waterfall')) mode = 'conversation';
  state.graphMode = mode;
  const scopeWrap = document.getElementById('topology-scope-wrap');
  if (scopeWrap) scopeWrap.classList.toggle('hidden', mode !== 'topology');
  const graphEl = document.getElementById('graph-container');
  const waterfall = document.getElementById('waterfall');
  const conversation = document.getElementById('conversation');
  const legend = document.getElementById('graph-legend');
  const special = mode === 'waterfall' || mode === 'conversation';
  if (graphEl) graphEl.classList.toggle('hidden', special);
  if (waterfall) waterfall.classList.toggle('hidden', mode !== 'waterfall');
  if (conversation) conversation.classList.toggle('hidden', mode !== 'conversation');
  if (legend) legend.classList.toggle('hidden', special);
  if (mode === 'waterfall') loadWaterfall();
  if (mode === 'conversation') loadConversation();
  document.querySelectorAll('.seg-btn[data-mode]').forEach((b) => {
    const isActive = b.dataset.mode === mode;
    b.classList.toggle('active', isActive);
    b.setAttribute('aria-selected', isActive ? 'true' : 'false');
  });
  syncSelectionHints();
  if (mode === 'topology') {
    // Stop any in-flight replay so the graph doesn't keep animating after the
    // user has explicitly asked to leave trace view.
    if (state.autoReplayInProgress && graph?.isReady()) {
      graph.cancelReplay();
    }
    state.autoReplayInProgress = false;
    state.playback = 'idle';
    updatePlaybackUI();
    hideReplayStepBadge();
    clearTimelineEventHighlight();
    // Hide the replay controls bar entirely while in pure topology — there's
    // nothing to replay when the user is browsing the system architecture.
    document.getElementById('replay-controls').classList.add('hidden');
    if (graph?.isReady()) {
      graph.clearStepHighlight();
      graph.clearRunTrace();   // resets to column layout, drops all run state
    }
  } else {
    // Replay animates the graph, so it has nothing to drive in the
    // waterfall or conversation views.
    document.getElementById('replay-controls').classList.toggle('hidden', special);
    if (!threadView()) applyRunTraceToGraph();
  }
}

/** Keep the waterfall and conversation scroll areas below the floating graph
 *  toolbar, whose height changes when it wraps in a narrow pane. */
function trackGraphControlsHeight() {
  const controls = document.querySelector('.graph-controls');
  const pane = document.querySelector('.pane-graph');
  if (!controls || !pane || typeof ResizeObserver === 'undefined') return;
  const update = () => {
    const bottom = controls.offsetTop + controls.offsetHeight;
    pane.style.setProperty('--graph-controls-h', `${bottom + 8}px`);
  };
  new ResizeObserver(update).observe(controls);
  update();
}

function setGraphModeAvailable(available) {
  for (const mode of ['trace', 'waterfall', 'conversation']) {
    const btn = document.querySelector(`.seg-btn[data-mode="${mode}"]`);
    if (btn) btn.disabled = !available;
  }
}

async function loadWaterfall() {
  const el = document.getElementById('waterfall');
  if (!el || !state.selectedRunId) return;
  try {
    const spans = await apiGet(`/runs/${encodeURIComponent(state.selectedRunId)}/spans`);
    renderWaterfall(el, spans, (eventId) => {
      const ev = state.journey.find((e) => e.event_id === eventId);
      if (ev) openStepDrawer(ev);
    });
  } catch (e) {
    el.innerHTML = `<div class="waterfall-empty">${escapeHtml(e.message)}</div>`;
  }
}

function renderRichText(text) {
  if (!text) return '';
  let html = escapeHtml(text);
  html = html.replace(/```([\s\S]*?)```/g, (_, code) => `<pre class="md-code">${code}</pre>`);
  html = html.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  html = html.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  return html;
}

function renderSteps(steps) {
  if (!steps || !steps.length) return '';
  const rows = steps.map((step) => {
    const llm = step.kind === 'llm';
    const name = escapeHtml(step.name || (llm ? 'Model' : 'tool'));
    const server = step.mcp_server ? ` · ${escapeHtml(step.mcp_server)}` : '';
    const line = step.summary ? `<span class="step-summary">${escapeHtml(step.summary)}</span>` : '';
    const ms = step.duration_ms != null ? `<span class="step-ms">${step.duration_ms} ms</span>` : '';
    const inputLabel = llm ? 'Prompt' : 'Input';
    const outputLabel = llm ? 'Reply' : 'Output';
    const body = [
      step.input ? `<div class="step-label">${inputLabel}</div><pre>${escapeHtml(step.input)}</pre>` : '',
      step.output ? `<div class="step-label">${outputLabel}</div><pre>${escapeHtml(step.output)}</pre>` : '',
      step.error ? `<div class="step-error">${escapeHtml(step.error)}</div>` : '',
    ].join('');
    return `<li class="step-call${llm ? ' llm' : ''}${step.error ? ' failed' : ''}">
      <details>
        <summary><span class="step-name">${name}${server}</span>${line}${ms}</summary>
        ${body || '<div class="step-empty">No input or output stored for this call.</div>'}
      </details>
    </li>`;
  }).join('');
  const label = `${steps.length} call${steps.length === 1 ? '' : 's'}`;
  return `<details class="turn-steps"><summary class="step-count">${label}</summary><ol>${rows}</ol></details>`;
}

async function loadConversation() {
  const el = document.getElementById('conversation');
  if (!el) return;
  const whole = threadView();
  const runId = state.selectedRunId;
  const threadId = whole ? state.selectedThreadId : state.runsById.get(runId)?.thread_id;
  const current = () => (whole
    ? threadView() && state.selectedThreadId === threadId
    : !threadView() && state.selectedRunId === runId);
  if (!threadId) {
    el.innerHTML = whole ? '' : '<div class="waterfall-empty">This run is not part of a thread.</div>';
    if (!whole) setCenterOverlay('none');
    return;
  }
  const params = new URLSearchParams(whole ? { last: String(THREAD_TURNS) } : { run: runId });
  try {
    const data = await apiGet(`/threads/${encodeURIComponent(threadId)}?${params}`);
    if (!current() || state.graphMode !== 'conversation') return;
    const turns = data.turns || [];
    const head = whole
      ? `<div class="conversation-scope">Whole chat${turns.length >= THREAD_TURNS ? ` · latest ${THREAD_TURNS} turns` : ''}</div>`
      : `<div class="conversation-scope">This turn
           <button type="button" class="link-btn" data-open-thread>Show the whole chat</button></div>`;
    el.innerHTML = head + (turns.map((turn) => {
      const tone = turn.status === 'failed' ? 'failed' : turn.interrupted ? 'paused' : '';
      const cost = formatCost(turn.cost_usd);
      const link = whole
        ? ` · <button type="button" class="link-btn" data-open-run="${escapeHtml(turn.run_id)}" title="Open this turn's trace and timeline">Open run ↗</button>`
        : '';
      return `
        <article class="turn ${tone}" data-run="${escapeHtml(turn.run_id)}">
          ${turn.user_text ? `<div class="bubble user">${renderRichText(turn.user_text)}</div>` : ''}
          ${renderSteps(turn.steps)}
          ${turn.assistant_text ? `<div class="bubble assistant">${renderRichText(turn.assistant_text)}</div>` : ''}
          <div class="turn-meta">${escapeHtml(turn.status)} · ↑${turn.tokens_in || 0} ↓${turn.tokens_out || 0}${cost ? ' · ' + cost : ''}${link}</div>
          ${turn.error ? `<div class="turn-error">${escapeHtml(turn.error)}</div>` : ''}
        </article>`;
    }).join('') || '<div class="waterfall-empty">No turns.</div>');
    el.querySelector('[data-open-thread]')?.addEventListener('click', () => openThreadFromRun(threadId));
    el.querySelectorAll('[data-open-run]').forEach((btn) => {
      btn.addEventListener('click', () => openRunFromThread(btn.dataset.openRun));
    });
    el.scrollTop = 0;
    if (whole) el.querySelector('.turn:last-of-type')?.scrollIntoView({ block: 'start' });
    else el.querySelector('.bubble.assistant')?.scrollIntoView({ block: 'start' });
  } catch (e) {
    if (!current()) return;
    el.innerHTML = `<div class="waterfall-empty">${escapeHtml(e.message)}</div>`;
  }
  setCenterOverlay('none');
}

/** "Show the whole chat" on a run: switch to Threads with that chat open. */
async function openThreadFromRun(threadId) {
  state.selectedThreadId = threadId;
  state.graphMode = 'conversation';
  await setListMode('threads');
}

async function loadNotes(runId) {
  const el = document.getElementById('run-notes');
  if (!el) return;
  try {
    const notes = await apiGet(`/runs/${encodeURIComponent(runId)}/notes`);
    if (!notes.length) {
      el.classList.add('hidden');
      el.innerHTML = '';
      return;
    }
    el.classList.remove('hidden');
    el.innerHTML = notes.map((n) =>
      `<p><strong>${escapeHtml(n.author || 'note')}</strong> ${escapeHtml(n.text)}</p>`
    ).join('');
  } catch {
    el.classList.add('hidden');
  }
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
    const gen = ++topologyGen;
    try {
      const topo = await apiGet(`/topology?scope=${encodeURIComponent(topologyScopeParam())}`);
      if (gen !== topologyGen) return;
      state.topology = topo;
      if (graph?.isReady()) graph.setTopology(topo);
      // setTopology rebuilds the node/edge elements, dropping all step-*
      // highlight classes — re-apply whatever should currently be showing.
      if (state.graphMode === 'trace') applyRunTraceToGraph();
      // A playing/paused replay holds the loop alive and is guarded above
      // (we return early on autoReplayInProgress), so no manual re-apply is needed.
    } catch { /* swallow */ }
  }, 300);
}

/* ============================================================
 * Empty state + replay
 * ============================================================ */

function renderEmptyState() {
  const empty = document.getElementById('graph-empty');
  // "No runs yet" only when nothing is stored; a filter matching nothing is not that.
  const filtered = state.filterStatus !== 'all' || state.source !== 'all'
    || (state.searchQuery || '').trim() !== '';
  if (state.runs.length === 0 && !filtered) {
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

/** Start (or RESTART) the replay from the beginning. Works even while playing or
 *  paused — it cancels the current run (resetting the cursor to 0) and plays
 *  fresh, which is what distinguishes Start from Resume. */
async function startReplay() {
  if (!state.selectedRunId || !graph?.isReady()) return;
  const speed = parseFloat(document.getElementById('replay-speed').value || '1');
  const edges = buildReplaySteps();
  if (edges.length === 0) {
    toast('Nothing to replay yet — no traversed edges.', 'info', 2500);
    return;
  }
  // Cancel any in-flight/paused run and reset the cursor → restart from scratch.
  if (graph.cancelReplay) graph.cancelReplay();
  const gen = ++state.replayGen;
  state.playback = 'playing';
  state.autoReplayInProgress = true;
  updatePlaybackUI();
  toast(`Replaying ${edges.length} step${edges.length === 1 ? '' : 's'} at ${speed}x`, 'info', 2000);
  showReplayStepBadge(0, edges.length, '');
  try {
    await graph.playRun(edges, speed);   // resolves when finished OR superseded
  } finally {
    // Only the newest run resets shared state — a superseded run (Start pressed
    // again) must not clobber the new run's 'playing' state.
    if (state.replayGen === gen) {
      state.autoReplayInProgress = false;
      state.playback = 'idle';
      updatePlaybackUI();
      hideReplayStepBadge();
    }
  }
}

/** Pause the running replay (keeps the loop alive so Continue resumes here). */
function pauseReplay() {
  if (state.playback !== 'playing') return;
  if (graph?.pauseReplay) graph.pauseReplay();
  state.playback = 'paused';
  updatePlaybackUI();
}

/** Resume a paused replay from exactly where it stopped. */
function resumeReplay() {
  if (state.playback !== 'paused') return;
  if (graph?.resumeReplay) graph.resumeReplay();
  state.playback = 'playing';
  updatePlaybackUI();
}

/** Manual stepping (prev/next). Works while idle (walk the trace by hand without
 *  auto-play) and while paused (nudge the cursor, then Resume continues from
 *  there). Disabled only while auto-play is running. */
function stepReplay(delta) {
  if (state.playback === 'playing' || !graph?.isReady() || !state.selectedRunId) return;
  // First manual step from idle: load the step list (no auto-play) and show step 0.
  const firstStep = graph.replayTotal === 0;
  if (firstStep) {
    const edges = buildReplaySteps();
    if (edges.length === 0) return;
    graph.primeReplay(edges);
  }
  const info = graph.stepTo(firstStep ? 0 : graph.replayCursor + delta);
  if (!info) return;
  showReplayStepBadge(info.index + 1, info.total, `${info.sourceLabel} → ${info.targetLabel}`);
  if (info.eventId) highlightTimelineEvent(info.eventId);
  updatePlaybackUI();
}

/** Clicking a timeline step while a replay is running/paused stops it there and
 *  jumps the graph trace to that step — regardless of where playback currently
 *  is. No-op when not replaying (the click just opens the step drawer). */
function seekReplayToEvent(eventId) {
  if (state.playback !== 'playing' && state.playback !== 'paused') return;
  if (!graph?.isReady()) return;
  const steps = (state.replaySteps && state.replaySteps.length) ? state.replaySteps : buildReplaySteps();
  if (!steps.length) return;
  // A step's eventId is its target event; fall back to the first step landing on
  // the clicked event's node (covers start/non-target events).
  let idx = steps.findIndex((s) => s.eventId === eventId);
  if (idx < 0) {
    const ev = state.journey.find((e) => e.event_id === eventId);
    const nodeId = ev ? nodeIdForEvent(ev) : null;
    if (nodeId) idx = steps.findIndex((s) => s.target === nodeId);
  }
  if (idx < 0) return;
  if (graph.replayTotal === 0) graph.primeReplay(steps);
  if (state.playback === 'playing') pauseReplay();   // stop the run where clicked
  const info = graph.stepTo(idx);                     // jump to the clicked element
  if (info) {
    showReplayStepBadge(info.index + 1, info.total, `${info.sourceLabel} → ${info.targetLabel}`);
    updatePlaybackUI();
  }
}

/** Reflect playback state in the controls. All three buttons (Start/Pause/Resume)
 *  are always visible; enable them per state so the user can navigate freely.
 *  Resume is enabled ONLY when paused — you can't resume unless you've paused. */
function updatePlaybackUI() {
  const pb = state.playback;
  const dis = (id, d) => { const e = document.getElementById(id); if (e) e.disabled = d; };
  dis('replay-start', pb === 'playing');    // Start/restart unless already playing
  dis('replay-pause', pb !== 'playing');    // Pause only while playing
  dis('replay-continue', pb !== 'paused');  // Resume only while paused
  // Manual prev/next: usable whenever a run with steps is loaded and we're not
  // auto-playing (idle = manual walk; paused = nudge then Resume).
  const hasSteps = !!(state.replaySteps && state.replaySteps.length);
  dis('replay-prev', pb === 'playing' || !hasSteps);
  dis('replay-next', pb === 'playing' || !hasSteps);
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
  // Threads mode keeps the right side to a Tools rail until the user opens it.
  const rightCollapsed = threadView() ? s.threadsRightCollapsed !== false : !!s.timelineCollapsed;
  if (timelinePane) timelinePane.classList.toggle('collapsed', rightCollapsed);
}

function togglePane(which) {
  const sel = which === 'runs' ? '.pane-runs' : '.pane-timeline';
  const pane = document.querySelector(sel);
  if (!pane) return;
  pane.classList.toggle('collapsed');
  const isCollapsed = pane.classList.contains('collapsed');
  const s = getPaneState();
  const key = which === 'runs' ? 'runsCollapsed' : threadView() ? 'threadsRightCollapsed' : 'timelineCollapsed';
  s[key] = isCollapsed;
  savePaneState(s);
}

/* ---- Pane resize (drag the inner edge of either side pane) ---- */

const PANE_LIMITS = { runs: [200, 560], timeline: [260, 760] };
const GRAPH_MIN_W = 320;

function paneVar(which) {
  return which === 'runs' ? '--pane-runs-w' : '--pane-timeline-w';
}

function getPaneWidths() {
  try { return JSON.parse(localStorage.getItem(STORAGE_PANE_WIDTHS)) || {}; }
  catch { return {}; }
}

/** Clamp to the pane's limits and leave the graph at least GRAPH_MIN_W. */
function setPaneWidth(which, px) {
  const main = document.querySelector('.app-main');
  const other = document.querySelector(which === 'runs' ? '.pane-timeline' : '.pane-runs');
  const [min, max] = PANE_LIMITS[which];
  const room = main.clientWidth - (other ? other.offsetWidth : 0) - GRAPH_MIN_W;
  const width = Math.round(Math.max(min, Math.min(px, max, room)));
  main.style.setProperty(paneVar(which), `${width}px`);
  return width;
}

function savePaneWidth(which, width) {
  const widths = getPaneWidths();
  if (width == null) delete widths[which];
  else widths[which] = width;
  try { localStorage.setItem(STORAGE_PANE_WIDTHS, JSON.stringify(widths)); } catch { /* full storage */ }
}

function applyPaneWidths() {
  const widths = getPaneWidths();
  for (const which of ['runs', 'timeline']) {
    if (Number.isFinite(widths[which])) setPaneWidth(which, widths[which]);
  }
}

function wirePaneResizers() {
  const main = document.querySelector('.app-main');
  document.querySelectorAll('.pane-resizer').forEach((handle) => {
    const which = handle.dataset.pane;
    const pane = () => document.querySelector(which === 'runs' ? '.pane-runs' : '.pane-timeline');
    handle.addEventListener('pointerdown', (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      try { handle.setPointerCapture(e.pointerId); } catch { /* pointer already gone */ }
      main.classList.add('resizing');
      const box = main.getBoundingClientRect();
      let width = pane().offsetWidth;
      const move = (ev) => {
        width = setPaneWidth(which, which === 'runs' ? ev.clientX - box.left : box.right - ev.clientX);
      };
      const done = () => {
        handle.removeEventListener('pointermove', move);
        handle.removeEventListener('pointerup', done);
        handle.removeEventListener('pointercancel', done);
        main.classList.remove('resizing');
        savePaneWidth(which, width);
      };
      handle.addEventListener('pointermove', move);
      handle.addEventListener('pointerup', done);
      handle.addEventListener('pointercancel', done);
    });
    handle.addEventListener('dblclick', () => {
      main.style.removeProperty(paneVar(which));
      savePaneWidth(which, null);
    });
    handle.addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
      e.preventDefault();
      const grow = (e.key === 'ArrowRight') === (which === 'runs');
      savePaneWidth(which, setPaneWidth(which, pane().offsetWidth + (grow ? 24 : -24)));
    });
  });
  window.addEventListener('resize', applyPaneWidths);
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
      const open = isDrawerOpen() || document.querySelector('.modal:not(.hidden)');
      closeDrawer();
      closeAllModals();
      if (!open && !inField) {
        if (threadView()) { if (state.selectedThreadId) openThread(state.selectedThreadId); }
        else deselectRun();
      }
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
    loadRuns();
  });
  document.getElementById('source-filter').addEventListener('change', (e) => {
    setSource(e.target.value);
    setHash(state.selectedRunId);
    loadRuns();
  });
  document.getElementById('runs-sort').addEventListener('change', (e) => {
    state.runsSort = e.target.value;
    loadRuns();
  });
  document.getElementById('threads-sort').addEventListener('change', (e) => {
    state.threadsSort = e.target.value;
    loadRuns();
  });
  document.getElementById('turns-filter').addEventListener('change', (e) => {
    state.minTurns = Number(e.target.value) || 1;
    loadRuns();
  });
  document.querySelectorAll('.list-toggle-btn[data-list]').forEach((btn) => {
    btn.addEventListener('click', () => setListMode(btn.dataset.list));
  });
  document.querySelectorAll('.list-toggle-btn[data-right]').forEach((btn) => {
    btn.addEventListener('click', () => setRightTab(btn.dataset.right));
  });
  document.getElementById('search-input').addEventListener('input', (e) => {
    state.searchQuery = e.target.value;
    clearTimeout(state.searchTimer);
    if (threadView()) {
      state.searchTimer = setTimeout(loadThreads, 250);
      return;
    }
    if (state.searchQuery.trim().length >= 2) {
      state.searchTimer = setTimeout(async () => {
        await loadRuns();
        try {
          const found = await apiGet(`/search?q=${encodeURIComponent(state.searchQuery.trim())}&limit=20`);
          state.searchHits = found.hits || [];
          const byRun = new Map();
          for (const hit of state.searchHits) {
            if (!byRun.has(hit.run_id)) {
              byRun.set(hit.run_id, { n: 0, snippet: hit.snippet, eventId: hit.event_id });
            }
            byRun.get(hit.run_id).n += 1;
          }
          for (const run of state.runs) {
            const hit = byRun.get(run.run_id);
            run._snippet = hit ? `${hit.n} match${hit.n === 1 ? '' : 'es'}: ${hit.snippet}` : '';
            run._hitEventId = hit ? hit.eventId : '';
          }
          renderRunList();
          renderTimeline();
        } catch { /* search stays on the run list */ }
      }, 250);
    } else {
      state.searchHits = [];
      renderRunList();
    }
  });
  document.getElementById('timeline-search').addEventListener('input', (e) => {
    state.timelineFilter = e.target.value;
    applyTimelineFilter();
  });

  document.querySelectorAll('.seg-btn[data-mode]').forEach((b) => {
    b.addEventListener('click', () => {
      if (b.disabled) return;
      const mode = b.dataset.mode;
      if (mode === 'conversation' && mode !== state.graphMode) setCenterOverlay('loading');
      setGraphMode(mode);
      // Conversation clears the overlay once loaded; the graph views draw at once.
      if (mode !== 'conversation' && !(threadView() && !state.selectedThreadId)) setCenterOverlay('none');
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

  document.getElementById('replay-start').addEventListener('click', startReplay);
  document.getElementById('replay-pause').addEventListener('click', pauseReplay);
  document.getElementById('replay-continue').addEventListener('click', resumeReplay);
  document.getElementById('replay-prev').addEventListener('click', () => stepReplay(-1));
  document.getElementById('replay-next').addEventListener('click', () => stepReplay(1));

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
    if ((params.source || 'all') !== state.source) {
      setSource(params.source);
      loadRuns();
    }
    if (params.run && params.run !== state.selectedRunId) {
      selectRun(params.run);
    } else if (!params.run && state.selectedRunId) {
      deselectRun();
    }
  });
}

function wsUrl(path) {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${location.host}${path}`;
}

/** Token travels in Sec-WebSocket-Protocol (base64url so it is a valid header
 *  token), never in the URL where proxies and history would record it. */
function wsProtocols() {
  if (!state.authToken) return undefined;
  const bytes = new TextEncoder().encode(state.authToken);
  let bin = '';
  bytes.forEach((b) => { bin += String.fromCharCode(b); });
  const enc = btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  return ['tracesage.v1', `tracesage.bearer.${enc}`];
}

function detachSocket(ws) {
  if (!ws) return;
  ws.onopen = null; ws.onclose = null; ws.onmessage = null; ws.onerror = null;
  try { ws.close(); } catch { /* noop */ }
}

/* ============================================================
 * Bootstrap
 * ============================================================ */

async function main() {
  applyTheme(state.theme);
  trackGraphControlsHeight();

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
  applyPaneState();
  applyRightPane();
  applyPaneWidths();
  wirePaneResizers();

  // Read initial hash before first load.
  const initial = readHash();
  if (initial.status) {
    state.filterStatus = initial.status;
    document.getElementById('status-filter').value = initial.status;
  }
  if (initial.source) setSource(initial.source);

  await Promise.all([loadRuns(), loadTopology(), pollHealth(), loadSourceCounts()]);

  // Auto-select run from hash, if any.
  if (initial.run) {
    if (!state.runsById.has(initial.run)) {
      try {
        state.runsById.set(initial.run, await apiGet(`/runs/${encodeURIComponent(initial.run)}`));
      } catch {
        initial.run = null;
      }
    }
    if (initial.run) selectRun(initial.run);
  }

  openRunsWS();

  // Background pollers.
  setTimeout(pollStats, STATS_POLL_MS);
  setInterval(loadRuns, RUNS_POLL_MS);
  setInterval(loadSourceCounts, 30000);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', main);
} else {
  main();
}
