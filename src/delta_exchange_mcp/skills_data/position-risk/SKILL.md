---
name: position-risk
description: Summarise open positions on Delta India with real notional exposure, distance to liquidation, margin headroom, and concentration. Needs API credentials.
requires: credentials
---

# Position and risk snapshot

Answer "where am I exposed right now, and what kills me" in one pass.

## When to run this

The user asks about open positions, current exposure, risk, margin, liquidation,
or how close they are to trouble. Also run it before any question that assumes
knowledge of current positions, such as "should I hedge".

## Get the data

1. `get_margined_positions()` — the only position tool worth using here. It
   carries `size`, `entry_price`, `mark_price`, `margin`, `liquidation_price`,
   `unrealized_pnl`, `realized_pnl` and `realized_funding`.
   Do **not** use `get_positions`: it returns `entry_price` and `size` and
   nothing else, so every risk number below would be missing.
2. `get_wallet_balances()` — `balance`, `available_balance`, `position_margin`
   and `strategy_blocked_amount` per asset.
3. Only if a position is missing `contract_value` or an index price, call
   `get_product(symbol)` for that one symbol. Do not fetch the whole product
   list for a handful of positions.
4. For each open option, call `get_ticker(symbol)` for its current delta. Read
   `delta` from the ticker or its greeks object when present. Do not use an old
   fill-time delta or estimate one from the option side.

## Three traps that produce wrong numbers

**Notional for options.** Exposure is driven by the underlying, not the premium:

```
notional_usd = abs(size) * contract_value * index_price
```

Use `index_price` (spot of the underlying). `mark_price` on an option is the
premium, so multiplying by it understates a short call's real exposure by orders
of magnitude — $7.60 instead of $542.70 on the example in the tool's own
docstring. Read `contract_value` from the position, falling back to
`product.contract_value` when it is nested.

**`unrealized_pnl` on short options is already corrected.** The upstream API
returns the unsigned premium value for short options; this server patches it to
`(mark_price - entry_price) * size * contract_value` before you see it. Use the
field as given. Do not "fix" it again — you would flip the sign back.

**`strategy_blocked_amount` is not a problem.** It is collateral reserved by an
active Algo Marketplace subscription. Report it as reserved, never as an anomaly
or a risk finding.

## Compute

`size` is signed: positive is long, negative is short.

- **Distance to liquidation**, always as a positive percentage move against the
  position:
  - long: `(mark_price - liquidation_price) / mark_price * 100`
  - short: `(liquidation_price - mark_price) / mark_price * 100`

  `liquidation_price` can be null or zero, most often on options. Print `n/a`
  rather than a fabricated number, and say why.
- **Effective leverage**: `notional_usd / margin` for the position;
  `total_notional / total_equity` for the book.
- **Concentration**: each underlying's share of total notional. Flag any single
  underlying above 50%.
- **Margin headroom**: `available_balance / balance`. Under 20% is thin — a
  routine adverse move starts forcing liquidations rather than just drawdown.
- **Gross exposure**: sum the absolute notional above. Keep this separate from
  directional exposure.
- **Directional net for futures**: `size * contract_value * index_price`.
- **Directional net for options**: delta-equivalent exposure is
  `size * contract_value * index_price * delta`. A put delta is negative, so a
  short put and a long call can point in the same direction. If an open option
  has no current delta, report directional net as `n/a` for that underlying and
  for the whole book. Do not substitute the position side. Gross exposure is
  still available.

## Output

Lead with the single riskiest thing, then the table.

| Symbol | Side | Size | Notional (USD) | Entry → Mark | Unrealized | Margin | To liquidation |
|---|---|---|---|---|---|---|---|

Then three or four lines: total notional and effective leverage, margin
headroom, the concentration call, and the nearest liquidation with the move
required to reach it. Name the position that fails first if the market moves
against the book, and by how much it would have to move.

If there are no open positions, say so in one line and stop. Do not produce an
empty table.

## Boundaries

This skill reads and describes. It does not place, size, close, or hedge
anything, and it must not tell the user what to trade. Sizing and stop advice
belongs to the person holding the risk. If they ask you to act, say the trading
tools are a separate opt-in surface (`DELTA_MCP_MODE=trade`) and stop there.
