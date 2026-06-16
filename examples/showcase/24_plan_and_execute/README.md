# 24 — Plan-and-Execute Agent

**Domain:** automation · **Base:** LangGraph · **Pattern:** plan/execute

A planner LLM turns an arithmetic word problem into an ordered list of steps via
structured output (`Plan.steps`). An executor node walks the list one step at a time:
the LLM rewrites each step as a single arithmetic expression, then a local `calculator`
tool evaluates it. A `replan` node revises the remaining plan when a step yields a
malformed expression — giving the graph a dynamic recovery loop instead of a fixed
pipeline.

## Run

```bash
pip install -r ../requirements.txt
export OPENAI_API_KEY=...            # or LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY
python before.py                     # plain app
python after.py                      # same app + live trace UI
```

## The integration

```bash
diff before.py after.py
```

The only difference is `from tracesage import TraceSage` and wrapping the run in
`async with TraceSage.session(install=True)` plus an `await tl.flush()` (and a one-line
keep-the-UI-up prompt for the demo). `install=True` registers the LangChain handler
globally, so there is no `callbacks=` wiring anywhere in the graph.

## What the trace shows

- The **plan/execute split**: a single `plan` node emitting a structured step list,
  followed by the `execute` node firing once per step in a self-loop.
- Each **executor pass** as a distinct iteration — the translate LLM call plus the
  `calculator` tool invocation, with the expression and computed value in the drawer.
- The **dynamic replanning branch**: when a step produces a bad expression the
  `calculator` raises, surfacing the failed node and (when wired in) the `replan`
  revision of the remaining steps — so you can see recovery, not just the happy path.
- Per-node **latency and token usage** across the loop, making it obvious how many
  execute iterations and replans a given problem actually took.
