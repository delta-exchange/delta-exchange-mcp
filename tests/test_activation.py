"""MCP 2026 discovery and request-scoped authorization behavior."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
import respx
from mcp import types
from mcp.client import Client, advertise
from mcp.server.apps import APP_MIME_TYPE, EXTENSION_ID
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, InputRequiredResult

from delta_exchange_mcp import config as config_mod
from delta_exchange_mcp import authorization
from delta_exchange_mcp import connection_app, store
from delta_exchange_mcp.server import DeltaMCP, build_server
from delta_exchange_mcp.tools import account, trading

MANAGE_URL = "http://127.0.0.1:43123/manage"
CLIENT_NAME = "Claude Desktop"


async def manage_url(ctx: Context, access: authorization.Access) -> str:
    """Return a deterministic loopback URL in protocol tests."""
    return MANAGE_URL


@asynccontextmanager
async def connected(
    app: DeltaMCP | None = None,
    *,
    mode: str = "auto",
    url_elicitation: bool = False,
    apps: bool = False,
) -> AsyncIterator[Client]:
    owned = app or build_server(manage_url=manage_url)

    async def elicit(ctx: object, params: object) -> types.ElicitResult:
        return types.ElicitResult(action="accept")

    extensions = (
        [advertise(EXTENSION_ID, {"mimeTypes": [APP_MIME_TYPE]})] if apps else None
    )
    client = Client(
        owned,
        mode=mode,
        client_info=types.Implementation(name=CLIENT_NAME, version="1"),
        elicitation_callback=elicit if url_elicitation else None,
        extensions=extensions,
    )
    try:
        async with client:
            yield client
    finally:
        if app is None:
            await owned.close_live_client()


def credentialled(*, trade: bool = False) -> None:
    values = {
        "DELTA_MCP_ENV": "india_testnet",
        "DELTA_API_KEY": "test-key",
        "DELTA_API_SECRET": "test-secret",
    }
    if trade:
        values[config_mod.mode_key(CLIENT_NAME)] = "trade"
    assert store.write(values) is None


async def test_auto_mode_discovers_the_2026_server_and_apps_extension() -> None:
    async with connected() as client:
        assert client.protocol_version == "2026-07-28"
        assert client.server_info.name == "delta-exchange"
        assert EXTENSION_ID in client.server_capabilities.extensions


async def test_tool_discovery_is_stable_across_authorization_changes() -> None:
    async with connected() as client:
        before = {
            tool.name
            for tool in (await client.list_tools(cache_mode="refresh")).tools
        }
        credentialled(trade=True)
        after = {
            tool.name
            for tool in (await client.list_tools(cache_mode="refresh")).tools
        }

    assert before == after
    assert account.TOOL_NAMES <= before
    assert trading.TOOL_NAMES <= before
    assert "setup_credentials" in before
    assert "save_credentials" not in before
    assert "save_mode" not in before


async def test_connection_status_does_not_return_credentials() -> None:
    credentialled()
    async with connected() as client:
        result = await client.call_tool("get_connection_status", {})

    rendered = json.dumps(result.structured_content)
    assert "test-key" not in rendered
    assert "test-secret" not in rendered
    assert "signature" not in rendered.lower()
    assert result.structured_content["credentials_configured"] is True


async def test_connection_status_rebinds_an_externally_rotated_credential() -> None:
    credentialled()
    app = build_server(manage_url=manage_url)
    try:
        async with connected(app) as client:
            await client.call_tool("get_connection_status", {})
            assert app.live_client.config.api_key == "test-key"

            assert store.write(
                {
                    "DELTA_MCP_ENV": "india_testnet",
                    "DELTA_API_KEY": "rotated-key",
                    "DELTA_API_SECRET": "rotated-secret",
                }
            ) is None
            await client.call_tool("get_connection_status", {})
            assert app.live_client.config.api_key == "rotated-key"
    finally:
        await app.close_live_client()


async def test_setup_has_no_secret_arguments() -> None:
    async with connected() as client:
        tools = {
            tool.name: tool
            for tool in (await client.list_tools(cache_mode="refresh")).tools
        }

    setup = tools["setup_credentials"]
    schema = setup.input_schema
    assert schema.get("properties", {}) == {}
    rendered = str(schema).lower()
    assert "api_key" not in rendered
    assert "api_secret" not in rendered
    assert setup.meta["ui"] == {
        "resourceUri": connection_app.VIEW_URI,
        "visibility": ["model", "app"],
    }


async def test_modern_account_gate_returns_url_input_and_sealed_request_state() -> None:
    async with connected(mode="2026-07-28", url_elicitation=True) as client:
        result = await client.session.call_tool(
            "get_positions", {}, allow_input_required=True
        )

    assert isinstance(result, InputRequiredResult)
    request = result.input_requests["delta_exchange_authorization"]
    assert request.params.mode == "url"
    assert request.params.url == MANAGE_URL
    assert result.request_state.startswith("v1.")
    assert "account" not in result.request_state
    assert MANAGE_URL not in result.request_state


async def test_setup_uses_url_elicitation_when_the_client_supports_it() -> None:
    async with connected(mode="2026-07-28", url_elicitation=True) as client:
        result = await client.session.call_tool(
            "setup_credentials", {}, allow_input_required=True
        )

    assert isinstance(result, InputRequiredResult)
    request = result.input_requests["delta_exchange_authorization"]
    assert request.params.url == MANAGE_URL


async def test_setup_request_resumption_does_not_loop_or_claim_completion() -> None:
    async with connected(mode="2026-07-28", url_elicitation=True) as client:
        result = await client.call_tool("setup_credentials", {})

    assert result.structured_content["status"] == "authorization_pending"
    assert "Finish it" in result.content[0].text


async def test_legacy_trade_mode_does_not_count_as_browser_consent() -> None:
    credentialled(trade=True)
    arguments = {
        "product_id": 27,
        "size": 1,
        "side": "buy",
        "order_type": "market_order",
    }
    async with connected(mode="2026-07-28", url_elicitation=True) as client:
        result = await client.session.call_tool(
            "place_order", arguments, allow_input_required=True
        )

    assert isinstance(result, InputRequiredResult)
    request = result.input_requests["delta_exchange_authorization"]
    assert "Enable trading" in request.params.message


async def test_resumed_trade_never_executes_the_pending_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enabled = False

    async def access_state(ctx: Context) -> authorization.AccessState:
        return authorization.AccessState(
            credentials_ready=True,
            trading_enabled=enabled,
            client_name=CLIENT_NAME,
        )

    monkeypatch.setenv("DELTA_MCP_AUDIT", "off")
    app = build_server(manage_url=manage_url, access_state=access_state)
    sent = False

    async def post(
        path: str, payload: dict[str, Any], *, auth: bool = False
    ) -> dict[str, Any]:
        nonlocal sent
        sent = True
        return {}

    monkeypatch.setattr(app.live_client, "post", post)
    arguments = {
        "product_id": 27,
        "size": 1,
        "side": "buy",
        "order_type": "market_order",
    }
    try:
        async with connected(
            app, mode="2026-07-28", url_elicitation=True
        ) as client:
            first = await client.session.call_tool(
                "place_order", arguments, allow_input_required=True
            )
            assert isinstance(first, InputRequiredResult)

            enabled = True
            second = await client.session.call_tool(
                "place_order",
                arguments,
                input_responses={
                    "delta_exchange_authorization": types.ElicitResult(action="accept")
                },
                request_state=first.request_state,
                allow_input_required=True,
            )
    finally:
        await app.close_live_client()

    assert isinstance(second, CallToolResult)
    assert second.structured_content["status"] == "authorization_complete"
    assert "pending trade was not sent" in second.content[0].text
    assert sent is False


async def test_apps_result_opens_the_external_page_and_keeps_a_text_link() -> None:
    async with connected(mode="2026-07-28", apps=True) as client:
        result = await client.call_tool("setup_credentials", {})
        resource = await client.read_resource(connection_app.VIEW_URI)

    assert result.meta["ui"] == {
        "resourceUri": connection_app.VIEW_URI,
        "manageUrl": MANAGE_URL,
    }
    assert MANAGE_URL in result.content[0].text
    assert resource.contents[0].mime_type == APP_MIME_TYPE
    html = resource.contents[0].text
    assert 'request("ui/open-link", { url: manageUrl })' in html
    assert 'request("tools/call"' not in html
    assert "api_secret" not in html.lower()


async def test_client_without_url_or_apps_gets_a_clickable_link() -> None:
    async with connected(mode="2026-07-28") as client:
        result = await client.call_tool("get_positions", {})

    assert result.is_error is True
    assert f"]({MANAGE_URL})" in result.content[0].text


async def test_legacy_url_elicitation_returns_without_running_the_account_call() -> None:
    seen = []

    async def elicit(ctx: object, params: Any) -> types.ElicitResult:
        seen.append(params)
        return types.ElicitResult(action="accept")

    app = build_server(manage_url=manage_url)
    try:
        async with Client(
            app,
            mode="legacy",
            client_info=types.Implementation(name=CLIENT_NAME, version="1"),
            elicitation_callback=elicit,
        ) as client:
            result = await client.call_tool("get_positions", {})
    finally:
        await app.close_live_client()

    assert len(seen) == 1
    assert seen[0].url == MANAGE_URL
    assert result.structured_content["status"] == "authorization_pending"


DRY_RUNS = {
    "place_order": {
        "product_id": 27,
        "size": 1,
        "side": "buy",
        "order_type": "market_order",
    },
    "edit_order": {"id": 1, "size": 1, "product_id": 27},
    "cancel_order": {"product_id": 27, "id": 1},
    "cancel_all_orders": {},
    "place_batch_orders": {
        "product_id": 27,
        "orders": [{"size": 1, "side": "buy", "order_type": "market_order"}],
    },
    "edit_batch_orders": {
        "product_id": 27,
        "orders": [{"id": 1, "size": 1}],
    },
    "cancel_batch_orders": {"product_id": 27, "orders": [{"id": 1}]},
    "place_bracket_order": {
        "product_id": 27,
        "stop_loss_order": {"order_type": "market_order", "trail_amount": "1"},
    },
    "edit_bracket_order": {"id": 1, "product_id": 27},
    "set_product_leverage": {"product_id": 27, "leverage": "10"},
    "adjust_position_margin": {"product_id": 27, "delta_margin": "5"},
    "close_all_positions": {"close_all_portfolio": True},
    "configure_auto_topup": {"product_id": 27, "auto_topup": True},
}


@pytest.mark.parametrize(("name", "arguments"), DRY_RUNS.items())
@respx.mock
async def test_every_trading_dry_run_works_without_consent_or_http(
    name: str, arguments: dict[str, Any]
) -> None:
    async with connected(mode="2026-07-28") as client:
        result = await client.call_tool(name, {**arguments, "dry_run": True})

    assert result.is_error is False
    assert result.structured_content["dry_run"] is True


async def test_every_tool_that_changes_state_has_a_write_annotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DELTA_MCP_AUDIT", "off")
    app = build_server()
    try:
        tools = await app.list_tools()
    finally:
        await app.close_live_client()

    expected = trading.TOOL_NAMES | {"bulk_fills_export", "setup_credentials"}
    writes = {tool.name for tool in tools if not tool.annotations.read_only_hint}
    assert writes == expected


async def test_non_idempotent_writes_do_not_invite_automatic_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DELTA_MCP_AUDIT", "off")
    app = build_server()
    try:
        tools = {tool.name: tool for tool in await app.list_tools()}
    finally:
        await app.close_live_client()

    for name in (
        "setup_credentials",
        "place_order",
        "place_batch_orders",
        "place_bracket_order",
        "adjust_position_margin",
    ):
        assert tools[name].annotations.idempotent_hint is False, name

