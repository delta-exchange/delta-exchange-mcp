import httpx
import pytest
import respx

from delta_exchange_mcp import config
from tests.test_activation import connected


@pytest.mark.parametrize(
    ("name", "arguments", "accepted"),
    [
        ("list_tickers", {"underlying": "BTC"}, "underlying_asset_symbols"),
        ("list_products", {"search": "BTC"}, "contract_types"),
        ("get_connection_status", {"ctx": {}}, "(none)"),
    ],
)
@respx.mock
async def test_unknown_arguments_return_an_error_without_requesting_data(
    name, arguments, accepted
):
    async with connected() as session:
        result = await session.raw_call(name, **arguments)
    assert result.is_error
    message = " ".join(block.text for block in result.content if block.type == "text")
    assert next(iter(arguments)) in message
    assert "Accepted arguments:" in message
    assert accepted in message
    assert not respx.calls


@respx.mock
async def test_a_known_filter_still_reaches_delta(monkeypatch):
    monkeypatch.setenv("DELTA_MCP_ENV", "india_testnet")
    route = respx.get(config.INDIA_TESTNET_REST + "/tickers").mock(
        return_value=httpx.Response(200, json={"success": True, "result": []})
    )
    async with connected() as session:
        result = await session.raw_call(
            "list_tickers", underlying_asset_symbols=["BTC"]
        )
    assert not result.is_error
    assert route.calls.last.request.url.params["underlying_asset_symbols"] == "BTC"


async def test_tool_schemas_disallow_unknown_top_level_arguments():
    async with connected() as session:
        tools = (await session.client.list_tools()).tools
    assert tools
    assert all(tool.input_schema.get("additionalProperties") is False for tool in tools)


@respx.mock
async def test_unknown_arguments_also_block_authenticated_mutations(monkeypatch):
    monkeypatch.setenv("DELTA_API_KEY", "test-key")
    monkeypatch.setenv("DELTA_API_SECRET", "test-secret")
    async with connected() as session:
        result = await session.raw_call("cancel_all_orders", productid=27)
    assert result.is_error
    assert "productid" in result.content[0].text
    assert "product_id" in result.content[0].text
    assert not respx.calls


@respx.mock
async def test_mixing_known_and_unknown_arguments_does_not_send_a_request():
    async with connected() as session:
        result = await session.raw_call(
            "list_tickers",
            underlying_asset_symbols=["BTC"],
            underlying="ETH",
            search="BTC",
        )
    assert result.is_error
    assert "search, underlying" in result.content[0].text
    assert not respx.calls


@respx.mock
async def test_existing_type_validation_still_rejects_invalid_filters():
    async with connected() as session:
        result = await session.raw_call("list_tickers", contract_types=42)
    assert result.is_error
    assert "contract_types" in result.content[0].text
    assert "valid list" in result.content[0].text
    assert not respx.calls


@respx.mock
async def test_an_intentionally_unfiltered_call_still_works(monkeypatch):
    monkeypatch.setenv("DELTA_MCP_ENV", "india_testnet")
    route = respx.get(config.INDIA_TESTNET_REST + "/products").mock(
        return_value=httpx.Response(200, json={"success": True, "result": []})
    )
    async with connected() as session:
        result = await session.raw_call("list_products")
    assert not result.is_error
    assert route.called


async def test_nested_json_keys_are_not_treated_as_tool_argument_names():
    from delta_exchange_mcp.server import DeltaMCP

    app = DeltaMCP()

    @app.tool()
    async def echo(payload: dict[str, object]) -> dict[str, object]:
        return payload

    async with connected(mcp=app) as session:
        result = await session.raw_call("echo", payload={"search": "BTC"})
    assert not result.is_error
    assert result.structured_content == {"search": "BTC"}
