# Claude Code

Record every Claude Code session (in the terminal, in an IDE, or in Claude
Desktop's Code tab) as a trace: each API request with its model, thinking and
tokens, each tool call, and each subagent under the Task call that started it.

## Connect it

```bash
pip install "tracesage[apps]"        # or: uv tool install "tracesage[apps]"
tracesage connect claude-code
```

Restart Claude Code if it was running, then open
[http://127.0.0.1:7842](http://127.0.0.1:7842) and set **Source** to Claude Code.
Your existing sessions are imported on first start, so the list is not empty
even before you send a new message.

## What is recorded, and from where

| What | Source |
|---|---|
| Every turn, model request (model, thinking, reply, input, output and cache tokens, cost) and tool call | `~/.claude/projects/<project>/<session>.jsonl` |
| Subagents (Task / Agent tool), nested under the spawning call | `<session>/subagents/agent-<id>.jsonl` and its `.meta.json` |
| MCP tool calls, grouped by server | `mcp__<server>__<tool>` names in the transcript |

The transcripts are read in full on first start; there is no day limit. A
session becomes a thread, each prompt you send becomes a run, and the run's
agent node is `claude-code`.

## What `connect` changes

- `~/.claude/settings.json`: HTTP hooks for `SessionStart`, `UserPromptSubmit`,
  `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `SubagentStart`,
  `SubagentStop`, `Stop` and `SessionEnd`, all posting to
  `http://127.0.0.1:7842/v1/hooks/claude-code`. A backup is kept as
  `settings.json.tracesage.bak`.
- The collector service, if it is not installed yet.

While the collector can read the transcripts it ignores these hooks, so a turn is
never recorded twice. The hooks are a fallback for machines where the
transcripts are not readable.

## Options

| Flag | Effect |
|---|---|
| `--metadata-only` | Keep names, timing, tokens and status, but not prompt, reply or tool text. |
| `--otel` | Also turn on Claude Code's OpenTelemetry export (`CLAUDE_CODE_ENABLE_TELEMETRY` and an OTLP endpoint in `settings.json`). Not needed for traces. |
| `--mcp` | Add tracesage as an MCP server in `~/.claude.json`, so Claude Code can query its own traces. See [MCP server](../mcp.md). |
| `--no-trace` | Skip the hooks; useful with `--mcp` alone. |
| `--dry-run`, `--print claude-code` | Show the planned changes and write nothing. |

## Limits

- Cost is filled in for models that `genai-prices` knows; others show `n/a`.
- Transcripts over 64 MB are skipped.

## Disconnect

```bash
tracesage disconnect claude-code
```

This restores `settings.json` (and `~/.claude.json` if you used `--mcp`) from
the backups. Recorded runs stay until you delete them; see
[Privacy and data](privacy-and-data.md).
