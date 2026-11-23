# Frameworks beyond LangChain

One script per framework, each with its own data dir under `~/.tracesage/<name>` so the
runs stay isolated. The first two need **no API key**.

| Script | Framework | Needs | How it gets in |
|---|---|---|---|
| [`pydantic_ai_agent.py`](pydantic_ai_agent.py) | Pydantic AI (`TestModel`) | `pip install "tracesage[pydantic-ai]" pydantic-ai-slim` | `instrument=["pydantic_ai"]` |
| [`manual_api.py`](manual_api.py) | none — plain Python | `pip install tracesage` | `tl.span(...)`, `@tl.trace_fn(...)` |
| [`openai_agents_sdk.py`](openai_agents_sdk.py) | OpenAI Agents SDK | `pip install "tracesage[openai-agents]" openai-agents` + `OPENAI_API_KEY` | `instrument=["openai_agents"]` |
| [`crewai_crew.py`](crewai_crew.py) | CrewAI | `pip install "tracesage[crewai]" crewai` + `OPENAI_API_KEY` | `instrument=["crewai"]` |
| [`openai_client.py`](openai_client.py) | `openai` client | `pip install "tracesage[openai]" openai` + `OPENAI_API_KEY` | `instrument=["openai"]` + a manual root span |
| [`anthropic_client.py`](anthropic_client.py) | `anthropic` client | `pip install "tracesage[anthropic]" anthropic` + `ANTHROPIC_API_KEY` | `instrument=["anthropic"]` + a manual root span |
| [`otlp_collector.md`](otlp_collector.md) | anything that speaks OTLP | `pip install "tracesage[otel]"` | `tracesage serve --ingest` + `OTEL_EXPORTER_OTLP_ENDPOINT` |

```bash
python examples/frameworks/pydantic_ai_agent.py     # no key; then open the printed URL
```

How it works, what the spans map to, and the collector mode are documented in
[docs/frameworks.md](../../docs/frameworks.md).
