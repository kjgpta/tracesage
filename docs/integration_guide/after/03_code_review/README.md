# 03 — Code Review Assistant (with tracelens)

Identical workflow to `../../before/03_code_review/`, plus tracelens.

## What changes from `before/`

- **`tracelens_setup.py`** (new) — tracer init helper.
- **`main.py`** — three lines added (import, init_tracer, config=).

`llm.py`, `tools.py`, `chains.py`, `graph.py` are byte-identical.

## Run

```bash
pip install tracelens[langchain] langgraph
python main.py
```

## What to look for in tracelens

- **LCEL decomposition** — each `prompt | llm | parser` chain shows up as a
  `chain:RunnableSequence` node connected to its components:
  `chain:ChatPromptTemplate`, `llm:FakeListChatModel`, `chain:StrOutputParser`.
  This is what makes LCEL traces uniquely informative — the structure of the
  LCEL graph is recoverable from the topology.
- **Retry edge** — diff 2 hits the retry path. Compare its journey timeline
  to diffs 1 and 3: you'll see the `comment` step appearing twice, with a
  `quality_check` between them.
- **`agent:comment` invocations** — should equal 4 across the demo (diff 1: 1,
  diff 2: 2 with retry, diff 3: 1).
- **Tools** — `tool:lint_diff` and `tool:run_tests` each appear with 3
  invocations (one per diff; quality_check runs once per pass).

## What this system spotlights

- **LCEL traces are hierarchical** — pipe operators in LangChain produce a
  single named `RunnableSequence`, but tracelens drills into its components
  and renders each as its own topology node.
- **Cyclic graph behavior** — even though LangGraph forbids true cycles,
  conditional retries between named nodes show up cleanly. The `comment ↔
  quality_check` cycle is visible as edges with `count > 1`.
