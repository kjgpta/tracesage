# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-05-02

### Added

#### Core pipeline
- LangChain `BaseCallbackHandler` integration for chain, agent, tool, LLM,
  chat-model, retriever events (`on_chain_start`, `on_chat_model_start`, etc.)
- `TraceLens.create()` factory wires storage + worker + server on a single event loop
- Async event queue with batched SQLite writes (50 events / 100 ms default)
- Gzipped blob storage for full event payloads (`*_END` events)
- Pluggable `StorageBackend` protocol (SQLite implementation in v0.1)

#### Server + UI
- FastAPI server with REST + WebSocket endpoints, lifespan-managed
- Endpoints: `/api/runs`, `/api/runs/{id}/journey`, `/api/runs/{id}/steps/{event_id}/full`,
  `/api/stats`, `/api/topology`, `/api/runs/{id}/export?format=jsonl`,
  `DELETE /api/runs/{id}`, `/ws/trace/{run_id}`, `/ws/runs`
- Single-page interactive dashboard with Cytoscape.js + dagre graph view
- Run list, timeline, step drawer, dark/light themes, keyboard shortcuts
- WebSocket reconnect with exponential backoff (1s → 30s)
- Replay mode for runs at 1x/2x/5x speed

#### CLI
- `tracelens serve` (read-only viewer)
- `tracelens export` (JSONL dump)
- `tracelens stats`
- `tracelens gc` (retention)
- `tracelens version`

#### Production safety
- Hard fail-stop when binding non-loopback addresses without `auth_token`
- Bearer-token HTTP auth (skip `/api/health`); constant-time comparison
- WebSocket auth via `?token=` query param or `Sec-WebSocket-Protocol` subprotocol
- Path-traversal guard in BlobStore
- Per-run event cap (circuit breaker) + root-level sampling
- Bounded LRU eviction on all internal run-id maps (no memory leaks in long runs)
- Worker FK auto-creation for sub-runs (handles nested LangGraph correctly)
- `queue.join()` waits for full persistence (not just dequeue)

#### Tests
- 77 unit + integration tests passing across all layers
- 4 viability test systems: order pipeline, research supervisor, parallel review, writer-critic loop
- 100-concurrent stress test (`tests/stress/`)
- Crash-recovery harness (`tools/crash_recovery_test.py`) verified ACID survival
- Bench tool (`tools/bench.py`) with cross-platform throughput numbers

#### Tooling
- GitHub Actions CI matrix: Linux/Windows/macOS × Python 3.11/3.12/3.13
- Trusted Publishing release workflow
- Ruff lint config

### Known limitations

- Centralized multi-producer mode planned for v0.2
- OpenInference / OpenTelemetry export planned for v0.3
- Cost tracking and PII redaction planned for v0.2
- CrewAI / AutoGen / LlamaIndex adapters planned for v0.4+

[0.1.0]: https://github.com/tracelens/tracelens/releases/tag/v0.1.0
