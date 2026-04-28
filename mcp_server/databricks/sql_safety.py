"""Static SQL safety check — runs before any statement reaches Databricks.

Parses input with sqlglot using the Databricks dialect. The verdict is
either:
  * `Allowed(statement_kind)`         — root + all subqueries are read-only
  * `Rejected(reason, statement_kind)` — caller gets a structured error

What we allow:  SELECT, EXPLAIN, SHOW, DESCRIBE
What we reject:
  * any DML (INSERT, UPDATE, DELETE, MERGE, REPLACE, TRUNCATE)
  * any DDL (CREATE, ALTER, DROP, RENAME, COMMENT, USE, SET, GRANT, REVOKE)
  * any other statement kind we don't recognise (default-deny)
  * multi-statement input — we want exactly one statement
  * CTEs that hide DML inside (WITH ... INSERT/UPDATE/...)

The parser is lenient by design (sqlglot recovers from many minor
syntax issues), so this layer is `defense in depth`, not the only
gate. Databricks itself enforces grants on whatever runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp

ALLOWED_KINDS = frozenset({
    "SELECT",
    "EXPLAIN",
    "SHOW",
    "DESCRIBE",
})

# Nodes that must NEVER appear in the parsed tree, even nested inside a
# CTE. Root-level USE / SET / GRANT / REVOKE / COMMENT etc. are caught
# by `_statement_kind` (their kind isn't in ALLOWED_KINDS); this list is
# the defense-in-depth catch for DML/DDL hidden inside a SELECT-shaped
# wrapper.
_DML_DDL_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.AlterColumn,
    exp.TruncateTable,
)


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    kind: str               # e.g. "SELECT", "INSERT", "UNKNOWN"
    reason: str | None      # populated when allowed=False

    @classmethod
    def allow(cls, kind: str) -> Verdict:
        return cls(allowed=True, kind=kind, reason=None)

    @classmethod
    def reject(cls, kind: str, reason: str) -> Verdict:
        return cls(allowed=False, kind=kind, reason=reason)


def _statement_kind(stmt: Any) -> str:
    """Map a sqlglot top-level expression to one of ALLOWED_KINDS or
    a free-form identifier for the error message."""
    if isinstance(stmt, (exp.Select, exp.Union, exp.Except, exp.Intersect)):
        return "SELECT"
    # WITH ... SELECT parses to an outer node that wraps a Select; sqlglot
    # exposes that via `.find(exp.Select)` returning non-None.
    if stmt.find(exp.Select) is not None and not isinstance(stmt, _DML_DDL_NODES):
        return "SELECT"
    # sqlglot represents EXPLAIN/SHOW/DESCRIBE as Command nodes carrying the
    # original keyword in `.this`. Be tolerant of either Command or
    # specific node types.
    if isinstance(stmt, exp.Command):
        head = (stmt.name or "").upper().split()[0] if stmt.name else ""
        if head in ALLOWED_KINDS:
            return head
        return head or "UNKNOWN"
    if isinstance(stmt, exp.Describe):
        return "DESCRIBE"
    if isinstance(stmt, exp.Show):
        return "SHOW"
    # Things we explicitly reject: classify so the error is helpful.
    return type(stmt).__name__.upper()


def _walk_for_forbidden(stmt: Any) -> Any:
    """Search the parsed tree for any DML/DDL node nested at any depth.
    Returns the first offending node or None."""
    for node in stmt.walk():
        if isinstance(node, _DML_DDL_NODES):
            return node
    return None


def classify(sql: str) -> Verdict:
    """Parse `sql` and return a Verdict.

    Defense-in-depth gate. Reject = never sent to Databricks.
    """
    if not isinstance(sql, str) or not sql.strip():
        return Verdict.reject("EMPTY", "empty SQL statement")

    try:
        parsed = sqlglot.parse(sql, read="databricks")
    except sqlglot.errors.ParseError as e:
        return Verdict.reject("PARSE_ERROR", f"could not parse SQL: {e!s}"[:200])

    statements = [s for s in parsed if s is not None]
    if not statements:
        return Verdict.reject("EMPTY", "no executable statement found")
    if len(statements) > 1:
        return Verdict.reject(
            "MULTI_STATEMENT",
            f"multi-statement input not allowed; got {len(statements)} statements "
            f"(separate them into individual calls)",
        )

    stmt = statements[0]
    kind = _statement_kind(stmt)

    # Defense-in-depth: even if the root looks like SELECT, walk the tree
    # for DML/DDL hidden inside (e.g. WITH x AS (INSERT ...)).
    nested = _walk_for_forbidden(stmt)
    if nested is not None:
        return Verdict.reject(
            kind,
            f"statement contains a forbidden nested operation: {type(nested).__name__}",
        )

    if kind in ALLOWED_KINDS:
        return Verdict.allow(kind)

    return Verdict.reject(
        kind,
        f"only SELECT / EXPLAIN / SHOW / DESCRIBE are allowed; got {kind}",
    )
