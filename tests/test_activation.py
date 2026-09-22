"""Bringing the account tools up mid-session, without restarting the client.

These drive a real client session over the real protocol rather than calling the tool
functions directly, because every interesting part of this is protocol-level: whether the
server declared that its tool list can change, whether the notification reaches the
client, and whether a `tools/list` after it shows tools that did not exist at startup.
Calling `save_credentials` in-process proves none of that.
"""

import json
from contextlib import asynccontextmanager

import anyio
import httpx
import mcp.types as types
import pytest
import respx
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from delta_exchange_mcp import config as config_mod
from delta_exchange_mcp import credentials, server, store

KEY = "typed-into-the-form-key"
SECRET = "typed-into-the-form-secret"


class Session:
    """A connected client plus the notifications the server pushed to it."""

    def __init__(self, client, initialized, mcp):
        self.client = client
        self.initialized = initialized
        self.server = mcp
        self.notifications = []

    async def tool_names(self):
        return {t.name for t in (await self.client.list_tools()).tools}

    async def call(self, name, **arguments):
        result = await self.client.call_tool(name, arguments)
        if result.structuredContent is not None:
            return result.structuredContent
        return json.loads(result.content[0].text)

    async def raw_call(self, name, **arguments):
        return await self.client.call_tool(name, arguments)

    async def open_form(self):
        result = await self.client.call_tool("setup_credentials", {})
        return result.meta["ui"]["saveGrant"]

    def saw_tool_list_changed(self):
        return any(
            isinstance(n, types.ToolListChangedNotification) for n in self.notifications
        )


@asynccontextmanager
async def connected(cfg=None, client_name=None, mcp=None):
    """A client talking to a server started the way `main` starts it.

    The SDK's own `create_connected_server_and_client_session` builds initialization
    options from scratch, which would silently drop the one capability under test here.
    """
    app = mcp or server.build_server(cfg or config_mod.load())
    owns_server = mcp is None
    try:
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            client_read, client_write = client_streams
            server_read, server_write = server_streams
            async with anyio.create_task_group() as tg:
                tg.start_soon(
                    lambda: app._mcp_server.run(
                        server_read,
                        server_write,
                        server.initialization_options(app),
                        raise_exceptions=True,
                    )
                )
                box = {}

                async def collect(message):
                    if isinstance(message, types.ServerNotification):
                        box["session"].notifications.append(message.root)

                info = (
                    types.Implementation(name=client_name, version="1")
                    if client_name
                    else None
                )
                async with ClientSession(
                    client_read, client_write, message_handler=collect, client_info=info
                ) as client:
                    initialized = await client.initialize()
                    box["session"] = Session(client, initialized, app)
                    yield box["session"]
                tg.cancel_scope.cancel()
    finally:
        if owns_server:
            await app.close_live_client()


@pytest.fixture
def accepted(monkeypatch):
    """Delta accepts whatever key is offered, without a live call."""

    async def check(env, key, secret):
        return credentials.Check(ok=True, reachable=True, detail="57354187")

    monkeypatch.setattr(credentials, "check", check)


async def save(session, **overrides):
    grant = await session.open_form()
    arguments = {
        "environment": "india_testnet",
        "api_key": KEY,
        "api_secret": SECRET,
        "grant": grant,
    }
    arguments.update(overrides)
    return await session.call("save_credentials", **arguments)


# --- what the server promises at startup ---------------------------------------------


async def test_the_server_declares_that_its_tool_list_can_change():
    """Without this the notification is one a client was told never to expect.

    A client reads `tools/list` once and re-reads it only when told the list changed, so
    declaring `listChanged: false` and then sending the notification means the account
    tools stay invisible and the restart is unavoidable — with nothing failing anywhere
    to say so.
    """
    async with connected() as session:
        assert session.initialized.capabilities.tools.listChanged is True


async def test_the_model_is_told_how_to_reach_the_form_before_any_key_exists():
    """The state with no key has no account tool to carry a hint on its own description."""
    async with connected() as session:
        instructions = session.initialized.instructions
        assert "setup_credentials" in instructions
        assert "Never ask for an API key" in instructions


async def test_the_status_tool_exists_with_no_credentials(accepted):
    """"Am I connected?" is asked most often by someone who is not."""
    async with connected() as session:
        assert "get_connection_status" in await session.tool_names()
        status = await session.call("get_connection_status")
        assert status["credentials_configured"] is False
        assert status["account_tools_available"] is False
        assert status["trading_tools_available"] is False


# --- bringing the surface up ---------------------------------------------------------


async def test_a_saved_key_makes_the_account_tools_reachable_without_a_restart(accepted):
    """The whole point: the client sees tools that did not exist when it connected."""
    async with connected() as session:
        before = await session.tool_names()
        assert "get_positions" not in before

        result = await save(session)
        assert result["status"] == "saved"

        assert session.saw_tool_list_changed()
        after = await session.tool_names()
        assert "get_positions" in after
        assert "get_wallet_balances" in after


async def test_the_saved_message_leads_with_carrying_on_rather_than_restarting(accepted):
    async with connected() as session:
        message = (await save(session))["message"]
        assert "live in this session" in message
        assert "57354187" in message


async def test_the_status_tool_reports_the_surface_that_is_actually_live(accepted):
    async with connected() as session:
        await save(session)
        status = await session.call("get_connection_status")
        assert status["credentials_configured"] is True
        assert status["account_tools_available"] is True
        assert status["trading_tools_available"] is True
        # The environment typed into the form, not the one loaded at startup.
        assert status["environment"] == "india_testnet"
        assert json.dumps(status).find(SECRET) == -1


async def test_a_second_save_does_not_register_the_account_tools_twice(accepted):
    """Rotating a key goes through the same path, and FastMCP would keep both copies."""
    async with connected() as session:
        await save(session)
        first = await session.tool_names()
        await save(session)
        assert await session.tool_names() == first


async def test_replacing_a_key_already_in_use_rebinds_without_a_restart(accepted):
    """Every registered surface shares the rebindable client, so rotation is immediate."""
    async with connected() as session:
        await save(session)
        again = await save(session)
        assert "live in this session" in again["message"]
        assert (await session.call("get_connection_status"))["credentials_configured"] is True


@respx.mock
async def test_a_rotated_key_signs_the_next_account_request(accepted):
    """Hot status is not enough: the shared client must actually use the new identity."""
    rotated = "rotated-in-the-form-key"
    route = respx.get(f"{config_mod.INDIA_TESTNET_REST}/users/trading_preferences").mock(
        return_value=httpx.Response(200, json={"success": True, "result": {"user_id": 7}})
    )
    async with connected() as session:
        await save(session)
        await save(session, api_key=rotated)
        await session.call("get_trading_preferences")

    assert route.called
    assert route.calls.last.request.headers["api-key"] == rotated


@respx.mock
async def test_the_first_save_rebinds_market_and_account_tools_to_one_environment(accepted):
    """Public and authenticated closures must move together, not split across sites."""
    ticker = respx.get(f"{config_mod.INDIA_TESTNET_REST}/tickers/BTCUSD").mock(
        return_value=httpx.Response(200, json={"success": True, "result": {"symbol": "BTCUSD"}})
    )
    preferences = respx.get(
        f"{config_mod.INDIA_TESTNET_REST}/users/trading_preferences"
    ).mock(
        return_value=httpx.Response(200, json={"success": True, "result": {"user_id": 7}})
    )
    async with connected() as session:
        await save(session)
        await session.call("get_ticker", symbol="BTCUSD")
        await session.call("get_trading_preferences")

    assert ticker.called and preferences.called


async def test_a_grant_from_a_closed_session_cannot_be_used_by_a_new_one(accepted):
    """The app capability is session-bound even while its ten-minute TTL remains."""
    app = server.build_server(config_mod.load())
    try:
        async with connected(mcp=app) as first:
            grant = await first.open_form()

        async with connected(mcp=app) as second:
            result = await second.call(
                "save_credentials",
                environment="india_testnet",
                api_key=KEY,
                api_secret=SECRET,
                grant=grant,
            )
            assert result["status"] == "refused"
    finally:
        await app.close_live_client()


async def test_a_concurrent_full_save_is_reported_as_superseded(accepted, monkeypatch):
    """Never claim checked account A is live after another process publishes account B."""
    real_save = credentials.save

    def save_then_supersede(env, key, secret):
        problem = real_save(env, key, secret)
        assert problem is None
        assert (
            store.write(
                {
                    "DELTA_MCP_ENV": "india_prod",
                    "DELTA_API_KEY": "newer-client-key",
                    "DELTA_API_SECRET": "newer-client-secret",
                }
            )
            is None
        )
        return None

    monkeypatch.setattr(credentials, "save", save_then_supersede)
    async with connected(client_name="Claude Desktop") as session:
        result = await save(session)

        assert result["status"] == "superseded"
        assert "someone@delta.exchange" not in result["message"]
        assert "changed the shared Delta settings" in result["message"]
        status = await session.call("get_connection_status")
        assert status["environment"] == "india_prod"
        assert session.server.live_client.config.api_key == "newer-client-key"


async def test_a_save_under_a_shell_export_is_not_reported_as_connected(accepted, monkeypatch):
    """The process environment outranks the file, so a correct save can still not be live.

    Reporting the checked account as connected would name an account the session is not
    talking to: every later read and every order goes to the exported key instead.
    """
    monkeypatch.setenv("DELTA_API_KEY", "exported-in-the-shell-key")
    monkeypatch.setenv("DELTA_API_SECRET", "exported-in-the-shell-secret")
    async with connected(client_name="Claude Desktop") as session:
        result = await save(session)

        assert result["status"] == "superseded"
        assert "someone@delta.exchange" not in result["message"]
        assert "DELTA_API_KEY" in result["message"]
        assert session.server.live_client.config.api_key == "exported-in-the-shell-key"
        # The file still carries what was typed: it is what every other client reads.
        assert store.read()["DELTA_API_KEY"] == KEY


def rejecting(code, detail="delta api error: raw [http 401] (context={...})", ip=""):
    async def check(env, key, secret):
        return credentials.Check(
            ok=False, reachable=True, detail=detail, code=code, ip=ip
        )

    return check


async def test_a_key_from_the_other_site_names_the_choice_not_the_variable(monkeypatch):
    """The commonest first-run mistake, answered in the words on the form's own radios."""
    monkeypatch.setattr(credentials, "check", rejecting("invalid_api_key"))
    async with connected() as session:
        message = (await save(session))["message"]
        # save() picks the practice site, so that is what it was checked against and the
        # other choice is the one worth offering.
        assert "demo.delta.exchange" in message
        assert "Real account" in message


async def test_a_rejection_never_shows_the_form_the_message_meant_for_a_log(monkeypatch):
    """`delta api error:`, the raw code, DELTA_MCP_ENV and `[http 401]` mean nothing here.

    Appending readable copy to that string rather than replacing it left the user reading
    the machine's version first and the same advice twice.
    """
    monkeypatch.setattr(credentials, "check", rejecting("invalid_api_key"))
    async with connected() as session:
        message = (await save(session))["message"]
        for leak in ("delta api error", "DELTA_MCP_ENV", "http 401", "context=", "_api_key"):
            assert leak not in message, f"{leak!r} leaked into the form"


async def test_a_blocked_ip_says_so_and_shows_the_address_delta_saw(monkeypatch):
    """Telling someone to switch sites over an unwhitelisted IP sends them nowhere useful."""
    monkeypatch.setattr(
        credentials, "check", rejecting("ip_not_whitelisted_for_api_key", ip="1.2.3.4")
    )
    async with connected() as session:
        message = (await save(session))["message"]
        assert "1.2.3.4" in message
        assert "whitelisted" in message
        assert "demo.delta.exchange" not in message


async def test_a_trading_preferences_permission_failure_is_not_an_invalid_key(monkeypatch):
    monkeypatch.setattr(credentials, "check", rejecting("unauthorized_api_access"))
    async with connected() as session:
        message = (await save(session))["message"]
        assert "lacks permission for trading preferences" in message
        assert "does not establish whether Read Data alone is sufficient" in message
        assert "invalid" not in message.lower()


async def test_an_unanticipated_failure_keeps_the_raw_message(monkeypatch):
    """For a code nobody wrote copy for, the raw text is the only information there is."""
    monkeypatch.setattr(
        credentials, "check", rejecting("SomethingNew", detail="delta api error: SomethingNew")
    )
    async with connected() as session:
        assert "SomethingNew" in (await save(session))["message"]


async def test_a_key_delta_rejects_changes_nothing(monkeypatch):
    async def rejected(env, key, secret):
        return credentials.Check(
            ok=False, reachable=True, detail="delta api error: InvalidApiKey"
        )

    monkeypatch.setattr(credentials, "check", rejected)
    async with connected() as session:
        assert (await save(session))["status"] == "rejected"
        assert not session.saw_tool_list_changed()
        assert "get_positions" not in await session.tool_names()
