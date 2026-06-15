"""Pure-stdlib renderers for a run's trace.

`render_run_tree` turns a Run + its events into an indented ASCII/Unicode tree for
terminal debugging (used by the `tracelens show` CLI). `TraceView` is a small
notebook helper whose `_repr_html_` embeds the live UI for a run.

No third-party dependencies and nothing here touches the DB — callers pass the Run
and the already-loaded event list.
"""
from __future__ import annotations

import html
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tracelens.models import Run, StoredEvent

# Kind glyphs mirror the UI's TYPE_ICONS so the terminal and browser agree.
_KIND_ICON = {
    "chain": "◇",      # ◇
    "agent": "⬡",      # ⬡
    "tool": "▭",       # ▭
    "llm": "◯",        # ◯
    "retriever": "⌭",  # ⌭
    "run": "•",        # •
    "retry": "↻",      # ↻
}

_ANSI = {
    "reset": "\033[0m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "cyan": "\033[36m",
    "bold": "\033[1m",
}


def _kind_of(event_type_value: str) -> str:
    et = event_type_value
    if et.startswith("chain"):
        return "chain"
    if et.startswith("agent"):
        return "agent"
    if et.startswith("tool"):
        return "tool"
    if et.startswith(("llm", "chat_model")):
        return "llm"
    if et.startswith("retriever"):
        return "retriever"
    if et.startswith("run"):
        return "run"
    if et == "retry":
        return "retry"
    return "chain"


def _fmt_ms(ms: int | float | None) -> str:
    if ms is None:
        return ""
    if ms < 1000:
        return f"{int(ms)}ms"
    return f"{ms / 1000:.2f}s"


class _Painter:
    def __init__(self, color: bool) -> None:
        self.color = color

    def __call__(self, text: str, *styles: str) -> str:
        if not self.color or not styles:
            return text
        prefix = "".join(_ANSI[s] for s in styles if s in _ANSI)
        return f"{prefix}{text}{_ANSI['reset']}"


def _build_nodes(events: list[StoredEvent]) -> dict[str, dict[str, Any]]:
    """Collapse the event stream into one node per run_id with derived attributes."""
    nodes: dict[str, dict[str, Any]] = {}
    for ev in events:
        rid = ev.run_id
        n = nodes.get(rid)
        if n is None:
            n = nodes[rid] = {
                "run_id": rid,
                "parent": ev.parent_run_id,
                "kind": _kind_of(ev.event_type.value),
                "name": None,
                "status": "running",
                "duration_ms": None,
                "tokens_in": None,
                "tokens_out": None,
                "error": None,
                "first_ts": ev.timestamp,
                "order": len(nodes),
            }
        et = ev.event_type.value
        name = ev.agent_name or ev.tool_name
        if name and not n["name"]:
            n["name"] = name
        if et.endswith("_start") or et in ("run_start",):
            n["kind"] = _kind_of(et)
        if et.endswith("_error"):
            n["status"] = "error"
            n["error"] = ev.error_message or ev.summary
        elif et.endswith(("_end", "_finish")) and n["status"] != "error":
            n["status"] = "ok"
        if ev.duration_ms is not None:
            n["duration_ms"] = ev.duration_ms
        if ev.token_input is not None:
            n["tokens_in"] = ev.token_input
        if ev.token_output is not None:
            n["tokens_out"] = ev.token_output
        if ev.parent_run_id and not n["parent"]:
            n["parent"] = ev.parent_run_id
        if ev.timestamp < n["first_ts"]:
            n["first_ts"] = ev.timestamp
    return nodes


def render_run_tree(
    run: Run | None,
    events: list[StoredEvent],
    *,
    use_color: bool | None = None,
    max_name: int = 48,
) -> str:
    """Render a run's events as an indented tree string."""
    if use_color is None:
        use_color = sys.stdout.isatty()
    paint = _Painter(use_color)

    if not events:
        return paint("(no events for this run)", "dim")

    nodes = _build_nodes(events)
    ids = set(nodes)
    children: dict[str | None, list[str]] = {}
    roots: list[str] = []
    for rid, n in nodes.items():
        parent = n["parent"]
        if parent is None or parent not in ids:
            roots.append(rid)
        else:
            children.setdefault(parent, []).append(rid)

    def _sort(rids: list[str]) -> list[str]:
        return sorted(rids, key=lambda r: (nodes[r]["first_ts"], nodes[r]["order"]))

    lines: list[str] = []

    if run is not None:
        status = run.status.value if hasattr(run.status, "value") else str(run.status)
        scolor = {"completed": "green", "failed": "red", "running": "yellow"}.get(status, "cyan")
        head = (
            f"Run {run.run_id}  "
            + paint(status, scolor, "bold")
            + paint(
                f"  · {run.total_steps} steps · "
                f"{run.total_tokens_input + run.total_tokens_output} tokens",
                "dim",
            )
        )
        lines.append(head)

    def walk(rid: str, prefix: str, is_last: bool, is_root: bool) -> None:
        n = nodes[rid]
        if is_root:
            connector = ""
            child_prefix = prefix
        else:
            connector = "└─ " if is_last else "├─ "  # └─ / ├─
            child_prefix = prefix + ("   " if is_last else "│  ")  # │
        icon = _KIND_ICON.get(n["kind"], "•")
        name = n["name"] or n["kind"]
        if len(name) > max_name:
            name = name[: max_name - 1] + "…"
        label = f"{icon} {paint(n['kind'], 'cyan')} {name}"
        meta_bits = []
        dur = _fmt_ms(n["duration_ms"])
        if dur:
            meta_bits.append(dur)
        if n["tokens_in"] is not None or n["tokens_out"] is not None:
            meta_bits.append(f"↑{n['tokens_in'] or 0}/↓{n['tokens_out'] or 0}")
        meta = paint("  " + " ".join(meta_bits), "dim") if meta_bits else ""
        status_mark = ""
        if n["status"] == "error":
            err = (n["error"] or "error").splitlines()[0][:80]
            status_mark = paint(f"  ✗ {err}", "red")
        elif n["status"] == "running":
            status_mark = paint("  …", "yellow")
        lines.append(f"{prefix}{connector}{label}{meta}{status_mark}")

        kids = _sort(children.get(rid, []))
        for i, kid in enumerate(kids):
            walk(kid, child_prefix, i == len(kids) - 1, is_root=False)

    sorted_roots = _sort(roots)
    for i, rid in enumerate(sorted_roots):
        walk(rid, "", i == len(sorted_roots) - 1, is_root=True)

    return "\n".join(lines)


class TraceView:
    """Notebook helper: rich-displays a run by embedding the live UI in an iframe.

    Returned by ``TraceLens.run_view(run_id)``. In a Jupyter cell it renders the
    interactive trace; outside a notebook it just shows the URL.
    """

    def __init__(self, run_id: str, url: str | None, *, height: int = 600) -> None:
        self.run_id = run_id
        self.url = url
        self.height = height

    def __repr__(self) -> str:
        if self.url:
            return f"<TraceView run={self.run_id} {self.url}>"
        return f"<TraceView run={self.run_id} (no server URL available)>"

    def _repr_html_(self) -> str:
        if not self.url:
            return (
                f"<div><b>tracelens</b>: no UI URL for run "
                f"<code>{html.escape(self.run_id)}</code> "
                "(start with an embedded server or set <code>public_url</code>).</div>"
            )
        safe = html.escape(self.url, quote=True)
        return (
            f'<div style="border:1px solid #ddd;border-radius:8px;overflow:hidden">'
            f'<div style="font:12px monospace;padding:4px 8px;background:#f5f5f5">'
            f'\U0001f50d tracelens · run {html.escape(self.run_id)} · '
            f'<a href="{safe}" target="_blank">open in new tab</a></div>'
            f'<iframe src="{safe}" width="100%" height="{self.height}" '
            f'style="border:0"></iframe></div>'
        )
