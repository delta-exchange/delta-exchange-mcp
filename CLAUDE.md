# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project in one line

FastMCP server (stdio only) that wraps Delta Exchange India's REST API as MCP tools — public market data unconditionally, plus authenticated read-only account tools when `DELTA_API_KEY`/`DELTA_API_SECRET` are set.

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
5. **User-Agent header is required by Delta** — a missing one returns 403. Do not remove it.

### Auth surface registration

`tools/account.py` exposes 12 authenticated read-only tools (positions / margined-positions / wallet-balances / wallet-transactions / fills / open-orders / order-history / order-by-id / product-leverage / trading-stats / trading-preferences / profile). All call `client.get(..., auth=True)`.

`server.build_server()` registers them only when both creds are present. Without creds, the server runs in pure-public mode — same behaviour as before this surface existed.

There's no future v2 "trade" gate yet; when that lands, add a `DELTA_MCP_MODE=trade` flag and a `tools/trading.py` register call gated on `(has_credentials and mode == "trade")`. The signer + auth plumbing is already in place.

### Environment naming

`DELTA_MCP_ENV` values are `india_prod` / `india_testnet` (not `mainnet`/`testnet`) to match Delta's own URL naming (`api.india.delta.exchange`, `cdn-ind.testnet.deltaex.org`). `india_prod` is the default — users ask "what's BTCUSD mid", they mean prod, not testnet.

API keys are env-scoped on Delta's side: prod keys created at delta.exchange only work against `india_prod`; demo keys at demo.delta.exchange only work against `india_testnet`. Mismatch → `InvalidApiKey`.

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
