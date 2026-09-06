"""Candidate validation against real Delta response shapes."""

import httpx
import respx

from delta_exchange_mcp import credentials
from delta_exchange_mcp.config import INDIA_TESTNET_REST
from delta_exchange_mcp.errors import DeltaApiError, is_auth_failure, is_permission_failure

KEY = "a-key"
SECRET = "a-secret"


@respx.mock
async def test_a_working_key_reports_the_account_id_it_belongs_to() -> None:
    respx.get(f"{INDIA_TESTNET_REST}/users/trading_preferences").mock(
        return_value=httpx.Response(
            200,
            json={"success": True, "result": {"user_id": 82373749}},
        )
    )
    result = await credentials.check("india_testnet", KEY, SECRET)
    assert (result.ok, result.reachable, result.detail) == (
        True,
        True,
        "82373749",
    )


@respx.mock
async def test_an_invalid_identity_response_is_inconclusive() -> None:
    respx.get(f"{INDIA_TESTNET_REST}/users/trading_preferences").mock(
        return_value=httpx.Response(200, json={"success": True, "result": {}})
    )
    result = await credentials.check("india_testnet", KEY, SECRET)
    assert (result.ok, result.reachable) == (False, False)
    assert "result.user_id" in result.detail


@respx.mock
async def test_an_invalid_key_preserves_its_decisive_code() -> None:
    respx.get(f"{INDIA_TESTNET_REST}/users/trading_preferences").mock(
        return_value=httpx.Response(
            401,
            json={"success": False, "error": {"code": "invalid_api_key"}},
        )
    )
    result = await credentials.check("india_testnet", KEY, SECRET)
    assert (result.ok, result.reachable, result.code) == (
        False,
        True,
        "invalid_api_key",
    )


@respx.mock
async def test_a_permission_failure_is_not_an_invalid_credential() -> None:
    respx.get(f"{INDIA_TESTNET_REST}/users/trading_preferences").mock(
        return_value=httpx.Response(
            401,
            json={
                "success": False,
                "error": {"code": "UnauthorizedApiAccess"},
            },
        )
    )
    result = await credentials.check("india_testnet", KEY, SECRET)
    error = DeltaApiError(result.code)
    assert (result.ok, result.reachable) == (False, True)
    assert result.detail == credentials.TRADING_PREFERENCES_PERMISSION_MESSAGE
    assert is_permission_failure(error)
    assert not is_auth_failure(error)


@respx.mock
async def test_an_unreachable_api_is_inconclusive() -> None:
    respx.get(f"{INDIA_TESTNET_REST}/users/trading_preferences").mock(
        side_effect=httpx.ConnectError("no route to host")
    )
    result = await credentials.check("india_testnet", KEY, SECRET)
    assert (result.ok, result.reachable, result.code) == (False, False, "")
