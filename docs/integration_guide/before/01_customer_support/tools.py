"""Customer support tools — billing and tech specialist toolboxes.

These are plain LangChain `@tool`-decorated functions. They appear in
tracelens as `tool:<name>` topology nodes once an agent calls them.
"""
from __future__ import annotations

from langchain_core.tools import tool


# --- Billing tools ---

@tool
def lookup_account(account_id: str) -> str:
    """Fetch summary information for a customer account."""
    return (
        f"Account {account_id}: Premium tier, balance $42.50, "
        f"last login 2d ago, open tickets: 0"
    )


@tool
def issue_refund(account_id: str, amount: float, reason: str) -> str:
    """Issue a refund to the given account. Returns confirmation text."""
    return (
        f"Refund of ${amount:.2f} to {account_id} approved "
        f"(reason: {reason}). Ref #RF-91234"
    )


@tool
def check_balance(account_id: str) -> str:
    """Return the current balance for a customer account."""
    return f"Current balance on {account_id}: $42.50, due 2026-06-01"


# --- Tech tools ---

@tool
def run_diagnostic(service: str) -> str:
    """Run an end-to-end diagnostic against a service."""
    return f"Diagnostic on {service}: all checks passed (latency 23ms, error rate 0.0%)"


@tool
def restart_service(service: str) -> str:
    """Restart a service. Use only as a last resort."""
    return f"Service {service} restarted successfully (downtime: 4s)"


@tool
def check_logs(service: str, lines: int = 50) -> str:
    """Return the last N log lines for a service."""
    return f"Last {lines} log lines for {service}: WARN x2, no ERRORs"


BILLING_TOOLS = [lookup_account, issue_refund, check_balance]
TECH_TOOLS = [run_diagnostic, restart_service, check_logs]
