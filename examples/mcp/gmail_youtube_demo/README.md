# Gmail + YouTube demo — before/after tracesage

A ReAct agent reads your real Gmail inbox, finds YouTube links in your emails, fetches their transcripts, and summarises everything. Two scripts — identical agent logic, one with tracesage added.

## What you see

### without tracesage (`before.py`)
```
Q: Search my Gmail inbox for the 5 most recent unread emails...

Here's what was in your inbox: ... The most interesting video was about ...
```
Final answer only. No visibility into tool calls, LLM rounds, token counts, or timing.

### with tracesage (`after.py`)
Open the UI URL the script prints (tracesage uses **http://localhost:7842/ui** by
default, but auto-picks the next free port — 7843, … — if 7842 is busy) and see:

| UI surface | What you see |
|---|---|
| **Topology graph** | agent node → `gmail` MCP server node + `youtube` MCP server node, each with their tools as leaves |
| **Timeline** | every tool call in sequence — `gmail_search_emails` → `gmail_get_message` → `get_transcript` — with full request/response payload |
| **Tools by source** | gmail tools and youtube tools grouped and labelled by which server they came from |
| **Node inspector** | click any LLM node → token count (input + output) and latency |

## What tracesage adds (the entire diff)

```python
# two imports
from tracesage import TraceSage, TraceSageConfig
from tracesage.adapters.mcp import register_mcp_client

# two setup lines (replace client.get_tools())
tracer = await TraceSage.create(TraceSageConfig(data_dir=DATA_DIR))
mcp_tools = await register_mcp_client(tracer, make_mcp_client())

# one kwarg on ainvoke
config={"callbacks": [tracer.handler], "recursion_limit": 25}
```

5 lines. The agent, query, MCP config, and LLM are byte-for-byte identical.

## Prerequisites

### 1. Install packages

```bash
pip install 'tracesage[mcp]' mcp-google-gmail mcp-youtube-transcript langchain-anthropic
```

The demo launches each server's console script directly. If you'd rather not
install them into this environment, install [`uv`](https://astral.sh/uv) and the
demo will run them via `uvx` automatically:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. Gmail — optional, needs Google credentials

The `mcp-google-gmail` server authenticates with **Google Application Default
Credentials** (it calls `google.auth.default()` at startup). The simplest setup:

```bash
gcloud auth application-default login    # needs the gcloud CLI + a GCP project with the Gmail API enabled
# …or set GOOGLE_APPLICATION_CREDENTIALS to an OAuth-client / service-account JSON
```

See the [mcp-google-gmail](https://pypi.org/project/mcp-google-gmail/) docs for the
exact GCP project and Gmail-API scope setup.

**Gmail is optional.** Without credentials the Gmail server simply fails to load
and the agent runs with YouTube only — the query falls back to summarising
`YOUTUBE_URL` (a public video), so the before/after comparison still works.

### 3. YouTube — no setup needed

`mcp-youtube-transcript` fetches public YouTube transcripts with no API key.

## Run

```bash
export ANTHROPIC_API_KEY=...

# No observability — final answer only
python examples/mcp/gmail_youtube_demo/before.py

# With tracesage — full trace in the browser
python examples/mcp/gmail_youtube_demo/after.py
python examples/mcp/gmail_youtube_demo/after.py --open   # auto-open browser
```

## How the agent uses both servers

1. Calls `gmail_search_emails` / `gmail_list_messages` → finds recent unread emails
2. Calls `gmail_get_message` on each → reads body to find YouTube URLs
3. Calls `get_transcript(url)` on the most interesting video → fetches full transcript
4. Summarises inbox + video content in one response

In tracesage's topology graph you see all three steps attributed to their source server.

## File layout

```
gmail_youtube_demo/
├── before.py    # vanilla agent — no observability
└── after.py     # +5 tracesage lines — full trace UI
```
