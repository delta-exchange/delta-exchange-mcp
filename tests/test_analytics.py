"""What the server tells Delta about the client and tool behind each API request."""

import json
import re
from pathlib import Path

import httpx
import mcp.types as types
import respx

from delta_exchange_mcp import analytics
from delta_exchange_mcp import config as config_mod
from delta_exchange_mcp.version import PACKAGE_VERSION
from tests.test_activation import connected

KEY = "env-key-never-forwarded"
SECRET = "env-secret-never-forwarded"
README = Path(__file__).parent.parent / "README.md"


def params(name="probe", version="1", protocol="2025-06-18", **capabilities):
    return types.InitializeRequestParams(
        protocolVersion=protocol,
        capabilities=types.ClientCapabilities(**capabilities),
        clientInfo=types.Implementation(name=name, version=version),
    )


def forwarded(request):
    return {k: v for k, v in request.headers.items() if k.lower().startswith("x-delta-mcp-")}


@respx.mock
async def test_a_tool_call_names_the_client_and_tool(monkeypatch):
    monkeypatch.setenv("DELTA_MCP_ENV", "india_testnet")
    route = respx.get(f"{config_mod.INDIA_TESTNET_REST}/tickers/BTCUSD").mock(
        return_value=httpx.Response(200, json={"success": True, "result": {}})
    )
    async with connected(client_name="probe-client") as session:
        await session.call("get_ticker", symbol="BTCUSD")
        negotiated = str(session.initialized.protocolVersion)

    sent = route.calls.last.request.headers
    assert sent["Source"].startswith("delta-exchange-mcp/")
    assert sent["X-Delta-MCP-Version"] == PACKAGE_VERSION
    assert sent["X-Delta-MCP-Client"] == "probe-client"
    assert sent["X-Delta-MCP-Client-Version"] == "1"
    assert sent["X-Delta-MCP-Tool"] == "get_ticker"
    assert sent["X-Delta-MCP-Protocol"] == negotiated
    assert {"platform", "python"} <= json.loads(sent["X-Delta-MCP-Context"]).keys()


async def test_an_unsupported_protocol_reports_the_version_the_server_answered():
    with analytics.scope(params(protocol="1999-01-01"), "get_ticker"):
        assert analytics.headers()["X-Delta-MCP-Protocol"] == types.LATEST_PROTOCOL_VERSION


@respx.mock
async def test_nothing_credential_shaped_is_ever_forwarded(monkeypatch):
    monkeypatch.setenv("DELTA_MCP_ENV", "india_testnet")
    monkeypatch.setenv("DELTA_API_KEY", KEY)
    monkeypatch.setenv("DELTA_API_SECRET", SECRET)
    route = respx.get(f"{config_mod.INDIA_TESTNET_REST}/users/trading_preferences").mock(
        return_value=httpx.Response(200, json={"success": True, "result": {"user_id": 7}})
    )
    async with connected(client_name="probe-client") as session:
        await session.call("get_trading_preferences")

    request = route.calls.last.request
    leaked = {KEY, SECRET, request.headers["signature"], request.headers["timestamp"]}
    for value in forwarded(request).values():
        assert not any(secret in value for secret in leaked)

    with analytics.scope(params(), "get_ticker"):
        analytics.headers()["api-key"] = KEY
        assert "api-key" not in analytics.headers()


async def test_a_client_name_cannot_forge_a_header():
    hostile = params(name="evil\r\nX-Injected: 1", version="Клод\x00")
    with analytics.scope(hostile, "get_ticker"):
        sent = httpx.Request("GET", "https://example.test", headers=analytics.headers())

    assert "x-injected" not in sent.headers
    for value in forwarded(sent).values():
        assert value.isascii() and value.isprintable()


async def test_the_header_set_stays_under_4096_bytes():
    huge = params(
        name="☃" * 5000,
        version="☃" * 5000,
        experimental={f"ext{i}": {} for i in range(500)},
        sampling=types.SamplingCapability(),
        roots=types.RootsCapability(),
    )
    with analytics.scope(huge, "x" * 5000):
        sent = analytics.headers()

    assert sum(len(k) + len(v) + 4 for k, v in sent.items()) <= 4096
    assert "ext0" not in sent["X-Delta-MCP-Context"]


async def test_a_request_outside_a_tool_call_still_names_the_package():
    assert analytics.headers() == {"X-Delta-MCP-Version": PACKAGE_VERSION}


async def test_the_readme_lists_exactly_what_gets_forwarded():
    with analytics.scope(params(roots=types.RootsCapability()), "get_ticker"):
        sent = set(analytics.headers())
    documented = set(re.findall(r"^\| `(X-Delta-MCP-[\w-]+)` \|", README.read_text(), re.M))
    assert documented == sent
