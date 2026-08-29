import httpx
import pytest
import respx

from delta_exchange_mcp.client import DeltaClient
from delta_exchange_mcp.config import INDIA_TESTNET_REST, Config
from delta_exchange_mcp.identity import InvalidIdentityResponse, fetch_account_identity


def _client() -> DeltaClient:
    return DeltaClient(
        Config(
            env="india_testnet",
            base_url=INDIA_TESTNET_REST,
            api_key="key",
            api_secret="secret",
        )
    )


@respx.mock
async def test_fetches_the_api_key_account_identity():
    response = {
        "success": True,
        "result": {"user_id": 57354187, "default_auto_topup": True},
    }
    route = respx.get(f"{INDIA_TESTNET_REST}/users/trading_preferences").mock(
        return_value=httpx.Response(200, json=response)
    )

    identity = await fetch_account_identity(_client())

    assert identity.user_id == 57354187
    assert identity.response == {"result": response["result"], "meta": None}
    assert route.called


@pytest.mark.parametrize(
    "response",
    [
        [],
        {},
        {"result": []},
        {"result": {}},
        {"result": {"user_id": True}},
        {"result": {"user_id": "57354187"}},
    ],
)
@respx.mock
async def test_rejects_a_response_without_an_integer_user_id(response):
    respx.get(f"{INDIA_TESTNET_REST}/users/trading_preferences").mock(
        return_value=httpx.Response(200, json=response)
    )

    with pytest.raises(InvalidIdentityResponse):
        await fetch_account_identity(_client())
