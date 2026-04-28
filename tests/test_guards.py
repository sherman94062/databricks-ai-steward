"""Tests for the shared tool guards in mcp_server.app.

These exist because the guards are what keeps the stdio server alive under
tool misbehavior — untested guards are worse than no guards.
"""

from __future__ import annotations

import pytest

from mcp_server import app
from mcp_server.app import _cap_response, _guard


def test_normal_return_passes_through():
    @_guard
    def ok():
        return {"hello": "world"}

    assert ok() == {"hello": "world"}


def test_exception_becomes_structured_error():
    @_guard
    def boom():
        raise ValueError("bad input")

    result = boom()
    assert result == {"error": {"type": "ValueError", "message": "bad input"}}


def test_oversized_response_is_rejected(monkeypatch):
    monkeypatch.setattr(app, "MAX_RESPONSE_BYTES", 100)

    big = {"rows": ["x" * 50 for _ in range(10)]}  # ~500+ bytes serialized
    result = _cap_response(big)
    assert result["error"]["type"] == "ResponseTooLarge"


def test_unserializable_response_is_rejected():
    # default=str rescues many "not JSON-native" types (sets, custom classes).
    # The genuinely dangerous case is an object whose own __repr__/__str__
    # raises during encoding — without a broad except, that exception would
    # escape the guard and kill the server.
    class Exploding:
        def __repr__(self):
            raise RuntimeError("cannot repr")

        __str__ = __repr__

    @_guard
    def returns_exploding():
        return {"bad": {Exploding()}}

    result = returns_exploding()
    assert result["error"]["type"] == "ResponseNotSerializable"


@pytest.mark.asyncio
async def test_async_tool_guarded():
    @_guard
    async def ok():
        return {"async": True}

    assert await ok() == {"async": True}

    @_guard
    async def boom():
        raise RuntimeError("async failure")

    result = await boom()
    assert result["error"]["type"] == "RuntimeError"
