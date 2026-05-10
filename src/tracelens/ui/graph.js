/* graph.js - custom SVG renderer for the tracelens topology view.
 *
 * Replaces the previous Cytoscape integration. Why:
 *   - Cytoscape's arrow rendering was hard to make impossible-to-miss in our
 *     dense column layouts; users reported "no arrows at all".
 *   - We want a deterministic horizontal column layout: chain | agent | tool |
 *     llm | retriever. Each kind in its own column makes "which tool belongs
 *     to which agent" obvious at a glance.
 *   - Owning the SVG ourselves means giant explicit <marker> arrowheads, no
 *     CDN dependency, predictable rendering across browsers/zoom levels.
 *
 * Public API (preserved from the previous Cytoscape module so app.js doesn't
 * need to change):
 *
 *   new GraphView(container, { onNodeClick, onEdgeClick, onPlayDone })
 *   graph.isReady()
 *   graph.refreshStyles()
 *   graph.setTopology({ nodes, edges })
 *   graph.setRunTrace(nodeIds, traversedEdges)
 *   graph.clearRunTrace()
 *   graph.pulseNode(nodeId)
 *   graph.flashEdge(sourceId, targetId)
 *   graph.playRun(edges, speed)
 */

const SVG_NS = 'http://www.w3.org/2000/svg';

// Column ordering left-to-right. Any unknown kind falls back to 'agent'.
const KIND_COLUMNS = ['chain', 'agent', 'tool', 'llm', 'retriever'];
const KIND_LABEL = {
  chain: 'CHAIN',
  agent: 'AGENT',
  tool: 'TOOL',
  llm: 'LLM',
  retriever: 'RETRIEVER',
};
// Subtitle shown under each column heading so users immediately understand the kind.
const KIND_SUBTITLE = {
  chain: 'Pipelines & orchestration',
  agent: 'Workers (your nodes)',
  tool: 'Functions called by agents',
  llm: 'Language models',
  retriever: 'Document retrievers',
};

// Layout constants in SVG user space.
const COLUMN_WIDTH = 260;
const COLUMN_PAD_X = 80;
const ROW_HEIGHT = 110;
const HEADER_HEIGHT = 56;
const TOP_PAD = 40;
const NODE_HALF = 38;          // half the bounding box of a node shape

function cssVar(name, el) {
  const root = el || document.documentElement;
  return getComputedStyle(root).getPropertyValue(name).trim();
}

function el(tag, attrs = {}, children = []) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null) continue;
    node.setAttribute(k, String(v));
  }
  for (const c of children) {
    if (c) node.appendChild(c);
  }
  return node;
}

/* =====================================================================
 * GraphView
 * ===================================================================== */

export class GraphView {
  constructor(container, handlers = {}) {
    this.container = container;
    this.handlers = handlers;
    this.tooltipEl = document.getElementById('graph-tooltip');

    this.nodes = [];
    this.edges = [];
    this.nodesById = new Map();
    this.edgesByKey = new Map();

    this.mode = 'topology';       // 'topology' | 'trace'
    this.runNodeIds = null;       // Set<string> when in run-trace mode
    this.runEdges = null;         // Set<sourceTarget> when in run-trace mode
    this.runFirstNodeId = null;   // first node visited in selected run
    this.runLastNodeId = null;    // last node visited in selected run
    this.runJourney = null;       // ordered events for the selected run
    this.focusedNodeId = null;    // node user clicked on (highlight neighborhood)
    this.hoveredNodeId = null;    // node user is currently hovering (ephemeral preview)
    this.activeDots = [];         // {dot, cancelled}[] — in-flight cursor dots

    this.viewBox = { x: 0, y: 0, w: 1400, h: 700 };

    this.svg = null;
    this.defs = null;
    this.edgesLayer = null;
    this.nodesLayer = null;
    this.headersLayer = null;
    this.bgLayer = null;

    this._dragOrigin = null;
    this._initialized = false;
    this._init();
  }

  _init() {
    this.container.innerHTML = '';
    this.svg = el('svg', {
      width: '100%',
      height: '100%',
      preserveAspectRatio: 'xMidYMid meet',
      'aria-label': 'agent topology graph',
    });
    this.svg.style.cursor = 'grab';

    this.defs = el('defs');
    this.svg.appendChild(this.defs);
    this._buildArrowMarkers();

    this.bgLayer = el('g', { class: 'bg-layer' });
    this.headersLayer = el('g', { class: 'headers-layer' });
    this.edgesLayer = el('g', { class: 'edges-layer' });
    this.nodesLayer = el('g', { class: 'nodes-layer' });
    this.svg.appendChild(this.bgLayer);
    this.svg.appendChild(this.headersLayer);
    this.svg.appendChild(this.edgesLayer);
    this.svg.appendChild(this.nodesLayer);

    this._wirePanZoom();
    this._applyViewBox();

    this.container.appendChild(this.svg);
    this._initialized = true;
  }

  /** Fresh arrow markers — rebuilt on theme change so colors track CSS vars. */
  _buildArrowMarkers() {
    while (this.defs.firstChild) this.defs.removeChild(this.defs.firstChild);
    const accent = cssVar('--accent') || '#58a6ff';
    const success = cssVar('--success') || '#3fb950';
    const dim = cssVar('--text-dim') || '#8b949e';

    const make = (id, color, w, h) =>
      el('marker', {
        id,
        viewBox: '0 0 14 14',
        refX: 12,
        refY: 7,
        markerWidth: w,
        markerHeight: h,
        orient: 'auto-start-reverse',
        markerUnits: 'userSpaceOnUse',
      }, [
        el('path', { d: 'M 0 0 L 14 7 L 0 14 L 3 7 Z', fill: color }),
      ]);

    this.defs.appendChild(make('at-arrow', accent, 18, 18));
    this.defs.appendChild(make('at-arrow-path', success, 24, 24));
    this.defs.appendChild(make('at-arrow-faded', dim, 12, 12));
  }

  _applyViewBox() {
    const { x, y, w, h } = this.viewBox;
    this.svg.setAttribute('viewBox', `${x} ${y} ${w} ${h}`);
  }

  _wirePanZoom() {
    let dragging = false;
    let lastX = 0, lastY = 0;

    const onMouseDown = (e) => {
      if (e.target.closest('.node-group') || e.target.closest('.edge-path')) return;
      dragging = true;
      lastX = e.clientX;
      lastY = e.clientY;
      this.svg.style.cursor = 'grabbing';
    };
    // Background click clears any node focus — but only if we didn't drag.
    let downX = 0, downY = 0;
    this.svg.addEventListener('mousedown', (e) => {
      downX = e.clientX; downY = e.clientY;
    });
    this.svg.addEventListener('click', (e) => {
      if (e.target.closest('.node-group') || e.target.closest('.edge-path')) return;
      const dx = Math.abs(e.clientX - downX);
      const dy = Math.abs(e.clientY - downY);
      if (dx + dy > 4) return; // it was a drag, not a click
      if (this.focusedNodeId) this.setFocusedNode(null);
    });
    const onMouseMove = (e) => {
      if (!dragging) return;
      const dx = e.clientX - lastX;
      const dy = e.clientY - lastY;
      lastX = e.clientX;
      lastY = e.clientY;
      const scale = this.viewBox.w / this.svg.clientWidth || 1;
      this.viewBox.x -= dx * scale;
      this.viewBox.y -= dy * scale;
      this._applyViewBox();
    };
    const onMouseUp = () => {
      dragging = false;
      this.svg.style.cursor = 'grab';
    };
    const onWheel = (e) => {
      e.preventDefault();
      const factor = e.deltaY > 0 ? 1.1 : 1 / 1.1;
      const rect = this.svg.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      const my = e.clientY - rect.top;
      const sx = this.viewBox.x + (mx / rect.width) * this.viewBox.w;
      const sy = this.viewBox.y + (my / rect.height) * this.viewBox.h;
      this.viewBox.w = Math.max(200, Math.min(8000, this.viewBox.w * factor));
      this.viewBox.h = Math.max(200, Math.min(8000, this.viewBox.h * factor));
      this.viewBox.x = sx - (mx / rect.width) * this.viewBox.w;
      this.viewBox.y = sy - (my / rect.height) * this.viewBox.h;
      this._applyViewBox();
    };

    this.svg.addEventListener('mousedown', onMouseDown);
    window.addEventListener('mousemove', onMouseMove);
    window.addEventListener('mouseup', onMouseUp);
    this.svg.addEventListener('wheel', onWheel, { passive: false });
  }

  isReady() {
    return this._initialized;
  }

  /** Re-apply CSS-var-derived colors. Called on theme toggle. */
  refreshStyles() {
    this._buildArrowMarkers();
    // Rerender edges so they pick up new colors via marker-end.
    this._renderEdges();
    this._renderNodes();
  }

  /* ----------------------------------------------------------- topology */

  setTopology(topology) {
    const incoming = (topology && topology.nodes) || [];
    const incomingEdges = (topology && topology.edges) || [];

    // Build node objects keyed by id.
    const nextNodes = new Map();
    for (const n of incoming) {
      const kind = KIND_COLUMNS.includes(n.type) ? n.type : 'agent';
      nextNodes.set(n.id, {
        id: n.id,
        name: n.name,
        type: kind,
        invocations: n.invocation_count || 0,
        errors: n.error_count || 0,
        avgMs: n.avg_duration_ms,
        p99Ms: n.p99_duration_ms,
        lastSeen: n.last_seen,
        x: 0,
        y: 0,
      });
    }
    this.nodes = [...nextNodes.values()];
    this.nodesById = nextNodes;

    // Build edges.
    const nextEdges = new Map();
    for (const e of incomingEdges) {
      const k = `${e.source}->${e.target}`;
      nextEdges.set(k, {
        key: k,
        source: e.source,
        target: e.target,
        count: e.count || 0,
        lastSeen: e.last_seen,
      });
    }
    this.edges = [...nextEdges.values()];
    this.edgesByKey = nextEdges;

    this._layout();
    this._renderHeaders();
    this._renderEdges();
    this._renderNodes();
    this._fitIfFirstRender();
  }

  /** Dispatch to the right layout function depending on mode. */
  _layout() {
    if (this.mode === 'trace' && this.runJourney) {
      this._layoutTrace();
    } else {
      this._layoutColumns();
    }
  }

  /** Trace layout: agents flow left-to-right in execution order, with the
   *  resources each agent invoked stacked vertically beneath it. Chain roots
   *  (LangGraph, LCEL pipelines) anchor the start; END marker on the right.
   *  Nodes that didn't participate in this run are hidden off-screen. */
  _layoutTrace() {
    const journey = this.runJourney;
    if (!journey || journey.length === 0) {
      this._layoutColumns();
      return;
    }

    // Walk journey to build:
    //   - orderedRoots: chain nodes (e.g. LangGraph) in first-seen order
    //   - orderedAgents: agent nodes in first-seen order
    //   - agentResources: agent_id -> ordered list of resource ids called by it
    const orderedRoots = [];
    const seenRoots = new Set();
    const orderedAgents = [];
    const seenAgents = new Set();
    const agentResources = new Map();
    const stack = []; // stack of agent/chain ids whose chain_start has fired

    for (const ev of journey) {
      const id = this._eventNodeId(ev);
      if (!id) continue;
      const node = this.nodesById.get(id);
      if (!node) continue;

      const t = ev.event_type;
      const isStart = t === 'chain_start' || t === 'run_start';
      const isEnd = t === 'chain_end' || t === 'chain_error' || t === 'run_end';

      if ((node.type === 'chain' || node.type === 'agent') && isStart) {
        stack.push(id);
        if (node.type === 'chain' && !seenRoots.has(id)) {
          seenRoots.add(id);
          orderedRoots.push(id);
        }
        if (node.type === 'agent' && !seenAgents.has(id)) {
          seenAgents.add(id);
          orderedAgents.push(id);
          agentResources.set(id, []);
        }
      } else if ((node.type === 'chain' || node.type === 'agent') && isEnd) {
        // Pop matching frame if any (defensive on order anomalies).
        const idx = stack.lastIndexOf(id);
        if (idx >= 0) stack.splice(idx, 1);
      } else if (
        (node.type === 'tool' || node.type === 'llm' || node.type === 'retriever') &&
        (t.endsWith('_start') || t === 'chat_model_start')
      ) {
        // Attribute this resource to the nearest agent on the stack (skip chains).
        let agentId = null;
        for (let i = stack.length - 1; i >= 0; i--) {
          const sid = stack[i];
          const sNode = this.nodesById.get(sid);
          if (sNode && sNode.type === 'agent') { agentId = sid; break; }
        }
        if (agentId == null && stack.length > 0) agentId = stack[stack.length - 1];
        if (agentId && agentResources.has(agentId)) {
          const list = agentResources.get(agentId);
          if (!list.includes(id)) list.push(id);
        } else if (agentId) {
          agentResources.set(agentId, [id]);
        }
      }
    }

    // Layout constants for trace mode.
    const COL_W = 240;
    const AGENT_Y = 200;
    const RESOURCE_Y0 = 360;
    const RESOURCE_DY = 100;
    const PAD_LEFT = 100;

    // Columns left to right: roots first, then agents in order.
    let colIdx = 0;
    for (const rid of orderedRoots) {
      const n = this.nodesById.get(rid);
      if (!n) continue;
      n.x = PAD_LEFT + colIdx * COL_W;
      n.y = AGENT_Y;
      colIdx++;
    }
    for (const aid of orderedAgents) {
      const a = this.nodesById.get(aid);
      if (!a) continue;
      a.x = PAD_LEFT + colIdx * COL_W;
      a.y = AGENT_Y;
      // Resources stacked beneath this agent.
      const list = agentResources.get(aid) || [];
      list.forEach((rid, i) => {
        const r = this.nodesById.get(rid);
        if (!r) return;
        r.x = PAD_LEFT + colIdx * COL_W;
        r.y = RESOURCE_Y0 + i * RESOURCE_DY;
      });
      colIdx++;
    }

    // Hide nodes that did not participate in this run.
    for (const n of this.nodes) {
      if (!this.runNodeIds.has(n.id)) {
        n.x = -100000;
        n.y = -100000;
      }
    }

    // Compute viewBox height based on max resource stack.
    let maxResources = 0;
    for (const list of agentResources.values()) maxResources = Math.max(maxResources, list.length);
    this.viewBox.h = Math.max(500, RESOURCE_Y0 + maxResources * RESOURCE_DY + 100);
    this.viewBox.w = Math.max(800, PAD_LEFT * 2 + colIdx * COL_W);
    this._applyViewBox();
  }

  /** Best-effort recreation of nodeIdForEvent kind logic for inside-class use. */
  _eventNodeId(ev) {
    return nodeIdForEvent(ev);
  }

  /** Position nodes in columns by kind, sorted alphabetically within each column. */
  _layoutColumns() {
    const groups = {};
    for (const k of KIND_COLUMNS) groups[k] = [];
    for (const n of this.nodes) {
      groups[KIND_COLUMNS.includes(n.type) ? n.type : 'agent'].push(n);
    }
    for (const k of KIND_COLUMNS) groups[k].sort((a, b) => a.name.localeCompare(b.name));

    let maxRows = 0;
    for (const k of KIND_COLUMNS) maxRows = Math.max(maxRows, groups[k].length);
    if (maxRows === 0) maxRows = 1;

    const totalH = HEADER_HEIGHT + TOP_PAD + maxRows * ROW_HEIGHT + 60;
    this.viewBox.h = Math.max(this.viewBox.h, totalH);
    this.viewBox.w = Math.max(this.viewBox.w, COLUMN_PAD_X * 2 + KIND_COLUMNS.length * COLUMN_WIDTH);

    for (const [colIdx, kind] of KIND_COLUMNS.entries()) {
      const col = groups[kind];
      const colX = COLUMN_PAD_X + colIdx * COLUMN_WIDTH + COLUMN_WIDTH / 2;
      // Center the column vertically against the tallest column.
      const startY = HEADER_HEIGHT + TOP_PAD + ((maxRows - col.length) * ROW_HEIGHT) / 2 + ROW_HEIGHT / 2;
      col.forEach((n, i) => {
        n.x = colX;
        n.y = startY + i * ROW_HEIGHT;
      });
    }

    this._applyViewBox();
  }

  _fitIfFirstRender() {
    if (this._didFitOnce) return;
    if (this.nodes.length === 0) return;
    this._didFitOnce = true;
    // Re-anchor viewBox to start at 0,0 so the whole layout is visible.
    this.viewBox.x = 0;
    this.viewBox.y = 0;
    this._applyViewBox();
  }

  /* --------------------------------------------------------- column headers */

  _renderHeaders() {
    // Clear both layers so we don't accumulate stale bg/header elements.
    this.headersLayer.innerHTML = '';
    this.bgLayer.innerHTML = '';
    // In trace mode the column layout doesn't apply; render a different header
    // that explains the run is laid out chronologically left-to-right.
    if (this.mode === 'trace') {
      this._renderTraceHeader();
      return;
    }
    const border = cssVar('--border') || '#30363d';
    const dim = cssVar('--text-dim') || '#8b949e';

    for (const [colIdx, kind] of KIND_COLUMNS.entries()) {
      const colX = COLUMN_PAD_X + colIdx * COLUMN_WIDTH;
      // Subtle column band for visual grouping.
      this.bgLayer.appendChild(el('rect', {
        x: colX,
        y: HEADER_HEIGHT,
        width: COLUMN_WIDTH,
        height: this.viewBox.h - HEADER_HEIGHT,
        fill: cssVar(`--node-${kind}`) || dim,
        'fill-opacity': 0.04,
      }));
      // Vertical separator.
      if (colIdx > 0) {
        this.bgLayer.appendChild(el('line', {
          x1: colX, y1: HEADER_HEIGHT,
          x2: colX, y2: this.viewBox.h - 20,
          stroke: border,
          'stroke-dasharray': '3 5',
          'stroke-width': 1,
          opacity: 0.4,
        }));
      }
      // Column heading text.
      const heading = el('text', {
        x: colX + COLUMN_WIDTH / 2,
        y: HEADER_HEIGHT - 22,
        'text-anchor': 'middle',
        'font-size': 13,
        'font-weight': 800,
        'letter-spacing': 1.2,
        fill: cssVar(`--node-${kind}`) || cssVar('--accent'),
      });
      heading.textContent = KIND_LABEL[kind] || kind.toUpperCase();
      this.headersLayer.appendChild(heading);
      // Subtitle so users know what each column means at a glance.
      const subtitle = el('text', {
        x: colX + COLUMN_WIDTH / 2,
        y: HEADER_HEIGHT - 6,
        'text-anchor': 'middle',
        'font-size': 10,
        fill: cssVar('--text-dim'),
      });
      subtitle.textContent = KIND_SUBTITLE[kind] || '';
      this.headersLayer.appendChild(subtitle);
    }
  }

  /** Trace header: a single bar with 'EXECUTION FLOW →' and an explainer line.
   *  Two horizontal swimlane labels mark the agent row and the resource row. */
  _renderTraceHeader() {
    const accent = cssVar('--accent');
    const dim = cssVar('--text-dim');
    // Big heading
    const heading = el('text', {
      x: this.viewBox.w / 2,
      y: HEADER_HEIGHT - 22,
      'text-anchor': 'middle',
      'font-size': 14,
      'font-weight': 800,
      'letter-spacing': 1.5,
      fill: accent,
    });
    heading.textContent = 'EXECUTION FLOW   →';
    this.headersLayer.appendChild(heading);
    const sub = el('text', {
      x: this.viewBox.w / 2,
      y: HEADER_HEIGHT - 6,
      'text-anchor': 'middle',
      'font-size': 11,
      fill: dim,
    });
    sub.textContent = 'Agents in time order  ·  resources stacked beneath the agent that called them';
    this.headersLayer.appendChild(sub);

    // Swimlane labels at the very left margin.
    const laneLabel = (text, y) => {
      const t = el('text', {
        x: 18, y,
        'font-size': 10,
        'font-weight': 800,
        'letter-spacing': 1,
        fill: dim,
        'text-anchor': 'start',
      });
      t.textContent = text;
      this.headersLayer.appendChild(t);
    };
    laneLabel('AGENTS', 200 + 5);
    laneLabel('RESOURCES', 360);
  }

  /* ------------------------------------------------------------- nodes */

  _renderNodes() {
    this.nodesLayer.innerHTML = '';
    for (const n of this.nodes) {
      const g = this._makeNodeElement(n);
      this.nodesLayer.appendChild(g);
    }
  }

  _makeNodeElement(n) {
    const g = el('g', {
      class: `node-group node-${n.type}`,
      'data-id': n.id,
      transform: `translate(${n.x},${n.y})`,
    });
    g.style.cursor = 'pointer';

    // Determine visual state.
    const inFocusMode = !!this.focusedNodeId;
    const isFocused = inFocusMode && this.focusedNodeId === n.id;
    const isNeighbor = inFocusMode && this._isNeighbor(this.focusedNodeId, n.id);
    const inRunMode = !!this.runNodeIds;
    const isInRun = inRunMode && this.runNodeIds.has(n.id);
    const dimmedByFocus = inFocusMode && !isFocused && !isNeighbor;
    const dimmedByRun = inRunMode && !isInRun;

    if (dimmedByFocus || dimmedByRun) g.classList.add('faded');
    if (isInRun) g.classList.add('path');
    if (isFocused) g.classList.add('focused');
    if (isNeighbor) g.classList.add('neighbor');
    if ((n.errors || 0) > 0) g.classList.add('error');

    // Invisible hitbox so the entire labeled area registers clicks reliably,
    // even on empty space between the shape and the text label.
    g.appendChild(el('rect', {
      x: -NODE_HALF * 1.5,
      y: -NODE_HALF * 1.0,
      width: NODE_HALF * 3,
      height: NODE_HALF * 2.6,
      fill: 'rgba(0,0,0,0.001)',  // not strictly transparent so pointer events fire
      stroke: 'none',
      'pointer-events': 'all',
    }));

    // START / END flag for the first/last visited node in run-trace mode.
    if (inRunMode && (n.id === this.runFirstNodeId || n.id === this.runLastNodeId)) {
      const isStart = n.id === this.runFirstNodeId;
      const isEnd = n.id === this.runLastNodeId;
      const flagY = -NODE_HALF - 18;
      const flagX = isStart ? -NODE_HALF - 36 : NODE_HALF + 36;
      const flagFill = isStart ? cssVar('--success') : cssVar('--accent');
      const flagText = isStart ? 'START ▶' : (isEnd ? '■ END' : '');
      const flagBg = el('rect', {
        x: flagX - 36, y: flagY - 12,
        width: 72, height: 22,
        rx: 11, ry: 11,
        fill: flagFill,
        'fill-opacity': 0.92,
      });
      const flagTxt = el('text', {
        x: flagX, y: flagY + 4,
        'text-anchor': 'middle',
        'font-size': 11,
        'font-weight': 800,
        'letter-spacing': 0.5,
        fill: '#0d1117',
      });
      flagTxt.textContent = flagText;
      g.appendChild(flagBg);
      g.appendChild(flagTxt);
    }

    // Shape by kind.
    const shape = this._shapeForKind(n.type, n);
    g.appendChild(shape);

    // Name label below the shape.
    const label = el('text', {
      class: 'node-name',
      'text-anchor': 'middle',
      x: 0,
      y: NODE_HALF + 18,
      'font-size': 13,
      'font-weight': 600,
    });
    label.textContent = this._truncate(n.name, 22);
    g.appendChild(label);

    // Invocation count bubble.
    const countBubble = el('g', { class: 'node-count' });
    const cb = el('circle', {
      cx: NODE_HALF * 0.7,
      cy: -NODE_HALF * 0.7,
      r: 12,
      fill: cssVar('--surface'),
      stroke: cssVar('--border'),
    });
    countBubble.appendChild(cb);
    const cn = el('text', {
      x: NODE_HALF * 0.7,
      y: -NODE_HALF * 0.7 + 4,
      'text-anchor': 'middle',
      'font-size': 11,
      'font-weight': 700,
      fill: cssVar('--text'),
    });
    cn.textContent = String(n.invocations);
    countBubble.appendChild(cn);
    g.appendChild(countBubble);

    // Click + hover.
    g.addEventListener('click', (e) => {
      e.stopPropagation();
      // Toggle focus: clicking the same node again clears focus.
      if (this.focusedNodeId === n.id) this.setFocusedNode(null);
      else this.setFocusedNode(n.id);
      this.handlers.onNodeClick && this.handlers.onNodeClick({
        id: n.id, type: n.type, label: n.name,
        invocations: n.invocations, errors: n.errors,
        avgMs: n.avgMs, p99Ms: n.p99Ms, lastSeen: n.lastSeen,
      });
    });
    g.addEventListener('mouseenter', (e) => {
      this._showTooltip(n, e);
      // Ephemeral hover preview: highlight this node + its direct neighbors
      // without changing focus state. Cleared on mouseleave.
      this._setHoverPreview(n.id);
    });
    g.addEventListener('mousemove', (e) => this._moveTooltip(e));
    g.addEventListener('mouseleave', () => {
      this._hideTooltip();
      this._setHoverPreview(null);
    });
    return g;
  }

  /** Apply hover-preview classes (focused + neighbor) without re-rendering.
   *  Re-rendering on every mouse event was the source of flicker; this just
   *  toggles classes on existing elements. */
  _setHoverPreview(nodeId) {
    if (this.hoveredNodeId === nodeId) return;
    this.hoveredNodeId = nodeId;
    if (!nodeId) {
      this.nodesLayer.querySelectorAll('.node-group').forEach((g) => {
        g.classList.remove('hover-preview', 'hover-neighbor');
      });
      this.edgesLayer.querySelectorAll('.edge-group .edge-path').forEach((p) => {
        p.classList.remove('hover-active');
      });
      return;
    }
    // Build neighbor set.
    const neighbors = new Set();
    for (const e of this.edges) {
      if (e.source === nodeId) neighbors.add(e.target);
      if (e.target === nodeId) neighbors.add(e.source);
    }
    this.nodesLayer.querySelectorAll('.node-group').forEach((g) => {
      const id = g.getAttribute('data-id');
      g.classList.toggle('hover-preview', id === nodeId);
      g.classList.toggle('hover-neighbor', neighbors.has(id));
    });
    // Highlight involved edges via a class added to .edge-path.
    this.edgesLayer.querySelectorAll('.edge-group').forEach((g) => {
      const k = g.getAttribute('data-key') || '';
      const [src, tgt] = k.split('->');
      const involved = src === nodeId || tgt === nodeId;
      const path = g.querySelector('.edge-path');
      if (path) path.classList.toggle('hover-active', involved);
    });
  }

  /** True if `otherId` is directly connected to `nodeId` (either direction). */
  _isNeighbor(nodeId, otherId) {
    for (const e of this.edges) {
      if ((e.source === nodeId && e.target === otherId) ||
          (e.target === nodeId && e.source === otherId)) {
        return true;
      }
    }
    return false;
  }

  /** Public: highlight a node and its connected neighbors; null clears focus. */
  setFocusedNode(nodeId) {
    this.focusedNodeId = nodeId || null;
    this._renderEdges();
    this._renderNodes();
  }

  /* ---------------------------------------------- manual-step highlighting */

  /** Mark a single step (one source -> target transition) as the 'current'
   *  step. Used by the manual replay's prev/next buttons.
   *
   *  options.animate (default true): play the moving dot along the edge
   *    so the user can SEE the transition. Cancellable — clicking fast
   *    won't pile up in-flight dots.
   *  options.pulse (default true): also pulse the target node. Manual
   *    replay passes false so the pulse doesn't overlap with the next
   *    click. Auto replay keeps pulse=true for the dramatic effect.
   *  options.reverse (default false): for manual 'prev', animate the dot
   *    target -> source so the user sees the direction of travel match
   *    their action. */
  highlightStep(sourceId, targetId, options = {}) {
    const animate = options.animate !== false;
    const pulse = options.pulse !== false;
    const reverse = options.reverse === true;
    this._cancelActiveDots();
    this.clearStepHighlight();
    const srcG = this.nodesLayer.querySelector(`[data-id="${cssEscape(sourceId)}"]`);
    const tgtG = this.nodesLayer.querySelector(`[data-id="${cssEscape(targetId)}"]`);
    if (srcG) srcG.classList.add('step-source');
    if (tgtG) tgtG.classList.add('step-target');
    const edgeKey = `${sourceId}->${targetId}`;
    const edgeG = this.edgesLayer.querySelector(`[data-key="${cssEscape(edgeKey)}"]`);
    if (edgeG) {
      const path = edgeG.querySelector('.edge-path');
      if (path) path.classList.add('step-active');
      if (animate && path) this._animateDotAlongPath(path, 700, reverse);
    }
    if (animate && pulse && tgtG) {
      tgtG.classList.remove('pulse');
      void tgtG.getBBox?.();
      tgtG.classList.add('pulse');
      setTimeout(() => tgtG.classList.remove('pulse'), 1100);
    }
  }

  /** Cancel and remove any in-flight dot animations so a new step doesn't
   *  pile up extras. */
  _cancelActiveDots() {
    for (const item of this.activeDots) {
      item.cancelled = true;
      try { item.dot.remove(); } catch { /* already removed */ }
    }
    this.activeDots = [];
  }

  /** Remove all step-source / step-target / step-active classes. */
  clearStepHighlight() {
    this.nodesLayer.querySelectorAll('.step-source, .step-target').forEach((g) => {
      g.classList.remove('step-source', 'step-target');
    });
    this.edgesLayer.querySelectorAll('.edge-path.step-active').forEach((p) => {
      p.classList.remove('step-active');
    });
  }

  /* ----------------------------------------------------------- view controls */

  /** Reset viewBox to fit the entire content. */
  fit() {
    if (this.nodes.length === 0) return;
    const xs = this.nodes.map(n => n.x);
    const ys = this.nodes.map(n => n.y);
    const minX = Math.min(...xs) - NODE_HALF * 3;
    const minY = Math.min(...ys) - NODE_HALF * 3;
    const maxX = Math.max(...xs) + NODE_HALF * 3;
    const maxY = Math.max(...ys) + NODE_HALF * 3;
    this.viewBox.x = minX;
    this.viewBox.y = Math.max(0, minY);
    this.viewBox.w = Math.max(400, maxX - minX);
    this.viewBox.h = Math.max(400, maxY - this.viewBox.y);
    this._applyViewBox();
  }

  zoomIn() { this._zoomBy(1 / 1.2); }
  zoomOut() { this._zoomBy(1.2); }
  _zoomBy(factor) {
    const cx = this.viewBox.x + this.viewBox.w / 2;
    const cy = this.viewBox.y + this.viewBox.h / 2;
    this.viewBox.w = Math.max(200, Math.min(8000, this.viewBox.w * factor));
    this.viewBox.h = Math.max(200, Math.min(8000, this.viewBox.h * factor));
    this.viewBox.x = cx - this.viewBox.w / 2;
    this.viewBox.y = cy - this.viewBox.h / 2;
    this._applyViewBox();
  }

  /** No-op in the SVG renderer (layout is always horizontal columns), but
   *  preserved so existing UI buttons don't error. */
  toggleLayoutDir() { /* no-op */ }
  setLayoutDir() { /* no-op */ }
  getLayoutDir() { return 'LR'; }

  _shapeForKind(kind, n) {
    const fill = cssVar(`--node-${kind}`) || cssVar('--accent');
    const stroke = (this.runNodeIds && this.runNodeIds.has(n.id))
      ? cssVar('--success')
      : ((n.errors || 0) > 0 ? cssVar('--error') : cssVar('--border'));
    const strokeW =
      (this.runNodeIds && this.runNodeIds.has(n.id)) ? 4
      : (this.runNodeIds && !this.runNodeIds.has(n.id)) ? 1
      : 2;
    const opacity = (this.runNodeIds && !this.runNodeIds.has(n.id)) ? 0.35 : 1;

    let shape;
    switch (kind) {
      case 'agent': {
        // Hexagon
        const r = NODE_HALF;
        const pts = [
          [r, 0], [r / 2, r * Math.sqrt(3) / 2],
          [-r / 2, r * Math.sqrt(3) / 2], [-r, 0],
          [-r / 2, -r * Math.sqrt(3) / 2], [r / 2, -r * Math.sqrt(3) / 2],
        ].map(p => p.join(',')).join(' ');
        shape = el('polygon', { points: pts });
        break;
      }
      case 'tool': {
        // Rounded rectangle
        shape = el('rect', {
          x: -NODE_HALF, y: -NODE_HALF * 0.7,
          width: NODE_HALF * 2, height: NODE_HALF * 1.4,
          rx: 8, ry: 8,
        });
        break;
      }
      case 'llm': {
        // Ellipse
        shape = el('ellipse', {
          cx: 0, cy: 0,
          rx: NODE_HALF * 1.05, ry: NODE_HALF * 0.7,
        });
        break;
      }
      case 'retriever': {
        // Cylinder via path
        const w = NODE_HALF * 1.6, h = NODE_HALF * 1.4, r = h * 0.18;
        const d = `
          M ${-w / 2} ${-h / 2 + r}
          a ${w / 2} ${r} 0 0 0 ${w} 0
          a ${w / 2} ${r} 0 0 0 ${-w} 0
          L ${-w / 2} ${h / 2 - r}
          a ${w / 2} ${r} 0 0 0 ${w} 0
          L ${w / 2} ${-h / 2 + r}
        `;
        shape = el('path', { d: d.trim().replace(/\s+/g, ' ') });
        break;
      }
      case 'chain':
      default: {
        // Diamond
        const r = NODE_HALF;
        shape = el('polygon', {
          points: `0,${-r} ${r * 1.1},0 0,${r} ${-r * 1.1},0`,
        });
      }
    }
    shape.setAttribute('class', `node-shape node-shape-${kind}`);
    shape.setAttribute('fill', fill);
    shape.setAttribute('stroke', stroke);
    shape.setAttribute('stroke-width', strokeW);
    shape.setAttribute('opacity', opacity);
    return shape;
  }

  _truncate(s, n) {
    if (!s) return '';
    return s.length > n ? s.slice(0, n - 1) + '…' : s;
  }

  /* ------------------------------------------------------------- edges */

  _renderEdges() {
    this.edgesLayer.innerHTML = '';
    for (const e of this.edges) {
      const elGroup = this._makeEdgeElement(e);
      if (elGroup) this.edgesLayer.appendChild(elGroup);
    }
  }

  _makeEdgeElement(edge) {
    const src = this.nodesById.get(edge.source);
    const tgt = this.nodesById.get(edge.target);
    if (!src || !tgt) return null;

    // Trace mode: skip any topology edge whose endpoints didn't both
    // participate in the run. Otherwise we would draw an outline path from
    // an off-screen hidden node (-100000) to a visible node, which renders
    // as a long horizontal line before the START marker. That artifact is
    // exactly what the user reported as 'a line before start'.
    if (this.mode === 'trace') {
      const inRun = this.runNodeIds &&
        this.runNodeIds.has(edge.source) && this.runNodeIds.has(edge.target);
      if (!inRun) return null;
    }

    const isPath = this.runEdges && this.runEdges.has(edge.key);
    const inFocus = !!this.focusedNodeId;
    const focusInvolves = inFocus &&
      (edge.source === this.focusedNodeId || edge.target === this.focusedNodeId);
    const isFaded =
      (this.runEdges && !isPath) ||
      (inFocus && !focusInvolves);
    const focusedActive = inFocus && focusInvolves;

    // Compute attachment points: right edge of source, left edge of target.
    const x1 = src.x + NODE_HALF + 4;
    const y1 = src.y;
    const x2 = tgt.x - NODE_HALF - 4;
    const y2 = tgt.y;
    const dx = x2 - x1;

    // Cubic bezier with horizontal control points so the line flows L->R.
    const c1x = x1 + Math.max(40, dx * 0.45);
    const c1y = y1;
    const c2x = x2 - Math.max(40, dx * 0.45);
    const c2y = y2;
    const d = `M ${x1} ${y1} C ${c1x} ${c1y}, ${c2x} ${c2y}, ${x2} ${y2}`;

    const g = el('g', { class: 'edge-group', 'data-key': edge.key });

    let strokeColor = cssVar('--accent');
    let strokeW = Math.min(4 + Math.log2((edge.count || 1) + 1) * 0.8, 7);
    let dash = null;
    let opacity = 1;
    let markerId = 'at-arrow';

    // In trace mode, ALL run edges are "isPath" — but if we color them all
    // bright green, the current step is indistinguishable. Use a calmer
    // accent for the run-path edges in trace mode and reserve success-green
    // + step-active class for the manually selected current step.
    if (isPath && this.mode === 'trace') {
      strokeColor = cssVar('--accent');
      strokeW = 4.5;
      markerId = 'at-arrow';
    } else if (isPath) {
      strokeColor = cssVar('--success');
      strokeW = 7;
      markerId = 'at-arrow-path';
    } else if (focusedActive) {
      // Highlight: edges entering or leaving the focused node.
      strokeColor = cssVar('--accent');
      strokeW = 6;
      markerId = 'at-arrow-path';
      opacity = 1;
    } else if (isFaded) {
      strokeColor = cssVar('--text-dim');
      strokeW = 2;
      dash = '6 6';
      opacity = 0.25;
      markerId = 'at-arrow-faded';
    }

    // Outline (subtle wider line behind for legibility against the background).
    g.appendChild(el('path', {
      d,
      fill: 'none',
      stroke: cssVar('--bg'),
      'stroke-width': strokeW + 4,
      'stroke-linecap': 'round',
      opacity: 0.6,
    }));

    const path = el('path', {
      class: 'edge-path',
      d,
      fill: 'none',
      stroke: strokeColor,
      'stroke-width': strokeW,
      'stroke-linecap': 'round',
      'stroke-dasharray': dash,
      opacity,
      'marker-end': `url(#${markerId})`,
    });
    path.addEventListener('click', (e) => {
      e.stopPropagation();
      this.handlers.onEdgeClick && this.handlers.onEdgeClick({
        source: edge.source, target: edge.target,
        count: edge.count, lastSeen: edge.lastSeen,
      });
    });
    path.addEventListener('mouseenter', (ev) => this._showEdgeTooltip(edge, ev));
    path.addEventListener('mousemove', (ev) => this._moveTooltip(ev));
    path.addEventListener('mouseleave', () => this._hideTooltip());
    g.appendChild(path);

    // Count label at midpoint.
    if (edge.count && edge.count > 0) {
      const midX = (x1 + x2) / 2;
      const midY = (y1 + y2) / 2 - 10;
      g.appendChild(el('rect', {
        x: midX - 16, y: midY - 11,
        width: 32, height: 18,
        rx: 9, ry: 9,
        fill: cssVar('--surface'),
        stroke: cssVar('--border'),
      }));
      const txt = el('text', {
        x: midX, y: midY + 3,
        'text-anchor': 'middle',
        'font-size': 11,
        'font-weight': 700,
        fill: cssVar('--text'),
      });
      txt.textContent = `×${edge.count}`;
      g.appendChild(txt);
    }
    return g;
  }

  /* ----------------------------------------------------------- run trace */

  setRunTrace(nodeIds, traversedEdges = [], journey = null) {
    this.runNodeIds = new Set(nodeIds);
    this.runEdges = new Set(traversedEdges.map(e => `${e.source}->${e.target}`));
    this.runJourney = journey;
    // Determine first / last node for START / END markers.
    if (traversedEdges.length > 0) {
      this.runFirstNodeId = traversedEdges[0].source;
      this.runLastNodeId = traversedEdges[traversedEdges.length - 1].target;
    } else if (nodeIds.length > 0) {
      this.runFirstNodeId = nodeIds[0];
      this.runLastNodeId = nodeIds[nodeIds.length - 1];
    } else {
      this.runFirstNodeId = null;
      this.runLastNodeId = null;
    }
    // Switch to trace layout.
    this.mode = 'trace';
    this._layout();
    this._renderHeaders();
    this._renderEdges();
    this._renderNodes();
  }

  clearRunTrace() {
    this.runNodeIds = null;
    this.runEdges = null;
    this.runFirstNodeId = null;
    this.runLastNodeId = null;
    this.runJourney = null;
    // Back to topology layout.
    this.mode = 'topology';
    this._layout();
    this._renderHeaders();
    this._renderEdges();
    this._renderNodes();
  }

  /* ----------------------------------------------------------- pulses */

  pulseNode(nodeId) {
    const g = this.nodesLayer.querySelector(`[data-id="${cssEscape(nodeId)}"]`);
    if (!g) return;
    g.classList.remove('pulse');
    void g.getBBox(); // force reflow
    g.classList.add('pulse');
    setTimeout(() => g.classList.remove('pulse'), 700);
  }

  flashEdge(source, target) {
    const key = `${source}->${target}`;
    const g = this.edgesLayer.querySelector(`[data-key="${cssEscape(key)}"]`);
    if (!g) return;
    const path = g.querySelector('.edge-path');
    if (!path) return;
    const origStroke = path.getAttribute('stroke');
    const origWidth = path.getAttribute('stroke-width');
    path.setAttribute('stroke', cssVar('--success'));
    path.setAttribute('stroke-width', '8');
    setTimeout(() => {
      path.setAttribute('stroke', origStroke);
      path.setAttribute('stroke-width', origWidth);
    }, 600);

    // Animate a moving dot along the edge.
    this._animateDotAlongPath(path);
  }

  _animateDotAlongPath(path, durationMs = 700, reverse = false) {
    if (!path) return;
    const totalLen = path.getTotalLength?.();
    if (!totalLen) return;
    const dot = el('circle', {
      r: 9,
      fill: cssVar('--success'),
      'stroke': cssVar('--bg'),
      'stroke-width': 2.5,
      'filter': 'drop-shadow(0 0 10px var(--success))',
    });
    this.edgesLayer.appendChild(dot);
    const handle = { dot, cancelled: false };
    this.activeDots.push(handle);

    const start = performance.now();
    const dur = Math.max(150, durationMs);
    const step = (now) => {
      if (handle.cancelled) { try { dot.remove(); } catch { /* */ } return; }
      const t = Math.min(1, (now - start) / dur);
      const pos = reverse ? (1 - t) : t;
      const pt = path.getPointAtLength(pos * totalLen);
      dot.setAttribute('cx', pt.x);
      dot.setAttribute('cy', pt.y);
      if (t < 1) requestAnimationFrame(step);
      else {
        try { dot.remove(); } catch { /* */ }
        this.activeDots = this.activeDots.filter((h) => h !== handle);
      }
    };
    requestAnimationFrame(step);
  }

  /* ----------------------------------------------------------- replay */

  /** Cancel an in-flight playRun. Sets the cancellation flag the loop checks
   *  between steps and tears down any active dot/edge highlight so the
   *  user doesn't see ghost animations after they've switched to manual. */
  cancelReplay() {
    this._replayCancelled = true;
    this._cancelActiveDots();
    this.clearStepHighlight();
  }

  async playRun(edges, speed = 1) {
    // Slow enough at 1x for a human to follow every transition. 2000 ms per
    // step + 1400 ms dot animation. Faster speeds compress predictably.
    const stepDur = Math.max(500, 2000 / speed);
    const dotDur  = Math.max(350, 1400 / speed);
    const total = edges.length;
    this._replayCancelled = false;

    for (let i = 0; i < total; i++) {
      if (this._replayCancelled) return;
      const e = edges[i];
      const tgt = this.nodesById.get(e.target);
      const src = this.nodesById.get(e.source);

      // Tell the listener which step we're on so it can update a step counter
      // AND highlight the matching event card in the timeline (eventId carries
      // through from the journey-derived edges built by app.js).
      this.handlers.onPlayStep && this.handlers.onPlayStep({
        index: i,
        total,
        source: e.source,
        target: e.target,
        sourceLabel: src ? src.name : e.source,
        targetLabel: tgt ? tgt.name : e.target,
        eventId: e.eventId || null,
      });

      // Apply the step-active highlight class so the current edge stands out
      // from the rest of the run path.
      this.clearStepHighlight();
      const srcG = this.nodesLayer.querySelector(`[data-id="${cssEscape(e.source)}"]`);
      const tgtG = this.nodesLayer.querySelector(`[data-id="${cssEscape(e.target)}"]`);
      if (srcG) srcG.classList.add('step-source');
      if (tgtG) tgtG.classList.add('step-target');
      const edgeG = this.edgesLayer.querySelector(`[data-key="${cssEscape(e.source + '->' + e.target)}"]`);
      const path = edgeG ? edgeG.querySelector('.edge-path') : null;
      if (path) path.classList.add('step-active');

      // Slow visible dot.
      if (path) this._animateDotAlongPath(path, dotDur);

      // Wait for the dot to reach target.
      await new Promise(r => setTimeout(r, dotDur));
      if (this._replayCancelled) return;

      // Pulse the target node so the landing is dramatic.
      if (tgt) this.pulseNode(tgt.id);

      // Hold so the user can read the moment.
      await new Promise(r => setTimeout(r, Math.max(150, stepDur - dotDur)));
    }
    if (!this._replayCancelled) {
      this.handlers.onPlayDone && this.handlers.onPlayDone();
    }
  }

  /* ----------------------------------------------------------- tooltip */

  _showTooltip(n, ev) {
    if (!this.tooltipEl) return;
    const html = `
      <div class="tt-title"><strong>${escapeHtml(n.name)}</strong>
        <span class="tt-kind">${escapeHtml(n.type)}</span></div>
      <div class="tt-row">invocations: <b>${n.invocations}</b></div>
      ${n.errors ? `<div class="tt-row">errors: <b>${n.errors}</b></div>` : ''}
      ${n.avgMs != null ? `<div class="tt-row">avg: ${formatMs(n.avgMs)}</div>` : ''}
      ${n.p99Ms != null ? `<div class="tt-row">p99: ${formatMs(n.p99Ms)}</div>` : ''}
      <div class="tt-row tt-dim">${formatRelTime(n.lastSeen)}</div>
    `;
    this.tooltipEl.innerHTML = html;
    this.tooltipEl.classList.remove('hidden');
    this._moveTooltip(ev);
  }

  _showEdgeTooltip(edge, ev) {
    if (!this.tooltipEl) return;
    const html = `
      <div class="tt-title"><strong>${escapeHtml(edge.source)}</strong></div>
      <div class="tt-row">→ ${escapeHtml(edge.target)}</div>
      <div class="tt-row">traversals: <b>${edge.count}</b></div>
      <div class="tt-row tt-dim">${formatRelTime(edge.lastSeen)}</div>
    `;
    this.tooltipEl.innerHTML = html;
    this.tooltipEl.classList.remove('hidden');
    this._moveTooltip(ev);
  }

  _moveTooltip(ev) {
    if (!this.tooltipEl) return;
    const rect = this.container.getBoundingClientRect();
    const x = ev.clientX - rect.left + 14;
    const y = ev.clientY - rect.top + 14;
    this.tooltipEl.style.left = `${x}px`;
    this.tooltipEl.style.top = `${y}px`;
  }

  _hideTooltip() {
    if (!this.tooltipEl) return;
    this.tooltipEl.classList.add('hidden');
  }
}

/* =====================================================================
 * Helpers shared with app.js (preserved exports from previous module)
 * ===================================================================== */

function cssEscape(s) {
  if (window.CSS && CSS.escape) return CSS.escape(s);
  return String(s).replace(/[^a-zA-Z0-9_-]/g, (m) => `\\${m}`);
}

export function nodeIdForEvent(ev) {
  // Mirror the backend get_topology CASE: classify by event_type first, then
  // fall back to agent_name pattern (LCEL Runnable* + LangGraph -> chain) so
  // per-run path computation matches the topology node IDs.
  const t = ev.event_type || '';
  let kind = null;
  if (t === 'chat_model_start' || t === 'llm_start' || t === 'llm_end' || t === 'llm_error') {
    kind = 'llm';
  } else if (t === 'retriever_start' || t === 'retriever_end' || t === 'retriever_error') {
    kind = 'retriever';
  } else if (ev.tool_name) {
    kind = 'tool';
  } else if (ev.agent_name) {
    if (
      ev.agent_name.startsWith('Runnable') ||
      ev.agent_name === 'LangGraph' ||
      ev.agent_name === 'StrOutputParser' ||
      ev.agent_name.endsWith('OutputParser') ||
      ev.agent_name.endsWith('PromptTemplate')
    ) {
      kind = 'chain';
    } else {
      kind = 'agent';
    }
  }
  if (!kind) return null;
  const name = ev.tool_name || ev.agent_name;
  if (!name) return null;
  return `${kind}:${name}`;
}

export function escapeHtml(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

export function formatMs(ms) {
  if (ms == null || isNaN(ms)) return '—';
  if (ms < 1) return '<1ms';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(2)}s`;
  return `${(ms / 60_000).toFixed(1)}m`;
}

export function formatRelTime(iso) {
  if (!iso) return '—';
  const d = typeof iso === 'string' ? new Date(iso) : iso;
  if (isNaN(d.getTime())) return '—';
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 5) return 'just now';
  if (diff < 60) return `${Math.floor(diff)}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}
