"""Public market-data tools (M1)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from delta_exchange_mcp.client import DeltaClient

Resolution = Literal["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "1d", "1w"]

# Movers considers a market only above this 24h notional floor. Live 24h turnover_usd
# across the 220 perpetual_futures tickers (2026-09-23) is heavily right-skewed: median
# ~$126k, but the bottom quartile trades under $26k. At that thin an end, a handful of
# resting orders can print a large 24h % swing on no real flow — e.g. STXUSD printed
# -4.0% on just $37.8k of turnover in that same snapshot, while BCHUSD's genuine +30.0%
# move carried $38.2M. $250k sits just above the 60th percentile (79/220 symbols clear
# it that day) — enough breadth to serve top_n up to 50 while cutting the noisy tail;
# raising/lowering it only trades universe size against how thin a mover it will surface.
DEFAULT_MIN_TURNOVER_USD = 250_000.0


def _csv(values: list[str] | None) -> str | None:
    if not values:
        return None
    return ",".join(values)


def _positive_float(value: Any) -> float | None:
    """Parse a ticker numeric field (arrives as a string) to a float > 0, else None.

    For price/volume fields (mark_price, open, high, turnover_usd, ...) a missing,
    empty, non-numeric, or placeholder-zero value signals a freshly listed or stale
    product rather than real data, so zero is treated the same as missing.
    """
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _signed_nonzero_float(value: Any) -> float | None:
    """Parse a ticker delta field (oi_change_usd_6h, funding_rate) to a nonzero float.

    Unlike `_positive_float`, negative values are real data here (OI can shrink,
    funding can go negative — the README's own worked example shows -0.284% funding).
    Only missing/unparseable/exactly-zero is treated as a skip. Checked against a live
    220-symbol snapshot: this only drops 3 rows (all exactly-zero oi_change_usd_6h);
    funding_rate never printed exactly 0.
    """
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed != 0 else None


def _ticker_row(ticker: dict[str, Any]) -> dict[str, Any] | None:
    """Build one compact movers row from a raw `/tickers` entry, or None to skip it.

    change_pct_24h is (close - open) / open — the last-traded-price move, not
    mark_change_24h. Verified against a live snapshot (2026-09-23) for BTCUSD, ETHUSD,
    SOLUSD and all 220 perpetual_futures tickers: (close-open)/open*100 matches the
    API's own `ltp_change_24h` field to within float noise (mean abs diff 2.6e-5 across
    220 symbols) — Delta's docs describe ltp_change_24h as "last traded price change
    over 24 hours", confirming this is the standard 24h % figure. mark_change_24h tracks
    the same move loosely (corr 0.99) but is a distinct, noisier quantity (mean abs diff
    0.43pp, one outlier at 7.5pp) because it's driven off the mark/index price rather
    than trades, so it is deliberately not used here.
    """
    symbol = ticker.get("symbol")
    if not symbol:
        return None
    mark_price = _positive_float(ticker.get("mark_price"))
    if mark_price is None:
        return None
    open_price = _positive_float(ticker.get("open"))
    if open_price is None:
        return None
    close_price = _positive_float(ticker.get("close"))
    if close_price is None:
        return None
    high = _positive_float(ticker.get("high"))
    if high is None:
        return None
    low = _positive_float(ticker.get("low"))
    if low is None:
        return None
    turnover_usd = _positive_float(ticker.get("turnover_usd"))
    if turnover_usd is None:
        return None
    oi_value_usd = _positive_float(ticker.get("oi_value_usd"))
    if oi_value_usd is None:
        return None
    oi_change_usd_6h = _signed_nonzero_float(ticker.get("oi_change_usd_6h"))
    if oi_change_usd_6h is None:
        return None
    funding_rate = _signed_nonzero_float(ticker.get("funding_rate"))
    if funding_rate is None:
        return None

    change_pct_24h = (close_price - open_price) / open_price * 100
    return {
        "symbol": symbol,
        # 4dp keeps this a compact summary (full mark-price precision is one
        # get_ticker/list_tickers call away) while still showing sub-cent altcoins.
        "mark_price": round(mark_price, 4),
        "change_pct_24h": round(change_pct_24h, 2),
        "high_24h": round(high, 4),
        "low_24h": round(low, 4),
        # Whole-dollar notional: cent-level precision on a multi-thousand/million-
        # dollar figure is noise, and dropping it keeps rows materially smaller.
        "turnover_usd": round(turnover_usd),
        "oi_value_usd": round(oi_value_usd),
        "oi_change_usd_6h": round(oi_change_usd_6h),
        "funding_rate": round(funding_rate, 6),
    }


def _as_of(tickers: list[dict[str, Any]]) -> str:
    """ISO timestamp for the response: the latest ticker `timestamp` (microseconds
    since epoch), falling back to the fetch time if none parse."""
    stamps = [_positive_float(t.get("timestamp")) for t in tickers]
    latest = max((s for s in stamps if s is not None), default=None)
    if latest is None:
        return datetime.now(timezone.utc).isoformat()
    return datetime.fromtimestamp(latest / 1_000_000, tz=timezone.utc).isoformat()


def _movers(
    tickers: list[dict[str, Any]],
    top_n: int,
    min_turnover_usd: float,
) -> dict[str, Any]:
    """Pure ranking helper: raw `/tickers` result -> compact movers payload.

    Kept separate from the tool so it's unit-testable without an HTTP mock.
    """
    rows = [row for row in (_ticker_row(t) for t in tickers) if row is not None]
    universe = [row for row in rows if row["turnover_usd"] >= min_turnover_usd]
    funding_n = max(1, min(top_n // 2, 5))

    def top(rows_: list[dict[str, Any]], key: str, reverse: bool, n: int) -> list[dict[str, Any]]:
        return sorted(rows_, key=lambda r: r[key], reverse=reverse)[:n]

    return {
        "as_of": _as_of(tickers),
        "universe": len(universe),
        "gainers": top(universe, "change_pct_24h", True, top_n),
        "losers": top(universe, "change_pct_24h", False, top_n),
        "most_active": top(universe, "turnover_usd", True, top_n),
        "oi_buildup": top(universe, "oi_change_usd_6h", True, top_n),
        "funding_extremes": {
            "highest": top(universe, "funding_rate", True, funding_n),
            "lowest": top(universe, "funding_rate", False, funding_n),
        },
        "note": "Market data only, not investment advice.",
    }


def register(mcp: FastMCP, client: DeltaClient) -> None:
    @mcp.tool()
    async def list_products(
        contract_types: list[str] | None = Field(
            default=None,
            description="Filter by contract types: perpetual_futures, call_options, put_options, futures, spot.",
        ),
        states: list[str] | None = Field(
            default=None,
            description="Filter by product state: live, upcoming, expired, settled.",
        ),
        expiry: str | None = Field(
            default=None,
            description="Expiry date filter in YYYY-MM-DD format (current and future expiries only).",
        ),
        page_size: int = Field(default=100, ge=1, le=500),
        after: str | None = Field(default=None, description="Cursor from a previous response's meta.after."),
    ) -> dict[str, Any]:
        """List tradable products on Delta Exchange with optional filters. Returns paginated result + meta cursors."""
        return await client.get(
            "/products",
            params={
                "contract_types": _csv(contract_types),
                "states": _csv(states),
                "expiry": expiry,
                "page_size": page_size,
                "after": after,
            },
        )

    @mcp.tool()
    async def get_product(symbol: str) -> dict[str, Any]:
        """Get full product details for a single symbol (e.g. BTCUSD, C-BTC-66400-010824)."""
        return await client.get(f"/products/{symbol}")

    @mcp.tool()
    async def get_ticker(symbol: str) -> dict[str, Any]:
        """Get 24h ticker (price, volume, OI, mark/spot) for one symbol."""
        return await client.get(f"/tickers/{symbol}")

    @mcp.tool()
    async def list_tickers(
        contract_types: list[str] | None = Field(
            default=None, description="Filter: perpetual_futures, futures, call_options, put_options."
        ),
        underlying_asset_symbols: list[str] | None = Field(
            default=None, description="Underlying symbols e.g. BTC, ETH, SOL."
        ),
    ) -> dict[str, Any]:
        """List tickers across many products with optional contract-type / underlying filters."""
        return await client.get(
            "/tickers",
            params={
                "contract_types": _csv(contract_types),
                "underlying_asset_symbols": _csv(underlying_asset_symbols),
            },
        )

    @mcp.tool()
    async def get_market_movers(
        contract_types: list[str] | None = Field(
            default=None,
            description="Filter: perpetual_futures, futures, call_options, put_options. Defaults to perpetual_futures.",
        ),
        top_n: int = Field(
            default=10, ge=1, le=50, description="Rows per list (gainers, losers, most_active, ...)."
        ),
        min_turnover_usd: float = Field(
            default=DEFAULT_MIN_TURNOVER_USD,
            ge=0,
            description="Exclude symbols below this 24h turnover_usd floor before ranking, so thin/illiquid "
            "contracts don't dominate the lists on noise.",
        ),
    ) -> dict[str, Any]:
        """Factual snapshot of which contracts are up/down over the last 24h — market data only, not investment advice.

        One `/tickers` fetch, ranked several ways: gainers/losers by 24h % change (last
        traded price basis), most_active by 24h turnover_usd, oi_buildup by 6h open-interest
        change, and funding_extremes (highest/lowest funding rate). Reports what the numbers
        are, not what they mean or what to do about them — no predictions, no buy/sell language.
        """
        raw = await client.get(
            "/tickers",
            params={"contract_types": _csv(contract_types) or "perpetual_futures"},
        )
        return _movers(raw.get("result", []), top_n, min_turnover_usd)

    @mcp.tool()
    async def get_orderbook(
        symbol: str,
        depth: int | None = Field(default=None, ge=1, le=100, description="Levels per side (max 100)."),
    ) -> dict[str, Any]:
        """L2 orderbook snapshot for a symbol."""
        return await client.get(f"/l2orderbook/{symbol}", params={"depth": depth})

    @mcp.tool()
    async def get_recent_trades(symbol: str) -> dict[str, Any]:
        """Recent public trades for a symbol."""
        return await client.get(f"/trades/{symbol}")

    @mcp.tool()
    async def get_candles(
        symbol: str,
        resolution: Resolution,
        start: int = Field(description="Unix timestamp in seconds, inclusive."),
        end: int = Field(description="Unix timestamp in seconds, inclusive."),
    ) -> dict[str, Any]:
        """OHLC candles. For funding/mark/OI history prefer the dedicated tools
        get_funding_history / get_mark_price_history / get_oi_history (or prefix the
        symbol manually: FUNDING:BTCUSD, MARK:BTCUSD, OI:BTCUSD).
        """
        return await client.get(
            "/history/candles",
            params={"symbol": symbol, "resolution": resolution, "start": start, "end": end},
        )

    @mcp.tool()
    async def get_settlement_prices(
        contract_types: list[str] | None = Field(
            default=None,
            description="Filter expired products by contract type (e.g. call_options, put_options, futures).",
        ),
        page_size: int = Field(default=100, ge=1, le=500),
        after: str | None = Field(default=None, description="Cursor from previous response's meta.after."),
    ) -> dict[str, Any]:
        """Historical settlement prices for expired/settled derivatives.

        Use for post-expiry P&L reconciliation, backtesting against realized
        settlements, or trade journaling. Each product object in the response carries
        the settlement details (settlement_time, settlement_price) alongside the
        product metadata. Returns paginated result + meta cursors.

        Under the hood this is `list_products(states=["expired"])` — Delta exposes
        settlement data through the products endpoint rather than a dedicated path.
        """
        return await client.get(
            "/products",
            params={
                "contract_types": _csv(contract_types),
                "states": "expired",
                "page_size": page_size,
                "after": after,
            },
        )

    @mcp.tool()
    async def get_funding_history(
        symbol: str = Field(description="Perpetual symbol, e.g. BTCUSD or ETHUSD."),
        resolution: Resolution = "1h",
        start: int = Field(description="Unix timestamp in seconds, inclusive."),
        end: int = Field(description="Unix timestamp in seconds, inclusive."),
    ) -> dict[str, Any]:
        """Historical funding rate candles for a perpetual.

        Use this for basis-trade analysis, computing realized funding over a holding
        period, or spotting funding-rate extremes. Returns OHLC over the funding rate.
        """
        return await client.get(
            "/history/candles",
            params={
                "symbol": f"FUNDING:{symbol}",
                "resolution": resolution,
                "start": start,
                "end": end,
            },
        )

    @mcp.tool()
    async def get_mark_price_history(
        symbol: str = Field(description="Product symbol, e.g. BTCUSD or C-BTC-66400-010824."),
        resolution: Resolution = "1m",
        start: int = Field(description="Unix timestamp in seconds, inclusive."),
        end: int = Field(description="Unix timestamp in seconds, inclusive."),
    ) -> dict[str, Any]:
        """Historical mark-price candles for a product.

        Useful for reconstructing P&L curves, slippage checks against mark, or
        comparing your fill price to fair value across an interval.
        """
        return await client.get(
            "/history/candles",
            params={
                "symbol": f"MARK:{symbol}",
                "resolution": resolution,
                "start": start,
                "end": end,
            },
        )

    @mcp.tool()
    async def get_oi_history(
        symbol: str = Field(description="Product symbol, e.g. BTCUSD or ETHUSD."),
        resolution: Resolution = "1h",
        start: int = Field(description="Unix timestamp in seconds, inclusive."),
        end: int = Field(description="Unix timestamp in seconds, inclusive."),
    ) -> dict[str, Any]:
        """Historical open-interest candles for a product.

        Use to detect positioning extremes, squeeze risk, or OI build-up around
        events. Returns OHLC over open interest.
        """
        return await client.get(
            "/history/candles",
            params={
                "symbol": f"OI:{symbol}",
                "resolution": resolution,
                "start": start,
                "end": end,
            },
        )

    @mcp.tool()
    async def get_options_chain(
        underlying: str = Field(description="Underlying asset symbol, e.g. BTC or ETH."),
        expiry_date: str = Field(description="Expiry date in DD-MM-YYYY format (note: different from /products)."),
    ) -> dict[str, Any]:
        """Options chain (all call+put tickers) for one underlying on one expiry."""
        return await client.get(
            "/tickers",
            params={
                "contract_types": "call_options,put_options",
                "underlying_asset_symbols": underlying,
                "expiry_date": expiry_date,
            },
        )

    @mcp.tool()
    async def get_indices() -> dict[str, Any]:
        """Spot price indices that Delta builds by combining prices from prominent exchanges.

        These indices underlie Delta's futures and options. Each index returns its
        constituent exchanges + weights, `index_type` (spot_pair / fixed_interest_rate /
        floating_interest_rate), tick_size, and the underlying/quoting asset.

        Use when you need to understand how a product's mark or settlement price is
        constructed, audit settlement composition, or assess outage/concentration risk
        from a single constituent exchange.
        """
        return await client.get("/indices")

    @mcp.tool()
    async def get_reference_data() -> dict[str, Any]:
        """Merged assets + indices listing — useful for symbol/asset metadata lookups.

        For index-only queries (composition, weights, index_type) prefer get_indices.
        """
        assets = await client.get("/assets")
        indices = await client.get("/indices")
        return {"assets": assets, "indices": indices}
