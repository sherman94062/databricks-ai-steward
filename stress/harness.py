"""Fault-injection harness for stress.server.

Each scenario spawns a fresh MCP server subprocess so a crash in one doesn't
affect others. Records for each scenario:
  - whether the tool call completed, errored, or timed out
  - whether the subprocess survived
  - a short description of the observed behavior

Run:   python -m stress.harness
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@dataclass
class Scenario:
    name: str
    tool: str
    expected: str
    timeout_s: float = 5.0
    tool_args: dict = field(default_factory=dict)


SCENARIOS: list[Scenario] = [
    Scenario(
        "baseline",
        "ok_guarded",
        expected="success",
    ),
    Scenario(
        "raises (unguarded)",
        "raises_unguarded",
        expected="MCP error response (FastMCP's default handling)",
    ),
    Scenario(
        "raises (guarded)",
        "raises_guarded",
        expected="structured {'error': {...}} return from @_guard",
    ),
    Scenario(
        "oversize return (unguarded)",
        "oversize_unguarded",
        expected="1 MB payload delivered; slow but succeeds",
    ),
    Scenario(
        "oversize return (guarded)",
        "oversize_guarded",
        expected="ResponseTooLarge structured error",
    ),
    Scenario(
        "stdout pollution",
        "stdout_pollution_guarded",
        expected="session dies — JSON-RPC stream corrupted by print()",
        timeout_s=3.0,
    ),
    Scenario(
        "hang (no timeout guard)",
        "hangs_forever_guarded",
        expected="client times out; server subprocess still running",
        timeout_s=2.0,
    ),
    Scenario(
        "unserializable return (unguarded)",
        "unserializable_unguarded",
        expected="MCP error from FastMCP or protocol failure",
    ),
    Scenario(
        "unserializable return (guarded)",
        "unserializable_guarded",
        expected="ResponseNotSerializable structured error",
    ),
]


@dataclass
class Outcome:
    scenario: str
    status: str            # "ok", "client_error", "timeout", "session_died", "server_died"
    server_alive: bool
    elapsed_s: float
    detail: str


async def _run(scenario: Scenario) -> Outcome:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "stress.server"],
    )

    t0 = time.monotonic()
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), timeout=5.0)
                try:
                    result = await asyncio.wait_for(
                        session.call_tool(scenario.tool, scenario.tool_args),
                        timeout=scenario.timeout_s,
                    )
                except asyncio.TimeoutError:
                    return Outcome(
                        scenario=scenario.name,
                        status="timeout",
                        server_alive=True,  # subprocess still running; stdio_client will kill it on exit
                        elapsed_s=time.monotonic() - t0,
                        detail=f"no response within {scenario.timeout_s}s",
                    )
                except Exception as e:
                    return Outcome(
                        scenario=scenario.name,
                        status="client_error",
                        server_alive=False,
                        elapsed_s=time.monotonic() - t0,
                        detail=f"{type(e).__name__}: {str(e)[:200]}",
                    )

                detail = _summarize_result(result)
                is_structured_err = "error" in detail and "type" in detail
                return Outcome(
                    scenario=scenario.name,
                    status="ok" if not is_structured_err else "ok_structured_error",
                    server_alive=True,
                    elapsed_s=time.monotonic() - t0,
                    detail=detail,
                )
    except asyncio.TimeoutError:
        return Outcome(
            scenario=scenario.name,
            status="timeout",
            server_alive=False,
            elapsed_s=time.monotonic() - t0,
            detail="session setup timed out",
        )
    except Exception as e:
        return Outcome(
            scenario=scenario.name,
            status="session_died",
            server_alive=False,
            elapsed_s=time.monotonic() - t0,
            detail=f"{type(e).__name__}: {str(e)[:200]}",
        )


def _summarize_result(result: Any) -> str:
    try:
        # CallToolResult has .content (list of TextContent) and .isError
        parts = []
        if getattr(result, "isError", False):
            parts.append("isError=True")
        for item in getattr(result, "content", []) or []:
            text = getattr(item, "text", None)
            if text:
                parts.append(text[:300])
        return " | ".join(parts) if parts else repr(result)[:300]
    except Exception as e:
        return f"<could not summarize: {e}>"


def _render_table(outcomes: list[Outcome]) -> None:
    print()
    print("=" * 100)
    print(f"{'SCENARIO':<36}{'STATUS':<22}{'ELAPSED':<10}DETAIL")
    print("-" * 100)
    for o in outcomes:
        elapsed = f"{o.elapsed_s:.2f}s"
        detail = o.detail[:40] + "..." if len(o.detail) > 40 else o.detail
        print(f"{o.scenario:<36}{o.status:<22}{elapsed:<10}{detail}")
    print("=" * 100)


async def main() -> None:
    outcomes: list[Outcome] = []
    for scenario in SCENARIOS:
        print(f"→ {scenario.name}  (expected: {scenario.expected})", file=sys.stderr)
        outcome = await _run(scenario)
        outcomes.append(outcome)
        print(
            f"  status={outcome.status} elapsed={outcome.elapsed_s:.2f}s detail={outcome.detail[:150]}",
            file=sys.stderr,
        )

    _render_table(outcomes)

    # Dump JSON for doc generation
    print()
    print(json.dumps(
        [o.__dict__ for o in outcomes],
        indent=2,
    ))


if __name__ == "__main__":
    asyncio.run(main())
