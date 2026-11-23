# When to use tracesage vs alternatives

`tracesage` is a local, offline debugger for LLM agents — LangChain / LangGraph
natively, and Pydantic AI, the OpenAI Agents SDK, CrewAI or any OpenTelemetry source
through its OTLP ingest. It is not
trying to be an LLM observability platform. Most tools in this space are platforms
(cloud or self-hosted) with evals, datasets and prompt management. tracesage is the
thing you `pip install` to see what your agent graph just did.

## Decision tree

- **You want zero infrastructure (no Docker, no database server, no signup), a live
  view of your LangGraph topology, and nothing leaving your machine** → **tracesage**
- **You want a local tool that also does evals, datasets and a prompt playground,
  and you can live with the Elastic 2.0 license** → **Arize Phoenix** (`pip install`,
  ingests any OpenTelemetry/OpenInference spans)
- **You want CI regression tests with LLM judges on top of tracing** → **MLflow 3**
  (`mlflow ui`, Apache-2.0) or **Comet Opik**
- **You want to edit graph state and re-run from a checkpoint (time-travel),
  breakpoints, and a managed platform** → **LangSmith / LangGraph Studio**
- **You want a self-hosted team platform with RBAC, evals, prompt versioning and
  dashboards** → **Langfuse** (MIT core; self-hosting v3 needs Postgres, ClickHouse,
  Redis and S3-compatible storage)
- **You want a proxy that logs LLM calls without code changes** → **Helicone**

## Feature comparison

| Feature | tracesage | LangSmith | Langfuse | Phoenix | MLflow 3 | Opik | Logfire | Laminar |
|---|---|---|---|---|---|---|---|---|
| Runs fully offline from `pip install` | ✓ | ✗ (cloud; self-host is Enterprise) | ✗ (Docker stack) | ✓ | ✓ | ✗ (cloud or Docker) | ✗ (cloud) | ✗ (cloud or Docker) |
| License | MIT | proprietary | MIT core | Elastic 2.0 | Apache-2.0 | Apache-2.0 | proprietary | Apache-2.0 |
| Live LangGraph topology graph | ✓ | ✓ (Studio) | agent graph view | span tree | span tree | span tree | span tree | span tree |
| MCP tool → server attribution | ✓ | not documented | not documented | not documented | not documented | not documented | not documented | not documented |
| HITL interrupt shown as paused, not failed | ✓ | ✓ (Studio) | not documented | ✗ (known issue) | not documented | not documented | not documented | not documented |
| Threads / sessions | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Cost in $ | ✓ (optional extra, offline) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Full-text search of payloads | ✓ | ✓ | ✓ | not documented | not documented | ✓ | not documented | ✓ |
| Latency waterfall | ✓ | ✓ | ✓ | span timeline | span timeline | span timeline | span timeline | span timeline |
| MCP server over your own traces | ✓ | not documented | not documented | not documented | not documented | not documented | not documented | not documented |
| One-command hook into Claude Code / Cursor / Codex | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| Time-travel / fork from checkpoint | ✗ | ✓ (Studio) | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| Prompt playground / management | ✗ (non-goal) | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ | ✓ |
| Evals / datasets | pytest assertions only | ✓ | ✓ | ✓ | ✓ | ✓ | not documented | ✓ |
| OpenTelemetry | ingest + export (GenAI semconv) | ingest | ingest | ingest + export | ingest + export | ingest | native (it is an OTel backend) | ingest |
| Frameworks | LangChain/LangGraph native; Pydantic AI, OpenAI Agents SDK, CrewAI, OpenAI, Anthropic via OTel; manual spans; Claude Code, Codex and Cursor hooks | all (SDK) | all (SDK) | all (OpenInference) | all (autolog) | all (SDK) | all (OTel) | all (SDK) |
| Terminal CLI (show / watch / diff) | ✓ | ✗ | ✗ | ✗ | partial | ✗ | ✗ | ✗ |
| Multi-user / RBAC | ✗ | ✓ | ✓ | partial | partial | ✓ | ✓ | ✓ |
| PII redaction | ✓ (regex, at capture) | ✓ | ✓ | partial | partial | ✓ | not documented | ✓ |

"—" means we could not confirm the behavior either way.

## Where tracesage is different

- **Nothing to deploy.** Traces go to a SQLite file plus gzipped blobs under
  `~/.tracesage`. The UI is a server inside your process (or, with
  `start_server=False`, you open the same files later with `tracesage serve`).
  There is no account and no per-trace pricing.
- **LangGraph-native.** It shows the live agent topology (nodes, tools, LLMs,
  retrievers) and lets you replay a run step by step. Interrupts are shown as
  paused, and every run carries its LangGraph thread id.
- **MCP attribution.** Each tool call is tagged with the MCP server it came from,
  and the topology shows each server's full tool list.
- **Crash-safe by design.** The callback handler never raises. If storage fails
  you lose trace data, but your agent keeps running.
- **Terminal and pytest friendly.** `tracesage show / watch / diff / export` work
  without a browser. The pytest fixture asserts on tool calls and errors locally,
  with no cloud quota.
- **A bridge out, and in.** OTLP export uses the OpenTelemetry GenAI conventions, so the
  same traces can go to Phoenix, Tempo, Datadog or Honeycomb when you need a
  production backend. In the other direction, `tracesage.instrument("pydantic_ai")`
  (or `openai_agents`, `crewai`, `openai`, `anthropic`) and `tracesage serve --ingest`
  accept OTel spans, so non-LangChain agents get the same UI — see [Frameworks](frameworks.md).

## What tracesage deliberately doesn't do

- Evaluation platform, datasets, annotation queues
- Prompt version control or a playground
- Team collaboration, RBAC, dashboards and alerting
- Cloud SaaS
- A general-purpose OpenTelemetry collector: `tracesage serve --ingest` accepts OTLP
  traces so agent frameworks can send spans to it, but it has no metrics/logs
  pipelines, no processors and no fan-out (see [Frameworks](frameworks.md))

For any of those, the tools above are better. For a developer who wants to debug a
LangGraph at 2am on a flight with no internet, `tracesage` works.

## Migration / coexistence

- Run `tracesage` in dev and a platform in production.
- Run both at once: `callbacks=[tl.handler, langsmith_tracer]`, where `tl` is
  the tracesage tracer from `trace()` / `session()` / `create()`.
- Point `otlp_endpoint` at Phoenix or any OTel backend to forward the same spans.
- Export to JSONL with `tracesage export --all`.

## When NOT to use tracesage

- You need multi-user dashboards or alerting (Langfuse, LangSmith).
- You need a hosted cost dashboard for a whole team (Langfuse, LangSmith, Opik, Logfire, Laminar). Local per-run cost is already in tracesage when `tracesage[pricing]` or `tracesage[apps]` is installed.
- You need eval pipelines with LLM judges and datasets (MLflow, Opik, Phoenix,
  LangSmith, Langfuse).
- You need to fork and re-run a graph from an edited checkpoint (LangGraph Studio).
- You're at million-events-per-day scale in a single Python process (centralize first).

## Roadmap

- Trajectory assertions and baseline-run comparison in the pytest fixture (`assert_cost_below` is already there)
- Loop / runaway detection (repeated identical tool calls, token budget)
