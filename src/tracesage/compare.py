"""Side-by-side comparison of two runs. Shared by the CLI and the MCP server."""
from __future__ import annotations

from typing import Any

from tracesage.models import Run, StoredEvent
from tracesage.pricing import format_cost


def summarize_run(run: Run, events: list[StoredEvent]) -> dict[str, Any]:
    tools = sorted({e.tool_name for e in events if e.tool_name})
    models = sorted({e.model for e in events if e.model})
    errors = sum(1 for e in events if e.event_type.value.endswith("_error"))
    return {
        "status": run.status.value,
        "steps": run.total_steps,
        "tok_in": run.total_tokens_input,
        "tok_out": run.total_tokens_output,
        "cost": format_cost(run.total_cost_usd),
        "cost_usd": run.total_cost_usd,
        "tool_calls": sum(1 for e in events if e.event_type.value == "tool_start"),
        "tools": ", ".join(tools) or "—",
        "models": ", ".join(models) or "—",
        "errors": errors,
        "error": run.error_message,
    }


async def compare_runs(db: Any, run_a: str, run_b: str) -> dict[str, Any] | None:
    """Return ``{run_a, run_b, rows}`` or None when either run is missing."""
    left = await db.get_run(run_a)
    right = await db.get_run(run_b)
    if left is None or right is None:
        return None
    a = summarize_run(left, await db.get_journey(run_a))
    b = summarize_run(right, await db.get_journey(run_b))
    rows = []
    for key in ("status", "steps", "tok_in", "tok_out", "cost", "tool_calls", "tools", "models", "errors"):
        rows.append({"metric": key, "a": a[key], "b": b[key], "same": a[key] == b[key]})
    return {"run_a": run_a, "run_b": run_b, "a": a, "b": b, "rows": rows}
