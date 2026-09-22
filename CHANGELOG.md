# Changelog

All notable changes to this project are documented here. Versions follow SemVer, with
major bumps deferred until the package leaves Beta — so a breaking change lands as a minor
bump and is marked BREAKING.

## 0.7.0

### BREAKING — the trading guardrails are gone

Trading is now gated solely by the API key's own permissions. A key with Trading permission
can place orders as soon as it is configured; a Read Data key cannot, because Delta rejects
the request. Everything the server used to do above that line has been removed.

**If you relied on any of this, read before upgrading.**

- **`DELTA_MCP_MODE` is gone**, along with the per-client `DELTA_MCP_MODE_<SLUG>_<DIGEST>`
  scheme and the restart it required. The trading tools register whenever credentials are
  present. Settings files that still carry the old keys keep loading — the keys are ignored,
  not rejected — and the bundle's Mode field is gone from its install form.
- **`dry_run` is gone** from every mutating tool. There is no way to rehearse an order;
  use `DELTA_MCP_ENV=india_testnet`.
- **The audit log is gone**, with `DELTA_MCP_AUDIT`, `DELTA_MCP_AUDIT_FILE`,
  `~/.delta-exchange-mcp/audit/` and the `get_trading_status` tool. Delta's own order and
  fill history remains the record of what executed. Upgrading does not delete audit files an
  earlier version already wrote — they are yours to keep or remove.
- **`close_all_positions` takes no arguments and closes the entire account**, both margin
  scopes. It previously refused a bare call and required opting into a scope. This is the
  sharpest behaviour change in the release.
- **`cancel_all_orders` loses its order-kind flags** and always cancels every kind.
  `product_id` and `contract_types` still narrow it.
- **Batch tools are uncapped** and return Delta's response unchanged. The 50-order limit and
  the `partial_failure` annotation are both gone, so a short response is the only signal that
  some legs were not accepted.
- **Client-side order validation is gone** — size, limit-price and bracket cross-field checks
  now surface as Delta's own errors instead. The checks that decide whether a request is
  well-formed at all stay: one of `product_id`/`product_symbol`, exactly one of
  `id`/`client_order_id` on `cancel_order`, and at least one leg on `place_bracket_order`.
- **Prices are sent exactly as given.** The tick-rounding preflight is gone, so Delta, not
  the server, moves an off-tick price onto the tick, and a priced order no longer costs a
  `GET /products` round trip first.
- **`_meta["delta.exchange/mutating"]` is gone**, and the bundle verifier no longer asserts
  that a default install cannot mutate.
- **The world-readable-settings warning and the client-override warning are gone.**
  `get_connection_status` now returns `{environment, credentials_configured,
  account_tools_available, trading_tools_available, client_name, version, view_build}`.

### BREAKING — `get_profile` is gone

Delta retired `/profile` for API-key requests, so `get_profile` failed on every call.
`close_all_positions` and the credential form's key check also depended on it and now read
the account from `/users/trading_preferences`. A key without permission for that endpoint
is rejected before it is saved, with a message naming the missing permission.

### Changed

- `save_mode` is removed from the credential form, which now only saves credentials.
- `login` shows a `*` for each character of the API key and secret, so a paste visibly
  lands. It used to echo nothing.
- The bundle manifest declares 43 tools, down from 46.

### Fixed

- **Mutations are no longer retried after a transport error.** Resending after a lost
  response could place an order twice. A connection that fails before the request is sent
  returns `upstream_unreachable`; a failure after it may have reached Delta returns
  `execution_outcome_unknown` and names the reads that show whether it landed. `GET`
  requests keep their bounded retries.
- The debug log refuses a directory that another OS user owns or can write to, checking
  every path component including symlink targets.
- The bundle build caches the mcpb CLI under `~/.cache/delta-exchange-mcp/mcpb-cli` rather
  than the shared temporary directory, and checks the cache's ownership before running
  anything from it.
