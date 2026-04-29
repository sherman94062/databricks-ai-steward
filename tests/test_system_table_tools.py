"""Tests for the system-table convenience tools.

Each tool is a thin wrapper over execute_sql_safe — we patch it to verify
that:
  * args are validated and clamped
  * the constructed SQL contains the expected predicates and limits
  * the response is reshaped from {columns, rows} into named dicts
  * passthrough errors from execute_sql_safe are not modified
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from mcp_server.tools import sql_tools


@pytest.fixture
def mock_execute_sql_safe(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(sql_tools, "execute_sql_safe", mock)
    return mock


def _ok_payload(rows, cols):
    return {
        "statement_id": "stmt-x",
        "warehouse_id": "wh-x",
        "columns": [{"name": n, "type": "STRING"} for n in cols],
        "rows": rows,
        "row_count": len(rows),
        "row_limit_applied": 100,
        "truncated": False,
    }


@pytest.mark.asyncio
async def test_list_system_tables_reshapes_rows(mock_execute_sql_safe):
    mock_execute_sql_safe.return_value = _ok_payload(
        rows=[
            ["access", "audit", "MANAGED", "audit log"],
            ["billing", "usage", "MANAGED", "DBU usage"],
        ],
        cols=["table_schema", "table_name", "table_type", "comment"],
    )
    r = await sql_tools.list_system_tables()
    assert r["row_count"] == 2
    assert r["tables"][0] == {
        "table_schema": "access",
        "table_name": "audit",
        "table_type": "MANAGED",
        "comment": "audit log",
    }


@pytest.mark.asyncio
async def test_recent_audit_events_clamps_args(mock_execute_sql_safe):
    mock_execute_sql_safe.return_value = _ok_payload(rows=[], cols=["event_time"])

    # Caller asks for 999 hours / 9999 rows — both well past the cap
    await sql_tools.recent_audit_events(since_hours=999, limit=9999)

    sql_arg = mock_execute_sql_safe.call_args.args[0]
    # 168h is the documented ceiling; 200 rows is the documented ceiling
    assert "INTERVAL 168 HOURS" in sql_arg
    assert "LIMIT 200" in sql_arg
    # And the SDK row_limit kwarg should match, not the user's 9999
    assert mock_execute_sql_safe.call_args.kwargs["row_limit"] == 200


@pytest.mark.asyncio
async def test_recent_audit_events_passes_long_wait_timeout(mock_execute_sql_safe):
    """audit table is known-slow on small warehouses; the tool should
    request the maximum wait (50 s, Databricks ceiling)."""
    mock_execute_sql_safe.return_value = _ok_payload(rows=[], cols=["event_time"])
    await sql_tools.recent_audit_events()
    assert mock_execute_sql_safe.call_args.kwargs.get("wait_timeout_s") == 50


@pytest.mark.asyncio
async def test_recent_query_history_returns_named_dicts(mock_execute_sql_safe):
    mock_execute_sql_safe.return_value = _ok_payload(
        rows=[
            ["2026-04-27T10:00:00Z", "alice@x.com", "FINISHED", "SELECT", "245", "55", "SELECT 1", None],
        ],
        cols=[
            "start_time", "executed_by", "execution_status", "statement_type",
            "total_duration_ms", "produced_rows", "statement_text", "error_message",
        ],
    )
    r = await sql_tools.recent_query_history(since_hours=2, limit=10)
    assert r["since_hours"] == 2
    assert r["row_count"] == 1
    assert r["queries"][0]["executed_by"] == "alice@x.com"
    assert r["queries"][0]["statement_type"] == "SELECT"


@pytest.mark.asyncio
async def test_billing_summary_clamps_to_90_days(mock_execute_sql_safe):
    mock_execute_sql_safe.return_value = _ok_payload(rows=[], cols=["sku_name"])
    await sql_tools.billing_summary(since_days=10000)
    sql_arg = mock_execute_sql_safe.call_args.args[0]
    assert "INTERVAL 90 DAYS" in sql_arg


@pytest.mark.asyncio
async def test_billing_summary_no_rate_card_returns_dbu_only(mock_execute_sql_safe, monkeypatch):
    """Default behavior — no MCP_DBU_RATE_CARD set. Response must not
    include cost_usd / total_usd. Preserves the contract for callers
    that have always parsed the DBU-only shape."""
    monkeypatch.delenv("MCP_DBU_RATE_CARD", raising=False)
    mock_execute_sql_safe.return_value = _ok_payload(
        rows=[["SQL_COMPUTE", "SQL", "100.0", "5", "DBU"]],
        cols=["sku_name", "billing_origin_product", "total_units", "record_count", "unit"],
    )
    r = await sql_tools.billing_summary(since_days=7)
    assert "cost_usd" not in r["summary"][0]
    assert "total_usd" not in r
    assert "rate_card_applied" not in r


@pytest.mark.asyncio
async def test_billing_summary_with_rate_card_adds_dollars(mock_execute_sql_safe, monkeypatch):
    """When MCP_DBU_RATE_CARD is set with an exact SKU match, each
    matching row gets cost_usd and the response carries total_usd."""
    monkeypatch.setenv("MCP_DBU_RATE_CARD", '{"PREMIUM_SQL": 0.55}')
    mock_execute_sql_safe.return_value = _ok_payload(
        rows=[["PREMIUM_SQL", "SQL", "100.0", "5", "DBU"]],
        cols=["sku_name", "billing_origin_product", "total_units", "record_count", "unit"],
    )
    r = await sql_tools.billing_summary(since_days=7)
    assert r["rate_card_applied"] is True
    assert r["total_usd"] == 55.0
    assert r["summary"][0]["cost_usd"] == 55.0


@pytest.mark.asyncio
async def test_billing_summary_rate_card_wildcard_fallback(mock_execute_sql_safe, monkeypatch):
    """`*` wildcard prices any SKU not explicitly listed."""
    monkeypatch.setenv("MCP_DBU_RATE_CARD", '{"PREMIUM_SQL": 0.55, "*": 0.10}')
    mock_execute_sql_safe.return_value = _ok_payload(
        rows=[
            ["PREMIUM_SQL", "SQL", "100.0", "5", "DBU"],
            ["UNKNOWN_SKU", "X", "200.0", "5", "DBU"],
        ],
        cols=["sku_name", "billing_origin_product", "total_units", "record_count", "unit"],
    )
    r = await sql_tools.billing_summary(since_days=7)
    assert r["summary"][0]["cost_usd"] == 55.0     # exact match
    assert r["summary"][1]["cost_usd"] == 20.0     # 200 * 0.10 via *
    assert r["total_usd"] == 75.0


@pytest.mark.asyncio
async def test_billing_summary_rate_card_unmatched_sku_is_null(
    mock_execute_sql_safe, monkeypatch,
):
    """A SKU with no exact match and no `*` fallback gets cost_usd=null
    (so the caller can see *which* row's pricing wasn't known) and is
    excluded from total_usd."""
    monkeypatch.setenv("MCP_DBU_RATE_CARD", '{"PREMIUM_SQL": 0.55}')
    mock_execute_sql_safe.return_value = _ok_payload(
        rows=[
            ["PREMIUM_SQL", "SQL", "100.0", "5", "DBU"],
            ["MYSTERY_SKU", "X", "999.0", "5", "DBU"],
        ],
        cols=["sku_name", "billing_origin_product", "total_units", "record_count", "unit"],
    )
    r = await sql_tools.billing_summary(since_days=7)
    assert r["summary"][0]["cost_usd"] == 55.0
    assert r["summary"][1]["cost_usd"] is None
    assert r["total_usd"] == 55.0   # mystery row excluded


@pytest.mark.asyncio
async def test_billing_summary_invalid_rate_card_falls_back_silently(
    mock_execute_sql_safe, monkeypatch,
):
    """A malformed rate card must NOT crash the tool. Behavior matches
    'no rate card set' — DBU-only output. Safer than failing closed
    when the env var is operator-misconfigured."""
    monkeypatch.setenv("MCP_DBU_RATE_CARD", "{not-valid-json")
    mock_execute_sql_safe.return_value = _ok_payload(
        rows=[["PREMIUM_SQL", "SQL", "100.0", "5", "DBU"]],
        cols=["sku_name", "billing_origin_product", "total_units", "record_count", "unit"],
    )
    r = await sql_tools.billing_summary(since_days=7)
    assert "rate_card_applied" not in r
    assert "cost_usd" not in r["summary"][0]


@pytest.mark.asyncio
async def test_billing_summary_rate_card_handles_string_total_units(
    mock_execute_sql_safe, monkeypatch,
):
    """Databricks' Decimal columns serialize as JSON strings (real
    payload shape: '247.221246666666666675'). Dollar-cost math must
    coerce them, not blow up on str * float."""
    monkeypatch.setenv("MCP_DBU_RATE_CARD", '{"PREMIUM_SQL": 0.5}')
    mock_execute_sql_safe.return_value = _ok_payload(
        rows=[["PREMIUM_SQL", "SQL", "247.221246666666666675", "115", "DBU"]],
        cols=["sku_name", "billing_origin_product", "total_units", "record_count", "unit"],
    )
    r = await sql_tools.billing_summary(since_days=7)
    assert r["summary"][0]["cost_usd"] == 123.61   # 247.22... * 0.5 → 123.61 rounded


@pytest.mark.asyncio
async def test_system_tools_passthrough_error(mock_execute_sql_safe):
    """If execute_sql_safe returns an error dict, the wrapper passes it
    through unchanged — does not paper over it."""
    mock_execute_sql_safe.return_value = {
        "error": {"type": "PermissionDenied", "message": "no SELECT on system.access.audit"}
    }
    r = await sql_tools.recent_audit_events()
    assert r == {
        "error": {"type": "PermissionDenied", "message": "no SELECT on system.access.audit"}
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fn, args",
    [
        (sql_tools.recent_audit_events, {"since_hours": "not-an-int"}),
        (sql_tools.recent_query_history, {"limit": "abc"}),
        (sql_tools.billing_summary, {"since_days": "ten"}),
    ],
)
async def test_system_tools_reject_non_int_args(mock_execute_sql_safe, fn, args):
    """Non-int args get caught by _coerce_int; the guard turns the
    ValueError into a structured error before any SQL is constructed."""
    r = await fn(**args)
    assert "error" in r
    # _coerce_int raises ValueError; _guard wraps as ValueError type
    assert r["error"]["type"] == "ValueError"
    mock_execute_sql_safe.assert_not_called()
