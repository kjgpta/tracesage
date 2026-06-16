"""Tests for TraceSageConfig validation (production-safety rail + numeric caps)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from tracesage.config import TraceSageConfig


def test_defaults_are_valid(tmp_path) -> None:
    cfg = TraceSageConfig(data_dir=tmp_path)
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 7842
    assert cfg.db_path == tmp_path / "traces.db"
    assert cfg.blob_dir == tmp_path / "blobs"


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "example.com"])  # noqa: S104
def test_non_loopback_without_token_fails_stop(host: str) -> None:
    """Hard fail-stop: refuse to bind a non-loopback host without an auth token."""
    with pytest.raises(ValidationError, match="TRACESAGE_AUTH_TOKEN"):
        TraceSageConfig(host=host)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_hosts_need_no_token(host: str, tmp_path) -> None:
    cfg = TraceSageConfig(host=host, data_dir=tmp_path)
    assert cfg.host == host


def test_non_loopback_with_token_ok(tmp_path) -> None:
    cfg = TraceSageConfig(host="0.0.0.0", auth_token="secret", data_dir=tmp_path)  # noqa: S104
    assert cfg.auth_token == "secret"


def test_ephemeral_port_zero_is_allowed(tmp_path) -> None:
    # port 0 = OS-assigned ephemeral port (used by tests and `serve --port 0`).
    assert TraceSageConfig(port=0, data_dir=tmp_path).port == 0


@pytest.mark.parametrize(
    ("kwargs", "needle"),
    [
        ({"sample_rate": 1.5}, "sample_rate"),
        ({"sample_rate": -0.1}, "sample_rate"),
        ({"queue_maxsize": 0}, "queue_maxsize"),
        ({"worker_batch_size": 0}, "worker_batch_size"),
        ({"per_run_event_cap": 0}, "per_run_event_cap"),
        ({"port": -1}, "port"),
        ({"port": 70000}, "port"),
        ({"db_pool_size": 0}, "db_pool_size"),
        ({"summary_max_chars": 0}, "summary_max_chars"),
        ({"max_runs": 0}, "max_runs"),
        ({"max_blob_size_gb": 0}, "max_blob_size_gb"),
        ({"worker_batch_timeout": 0}, "worker_batch_timeout"),
        ({"startup_health_timeout_s": -1}, "startup_health_timeout_s"),
    ],
)
def test_invalid_numeric_caps_rejected(kwargs: dict, needle: str, tmp_path) -> None:
    with pytest.raises(ValidationError, match=needle):
        TraceSageConfig(data_dir=tmp_path, **kwargs)


def test_redaction_defaults_off(tmp_path) -> None:
    cfg = TraceSageConfig(data_dir=tmp_path)
    assert cfg.redact_patterns == []
    assert cfg.redact_replacement == "[REDACTED]"
