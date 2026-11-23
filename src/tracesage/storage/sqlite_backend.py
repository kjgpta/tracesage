"""SQLite implementation of the StorageBackend protocol.

Uses aiosqlite + an asyncio.Semaphore-based connection pool. Schema and queries
match design doc section 6.5 exactly.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from tracesage.models import (
    EventType,
    Run,
    RunNote,
    RunStatus,
    SearchHit,
    StoredEvent,
    ThreadSummary,
    Topology,
    TopologyEdge,
    TopologyNode,
)
from tracesage.search import fts_match_query, like_contains

_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY,
        root_run_id TEXT NOT NULL,
        tags TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'running',
        started_at TEXT NOT NULL,
        completed_at TEXT,
        total_steps INTEGER NOT NULL DEFAULT 0,
        total_tokens_input INTEGER NOT NULL DEFAULT 0,
        total_tokens_output INTEGER NOT NULL DEFAULT 0,
        total_cost_usd REAL,
        graph_definition TEXT,
        error_message TEXT,
        thread_id TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        event_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        parent_run_id TEXT,
        root_run_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        agent_name TEXT,
        tool_name TEXT,
        mcp_server TEXT,
        summary TEXT NOT NULL,
        blob_path TEXT,
        duration_ms INTEGER,
        token_input INTEGER,
        token_output INTEGER,
        token_cache_read INTEGER,
        token_cache_write INTEGER,
        model TEXT,
        provider TEXT,
        cost_usd REAL,
        error_message TEXT,
        FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_run_id ON events(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_root_run_id ON events(root_run_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status)",
    "CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs(started_at)",
    # Backs the topology edge self-join (p.run_id = c.parent_run_id) and the
    # has_children scan over parent_run_id.
    "CREATE INDEX IF NOT EXISTS idx_events_parent_run_id ON events(parent_run_id)",
    # Composite index backing get_journey/iter_journey: root_run_id filter +
    # timestamp ordering in a single index.
    "CREATE INDEX IF NOT EXISTS idx_events_root_ts ON events(root_run_id, timestamp)",
    # MCP tool registry (server -> tool name), persisted at registration so the
    # topology can show ALL of a server's tools, even ones never invoked.
    """
    CREATE TABLE IF NOT EXISTS mcp_tools (
        server TEXT NOT NULL,
        tool_name TEXT NOT NULL,
        PRIMARY KEY (server, tool_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS run_notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        author TEXT,
        text TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_run_notes_run_id ON run_notes(run_id)",
    """
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
        name,
        summary,
        error,
        payload,
        event_id UNINDEXED,
        root_run_id UNINDEXED,
        tokenize='unicode61 remove_diacritics 2'
    )
    """,
)

# Current on-disk schema version, stamped into PRAGMA user_version by init().
# Future migrations key off the persisted value to upgrade old DBs.
#   v1 -> v2: add events.mcp_server (MCP tool-source attribution).
#   v2 -> v3: add mcp_tools registry table (created by the CREATE TABLE IF NOT
#             EXISTS in _SCHEMA_STATEMENTS, so no ALTER is needed).
#   v3 -> v4: add runs.thread_id (+ index) for conversation/thread grouping.
#   v4 -> v5: model/provider/cache-token/cost columns, events_fts, run_notes.
#   v5 -> v6: events_fts rowid equals the events rowid, so an upsert replaces
#             its index row by rowid instead of scanning the index by event_id.
_SCHEMA_VERSION = 6

_PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA temp_store=MEMORY",
    "PRAGMA cache_size=-64000",
    "PRAGMA busy_timeout=5000",
)


def _iso(dt: datetime | None) -> str | None:
    """Serialize a datetime to a fixed-width UTC ISO string.

    All chronological ordering (ORDER BY timestamp, keyset pagination) compares
    these as TEXT, so they MUST be lexically monotonic. We normalize every value
    to UTC and always emit microseconds, giving a constant-width string whose
    byte order matches chronological order. Naive datetimes are assumed UTC.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


def _cols(row: aiosqlite.Row) -> set[str]:
    return set(row.keys())


def _parse_tags(raw: str | None) -> list[str]:
    try:
        tags = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return [str(t) for t in tags] if isinstance(tags, list) else []


def _row_to_run(row: aiosqlite.Row) -> Run:
    cols = _cols(row)
    return Run(
        run_id=row["run_id"],
        root_run_id=row["root_run_id"],
        tags=json.loads(row["tags"] or "[]"),
        status=RunStatus(row["status"]),
        started_at=_parse_dt(row["started_at"]),  # type: ignore[arg-type]
        completed_at=_parse_dt(row["completed_at"]),
        total_steps=row["total_steps"],
        total_tokens_input=row["total_tokens_input"],
        total_tokens_output=row["total_tokens_output"],
        total_cost_usd=row["total_cost_usd"] if "total_cost_usd" in cols else None,
        graph_definition=row["graph_definition"],
        error_message=row["error_message"],
        thread_id=row["thread_id"] if "thread_id" in cols else None,
    )


def _row_to_event(row: aiosqlite.Row) -> StoredEvent:
    cols = _cols(row)
    return StoredEvent(
        event_id=row["event_id"],
        run_id=row["run_id"],
        parent_run_id=row["parent_run_id"],
        root_run_id=row["root_run_id"],
        event_type=EventType(row["event_type"]),
        timestamp=_parse_dt(row["timestamp"]),  # type: ignore[arg-type]
        agent_name=row["agent_name"],
        tool_name=row["tool_name"],
        mcp_server=row["mcp_server"],
        summary=row["summary"],
        blob_path=row["blob_path"],
        duration_ms=row["duration_ms"],
        token_input=row["token_input"],
        token_output=row["token_output"],
        token_cache_read=row["token_cache_read"] if "token_cache_read" in cols else None,
        token_cache_write=row["token_cache_write"] if "token_cache_write" in cols else None,
        model=row["model"] if "model" in cols else None,
        provider=row["provider"] if "provider" in cols else None,
        cost_usd=row["cost_usd"] if "cost_usd" in cols else None,
        error_message=row["error_message"],
    )


# ORDER BY clauses keyed by the public ``sort`` value. Only these static strings
# ever reach the SQL; unknown keys fall back to "recent".
_RUN_ORDER = {
    "recent": "started_at DESC",
    "oldest": "started_at ASC",
    "steps": "total_steps DESC, started_at DESC",
    "tokens": "(IFNULL(total_tokens_input, 0) + IFNULL(total_tokens_output, 0)) DESC, started_at DESC",
    "cost": "IFNULL(total_cost_usd, 0) DESC, started_at DESC",
}
# Runs imported from a coding assistant carry a ``source:<app>`` tag.
_NO_SOURCE_CLAUSE = "tags NOT LIKE '%\"source:%'"
_THREAD_ORDER = {
    "recent": "last_at DESC",
    "oldest": "last_at ASC",
    "turns": "run_count DESC, last_at DESC",
    "tokens": "(total_tokens_input + total_tokens_output) DESC, last_at DESC",
}


_RUN_UPSERT_SQL = """
    INSERT INTO runs (
        run_id, root_run_id, tags, status, started_at, completed_at,
        total_steps, total_tokens_input, total_tokens_output, total_cost_usd,
        graph_definition, error_message, thread_id
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(run_id) DO UPDATE SET
        root_run_id = excluded.root_run_id,
        tags = CASE WHEN excluded.tags = '[]' THEN runs.tags ELSE excluded.tags END,
        status = CASE
            WHEN excluded.status = 'running' AND runs.status != 'running'
            THEN runs.status ELSE excluded.status END,
        started_at = MIN(runs.started_at, excluded.started_at),
        completed_at = CASE
            WHEN excluded.status = 'running' AND runs.status != 'running'
            THEN runs.completed_at ELSE excluded.completed_at END,
        total_steps = MAX(runs.total_steps, excluded.total_steps),
        total_tokens_input = MAX(runs.total_tokens_input, excluded.total_tokens_input),
        total_tokens_output = MAX(runs.total_tokens_output, excluded.total_tokens_output),
        total_cost_usd = COALESCE(excluded.total_cost_usd, runs.total_cost_usd),
        graph_definition = COALESCE(excluded.graph_definition, runs.graph_definition),
        error_message = CASE
            WHEN excluded.status = 'running' AND runs.status != 'running'
            THEN runs.error_message ELSE excluded.error_message END,
        thread_id = COALESCE(excluded.thread_id, runs.thread_id)
"""


def _run_params(run: Run) -> tuple[Any, ...]:
    return (
        run.run_id,
        run.root_run_id,
        json.dumps(run.tags),
        run.status.value,
        _iso(run.started_at),
        _iso(run.completed_at),
        run.total_steps,
        run.total_tokens_input,
        run.total_tokens_output,
        run.total_cost_usd,
        run.graph_definition,
        run.error_message,
        run.thread_id,
    )


class SQLiteBackend:
    """aiosqlite-backed implementation of StorageBackend."""

    def __init__(self, db_path: Path, pool_size: int = 5) -> None:
        self._db_path = Path(db_path)
        self._sem = asyncio.Semaphore(pool_size)

    @asynccontextmanager
    async def _conn(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._sem:
            conn = await aiosqlite.connect(self._db_path)
            try:
                conn.row_factory = aiosqlite.Row
                for pragma in _PRAGMAS:
                    await conn.execute(pragma)
                yield conn
            finally:
                await conn.close()

    async def init(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with self._conn() as conn:
            for stmt in _SCHEMA_STATEMENTS:
                await conn.execute(stmt)
            await conn.commit()

            # Stamp the schema version. PRAGMA user_version takes no `?` placeholder,
            # so it cannot be parameterized; we use a plain literal (kept in sync with
            # _SCHEMA_VERSION) rather than an f-string to stay clear of SQL-build lints.
            cur = await conn.execute("PRAGMA user_version")
            row = await cur.fetchone()
            await cur.close()
            current = row[0] if row else 0
            if current < 5:
                # v1 -> v2: add events.mcp_server. Keyed off the live columns, not
                # `current`: a legacy DB created before versioning reports 0 yet
                # still has the pre-v2 events table (CREATE IF NOT EXISTS skips it).
                cur = await conn.execute("PRAGMA table_info(events)")
                event_cols = {r[1] for r in await cur.fetchall()}
                await cur.close()
                if "mcp_server" not in event_cols:
                    with suppress(aiosqlite.OperationalError):
                        await conn.execute("ALTER TABLE events ADD COLUMN mcp_server TEXT")
                # v2 -> v3 (mcp_tools table) needs no ALTER — the CREATE TABLE IF NOT
                # EXISTS in the schema loop above already created it on this connection.
                cur = await conn.execute("PRAGMA table_info(runs)")
                run_cols = {r[1] for r in await cur.fetchall()}
                await cur.close()
                if "thread_id" not in run_cols:
                    with suppress(aiosqlite.OperationalError):
                        await conn.execute("ALTER TABLE runs ADD COLUMN thread_id TEXT")
                # Created here, not in _SCHEMA_STATEMENTS: on a legacy DB the column
                # only exists after the ALTER above.
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_runs_thread_id ON runs(thread_id)"
                )
                # v4 -> v5: cost, model, cache tokens, FTS. Columns are added only
                # when missing so a fresh v5 database (CREATE TABLE already has
                # them) and a legacy database both end up identical.
                await self._ensure_v5_columns(conn, event_cols, run_cols)
                cur = await conn.execute("SELECT COUNT(*) AS n FROM events")
                count_row = await cur.fetchone()
                n_events = int(count_row["n"]) if count_row is not None else 0
                await cur.close()
                # Existing rows (including an unversioned legacy DB) have summaries
                # only. A brand-new empty database does not need a reindex.
                if n_events > 0:
                    await conn.execute(
                        "INSERT INTO meta(key, value) VALUES('needs_reindex', '1') "
                        "ON CONFLICT(key) DO UPDATE SET value = '1'"
                    )
                    await self._backfill_fts_summaries(conn)
                await conn.execute("PRAGMA user_version = 5")
                await conn.commit()
            if current < 6:
                await self._align_fts_rowids(conn)
                await conn.execute("PRAGMA user_version = 6")
                await conn.commit()

    @staticmethod
    async def _ensure_v5_columns(
        conn: aiosqlite.Connection,
        event_cols: set[str],
        run_cols: set[str],
    ) -> None:
        # Literals, not interpolated values: SQLite ALTER cannot take a placeholder.
        if "token_cache_read" not in event_cols:
            with suppress(aiosqlite.OperationalError):
                await conn.execute("ALTER TABLE events ADD COLUMN token_cache_read INTEGER")
        if "token_cache_write" not in event_cols:
            with suppress(aiosqlite.OperationalError):
                await conn.execute("ALTER TABLE events ADD COLUMN token_cache_write INTEGER")
        if "model" not in event_cols:
            with suppress(aiosqlite.OperationalError):
                await conn.execute("ALTER TABLE events ADD COLUMN model TEXT")
        if "provider" not in event_cols:
            with suppress(aiosqlite.OperationalError):
                await conn.execute("ALTER TABLE events ADD COLUMN provider TEXT")
        if "cost_usd" not in event_cols:
            with suppress(aiosqlite.OperationalError):
                await conn.execute("ALTER TABLE events ADD COLUMN cost_usd REAL")
        if "total_cost_usd" not in run_cols:
            with suppress(aiosqlite.OperationalError):
                await conn.execute("ALTER TABLE runs ADD COLUMN total_cost_usd REAL")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_events_model ON events(model)")
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_tool_name ON events(tool_name)"
        )

    @staticmethod
    async def _backfill_fts_summaries(conn: aiosqlite.Connection) -> None:
        """Index existing summaries once. Payload text is filled by ``reindex``."""
        cur = await conn.execute("SELECT COUNT(*) AS n FROM events_fts")
        count_row = await cur.fetchone()
        existing = int(count_row["n"]) if count_row is not None else 0
        await cur.close()
        if existing:
            return
        await conn.execute(
            "INSERT INTO events_fts(rowid, name, summary, error, payload, event_id, root_run_id) "
            "SELECT rowid, COALESCE(tool_name, agent_name, ''), COALESCE(summary, ''), "
            "COALESCE(error_message, ''), '', event_id, root_run_id FROM events"
        )

    @staticmethod
    async def _align_fts_rowids(conn: aiosqlite.Connection) -> None:
        """Renumber index rows to their event's rowid. Indexed payload text is kept."""
        await conn.execute("DROP TABLE IF EXISTS temp.fts_align")
        await conn.execute(
            "CREATE TEMP TABLE fts_align AS "
            "SELECT e.rowid AS rid, f.name, f.summary, f.error, f.payload, f.event_id, "
            "f.root_run_id FROM events_fts f JOIN events e ON e.event_id = f.event_id"
        )
        await conn.execute("DELETE FROM events_fts")
        await conn.execute(
            "INSERT INTO events_fts(rowid, name, summary, error, payload, event_id, root_run_id) "
            "SELECT rid, name, summary, error, payload, event_id, root_run_id "
            "FROM fts_align GROUP BY rid"
        )
        await conn.execute("DROP TABLE temp.fts_align")

    async def close(self) -> None:
        # No persistent connections held; pool opens/closes per acquire.
        return None

    # -- Run operations --

    async def upsert_run(self, run: Run) -> None:
        async with self._conn() as conn:
            await conn.execute(_RUN_UPSERT_SQL, _run_params(run))
            await conn.commit()

    async def upsert_runs_batch(self, runs: list[Run]) -> None:
        """Upsert several runs in one transaction. A bad row is logged and skipped."""
        if not runs:
            return
        async with self._conn() as conn:
            await conn.execute("BEGIN")
            for run in runs:
                try:
                    await conn.execute(_RUN_UPSERT_SQL, _run_params(run))
                except Exception as exc:
                    print(f"tracesage: upsert_run failed for {run.run_id}: {exc}", file=sys.stderr)
            await conn.commit()

    async def get_run(self, run_id: str) -> Run | None:
        async with self._conn() as conn:
            cursor = await conn.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            )
            row = await cursor.fetchone()
            await cursor.close()
        if row is None:
            return None
        return _row_to_run(row)

    async def list_runs(
        self,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        tag: str | None = None,
        thread_id: str | None = None,
        q: str | None = None,
        model: str | None = None,
        tool: str | None = None,
        errors_only: bool = False,
        sort: str = "recent",
        no_source: bool = False,
    ) -> tuple[list[Run], int]:
        order = _RUN_ORDER.get(sort, _RUN_ORDER["recent"])
        # Filter to top-level runs only (run_id == root_run_id). Sub-runs from nested
        # LangGraph nodes have their own rows for events FK but should not appear in
        # the user-facing run list.
        clauses = ["run_id = root_run_id"]
        where_params: list = []
        if status is not None and status != "all":
            clauses.append("status = ?")
            where_params.append(status)
        if tag:
            # tags is stored as a JSON array TEXT; a substring match on the raw
            # JSON is an acceptable (and intentionally loose) tag-substring filter.
            # Applied in SQL so it composes with COUNT/LIMIT/OFFSET below.
            clauses.append("tags LIKE ?")
            where_params.append(f"%{tag}%")
        if no_source:
            clauses.append(_NO_SOURCE_CLAUSE)
        if thread_id:
            clauses.append("thread_id = ?")
            where_params.append(thread_id)
        if errors_only:
            clauses.append("status = 'failed'")
        if model:
            clauses.append(
                "run_id IN (SELECT root_run_id FROM events WHERE model = ?)"
            )
            where_params.append(model)
        if tool:
            clauses.append(
                "run_id IN (SELECT root_run_id FROM events WHERE tool_name = ?)"
            )
            where_params.append(tool)
        if q:
            like = like_contains(q)
            match = fts_match_query(q)
            text_clauses = [
                "run_id LIKE ? ESCAPE '\\'",
                "IFNULL(thread_id, '') LIKE ? ESCAPE '\\'",
                "tags LIKE ? ESCAPE '\\'",
            ]
            where_params.extend((like, like, like))
            if match is not None:
                text_clauses.append(
                    "run_id IN (SELECT root_run_id FROM events_fts WHERE events_fts MATCH ?)"
                )
                where_params.append(match)
            clauses.append("(" + " OR ".join(text_clauses) + ")")
        where = " AND ".join(clauses)

        count_sql = f"SELECT COUNT(*) AS n FROM runs WHERE {where}"  # noqa: S608 - clauses are static, values bound via ?
        count_params: tuple = tuple(where_params)
        list_sql = (
            f"SELECT * FROM runs WHERE {where} "  # noqa: S608 - clauses are static, values bound via ?
            f"ORDER BY {order} LIMIT ? OFFSET ?"
        )
        list_params: tuple = (*where_params, limit, offset)

        async with self._conn() as conn:
            cur = await conn.execute(count_sql, count_params)
            total_row = await cur.fetchone()
            await cur.close()
            total = int(total_row["n"]) if total_row is not None else 0

            cur = await conn.execute(list_sql, list_params)
            rows = await cur.fetchall()
            await cur.close()

        return [_row_to_run(r) for r in rows], total

    async def update_run_status(
        self,
        run_id: str,
        status: RunStatus,
        completed_at: datetime | None = None,
        error: str | None = None,
    ) -> None:
        sql = """
            UPDATE runs
               SET status = ?,
                   completed_at = ?,
                   error_message = ?
             WHERE run_id = ? AND status = 'running'
        """
        async with self._conn() as conn:
            await conn.execute(
                sql, (status.value, _iso(completed_at), error, run_id)
            )
            await conn.commit()

    async def increment_run_counters(
        self,
        run_id: str,
        steps: int = 0,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost: float | None = None,
    ) -> None:
        # cost is None when this batch priced nothing, so a NULL total stays NULL
        # (shown as "n/a") instead of becoming 0.
        sql = """
            UPDATE runs
               SET total_steps = total_steps + ?,
                   total_tokens_input = total_tokens_input + ?,
                   total_tokens_output = total_tokens_output + ?,
                   total_cost_usd = CASE
                       WHEN ? IS NULL THEN total_cost_usd
                       ELSE COALESCE(total_cost_usd, 0) + ?
                   END
             WHERE run_id = ?
        """
        async with self._conn() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            await conn.execute(sql, (steps, tokens_in, tokens_out, cost, cost, run_id))
            await conn.commit()

    async def recompute_run_counters(self, run_id: str) -> None:
        sql = """
            UPDATE runs
               SET (total_steps, total_tokens_input, total_tokens_output, total_cost_usd) = (
                   SELECT COUNT(*),
                          COALESCE(SUM(token_input), 0),
                          COALESCE(SUM(token_output), 0),
                          SUM(cost_usd)
                     FROM events WHERE root_run_id = ?
               )
             WHERE run_id = ?
        """
        async with self._conn() as conn:
            await conn.execute(sql, (run_id, run_id))
            await conn.commit()

    async def delete_run(self, run_id: str) -> None:
        # Delete the WHOLE run tree, not just the root row. Nested LangGraph runs
        # get their own auto-created run rows (run_id != root_run_id) plus events,
        # which the FK cascade on the single root row would NOT remove. Match by
        # root_run_id (the tree) OR run_id (defensive, if a sub-run id is passed).
        async with self._conn() as conn:
            await conn.execute(
                "DELETE FROM events_fts WHERE rowid IN ("
                "SELECT rowid FROM events WHERE root_run_id = ? OR run_id = ?)",
                (run_id, run_id),
            )
            await conn.execute(
                "DELETE FROM run_notes WHERE run_id = ? OR run_id IN ("
                "SELECT run_id FROM runs WHERE root_run_id = ?)",
                (run_id, run_id),
            )
            await conn.execute(
                "DELETE FROM events WHERE root_run_id = ? OR run_id = ?",
                (run_id, run_id),
            )
            await conn.execute(
                "DELETE FROM runs WHERE root_run_id = ? OR run_id = ?",
                (run_id, run_id),
            )
            await conn.commit()

    # -- Event operations --

    async def upsert_event(self, event: StoredEvent) -> None:
        sql, params = self._event_upsert_sql(event)
        async with self._conn() as conn:
            await conn.execute(sql, params)
            await self._upsert_fts(conn, event)
            await conn.commit()

    async def upsert_events_batch(self, events: list[StoredEvent]) -> None:
        if not events:
            return
        async with self._conn() as conn:
            await conn.execute("BEGIN")
            for event in events:
                try:
                    sql, params = self._event_upsert_sql(event)
                    await conn.execute(sql, params)
                    await self._upsert_fts(conn, event)
                except Exception as exc:
                    # One bad event must not abort the batch.
                    print(
                        f"tracesage: skipping bad event "
                        f"{event.event_id!r}: {exc}",
                        file=sys.stderr,
                    )
            await conn.commit()

    @staticmethod
    async def _upsert_fts(conn: aiosqlite.Connection, event: StoredEvent) -> None:
        cur = await conn.execute("SELECT rowid FROM events WHERE event_id = ?", (event.event_id,))
        found = await cur.fetchone()
        await cur.close()
        if found is None:
            return
        rowid = found[0]
        await conn.execute("DELETE FROM events_fts WHERE rowid = ?", (rowid,))
        name = event.tool_name or event.agent_name or ""
        await conn.execute(
            "INSERT INTO events_fts(rowid, name, summary, error, payload, event_id, root_run_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                rowid,
                name,
                event.summary or "",
                event.error_message or "",
                event.search_text or "",
                event.event_id,
                event.root_run_id,
            ),
        )

    @staticmethod
    def _event_upsert_sql(event: StoredEvent) -> tuple[str, tuple]:
        sql = """
            INSERT INTO events (
                event_id, run_id, parent_run_id, root_run_id, event_type,
                timestamp, agent_name, tool_name, mcp_server, summary, blob_path,
                duration_ms, token_input, token_output, token_cache_read,
                token_cache_write, model, provider, cost_usd, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                run_id = excluded.run_id,
                parent_run_id = excluded.parent_run_id,
                root_run_id = excluded.root_run_id,
                event_type = excluded.event_type,
                timestamp = excluded.timestamp,
                agent_name = excluded.agent_name,
                tool_name = excluded.tool_name,
                mcp_server = excluded.mcp_server,
                summary = excluded.summary,
                blob_path = excluded.blob_path,
                duration_ms = excluded.duration_ms,
                token_input = excluded.token_input,
                token_output = excluded.token_output,
                token_cache_read = excluded.token_cache_read,
                token_cache_write = excluded.token_cache_write,
                model = excluded.model,
                provider = excluded.provider,
                cost_usd = excluded.cost_usd,
                error_message = excluded.error_message
        """
        params = (
            event.event_id,
            event.run_id,
            event.parent_run_id,
            event.root_run_id,
            event.event_type.value,
            _iso(event.timestamp),
            event.agent_name,
            event.tool_name,
            event.mcp_server,
            event.summary,
            event.blob_path,
            event.duration_ms,
            event.token_input,
            event.token_output,
            event.token_cache_read,
            event.token_cache_write,
            event.model,
            event.provider,
            event.cost_usd,
            event.error_message,
        )
        return sql, params

    async def get_journey(self, run_id: str, limit: int | None = None) -> list[StoredEvent]:
        async with self._conn() as conn:
            if limit is None:
                cur = await conn.execute(
                    # (timestamp, event_id) ordering matches iter_journey so the two
                    # return events in the same order even when timestamps collide.
                    "SELECT * FROM events WHERE root_run_id = ? "
                    "ORDER BY timestamp ASC, event_id ASC",
                    (run_id,),
                )
                rows = list(await cur.fetchall())
            else:
                cur = await conn.execute(
                    "SELECT * FROM events WHERE root_run_id = ? "
                    "ORDER BY timestamp DESC, event_id DESC LIMIT ?",
                    (run_id, max(0, limit)),
                )
                rows = list(await cur.fetchall())
                rows.reverse()
            await cur.close()
        return [_row_to_event(r) for r in rows]

    async def iter_journey(
        self, run_id: str, batch_size: int = 500
    ) -> AsyncIterator[StoredEvent]:
        # Keyset pagination on (timestamp, event_id): each page is fetched with a
        # SHORT-LIVED connection that is released BEFORE the (possibly slow) consumer
        # processes/streams the batch. This prevents a slow or abandoned streamed
        # export from pinning a slot in the shared pool semaphore and starving the
        # worker's writes — unlike a single connection held open across all yields.
        last_ts: str | None = None
        last_id: str | None = None
        while True:
            async with self._conn() as conn:
                if last_ts is None:
                    cur = await conn.execute(
                        "SELECT * FROM events WHERE root_run_id = ? "
                        "ORDER BY timestamp ASC, event_id ASC LIMIT ?",
                        (run_id, batch_size),
                    )
                else:
                    cur = await conn.execute(
                        "SELECT * FROM events WHERE root_run_id = ? "
                        "AND (timestamp > ? OR (timestamp = ? AND event_id > ?)) "
                        "ORDER BY timestamp ASC, event_id ASC LIMIT ?",
                        (run_id, last_ts, last_ts, last_id, batch_size),
                    )
                rows = list(await cur.fetchall())
                await cur.close()
            if not rows:
                break
            for r in rows:
                yield _row_to_event(r)
            if len(rows) < batch_size:
                break
            last_ts = rows[-1]["timestamp"]
            last_id = rows[-1]["event_id"]

    async def get_event(self, event_id: str) -> StoredEvent | None:
        async with self._conn() as conn:
            cur = await conn.execute(
                "SELECT * FROM events WHERE event_id = ?", (event_id,)
            )
            row = await cur.fetchone()
            await cur.close()
        if row is None:
            return None
        return _row_to_event(row)

    async def search_events(
        self, q: str, limit: int = 50, run_id: str | None = None
    ) -> list[SearchHit]:
        match = fts_match_query(q)
        if match is None:
            return []
        sql = """
            SELECT f.event_id AS event_id,
                   f.root_run_id AS run_id,
                   snippet(events_fts, 3, '[', ']', '…', 12) AS payload_snip,
                   snippet(events_fts, 1, '[', ']', '…', 12) AS summary_snip,
                   e.event_type AS event_type,
                   e.timestamp AS timestamp,
                   COALESCE(e.tool_name, e.agent_name, '') AS name
              FROM events_fts f
              JOIN events e ON e.event_id = f.event_id
             WHERE events_fts MATCH ?
               AND (? IS NULL OR f.root_run_id = ?)
             ORDER BY bm25(events_fts)
             LIMIT ?
        """
        async with self._conn() as conn:
            cur = await conn.execute(sql, (match, run_id, run_id, max(0, limit)))
            rows = await cur.fetchall()
            await cur.close()
        hits: list[SearchHit] = []
        for row in rows:
            snippet = row["payload_snip"] or row["summary_snip"] or ""
            hits.append(
                SearchHit(
                    run_id=row["run_id"],
                    event_id=row["event_id"],
                    event_type=row["event_type"],
                    name=row["name"] or None,
                    snippet=snippet,
                    timestamp=_parse_dt(row["timestamp"]),  # type: ignore[arg-type]
                )
            )
        return hits

    async def list_threads(
        self,
        limit: int = 50,
        offset: int = 0,
        q: str | None = None,
        tag: str | None = None,
        sort: str = "recent",
        min_turns: int = 1,
        no_source: bool = False,
    ) -> tuple[list[ThreadSummary], int]:
        order = _THREAD_ORDER.get(sort, _THREAD_ORDER["recent"])
        clauses = ["thread_id IS NOT NULL", "run_id = root_run_id"]
        params: list = []
        if q:
            text = ["thread_id LIKE ? ESCAPE '\\'"]
            params.append(like_contains(q))
            match = fts_match_query(q)
            if match is not None:
                text.append(
                    "thread_id IN (SELECT r4.thread_id FROM runs r4 WHERE r4.run_id IN"
                    " (SELECT root_run_id FROM events_fts WHERE events_fts MATCH ?))"
                )
                params.append(match)
            clauses.append("(" + " OR ".join(text) + ")")
        if tag:
            clauses.append("tags LIKE ? ESCAPE '\\'")
            params.append(like_contains(tag))
        if no_source:
            clauses.append(_NO_SOURCE_CLAUSE)
        where = " AND ".join(clauses)
        min_turns = max(1, min_turns)
        count_sql = (
            f"SELECT COUNT(*) AS n FROM (SELECT thread_id FROM runs WHERE {where}"  # noqa: S608
            " GROUP BY thread_id HAVING COUNT(*) >= ?)"
        )
        list_sql = f"""
            SELECT thread_id,
                   COUNT(*) AS run_count,
                   MIN(started_at) AS first_at,
                   MAX(COALESCE(completed_at, started_at)) AS last_at,
                   COALESCE(SUM(total_tokens_input), 0) AS total_tokens_input,
                   COALESCE(SUM(total_tokens_output), 0) AS total_tokens_output,
                   SUM(total_cost_usd) AS total_cost_usd,
                   (
                       SELECT status FROM runs r2
                        WHERE r2.thread_id = runs.thread_id
                          AND r2.run_id = r2.root_run_id
                        ORDER BY r2.started_at DESC
                        LIMIT 1
                   ) AS last_status,
                   (
                       SELECT r2.run_id FROM runs r2
                        WHERE r2.thread_id = runs.thread_id
                          AND r2.run_id = r2.root_run_id
                        ORDER BY r2.started_at DESC
                        LIMIT 1
                   ) AS last_run_id,
                   MAX(tags) AS tags,
                   (
                       SELECT e.summary FROM events e
                         JOIN runs r3 ON r3.run_id = e.run_id
                        WHERE r3.thread_id = runs.thread_id
                          AND e.event_type = 'chain_start'
                          AND e.run_id = e.root_run_id
                        ORDER BY e.timestamp ASC
                        LIMIT 1
                   ) AS label
              FROM runs
             WHERE {where}
             GROUP BY thread_id
            HAVING COUNT(*) >= ?
             ORDER BY {order}
             LIMIT ? OFFSET ?
        """  # noqa: S608
        async with self._conn() as conn:
            cur = await conn.execute(count_sql, (*params, min_turns))
            total_row = await cur.fetchone()
            await cur.close()
            total = int(total_row["n"]) if total_row is not None else 0
            cur = await conn.execute(list_sql, (*params, min_turns, limit, offset))
            rows = await cur.fetchall()
            await cur.close()
        threads = [
            ThreadSummary(
                thread_id=row["thread_id"],
                run_count=int(row["run_count"] or 0),
                first_at=_parse_dt(row["first_at"]),
                last_at=_parse_dt(row["last_at"]),
                total_tokens_input=int(row["total_tokens_input"] or 0),
                total_tokens_output=int(row["total_tokens_output"] or 0),
                total_cost_usd=(
                    float(row["total_cost_usd"]) if row["total_cost_usd"] is not None else None
                ),
                last_status=row["last_status"] or "running",
                label=row["label"] or None,
                last_run_id=row["last_run_id"],
                tags=_parse_tags(row["tags"]),
            )
            for row in rows
        ]
        return threads, total

    async def get_thread_runs(self, thread_id: str) -> list[Run]:
        async with self._conn() as conn:
            cur = await conn.execute(
                "SELECT * FROM runs WHERE thread_id = ? AND run_id = root_run_id "
                "ORDER BY started_at ASC",
                (thread_id,),
            )
            rows = await cur.fetchall()
            await cur.close()
        return [_row_to_run(r) for r in rows]

    async def add_note(self, run_id: str, text: str, author: str | None = None) -> RunNote:
        created = _iso(datetime.now(timezone.utc))
        async with self._conn() as conn:
            cur = await conn.execute(
                "INSERT INTO run_notes(run_id, created_at, author, text) VALUES (?, ?, ?, ?)",
                (run_id, created, author, text),
            )
            note_id = cur.lastrowid
            await cur.close()
            await conn.commit()
        return RunNote(
            id=int(note_id or 0),
            run_id=run_id,
            created_at=_parse_dt(created),  # type: ignore[arg-type]
            author=author,
            text=text,
        )

    async def list_notes(self, run_id: str) -> list[RunNote]:
        async with self._conn() as conn:
            cur = await conn.execute(
                "SELECT id, run_id, created_at, author, text FROM run_notes "
                "WHERE run_id = ? ORDER BY id ASC",
                (run_id,),
            )
            rows = await cur.fetchall()
            await cur.close()
        return [
            RunNote(
                id=int(row["id"]),
                run_id=row["run_id"],
                created_at=_parse_dt(row["created_at"]),  # type: ignore[arg-type]
                author=row["author"],
                text=row["text"],
            )
            for row in rows
        ]

    async def set_run_tags(
        self, run_id: str, add: list[str] | None = None, remove: list[str] | None = None
    ) -> list[str] | None:
        run = await self.get_run(run_id)
        if run is None:
            return None
        tags = list(run.tags)
        for tag in remove or []:
            tags = [t for t in tags if t != tag]
        for tag in add or []:
            if tag and tag not in tags:
                tags.append(tag)
        async with self._conn() as conn:
            await conn.execute(
                "UPDATE runs SET tags = ? WHERE run_id = ?",
                (json.dumps(tags), run_id),
            )
            await conn.commit()
        return tags

    async def recompute_run_costs(self, run_id: str) -> None:
        """Recompute step, token and cost totals. Same statement as recompute_run_counters."""
        await self.recompute_run_counters(run_id)

    async def needs_reindex(self) -> bool:
        async with self._conn() as conn:
            cur = await conn.execute("SELECT value FROM meta WHERE key = 'needs_reindex'")
            row = await cur.fetchone()
            await cur.close()
        return row is not None and row["value"] == "1"

    async def set_meta(self, key: str, value: str) -> None:
        async with self._conn() as conn:
            await conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            await conn.commit()

    async def replace_event_search(
        self,
        event_id: str,
        *,
        search_text: str,
        model: str | None = None,
        provider: str | None = None,
        cost_usd: float | None = None,
    ) -> None:
        """Update FTS payload text and optional pricing columns for one event."""
        async with self._conn() as conn:
            await conn.execute(
                "UPDATE events SET "
                "model = COALESCE(?, model), "
                "provider = COALESCE(?, provider), "
                "cost_usd = COALESCE(?, cost_usd) "
                "WHERE event_id = ?",
                (model, provider, cost_usd, event_id),
            )
            cur = await conn.execute("SELECT * FROM events WHERE event_id = ?", (event_id,))
            row = await cur.fetchone()
            await cur.close()
            if row is not None:
                stored = _row_to_event(row)
                stored.search_text = search_text
                await self._upsert_fts(conn, stored)
            await conn.commit()

    async def iter_all_events(self, batch_size: int = 500) -> AsyncIterator[StoredEvent]:
        """Every event, oldest first. Used by ``tracesage reindex``."""
        last_ts: str | None = None
        last_id: str | None = None
        while True:
            async with self._conn() as conn:
                if last_ts is None:
                    cur = await conn.execute(
                        "SELECT * FROM events ORDER BY timestamp ASC, event_id ASC LIMIT ?",
                        (batch_size,),
                    )
                else:
                    cur = await conn.execute(
                        "SELECT * FROM events WHERE timestamp > ? "
                        "OR (timestamp = ? AND event_id > ?) "
                        "ORDER BY timestamp ASC, event_id ASC LIMIT ?",
                        (last_ts, last_ts, last_id, batch_size),
                    )
                rows = list(await cur.fetchall())
                await cur.close()
            if not rows:
                break
            for row in rows:
                yield _row_to_event(row)
            if len(rows) < batch_size:
                break
            last_ts = rows[-1]["timestamp"]
            last_id = rows[-1]["event_id"]

    # -- Stats and topology --

    async def get_stats(self) -> dict:
        # Only count ROOT runs (run_id = root_run_id). Sub-runs are auto-created
        # to satisfy the events FK and never receive a terminal status update,
        # so including them would inflate `total_runs` and `running` and
        # under-report `completed` ratio.
        sql = """
            SELECT
                COUNT(*) AS total_runs,
                SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                SUM(CASE WHEN status = 'interrupted' THEN 1 ELSE 0 END) AS interrupted,
                COALESCE(SUM(total_tokens_input), 0) AS total_tokens_input,
                COALESCE(SUM(total_tokens_output), 0) AS total_tokens_output,
                SUM(total_cost_usd) AS total_cost_usd
              FROM runs
             WHERE run_id = root_run_id
        """
        avg_dur_sql = """
            SELECT AVG(duration_ms) AS avg_dur
              FROM events
             WHERE duration_ms IS NOT NULL
        """
        async with self._conn() as conn:
            cur = await conn.execute(sql)
            row = await cur.fetchone()
            await cur.close()
            cur = await conn.execute(avg_dur_sql)
            dur_row = await cur.fetchone()
            await cur.close()

        avg_duration_ms = (
            float(dur_row["avg_dur"])
            if dur_row is not None and dur_row["avg_dur"] is not None
            else 0.0
        )

        try:
            # Brief explicitly mandates os.path.getsize for db_size_bytes.
            # The stat call is fast enough not to warrant a thread offload.
            db_size = os.path.getsize(self._db_path)  # noqa: ASYNC240
        except OSError:
            db_size = 0

        return {
            "total_runs": int(row["total_runs"] or 0) if row else 0,
            "running": int(row["running"] or 0) if row else 0,
            "completed": int(row["completed"] or 0) if row else 0,
            "failed": int(row["failed"] or 0) if row else 0,
            "interrupted": int(row["interrupted"] or 0) if row else 0,
            "avg_duration_ms": avg_duration_ms,
            "total_tokens_input": int(row["total_tokens_input"] or 0) if row else 0,
            "total_tokens_output": int(row["total_tokens_output"] or 0) if row else 0,
            "total_cost_usd": (
                float(row["total_cost_usd"])
                if row is not None and row["total_cost_usd"] is not None
                else None
            ),
            "db_size_bytes": db_size,
        }

    async def _scope_run_ids(
        self, conn: aiosqlite.Connection, scope: str | None
    ) -> list[str] | None:
        """Resolve a topology/tools scope string to the root_run_ids it covers.

        - None / "all"     -> None (all-time aggregate; back-compat).
        - "run:<id>"       -> [<id>] (a single run's structure).
        - "thread:<id>"    -> every root run of that conversation.
        - "last_n:<N>"     -> the N most-recent root runs.
        Returns [] when the scope matches no runs (caller returns an empty result).
        """
        if not scope or scope == "all":
            return None
        if scope.startswith("run:"):
            rid = scope[4:].strip()
            return [rid] if rid else None
        if scope.startswith("thread:"):
            thread_id = scope[len("thread:"):].strip()
            if not thread_id:
                return []
            cur = await conn.execute(
                "SELECT run_id FROM runs WHERE thread_id = ? AND run_id = root_run_id "
                "ORDER BY started_at ASC",
                (thread_id,),
            )
            rows = await cur.fetchall()
            await cur.close()
            return [r["run_id"] for r in rows]
        if scope.startswith("last_n:"):
            try:
                n = max(1, min(1000, int(scope.split(":", 1)[1])))
            except (ValueError, IndexError):
                return None
            cur = await conn.execute(
                "SELECT root_run_id FROM runs WHERE run_id = root_run_id "
                "ORDER BY started_at DESC LIMIT ?",
                (n,),
            )
            rows = await cur.fetchall()
            await cur.close()
            return [r["root_run_id"] for r in rows]
        return None

    async def get_topology(self, scope: str | None = None) -> Topology:
        # Aggregate per-node stats from events.
        #
        # Counting model:
        #   - invocation_count = number of *_start (or agent_action) events for that
        #     (kind, name). One start = one invocation. _end / _error events do not
        #     bump invocations (they are paired with a prior start).
        #   - error_count = number of *_error events for that (kind, name).
        #   - duration aggregates use _end events (which carry duration_ms).
        #
        # Edge model:
        #   - One row per (parent_invocation, child_invocation), produced by joining
        #     each child *_start event to its single matching parent *_start event.
        #     This makes COUNT(*) equal "how many times parent invoked child", not
        #     a Cartesian product of every event under either side.
        #
        # Kind classification:
        #   LLM/retriever/tool win over chain/agent (based on event_type/tool_name).
        #   Among chain-shaped events: LCEL Runnable primitives + LangGraph itself
        #   are 'chain'. Any other named user node is provisionally 'agent', then
        #   demoted to 'chain' here in Python if it has no descendant events
        #   (a leaf node — typically a routing/conditional-edge function).
        kind_case = """
            CASE
                WHEN event_type IN ('chat_model_start','llm_start','llm_end','llm_error')
                    THEN 'llm'
                WHEN event_type IN ('retriever_start','retriever_end','retriever_error')
                    THEN 'retriever'
                WHEN tool_name IS NOT NULL
                    THEN 'tool'
                WHEN agent_name LIKE 'Runnable%'
                  OR agent_name = 'LangGraph'
                  OR agent_name LIKE '%OutputParser'
                  OR agent_name LIKE '%PromptTemplate'
                  OR agent_name = 'StrOutputParser'
                    THEN 'chain'
                WHEN agent_name IS NOT NULL
                    THEN 'agent'
                ELSE NULL
            END
        """
        # kind_case is a hardcoded SQL CASE expression — no user input.

        # Edge SQL: restrict both sides to canonical "start" events so each
        # pairing represents one actual parent->child invocation.
        start_event_types = (
            "('chain_start','tool_start','llm_start','chat_model_start',"
            "'retriever_start','agent_action')"
        )
        error_event_types = "('chain_error','tool_error','llm_error','retriever_error')"

        # Per-node aggregates are computed in SQL so memory scales with the number
        # of distinct nodes, not events (an all-time scope can span millions of
        # rows). `has_children` flags nodes with any descendant event on any of
        # their run_ids, used below to demote leaf 'agent' nodes to 'chain'.
        # {scope_where} / {scope_and} are filled with `root_run_id IN (?, ...)`.
        node_agg_sql = f"""
            WITH ev AS (
                SELECT event_type, mcp_server, duration_ms, cost_usd, timestamp, run_id,
                       {kind_case} AS kind,
                       COALESCE(tool_name, agent_name) AS display_name
                  FROM events{{scope_where}}
            )
            SELECT kind, display_name,
                   SUM(CASE WHEN event_type IN {start_event_types} THEN 1 ELSE 0 END)
                       AS invocations,
                   SUM(CASE WHEN event_type IN {error_event_types} THEN 1 ELSE 0 END)
                       AS errors,
                   COALESCE(SUM(duration_ms), 0) AS total_duration_ms,
                   SUM(cost_usd) AS total_cost_usd,
                   COUNT(duration_ms) AS duration_samples,
                   MAX(timestamp) AS last_seen,
                   MIN(mcp_server) AS source,
                   MAX(CASE WHEN run_id IN (
                           SELECT parent_run_id FROM events
                            WHERE parent_run_id IS NOT NULL{{scope_and}}
                       ) THEN 1 ELSE 0 END) AS has_children
              FROM ev
             WHERE kind IS NOT NULL AND display_name IS NOT NULL
             GROUP BY kind, display_name
        """  # noqa: S608

        # Most-recent <=2000 durations per node for an approximate p99, bounded
        # per node by the window function rather than by loading every row.
        durations_sql = f"""
            SELECT kind, display_name, duration_ms FROM (
                SELECT {kind_case} AS kind,
                       COALESCE(tool_name, agent_name) AS display_name,
                       duration_ms,
                       ROW_NUMBER() OVER (
                           PARTITION BY {kind_case}, COALESCE(tool_name, agent_name)
                           ORDER BY timestamp DESC
                       ) AS rn
                  FROM events
                 WHERE duration_ms IS NOT NULL{{scope_and}}
            )
             WHERE rn <= 2000 AND kind IS NOT NULL AND display_name IS NOT NULL
        """  # noqa: S608
        kind_p = (
            kind_case
            .replace("event_type", "p.event_type")
            .replace("tool_name", "p.tool_name")
            .replace("agent_name", "p.agent_name")
        )
        kind_c = (
            kind_case
            .replace("event_type", "c.event_type")
            .replace("tool_name", "c.tool_name")
            .replace("agent_name", "c.agent_name")
        )
        edge_sql = f"""
            SELECT
                {kind_p} AS src_kind,
                COALESCE(p.tool_name, p.agent_name) AS src_name,
                {kind_c} AS tgt_kind,
                COALESCE(c.tool_name, c.agent_name) AS tgt_name,
                MAX(c.timestamp) AS last_seen,
                COUNT(*) AS n
              FROM events c
              JOIN events p ON p.run_id = c.parent_run_id
             WHERE c.event_type IN {start_event_types}
               AND p.event_type IN {start_event_types}
               AND COALESCE(p.tool_name, p.agent_name) IS NOT NULL
               AND COALESCE(c.tool_name, c.agent_name) IS NOT NULL
               AND (
                    COALESCE(p.tool_name, p.agent_name) <> COALESCE(c.tool_name, c.agent_name)
                    OR ({kind_p} <> {kind_c})
               )
             GROUP BY src_kind, src_name, tgt_kind, tgt_name
        """  # noqa: S608

        async with self._conn() as conn:
            run_ids = await self._scope_run_ids(conn, scope)
            if run_ids == []:
                return Topology(nodes=[], edges=[])  # scope matched no runs
            if run_ids is None:
                scope_where = scope_and = scope_edge = ""
                params: tuple = ()
            else:
                ph = ",".join("?" * len(run_ids))
                scope_where = f" WHERE root_run_id IN ({ph})"
                scope_and = f" AND root_run_id IN ({ph})"
                scope_edge = f" AND c.root_run_id IN ({ph})"
                params = tuple(run_ids)
            # Inject the scope filter (values bound via ? — no interpolation of data).
            node_sql_x = node_agg_sql.format(scope_where=scope_where, scope_and=scope_and)
            durations_sql_x = durations_sql.format(scope_and=scope_and)
            edge_sql_x = edge_sql.replace(
                f"WHERE c.event_type IN {start_event_types}",
                f"WHERE c.event_type IN {start_event_types}{scope_edge}",
            )
            servers_sql_x = (
                "SELECT DISTINCT mcp_server FROM events WHERE mcp_server IS NOT NULL"  # noqa: S608 - scope clause is static, values bound via ?
                + scope_and
            )

            cur = await conn.execute(node_sql_x, params + params)
            node_rows = await cur.fetchall()
            await cur.close()
            cur = await conn.execute(durations_sql_x, params)
            duration_rows = await cur.fetchall()
            await cur.close()
            cur = await conn.execute(edge_sql_x, params)
            edge_rows = await cur.fetchall()
            await cur.close()
            cur = await conn.execute(servers_sql_x, params)
            server_rows = await cur.fetchall()
            await cur.close()

        # Servers active within the scope (any tool of theirs ran). Used to gate the
        # registered-tool folding below so a server removed from the app (or simply
        # not used in the scoped run) doesn't linger in a scoped topology.
        scoped = run_ids is not None
        active_servers = {r["mcp_server"] for r in server_rows}

        durations_by_node: dict[str, list[int]] = defaultdict(list)
        for r in duration_rows:
            durations_by_node[f"{r['kind']}:{r['display_name']}"].append(int(r["duration_ms"]))

        agg: dict[str, dict] = {}
        for r in node_rows:
            node_id = f"{r['kind']}:{r['display_name']}"
            agg[node_id] = {
                "type": r["kind"],
                "name": r["display_name"],
                "invocations": int(r["invocations"] or 0),
                "errors": int(r["errors"] or 0),
                "total_duration_ms": int(r["total_duration_ms"] or 0),
                "total_cost_usd": (
                    float(r["total_cost_usd"]) if r["total_cost_usd"] is not None else None
                ),
                "duration_samples": int(r["duration_samples"] or 0),
                "durations": durations_by_node.get(node_id, []),
                "last_seen": _parse_dt(r["last_seen"]),
                "source": r["source"],
                "has_children": bool(r["has_children"]),
            }

        # Demote leaf 'agent' nodes (no descendant events on any of their run_ids)
        # to 'chain'. These are typically LangGraph conditional-edge wrappers and
        # other deterministic transformations, not real agents.
        renamed_keys: list[tuple[str, str]] = []
        for node_id, b in list(agg.items()):
            if b["type"] != "agent":
                continue
            if not b["has_children"]:
                # No run for this node has any descendant events. Demote.
                b["type"] = "chain"
                new_id = f"chain:{b['name']}"
                renamed_keys.append((node_id, new_id))
        for old_id, new_id in renamed_keys:
            agg[new_id] = agg.pop(old_id)

        nodes: list[TopologyNode] = []
        for node_id, b in agg.items():
            samples = b["duration_samples"]
            avg_ms = (b["total_duration_ms"] / samples) if samples else 0.0
            # Approximate p99 over the most-recent <=2000 duration samples per
            # node (bounded by durations_sql's window). Exact for <=2000 samples.
            durs = sorted(b["durations"])
            p99 = durs[max(0, int(len(durs) * 0.99) - 1)] if durs else None
            nodes.append(
                TopologyNode(
                    id=node_id,
                    name=b["name"],
                    type=b["type"],
                    source=b["source"],
                    invocation_count=b["invocations"],
                    error_count=b["errors"],
                    total_duration_ms=b["total_duration_ms"],
                    total_cost_usd=b["total_cost_usd"],
                    avg_duration_ms=avg_ms,
                    p99_duration_ms=p99,
                    last_seen=b["last_seen"],
                )
            )

        # If we demoted any agent->chain nodes, edge endpoints that pointed at
        # the old "agent:<name>" id need to be rewritten to "chain:<name>".
        rename_map = {old_id: new_id for old_id, new_id in renamed_keys}

        edges: list[TopologyEdge] = []
        for er in edge_rows:
            src_kind = er["src_kind"]
            tgt_kind = er["tgt_kind"]
            src_name = er["src_name"]
            tgt_name = er["tgt_name"]
            if not src_kind or not tgt_kind or not src_name or not tgt_name:
                continue
            src_id = rename_map.get(f"{src_kind}:{src_name}", f"{src_kind}:{src_name}")
            tgt_id = rename_map.get(f"{tgt_kind}:{tgt_name}", f"{tgt_kind}:{tgt_name}")
            if src_id == tgt_id:
                continue
            edges.append(
                TopologyEdge(
                    source=src_id,
                    target=tgt_id,
                    count=int(er["n"]),
                    last_seen=_parse_dt(er["last_seen"]),
                )
            )

        # Bring in EVERY tool each MCP server provides (from the persisted registry),
        # even ones never invoked, so opening a server shows all its connected tools.
        nodes_by_id = {n.id: n for n in nodes}
        registered = await self.get_mcp_tools()
        for server, tool_names in registered.items():
            # When scoped, only show a registered server's (possibly uncalled) tools
            # if that server was actually active in the scoped run(s).
            if scoped and server not in active_servers:
                continue
            for tname in tool_names:
                tid = f"tool:{tname}"
                node = nodes_by_id.get(tid)
                if node is None:
                    node = TopologyNode(
                        id=tid, name=tname, type="tool", source=server, invocation_count=0
                    )
                    nodes.append(node)
                    nodes_by_id[tid] = node
                elif not node.source:
                    node.source = server  # registry is authoritative for provenance

        # Synthesize one node per MCP server plus (mcp server -> tool) edges, so the
        # topology shows each MCP server as a first-class node with ALL its tools
        # hanging off it (called or not), alongside the agent->tool call edges.
        mcp_agg: dict[str, dict] = {}
        for node in nodes:
            if node.type == "tool" and node.source:
                m = mcp_agg.setdefault(
                    node.source, {"invocations": 0, "errors": 0, "last_seen": None}
                )
                m["invocations"] += node.invocation_count
                m["errors"] += node.error_count
                if node.last_seen and (
                    m["last_seen"] is None or node.last_seen > m["last_seen"]
                ):
                    m["last_seen"] = node.last_seen
                edges.append(
                    TopologyEdge(
                        source=f"mcp:{node.source}",
                        target=node.id,
                        count=node.invocation_count,
                        last_seen=node.last_seen,
                    )
                )
        for server, m in mcp_agg.items():
            nodes.append(
                TopologyNode(
                    id=f"mcp:{server}",
                    name=server,
                    type="mcp",
                    source=server,
                    invocation_count=m["invocations"],
                    error_count=m["errors"],
                    last_seen=m["last_seen"],
                )
            )

        # Caller -> MCP-server edges: when an agent/chain invokes a tool that belongs
        # to an MCP server, link the caller to the server too, so the agent <-> mcp
        # relationship is visible (and summarised per server, not per tool).
        caller_mcp: dict[tuple[str, str], dict] = {}
        for e in list(edges):
            tgt = nodes_by_id.get(e.target)
            if tgt and tgt.type == "tool" and tgt.source and not e.source.startswith("mcp:"):
                key = (e.source, f"mcp:{tgt.source}")
                a = caller_mcp.setdefault(key, {"count": 0, "last_seen": None})
                a["count"] += e.count
                if e.last_seen and (a["last_seen"] is None or e.last_seen > a["last_seen"]):
                    a["last_seen"] = e.last_seen
        for (src, mcp_id), a in caller_mcp.items():
            edges.append(
                TopologyEdge(source=src, target=mcp_id, count=a["count"], last_seen=a["last_seen"])
            )

        return Topology(nodes=nodes, edges=edges)

    async def get_tool_inventory(self, scope: str | None = None) -> dict:
        """Tools grouped by source (MCP server name, or 'local' when unattributed).

        `scope` matches :meth:`get_topology` ("run:<id>" / "last_n:<N>" / "all") so the
        panel agrees with the topology. Returns {"sources": [{source, kind, tool_count,
        invocation_count, error_count, tools: [{name, invocations, errors}]}]} sorted
        with MCP servers first (by name) then the local bucket last.
        """
        sql = """
            SELECT
                mcp_server,
                tool_name,
                SUM(CASE WHEN event_type = 'tool_start' THEN 1 ELSE 0 END) AS invocations,
                SUM(CASE WHEN event_type = 'tool_error' THEN 1 ELSE 0 END) AS errors
              FROM events
             WHERE tool_name IS NOT NULL
        """
        async with self._conn() as conn:
            run_ids = await self._scope_run_ids(conn, scope)
            if run_ids == []:
                return {"sources": []}  # scope matched no runs
            params: tuple = ()
            if run_ids is not None:
                ph = ",".join("?" * len(run_ids))
                sql += f" AND root_run_id IN ({ph})"
                params = tuple(run_ids)
            sql += " GROUP BY mcp_server, tool_name"
            cur = await conn.execute(sql, params)
            rows = await cur.fetchall()
            await cur.close()
        scoped = run_ids is not None
        active_servers = {r["mcp_server"] for r in rows if r["mcp_server"]}

        grouped: dict[str | None, dict] = {}
        for r in rows:
            server = r["mcp_server"]  # None for local/unattributed tools
            bucket = grouped.setdefault(
                server,
                {"invocation_count": 0, "error_count": 0, "tools": []},
            )
            invocations = int(r["invocations"] or 0)
            errors = int(r["errors"] or 0)
            bucket["invocation_count"] += invocations
            bucket["error_count"] += errors
            bucket["tools"].append(
                {"name": r["tool_name"], "invocations": invocations, "errors": errors}
            )

        # Fold in every registered MCP tool, even ones never invoked, so the
        # inventory matches the topology (a server lists ALL the tools it provides).
        registered = await self.get_mcp_tools()
        for server, names in registered.items():
            # When scoped, only fold a registered server's tools if it was active.
            if scoped and server not in active_servers:
                continue
            bucket = grouped.setdefault(
                server, {"invocation_count": 0, "error_count": 0, "tools": []}
            )
            have = {t["name"] for t in bucket["tools"]}
            for name in names:
                if name not in have:
                    bucket["tools"].append({"name": name, "invocations": 0, "errors": 0})
                    have.add(name)

        sources: list[dict] = []
        # MCP servers first (sorted by name), then the local bucket last.
        for server in sorted(k for k in grouped if k is not None):
            b = grouped[server]
            b["tools"].sort(key=lambda t: t["name"])
            sources.append(
                {
                    "source": server,
                    "kind": "mcp",
                    "tool_count": len(b["tools"]),
                    "invocation_count": b["invocation_count"],
                    "error_count": b["error_count"],
                    "tools": b["tools"],
                }
            )
        if None in grouped:
            b = grouped[None]
            b["tools"].sort(key=lambda t: t["name"])
            sources.append(
                {
                    "source": "local",
                    "kind": "local",
                    "tool_count": len(b["tools"]),
                    "invocation_count": b["invocation_count"],
                    "error_count": b["error_count"],
                    "tools": b["tools"],
                }
            )
        return {"sources": sources}

    # -- MCP tool registry --

    async def upsert_mcp_tools(self, server: str, tool_names: list[str]) -> None:
        """Persist which tools an MCP server provides (idempotent). Lets the topology
        show every tool of a server even if it was never invoked."""
        if not server or not tool_names:
            return
        rows = [(server, name) for name in tool_names if name]
        if not rows:
            return
        async with self._conn() as conn:
            await conn.executemany(
                "INSERT OR IGNORE INTO mcp_tools (server, tool_name) VALUES (?, ?)", rows
            )
            await conn.commit()

    async def get_mcp_tools(self) -> dict[str, list[str]]:
        """Return {mcp_server: [tool names]} from the registry (empty if none)."""
        out: dict[str, list[str]] = {}
        try:
            async with self._conn() as conn:
                cur = await conn.execute("SELECT server, tool_name FROM mcp_tools")
                rows = await cur.fetchall()
                await cur.close()
        except aiosqlite.OperationalError:
            return out  # table absent (pre-v3 DB not yet migrated)
        for r in rows:
            out.setdefault(r["server"], []).append(r["tool_name"])
        for names in out.values():
            names.sort()
        return out
