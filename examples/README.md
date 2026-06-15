# tracelens examples

Three tiers, from a 30-second first taste to a 30-app real-world gallery.

| Tier | Folder | Needs | What it is |
|---|---|---|---|
| **Getting started** | [`getting_started/`](getting_started/) | no API key | 3 standalone demos driven by `FakeListChatModel` — run instantly, see your first trace |
| **MCP tools** | [`mcp/`](mcp/) | `tracelens[mcp]` | tools from local MCP servers attributed by source, plus hardcoded tools |
| **Showcase** | [`showcase/`](showcase/) | an LLM API key | **30 real before/after apps** across popular use cases — the integration gallery |

## Getting started (zero setup)

```bash
pip install "tracelens[langchain]"
python examples/getting_started/01_smart_search_agent.py   # then open http://localhost:7842/ui
```

| File | What it shows |
|---|---|
| [`01_smart_search_agent.py`](getting_started/01_smart_search_agent.py) | One agent, four tools, picks one per query |
| [`02_research_supervisor.py`](getting_started/02_research_supervisor.py) | Multi-agent supervisor with conditional routing |
| [`03_rag_with_tools.py`](getting_started/03_rag_with_tools.py) | LCEL chain + retriever + tools |

These use `FakeListChatModel`, so they run with **no API key** — the fastest way to see
the UI working.

## MCP tools

```bash
pip install "tracelens[mcp]"
python examples/mcp/main.py            # then open http://localhost:7842/ui
```

Two local stdio MCP servers (`weather`, `math`) plus two hardcoded tools, all attributed
by source in the topology and the "Tools by source" panel. See [`mcp/README.md`](mcp/README.md)
for the `mcp_only` / `local_only` / `single_agent_multi_mcp` variants.

## Showcase — 30 before/after apps

```bash
pip install -r examples/showcase/requirements.txt
export OPENAI_API_KEY=...               # or LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY
python examples/showcase/01_support_faq_router/before.py    # plain app
python examples/showcase/01_support_faq_router/after.py     # same app + live trace
```

The flagship gallery: each app ships a **`before.py`** (plain LangChain/LangGraph, real
LLMs) and an **`after.py`** (the same app + tracelens). `diff before.py after.py` shows
exactly how little it takes to add observability. Covers customer support, RAG, multi-agent
systems, MCP, reasoning loops, and finance/legal/insurance verticals. See
[`showcase/README.md`](showcase/README.md) for the full index.
