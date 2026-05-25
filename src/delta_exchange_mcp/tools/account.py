"""Authenticated read-only account tools. Registered when DELTA_API_KEY/SECRET are set."""

from __future__ import annotations

from pathlib import Path
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


def _safe_export_path(output_path: str) -> Path:
    """Resolve `output_path` and reject anything outside cwd or $HOME.

    Guards against path-traversal and absolute writes to unexpected locations,
    since this tool is the only one that writes to the user's disk.
    """
    raw = Path(output_path).expanduser()
    resolved = (Path.cwd() / raw).resolve() if not raw.is_absolute() else raw.resolve()
    cwd = Path.cwd().resolve()
    home = Path.home().resolve()
    if not (resolved.is_relative_to(cwd) or resolved.is_relative_to(home)):
        raise ValueError(
            f"output_path must be inside cwd ({cwd}) or home ({home}); got {resolved}"
        )
    return resolved


def register(mcp: FastMCP, client: DeltaClient) -> None:
    @mcp.tool()
    async def get_positions(
        product_id: int | None = Field(default=None, description="Single product id."),
        underlying_asset_symbol: str | None = Field(
            default=None,
            description="Underlying asset symbol (e.g. BTC) — returns all positions under it.",
        ),
    ) -> dict[str, Any]:
        """Open position(s). Pass exactly one of product_id or underlying_asset_symbol."""
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
        """All open margined positions, optionally filtered."""
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
        """Wallet balances across all assets. Fields: asset_symbol, balance, available_balance, position_margin."""
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

    @mcp.tool()
    async def bulk_fills_export(
        output_path: str = Field(
            description=(
                "Where to write the CSV. Must be inside the current working directory or "
                "the user's home directory; ~ is expanded."
            )
        ),
        start_time_us: int | None = Field(
            default=None, description="Window start in microseconds epoch."
        ),
        end_time_us: int | None = Field(
            default=None, description="Window end in microseconds epoch."
        ),
        product_ids: list[int] | None = None,
        contract_types: list[str] | None = None,
    ) -> dict[str, Any]:
        """Bulk-export your fills to a CSV file on disk.

        Use this for full-history analysis, tax reports, or backtesting against your
        own trade record — anything where the paginated `get_fills` would require
        dozens of round-trips. Calls `/fills/history/download/csv` and writes the
        raw CSV to `output_path`. Returns `{path, row_count, size_bytes}`.

        The output path is restricted to the current working directory or the user's
        home directory to keep the write scope predictable.
        """
        resolved = _safe_export_path(output_path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        data = await client.get_raw(
            "/fills/history/download/csv",
            params={
                "product_ids": _csv_ints(product_ids),
                "contract_types": _csv(contract_types),
                "start_time": start_time_us,
                "end_time": end_time_us,
            },
            auth=True,
        )
        resolved.write_bytes(data)
        # Row count = newline-delimited rows minus header. Be lenient if the response
        # is empty or missing a trailing newline.
        row_count = max(0, data.count(b"\n") - 1) if data else 0
        return {
            "path": str(resolved),
            "row_count": row_count,
            "size_bytes": len(data),
        }
