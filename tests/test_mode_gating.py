"""Account tools register only when both DELTA_API_KEY and DELTA_API_SECRET are set."""

import asyncio

from mcp.server.fastmcp import FastMCP

from delta_exchange_mcp.client import DeltaClient
from delta_exchange_mcp.config import INDIA_TESTNET_REST, Config
from delta_exchange_mcp.server import build_server
from delta_exchange_mcp.tools import account, trading


MARKET_TOOLS = {
    "list_products",
    "get_product",
    "get_ticker",
    "list_tickers",
    "get_orderbook",
    "get_recent_trades",
    "get_candles",
    "get_options_chain",
    "get_reference_data",
}

ACCOUNT_TOOLS = {
    "get_positions",
    "get_margined_positions",
    "get_wallet_balances",
    "get_wallet_transactions",
    "get_fills",
    "get_open_orders",
    "get_order_history",
    "get_order_by_id",
    "get_product_leverage",
    "get_trading_stats",
    "get_trading_preferences",
    "get_profile",
}


def _tool_names(mcp) -> set[str]:
    return {t.name for t in asyncio.run(mcp.list_tools())}


def test_market_only_without_creds(monkeypatch):
    monkeypatch.delenv("DELTA_API_KEY", raising=False)
    monkeypatch.delenv("DELTA_API_SECRET", raising=False)
    names = _tool_names(build_server())
    assert MARKET_TOOLS.issubset(names)
    assert ACCOUNT_TOOLS.isdisjoint(names)


def test_account_tools_register_with_creds(monkeypatch):
    monkeypatch.setenv("DELTA_API_KEY", "k")
    monkeypatch.setenv("DELTA_API_SECRET", "s")
    names = _tool_names(build_server())
    assert MARKET_TOOLS.issubset(names)
    assert ACCOUNT_TOOLS.issubset(names)


def test_partial_creds_skip_account_tools(monkeypatch):
    monkeypatch.setenv("DELTA_API_KEY", "k")
    monkeypatch.delenv("DELTA_API_SECRET", raising=False)
    names = _tool_names(build_server())
    assert MARKET_TOOLS.issubset(names)
    assert ACCOUNT_TOOLS.isdisjoint(names)


def test_account_removal_manifest_matches_the_registered_surface():
    mcp = FastMCP("account-tools")
    client = DeltaClient(
        Config(
            env="india_testnet",
            base_url=INDIA_TESTNET_REST,
            api_key="k",
            api_secret="s",
        )
    )
    account.register(mcp, client)
    assert _tool_names(mcp) == account.TOOL_NAMES


def test_trading_removal_manifest_matches_the_registered_surface():
    mcp = FastMCP("trading-tools")
    client = DeltaClient(
        Config(
            env="india_testnet",
            base_url=INDIA_TESTNET_REST,
            api_key="k",
            api_secret="s",
            mode="trade",
        )
    )
    trading.register(mcp, client)
    assert _tool_names(mcp) == trading.TOOL_NAMES
