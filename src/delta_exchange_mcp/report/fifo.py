"""FIFO matching for Delta fill exports."""

import csv
import math
from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from delta_exchange_mcp.report.contract import Product

REQUIRED_COLUMNS = frozenset(
    {
        "product_id",
        "product_symbol",
        "size",
        "side",
        "price",
        "commission",
        "created_at",
        "role",
    }
)


@dataclass
class Fill:
    product_id: int
    product_symbol: str
    quantity: float
    side: str
    price: float
    fee: float
    created_at: datetime
    role: str

    @property
    def signed_quantity(self) -> float:
        return self.quantity if self.side == "buy" else -self.quantity


@dataclass
class Lot:
    quantity: float
    price: float
    fee: float
    opened_at: datetime


@dataclass(frozen=True)
class Trade:
    underlying: str
    product_symbol: str
    instrument_type: str
    direction: str
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    size: float
    notional_value: float
    pnl: float
    fees: float
    net_pnl: float
    pnl_pct: float
    hold_duration_hours: float


def _time(raw: str) -> datetime:
    value = raw.strip()
    try:
        number = float(value)
    except ValueError:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (
            parsed.replace(tzinfo=UTC)
            if parsed.tzinfo is None
            else parsed.astimezone(UTC)
        )
    if not math.isfinite(number):
        raise ValueError("timestamp must be finite")
    if number > 100_000_000_000_000:
        number /= 1_000_000
    elif number > 100_000_000_000:
        number /= 1_000
    try:
        return datetime.fromtimestamp(number, UTC)
    except (OverflowError, OSError) as exc:
        raise ValueError("timestamp is outside the supported range") from exc


def read_fills(path: Path) -> list[Fill]:
    """Read and validate the stable fields from a bulk fill export."""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"fills CSV is missing columns: {sorted(missing)}")
        fills: list[Fill] = []
        for row_number, row in enumerate(reader, start=2):
            try:
                side = row["side"].strip().lower()
                if side not in {"buy", "sell"}:
                    raise ValueError(f"invalid side {side!r}")
                quantity = float(row["size"])
                price = float(row["price"])
                fee = float(row["commission"])
                if not all(math.isfinite(value) for value in (quantity, price, fee)):
                    raise ValueError("size, price, and commission must be finite")
                if quantity < 0 or price < 0:
                    raise ValueError("size and price must not be negative")
                if quantity == 0 or price == 0:
                    continue
                role = row["role"].strip().lower()
                if role not in {"maker", "taker"}:
                    raise ValueError(f"invalid role {role!r}")
                fills.append(
                    Fill(
                        product_id=int(row["product_id"]),
                        product_symbol=row["product_symbol"].strip(),
                        quantity=quantity,
                        side=side,
                        price=price,
                        fee=fee,
                        created_at=_time(row["created_at"]),
                        role=role,
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"invalid fills CSV row {row_number}: {exc}") from exc
    return sorted(fills, key=lambda fill: fill.created_at)


def _instrument(contract_type: str) -> str:
    lowered = contract_type.lower()
    if "call" in lowered:
        return "call"
    if "put" in lowered:
        return "put"
    return "perpetual"


def _opposes(left: float, right: float) -> bool:
    return (left > 0 > right) or (left < 0 < right)


def match(fills: Iterable[Fill], products: dict[int, Product]) -> list[Trade]:
    """Match fills against FIFO entry lots and return realized round trips."""
    lots: dict[int, deque[Lot]] = defaultdict(deque)
    trades: list[Trade] = []
    for fill in sorted(fills, key=lambda item: item.created_at):
        product = products.get(fill.product_id)
        if product is None:
            raise ValueError(f"no product contract for product_id {fill.product_id}")
        queue = lots[fill.product_id]
        remaining = fill.quantity
        signed = fill.signed_quantity
        exit_fee_per_unit = fill.fee / fill.quantity

        while remaining > 0 and queue and _opposes(queue[0].quantity, signed):
            lot = queue[0]
            lot_quantity = abs(lot.quantity)
            close_quantity = min(remaining, lot_quantity)
            entry_fee = lot.fee * close_quantity / lot_quantity
            exit_fee = exit_fee_per_unit * close_quantity
            direction = "long" if lot.quantity > 0 else "short"
            price_move = fill.price - lot.price
            if direction == "short":
                price_move = -price_move
            pnl = close_quantity * product.contract_value * price_move
            notional = close_quantity * product.contract_value * fill.price
            fees = entry_fee + exit_fee
            trades.append(
                Trade(
                    underlying=product.underlying,
                    product_symbol=fill.product_symbol or product.symbol,
                    instrument_type=_instrument(product.contract_type),
                    direction=direction,
                    entry_time=lot.opened_at,
                    exit_time=fill.created_at,
                    entry_price=lot.price,
                    exit_price=fill.price,
                    size=close_quantity,
                    notional_value=notional,
                    pnl=pnl,
                    fees=fees,
                    net_pnl=pnl - fees,
                    pnl_pct=(pnl / notional * 100) if notional else 0,
                    hold_duration_hours=(
                        fill.created_at - lot.opened_at
                    ).total_seconds()
                    / 3600,
                )
            )
            remaining -= close_quantity
            if close_quantity == lot_quantity:
                queue.popleft()
            else:
                lot.quantity += close_quantity if lot.quantity < 0 else -close_quantity
                lot.fee -= entry_fee

        if remaining > 0:
            queue.append(
                Lot(
                    quantity=remaining if signed > 0 else -remaining,
                    price=fill.price,
                    fee=exit_fee_per_unit * remaining,
                    opened_at=fill.created_at,
                )
            )
    return trades
