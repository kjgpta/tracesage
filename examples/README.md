# tracesage examples

Three tiers, from a 30-second first taste to a 30-app real-world gallery.

| Tier | Folder | Needs | What it is |
|---|---|---|---|
| **Getting started** | [`getting_started/`](getting_started/) | no API key | 3 standalone demos driven by `FakeListChatModel` — run instantly, see your first trace |
| **MCP tools** | [`mcp/`](mcp/) | `tracesage[mcp]` | tools from local MCP servers attributed by source, plus hardcoded tools |
| **Showcase** | [`showcase/`](showcase/) | an LLM API key | **30 real before/after apps** across popular use cases — the integration gallery |

## Getting started (zero setup)

```bash
pip install "tracesage[langchain]"
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
pip install "tracesage[mcp]"
python examples/mcp/main.py            # then open http://localhost:7842/ui
```

Tools attributed by source in the topology and the "Tools by source" panel. See
[`mcp/README.md`](mcp/README.md) for:

- **Intro scenarios** (`main.py`, `mcp_only.py`, `local_only.py`, `single_agent_multi_mcp.py`) —
  local stdio MCP servers (`weather`, `math`) + hardcoded tools; no API key.
- **Real-world demos** (need an LLM key) — `trip_demo/demo.py` (one agent over 3 bundled MCP
  servers, no external installs) and `gmail_youtube_demo/` (Gmail + YouTube, before/after pair).

## Showcase — 30 before/after apps

```bash
pip install -r examples/showcase/requirements.txt
export OPENAI_API_KEY=...               # or LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY
python examples/showcase/01_support_faq_router/before.py    # plain app
python examples/showcase/01_support_faq_router/after.py     # same app + live trace
```

The flagship gallery: each app ships a **`before.py`** (plain LangChain/LangGraph, real
LLMs) and an **`after.py`** (the same app + tracesage). `diff before.py after.py` shows
exactly how little it takes to add observability. Covers customer support, RAG, multi-agent
systems, MCP, reasoning loops, and finance/legal/insurance verticals. See
[`showcase/README.md`](showcase/README.md) for the full index.

## Each example is its own application (isolated data dir)

The topology map and the **"Tools by source"** panel aggregate every run in a data
dir, so two applications sharing one dir would merge into one graph. To keep them
separate, **every example writes to its own data dir** under `~/.tracesage/`:

| Example | Data dir |
|---|---|
| `getting_started/01_smart_search_agent.py` | `~/.tracesage/smart-search` |
| `getting_started/02_research_supervisor.py` | `~/.tracesage/research-supervisor` |
| `getting_started/03_rag_with_tools.py` | `~/.tracesage/rag-tools` |
| `mcp/main.py` · `mcp_only.py` · `local_only.py` · `single_agent_multi_mcp.py` | `~/.tracesage/mcp-mixed` · `mcp-only` · `local-tools` · `multi-mcp` |
| `mcp/trip_demo/demo.py` · `gmail_youtube_demo/after.py` | `~/.tracesage/trip-demo` · `gmail-youtube-demo` |
| `showcase/<NN>_<name>/after.py` | `~/.tracesage/<NN>_<name>` (its folder name) |

Each script prints its `Data dir:` and the exact `tracesage runs -d <dir>` command on
startup. To inspect a specific example's runs:

```bash
tracesage runs  -d ~/.tracesage/smart-search      # only that app's runs
tracesage serve -d ~/.tracesage/smart-search      # UI scoped to that app
```

This is the general pattern for your own apps too — give each application its own
`data_dir` (see [Configuration → Isolating multiple applications](../docs/configuration.md)).
