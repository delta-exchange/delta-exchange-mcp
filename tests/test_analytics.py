"""Client and tool analytics on the MCP-to-Delta request path."""

import asyncio
import json
import re
from pathlib import Path

import httpx
import pytest
import respx
from mcp import types
from mcp.server.mcpserver import Context

from delta_exchange_mcp import analytics
from delta_exchange_mcp.client import DeltaClient
from delta_exchange_mcp.config import INDIA_TESTNET_REST, Config
from delta_exchange_mcp.server import build_server
from delta_exchange_mcp.version import PACKAGE_VERSION

from .test_activation import connected, connection_service

TICKER = {"success": True, "result": {"symbol": "BTCUSD", "mark_price": "1"}}


@pytest.fixture(autouse=True)
def testnet_everywhere(monkeypatch):
    monkeypatch.setenv("DELTA_MCP_ENV", "india_testnet")
    monkeypatch.delenv("DELTA_API_KEY", raising=False)
    monkeypatch.delenv("DELTA_API_SECRET", raising=False)
    monkeypatch.delenv("DELTA_MCP_MODE", raising=False)


async def call_ticker(client_info: types.Implementation) -> httpx.Headers:
    route = respx.get(f"{INDIA_TESTNET_REST}/tickers/BTCUSD").mock(
        return_value=httpx.Response(200, json=TICKER)
    )
    app = build_server(connection_service=connection_service())
    try:
        async with connected(
            app,
            mode="2026-07-28",
            client_info=client_info,
        ) as client:
            await client.call_tool("get_ticker", {"symbol": "BTCUSD"})
    finally:
        await app.close_live_client()
    assert route.called
    return route.calls[-1].request.headers


@respx.mock
async def test_request_identifies_the_exact_client_and_tool() -> None:
    sent = await call_ticker(
        types.Implementation(
            name="claude-ai (via mcp-remote 0.1.37)",
            version="1.30096.5",
            title="Claude",
        )
    )

    assert sent[f"{analytics.PREFIX}Client"] == (
        "claude-ai%20(via%20mcp-remote%200.1.37)"
    )
    assert sent[f"{analytics.PREFIX}Client-Version"] == "1.30096.5"
    assert sent[f"{analytics.PREFIX}Tool"] == "get_ticker"
    assert sent[f"{analytics.PREFIX}Version"] == PACKAGE_VERSION
    assert sent[f"{analytics.PREFIX}Protocol"] == "2026-07-28"
    assert json.loads(sent[analytics.CONTEXT_HEADER])["title"] == "Claude"
    assert f"{analytics.PREFIX}Env" not in sent
    assert f"{analytics.PREFIX}Mode" not in sent
    assert f"{analytics.PREFIX}Session" not in sent


@respx.mock
async def test_concurrent_clients_keep_exact_request_identity() -> None:
    seen: list[tuple[str, str]] = []
    both_started = asyncio.Event()

    async def respond(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                request.headers[f"{analytics.PREFIX}Client"],
                request.headers[f"{analytics.PREFIX}Tool"],
            )
        )
        if len(seen) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=2)
        return httpx.Response(200, json=TICKER)

    respx.get(f"{INDIA_TESTNET_REST}/tickers/BTCUSD").mock(side_effect=respond)
    app = build_server(connection_service=connection_service())
    try:
        async with (
            connected(
                app,
                mode="2026-07-28",
                client_info=types.Implementation(name="client-a", version="1"),
            ) as first,
            connected(
                app,
                mode="2026-07-28",
                client_info=types.Implementation(name="client-b", version="2"),
            ) as second,
        ):
            await asyncio.gather(
                first.call_tool("get_ticker", {"symbol": "BTCUSD"}),
                second.call_tool("get_ticker", {"symbol": "BTCUSD"}),
            )
    finally:
        await app.close_live_client()

    assert set(seen) == {("client-a", "get_ticker"), ("client-b", "get_ticker")}


@respx.mock
async def test_client_name_cannot_inject_an_http_header() -> None:
    sent = await call_ticker(
        types.Implementation(name="evil\r\napi-key: stolen", version="1")
    )

    reported = sent[f"{analytics.PREFIX}Client"]
    assert "\r" not in reported and "\n" not in reported
    assert "%0D%0A" in reported
    assert sent.get("api-key") is None


@pytest.mark.parametrize(
    "client_name",
    [" leading", "trailing ", f"{'a' * 199} tail"],
)
async def test_client_name_spaces_survive_real_http_serialization(
    client_name: str,
) -> None:
    requests: list[bytes] = []

    async def accept(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        requests.append(await reader.readuntil(b"\r\n\r\n"))
        body = json.dumps(TICKER).encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = DeltaClient(
        Config(
            env="india_testnet",
            base_url=f"http://127.0.0.1:{port}/v2",
        )
    )
    token = analytics._current.set(
        analytics._Call(client_name=client_name, tool="get_ticker")
    )
    try:
        async with server:
            await client.get("/tickers/BTCUSD")
    finally:
        analytics._current.reset(token)
        await client.aclose()

    assert len(requests) == 1
    expected = analytics.clean(client_name)
    assert expected
    assert not expected.startswith(" ")
    assert not expected.endswith(" ")
    header = next(
        line
        for line in requests[0].split(b"\r\n")
        if line.lower().startswith(b"x-delta-mcp-client:")
    )
    assert header.endswith(expected.encode())


@respx.mock
async def test_credentials_never_enter_analytics_headers() -> None:
    cfg = Config(
        env="india_testnet",
        base_url=INDIA_TESTNET_REST,
        api_key="key-that-must-not-travel",
        api_secret="secret-that-must-not-travel",
    )
    route = respx.get(f"{INDIA_TESTNET_REST}/positions").mock(
        return_value=httpx.Response(200, json={"success": True, "result": []})
    )
    client = DeltaClient(cfg)
    try:
        with analytics.scope(Context(), "get_positions"):
            await client.get("/positions", auth=True)
    finally:
        await client.aclose()

    ours = {
        name: value
        for name, value in route.calls[-1].request.headers.items()
        if name.lower().startswith(analytics.PREFIX.lower())
    }
    rendered = " ".join(ours.values())
    assert "key-that-must-not-travel" not in rendered
    assert "secret-that-must-not-travel" not in rendered
    assert "signature" not in rendered.lower()


@respx.mock
async def test_long_client_description_cannot_break_the_request() -> None:
    sent = await call_ticker(
        types.Implementation(
            name="verbose",
            version="1",
            title="😀" * 5000,
            description="😀" * 5000,
            website_url="https://example.test",
        )
    )

    total = sum(
        len(name) + len(value) + 4
        for name, value in sent.items()
        if name.lower().startswith(analytics.PREFIX.lower())
    )
    assert total <= analytics.BUDGET_BYTES
    assert sent[f"{analytics.PREFIX}Client"] == "verbose"
    assert sent[f"{analytics.PREFIX}Tool"] == "get_ticker"
    assert json.loads(sent[analytics.CONTEXT_HEADER])


@respx.mock
async def test_context_header_is_directly_parseable_json() -> None:
    sent = await call_ticker(
        types.Implementation(
            name="punctuation",
            version="1",
            title='he said "hi" \\ and left\nthen returned',
        )
    )

    raw = sent[analytics.CONTEXT_HEADER]
    assert "%5C" not in raw
    assert raw.isascii() and raw.isprintable()
    assert json.loads(raw)["title"].startswith('he said "hi"')


def test_context_header_rejects_unprintable_output(monkeypatch) -> None:
    monkeypatch.setattr(analytics.json, "dumps", lambda *args, **kwargs: '{"x":"a\nb"}')
    assert analytics.as_header({"x": "anything"}) == ""


def test_open_capability_settings_are_not_forwarded() -> None:
    capabilities = types.ClientCapabilities(
        experimental={"private-extension": {"api_key": "client-secret-value"}},
        extensions={"another-private-extension": {"token": "private-token"}},
        roots=types.RootsCapability(listChanged=True),
    )

    projected = analytics._capabilities(capabilities)
    rendered = json.dumps(projected)
    assert projected == {"roots": True, "experimental": 1, "extensions": 1}
    assert "private-extension" not in rendered
    assert "client-secret-value" not in rendered
    assert "private-token" not in rendered


@respx.mock
async def test_encoded_client_fields_stay_bounded() -> None:
    sent = await call_ticker(types.Implementation(name="😀" * 200, version="😀" * 200))

    for header in (f"{analytics.PREFIX}Client", f"{analytics.PREFIX}Client-Version"):
        assert len(sent[header]) <= analytics.FIELD_LIMIT
        assert not re.search(r"%[0-9A-Fa-f]?$", sent[header])


@respx.mock
async def test_unpaired_surrogate_in_client_name_does_not_fail_the_call() -> None:
    sent = await call_ticker(types.Implementation(name="bad\ud800name", version="1"))
    assert sent[f"{analytics.PREFIX}Tool"] == "get_ticker"
    assert "bad" in sent[f"{analytics.PREFIX}Client"]


@respx.mock
async def test_analytics_context_does_not_leak_after_tool_call() -> None:
    route = respx.get(f"{INDIA_TESTNET_REST}/tickers/BTCUSD").mock(
        return_value=httpx.Response(200, json=TICKER)
    )
    app = build_server(connection_service=connection_service())
    try:
        async with connected(
            app,
            mode="2026-07-28",
            client_info=types.Implementation(name="one-call", version="1"),
        ) as client:
            await client.call_tool("get_ticker", {"symbol": "BTCUSD"})
            await app.live_client.get("/tickers/BTCUSD")
    finally:
        await app.close_live_client()

    first, second = (call.request.headers for call in route.calls[-2:])
    assert first[f"{analytics.PREFIX}Client"] == "one-call"
    assert first[f"{analytics.PREFIX}Tool"] == "get_ticker"
    assert f"{analytics.PREFIX}Client" not in second
    assert f"{analytics.PREFIX}Tool" not in second
    assert second[f"{analytics.PREFIX}Version"] == PACKAGE_VERSION


@respx.mock
async def test_readme_discloses_every_analytics_header() -> None:
    sent = await call_ticker(
        types.Implementation(name="documented-client", version="1", title="Client")
    )
    forwarded = {
        name for name in sent if name.lower().startswith(analytics.PREFIX.lower())
    }
    readme = Path(__file__).resolve().parents[1] / "README.md"
    documented = set(re.findall(r"`(X-Delta-MCP-[A-Za-z-]+)`", readme.read_text()))
    documented_lower = {item.lower() for item in documented}

    missing = sorted(name for name in forwarded if name.lower() not in documented_lower)
    assert not missing, f"forwarded but undocumented: {missing}"
