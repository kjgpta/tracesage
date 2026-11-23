"""Plain ``anthropic`` client calls traced by tracesage (needs ANTHROPIC_API_KEY).

``instrument=["anthropic"]`` wraps the Anthropic Python client so each
``messages.create`` becomes a ``chat {model}`` step with tokens, prompt and completion.
An API error (rate limit, bad model name) shows up as a red step with the error text.

    pip install "tracesage[anthropic]" anthropic
    export ANTHROPIC_API_KEY=...
    python examples/frameworks/anthropic_client.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import tracesage
from tracesage import TraceSageConfig

DATA_DIR = Path.home() / ".tracesage" / "anthropic-client-demo"


def main() -> None:
    cfg = TraceSageConfig(data_dir=DATA_DIR, project_name="anthropic-client-demo")
    tl = tracesage.start(cfg, instrument=["anthropic"])

    import anthropic

    client = anthropic.Anthropic()

    try:
        with tl.span("summarise", kind="agent") as root:
            msg = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=200,
                messages=[{"role": "user", "content": "Summarise the water cycle in 3 bullets."}],
            )
            text = "".join(getattr(b, "text", "") for b in msg.content)
            root.set_output(text)
        print(text)
        print(f"\nOpen {tl.ui_url}: a chat claude-sonnet-4-5 step with input/output tokens.")
        print("Ctrl+C to stop.")
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        tl.stop()


if __name__ == "__main__":
    main()
