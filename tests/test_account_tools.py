"""URL/param contract tests for the 12 authenticated read-only tools.

Each test mocks the upstream Delta endpoint, calls the registered tool through
the MCP server, and asserts the request URL + query string + auth headers.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from mcp.server.fastmcp import FastMCP

from delta_exchange_mcp.client import DeltaClient
from delta_exchange_mcp.config import INDIA_TESTNET_REST, Config
from delta_exchange_mcp.tools import account
from delta_exchange_mcp.tools.account import _safe_export_path


def _client() -> DeltaClient:
    cfg = Config(
        env="india_testnet", base_url=INDIA_TESTNET_REST, api_key="k", api_secret="s"
    )
    return DeltaClient(cfg)


def _build_tools() -> dict[str, Any]:
    mcp = FastMCP("test")
    account.register(mcp, _client())
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


async def _call_tool(tool_name: str, **kwargs: Any) -> Any:
    """Invoke a registered tool through the FastMCP runtime."""
    mcp = FastMCP("test")
    account.register(mcp, _client())
    return await mcp.call_tool(tool_name, kwargs)


def _ok(json_body: Any = None) -> httpx.Response:
    return httpx.Response(200, json=json_body or {"success": True, "result": []})


def test_all_twelve_tools_registered():
    names = set(_build_tools())
    expected = {
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
    assert expected.issubset(names)


@pytest.mark.asyncio
@respx.mock
async def test_get_wallet_balances_hits_balances():
    route = respx.get(f"{INDIA_TESTNET_REST}/wallet/balances").mock(return_value=_ok())
    await _call_tool("get_wallet_balances")
    assert route.called
    assert route.calls[0].request.headers.get("api-key") == "k"


@pytest.mark.asyncio
@respx.mock
async def test_get_wallet_transactions_csv_and_pagination():
    route = respx.get(f"{INDIA_TESTNET_REST}/wallet/transactions").mock(return_value=_ok())
    await _call_tool(
        "get_wallet_transactions",
        asset_ids=[1, 2],
        transaction_types=["funding", "settlement"],
        page_size=20,
        after="cur",
    )
    url = str(route.calls[0].request.url)
    assert "asset_ids=1%2C2" in url
    assert "transaction_types=funding%2Csettlement" in url
    assert "page_size=20" in url
    assert "after=cur" in url


@pytest.mark.asyncio
@respx.mock
async def test_get_positions_requires_one_param():
    with pytest.raises(Exception):
        await _call_tool("get_positions")
    with pytest.raises(Exception):
        await _call_tool("get_positions", product_id=1, underlying_asset_symbol="BTC")


@pytest.mark.asyncio
@respx.mock
async def test_get_positions_with_product_id():
    route = respx.get(f"{INDIA_TESTNET_REST}/positions").mock(return_value=_ok())
    await _call_tool("get_positions", product_id=27)
    assert "product_id=27" in str(route.calls[0].request.url)


@pytest.mark.asyncio
@respx.mock
async def test_get_margined_positions_filters():
    route = respx.get(f"{INDIA_TESTNET_REST}/positions/margined").mock(return_value=_ok())
    await _call_tool(
        "get_margined_positions",
        product_ids=[1, 2, 3],
        contract_types=["perpetual_futures"],
    )
    url = str(route.calls[0].request.url)
    assert "product_ids=1%2C2%2C3" in url
    assert "contract_types=perpetual_futures" in url


@pytest.mark.asyncio
@respx.mock
async def test_get_open_orders_csv():
    route = respx.get(f"{INDIA_TESTNET_REST}/orders").mock(return_value=_ok())
    await _call_tool("get_open_orders", product_ids=[10], states=["open", "pending"])
    url = str(route.calls[0].request.url)
    assert "product_ids=10" in url
    assert "states=open%2Cpending" in url


@pytest.mark.asyncio
@respx.mock
async def test_get_order_history_time_range():
    route = respx.get(f"{INDIA_TESTNET_REST}/orders/history").mock(return_value=_ok())
    await _call_tool(
        "get_order_history",
        start_time_us=1000,
        end_time_us=2000,
        order_types=["limit"],
    )
    url = str(route.calls[0].request.url)
    assert "start_time=1000" in url
    assert "end_time=2000" in url
    assert "order_types=limit" in url


@pytest.mark.asyncio
@respx.mock
async def test_get_order_by_id_uses_order_id_path():
    route = respx.get(f"{INDIA_TESTNET_REST}/orders/12345").mock(return_value=_ok())
    await _call_tool("get_order_by_id", order_id=12345)
    assert route.called


@pytest.mark.asyncio
@respx.mock
async def test_get_order_by_id_uses_client_order_id_path():
    route = respx.get(f"{INDIA_TESTNET_REST}/orders/client_order_id/abc").mock(return_value=_ok())
    await _call_tool("get_order_by_id", client_order_id="abc")
    assert route.called


@pytest.mark.asyncio
@respx.mock
async def test_get_order_by_id_requires_one_param():
    with pytest.raises(Exception):
        await _call_tool("get_order_by_id")


@pytest.mark.asyncio
@respx.mock
async def test_get_fills_pagination():
    route = respx.get(f"{INDIA_TESTNET_REST}/fills").mock(return_value=_ok())
    await _call_tool("get_fills", product_ids=[1], page_size=10, after="cur")
    url = str(route.calls[0].request.url)
    assert "product_ids=1" in url
    assert "page_size=10" in url
    assert "after=cur" in url


@pytest.mark.asyncio
@respx.mock
async def test_get_product_leverage_path_arg():
    route = respx.get(f"{INDIA_TESTNET_REST}/products/27/orders/leverage").mock(return_value=_ok())
    await _call_tool("get_product_leverage", product_id=27)
    assert route.called


@pytest.mark.asyncio
@respx.mock
async def test_get_trading_stats():
    route = respx.get(f"{INDIA_TESTNET_REST}/stats").mock(return_value=_ok())
    await _call_tool("get_trading_stats")
    assert route.called


@pytest.mark.asyncio
@respx.mock
async def test_get_trading_preferences():
    route = respx.get(f"{INDIA_TESTNET_REST}/users/trading_preferences").mock(return_value=_ok())
    await _call_tool("get_trading_preferences")
    assert route.called


@pytest.mark.asyncio
@respx.mock
async def test_get_profile():
    route = respx.get(f"{INDIA_TESTNET_REST}/profile").mock(return_value=_ok())
    await _call_tool("get_profile")
    assert route.called


# --- GH #6: bulk_fills_export ----------------------------------------------


def test_safe_export_path_accepts_cwd_relative(tmp_path: Any, monkeypatch: Any):
    monkeypatch.chdir(tmp_path)
    resolved = _safe_export_path("fills.csv")
    assert resolved == (tmp_path / "fills.csv").resolve()


def test_safe_export_path_accepts_home_tilde():
    resolved = _safe_export_path("~/fills.csv")
    assert resolved == (Path.home() / "fills.csv").resolve()


def test_safe_export_path_rejects_outside_scope(tmp_path: Any, monkeypatch: Any):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="must be inside"):
        _safe_export_path("/etc/passwd")


def test_safe_export_path_rejects_dotdot_traversal(tmp_path: Any, monkeypatch: Any):
    # tmp_path lives under /private/var/... on macOS, neither inside cwd (set below)
    # nor under $HOME, so a `..` escape from cwd should be rejected.
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.chdir(sandbox)
    with pytest.raises(ValueError, match="must be inside"):
        _safe_export_path("../../../../etc/passwd")


@pytest.mark.asyncio
@respx.mock
async def test_bulk_fills_export_writes_csv(tmp_path: Any, monkeypatch: Any):
    monkeypatch.chdir(tmp_path)
    csv_body = b"id,size,price\n1,10,77000\n2,-5,77100\n"
    respx.get(f"{INDIA_TESTNET_REST}/fills/history/download/csv").mock(
        return_value=httpx.Response(
            200, content=csv_body, headers={"content-type": "text/csv"}
        )
    )
    result = await _call_tool(
        "bulk_fills_export",
        output_path="fills.csv",
        start_time_us=1000,
        end_time_us=2000,
        product_ids=[1, 2],
    )
    structured = result[1] if isinstance(result, tuple) else result
    out = (tmp_path / "fills.csv").read_bytes()
    assert out == csv_body
    assert structured["row_count"] == 2  # 3 newlines - 1 header
    assert structured["size_bytes"] == len(csv_body)
    assert structured["path"].endswith("fills.csv")


@pytest.mark.asyncio
@respx.mock
async def test_bulk_fills_export_passes_query_params(tmp_path: Any, monkeypatch: Any):
    monkeypatch.chdir(tmp_path)
    route = respx.get(f"{INDIA_TESTNET_REST}/fills/history/download/csv").mock(
        return_value=httpx.Response(200, content=b"a,b\n", headers={"content-type": "text/csv"})
    )
    await _call_tool(
        "bulk_fills_export",
        output_path="fills.csv",
        product_ids=[1, 2],
        contract_types=["perpetual_futures"],
        start_time_us=100,
        end_time_us=200,
    )
    url = str(route.calls[0].request.url)
    assert "product_ids=1%2C2" in url
    assert "contract_types=perpetual_futures" in url
    assert "start_time=100" in url
    assert "end_time=200" in url


@pytest.mark.asyncio
@respx.mock
async def test_bulk_fills_export_rejects_unsafe_path(tmp_path: Any, monkeypatch: Any):
    monkeypatch.chdir(tmp_path)
    # Should not even reach the network — raise before issuing the request.
    route = respx.get(f"{INDIA_TESTNET_REST}/fills/history/download/csv").mock(
        return_value=httpx.Response(200, content=b"")
    )
    with pytest.raises(Exception):
        await _call_tool("bulk_fills_export", output_path="/etc/passwd")
    assert not route.called
