"""Tools for the data analyst workers."""
from __future__ import annotations

from langchain_core.tools import tool


@tool
def fetch_schema(table: str) -> str:
    """Return the schema definition for a database table."""
    return (
        f"Table {table}: id INT PK, user_id INT, value DECIMAL(10,2), "
        f"created_at TIMESTAMP, region TEXT"
    )


@tool
def run_sql(query: str) -> str:
    """Execute a SQL query against the warehouse."""
    return f"42 rows returned for: {query[:60]}..."


@tool
def plot_chart(data: str, chart_type: str = "line") -> str:
    """Render a chart of the given data."""
    return f"{chart_type} chart rendered ({len(data)} chars of data) -> /tmp/chart.png"


@tool
def write_summary(content: str) -> str:
    """Produce a one-paragraph executive summary."""
    return f"Summary: {content[:100]}..."
