"""Claude Desktop entry point. Desktop installs deps from pyproject.toml via uv."""
from __future__ import annotations

import os
from pathlib import Path

from tracesage.mcp_server import serve


def main() -> None:
    raw = os.environ.get("TRACESAGE_DATA_DIR") or ""
    data_dir = Path(raw).expanduser() if raw else Path.home() / ".tracesage"
    serve(data_dir)


if __name__ == "__main__":
    main()
