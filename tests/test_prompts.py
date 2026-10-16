"""Onboarding prompts: listed with and without credentials, rendered text is sound.

Prompts are registered unconditionally and statically (see `prompts.py`'s module
docstring for why `portfolio_check` is not gated behind credentials the way the account
and trading tools are) — these tests confirm that decision rather than assume it, and
lock down what each prompt's rendered text actually tells the assistant to do.
"""

from delta_exchange_mcp import config as config_mod
from delta_exchange_mcp.config import Config
from delta_exchange_mcp.server import build_server

from tests.test_activation import connected

PROMPT_NAMES = {"get_started", "market_brief", "portfolio_check"}


def _config(*, with_credentials: bool) -> Config:
    kwargs = {"env": "india_testnet", "base_url": "https://example.invalid"}
    if with_credentials:
        kwargs.update(api_key="k", api_secret="s")
    return Config(**kwargs)


async def _prompt_names(mcp) -> set[str]:
    return {p.name for p in await mcp.list_prompts()}


async def _render(mcp, name: str, arguments: dict[str, str] | None = None) -> str:
    # get_prompt needs no live request context here: none of these prompt functions take
    # a Context argument, so `Prompt.render` never dereferences one.
    result = await mcp.get_prompt(name, arguments)
    (message,) = result.messages
    return message.content.text


# --- listing, with and without credentials --------------------------------------------


async def test_all_three_prompts_are_listed_without_credentials():
    mcp = build_server(_config(with_credentials=False))
    assert await _prompt_names(mcp) == PROMPT_NAMES


async def test_all_three_prompts_are_listed_with_credentials():
    mcp = build_server(_config(with_credentials=True))
    assert await _prompt_names(mcp) == PROMPT_NAMES


# --- get_started ------------------------------------------------------------------------


async def test_get_started_names_setup_credentials_and_gives_examples():
    mcp = build_server(_config(with_credentials=False))
    text = await _render(mcp, "get_started")
    assert "setup_credentials" in text
    # At least 3 concrete example asks, one per bullet line starting with "- ".
    examples = [
        line
        for line in text.splitlines()
        if line.startswith("- ") and "?" in line or line.startswith("- Give me")
    ]
    assert len(examples) >= 3


async def test_get_started_never_asks_for_a_key_in_the_chat():
    mcp = build_server(_config(with_credentials=False))
    text = await _render(mcp, "get_started")
    assert "never ask me to paste an api key or secret into this chat" in text.lower()


# --- market_brief ------------------------------------------------------------------------


async def test_market_brief_prefers_get_market_movers_with_a_list_tickers_fallback():
    mcp = build_server(_config(with_credentials=False))
    text = await _render(mcp, "market_brief")
    assert "get_market_movers" in text
    assert "list_tickers" in text
    assert "BTC" in text and "ETH" in text


async def test_market_brief_takes_an_optional_underlying_argument():
    mcp = build_server(_config(with_credentials=False))
    prompts = await mcp.list_prompts()
    (market_brief,) = [p for p in prompts if p.name == "market_brief"]
    (arg,) = market_brief.arguments
    assert arg.name == "underlying"
    assert arg.required is False

    text = await _render(mcp, "market_brief", {"underlying": "SOL"})
    assert "SOL" in text


async def test_market_brief_disclaims_advice_and_predictions():
    mcp = build_server(_config(with_credentials=False))
    text = await _render(mcp, "market_brief")
    assert "not investment advice" in text.lower()
    assert "predict" in text.lower()


# --- portfolio_check ----------------------------------------------------------------------


async def test_portfolio_check_is_available_even_with_no_credentials_configured():
    """No public API removes a FastMCP prompt, so it stays listed and says so itself."""
    mcp = build_server(_config(with_credentials=False))
    text = await _render(mcp, "portfolio_check")
    assert "setup_credentials" in text


async def test_portfolio_check_covers_positions_pnl_margin_and_orders():
    mcp = build_server(_config(with_credentials=True))
    text = await _render(mcp, "portfolio_check")
    for term in ("positions", "unrealized", "liquidation", "margin", "open orders"):
        assert term in text.lower()


async def test_portfolio_check_never_asks_for_a_key_in_the_chat():
    mcp = build_server(_config(with_credentials=False))
    text = await _render(mcp, "portfolio_check")
    assert "paste a key or secret into this chat" in text.lower()


# --- suggested_next on get_connection_status ----------------------------------------------


async def test_suggested_next_without_credentials_points_at_the_tour_and_setup(
    monkeypatch,
):
    monkeypatch.delenv("DELTA_API_KEY", raising=False)
    monkeypatch.delenv("DELTA_API_SECRET", raising=False)
    app = build_server(config_mod.load())
    try:
        async with connected(mcp=app) as session:
            status = await session.call("get_connection_status")
    finally:
        await app.close_live_client()
    suggested = status["suggested_next"]
    assert any("get_started" in item for item in suggested)
    assert any("setup_credentials" in item for item in suggested)
    assert any("market brief" in item.lower() for item in suggested)


async def test_suggested_next_with_credentials_points_at_portfolio_and_market_brief(
    monkeypatch,
):
    # Credentials must be visible where `reconcile()` actually looks — the environment or
    # the shared file — not just on the `Config` object build_server() was handed: every
    # tool call reconciles against a freshly loaded config, which would otherwise disarm
    # the surface this test means to exercise.
    monkeypatch.setenv("DELTA_API_KEY", "k")
    monkeypatch.setenv("DELTA_API_SECRET", "s")
    app = build_server(config_mod.load())
    try:
        async with connected(mcp=app) as session:
            status = await session.call("get_connection_status")
    finally:
        await app.close_live_client()
    suggested = status["suggested_next"]
    assert any("portfolio" in item.lower() for item in suggested)
    assert any("market brief" in item.lower() for item in suggested)
    assert not any("setup_credentials" in item for item in suggested)
