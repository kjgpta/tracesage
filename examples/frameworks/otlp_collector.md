# Sending spans from another process: `tracesage serve --ingest`

Every script in this folder uses the in-process bridge (`instrument=[...]`). When the
agent runs somewhere else — a different process, a container, a Node or Go service, or
an app that already exports OTLP — run tracesage as a collector instead:

```bash
pip install "tracesage[otel]"
tracesage serve --ingest -d ~/.tracesage/collector --open
# tracesage collector at http://127.0.0.1:7842/ui  (data: ~/.tracesage/collector)
#   OTLP/HTTP ingest: POST http://127.0.0.1:7842/v1/traces
```

Then, in the producer, point the standard OpenTelemetry exporter at it. For example the
Pydantic AI script with **no tracesage code at all**:

```bash
pip install pydantic-ai-slim opentelemetry-sdk opentelemetry-exporter-otlp-proto-http
export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:7842
export OTEL_SERVICE_NAME=weather-app
```

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from pydantic_ai import Agent

provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))  # reads OTEL_EXPORTER_OTLP_ENDPOINT
trace.set_tracer_provider(provider)
Agent.instrument_all()

agent = Agent("openai:gpt-4o", instructions="Be concise.")
agent.run_sync("What's the weather in Paris?")
provider.force_flush()
```

The run appears in the collector's UI tagged `otlp` and `weather-app`. With a token
(`tracesage serve --ingest --auth-token ...`) add
`OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer%20<token>"`.

See [docs/frameworks.md](../../docs/frameworks.md) and
[docs/api.md → `POST /v1/traces`](../../docs/api.md#post-v1traces).
