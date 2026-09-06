"""MCP 2026 discovery and request-scoped authorization behavior."""

import asyncio
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

from delta_exchange_mcp import authorization, debug_log
from delta_exchange_mcp import connection_app, store
from delta_exchange_mcp.auth.connection import ConnectionService
from delta_exchange_mcp.auth.consent import (
    ConsentBinding,
    ConsentStore,
    MemoryConsentBackend,
)
from delta_exchange_mcp.auth.store import (
    CredentialSource,
    CredentialState,
    CredentialStore,
    MemoryMetadata,
    MemorySecretBackend,
)
from delta_exchange_mcp.errors import DeltaApiError
from delta_exchange_mcp.server import DeltaMCP, build_server
from delta_exchange_mcp.tools import account, trading

MANAGE_URL = "http://127.0.0.1:43123/manage"
CLIENT_NAME = "Claude Desktop"


async def manage_url(ctx: Context, access: authorization.Access) -> str:
    """Return a deterministic loopback URL in protocol tests."""
    return MANAGE_URL


def connection_service() -> ConnectionService:
    credentials = CredentialStore(
        MemorySecretBackend(),
        MemoryMetadata(),
        CredentialSource.OS_STORE,
    )
    consent = ConsentStore(
        store.path().with_name("consent.json"),
        secure_backend_available=True,
        memory_backend=MemoryConsentBackend(),
    )
    return ConnectionService.open(credentials=credentials, consent=consent)


@asynccontextmanager
async def connected(
    app: DeltaMCP | None = None,
    *,
    mode: str = "auto",
    url_elicitation: bool = False,
    apps: bool = False,
) -> AsyncIterator[Client]:
    owned = (
        build_server(
            manage_url=manage_url,
            connection_service=connection_service(),
        )
        if app is None
        else app
    )

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
        values["DELTA_MCP_MODE"] = "trade"
    target = store.path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(f"{name}={value}\n" for name, value in values.items()))


async def test_auto_mode_discovers_the_2026_server_and_apps_extension() -> None:
    async with connected() as client:
        assert client.protocol_version == "2026-07-28"
        assert client.server_info.name == "delta-exchange"
        assert EXTENSION_ID in client.server_capabilities.extensions


async def test_tool_discovery_is_stable_across_authorization_changes() -> None:
    service = connection_service()
    app = build_server(manage_url=manage_url, connection_service=service)
    environment = service.client.config.env
    try:
        async with connected(app) as client:
            before = {
                tool.name
                for tool in (await client.list_tools(cache_mode="refresh")).tools
            }

            service.credentials.replace(
                environment,
                "test-key",
                "test-secret",
                state=CredentialState.VERIFIED,
            )
            connected_status = await client.call_tool("get_connection_status", {})
            connected_tools = {
                tool.name
                for tool in (await client.list_tools(cache_mode="refresh")).tools
            }

            metadata = service.credentials.metadata(environment)
            service.consent.enable(
                ConsentBinding(
                    client_name=CLIENT_NAME,
                    environment=environment,
                    credential_revision=metadata.revision,
                    credential_generation=metadata.generation,
                    credential_session_generation=None,
                ),
                expected_generation=0,
            )
            approved_status = await client.call_tool("get_connection_status", {})
            approved_tools = {
                tool.name
                for tool in (await client.list_tools(cache_mode="refresh")).tools
            }
    finally:
        await app.close_live_client()

    assert connected_status.structured_content["credentials_configured"] is True
    assert approved_status.structured_content["trading"]["enabled"] is True
    assert before == connected_tools == approved_tools
    assert account.TOOL_NAMES <= before
    assert trading.TOOL_NAMES <= before
    assert "setup_credentials" in before
    assert "save_credentials" not in before
    assert "save_mode" not in before


async def test_debug_setting_does_not_change_tool_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DELTA_MCP_DEBUG", raising=False)
    without_debug = build_server(connection_service=connection_service())
    monkeypatch.setenv("DELTA_MCP_DEBUG", "1")
    with_debug = build_server(connection_service=connection_service())
    try:
        before = {tool.name for tool in await without_debug.list_tools()}
        after = {tool.name for tool in await with_debug.list_tools()}
    finally:
        await without_debug.close_live_client()
        await with_debug.close_live_client()
        debug_log.shutdown()

    assert before == after
    assert "get_debug_status" in before


async def test_connection_status_does_not_return_credentials() -> None:
    credentialled()
    async with connected() as client:
        result = await client.call_tool("get_connection_status", {})

    rendered = json.dumps(result.structured_content)
    assert "test-key" not in rendered
    assert "test-secret" not in rendered
    assert "signature" not in rendered.lower()
    assert result.structured_content["credentials_configured"] is True
    assert result.structured_content["client_name"] == CLIENT_NAME
    assert result.structured_content["client_version"] == "1"


async def test_tool_errors_expose_only_deliberate_safe_messages() -> None:
    app = DeltaMCP()

    @app.tool()
    async def rejected_by_delta() -> None:
        raise DeltaApiError(
            "insufficient_margin",
            context={"upstream_private_value": "must-not-cross-mcp"},
            status=400,
        )

    @app.tool()
    async def crashed() -> None:
        raise RuntimeError("unexpected-private-value")

    try:
        async with connected(app) as client:
            rejected = await client.call_tool("rejected_by_delta", {})
            unexpected = await client.call_tool("crashed", {})
    finally:
        await app.close_live_client()

    assert rejected.is_error is True
    assert rejected.content[0].text == (
        "Error executing tool rejected_by_delta: "
        "delta api error: insufficient_margin [http 400]"
    )
    assert "must-not-cross-mcp" not in rejected.content[0].text
    assert unexpected.is_error is True
    assert unexpected.content[0].text == "Error executing tool crashed"
    assert "unexpected-private-value" not in unexpected.content[0].text


async def test_connection_status_ignores_file_credentials_after_migration() -> None:
    credentialled()
    app = build_server(
        manage_url=manage_url,
        connection_service=connection_service(),
    )
    try:
        async with connected(app) as client:
            await client.call_tool("get_connection_status", {})
            assert app.live_client.config.api_key == "test-key"

            store.path().write_text(
                "DELTA_MCP_ENV=india_testnet\n"
                "DELTA_API_KEY=rotated-key\n"
                "DELTA_API_SECRET=rotated-secret\n"
            )
            await client.call_tool("get_connection_status", {})
            assert app.live_client.config.api_key == "test-key"
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
            final_trading_check=lambda: enabled,
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


async def test_final_checker_blocks_a_mutation_when_consent_changes_during_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consent = True
    state_checks: list[bool] = []
    lookup_started = asyncio.Event()
    release_lookup = asyncio.Event()
    mutations: list[str] = []

    async def access_state(ctx: Context) -> authorization.AccessState:
        state_checks.append(consent)
        return authorization.AccessState(
            credentials_ready=True,
            trading_enabled=True,
            client_name=CLIENT_NAME,
            final_trading_check=lambda: consent,
        )

    monkeypatch.setenv("DELTA_MCP_AUDIT", "off")
    app = build_server(manage_url=manage_url, access_state=access_state)

    async def get(
        path: str, params: dict[str, Any] | None = None, *, auth: bool = False
    ) -> dict[str, Any]:
        assert path == "/products/BTCUSD"
        lookup_started.set()
        await release_lookup.wait()
        return {"result": {"id": 27, "symbol": "BTCUSD", "tick_size": "0.1"}}

    async def mutate(
        path: str, payload: dict[str, Any], *, auth: bool = False
    ) -> dict[str, Any]:
        mutations.append(path)
        return {}

    monkeypatch.setattr(app.live_client, "get", get)
    monkeypatch.setattr(app.live_client, "post", mutate)
    monkeypatch.setattr(app.live_client, "put", mutate)
    monkeypatch.setattr(app.live_client, "delete", mutate)
    arguments = {
        "product_symbol": "BTCUSD",
        "size": 1,
        "side": "buy",
        "order_type": "limit_order",
        "limit_price": "62000.07",
    }

    try:
        async with connected(app, mode="2026-07-28") as client:
            call = asyncio.create_task(client.call_tool("place_order", arguments))
            await lookup_started.wait()
            assert state_checks == [True]
            consent = False
            release_lookup.set()
            result = await call
    finally:
        await app.close_live_client()

    assert result.is_error is True
    assert "trading was disabled" in result.content[0].text
    assert mutations == []


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


@pytest.mark.parametrize(("name", "arguments"), DRY_RUNS.items())
async def test_every_real_trading_tool_is_blocked_before_any_mutation(
    name: str,
    arguments: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentialled()
    app = build_server(
        manage_url=manage_url,
        connection_service=connection_service(),
    )
    mutations: list[tuple[str, str]] = []

    async def mutate(
        path: str,
        json_body: Any = None,
        *,
        auth: bool = False,
    ) -> dict[str, Any]:
        mutations.append((path, repr(json_body)))
        return {}

    monkeypatch.setattr(app.live_client, "post", mutate)
    monkeypatch.setattr(app.live_client, "put", mutate)
    monkeypatch.setattr(app.live_client, "delete", mutate)
    try:
        async with connected(
            app,
            mode="2026-07-28",
            url_elicitation=True,
        ) as client:
            result = await client.session.call_tool(
                name,
                arguments,
                allow_input_required=True,
            )
    finally:
        await app.close_live_client()

    assert isinstance(result, InputRequiredResult)
    prompt = result.input_requests["delta_exchange_authorization"]
    assert "Enable trading" in prompt.params.message
    assert mutations == []


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
