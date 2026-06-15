"""Filesystem blob store. Gzipped JSON per (run_id, event_id).

Storage layout:
    {base_dir}/{run_id}/{event_id}.json.gz

Path-traversal guarded on both write and read.
"""
from __future__ import annotations

import asyncio
import contextlib
import gzip
import json
import os
import shutil
import uuid
from functools import partial
from pathlib import Path
from typing import Any

import aiofiles


def _safe_serialize(payload: Any) -> str:
    """Serialize to JSON, falling back to str() for non-serializable objects.

    `default=str` handles datetimes, Pydantic models, custom classes, etc.
    """
    return json.dumps(payload, default=str)


def _reject_unsafe_id(name: str, kind: str) -> None:
    """Reject ids that could escape base_dir before they touch the filesystem.

    After this passes, the id is a single clean path component — no separators
    (`/` or `\\`), no parent/cur refs, no drive/ADS colon, no NUL — so
    ``base_dir / run_id / <event_id>.json.gz`` is provably under base_dir on
    every OS, with no need for a resolve()-based check (which is unreliable on
    Windows for a path that doesn't exist yet).
    """
    if (
        not name
        or name in (".", "..")
        or "/" in name
        or "\\" in name
        or ":" in name      # Windows drive letter / NTFS alternate-data-stream
        or "\x00" in name
    ):
        raise ValueError(f"unsafe {kind} for blob path: {name!r}")


def _write_blob_sync(base_dir: Path, run_id: str, event_id: str, payload: dict) -> str:
    # run_id/event_id are validated as clean single components, so the join below
    # can never escape base_dir — no resolve()-based traversal check on write (it
    # false-positives on Windows for a not-yet-created path). read() still guards
    # its caller-supplied blob_path, which legitimately contains a separator.
    _reject_unsafe_id(run_id, "run_id")
    _reject_unsafe_id(event_id, "event_id")
    run_dir = base_dir / run_id
    final_path = run_dir / f"{event_id}.json.gz"
    run_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = run_dir / f"{event_id}.json.gz.{uuid.uuid4().hex}.tmp"
    data = gzip.compress(_safe_serialize(payload).encode("utf-8"))
    try:
        with open(tmp_path, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, final_path)  # atomic on the same filesystem
    finally:
        # On success os.replace consumed tmp_path (no-op here). On any failure
        # (full disk, EIO, replace error) remove the orphan temp file so it can't
        # accumulate on disk or inflate blob-size accounting. Exception still propagates.
        with contextlib.suppress(OSError):
            if tmp_path.exists():
                tmp_path.unlink()
    return f"{run_id}/{event_id}.json.gz"


def _dir_size_bytes(base: Path) -> int:
    total = 0
    for p in base.rglob("*"):
        # Skip in-flight / orphaned temp files: they are transient and must not
        # inflate size accounting (which drives gc --max-blob-size-gb retention).
        if p.is_file() and not p.name.endswith(".tmp"):
            try:
                total += p.stat().st_size
            except OSError:
                continue
    return total


class BlobStore:
    """Filesystem-backed gzipped JSON blob store."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    async def write(
        self, run_id: str, event_id: str, payload: dict
    ) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, _write_blob_sync, self.base_dir, run_id, event_id, payload
        )

    async def read(self, blob_path: str) -> dict:
        base_resolved = self.base_dir.resolve()
        full = (self.base_dir / blob_path).resolve()
        if not full.is_relative_to(base_resolved):
            raise ValueError("blob path escapes base_dir")

        if not full.exists():
            raise FileNotFoundError(f"Blob not found: {blob_path}")

        async with aiofiles.open(full, "rb") as f:
            compressed = await f.read()

        json_bytes = gzip.decompress(compressed)
        return json.loads(json_bytes.decode("utf-8"))

    async def delete_run(self, run_id: str) -> int:
        run_dir = self.base_dir / run_id
        if not run_dir.exists():
            return 0

        # Count files before deletion.
        count = sum(1 for p in run_dir.rglob("*") if p.is_file())

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, partial(shutil.rmtree, run_dir, ignore_errors=True)
        )
        return count

    def get_size_bytes(self, run_id: str) -> int:
        run_dir = self.base_dir / run_id
        if not run_dir.exists():
            return 0
        return _dir_size_bytes(run_dir)

    async def total_size_bytes(self) -> int:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _dir_size_bytes, self.base_dir)
