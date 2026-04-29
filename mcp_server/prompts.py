"""MCP Prompts — pre-defined prompt templates the user can invoke.

Distinct from Tools (which the LLM calls) and Resources (which the
client fetches by URI). Prompts are user-initiated: the user picks
one from the client UI, fills in the typed arguments, and the
returned text becomes the prompt the LLM sees.

Use prompts to bundle a multi-step tool-driven recipe behind a
single human-friendly entry point. The LLM still does the work via
tool calls — the prompt just says *what to do and how to format it*.

If a tool name referenced inside a prompt template ever changes,
the prompt silently breaks (the LLM will look for the renamed tool
and fail). Keep prompt → tool references reviewed when renaming.
"""

from __future__ import annotations

from mcp_server.app import mcp


@mcp.prompt(
    title="Databricks billing report (stakeholder-friendly)",
    description=(
        "Generate a non-technical Databricks spend report for the last "
        "N weeks. Translates DBU/DSU/SKU jargon to dollars and plain "
        "English. Use when leadership asks 'what did we spend on "
        "Databricks?'"
    ),
)
def billing_report(weeks_back: int = 1) -> str:
    """Stakeholder-friendly billing report covering the last `weeks_back`
    weeks. The LLM is instructed to call `billing_summary` (twice — once
    for the requested window, once for the prior comparable period) and
    format the result for a non-technical audience.

    Args:
        weeks_back: Number of weeks to cover. Defaults to 1.
    """
    return f"""You are generating a Databricks spend report for non-technical
leadership (CFO, CTO, business stakeholders). They do not speak DBU,
DSU, SKU, or "billing_origin_product." Translate everything to
dollars and plain English.

Steps:

1. Call the `billing_report` tool with `weeks_back={weeks_back}`. The
   tool pre-computes everything you need:
     - `current_period_total_usd` — total spend for the requested window
     - `prior_period_total_usd` — same-length prior window for comparison
     - `delta_usd`, `delta_percent` — current vs prior
     - `projected_monthly_run_rate_usd` — extrapolated 30-day run rate
     - `summary[]` — per-line breakdown, each with a `friendly_label`
       field carrying the plain-English name (use this, NOT `sku_name`)
   When `rate_card_applied` is `false`, dollar fields are absent and
   a `warning` field explains why. Pass that warning through to the
   report rather than guessing prices.

2. Format the report exactly as:

   **Databricks spend — last {weeks_back} week(s)**

   - **Total spend**: $X.XX
   - **vs prior {weeks_back} week(s)**: +$X.XX (+X%) or -$X.XX (-X%)
   - **Projected monthly run rate**: $X.XX

   **Top cost drivers** (descending by `cost_usd`):
   - {{friendly_label}}: $X.XX (X% of total)
   - {{friendly_label}}: $X.XX (X% of total)
   - {{friendly_label}}: $X.XX (X% of total)

   **Notes**: any caveats — rate-card not configured, partial data, etc.

3. Keep the report under 200 words. No SKU codes in the body — only
   in a footnote if a row's pricing was unknown.
"""
