# Integrating tracelens

This walkthrough shows you exactly what to add to a LangChain or LangGraph
multi-agent system to get live observability. By the end you'll have:

- A working tracer in your app, with three lines of new code
- A live UI at `http://localhost:7842/ui` showing every run, step, and tool call
- Confidence in how to scale this into production

We'll use the systems in this guide as concrete examples. Each system has a
`before/` version (no tracelens) and an `after/` version (with tracelens).
The diff between them is what you'll learn to apply to your own code.

---

## The three-line change

Bring up `before/01_customer_support/main.py` on one side and
`after/01_customer_support/main.py` on the other. The diff is:

```diff
 import asyncio

 from graph import build_graph
+from tracelens_setup import init_tracer, DEFAULT_TAGS


 async def main() -> None:
+    tracer = await init_tracer()
+    print("tracelens UI: http://localhost:7842/ui")
+
     graph = build_graph()
     for q in SAMPLE_QUERIES:
-        result = await graph.ainvoke({"query": q})
+        result = await graph.ainvoke(
+            {"query": q},
+            config={"callbacks": [tracer.handler], "tags": DEFAULT_TAGS},
+        )
```

Three lines added in `main.py` and one new helper file (`tracelens_setup.py`).
Everything else — `graph.py`, `agents.py`, `tools.py`, `llm.py` — is byte-identical
between `before/` and `after/`.

That's the contract: **you add tracelens, you don't change your workflow.**

---

## Why it works

LangChain and LangGraph both expose a callback system. Every chain / agent /
tool / LLM / retriever fires `*_start` and `*_end` events through any handler
registered in the `callbacks` config. tracelens implements one of those handlers
— so adding it captures the entire event stream without changing your workflow.

The tracer:
1. Buffers events in an asyncio queue (non-blocking, drops on overflow).
2. A worker drains the queue into a SQLite + gzipped-blob store on disk.
3. A FastAPI server serves the UI from the same data, with WebSocket push.

---

## Step by step

### Step 1 — Install

```bash
pip install tracelens[langchain]
```

The `[langchain]` extra pulls `langchain-core`, the only mandatory third-party
dep beyond FastAPI / SQLite / pydantic. LangGraph is optional — needed only if
your workflow uses it.

### Step 2 — Construct the tracer

The tracer is async-first. Build it in your entry point:

```python
from tracelens import TraceLens

tracer = await TraceLens.create()
```

That single call:

- Creates the data dir (`~/.tracelens` by default)
- Initializes the SQLite schema
- Spawns the worker task
- Starts the embedded UI server on port 7842

`TraceLens.create()` reads `TRACELENS_*` env vars for config. To pass values
explicitly:

```python
from pathlib import Path
from tracelens import TraceLensConfig

cfg = TraceLensConfig(data_dir=Path("/tmp/my-data"), port=8000)
tracer = await TraceLens.create(cfg)
```

For systems with multiple entry points (CLI, HTTP server, batch worker),
encapsulate this in a small helper so each entry point shares one tracer:

```python
# tracelens_setup.py
from tracelens import TraceLens, TraceLensConfig

DEFAULT_TAGS = ["my-system"]

async def init_tracer() -> TraceLens:
    return await TraceLens.create(TraceLensConfig())
```

Every `after/<system>/tracelens_setup.py` in this guide follows that exact shape.

### Step 3 — Wire the handler

Pass `tracer.handler` in the `callbacks` list of every `ainvoke` / `invoke` call:

```python
result = await graph.ainvoke(
    {"input": payload},
    config={
        "callbacks": [tracer.handler],
        "tags": ["my-system"],
    },
)
```

The handler is **idempotent and reusable**. Construct it once, pass it
everywhere. There is no per-call setup.

### Step 4 — Tag your runs

Tags travel with the run and let you filter in the UI and via the API:

```python
config={
    "callbacks": [tracer.handler],
    "tags": ["v2", "user:alice", "experiment-rerank"],
}
```

In the UI:
- Click a tag chip on any run row to filter
- Use `/api/runs?tag=experiment-rerank` programmatically

Common tagging schemes:

| Scheme | Examples |
|---|---|
| System name | `customer-support`, `research-pipeline` |
| Version | `v2`, `prod`, `staging` |
| User / tenant | `user:alice`, `tenant:acme` |
| Experiment | `experiment-rerank-v3`, `ab-test-cohort-A` |

You can attach as many tags as you want to a single run.

### Step 5 — Open the UI

`http://localhost:7842/ui`

What you'll see:

- **Run list (left)** — every run with status badges, tag chips, started-at,
  total steps, total tokens. Click to inspect.
- **Topology graph (center)** — the agent / tool / chain / retriever
  relationships across all runs. Live-pulses as new events land.
- **Timeline** — chronological steps for the selected run, expandable to see
  full payloads. Payloads are gzipped on disk and lazy-loaded on click.
- **Replay** — animate a completed run at 1x / 2x / 5x speed.

Keyboard shortcuts:

| Key | Action |
|---|---|
| `j` / `k` | Next / previous run |
| `/` | Focus search |
| `t` | Toggle theme |
| `Esc` | Close modal |
| `?` | Help |

---

## What the topology nodes mean

Every node in the topology graph belongs to one of **five kinds**. The
per-system tour below uses these names freely (`agent:billing_agent`,
`tool:lookup_account`, `llm:FakeListChatModel`, etc.), so this is the
section to read first if you're new to tracelens:

| Kind | What it is | Quick examples |
|---|---|---|
| `agent` | A LangGraph node **you** wrote that calls other components | `agent:billing_agent`, `agent:fact_extractor`, `agent:supervisor` |
| `tool` | A side-effect function decorated with `@tool` | `tool:lookup_account`, `tool:run_sql`, `tool:flaky_fetch` |
| `llm` | A language-model invocation | `llm:FakeListChatModel`, `llm:ChatOpenAI` |
| `retriever` | A `BaseRetriever` subclass invocation | `retriever:FastFakeRetriever`, `retriever:Chroma` |
| `chain` | Plumbing — LCEL primitives, the LangGraph orchestrator, routing functions | `chain:LangGraph`, `chain:RunnableSequence`, `chain:route_after_quality` |

The full reference, including how tracelens classifies events into kinds
and why "agents" with no descendants get demoted to `chain`, lives at
[`docs/concepts.md`](../concepts.md). It walks through System 2's topology
piece by piece if you want a worked example.

---

## Per-system tour

Each system in this guide demonstrates a different agent pattern and
spotlights a different tracelens feature. Pick the one nearest your own
architecture and start there.

### System 1 — Customer Support Triage

`before/01_customer_support/` → `after/01_customer_support/`

A LangGraph state machine that triages customer queries to specialist agents.
Each specialist combines a tool-selection LLM, a tool invocation, and a
reply-formatting LLM.

**Architecture:**

```
[customer query] → triage → router → { billing | tech | escalate } → END
```

**What to look for in tracelens:**

- The three specialist agents (`billing_agent`, `tech_agent`,
  `escalation_agent`) each connect to a different toolbox in the **topology**.
- Different runs highlight different paths. Faded edges in the graph view
  show branches that this particular run did *not* take.
- Click any LLM step in the **timeline** and hit "show full payload" to see
  the exact prompts and responses (gzip-decoded on demand).
- Filter the run list by the `customer-support` tag to isolate this system's
  runs from others sharing the same data dir.

**Run it:**

```bash
cd after/01_customer_support
python main.py
# Open http://localhost:7842/ui
```

### System 2 — Document Research Pipeline

`before/02_research_pipeline/` → `after/02_research_pipeline/`

A LangGraph pipeline with **parallel fan-out**: after retrieval, three
analyzers (facts, sentiment, entities) run concurrently, then a `synthesize`
node merges their outputs.

**Architecture:**

```
ingest → retrieve → ┬→ fact_extractor ─┐
                    ├→ sentiment       ├→ synthesize → END
                    └→ entities        ┘
```

**What to look for in tracelens:**

- The fan-out shape in the **topology**: three parallel edges from `retrieve`
  to three sibling nodes, then three converging edges into `synthesize`.
- The **timeline** shows the three branches' LLM calls overlapping in time —
  the parallel execution is visible in horizontal bar widths.
- `retrieve` produces a `retriever_start` / `retriever_end` pair, which
  surfaces as a `retriever:_FixedCorpusRetriever` node — distinct from agents
  and LLMs.
- The same retriever node will accumulate `invocation_count = 3` after running
  three topics. Compare with the LLM nodes whose counts grow faster.

**Run it:**

```bash
cd after/02_research_pipeline
python main.py
# Open http://localhost:7842/ui
```

### System 3 — Code Review Assistant

`before/03_code_review/` → `after/03_code_review/`

A code review pipeline built from two **LangChain LCEL chains** wrapped in a
**LangGraph state machine** with a retry edge. The 3 demo diffs exercise the
retry path on diff 2.

**What to look for:**

- Each `prompt | llm | parser` LCEL chain decomposes into separate
  `chain:RunnableSequence`, `chain:ChatPromptTemplate`,
  `llm:FakeListChatModel`, `chain:StrOutputParser` topology nodes.
- `agent:comment` has `invocation_count = 4` for 3 diffs (1 + 2 + 1) — the
  extra invocation is the retry on diff 2.
- The retry edge `comment → quality_check → comment` shows up as an edge
  with `count > 1`.

### System 4 — Data Analyst Multi-Agent

`before/04_data_analyst/` → `after/04_data_analyst/`

The classic LangGraph **supervisor pattern** — one orchestrator routes work
to specialized workers (`sql_agent`, `chart_agent`, `narrative_agent`).
The 3 demo questions exercise different worker compositions: 1, 2, and 3
workers respectively.

**What to look for:**

- `agent:supervisor` sits at the center of the topology. Its
  `invocation_count` equals (workers + 1) per question — 9 across the demo.
- Workers connect bidirectionally to the supervisor; you can see the
  supervisor → worker → supervisor loop in the timeline.
- Per-question worker composition is visible in each run's timeline. Q1
  shows only `sql_agent`; Q3 shows all three.

### System 5 — RAG with Reranker

`before/05_rag_reranker/` → `after/05_rag_reranker/`

A two-stage retrieval pipeline: a fast retriever returns 8 candidates, an
LLM reranker scores them, the top 3 feed an answer chain.

**What to look for:**

- `retriever:FastFakeRetriever` appears as its own topology kind — distinct
  from agents, LLMs, and tools. Its full payload (visible via "show full
  payload") includes the candidate list with relevance scores from the
  retriever's `metadata`.
- Both LCEL chains (`rerank_chain`, `answer_chain`) decompose. The topology
  shows two `RunnableSequence` invocations per question.
- The two-stage retrieval shape (`retrieve → rerank → answer`) makes the
  pipeline structure obvious without reading the code.

### System 6 — Writer-Critic Loop

`before/06_writer_critic/` → `after/06_writer_critic/`

A self-correcting two-agent loop. The writer drafts; the critic scores. On
`REVISE`, the loop returns to the writer with the critic's feedback. The
3 demo topics give attempts of 1, 2, 1 respectively.

**What to look for:**

- `agent:writer` and `agent:critic` both have `invocation_count = 4` (1 + 2 + 1).
  When writer and critic counts match, you have a clean writer-critic shape;
  when they diverge, somebody is short-circuiting.
- Topic 2's run timeline shows two writer steps and two critic steps —
  visually distinct from topics 1 and 3.
- The critic's tools (`tool:word_count`, `tool:readability_check`) each show
  4 invocations — one per critic call. They surface as their own topology
  nodes alongside the LLM.

### System 7 — Map-Reduce Summarizer

`before/07_map_reduce/` → `after/07_map_reduce/`

A LangGraph pipeline that splits a document into chunks, summarizes each
chunk in parallel via **dynamic fan-out** (LangGraph `Send`), then reduces.
Different documents produce different fan-out widths.

**What to look for:**

- `agent:summarize_chunk` invocation count equals total chunks across all
  documents — 7 for the demo (3 + 2 + 2). Compare with `agent:reduce` (3)
  and `agent:split` (3). The summarize/reduce ratio > 1 is the signature
  of map-reduce.
- The edge `chain:LangGraph → agent:summarize_chunk` has `count = 7` — the
  same value as the dynamic dispatch count.
- Per-run timelines show variable parallel widths: 3 horizontally-stacked
  summarize bars for doc 1, 2 for docs 2 and 3.

### System 8 — Streaming Token Agent

`before/08_streaming_agent/` → `after/08_streaming_agent/`

A LangGraph pipeline that streams tokens from an LCEL chain, then post-processes
with two tools. Streaming telemetry surfaces on the `LLM_END` event.

**What to look for:**

- Open any LLM step's full payload — the `_stream` field contains
  `streamed_token_count`, `first_token_ts`, and `stream_duration_ms`.
- The LLM step summary surfaces `streamed=<N> stream_dur=<ms> tps=<X>`.
- Even with the fake provider, `token_output` is non-zero on the run row
  (filled from streamed chunks when the model doesn't report usage). With
  a real streaming provider, both `token_input` and `token_output` are
  populated from the actual usage block.
- Switching to a real LLM (`LLM_PROVIDER=openai`) is where the streaming
  spotlight matters most — TTFT and tps become real performance metrics.

### System 9 — Error Recovery Pipeline

`before/09_error_recovery/` → `after/09_error_recovery/`

A LangGraph pipeline where the primary fetch tool fails on a deterministic
schedule. The graph routes through a fallback when an error occurs, then
continues processing normally. Calls 1 and 2 succeed; call 3 fails and
takes the fallback path.

**What to look for:**

- `tool:flaky_fetch` shows `invocation_count = 3, error_count = 1`. This
  per-node error count is the production metric you'd alert on.
- A `tool_error` step shows up in run 3's timeline with the exception text
  visible in the summary.
- `agent:fallback` has `invocation_count = 1` — only run 3 took the
  fallback path.
- All 3 runs have status `completed` (not `failed`) — the graph caught the
  exception and recovered. tracelens distinguishes "tool errored" from "run
  failed", which matters for alerting policy.

### System 10 — Planner-Executor

`before/10_planner_executor/` → `after/10_planner_executor/`

A planner-executor loop. The planner emits a comma-separated step list once
per task; the executor runs once per step until the plan is empty. The 3
demo tasks have plan lengths 4, 3, 2 — so the executor runs 9 times total.

**What to look for:**

- `agent:executor` has `invocation_count = 9` (sum of plan lengths).
  `agent:planner` has `invocation_count = 3` (once per task). The 3:1 ratio
  is the signature of a planner-executor pattern.
- Each run's timeline shows the iterative loop laid out chronologically:
  planner LLM call (with the comma-separated plan in its output), then N
  executor steps in order.
- Tools are dispatched by step type. `tool:synthesize` runs 3 times (every
  task ends with one); `tool:search`, `tool:read_doc`, `tool:take_notes`
  each run twice (different tasks include them).

---

## Switching to a real LLM

Every system uses `FakeListChatModel` by default. To use a real LLM:

```bash
pip install langchain-openai     # or langchain-anthropic

export LLM_PROVIDER=openai
export OPENAI_API_KEY=sk-...
python main.py
```

What changes in tracelens with a real LLM:

- **Real token counts** appear on every `LLM_END` event (input + output)
- **Token totals** in the run summary reflect actual API usage
- **Streaming telemetry** (TTFT, `streamed_token_count`) appears for streaming
  models (try System 8)
- **Latency** shifts from microseconds to hundreds of milliseconds, making the
  timeline view far more informative

The integration code stays **identical**. Only `llm.py` (provider switch)
returns a different model.

---

## Common patterns

### Multiple entry points sharing one tracer

If your system has multiple async entry points (CLI, HTTP server, Celery
worker), construct **one** tracer at process startup and pass `tracer.handler`
to each invocation:

```python
# app.py
from contextlib import asynccontextmanager
from fastapi import FastAPI
from tracelens import TraceLens

tracer: TraceLens

@asynccontextmanager
async def lifespan(app: FastAPI):
    global tracer
    tracer = await TraceLens.create()
    yield
    await tracer.stop()

app = FastAPI(lifespan=lifespan)

@app.post("/run")
async def run(payload: dict):
    return await graph.ainvoke(
        payload,
        config={"callbacks": [tracer.handler], "tags": ["http"]},
    )
```

Don't construct a new tracer per request — the embedded server only binds
once, and per-request creation leaks worker tasks and DB pools.

### Custom data dir

For multi-tenant or per-environment isolation:

```bash
TRACELENS_DATA_DIR=/var/tracelens/prod    python app.py
TRACELENS_DATA_DIR=/var/tracelens/staging python app.py
```

Or programmatically:

```python
cfg = TraceLensConfig(data_dir=Path(f"/var/tracelens/{env}"))
```

### Programmatic queries

Sometimes you want trace data inside your own code (debugging, alerting,
analytics). The `tracer.db` handle is the same one the UI uses:

```python
runs, total = await tracer.db.list_runs(status="failed", limit=10)
journey = await tracer.db.get_journey(run_id)
topology = await tracer.db.get_topology()
```

### Disable the embedded server

If you only want to capture (and view from a separate viewer process):

```python
tracer = await TraceLens.create(cfg, start_server=False)
```

Then run the read-only viewer pointing at the same data dir:

```bash
tracelens serve --data-dir /var/tracelens/prod --port 7842
```

This is the recommended pattern for production: one process produces, another
serves the UI with auth.

---

## Troubleshooting

### UI shows nothing

Most common cause: **the tracer was constructed but no `callbacks` config was
passed**. Verify your `ainvoke` call has
`config={"callbacks": [tracer.handler]}`.

Second cause: **the data dir is different between writer and reader**. Check
that `TRACELENS_DATA_DIR` matches across processes. Run `tracelens stats
--data-dir <path>` to confirm where data is landing.

### Port 7842 already in use

```bash
TRACELENS_PORT=0    python main.py    # ephemeral port (printed at startup)
TRACELENS_PORT=8123 python main.py    # explicit port
```

### Events dropped warning

The queue is bounded (default 50,000). At very high event rates you can hit
it. Check `/api/stats` — `events_dropped` should be 0 in steady state. To
tune:

- Raise `TRACELENS_QUEUE_MAXSIZE`
- Raise `TRACELENS_WORKER_BATCH_SIZE` (default 50; try 200 on Windows NTFS)
- Drop sample rate: `TRACELENS_SAMPLE_RATE=0.1` keeps 10% of root runs

See `PRODUCTION.md` for full guidance.

### "Refuses to start with host=0.0.0.0"

This is intentional. Set `TRACELENS_AUTH_TOKEN` whenever binding to anything
other than `127.0.0.1` / `localhost` / `::1`. See `PRODUCTION.md`.

### Real LLM, no token counts

`token_input` / `token_output` come from the model's own `llm_output` payload.
For models that don't emit usage in the standard format, the counts will be
`null`. Streaming models that emit `on_llm_new_token` will surface the
streamed count instead.
