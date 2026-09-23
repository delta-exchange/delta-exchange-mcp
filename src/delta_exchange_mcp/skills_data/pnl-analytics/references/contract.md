# Local calculator contract

The calculator is installed with this server as `delta-exchange-pnl`. It reads
one JSON input file and the fill CSV named by that file. It writes a versioned
JSON report and, when requested, a self-contained HTML dashboard.

Run it without copying the fills into the conversation. The `uvx --from`
form works when the MCP server itself was installed with `uvx` and the second
entry point is not on the shell's `PATH`:

```sh
uvx --from delta-exchange-mcp delta-exchange-pnl \
  --input ~/.delta-exchange-mcp/reports/pnl-input.json \
  --output ~/.delta-exchange-mcp/reports/pnl-report.json \
  --dashboard ~/.delta-exchange-mcp/reports/pnl-report.html
```

## Input: `delta.pnl.input.v1`

Every key shown below is checked. Unknown keys and missing required keys fail the
calculation. `fills_csv` is relative to the input file unless it is absolute.

```json
{
  "schema_version": "delta.pnl.input.v1",
  "fills_csv": "fills.csv",
  "window_start": "2026-01-01T00:00:00Z",
  "window_end": "2026-08-01T00:00:00Z",
  "generated_at": "2026-08-01T12:00:00Z",
  "products": [
    {
      "product_id": 27,
      "symbol": "BTCUSD",
      "underlying": "BTC",
      "contract_type": "perpetual_futures",
      "contract_value": 0.001
    }
  ],
  "funding": [
    {"amount": -1.2, "created_at": "2026-01-02T08:00:00Z", "product_id": 27}
  ],
  "positions": []
}
```

The fill CSV must contain `product_id`, `product_symbol`, `size`, `side`,
`price`, `commission`, `created_at`, and `role`. `created_at` accepts an ISO 8601
timestamp or a Unix timestamp in seconds, milliseconds, or microseconds.
The report window is inclusive. Fills before `window_start` are used only to
reconstruct FIFO lots for positions that close inside the window. Fill counts,
charges, funding, and realized trades include only activity from `window_start`
through `window_end`; later activity is ignored.

Each product used by a fill or funding transaction must have one product entry.
The calculator fails if a contract value is missing. It never assumes a contract
value of 1. Map `product_id` from the product's `id`, and map `underlying` from
`underlying_asset.symbol`.

`funding: null` means funding was not fetched. `funding: []` means funding was
fetched and the result was empty. The report keeps these states separate. The
same rule applies to `positions`.

Position entries have these required keys: `symbol`, `underlying`,
`contract_type`, `size`, `contract_value`, `index_price`, `entry_price`,
`mark_price`, `unrealized_pnl`, and `margin`. `liquidation_price` is optional.
Use the current underlying index price for `index_price`, not an option premium.

## Output: `delta.pnl.report.v1`

The output is the JSON object embedded in `assets/dashboard.html`. Its
`meta.schema_version` is `delta.pnl.report.v1`. It contains the headline,
grade dimensions, FIFO equity and drawdown series, daily and monthly groups,
hourly and weekday groups, instrument and underlying groups, correlation,
funding, risk, charges, and open positions.

Money keeps full precision during calculation and is rounded to two decimal
places at the report boundary. Percentages are rounded to the precision of the
displayed metric. A grade is absent for fewer than 30 matched round trips.
