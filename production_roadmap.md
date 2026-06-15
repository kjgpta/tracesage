# tracelens — Production Readiness Roadmap

A prioritized analysis of what's missing to run tracelens as production-grade
observability infrastructure (not just local development — see the separate dev
notes for that). Grounded in the current codebase.

## Where we are today (the foundation)

A strong **single-node, embedded** core:

- Embedded pipeline: sampling → bounded `asyncio.Queue` → batched `StorageWorker`
  → SQLite (`aiosqlite`) + gzipped filesystem blobs.
- Safety: never-raise handler, fail-closed redaction, deterministic head sampling,
  per-run event cap (circuit breaker), bearer auth, `0.0.0.0`-without-token fail-stop,
  path-traversal guards on blob read **and** write.
- `StorageBackend` is a `Protocol` (~18 methods) → storage is pluggable.
- Schema versioning + migrations, env/TOML config, REST + WebSocket API, a UI, and
  CLI ops: `serve / export / import / stats / runs / gc / doctor / version`.

## The strategic fork (drives prioritization)

The pipeline is **in-process and embedded** — queue, worker, and SQLite live inside
the traced agent process. The single biggest decision:

- **(A) Embedded library** — each app vendors tracelens and writes to its own local DB.
- **(B) Centralized collector** — a standalone service many agents push to.

Most Tier-1 items below only matter for **(B)**. They are flagged `(B)`.

---

## Tier 1 — Foundational, hard to retrofit later (do first)

| Gap | Why production needs it | Seam today |
|---|---|---|
| **Identity fields on `Run`** — `session_id`, `user_id`, `project_id`, `environment`, `release`/version | Multi-turn conversations span *many* runs; production must group by session, attribute to user, isolate by project/tenant, split prod/staging, and pin a release. Today there is only `tags: list[str]`. Retrofitting identity + indexes after data accumulates is painful. | Additive columns + a migration (migration scaffold already exists). **Highest leverage; cheap now.** |
| **Postgres backend** `(B)` | SQLite is single-writer/single-node — it cannot take concurrent writes from multiple agent processes or server replicas. | `StorageBackend` Protocol already abstracts this; needs a second impl + real connection pooling. |
| **Object-store blobs (S3/GCS)** `(B)` | Local-FS blobs don't survive across replicas or ephemeral containers. | `BlobStore` is a clean class; abstract behind an interface. |
| **Remote collection mode** `(B)` | Decouple ingestion from the app so traces survive app crashes/restarts and land centrally. Today ingestion is welded into the traced process. | New `RemoteSink` honoring the same emit contract + an ingest endpoint on the server. |

## Tier 2 — Interop & coverage (adoption blockers)

- **OpenTelemetry / OTLP** — both *export* (Tempo / Datadog / Honeycomb / Grafana) and
  GenAI semantic-convention spans, plus **W3C trace-context propagation** so an agent
  trace correlates with the surrounding distributed trace. The #1 thing that makes
  tracelens fit existing stacks instead of being a silo.
- **Beyond LangChain** — only the LangChain/LangGraph adapter exists. Add a **manual
  API** (`with tracer.span(...)` / `@trace` decorator) so any code can emit, plus
  adapters for OpenAI/Anthropic SDKs, LlamaIndex, CrewAI/AutoGen.
- **Cost tracking** — tokens are captured but not **$**. Add a per-model pricing table
  → cost per event/run/session/user/project. Usually the first metric a team asks for.

## Tier 3 — Quality loop & analytics (the payoff)

- **Feedback, scores, evals, datasets** — capture thumbs up/down, attach numeric/
  categorical scores, run online/offline evals, curate datasets from real traces.
  None exist today. This is what turns traces into product improvement.
- **Search & aggregation** — `list_runs` only paginates + filters by tag. Add full-text
  search, multi-dimensional filtering (tool/model/error/latency/user), and **time-series
  rollups**: error rate over time, latency p50/p95/p99 per agent, throughput, top
  tools/models. `get_stats` is global-only today.
- **Alerting** — thresholds on error rate / latency regression / cost spike / volume →
  webhook / Slack / PagerDuty.

## Tier 4 — Ops & security hardening

- **Automatic retention** — `gc` exists but is **manual CLI only**. Add a background
  scheduler with per-project TTL policies, plus **tail-based sampling** (always keep
  errors/slow traces regardless of head sample rate — today a dropped error is gone).
- **Multi-key auth + RBAC + rate limiting** — single static token, no rotation, no
  scopes (ingest vs read), no per-project keys, no rate limiting, no audit log.
- **Self-observability** — Prometheus `/metrics`, readiness-vs-liveness split, and
  surfacing pipeline **lag / drop / queue-depth** counters (p99 is computed internally
  but drop rate is not exposed). Operators must see when traces are dropped.
- **Durability / backpressure** *(optional)* — disk-spillover buffer or at-least-once
  delivery so a burst or crash doesn't silently drop; a dead-letter path.
- **Packaging** — Docker image, Helm chart, `/api/v1` versioning, published OpenAPI,
  backup/restore guidance.

## Tier 5 — Compliance & sampling sophistication

- **PII / secret detection** — redaction is regex + fail-closed today; add built-in
  detectors (emails, API keys, cards) and field-level allow/deny lists.
- **Data residency / encryption-at-rest** notes for blobs + DB.

---

## Suggested sequencing

1. **Data-model identity** (session / user / project / environment) — cheap, additive,
   unblocks Tiers 3–4. Do it before more data accumulates.
2. **Cost tracking** — high perceived value, small surface (pricing map + rollups).
3. **OTLP export + manual span API** — interop + framework-agnostic capture; unblocks adoption.
4. Then fork on deployment model:
   - **Centralized** → Postgres + object-store blobs + remote collector.
   - **Embedded** → richer in-app analytics + feedback/eval loop.

Architectural (multi-week): remote collection, Postgres, OTLP.
Quick wins (days): identity fields, cost, retention scheduler, `/metrics`.
