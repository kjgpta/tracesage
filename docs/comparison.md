# When to use tracesage vs alternatives

`tracesage` is one of several tools for LangChain observability. Each has a
sweet spot. Here's when to pick which.

## Decision tree

- **You want zero infrastructure (no Docker, no Postgres, no cloud signup)**
  and a UI you can open immediately → **tracesage**
- **You want full-featured cloud observability and don't mind sending data to LangChain Inc.**
  → **LangSmith** (it's tightly integrated and has a free tier)
- **You want a self-hostable team observability platform with eval, datasets, prompt versioning**
  → **LangFuse** (Docker + Postgres, more features, more setup)
- **You want OpenInference / OpenTelemetry standards compliance for ecosystem export**
  → **Phoenix (Arize)** (also runs locally, ties into broader OTEL)
- **You want a thin proxy that logs LLM calls without changing client code**
  → **Helicone**

## Feature comparison

| Feature | tracesage | LangSmith | LangFuse | Phoenix |
|---|---|---|---|---|
| Local-only mode | ✓ | self-host enterprise | ✓ (Docker) | ✓ (in-process) |
| Pure pip install | ✓ | ✓ (cloud) | ✗ (Docker required) | ✓ |
| LangChain native | ✓ | ✓ (auto-instrument) | ✓ (SDK) | ✓ (OpenInference) |
| Live UI (WebSocket) | ✓ | ✓ | ✓ | ✓ |
| Interactive graph view | ✓ | ✓ | ✓ | ✓ |
| MCP tool attribution | ✓ | ✗ | ✗ | ✗ |
| Multi-user / RBAC | v0.4+ | ✓ | ✓ | ✓ |
| Eval framework | ✗ (non-goal) | ✓ | ✓ | ✓ |
| Dataset management | ✗ (non-goal) | ✓ | ✓ | ✓ |
| Prompt versioning | ✗ (non-goal) | ✓ | ✓ | ✗ |
| OpenTelemetry export | v0.3+ | partial | ✓ | ✓ |
| Cost tracking | v0.3+ | ✓ | ✓ | ✓ |
| PII redaction | ✓ | ✓ | ✓ | partial |
| MIT licensed | ✓ | proprietary | MIT | Elastic v2 |

## Why tracesage exists

`tracesage` aims to be the **smallest possible thing** that gives you a useful
trace UI for a LangChain app — pip install, two lines of code, browser open.

It is intentionally NOT trying to be:

- An evaluation framework
- A prompt version control system
- A team collaboration platform
- A cloud SaaS
- An OpenTelemetry collector (yet)

For any of those, the alternatives above are better. For a developer who wants
to debug a LangGraph at 2am on a flight with no internet, `tracesage` works.

## Migration / coexistence

The data directory is portable JSONL-exportable. You can:

- Run `tracesage` in dev, switch to LangSmith in production.
- Run both — multiple callbacks work simultaneously: `callbacks=[tracer.handler, langsmith_tracer]`.
- Export to JSONL via `tracesage export --all` and import elsewhere.

## When NOT to use tracesage

- You need multi-user dashboards (use LangFuse self-host or LangSmith).
- You need cost reporting / billing alerts (use LangSmith or LangFuse).
- You need eval pipelines wired into CI (use LangSmith / Phoenix / LangFuse).
- You're at million-events-per-day scale on a single Python process (centralize first).

## What we're aiming for in v1.0

- OpenInference / OTLP export
- Cost tracking
- React-based UI with eval scaffolding
- Postgres backend for centralized deployments
- CrewAI / AutoGen / LlamaIndex adapters
