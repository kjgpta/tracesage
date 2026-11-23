/* Latency waterfall. HTML bars, so a long run scrolls instead of redrawing SVG. */

import { escapeHtml, formatMs } from './graph.js';

const KIND_CLASS = {
  agent: 'kind-agent',
  llm: 'kind-llm',
  tool: 'kind-tool',
  retriever: 'kind-retriever',
  chain: 'kind-chain',
  mcp: 'kind-mcp',
};

export function renderWaterfall(container, spans, onSelect) {
  if (!container) return;
  const collapsed = container._collapsed || (container._collapsed = new Set());
  if (!spans || spans.length === 0) {
    container.innerHTML = '<div class="waterfall-empty">No spans in this run.</div>';
    return;
  }
  const hidden = new Set();
  const byId = new Map(spans.map((s) => [s.run_id, s]));
  function hideUnder(id) {
    const row = byId.get(id);
    if (!row) return;
    for (const child of row.children || []) {
      hidden.add(child);
      hideUnder(child);
    }
  }
  for (const id of collapsed) hideUnder(id);
  const visible = spans.filter((s) => !hidden.has(s.run_id));
  const span = Math.max(...visible.map((s) => s.end_ms), 1);
  const ticks = 4;
  const axis = [];
  for (let i = 0; i <= ticks; i++) {
    axis.push(`<span style="left:${(i / ticks) * 100}%">${formatMs(Math.round(span * i / ticks))}</span>`);
  }
  const rows = visible.map((row) => {
    const left = (row.start_ms / span) * 100;
    const width = Math.max(0.8, (row.duration_ms / span) * 100);
    const kind = KIND_CLASS[row.kind] || 'kind-chain';
    const status = row.status === 'error' ? 'is-error' : row.status === 'interrupted' ? 'is-paused' : '';
    const cost = row.cost_usd == null ? '' : ` · $${Number(row.cost_usd).toFixed(4)}`;
    const tokens = row.tokens_in == null && row.tokens_out == null
      ? ''
      : ` · ↑${row.tokens_in || 0} ↓${row.tokens_out || 0}`;
    const hasKids = (row.children || []).length > 0;
    const mark = hasKids ? (collapsed.has(row.run_id) ? '▸' : '▾') : '';
    return `
      <div class="wf-row ${status}" data-run="${escapeHtml(row.run_id)}" data-event="${escapeHtml(row.event_id)}" style="padding-left:${row.depth * 14}px">
        <button class="wf-twist" type="button" data-toggle="${escapeHtml(row.run_id)}" ${hasKids ? '' : 'disabled'}>${mark}</button>
        <span class="wf-name" title="${escapeHtml(row.name)}">${escapeHtml(row.name)}</span>
        <span class="wf-track">
          <span class="wf-bar ${kind} ${status}" style="left:${left}%;width:${width}%" title="${formatMs(row.duration_ms)}${tokens}${cost}"></span>
        </span>
        <span class="wf-meta">${formatMs(row.duration_ms)}${cost}</span>
      </div>`;
  }).join('');
  container.innerHTML = `<div class="wf-axis">${axis.join('')}</div><div class="wf-rows">${rows}</div>`;
  container.querySelectorAll('.wf-twist').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const id = btn.dataset.toggle;
      if (!id) return;
      if (collapsed.has(id)) collapsed.delete(id);
      else collapsed.add(id);
      renderWaterfall(container, spans, onSelect);
    });
  });
  container.querySelectorAll('.wf-row').forEach((row) => {
    row.addEventListener('click', () => {
      const eventId = row.dataset.event;
      if (eventId && onSelect) onSelect(eventId);
    });
  });
}
