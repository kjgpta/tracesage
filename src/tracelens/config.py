"""Configuration via env vars (TRACELENS_*) and optional TOML file.

Production safety: refuses to start if host=0.0.0.0 without auth_token.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TraceLensConfig(BaseSettings):
    """All configuration. Override via TRACELENS_* env vars or constructor kwargs."""

    model_config = SettingsConfigDict(
        env_prefix="TRACELENS_",
        env_file=None,
        extra="ignore",
    )

    # --- Server ---
    host: str = "127.0.0.1"
    port: int = 7842
    auth_token: str | None = None

    # --- Storage ---
    data_dir: Path = Field(default_factory=lambda: Path.home() / ".tracelens")
    db_filename: str = "traces.db"
    blob_subdir: str = "blobs"
    db_pool_size: int = 5

    # --- Queue + worker ---
    queue_maxsize: int = 50_000
    worker_batch_size: int = 50
    worker_batch_timeout: float = 0.1  # seconds

    # --- Production knobs ---
    sample_rate: float = 1.0  # 0.0..1.0; 1.0 = capture everything
    per_run_event_cap: int = 50_000  # circuit breaker per run
    summary_max_chars: int = 500

    # --- Retention ---
    max_runs: int = 10_000
    max_blob_size_gb: float = 10.0

    # --- Logging ---
    log_level: str = "WARNING"

    # --- Server startup health check ---
    startup_health_timeout_s: float = 3.0

    @property
    def db_path(self) -> Path:
        return self.data_dir / self.db_filename

    @property
    def blob_dir(self) -> Path:
        return self.data_dir / self.blob_subdir

    @model_validator(mode="after")
    def _validate_production_safety(self) -> TraceLensConfig:
        # Hard fail-stop: must have an auth token when binding to non-loopback.
        if self.host not in {"127.0.0.1", "localhost", "::1"} and not self.auth_token:
            raise ValueError(
                f"TRACELENS_AUTH_TOKEN must be set when binding to {self.host!r}. "
                "Either set the env var, pass auth_token to TraceLensConfig, or bind to 127.0.0.1."
            )
        if not 0.0 <= self.sample_rate <= 1.0:
            raise ValueError(f"sample_rate must be in [0.0, 1.0], got {self.sample_rate}")
        if self.queue_maxsize <= 0:
            raise ValueError(f"queue_maxsize must be > 0, got {self.queue_maxsize}")
        if self.worker_batch_size <= 0:
            raise ValueError(f"worker_batch_size must be > 0, got {self.worker_batch_size}")
        if self.per_run_event_cap <= 0:
            raise ValueError(f"per_run_event_cap must be > 0, got {self.per_run_event_cap}")
        return self

    def ensure_data_dirs(self) -> None:
        """Create data_dir and blob_dir if they don't exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.blob_dir.mkdir(parents=True, exist_ok=True)
