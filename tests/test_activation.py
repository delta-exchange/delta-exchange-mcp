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
        if result.structured_content is not None:
            return result.structured_content
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
                    lambda: app._lowlevel_server.run(
                        server_read,
                        server_write,
                        server.initialization_options(app),
                        raise_exceptions=True,
                    )
                )
                box = {}

                async def collect(message):
                    if isinstance(message, types.ServerNotification):
                        box["session"].notifications.append(message)

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
        return credentials.Check(ok=True, reachable=True, detail="someone@delta.exchange")

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
        assert session.initialized.capabilities.tools.list_changed is True


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
        assert status["restart_required"] is False


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
        assert "someone@delta.exchange" in message


async def test_the_status_tool_reports_the_surface_that_is_actually_live(accepted):
    async with connected() as session:
        await save(session)
        status = await session.call("get_connection_status")
        assert status["credentials_configured"] is True
        assert status["account_tools_available"] is True
        assert status["restart_required"] is False
        # The environment typed into the form, not the one loaded at startup.
        assert status["environment"] == "india_testnet"
        assert json.dumps(status).find(SECRET) == -1


async def test_a_second_save_does_not_register_the_account_tools_twice(accepted):
    """Rotating a key goes through the same path, and the SDK would keep both copies."""
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
        assert (await session.call("get_connection_status"))["restart_required"] is False


@respx.mock
async def test_a_rotated_key_signs_the_next_account_request(accepted):
    """Hot status is not enough: the shared client must actually use the new identity."""
    rotated = "rotated-in-the-form-key"
    route = respx.get(f"{config_mod.INDIA_TESTNET_REST}/profile").mock(
        return_value=httpx.Response(200, json={"success": True, "result": {"id": 7}})
    )
    async with connected() as session:
        await save(session)
        await save(session, api_key=rotated)
        await session.call("get_profile")

    assert route.called
    assert route.calls.last.request.headers["api-key"] == rotated


@respx.mock
async def test_the_first_save_rebinds_market_and_account_tools_to_one_environment(accepted):
    """Public and authenticated closures must move together, not split across sites."""
    ticker = respx.get(f"{config_mod.INDIA_TESTNET_REST}/tickers/BTCUSD").mock(
        return_value=httpx.Response(200, json={"success": True, "result": {"symbol": "BTCUSD"}})
    )
    profile = respx.get(f"{config_mod.INDIA_TESTNET_REST}/profile").mock(
        return_value=httpx.Response(200, json={"success": True, "result": {"id": 7}})
    )
    async with connected() as session:
        await save(session)
        await session.call("get_ticker", symbol="BTCUSD")
        await session.call("get_profile")

    assert ticker.called and profile.called


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


# --- what still needs a restart ------------------------------------------------------


async def test_trade_mode_still_waits_for_a_restart(accepted, monkeypatch):
    """Order placement must follow from the client config the user edited.

    Arming it from a form submitted in a chat would mean a mutating surface appeared
    without the user doing the thing that enables it.
    """
    monkeypatch.setenv("DELTA_MCP_MODE", "trade")
    async with connected() as session:
        result = await save(session, mode="trade")
        assert "Restart this app to turn trading on" in result["message"]

        # The reads still came up — only the mutations wait. Suppressing the notification
        # too would leave them registered and unreachable, which is the worst of both.
        assert session.saw_tool_list_changed()
        names = await session.tool_names()
        assert "get_positions" in names
        assert "place_order" not in names

        status = await session.call("get_connection_status")
        assert status["mode"] == "read"
        assert status["mode_after_restart"] == "trade"
        assert status["restart_required"] is True


async def test_a_key_the_client_config_outranks_says_so_instead_of_reporting_success(
    accepted, monkeypatch
):
    """The failure this replaces was silent, and restarting made it look broken.

    config resolves the process environment before the shared file, so a client passing
    its own key wins on every launch. The save verified a real account and would have
    reported it by name while the server went on signing with the other one.
    """
    monkeypatch.setenv("DELTA_API_KEY", "from-the-client-config")
    monkeypatch.setenv("DELTA_API_SECRET", "from-the-client-config")
    async with connected() as session:
        result = await save(session)
        assert result["status"] == "overridden"
        assert "DELTA_API_KEY" in result["message"]
        assert "will not be used" in result["message"]
        # The email of the account it checked must not be reported as connected.
        assert "someone@delta.exchange" not in result["message"]

        # The key still belongs in the file — every other client on the machine reads it.
        assert store.read()["DELTA_API_KEY"] == KEY

        status = await session.call("get_connection_status")
        assert status["overridden_by_client"] == ["DELTA_API_KEY", "DELTA_API_SECRET"]
        # Restarting cannot help: the client passes its own value again every launch.
        assert status["restart_required"] is False


async def test_an_environment_the_client_config_outranks_is_reported_too(accepted, monkeypatch):
    """Picking the practice site is just as ignorable, and fails as a rejected key later."""
    monkeypatch.setenv("DELTA_MCP_ENV", "india_prod")
    async with connected() as session:
        result = await save(session)
        assert result["status"] == "overridden"
        assert "DELTA_MCP_ENV" in result["message"]
        # This case still uses the key, so saying it is unused would be wrong — it is sent
        # to the site it was not created on, where Delta rejects it as unknown.
        assert "will not be used at all" not in result["message"]
        assert "against the other site" in result["message"]


async def test_a_client_pinning_the_same_environment_is_not_reported(accepted, monkeypatch):
    """The Cursor install link sets DELTA_MCP_ENV=india_prod for everyone who uses it.

    Testing for presence rather than for a difference would tell every one of those users
    that the key they just saved would not be used, which is both false and alarming.
    """
    monkeypatch.setenv("DELTA_MCP_ENV", "india_testnet")
    async with connected() as session:
        # The same environment the form is about to save.
        result = await save(session)
        assert result["status"] == "saved"
        assert (await session.call("get_connection_status"))["overridden_by_client"] == []


# --- trading, scoped to the client that asked for it ---------------------------------


def credentialled(mode_for=None):
    """A settings file with a working key, and optionally a client entitled to trade."""
    values = {
        "DELTA_MCP_ENV": "india_testnet",
        "DELTA_API_KEY": KEY,
        "DELTA_API_SECRET": SECRET,
    }
    if mode_for:
        values[config_mod.mode_key(mode_for)] = "trade"
    store.write(values)


async def test_trading_arms_only_for_the_client_it_was_enabled_for():
    """The settings file is shared by every client on the machine.

    An unscoped mode in it would hand order placement to all of them at once, which is
    why `load` refuses to read that name from the file. The scoped name is what the form
    writes, and only the client whose handshake matches it gets the mutating tools.
    """
    credentialled(mode_for="Claude Desktop")

    async with connected(client_name="Claude Desktop") as session:
        names = await session.tool_names()
        assert "place_order" in names
        assert "get_trading_status" in names
        assert (await session.call("get_connection_status"))["mode"] == "trade"

    async with connected(client_name="Cursor") as session:
        names = await session.tool_names()
        assert "place_order" not in names
        status = await session.call("get_connection_status")
        assert status["mode"] == "read"
        assert status["mode_after_restart"] == "read"


async def test_mode_only_save_does_not_read_or_rewrite_the_stored_credentials():
    """Once connected, changing access mode never asks the app to handle the key again."""
    credentialled()
    before = store.read()
    async with connected(client_name="Claude Desktop") as session:
        grant = await session.open_form()
        result = await session.call("save_mode", mode="trade", grant=grant)
        assert result["status"] == "saved"
        assert result["effective_mode"] == "trade"
        assert "Restart this app to turn trading on" in result["next_step"]
        assert "place_order" not in await session.tool_names()

    after = store.read()
    assert after["DELTA_API_KEY"] == before["DELTA_API_KEY"]
    assert after["DELTA_API_SECRET"] == before["DELTA_API_SECRET"]
    assert after[config_mod.mode_key("Claude Desktop")] == "trade"


async def test_another_session_cannot_call_a_globally_registered_trade_tool(monkeypatch):
    """The process registry is shared, so authorization also runs at tools/call."""
    monkeypatch.setenv("DELTA_MCP_AUDIT", "off")
    credentialled(mode_for="Trader")
    app = server.build_server(config_mod.load())
    try:
        async with connected(client_name="Trader", mcp=app) as trader:
            assert "place_order" in await trader.tool_names()

        # Deliberately skip tools/list. MCP permits a direct tools/call, and the trading
        # function remains in the SDK's process-global tool registry from the prior session.
        async with connected(client_name="Reader", mcp=app) as reader:
            result = await reader.raw_call(
                "place_order",
                product_id=27,
                size=1,
                side="buy",
                order_type="market_order",
                dry_run=True,
            )
            assert result.is_error is True
            assert "not enabled for this MCP session" in result.content[0].text
    finally:
        await app.close_live_client()


async def test_mode_only_read_disarms_live_trading_immediately(capsys):
    """The safe direction takes effect now and leaves an explicit runtime transition."""
    credentialled(mode_for="Claude Desktop")
    async with connected(client_name="Claude Desktop") as session:
        assert "place_order" in await session.tool_names()
        grant = await session.open_form()
        result = await session.call("save_mode", mode="read", grant=grant)

        assert result["status"] == "saved"
        assert "Trading stays off" in result["next_step"]
        assert "place_order" not in await session.tool_names()
        status = await session.call("get_connection_status")
        assert status["mode"] == "read"
        assert status["mode_after_restart"] == "read"
        assert status["restart_required"] is False

    transitions = capsys.readouterr().err
    assert "runtime transition=trade-armed" in transitions
    assert "runtime transition=trade-disarmed-read-mode" in transitions
    assert KEY not in transitions and SECRET not in transitions


async def test_external_identity_drift_disarms_trading_before_hot_rebind(capsys):
    """A status check cannot leave mutations signed for a changed site or account."""
    client_name = "Claude Desktop"
    credentialled(mode_for=client_name)
    async with connected(client_name=client_name) as session:
        assert "place_order" in await session.tool_names()
        store.write(
            {
                "DELTA_MCP_ENV": "india_prod",
                "DELTA_API_KEY": "externally-rotated-key",
                "DELTA_API_SECRET": "externally-rotated-secret",
                config_mod.mode_key(client_name): "trade",
            }
        )

        status = await session.call("get_connection_status")
        assert status["environment"] == "india_prod"
        assert status["mode"] == "read"
        assert status["mode_after_restart"] == "trade"
        assert status["restart_required"] is True
        assert "place_order" not in await session.tool_names()
        assert "get_positions" in await session.tool_names()

    transitions = capsys.readouterr().err
    assert "runtime transition=trade-disarmed-identity-change" in transitions
    assert "identity-rebound" in transitions
    assert "env=india_prod mode=read surface=market+account" in transitions
    assert "externally-rotated" not in transitions


async def test_mode_only_save_reports_when_trading_is_already_live():
    """Re-saving the current choice must not tell someone to restart a live surface."""
    credentialled(mode_for="Claude Desktop")
    async with connected(client_name="Claude Desktop") as session:
        assert "place_order" in await session.tool_names()
        grant = await session.open_form()
        result = await session.call("save_mode", mode="trade", grant=grant)
        assert result["status"] == "saved"
        assert "Trading is live" in result["next_step"]
        assert result["effective_mode"] == "trade"
        assert (await session.call("get_connection_status"))["restart_required"] is False


async def test_process_mode_override_is_reported_with_the_effective_mode(
    accepted, monkeypatch
):
    """The form shows what will run, even when the client's config beats its selection."""
    monkeypatch.setenv("DELTA_MCP_MODE", "trade")
    async with connected(client_name="Claude Desktop") as session:
        result = await save(session, mode="read")
        assert result["status"] == "overridden"
        assert result["effective_mode"] == "trade"
        assert result["mode_setting"] == config_mod.mode_key("Claude Desktop")
        assert "DELTA_MCP_MODE" in result["message"]
        assert "future sessions trade" in result["next_step"]
        assert "Restart this app to turn trading on" not in result["message"]
        assert "get_positions" in await session.tool_names()
        assert "place_order" not in await session.tool_names()
        status = await session.call("get_connection_status")
        assert status["restart_required"] is True
        assert status["overridden_by_client"] == ["DELTA_MCP_MODE"]


async def test_a_concurrent_full_save_is_reported_as_superseded(accepted, monkeypatch):
    """Never claim checked account A is live after another process publishes account B."""
    real_save = credentials.save

    def save_then_supersede(env, key, secret, client="", mode=""):
        problem = real_save(env, key, secret, client, mode)
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
        assert "newer settings" in result["message"]
        status = await session.call("get_connection_status")
        assert status["environment"] == "india_prod"
        assert session.server.live_client.config.api_key == "newer-client-key"


async def test_punctuation_variants_do_not_inherit_each_others_trading_choice():
    """The readable slug may match, but the exact handshake name is the binding."""
    credentialled(mode_for="claude-ai")
    async with connected(client_name="Claude AI") as session:
        assert "place_order" not in await session.tool_names()


async def test_choosing_trade_does_not_arm_it_in_the_session_that_chose_it(accepted):
    """The restart is the point. Order placement appearing mid-conversation, in the same
    turn that asked for it, is exactly what the whole gate exists to prevent.
    """
    async with connected(client_name="Claude Desktop") as session:
        grant = await session.open_form()
        result = await session.call(
            "save_credentials",
            environment="india_testnet",
            api_key=KEY,
            api_secret=SECRET,
            mode="trade",
            grant=grant,
        )
        assert result["status"] == "saved"
        assert "Restart this app to turn trading on" in result["message"]

        # The reads came up; the mutations did not.
        assert "get_positions" in await session.tool_names()
        assert "place_order" not in await session.tool_names()

        status = await session.call("get_connection_status")
        assert status["mode"] == "read"
        assert status["mode_after_restart"] == "trade"
        # It must not claim everything is done while trading still waits.
        assert status["restart_required"] is True

    # The written entitlement is what the next start reads.
    assert store.read()[config_mod.mode_key("Claude Desktop")] == "trade"
    async with connected(client_name="Claude Desktop") as session:
        assert "place_order" in await session.tool_names()


async def test_a_punctuation_only_client_name_gets_a_distinct_safe_binding(accepted):
    """The digest binds even names whose readable slug has no letters or digits."""
    async with connected(client_name="!!!") as session:
        grant = await session.open_form()
        result = await session.call(
            "save_credentials",
            environment="india_testnet",
            api_key=KEY,
            api_secret=SECRET,
            mode="trade",
            grant=grant,
        )
        assert result["status"] == "saved"
        assert result["mode_setting"] == config_mod.mode_key("!!!")
        assert store.read()[config_mod.mode_key("!!!")] == "trade"


async def test_a_client_env_var_still_outranks_the_scoped_setting(monkeypatch):
    """Editing the client's own config stays the most deliberate thing anyone can do."""
    credentialled(mode_for="Claude Desktop")
    monkeypatch.setenv("DELTA_MCP_MODE", "read")
    async with connected(client_name="Claude Desktop") as session:
        assert "place_order" not in await session.tool_names()


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


async def test_a_key_without_read_data_says_which_permission_to_add(monkeypatch):
    monkeypatch.setattr(credentials, "check", rejecting("unauthorized_api_access"))
    async with connected() as session:
        assert "Read Data" in (await save(session))["message"]


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
