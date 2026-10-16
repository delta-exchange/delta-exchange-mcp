# Data acquisition and round-trip matching

The installed `delta-exchange-pnl` command owns this algorithm. This reference
explains its output; do not reimplement the matcher in generated code.

## 1. Fetch

| Step | Call | Why |
|---|---|---|
| 1 | `get_profile()` | Confirms auth and gives the user id. |
| 2 | `list_products(page_size=500)` | Product map keyed by `product_id`. Page through `meta.after` until exhausted; there are well over 500 products. |
| 3 | `bulk_fills_export(output_path, start_time_us)` or paged `get_fills` | The fills. |
| 4 | `get_wallet_transactions(transaction_types=["funding"], start_time_us=...)` | Funding is not in fills. |
| 5 | `get_margined_positions()` | Open positions, for unrealized P&L. |
| 6 | `get_wallet_balances()` | Equity, for portfolio context. |

From each product keep `contract_value`, `contract_type`, `underlying_asset.symbol`
and `symbol`. Expired products are absent from the default `list_products`
response; for a history that includes settled options, also pull
`get_settlement_prices(page_size=500)` and merge, or fall back to parsing the
underlying out of the fill's `product_symbol`.

Every `*_us` argument is microseconds since epoch. Multiply seconds by 1e6.

## 2. Match fills into round trips

Sort all fills ascending by `created_at`. Hold a FIFO queue of entry lots per
`product_id`:

```
lot = {signed_size, entry_price, remaining_entry_fee, opened_at}
```

`size` is signed: positive long, negative short. For each fill:

```
size   = int(fill.size)
price  = float(fill.price)
fee    = float(fill.commission)
signed = +size if fill.side == "buy" else -size
```

Skip the fill when `size` or `price` is zero.

**Same direction, or flat.** Append one lot. Do not average it with an older
lot. That separate entry price and time are what make the result FIFO.

**Opposing direction.** The fill closes, and may then flip:

```
close_qty = min(abs(signed_remaining), abs(oldest_lot.signed_size))
direction = "long" if oldest_lot.signed_size > 0 else "short"

pnl = close_qty * contract_value * (price - lot.entry_price)        # long
pnl = close_qty * contract_value * (lot.entry_price - price)        # short
```

Consume the oldest lot first. A close that spans three entry lots emits three
round trips. A partial close leaves the unused quantity in the oldest lot. A
fill that crosses through zero consumes the old queue, then opens one new lot in
the other direction.

Allocate each fill's commission by quantity. Keep its sign: a negative maker
commission is a rebate and increases net P&L. The emitted round trip receives
the matched share of the entry lot's remaining fee and the matched share of the
closing fill's fee. Leave the unconsumed entry fee on a partial lot. This keeps
total fees exact without charging an old entry fee twice.

Emit one round trip:

| Field | Value |
|---|---|
| `underlying` | `product.underlying_asset.symbol`, else the first 3 chars of `product_symbol` |
| `product_symbol` | from the fill |
| `instrument_type` | `call` / `put` if `contract_type` contains "call" / "put", else `perpetual` |
| `direction` | as above |
| `entry_time` / `exit_time` | the lot's `opened_at` / this fill's `created_at` |
| `entry_price` / `exit_price` | the lot's entry price / this fill's price |
| `size` | `close_qty` |
| `notional_value` | `close_qty * contract_value * price` |
| `pnl` | gross, from above |
| `fees` | the quantity-matched entry fee plus the quantity-matched exit fee |
| `net_pnl` | `pnl - fees` |
| `pnl_pct` | `pnl / notional_value * 100`, or 0 when notional is 0 |
| `hold_duration_hours` | `(exit_time - entry_time) / 3600` |

## 3. Known limits of this method

State these when they apply rather than letting the reader assume otherwise.

- **Positions open at the start of the window** produce an exit with no matching
  entry. They are skipped, because the first fill seen for a product is treated
  as an opening fill. A window that begins mid-position understates activity —
  another reason to pass a real `start_time_us`.
- **`funding_pnl` on a round trip is 0.** Funding is settled against the
  position, not the fill, so it is accounted separately from
  `get_wallet_transactions`. Never add it into `net_pnl` per trade.
- **Options that expired worthless** may have no closing fill at all. They
  settle. Reconcile with `get_settlement_prices` if the user's history is
  options-heavy and the numbers look light.
- **A missing product contract stops the calculation.** The calculator never
  defaults `contract_value` to 1 because that can silently rescale every result.
