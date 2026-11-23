"""CrewAI crew traced by tracesage (needs OPENAI_API_KEY).

``instrument=["crewai"]`` enables the OpenInference CrewAI instrumentation
(Crew.kickoff → Task → Agent → Tool → LLM spans) and sets
``CREWAI_DISABLE_TELEMETRY=true`` so CrewAI's own usage telemetry stays out of the trace.

    pip install "tracesage[crewai]" crewai
    export OPENAI_API_KEY=...
    python examples/frameworks/crewai_crew.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import tracesage
from tracesage import TraceSageConfig

DATA_DIR = Path.home() / ".tracesage" / "crewai-demo"


def main() -> None:
    cfg = TraceSageConfig(data_dir=DATA_DIR, project_name="crewai-demo")
    # instrument() must run BEFORE crewai is imported.
    tl = tracesage.start(cfg, instrument=["crewai"])

    from crewai import Agent, Crew, Task
    from crewai.tools import tool

    @tool("word_count")
    def word_count(text: str) -> int:
        """Count the words in a piece of text."""
        return len(text.split())

    researcher = Agent(
        role="Researcher",
        goal="Collect three facts about {topic}",
        backstory="Terse and accurate.",
        tools=[word_count],
    )
    writer = Agent(role="Writer", goal="Write a two-sentence summary", backstory="Plain prose.")

    research = Task(
        description="List three facts about {topic}.", expected_output="3 bullets", agent=researcher
    )
    summary = Task(
        description="Summarise the facts in two sentences.",
        expected_output="2 sentences",
        agent=writer,
        context=[research],
    )
    crew = Crew(agents=[researcher, writer], tasks=[research, summary])

    try:
        result = crew.kickoff(inputs={"topic": "solar sails"})
        print("  ->", result)
        print(f"\nOpen {tl.ui_url}: Crew.kickoff → Task → Agent (Researcher/Writer) → tool/LLM")
        print("Ctrl+C to stop.")
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        tl.stop()


if __name__ == "__main__":
    main()
