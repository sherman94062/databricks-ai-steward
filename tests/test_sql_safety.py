"""Static SQL safety classifier tests.

Covers each governance verdict path. Pure-CPU; no Databricks calls.
"""

from __future__ import annotations

import pytest

from mcp_server.databricks.sql_safety import classify


@pytest.mark.parametrize(
    "sql, kind",
    [
        ("SELECT 1", "SELECT"),
        ("SELECT * FROM samples.nyctaxi.trips LIMIT 10", "SELECT"),
        ("WITH x AS (SELECT 1) SELECT * FROM x", "SELECT"),
        ("SELECT a FROM t UNION ALL SELECT b FROM s", "SELECT"),
        ("EXPLAIN SELECT * FROM t", "EXPLAIN"),
        ("SHOW TABLES IN samples.nyctaxi", "SHOW"),
        ("DESCRIBE samples.nyctaxi.trips", "DESCRIBE"),
    ],
)
def test_allowed(sql, kind):
    v = classify(sql)
    assert v.allowed, f"expected ALLOW for {sql!r}, got {v}"
    assert v.kind == kind


@pytest.mark.parametrize(
    "sql, expected_kind",
    [
        ("INSERT INTO t VALUES (1)", "INSERT"),
        ("UPDATE t SET x=1", "UPDATE"),
        ("DELETE FROM t", "DELETE"),
        ("DROP TABLE t", "DROP"),
        ("CREATE TABLE t (x INT)", "CREATE"),
        ("ALTER TABLE t ADD COLUMN y INT", "ALTER"),
        ("MERGE INTO t USING s ON t.id = s.id "
         "WHEN MATCHED THEN UPDATE SET t.x = s.x", "MERGE"),
        ("TRUNCATE TABLE t", "TRUNCATETABLE"),
    ],
)
def test_dml_ddl_rejected(sql, expected_kind):
    v = classify(sql)
    assert not v.allowed, f"expected REJECT for {sql!r}, got {v}"
    assert v.kind == expected_kind


@pytest.mark.parametrize(
    "sql",
    [
        "USE catalog main",
        "GRANT SELECT ON t TO `user@x.com`",
        "REVOKE SELECT ON t FROM `user@x.com`",
        "SET spark.sql.shuffle.partitions = 50",
        "COMMENT ON TABLE t IS 'hi'",
    ],
)
def test_session_and_grant_rejected(sql):
    v = classify(sql)
    assert not v.allowed, f"expected REJECT for {sql!r}, got {v}"


def test_multi_statement_rejected():
    v = classify("SELECT 1; SELECT 2")
    assert not v.allowed
    assert v.kind == "MULTI_STATEMENT"


def test_cte_with_dml_rejected():
    v = classify("WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x")
    assert not v.allowed
    # The DML node is what we report — INSERT here.
    assert v.kind == "INSERT"


def test_empty_rejected():
    assert not classify("").allowed
    assert not classify("   ").allowed


def test_unparsable_rejected():
    v = classify("not really SQL at all !!@#$")
    assert not v.allowed
