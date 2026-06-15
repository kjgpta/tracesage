"""REST routes under `/api`. All routes are mounted on a single APIRouter.

Dependencies (`db`, `blob_store`, `config`, `stats`) come from
`request.app.state`, populated by `create_app`. Routes never accept
user-supplied filesystem paths — `blob_path` is always read from the DB row.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path as FsPath
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from tracelens.config import TraceLensConfig
from tracelens.models import Run, Stats, StoredEvent, Topology
from tracelens.storage.backend import StorageBackend
from tracelens.storage.blob_store import BlobStore

_LOG = logging.getLogger("tracelens.server.rest")

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


def get_config(request: Request) -> TraceLensConfig:
    return request.app.state.config


def get_stats(request: Request) -> Stats:
    return request.app.state.stats


# ---------- Response models ----------


class HealthResponse(BaseModel):
    status: str
    version: str


class RunListResponse(BaseModel):
    runs: list[Run]
    total: int
    limit: int
    offset: int


class JourneyResponse(BaseModel):
    run_id: str
    steps: list[StoredEvent]


class FullStepResponse(BaseModel):
    event_id: str
    run_id: str
    event_type: str
    full_payload: dict[str, Any]


class DeleteResponse(BaseModel):
    deleted: bool
    run_id: str


# ---------- Routes ----------


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    from tracelens import __version__

    return HealthResponse(status="ok", version=__version__)


@router.get("/runs", response_model=RunListResponse)
async def list_runs(
    db: Annotated[StorageBackend, Depends(get_db)],
    status: Annotated[str | None, Query(pattern="^(running|completed|failed|all)$")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RunListResponse:
    runs, total = await db.list_runs(status=status, limit=limit, offset=offset)
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
) -> JourneyResponse:
    run = await db.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    steps = await db.get_journey(run_id)
    return JourneyResponse(run_id=run_id, steps=steps)


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
            loop = asyncio.get_running_loop()
            merged["blob_size_bytes"] = await loop.run_in_executor(
                None, _scan_blob_size_bytes, blob_store.base_dir
            )
        except Exception as e:
            _LOG.debug("blob size scan failed: %s", e)
    return merged


@router.get("/topology", response_model=Topology)
async def topology_endpoint(
    db: Annotated[StorageBackend, Depends(get_db)],
) -> Topology:
    return await db.get_topology()


@router.get("/tools")
async def tools_endpoint(
    db: Annotated[StorageBackend, Depends(get_db)],
) -> dict[str, Any]:
    """Tools grouped by source: each MCP server plus a 'local' bucket for
    unattributed (hardcoded) tools."""
    return await db.get_tool_inventory()


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

    return StreamingResponse(
        lines(),
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": f'attachment; filename="{run_id}.jsonl"',
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
