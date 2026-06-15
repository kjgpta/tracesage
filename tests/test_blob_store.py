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
async def test_write_rejects_unsafe_run_id(tmp_data_dir: Path) -> None:
    """The relative-under-base_dir invariant is enforced on WRITE too: an id with a
    path separator or '..' must be rejected before anything is written outside base_dir."""
    store = BlobStore(tmp_data_dir / "blobs")
    for bad in ("../escape", "a/b", "..", ""):
        with pytest.raises(ValueError, match="blob path"):
            await store.write(bad, "evt", {"x": 1})
        with pytest.raises(ValueError, match="blob path"):
            await store.write("run", bad, {"x": 1})
    # Nothing leaked outside the blobs dir.
    assert not (tmp_data_dir / "escape").exists()


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
async def test_atomic_write_read_roundtrip(tmp_data_dir: Path) -> None:
    # The executor-offloaded atomic write must still round-trip cleanly,
    # and must not leave any .tmp files behind.
    store = BlobStore(tmp_data_dir / "blobs")
    payload = {"x": 42, "deep": {"list": [1, 2, {"k": "v"}]}, "s": "hello"}
    path = await store.write("run-atomic", "evt-atomic", payload)

    assert path == "run-atomic/evt-atomic.json.gz"

    got = await store.read(path)
    assert got == payload

    run_dir = tmp_data_dir / "blobs" / "run-atomic"
    leftover = [p for p in run_dir.iterdir() if p.name.endswith(".tmp")]
    assert leftover == []


@pytest.mark.asyncio
async def test_total_size_bytes_positive_after_write(
    tmp_data_dir: Path,
) -> None:
    store = BlobStore(tmp_data_dir / "blobs")
    assert await store.total_size_bytes() == 0

    await store.write("run-sz", "evt-sz", {"data": "abc" * 1000})

    assert await store.total_size_bytes() > 0


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


@pytest.mark.asyncio
async def test_no_tmp_left_and_size_excludes_tmp(tmp_data_dir: Path) -> None:
    """A successful write leaves no .tmp behind, and total_size_bytes ignores any
    stray/in-flight .tmp file (so gc --max-blob-size-gb accounting is accurate)."""
    base = tmp_data_dir / "blobs"
    store = BlobStore(base)
    await store.write("run-t", "evt-t", {"x": "y"})

    run_dir = base / "run-t"
    assert list(run_dir.glob("*.tmp")) == []  # success path cleaned up

    # A stray temp file must NOT be counted toward the total.
    (run_dir / "evt-t.json.gz.deadbeef.tmp").write_bytes(b"x" * 100_000)
    size = await store.total_size_bytes()
    assert 0 < size < 100_000


def test_write_blob_sync_cleans_tmp_on_failure(tmp_data_dir: Path, monkeypatch) -> None:
    """If os.replace fails after the temp file is written, the orphan .tmp must be
    removed (and the error must propagate, not be swallowed)."""
    from tracelens.storage import blob_store as bs

    def _boom(*_a: object, **_k: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(bs.os, "replace", _boom)
    run_dir = tmp_data_dir / "r-fail"
    with pytest.raises(OSError, match="simulated replace failure"):
        bs._write_blob_sync(tmp_data_dir, "r-fail", "e1", {"k": "v"})
    assert list(run_dir.glob("*.tmp")) == []  # orphan cleaned up
    assert not (run_dir / "e1.json.gz").exists()  # final never created
