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
from contextlib import asynccontextmanager
from datetime import datetime
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
)

_PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA temp_store=MEMORY",
    "PRAGMA cache_size=-64000",
)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


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
             WHERE run_id = ?
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
        async with self._conn() as conn:
            await conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
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
                timestamp, agent_name, tool_name, summary, blob_path,
                duration_ms, token_input, token_output, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                run_id = excluded.run_id,
                parent_run_id = excluded.parent_run_id,
                root_run_id = excluded.root_run_id,
                event_type = excluded.event_type,
                timestamp = excluded.timestamp,
                agent_name = excluded.agent_name,
                tool_name = excluded.tool_name,
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
                "SELECT * FROM events WHERE root_run_id = ? "
                "ORDER BY timestamp ASC",
                (run_id,),
            )
            rows = await cur.fetchall()
            await cur.close()
        return [_row_to_event(r) for r in rows]

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
            SELECT event_type, agent_name, tool_name, duration_ms, timestamp,
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
                "last_seen": None,
                "run_ids": set(),
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
            if row_run_id:
                bucket["run_ids"].add(row_run_id)
            if event_type in start_types:
                bucket["invocations"] += 1
            if event_type in error_types:
                bucket["errors"] += 1
            if duration is not None:
                bucket["total_duration_ms"] += int(duration)
                bucket["duration_samples"] += 1
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
            nodes.append(
                TopologyNode(
                    id=node_id,
                    name=b["name"],
                    type=b["type"],
                    invocation_count=b["invocations"],
                    error_count=b["errors"],
                    total_duration_ms=b["total_duration_ms"],
                    avg_duration_ms=avg_ms,
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

        return Topology(nodes=nodes, edges=edges)
