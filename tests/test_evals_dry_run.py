"""The eval harness must never send a live mutation: dry_run is forced at the
call_tool boundary and the dry-run echo is verified."""

import pytest
from mcp.types import CallToolResult, TextContent, Tool

from evals.agent import _call, mutating_tools


class FakeSession:
    def __init__(self, result):
        self.result = result
        self.sent = None

    async def call_tool(self, name, args):
        self.sent = (name, args)
        return self.result


def _tool(name, properties):
    return Tool(name=name, input_schema={"type": "object", "properties": properties})


def _echo(payload):
    return CallToolResult(content=[], structured_content=payload, is_error=False)


MUTATING = frozenset({"place_order"})


def test_mutating_set_derived_from_dry_run_property():
    tools = [
        _tool("place_order", {"size": {}, "dry_run": {}}),
        _tool("get_ticker", {"symbol": {}}),
    ]
    assert mutating_tools(tools) == {"place_order"}


async def test_explicit_dry_run_false_is_overridden():
    session = FakeSession(_echo({"dry_run": True, "method": "POST", "path": "/orders"}))
    call = await _call(session, MUTATING, "place_order", {"size": 1, "dry_run": False})
    assert session.sent[1]["dry_run"] is True
    # recorded args are the model's intent, without the harness override
    assert "dry_run" not in call.args
    assert call.args == {"size": 1}


async def test_success_without_dry_run_echo_is_rejected():
    session = FakeSession(_echo({"id": 42, "state": "open"}))
    with pytest.raises(RuntimeError, match="did not honour dry_run"):
        await _call(session, MUTATING, "place_order", {"size": 1})


async def test_error_results_skip_the_echo_check():
    session = FakeSession(
        CallToolResult(
            content=[TextContent(type="text", text="delta api error: insufficient_margin")],
            is_error=True,
        )
    )
    call = await _call(session, MUTATING, "place_order", {"size": 1})
    assert call.is_error
    assert session.sent[1]["dry_run"] is True


async def test_non_mutating_tools_pass_through_untouched():
    session = FakeSession(_echo({"symbol": "BTCUSD"}))
    call = await _call(session, frozenset(), "get_ticker", {"symbol": "BTCUSD"})
    assert session.sent == ("get_ticker", {"symbol": "BTCUSD"})
    assert call.args == {"symbol": "BTCUSD"}
