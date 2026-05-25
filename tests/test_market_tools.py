from typing import Any

import httpx
import pytest
import respx

from delta_exchange_mcp.client import DeltaClient
from delta_exchange_mcp.config import INDIA_TESTNET_REST, Config


@pytest.fixture
def client() -> DeltaClient:
    cfg = Config(env="india_testnet", base_url=INDIA_TESTNET_REST)
    return DeltaClient(cfg)


@pytest.mark.asyncio
@respx.mock
async def test_list_products_passes_csv_filters(client: DeltaClient):
    route = respx.get(f"{INDIA_TESTNET_REST}/products").mock(
        return_value=httpx.Response(200, json={"success": True, "result": [], "meta": {"after": None}})
    )
    res = await client.get(
        "/products",
        params={"contract_types": "perpetual_futures,call_options", "states": "live", "page_size": 50},
    )
    assert route.called
    sent = route.calls[0].request.url
    assert "contract_types=perpetual_futures%2Ccall_options" in str(sent)
    assert "states=live" in str(sent)
    assert res["result"] == []


@pytest.mark.asyncio
@respx.mock
async def test_error_body_raises_delta_api_error(client: DeltaClient):
    from delta_exchange_mcp.errors import DeltaApiError

    respx.get(f"{INDIA_TESTNET_REST}/products/NOPE").mock(
        return_value=httpx.Response(
            404,
            json={"success": False, "error": {"code": "product_not_found", "context": {"symbol": "NOPE"}}},
        )
    )
    with pytest.raises(DeltaApiError) as exc:
        await client.get("/products/NOPE")
    assert exc.value.code == "product_not_found"
    assert exc.value.context == {"symbol": "NOPE"}


@pytest.mark.asyncio
@respx.mock
async def test_none_params_are_stripped_before_send(client: DeltaClient):
    """Delta rejects empty-string params (e.g. expiry=) as invalid dates."""
    route = respx.get(f"{INDIA_TESTNET_REST}/products").mock(
        return_value=httpx.Response(200, json={"success": True, "result": []})
    )
    await client.get(
        "/products",
        params={"states": "live", "expiry": None, "contract_types": None, "page_size": 3},
    )
    sent = str(route.calls[0].request.url)
    assert "expiry" not in sent
    assert "contract_types" not in sent
    assert "states=live" in sent
    assert "page_size=3" in sent


async def _call_market_tool(client: DeltaClient, name: str, **kwargs: Any) -> Any:
    from mcp.server.fastmcp import FastMCP

    from delta_exchange_mcp.tools import market

    mcp = FastMCP("test")
    market.register(mcp, client)
    return await mcp.call_tool(name, kwargs)


@pytest.mark.asyncio
@respx.mock
async def test_get_settlement_prices_filters_states_expired(client: DeltaClient):
    route = respx.get(f"{INDIA_TESTNET_REST}/products").mock(
        return_value=httpx.Response(
            200, json={"success": True, "result": [], "meta": {"after": None}}
        )
    )
    await _call_market_tool(
        client,
        "get_settlement_prices",
        contract_types=["call_options", "put_options"],
        page_size=50,
        after="cur",
    )
    url = str(route.calls[0].request.url)
    assert "states=expired" in url
    assert "contract_types=call_options%2Cput_options" in url
    assert "page_size=50" in url
    assert "after=cur" in url


@pytest.mark.asyncio
@respx.mock
async def test_429_retries_then_succeeds(client: DeltaClient):
    route = respx.get(f"{INDIA_TESTNET_REST}/tickers/BTCUSD").mock(
        side_effect=[
            httpx.Response(429, headers={"X-RATE-LIMIT-RESET": "50"}, json={}),
            httpx.Response(200, json={"success": True, "result": {"close": "100"}}),
        ]
    )
    res = await client.get("/tickers/BTCUSD")
    assert route.call_count == 2
    assert res["result"] == {"close": "100"}
