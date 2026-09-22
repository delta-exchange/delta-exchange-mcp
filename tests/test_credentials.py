"""The real `check`, against real response shapes.

Every other suite monkeypatches `credentials.check` wholesale, which replaces the parsing
along with the network call — so nothing exercised what it does with an actual body. That
is how it came to read the account name off the wrong level of the envelope and report
every successful save as an unnamed "Connected."
"""

import httpx
import respx

from delta_exchange_mcp import credentials
from delta_exchange_mcp.config import INDIA_TESTNET_REST

KEY = "a-key"
SECRET = "a-secret"


@respx.mock
async def test_a_working_key_is_accepted():
    respx.get(f"{INDIA_TESTNET_REST}/wallet/balances").mock(
        return_value=httpx.Response(200, json={"success": True, "result": []})
    )
    result = await credentials.check("india_testnet", KEY, SECRET)
    assert (result.ok, result.reachable) == (True, True)
    assert result.detail == ""


@respx.mock
async def test_a_key_is_never_checked_against_profile():
    """Delta no longer serves /profile to API keys, so a probe there rejects every key."""
    profile = respx.get(f"{INDIA_TESTNET_REST}/profile").mock(
        return_value=httpx.Response(403, json={"success": False, "error": {"code": "forbidden"}})
    )
    respx.get(f"{INDIA_TESTNET_REST}/wallet/balances").mock(
        return_value=httpx.Response(200, json={"success": True, "result": []})
    )
    assert (await credentials.check("india_testnet", KEY, SECRET)).ok is True
    assert not profile.called


@respx.mock
async def test_a_key_delta_has_never_seen_is_rejected_with_its_code():
    """The code is what the form branches on to decide whether to mention the other site."""
    respx.get(f"{INDIA_TESTNET_REST}/wallet/balances").mock(
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
    respx.get(f"{INDIA_TESTNET_REST}/wallet/balances").mock(
        side_effect=httpx.ConnectError("no route to host")
    )
    result = await credentials.check("india_testnet", KEY, SECRET)
    assert (result.ok, result.reachable) == (False, False)
    assert result.code == ""


