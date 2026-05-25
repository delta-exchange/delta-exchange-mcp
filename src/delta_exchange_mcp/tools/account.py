"""Authenticated read-only account tools. Registered when DELTA_API_KEY/SECRET are set."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from delta_exchange_mcp.client import DeltaClient


def _csv(values: list[str] | None) -> str | None:
    if not values:
        return None
    return ",".join(values)


def _csv_ints(values: list[int] | None) -> str | None:
    if not values:
        return None
    return ",".join(str(v) for v in values)


def register(mcp: FastMCP, client: DeltaClient) -> None:
    @mcp.tool()
    async def get_positions(
        product_id: int | None = Field(default=None, description="Single product id."),
        underlying_asset_symbol: str | None = Field(
            default=None,
            description="Underlying asset symbol (e.g. BTC) — returns all positions under it.",
        ),
    ) -> dict[str, Any]:
        """Open position(s). Pass exactly one of product_id or underlying_asset_symbol.

        Returns only `entry_price` and `size`. For `realized_pnl`, `realized_funding`,
        `margin`, `mark_price`, `unrealized_pnl`, `liquidation_price` and other analytical
        fields, call `get_margined_positions` instead.
        """
        if (product_id is None) == (not underlying_asset_symbol):
            raise ValueError("pass exactly one of product_id or underlying_asset_symbol")
        return await client.get(
            "/positions",
            params={"product_id": product_id, "underlying_asset_symbol": underlying_asset_symbol},
            auth=True,
        )

    @mcp.tool()
    async def get_margined_positions(
        product_ids: list[int] | None = Field(default=None, description="Max 10 product ids."),
        contract_types: list[str] | None = Field(
            default=None,
            description="Subset of: perpetual_futures, call_options, put_options.",
        ),
    ) -> dict[str, Any]:
        """All open margined positions, optionally filtered.

        Computing notional exposure (especially for options):
            notional_usd = abs(size) * contract_value * index_price

        Use `index_price` (spot of the underlying), NOT `mark_price` — mark_price for an
        option is the option premium, so multiplying by it gives the premium value, not the
        underlying exposure. For a short BTC call with size 10, contract_value 0.001 and
        BTC index 54_270, notional is 10 * 0.001 * 54_270 = $542.70, not the ~$7.60 you'd
        get by multiplying the premium.

        `size` is signed: positive = long, negative = short.
        """
        return await client.get(
            "/positions/margined",
            params={
                "product_ids": _csv_ints(product_ids),
                "contract_types": _csv(contract_types),
            },
            auth=True,
        )

    @mcp.tool()
    async def get_wallet_balances() -> dict[str, Any]:
        """Wallet balances across all assets.

        Fields: asset_symbol, balance, available_balance, position_margin,
        strategy_blocked_amount.

        `strategy_blocked_amount` is collateral reserved by an active Algo Marketplace
        strategy subscription. It is normal and expected — do not flag it as a risk or
        anomaly. To release it, the user must stop or unsubscribe from the strategy.
        """
        return await client.get("/wallet/balances", auth=True)

    @mcp.tool()
    async def get_wallet_transactions(
        asset_ids: list[int] | None = Field(default=None, description="Filter by asset ids."),
        transaction_types: list[str] | None = Field(
            default=None,
            description="Filter by transaction type (e.g. deposit, withdrawal, funding, settlement, commission).",
        ),
        start_time_us: int | None = Field(default=None, description="Microseconds epoch."),
        end_time_us: int | None = None,
        page_size: int = Field(default=50, ge=1, le=200),
        after: str | None = None,
        before: str | None = None,
    ) -> dict[str, Any]:
        """Wallet transaction history. Paginated. Timestamps are microseconds."""
        return await client.get(
            "/wallet/transactions",
            params={
                "asset_ids": _csv_ints(asset_ids),
                "transaction_types": _csv(transaction_types),
                "start_time": start_time_us,
                "end_time": end_time_us,
                "page_size": page_size,
                "after": after,
                "before": before,
            },
            auth=True,
        )

    @mcp.tool()
    async def get_fills(
        product_ids: list[int] | None = None,
        contract_types: list[str] | None = None,
        start_time_us: int | None = Field(default=None, description="Microseconds epoch."),
        end_time_us: int | None = None,
        page_size: int = Field(default=50, ge=1, le=200),
        after: str | None = None,
    ) -> dict[str, Any]:
        """Your trade fills (executed trades). Paginated. Timestamps are microseconds."""
        return await client.get(
            "/fills",
            params={
                "product_ids": _csv_ints(product_ids),
                "contract_types": _csv(contract_types),
                "start_time": start_time_us,
                "end_time": end_time_us,
                "page_size": page_size,
                "after": after,
            },
            auth=True,
        )

    @mcp.tool()
    async def get_open_orders(
        product_ids: list[int] | None = Field(default=None, description="Max 10 product ids."),
        states: list[str] | None = Field(default=None, description="Subset of: open, pending."),
        contract_types: list[str] | None = Field(default=None),
        page_size: int = Field(default=50, ge=1, le=200),
        after: str | None = Field(default=None, description="Cursor from previous response's meta.after."),
    ) -> dict[str, Any]:
        """Current open/pending orders. Paginated via meta.after / meta.before."""
        return await client.get(
            "/orders",
            params={
                "product_ids": _csv_ints(product_ids),
                "states": _csv(states),
                "contract_types": _csv(contract_types),
                "page_size": page_size,
                "after": after,
            },
            auth=True,
        )

    @mcp.tool()
    async def get_order_history(
        product_ids: list[int] | None = None,
        contract_types: list[str] | None = None,
        order_types: list[str] | None = Field(
            default=None,
            description="market, limit, stop_market, stop_limit, all_stop.",
        ),
        start_time_us: int | None = Field(default=None, description="Microseconds epoch (note: micro, not milli)."),
        end_time_us: int | None = None,
        page_size: int = Field(default=50, ge=1, le=200),
        after: str | None = None,
    ) -> dict[str, Any]:
        """Closed / cancelled orders, filterable + paginated. Timestamps are microseconds."""
        return await client.get(
            "/orders/history",
            params={
                "product_ids": _csv_ints(product_ids),
                "contract_types": _csv(contract_types),
                "order_types": _csv(order_types),
                "start_time": start_time_us,
                "end_time": end_time_us,
                "page_size": page_size,
                "after": after,
            },
            auth=True,
        )

    @mcp.tool()
    async def get_order_by_id(
        order_id: int | None = Field(default=None, description="Delta-assigned order id."),
        client_order_id: str | None = Field(default=None, description="Your client_order_id if you set one."),
    ) -> dict[str, Any]:
        """Fetch a single order by id or client_order_id. Exactly one must be provided."""
        if (order_id is None) == (not client_order_id):
            raise ValueError("pass exactly one of order_id or client_order_id")
        if order_id is not None:
            return await client.get(f"/orders/{order_id}", auth=True)
        return await client.get(f"/orders/client_order_id/{client_order_id}", auth=True)

    @mcp.tool()
    async def get_product_leverage(
        product_id: int = Field(description="Product id to fetch leverage for."),
    ) -> dict[str, Any]:
        """Configured order leverage for a product."""
        return await client.get(f"/products/{product_id}/orders/leverage", auth=True)

    @mcp.tool()
    async def get_trading_stats() -> dict[str, Any]:
        """Account-level trading volume / stats."""
        return await client.get("/stats", auth=True)

    @mcp.tool()
    async def get_trading_preferences() -> dict[str, Any]:
        """User trading preferences (margin mode, notifications, etc.)."""
        return await client.get("/users/trading_preferences", auth=True)

    @mcp.tool()
    async def get_profile() -> dict[str, Any]:
        """User profile."""
        return await client.get("/profile", auth=True)
