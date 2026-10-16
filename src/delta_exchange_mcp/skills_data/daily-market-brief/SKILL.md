---
name: daily-market-brief
description: Summarise what moved on Delta India over the last 24 hours — gainers, losers, activity, open-interest build-up and funding extremes. Market data only, no predictions. Needs no API key.
requires: public
---

# Daily market brief

Answer "what's moving today" with the numbers, in one screen.

## When to run this

The user asks what is moving, what is up or down today, which coins are pumping
or dumping, where the action is, or for a morning market check-in. Also run it
when they ask about one coin's day and would benefit from seeing it next to the
rest of the market.

This skill needs no credentials.

## Procedure

1. Call `get_market_movers()` with the defaults: perpetual futures, top 10, a
   $250k 24h turnover floor. If the tool is not available on this server, call
   `list_tickers(contract_types=["perpetual_futures"])` instead, drop symbols
   with `turnover_usd` under 250000, and compute the 24h change yourself as
   `(close - open) / open * 100`.
2. Call `get_ticker("BTCUSD")` and `get_ticker("ETHUSD")` only if they are not
   already in the lists — every brief anchors on them.
3. If the user named a coin, call `get_ticker` for its perpetual and place it
   against the lists: where it ranks, and how its turnover compares.

## Reading the numbers

- `change_pct_24h` is the last-traded-price move over 24 hours, in percent.
- `turnover_usd` is 24h notional. A big move on small turnover is thin, not
  strong — say so.
- `oi_change_usd_6h` rising alongside price means new positions are opening in
  that direction; rising against price means the move is being faded. Describe
  what the two numbers show, not what they will cause.
- `funding_rate` is a **percent per 8 hours** (`0.01` means 0.01%). Positive
  means longs pay shorts. Do not annualise unless asked; if asked, multiply by
  3 × 365.
- Quote `as_of` so the reader knows how fresh the snapshot is.

## Output

Three short blocks, no more than fifteen lines in all:

1. **Market tone** — one line on BTC and ETH: price, 24h %, turnover.
2. **Movers** — a table of the top 5 gainers and top 5 losers.

   | Symbol | 24h % | Price | Turnover (USD) | OI Δ 6h (USD) |
   |---|---|---|---|---|

3. **Worth noticing** — two or three bullets from `most_active`, `oi_buildup`
   and `funding_extremes`, each a fact with its number.

End with: "Market data as of <as_of>. Not investment advice."

## Boundaries

Describe what happened; never forecast what will happen next. No "likely to
rise", no targets, no buy or sell language, no ranking of what to trade. If the
user asks what to buy, say this brief reports market data and the decision is
theirs. It never places, edits or cancels orders; if the user wants to act,
they ask for that separately and every order is real and immediate.
