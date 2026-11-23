"""Offline maintenance: backfill search text and dollar cost for existing events."""
from __future__ import annotations

import logging

from tracesage.config import TraceSageConfig
from tracesage.models import EventType
from tracesage.pricing import price_usd
from tracesage.search import extract_search_text
from tracesage.storage.blob_store import BlobStore
from tracesage.storage.sqlite_backend import SQLiteBackend

log = logging.getLogger("tracesage.maintenance")

_LLM_END = EventType.LLM_END


async def reindex(db: SQLiteBackend, blob_store: BlobStore, config: TraceSageConfig) -> dict[str, int]:
    """Fill FTS payload text from blobs and recompute cost. Never raises per event."""
    priced = 0
    indexed = 0
    roots: set[str] = set()
    async for event in db.iter_all_events():
        roots.add(event.root_run_id)
        search_text = ""
        if config.search_index_payloads and event.blob_path:
            try:
                payload = await blob_store.read(event.blob_path)
                search_text = extract_search_text(
                    payload, limit=config.search_max_chars_per_event
                )
            except Exception as exc:
                log.warning("reindex blob read failed for %s: %s", event.event_id, exc)
                search_text = ""
        model = event.model
        provider = event.provider
        cost = event.cost_usd
        if event.event_type == _LLM_END:
            if not model:
                model = event.agent_name
            if cost is None:
                try:
                    cost = price_usd(
                        model,
                        provider,
                        event.token_input,
                        event.token_output,
                        event.token_cache_read,
                        event.token_cache_write,
                        config,
                    )
                except Exception as exc:
                    log.warning("reindex price failed for %s: %s", event.event_id, exc)
                    cost = None
                if cost is not None:
                    priced += 1
        try:
            await db.replace_event_search(
                event.event_id,
                search_text=search_text,
                model=model,
                provider=provider,
                cost_usd=cost,
            )
            indexed += 1
        except Exception as exc:
            log.warning("reindex event %s failed: %s", event.event_id, exc)
            continue
    for root in roots:
        try:
            await db.recompute_run_costs(root)
        except Exception as exc:
            log.warning("reindex cost rollup failed for %s: %s", root, exc)
            continue
    await db.set_meta("needs_reindex", "0")
    return {"events": indexed, "priced": priced, "runs": len(roots)}
