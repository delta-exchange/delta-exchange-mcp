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


_OPTION_CONTRACT_TYPES = {"call_options", "put_options"}


def _is_option(pos: dict[str, Any]) -> bool:
    ctype = pos.get("contract_type")
    if not ctype and isinstance(pos.get("product"), dict):
        ctype = pos["product"].get("contract_type")
    if ctype in _OPTION_CONTRACT_TYPES:
        return True
    symbol = pos.get("product_symbol") or ""
    return isinstance(symbol, str) and (symbol.startswith("C-") or symbol.startswith("P-"))


def _lookup_contract_value(pos: dict[str, Any]) -> float | None:
    raw = pos.get("contract_value")
    if raw is None and isinstance(pos.get("product"), dict):
        raw = pos["product"].get("contract_value")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _patch_short_option_pnl(result: Any) -> Any:
    """Server returns unsigned `unrealized_pnl` (premium value) for short options.
    Recompute the signed P&L for those positions; leave others untouched.

    Reason: upstream bug — for short option positions the API returns
    mark_price * |size| * contract_value (premium value), ignoring the position
    direction. Futures and long options are unaffected. See GH #9.
    """
    if not isinstance(result, dict):
        return result
    positions = result.get("result")
    if not isinstance(positions, list):
        return result
    for pos in positions:
        if not isinstance(pos, dict) or not _is_option(pos):
            continue
        size_raw = pos.get("size")
        try:
            size = int(size_raw)
        except (TypeError, ValueError):
            continue
        if size >= 0:
            continue
        try:
            entry = float(pos["entry_price"])
            mark = float(pos["mark_price"])
        except (KeyError, TypeError, ValueError):
            continue
        contract_value = _lookup_contract_value(pos)
        if contract_value is None:
            continue
        pos["unrealized_pnl"] = str((mark - entry) * size * contract_value)
    return result


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

        Note on `unrealized_pnl` for short options: the upstream API returns
        the absolute premium value (unsigned) rather than the signed mark-to-market
        P&L. This tool patches the field client-side using
        (mark_price - entry_price) * size * contract_value, with `size` signed.
        Long options and futures pass through unchanged. See GH #9.
        """
        result = await client.get(
            "/positions/margined",
            params={
                "product_ids": _csv_ints(product_ids),
                "contract_types": _csv(contract_types),
            },
            auth=True,
        )
        return _patch_short_option_pnl(result)

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
