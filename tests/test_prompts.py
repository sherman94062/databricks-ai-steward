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
    assert "weeks_back=1" in rendered


@pytest.mark.asyncio
async def test_billing_report_custom_window_propagates():
    rendered = prompts.billing_report(weeks_back=4)
    assert "weeks_back=4" in rendered


def test_billing_report_references_billing_report_tool():
    """The prompt orchestrates by tool name. If `billing_report` (the
    tool) is ever renamed, this test breaks before we ship a
    silently-broken prompt."""
    rendered = prompts.billing_report(weeks_back=1)
    assert "billing_report" in rendered


def test_billing_report_template_avoids_jargon_in_output_format():
    """Plain-English label translation now lives in the
    `billing_report` tool (`_friendly_sku_label`) — the prompt's job
    is to tell the LLM to *use* the tool's friendly_label field
    instead of the raw sku_name. Verify the contract."""
    rendered = prompts.billing_report(weeks_back=1)
    assert "friendly_label" in rendered
    # Output format should reference friendly_label, not sku_name,
    # as the body label.
    assert "{friendly_label}" in rendered
    # No SKU codes in the body of the report.
    assert "SKU codes in the body" in rendered or "No SKU" in rendered
