"""REST routes under `/api`. All routes are mounted on a single APIRouter.

Dependencies (`db`, `blob_store`, `config`, `stats`) come from
`request.app.state`, populated by `create_app`. Routes never accept
user-supplied filesystem paths — `blob_path` is always read from the DB row.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from pathlib import Path as FsPath
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from tracesage.config import TraceSageConfig
from tracesage.conversation import Turn, build_turns
from tracesage.models import Run, RunNote, SearchHit, Stats, StoredEvent, ThreadSummary, Topology
from tracesage.spans import SpanRow, build_spans
from tracesage.storage.backend import StorageBackend
from tracesage.storage.blob_store import BlobStore

_LOG = logging.getLogger("tracesage.server.rest")

router = APIRouter(prefix="/api")


def _scan_blob_size_bytes(base_dir: FsPath) -> int:
    total = 0
    for p in base_dir.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                continue
    return total


# ---------- Dependency helpers ----------


def get_db(request: Request) -> StorageBackend:
    return request.app.state.db


def get_blob_store(request: Request) -> BlobStore:
    return request.app.state.blob_store


def get_config(request: Request) -> TraceSageConfig:
    return request.app.state.config


def get_stats(request: Request) -> Stats:
    return request.app.state.stats


# ---------- Response models ----------


class HealthResponse(BaseModel):
    status: str
    version: str
    project_name: str | None = None


class RunListResponse(BaseModel):
    runs: list[Run]
    total: int
    limit: int
    offset: int


class JourneyResponse(BaseModel):
    run_id: str
    steps: list[StoredEvent]
    # True when only the most recent `limit` steps were returned; use the
    # streaming /export endpoint for the complete journey.
    truncated: bool = False


# Hard ceiling on steps materialized per journey/catchup response. A run can hold
# up to per_run_event_cap events; loading all of them per request pins memory and
# stalls the loop for every other client.
JOURNEY_MAX_STEPS = 10_000


class FullStepResponse(BaseModel):
    event_id: str
    run_id: str
    event_type: str
    full_payload: dict[str, Any]


class DeleteResponse(BaseModel):
    deleted: bool
    run_id: str


class SearchResponse(BaseModel):
    hits: list[SearchHit]


class ThreadListResponse(BaseModel):
    threads: list[ThreadSummary]
    total: int
    limit: int
    offset: int


class ThreadDetailResponse(BaseModel):
    thread_id: str
    runs: list[Run]
    turns: list[Turn]


class NoteCreate(BaseModel):
    text: str
    author: str | None = None


class TagPatch(BaseModel):
    add: list[str] = []
    remove: list[str] = []


class TagResponse(BaseModel):
    run_id: str
    tags: list[str]


# ---------- Routes ----------


@router.get("/health", response_model=HealthResponse)
async def health(
    config: Annotated[TraceSageConfig, Depends(get_config)],
) -> HealthResponse:
    from tracesage import __version__

    return HealthResponse(
        status="ok", version=__version__, project_name=config.project_name
    )


@router.get("/runs", response_model=RunListResponse)
async def list_runs(
    db: Annotated[StorageBackend, Depends(get_db)],
    status: Annotated[
        str | None, Query(pattern="^(running|completed|failed|interrupted|all)$")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    tag: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    thread_id: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    q: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    model: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    tool: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    errors_only: Annotated[bool, Query()] = False,
    sort: Annotated[str, Query(pattern="^(recent|oldest|steps|tokens|cost)$")] = "recent",
    no_source: Annotated[bool, Query()] = False,
) -> RunListResponse:
    runs, total = await db.list_runs(
        sort=sort,
        no_source=no_source,
        status=status,
        limit=limit,
        offset=offset,
        tag=tag,
        thread_id=thread_id,
        q=q,
        model=model,
        tool=tool,
        errors_only=errors_only,
    )
    return RunListResponse(runs=runs, total=total, limit=limit, offset=offset)


@router.get("/runs/{run_id}", response_model=Run)
async def get_run(
    db: Annotated[StorageBackend, Depends(get_db)],
    run_id: Annotated[str, Path(min_length=1)],
) -> Run:
    run = await db.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@router.get("/runs/{run_id}/journey", response_model=JourneyResponse)
async def get_journey(
    db: Annotated[StorageBackend, Depends(get_db)],
    run_id: Annotated[str, Path(min_length=1)],
    limit: Annotated[int, Query(ge=1, le=JOURNEY_MAX_STEPS)] = JOURNEY_MAX_STEPS,
) -> JourneyResponse:
    run = await db.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    steps = await db.get_journey(run_id, limit=limit + 1)
    truncated = len(steps) > limit
    return JourneyResponse(run_id=run_id, steps=steps[-limit:], truncated=truncated)


@router.get("/runs/{run_id}/steps/{event_id}/full", response_model=FullStepResponse)
async def get_full_step(
    db: Annotated[StorageBackend, Depends(get_db)],
    blob_store: Annotated[BlobStore, Depends(get_blob_store)],
    run_id: Annotated[str, Path(min_length=1)],
    event_id: Annotated[str, Path(min_length=1)],
) -> FullStepResponse:
    event = await db.get_event(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Step not found")
    if event.run_id != run_id and event.root_run_id != run_id:
        raise HTTPException(status_code=404, detail="Step not found for this run")
    if not event.blob_path:
        raise HTTPException(
            status_code=404,
            detail="Step has no full blob (not a blob-eligible event)",
        )
    try:
        full_payload = await blob_store.read(event.blob_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Blob not found for this step") from exc
    except ValueError as exc:
        # path-traversal guard tripped — treat as 404 to avoid leaking layout
        _LOG.warning("blob_path rejected by store: %s", exc)
        raise HTTPException(status_code=404, detail="Blob not found for this step") from exc

    return FullStepResponse(
        event_id=event.event_id,
        run_id=event.run_id,
        event_type=event.event_type.value,
        full_payload=full_payload,
    )


@router.get("/stats")
async def stats_endpoint(
    db: Annotated[StorageBackend, Depends(get_db)],
    runtime_stats: Annotated[Stats, Depends(get_stats)],
    blob_store: Annotated[BlobStore, Depends(get_blob_store)],
) -> dict[str, Any]:
    db_stats = await db.get_stats()
    merged: dict[str, Any] = {**db_stats}
    merged.update(runtime_stats.model_dump())
    # runtime Stats.db_size_bytes is never populated (defaults to 0) and the merge
    # above would clobber the DB-computed value; restore it from db_stats.
    if not merged.get("db_size_bytes"):
        merged["db_size_bytes"] = db_stats.get("db_size_bytes", 0)
    # blob_size_bytes from runtime stats may be stale; fill from blob_store if 0.
    if not merged.get("blob_size_bytes"):
        try:
            merged["blob_size_bytes"] = await _cached_blob_size(blob_store.base_dir)
        except Exception as e:
            _LOG.debug("blob size scan failed: %s", e)
    return merged


# The UI polls /stats every few seconds per tab. Walking a large blob tree that
# often competes with the worker's writes, so a scan is reused for a minute.
_BLOB_SIZE_TTL = 60.0
_blob_size_cache: dict[str, tuple[float, int]] = {}


async def _cached_blob_size(base_dir: FsPath) -> int:
    loop = asyncio.get_running_loop()
    key = str(base_dir)
    cached = _blob_size_cache.get(key)
    now = loop.time()
    if cached is not None and now - cached[0] < _BLOB_SIZE_TTL:
        return cached[1]
    size = await loop.run_in_executor(None, _scan_blob_size_bytes, base_dir)
    _blob_size_cache[key] = (now, size)
    return size


# Scope for topology/tools: all-time, a single run, or the N most-recent runs.
_SCOPE_PATTERN = r"^(all|run:.+|thread:.+|last_n:[0-9]+)$"


@router.get("/topology", response_model=Topology)
async def topology_endpoint(
    db: Annotated[StorageBackend, Depends(get_db)],
    scope: Annotated[str | None, Query(pattern=_SCOPE_PATTERN, max_length=200)] = None,
) -> Topology:
    return await db.get_topology(scope=scope)


@router.get("/tools")
async def tools_endpoint(
    db: Annotated[StorageBackend, Depends(get_db)],
    scope: Annotated[str | None, Query(pattern=_SCOPE_PATTERN, max_length=200)] = None,
) -> dict[str, Any]:
    """Tools grouped by source: each MCP server plus a 'local' bucket for
    unattributed (hardcoded) tools."""
    return await db.get_tool_inventory(scope=scope)


@router.get("/runs/{run_id}/export")
async def export_run(
    db: Annotated[StorageBackend, Depends(get_db)],
    run_id: Annotated[str, Path(min_length=1)],
    format: Annotated[str, Query(pattern="^jsonl$")] = "jsonl",
) -> StreamingResponse:
    run = await db.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    async def lines() -> AsyncIterator[bytes]:
        # First line: the Run itself.
        yield (run.model_dump_json() + "\n").encode("utf-8")
        # Stream events lazily so the full journey is never materialized. The
        # 200 status + headers are already committed once the body starts, so a
        # mid-stream failure cannot become a 5xx — instead we emit a terminal
        # error record so consumers can distinguish a complete export from a
        # truncated one (a clean cutoff would otherwise look complete).
        try:
            async for ev in db.iter_journey(run_id):
                yield (ev.model_dump_json() + "\n").encode("utf-8")
        except Exception as e:
            _LOG.warning("export stream failed mid-run for %s: %s", run_id, e)
            marker = json.dumps({"_kind": "error", "detail": "export truncated"})
            yield (marker + "\n").encode("utf-8")

    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", run_id)[:200] or "run"
    return StreamingResponse(
        lines(),
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}.jsonl"',
        },
    )


@router.delete("/runs/{run_id}", response_model=DeleteResponse)
async def delete_run(
    db: Annotated[StorageBackend, Depends(get_db)],
    blob_store: Annotated[BlobStore, Depends(get_blob_store)],
    run_id: Annotated[str, Path(min_length=1)],
) -> DeleteResponse:
    run = await db.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    await db.delete_run(run_id)
    try:
        await blob_store.delete_run(run_id)
    except Exception as e:
        _LOG.warning("blob delete failed for run %s: %s", run_id, e)
    return DeleteResponse(deleted=True, run_id=run_id)


@router.get("/search", response_model=SearchResponse)
async def search(
    db: Annotated[StorageBackend, Depends(get_db)],
    q: Annotated[str, Query(min_length=1, max_length=200)],
    run_id: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> SearchResponse:
    hits = await db.search_events(q, limit=limit, run_id=run_id)
    return SearchResponse(hits=hits)


@router.get("/runs/{run_id}/spans", response_model=list[SpanRow])
async def get_spans(
    db: Annotated[StorageBackend, Depends(get_db)],
    run_id: Annotated[str, Path(min_length=1)],
) -> list[SpanRow]:
    run = await db.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    events = await db.get_journey(run_id)
    return build_spans(events, running=run.status.value == "running")


@router.get("/runs/{run_id}/notes", response_model=list[RunNote])
async def get_notes(
    db: Annotated[StorageBackend, Depends(get_db)],
    run_id: Annotated[str, Path(min_length=1)],
) -> list[RunNote]:
    if await db.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return await db.list_notes(run_id)


@router.post("/runs/{run_id}/notes", response_model=RunNote)
async def post_note(
    db: Annotated[StorageBackend, Depends(get_db)],
    run_id: Annotated[str, Path(min_length=1)],
    body: NoteCreate,
) -> RunNote:
    if await db.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="Run not found")
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Note text is empty")
    return await db.add_note(run_id, text, author=body.author)


@router.patch("/runs/{run_id}/tags", response_model=TagResponse)
async def patch_tags(
    db: Annotated[StorageBackend, Depends(get_db)],
    run_id: Annotated[str, Path(min_length=1)],
    body: TagPatch,
) -> TagResponse:
    tags = await db.set_run_tags(run_id, add=body.add, remove=body.remove)
    if tags is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return TagResponse(run_id=run_id, tags=tags)


@router.get("/threads", response_model=ThreadListResponse)
async def list_threads(
    db: Annotated[StorageBackend, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    q: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    tag: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    sort: Annotated[str, Query(pattern="^(recent|oldest|turns|tokens)$")] = "recent",
    min_turns: Annotated[int, Query(ge=1, le=10_000)] = 1,
    no_source: Annotated[bool, Query()] = False,
) -> ThreadListResponse:
    threads, total = await db.list_threads(
        limit=limit,
        offset=offset,
        q=q,
        tag=tag,
        sort=sort,
        min_turns=min_turns,
        no_source=no_source,
    )
    return ThreadListResponse(threads=threads, total=total, limit=limit, offset=offset)


@router.get("/threads/{thread_id}", response_model=ThreadDetailResponse)
async def get_thread(
    db: Annotated[StorageBackend, Depends(get_db)],
    blob_store: Annotated[BlobStore, Depends(get_blob_store)],
    thread_id: Annotated[str, Path(min_length=1, max_length=200)],
    run: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    last: Annotated[int | None, Query(ge=1, le=500)] = None,
) -> ThreadDetailResponse:
    """A thread's turns; ``run`` narrows to one turn, ``last`` to the latest N."""
    runs = await db.get_thread_runs(thread_id)
    if not runs:
        raise HTTPException(status_code=404, detail="Thread not found")
    if run is not None:
        runs = [r for r in runs if r.run_id == run]
        if not runs:
            raise HTTPException(status_code=404, detail="Run not in thread")
    elif last is not None:
        runs = runs[-last:]
    events: list[StoredEvent] = []
    payloads: dict[str, Any] = {}
    for run in runs:
        journey = await db.get_journey(run.run_id)
        events.extend(journey)
        for event in journey:
            if not event.blob_path:
                continue
            try:
                payloads[event.event_id] = await blob_store.read(event.blob_path)
            except (FileNotFoundError, ValueError):
                continue
    return ThreadDetailResponse(
        thread_id=thread_id,
        runs=runs,
        turns=build_turns(runs, events, payloads),
    )
