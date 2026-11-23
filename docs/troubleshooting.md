# Troubleshooting & FAQ

Quick answers to the things that trip people up first. For production-specific
issues (queue drops, `FOREIGN KEY` warnings, missing `agent_name`), see
[Production → Troubleshooting](production.md#troubleshooting).

## Install

### `zsh: no matches found: tracesage[langchain]`

zsh (the default macOS shell) treats `[...]` as a glob. **Quote the extra:**

```bash
pip install "tracesage[langchain]"
```

Same for `"tracesage[mcp]"` and `"tracesage[otel]"`.

### `ImportError: ... Install with: pip install "tracesage[langchain]"`

`import tracesage` works with no extras, but using the LangChain handler needs
`langchain-core`. Install the adapter extra:

```bash
pip install "tracesage[langchain]"
```

If your app uses **LangGraph**, also `pip install langgraph` (tracesage doesn't
pull it). For MCP attribution add `"tracesage[mcp]"`.

## "Where are my runs?"

This is the **#1** confusion, and it's almost always a **data-dir mismatch**.

Every cross-run view — the run list, topology, and the Tools tab — is scoped
to a single `data_dir`. If the app that *wrote* the traces and the viewer you
*opened* point at different dirs, the viewer looks empty (or shows someone else's
runs).

- Each bundled example writes to its **own** dir under `~/.tracesage/` (e.g.
  `~/.tracesage/mcp-mixed`) and prints `Data dir:` + the exact `tracesage serve -d …`
  on startup. Read that line.
- Open the **printed** `🔍 tracesage:` link rather than guessing a URL.
- To inspect a specific app's data:

  ```bash
  tracesage runs  -d ~/.tracesage/mcp-mixed     # list what's actually there
  tracesage serve -d ~/.tracesage/mcp-mixed     # view that exact dir
  ```

- `tracesage doctor -d <dir>` reports whether a dir has a DB, how many runs, and
  blob health — run it when in doubt.

See [Configuration → Isolating multiple applications](configuration.md#isolating-multiple-applications)
for why each app should get its own `data_dir`.

## "All my runs have the same run ID" / the IDs look identical

They're not the same — look past the prefix. tracesage run IDs are **UUIDv7**,
which are *time-ordered*: the leading characters encode a millisecond timestamp,
so runs created close together **share a prefix** (`019ed9e9-…`, `019ed9ea-…`)
and look alike at a glance. The full ID is unique. The run list sorts by this
ordering (newest first), which is why it's stable and chronological.

## "I removed a tool / MCP server but it's still in the topology"

The topology defaults to the **selected run** ("This run"), so a fresh run won't
show components it didn't use. If you still see stale nodes:

- Check the **scope selector** in the topology toolbar — if it's set to *All time*
  or *Last N runs*, it's aggregating older runs on purpose. Switch it back to
  *This run*.
- Selecting an older run shows **that run's** structure, not the latest.
- Via the API: `GET /api/topology?scope=run:<id>` (vs `last_n:<N>` / `all`).

## AI-app sessions

These apply to sessions recorded by `tracesage connect`. Start every check with:

```bash
tracesage connect --status
```

```text
collector port: 7842 (listening)
recent sources:
  source:claude-code: last run 2026-11-22T10:12:40+00:00
installed: claude-code, cursor, claude-desktop
  claude-code hooks: connected
  codex: not configured
  cursor hooks: connected
  cursor mcp: present, no tracesage entry
  claude desktop: present, no tracesage entry
```

`claude desktop: present, no tracesage entry` is normal: recording Claude Desktop
needs no entry. The entry only appears after `connect claude-desktop --mcp`.

### Nothing shows up at all

- **`not listening`.** The collector is not running. Run `tracesage service start`
  (or `tracesage service status`), then look at `~/.tracesage/logs/collector.log`.
  Its first lines name the sources it found, for example
  `local sessions (claude-code, cursor), last 30 days on first import`.
- **The UI is on another port.** The port is in `~/.tracesage/connect.json`. Open
  the URL from `--status`.
- **A source filter is on.** Set Source to All sources and clear the search box.

### One app's sessions are missing

- **Claude Desktop chat.** Desktop only saves a chat to disk once it is opened
  on this machine. Open it in Desktop and wait a few seconds. Chats from the web
  or another computer are not on this machine. The Code tab is Claude Code:
  connect `claude-code` for it.
- **Cursor or Codex chat older than 30 days.** The first import reaches back
  `TRACESAGE_IMPORT_DAYS` days. To import more, set it in the service
  environment and [import again](apps/privacy-and-data.md#import-again-from-scratch).
- **Codex.** Run `/hooks` in Codex once and trust the tracesage hooks.
- **A very large session.** Claude Code transcripts over 64 MB are skipped.
- **ChatGPT chats.** Not recordable; only Codex sessions inside the ChatGPT app are.

### New turns appear late, or only after a restart

The collector polls the apps' files; a finished turn appears within a few
seconds. If turns only
appear after restarting the collector, check `collector.log` for errors.

### A turn shows the wrong numbers

- **Cost is n/a.** The model is unknown to `genai-prices` (common for Cursor,
  whose model names such as `default` have no price). Set
  `TRACESAGE_PRICE_OVERRIDES` to price it.
- **Cursor steps look merged.** Cursor does not store individual model requests,
  so steps are inferred from its message segments.

### The Claude Desktop MCP server does not start (macOS)

macOS stops Claude Desktop from running programs or reading data under
`~/Documents`, `~/Desktop` and `~/Downloads`. Install with
`uv tool install "tracesage[apps]"`, keep the data dir outside those folders, and
run `tracesage connect claude-desktop --mcp` again. More in
[Claude Desktop](apps/claude-desktop.md#ask-claude-about-your-traces).

### Threads mode has no Run trace or Waterfall

That is on purpose: Threads shows whole chats. Click **Open run ↗** on a turn to
see its trace. See [Using the UI](apps/using-the-ui.md#runs-and-threads).

## Port / opening the UI

### "Address already in use" / it opened on a different port

By default tracesage binds `7842`; if that's busy (e.g. a second app), **auto-port**
takes the next free one (`7843`, …). The actual URL is printed on startup and
available as `tracer.ui_url`. Open the printed link, not a hardcoded `7842`.

To pin a port and *fail* instead of hopping, set `port_auto=False`
(`TRACESAGE_PORT_AUTO=false`) with an explicit `port`.

### Two apps' traces are bleeding into one graph

They're sharing a `data_dir`. Give each its own — see
[Running several apps at once](configuration.md#running-several-apps-at-once):

```bash
TRACESAGE_PROJECT_NAME=app-a TRACESAGE_DATA_DIR=~/.tracesage/app-a python app_a.py
TRACESAGE_PROJECT_NAME=app-b TRACESAGE_DATA_DIR=~/.tracesage/app-b python app_b.py
```

## The UI loads but looks unstyled

The UI is fully self-contained (CSS is vendored, no CDN) and works offline. If it
looks broken, it's usually a **reverse proxy** rewriting or blocking the static
assets under `/ui/` — make sure `/ui/`, `/ui/vendor/`, `/ui/styles.css`, and
`/ui/app.js` are passed through. The UI uses relative URLs, so a path prefix
(`location /tracesage/`) works as long as it's not stripped inconsistently. See
[Production → TLS](production.md#tls) for an nginx example.

## The trace link prints but the page is empty when I open it later

The embedded UI server stops when your script's process exits. A one-shot script
must stay alive while you look (the `input(...)` trick in the
[Quickstart](quickstart.md)). The data still persists to `data_dir`, so you can
always reopen it afterwards with `tracesage serve -d <dir>`.

## A token-budget test always passes (even when it shouldn't)

`FakeListChatModel` (used by the no-key examples) reports **no** token usage, so
`total_tokens()` is `(0, 0)` and a `< N` assertion is vacuous. Gate token
assertions behind a real provider — see
[Developer guide → Testing your agents](development.md#testing-your-agents).

## Async test silently "passes" without running

Async tests need [`pytest-asyncio`](https://pytest-asyncio.readthedocs.io/) with
`asyncio_mode = "auto"`, or pytest skips the coroutine and reports a false green.
See the [Developer guide](development.md#testing-your-agents).
