# Examples

tracesage ships a gallery of **runnable** examples in the
[`examples/`](https://github.com/kjgpta/tracesage/tree/main/examples) directory,
in three tiers — from a 30-second first taste to a 30-app before/after gallery.

| Tier | Folder | Needs | What it is |
|---|---|---|---|
| **Getting started** | [`examples/getting_started/`](https://github.com/kjgpta/tracesage/tree/main/examples/getting_started) | no API key | 3 standalone demos driven by `FakeListChatModel` — run instantly |
| **MCP tools** | [`examples/mcp/`](https://github.com/kjgpta/tracesage/tree/main/examples/mcp) | `tracesage[mcp]` (+ LLM key for the real-world demos) | intro scenarios + two real-world demos (Trip Planner, Gmail+YouTube) — tools attributed by source |
| **Showcase** | [`examples/showcase/`](https://github.com/kjgpta/tracesage/tree/main/examples/showcase) | an LLM API key | **30 real before/after apps** across popular use cases |

## Getting started (zero setup)

```bash
pip install "tracesage[langchain]"
python examples/getting_started/01_smart_search_agent.py   # then open the URL it prints (default http://localhost:7842/ui)
```

`01_smart_search_agent` (one agent, four tools), `02_research_supervisor`
(multi-agent supervisor), `03_rag_with_tools` (LCEL chain + retriever + tools).
These use `FakeListChatModel`, so they run with **no API key**.

## MCP tools

```bash
pip install "tracesage[mcp]"
python examples/mcp/main.py            # then open the URL it prints (default http://localhost:7842/ui)
```

Tools attributed by source in the topology and the "Tools by source" panel:

- **Intro scenarios** (`main.py`, `mcp_only.py`, `local_only.py`, `single_agent_multi_mcp.py`) —
  two local stdio MCP servers (`weather`, `math`) plus hardcoded tools; **no API key**.
- **Real-world demos** (need an LLM key):
    - **Trip Planner** (`trip_demo/demo.py`) — one agent across **three** bundled MCP servers
      (flights, weather, hotels; 7 tools each) plus a local tool. No external installs.
    - **Gmail + YouTube** (`gmail_youtube_demo/`) — a ReAct agent reading Gmail + YouTube
      transcripts, with a `before.py` / `after.py` pair showing the exact diff.

See [MCP support](mcp.md) for how attribution works.

## Showcase — 30 before/after apps

```bash
pip install -r examples/showcase/requirements.txt
export OPENAI_API_KEY=...               # or LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY
python examples/showcase/01_support_faq_router/before.py    # plain app
python examples/showcase/01_support_faq_router/after.py     # same app + live trace
```

The flagship gallery: each app ships a **`before.py`** (plain LangChain/LangGraph,
real LLMs) and an **`after.py`** (the same app + tracesage), so `diff before.py
after.py` shows exactly how little it takes to add observability.

> The `LLM_PROVIDER` / `LLM_MODEL` env vars are read by the **example apps** (via
> LangChain's `init_chat_model`) to pick a provider — they are **not** tracesage
> settings. tracesage itself is provider-agnostic and has no provider config.

The 30 apps span five themes — see the full index in the
[showcase README](https://github.com/kjgpta/tracesage/tree/main/examples/showcase):

- **Foundational patterns** — router, ReAct agent, text-to-SQL, sequential chain, parallel fan-out
- **RAG & knowledge** — docs Q&A, multi-query, agentic RAG, reranker, conversational (memory)
- **Multi-agent systems** — supervisor, hierarchical, support triage, competitive intel, code migration, sales, debate
- **Tools & MCP** — MCP personal assistant, GitHub triage, multi-MCP travel, DevOps responder, e-commerce concierge
- **Reasoning loops & evaluation** — reflexion writer, plan-and-execute, self-correcting codegen, LLM-as-judge, map-reduce
- **Domain verticals** — invoice extraction, contract clause risk, insurance claim intake

Each app folder has its own README explaining what the trace reveals.

## Each example is its own application (isolated data dir)

The topology map and the **"Tools by source"** panel aggregate every run in a data dir,
so two applications sharing one dir would merge into a single graph. To keep them
separate, **every example writes to its own data dir** under `~/.tracesage/`:

- `getting_started/` → `~/.tracesage/smart-search`, `…/research-supervisor`, `…/rag-tools`
- `mcp/` → `~/.tracesage/mcp-mixed`, `…/mcp-only`, `…/local-tools`, `…/multi-mcp`
- `showcase/<NN>_<name>/after.py` → `~/.tracesage/<NN>_<name>` (its folder name)

Each script prints its `Data dir:` and `tracesage runs -d <dir>` on startup. Inspect one
example's runs without others bleeding in:

```bash
tracesage runs  -d ~/.tracesage/smart-search
tracesage serve -d ~/.tracesage/smart-search
```

Use the same pattern for your own apps — one `data_dir` per application. See
[Configuration → Isolating multiple applications](configuration.md).
