# Performance: startup, memory, and context cost

Measured with `scripts/bench.py` (stdlib only, N=5 median unless noted), macOS/arm64,
Python 3.12.13, mcp 1.26-line. Re-run with `uv run python scripts/bench.py` /
`--importtime` / `--api`.

## Startup (spawn -> tools/list reply)

| Surface | Wall time | Peak RSS (child) | tools/list | tool count |
|---|---|---|---|---|
| no key | ~750-880 ms | ~75 MB | 12.1 KB / ~3.0k tokens | 17 (market only) |
| fake key (`DELTA_API_KEY=x`/`DELTA_API_SECRET=y`) | ~745-875 ms | ~76 MB | 38.7 KB / ~9.7k tokens | 42 (market+account+trade) |

`prompts/list` and `resources/list` are both tiny (49 and 330 bytes) — not a lever here.

## Import time (`python -X importtime -c "import delta_exchange_mcp.server"`)

Total: ~725-870 ms. Top 5 self-time modules:

| self (us) | module |
|---|---|
| 305,380 | `pydantic_core._pydantic_core` |
| 180,244 | `rpds.rpds` |
| 84,531 | `mcp.types` |
| 71,370 | `jsonschema_specifications` |
| 25,622 | `pydantic_core.core_schema` |

All five are compiled extensions or schema machinery pulled in transitively by the `mcp`
SDK (pydantic -> pydantic_core; jsonschema_specifications/rpds via `mcp.types`'
JSON-schema generation). This confirms static suspect (d): import time is dominated by
the SDK dependency chain, not this repo's own modules. This repo's own modules
(`delta_exchange_mcp.form`, `.store`, `.client`, `.config`, `.credentials`, `.tools.*`)
each cost 0.7-1.6 ms self time — three orders of magnitude below the leaders and not
worth touching. In particular `form.py`'s module-level `VIEW_HTML` build (a
`_TEMPLATE.replace` + `json.dumps` of a small dict) costs ~1.6ms total for the whole
module including its imports — nowhere near the >10ms bar for lazy-loading it, so
suspect (e) is **not justified** and was left as-is.

**Not our lever**: the SDK import chain is upstream (`mcp` -> `pydantic`/`starlette`).
No fix applied.

## API response sizes (live, `--api`)

| Call | Bytes | ~Tokens |
|---|---|---|
| `list_products` (default `page_size=100`) | 391,562 | ~97,890 |
| `get_orderbook(BTCUSD)` with **no** `depth` | 174,002 | ~43,500 |
| `get_orderbook(BTCUSD, depth=100)` (the tool's own max) | 9,572 | ~2,400 |
| `get_orderbook(BTCUSD, depth=20)` | 2,042 | ~510 |

`get_orderbook`'s `depth` param was already bounded `ge=1, le=100` but defaulted to
`None`, which Delta's API treats as "no cap" — 2,477 buy levels + 1,180 sell levels for
BTCUSD instead of the 100-per-side the tool already advertises as its maximum. **Fixed**:
default changed from `None` to `100` (`tools/market.py`, `get_orderbook`). This is a
default-value fix only — the parameter's declared bounds, the return shape, and explicit
caller overrides (including passing `depth=None` deliberately) are unchanged. ~18x smaller
response for the common no-args call.

`list_products`'s default `page_size=100` returns ~98k tokens — each product row carries
full contract specs (greeks config, margin scaling, tick sizes, etc.), so 100 rows is
already very large. **Not fixed**: `page_size` is a documented, caller-visible knob with
existing cursor pagination (`after`), and lowering its default changes what a naive
no-args call returns by silently truncating the list rather than fixing an
oversight — a different kind of change than `get_orderbook`'s case, where `None` was
never a sensible "no argument passed" value. Recommendation: leave as-is for this PR;
if this needs to shrink, do it as a documented API change (e.g. default `page_size=25`)
coordinated with the description rewrite already in flight upstream (PR #65), not as a
silent perf patch.

## Tool-list token cost: top 10 heaviest schemas (fake-key surface)

| Bytes | Tool |
|---|---|
| 4,181 | `place_order` |
| 2,126 | `edit_bracket_order` |
| 1,886 | `bulk_fills_export` |
| 1,800 | `get_wallet_transactions` |
| 1,693 | `get_margined_positions` |
| 1,591 | `edit_order` |
| 1,438 | `place_bracket_order` |
| 1,330 | `get_fills` |
| 1,324 | `get_settlement_prices` |
| 1,282 | `get_order_history` |

These 10 tools alone are ~18.8 KB of the 38.7 KB armed `tools/list` payload (~48%).
Concrete trimming suggestions for a future pass (not applied here — out of scope per
task constraints, and upstream PR #65 already rewrites tool descriptions):

- `place_order` / `edit_order` / `place_bracket_order` / `edit_bracket_order` repeat the
  same order-flag descriptions (`post_only`, `reduce_only`, `time_in_force`,
  `stop_order_type`) near-verbatim across four tools. A shared constant string
  interpolated into each `Field(description=...)` would cut duplication without
  changing any schema's content.
- `get_wallet_transactions` / `bulk_fills_export` / `get_fills` / `get_order_history`
  each carry a multi-sentence prose docstring plus per-field descriptions that restate
  the same pagination contract (`page_size`, `after`, cursor semantics) already
  documented once in `list_products`. Factoring that into one shared description
  constant would be the highest-leverage token cut in this list.
- `get_settlement_prices`'s docstring already cross-references `list_products` in prose;
  the per-field `contract_types`/`page_size`/`after` descriptions could do the same by
  reference instead of restating the filter semantics.

## Fixes applied

### 1. `store.py::read()` — mtime+size cache

`reconcile()` (`server.py`) calls `store.read()` on every `tools/list` (via
`refresh_before_list`, registered as FastMCP's pre-list hook) to pick up an externally
edited credentials file without a restart. Before this change, that meant a fresh
`dotenv_values()` open+parse of the file on every single `tools/list`, regardless of
whether it had changed.

Measured directly (2,000 calls, isolated temp `HOME`):

| | before | after |
|---|---|---|
| `store.read()`, unchanged file | 368 us/call | 9 us/call (~40x) |

Cache is keyed on `(mtime_ns, size)` of the resolved config path; a stat() miss (file
changed or ge one) reparses. `store.write()` invalidates the cache entry directly at the
point it replaces the file, rather than relying on the new stat differing — a same-size,
same-tick rewrite (e.g. swapping one key for another of equal length, inside one
filesystem's mtime-resolution window) is exactly the case a passive cache would get
wrong, and it's cheap to close that gap at the one place this process changes the file.
Correctness (external edits still observed) and the invalidation edge case are covered
by new tests in `tests/test_store.py`.

This is a small absolute win (~360us saved per `tools/list`) against a startup dominated
by ~750-880ms of SDK import time — it does not move the wall-clock numbers above
noise. It's included because it's a correct, low-risk, already-scoped fix for a
real repeated-work bug (re-parsing an unchanged file on every list call), not because it
changes the headline startup number.

### 2. `tools/market.py::get_orderbook` — bounded default depth

See "API response sizes" above. One-line default-value change
(`Field(default=None, ...)` -> `Field(default=100, ...)`); no schema bound, return
shape, or explicit-argument behavior changed.

## Not applied (no fix, or explicitly out of scope)

- **Import time** (SDK dependency chain): not our lever, see above.
- **`form.py` import-time HTML build**: measured at ~1.6ms total module cost, far under
  the 10ms bar — not lazy-loaded.
- **`list_products` default `page_size`**: real oversized-default cost (~98k tokens) but
  changing it silently alters what a no-args call returns; recommendation left above
  rather than applied.
- **Tool-description deduplication** (top-10 table above): explicitly out of scope for
  this PR — tool descriptions in `tools/market.py`/`account.py`/`trading.py` are being
  rewritten wholesale in upstream PR #65; duplicating that work here would only create
  merge conflicts. Recommendations recorded above for whoever does that pass.
