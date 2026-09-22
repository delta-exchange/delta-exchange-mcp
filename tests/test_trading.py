"""Trading tools: body signing, flag encoding, unconditional registration."""

import hashlib
import hmac
import json
from typing import Any

import httpx
import pytest
import respx

from delta_exchange_mcp.client import DeltaClient
from delta_exchange_mcp.config import INDIA_TESTNET_REST, Config
from delta_exchange_mcp.server import build_server
from delta_exchange_mcp.tools import trading
from mcp.server.fastmcp import FastMCP


def _client() -> DeltaClient:
    cfg = Config(
        env="india_testnet", base_url=INDIA_TESTNET_REST,
        api_key="k1", api_secret="s1",
    )
    return DeltaClient(cfg)


async def _call(client: DeltaClient, name: str, **kwargs: Any) -> Any:
    mcp = FastMCP("test")
    trading.register(mcp, client)
    return await mcp.call_tool(name, kwargs)


# --------------------------------------------------------------- body signing (critical)


@pytest.mark.asyncio
@respx.mock
async def test_place_order_signs_exact_body_bytes():
    route = respx.post(f"{INDIA_TESTNET_REST}/orders").mock(
        return_value=httpx.Response(200, json={"success": True, "result": {"id": 7}})
    )
    client = _client()
    await _call(
        client, "place_order",
        product_id=27, size=1, side="buy", order_type="limit_order", limit_price="10000",
    )

    req = route.calls[0].request
    body = req.content.decode()
    # Sent bytes must be compact JSON (no spaces) so the signature matches.
    assert body == json.dumps(json.loads(body), separators=(",", ":"))
    ts = req.headers["timestamp"]
    expected = hmac.new(b"s1", f"POST{ts}/v2/orders{body}".encode(), hashlib.sha256).hexdigest()
    assert req.headers["signature"] == expected
    assert req.headers["api-key"] == "k1"


@pytest.mark.asyncio
@respx.mock
async def test_post_only_bool_becomes_string_enum():
    route = respx.post(f"{INDIA_TESTNET_REST}/orders").mock(
        return_value=httpx.Response(200, json={"success": True, "result": {}})
    )
    client = _client()
    await _call(
        client, "place_order",
        product_id=27, size=1, side="buy", order_type="market_order", post_only=True,
    )
    assert b'"post_only":"true"' in route.calls[0].request.content


@pytest.mark.asyncio
@respx.mock
async def test_auto_topup_bool_stays_json_bool():
    route = respx.put(f"{INDIA_TESTNET_REST}/positions/auto_topup").mock(
        return_value=httpx.Response(200, json={"success": True, "result": {}})
    )
    client = _client()
    await _call(client, "configure_auto_topup", product_id=27, auto_topup=True)
    assert b'"auto_topup":true' in route.calls[0].request.content


# --------------------------------------------------------------- request shape


@pytest.mark.asyncio
async def test_place_order_requires_exactly_one_product_ref():
    client = _client()
    with pytest.raises(Exception, match="exactly one of product_id or product_symbol"):
        await _call(client, "place_order", size=1, side="buy", order_type="market_order")
    with pytest.raises(Exception, match="exactly one of product_id or product_symbol"):
        await _call(
            client, "place_order",
            product_id=1, product_symbol="BTCUSD", size=1, side="buy", order_type="market_order",
        )


@pytest.mark.asyncio
@respx.mock
async def test_prices_are_sent_exactly_as_given():
    """No tick preflight: an off-tick price reaches Delta unchanged, for Delta to reject."""
    route = respx.post(f"{INDIA_TESTNET_REST}/orders").mock(
        return_value=httpx.Response(200, json={"success": True, "result": {}})
    )
    products = respx.get(f"{INDIA_TESTNET_REST}/products")
    client = _client()
    await _call(
        client, "place_order",
        product_symbol="BTCUSD", size=1, side="buy", order_type="limit_order",
        limit_price="61000.37",
    )
    assert b'"limit_price":"61000.37"' in route.calls[0].request.content
    assert products.call_count == 0  # no metadata round trip before the order


@pytest.mark.asyncio
@respx.mock
async def test_place_order_includes_bracket_params():
    route = respx.post(f"{INDIA_TESTNET_REST}/orders").mock(
        return_value=httpx.Response(200, json={"success": True, "result": {"id": 9}})
    )
    client = _client()
    await _call(
        client, "place_order",
        product_id=84, size=1, side="buy", order_type="limit_order", limit_price="61000",
        bracket_take_profit_price="66500", bracket_stop_loss_price="60000",
    )
    body = route.calls[0].request.content
    assert b'"bracket_take_profit_price":"66500"' in body
    assert b'"bracket_stop_loss_price":"60000"' in body


# --------------------------------------------- request shape, not a guardrail


@pytest.mark.asyncio
async def test_cancel_order_requires_exactly_one_identifier():
    """Neither leaves a product-wide DELETE; both is ambiguous. Unsendable either way."""
    client = _client()
    for kwargs in ({}, {"id": 5, "client_order_id": "abc"}):
        with pytest.raises(Exception, match="exactly one of id or client_order_id"):
            await _call(client, "cancel_order", product_id=27, **kwargs)


@pytest.mark.asyncio
async def test_bracket_order_requires_at_least_one_leg():
    """A bracket with no legs is a request with nothing to do; its contract says so."""
    client = _client()
    with pytest.raises(Exception, match="at least one of stop_loss_order or take_profit_order"):
        await _call(client, "place_bracket_order", product_id=27)


# --------------------------------------------------------------- account-wide by default


@pytest.mark.asyncio
@respx.mock
async def test_cancel_all_cancels_every_order_kind():
    route = respx.delete(f"{INDIA_TESTNET_REST}/orders/all").mock(
        return_value=httpx.Response(200, json={"success": True, "result": {}})
    )
    client = _client()
    await _call(client, "cancel_all_orders", product_id=84)
    body = route.calls[0].request.content
    assert b'"cancel_limit_orders":"true"' in body
    assert b'"cancel_stop_orders":"true"' in body
    assert b'"cancel_reduce_only_orders":"true"' in body


# --------------------------------------------------------------- batches pass through


@pytest.mark.asyncio
@respx.mock
async def test_batch_is_not_capped_and_response_is_returned_as_is():
    sent = [{"size": 1, "side": "buy", "order_type": "limit_order", "limit_price": "1"}] * 80
    route = respx.post(f"{INDIA_TESTNET_REST}/orders/batch").mock(
        return_value=httpx.Response(200, json={"success": True, "result": [{"id": 1}]})
    )
    client = _client()
    result = await _call(client, "place_batch_orders", orders=sent, product_id=84)
    assert len(json.loads(route.calls[0].request.content)["orders"]) == 80
    structured = result[1]
    payload = structured.get("result", structured)
    # No partial_failure annotation: 1 of 80 came back and the response is passed through.
    assert "partial_failure" not in json.dumps(payload)


# --------------------------------------------------------------- registration


def test_trading_tools_register_with_credentials_and_no_env_var(monkeypatch):
    """DEA-881: a key is the whole gate — no DELTA_MCP_MODE, no restart."""
    monkeypatch.delenv("DELTA_MCP_MODE", raising=False)
    cfg = Config(
        env="india_testnet", base_url=INDIA_TESTNET_REST, api_key="k", api_secret="s"
    )
    mcp = build_server(cfg)
    names = {t.name for t in mcp._tool_manager.list_tools()}
    assert trading.TOOL_NAMES <= names


def test_trading_tools_absent_without_credentials():
    cfg = Config(env="india_testnet", base_url=INDIA_TESTNET_REST)
    mcp = build_server(cfg)
    names = {t.name for t in mcp._tool_manager.list_tools()}
    assert not (trading.TOOL_NAMES & names)
