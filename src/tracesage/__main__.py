"""Enable `python -m tracesage` to invoke the CLI (same as the `tracesage` entry point)."""
from __future__ import annotations

from tracesage.cli import app

if __name__ == "__main__":
    app()
