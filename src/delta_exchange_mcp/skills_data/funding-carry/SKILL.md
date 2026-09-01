---
name: funding-carry
description: Scan perpetual funding rates across Delta India, rank the annualised carry, and flag the traps that make a high rate unearnable. Needs no API key.
requires: public
---

# Funding carry scan

Rank Delta India perpetuals by the funding they pay, then say which of those
numbers a person could actually collect.

## When to run this

The user asks which perps are worth the carry, where funding is richest, what
they would earn shorting a funded perp, or simply "show me funding rates". Also
run it when someone asks whether a specific perp is expensive to hold.

This skill needs no credentials. It works on a server configured for public data
only.

## How funding works here

Delta India settles funding every 8 hours. A positive rate means longs pay
shorts, so the short side collects. A negative rate means shorts pay longs.
Funding is charged on the mark price (`funding_method: mark_price`).

### The scale trap — read this before you compute anything

Two fields in the same ticker payload use different scales:

- `funding_rate` is a **percent per 8 hours**. `0.01` means 0.01%, not 1%.
- `mark_basis` is a **fraction**. `-0.00035118` means -0.035%.

Mixing them up inflates an answer by 100x. BTCUSD sits at the venue's usual
`0.01` floor, which annualises to **10.95%**, not 1095% and not 0.1%.

Annualised carry, in percent:

```
annualised_pct = funding_rate * 3 * 365      # 3 settlements a day
```

## Procedure

1. `list_tickers(contract_types=["perpetual_futures"])` — one call returns every
   perp with `funding_rate`, `oi_value_usd`, `mark_price`, `spot_price`,
   `mark_basis`, `turnover_usd` and `volume`. Do not loop `get_ticker` per
   symbol; there are over 200 perps.
2. Drop anything with `product_trading_status` other than `operational`.
3. Drop anything below a liquidity floor. Default to `oi_value_usd >= 250000`.
   Say in the output what floor you used and how many symbols it removed.
4. Compute `annualised_pct` for the rest and sort by absolute value, descending.
5. For the top 5 to 10 only, call `get_funding_history(symbol, resolution="1h")`
   over the last 7 days and compute:
   - the mean realised rate over the window,
   - the share of hours whose sign matches the current rate.
   A rate that has held its sign for under about 70% of the week is a snapshot,
   not a carry.
6. Report. Never scan history for all 200 symbols — it is 200 round-trips for
   information that changes nothing about the ranking.

## What to flag on every candidate

State these next to the number, not in a footnote. A high rate with any of these
attached is not an opportunity.

- **Thin open interest.** `oi_value_usd` under a few hundred thousand means the
  position cannot be sized or exited. Rank it, then say it is untradeable.
- **A rate that just flipped.** Compare the current `funding_rate` sign with the
  7-day history. A fresh flip usually mean-reverts within a settlement or two.
- **The fee floor.** Entering and exiting a perp costs 0.05% taker each way, so
  0.10% round-trip (`taker_commission_rate: 0.0005`). At the 0.01%/8h baseline
  that is 0.03% a day, so the trade needs roughly **3.3 days** just to clear
  fees. Compute the real break-even for each candidate and print it in days.
- **The carry is only real when the leg is hedged.** An unhedged short in a perp
  paying 40% annualised still carries full price risk. A 5% move against the
  position erases six weeks of funding. Say this plainly whenever the user talks
  about "earning" the rate.
- **Floor clustering.** Many symbols sit exactly at `0.01`. They tie; do not
  present an arbitrary ordering among them as a ranking.

## Output

A table of the top candidates, then two or three sentences of judgement.

| Symbol | Funding /8h | Annualised | Side that collects | OI (USD) | 7d sign held | Fee break-even |
|---|---|---|---|---|---|---|

Follow it with: which one or two are genuinely worth attention and why, which
look rich but fail on depth or persistence, and a single line on the hedge
requirement.

Keep it under fifteen lines unless the user asks to see the full scan.

## Boundaries

This skill reads market data and describes what it finds. It does not place,
size, or cancel orders, and it must not present a scan as a recommendation to
trade. If the user asks you to act on it, say the server's trading tools are a
separate opt-in surface and let them decide.
