# 18 — Personal Assistant (MCP)

**Domain:** productivity · **Base:** LangGraph · **Pattern:** 2 MCP servers + local tool

A LangGraph ReAct agent whose tools come from THREE sources: one in-code `current_time`
tool plus two tiny local FastMCP stdio servers — `notes` (`add_note` / `list_notes`) and
`tasks` (`add_task` / `list_tasks`) — loaded over `MultiServerMCPClient`. The agent works
a short productivity request that touches all three sources in one run.

## Run

```bash
pip install -r ../requirements.txt
pip install 'tracelens[mcp]' mcp langchain-mcp-adapters   # MCP extras (guarded in code)
export OPENAI_API_KEY=...            # or LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY
python before.py                     # plain app
python after.py                      # same app + live trace UI
```

The two MCP servers (`notes_server.py`, `tasks_server.py`) are started for you as stdio
subprocesses by `MultiServerMCPClient` — you never run them directly.

## The integration

```bash
diff before.py after.py
```

The only differences are `from tracelens import TraceLens`, the
`from tracelens.adapters.mcp import register_mcp_client` helper, wrapping the run in
`async with TraceLens.session(install=True)`, swapping `client.get_tools()` for
`register_mcp_client(tl, client)` (which records each tool's originating server), and a
final `await tl.flush()`. `install=True` registers a global LangChain handler, so there is
no `callbacks=` wiring anywhere.

## What the trace shows

- **MCP tool-source attribution** (the flagship feature): every tool call is color-coded
  by where it came from — `local` (`current_time`) vs the `notes` MCP server vs the
  `tasks` MCP server — in the "Tools by source" panel.
- The **ReAct loop**: alternating LLM planning calls and tool calls until the agent has
  added the note, added the task, read the clock, and listed everything back.
- Each server's **full tool inventory** (`add_note`/`list_notes`, `add_task`/`list_tasks`)
  in the topology, even when a tool is not invoked on this run.
- Per-tool **latency, arguments, and return payloads** in the drawer, so you can see
  exactly which server handled each step and what it returned.
