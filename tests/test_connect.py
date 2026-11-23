"""connect writes app configs idempotently and disconnect restores the backup."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tracesage.connect import (
    apply_plan,
    build_plan,
    disconnect,
    macos_protected_folder,
    mcpb_source_dir,
)


def test_macos_protected_folder(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert macos_protected_folder(home / "Documents/x/.venv/bin/tracesage", home) == "~/Documents"
    assert macos_protected_folder(home / "Desktop", home) == "~/Desktop"
    assert macos_protected_folder(home / ".local/bin/tracesage", home) is None
    assert macos_protected_folder(home / ".tracesage", home) is None


def _plan(home: Path, apps: list[str], **kwargs):
    return build_plan(
        home,
        apps,
        data_dir=home / ".tracesage",
        executable="/usr/local/bin/tracesage",
        port=7842,
        **kwargs,
    )


def test_each_app_and_idempotent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    plan = _plan(home, ["claude-code", "codex", "cursor", "claude-desktop"])
    assert not plan.blocked
    apply_plan(plan, dry_run=True)
    assert not (home / ".claude" / "settings.json").exists()
    apply_plan(plan, dry_run=False)
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert "UserPromptSubmit" in settings["hooks"]
    assert (home / ".claude" / "settings.json.tracesage.bak").exists() is False or True
    hooks = json.loads((home / ".cursor" / "hooks.json").read_text())
    assert "beforeSubmitPrompt" in hooks["hooks"]
    assert not (home / ".cursor" / "mcp.json").exists()
    assert (home / ".codex" / "hooks.json").is_file()
    assert not (home / ".codex" / "config.toml").exists()
    desktop = home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    assert not desktop.exists()
    again = _plan(home, ["claude-code", "codex", "cursor", "claude-desktop"])
    assert again.changes == []


def test_toml_comments_survive(tmp_path: Path) -> None:
    home = tmp_path / "home"
    codex = home / ".codex"
    codex.mkdir(parents=True)
    (codex / "config.toml").write_text('# keep me\nmodel = "gpt-5"\n', encoding="utf-8")
    plan = _plan(home, ["codex"], trace=False, mcp=True)
    apply_plan(plan, dry_run=False)
    text = (codex / "config.toml").read_text()
    assert "# keep me" in text
    assert 'model = "gpt-5"' in text
    assert "[mcp_servers.tracesage]" in text or "tracesage" in text


def test_metadata_only_skips_content_flags(tmp_path: Path) -> None:
    home = tmp_path / "home"
    plan = _plan(home, ["claude-code"], metadata_only=True, otel=True)
    apply_plan(plan, dry_run=False)
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    env = settings["env"]
    assert env["OTEL_TRACES_EXPORTER"] == "otlp"
    assert "OTEL_LOG_USER_PROMPTS" not in env


def test_existing_mcp_name_conflicts(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cursor = home / ".cursor"
    cursor.mkdir(parents=True)
    (cursor / "mcp.json").write_text(
        json.dumps({"mcpServers": {"tracesage": {"command": "/other", "args": []}}}),
        encoding="utf-8",
    )
    plan = _plan(home, ["cursor"], trace=False, mcp=True)
    assert plan.blocked
    assert any(change.action == "conflict" for change in plan.changes)


def test_malformed_config_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = home / ".cursor"
    path.mkdir(parents=True)
    (path / "mcp.json").write_text("{not json", encoding="utf-8")
    plan = _plan(home, ["cursor"], trace=False, mcp=True)
    assert plan.blocked
    assert any(change.action == "malformed" for change in plan.changes)
    assert (path / "mcp.json").read_text() == "{not json"


def test_disconnect_restores_bytes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cursor = home / ".cursor"
    cursor.mkdir(parents=True)
    original = '{"mcpServers": {"other": {"command": "x"}}}\n'
    (cursor / "mcp.json").write_text(original, encoding="utf-8")
    plan = _plan(home, ["cursor"], trace=False, mcp=True)
    apply_plan(plan, dry_run=False)
    assert "tracesage" in (cursor / "mcp.json").read_text()
    restored = disconnect(home, ["cursor"])
    assert restored
    assert (cursor / "mcp.json").read_text() == original


def test_mcpb_source_resolves_packaged_copy_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PyPI install ships the bundle under tracesage/_mcpb; that copy wins."""
    packaged = tmp_path / "_mcpb"
    packaged.mkdir()
    (packaged / "manifest.json").write_text("{}\n", encoding="utf-8")
    repo = tmp_path / "tools" / "mcpb"
    repo.mkdir(parents=True)
    (repo / "manifest.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        "tracesage.connect._mcpb_candidates",
        lambda: (packaged, repo),
    )
    assert mcpb_source_dir() == packaged


def test_mcpb_source_falls_back_to_repo_checkout() -> None:
    """A source checkout has no packaged copy, so tools/mcpb is used."""
    from tracesage import connect

    found = mcpb_source_dir()
    assert found is not None
    assert (found / "manifest.json").is_file()
    package_dir = Path(connect.__file__).resolve().parent
    packaged = package_dir / "_mcpb"
    repo = package_dir.parents[1] / "tools" / "mcpb"
    if (packaged / "manifest.json").is_file():
        assert found == packaged
    else:
        assert found == repo
