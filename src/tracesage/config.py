"""Configuration via constructor kwargs and TRACESAGE_* environment variables.

Production safety: refuses to start if host=0.0.0.0 without auth_token.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ModelPrice(BaseModel):
    """Per-million-token rates that override genai-prices for one model."""

    input_per_mtok: float = 0.0
    output_per_mtok: float = 0.0
    cache_read_per_mtok: float = 0.0
    cache_write_per_mtok: float = 0.0


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
    # Extra origins allowed to call the API / open WebSockets from a browser. Empty by
    # default: the bundled UI is same-origin and needs none, and a wildcard would let
    # any website the user visits read traces from an unauthenticated local server.
    # Also gates the WebSocket Origin check (browsers skip CORS for WS handshakes).
    cors_origins: list[str] = Field(default_factory=list)

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

    # --- Cost ---
    # Dollar pricing uses the optional ``tracesage[pricing]`` extra (genai-prices,
    # offline snapshot). When the extra is missing, cost stays null ("n/a").
    pricing_enabled: bool = True
    # Keys are "provider:model" or "model". Env: TRACESAGE_PRICE_OVERRIDES as JSON.
    price_overrides: dict[str, ModelPrice] = Field(default_factory=dict)

    # --- Search ---
    # Index redacted payload text in SQLite FTS5. Summaries are always indexed.
    search_index_payloads: bool = True
    search_max_chars_per_event: int = 20_000

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

    # --- OpenTelemetry ingest (OTLP/HTTP) ---
    # The embedded server accepts spans on `POST /v1/traces` (protobuf or JSON, gzip
    # ok), so any OTel-instrumented process — Pydantic AI, OpenAI Agents SDK, CrewAI,
    # the OpenAI/Anthropic clients — can send traces here by pointing
    # OTEL_EXPORTER_OTLP_ENDPOINT at this server. Same trust boundary as /api/*
    # (bearer token when configured). Set False (TRACESAGE_INGEST=false) to turn
    # the endpoint off.
    ingest: bool = True
    # Largest accepted request body (after decompression), in bytes.
    ingest_max_body_bytes: int = 16 * 1024 * 1024
    # Hook ingest from Claude Code / Codex / Cursor. When true, prompts and tool
    # I/O text are dropped and only names, timing and status are stored.
    hook_metadata_only: bool = False

    # --- Logging ---
    log_level: str = "WARNING"

    # --- Server startup health check ---
    startup_health_timeout_s: float = 3.0

    @field_validator("price_overrides", mode="before")
    @classmethod
    def _parse_price_overrides(cls, value: Any) -> Any:
        if value is None or value == "":
            return {}
        if isinstance(value, str):
            parsed = json.loads(value)
            if not isinstance(parsed, dict):
                raise ValueError("price_overrides must be a JSON object")
            return parsed
        return value

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
        if self.search_max_chars_per_event <= 0:
            raise ValueError(
                f"search_max_chars_per_event must be > 0, got {self.search_max_chars_per_event}"
            )
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
        if self.ingest_max_body_bytes <= 0:
            raise ValueError(
                f"ingest_max_body_bytes must be > 0, got {self.ingest_max_body_bytes}"
            )
        return self

    def ensure_data_dirs(self) -> None:
        """Create data_dir and blob_dir if they don't exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.blob_dir.mkdir(parents=True, exist_ok=True)
