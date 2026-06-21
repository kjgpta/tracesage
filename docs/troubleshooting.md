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

Every cross-run view — the run list, topology, and "Tools by source" — is scoped
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
