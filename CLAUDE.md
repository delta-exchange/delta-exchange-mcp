# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project in one line

FastMCP server (stdio only) that wraps Delta Exchange India's REST API as MCP tools — public market data unconditionally, authenticated read-only account tools when `DELTA_API_KEY`/`DELTA_API_SECRET` are set, plus authenticated trading mutations when `DELTA_MCP_MODE=trade` is also set.

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

Each tool module exposes `register(mcp: FastMCP, client: DeltaClient) -> None` that attaches `@mcp.tool()`-decorated closures. `server.py::build_server()` instantiates `DeltaClient` once and passes it into every `register` call. **To add a tool group**: create `src/delta_exchange_mcp/tools/<group>.py` with a `register(mcp, client)`, then call it from `build_server`.

`market.register` always runs; `account.register` runs only when `cfg.has_credentials` is true (both `DELTA_API_KEY` **and** `DELTA_API_SECRET` set).

### DeltaClient — single point for HTTP concerns

`src/delta_exchange_mcp/client.py` centralizes the cross-cutting behaviors every tool depends on. Read this file before touching any tool logic:

1. **None-param stripping** — `filtered_params` is computed once and fed to **both** the signing payload (`query_str`) and `httpx.request(params=...)`. Delta's API rejects `?expiry=` as "invalid date"; this is why the same filter applies in two places. Regression test: `test_none_params_are_stripped_before_send`.
2. **Retry policy** — 429 backs off using the `X-RATE-LIMIT-RESET` header (ms); 5xx uses exponential backoff. Only retries GET; POST/PUT/DELETE never auto-retry.
3. **Error-envelope unwrapping** — `{success: false, error: {code, context}}` is raised as `DeltaApiError` (see `errors.py`). `errors.py` carries a hint table for documented auth codes (`SignatureExpired`, `InvalidApiKey`, `UnauthorizedApiAccess`, `ip_not_whitelisted_for_api_key`, `Signature Mismatch`) and extracts the request IP from the error context for the IP-whitelist case.
4. **HMAC-SHA256 signing** — `sign()` concatenates `method + timestamp + path + query + body`. The signing path **must include the `/v2` prefix** per Delta's spec; the client derives it once from `urlparse(base_url).path` and prepends it before calling `sign()`. Don't pass `path="/v2/..."` from callers — they pass relative paths like `/orders`, the client adds the prefix.
5. **Body signing (POST/PUT/DELETE)** — the signed `body` must be the **exact bytes sent on the wire**. `_request` serializes `json_body` once with `json.dumps(..., separators=(",", ":"))`, signs that string, and sends the **same** string via `httpx.request(content=...)`. Do **not** switch back to `json=json_body` — httpx would re-serialize with different spacing and the signature would mismatch. Same "compute once, feed both" rule as None-param stripping (#1). Regression test: `test_place_order_signs_exact_body_bytes`. Convenience methods: `post()` / `put()` / `delete()`.
6. **User-Agent header is required by Delta** — a missing one returns 403. Do not remove it.

### Auth surface registration

`tools/account.py` exposes the authenticated read-only tools (positions / margined-positions / wallet-balances / wallet-transactions / fills / bulk-fills-export / open-orders / order-history / order-by-id / product-leverage / trading-stats / trading-preferences / profile). All call `client.get(..., auth=True)`.

`server.build_server()` registers them only when both creds are present. Without creds, the server runs in pure-public mode — same behaviour as before this surface existed.

### Trading surface (mutations)

`tools/trading.py` exposes the authenticated write tools (place/edit/cancel order, cancel-all, place/edit/cancel batch, place/edit bracket, set-leverage, change-margin, close-all, auto-topup). Its `register(mcp, client, audit)` is gated on `(cfg.has_credentials and cfg.mode == "trade")` in `build_server`; `DELTA_MCP_MODE` defaults to `read`, so the surface is off unless explicitly opted into.

Conventions in `trading.py`:
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

## Evals (tool-selection quality)

`evals/` scores whether the tool names/descriptions/schemas lead a real LLM agent to pick the right tool with the right args — run it when renaming tools or rewriting docstrings, **never in CI** (costs Anthropic tokens):

```bash
uv sync --group evals
uv run --group evals python -m evals.run --list
uv run --group evals python -m evals.run --case ticker_basic --no-judge   # cheap smoke
uv run --group evals python -m evals.run                                  # full run
```

Deterministic expect/forbid asserts in `evals/dataset.py` are the gate; DeepEval LLM-judge scores (`MCPUseMetric` etc.) are advisory. The harness (`evals/agent.py`) spawns the server over stdio, refuses `india_prod`, and **forces `dry_run=True` on every mutating call** at the `call_tool` boundary — recorded args are the model's own so asserts and judge score its intent. DeepEval 4.x has undocumented shape requirements (`MCPToolCall.result` must be a real `CallToolResult`, one assistant `Turn` per tool call, `structuredContent={"result": ...}`); those workarounds live in `evals/scoring.py`, keep all deepeval imports there.
