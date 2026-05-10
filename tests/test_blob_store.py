"""Tests for BlobStore."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tracelens.storage import BlobStore


@pytest.mark.asyncio
async def test_write_read_roundtrip(tmp_data_dir: Path) -> None:
    store = BlobStore(tmp_data_dir / "blobs")
    payload = {"a": 1, "b": "two", "nested": {"c": [1, 2, 3]}}
    path = await store.write("run1", "evt1", payload)

    assert path == "run1/evt1.json.gz"

    got = await store.read(path)
    assert got == payload


@pytest.mark.asyncio
async def test_gzip_compresses(tmp_data_dir: Path) -> None:
    store = BlobStore(tmp_data_dir / "blobs")
    # Highly compressible repetitive payload.
    payload = {"data": "abc" * 5000, "items": ["xx"] * 200}
    raw_size = len(json.dumps(payload).encode("utf-8"))
    assert raw_size > 10_000

    path = await store.write("run1", "evt1", payload)
    on_disk = (tmp_data_dir / "blobs" / path).stat().st_size

    assert on_disk < raw_size
    # Sanity: must still round-trip.
    got = await store.read(path)
    assert got == payload


@pytest.mark.asyncio
async def test_path_traversal_blocked(tmp_data_dir: Path) -> None:
    store = BlobStore(tmp_data_dir / "blobs")
    with pytest.raises(ValueError, match="escapes base_dir"):
        await store.read("../../etc/passwd")


@pytest.mark.asyncio
async def test_delete_run_removes_all_blobs(tmp_data_dir: Path) -> None:
    store = BlobStore(tmp_data_dir / "blobs")
    for i in range(5):
        await store.write("run-x", f"evt{i}", {"i": i})

    run_dir = tmp_data_dir / "blobs" / "run-x"
    assert run_dir.exists()
    assert len(list(run_dir.iterdir())) == 5

    deleted = await store.delete_run("run-x")
    assert deleted == 5
    assert not run_dir.exists()


@pytest.mark.asyncio
async def test_non_serializable_falls_back_to_str(
    tmp_data_dir: Path,
) -> None:
    class Custom:
        def __repr__(self) -> str:
            return "Custom(stringified)"

    store = BlobStore(tmp_data_dir / "blobs")
    payload = {"obj": Custom(), "ok": 1}
    path = await store.write("run1", "evt1", payload)

    got = await store.read(path)
    assert got["ok"] == 1
    assert got["obj"] == "Custom(stringified)"


@pytest.mark.asyncio
async def test_concurrent_writes_to_same_run(tmp_data_dir: Path) -> None:
    store = BlobStore(tmp_data_dir / "blobs")

    async def w(i: int) -> str:
        return await store.write("run-c", f"evt{i:02d}", {"i": i})

    paths = await asyncio.gather(*(w(i) for i in range(10)))
    assert len(paths) == 10
    assert len(set(paths)) == 10

    for i, p in enumerate(paths):
        got = await store.read(p)
        assert got == {"i": i}
