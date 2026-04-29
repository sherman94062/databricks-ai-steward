"""Tests for MCP Prompts.

Prompts are user-initiated templates. Distinct from tool tests, the
assertions here are about:
  - the prompt is registered with the FastMCP instance
  - argument schema is what the client UI will see
  - the rendered template references the right tools (renaming a
    tool would silently break the prompt; keep that contract tested)
"""

from __future__ import annotations

import pytest

from mcp_server import prompts
from mcp_server.app import mcp


@pytest.mark.asyncio
async def test_billing_report_prompt_is_registered():
    """The prompt appears in the server's prompt list with the
    declared name + description visible to the client."""
    listed = await mcp.list_prompts()
    names = {p.name for p in listed}
    assert "billing_report" in names

    p = next(p for p in listed if p.name == "billing_report")
    assert "leadership" in (p.description or "").lower()
    # The argument schema is what the client UI shows the user.
    arg_names = {a.name for a in p.arguments}
    assert arg_names == {"weeks_back"}


@pytest.mark.asyncio
async def test_billing_report_default_window_is_one_week():
    rendered = prompts.billing_report()
    assert "since_days=7" in rendered
    # Prior comparable period: 2 weeks back so the LLM can isolate
    # the prior 1 week by subtraction.
    assert "since_days=14" in rendered


@pytest.mark.asyncio
async def test_billing_report_custom_window_propagates():
    rendered = prompts.billing_report(weeks_back=4)
    assert "since_days=28" in rendered
    assert "since_days=56" in rendered  # prior comparable period


def test_billing_report_references_billing_summary_tool():
    """The prompt orchestrates by name. If `billing_summary` is ever
    renamed, this test breaks before we ship a silently-broken
    prompt."""
    rendered = prompts.billing_report(weeks_back=1)
    assert "billing_summary" in rendered


def test_billing_report_template_avoids_jargon_in_output_format():
    """The whole point of the prompt is translation — output format
    must instruct plain-English labels for common SKUs."""
    rendered = prompts.billing_report(weeks_back=1)
    assert "Interactive SQL queries" in rendered
    assert "Data storage" in rendered
    assert "Background table optimization" in rendered
