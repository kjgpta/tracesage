"""Filesystem blob store. Gzipped JSON per (run_id, event_id).

Storage layout:
    {base_dir}/{run_id}/{event_id}.json.gz

Path-traversal guarded on read.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import shutil
from functools import partial
from pathlib import Path
from typing import Any

import aiofiles


def _safe_serialize(payload: Any) -> str:
    """Serialize to JSON, falling back to str() for non-serializable objects.

    `default=str` handles datetimes, Pydantic models, custom classes, etc.
    """
    return json.dumps(payload, default=str)


class BlobStore:
    """Filesystem-backed gzipped JSON blob store."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    async def write(
        self, run_id: str, event_id: str, payload: dict
    ) -> str:
        run_dir = self.base_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        file_path = run_dir / f"{event_id}.json.gz"

        json_bytes = _safe_serialize(payload).encode("utf-8")
        compressed = gzip.compress(json_bytes)

        async with aiofiles.open(file_path, "wb") as f:
            await f.write(compressed)

        # Forward slashes for DB consistency across OSes.
        return f"{run_id}/{event_id}.json.gz"

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
        total = 0
        for p in run_dir.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    continue
        return total
