"""Public market-data tools (M1)."""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from delta_exchange_mcp.client import DeltaClient

Resolution = Literal["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "1d", "1w"]


def _csv(values: list[str] | None) -> str | None:
    if not values:
        return None
    return ",".join(values)


def register(mcp: MCPServer, client: DeltaClient) -> None:
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
