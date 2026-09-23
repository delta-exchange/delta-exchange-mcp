import json

import httpx
import pytest
import respx

from delta_exchange_mcp.client import DeltaClient
from delta_exchange_mcp.config import INDIA_TESTNET_REST, Config
from delta_exchange_mcp.tools import market
from delta_exchange_mcp.tools.market import DEFAULT_MIN_TURNOVER_USD, _movers

# Trimmed from a real `/tickers?contract_types=perpetual_futures` response
# (api.india.delta.exchange, 2026-09-23). Numeric fields arrive as a mix of native
# numbers and strings, matching the live payload, so parsing must handle both.
LIVE_TICKERS = [
    {
        "symbol": "BTCUSD",
        "open": 85447.0,
        "close": 86429.5,
        "high": 87256.5,
        "low": 85145.0,
        "mark_price": "86429.81570295",
        "turnover_usd": 1_119_841_938.7389941,
        "oi_value_usd": "78637378.9317",
        "oi_change_usd_6h": "-769816.3400",
        "funding_rate": "0.009904245594552821",
        "timestamp": 1790143311665081,
    },
    {
        "symbol": "ETHUSD",
        "open": 2734.25,
        "close": 2754.3,
        "high": 2787.85,
        "low": 2714.7,
        "mark_price": "2754.32329289",
        "turnover_usd": 706_906_887.3589928,
        "oi_value_usd": "45265035.7397",
        "oi_change_usd_6h": "2646362.3300",
        "funding_rate": "0.006692546144076279",
        "timestamp": 1790143311665081,
    },
    {
        "symbol": "SOLUSD",
        "open": 117.059,
        "close": 118.45,
        "high": 119.7379,
        "low": 115.787,
        "mark_price": "118.44025112",
        "turnover_usd": 172_005_993.1228003,
        "oi_value_usd": "16040876.7271",
        "oi_change_usd_6h": "680664.0000",
        "funding_rate": "0.01",
        "timestamp": 1790143311665081,
    },
    {
        # Real 24h move (-4.0%) on trivial turnover_usd ($37.8k) — the kind of thin,
        # noisy print the turnover floor exists to exclude.
        "symbol": "STXUSD",
        "open": 0.3473,
        "close": 0.3334,
        "high": 0.3547,
        "low": 0.3334,
        "mark_price": "0.33368355",
        "turnover_usd": 37_801.5113,
        "oi_value_usd": "62676.6660",
        "oi_change_usd_6h": "18894.5200",
        "funding_rate": "0.01",
        "timestamp": 1790143311665081,
    },
    {
        # Biggest genuine gainer in the same snapshot: +30.0% on $38.2M turnover.
        "symbol": "BCHUSD",
        "open": 265.07,
        "close": 344.59,
        "high": 350.0,
        "low": 263.76,
        "mark_price": "344.53193446",
        "turnover_usd": 38_156_997.95989992,
        "oi_value_usd": "499769.7750",
        "oi_change_usd_6h": "122108.0300",
        "funding_rate": "0.01",
        "timestamp": 1790143311665081,
    },
]


def test_change_pct_matches_close_open_formula():
    """change_pct_24h is (close-open)/open*100 — verified against Delta's own
    ltp_change_24h (docs: 'last traded price change over 24 hours') to float noise."""
    result = _movers(LIVE_TICKERS, top_n=10, min_turnover_usd=0)
    by_symbol = {
        row["symbol"]: row
        for lst in (result["gainers"], result["losers"])
        for row in lst
    }
    assert by_symbol["BTCUSD"]["change_pct_24h"] == round(
        (86429.5 - 85447.0) / 85447.0 * 100, 2
    )
    assert by_symbol["ETHUSD"]["change_pct_24h"] == round(
        (2754.3 - 2734.25) / 2734.25 * 100, 2
    )
    assert by_symbol["BCHUSD"]["change_pct_24h"] == round(
        (344.59 - 265.07) / 265.07 * 100, 2
    )


def test_gainers_and_losers_are_sorted():
    result = _movers(LIVE_TICKERS, top_n=10, min_turnover_usd=0)
    gainers_pct = [row["change_pct_24h"] for row in result["gainers"]]
    losers_pct = [row["change_pct_24h"] for row in result["losers"]]
    assert gainers_pct == sorted(gainers_pct, reverse=True)
    assert losers_pct == sorted(losers_pct)
    assert result["gainers"][0]["symbol"] == "BCHUSD"
    assert result["losers"][0]["symbol"] == "STXUSD"


def test_most_active_sorted_by_turnover_desc():
    result = _movers(LIVE_TICKERS, top_n=10, min_turnover_usd=0)
    turnovers = [row["turnover_usd"] for row in result["most_active"]]
    assert turnovers == sorted(turnovers, reverse=True)
    assert result["most_active"][0]["symbol"] == "BTCUSD"


def test_oi_buildup_sorted_by_oi_change_desc_and_keeps_negative():
    result = _movers(LIVE_TICKERS, top_n=10, min_turnover_usd=0)
    changes = [row["oi_change_usd_6h"] for row in result["oi_buildup"]]
    assert changes == sorted(changes, reverse=True)
    # BTCUSD's real oi_change_usd_6h is negative — must survive parsing, not be skipped.
    btc = next(row for row in result["oi_buildup"] if row["symbol"] == "BTCUSD")
    assert btc["oi_change_usd_6h"] < 0


def test_funding_extremes_highest_and_lowest():
    tickers = LIVE_TICKERS + [
        {**LIVE_TICKERS[0], "symbol": "LOWFUND", "funding_rate": "-0.02"},
    ]
    result = _movers(tickers, top_n=10, min_turnover_usd=0)
    assert (
        result["funding_extremes"]["highest"][0]["funding_rate"]
        >= result["funding_extremes"]["lowest"][0]["funding_rate"]
    )
    assert result["funding_extremes"]["lowest"][0]["symbol"] == "LOWFUND"


def test_turnover_floor_excludes_thin_symbols():
    result = _movers(LIVE_TICKERS, top_n=10, min_turnover_usd=DEFAULT_MIN_TURNOVER_USD)
    symbols = {
        row["symbol"]
        for lst in (result["gainers"], result["losers"], result["most_active"])
        for row in lst
    }
    assert "STXUSD" not in symbols
    assert (
        result["universe"] == 4
    )  # STXUSD ($37.8k) is the only one under the $250k floor


def test_bad_values_are_skipped():
    tickers = [
        {**LIVE_TICKERS[0], "symbol": "MISSING_MARK", "mark_price": None},
        {**LIVE_TICKERS[0], "symbol": "ZERO_OPEN", "open": 0},
        {**LIVE_TICKERS[0], "symbol": "NOT_A_NUMBER", "close": "N/A"},
        {**LIVE_TICKERS[0], "symbol": "NO_SYMBOL_FIELD"},
    ]
    tickers[-1].pop("symbol")
    result = _movers(tickers, top_n=10, min_turnover_usd=0)
    assert result["universe"] == 0
    assert result["gainers"] == []
    assert result["losers"] == []


def test_zero_signed_delta_is_also_skipped():
    """oi_change_usd_6h / funding_rate == 0 is treated like missing (rare in live data)."""
    tickers = [{**LIVE_TICKERS[0], "symbol": "ZERO_FUNDING", "funding_rate": "0"}]
    result = _movers(tickers, top_n=10, min_turnover_usd=0)
    assert result["universe"] == 0


def test_negative_oi_change_and_funding_are_not_skipped():
    tickers = [
        {
            **LIVE_TICKERS[0],
            "symbol": "NEGATIVE_DELTAS",
            "oi_change_usd_6h": "-100",
            "funding_rate": "-0.5",
        }
    ]
    result = _movers(tickers, top_n=10, min_turnover_usd=0)
    assert result["universe"] == 1
    row = result["gainers"][0]
    assert row["oi_change_usd_6h"] == -100.0
    assert row["funding_rate"] == -0.5


def test_empty_tickers_returns_empty_result_and_falls_back_to_now():
    result = _movers([], top_n=10, min_turnover_usd=0)
    assert result["universe"] == 0
    assert (
        result["gainers"]
        == result["losers"]
        == result["most_active"]
        == result["oi_buildup"]
        == []
    )
    assert result["funding_extremes"] == {"highest": [], "lowest": []}
    assert result["note"] == "Market data only, not investment advice."
    assert result[
        "as_of"
    ]  # a valid ISO string was produced from the fetch-time fallback


def test_top_n_is_respected():
    tickers = [
        {**LIVE_TICKERS[0], "symbol": f"SYM{i}", "turnover_usd": 1_000_000 + i}
        for i in range(20)
    ]
    result = _movers(tickers, top_n=3, min_turnover_usd=0)
    assert len(result["gainers"]) == 3
    assert len(result["most_active"]) == 3


def test_funding_extremes_capped_at_five_even_for_large_top_n():
    tickers = [
        {**LIVE_TICKERS[0], "symbol": f"SYM{i}", "funding_rate": str(0.001 * (i + 1))}
        for i in range(30)
    ]
    result = _movers(tickers, top_n=50, min_turnover_usd=0)
    assert len(result["funding_extremes"]["highest"]) == 5
    assert len(result["funding_extremes"]["lowest"]) == 5


def test_funding_extremes_at_least_one_row_for_top_n_one():
    tickers = [{**LIVE_TICKERS[0], "symbol": f"SYM{i}"} for i in range(3)]
    result = _movers(tickers, top_n=1, min_turnover_usd=0)
    assert len(result["gainers"]) == 1
    assert len(result["funding_extremes"]["highest"]) == 1
    assert len(result["funding_extremes"]["lowest"]) == 1


def test_as_of_uses_max_ticker_timestamp():
    tickers = [
        {**LIVE_TICKERS[0], "symbol": "A", "timestamp": 1_000_000_000_000},
        {**LIVE_TICKERS[0], "symbol": "B", "timestamp": 2_000_000_000_000},
    ]
    result = _movers(tickers, top_n=10, min_turnover_usd=0)
    assert result["as_of"] == "1970-01-24T03:33:20+00:00"


def test_full_live_size_response_is_compact():
    """220 synthetic tickers (the live perpetual_futures universe size) must still
    serialize small — the point of ranking + rounding instead of returning raw rows.

    Every field is driven off the same per-symbol rank `i`, so the names with the
    biggest 24h move also carry the biggest turnover/OI/funding swings — as in real
    markets, where a big mover usually shows up across more than one list. That gives
    gainers/most_active/oi_buildup/funding_extremes real overlap instead of the
    maximally-adversarial case of 50 fully distinct rows.
    """
    tickers = [
        {
            "symbol": f"SYM{i}USD",
            "open": 100.0 + i,
            "close": 101.0 + i * 0.9,  # change_pct_24h falls monotonically as i rises
            "high": 105.0 + i,
            "low": 95.0 + i,
            "mark_price": str(100.5 + i),
            "turnover_usd": 2_000_000.0 - i * 5_000,  # biggest movers, biggest turnover
            "oi_value_usd": str(500_000.0 + i * 10),
            "oi_change_usd_6h": str(
                5_000.5 - i * 50
            ),  # biggest movers, biggest OI buildup (offset avoids landing on exactly 0)
            "funding_rate": str(
                0.0100005 - i * 0.00005
            ),  # biggest movers, richest funding (ditto)
            "timestamp": 1790143311665081,
        }
        for i in range(220)
    ]
    result = _movers(tickers, top_n=10, min_turnover_usd=DEFAULT_MIN_TURNOVER_USD)
    assert result["universe"] == 220
    encoded = json.dumps(result).encode()
    # Real `/tickers?contract_types=perpetual_futures` for these same 220 symbols is
    # 269,483 bytes (measured live, 2026-09-23) — every field on every product. 50 rows
    # (4 lists x top_n=10 + 2 lists x funding_n=5) of only the spec's 9 named fields
    # carry an inherent ~150 bytes/row of key/quote/comma overhead before a single value
    # is written, which puts the honest floor near 10.5KB — still a >25x cut versus raw.
    live_raw_bytes = 269_483
    assert len(encoded) < 10_600, f"response is {len(encoded)} bytes"
    assert len(encoded) < live_raw_bytes / 25


@pytest.fixture
def client() -> DeltaClient:
    cfg = Config(env="india_testnet", base_url=INDIA_TESTNET_REST)
    return DeltaClient(cfg)


@pytest.mark.asyncio
@respx.mock
async def test_get_market_movers_defaults_to_perpetual_futures_and_makes_one_fetch(
    client: DeltaClient,
):
    from mcp.server.fastmcp import FastMCP

    route = respx.get(f"{INDIA_TESTNET_REST}/tickers").mock(
        return_value=httpx.Response(200, json={"success": True, "result": LIVE_TICKERS})
    )
    mcp = FastMCP("test")
    market.register(mcp, client)
    await mcp.call_tool("get_market_movers", {})
    assert route.call_count == 1
    assert "contract_types=perpetual_futures" in str(route.calls[0].request.url)


@pytest.mark.asyncio
@respx.mock
async def test_get_market_movers_passes_through_explicit_contract_types(
    client: DeltaClient,
):
    from mcp.server.fastmcp import FastMCP

    route = respx.get(f"{INDIA_TESTNET_REST}/tickers").mock(
        return_value=httpx.Response(200, json={"success": True, "result": []})
    )
    mcp = FastMCP("test")
    market.register(mcp, client)
    await mcp.call_tool("get_market_movers", {"contract_types": ["futures"]})
    assert "contract_types=futures" in str(route.calls[0].request.url)
