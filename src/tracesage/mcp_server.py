"""MCP server so a coding agent can read and annotate local traces.

The ``mcp`` package is imported lazily. ``tracesage mcp`` exits with an install
hint when the ``[apps]`` extra is missing, before it opens the database.
"""
from __future__ import annotations

import json
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

log = logging.getLogger("tracesage.mcp")

INSTRUCTIONS = (
    "tracesage stores local traces of LLM agent runs. "
    "To debug a failure: call list_errors, then get_run_spans for the run, "
    "then get_step for the failing step. search finds text in prompts and tool "
    "output. list_threads and get_thread show a conversation. "
    "add_note and set_tags record what you found. delete_run removes a run and "
    "is off unless the server was started with --allow-delete."
)


def mcp_installed() -> bool:
    try:
        import mcp.server.fastmcp  # noqa: F401
    except ImportError:
        return False
    return True


def _clip(value: Any, max_chars: int) -> Any:
    try:
        text = json.dumps(value, default=str)
    except Exception:
        text = str(value)
    if len(text) <= max_chars:
        return value
    return {"truncated": True, "max_chars": max_chars, "preview": text[:max_chars]}


def build_server(data_dir: Path, *, read_only: bool = False, allow_delete: bool = False) -> Any:
    """A FastMCP server bound to ``data_dir``. Imports ``mcp``."""
    from mcp.server.fastmcp import FastMCP

    from tracesage.storage.blob_store import BlobStore
    from tracesage.storage.sqlite_backend import SQLiteBackend

    mcp = FastMCP("tracesage", instructions=INSTRUCTIONS)
    data_dir = Path(data_dir)

    @asynccontextmanager
    async def _open():
        db = SQLiteBackend(data_dir / "traces.db")
        blob = BlobStore(data_dir / "blobs")
        await db.init()
        try:
            yield db, blob
        finally:
            await db.close()

    def _refuse_write() -> dict[str, str] | None:
        if read_only:
            return {"error": "server is read-only"}
        return None

    @mcp.tool()
    async def list_runs(
        status: str | None = None,
        q: str | None = None,
        thread_id: str | None = None,
        tag: str | None = None,
        limit: int = 20,
    ) -> Any:
        """List root runs, newest first."""
        async with _open() as (db, _blob):
            runs, total = await db.list_runs(
                status=status, q=q, thread_id=thread_id, tag=tag, limit=min(limit, 100)
            )
            return _clip(
                {"total": total, "runs": [r.model_dump(mode="json") for r in runs]},
                4000,
            )

    @mcp.tool()
    async def get_run(run_id: str) -> Any:
        """Status, tokens, cost, tools, error and notes for one run."""
        async with _open() as (db, _blob):
            run = await db.get_run(run_id)
            if run is None:
                return {"error": "run not found"}
            events = await db.get_journey(run_id)
            notes = await db.list_notes(run_id)
            tools = sorted({e.tool_name for e in events if e.tool_name})
            models = sorted({e.model for e in events if e.model})
            return _clip(
                {
                    "run": run.model_dump(mode="json"),
                    "tools": tools,
                    "models": models,
                    "step_count": len(events),
                    "notes": [n.model_dump(mode="json") for n in notes],
                },
                4000,
            )

    @mcp.tool()
    async def get_run_spans(run_id: str) -> Any:
        """Latency waterfall for one run."""
        from tracesage.spans import build_spans

        async with _open() as (db, _blob):
            run = await db.get_run(run_id)
            if run is None:
                return {"error": "run not found"}
            events = await db.get_journey(run_id)
            rows = build_spans(events, running=run.status.value == "running")
            return _clip([row.model_dump(mode="json") for row in rows], 8000)

    @mcp.tool()
    async def get_step(
        event_id: str, include_payload: bool = False, max_chars: int = 4000
    ) -> Any:
        """One step. Payload is included only when include_payload is true."""
        async with _open() as (db, blob):
            event = await db.get_event(event_id)
            if event is None:
                return {"error": "step not found"}
            body = event.model_dump(mode="json")
            if include_payload and event.blob_path:
                try:
                    body["payload"] = await blob.read(event.blob_path)
                except Exception as exc:
                    body["payload_error"] = str(exc)
            return _clip(body, max(200, min(max_chars, 20000)))

    @mcp.tool()
    async def search(q: str, run_id: str | None = None, limit: int = 20) -> Any:
        """Full-text search over summaries and payload text."""
        async with _open() as (db, _blob):
            hits = await db.search_events(q, limit=min(limit, 50), run_id=run_id)
            return _clip([h.model_dump(mode="json") for h in hits], 4000)

    @mcp.tool()
    async def list_errors(limit: int = 20, since: str | None = None) -> Any:
        """Recent failed runs. ``since`` is an ISO timestamp; runs started before it are skipped."""
        async with _open() as (db, _blob):
            runs, _total = await db.list_runs(status="failed", limit=min(limit, 100))
            if since:
                runs = [r for r in runs if r.started_at.isoformat() >= since]
            return _clip(
                [
                    {
                        "run_id": r.run_id,
                        "started_at": r.started_at.isoformat(),
                        "error": r.error_message,
                    }
                    for r in runs
                ],
                4000,
            )

    @mcp.tool()
    async def list_threads(limit: int = 20) -> Any:
        """Conversation threads, most recently active first."""
        async with _open() as (db, _blob):
            threads, total = await db.list_threads(limit=min(limit, 100))
            return _clip(
                {"total": total, "threads": [t.model_dump(mode="json") for t in threads]},
                4000,
            )

    @mcp.tool()
    async def get_thread(thread_id: str) -> Any:
        """Turns of one conversation."""
        from tracesage.conversation import build_turns

        async with _open() as (db, blob):
            runs = await db.get_thread_runs(thread_id)
            if not runs:
                return {"error": "thread not found"}
            events = []
            payloads: dict[str, Any] = {}
            for run in runs:
                journey = await db.get_journey(run.run_id)
                events.extend(journey)
                for event in journey:
                    if event.blob_path:
                        try:
                            payloads[event.event_id] = await blob.read(event.blob_path)
                        except Exception as exc:
                            log.debug("thread blob skipped for %s: %s", event.event_id, exc)
            turns = build_turns(runs, events, payloads)
            return _clip([t.model_dump(mode="json") for t in turns], 8000)

    @mcp.tool()
    async def get_stats() -> Any:
        """Aggregate counts, tokens and cost."""
        async with _open() as (db, _blob):
            return await db.get_stats()

    @mcp.tool()
    async def get_topology(scope: str | None = None) -> Any:
        """Agent topology. scope is all, run:<id>, or last_n:<N>."""
        async with _open() as (db, _blob):
            topo = await db.get_topology(scope)
            return _clip(topo.model_dump(mode="json"), 8000)

    @mcp.tool()
    async def compare_runs(run_a: str, run_b: str) -> Any:
        """Compare two runs."""
        from tracesage.compare import compare_runs as _compare

        async with _open() as (db, _blob):
            result = await _compare(db, run_a, run_b)
            if result is None:
                return {"error": "run not found"}
            return _clip(result, 4000)

    @mcp.tool()
    async def add_note(run_id: str, text: str) -> Any:
        """Attach a note to a run."""
        refused = _refuse_write()
        if refused:
            return refused
        async with _open() as (db, _blob):
            if await db.get_run(run_id) is None:
                return {"error": "run not found"}
            note = await db.add_note(run_id, text, author="mcp")
            print(f"tracesage mcp: add_note {run_id}", file=sys.stderr)
            return note.model_dump(mode="json")

    @mcp.tool()
    async def set_tags(run_id: str, add: list[str] | None = None, remove: list[str] | None = None) -> Any:
        """Add or remove tags on a run."""
        refused = _refuse_write()
        if refused:
            return refused
        async with _open() as (db, _blob):
            tags = await db.set_run_tags(run_id, add=add or [], remove=remove or [])
            if tags is None:
                return {"error": "run not found"}
            print(f"tracesage mcp: set_tags {run_id}", file=sys.stderr)
            return {"run_id": run_id, "tags": tags}

    @mcp.tool()
    async def delete_run(run_id: str, confirm: bool = False) -> Any:
        """Delete a run. Requires confirm=true and the server flag --allow-delete."""
        refused = _refuse_write()
        if refused:
            return refused
        if not allow_delete:
            return {"error": "delete_run requires --allow-delete"}
        if not confirm:
            return {"error": "pass confirm=true to delete"}
        async with _open() as (db, blob):
            if await db.get_run(run_id) is None:
                return {"error": "run not found"}
            await db.delete_run(run_id)
            try:
                await blob.delete_run(run_id)
            except Exception as exc:
                log.warning("blob delete failed for %s: %s", run_id, exc)
            print(f"tracesage mcp: delete_run {run_id}", file=sys.stderr)
            return {"deleted": True, "run_id": run_id}

    @mcp.prompt()
    def debug_failed_run() -> str:
        """How to investigate the most recent failed run."""
        return (
            "Call list_errors with limit 5. For the first run, call get_run_spans, "
            "then get_step on the span whose status is error, with include_payload true. "
            "Summarize the failing step and, if it helps, add_note with the cause."
        )

    return mcp


def serve(data_dir: Path, *, read_only: bool = False, allow_delete: bool = False) -> None:
    """Run the server on stdio. Caller has already checked that ``mcp`` imports."""
    server = build_server(data_dir, read_only=read_only, allow_delete=allow_delete)
    server.run(transport="stdio")
