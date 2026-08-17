# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project in one line

MCP server (stdio only, built on the `mcp` SDK's `MCPServer`) that wraps Delta Exchange India's REST API as MCP tools — public market data unconditionally, authenticated read-only account tools when `DELTA_API_KEY`/`DELTA_API_SECRET` are set, plus authenticated trading mutations when `DELTA_MCP_MODE=trade` is also set.

## Style

Don't add typing slop. In particular:

- Don't annotate pytest fixtures (`tmp_path`, `monkeypatch`, etc.) — pytest discovers them by name, the annotation adds nothing.
- Don't write `**kwargs: Any` / `-> Any` on internal test helpers. If the only honest type is `Any`, leave it off.
- Use `Any` only when it carries real information: a public boundary that genuinely accepts arbitrary JSON, a return type that is genuinely heterogeneous. Otherwise prefer the real type or no annotation at all.
- Don't add `from typing import Any` just to satisfy a redundant annotation.

## Commands

```bash
uv sync                                        # install deps (runtime + dev)
uv run pytest                                  # run full suite (asyncio_mode=auto)
uv run pytest tests/test_market_tools.py::test_429_retries_then_succeeds  # single test
uv run ruff check src tests scripts            # lint
uv run ruff check --fix src tests scripts      # lint + autofix

uv run delta-exchange-mcp                      # stdio (the only transport)

uv run python scripts/smoke.py                 # live smoke against DELTA_MCP_ENV

bash scripts/inspect.sh --cli --method tools/list
bash scripts/inspect.sh --cli --method tools/call --tool-name get_ticker --tool-arg symbol=BTCUSD
bash scripts/inspect.sh                                                          # Inspector web UI on :6274
```

**Rebuilding the editable install after changing `pyproject.toml` or entry points**: `uv sync` again — `uv run` caches the build.

## Architecture

### Tool registration pattern

Each tool module exposes `register(mcp: MCPServer, client: DeltaClient) -> None` that attaches `@mcp.tool()`-decorated closures. `server.py::build_server()` instantiates `DeltaClient` once and passes it into every `register` call. **To add a tool group**: create `src/delta_exchange_mcp/tools/<group>.py` with a `register(mcp, client)`, then call it from `build_server`.

`market.register` always runs; `account.register` starts only when `cfg.has_credentials` is true (both `DELTA_API_KEY` **and** `DELTA_API_SECRET` set), and reconciliation can add or remove that whole manifest later.

### Bringing the account surface up without a restart

A credential saved through the in-chat form arrives in the running process, so `build_server` closes over an `activate(session)` callback and hands it to `form.register`. Every tool closure holds the same rebindable `DeltaClient`: reconciliation swaps one immutable `{config, signing path, http client}` state so market and account calls move to a new environment or credential pair together. It then adds or removes the complete account-tool manifest, disarms trading before any identity change, and sends `session.send_tool_list_changed()` whenever the surface changes. `activate` returns the account and trading surfaces that are actually live, which is what drives the form's carry-on/restart copy.

Three things here are load-bearing:

- **The capability has to be declared.** `serve()` runs stdio with `initialization_options(mcp)`, which passes `NotificationOptions(tools_changed=True)`. The SDK's own `run_stdio_async` leaves every flag off, so the server would advertise `tools.listChanged: false` and a client would never re-read the tool list — the notification would be silently useless. `main` therefore calls `anyio.run(serve, mcp)`, **not** `mcp.run()`. Regression test: `test_the_server_declares_that_its_tool_list_can_change`.
- **Trade mode is never armed by a form save.** `activate` hot-applies reads and the safe trade→read direction, but read→trade waits for a new session's first `tools/list`. Regression tests: `test_trade_mode_still_waits_for_a_restart`, `test_mode_only_read_disarms_live_trading_immediately`.
- **Rotation and environment changes are coherent hot changes.** The shared client swaps its entire request identity before the next call; an in-flight request keeps the state it captured. If trading was live, its tools are removed before that swap and require a restart to re-arm. Regression tests: `test_a_rotated_key_signs_the_next_account_request`, `test_the_first_save_rebinds_market_and_account_tools_to_one_environment`, `test_external_identity_drift_disarms_trading_before_hot_rebind`.
- **Runtime transitions are observable without secrets.** Startup and each trade arm/disarm write one structured line to stderr with environment, live mode, registered surface, and audit path. Never add keys, secrets, signatures, or credential fingerprints to this output.

`get_connection_status` is registered unconditionally, reconciles safe external file changes, and reports `{environment, credentials_configured, account_tools_available, mode, mode_after_restart, restart_required, overridden_by_client, client_name, client_version, mode_setting, client_identity, version, view_build}` — never a key, secret, or fingerprint. `client_name` is self-reported convenience scope, explicitly not authenticated identity. `client_version` is the build behind that name, and it is the field that makes a "the form did not render" report actionable — one client name spans versions that differ in whether they render an MCP App at all. `view_build` is `form.build_id()`, a 10-character SHA-256 prefix of the exact `VIEW_HTML` bytes the process would serve. It exists because `version` is identical on every commit of a branch and so cannot distinguish a client that fetched from one that reused a cached build — a question that cost several round trips of reading package caches, and once cost them against the wrong machine entirely. Ask the assistant for the connection status and compare `view_build` against `uv run python -c "from delta_exchange_mcp import form; print(form.build_id())"` before drawing any conclusion from how a rendered form looks. It exists because the save tools are hidden from the model, so after a save the model cannot see whether it worked; without this it has no way to answer "am I connected?".

`MCPServer` is constructed with `instructions=INSTRUCTIONS`, which is the only channel that reaches the model when no key is configured — there is no account tool then to carry a hint on its own description.

### DeltaClient — single point for HTTP concerns

`src/delta_exchange_mcp/client.py` centralizes the cross-cutting behaviors every tool depends on. Read this file before touching any tool logic:

1. **None-param stripping** — `filtered_params` is computed once and fed to **both** the signing payload (`query_str`) and `httpx.request(params=...)`. Delta's API rejects `?expiry=` as "invalid date"; this is why the same filter applies in two places. Regression test: `test_none_params_are_stripped_before_send`.
2. **Retry policy** — 429 backs off using the `X-RATE-LIMIT-RESET` header (ms); 5xx uses exponential backoff. Only retries GET; POST/PUT/DELETE never auto-retry.
3. **Error-envelope unwrapping** — `{success: false, error: {code, context}}` is raised as `DeltaApiError` (see `errors.py`). `errors.py` carries a hint table for documented auth codes (`SignatureExpired`, `InvalidApiKey`, `UnauthorizedApiAccess`, `ip_not_whitelisted_for_api_key`, `Signature Mismatch`) and extracts the request IP from the error context for the IP-whitelist case.
4. **HMAC-SHA256 signing** — `sign()` concatenates `method + timestamp + path + query + body`. The signing path **must include the `/v2` prefix** per Delta's spec; the client derives it once from `urlparse(base_url).path` and prepends it before calling `sign()`. Don't pass `path="/v2/..."` from callers — they pass relative paths like `/orders`, the client adds the prefix.
5. **Body signing (POST/PUT/DELETE)** — the signed `body` must be the **exact bytes sent on the wire**. `_request` serializes `json_body` once with `json.dumps(..., separators=(",", ":"))`, signs that string, and sends the **same** string via `httpx.request(content=...)`. Do **not** switch back to `json=json_body` — httpx would re-serialize with different spacing and the signature would mismatch. Same "compute once, feed both" rule as None-param stripping (#1). Regression test: `test_place_order_signs_exact_body_bytes`. Convenience methods: `post()` / `put()` / `delete()`.
6. **User-Agent header is required by Delta** — a missing one returns 403. Do not remove it.
7. **Hot rebind is one atomic state swap** — `_request` captures `_ClientState` before its first await, and `rebind()` replaces the config, signing prefix, and transport together. A retired transport closes when its final active request or pinned operation exits, with server shutdown as the backstop. Every dispatched trading tool also enters `client.pin()`: helpers such as tick-size lookup may await before the mutation, and every request in that one tool call must stay on the account that was armed when it began. `TradeGate` is the separate permission lease: it is bound to the entitled connection and checked on every `tools/call` because the SDK's tool registry is process-global; disarming invalidates its generation, and `_finish` fails closed if a mutation was still in preflight.

### Auth surface registration

`tools/account.py` exposes the authenticated read-only tools (positions / margined-positions / wallet-balances / wallet-transactions / fills / bulk-fills-export / open-orders / order-history / order-by-id / product-leverage / trading-stats / trading-preferences / profile). All call `client.get(..., auth=True)`.

`server.build_server()` registers them only when both creds are present. Without creds, the server runs in pure-public mode — same behaviour as before this surface existed.

### Credential entry

Three front-ends fill one file, `~/.delta-exchange-mcp/config.env`:

- `store.py` owns the file — `path/read/ensure/write/insecure_permissions`. `ensure` creates it `0600` from a commented `TEMPLATE` on first run; `write` goes through dotenv's `set_key` so comments and unrelated settings survive. `config.setting(name)` resolves the process environment first and this file second, with empty meaning unanswered (a bundle substitutes every declared variable whether or not the field was filled).
- `credentials.py` is the shared domain: `check(env, key, secret)` makes one `/profile` call, and `save(...)` writes the key, secret and environment together. Neither front-end owns these. `Check.code` carries Delta's own error code beside the rendered message so a caller can branch on which failure it was without matching on that message's text.
- `store.write()` holds an OS-backed advisory lock around its complete copy-modify-replace transaction. This matters now that different clients can save disjoint scoped mode keys: without serialization, a stale staging copy can undo another client's successful trade-to-read de-escalation. The hidden lock file is persistent by design and contains no settings; the kernel releases its lock after a crash.
- `credentials.overridden_by_client()` names the settings in the shared file that the process environment is beating, and both front-ends report it — `login` as a note on stderr, the form as an `overridden` status. Without it a save is silently useless: `config` resolves the environment first, so a client passing its own key (the bundle's `user_config`, VS Code's `inputs`, an edited Cursor entry) wins on every launch, and the form would verify one account, name it, and leave the server signing with another. **It compares what `config` resolves against what the file holds, never mere presence** — the Cursor install link sets `DELTA_MCP_ENV` for everyone, so a presence test would tell every Cursor user their working key was ignored. Regression test: `test_a_client_pinning_the_same_environment_is_not_reported`.
- `login.py` is the terminal front-end. It refuses a non-TTY stdin on purpose — `getpass` reads a pipe rather than rejecting it, so `echo $KEY | ... login` would put the secret in shell history.
- `form.py` is the in-chat front-end, an **MCP App** (SEP-1865): a `ui://` HTML resource with mime `text/html;profile=mcp-app`, opened by `setup_credentials` via `_meta.ui.resourceUri`, submitting to the app-only `save_credentials` or mode-only `save_mode`. Opening issues a random ten-minute, one-use grant in tool-result `_meta`; it is bound to the exact protocol session and never appears in model-visible content or structured content. Invalid input releases it for correction, a durable write consumes it before notification, and a new session cannot inherit it. This is defence in depth for a host that mistakenly exposes an app-only schema, not authenticated user presence. Its `register(mcp)` takes no `DeltaClient` — `credentials.check` builds its own from the candidate key. Three constraints were established empirically against Claude Desktop and Codex desktop and must not regress: **inline every asset** (both hosts' CSP blocks external fetches, and one CDN reference blanks the frame); **complete the `ui/initialize` → `ui/notifications/initialized` handshake** or the frame stays collapsed; and **never feature-test on the `io.modelcontextprotocol/ui` capability** — Claude Desktop 1.0.0 rendered these views without advertising it, and while build 1.30096.5 does advertise it (read 2026-08-16), gating would still turn the form off for anyone on the older build and buys nothing either way. The view must never call `ui/message` or `ui/update-model-context`, which would hand the typed credential to the model. Regression tests: `tests/test_form.py`, `test_a_grant_from_a_closed_session_cannot_be_used_by_a_new_one`.

### The view's own three rules

Read `src/spec.types.ts` in `modelcontextprotocol/ext-apps` before changing any of these; each was wrong once.

- **Never report `document.documentElement.scrollHeight` as the height.** In an iframe it never returns less than the frame it is measured in, so it echoes back the current size and the frame can only grow. Measured: at frame heights 200 / 560 / 1200 / 2000 it reports 535 / 560 / 1200 / 2000 for content that is really 535. One long rejection message would leave the frame tall for the rest of the conversation. `resize()` therefore sets `height: max-content` on the root, reads `getBoundingClientRect().height`, and restores.
- **`preferredSize` is not a field.** It does not appear anywhere in the spec types; the height is whatever the view reports. `prefersBorder` *is* a field, and omitting it is what produced two nested boxes — the host drew one and the view drew another inside it. The resource asks for `prefersBorder: True` and the view draws no border, which also degrades correctly on a host that draws nothing, because the input fills carry the structure.
- **`prefersBorder` says nothing about padding, and no field does.** Observed in Codex: it draws the border and insets the frame by zero, so the view's text sat against the line, nearer to it than Codex's own tool label. Claude Desktop does inset, which is why the earlier "the host draws the box, so the view draws nothing" reasoning looked right and was only half right. The view now pads itself with `var(--gap)` on all four sides; on a host that already insets, the content is inset twice, which is loose but not broken — the cheaper of the two failures. **That padding must stay on the `.pad` wrapper and not move to `body`.** On `body` it did not visibly take effect in Codex, and `body` is the one element every injected reset names, so a host stylesheet we cannot see is the likely cause; a wrapper class no reset can name is immune either way. `host.html`'s `chrome=tight` mode reproduces the Codex case (border, no inset) and `chrome=host` the Claude Desktop one. The padding is inside the `max-content` measurement, so the reported height still lands with 0px dead space.
- **The `ui/initialize` result is not empty.** It carries `hostContext` with the active `theme`, a `styles.variables` palette **and** `styles.css.fonts` — the host's own `@font-face` rules, which the spec makes the *app's* job to install. It is refreshed later by a `ui/notifications/host-context-changed` notification, which has a method and no id and so matches neither branch of a listener written only for replies and host-initiated requests. All three are handled now. Dropping the font rules is the quiet failure: `--font-sans` then names a family the frame never loaded, the view renders in a substituted face, and the height it measures is measured against that face. Regression test: `test_the_view_installs_the_font_rules_the_host_hands_it`.

Colour is split deliberately: surfaces, text and borders prefer the host's tokens so the form sits inside the client's theme, while brand and semantic colours are always Delta's own (`--brand-india-*`, `--positive-*`, `--negative-*` from delta.exchange). The logo is the official mark inlined as `<svg>` with its one gradient flattened, because the test bans the syntax a gradient reference needs. Delta's Aileron typeface cannot come across — it is a web font, and fetching it hits the policy that blanks the frame.

**Type size is the host's, never the view's.** The stylesheet names no pixel type size and no width. Type comes from the `--font-text-*` / `--font-heading-*` / `--font-weight-*` tokens, and the two spacing steps — `--gap: 1em` between questions, `--gap-tight: .35em` binding a label to its control and a control to its note — are in em, so both track whatever type the host asked for. An earlier version hardcoded `14px`/`13px` type and a 3/4/6/9/12/14/16px spacing ladder tuned against that base, and that is what looked wrong in Codex, which does not run a 14px base. Measured with the host's own tokens applied: identical content reported 437px at the 16px default and 381px at a host-supplied 14px, with no rule re-tuned. Regression test: `test_the_view_names_no_type_size_of_its_own` fails on a px literal in any font declaration. Note that `font: inherit` on a control drags the body's prose line-height in with it, which leaves a single-line field standing a third taller than its text — the controls reset `line-height` immediately after, and it has to stay after or the shorthand wins.

**Spacing lives on a `.field` wrapper, one per question, not on the controls.** Putting it on the controls cannot express the mode note, which is empty most of the time: a bottom margin on the select leaves a hole when there is no note and doubles the gap when there is one.

**The controls are native, but not bare.** They keep the platform's keyboard behaviour and, for the select, a menu drawn outside the frame where the app's bounds cannot clip it. What is set on them is only what makes them belong to this form: padding in em, and colour, border and radius from `--color-background-secondary` / `--color-border-primary` / `--border-radius-md` / `--border-width-regular`, so a client's own field styling carries through. Fallbacks are `color-mix()` against the `canvas`/`canvastext` system colours rather than literals, which means they follow `color-scheme` — verified in both light and dark against a host sending no palette at all. The one pair that genuinely needs a value per scheme is `--positive` / `--negative`, which use `light-dark()` with Delta's own light and dark tones. Everything else that overrides anything is deliberate and short: the brand-filled button (a native one stops reading as the primary action), `accent-color` on the radios and checkbox, a focus ring at `outline-offset: 0` on fields so it hugs their existing border, and the two flex rows that are structural — logo beside title, checkbox opposite link.

`save_credentials` returns `account`, `path`, `next_step`, `effective_mode`, `client_name`, and `mode_setting` as fields alongside `message` on a clean save, because the view renders its own connected state from them rather than printing the sentence. `save_mode` changes only the scoped mode and never reads or rewrites stored credentials. `message` stays for clients that show no view.

Those `_meta` arguments need `meta=` on both the tool and the resource decorator, which 2.x carries on `MCPServer`.

### Trading surface (mutations)

`tools/trading.py` exposes the authenticated write tools (place/edit/cancel order, cancel-all, place/edit/cancel batch, place/edit bracket, set-leverage, change-margin, close-all, auto-topup). Its `register(mcp, client, audit)` is gated on `(cfg.has_credentials and cfg.mode == "trade")` in `build_server`; `DELTA_MCP_MODE` defaults to `read`, so the surface is off unless explicitly opted into.

### Trading is enabled per client, and that is load-bearing

`DELTA_MCP_MODE` is read **only from the process environment**, never from the shared file, because every MCP client on the machine reads that file and one value in it would arm order placement in all of them. The in-chat form can still turn trading on, because it writes a *scoped* name instead: `config.mode_key(client)` produces `DELTA_MCP_MODE_<READABLE>_<DIGEST>` from the exact name the client gave in the MCP handshake. The readable ASCII slug is only a label; a truncated SHA-256 digest is the collision-resistant binding, so punctuation variants cannot inherit one another's choice. This is convenience scope, not authentication: a client can still claim the same exact name. `config.mode_for_client(name)` resolves the process environment first and that scoped key second.

`request.client(session)` is the one reader of that handshake identity, returning `name`, `title` and `version`. Read it there rather than reaching into `session.client_params`, which is what both call sites used to do separately. Two properties of it are load-bearing. **An empty name never occurs over a real connection**: the SDK substitutes `DEFAULT_CLIENT_INFO` (`mcp/0.1.0`) for a client that sends no identity, so every such client scopes its mode under the single name `mcp` and shares that entitlement with the others — consistent with the convenience-scope model, but not what the empty-name guard alone suggests. Only a call with no session, as the tests make, yields an empty name. **`title` is never used as a key and never sent anywhere**, because a host may let the person edit it. Regression tests: `test_the_status_tool_reports_the_client_that_asked`, `test_a_client_that_names_itself_nothing_still_arrives_named`.

A client only identifies itself during the handshake, which happens after `build_server` has finished assembling the tool list, so the entitlement is applied at the **first `tools/list` of a session**. `DeltaMCP` does that from a `ServerMiddleware` the SDK is constructed with; do not mutate its private request-handler table. `MCPServer.list_tools()` takes no context, so an override there cannot see who is asking. The mutating tools therefore appear in that first listing rather than behind a later notification. It is decided once per session on purpose: choosing trade in the form writes the key but must not arm order placement in the session that asked for it. Regression tests: `test_trading_arms_only_for_the_client_it_was_enabled_for`, `test_choosing_trade_does_not_arm_it_in_the_session_that_chose_it`, `test_a_client_env_var_still_outranks_the_scoped_setting`.

A `ServerSession` is built **per request**, so it is not the identity of a client. Three things have to outlive one call — the entitlement decided once per client, the trade lease, and the form's one-use grant — and all three key on `request.peer(session)`, the connection behind the session. `request.session` is the contextvar the middleware publishes for the trade gate, which cannot declare a `Context` parameter for the tool functions it wraps. Reaching the connection reads a private attribute, because the SDK's public `connection` accessor hangs off a context class its runner does not build yet. The fallback fails closed: a per-request identity refuses a lease rather than sharing one. Regression tests: `test_another_session_cannot_call_a_globally_registered_trade_tool`, `test_a_grant_from_a_closed_session_cannot_be_used_by_a_new_one`.

`get_connection_status` reports `mode` (live now) and `mode_after_restart` (what this client is entitled to), and folds the difference into `restart_required` — otherwise it would report nothing outstanding while trading was still waiting, which is the contradiction the field exists to prevent.

Conventions in `trading.py`:
- Register every mutation through the shared `@mutation_tool` decorator. It publishes
  `_meta["delta.exchange/mutating"] = true`, which the bundle verifier uses instead of
  inferring safety from tool-name prefixes.
- Every mutating tool takes `dry_run: bool`. The shared `_finish(tool, method, path, payload, dry_run)` helper strips `None` keys, and when `dry_run` returns `{dry_run, method, path, payload}` **without** any HTTP call; otherwise it sends via `client.post/put/delete` and records to the audit log on both success and `DeltaApiError`.
- Order-level boolean flags (`post_only`, `reduce_only`, `cancel_*`) are Delta **string enums** — convert with `_bs()` to `"true"`/`"false"`. Position-level flags (`auto_topup`, `close_all_*`) are real JSON booleans.
- `close_all_positions` needs `user_id`; it is auto-resolved from `/profile` once and cached per-process in the `register` closure — never a tool param.
- Batch tools cap at `_MAX_BATCH = 50`.

### Audit logging

`audit_log.py` exposes `configure(cfg) -> AuditLog | None` (returns `None` unless `mode == "trade"`; `DELTA_MCP_AUDIT=off|false|0|no` is a kill switch). `AuditLog.record(...)` appends one JSON line per mutation to `~/.delta-exchange-mcp/audit/audit-<ts>-<pid>.log`, created `0600`. **Invariant: no credentials** — only the request body (which carries none) and a summarized result are recorded. `configure` caches a single `_INSTANCE` per process so `build_server` and `main`'s banner share one file. `server.py` registers `get_trading_status` (trade mode only) to report `{mode, audit_log_path}`. Regression test: `test_audit_records_success_and_error_without_secrets`.

### Debug logging

`debug_log.py` exposes `configure(cfg) -> Path | None`, called from `build_server`. When
`DELTA_MCP_DEBUG` is truthy it attaches a `FileHandler` to the `delta_exchange_mcp` and `httpx`
loggers (INFO, `propagate=False`, **never** `logging.basicConfig`) so request URLs + response
bodies land in `~/.delta-exchange-mcp/logs/debug-<ts>-<pid>.log`. `client.py` emits the `→`/`←`/`✗`
lines. **Invariant: credentials (api-key / api_secret / signature / timestamp) are never logged** —
only headers carry them and we never log the headers dict. Regression test:
`test_logs_request_and_body_but_no_secrets`. The module is deliberately **not** named `logging.py`
(would shadow the stdlib `logging` import). `server.py` registers a `get_debug_status` tool (only
when debug is on) so the assistant can report the log path; the path is also in the stderr startup
banner.

### Environment naming

`DELTA_MCP_ENV` values are `india_prod` / `india_testnet` (not `mainnet`/`testnet`) to match Delta's own URL naming (`api.india.delta.exchange`, `cdn-ind.testnet.deltaex.org`). `india_prod` is the default — users ask "what's BTCUSD mid", they mean prod, not testnet.

API keys are env-scoped on Delta's side: prod keys created at delta.exchange only work against `india_prod`; demo keys at demo.delta.exchange only work against `india_testnet`. Mismatch → `InvalidApiKey`.

`DELTA_MCP_MODE` is `read` (default) or `trade`; only `trade` registers `tools/trading.py`. `DELTA_MCP_AUDIT` (kill switch) and `DELTA_MCP_AUDIT_FILE` (path override) govern the audit log.

## Reference — Delta Exchange API

The upstream source of truth for endpoint shapes is the **Slate docs repo at `/Users/anuj/Documents/work/Delta/slate`**, specifically `swagger_v2.json` and `source/includes/_*.md`. When adding or fixing a tool:

```bash
jq '.paths["/products"].get.parameters' /Users/anuj/Documents/work/Delta/slate/swagger_v2.json
```

Auth spec lives at `source/includes/_authentication.md` (signing payload format, ±5 sec timestamp window, documented error codes).

## Distribution

**Local stdio only.** Each user runs the server as a subprocess of their MCP client via `uvx`:

```bash
uvx delta-exchange-mcp
```

There is intentionally **no HTTP transport, no Docker image, and no shared hosted endpoint**. Per-user API keys can't safely route through a shared HTTP server, and the financial-tool nature of this MCP means users should be able to read the code that runs against their account. If you find yourself adding `streamable-http`, `transport=` flags, or a `Dockerfile`, stop and discuss first.

## Tests

`respx` mocks httpx for unit tests (no live network). Live verification happens through `scripts/smoke.py` (Python-level) and `scripts/inspect.sh --cli` (MCP-protocol-level) — both hit real testnet/prod and are run manually, not in CI. When fixing a bug surfaced by live use, add a `respx` regression test (see `test_none_params_are_stripped_before_send` and `test_signing_payload_includes_v2_prefix` for the pattern).
