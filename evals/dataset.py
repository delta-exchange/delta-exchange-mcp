"""Eval cases with tool expectations scoped to each conversation turn."""

from dataclasses import dataclass, field
from typing import Any, Literal

from delta_exchange_mcp.tools.trading import TOOL_NAMES as MUTATING_TOOLS

ANY = object()


@dataclass(frozen=True)
class Expect:
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Turn:
    prompt: str
    expect: tuple[Expect, ...] = ()
    allowed_reads: frozenset[str] = frozenset()
    forbidden_reads: frozenset[str] = frozenset()
    forbidden_mutations: frozenset[str] = MUTATING_TOOLS


@dataclass(frozen=True)
class Case:
    id: str
    mode: Literal["read", "trade"]
    turns: tuple[Turn, ...]
    judge: bool = False

    def __post_init__(self) -> None:
        if not self.turns:
            raise ValueError(f"{self.id}: at least one turn is required")
        if self.mode == "read" and any(
            turn.forbidden_mutations != MUTATING_TOOLS
            or any(expected.name in MUTATING_TOOLS for expected in turn.expect)
            for turn in self.turns
        ):
            raise ValueError(f"{self.id}: read cases cannot permit mutations")


def _expect(name: str, **args: Any) -> Expect:
    return Expect(name, args)


def _turn(
    prompt: str,
    *expect: Expect,
    allowed_reads: tuple[str, ...] = (),
    forbid: tuple[str, ...] = (),
) -> Turn:
    allowed = frozenset(allowed_reads)
    if allowed & MUTATING_TOOLS:
        raise ValueError("allowed_reads cannot contain a mutation")
    expected_mutations = frozenset(exp.name for exp in expect) & MUTATING_TOOLS
    forbidden = frozenset(forbid)
    return Turn(
        prompt=prompt,
        expect=expect,
        allowed_reads=allowed,
        forbidden_reads=forbidden - MUTATING_TOOLS,
        forbidden_mutations=(MUTATING_TOOLS - expected_mutations)
        | (forbidden & MUTATING_TOOLS),
    )


def _case(
    id: str,
    mode: Literal["read", "trade"],
    *turns: Turn,
    judge: bool = False,
) -> Case:
    return Case(id=id, mode=mode, turns=turns, judge=judge)


CASES: tuple[Case, ...] = (
    # Market data
    _case(
        "ticker_basic",
        "read",
        _turn(
            "What's BTCUSD trading at right now?",
            _expect("get_ticker", symbol="BTCUSD"),
        ),
    ),
    _case(
        "orderbook_depth",
        "read",
        _turn(
            "Show me the top 5 levels of the ETHUSD order book.",
            _expect("get_orderbook", symbol="ETHUSD", depth=5),
            forbid=("get_ticker", "get_recent_trades"),
        ),
    ),
    _case(
        "candles_basic",
        "read",
        _turn(
            "Give me 15-minute candles for BTCUSD covering the last day.",
            _expect(
                "get_candles",
                symbol="BTCUSD",
                resolution="15m",
                start=ANY,
                end=ANY,
            ),
            forbid=("get_mark_price_history",),
        ),
    ),
    _case(
        "funding_not_mark",
        "read",
        _turn(
            "How has the ETHUSD funding rate moved over the last few days?",
            _expect("get_funding_history", symbol="ETHUSD", start=ANY, end=ANY),
            forbid=("get_mark_price_history", "get_candles"),
        ),
    ),
    _case(
        "oi_history",
        "read",
        _turn(
            "How has open interest in BTCUSD changed over the past week?",
            _expect("get_oi_history", symbol="BTCUSD", start=ANY, end=ANY),
            forbid=("get_candles",),
        ),
    ),
    _case(
        "mark_price_history",
        "read",
        _turn(
            "Show me BTCUSD's mark price over the last hour.",
            _expect("get_mark_price_history", symbol="BTCUSD", start=ANY, end=ANY),
            forbid=("get_candles",),
        ),
    ),
    _case(
        "options_chain",
        "read",
        _turn(
            "Show me the BTC options chain for the nearest expiry.",
            _expect("get_options_chain", underlying="BTC", expiry_date=ANY),
            allowed_reads=("list_products",),
        ),
        judge=True,
    ),
    _case(
        "products_perps",
        "read",
        _turn(
            "List the live perpetual futures I can trade.",
            _expect(
                "list_products",
                contract_types=["perpetual_futures"],
                states=["live"],
            ),
        ),
        judge=True,
    ),
    _case(
        "product_detail",
        "read",
        _turn(
            "What's the tick size and contract value for SOLUSD?",
            _expect("get_product", symbol="SOLUSD"),
            forbid=("list_products",),
        ),
    ),
    _case(
        "public_trades_not_fills",
        "read",
        _turn(
            "Show me the latest trades that happened in the BTCUSD market.",
            _expect("get_recent_trades", symbol="BTCUSD"),
            forbid=("get_fills",),
        ),
    ),
    _case(
        "settlement_prices",
        "read",
        _turn(
            "What did the recently expired futures settle at?",
            _expect("get_settlement_prices", contract_types=["futures"]),
            forbid=("get_ticker",),
        ),
    ),
    # Account data
    _case(
        "positions_all",
        "read",
        _turn(
            "Show me all my open positions.",
            _expect("get_margined_positions"),
            forbid=("get_positions",),
        ),
    ),
    _case(
        "positions_single",
        "read",
        _turn(
            "What's my current position in BTCUSD?",
            _expect("get_positions", product_id=ANY),
            allowed_reads=("get_product",),
        ),
        judge=True,
    ),
    _case(
        "balances",
        "read",
        _turn(
            "How much money do I have in my account?",
            _expect("get_wallet_balances"),
            forbid=("get_wallet_transactions", "get_trading_stats"),
        ),
    ),
    _case(
        "deposits_last_month",
        "read",
        _turn(
            "Show my deposits from the last month.",
            _expect(
                "get_wallet_transactions",
                transaction_types=["deposit"],
                start_time_us=ANY,
            ),
            forbid=("get_wallet_balances",),
        ),
        judge=True,
    ),
    _case(
        "fills_not_history",
        "read",
        _turn(
            "Which of my ETHUSD orders actually executed in the last day?",
            _expect("get_fills", product_ids=[ANY], start_time_us=ANY),
            allowed_reads=("get_product",),
            forbid=("get_order_history", "get_recent_trades"),
        ),
    ),
    _case(
        "open_orders",
        "read",
        _turn(
            "What orders do I have resting on the book right now?",
            _expect("get_open_orders"),
            forbid=("get_order_history",),
        ),
    ),
    _case(
        "order_history",
        "read",
        _turn(
            "Show my closed and cancelled orders from today.",
            _expect("get_order_history", start_time_us=ANY),
            forbid=("get_open_orders",),
        ),
    ),
    _case(
        "order_by_id",
        "read",
        _turn(
            "Look up my order with id 12345. Did it fill?",
            _expect("get_order_by_id", order_id=12345),
            forbid=("get_order_history",),
        ),
    ),
    _case(
        "leverage_check",
        "read",
        _turn(
            "What leverage am I set to on product 27?",
            _expect("get_product_leverage", product_id=27),
        ),
    ),
    _case(
        "trading_stats",
        "read",
        _turn(
            "How much volume have I traded on this account?",
            _expect("get_trading_stats"),
            forbid=("get_fills",),
        ),
    ),
    # Trading calls; the harness forces dry_run=true.
    _case(
        "post_only_limit",
        "trade",
        _turn(
            "Place a limit buy for 1 contract of BTCUSD at 50000, and make sure it never takes liquidity.",
            _expect(
                "place_order",
                side="buy",
                order_type="limit_order",
                product_symbol="BTCUSD",
                size=1,
                limit_price="50000",
                post_only=True,
            ),
        ),
    ),
    _case(
        "bracket_on_position",
        "trade",
        _turn(
            "I'm long 5 contracts of ETHUSD. Protect the position with a take-profit at 3000 and a stop-loss at 2000.",
            _expect(
                "place_bracket_order",
                product_symbol="ETHUSD",
                take_profit_order={"stop_price": "3000"},
                stop_loss_order={"stop_price": "2000"},
            ),
            forbid=("place_order",),
        ),
        judge=True,
    ),
    _case(
        "entry_with_bracket",
        "trade",
        _turn(
            "Buy 1 contract of BTCUSD at market and attach a stop-loss at 60000 and a take-profit at 70000 to the entry.",
            _expect(
                "place_order",
                product_symbol="BTCUSD",
                size=1,
                side="buy",
                order_type="market_order",
                bracket_stop_loss_price="60000",
                bracket_take_profit_price="70000",
            ),
            forbid=("place_bracket_order",),
        ),
        judge=True,
    ),
    _case(
        "flatten_vs_cancel",
        "trade",
        _turn(
            "Get me completely flat, close out all my positions right now.",
            _expect(
                "close_all_positions",
                close_all_portfolio=True,
                close_all_isolated=True,
            ),
            allowed_reads=("get_margined_positions",),
            forbid=("cancel_all_orders",),
        ),
    ),
    _case(
        "cancel_not_close",
        "trade",
        _turn(
            "Cancel all my open BTCUSD orders, but leave my positions alone.",
            _expect("cancel_all_orders", product_id=ANY),
            allowed_reads=("get_product", "get_open_orders"),
            forbid=("close_all_positions",),
        ),
        judge=True,
    ),
    _case(
        "set_leverage",
        "trade",
        _turn(
            "Set my BTCUSD leverage to 10x.",
            _expect("set_product_leverage", product_id=ANY, leverage="10"),
            allowed_reads=("get_product",),
        ),
        judge=True,
    ),
    _case(
        "margin_add",
        "trade",
        _turn(
            "Add 5 USD of margin to my position on product 27.",
            _expect("adjust_position_margin", product_id=27, delta_margin="5"),
            forbid=("set_product_leverage",),
        ),
    ),
    _case(
        "auto_topup_on",
        "trade",
        _turn(
            "Turn on auto top-up for my position on product 27.",
            _expect("configure_auto_topup", product_id=27, auto_topup=True),
            forbid=("adjust_position_margin",),
        ),
    ),
    _case(
        "batch_not_single",
        "trade",
        _turn(
            "Place two limit buys on BTCUSD in one shot: 1 contract at 50000 and 1 contract at 49000.",
            _expect(
                "place_batch_orders",
                product_symbol="BTCUSD",
                orders=[
                    {
                        "size": 1,
                        "side": "buy",
                        "order_type": "limit_order",
                        "limit_price": "50000",
                    },
                    {
                        "size": 1,
                        "side": "buy",
                        "order_type": "limit_order",
                        "limit_price": "49000",
                    },
                ],
            ),
            forbid=("place_order",),
        ),
        judge=True,
    ),
    _case(
        "edit_not_cancel_replace",
        "trade",
        _turn(
            "Change my order 12345 on BTCUSD to size 3 at price 51000.",
            _expect(
                "edit_order",
                id=12345,
                product_symbol="BTCUSD",
                size=3,
                limit_price="51000",
            ),
            allowed_reads=("get_order_by_id",),
            forbid=("cancel_order", "place_order"),
        ),
    ),
    # Multi-turn flows
    _case(
        "flow_check_then_reduce",
        "trade",
        _turn(
            "What's BTCUSD trading at right now?",
            _expect("get_ticker", symbol="BTCUSD"),
        ),
        _turn(
            "OK, sell 2 contracts at market, but only to reduce my existing position.",
            _expect(
                "place_order",
                product_symbol="BTCUSD",
                side="sell",
                order_type="market_order",
                size=2,
                reduce_only=True,
            ),
            allowed_reads=("get_positions",),
        ),
        judge=True,
    ),
    _case(
        "flow_position_then_protect",
        "trade",
        _turn(
            "Do I have an open ETHUSD position?",
            _expect("get_positions", product_id=ANY),
            allowed_reads=("get_product",),
        ),
        _turn(
            "Either way, set the protection up now: place a stop-loss bracket at 2000 on ETHUSD.",
            _expect(
                "place_bracket_order",
                product_symbol="ETHUSD",
                stop_loss_order={"stop_price": "2000"},
            ),
            forbid=("place_order",),
        ),
        judge=True,
    ),
    _case(
        "flow_leverage_check_then_set",
        "trade",
        _turn(
            "What leverage am I running on BTCUSD?",
            _expect("get_product_leverage", product_id=ANY),
            allowed_reads=("get_product",),
        ),
        _turn(
            "Drop it to 5x.",
            _expect("set_product_leverage", product_id=ANY, leverage="5"),
        ),
        judge=True,
    ),
    _case(
        "flow_book_then_post_only",
        "trade",
        _turn(
            "What's the ETHUSD order book looking like?",
            _expect("get_orderbook", symbol="ETHUSD"),
        ),
        _turn(
            "Put a post-only bid one tick under the best bid, for 2 contracts.",
            _expect(
                "place_order",
                product_symbol="ETHUSD",
                side="buy",
                order_type="limit_order",
                size=2,
                limit_price=ANY,
                post_only=True,
            ),
        ),
        judge=True,
    ),
)
