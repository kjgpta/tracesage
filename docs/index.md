<p align="center">
  <img src="assets/tracesage-logo-badge.png" alt="tracesage" width="420">
</p>

# tracesage

**Local-first observability for LangChain & LangGraph multi-agent systems.**
Drop in two lines, see live execution traces in your browser.

```python
from tracesage import TraceSage

tracer = await TraceSage.create()                          # one-time setup

result = await graph.ainvoke(
    {"input": payload},
    config={"callbacks": [tracer.handler]},                # only line you add
)

# Open http://localhost:7842/ui to see the trace live
```

[Get started in 5 minutes →](quickstart.md){ .md-button .md-button--primary }
[Browse the examples →](examples.md){ .md-button }

---

## Why tracesage

LangChain agents emit a rich callback stream — chain start/end, tool start/end,
LLM start/end, retrieval, errors. tracesage captures all of it without
changing your workflow logic, persists it locally (SQLite + gzipped blobs),
and renders it in an interactive graph + timeline UI in real time.

- **Zero infrastructure.** No Docker. No Postgres. No external services. `pip install`.
- **Two-line integration.** One callback added to your existing `ainvoke`.
- **Crash-safe by design.** The handler never raises and the tracer never crashes
  your pipeline.
- **Interactive graph view.** Custom SVG graph (no framework), auto-laid-out. Hover, click, replay any run.
- **MCP-aware.** Tools loaded from MCP servers are attributed by source — see which tools
  came from which server vs. which are hardcoded. See [MCP support](mcp.md).
- **OpenTelemetry export.** Optionally ship every trace as OTel spans to a collector /
  Tempo / Jaeger / Datadog / Honeycomb — the bridge to your production stack. See
  [Configuration → OpenTelemetry export](configuration.md).
- **Pluggable storage.** SQLite today; Postgres / remote-collector / object-store backends planned.
- **MIT licensed.** Free forever.

## Where to go next

<div class="grid cards" markdown>

-   :material-rocket-launch: **[Quickstart](quickstart.md)**

    Install, run an example, open the UI. Five minutes.

-   :material-graph: **[Concepts](concepts.md)**

    What `agent`, `tool`, `llm`, `retriever`, `chain`, and `mcp` mean — read
    this first if you want to interpret a topology.

-   :material-cog: **[Configuration](configuration.md)**

    Every `TRACESAGE_*` env var explained.

-   :material-shield-check: **[Deploying & hardening](production.md)**

    Auth, sampling, retention, the safety rails, and the OTel bridge to your stack.

-   :material-book-open-variant: **[Examples](examples.md)**

    30 before/after apps across popular use cases. Pick the closest match to
    your architecture and copy the integration.

-   :material-console: **[CLI reference](cli.md)**

    `tracesage serve` / `export` / `stats` / `runs` / `gc`.

-   :material-puzzle: **[Extending](extending.md)**

    Adding framework adapters and storage backends.

</div>

## What you'll see

![tracesage topology graph](assets/ui-topology.png)

Once a run lands, the UI shows:

- **Run list** — every run with status, tags, started-at, total steps, total tokens
- **Topology graph** — agent / tool / chain / retriever relationships across runs
- **Timeline** — chronological steps; click any step to expand its full **request and
  response** payloads (MCP-backed tools are tagged with their server)
- **Replay** — animate any completed run at 1x / 2x / 5x

Keyboard: `j` / `k` next/prev run, `/` focus search, `t` theme, `Esc` close, `?` help.

### Watch a trace stream in

<video controls muted playsinline width="100%" poster="assets/ui-topology.png">
  <source src="assets/tracesage-demo.mp4" type="video/mp4">
  Your browser can't play embedded video —
  <a href="assets/tracesage-demo.mp4">download the demo clip</a> instead.
</video>

### Inspect any node

Click a node to open its drawer — counts, durations, errors, the tools it provides
or uses, and (for MCP) its server of origin.

<table>
  <tr>
    <td width="50%"><img src="assets/ui-tools-by-source.png" alt="Tools by source panel"></td>
    <td width="50%"><img src="assets/ui-mcp-drawer.png" alt="MCP server inspector"></td>
  </tr>
  <tr>
    <td align="center"><em>“Tools by source” — tools grouped by origin (MCP vs. local).</em></td>
    <td align="center"><em>MCP server inspector — provided tools, invocations and callers.</em></td>
  </tr>
</table>

## Status

**Beta.** API may still shift before v1.0. The PyPI badge shows the published version,
stamped by the release workflow when a version actually ships (so it matches PyPI).
Built for local development and single-process tracing, with OpenTelemetry export to
bridge into a central stack; native multi-process / remote-collector storage is on the
roadmap. See the [changelog](changelog.md) for release notes.
