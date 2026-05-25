"""Public market-data tools (M1)."""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from delta_exchange_mcp.client import DeltaClient

Resolution = Literal["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "1d", "1w"]


def _csv(values: list[str] | None) -> str | None:
    if not values:
        return None
    return ",".join(values)


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
        """OHLC candles. For funding/mark/OI history prefix the symbol: FUNDING:BTCUSD, MARK:BTCUSD, OI:BTCUSD."""
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
    async def get_reference_data() -> dict[str, Any]:
        """Merged assets + indices listing — useful for symbol/asset metadata lookups."""
        assets = await client.get("/assets")
        indices = await client.get("/indices")
        return {"assets": assets, "indices": indices}
