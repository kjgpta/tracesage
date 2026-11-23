"""Background collector: launchd, systemd --user, or a Windows logon task.

The generated unit files are pure text. Tests check that text and never start
the service.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

LABEL = "dev.tracesage.collector"
UNIT_NAME = "tracesage.service"
TASK_NAME = "tracesage-collector"


def choose_port(preferred: int = 7842) -> int:
    """First free loopback port at or above ``preferred``."""
    for port in range(preferred, preferred + 30):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    return preferred


def connect_state_path(data_dir: Path) -> Path:
    return data_dir / "connect.json"


def read_state(data_dir: Path) -> dict:
    path = connect_state_path(data_dir)
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def write_state(data_dir: Path, state: dict) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    current = read_state(data_dir)
    current.update(state)
    connect_state_path(data_dir).write_text(
        json.dumps(current, indent=2) + "\n", encoding="utf-8"
    )


def _plist_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def render_launchd(
    *,
    executable: str,
    data_dir: Path,
    port: int,
    log_path: Path,
    metadata_only: bool = False,
) -> str:
    env = ""
    if metadata_only:
        env = (
            "    <key>EnvironmentVariables</key>\n"
            "    <dict>\n"
            "      <key>TRACESAGE_HOOK_METADATA_ONLY</key>\n"
            "      <string>true</string>\n"
            "    </dict>\n"
        )
    exe = _plist_escape(executable)
    data = _plist_escape(str(data_dir))
    log = _plist_escape(str(log_path))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LABEL}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ProgramArguments</key>
    <array>
      <string>{exe}</string>
      <string>serve</string>
      <string>--ingest</string>
      <string>--host</string>
      <string>127.0.0.1</string>
      <string>--port</string>
      <string>{port}</string>
      <string>-d</string>
      <string>{data}</string>
    </array>
{env}    <key>StandardOutPath</key>
    <string>{log}</string>
    <key>StandardErrorPath</key>
    <string>{log}</string>
</dict>
</plist>
"""


def render_systemd(
    *,
    executable: str,
    data_dir: Path,
    port: int,
    log_path: Path,
    metadata_only: bool = False,
) -> str:
    env = "Environment=TRACESAGE_HOOK_METADATA_ONLY=true\n" if metadata_only else ""
    return (
        "[Unit]\n"
        "Description=tracesage collector\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={executable} serve --ingest --host 127.0.0.1 --port {port} -d {data_dir}\n"
        f"{env}"
        f"StandardOutput=append:{log_path}\n"
        f"StandardError=append:{log_path}\n"
        "Restart=on-failure\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def render_schtasks(
    *,
    executable: str,
    data_dir: Path,
    port: int,
    log_path: Path,
    metadata_only: bool = False,
) -> list[str]:
    env = "set TRACESAGE_HOOK_METADATA_ONLY=true&& " if metadata_only else ""
    command = (
        f'{env}"{executable}" serve --ingest --host 127.0.0.1 --port {port} '
        f'-d "{data_dir}" >> "{log_path}" 2>&1'
    )
    return [
        "schtasks",
        "/create",
        "/sc",
        "onlogon",
        "/tn",
        TASK_NAME,
        "/tr",
        command,
        "/f",
    ]


def launchd_path(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def systemd_path(home: Path) -> Path:
    return home / ".config" / "systemd" / "user" / UNIT_NAME


def platform_name() -> str:
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform.startswith("win"):
        return "windows"
    return "systemd"


def collector_listening(port: int) -> bool:
    """True when something accepts TCP on the collector's loopback port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def recent_sources(data_dir: Path) -> list[str]:
    """Last start time seen for each ``source:`` tag. Read-only; never raises."""
    db_path = data_dir / "traces.db"
    if not db_path.is_file():
        return ["no traces yet"]
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
        try:
            rows = con.execute(
                "SELECT tags, started_at FROM runs ORDER BY started_at DESC LIMIT 200"
            ).fetchall()
        finally:
            con.close()
    except Exception as exc:
        return [f"could not read traces: {exc}"]
    latest: dict[str, str] = {}
    for tags, started in rows:
        try:
            parsed = json.loads(tags or "[]")
        except Exception:
            parsed = None
        if not isinstance(parsed, list):
            continue
        for tag in parsed:
            if isinstance(tag, str) and tag.startswith("source:") and tag not in latest:
                latest[tag] = str(started)
    if not latest:
        return ["no source-tagged runs"]
    return [f"{name}: last run {when}" for name, when in latest.items()]


def start_service(
    home: Path,
    *,
    data_dir: Path,
    executable: str,
    port: int,
    metadata_only: bool,
) -> tuple[int, str]:
    """Load the installed unit. Returns ``(exit_code, message)``."""
    kind = platform_name()
    log_path = data_dir / "logs" / "collector.log"
    if kind == "launchd":
        plist = launchd_path(home)
        if not plist.is_file():
            return 1, "no LaunchAgent installed; run `tracesage service install`"
        domain = f"gui/{os.getuid()}"
        subprocess.run(
            ["launchctl", "bootout", domain, str(plist)],
            check=False,
            capture_output=True,
            text=True,
        )
        proc = subprocess.run(
            ["launchctl", "bootstrap", domain, str(plist)],
            check=False,
            capture_output=True,
            text=True,
        )
        detail = (proc.stderr or proc.stdout or "").strip()
        if proc.returncode != 0 and "already" not in detail.lower():
            return proc.returncode or 1, detail or "launchctl bootstrap failed"
        return 0, f"started {LABEL}"
    if kind == "systemd":
        unit = systemd_path(home)
        if not unit.is_file():
            return 1, "no user unit installed; run `tracesage service install`"
        reload = subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            check=False,
            capture_output=True,
            text=True,
        )
        if reload.returncode != 0:
            return reload.returncode, (reload.stderr or reload.stdout or "daemon-reload failed").strip()
        proc = subprocess.run(
            ["systemctl", "--user", "enable", "--now", UNIT_NAME],
            check=False,
            capture_output=True,
            text=True,
        )
        detail = (proc.stderr or proc.stdout or "").strip()
        if proc.returncode != 0:
            return proc.returncode, detail or "systemctl enable failed"
        return 0, f"started {UNIT_NAME}"
    args = render_schtasks(
        executable=executable,
        data_dir=data_dir,
        port=port,
        log_path=log_path,
        metadata_only=metadata_only,
    )
    created = subprocess.run(args, check=False, capture_output=True, text=True)
    if created.returncode != 0:
        return created.returncode, (created.stderr or created.stdout or "schtasks /create failed").strip()
    ran = subprocess.run(
        ["schtasks", "/run", "/tn", TASK_NAME],
        check=False,
        capture_output=True,
        text=True,
    )
    if ran.returncode != 0:
        return ran.returncode, (ran.stderr or ran.stdout or "schtasks /run failed").strip()
    return 0, f"started {TASK_NAME}"


def stop_service(home: Path) -> tuple[int, str]:
    """Unload the installed unit. Returns ``(exit_code, message)``."""
    kind = platform_name()
    if kind == "launchd":
        plist = launchd_path(home)
        if not plist.is_file():
            return 1, "no LaunchAgent installed"
        proc = subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)],
            check=False,
            capture_output=True,
            text=True,
        )
        detail = (proc.stderr or proc.stdout or "").strip()
        if proc.returncode != 0:
            return proc.returncode, detail or "launchctl bootout failed"
        return 0, f"stopped {LABEL}"
    if kind == "systemd":
        proc = subprocess.run(
            ["systemctl", "--user", "disable", "--now", UNIT_NAME],
            check=False,
            capture_output=True,
            text=True,
        )
        detail = (proc.stderr or proc.stdout or "").strip()
        if proc.returncode != 0:
            return proc.returncode, detail or "systemctl disable failed"
        return 0, f"stopped {UNIT_NAME}"
    proc = subprocess.run(
        ["schtasks", "/end", "/tn", TASK_NAME],
        check=False,
        capture_output=True,
        text=True,
    )
    detail = (proc.stderr or proc.stdout or "").strip()
    if proc.returncode != 0:
        return proc.returncode, detail or "schtasks /end failed"
    return 0, f"stopped {TASK_NAME}"


def render_for_platform(
    *,
    executable: str,
    data_dir: Path,
    port: int,
    log_path: Path,
    metadata_only: bool = False,
    platform: str | None = None,
) -> str | list[str]:
    kind = platform or platform_name()
    if kind == "launchd":
        return render_launchd(
            executable=executable,
            data_dir=data_dir,
            port=port,
            log_path=log_path,
            metadata_only=metadata_only,
        )
    if kind == "windows":
        return render_schtasks(
            executable=executable,
            data_dir=data_dir,
            port=port,
            log_path=log_path,
            metadata_only=metadata_only,
        )
    return render_systemd(
        executable=executable,
        data_dir=data_dir,
        port=port,
        log_path=log_path,
        metadata_only=metadata_only,
    )
