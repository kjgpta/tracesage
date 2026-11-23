"""Plain ``openai`` client calls traced by tracesage (needs OPENAI_API_KEY).

No agent framework at all: ``instrument=["openai"]`` wraps the OpenAI Python client so
each ``chat.completions.create`` becomes a ``chat {model}`` step with tokens, prompt and
completion. Wrapping the calls in a manual span (``tl.span``) groups them into one run.

    pip install "tracesage[openai]" openai
    export OPENAI_API_KEY=...
    python examples/frameworks/openai_client.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import tracesage
from tracesage import TraceSageConfig

DATA_DIR = Path.home() / ".tracesage" / "openai-client-demo"


def main() -> None:
    cfg = TraceSageConfig(data_dir=DATA_DIR, project_name="openai-client-demo")
    tl = tracesage.start(cfg, instrument=["openai"])

    from openai import OpenAI

    client = OpenAI()

    try:
        # Without the manual span each chat call would be its own run. With it,
        # both calls nest under one "haiku pipeline" run.
        with tl.span("haiku pipeline", kind="agent", inputs={"topic": "autumn"}) as root:
            draft = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": "Write a haiku about autumn."}],
            )
            text = draft.choices[0].message.content
            critique = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": f"Improve this haiku:\n{text}"}],
            )
            final = critique.choices[0].message.content
            root.set_output(final)
        print(final)
        print(f"\nOpen {tl.ui_url}: one run with two chat gpt-4o-mini steps and token counts.")
        print("Ctrl+C to stop.")
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        tl.stop()


if __name__ == "__main__":
    main()
