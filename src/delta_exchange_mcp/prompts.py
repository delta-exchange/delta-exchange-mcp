"""Onboarding prompts — the only channel a server can use to greet a new install.

An MCP server cannot message a user on connect. `INSTRUCTIONS` reaches the model once
per session, but only a prompt (surfaced as a slash command in a client that supports
them) puts words in front of the *user* before they have asked anything. These three
exist to answer the three questions a first run actually has: what works today, how to
connect a key without ever pasting it into the chat, and what today's account or market
picture looks like.

Each prompt function returns a plain string. `Prompt.render` (see
`mcp.server.fastmcp.prompts.base`) wraps a returned `str` in a `UserMessage` — the client
injects it exactly as if the user had typed it — so there is no need to build that
wrapper by hand here.

Registration is unconditional and static: all three prompts are added at startup and
never removed. `portfolio_check` is registered even with no credentials configured,
rather than gated behind them, because `PromptManager` (see
`mcp/server/fastmcp/prompts/manager.py` in the installed `mcp` package) exposes
`add_prompt`/`get_prompt`/`list_prompts` and nothing to remove one — there is no public
`remove_prompt` to mirror the account/trading tool arm-disarm pattern in `server.py`.
Reimplementing that removal privately (reaching into `mcp._prompt_manager._prompts`)
would tie this file to an internal dict shape for a prompt whose only job, absent a key,
is to say "connect one first" — cheaper and just as correct to always list it and let its
own text carry that branch. Because the set of registered prompts never changes, there is
nothing here that needs a `prompts.listChanged` notification either.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    @mcp.prompt()
    def get_started() -> str:
        """A first-run tour: what works without a key, how to connect one, example asks."""
        return (
            "Give me a quick tour of what this Delta Exchange MCP server can do, then ask "
            "what I'd like to try.\n\n"
            "Cover:\n"
            "- What already works with no API key: live prices and tickers, order books, "
            "options chains, funding rates, open-interest history, and candles for any "
            "product.\n"
            "- How to connect an account, only if I want my positions, balances or orders: "
            "call setup_credentials, which opens a form I type the key into. Never ask me "
            "to paste an API key or secret into this chat — anything sent that way is "
            "stored in the conversation.\n"
            "- That placing, editing, cancelling or closing an order is real and "
            "immediate. There is no rehearsal mode, so always confirm with me before "
            "calling a trading tool.\n\n"
            "Then suggest a few things I could ask, such as:\n"
            "- What is BTC's funding rate right now?\n"
            "- Show me the ETH options chain for next Friday's expiry.\n"
            "- What's the order book depth on BTCUSD?\n"
            "- Give me today's market brief."
        )

    @mcp.prompt()
    def market_brief(underlying: str | None = None) -> str:
        """Today's market snapshot: movers, BTC/ETH context, funding extremes — no calls."""
        focus = f", with extra attention on {underlying}" if underlying else ""
        return (
            f"Give me today's Delta Exchange market brief{focus}. If get_market_movers is "
            "available, use it for the top gainers, losers, most active products, "
            "open-interest build-up and funding-rate extremes. If it isn't, call "
            "list_tickers for contract_types=perpetual_futures and work out each product's "
            "24h percent change yourself from the price fields already in that response. "
            "Cover BTC and ETH specifically either way, and call out any funding-rate "
            "extremes you see across products. This is a market-data summary, not "
            "investment advice — report what the data shows and don't predict where price "
            "is headed next."
        )

    @mcp.prompt()
    def portfolio_check() -> str:
        """Positions, unrealized P&L, margin/liquidation proximity, open orders."""
        return (
            "Give me a full portfolio check: my open positions with unrealized P&L, how "
            "close each one sits to liquidation, my margin usage, and my open orders. If "
            "the account tools aren't available in this session, call setup_credentials "
            "first so I can connect an API key — don't ask me to paste a key or secret "
            "into this chat."
        )
