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
Open **http://localhost:7842/ui** and see:

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

Both MCP servers run via `uvx` (comes with `uv`). If you don't have `uv`:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. Gmail — one-time OAuth2 setup

```bash
uv tool install mcp-google-gmail   # installs the CLI globally
mcp-google-gmail auth              # opens browser to authorise — token cached after this
```

No `credentials.json` to download. The auth flow handles everything and stores the token automatically.

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
