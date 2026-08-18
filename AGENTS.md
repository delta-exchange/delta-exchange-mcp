# AGENTS.md

Guidance for any coding agent working in this repository. `CLAUDE.md` is a symlink to this
file, so Claude Code and the tools that read `AGENTS.md` get the same content and it cannot
drift into two versions.

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

These are load-bearing:

- **The capability has to be declared.** `serve()` runs stdio with `initialization_options(mcp)`, which passes `NotificationOptions(tools_changed=True)`. The SDK's own `run_stdio_async` leaves every flag off, so the server would advertise `tools.listChanged: false` and a client would never re-read the tool list — the notification would be silently useless. `main` therefore calls `anyio.run(serve, mcp)`, **not** `mcp.run()`. Regression test: `test_the_server_declares_that_its_tool_list_can_change`.
- **Trade mode is never armed by a form save.** `activate` hot-applies reads and the safe trade→read direction, but read→trade waits for a new session's first `tools/list`. Regression tests: `test_trade_mode_still_waits_for_a_restart`, `test_mode_only_read_disarms_live_trading_immediately`.
- **Rotation and environment changes are coherent hot changes.** The shared client swaps its entire request identity before the next call; an in-flight request keeps the state it captured. If trading was live, its tools are removed before that swap and require a restart to re-arm. Regression tests: `test_a_rotated_key_signs_the_next_account_request`, `test_the_first_save_rebinds_market_and_account_tools_to_one_environment`, `test_external_identity_drift_disarms_trading_before_hot_rebind`.
- **Trading is re-checked before every order, not only at the start.** `reconcile` used to run at a session's first `tools/list`, on `get_connection_status`, and on a form save. Anything that changed the settings by another route stayed invisible for the rest of the session, so trading turned off through the browser page, by a hand edit, or by a second client left order placement armed until a restart. `DeltaMCP.before_mutation` now runs the same reconciliation before any tool in `trading.TOOL_NAMES` executes, which refuses at the point of use rather than at the point of the save. Two conditions on it are load-bearing. It passes `allow_trade=False`, so catching up can only stop mutations and never arm them. It runs **only for the connection the trade gate is bound to**, because `reconcile` resolves the mode of whichever client is asking — running it for any other caller would let one client's settings tear down another client's live entitlement, and that caller is already refused by the gate with a message that explains why. Regression tests: `test_turning_trading_off_anywhere_stops_the_next_order`, `test_catching_up_before_a_mutation_never_arms_trading`, `test_another_session_cannot_call_a_globally_registered_trade_tool`.
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
- `credentials.overridden_by_client()` names the settings in the shared file that the process environment is beating, and both front-ends report it — `login` as a note on stderr, the form as an `overridden` status. Without it a save is silently useless: `config` resolves the environment first, so a client passing its own key (the bundle's `user_config`, VS Code's `inputs`, an edited Cursor entry) wins on every launch, and the form would verify one account, name it, and leave the server signing with another. **It asks whether the process environment supplies a value that differs from what the file holds** — presence alone is not enough, because the Cursor install link sets `DELTA_MCP_ENV` for everyone and a presence test would tell every Cursor user their working key was ignored. Two things follow that are easy to get wrong. **An empty file is not an exemption**: the first save is exactly when the file holds nothing, so a client's own key has to lock the field then too, and comparing the file against the resolved value misses precisely that case. **The key and the secret lock together**, because `config._credentials` reads both from whichever source holds either — a client naming only `DELTA_API_KEY` also decides the secret, and the secret it decides is nothing. Regression tests: `test_a_client_pinning_the_same_environment_is_not_reported`, `test_a_client_key_is_reported_even_when_the_file_holds_none`, `test_a_client_supplying_only_the_key_locks_the_secret_too`.
- `login.py` is the terminal front-end. It refuses a non-TTY stdin on purpose — `getpass` reads a pipe rather than rejecting it, so `echo $KEY | ... login` would put the secret in shell history.
- `form.py` is the in-chat front-end, an **MCP App** (SEP-1865): a `ui://` HTML resource with mime `text/html;profile=mcp-app`, opened by `setup_credentials` via `_meta.ui.resourceUri`, submitting to the app-only `save_credentials` or mode-only `save_mode`. Opening issues a random ten-minute, one-use grant in tool-result `_meta`; it is bound to the exact protocol session and never appears in model-visible content or structured content. Invalid input releases it for correction, a durable write consumes it before notification, and a new session cannot inherit it. This is defence in depth for a host that mistakenly exposes an app-only schema, not authenticated user presence. Its `register(mcp)` takes no `DeltaClient` — `credentials.check` builds its own from the candidate key. Three constraints were established empirically against Claude Desktop and Codex desktop and must not regress: **inline every asset** (both hosts' CSP blocks external fetches, and one CDN reference blanks the frame); **complete the `ui/initialize` → `ui/notifications/initialized` handshake** or the frame stays collapsed; and **never feature-test on the `io.modelcontextprotocol/ui` capability** — Claude Desktop 1.0.0 rendered these views without advertising it, and while build 1.30096.5 does advertise it (read 2026-08-16), gating would still turn the form off for anyone on the older build and buys nothing either way. The view must never call `ui/message` or `ui/update-model-context`, which would hand the typed credential to the model. Regression tests: `tests/test_form.py`, `test_a_grant_from_a_closed_session_cannot_be_used_by_a_new_one`.

### The view

Its constraints are recorded where they apply rather than here: the module docstring in
`src/delta_exchange_mcp/form.py` for the ones that decide whether it renders at all, and an
inline comment on each rule that was wrong once. Most were measured against a real host and
cost a round of guessing each, so read them before changing the stylesheet.

Three things to know before opening it. **`src/spec.types.ts` in
`modelcontextprotocol/ext-apps` is the only authority on which `_meta.ui` fields exist** —
inventing one that reads plausibly is how `preferredSize` came to be declared while doing
nothing. **The height has a ceiling of 500px and roughly 5px spare**, so anything added
takes its room from something already there. And **the view never names a type size or a
width of its own**; both come from the host's tokens, and a px literal in a font
declaration fails `test_the_view_names_no_type_size_of_its_own`.

To see and measure it:

```bash
uv run python scripts/host.py --open
```

That frames the view in a stand-in host, answers the `ui/initialize` handshake with a
palette and the host's font rules, and reports the height the view asks for against the
ceiling. `chrome=tight` reproduces Codex, which draws a border and insets by nothing;
`chrome=host` reproduces Claude Desktop, which insets. The touch control injects the view's
own coarse-pointer rules, which is the only way to see that layout, because
`(pointer: coarse)` answers to the device rather than to the page — and that layout is the
one at risk, since larger targets are what make it taller.

`save_credentials` returns `account`, `path`, `next_step`, `effective_mode`, `client_name`, and `mode_setting` as fields alongside `message` on a clean save, because the view renders its own connected state from them rather than printing the sentence. `save_mode` changes only the scoped mode and never reads or rewrites stored credentials. `message` stays for clients that show no view.

Those `_meta` arguments need `meta=` on both the tool and the resource decorator, which 2.x carries on `MCPServer`.

### Trading surface (mutations)

`tools/trading.py` exposes the authenticated write tools (place/edit/cancel order, cancel-all, place/edit/cancel batch, place/edit bracket, set-leverage, change-margin, close-all, auto-topup). Its `register(mcp, client, audit)` is gated on `(cfg.has_credentials and cfg.mode == "trade")` in `build_server`; `DELTA_MCP_MODE` defaults to `read`, so the surface is off unless explicitly opted into.

### Trading is enabled per client, and that is load-bearing

`DELTA_MCP_MODE` is read **only from the process environment**, never from the shared file, because every MCP client on the machine reads that file and one value in it would arm order placement in all of them. The in-chat form can still turn trading on, because it writes a *scoped* name instead: `config.mode_key(client)` produces `DELTA_MCP_MODE_<READABLE>_<DIGEST>` from the exact name the client gave in the MCP handshake. The readable ASCII slug is only a label; a truncated SHA-256 digest is the collision-resistant binding, so punctuation variants cannot inherit one another's choice. This is convenience scope, not authentication: a client can still claim the same exact name. `config.mode_for_client(name)` resolves the process environment first and that scoped key second.

`request.client(session)` is the one reader of that handshake identity, returning `name`, `title` and `version`. Read it there rather than reaching into `session.client_params`, which is what both call sites used to do separately. Two properties of it are load-bearing. **An empty name never occurs over a real connection**: the SDK substitutes `DEFAULT_CLIENT_INFO` (`mcp/0.1.0`) for a client that sends no identity, so every such client scopes its mode under the single name `mcp` and shares that entitlement with the others — consistent with the convenience-scope model, but not what the empty-name guard alone suggests. Only a call with no session, as the tests make, yields an empty name. **`title` is never used as a key**, because a host may let the person edit it — two people on the same client would otherwise land in different places, and renaming one would lose whatever was stored under the old name. It *is* forwarded to Delta with the rest of the handshake (see `analytics.py`), so the older "never sent anywhere" no longer holds. **`name` is reported exactly as sent**, proxy suffix included, because which bridge a request came through is worth counting; `config.stable_name` strips that suffix and only for keying a stored setting. Regression tests: `test_the_status_tool_reports_the_client_that_asked`, `test_a_client_that_names_itself_nothing_still_arrives_named`.

**What `analytics.py` forwards is a closed set, decided here — never the handshake as it arrived.** A client's capability map is not a closed type: `experimental` and `extensions` are `dict[str, dict[str, Any]]`, filled by an extension author with whatever that extension needs. Dumping the map sent all of it to Delta on every request, so an extension keeping an API key in its own capability object had that key forwarded intact, alongside this server's own credentials. `_capabilities` now emits presence for the four closed capabilities and a count for the two open maps: no key the client chose, no value. Widening that stays a decision someone makes on purpose, which is why the names are listed in `_CAPABILITY_NAMES` rather than read off the model — a field added to the protocol later must not start forwarding itself. Regression test: `test_no_client_extension_setting_is_ever_forwarded`.

**Bound header fields by encoded bytes, never by input length.** `clean` caps `_FIELD_LIMIT` *after* percent-encoding. One emoji is one character to a slice and twelve once encoded, so bounding the input let a 200-character client name become a 2400-character header; two such fields cleared the whole 4096-byte budget on their own, and nothing sheds a discrete field — only the JSON context gives way. The cut is also moved back off a partial `%XX`, which decodes to nothing. Separately, `encode` and `config.mode_key` both pass a policy to `str.encode` rather than taking the strict default: a client name can carry an unpaired surrogate that UTF-8 refuses, and strict encoding raised inside the header build and inside the trading-mode key, taking down a connection over the client's own label. `mode_key` uses `surrogatepass` rather than `replace` because that digest must stay injective.

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

## Evals (tool-selection quality)

`evals/` scores whether the tool names, descriptions and schemas lead a real LLM agent to
pick the right tool with the right arguments. Run it when renaming a tool or rewriting a
docstring, and **never in CI** — it spends real tokens, roughly $10-15 for a full run.

```bash
uv sync --group evals
uv run --group evals python -m evals.run --list                          # free
uv run --group evals python -m evals.run --case ticker_basic --no-judge  # cheap smoke
uv run --group evals python -m evals.run                                 # full run
```

Deterministic expect/forbid asserts in `evals/dataset.py` are the gate; the DeepEval judge
scores are advisory. The harness in `evals/agent.py` starts the server over stdio, refuses
`india_prod` outright, and **forces `dry_run=True` on every mutating call** at the
`call_tool` boundary — the recorded arguments are the model's own, so the asserts and the
judge score what it intended rather than what was forced.

DeepEval 4.x has undocumented shape requirements (`MCPToolCall.result` must be a real
`CallToolResult`, one assistant `Turn` per tool call, a `{"result": ...}` structured body).
Those workarounds live in `evals/scoring.py`; keep every deepeval import there.

**The harness reads the protocol types, so an SDK rename reaches it.** mcp 2.x renamed the
result fields to snake_case and removed the old spellings for reading — `structured_content`,
`is_error` and `input_schema` now, where 1.x had `structuredContent`, `isError` and
`inputSchema`. Constructing with the old names still works, so this fails only when
something runs. Reading is what breaks, and the harness reads them on every tool call.

**Any model that speaks the Anthropic Messages API works**, not only Anthropic's own
endpoint. The client honours `ANTHROPIC_BASE_URL`, so an OpenRouter key runs the whole
harness against its Anthropic-compatible endpoint, tool use included:

```bash
ANTHROPIC_BASE_URL=https://openrouter.ai/api/v1 \
ANTHROPIC_API_KEY=$OPENROUTER_API_KEY \
uv run --group evals python -m evals.run --case ticker_basic --no-judge \
  --model anthropic/claude-haiku-4.5
```

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

**What that rule is about, and what it is not.** It targets a *shared* server holding *other people's* keys. `setup.py` serves the settings page over HTTP and does not break it: the listener binds the loopback address, exists for one person on their own machine, and closes on the first save or after ten minutes. Nothing is shared, nothing is reachable from the network, and the key still goes only into the file it always went into. The page writes that file and nothing else: it runs on its own thread and never touches the live server's state, which is why the running server re-reads the settings before a mutation instead of the page trying to reach in. The MCP protocol itself is still stdio only — the page is a way to *fill in* settings, not a way to serve tools. Judge a future proposal the same way: ask who else could reach it and whose credentials it would hold, not whether the word HTTP appears.

**One page, one save, and the token does not enforce that.** The URL token answers who may ask, never how often: it stays in the address bar for the page's whole life, so a reload, a duplicated tab and a double-click all present a valid one. `_Save` in `setup.py` holds the rest — a claim taken *before* the call to Delta, because that call is the slow part and therefore the entire race, released when a save never reaches the file, and committed the moment it does. Two saves once ran to completion side by side, each naming back the account for its own key, with only one key reaching the file; the store's own lock kept that file coherent, which is why nothing looked wrong from the outside. Anything added here that writes settings takes the claim too.

## Tests

**Nothing in the suite runs the view's JavaScript.** `tests/test_setup_page.py` drives real
HTTP against a real listener, but it posts hand-built bodies to the endpoint, so the server
half is well covered and the client half is not executed at all. The settings page once
shipped unable to save anything — the save button could never leave its disabled state,
because in a client the save grant arrives as a host notification and on the page there is
no host to send one — and the whole suite passed. Open the page and press the button before
believing it works:

```bash
uv run delta-exchange-mcp setup --no-browser   # prints the address
```

Two structural checks stand in for a browser and are labelled as such in their docstrings.
Neither proves the button works.

`respx` mocks httpx for unit tests (no live network). Live verification happens through `scripts/smoke.py` (Python-level) and `scripts/inspect.sh --cli` (MCP-protocol-level) — both hit real testnet/prod and are run manually, not in CI. When fixing a bug surfaced by live use, add a `respx` regression test (see `test_none_params_are_stripped_before_send` and `test_signing_payload_includes_v2_prefix` for the pattern).
