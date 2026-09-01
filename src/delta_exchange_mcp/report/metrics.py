"""Deterministic metrics for the versioned P&L report."""

import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, date as Date, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal

from delta_exchange_mcp.report.contract import (
    Charges,
    Correlation,
    Daily,
    Distribution,
    DrawdownPoint,
    EquityPoint,
    FeeToken,
    FundingPoint,
    FundingReport,
    GradeDimension,
    Headline,
    Instruments,
    Meta,
    Monthly,
    OpenPosition,
    Pareto,
    PeriodBucket,
    Product,
    REPORT_VERSION,
    Report,
    ReportInput,
    Stat,
    TokenFunding,
    Underlying,
)
from delta_exchange_mcp.report.fifo import Fill, Trade, match

CHARGE_PLACES = 8
CHARGE_QUANTUM = Decimal(1).scaleb(-CHARGE_PLACES)


@dataclass(frozen=True)
class _ChargeSummary:
    total_fees: float
    maker_fees: float
    taker_fees: float
    maker_fill_rate: float
    total_volume: float
    fees_by_token: dict[str, float]


def _summarize_charges(
    fills: list[Fill], products: dict[int, Product]
) -> _ChargeSummary:
    maker_fee_values = []
    taker_fee_values = []
    volume_values = []
    maker_fills = 0
    fee_values_by_token: dict[str, list[float]] = defaultdict(list)
    for fill in fills:
        product = products[fill.product_id]
        volume_values.append(fill.quantity * product.contract_value * fill.price)
        fee_values_by_token[product.underlying].append(fill.fee)
        if fill.role == "maker":
            maker_fee_values.append(fill.fee)
            maker_fills += 1
        else:
            taker_fee_values.append(fill.fee)
    maker_fees = math.fsum(maker_fee_values)
    taker_fees = math.fsum(taker_fee_values)
    return _ChargeSummary(
        total_fees=math.fsum((maker_fees, taker_fees)),
        maker_fees=maker_fees,
        taker_fees=taker_fees,
        maker_fill_rate=maker_fills / len(fills) * 100 if fills else 0,
        total_volume=math.fsum(volume_values),
        fees_by_token={
            token: math.fsum(values) for token, values in fee_values_by_token.items()
        },
    )


def _round(value: float, places: int = 2) -> float:
    return round(value, places)


def _in_window(timestamp: datetime, data: ReportInput) -> bool:
    """Return whether an aware timestamp is inside the inclusive report window."""
    return data.window_start <= timestamp <= data.window_end


def _utc_date(timestamp: datetime) -> str:
    return timestamp.astimezone(UTC).date().isoformat()


def _reconcile_charges(
    values: dict[str, float], total: float
) -> tuple[float, dict[str, float]]:
    """Round one charge breakdown while preserving its rounded total."""
    raw = {key: Decimal(str(value)) for key, value in values.items()}
    rounded_total = Decimal(str(total)).quantize(
        CHARGE_QUANTUM, rounding=ROUND_HALF_EVEN
    )
    rounded = {
        key: value.quantize(CHARGE_QUANTUM, rounding=ROUND_DOWN)
        for key, value in raw.items()
    }
    if not rounded:
        return float(rounded_total), {}
    difference = rounded_total - sum(rounded.values(), Decimal())
    direction = 1 if difference > 0 else -1
    units = int(abs(difference / CHARGE_QUANTUM))
    residuals = {
        key: raw[key] - rounded_value for key, rounded_value in rounded.items()
    }
    eligible = sorted(
        (key for key, residual in residuals.items() if residual * direction > 0),
        key=lambda key: residuals[key] * direction,
        reverse=True,
    )
    if units > len(eligible):
        raise ValueError("charge components do not match their total")
    adjustment = CHARGE_QUANTUM * direction
    for key in eligible[:units]:
        rounded[key] += adjustment
    return float(rounded_total), {key: float(value) for key, value in rounded.items()}


def _mean(values: Iterable[float]) -> float:
    items = list(values)
    return statistics.fmean(items) if items else 0


def _rate(trades: list[Trade]) -> float:
    return (
        len([trade for trade in trades if trade.net_pnl > 0]) / len(trades) * 100
        if trades
        else 0
    )


def _money(value: float) -> str:
    sign = "−" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def _number(value: float | None, places: int = 2) -> str:
    return "n/a" if value is None else f"{value:.{places}f}"


def _group(
    trades: Iterable[Trade], key: Callable[[Trade], str]
) -> dict[str, list[Trade]]:
    grouped: dict[str, list[Trade]] = defaultdict(list)
    for trade in trades:
        grouped[key(trade)].append(trade)
    return grouped


def _histogram(values: list[float]) -> list[Distribution]:
    if not values:
        return []
    low, high = min(values), max(values)
    if low == high:
        return [Distribution(range=f"{low:.2f}", count=len(values))]
    width = (high - low) / 20
    counts = [0] * 20
    for value in values:
        index = min(19, int((value - low) / width))
        counts[index] += 1
    return [
        Distribution(
            range=f"{low + index * width:.2f} - {low + (index + 1) * width:.2f}",
            count=count,
        )
        for index, count in enumerate(counts)
        if count
    ]


def _pearson(left: list[float], right: list[float]) -> float:
    if len(left) < 3:
        return 0
    left_mean, right_mean = _mean(left), _mean(right)
    left_delta = [value - left_mean for value in left]
    right_delta = [value - right_mean for value in right]
    denominator = math.sqrt(
        sum(value * value for value in left_delta)
        * sum(value * value for value in right_delta)
    )
    if denominator == 0:
        return 0
    return (
        sum(a * b for a, b in zip(left_delta, right_delta, strict=True)) / denominator
    )


def _correlation(trades: list[Trade]) -> Correlation | None:
    series: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for trade in trades:
        series[trade.underlying][trade.exit_time.date().isoformat()] += trade.net_pnl
    tokens = sorted(
        (token for token, days in series.items() if len(days) >= 5),
        key=lambda token: sum(abs(value) for value in series[token].values()),
        reverse=True,
    )[:10]
    if not tokens:
        return None
    dates = sorted({date for token in tokens for date in series[token]})
    matrix = []
    for left in tokens:
        row = []
        for right in tokens:
            values_left = [series[left].get(date, 0) for date in dates]
            values_right = [series[right].get(date, 0) for date in dates]
            row.append(_round(_pearson(values_left, values_right), 2))
        matrix.append(row)
    return Correlation(tokens=tokens, matrix=matrix)


def _persona(trades: list[Trade]) -> str:
    if not trades:
        return "The Observer"
    assets = Counter(trade.underlying for trade in trades)
    instruments = Counter(trade.instrument_type for trade in trades)
    top_asset, top_count = assets.most_common(1)[0]
    top_instrument = instruments.most_common(1)[0][0]
    diversified = len(assets) >= 5 and top_count / len(trades) < 0.5
    speed = _mean(trade.hold_duration_hours for trade in trades) < 1
    long_hold = _mean(trade.hold_duration_hours for trade in trades) >= 24
    perps = top_instrument == "perpetual"
    options = top_instrument in {"call", "put"}
    if diversified:
        return "Multi-Asset Diversifier"
    if speed and perps and len(trades) > 500:
        return "Degen Speed Trader"
    if speed and perps:
        return "Perps Scalper"
    if long_hold:
        return "Diamond Hands Holder"
    if options:
        return f"{top_asset} Options Strategist"
    if perps and top_asset == "ETH":
        return "ETH Perps Warrior"
    if perps:
        return f"{top_asset} Perps Warrior"
    return f"{top_asset} Options Strategist"


def _win_rate_points(win_rate: float) -> float:
    if win_rate >= 65:
        return 25
    if win_rate >= 55:
        return 20 + (win_rate - 55) * 0.5
    if win_rate >= 45:
        return 12 + (win_rate - 45) * 0.8
    if win_rate >= 35:
        return 5 + (win_rate - 35) * 0.7
    return max(0, win_rate * 0.14)


def _maker_points(maker_rate: float) -> float:
    if maker_rate >= 80:
        return 12
    if maker_rate >= 50:
        return 6 + (maker_rate - 50) * 0.2
    if maker_rate >= 20:
        return 2 + (maker_rate - 20) * 0.133
    return maker_rate * 0.1


def _fee_points(
    net_pnl: float, fees_pct_pnl: float | None, fees_pct_volume: float | None
) -> float:
    if net_pnl <= 0:
        volume = fees_pct_volume or 0
        if volume <= 0.02:
            return 10
        if volume <= 0.05:
            return 6
        if volume <= 0.1:
            return 3
        return 1
    fees = fees_pct_pnl or 0
    if fees <= 5:
        return 13
    if fees <= 15:
        return max(0, 10 - (fees - 5) * 0.3)
    if fees <= 30:
        return max(0, 7 - (fees - 15) * 0.233)
    if fees <= 60:
        return max(0, 3.5 - (fees - 30) * 0.117)
    return max(0, 1.5 - (fees - 60) * 0.025)


def _letter(score: float) -> str:
    for minimum, grade in (
        (93, "A+"),
        (87, "A"),
        (80, "A-"),
        (73, "B+"),
        (67, "B"),
        (60, "B-"),
        (53, "C+"),
        (47, "C"),
        (40, "C-"),
        (33, "D+"),
        (27, "D"),
    ):
        if score >= minimum:
            return grade
    return "D-"


def _grade(
    trades: list[Trade],
    win_rate: float,
    profit_factor: float,
    payoff_ratio: float,
    sharpe: float | None,
    maker_rate: float,
    fees_pct_pnl: float | None,
    fees_pct_volume: float | None,
    expectancy: float,
    best_day_streak: int,
) -> tuple[str | None, float | None, list[GradeDimension]]:
    if len(trades) < 30:
        return None, None, []
    net_pnl = sum(trade.net_pnl for trade in trades)
    sharpe_points = (
        min(7, max(0, (min(5, max(-2, sharpe)) + 0.5) * 2.33))
        if sharpe is not None
        else 0
    )
    risk_reward = (
        min(10, min(profit_factor, 5) * 3.33)
        + min(8, min(payoff_ratio, 5) * 2.67)
        + sharpe_points
    )
    charges = _maker_points(maker_rate) + _fee_points(
        net_pnl, fees_pct_pnl, fees_pct_volume
    )
    best = max((trade.net_pnl for trade in trades), default=0)
    worst = min((trade.net_pnl for trade in trades), default=0)
    asymmetry = best / abs(worst) if worst else 0
    win_fraction = win_rate / 100
    kelly = win_fraction - (1 - win_fraction) / payoff_ratio if payoff_ratio else 0
    edge = (
        min(8, max(0, min(asymmetry, 10) * 1.6))
        + min(8, max(0, max(kelly * 100, -50) * 0.4))
        + (min(5, expectancy * 2) if expectancy > 0 else 0)
        + min(4, best_day_streak * 0.5)
    )
    dimensions = [
        GradeDimension(label="Win Rate", score=_round(_win_rate_points(win_rate), 1)),
        GradeDimension(label="Risk-Reward", score=_round(risk_reward, 1)),
        GradeDimension(label="Charges Discipline", score=_round(charges, 1)),
        GradeDimension(label="Asymmetry & Edge", score=_round(edge, 1)),
    ]
    score = min(100, sum(item.score for item in dimensions))
    return _letter(score), _round(score, 1), dimensions


def _streaks(values: list[float]) -> tuple[int, int, int, int]:
    best_win = best_loss = current_win = current_loss = 0
    for value in values:
        if value > 0:
            current_win += 1
            current_loss = 0
        elif value < 0:
            current_loss += 1
            current_win = 0
        else:
            current_win = current_loss = 0
        best_win = max(best_win, current_win)
        best_loss = max(best_loss, current_loss)
    return best_win, best_loss, current_win, current_loss


def _drawdown_duration(
    daily: list[tuple[str, float]], window_start: Date, window_end: Date
) -> tuple[int, bool]:
    """Return the longest duration and whether a selected longest interval is current.

    An ongoing interval wins a tie because one maximum-duration drawdown is still
    active at the report window end.
    """
    cumulative = peak = 0.0
    started = None
    longest_recovered = 0
    for date, pnl in daily:
        current_date = datetime.fromisoformat(date).date()
        cumulative += pnl
        if cumulative >= peak:
            if started is not None:
                longest_recovered = max(
                    longest_recovered, (current_date - started).days
                )
            peak = cumulative
            started = None
        elif started is None:
            # Missing dates have zero P&L. Equity remains at its peak until the day
            # before this first loss, even when the last observed trade is older.
            started = max(window_start, current_date - timedelta(days=1))
    if started is None:
        return longest_recovered, False
    ongoing_duration = (window_end - started).days
    if ongoing_duration >= longest_recovered:
        return ongoing_duration, True
    return longest_recovered, False


def _funding(
    data: ReportInput, products: dict[int, Product]
) -> tuple[FundingReport | None, float | None]:
    if data.funding is None:
        return None, None
    by_token: dict[str, list[float]] = defaultdict(list)
    by_date: dict[str, float] = defaultdict(float)
    for item in data.funding:
        if not _in_window(item.created_at, data):
            continue
        if item.underlying:
            token = item.underlying
        else:
            product = products.get(item.product_id)
            if product is None:
                raise ValueError(
                    f"no product contract for funding product_id {item.product_id}"
                )
            token = product.underlying
        by_token[token].append(item.amount)
        by_date[_utc_date(item.created_at)] += item.amount
    total = sum(amount for amounts in by_token.values() for amount in amounts)
    cumulative = 0.0
    points = []
    for date, amount in sorted(by_date.items()):
        cumulative += amount
        points.append(FundingPoint(date=date, cumulative=_round(cumulative)))
    return (
        FundingReport(
            total=_round(total),
            paid=_round(
                sum(
                    amount
                    for amounts in by_token.values()
                    for amount in amounts
                    if amount < 0
                )
            ),
            received=_round(
                sum(
                    amount
                    for amounts in by_token.values()
                    for amount in amounts
                    if amount > 0
                )
            ),
            by_token=[
                TokenFunding(token=token, pnl=_round(sum(amounts)), count=len(amounts))
                for token, amounts in sorted(by_token.items())
            ],
            cumulative=points,
        ),
        total,
    )


def _positions(data: ReportInput) -> list[OpenPosition] | None:
    if data.positions is None:
        return None
    output = []
    for position in data.positions:
        if position.size == 0:
            continue
        distance = None
        if position.liquidation_price and position.mark_price:
            if position.size > 0:
                distance = (
                    (position.mark_price - position.liquidation_price)
                    / position.mark_price
                    * 100
                )
            else:
                distance = (
                    (position.liquidation_price - position.mark_price)
                    / position.mark_price
                    * 100
                )
        output.append(
            OpenPosition(
                symbol=position.symbol,
                direction="long" if position.size > 0 else "short",
                size=abs(position.size),
                notional=_round(
                    abs(position.size) * position.contract_value * position.index_price
                ),
                entry_price=position.entry_price,
                mark_price=position.mark_price,
                unrealized_pnl=_round(position.unrealized_pnl),
                margin=_round(position.margin),
                to_liquidation=f"{distance:.1f}%" if distance is not None else "n/a",
            )
        )
    return output


def calculate(data: ReportInput, fills: list[Fill]) -> Report:
    """Calculate a complete dashboard report from validated local inputs."""
    products = {product.product_id: product for product in data.products}
    if len(products) != len(data.products):
        raise ValueError("products contains duplicate product_id values")
    context_fills = [
        replace(fill, created_at=fill.created_at.astimezone(UTC))
        for fill in fills
        if fill.created_at <= data.window_end
    ]
    window_fills = [
        fill for fill in context_fills if fill.created_at >= data.window_start
    ]
    trades = [
        trade
        for trade in match(context_fills, products)
        if _in_window(trade.exit_time, data)
    ]
    charges = _summarize_charges(window_fills, products)
    winners = [trade for trade in trades if trade.net_pnl > 0]
    losers = [trade for trade in trades if trade.net_pnl < 0]
    net_pnl = sum(trade.net_pnl for trade in trades)
    gross_pnl = sum(trade.pnl for trade in trades)
    total_fees = charges.total_fees
    total_volume = charges.total_volume
    win_rate = _rate(trades)
    avg_winner = _mean(trade.net_pnl for trade in winners)
    avg_loser = _mean(trade.net_pnl for trade in losers)
    payoff_ratio = abs(avg_winner / avg_loser) if avg_loser else 0
    expectancy = win_rate / 100 * avg_winner + (1 - win_rate / 100) * avg_loser
    profit_factor = (
        sum(trade.net_pnl for trade in winners)
        / abs(sum(trade.net_pnl for trade in losers))
        if losers
        else (999 if winners else 0)
    )

    by_date = _group(trades, lambda trade: trade.exit_time.date().isoformat())
    daily_series = [
        (date, sum(trade.net_pnl for trade in group))
        for date, group in sorted(by_date.items())
    ]
    daily_values = [pnl for _, pnl in daily_series]
    daily = [
        Daily(date=date, pnl=_round(pnl), trades=len(by_date[date]))
        for date, pnl in daily_series
    ]
    mean_daily = _mean(daily_values)
    daily_std = statistics.stdev(daily_values) if len(daily_values) >= 2 else None
    sharpe = mean_daily / daily_std * math.sqrt(365) if daily_std else 0
    downside_values = [value for value in daily_values if value < 0]
    downside = (
        math.sqrt(
            sum(value * value for value in downside_values) / len(downside_values)
        )
        if downside_values
        else 0
    )
    sortino = mean_daily / downside * math.sqrt(365) if downside else 0
    best_win_streak, best_loss_streak, current_win, current_loss = _streaks(
        daily_values
    )

    trade_cumulative = 0.0
    equity: list[EquityPoint] = []
    for trade in sorted(trades, key=lambda item: item.exit_time):
        trade_cumulative += trade.net_pnl
        equity.append(
            EquityPoint(
                date=trade.exit_time.isoformat(), cumulative=_round(trade_cumulative)
            )
        )

    cumulative = peak = 0.0
    max_dd_amount = 0.0
    drawdown: list[DrawdownPoint] = []
    has_drawdown_base = False
    for date, value in daily_series:
        cumulative += value
        peak = max(peak, cumulative)
        delta = cumulative - peak
        max_dd_amount = min(max_dd_amount, delta)
        percent = delta / peak * 100 if peak > 0 else None
        has_drawdown_base = has_drawdown_base or peak > 0
        drawdown.append(
            DrawdownPoint(
                date=date,
                drawdown=_round(percent, 1) if percent is not None else None,
            )
        )

    by_underlying_group = _group(trades, lambda trade: trade.underlying)
    by_underlying = []
    for underlying, group in by_underlying_group.items():
        capital = sum(trade.notional_value for trade in group)
        pnl = sum(trade.net_pnl for trade in group)
        by_underlying.append(
            Underlying(
                underlying=underlying,
                num_trades=len(group),
                pnl=_round(pnl),
                win_rate=_round(_rate(group), 1),
                avg_return=_round(pnl / capital * 100, 2) if capital else 0,
                capital=_round(capital),
            )
        )
    by_underlying.sort(key=lambda item: abs(item.pnl), reverse=True)

    positive = sorted(
        (item for item in by_underlying if item.pnl > 0),
        key=lambda item: item.pnl,
        reverse=True,
    )
    pareto = None
    if positive:
        target = sum(item.pnl for item in positive) * 0.8
        running = 0.0
        leaders = []
        for item in positive:
            running += item.pnl
            leaders.append(item.underlying)
            if running >= target:
                break
        pareto = Pareto(index=len(leaders), total=len(positive), leaders=leaders)

    by_month = _group(trades, lambda trade: trade.exit_time.strftime("%Y-%m"))
    monthly = [
        Monthly(
            month=month,
            trades=len(group),
            gross_pnl=_round(sum(trade.pnl for trade in group)),
            fees=_round(sum(trade.fees for trade in group)),
            net_pnl=_round(sum(trade.net_pnl for trade in group)),
            win_rate=_round(_rate(group), 1),
            best=_round(max(trade.net_pnl for trade in group)),
            worst=_round(min(trade.net_pnl for trade in group)),
        )
        for month, group in sorted(by_month.items())
    ]
    hourly_group = _group(trades, lambda trade: str(trade.exit_time.hour))
    hourly = [
        PeriodBucket(
            hour=hour,
            avg_pnl=_round(
                _mean(trade.net_pnl for trade in hourly_group.get(str(hour), []))
            ),
            trades=len(hourly_group.get(str(hour), [])),
            win_rate=_round(_rate(hourly_group.get(str(hour), [])), 1),
        )
        for hour in range(24)
    ]
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    weekday_group = _group(trades, lambda trade: str(trade.exit_time.weekday()))
    day_of_week = [
        PeriodBucket(
            day=name,
            avg_pnl=_round(
                _mean(trade.net_pnl for trade in weekday_group.get(str(index), []))
            ),
            trades=len(weekday_group.get(str(index), [])),
            win_rate=_round(_rate(weekday_group.get(str(index), [])), 1),
        )
        for index, name in enumerate(day_names)
    ]

    fees_pct_pnl = total_fees / abs(gross_pnl) * 100 if gross_pnl else None
    fees_pct_volume = total_fees / total_volume * 100 if total_volume else None
    grade, score, dimensions = _grade(
        trades,
        win_rate,
        profit_factor,
        payoff_ratio,
        sharpe if len(daily_values) >= 7 else None,
        charges.maker_fill_rate,
        fees_pct_pnl,
        fees_pct_volume,
        expectancy,
        best_win_streak,
    )
    funding, funding_total = _funding(data, products)
    positions = _positions(data)
    unrealized = (
        _round(sum(position.unrealized_pnl for position in data.positions))
        if data.positions is not None
        else None
    )
    instruments = {
        name: [trade for trade in trades if trade.instrument_type == name]
        for name in ("perpetual", "call", "put")
    }
    rounded_total_fees, role_fees = _reconcile_charges(
        {
            "maker": charges.maker_fees,
            "taker": charges.taker_fees,
        },
        total_fees,
    )
    rounded_token_total, token_fees = _reconcile_charges(
        charges.fees_by_token, total_fees
    )
    sorted_token_fees = sorted(
        token_fees.items(),
        key=lambda item: item[1],
        reverse=True,
    )
    shown_token_fees = sorted_token_fees[:10]
    fee_by_token = [
        FeeToken(token=token, fees=fees) for token, fees in shown_token_fees
    ]
    if len(sorted_token_fees) > len(shown_token_fees):
        fee_by_token.append(
            FeeToken(
                token="Other underlyings",
                fees=_round(
                    rounded_token_total
                    - math.fsum(fees for _, fees in shown_token_fees),
                    CHARGE_PLACES,
                ),
            )
        )
    leak_item = min(by_underlying, key=lambda item: item.pnl, default=None)
    leak = (
        f"Largest detractor: {leak_item.underlying} at {_money(leak_item.pnl)}."
        if leak_item is not None and leak_item.pnl < 0
        else None
    )
    duration, ongoing_drawdown = _drawdown_duration(
        daily_series,
        data.window_start.astimezone(UTC).date(),
        data.window_end.astimezone(UTC).date(),
    )
    recovery_factor = net_pnl / abs(max_dd_amount) if max_dd_amount else None
    calmar = mean_daily * 365 / abs(max_dd_amount) if max_dd_amount else None
    risk = [
        Stat(
            label="Sharpe",
            value=_number(sharpe if len(daily_values) >= 7 else None),
            note=f"{len(daily_values)} daily observations",
        ),
        Stat(
            label="Sortino",
            value=_number(sortino if len(daily_values) >= 7 else None),
            note=f"{len(daily_values)} daily observations",
        ),
        Stat(
            label="Daily volatility",
            value=_money(daily_std) if daily_std is not None else "n/a",
        ),
        Stat(label="Profit factor", value=_number(profit_factor)),
        Stat(label="Payoff ratio", value=_number(payoff_ratio)),
        Stat(label="Expectancy", value=_money(expectancy)),
        Stat(
            label="Max drawdown",
            value=(
                f"{min((point.drawdown for point in drawdown if point.drawdown is not None), default=0):.1f}%"
                if has_drawdown_base
                else "n/a"
            ),
            note=_money(max_dd_amount),
        ),
        Stat(label="Recovery factor", value=_number(recovery_factor)),
        Stat(label="Calmar", value=_number(calmar)),
        Stat(
            label="Longest drawdown",
            value=f"{duration} days",
            note="ongoing at window end" if ongoing_drawdown else None,
        ),
        Stat(label="Best winning-day streak", value=str(best_win_streak)),
        Stat(label="Best losing-day streak", value=str(best_loss_streak)),
        Stat(
            label="Current daily streak",
            value=(
                f"{current_win} wins"
                if current_win
                else f"{current_loss} losses"
                if current_loss
                else "none"
            ),
        ),
    ]
    return Report(
        meta=Meta(
            schema_version=REPORT_VERSION,
            generated=_utc_date(data.generated_at),
            window=(f"{_utc_date(data.window_start)} to {_utc_date(data.window_end)}"),
            fills=len(window_fills),
            trades=len(trades),
        ),
        headline=Headline(
            net_pnl=_round(net_pnl),
            unrealized=unrealized,
            win_rate=_round(win_rate, 1),
            total_fees=rounded_total_fees,
            funding=_round(funding_total) if funding_total is not None else None,
            net_including_funding=(
                _round(net_pnl + funding_total) if funding_total is not None else None
            ),
            grade=grade,
            score=score,
            persona=_persona(trades),
            leak=leak,
        ),
        grade_dimensions=dimensions,
        equity=equity[-500:],
        drawdown=drawdown[-500:],
        overview=[
            Stat(label="Gross P&L", value=_money(gross_pnl)),
            Stat(
                label="Long net P&L",
                value=_money(
                    sum(trade.net_pnl for trade in trades if trade.direction == "long")
                ),
            ),
            Stat(
                label="Short net P&L",
                value=_money(
                    sum(trade.net_pnl for trade in trades if trade.direction == "short")
                ),
            ),
            Stat(label="Avg winner", value=_money(avg_winner)),
            Stat(label="Avg loser", value=_money(avg_loser)),
            Stat(
                label="Best trade",
                value=_money(max((trade.net_pnl for trade in trades), default=0)),
            ),
            Stat(
                label="Worst trade",
                value=_money(min((trade.net_pnl for trade in trades), default=0)),
            ),
        ],
        daily=daily,
        monthly=monthly,
        hourly=hourly,
        day_of_week=day_of_week,
        pnl_distribution=_histogram([trade.net_pnl for trade in trades]),
        by_underlying=by_underlying,
        instruments=Instruments(
            perps_pnl=_round(sum(trade.net_pnl for trade in instruments["perpetual"])),
            perps_count=len(instruments["perpetual"]),
            calls_pnl=_round(sum(trade.net_pnl for trade in instruments["call"])),
            calls_count=len(instruments["call"]),
            puts_pnl=_round(sum(trade.net_pnl for trade in instruments["put"])),
            puts_count=len(instruments["put"]),
        ),
        correlation=_correlation(trades),
        funding=funding,
        pareto=pareto,
        risk=risk,
        charges=Charges(
            total_fees=rounded_total_fees,
            maker_fees=role_fees["maker"],
            taker_fees=role_fees["taker"],
            maker_fill_rate=_round(charges.maker_fill_rate, 1),
            fees_pct_pnl=_round(fees_pct_pnl, 1) if fees_pct_pnl is not None else None,
            fees_pct_volume=(
                _round(fees_pct_volume, 4) if fees_pct_volume is not None else None
            ),
            gst_estimate=_round(max(0, total_fees) * 0.18, CHARGE_PLACES),
            trades_to_cover=(
                math.ceil(total_fees / expectancy)
                if total_fees > 0 and expectancy > 0
                else None
            ),
            by_token=fee_by_token,
        ),
        positions=positions,
    )
