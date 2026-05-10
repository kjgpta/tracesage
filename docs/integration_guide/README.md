# tracelens integration guide

A hands-on, before/after tour of integrating tracelens into multi-agent systems
built with LangChain and LangGraph.

## What's here

```
docs/integration_guide/
├── INTEGRATION.md        # tutorial walkthrough — start here
├── PRODUCTION.md         # auth, sampling, retention, deployment
├── before/               # 10 multi-agent systems WITHOUT tracelens
└── after/                # the same 10 systems WITH tracelens added
```

The `before/` and `after/` versions of each system are **full duplicates**.
The diff between them is the entire integration. Read it side-by-side.

## How to read this guide

1. Open **`INTEGRATION.md`** for the step-by-step tutorial.
2. Pick a system below that resembles your own workflow.
3. Read its `before/<system>/README.md` to understand the architecture.
4. Run it: `cd before/<system> && python main.py`.
5. Compare with `after/<system>/main.py` — three lines tell you what to add.
6. Run the after version and explore at `http://localhost:7842/ui`.

> **First time using tracelens?** Read
> [`docs/concepts.md`](../concepts.md) first — it explains the five
> topology kinds (`agent`, `tool`, `llm`, `retriever`, `chain`) so the
> per-system tour below is interpretable.

## The 10 systems

| # | System | Frameworks | Pattern | Spotlights |
|---|---|---|---|---|
| 1 | [Customer Support Triage](before/01_customer_support/README.md) | LangGraph + LangChain | Hybrid: state machine + specialist agents | Mixed-framework topology, agent-in-agent nesting |
| 2 | [Document Research Pipeline](before/02_research_pipeline/README.md) | LangGraph | Parallel fan-out for analysis | Concurrent branches in topology + timeline |
| 3 | [Code Review Assistant](before/03_code_review/README.md) | LangGraph + LCEL | LCEL chain inside a retry loop | LCEL `RunnableSequence` decomposition |
| 4 | [Data Analyst Multi-Agent](before/04_data_analyst/README.md) | LangGraph | Supervisor with worker agents | Conditional routing, supervisor topology |
| 5 | [RAG with Reranker](before/05_rag_reranker/README.md) | LangGraph + LangChain | Two-stage retrieval | Retriever events, multi-stage retrieval |
| 6 | [Writer-Critic Loop](before/06_writer_critic/README.md) | LangGraph | Self-correcting two-agent loop | Cyclic edges, iteration depth |
| 7 | [Map-Reduce Summarizer](before/07_map_reduce/README.md) | LangGraph | Dynamic fan-out via `Send` + reducer | Variable parallelism per run |
| 8 | [Streaming Token Agent](before/08_streaming_agent/README.md) | LangChain LCEL + LangGraph | Streaming LLM with tools | TTFT and stream telemetry |
| 9 | [Error Recovery Pipeline](before/09_error_recovery/README.md) | LangGraph | Tool failures + fallback edge | `*_error` events, per-node `error_count` |
| 10 | [Planner-Executor](before/10_planner_executor/README.md) | LangGraph | Iterative plan + execute loop | Deep nesting, iterative loops |

## Setup

```bash
pip install tracelens[langchain] langgraph
```

All systems run with no API keys by default (using `FakeListChatModel`).
To use a real LLM, install the matching SDK and set two env vars:

```bash
pip install langchain-openai langchain-anthropic

LLM_PROVIDER=openai    OPENAI_API_KEY=sk-...     python main.py
LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=sk-ant- python main.py
```

The integration code stays identical. Only the LLM behind it changes.

## What you'll learn

- How to add tracelens to an existing LangChain / LangGraph app in three lines
- How to interpret the topology, timeline, and run list
- How to tag runs for filtering across systems and tenants
- How to swap to OpenAI / Anthropic without changing your tracing code
- How to deploy this to production with auth, sampling, and retention

## Next

- New to tracelens? Start with **`INTEGRATION.md`**.
- Going to production? Read **`PRODUCTION.md`**.
- Just want to see the integration diff? Compare any
  `before/<system>/main.py` with the matching `after/<system>/main.py`.
