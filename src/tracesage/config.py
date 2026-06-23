"""Configuration via env vars (TRACESAGE_*) and optional TOML file.

Production safety: refuses to start if host=0.0.0.0 without auth_token.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TraceSageConfig(BaseSettings):
    """All configuration. Override via TRACESAGE_* env vars or constructor kwargs."""

    model_config = SettingsConfigDict(
        env_prefix="TRACESAGE_",
        env_file=None,
        extra="ignore",
    )

    # --- Kill switch ---
    # Set TRACESAGE_ENABLED=false (or enabled=False) to make tracesage a complete
    # no-op: no embedded server, no DB/worker/queue, a no-op callback handler, and
    # near-zero overhead. This lets you wire tracesage into your code ONCE and turn
    # it off per-environment (e.g. disable in prod) without touching the integration.
    enabled: bool = True

    # --- Server ---
    host: str = "127.0.0.1"
    port: int = 7842
    # If the chosen port is busy, automatically bind the next free port (scanning
    # upward from `port`, then an OS-ephemeral port as a last resort) so multiple
    # apps can run at once without a port clash. Set False to use exactly `port`.
    port_auto: bool = True
    auth_token: str | None = None
    # Optional human-friendly label for THIS application, shown in the UI header (and
    # browser tab) so you can tell apart UIs when tracing several apps at once. Env:
    # TRACESAGE_PROJECT_NAME. Unset -> nothing shown.
    project_name: str | None = None
    # If set, used verbatim as the base for run-trace deep links (e.g. behind a
    # reverse proxy: "https://traces.example.com"). Otherwise derived from host/port.
    public_url: str | None = None
    # Print a clickable "view trace" link to stderr the first time each root run is
    # seen (dev convenience). Only prints when a UI URL is known (embedded server
    # running or public_url set). Disable in noisy/production setups.
    print_run_url: bool = True
    # Start the embedded uvicorn UI server inside the traced process. Set False (or
    # TRACESAGE_START_SERVER=false) in production to keep capturing traces to the data
    # dir without running a web server in your app process — view them later with
    # `tracesage serve`. A `start_server=` kwarg to create()/session()/start() overrides this.
    start_server: bool = True
    # Allowed CORS origins. Defaults to "*" (the bundled UI is same-origin and does
    # not need it); tighten to an explicit allowlist when exposing the server beyond
    # localhost so other sites cannot drive the API from a user's browser.
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])

    # --- Storage ---
    data_dir: Path = Field(default_factory=lambda: Path.home() / ".tracesage")
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

    # --- Redaction ---
    redact_patterns: list[str] = Field(default_factory=list)
    redact_replacement: str = "[REDACTED]"

    # --- OpenTelemetry export (optional) ---
    # When otlp_endpoint is set, every event is ALSO exported as an OTel span to that
    # OTLP/HTTP endpoint (e.g. "http://localhost:4318" — "/v1/traces" is appended if
    # absent), in addition to tracesage's own SQLite store. Best-effort: requires the
    # `tracesage[otel]` extra and never breaks the app if the collector is down.
    otlp_endpoint: str | None = None
    otlp_service_name: str = "tracesage"
    otlp_headers: dict[str, str] = Field(default_factory=dict)

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
    def _validate_production_safety(self) -> TraceSageConfig:
        # Hard fail-stop: must have an auth token when binding to non-loopback.
        # Skipped when disabled — nothing binds, so the bind-safety rule is moot.
        if self.enabled and self.host not in {"127.0.0.1", "localhost", "::1"} and not self.auth_token:
            raise ValueError(
                f"TRACESAGE_AUTH_TOKEN must be set when binding to {self.host!r}. "
                "Either set the env var, pass auth_token to TraceSageConfig, or bind to 127.0.0.1."
            )
        if not 0.0 <= self.sample_rate <= 1.0:
            raise ValueError(f"sample_rate must be in [0.0, 1.0], got {self.sample_rate}")
        if self.queue_maxsize <= 0:
            raise ValueError(f"queue_maxsize must be > 0, got {self.queue_maxsize}")
        if self.worker_batch_size <= 0:
            raise ValueError(f"worker_batch_size must be > 0, got {self.worker_batch_size}")
        if self.per_run_event_cap <= 0:
            raise ValueError(f"per_run_event_cap must be > 0, got {self.per_run_event_cap}")
        # Remaining numeric caps must be positive (documented contract; an invalid
        # value such as db_pool_size=0 would otherwise deadlock the connection pool).
        # port 0 is valid: it asks the OS for an ephemeral port (used by tests and
        # `serve --port 0`); reject only negative or out-of-range ports.
        if not 0 <= self.port <= 65535:
            raise ValueError(f"port must be in [0, 65535], got {self.port}")
        if self.db_pool_size <= 0:
            raise ValueError(f"db_pool_size must be > 0, got {self.db_pool_size}")
        if self.summary_max_chars <= 0:
            raise ValueError(f"summary_max_chars must be > 0, got {self.summary_max_chars}")
        if self.max_runs <= 0:
            raise ValueError(f"max_runs must be > 0, got {self.max_runs}")
        if self.max_blob_size_gb <= 0:
            raise ValueError(f"max_blob_size_gb must be > 0, got {self.max_blob_size_gb}")
        if self.worker_batch_timeout <= 0:
            raise ValueError(
                f"worker_batch_timeout must be > 0, got {self.worker_batch_timeout}"
            )
        if self.startup_health_timeout_s <= 0:
            raise ValueError(
                f"startup_health_timeout_s must be > 0, got {self.startup_health_timeout_s}"
            )
        return self

    def ensure_data_dirs(self) -> None:
        """Create data_dir and blob_dir if they don't exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.blob_dir.mkdir(parents=True, exist_ok=True)
