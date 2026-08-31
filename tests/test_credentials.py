"""The real `check`, against real response shapes.

Every other suite monkeypatches `credentials.check` wholesale, which replaces the parsing
along with the network call — so nothing exercised what it does with an actual body. That
is how it came to read the account name off the wrong level of the envelope and report
every successful save as an unnamed "Connected."
"""

import httpx
import respx

from delta_exchange_mcp import credentials, store
from delta_exchange_mcp import config as config_mod
from delta_exchange_mcp.config import INDIA_TESTNET_REST

KEY = "a-key"
SECRET = "a-secret"


@respx.mock
async def test_a_working_key_reports_the_account_it_belongs_to():
    """The name is the only signal separating "saved" from "saved the wrong key"."""
    respx.get(f"{INDIA_TESTNET_REST}/profile").mock(
        # Delta's envelope, which the client deliberately keeps rather than unwrapping.
        return_value=httpx.Response(
            200, json={"success": True, "result": {"email": "someone@delta.exchange"}}
        )
    )
    result = await credentials.check("india_testnet", KEY, SECRET)
    assert (result.ok, result.reachable) == (True, True)
    assert result.detail == "someone@delta.exchange"


@respx.mock
async def test_an_account_without_an_email_falls_back_to_its_id():
    respx.get(f"{INDIA_TESTNET_REST}/profile").mock(
        return_value=httpx.Response(200, json={"success": True, "result": {"id": 82373749}})
    )
    assert (await credentials.check("india_testnet", KEY, SECRET)).detail == "82373749"


@respx.mock
async def test_a_key_delta_has_never_seen_is_rejected_with_its_code():
    """The code is what the form branches on to decide whether to mention the other site."""
    respx.get(f"{INDIA_TESTNET_REST}/profile").mock(
        return_value=httpx.Response(
            401, json={"success": False, "error": {"code": "invalid_api_key"}}
        )
    )
    result = await credentials.check("india_testnet", KEY, SECRET)
    assert (result.ok, result.reachable) == (False, True)
    assert result.code == "invalid_api_key"


@respx.mock
async def test_an_unreachable_api_is_not_a_rejection():
    """These call for opposite responses, so they must not collapse into one another."""
    respx.get(f"{INDIA_TESTNET_REST}/profile").mock(
        side_effect=httpx.ConnectError("no route to host")
    )
    result = await credentials.check("india_testnet", KEY, SECRET)
    assert (result.ok, result.reachable) == (False, False)
    assert result.code == ""


def test_override_detection_uses_one_store_snapshot(monkeypatch):
    """An atomic file replacement is not evidence of a process-environment override."""
    for name in ("DELTA_MCP_ENV", "DELTA_API_KEY", "DELTA_API_SECRET"):
        monkeypatch.delenv(name, raising=False)
    before = {
        "DELTA_MCP_ENV": "india_testnet",
        "DELTA_API_KEY": "before-key",
        "DELTA_API_SECRET": "before-secret",
    }
    after = {
        "DELTA_MCP_ENV": "india_prod",
        "DELTA_API_KEY": "after-key",
        "DELTA_API_SECRET": "after-secret",
    }
    reads = 0

    def changing_store():
        nonlocal reads
        reads += 1
        return before if reads == 1 else after

    monkeypatch.setattr(store, "read", changing_store)

    assert credentials.overridden_by_client() == []
    assert reads == 1
    assert config_mod.load(before).api_key == "before-key"


def test_a_client_key_is_reported_even_when_the_file_holds_none(monkeypatch):
    """The first save is exactly when the file is empty, so this is the common case.

    Every other test here fills the file first, which is how a test for a value the file
    does not hold came to be missing. A client supplying its own key leaves a field that
    looks editable: someone types a key, the form checks it against Delta and names the
    account back to them, and the server signs every request with the client's key.
    """
    monkeypatch.setenv("DELTA_API_KEY", "from-the-clients-own-config")
    monkeypatch.setenv("DELTA_API_SECRET", "also-from-the-client")
    store.write({"DELTA_API_KEY": "", "DELTA_API_SECRET": ""})

    assert credentials.overridden_by_client() == ["DELTA_API_KEY", "DELTA_API_SECRET"]
    # And it stays true of the value that would actually be signed with.
    store.write({"DELTA_API_KEY": KEY, "DELTA_API_SECRET": SECRET})
    assert config_mod.load(store.read()).api_key == "from-the-clients-own-config"


def test_a_client_supplying_only_the_key_locks_the_secret_too(monkeypatch):
    """Both names or neither, because `config` reads both from whichever source has either.

    A client naming only DELTA_API_KEY also decides the secret, and the secret it decides
    is nothing at all. Reporting the key alone would leave the secret field editable while
    nothing typed into it can ever be used, and every signed request then fails against a
    form that said the account was connected.
    """
    monkeypatch.setenv("DELTA_API_KEY", "from-the-clients-own-config")
    monkeypatch.delenv("DELTA_API_SECRET", raising=False)
    store.write({"DELTA_API_KEY": KEY, "DELTA_API_SECRET": SECRET})

    assert credentials.overridden_by_client() == ["DELTA_API_KEY", "DELTA_API_SECRET"]
    live = config_mod.load(store.read())
    assert (live.api_key, live.api_secret) == ("from-the-clients-own-config", None)


def test_a_client_passing_back_the_saved_key_is_not_reported(monkeypatch):
    """Same value on both sides changes no outcome, and saying so would only alarm.

    This is the credential twin of the Cursor case that keeps DELTA_MCP_ENV quiet: a client
    that re-supplies exactly what the file holds overrides nothing anyone can observe.
    """
    monkeypatch.setenv("DELTA_API_KEY", KEY)
    monkeypatch.setenv("DELTA_API_SECRET", SECRET)
    store.write({"DELTA_API_KEY": KEY, "DELTA_API_SECRET": SECRET})

    assert credentials.overridden_by_client() == []


def test_the_built_in_default_is_not_a_client_override(monkeypatch):
    """Nothing set anywhere resolves to india_prod, and that is a default, not a client.

    Comparing the resolved value against the file would report this one, because the file
    holds nothing and the resolved value is india_prod. Reading the process environment
    keeps the two apart.
    """
    for name in ("DELTA_MCP_ENV", "DELTA_API_KEY", "DELTA_API_SECRET"):
        monkeypatch.delenv(name, raising=False)
    store.write({"DELTA_API_KEY": KEY, "DELTA_API_SECRET": SECRET})

    assert config_mod.load(store.read()).env == config_mod.DEFAULT_ENV
    assert credentials.overridden_by_client() == []
