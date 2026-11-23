# Codex and the ChatGPT desktop app

Record Codex sessions (the Codex CLI, the Codex IDE extension, and Codex inside
the ChatGPT desktop app) as traces: each model request with its model, reasoning
and tokens, each tool call and shell command, and each subagent started with
`spawn_agent`.

Ordinary ChatGPT chats cannot be recorded. The desktop app encrypts its local
cache with a key in the macOS Keychain, and web chats are stored on OpenAI's
servers.

## Connect it

```bash
pip install "tracesage[apps]"        # or: uv tool install "tracesage[apps]"
tracesage connect codex              # "chatgpt" is an alias
```

Then, once, in Codex: run `/hooks` and trust the tracesage hooks. Open
[http://127.0.0.1:7842](http://127.0.0.1:7842) and set **Source** to Codex.
Sessions from the last 30 days are imported on first start.

## What is recorded, and from where

| What | Source |
|---|---|
| Turns, model requests (model, reasoning, reply, tokens, cost) | `~/.codex/sessions/YYYY/MM/DD/rollout-<time>-<id>.jsonl` |
| Tool calls: function and custom tools, shell commands, web searches | The same rollout, paired by call id |
| Subagents from `spawn_agent` | Their own rollout, nested under the spawning call |

A session becomes a thread and each prompt a run. The same folder is used on
every platform (`%USERPROFILE%\.codex\sessions` on Windows).

## What `connect` changes

- `~/.codex/hooks.json`: command hooks that run
  `~/.tracesage/bin/forward codex`. Codex asks you to trust them through
  `/hooks`. When the collector is down, events go to
  `~/.tracesage/spool/codex.jsonl` and are replayed on its next start.
- `~/.codex/config.toml`, only with `--mcp` or `--otel`.
- The collector service, if it is not installed yet.

While the collector can read the rollouts it ignores the hooks, so turns are not
recorded twice.

## Options

| Flag | Effect |
|---|---|
| `--metadata-only` | Keep names, timing, tokens and status, but not prompt, reply or tool text. |
| `--otel` | Also add `[otel]` exporters to `config.toml` (`/v1/logs` and `/v1/traces`). Not needed for traces. |
| `--mcp` | Add `[mcp_servers.tracesage]` to `config.toml`, so Codex can query traces. See [MCP server](../mcp.md). |
| `--no-trace` | Skip the hooks; useful with `--mcp` alone. |
| `--dry-run`, `--print codex` | Show the planned changes and write nothing. |

Set `TRACESAGE_IMPORT_DAYS` before the first start to import more than 30 days
(see [Configuration](../configuration.md#ai-app-collector)).

## Limits

- Cost is filled in for models that `genai-prices` knows; others show `n/a`.
- Sessions older than the import window are skipped on first start.

## Disconnect

```bash
tracesage disconnect codex
```

This restores `hooks.json` and `config.toml` from the backups. A file that did
not exist before has no backup; remove the tracesage entries from it by hand.
Recorded runs stay until you delete them; see
[Privacy and data](privacy-and-data.md).
