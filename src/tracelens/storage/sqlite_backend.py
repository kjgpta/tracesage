"""SQLite implementation of the StorageBackend protocol.

Uses aiosqlite + an asyncio.Semaphore-based connection pool. Schema and queries
match design doc section 6.5 exactly.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from tracelens.models import (
    EventType,
    Run,
    RunStatus,
    StoredEvent,
    Topology,
    TopologyEdge,
    TopologyNode,
)

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
        graph_definition TEXT,
        error_message TEXT
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
)

# Current on-disk schema version, stamped into PRAGMA user_version by init().
# Future migrations key off the persisted value to upgrade old DBs.
#   v1 -> v2: add events.mcp_server (MCP tool-source attribution).
#   v2 -> v3: add mcp_tools registry table (created by the CREATE TABLE IF NOT
#             EXISTS in _SCHEMA_STATEMENTS, so no ALTER is needed).
_SCHEMA_VERSION = 3

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


def _row_to_run(row: aiosqlite.Row) -> Run:
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
        graph_definition=row["graph_definition"],
        error_message=row["error_message"],
    )


def _row_to_event(row: aiosqlite.Row) -> StoredEvent:
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
        error_message=row["error_message"],
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
            if current < _SCHEMA_VERSION:
                # v1 -> v2: add events.mcp_server to an existing pre-v2 DB. Fresh DBs
                # (current == 0) already have the column from the CREATE TABLE above,
                # so only a real v1 DB needs the ALTER. Guarded for idempotency.
                if current == 1:
                    with suppress(aiosqlite.OperationalError):
                        await conn.execute("ALTER TABLE events ADD COLUMN mcp_server TEXT")
                # v2 -> v3 (mcp_tools table) needs no ALTER — the CREATE TABLE IF NOT
                # EXISTS in the schema loop above already created it on this connection.
                await conn.execute("PRAGMA user_version = 3")
                await conn.commit()

    async def close(self) -> None:
        # No persistent connections held; pool opens/closes per acquire.
        return None

    # -- Run operations --

    async def upsert_run(self, run: Run) -> None:
        sql = """
            INSERT INTO runs (
                run_id, root_run_id, tags, status, started_at, completed_at,
                total_steps, total_tokens_input, total_tokens_output,
                graph_definition, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                root_run_id = excluded.root_run_id,
                tags = excluded.tags,
                status = excluded.status,
                started_at = excluded.started_at,
                completed_at = excluded.completed_at,
                total_steps = excluded.total_steps,
                total_tokens_input = excluded.total_tokens_input,
                total_tokens_output = excluded.total_tokens_output,
                graph_definition = excluded.graph_definition,
                error_message = excluded.error_message
        """
        params = (
            run.run_id,
            run.root_run_id,
            json.dumps(run.tags),
            run.status.value,
            _iso(run.started_at),
            _iso(run.completed_at),
            run.total_steps,
            run.total_tokens_input,
            run.total_tokens_output,
            run.graph_definition,
            run.error_message,
        )
        async with self._conn() as conn:
            await conn.execute(sql, params)
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
    ) -> tuple[list[Run], int]:
        # Filter to top-level runs only (run_id == root_run_id). Sub-runs from nested
        # LangGraph nodes have their own rows for events FK but should not appear in
        # the user-facing run list.
        if status is None or status == "all":
            count_sql = "SELECT COUNT(*) AS n FROM runs WHERE run_id = root_run_id"
            count_params: tuple = ()
            list_sql = (
                "SELECT * FROM runs WHERE run_id = root_run_id "
                "ORDER BY started_at DESC LIMIT ? OFFSET ?"
            )
            list_params: tuple = (limit, offset)
        else:
            count_sql = (
                "SELECT COUNT(*) AS n FROM runs WHERE run_id = root_run_id AND status = ?"
            )
            count_params = (status,)
            list_sql = (
                "SELECT * FROM runs WHERE run_id = root_run_id AND status = ? "
                "ORDER BY started_at DESC LIMIT ? OFFSET ?"
            )
            list_params = (status, limit, offset)

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
    ) -> None:
        sql = """
            UPDATE runs
               SET total_steps = total_steps + ?,
                   total_tokens_input = total_tokens_input + ?,
                   total_tokens_output = total_tokens_output + ?
             WHERE run_id = ?
        """
        async with self._conn() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            await conn.execute(sql, (steps, tokens_in, tokens_out, run_id))
            await conn.commit()

    async def delete_run(self, run_id: str) -> None:
        # Delete the WHOLE run tree, not just the root row. Nested LangGraph runs
        # get their own auto-created run rows (run_id != root_run_id) plus events,
        # which the FK cascade on the single root row would NOT remove. Match by
        # root_run_id (the tree) OR run_id (defensive, if a sub-run id is passed).
        async with self._conn() as conn:
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
                except Exception as exc:
                    # Per CLAUDE.md: one bad event must not abort the batch.
                    print(
                        f"tracelens: skipping bad event "
                        f"{event.event_id!r}: {exc}",
                        file=sys.stderr,
                    )
            await conn.commit()

    @staticmethod
    def _event_upsert_sql(event: StoredEvent) -> tuple[str, tuple]:
        sql = """
            INSERT INTO events (
                event_id, run_id, parent_run_id, root_run_id, event_type,
                timestamp, agent_name, tool_name, mcp_server, summary, blob_path,
                duration_ms, token_input, token_output, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            event.error_message,
        )
        return sql, params

    async def get_journey(self, run_id: str) -> list[StoredEvent]:
        async with self._conn() as conn:
            cur = await conn.execute(
                # (timestamp, event_id) ordering matches iter_journey so the two
                # return events in the same order even when timestamps collide.
                "SELECT * FROM events WHERE root_run_id = ? "
                "ORDER BY timestamp ASC, event_id ASC",
                (run_id,),
            )
            rows = await cur.fetchall()
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
                COALESCE(SUM(total_tokens_input), 0) AS total_tokens_input,
                COALESCE(SUM(total_tokens_output), 0) AS total_tokens_output
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
            "avg_duration_ms": avg_duration_ms,
            "total_tokens_input": int(row["total_tokens_input"] or 0) if row else 0,
            "total_tokens_output": int(row["total_tokens_output"] or 0) if row else 0,
            "db_size_bytes": db_size,
        }

    async def get_topology(self) -> Topology:
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

        # Per-node SQL: also return run_id so we can later detect leaf nodes
        # (no descendants) and demote them from 'agent' to 'chain'.
        node_sql = f"""
            SELECT event_type, agent_name, tool_name, mcp_server, duration_ms, timestamp,
                   error_message, run_id,
                   {kind_case} AS kind,
                   COALESCE(tool_name, agent_name) AS display_name
              FROM events
        """  # noqa: S608

        # Edge SQL: restrict both sides to canonical "start" events so each
        # pairing represents one actual parent->child invocation.
        start_event_types = (
            "('chain_start','tool_start','llm_start','chat_model_start',"
            "'retriever_start','agent_action')"
        )
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

        # Run-ids that have at least one descendant event. Used to demote
        # leaf 'agent' nodes (e.g. LangGraph conditional-edge routing functions
        # like `route()` that return a string but don't call any sub-tools/LLMs).
        has_children_sql = """
            SELECT DISTINCT parent_run_id
              FROM events
             WHERE parent_run_id IS NOT NULL
        """

        async with self._conn() as conn:
            cur = await conn.execute(node_sql)
            event_rows = await cur.fetchall()
            await cur.close()
            cur = await conn.execute(edge_sql)
            edge_rows = await cur.fetchall()
            await cur.close()
            cur = await conn.execute(has_children_sql)
            has_children_rows = await cur.fetchall()
            await cur.close()

        runs_with_children: set[str] = {
            r["parent_run_id"] for r in has_children_rows if r["parent_run_id"]
        }

        # Event types that count as one "invocation" of the node.
        start_types = {
            "chain_start", "tool_start", "llm_start", "chat_model_start",
            "retriever_start", "agent_action",
        }
        # Event types that count as an error of the node.
        error_types = {
            "chain_error", "tool_error", "llm_error", "retriever_error",
        }

        agg: dict[str, dict] = defaultdict(
            lambda: {
                "type": "agent",
                "name": "",
                "invocations": 0,
                "errors": 0,
                "total_duration_ms": 0,
                "duration_samples": 0,
                # Bounded recent-sample window for an approximate p99. maxlen
                # keeps memory flat regardless of invocation count.
                "durations": deque(maxlen=2000),
                "last_seen": None,
                "run_ids": set(),
                # MCP server this node's tool came from (first non-null seen).
                "source": None,
            }
        )
        for r in event_rows:
            event_type = r["event_type"]
            kind = r["kind"]
            display_name = r["display_name"]
            duration = r["duration_ms"]
            ts = _parse_dt(r["timestamp"])
            row_run_id = r["run_id"]
            if not kind or not display_name:
                continue

            node_id = f"{kind}:{display_name}"
            bucket = agg[node_id]
            bucket["type"] = kind
            bucket["name"] = display_name
            if r["mcp_server"] and bucket["source"] is None:
                bucket["source"] = r["mcp_server"]
            if row_run_id:
                bucket["run_ids"].add(row_run_id)
            if event_type in start_types:
                bucket["invocations"] += 1
            if event_type in error_types:
                bucket["errors"] += 1
            if duration is not None:
                bucket["total_duration_ms"] += int(duration)
                bucket["duration_samples"] += 1
                bucket["durations"].append(int(duration))
            if ts is not None and (
                bucket["last_seen"] is None or ts > bucket["last_seen"]
            ):
                bucket["last_seen"] = ts

        # Demote leaf 'agent' nodes (no descendant events on any of their run_ids)
        # to 'chain'. These are typically LangGraph conditional-edge wrappers and
        # other deterministic transformations, not real agents.
        renamed_keys: list[tuple[str, str]] = []
        for node_id, b in list(agg.items()):
            if b["type"] != "agent":
                continue
            if not b["run_ids"]:
                continue
            if not (b["run_ids"] & runs_with_children):
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
            # node (the bounded deque above). Exact for <=2000 samples.
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

    async def get_tool_inventory(self) -> dict:
        """Tools grouped by source (MCP server name, or 'local' when unattributed).

        Returns {"sources": [{source, kind, tool_count, invocation_count,
        error_count, tools: [{name, invocations, errors}]}]} sorted with MCP
        servers first (by name) then the local bucket last.
        """
        sql = """
            SELECT
                mcp_server,
                tool_name,
                SUM(CASE WHEN event_type = 'tool_start' THEN 1 ELSE 0 END) AS invocations,
                SUM(CASE WHEN event_type = 'tool_error' THEN 1 ELSE 0 END) AS errors
              FROM events
             WHERE tool_name IS NOT NULL
             GROUP BY mcp_server, tool_name
        """
        async with self._conn() as conn:
            cur = await conn.execute(sql)
            rows = await cur.fetchall()
            await cur.close()

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
