"""Authenticated trading tools (mutations).

Registered only when DELTA_API_KEY/SECRET are set AND DELTA_MCP_MODE=trade. Every tool
takes a `dry_run` flag that validates and echoes the payload without sending it, and every
call (dry-run or real) is recorded to the audit log. Mutations never auto-retry (see
DeltaClient retry policy) — a timeout is surfaced, not silently re-sent.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from functools import wraps
from typing import Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from delta_exchange_mcp import hints
from delta_exchange_mcp.audit_log import AuditLog
from delta_exchange_mcp.client import DeltaClient
from delta_exchange_mcp.errors import DeltaApiError

logger = logging.getLogger("delta_exchange_mcp")

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

_MAX_BATCH = 50
_STOP_TRIGGER_METHODS = "mark_price, last_traded_price, spot_price"
MUTATING_TOOL_META_KEY = "delta.exchange/mutating"
_MUTATING_TOOL_META = {MUTATING_TOOL_META_KEY: True}
_REVOKED_MESSAGE = "trading was disabled while this request was being prepared; no mutation was sent"
_CHECK_FAILED_MESSAGE = "trading authorization could not be confirmed; no mutation was sent"

FinalTradingCheck = Callable[[], bool]


class FinalTradingCheckError(RuntimeError):
    """The point-of-use authorization checker failed."""


@dataclass(frozen=True)
class TradeLease:
    """The gate generation and final checker captured for one tool request."""

    generation: int
    final_check: FinalTradingCheck


@dataclass
class TradeGate:
    """An invalidatable lease for already-dispatched trading tool calls."""

    generation: int = 0
    armed: bool = True
    _final_check: ContextVar[FinalTradingCheck | None] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        self._final_check = ContextVar(
            f"delta_trade_final_check_{id(self)}", default=None
        )

    def bind_final_check(self, checker: FinalTradingCheck | None) -> None:
        """Bind a storage-backed checker to the current request task."""
        self._final_check.set(checker)

    def arm(self) -> None:
        """Authorize future mutations after the request-scoped check succeeds."""
        if not self.armed:
            self.generation += 1
        self.armed = True

    def lease(self) -> TradeLease:
        if not self.armed:
            raise RuntimeError(_REVOKED_MESSAGE)
        checker = self._final_check.get() or self._in_memory_check
        self._final_check.set(None)
        return TradeLease(generation=self.generation, final_check=checker)

    def revoke(self) -> None:
        self.armed = False
        self.generation += 1

    def accepts(self, lease: TradeLease | None) -> bool:
        if not self.armed or lease is None or lease.generation != self.generation:
            return False
        try:
            return lease.final_check() is True
        except Exception as exc:
            raise FinalTradingCheckError(_CHECK_FAILED_MESSAGE) from exc

    def _in_memory_check(self) -> bool:
        return self.armed


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


def _validate_bracket_sl(stop_loss_price: str | None, trail_amount: str | None) -> None:
    """A bracket stop-loss is either a fixed trigger price or a trailing amount, never both.

    Delta rejects the combination with a bad_schema error; guarding here fails fast (and in
    dry-run) with a clearer message instead of spending a live round-trip.
    """
    if stop_loss_price is not None and trail_amount is not None:
        raise ValueError("bracket stop-loss takes either a fixed price or a trailing amount, not both")


def _validate_order(order_type: str | None, limit_price: str | None, size: int | None) -> None:
    """Client-side cross-field checks the API would otherwise only catch at send time.

    Runs before _finish, so dry_run surfaces the same failures as a real call. These mirror
    documented API constraints — they are not a full server simulation.
    """
    if size is not None and size <= 0:
        raise ValueError("size must be a positive integer")
    if order_type == "limit_order" and limit_price is None:
        raise ValueError("limit_price is required for limit_order")
    if order_type == "market_order" and limit_price is not None:
        raise ValueError("market_order must not carry a limit_price (it is ignored, not a cap)")


def _flag_partial(result: Any, sent: list[dict[str, Any]]) -> Any:
    """Detect a batch partial failure and annotate the response (BUG-2).

    Delta returns only the processed orders with no per-index error, so we compare counts.
    When fewer come back than were sent, attach a `partial_failure` block (and the dropped
    ids / client_order_ids we can identify) without hiding the orders that succeeded. No-op
    for dry-run echoes (nothing was sent) and when every item came back.
    """
    if not isinstance(result, dict) or result.get("dry_run"):
        return result
    returned = result.get("result")
    if not isinstance(returned, list) or len(returned) >= len(sent):
        return result
    returned_ids = {o.get("id") for o in returned if isinstance(o, dict)}
    returned_coids = {o.get("client_order_id") for o in returned if isinstance(o, dict)}
    dropped_ids = [
        o["id"] for o in sent
        if isinstance(o, dict) and o.get("id") is not None and o["id"] not in returned_ids
    ]
    dropped_coids = [
        o["client_order_id"] for o in sent
        if isinstance(o, dict)
        and o.get("client_order_id") is not None
        and o["client_order_id"] not in returned_coids
    ]
    partial: dict[str, Any] = {
        "requested": len(sent),
        "succeeded": len(returned),
        "dropped": len(sent) - len(returned),
    }
    if dropped_ids:
        partial["dropped_ids"] = dropped_ids
    if dropped_coids:
        partial["dropped_client_order_ids"] = dropped_coids
    result["partial_failure"] = partial
    return result


def _round_to_tick(price: str, tick: Decimal) -> tuple[str, bool]:
    """Round a price string to the nearest multiple of tick.

    Returns (normalized_string, changed). On any parse failure the input is returned
    unchanged so a malformed price still reaches the API for its own error.
    """
    try:
        value = Decimal(price)
    except (InvalidOperation, ValueError, TypeError):
        return price, False
    if tick <= 0:
        return price, False
    steps = (value / tick).to_integral_value(rounding="ROUND_HALF_UP")
    snapped = (steps * tick).normalize()
    # Render without exponent/trailing-zero noise (e.g. 62000.0 not 6.2E+4).
    normalized = f"{snapped:f}"
    return normalized, normalized != price


def register(
    mcp: MCPServer,
    client: DeltaClient,
    audit: AuditLog | Callable[[], AuditLog | None] | None = None,
    gate: TradeGate | None = None,
) -> None:
    gate = gate or TradeGate()
    active_lease: ContextVar[TradeLease | None] = ContextVar(
        f"delta_trade_lease_{id(gate)}", default=None
    )
    _uid_cache: dict[str, int] = {}
    # tick_size keyed by both product id (int) and symbol (str); filled lazily.
    _tick_cache: dict[int | str, Decimal] = {}
    _tick_list_loaded = {"done": False}

    def current_audit() -> AuditLog | None:
        return audit() if callable(audit) else audit

    def mutation_tool(
        title: str, *, destructive: bool, idempotent: bool
    ) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
        """Pin every request in one dispatched mutation to the same client state.

        It also carries the annotations, which is why it takes arguments rather than
        wrapping bare. Putting them here rather than on thirteen registrations keeps the
        `_meta` mutating flag and the client-facing hints in one place, so a new mutation
        cannot acquire one and miss the other.
        """

        def decorate(
            function: Callable[..., Awaitable[Any]],
        ) -> Callable[..., Awaitable[Any]]:
            @wraps(function)
            async def pinned(*args: Any, **kwargs: Any) -> Any:
                if kwargs.get("dry_run") is True:
                    async with client.pin():
                        return await function(*args, **kwargs)
                lease = gate.lease()
                token = active_lease.set(lease)
                try:
                    async with client.pin():
                        return await function(*args, **kwargs)
                finally:
                    active_lease.reset(token)

            return mcp.tool(
                annotations=hints.mutates(
                    title, destructive=destructive, idempotent=idempotent
                ),
                meta=_MUTATING_TOOL_META,
            )(pinned)

        return decorate

    def _store_product(prod: dict[str, Any]) -> None:
        tick = prod.get("tick_size")
        if tick is None:
            return
        try:
            dec = Decimal(str(tick))
        except (InvalidOperation, ValueError):
            return
        if prod.get("id") is not None:
            _tick_cache[int(prod["id"])] = dec
        if prod.get("symbol"):
            _tick_cache[str(prod["symbol"])] = dec

    async def _tick_size(product_id: int | None, product_symbol: str | None) -> Decimal | None:
        """Resolve a product's tick_size (cached per process). Returns None if unresolvable.

        Prefers GET /products/{symbol}; for an id-only call with a cache miss it falls back
        to the full /products list once (there is no by-id single-product endpoint) and
        indexes every product. Never raises — price rounding must not block an order on a
        metadata-lookup failure.
        """
        key: int | str | None = product_symbol if product_symbol is not None else product_id
        if key is not None and key in _tick_cache:
            return _tick_cache[key]
        try:
            if product_symbol is not None:
                resp = await client.get(f"/products/{product_symbol}")
                inner = resp.get("result", resp) if isinstance(resp, dict) else None
                if isinstance(inner, dict):
                    _store_product(inner)
            elif not _tick_list_loaded["done"]:
                resp = await client.get("/products")
                products = resp.get("result", []) if isinstance(resp, dict) else []
                for prod in products if isinstance(products, list) else []:
                    if isinstance(prod, dict):
                        _store_product(prod)
                _tick_list_loaded["done"] = True
        except Exception as e:  # noqa: BLE001 — never block an order on a lookup failure
            logger.info("tick_size lookup failed for %s/%s: %s", product_id, product_symbol, e)
            return None
        return _tick_cache.get(key) if key is not None else None

    async def _normalize_prices(
        product_id: int | None, product_symbol: str | None, fields: dict[str, str | None]
    ) -> list[dict[str, str]]:
        """Round each non-None price in `fields` (mutated in place) to the nearest tick.

        Returns a list of {field, sent, normalized} for the values that changed.
        """
        if not any(v is not None for v in fields.values()):
            return []
        tick = await _tick_size(product_id, product_symbol)
        if tick is None:
            return []
        adjustments: list[dict[str, str]] = []
        for name, raw in fields.items():
            if raw is None:
                continue
            snapped, changed = _round_to_tick(raw, tick)
            fields[name] = snapped
            if changed:
                adjustments.append({"field": name, "sent": raw, "normalized": snapped})
        return adjustments

    async def _user_id() -> int:
        if "id" not in _uid_cache:
            prof = await client.get("/profile", auth=True)
            inner = prof.get("result", prof) if isinstance(prof, dict) else {}
            uid = inner.get("id") or inner.get("user_id") if isinstance(inner, dict) else None
            if uid is None:
                raise ValueError("could not resolve user_id from /profile")
            _uid_cache["id"] = int(uid)
        return _uid_cache["id"]

    async def _finish(
        tool: str, method: str, path: str, payload: dict[str, Any], *, dry_run: bool
    ) -> Any:
        payload = _clean(payload)
        log = current_audit()
        if dry_run:
            if log:
                log.record(tool, payload, dry_run=True)
            return {"dry_run": True, "method": method, "path": path, "payload": payload}
        sender = {"POST": client.post, "PUT": client.put, "DELETE": client.delete}[method]
        try:
            accepted = gate.accepts(active_lease.get())
        except FinalTradingCheckError:
            raise RuntimeError(_CHECK_FAILED_MESSAGE) from None
        if not accepted:
            if log:
                log.record(tool, payload, error=_REVOKED_MESSAGE)
            raise RuntimeError(_REVOKED_MESSAGE)
        try:
            result = await sender(path, payload, auth=True)
        except DeltaApiError as e:
            if log:
                log.record(tool, payload, error=str(e))
            raise
        if log:
            log.record(tool, payload, result=result)
        return result

    def _attach(result: Any, adjustments: list[dict[str, str]]) -> Any:
        """Surface tick-rounding adjustments on the response without hiding the result."""
        if adjustments and isinstance(result, dict):
            key = "adjustments" if result.get("dry_run") else "price_adjustments"
            result[key] = adjustments
        return result

    # ---------------------------------------------------------------- single order

    @mutation_tool("Place order", destructive=False, idempotent=False)
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
        dry_run: bool = Field(default=False, description="Validate + echo payload without sending."),
    ) -> dict[str, Any]:
        """Place a single order. Pass exactly one of product_id or product_symbol.

        limit_price is required for limit_order (and rejected on market_order). For stop
        orders set stop_order_type plus stop_price (or trail_amount).

        To attach a bracket (TP/SL) you can later edit, pass the bracket_* params here — this
        creates an *entry-order* bracket whose id (the returned order id) is what
        edit_bracket_order expects. (place_bracket_order instead attaches a bracket to an open
        position; those legs are not editable via edit_bracket_order — cancel and re-place.)
        Prices are rounded to the product's tick; see price_adjustments in the response.
        """
        _require_one(product_id, product_symbol)
        _validate_order(order_type, limit_price, size)
        _validate_bracket_sl(bracket_stop_loss_price, bracket_trail_amount)
        price_fields = {
            "limit_price": limit_price,
            "stop_price": stop_price,
            "bracket_stop_loss_price": bracket_stop_loss_price,
            "bracket_stop_loss_limit_price": bracket_stop_loss_limit_price,
            "bracket_take_profit_price": bracket_take_profit_price,
            "bracket_take_profit_limit_price": bracket_take_profit_limit_price,
        }
        adjustments = await _normalize_prices(product_id, product_symbol, price_fields)
        payload = {
            "size": size,
            "side": side,
            "order_type": order_type,
            "product_id": product_id,
            "product_symbol": product_symbol,
            "limit_price": price_fields["limit_price"],
            "stop_order_type": stop_order_type,
            "stop_price": price_fields["stop_price"],
            "trail_amount": trail_amount,
            "stop_trigger_method": stop_trigger_method,
            "time_in_force": time_in_force,
            "post_only": _bs(post_only),
            "reduce_only": _bs(reduce_only),
            "client_order_id": client_order_id,
            "bracket_stop_loss_price": price_fields["bracket_stop_loss_price"],
            "bracket_stop_loss_limit_price": price_fields["bracket_stop_loss_limit_price"],
            "bracket_take_profit_price": price_fields["bracket_take_profit_price"],
            "bracket_take_profit_limit_price": price_fields["bracket_take_profit_limit_price"],
            "bracket_trail_amount": bracket_trail_amount,
            "bracket_stop_trigger_method": bracket_stop_trigger_method,
        }
        result = await _finish("place_order", "POST", "/orders", payload, dry_run=dry_run)
        return _attach(result, adjustments)

    @mutation_tool("Edit order", destructive=True, idempotent=True)
    async def edit_order(
        id: int = Field(description="Order id to edit."),
        size: int = Field(description="Total size after the edit."),
        product_id: int | None = Field(default=None, description="Product id (or pass product_symbol)."),
        product_symbol: str | None = Field(default=None, description="e.g. BTCUSD (or pass product_id)."),
        limit_price: str | None = Field(default=None, description="New limit price."),
        stop_price: str | None = Field(default=None, description="New stop trigger price."),
        trail_amount: str | None = Field(default=None, description="New trailing-stop amount."),
        post_only: bool | None = Field(default=None, description="Reject if it would take liquidity."),
        dry_run: bool = Field(default=False, description="Validate + echo payload without sending."),
    ) -> dict[str, Any]:
        """Edit an open order. Pass exactly one of product_id or product_symbol.

        Prices are rounded to the product's tick; see price_adjustments in the response.
        """
        _require_one(product_id, product_symbol)
        _validate_order(None, None, size)  # order_type can't change on edit; just guard size
        price_fields = {"limit_price": limit_price, "stop_price": stop_price}
        adjustments = await _normalize_prices(product_id, product_symbol, price_fields)
        payload = {
            "id": id,
            "size": size,
            "product_id": product_id,
            "product_symbol": product_symbol,
            "limit_price": price_fields["limit_price"],
            "stop_price": price_fields["stop_price"],
            "trail_amount": trail_amount,
            "post_only": _bs(post_only),
        }
        result = await _finish("edit_order", "PUT", "/orders", payload, dry_run=dry_run)
        return _attach(result, adjustments)

    @mutation_tool("Cancel order", destructive=True, idempotent=True)
    async def cancel_order(
        product_id: int = Field(description="Product id the order belongs to."),
        id: int | None = Field(default=None, description="Order id to cancel."),
        client_order_id: str | None = Field(default=None, description="Your client_order_id."),
        dry_run: bool = Field(default=False, description="Validate + echo payload without sending."),
    ) -> dict[str, Any]:
        """Cancel a single order by id or client_order_id."""
        if (id is None) == (client_order_id is None):
            raise ValueError("pass exactly one of id or client_order_id")
        payload = {"product_id": product_id, "id": id, "client_order_id": client_order_id}
        return await _finish("cancel_order", "DELETE", "/orders", payload, dry_run=dry_run)

    @mutation_tool("Cancel all orders", destructive=True, idempotent=True)
    async def cancel_all_orders(
        product_id: int | None = Field(default=None, description="Limit to one product."),
        contract_types: list[str] | None = Field(
            default=None, description="Limit to contract types (ignored if product_id is set)."
        ),
        cancel_limit_orders: bool | None = Field(default=None, description="Include limit orders."),
        cancel_stop_orders: bool | None = Field(default=None, description="Include stop orders."),
        cancel_reduce_only_orders: bool | None = Field(default=None, description="Include reduce-only orders."),
        dry_run: bool = Field(default=False, description="Validate + echo payload without sending."),
    ) -> dict[str, Any]:
        """Cancel open orders. WARNING: with no filters this cancels ALL of your open orders.

        The API's cancel_limit_orders/cancel_stop_orders/cancel_reduce_only_orders flags
        default to false server-side, so a bare call would otherwise be a no-op. When none of
        them is set we default all three to true so "cancel all" actually cancels everything;
        set any flag explicitly to narrow the scope.
        """
        if cancel_limit_orders is None and cancel_stop_orders is None and cancel_reduce_only_orders is None:
            cancel_limit_orders = cancel_stop_orders = cancel_reduce_only_orders = True
        payload = {
            "product_id": product_id,
            "contract_types": _csv(contract_types),
            "cancel_limit_orders": _bs(cancel_limit_orders),
            "cancel_stop_orders": _bs(cancel_stop_orders),
            "cancel_reduce_only_orders": _bs(cancel_reduce_only_orders),
        }
        return await _finish("cancel_all_orders", "DELETE", "/orders/all", payload, dry_run=dry_run)

    # ---------------------------------------------------------------- batch orders
    # BUG-2: Delta's batch endpoints return only the processed orders with no per-index
    # error info, so itemized per-index status is impossible. _flag_partial detects a
    # count mismatch (sent N, returned M<N) and attaches a `partial_failure` block while
    # still returning the orders that went through — the caller is told some items were
    # dropped without losing sight of what is now live.

    def _check_batch(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not orders:
            raise ValueError("orders must be a non-empty list")
        if len(orders) > _MAX_BATCH:
            raise ValueError(f"batch size {len(orders)} exceeds max {_MAX_BATCH}")
        return [_clean(o) for o in orders]

    @mutation_tool("Place orders in batch", destructive=False, idempotent=False)
    async def place_batch_orders(
        orders: list[dict[str, Any]] = Field(
            description="Up to 50 orders, each {size, side, order_type, limit_price?, "
            "time_in_force?, post_only?, client_order_id?}. All same contract. No IOC/stop."
        ),
        product_id: int | None = Field(default=None, description="Product id (or pass product_symbol)."),
        product_symbol: str | None = Field(default=None, description="e.g. BTCUSD (or pass product_id)."),
        dry_run: bool = Field(default=False, description="Validate + echo payload without sending."),
    ) -> dict[str, Any]:
        """Place up to 50 orders on one contract in a single request.

        client_order_id must be unique within the batch (mirrors the single-order rejection);
        cross-checking against already-open orders is not done here.
        """
        _require_one(product_id, product_symbol)
        cleaned = _check_batch(orders)
        seen_coids: set[str] = set()
        for order in cleaned:
            _validate_order(order.get("order_type"), order.get("limit_price"), order.get("size"))
            coid = order.get("client_order_id")
            if coid is not None:
                if coid in seen_coids:
                    raise ValueError(f"duplicate client_order_id in batch: {coid}")
                seen_coids.add(coid)
        payload = {
            "product_id": product_id,
            "product_symbol": product_symbol,
            "orders": cleaned,
        }
        result = await _finish("place_batch_orders", "POST", "/orders/batch", payload, dry_run=dry_run)
        return _flag_partial(result, cleaned)

    @mutation_tool("Edit orders in batch", destructive=True, idempotent=True)
    async def edit_batch_orders(
        orders: list[dict[str, Any]] = Field(
            description="Up to 50 edits, each {id, size, order_type, limit_price?, post_only?}."
        ),
        product_id: int | None = Field(default=None, description="Product id (or pass product_symbol)."),
        product_symbol: str | None = Field(default=None, description="e.g. BTCUSD (or pass product_id)."),
        dry_run: bool = Field(default=False, description="Validate + echo payload without sending."),
    ) -> dict[str, Any]:
        """Edit up to 50 orders on one contract in a single request."""
        _require_one(product_id, product_symbol)
        cleaned = _check_batch(orders)
        for order in cleaned:
            _validate_order(None, None, order.get("size"))  # order_type can't change on edit
        payload = {
            "product_id": product_id,
            "product_symbol": product_symbol,
            "orders": cleaned,
        }
        result = await _finish("edit_batch_orders", "PUT", "/orders/batch", payload, dry_run=dry_run)
        return _flag_partial(result, cleaned)

    @mutation_tool("Cancel orders in batch", destructive=True, idempotent=True)
    async def cancel_batch_orders(
        orders: list[dict[str, Any]] = Field(
            description="Up to 50 orders to cancel, each {id} or {client_order_id}."
        ),
        product_id: int | None = Field(default=None, description="Product id (or pass product_symbol)."),
        product_symbol: str | None = Field(default=None, description="e.g. BTCUSD (or pass product_id)."),
        dry_run: bool = Field(default=False, description="Validate + echo payload without sending."),
    ) -> dict[str, Any]:
        """Cancel up to 50 orders on one contract in a single request."""
        _require_one(product_id, product_symbol)
        cleaned = _check_batch(orders)
        payload = {
            "product_id": product_id,
            "product_symbol": product_symbol,
            "orders": cleaned,
        }
        result = await _finish("cancel_batch_orders", "DELETE", "/orders/batch", payload, dry_run=dry_run)
        return _flag_partial(result, cleaned)

    # ---------------------------------------------------------------- bracket orders

    @mutation_tool("Place bracket order", destructive=False, idempotent=False)
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
        dry_run: bool = Field(default=False, description="Validate + echo payload without sending."),
    ) -> dict[str, Any]:
        """Attach a take-profit / stop-loss bracket to a position. Provide at least one leg.

        Note: these legs are not editable via edit_bracket_order (cancel + re-place to change
        them). Prices are rounded to the product's tick; see price_adjustments in the response.
        """
        _require_one(product_id, product_symbol)
        if stop_loss_order is None and take_profit_order is None:
            raise ValueError("provide at least one of stop_loss_order or take_profit_order")
        sl = _clean(stop_loss_order) if stop_loss_order else None
        tp = _clean(take_profit_order) if take_profit_order else None
        if sl:
            _validate_bracket_sl(sl.get("stop_price"), sl.get("trail_amount"))
        adjustments: list[dict[str, str]] = []
        for leg_name, leg in (("stop_loss_order", sl), ("take_profit_order", tp)):
            if not leg:
                continue
            leg_fields = {k: leg.get(k) for k in ("stop_price", "limit_price")}
            for adj in await _normalize_prices(product_id, product_symbol, leg_fields):
                adj["field"] = f"{leg_name}.{adj['field']}"
                adjustments.append(adj)
            leg.update({k: v for k, v in leg_fields.items() if v is not None})
        payload = {
            "product_id": product_id,
            "product_symbol": product_symbol,
            "stop_loss_order": sl,
            "take_profit_order": tp,
            "bracket_stop_trigger_method": bracket_stop_trigger_method,
        }
        result = await _finish("place_bracket_order", "POST", "/orders/bracket", payload, dry_run=dry_run)
        return _attach(result, adjustments)

    @mutation_tool("Edit bracket order", destructive=True, idempotent=True)
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
        dry_run: bool = Field(default=False, description="Validate + echo payload without sending."),
    ) -> dict[str, Any]:
        """Edit the bracket (TP/SL) params on an existing order.

        `id` is the *entry order* id (an order created with bracket_* params, e.g. via
        place_order(..., bracket_take_profit_price=...)). It does NOT accept the leg ids of a
        position bracket created by place_bracket_order. Prices are rounded to the product's
        tick; see price_adjustments in the response.
        """
        _require_one(product_id, product_symbol)
        _validate_bracket_sl(bracket_stop_loss_price, bracket_trail_amount)
        price_fields = {
            "bracket_stop_loss_price": bracket_stop_loss_price,
            "bracket_stop_loss_limit_price": bracket_stop_loss_limit_price,
            "bracket_take_profit_price": bracket_take_profit_price,
            "bracket_take_profit_limit_price": bracket_take_profit_limit_price,
        }
        adjustments = await _normalize_prices(product_id, product_symbol, price_fields)
        payload = {
            "id": id,
            "product_id": product_id,
            "product_symbol": product_symbol,
            "bracket_stop_loss_price": price_fields["bracket_stop_loss_price"],
            "bracket_stop_loss_limit_price": price_fields["bracket_stop_loss_limit_price"],
            "bracket_take_profit_price": price_fields["bracket_take_profit_price"],
            "bracket_take_profit_limit_price": price_fields["bracket_take_profit_limit_price"],
            "bracket_trail_amount": bracket_trail_amount,
            "bracket_stop_trigger_method": bracket_stop_trigger_method,
        }
        result = await _finish("edit_bracket_order", "PUT", "/orders/bracket", payload, dry_run=dry_run)
        return _attach(result, adjustments)

    # ---------------------------------------------------------------- positions & leverage

    @mutation_tool("Set leverage", destructive=True, idempotent=True)
    async def set_product_leverage(
        product_id: int = Field(description="Product id to set order leverage for."),
        leverage: str = Field(description="Leverage multiplier, e.g. '10'."),
        dry_run: bool = Field(default=False, description="Validate + echo payload without sending."),
    ) -> dict[str, Any]:
        """Set order leverage for a product."""
        return await _finish(
            "set_product_leverage", "POST",
            f"/products/{product_id}/orders/leverage", {"leverage": leverage}, dry_run=dry_run,
        )

    @mutation_tool("Adjust position margin", destructive=True, idempotent=False)
    async def adjust_position_margin(
        product_id: int = Field(description="Product id of the position."),
        delta_margin: str = Field(description="Margin to add (positive) or remove (negative), e.g. '5.0'."),
        dry_run: bool = Field(default=False, description="Validate + echo payload without sending."),
    ) -> dict[str, Any]:
        """Add or remove isolated margin on a position."""
        payload = {"product_id": product_id, "delta_margin": delta_margin}
        return await _finish(
            "adjust_position_margin", "POST", "/positions/change_margin", payload, dry_run=dry_run
        )

    @mutation_tool("Close all positions", destructive=True, idempotent=True)
    async def close_all_positions(
        close_all_portfolio: bool = Field(default=False, description="Close cross/portfolio-margined positions."),
        close_all_isolated: bool = Field(default=False, description="Close isolated-margin positions."),
        dry_run: bool = Field(default=False, description="Validate + echo payload without sending."),
    ) -> dict[str, Any]:
        """Close open positions in the scopes you set to true. Both flags default to false,
        so you must explicitly opt into a scope — this never broadens beyond your request.

        Your user_id is required by the API and is resolved automatically from your profile
        (fetched once and cached) — you do not pass it.
        """
        if not (close_all_portfolio or close_all_isolated):
            raise ValueError("set at least one of close_all_portfolio or close_all_isolated to true")
        payload = {
            "close_all_portfolio": close_all_portfolio,
            "close_all_isolated": close_all_isolated,
            "user_id": None if dry_run else await _user_id(),
        }
        return await _finish("close_all_positions", "POST", "/positions/close_all", payload, dry_run=dry_run)

    @mutation_tool("Configure auto top-up", destructive=True, idempotent=True)
    async def configure_auto_topup(
        product_id: int = Field(description="Product id of the position."),
        auto_topup: bool = Field(description="Enable or disable auto top-up for this position."),
        dry_run: bool = Field(default=False, description="Validate + echo payload without sending."),
    ) -> dict[str, Any]:
        """Override auto top-up for a single position (otherwise inherits the account setting)."""
        payload = {"product_id": product_id, "auto_topup": auto_topup}
        return await _finish("configure_auto_topup", "PUT", "/positions/auto_topup", payload, dry_run=dry_run)
