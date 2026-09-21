"""Authenticated trading tools (mutations).

Registered whenever DELTA_API_KEY/SECRET are set. The API key's own permissions are the
authorization boundary: a key without Trading enabled is rejected by Delta, and that error
is surfaced verbatim. Mutations never auto-retry (see DeltaClient retry policy) — a timeout
is surfaced, not silently re-sent.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from delta_exchange_mcp.client import DeltaClient

TOOL_NAMES = frozenset(
    {
        "place_order",
        "edit_order",
        "cancel_order",
        "cancel_all_orders",
        "place_batch_orders",
        "edit_batch_orders",
        "cancel_batch_orders",
        "place_bracket_order",
        "edit_bracket_order",
        "set_product_leverage",
        "adjust_position_margin",
        "close_all_positions",
        "configure_auto_topup",
    }
)

_STOP_TRIGGER_METHODS = "mark_price, last_traded_price, spot_price"


def _bs(value: bool | None) -> str | None:
    """Delta's order-level flags are string enums "true"/"false", not JSON booleans."""
    if value is None:
        return None
    return "true" if value else "false"


def _csv(values: list[str] | None) -> str | None:
    if not values:
        return None
    return ",".join(values)


def _clean(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop None-valued keys so we never send `field: null` in a request body."""
    return {k: v for k, v in payload.items() if v is not None}


def _require_one(product_id: int | None, product_symbol: str | None) -> None:
    if (product_id is None) == (product_symbol is None):
        raise ValueError("pass exactly one of product_id or product_symbol")


def register(mcp: FastMCP, client: DeltaClient) -> None:
    _uid_cache: dict[tuple[str, str], int] = {}

    def mutation_tool(
        function: Callable[..., Awaitable[Any]],
    ) -> Callable[..., Awaitable[Any]]:
        """Pin every request in one dispatched mutation to the same client state."""

        @wraps(function)
        async def pinned(*args: Any, **kwargs: Any) -> Any:
            async with client.pin():
                return await function(*args, **kwargs)

        return mcp.tool()(pinned)

    async def _user_id() -> int:
        # Keyed by the whole HTTP identity, not cached once per process: credentials now
        # rotate without a restart, and a user_id left over from the previous account signs
        # cleanly under the new key while naming someone else's positions to close. The
        # base URL is part of the key because one key string can exist on both testnet and
        # prod, where it identifies two different accounts.
        live = client.config
        cache_key = (live.base_url, live.api_key or "")
        if cache_key not in _uid_cache:
            prof = await client.get("/profile", auth=True)
            inner = prof.get("result", prof) if isinstance(prof, dict) else {}
            uid = inner.get("id") or inner.get("user_id") if isinstance(inner, dict) else None
            if uid is None:
                raise ValueError("could not resolve user_id from /profile")
            _uid_cache[cache_key] = int(uid)
        return _uid_cache[cache_key]

    async def _finish(method: str, path: str, payload: dict[str, Any]) -> Any:
        payload = _clean(payload)
        sender = {"POST": client.post, "PUT": client.put, "DELETE": client.delete}[method]
        return await sender(path, payload, auth=True)

    # ---------------------------------------------------------------- single order

    @mutation_tool
    async def place_order(
        size: int = Field(description="Order size in contracts."),
        side: str = Field(description="buy or sell."),
        order_type: str = Field(description="limit_order or market_order."),
        product_id: int | None = Field(default=None, description="Product id (or pass product_symbol)."),
        product_symbol: str | None = Field(default=None, description="e.g. BTCUSD (or pass product_id)."),
        limit_price: str | None = Field(default=None, description="Required for limit_order."),
        stop_order_type: str | None = Field(default=None, description="stop_loss_order or take_profit_order."),
        stop_price: str | None = Field(default=None, description="Trigger price for stop orders."),
        trail_amount: str | None = Field(default=None, description="Trailing-stop amount."),
        stop_trigger_method: str | None = Field(default=None, description=_STOP_TRIGGER_METHODS),
        time_in_force: str | None = Field(default=None, description="gtc or ioc."),
        post_only: bool | None = Field(default=None, description="Reject if it would take liquidity."),
        reduce_only: bool | None = Field(default=None, description="Only reduce an existing position."),
        client_order_id: str | None = Field(default=None, description="Your id, max 32 chars."),
        bracket_stop_loss_price: str | None = Field(default=None, description="Bracket SL trigger price."),
        bracket_stop_loss_limit_price: str | None = Field(default=None, description="Bracket SL limit price."),
        bracket_take_profit_price: str | None = Field(default=None, description="Bracket TP trigger price."),
        bracket_take_profit_limit_price: str | None = Field(default=None, description="Bracket TP limit price."),
        bracket_trail_amount: str | None = Field(default=None, description="Bracket trailing-stop amount."),
        bracket_stop_trigger_method: str | None = Field(default=None, description=_STOP_TRIGGER_METHODS),
    ) -> dict[str, Any]:
        """Place a single order. Pass exactly one of product_id or product_symbol.

        The API requires limit_price for limit_order and ignores it on market_order. For stop
        orders set stop_order_type plus stop_price (or trail_amount).

        To attach a bracket (TP/SL) you can later edit, pass the bracket_* params here — this
        creates an *entry-order* bracket whose id (the returned order id) is what
        edit_bracket_order expects. (place_bracket_order instead attaches a bracket to an open
        position; those legs are not editable via edit_bracket_order — cancel and re-place.)
        Prices are sent exactly as given; the API rejects a price that is off the product tick.
        """
        _require_one(product_id, product_symbol)
        payload = {
            "size": size,
            "side": side,
            "order_type": order_type,
            "product_id": product_id,
            "product_symbol": product_symbol,
            "limit_price": limit_price,
            "stop_order_type": stop_order_type,
            "stop_price": stop_price,
            "trail_amount": trail_amount,
            "stop_trigger_method": stop_trigger_method,
            "time_in_force": time_in_force,
            "post_only": _bs(post_only),
            "reduce_only": _bs(reduce_only),
            "client_order_id": client_order_id,
            "bracket_stop_loss_price": bracket_stop_loss_price,
            "bracket_stop_loss_limit_price": bracket_stop_loss_limit_price,
            "bracket_take_profit_price": bracket_take_profit_price,
            "bracket_take_profit_limit_price": bracket_take_profit_limit_price,
            "bracket_trail_amount": bracket_trail_amount,
            "bracket_stop_trigger_method": bracket_stop_trigger_method,
        }
        return await _finish("POST", "/orders", payload)

    @mutation_tool
    async def edit_order(
        id: int = Field(description="Order id to edit."),
        size: int = Field(description="Total size after the edit."),
        product_id: int | None = Field(default=None, description="Product id (or pass product_symbol)."),
        product_symbol: str | None = Field(default=None, description="e.g. BTCUSD (or pass product_id)."),
        limit_price: str | None = Field(default=None, description="New limit price."),
        stop_price: str | None = Field(default=None, description="New stop trigger price."),
        trail_amount: str | None = Field(default=None, description="New trailing-stop amount."),
        post_only: bool | None = Field(default=None, description="Reject if it would take liquidity."),
    ) -> dict[str, Any]:
        """Edit an open order. Pass exactly one of product_id or product_symbol.

        Prices are sent exactly as given; the API rejects a price that is off the product tick.
        """
        _require_one(product_id, product_symbol)
        payload = {
            "id": id,
            "size": size,
            "product_id": product_id,
            "product_symbol": product_symbol,
            "limit_price": limit_price,
            "stop_price": stop_price,
            "trail_amount": trail_amount,
            "post_only": _bs(post_only),
        }
        return await _finish("PUT", "/orders", payload)

    @mutation_tool
    async def cancel_order(
        product_id: int = Field(description="Product id the order belongs to."),
        id: int | None = Field(default=None, description="Order id to cancel."),
        client_order_id: str | None = Field(default=None, description="Your client_order_id."),
    ) -> dict[str, Any]:
        """Cancel a single order by id or client_order_id."""
        if (id is None) == (client_order_id is None):
            raise ValueError("pass exactly one of id or client_order_id")
        payload = {"product_id": product_id, "id": id, "client_order_id": client_order_id}
        return await _finish("DELETE", "/orders", payload)

    @mutation_tool
    async def cancel_all_orders(
        product_id: int | None = Field(default=None, description="Limit to one product."),
        contract_types: list[str] | None = Field(
            default=None, description="Limit to contract types (ignored if product_id is set)."
        ),
    ) -> dict[str, Any]:
        """Cancel open orders. WARNING: with no filters this cancels ALL of your open orders.

        Limit orders, stop orders and reduce-only orders are all cancelled. Narrow the scope
        with product_id or contract_types.
        """
        payload = {
            "product_id": product_id,
            "contract_types": _csv(contract_types),
            "cancel_limit_orders": "true",
            "cancel_stop_orders": "true",
            "cancel_reduce_only_orders": "true",
        }
        return await _finish("DELETE", "/orders/all", payload)

    # ---------------------------------------------------------------- batch orders

    @mutation_tool
    async def place_batch_orders(
        orders: list[dict[str, Any]] = Field(
            description="Orders, each {size, side, order_type, limit_price?, "
            "time_in_force?, post_only?, client_order_id?}. All same contract. No IOC/stop."
        ),
        product_id: int | None = Field(default=None, description="Product id (or pass product_symbol)."),
        product_symbol: str | None = Field(default=None, description="e.g. BTCUSD (or pass product_id)."),
    ) -> dict[str, Any]:
        """Place orders on one contract in a single request.

        The API's response is returned as-is. When it comes back with fewer orders than were
        sent, the ones missing from the response were not accepted.
        """
        _require_one(product_id, product_symbol)
        payload = {
            "product_id": product_id,
            "product_symbol": product_symbol,
            "orders": [_clean(o) for o in orders],
        }
        return await _finish("POST", "/orders/batch", payload)

    @mutation_tool
    async def edit_batch_orders(
        orders: list[dict[str, Any]] = Field(
            description="Edits, each {id, size, order_type, limit_price?, post_only?}."
        ),
        product_id: int | None = Field(default=None, description="Product id (or pass product_symbol)."),
        product_symbol: str | None = Field(default=None, description="e.g. BTCUSD (or pass product_id)."),
    ) -> dict[str, Any]:
        """Edit orders on one contract in a single request."""
        _require_one(product_id, product_symbol)
        payload = {
            "product_id": product_id,
            "product_symbol": product_symbol,
            "orders": [_clean(o) for o in orders],
        }
        return await _finish("PUT", "/orders/batch", payload)

    @mutation_tool
    async def cancel_batch_orders(
        orders: list[dict[str, Any]] = Field(
            description="Orders to cancel, each {id} or {client_order_id}."
        ),
        product_id: int | None = Field(default=None, description="Product id (or pass product_symbol)."),
        product_symbol: str | None = Field(default=None, description="e.g. BTCUSD (or pass product_id)."),
    ) -> dict[str, Any]:
        """Cancel orders on one contract in a single request."""
        _require_one(product_id, product_symbol)
        payload = {
            "product_id": product_id,
            "product_symbol": product_symbol,
            "orders": [_clean(o) for o in orders],
        }
        return await _finish("DELETE", "/orders/batch", payload)

    # ---------------------------------------------------------------- bracket orders

    @mutation_tool
    async def place_bracket_order(
        product_id: int | None = Field(default=None, description="Product id (or pass product_symbol)."),
        product_symbol: str | None = Field(default=None, description="e.g. BTCUSD (or pass product_id)."),
        stop_loss_order: dict[str, Any] | None = Field(
            default=None, description="{order_type, stop_price, limit_price?, trail_amount?}."
        ),
        take_profit_order: dict[str, Any] | None = Field(
            default=None, description="{order_type, stop_price, limit_price?}."
        ),
        bracket_stop_trigger_method: str | None = Field(default=None, description=_STOP_TRIGGER_METHODS),
    ) -> dict[str, Any]:
        """Attach a take-profit / stop-loss bracket to a position. Provide at least one leg.

        Note: these legs are not editable via edit_bracket_order (cancel + re-place to change
        them). Prices are sent exactly as given.
        """
        _require_one(product_id, product_symbol)
        if stop_loss_order is None and take_profit_order is None:
            raise ValueError("provide at least one of stop_loss_order or take_profit_order")
        payload = {
            "product_id": product_id,
            "product_symbol": product_symbol,
            "stop_loss_order": _clean(stop_loss_order) if stop_loss_order else None,
            "take_profit_order": _clean(take_profit_order) if take_profit_order else None,
            "bracket_stop_trigger_method": bracket_stop_trigger_method,
        }
        return await _finish("POST", "/orders/bracket", payload)

    @mutation_tool
    async def edit_bracket_order(
        id: int = Field(description="Order id whose bracket params to update."),
        product_id: int | None = Field(default=None, description="Product id (or pass product_symbol)."),
        product_symbol: str | None = Field(default=None, description="e.g. BTCUSD (or pass product_id)."),
        bracket_stop_loss_price: str | None = Field(default=None, description="Stop-loss trigger price."),
        bracket_stop_loss_limit_price: str | None = Field(default=None, description="Stop-loss limit price."),
        bracket_take_profit_price: str | None = Field(default=None, description="Take-profit trigger price."),
        bracket_take_profit_limit_price: str | None = Field(default=None, description="Take-profit limit price."),
        bracket_trail_amount: str | None = Field(default=None, description="Trailing-stop amount."),
        bracket_stop_trigger_method: str | None = Field(default=None, description=_STOP_TRIGGER_METHODS),
    ) -> dict[str, Any]:
        """Edit the bracket (TP/SL) params on an existing order.

        `id` is the *entry order* id (an order created with bracket_* params, e.g. via
        place_order(..., bracket_take_profit_price=...)). It does NOT accept the leg ids of a
        position bracket created by place_bracket_order. Prices are sent exactly as given.
        """
        _require_one(product_id, product_symbol)
        payload = {
            "id": id,
            "product_id": product_id,
            "product_symbol": product_symbol,
            "bracket_stop_loss_price": bracket_stop_loss_price,
            "bracket_stop_loss_limit_price": bracket_stop_loss_limit_price,
            "bracket_take_profit_price": bracket_take_profit_price,
            "bracket_take_profit_limit_price": bracket_take_profit_limit_price,
            "bracket_trail_amount": bracket_trail_amount,
            "bracket_stop_trigger_method": bracket_stop_trigger_method,
        }
        return await _finish("PUT", "/orders/bracket", payload)

    # ---------------------------------------------------------------- positions & leverage

    @mutation_tool
    async def set_product_leverage(
        product_id: int = Field(description="Product id to set order leverage for."),
        leverage: str = Field(description="Leverage multiplier, e.g. '10'."),
    ) -> dict[str, Any]:
        """Set order leverage for a product."""
        return await _finish(
            "POST", f"/products/{product_id}/orders/leverage", {"leverage": leverage}
        )

    @mutation_tool
    async def adjust_position_margin(
        product_id: int = Field(description="Product id of the position."),
        delta_margin: str = Field(description="Margin to add (positive) or remove (negative), e.g. '5.0'."),
    ) -> dict[str, Any]:
        """Add or remove isolated margin on a position."""
        payload = {"product_id": product_id, "delta_margin": delta_margin}
        return await _finish("POST", "/positions/change_margin", payload)

    @mutation_tool
    async def close_all_positions() -> dict[str, Any]:
        """Close every open position on the account, both cross/portfolio and isolated.

        WARNING: this closes the whole account. There is no narrower scope.

        Your user_id is required by the API and is resolved automatically from your profile
        (fetched once and cached) — you do not pass it.
        """
        payload = {
            "close_all_portfolio": True,
            "close_all_isolated": True,
            "user_id": await _user_id(),
        }
        return await _finish("POST", "/positions/close_all", payload)

    @mutation_tool
    async def configure_auto_topup(
        product_id: int = Field(description="Product id of the position."),
        auto_topup: bool = Field(description="Enable or disable auto top-up for this position."),
    ) -> dict[str, Any]:
        """Override auto top-up for a single position (otherwise inherits the account setting)."""
        payload = {"product_id": product_id, "auto_topup": auto_topup}
        return await _finish("PUT", "/positions/auto_topup", payload)
