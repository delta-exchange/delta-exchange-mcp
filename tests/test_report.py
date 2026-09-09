"""The shipped P&L calculator owns matching, funding state, and report output."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from delta_exchange_mcp.report.cli import render_dashboard, run
from delta_exchange_mcp.report.contract import (
    Funding,
    INPUT_VERSION,
    Product,
    ReportInput,
)
from delta_exchange_mcp.report.fifo import Fill, match
from delta_exchange_mcp.report.metrics import calculate

START = datetime(2026, 1, 1, tzinfo=UTC)
PRODUCT = Product(
    product_id=27,
    symbol="BTCUSD",
    underlying="BTC",
    contract_type="perpetual_futures",
    contract_value=1,
)


def fill(side: str, price: float, hour: int, fee: float = 1) -> Fill:
    return Fill(
        product_id=27,
        product_symbol="BTCUSD",
        quantity=1,
        side=side,
        price=price,
        fee=fee,
        created_at=START + timedelta(hours=hour),
        role="maker",
    )


def report_input(tmp_path: Path, **overrides: Any) -> ReportInput:
    values = {
        "schema_version": INPUT_VERSION,
        "fills_csv": tmp_path / "fills.csv",
        "window_start": START,
        "window_end": START + timedelta(days=1),
        "generated_at": START + timedelta(days=1),
        "products": [PRODUCT],
        "funding": None,
        "positions": None,
    }
    values.update(overrides)
    return ReportInput(**values)


def test_fifo_closes_the_oldest_entry_instead_of_the_average_price() -> None:
    trades = match(
        [fill("buy", 100, 0), fill("buy", 200, 1), fill("sell", 150, 2)],
        {27: PRODUCT},
    )

    assert len(trades) == 1
    assert trades[0].entry_price == 100
    assert trades[0].pnl == 50
    assert trades[0].fees == 2
    assert trades[0].net_pnl == 48


def test_fifo_allocates_entry_and_exit_fees_across_partial_lots() -> None:
    opening = Fill(
        product_id=27,
        product_symbol="BTCUSD",
        quantity=2,
        side="buy",
        price=100,
        fee=4,
        created_at=START,
        role="taker",
    )
    first_close = fill("sell", 110, 1, fee=3)
    second_close = fill("sell", 120, 2, fee=5)

    trades = match([opening, first_close, second_close], {27: PRODUCT})

    assert [trade.fees for trade in trades] == [5, 7]
    assert sum(trade.fees for trade in trades) == 12


def test_fifo_preserves_negative_maker_commission_as_a_rebate() -> None:
    trades = match(
        [fill("buy", 100, 0, fee=-1), fill("sell", 150, 1, fee=-1)],
        {27: PRODUCT},
    )

    assert trades[0].fees == -2
    assert trades[0].net_pnl == 52


def test_charges_use_each_fill_role_and_include_open_fill_fees(tmp_path: Path) -> None:
    fills = [
        fill("buy", 100, 0, fee=1),
        Fill(
            product_id=27,
            product_symbol="BTCUSD",
            quantity=1,
            side="sell",
            price=120,
            fee=9,
            created_at=START + timedelta(hours=1),
            role="taker",
        ),
        fill("buy", 110, 2, fee=2),
    ]

    report = calculate(report_input(tmp_path), fills)

    assert report.headline.total_fees == 12
    assert report.charges.total_fees == 12
    assert report.charges.maker_fees == 3
    assert report.charges.taker_fees == 9
    assert report.charges.maker_fill_rate == 66.7
    assert report.charges.by_token[0].fees == 12


def test_charge_breakdowns_reconcile_sub_cent_fees(tmp_path: Path) -> None:
    products = [
        Product(
            product_id=100 + index,
            symbol=f"T{index}USD",
            underlying=f"T{index}",
            contract_type="perpetual_futures",
            contract_value=1,
        )
        for index in range(11)
    ]
    fills = [
        Fill(
            product_id=product.product_id,
            product_symbol=product.symbol,
            quantity=1,
            side="buy",
            price=10,
            fee=0.000000006,
            created_at=START + timedelta(minutes=index),
            role="maker" if index % 2 == 0 else "taker",
        )
        for index, product in enumerate(products)
    ]

    report = calculate(report_input(tmp_path, products=products), fills)

    assert report.headline.total_fees == 0.00000007
    assert report.charges.total_fees == 0.00000007
    assert report.charges.maker_fees == 0.00000004
    assert report.charges.taker_fees == 0.00000003
    assert (
        round(report.charges.maker_fees + report.charges.taker_fees, 8)
        == report.charges.total_fees
    )
    assert (
        round(sum(item.fees for item in report.charges.by_token), 8)
        == report.charges.total_fees
    )
    assert len(report.charges.by_token) == 11
    assert all(item.fees >= 0 for item in report.charges.by_token)
    assert report.charges.by_token[-1].token == "Other underlyings"


def test_fifo_close_can_span_lots_and_flip_direction() -> None:
    closing = Fill(
        product_id=27,
        product_symbol="BTCUSD",
        quantity=3,
        side="sell",
        price=130,
        fee=0,
        created_at=START + timedelta(hours=2),
        role="taker",
    )
    trades = match(
        [
            fill("buy", 100, 0, fee=0),
            fill("buy", 120, 1, fee=0),
            closing,
            fill("buy", 120, 3, fee=0),
        ],
        {27: PRODUCT},
    )

    assert [trade.entry_price for trade in trades] == [100, 120, 130]
    assert [trade.direction for trade in trades] == ["long", "long", "short"]
    assert [trade.pnl for trade in trades] == [30, 10, 10]


def test_missing_funding_stays_unavailable_instead_of_becoming_zero(
    tmp_path: Path,
) -> None:
    fills = [fill("buy", 100, 0), fill("sell", 150, 1)]
    unavailable = calculate(report_input(tmp_path, funding=None), fills)
    fetched_empty = calculate(report_input(tmp_path, funding=[]), fills)

    assert unavailable.headline.funding is None
    assert unavailable.headline.net_including_funding is None
    assert unavailable.funding is None
    assert fetched_empty.headline.funding == 0
    assert fetched_empty.headline.net_including_funding == 48
    assert fetched_empty.funding is not None


def test_missing_product_contract_fails_instead_of_assuming_one(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="no product contract"):
        calculate(report_input(tmp_path, products=[]), [fill("buy", 100, 0)])


def test_report_window_uses_earlier_fills_only_as_fifo_context(
    tmp_path: Path,
) -> None:
    window_end = START + timedelta(days=1)
    fills = [
        Fill(
            product_id=27,
            product_symbol="BTCUSD",
            quantity=1,
            side="buy",
            price=100,
            fee=1,
            created_at=START - timedelta(hours=1),
            role="maker",
        ),
        Fill(
            product_id=27,
            product_symbol="BTCUSD",
            quantity=1,
            side="sell",
            price=110,
            fee=2,
            created_at=START,
            role="taker",
        ),
        Fill(
            product_id=27,
            product_symbol="BTCUSD",
            quantity=1,
            side="buy",
            price=200,
            fee=3,
            created_at=window_end,
            role="maker",
        ),
        Fill(
            product_id=27,
            product_symbol="BTCUSD",
            quantity=1,
            side="sell",
            price=220,
            fee=4,
            created_at=window_end + timedelta(microseconds=1),
            role="taker",
        ),
    ]
    funding = [
        Funding(
            amount=-100,
            created_at=START - timedelta(microseconds=1),
            underlying="BTC",
        ),
        Funding(amount=-2, created_at=START, underlying="BTC"),
        Funding(amount=3, created_at=window_end, underlying="BTC"),
        Funding(
            amount=100,
            created_at=window_end + timedelta(microseconds=1),
            product_id=999,
        ),
    ]

    report = calculate(
        report_input(tmp_path, window_end=window_end, funding=funding),
        fills,
    )

    assert report.meta.fills == 2
    assert report.meta.trades == 1
    assert report.headline.net_pnl == 7
    assert report.headline.total_fees == 5
    assert report.headline.funding == 1
    assert report.headline.net_including_funding == 8
    assert report.charges.maker_fees == 3
    assert report.charges.taker_fees == 2
    assert report.charges.maker_fill_rate == 50
    assert report.funding is not None
    assert report.funding.by_token[0].count == 2


def test_report_dates_use_utc_at_offset_aware_window_boundary(
    tmp_path: Path,
) -> None:
    window_start = datetime.fromisoformat("2026-01-02T05:00:00+05:30")
    window_end = datetime.fromisoformat("2026-01-03T05:00:00+05:30")
    funding = Funding(
        amount=1,
        created_at=window_end,
        underlying="BTC",
    )

    report = calculate(
        report_input(
            tmp_path,
            window_start=window_start,
            window_end=window_end,
            generated_at=window_end,
            funding=[funding],
        ),
        [],
    )

    assert report.funding is not None
    assert report.funding.cumulative[0].date == "2026-01-02"
    assert report.meta.window == "2026-01-01 to 2026-01-02"
    assert report.meta.generated == "2026-01-02"


def test_offset_aware_fills_use_utc_for_all_calendar_axes(tmp_path: Path) -> None:
    eth = Product(
        product_id=28,
        symbol="ETHUSD",
        underlying="ETH",
        contract_type="perpetual_futures",
        contract_value=1,
    )
    first_btc_close = datetime.fromisoformat("2026-02-01T00:30:00+05:30")
    first_eth_close = datetime.fromisoformat("2026-01-31T23:30:00+05:30")
    window_start = first_eth_close - timedelta(minutes=30)
    window_end = first_btc_close + timedelta(days=4)
    fills = []
    for index in range(5):
        for product, close_time in (
            (PRODUCT, first_btc_close + timedelta(days=index)),
            (eth, first_eth_close + timedelta(days=index)),
        ):
            fills.extend(
                [
                    Fill(
                        product_id=product.product_id,
                        product_symbol=product.symbol,
                        quantity=1,
                        side="buy",
                        price=100,
                        fee=0,
                        created_at=close_time - timedelta(minutes=30),
                        role="maker",
                    ),
                    Fill(
                        product_id=product.product_id,
                        product_symbol=product.symbol,
                        quantity=1,
                        side="sell",
                        price=101 + index,
                        fee=0,
                        created_at=close_time,
                        role="maker",
                    ),
                ]
            )

    report = calculate(
        report_input(
            tmp_path,
            window_start=window_start,
            window_end=window_end,
            generated_at=window_end,
            products=[PRODUCT, eth],
            funding=[],
        ),
        fills,
    )

    utc_dates = [
        "2026-01-31",
        "2026-02-01",
        "2026-02-02",
        "2026-02-03",
        "2026-02-04",
    ]
    assert report.meta.window == "2026-01-31 to 2026-02-04"
    assert report.meta.fills == 20
    assert report.meta.trades == 10
    assert [item.date for item in report.daily] == utc_dates
    assert [item.date for item in report.drawdown] == utc_dates
    assert report.equity[0].date == "2026-01-31T18:00:00+00:00"
    assert report.equity[-1].date == "2026-02-04T19:00:00+00:00"
    assert [(item.month, item.trades) for item in report.monthly] == [
        ("2026-01", 2),
        ("2026-02", 8),
    ]
    assert [item.hour for item in report.hourly if item.trades] == [18, 19]
    assert {item.day: item.trades for item in report.day_of_week if item.trades} == {
        "Mon": 2,
        "Tue": 2,
        "Wed": 2,
        "Sat": 2,
        "Sun": 2,
    }
    assert report.correlation is not None
    assert report.correlation.tokens == ["ETH", "BTC"]
    assert report.correlation.matrix == [[1.0, 1.0], [1.0, 1.0]]


def test_post_window_recovery_cannot_change_drawdown_or_headline(
    tmp_path: Path,
) -> None:
    fills = []
    for day, pnl in [(0, 10), (1, -5), (10, 5)]:
        fills.extend(
            [
                fill("buy", 100, day * 24, fee=0),
                fill("sell", 100 + pnl, day * 24 + 1, fee=0),
            ]
        )

    report = calculate(
        report_input(tmp_path, window_end=START + timedelta(days=5)),
        fills,
    )

    duration = next(item for item in report.risk if item.label == "Longest drawdown")
    assert report.meta.fills == 4
    assert report.meta.trades == 2
    assert report.headline.net_pnl == 5
    assert duration.value == "5 days"
    assert duration.note == "ongoing at window end"


@pytest.mark.parametrize(
    ("daily_pnl", "window_days", "expected_days", "expected_note"),
    [
        (
            [(0, 10), (1, -5), (10, 5), (18, 1), (19, -1)],
            19,
            10,
            None,
        ),
        (
            [(0, 10), (1, -5), (11, 5), (20, -1)],
            25,
            11,
            None,
        ),
        (
            [(5, -1)],
            10,
            6,
            "ongoing at window end",
        ),
        (
            [(0, -1)],
            10,
            10,
            "ongoing at window end",
        ),
        (
            [(0, 10), (1, -5), (2, 5), (3, 1), (4, -1)],
            10,
            7,
            "ongoing at window end",
        ),
        (
            [(0, 10), (1, -5), (5, 5), (6, 1), (7, -1)],
            11,
            5,
            "ongoing at window end",
        ),
    ],
    ids=[
        "recovered-longer",
        "recovered-longer-after-idle-peak",
        "first-loss-after-idle-peak",
        "first-loss-on-window-start",
        "ongoing-longer",
        "ongoing-wins-tie",
    ],
)
def test_longest_drawdown_selects_the_interval_reported_as_ongoing(
    tmp_path: Path,
    daily_pnl: list[tuple[int, float]],
    window_days: int,
    expected_days: int,
    expected_note: str | None,
) -> None:
    fills = []
    for day, pnl in daily_pnl:
        fills.extend(
            [
                fill("buy", 100, day * 24, fee=0),
                fill("sell", 100 + pnl, day * 24 + 1, fee=0),
            ]
        )
    report = calculate(
        report_input(tmp_path, window_end=START + timedelta(days=window_days)),
        fills,
    )

    duration = next(item for item in report.risk if item.label == "Longest drawdown")
    assert duration.value == f"{expected_days} days"
    assert duration.note == expected_note


def test_cli_validates_and_writes_the_versioned_report_and_dashboard(
    tmp_path: Path,
) -> None:
    fills = tmp_path / "fills.csv"
    fills.write_text(
        "product_id,product_symbol,size,side,price,commission,created_at,role\n"
        "27,BTCUSD,1,buy,100,1,2026-01-01T00:00:00Z,maker\n"
        "27,BTCUSD,1,sell,150,1,2026-01-01T01:00:00Z,maker\n",
        encoding="utf-8",
    )
    source = tmp_path / "input.json"
    source.write_text(
        report_input(tmp_path, fills_csv="fills.csv", funding=[]).model_dump_json(),
        encoding="utf-8",
    )
    output = tmp_path / "report.json"
    dashboard = tmp_path / "report.html"

    run(source, output, dashboard)

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["meta"]["schema_version"] == "delta.pnl.report.v1"
    assert payload["headline"]["net_pnl"] == 48
    assert (
        json.loads(
            dashboard.read_text(encoding="utf-8")
            .split('<script id="pnl-data" type="application/json">', 1)[1]
            .split("</script>", 1)[0]
        )
        == payload
    )
    with pytest.raises(ValueError, match="must not overwrite"):
        run(source, source)


def test_dashboard_embedding_escapes_a_script_close_sequence() -> None:
    rendered = render_dashboard(
        {"headline": {"leak": "</script><script>bad()</script>"}}
    )
    island = rendered.split('<script id="pnl-data" type="application/json">', 1)[
        1
    ].split("</script>", 1)[0]

    assert "</script>" not in island
    assert json.loads(island)["headline"]["leak"] == "</script><script>bad()</script>"
