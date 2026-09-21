# Changelog

All notable changes to this project are documented here. Versions follow SemVer, with
major bumps deferred until the package leaves Beta — so a breaking change lands as a minor
bump and is marked BREAKING.

## 0.7.0

### BREAKING — the trading guardrails are gone (DEA-881)

Trading is now gated solely by the API key's own permissions. A key with Trading permission
can place orders as soon as it is configured; a Read Data key cannot, because Delta rejects
the request. Everything the server used to do above that line has been removed.

**If you relied on any of this, read before upgrading.**

- **`DELTA_MCP_MODE` is gone**, along with the per-client `DELTA_MCP_MODE_<SLUG>_<DIGEST>`
  scheme and the restart it required. The trading tools register whenever credentials are
  present. Settings files that still carry the old keys keep loading — the keys are ignored,
  not rejected — and the bundle's Mode field is gone from its install form.
- **`dry_run` is gone** from all 13 mutating tools. There is no way to rehearse an order;
  use `DELTA_MCP_ENV=india_testnet`.
- **The audit log is gone**, with `DELTA_MCP_AUDIT`, `DELTA_MCP_AUDIT_FILE`,
  `~/.delta-exchange-mcp/audit/` and the `get_trading_status` tool. Delta's own order and
  fill history remains the record of what executed.
- **`close_all_positions` takes no arguments and closes the entire account**, both margin
  scopes. It previously refused a bare call and required opting into a scope. This is the
  sharpest behaviour change in the release.
- **`cancel_all_orders` loses its order-kind flags** and always cancels every kind.
  `product_id` and `contract_types` still narrow it.
- **Batch tools are uncapped** and return Delta's response unchanged. The 50-order limit and
  the `partial_failure` annotation are both gone, so a short response is the only signal that
  some legs were not accepted.
- **Client-side order validation is gone** — size, limit-price and bracket cross-field checks
  now surface as Delta's own errors instead.
- **Prices are sent exactly as given.** The tick-rounding preflight is gone, so an off-tick
  price is rejected by Delta rather than silently snapped, and a priced order no longer costs
  a `GET /products` round trip first.
- **`_meta["delta.exchange/mutating"]` is gone**, and the bundle verifier no longer asserts
  that a default install cannot mutate.
- **The world-readable-settings warning and the client-override warning are gone.**
  `get_connection_status` now returns `{environment, credentials_configured,
  account_tools_available, trading_tools_available, client_name, version, view_build}`.

### Changed

- `save_mode` is removed from the credential form, which now only saves credentials.
- The bundle manifest declares 44 tools, down from 46.
