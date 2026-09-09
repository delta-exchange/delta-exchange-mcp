"""Versioned input and output contracts for the local P&L calculator."""

from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

INPUT_VERSION = "delta.pnl.input.v1"
REPORT_VERSION = "delta.pnl.report.v1"


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Product(Contract):
    product_id: int
    symbol: str
    underlying: str
    contract_type: str
    contract_value: float = Field(gt=0)


class Funding(Contract):
    amount: float
    created_at: AwareDatetime
    product_id: int | None = None
    underlying: str | None = None

    @model_validator(mode="after")
    def has_instrument(self) -> "Funding":
        if self.product_id is None and not self.underlying:
            raise ValueError("funding needs product_id or underlying")
        return self


class Position(Contract):
    symbol: str
    underlying: str
    contract_type: str
    size: float
    contract_value: float = Field(gt=0)
    index_price: float = Field(ge=0)
    entry_price: float
    mark_price: float
    unrealized_pnl: float
    margin: float = Field(ge=0)
    liquidation_price: float | None = None


class ReportInput(Contract):
    schema_version: Literal[INPUT_VERSION]
    fills_csv: Path
    window_start: AwareDatetime
    window_end: AwareDatetime
    generated_at: AwareDatetime
    products: list[Product]
    funding: list[Funding] | None = None
    positions: list[Position] | None = None

    @model_validator(mode="after")
    def valid_window(self) -> "ReportInput":
        if self.window_end < self.window_start:
            raise ValueError("window_end must be on or after window_start")
        return self


class Meta(Contract):
    schema_version: Literal[REPORT_VERSION]
    generated: str
    window: str
    fills: int
    trades: int
    tier: Literal["A"] = "A"
    notes: str | None = None


class Headline(Contract):
    net_pnl: float
    unrealized: float | None
    win_rate: float
    total_fees: float
    funding: float | None
    net_including_funding: float | None
    grade: str | None
    score: float | None
    persona: str
    leak: str | None = None


class GradeDimension(Contract):
    label: str
    score: float
    max: float = 25


class EquityPoint(Contract):
    date: str
    cumulative: float


class DrawdownPoint(Contract):
    date: str
    drawdown: float | None


class Stat(Contract):
    label: str
    value: str
    note: str | None = None
    tone: str | None = None


class Daily(Contract):
    date: str
    pnl: float
    trades: int


class Monthly(Contract):
    month: str
    trades: int
    gross_pnl: float
    fees: float
    net_pnl: float
    win_rate: float
    best: float
    worst: float


class PeriodBucket(Contract):
    hour: int | None = None
    day: str | None = None
    avg_pnl: float
    trades: int
    win_rate: float

    @model_validator(mode="after")
    def has_one_label(self) -> "PeriodBucket":
        if (self.hour is None) == (self.day is None):
            raise ValueError("period bucket needs exactly one of hour or day")
        return self


class Distribution(Contract):
    range: str
    count: int


class Underlying(Contract):
    underlying: str
    num_trades: int
    pnl: float
    win_rate: float
    avg_return: float
    capital: float


class Instruments(Contract):
    perps_pnl: float
    perps_count: int
    calls_pnl: float
    calls_count: int
    puts_pnl: float
    puts_count: int


class Correlation(Contract):
    tokens: list[str]
    matrix: list[list[float]]


class TokenFunding(Contract):
    token: str
    pnl: float
    count: int


class FundingPoint(Contract):
    date: str
    cumulative: float


class FundingReport(Contract):
    total: float
    paid: float
    received: float
    by_token: list[TokenFunding]
    cumulative: list[FundingPoint]


class Pareto(Contract):
    index: int
    total: int
    leaders: list[str]


class FeeToken(Contract):
    token: str
    fees: float


class Charges(Contract):
    total_fees: float
    maker_fees: float
    taker_fees: float
    maker_fill_rate: float
    fees_pct_pnl: float | None
    fees_pct_volume: float | None
    gst_estimate: float
    trades_to_cover: int | None
    by_token: list[FeeToken]


class OpenPosition(Contract):
    symbol: str
    direction: Literal["long", "short"]
    size: float
    notional: float
    entry_price: float
    mark_price: float
    unrealized_pnl: float
    margin: float
    to_liquidation: str


class Report(Contract):
    meta: Meta
    headline: Headline
    grade_dimensions: list[GradeDimension]
    equity: list[EquityPoint]
    drawdown: list[DrawdownPoint]
    overview: list[Stat]
    daily: list[Daily]
    monthly: list[Monthly]
    hourly: list[PeriodBucket]
    day_of_week: list[PeriodBucket]
    pnl_distribution: list[Distribution]
    by_underlying: list[Underlying]
    instruments: Instruments
    correlation: Correlation | None
    funding: FundingReport | None
    pareto: Pareto | None
    risk: list[Stat]
    charges: Charges
    positions: list[OpenPosition] | None
