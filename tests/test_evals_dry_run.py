"""The eval harness must never send a live mutation: dry_run is forced at the
call_tool boundary and the dry-run echo is verified."""

import pytest
from delta_exchange_mcp.client import DeltaClient
from delta_exchange_mcp.config import INDIA_TESTNET_REST, Config
from delta_exchange_mcp.server import DeltaMCP
from delta_exchange_mcp.tools import trading
from evals.agent import _call, mutating_tools, server_environment
from mcp.client import Client
from mcp.types import CallToolResult, Implementation, TextContent, Tool


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


def test_child_environment_does_not_export_legacy_trading_mode(monkeypatch):
    monkeypatch.setenv("DELTA_MCP_ENV", "india_testnet")
    monkeypatch.setenv("DELTA_MCP_MODE", "trade")

    assert "DELTA_MCP_MODE" not in server_environment()


async def test_modern_discovery_lists_every_trade_tool_without_consent(
    monkeypatch,
):
    app = DeltaMCP()
    delta = DeltaClient(Config(env="india_testnet", base_url=INDIA_TESTNET_REST))
    trading.register(app, delta, gate=trading.TradeGate(armed=False))

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("dry-run evaluation sent a mutation request")

    monkeypatch.setattr(delta, "post", fail_if_called)
    try:
        async with Client(
            app,
            mode="auto",
            client_info=Implementation(name="delta-mcp-evals", version="1"),
        ) as client:
            tools = (await client.list_tools(cache_mode="refresh")).tools
            result = await client.call_tool(
                "place_order",
                {
                    "product_symbol": "BTCUSD",
                    "size": 1,
                    "side": "buy",
                    "order_type": "market_order",
                    "dry_run": True,
                },
            )
            protocol_version = client.protocol_version
    finally:
        await delta.aclose()

    assert protocol_version == "2026-07-28"
    assert mutating_tools(tools) == trading.TOOL_NAMES
    assert result.structured_content["dry_run"] is True


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
            content=[
                TextContent(type="text", text="delta api error: insufficient_margin")
            ],
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
