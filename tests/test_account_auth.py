"""Auth plumbing: signing path, headers, and documented error mapping."""

import asyncio
import hashlib
import hmac

import httpx
import pytest
import respx

from delta_exchange_mcp.client import DeltaClient, sign
from delta_exchange_mcp.config import INDIA_PROD_REST, INDIA_TESTNET_REST, Config
from delta_exchange_mcp.errors import DeltaApiError


def _client_with_creds() -> DeltaClient:
    cfg = Config(
        env="india_testnet", base_url=INDIA_TESTNET_REST, api_key="k1", api_secret="s1"
    )
    return DeltaClient(cfg)


def test_sign_matches_hmac_sha256_spec():
    expected = hmac.new(b"s1", b"GET1600000000/v2/wallet/balances", hashlib.sha256).hexdigest()
    assert sign("s1", "GET", "1600000000", "/v2/wallet/balances", "", "") == expected


@pytest.mark.asyncio
@respx.mock
async def test_authenticated_request_sends_signed_headers():
    route = respx.get(f"{INDIA_TESTNET_REST}/wallet/balances").mock(
        return_value=httpx.Response(200, json={"success": True, "result": []})
    )
    client = _client_with_creds()
    await client.get("/wallet/balances", auth=True)

    assert route.called
    req = route.calls[0].request
    assert req.headers.get("api-key") == "k1"
    assert req.headers.get("timestamp") is not None
    sig = req.headers.get("signature")
    assert sig and len(sig) == 64


@pytest.mark.asyncio
@respx.mock
async def test_signing_payload_includes_v2_prefix():
    """Delta signs the FULL path including /v2 — see slate _authentication.md example."""
    route = respx.get(f"{INDIA_TESTNET_REST}/wallet/balances").mock(
        return_value=httpx.Response(200, json={"success": True, "result": []})
    )
    client = _client_with_creds()
    await client.get("/wallet/balances", auth=True)

    req = route.calls[0].request
    ts = req.headers["timestamp"]
    expected_payload = f"GET{ts}/v2/wallet/balances"
    expected_sig = hmac.new(b"s1", expected_payload.encode(), hashlib.sha256).hexdigest()
    assert req.headers["signature"] == expected_sig


@pytest.mark.asyncio
@respx.mock
async def test_signing_payload_includes_query_string():
    route = respx.get(f"{INDIA_TESTNET_REST}/orders").mock(
        return_value=httpx.Response(200, json={"success": True, "result": []})
    )
    client = _client_with_creds()
    await client.get("/orders", params={"product_id": 1, "states": "open"}, auth=True)

    req = route.calls[0].request
    ts = req.headers["timestamp"]
    qs = "?product_id=1&states=open"
    expected_sig = hmac.new(
        b"s1", f"GET{ts}/v2/orders{qs}".encode(), hashlib.sha256
    ).hexdigest()
    assert req.headers["signature"] == expected_sig


@pytest.mark.asyncio
async def test_auth_required_without_creds_raises():
    cfg = Config(env="india_testnet", base_url=INDIA_TESTNET_REST)
    client = DeltaClient(cfg)
    with pytest.raises(DeltaApiError, match="credentials_missing"):
        await client.get("/wallet/balances", auth=True)


@pytest.mark.asyncio
async def test_an_in_flight_request_keeps_one_coherent_state_during_rebind():
    """A hot save cannot mix the old URL with the new key or close the old transport."""
    entered = asyncio.Event()
    release = asyncio.Event()
    seen = {}

    async def handle(request: httpx.Request) -> httpx.Response:
        entered.set()
        await release.wait()
        seen["url"] = str(request.url)
        seen["key"] = request.headers["api-key"]
        return httpx.Response(200, json={"success": True, "result": []})

    transport = httpx.AsyncClient(
        base_url=INDIA_TESTNET_REST, transport=httpx.MockTransport(handle)
    )
    client = DeltaClient(
        Config(
            env="india_testnet",
            base_url=INDIA_TESTNET_REST,
            api_key="old-key",
            api_secret="old-secret",
        ),
        http=transport,
    )
    request = asyncio.create_task(client.get("/wallet/balances", auth=True))
    await entered.wait()
    client.rebind(
        Config(
            env="india_prod",
            base_url=INDIA_PROD_REST,
            api_key="new-key",
            api_secret="new-secret",
        )
    )
    release.set()
    await request
    assert transport.is_closed
    assert client._retired == {}
    await client.aclose()

    assert seen == {
        "url": f"{INDIA_TESTNET_REST}/wallet/balances",
        "key": "old-key",
    }


@pytest.mark.asyncio
async def test_idle_rebinds_close_retired_transports_promptly():
    client = DeltaClient(
        Config(env="india_testnet", base_url=INDIA_TESTNET_REST)
    )

    for index in range(250):
        client.rebind(
            Config(
                env="india_testnet",
                base_url=f"https://example-{index}.invalid/v2",
            )
        )
    await asyncio.sleep(0)

    assert client._retired == {}
    await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_invalid_api_key_message_hints_env():
    respx.get(f"{INDIA_TESTNET_REST}/wallet/balances").mock(
        return_value=httpx.Response(
            401, json={"success": False, "error": {"code": "InvalidApiKey"}}
        )
    )
    client = _client_with_creds()
    with pytest.raises(DeltaApiError) as exc:
        await client.get("/wallet/balances", auth=True)
    assert exc.value.code == "InvalidApiKey"
    assert "DELTA_MCP_ENV" in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["UnauthorizedApiAccess", "unauthorized_api_access"])
@respx.mock
async def test_account_permission_error_does_not_name_the_validation_endpoint(
    code: str,
) -> None:
    respx.get(f"{INDIA_TESTNET_REST}/wallet/balances").mock(
        return_value=httpx.Response(
            403,
            json={"success": False, "error": {"code": code}},
        )
    )

    with pytest.raises(DeltaApiError) as exc:
        await _client_with_creds().get("/wallet/balances", auth=True)

    message = str(exc.value)
    assert "lacks permission for this endpoint" in message
    assert "trading preferences" not in message


@pytest.mark.asyncio
@respx.mock
async def test_ip_not_whitelisted_includes_ip_in_message():
    respx.get(f"{INDIA_TESTNET_REST}/wallet/balances").mock(
        return_value=httpx.Response(
            403,
            json={
                "success": False,
                "error": {
                    "code": "ip_not_whitelisted_for_api_key",
                    "context": {"ip": "1.2.3.4"},
                },
            },
        )
    )
    client = _client_with_creds()
    with pytest.raises(DeltaApiError) as exc:
        await client.get("/wallet/balances", auth=True)
    assert "1.2.3.4" in str(exc.value)
    assert "whitelist" in str(exc.value).lower()


@pytest.mark.asyncio
@respx.mock
async def test_signature_expired_message_hints_clock_sync():
    respx.get(f"{INDIA_TESTNET_REST}/wallet/balances").mock(
        return_value=httpx.Response(
            401, json={"success": False, "error": {"code": "SignatureExpired"}}
        )
    )
    client = _client_with_creds()
    with pytest.raises(DeltaApiError) as exc:
        await client.get("/wallet/balances", auth=True)
    assert "clock" in str(exc.value).lower() or "ntp" in str(exc.value).lower()
