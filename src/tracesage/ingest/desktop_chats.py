"""Import Claude Desktop sidebar chats that the app has saved locally.

Cowork sessions live in ``audit.jsonl``. Ordinary chats (the Desktop sidebar)
are Claude.ai conversations. Desktop keeps the full transcript of a chat it
has opened in its HTTP cache, and a shorter copy of recently opened chats in
IndexedDB. Chats that were never opened on this machine are not on disk.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import struct
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tracesage.ingest.anthropic_log import build_turns
from tracesage.ingest.traces import Turn, turn_events

log = logging.getLogger("tracesage.ingest.desktop")

# Version 2 records model requests as spans. Older state is re-imported once.
_IMPORTS_VERSION = 2
_BLOCK = 32768
_MAX_CACHE_FILE = 20_000_000
_MAX_CHAT_BYTES = 8_000_000
_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_CHAT_URL = re.compile(
    r"/chat_conversations/(" + _UUID.pattern + r")",
    re.IGNORECASE,
)
_HASH_UUID = re.compile(r'"uuid"\s*:\s*"(' + _UUID.pattern + r')"', re.IGNORECASE)
_SERVER_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,40}")


def claude_support_dir(home: Path | None = None) -> Path:
    """Claude Desktop's application-support directory for this platform."""
    root = home or Path.home()
    if sys.platform == "darwin":
        return root / "Library" / "Application Support" / "Claude"
    if sys.platform == "win32":
        return root / "AppData" / "Roaming" / "Claude"
    return root / ".config" / "Claude"


def chat_records(chat: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn one cached conversation into audit records the turn parser understands."""
    session = chat.get("uuid")
    if not isinstance(session, str) or not _UUID.fullmatch(session):
        return []
    model = chat.get("model") if isinstance(chat.get("model"), str) else None
    title = chat.get("name") if isinstance(chat.get("name"), str) else None
    messages = chat.get("messages")
    if not isinstance(messages, list):
        return []
    ordered: list[tuple[int, int, dict[str, Any]]] = []
    for position, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        index = message.get("index")
        key = index if isinstance(index, int) else position
        ordered.append((key, position, message))
    ordered.sort()
    records: list[dict[str, Any]] = []
    titled = False
    for position, (_, _, message) in enumerate(ordered):
        uid = message.get("uuid")
        message_id = uid if isinstance(uid, str) and uid else f"{session}:{position}"
        sender = message.get("sender")
        blocks = _blocks(message)
        stamp = message.get("created_at")
        start = len(records)
        if sender == "human":
            tools = [block for block in blocks if block.get("type") == "tool_result"]
            text = _joined_text(blocks)
            if tools:
                records.append(_user(session, message_id + ":tools", tools))
            if text.strip():
                record = _user(
                    session, message_id, [{"type": "text", "text": text}],
                )
                if title and not titled:
                    record["title"] = title.strip()
                    titled = True
                records.append(record)
        elif sender == "assistant":
            pending: list[dict[str, Any]] = []
            part = 0
            for block in blocks:
                kind = block.get("type")
                if kind == "tool_result":
                    if pending:
                        records.append(_assistant(session, f"{message_id}:{part}", pending, model))
                        pending = []
                        part += 1
                    records.append(_user(session, f"{message_id}:result:{part}", [block]))
                    part += 1
                elif kind in {"text", "thinking", "tool_use"}:
                    pending.append(_tool_name(block) if kind == "tool_use" else block)
            if pending:
                records.append(_assistant(session, f"{message_id}:{part}", pending, model))
        if isinstance(stamp, str) and stamp:
            for record in records[start:]:
                record["timestamp"] = _block_time(record) or stamp
    if records:
        done: dict[str, Any] = {
            "type": "result",
            "uuid": f"{session}:done",
            "session_id": session,
        }
        last = records[-1].get("timestamp")
        if last:
            done["timestamp"] = last
        if model:
            done["modelUsage"] = {model: {}}
        records.append(done)
    return records


def chat_turns(chat: dict[str, Any]) -> list[Turn]:
    """Turns of one sidebar chat. Every turn is finished. Never raises."""
    try:
        session = chat.get("uuid")
        if not isinstance(session, str):
            return []
        return build_turns(
            chat_records(chat), app="claude-desktop", session=session, always_complete=True,
        )
    except Exception as exc:
        log.debug("desktop chat skipped: %s", exc)
        return []


def _block_time(record: dict[str, Any]) -> str | None:
    message = record.get("message") if isinstance(record.get("message"), dict) else {}
    content = message.get("content")
    if not isinstance(content, list):
        return None
    stamps = [
        block.get("stop_timestamp") or block.get("start_timestamp")
        for block in content
        if isinstance(block, dict)
    ]
    found = [stamp for stamp in stamps if isinstance(stamp, str) and stamp]
    return max(found) if found else None


def load_cached_chats(cache_dir: Path) -> list[dict[str, Any]]:
    """Conversations stored in Claude Desktop's HTTP cache. Never raises."""
    found: dict[str, dict[str, Any]] = {}
    try:
        if not cache_dir.is_dir():
            return []
        for path in cache_dir.iterdir():
            if not path.is_file():
                continue
            try:
                chat = _chat_from_cache_file(path)
            except Exception as exc:
                log.debug("desktop cache file skipped (%s): %s", path.name, exc)
                continue
            if chat is not None:
                _keep_longer(found, chat)
    except Exception as exc:
        log.warning("desktop cache scan failed: %s", exc)
    return list(found.values())


def load_indexeddb_chats(indexeddb: Path) -> list[dict[str, Any]]:
    """Conversations stored in the claude.ai IndexedDB. Never raises."""
    found: dict[str, dict[str, Any]] = {}
    names: dict[str, str] = {}
    try:
        if not indexeddb.is_dir():
            return []
        for path in indexeddb.rglob("*"):
            if not path.is_file() or path.stat().st_size > 20_000_000:
                continue
            if path.suffix not in {".log", ".ldb", ""} and "blob" not in path.parts:
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            for blob in _serialized_values(data):
                try:
                    obj = _decode_v8(blob)
                except Exception as exc:
                    log.debug("desktop indexeddb value skipped: %s", exc)
                    continue
                chats, titled = _conversations_in(obj)
                names.update(titled)
                for chat in chats:
                    _keep_longer(found, chat)
    except Exception as exc:
        log.warning("desktop indexeddb scan failed: %s", exc)
    for chat in found.values():
        if not chat.get("name"):
            name = names.get(str(chat.get("uuid")))
            if name:
                chat["name"] = name
    return list(found.values())


def collect_desktop_chats(home: Path | None = None) -> list[dict[str, Any]]:
    """Every locally saved sidebar chat, keeping the longest copy of each."""
    support = claude_support_dir(home)
    found: dict[str, dict[str, Any]] = {}
    for chat in load_cached_chats(support / "Cache" / "Cache_Data"):
        _keep_longer(found, chat)
    indexed = support / "IndexedDB" / "https_claude.ai_0.indexeddb.leveldb"
    for chat in load_indexeddb_chats(indexed.parent):
        _keep_longer(found, chat)
    return list(found.values())


def poll_desktop_chats(
    home: Path | None,
    imported: dict[str, int],
    emit: Callable[[Any], None],
    *,
    metadata_only: bool = False,
) -> dict[str, int]:
    """Emit chats whose local copy is longer than the last import. Never raises."""
    try:
        return _emit_new(collect_desktop_chats(home), imported, emit, metadata_only=metadata_only)
    except Exception as exc:
        log.warning("desktop chat import failed: %s", exc)
    return imported


def _imports_path(data_dir: Path) -> Path:
    return data_dir / "desktop-chat-imports.json"


def _load_imports(path: Path) -> dict[str, int]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(loaded, dict) or loaded.get("version") != _IMPORTS_VERSION:
        return {}
    chats = loaded.get("chats")
    if not isinstance(chats, dict):
        return {}
    return {
        key: value
        for key, value in chats.items()
        if isinstance(key, str) and isinstance(value, int) and value >= 0
    }


def _save_imports(path: Path, imported: dict[str, int]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"version": _IMPORTS_VERSION, "chats": imported}, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        log.warning("desktop chat imports not saved: %s", exc)


async def follow_desktop_chats(
    emit: Callable[[Any], None],
    data_dir: Path,
    *,
    metadata_only: bool = False,
    home: Path | None = None,
    interval: float = 30.0,
) -> None:
    """Re-read Desktop's local chat cache until cancelled. Never raises on a bad file."""
    path = _imports_path(data_dir)
    imported = _load_imports(path)
    try:
        while True:
            loop = asyncio.get_running_loop()
            found = await loop.run_in_executor(None, collect_desktop_chats, home)
            imported = _emit_new(found, imported, emit, metadata_only=metadata_only)
            _save_imports(path, imported)
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        _save_imports(path, imported)
        raise
    except Exception as exc:
        log.warning("desktop chat follower stopped: %s", exc)


def _emit_new(
    chats: list[dict[str, Any]],
    imported: dict[str, int],
    emit: Callable[[Any], None],
    *,
    metadata_only: bool,
) -> dict[str, int]:
    for chat in chats:
        uuid = chat.get("uuid")
        messages = chat.get("messages")
        if not isinstance(uuid, str) or not isinstance(messages, list):
            continue
        count = len(messages)
        if imported.get(uuid, -1) >= count:
            continue
        for turn in chat_turns(chat):
            for event in turn_events(turn, metadata_only=metadata_only):
                try:
                    emit(event)
                except Exception as exc:
                    log.warning("desktop chat emit failed: %s", exc)
        imported[uuid] = count
    return imported


def _keep_longer(found: dict[str, dict[str, Any]], chat: dict[str, Any]) -> None:
    uuid = chat.get("uuid")
    messages = chat.get("messages")
    if not isinstance(uuid, str) or not isinstance(messages, list) or not messages:
        return
    previous = found.get(uuid)
    if previous is None or len(messages) > len(previous.get("messages") or []):
        found[uuid] = chat
    elif previous is not None and not previous.get("name") and chat.get("name"):
        previous["name"] = chat["name"]


def _user(
    session: str, uid: str, content: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": uid,
        "session_id": session,
        "message": {"role": "user", "content": content},
    }


def _assistant(
    session: str,
    uid: str,
    blocks: list[dict[str, Any]],
    model: str | None,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "id": uid, "content": blocks}
    if model:
        message["model"] = model
    return {
        "type": "assistant",
        "uuid": uid,
        "session_id": session,
        "message": message,
    }


def _blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    blocks = [item for item in content if isinstance(item, dict)] if isinstance(content, list) else []
    has_text = any(
        item.get("type") == "text" and isinstance(item.get("text"), str) and item["text"].strip()
        for item in blocks
    )
    text = message.get("text")
    if not has_text and isinstance(text, str) and text.strip():
        return [{"type": "text", "text": text}, *blocks]
    return blocks


def _joined_text(blocks: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for block in blocks:
        if block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text)
    return "\n".join(parts)


def _tool_name(block: dict[str, Any]) -> dict[str, Any]:
    name = block.get("name") if isinstance(block.get("name"), str) else "tool"
    integration = block.get("integration_name")
    if (
        isinstance(integration, str)
        and _SERVER_NAME.fullmatch(integration)
        and not name.startswith("mcp__")
    ):
        named = dict(block)
        named["name"] = f"mcp__{integration}__{name}"
        return named
    return block


def _chat_from_cache_file(path: Path) -> dict[str, Any] | None:
    size = path.stat().st_size
    if size < 80 or size > _MAX_CACHE_FILE:
        return None
    with path.open("rb") as handle:
        head = handle.read(700)
    match = _CHAT_URL.search(head.decode("latin1", errors="ignore"))
    if match is None:
        return None
    after = head[match.end():match.end() + 1]
    if after == b"/":
        return None
    data = head if size <= 700 else head + path.read_bytes()[700:]
    magic = data.find(b"\x28\xb5\x2f\xfd", 40)
    if magic < 0:
        return None
    raw = _decompress_zstd_frame(data[magic:])
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return _chat_from_api(obj)


def _chat_from_api(obj: Any) -> dict[str, Any] | None:
    if not isinstance(obj, dict):
        return None
    uuid = obj.get("uuid")
    messages = obj.get("chat_messages")
    if not isinstance(uuid, str) or not isinstance(messages, list):
        return None
    if not any(isinstance(item, dict) and item.get("sender") == "human" for item in messages):
        return None
    chat: dict[str, Any] = {"uuid": uuid, "messages": messages}
    name = obj.get("name")
    if isinstance(name, str) and name.strip():
        chat["name"] = name.strip()
    model = obj.get("model")
    if isinstance(model, str) and model:
        chat["model"] = model
    return chat


def _decompress_zstd_frame(data: bytes) -> bytes | None:
    """Decompress the first zstd frame. Trailing cache bytes are ignored."""
    for reader in (_zstd_stdlib, _zstd_module, _zstd_cli):
        try:
            raw = reader(data)
        except Exception as exc:
            log.debug("zstd reader skipped: %s", exc)
            raw = None
        if raw and (raw.startswith(b"{") or raw.startswith(b"[")):
            return raw
    return None


def _zstd_stdlib(data: bytes) -> bytes | None:
    from compression import zstd

    decoder = zstd.ZstdDecompressor()
    out = bytearray()
    pos = 0
    while pos < len(data) and not decoder.eof and len(out) < _MAX_CHAT_BYTES:
        out += decoder.decompress(data[pos:pos + 65536])
        pos += 65536
    return bytes(out)


def _zstd_module(data: bytes) -> bytes | None:
    import zstandard

    return zstandard.ZstdDecompressor().decompress(data, max_output_size=_MAX_CHAT_BYTES)


def _zstd_cli(data: bytes) -> bytes | None:
    binary = next(
        (
            candidate
            for candidate in (
                shutil.which("zstd"),
                "/opt/homebrew/bin/zstd",
                "/usr/local/bin/zstd",
                "/usr/bin/zstd",
            )
            if candidate and Path(candidate).is_file()
        ),
        None,
    )
    if not binary:
        return None
    completed = subprocess.run(  # noqa: S603
        [binary, "-d", "-c"],
        input=data,
        capture_output=True,
        check=False,
        timeout=20,
    )
    return completed.stdout or None


def _serialized_values(data: bytes) -> list[bytes]:
    """Blink values in a leveldb log, an sst file, or an IndexedDB blob."""
    values: list[bytes] = []
    blobs = [data, *_leveldb_records(data)]
    for blob in blobs:
        start = 0
        while True:
            marker = blob.find(b"\xff\x11\x02", start)
            if marker < 0:
                break
            start = marker + 3
            try:
                length, cursor = _varint(blob, marker + 3)
            except ValueError:
                continue
            if length <= 0 or length > _MAX_CHAT_BYTES:
                continue
            try:
                values.append(_snappy(blob, cursor, length))
            except Exception as exc:
                log.debug("desktop snappy value skipped: %s", exc)
                continue
        if b"\xff\x10o" in blob and b"conversationUuid" in blob:
            values.append(blob)
    return values


def _leveldb_records(data: bytes) -> list[bytes]:
    records: list[bytes] = []
    pending = b""
    for base in range(0, len(data), _BLOCK):
        block = data[base:base + _BLOCK]
        pos = 0
        while pos + 7 <= len(block):
            _, length, kind = struct.unpack_from("<IHB", block, pos)
            if kind == 0 and length == 0:
                break
            if kind > 4 or pos + 7 + length > len(block):
                break
            frag = block[pos + 7:pos + 7 + length]
            pos += 7 + length
            if kind == 1:
                records.append(frag)
            elif kind == 2:
                pending = frag
            elif kind == 3:
                pending += frag
            elif kind == 4:
                records.append(pending + frag)
                pending = b""
    return records


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    for _ in range(5):
        if pos >= len(data):
            raise ValueError("varint")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, pos
        shift += 7
    raise ValueError("varint")


def _snappy(data: bytes, start: int, expect: int) -> bytes:
    pos = start
    out = bytearray()
    while len(out) < expect:
        if pos >= len(data):
            raise ValueError("snappy eof")
        tag = data[pos]
        pos += 1
        kind = tag & 3
        if kind == 0:
            literal = tag >> 2
            if literal < 60:
                length = literal + 1
            elif literal == 60:
                length = data[pos] + 1
                pos += 1
            elif literal == 61:
                length = int.from_bytes(data[pos:pos + 2], "little") + 1
                pos += 2
            elif literal == 62:
                length = int.from_bytes(data[pos:pos + 3], "little") + 1
                pos += 3
            else:
                length = int.from_bytes(data[pos:pos + 4], "little") + 1
                pos += 4
            out += data[pos:pos + length]
            pos += length
        elif kind == 1:
            length = ((tag >> 2) & 7) + 4
            offset = ((tag >> 5) << 8) | data[pos]
            pos += 1
            _copy(out, offset, length)
        elif kind == 2:
            length = (tag >> 2) + 1
            offset = int.from_bytes(data[pos:pos + 2], "little")
            pos += 2
            _copy(out, offset, length)
        else:
            length = (tag >> 2) + 1
            offset = int.from_bytes(data[pos:pos + 4], "little")
            pos += 4
            _copy(out, offset, length)
    return bytes(out)


def _copy(out: bytearray, offset: int, length: int) -> None:
    if offset <= 0 or offset > len(out):
        raise ValueError("snappy offset")
    for _ in range(length):
        out.append(out[-offset])


class _V8Error(Exception):
    pass


class _V8:
    """Enough of V8's value serializer (version 16) to read a conversation tree."""

    def __init__(self, data: bytes, pos: int) -> None:
        self.data = data
        self.pos = pos
        self.objs: list[Any] = []

    def u32(self) -> int:
        value, self.pos = _varint(self.data, self.pos)
        return value

    def zigzag(self) -> int:
        value = self.u32()
        return (value >> 1) ^ -(value & 1)

    def raw(self, size: int) -> bytes:
        if size < 0 or self.pos + size > len(self.data):
            raise _V8Error("eof")
        chunk = self.data[self.pos:self.pos + size]
        self.pos += size
        return chunk

    def value(self, depth: int = 0) -> Any:
        if depth > 200 or self.pos >= len(self.data):
            raise _V8Error("bounds")
        tag = self.data[self.pos]
        self.pos += 1
        if tag == 0:
            return self.value(depth)
        if tag == 0x3F:  # object-count check, safe to skip
            self.u32()
            return self.value(depth)
        if tag == 0x22:
            return self.raw(self.u32()).decode("latin1")
        if tag == 0x63:
            return self.raw(self.u32()).decode("utf-16le", "replace")
        if tag == 0x53:
            return self.raw(self.u32()).decode("utf-8", "replace")
        if tag == 0x54:
            return True
        if tag == 0x46:
            return False
        if tag in {0x30, 0x5F, 0x2D}:
            return None
        if tag == 0x49:
            return self.zigzag()
        if tag == 0x55:
            return self.u32()
        if tag == 0x4E:
            return struct.unpack("<d", self.raw(8))[0]
        if tag == 0x44:
            value = struct.unpack("<d", self.raw(8))[0]
            self.objs.append(value)
            return value
        if tag == 0x5E:
            index = self.u32()
            return self.objs[index] if index < len(self.objs) else None
        if tag in {0x42, 0x43}:
            self.raw(self.u32())
            self._view()
            return None
        if tag == 0x7E:
            length = self.u32()
            self.u32()
            self.raw(length)
            self._view()
            return None
        if tag == 0x6F:
            return self.obj(depth)
        if tag == 0x41:
            return self.dense(depth)
        if tag == 0x61:
            return self.sparse(depth)
        if tag == 0x3B:
            return self.mapping(depth)
        if tag == 0x27:
            return self.group(depth)
        if tag in {0x79, 0x78}:
            return None
        if tag == 0x6E:
            self.raw(8)
            return None
        if tag == 0x73:
            return self.value(depth)
        if tag == 0x52:
            self.value(depth + 1)
            self.u32()
            return None
        raise _V8Error(f"tag {tag:#x}")

    def _view(self) -> None:
        """A typed-array view tag sits immediately after its buffer."""
        if self.pos < len(self.data) and self.data[self.pos] == 0x56:
            self.pos += 1
            self.u32()
            self.u32()
            self.u32()
            self.u32()

    def obj(self, depth: int) -> dict[str, Any]:
        result: dict[str, Any] = {}
        self.objs.append(result)
        try:
            while self.pos < len(self.data) and self.data[self.pos] != 0x7B:
                key = self.value(depth + 1)
                value = self.value(depth + 1)
                if isinstance(key, str):
                    result[key] = value
            if self.pos < len(self.data) and self.data[self.pos] == 0x7B:
                self.pos += 1
                self.u32()
        except _V8Error:
            return result
        return result

    def dense(self, depth: int) -> list[Any]:
        count = self.u32()
        result: list[Any] = []
        self.objs.append(result)
        try:
            for _ in range(count):
                if self.pos < len(self.data) and self.data[self.pos] == 0x2D:
                    self.pos += 1
                    result.append(None)
                else:
                    result.append(self.value(depth + 1))
            while self.pos < len(self.data) and self.data[self.pos] != 0x24:
                self.value(depth + 1)
                self.value(depth + 1)
            if self.pos < len(self.data) and self.data[self.pos] == 0x24:
                self.pos += 1
                self.u32()
                self.u32()
        except _V8Error:
            return result
        return result

    def sparse(self, depth: int) -> list[Any]:
        self.u32()
        slots: dict[int, Any] = {}
        self.objs.append(slots)
        try:
            while self.pos < len(self.data) and self.data[self.pos] != 0x40:
                key = self.value(depth + 1)
                value = self.value(depth + 1)
                if isinstance(key, int) and key >= 0:
                    slots[key] = value
            if self.pos < len(self.data) and self.data[self.pos] == 0x40:
                self.pos += 1
                self.u32()
                self.u32()
        except _V8Error:
            return [slots[key] for key in sorted(slots)]
        if not slots or max(slots) > 100_000:
            return [slots[key] for key in sorted(slots)]
        return [slots.get(index) for index in range(max(slots) + 1)]

    def mapping(self, depth: int) -> dict[str, Any]:
        result: dict[str, Any] = {}
        self.objs.append(result)
        try:
            while self.pos < len(self.data) and self.data[self.pos] != 0x3A:
                key = self.value(depth + 1)
                result[str(key)] = self.value(depth + 1)
            if self.pos < len(self.data) and self.data[self.pos] == 0x3A:
                self.pos += 1
                self.u32()
        except _V8Error:
            return result
        return result

    def group(self, depth: int) -> list[Any]:
        result: list[Any] = []
        self.objs.append(result)
        try:
            while self.pos < len(self.data) and self.data[self.pos] != 0x2C:
                result.append(self.value(depth + 1))
            if self.pos < len(self.data) and self.data[self.pos] == 0x2C:
                self.pos += 1
                self.u32()
        except _V8Error:
            return result
        return result


def _decode_v8(blob: bytes) -> Any:
    marker = blob.find(b"\xff\x10o")
    if marker < 0:
        raise _V8Error("no object")
    return _V8(blob, marker + 3).obj(0)


def _conversations_in(obj: Any) -> tuple[list[dict[str, Any]], dict[str, str]]:
    chats: list[dict[str, Any]] = []
    names: dict[str, str] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            _take(node, chats, names)
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                if isinstance(value, (dict, list)):
                    walk(value)

    walk(obj)
    best: dict[str, dict[str, Any]] = {}
    for chat in chats:
        uuid = str(chat.get("uuid"))
        previous = best.get(uuid)
        if previous is None or len(chat.get("messages") or []) > len(previous.get("messages") or []):
            best[uuid] = chat
        elif not previous.get("name") and chat.get("name"):
            previous["name"] = chat["name"]
    return list(best.values()), names


def _take(node: dict[str, Any], chats: list[dict[str, Any]], names: dict[str, str]) -> None:
    uuid = node.get("conversationUuid") or node.get("uuid")
    name = node.get("name") if isinstance(node.get("name"), str) else node.get("title")
    if isinstance(uuid, str) and _UUID.fullmatch(uuid) and isinstance(name, str) and name.strip():
        names.setdefault(uuid, name.strip())
    messages = node.get("messages")
    if not isinstance(messages, list):
        messages = node.get("chat_messages")
    tree = node.get("tree")
    if not isinstance(messages, list) and isinstance(tree, dict):
        nested = tree.get("messages")
        messages = nested if isinstance(nested, list) else None
        if not isinstance(uuid, str):
            uuid = tree.get("conversationUuid")
    if not isinstance(messages, list) or not isinstance(uuid, str) or not _UUID.fullmatch(uuid):
        hashed = node.get("queryHash")
        state = node.get("state")
        data = state.get("data") if isinstance(state, dict) else None
        if isinstance(hashed, str) and isinstance(data, dict):
            found = _HASH_UUID.search(hashed)
            nested = data.get("messages")
            if found and isinstance(nested, list):
                uuid = found.group(1)
                messages = nested
    if not isinstance(messages, list) or not isinstance(uuid, str):
        return
    if not any(isinstance(item, dict) and item.get("sender") == "human" for item in messages):
        return
    chat: dict[str, Any] = {"uuid": uuid, "messages": [item for item in messages if isinstance(item, dict)]}
    if isinstance(name, str) and name.strip():
        chat["name"] = name.strip()
    model = node.get("model")
    if isinstance(model, str) and model:
        chat["model"] = model
    chats.append(chat)
