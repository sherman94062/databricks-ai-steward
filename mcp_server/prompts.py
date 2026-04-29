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
    days = weeks_back * 7
    prior_window_days = weeks_back * 14
    return f"""You are generating a Databricks spend report for non-technical
leadership (CFO, CTO, business stakeholders). They do not speak DBU,
DSU, SKU, or "billing_origin_product." Translate everything to
dollars and plain English.

Steps:

1. Call `billing_summary` with `since_days={days}` to get the
   current-period spend. If the response includes `total_usd` and
   `cost_usd` per row, use those directly. If those fields are
   missing, the operator hasn't configured `MCP_DBU_RATE_CARD` —
   say so explicitly in the report rather than guessing at prices.

2. Call `billing_summary` again with `since_days={prior_window_days}`
   to get a window that includes the prior comparable period.
   Subtract the current-period totals from the wider window to
   isolate the prior {weeks_back} week(s).

3. Compute the projected monthly run rate by extrapolating the
   current-period total to 30 days.

4. Format the report exactly as:

   **Databricks spend — last {weeks_back} week(s)**

   - **Total spend**: $X.XX
   - **vs prior {weeks_back} week(s)**: +$X.XX (+X%) or -$X.XX (-X%)
   - **Projected monthly run rate**: $X.XX

   **Top cost drivers**:
   - <plain-language label>: $X.XX (X% of total)
   - <plain-language label>: $X.XX (X% of total)
   - <plain-language label>: $X.XX (X% of total)

   Use these plain-language labels for common SKUs:
   - SKU containing `SQL_COMPUTE` → "Interactive SQL queries"
   - SKU containing `JOBS_*_COMPUTE` → "Scheduled jobs"
   - SKU containing `STORAGE` → "Data storage"
   - SKU containing `PREDICTIVE_OPTIMIZATION` → "Background table optimization"
   - For anything else, derive a reasonable plain-English name and
     flag it in a footnote.

   **Notes**:
   - Any caveats: rate-card SKUs not configured, partial data, etc.

5. Keep the report under 200 words. No SKU codes in the body —
   they belong only in a footnote if a row's pricing was unknown.
"""
