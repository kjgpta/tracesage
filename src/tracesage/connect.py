"""Write MCP and hook configuration for the AI apps a user already has installed.

``tracesage connect`` shows a diff, confirms once, backs up each file, and
writes only the tracesage entries. ``disconnect`` restores the backup bytes.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tracesage.service import (
    choose_port,
    collector_listening,
    launchd_path,
    platform_name,
    read_state,
    recent_sources,
    render_for_platform,
    systemd_path,
    write_state,
)

INSTALL_HINT = 'pip install "tracesage[apps]"'

CLAUDE_HOOKS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "SubagentStart",
    "SubagentStop",
    "Stop",
    "SessionEnd",
)
CURSOR_HOOKS = (
    "sessionStart",
    "sessionEnd",
    "beforeSubmitPrompt",
    "postToolUse",
    "postToolUseFailure",
    "afterShellExecution",
    "afterMCPExecution",
    "afterFileEdit",
    "subagentStart",
    "subagentStop",
    "afterAgentResponse",
    "afterAgentThought",
    "stop",
)
CODEX_HOOKS = CLAUDE_HOOKS

APPS = (
    "claude-code",
    "codex",
    "cursor",
    "claude-desktop",
    "chatgpt",
    "vscode",
    "windsurf",
    "gemini",
)


def apps_extra_ready() -> bool:
    try:
        import mcp  # noqa: F401
        import tomlkit  # noqa: F401
    except ImportError:
        return False
    return True


def executable_path() -> str:
    candidate = Path(sys.argv[0]).resolve()
    if candidate.name.startswith("tracesage") and candidate.is_file():
        return str(candidate)
    found = shutil.which("tracesage")
    return found or sys.executable


def in_project_venv(path: str) -> bool:
    lowered = path.replace("\\", "/").lower()
    return (
        "/.venv/" in lowered
        or "/venv/" in lowered
        or ("site-packages" not in lowered and ".venv" in lowered)
    )


def macos_protected_folder(path: str | Path, home: Path) -> str | None:
    """Return the TCC-protected home folder holding ``path``, if any.

    macOS blocks GUI apps such as Claude Desktop from reading these folders
    unless the user grants access, so an MCP server installed there fails
    with "Operation not permitted" at startup.
    """
    resolved = Path(path).expanduser()
    for name in ("Documents", "Desktop", "Downloads", "Library/Mobile Documents"):
        if resolved.is_relative_to(home / name):
            return f"~/{name}"
    return None


@dataclass
class FileChange:
    path: Path
    before: str
    after: str
    action: str = "write"  # write | conflict | malformed


@dataclass
class Plan:
    changes: list[FileChange] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    service_text: str = ""
    port: int = 7842

    @property
    def blocked(self) -> bool:
        return any(change.action in {"conflict", "malformed"} for change in self.changes)


def _load_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.exists():
        return {}, None
    raw = path.read_text(encoding="utf-8")
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"{path}: invalid JSON ({exc.msg})"
    if not isinstance(loaded, dict):
        return None, f"{path}: expected a JSON object"
    return loaded, None


def _dump(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2) + "\n"


def _mcp_entry(executable: str, data_dir: Path) -> dict[str, Any]:
    return {"command": executable, "args": ["mcp", "-d", str(data_dir)]}


def _ours_http(entry: Any, marker: str) -> bool:
    if not isinstance(entry, dict):
        return False
    hooks = entry.get("hooks")
    if isinstance(hooks, list):
        return any(
            isinstance(h, dict) and marker in str(h.get("url", "")) for h in hooks
        )
    command = str(entry.get("command", ""))
    return "tracesage" in command and "forward" in command


def _hook_block(url: str) -> dict[str, Any]:
    return {"hooks": [{"type": "http", "url": url, "timeout": 1}]}


def _command_block(command: str) -> dict[str, Any]:
    return {"command": command}


def _merge_hooks(
    doc: dict[str, Any],
    events: tuple[str, ...],
    block: dict[str, Any],
    marker: str,
) -> dict[str, Any]:
    hooks = doc.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        doc["hooks"] = hooks
    for event in events:
        current = hooks.get(event)
        if not isinstance(current, list):
            current = []
        kept = [item for item in current if not _ours_http(item, marker)]
        kept.append(block)
        hooks[event] = kept
    return doc


def _merge_mcp(doc: dict[str, Any], key: str, entry: dict[str, Any], *, overwrite: bool) -> str | None:
    servers = doc.setdefault(key, {})
    if not isinstance(servers, dict):
        return "mcp server table is not an object"
    existing = servers.get("tracesage")
    if existing is not None and existing != entry and not overwrite:
        return "an MCP server named tracesage already exists"
    servers["tracesage"] = entry
    return None


def _forward_command(home: Path, app: str, port: int) -> str:
    script = home / ".tracesage" / "bin" / ("forward.ps1" if os.name == "nt" else "forward")
    url = f"http://127.0.0.1:{port}/v1/hooks/{app}"
    spool = home / ".tracesage" / "spool" / f"{app}.jsonl"
    if os.name == "nt":
        return f'powershell -NoProfile -File "{script}" {app} {url} "{spool}"'
    return f'"{script}" {app} {url} "{spool}"'


def forward_script() -> str:
    return """#!/bin/sh
# tracesage hook forwarder. Always exits 0 and prints {} so the agent is never blocked.
URL="$2"
SPOOL="$3"
payload=$(cat || true)
mkdir -p "$(dirname "$SPOOL")"
if ! printf '%s' "$payload" | curl -s -m 1 --data-binary @- -H 'content-type: application/json' -o /dev/null "$URL"; then
  printf '%s\\n' "$payload" >> "$SPOOL"
fi
printf '%s\\n' '{}'
exit 0
"""


def forward_script_ps1() -> str:
    return """param([string]$App, [string]$Url, [string]$Spool)
$payload = [Console]::In.ReadToEnd()
New-Item -ItemType Directory -Force -Path (Split-Path $Spool) | Out-Null
try {
  Invoke-RestMethod -Uri $Url -Method Post -Body $payload -ContentType 'application/json' -TimeoutSec 1 | Out-Null
} catch {
  Add-Content -Path $Spool -Value $payload
}
Write-Output '{}'
exit 0
"""


def _claude_desktop_config(home: Path) -> Path:
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if sys.platform.startswith("win"):
        return home / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"
    return home / ".config" / "Claude" / "claude_desktop_config.json"


def _vscode_mcp(home: Path) -> Path:
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Code" / "User" / "mcp.json"
    if sys.platform.startswith("win"):
        return home / "AppData" / "Roaming" / "Code" / "User" / "mcp.json"
    return home / ".config" / "Code" / "User" / "mcp.json"


def detect_installed(home: Path) -> list[str]:
    found: list[str] = []
    checks = {
        "claude-code": home / ".claude",
        "codex": home / ".codex",
        "cursor": home / ".cursor",
        "claude-desktop": _claude_desktop_config(home).parent,
        "vscode": _vscode_mcp(home).parent,
        "windsurf": home / ".codeium" / "windsurf",
        "gemini": home / ".gemini",
    }
    for name, path in checks.items():
        if path.exists():
            found.append(name)
    if "codex" in found:
        found.append("chatgpt")
    return found


def _queue(plan: Plan, path: Path, before: str, after: str, action: str = "write") -> None:
    if before == after and action == "write":
        return
    plan.changes.append(FileChange(path=path, before=before, after=after, action=action))


def _json_change(
    plan: Plan,
    path: Path,
    mutate,
) -> None:
    doc, error = _load_json(path)
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    if error or doc is None:
        plan.changes.append(FileChange(path, before, before, action="malformed"))
        plan.notes.append(error or f"{path}: could not read")
        return
    problem = mutate(doc)
    if problem:
        plan.changes.append(FileChange(path, before, before, action="conflict"))
        plan.notes.append(f"{path}: {problem}")
        return
    _queue(plan, path, before, _dump(doc))


def build_plan(
    home: Path,
    apps: list[str],
    *,
    data_dir: Path,
    executable: str,
    port: int = 7842,
    metadata_only: bool = False,
    mcp: bool = False,
    trace: bool = True,
    otel: bool = False,
    overwrite: bool = False,
) -> Plan:
    plan = Plan(port=port)
    selected = ["codex" if name == "chatgpt" else name for name in apps]
    marker = "/v1/hooks/"
    entry = _mcp_entry(executable, data_dir)

    if "claude-code" in selected:
        settings = home / ".claude" / "settings.json"
        if trace:
            url = f"http://127.0.0.1:{port}/v1/hooks/claude-code"

            def mutate_settings(doc: dict[str, Any]) -> str | None:
                _merge_hooks(doc, CLAUDE_HOOKS, _hook_block(url), marker)
                if otel:
                    env = doc.setdefault("env", {})
                    if isinstance(env, dict):
                        env["CLAUDE_CODE_ENABLE_TELEMETRY"] = "1"
                        env["CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"] = "1"
                        env["OTEL_TRACES_EXPORTER"] = "otlp"
                        env["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"http://127.0.0.1:{port}"
                        if not metadata_only:
                            env["OTEL_LOG_USER_PROMPTS"] = "1"
                            env["OTEL_LOG_TOOL_DETAILS"] = "1"
                            env["OTEL_LOG_TOOL_CONTENT"] = "1"
                return None

            _json_change(plan, settings, mutate_settings)  # returns None on success
        if mcp:
            _json_change(
                plan,
                home / ".claude.json",
                lambda doc: _merge_mcp(doc, "mcpServers", entry, overwrite=overwrite),
            )
        plan.notes.append("Restart Claude Code to pick up the new hooks.")

    if "codex" in selected:
        if trace:
            url_cmd = _forward_command(home, "codex", port)
            hooks_path = home / ".codex" / "hooks.json"
            def mutate_codex_hooks(doc: dict[str, Any]) -> None:
                _merge_hooks(doc, CODEX_HOOKS, _command_block(url_cmd), "forward")
                return None

            _json_change(plan, hooks_path, mutate_codex_hooks)
            plan.notes.append("In Codex, run /hooks once and trust the tracesage hooks.")
        if mcp or otel:
            plan.changes.extend(
                _codex_toml(
                    home / ".codex" / "config.toml",
                    executable,
                    data_dir,
                    port,
                    mcp=mcp,
                    otel=otel,
                    metadata_only=metadata_only,
                    overwrite=overwrite,
                )
            )

    if "cursor" in selected:
        if trace:
            command = _forward_command(home, "cursor", port)
            def mutate_cursor_hooks(doc: dict[str, Any]) -> None:
                _merge_hooks(doc, CURSOR_HOOKS, _command_block(command), "forward")
                return None

            _json_change(plan, home / ".cursor" / "hooks.json", mutate_cursor_hooks)
        if mcp:
            _json_change(
                plan,
                home / ".cursor" / "mcp.json",
                lambda doc: _merge_mcp(doc, "mcpServers", entry, overwrite=overwrite),
            )
        plan.notes.append("Restart Cursor. New agent chats show up in the tracesage UI.")

    if "claude-desktop" in selected and mcp:
        _json_change(
            plan,
            _claude_desktop_config(home),
            lambda doc: _merge_mcp(doc, "mcpServers", entry, overwrite=overwrite),
        )
        plan.notes.append(
            "This lets Claude Desktop query traces over MCP. Its sidebar chats and Cowork "
            "are recorded separately, from local files, while the collector runs. "
            "Restart the app to load the MCP server."
        )
        if sys.platform == "darwin":
            for label, value in (("tracesage", executable), ("Data dir", data_dir)):
                folder = macos_protected_folder(value, home)
                if folder:
                    plan.notes.append(
                        f"{label} is under {folder}, which macOS blocks Claude Desktop "
                        "from reading. Install with `uv tool install 'tracesage[apps]'` "
                        "and keep the data dir outside that folder."
                    )
    elif "claude-desktop" in selected:
        plan.notes.append(
            "Claude Desktop sidebar chats saved on this machine, and Cowork, are read "
            "while this collector is running. Its Code tab is Claude Code; "
            "`tracesage connect claude-code` records those sessions."
        )

    if "vscode" in selected and mcp:
        _json_change(
            plan,
            _vscode_mcp(home),
            lambda doc: _merge_mcp(doc, "servers", entry, overwrite=overwrite),
        )
    if "windsurf" in selected and mcp:
        _json_change(
            plan,
            home / ".codeium" / "windsurf" / "mcp_config.json",
            lambda doc: _merge_mcp(doc, "mcpServers", entry, overwrite=overwrite),
        )
    if "gemini" in selected and mcp:
        _json_change(
            plan,
            home / ".gemini" / "settings.json",
            lambda doc: _merge_mcp(doc, "mcpServers", entry, overwrite=overwrite),
        )

    if in_project_venv(executable):
        plan.notes.append(
            f"{executable} is inside a project virtualenv. "
            "Prefer `uv tool install 'tracesage[apps]'` or pipx so desktop apps keep working."
        )
    plan.notes.append(
        "Prompts and tool output stay on this machine and pass through redaction."
        if not metadata_only
        else "Metadata only: prompt and tool text will not be stored."
    )
    kind = platform_name()
    rendered = render_for_platform(
        executable=executable,
        data_dir=data_dir,
        port=port,
        log_path=data_dir / "logs" / "collector.log",
        metadata_only=metadata_only,
        platform=kind,
    )
    plan.service_text = rendered if isinstance(rendered, str) else " ".join(rendered)
    return plan


def _codex_toml(
    path: Path,
    executable: str,
    data_dir: Path,
    port: int,
    *,
    mcp: bool,
    otel: bool,
    metadata_only: bool,
    overwrite: bool,
) -> list[FileChange]:
    import tomlkit

    before = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        doc = tomlkit.parse(before) if before else tomlkit.document()
    except Exception:
        return [FileChange(path, before, before, action="malformed")]
    if mcp:
        servers = doc.get("mcp_servers")
        if servers is None:
            servers = tomlkit.table()
            doc["mcp_servers"] = servers
        existing = servers.get("tracesage") if hasattr(servers, "get") else None
        entry = tomlkit.table()
        entry.add("command", executable)
        entry.add("args", ["mcp", "-d", str(data_dir)])
        if existing is not None and not overwrite:
            current_cmd = existing.get("command") if hasattr(existing, "get") else None
            if current_cmd not in (None, executable):
                return [FileChange(path, before, before, action="conflict")]
        servers["tracesage"] = entry
    if otel:
        block = doc.get("otel")
        if block is None:
            block = tomlkit.table()
            doc["otel"] = block
        exporter = tomlkit.table()
        http = tomlkit.table()
        http.add("endpoint", f"http://127.0.0.1:{port}/v1/logs")
        http.add("protocol", "binary")
        exporter.add("otlp-http", http)
        block["exporter"] = exporter
        trace = tomlkit.table()
        thttp = tomlkit.table()
        thttp.add("endpoint", f"http://127.0.0.1:{port}/v1/traces")
        thttp.add("protocol", "binary")
        trace.add("otlp-http", thttp)
        block["trace_exporter"] = trace
        if not metadata_only:
            block["log_user_prompt"] = True
    after = tomlkit.dumps(doc)
    if after == before:
        return []
    return [FileChange(path, before, after)]


def apply_plan(plan: Plan, *, dry_run: bool) -> None:
    if dry_run or plan.blocked:
        return
    for change in plan.changes:
        if change.action != "write":
            continue
        change.path.parent.mkdir(parents=True, exist_ok=True)
        backup = Path(str(change.path) + ".tracesage.bak")
        if change.path.exists() and not backup.exists():
            shutil.copy2(change.path, backup)
        change.path.write_text(change.after, encoding="utf-8")


def write_forwarder(home: Path) -> Path:
    bindir = home / ".tracesage" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    path = bindir / ("forward.ps1" if os.name == "nt" else "forward")
    path.write_text(forward_script_ps1() if os.name == "nt" else forward_script(), encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o755)
    (home / ".tracesage" / "spool").mkdir(parents=True, exist_ok=True)
    return path


def install_service_file(
    home: Path,
    *,
    executable: str,
    data_dir: Path,
    port: int,
    metadata_only: bool,
) -> Path | None:
    """Write the unit file. Does not start it (tests and --dry-run stop here)."""
    kind = platform_name()
    log_path = data_dir / "logs" / "collector.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    rendered = render_for_platform(
        executable=executable,
        data_dir=data_dir,
        port=port,
        log_path=log_path,
        metadata_only=metadata_only,
        platform=kind,
    )
    if kind == "launchd":
        path = launchd_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(rendered), encoding="utf-8")
        return path
    if kind == "systemd":
        path = systemd_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(rendered), encoding="utf-8")
        return path
    return None


def disconnect(home: Path, apps: list[str] | None = None) -> list[Path]:
    """Restore ``*.tracesage.bak`` files under the app config dirs. Returns restored paths."""
    roots = [
        home / ".claude",
        home / ".codex",
        home / ".cursor",
        home / ".gemini",
        home / ".codeium",
        _claude_desktop_config(home).parent,
        _vscode_mcp(home).parent,
        home,
    ]
    restored: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        for backup in root.rglob("*.tracesage.bak"):
            target = Path(str(backup)[: -len(".tracesage.bak")])
            if target in seen:
                continue
            if apps and not _backup_matches(target, apps):
                continue
            seen.add(target)
            shutil.copy2(backup, target)
            backup.unlink()
            restored.append(target)
    return restored


def _backup_matches(path: Path, apps: list[str]) -> bool:
    text = str(path)
    mapping = {
        "claude-code": ".claude",
        "codex": ".codex",
        "chatgpt": ".codex",
        "cursor": ".cursor",
        "claude-desktop": "Claude",
        "vscode": "Code",
        "windsurf": "windsurf",
        "gemini": ".gemini",
    }
    return any(mapping.get(name, name) in text for name in apps)


def status_lines(home: Path, data_dir: Path) -> list[str]:
    lines = []
    state = read_state(data_dir)
    port = int(state.get("port") or 7842)
    listening = "listening" if collector_listening(port) else "not listening"
    lines.append(f"collector port: {port} ({listening})")
    lines.append("recent sources:")
    for line in recent_sources(data_dir):
        lines.append(f"  {line}")
    installed = detect_installed(home)
    lines.append("installed: " + (", ".join(installed) if installed else "(none detected)"))
    for name, path in (
        ("claude-code hooks", home / ".claude" / "settings.json"),
        ("codex", home / ".codex" / "config.toml"),
        ("cursor hooks", home / ".cursor" / "hooks.json"),
        ("cursor mcp", home / ".cursor" / "mcp.json"),
        ("claude desktop", _claude_desktop_config(home)),
    ):
        if not path.exists():
            lines.append(f"  {name}: not configured")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        mark = (
            "connected"
            if "tracesage" in text or "/v1/hooks/" in text
            else "present, no tracesage entry"
        )
        lines.append(f"  {name}: {mark}")
    return lines


MCPB_RELEASE_URL = (
    "https://github.com/kjgpta/tracesage/releases/latest/download/tracesage.mcpb"
)


def _mcpb_candidates() -> tuple[Path, Path]:
    """Packaged copy first (a PyPI install), then the repo copy (a source checkout)."""
    here = Path(__file__).resolve().parent
    # here is the package dir: <repo>/src/tracesage in a checkout, so the repo
    # root is parents[1]. A wheel has no tools/mcpb there; _mcpb is used instead.
    return (here / "_mcpb", here.parents[1] / "tools" / "mcpb")


def mcpb_source_dir() -> Path | None:
    """Directory that holds ``manifest.json`` for the Desktop extension."""
    for candidate in _mcpb_candidates():
        if (candidate / "manifest.json").is_file():
            return candidate
    return None


def pack_mcpb(data_dir: Path) -> Path | None:
    """Build ``tracesage.mcpb`` into ``data_dir``. Returns the bundle path, or None.

    Packs from a temporary copy so the install directory stays untouched, including
    when that directory is read-only (a typical ``site-packages``).
    """
    root = mcpb_source_dir()
    if root is None:
        sys.stderr.write(
            "Claude Desktop extension source was not found. "
            f"Download {MCPB_RELEASE_URL}\n"
        )
        return None
    dest = data_dir / "tracesage.mcpb"
    data_dir.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="tracesage-mcpb-") as tmp:
            stage = Path(tmp) / "mcpb"
            shutil.copytree(
                root,
                stage,
                ignore=shutil.ignore_patterns("*.mcpb", "__pycache__", ".venv"),
            )
            proc = subprocess.run(  # noqa: S603
                ["npx", "--yes", "@anthropic-ai/mcpb", "pack", str(stage), str(dest)],  # noqa: S607
                check=False,
                capture_output=True,
                text=True,
            )
    except FileNotFoundError:
        sys.stderr.write(
            "npx is not installed, so the Desktop extension was not built. "
            f"Download {MCPB_RELEASE_URL}\n"
        )
        return None
    if proc.returncode != 0 or not dest.is_file():
        detail = (proc.stderr or proc.stdout or "").strip()
        if detail:
            sys.stderr.write(detail + "\n")
        sys.stderr.write(f"Download {MCPB_RELEASE_URL}\n")
        return None
    return dest


def prepare_port(data_dir: Path, preferred: int = 7842) -> int:
    state = read_state(data_dir)
    if isinstance(state.get("port"), int):
        return int(state["port"])
    port = choose_port(preferred)
    write_state(data_dir, {"port": port})
    return port
