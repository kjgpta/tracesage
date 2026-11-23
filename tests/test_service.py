"""Service unit files are generated. Start/stop are checked with a fake subprocess."""
from __future__ import annotations

from pathlib import Path

from tracesage.service import (
    launchd_path,
    render_launchd,
    render_schtasks,
    render_systemd,
    start_service,
    stop_service,
)


def test_launchd_plist(tmp_path: Path) -> None:
    text = render_launchd(
        executable="/usr/local/bin/tracesage",
        data_dir=tmp_path,
        port=7842,
        log_path=tmp_path / "collector.log",
        metadata_only=True,
    )
    assert "dev.tracesage.collector" in text
    assert "RunAtLoad" in text
    assert "KeepAlive" in text
    assert "serve" in text
    assert "--ingest" in text
    assert "TRACESAGE_HOOK_METADATA_ONLY" in text
    assert "127.0.0.1" in text


def test_systemd_unit(tmp_path: Path) -> None:
    text = render_systemd(
        executable="/usr/bin/tracesage",
        data_dir=tmp_path,
        port=7843,
        log_path=tmp_path / "collector.log",
    )
    assert "ExecStart=/usr/bin/tracesage serve --ingest" in text
    assert "--port 7843" in text
    assert "WantedBy=default.target" in text


def test_schtasks_args(tmp_path: Path) -> None:
    args = render_schtasks(
        executable=r"C:\tracesage.exe",
        data_dir=tmp_path,
        port=7842,
        log_path=tmp_path / "collector.log",
    )
    assert args[:4] == ["schtasks", "/create", "/sc", "onlogon"]
    assert "/tn" in args
    joined = " ".join(args)
    assert "serve --ingest" in joined


def test_start_and_stop_use_launchctl_without_running_it(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []

    class _Result:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return _Result()

    plist = launchd_path(tmp_path)
    plist.parent.mkdir(parents=True)
    plist.write_text("<plist/>", encoding="utf-8")
    monkeypatch.setattr("tracesage.service.platform_name", lambda: "launchd")
    monkeypatch.setattr("tracesage.service.os.getuid", lambda: 501, raising=False)
    monkeypatch.setattr("tracesage.service.subprocess.run", fake_run)
    code, message = start_service(
        tmp_path,
        data_dir=tmp_path,
        executable="/usr/local/bin/tracesage",
        port=7842,
        metadata_only=False,
    )
    assert code == 0
    assert "started" in message
    assert calls[-1][:3] == ["launchctl", "bootstrap", "gui/501"]
    code, message = stop_service(tmp_path)
    assert code == 0
    assert calls[-1][:2] == ["launchctl", "bootout"]
