"""Guard tests against doc/code drift.

These tests assert that the CLI commands and config env vars advertised in the
docs (README, CLI reference, configuration guide, integration guide) actually
exist in the code. They are intentionally robust — no fragile markdown parsing,
just import the real objects and check membership.
"""
from __future__ import annotations

from tracelens.cli import app
from tracelens.config import TraceLensConfig


def _registered_command_names() -> set[str]:
    """Collect the command names registered on the Typer app.

    Each entry in `app.registered_commands` carries an explicit `name` or falls
    back to the callback function's `__name__` (Typer's own default), so mirror
    that resolution here.
    """
    names: set[str] = set()
    for cmd in app.registered_commands:
        name = getattr(cmd, "name", None)
        if not name:
            callback = getattr(cmd, "callback", None)
            if callback is not None:
                name = callback.__name__
        if name:
            names.add(name)
    return names


def test_documented_cli_commands_exist() -> None:
    """Every CLI command the docs advertise must be registered in the app."""
    documented = {
        "serve", "export", "stats", "runs", "gc", "version",
        # developer commands (docs/cli.md, docs/development.md, README)
        "demo", "show", "watch", "diff", "view", "import", "doctor",
    }
    registered = _registered_command_names()
    missing = documented - registered
    assert not missing, f"documented CLI commands missing from app: {sorted(missing)}"


def test_documented_config_fields_exist() -> None:
    """Core documented config env vars must map to real TraceLensConfig fields."""
    documented = {
        "host",
        "port",
        "auth_token",
        "data_dir",
        "sample_rate",
        "per_run_event_cap",
        "max_runs",
    }
    fields = set(TraceLensConfig.model_fields)
    missing = documented - fields
    assert not missing, f"documented config fields missing from TraceLensConfig: {sorted(missing)}"
