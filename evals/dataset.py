"""Eval cases: trader prompts + expected tool calls.

Each case is a description-quality probe — the discrimination pairs (forbid sets)
are the signal; a regression in a docstring shows up as the agent picking the
sibling tool.
"""

from dataclasses import dataclass, field
from typing import Any, Literal

ANY = object()  # arg must be present, any value


@dataclass(frozen=True)
class Expect:
    name: str
    args: dict[str, Any] = field(default_factory=dict)  # required subset


@dataclass(frozen=True)
class Case:
    id: str
    mode: Literal["read", "trade"]
    prompts: tuple[str, ...]
    expect: tuple[Expect, ...] = ()
    forbid: frozenset[str] = frozenset()
    judge: bool = False


CASES: tuple[Case, ...] = (
    # -- market (public) --
    Case(
        id="ticker_basic",
        mode="read",
        prompts=("What's BTCUSD trading at right now?",),
        expect=(Expect("get_ticker", {"symbol": "BTCUSD"}),),
    ),
    Case(
        id="orderbook_depth",
        mode="read",
        prompts=("Show me the top 5 levels of the ETHUSD order book.",),
        expect=(Expect("get_orderbook", {"symbol": "ETHUSD", "depth": 5}),),
        forbid=frozenset({"get_ticker", "get_recent_trades"}),
    ),
    Case(
        id="candles_basic",
        mode="read",
        prompts=("Give me 15-minute candles for BTCUSD covering the last day.",),
        expect=(Expect("get_candles", {"symbol": "BTCUSD", "resolution": ANY, "start": ANY, "end": ANY}),),
        forbid=frozenset({"get_mark_price_history"}),
    ),
    Case(
        id="funding_not_mark",
        mode="read",
        prompts=("How has the ETHUSD funding rate moved over the last few days?",),
        expect=(Expect("get_funding_history", {"symbol": "ETHUSD"}),),
        forbid=frozenset({"get_mark_price_history", "get_candles"}),
    ),
    Case(
        id="oi_history",
        mode="read",
        prompts=("How has open interest in BTCUSD changed over the past week?",),
        expect=(Expect("get_oi_history", {"symbol": "BTCUSD"}),),
        forbid=frozenset({"get_candles"}),
    ),
    Case(
        id="mark_price_history",
        mode="read",
        prompts=("Show me BTCUSD's mark price over the last hour.",),
        expect=(Expect("get_mark_price_history", {"symbol": "BTCUSD"}),),
        forbid=frozenset({"get_candles"}),
    ),
    Case(
        id="options_chain",
        mode="read",
        prompts=("Show me the BTC options chain for the nearest expiry.",),
        expect=(Expect("get_options_chain", {"underlying": "BTC"}),),
        judge=True,
    ),
    Case(
        id="products_perps",
        mode="read",
        prompts=("List the live perpetual futures I can trade.",),
        expect=(Expect("list_products", {"contract_types": ANY}),),
        judge=True,
    ),
    Case(
        id="product_detail",
        mode="read",
        prompts=("What's the tick size and contract value for SOLUSD?",),
        expect=(Expect("get_product", {"symbol": "SOLUSD"}),),
        forbid=frozenset({"list_products"}),
    ),
    Case(
        id="public_trades_not_fills",
        mode="read",
        prompts=("Show me the latest trades that happened in the BTCUSD market.",),
        expect=(Expect("get_recent_trades", {"symbol": "BTCUSD"}),),
        forbid=frozenset({"get_fills"}),
    ),
    Case(
        id="settlement_prices",
        mode="read",
        prompts=("What did the recently expired futures settle at?",),
        expect=(Expect("get_settlement_prices"),),
        forbid=frozenset({"get_ticker"}),
    ),
    # -- account (needs testnet creds) --
    Case(
        id="positions_all",
        mode="read",
        prompts=("Show me all my open positions.",),
        expect=(Expect("get_margined_positions"),),
        forbid=frozenset({"get_positions"}),
    ),
    Case(
        id="positions_single",
        mode="read",
        prompts=("What's my current position in BTCUSD?",),
        expect=(Expect("get_positions"),),
        judge=True,
    ),
    Case(
        id="balances",
        mode="read",
        prompts=("How much money do I have in my account?",),
        expect=(Expect("get_wallet_balances"),),
        forbid=frozenset({"get_wallet_transactions", "get_trading_stats"}),
    ),
    Case(
        id="deposits_last_month",
        mode="read",
        prompts=("Show my deposits from the last month.",),
        expect=(Expect("get_wallet_transactions", {"transaction_types": ANY}),),
        forbid=frozenset({"get_wallet_balances"}),
        judge=True,
    ),
    Case(
        id="fills_not_history",
        mode="read",
        prompts=("Which of my ETHUSD orders actually executed in the last day?",),
        expect=(Expect("get_fills"),),
        forbid=frozenset({"get_order_history", "get_recent_trades"}),
    ),
    Case(
        id="open_orders",
        mode="read",
        prompts=("What orders do I have resting on the book right now?",),
        expect=(Expect("get_open_orders"),),
        forbid=frozenset({"get_order_history"}),
    ),
    Case(
        id="order_history",
        mode="read",
        prompts=("Show my closed and cancelled orders from today.",),
        expect=(Expect("get_order_history"),),
        forbid=frozenset({"get_open_orders"}),
    ),
    Case(
        id="order_by_id",
        mode="read",
        prompts=("Look up my order with id 12345. Did it fill?",),
        expect=(Expect("get_order_by_id", {"order_id": 12345}),),
        forbid=frozenset({"get_order_history"}),
    ),
    Case(
        id="leverage_check",
        mode="read",
        prompts=("What leverage am I set to on product 27?",),
        expect=(Expect("get_product_leverage", {"product_id": 27}),),
    ),
    Case(
        id="trading_stats",
        mode="read",
        prompts=("How much volume have I traded on this account?",),
        expect=(Expect("get_trading_stats"),),
        forbid=frozenset({"get_fills"}),
    ),
    # -- trading (dry_run forced by the harness) --
    Case(
        id="post_only_limit",
        mode="trade",
        prompts=(
            "Place a limit buy for 1 contract of BTCUSD at 50000, "
            "and make sure it never takes liquidity.",
        ),
        expect=(
            Expect(
                "place_order",
                {
                    "side": "buy",
                    "order_type": "limit_order",
                    "product_symbol": "BTCUSD",
                    "size": 1,
                    "limit_price": ANY,
                    "post_only": True,
                },
            ),
        ),
    ),
    Case(
        id="bracket_on_position",
        mode="trade",
        prompts=(
            "I'm long 5 contracts of ETHUSD. Protect the position with "
            "a take-profit at 3000 and a stop-loss at 2000.",
        ),
        expect=(
            Expect(
                "place_bracket_order",
                {"take_profit_order": ANY, "stop_loss_order": ANY},
            ),
        ),
        forbid=frozenset({"place_order"}),
        judge=True,
    ),
    Case(
        id="entry_with_bracket",
        mode="trade",
        prompts=(
            "Buy 1 contract of BTCUSD at market and attach a stop-loss at 60000 "
            "and a take-profit at 70000 to the entry.",
        ),
        expect=(
            Expect(
                "place_order",
                {
                    "order_type": "market_order",
                    "bracket_stop_loss_price": ANY,
                    "bracket_take_profit_price": ANY,
                },
            ),
        ),
        forbid=frozenset({"place_bracket_order"}),
        judge=True,
    ),
    Case(
        id="flatten_vs_cancel",
        mode="trade",
        prompts=("Get me completely flat, close out all my positions right now.",),
        expect=(Expect("close_all_positions"),),
        forbid=frozenset({"cancel_all_orders"}),
    ),
    Case(
        id="cancel_not_close",
        mode="trade",
        prompts=("Cancel all my open BTCUSD orders, but leave my positions alone.",),
        expect=(Expect("cancel_all_orders"),),
        forbid=frozenset({"close_all_positions"}),
        judge=True,
    ),
    Case(
        id="set_leverage",
        mode="trade",
        prompts=("Set my BTCUSD leverage to 10x.",),
        expect=(Expect("set_product_leverage", {"product_id": ANY, "leverage": ANY}),),
        judge=True,
    ),
    Case(
        id="margin_add",
        mode="trade",
        prompts=("Add 5 USD of margin to my position on product 27.",),
        expect=(Expect("adjust_position_margin", {"product_id": 27, "delta_margin": ANY}),),
        forbid=frozenset({"set_product_leverage"}),
    ),
    Case(
        id="auto_topup_on",
        mode="trade",
        prompts=("Turn on auto top-up for my position on product 27.",),
        expect=(Expect("configure_auto_topup", {"product_id": 27, "auto_topup": True}),),
        forbid=frozenset({"adjust_position_margin"}),
    ),
    Case(
        id="batch_not_single",
        mode="trade",
        prompts=(
            "Place two limit buys on BTCUSD in one shot: "
            "1 contract at 50000 and 1 contract at 49000.",
        ),
        expect=(Expect("place_batch_orders"),),
        forbid=frozenset({"place_order"}),
        judge=True,
    ),
    Case(
        id="edit_not_cancel_replace",
        mode="trade",
        prompts=("Change my order 12345 on BTCUSD to size 3 at price 51000.",),
        expect=(Expect("edit_order", {"id": 12345, "size": 3}),),
        forbid=frozenset({"cancel_order", "place_order"}),
    ),
    # -- multi-turn flows --
    Case(
        id="flow_check_then_reduce",
        mode="trade",
        prompts=(
            "What's BTCUSD trading at right now?",
            "OK, sell 2 contracts at market, but only to reduce my existing position.",
        ),
        expect=(
            Expect("get_ticker", {"symbol": "BTCUSD"}),
            Expect(
                "place_order",
                {"side": "sell", "order_type": "market_order", "size": 2, "reduce_only": True},
            ),
        ),
        judge=True,
    ),
    Case(
        id="flow_position_then_protect",
        mode="trade",
        prompts=(
            "Do I have an open ETHUSD position?",
            # unconditional instruction: devnet accounts are usually flat, and a
            # correct agent refuses to protect a position that doesn't exist
            "Either way, set the protection up now: place a stop-loss bracket "
            "at 2000 on ETHUSD.",
        ),
        expect=(Expect("place_bracket_order", {"stop_loss_order": ANY}),),
        forbid=frozenset({"place_order"}),
        judge=True,
    ),
    Case(
        id="flow_leverage_check_then_set",
        mode="trade",
        prompts=(
            "What leverage am I running on BTCUSD?",
            "Drop it to 5x.",
        ),
        expect=(
            Expect("get_product_leverage"),
            Expect("set_product_leverage", {"leverage": ANY}),
        ),
        judge=True,
    ),
    Case(
        id="flow_book_then_post_only",
        mode="trade",
        prompts=(
            "What's the ETHUSD order book looking like?",
            "Put a post-only bid one tick under the best bid, for 2 contracts.",
        ),
        expect=(
            Expect("get_orderbook", {"symbol": "ETHUSD"}),
            Expect("place_order", {"side": "buy", "size": 2, "post_only": True}),
        ),
        judge=True,
    ),
)
