"""Optional exporters that forward tracesage's event stream to external systems."""
from __future__ import annotations

from tracesage.exporters.otel import OTelSpanExporter

__all__ = ["OTelSpanExporter"]
