"""What reaches Delta about the client, and what must never reach it.

These drive the real protocol where the point is that a value survives the whole path from
a client's handshake to an outbound HTTP header. Asserting on `analytics.headers` alone
would pass while the contextvars that carry the session and the tool name were never set.
"""

import json
import re
from urllib.parse import unquote

import httpx
import mcp.types as types
import pytest
import respx

from delta_exchange_mcp import analytics, store
from delta_exchange_mcp.config import INDIA_TESTNET_REST, Config
from delta_exchange_mcp.version import PACKAGE_VERSION

from .test_activation import connected

TICKER = {"success": True, "result": {"symbol": "BTCUSD", "mark_price": "1"}}


@pytest.fixture(autouse=True)
def testnet_everywhere(tmp_path, monkeypatch):
    """Make the resolved settings agree with the config each test hands to `build_server`.

    A session's first `tools/list` reconciles the running server against what the settings
    actually resolve to, so a server built for testnet while the environment says prod
    rebinds itself to prod mid-test and the mocked URL stops matching. That reconciliation
    is correct behaviour — it is how an edited settings file reaches a running server — so
    the environment is what has to agree here. Pointing the settings file at `tmp_path` also
    keeps these tests off the real one.
    """
    monkeypatch.setenv("DELTA_MCP_ENV", "india_testnet")
    monkeypatch.setattr(store, "path", lambda: tmp_path / "config.env")


def _testnet() -> Config:
    return Config(env="india_testnet", base_url=INDIA_TESTNET_REST)


async def call_ticker(client_info=None):
    """Drive one market tool over a real session and return the headers Delta received."""
    route = respx.get(f"{INDIA_TESTNET_REST}/tickers/BTCUSD").mock(
        return_value=httpx.Response(200, json=TICKER)
    )
    async with connected(cfg=_testnet(), client_info=client_info) as session:
        await session.call("get_ticker", symbol="BTCUSD")
    assert route.called
    # The newest call, not the first: `respx.get` on a pattern that already exists hands
    # back the same route, so its `calls` accumulate across every connection in one test.
    return route.calls[-1].request.headers


@respx.mock
async def test_the_request_says_which_client_and_which_tool_caused_it():
    """The whole point: an outbound call to Delta is otherwise anonymous.

    A tool call arrives over a pipe, and the request this server then makes looks identical
    whichever client asked. Without these, "which clients do people use" and "which tools
    get called" have no answer at the other end.
    """
    sent = await call_ticker(
        types.Implementation(name="claude-ai", version="1.30096.5", title="Claude")
    )
    assert sent[f"{analytics.PREFIX}Client"] == "claude-ai"
    assert sent[f"{analytics.PREFIX}Client-Version"] == "1.30096.5"
    assert sent[f"{analytics.PREFIX}Tool"] == "get_ticker"
    assert sent[f"{analytics.PREFIX}Version"] == PACKAGE_VERSION
    assert sent[f"{analytics.PREFIX}Env"] == "india_testnet"
    assert sent[f"{analytics.PREFIX}Mode"] == "read"
    assert sent[f"{analytics.PREFIX}Session"]


@respx.mock
async def test_one_connection_keeps_one_session_identifier():
    """Two requests from one person have to be recognisable as related, or none of this
    answers a question about people rather than calls."""
    route = respx.get(f"{INDIA_TESTNET_REST}/tickers/BTCUSD").mock(
        return_value=httpx.Response(200, json=TICKER)
    )
    async with connected(cfg=_testnet()) as session:
        await session.call("get_ticker", symbol="BTCUSD")
        await session.call("get_ticker", symbol="BTCUSD")
    first, second = (call.request.headers[f"{analytics.PREFIX}Session"] for call in route.calls)
    assert first == second

    second_connection = await call_ticker()
    assert second_connection[f"{analytics.PREFIX}Session"] != first


@respx.mock
async def test_a_client_name_cannot_forge_a_header():
    """These strings are whatever the client decided to call itself.

    A newline in one would end the header and let the rest be read as another, which is how
    a self-reported label becomes a way to set `api-key`. Percent-encoding removes the
    possibility rather than trusting clients to be well behaved.
    """
    hostile = "evil\r\napi-key: stolen"
    sent = await call_ticker(types.Implementation(name=hostile, version="1"))
    reported = sent[f"{analytics.PREFIX}Client"]
    assert "\r" not in reported and "\n" not in reported
    assert "%0D%0A" in reported
    assert sent.get("api-key") is None


@respx.mock
async def test_nothing_credential_shaped_is_ever_forwarded():
    """The invariant the audit log and the debug log already hold, checked at this seam too."""
    cfg = Config(
        env="india_testnet",
        base_url=INDIA_TESTNET_REST,
        api_key="key-that-must-not-travel",
        api_secret="secret-that-must-not-travel",
    )
    route = respx.get(f"{INDIA_TESTNET_REST}/tickers/BTCUSD").mock(
        return_value=httpx.Response(200, json=TICKER)
    )
    async with connected(cfg=cfg) as session:
        await session.call("get_ticker", symbol="BTCUSD")

    ours = {
        name: value
        for name, value in route.calls[-1].request.headers.items()
        if name.lower().startswith(analytics.PREFIX.lower())
    }
    blob = " ".join(ours.values())
    assert "key-that-must-not-travel" not in blob
    assert "secret-that-must-not-travel" not in blob
    assert ours


@respx.mock
async def test_a_client_that_describes_itself_at_length_cannot_break_the_request():
    """A gateway that caps header bytes answers 431, which fails the person's question.

    `description` is an unbounded string the client chooses, so the budget has to hold
    whatever it sends rather than trusting it to be short.
    """
    sent = await call_ticker(
        types.Implementation(
            name="verbose", version="1", title="t" * 5000, website_url="https://example.test"
        )
    )
    ours = sum(
        len(name) + len(value) + 4
        for name, value in sent.items()
        if name.lower().startswith(analytics.PREFIX.lower())
    )
    assert ours <= analytics.BUDGET_BYTES, ours
    # The fields that get filtered on survive the trim; only the long tail gives way.
    assert sent[f"{analytics.PREFIX}Client"] == "verbose"
    assert sent[f"{analytics.PREFIX}Tool"] == "get_ticker"


@respx.mock
async def test_the_context_header_is_always_readable_json():
    """Staying inside the budget is not enough; the header has to be parseable.

    Bounding it by cutting the string to a length lands mid-token and produces JSON no
    consumer can read — a header that is present, within budget, and useless. It is bounded
    by dropping whole fields instead, so it is always either complete or one field shorter.
    Sizes here straddle the point where a per-field limit used to cut it.
    """
    for length in (10, 150, 400, 5000):
        sent = await call_ticker(
            types.Implementation(name="c", version="1", title="T" * length)
        )
        raw = sent.get(f"{analytics.PREFIX}Context")
        if raw is None:
            continue
        decoded = unquote(raw)
        parsed = json.loads(decoded)  # raises if it was cut mid-token
        assert isinstance(parsed, dict), decoded


@respx.mock
async def test_the_readme_lists_exactly_what_gets_forwarded():
    """Disclosure is the whole mitigation here, so it has to stay true on its own.

    There is deliberately no switch to turn this off; what makes that defensible is that
    the README names every field. A header added later without a line in that table would
    quietly break the promise, and nobody would notice from the code alone.
    """
    from pathlib import Path

    sent = await call_ticker(
        types.Implementation(name="claude-ai", version="1", title="Claude")
    )
    forwarded = {
        name for name in sent if name.lower().startswith(analytics.PREFIX.lower())
    }
    readme = Path(__file__).resolve().parents[1] / "README.md"
    documented = set(re.findall(r"`(X-Delta-MCP-[A-Za-z-]+)`", readme.read_text()))

    lowered = {name.lower() for name in documented}
    missing = sorted(n for n in forwarded if n.lower() not in lowered)
    assert not missing, f"forwarded but undocumented: {missing}"
