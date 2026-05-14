# delta-exchange-mcp

![Status: Beta](https://img.shields.io/badge/status-beta-orange)
[![PyPI version](https://img.shields.io/pypi/v/delta-exchange-mcp)](https://pypi.org/project/delta-exchange-mcp/)

Official MCP (Model Context Protocol) server for **Delta Exchange India**. Lets AI assistants (Claude Desktop, Claude Code, Cursor, Zed, Codex) query Delta Exchange market data and your own account (read-only) through standardized tools.

> **Status:** Beta. Functional and used internally, but the tool surface and configuration may still change. Please [open an issue](https://github.com/delta-exchange/delta-exchange-mcp/issues) for bugs, missing tools, or rough edges. Early reports directly shape what ships next.

**What you get:** 9 public market-data tools + 12 authenticated read-only account tools (positions, orders, fills, wallet, stats, leverage, preferences, profile). **No mutations.** The server cannot place, edit, or cancel orders.

---

## Quick start

**Prerequisite:** [`uv`](https://docs.astral.sh/uv/getting-started/installation/) installed on your machine.

Sanity-check the install:

```bash
uvx delta-exchange-mcp --help
```

The server runs **local stdio only**: your MCP client launches it as a subprocess, and your API keys never leave your machine. `uvx` resolves the latest published version from PyPI on each launch. To pin a specific version, use `uvx "delta-exchange-mcp==0.1.0"`.

## Install in your MCP client

### Claude Code

```bash
claude mcp add delta-exchange-mcp \
  --scope user \
  --env DELTA_MCP_ENV=india_prod \
  --env DELTA_API_KEY=your-api-key \
  --env DELTA_API_SECRET=your-api-secret \
  -- uvx delta-exchange-mcp
```

`--scope user` makes the server available across all projects. Drop the API-key envs for market-only mode. Verify with `claude mcp list`.

### Codex

Add to `~/.codex/config.toml`:

```toml
[mcp_servers.delta-exchange-mcp]
command = "uvx"
args = ["delta-exchange-mcp"]
env = { DELTA_MCP_ENV = "india_prod", DELTA_API_KEY = "your-api-key", DELTA_API_SECRET = "your-api-secret" }
```

### Claude Desktop / Zed / Windsurf / other clients

Add to your MCP client's config file:

```json
{
  "mcpServers": {
    "delta-exchange": {
      "command": "uvx",
      "args": ["delta-exchange-mcp"],
      "env": {
        "DELTA_MCP_ENV": "india_prod",
        "DELTA_API_KEY": "your-api-key",
        "DELTA_API_SECRET": "your-api-secret"
      }
    }
  }
}
```

`DELTA_API_KEY` / `DELTA_API_SECRET` are **optional**: without them, only the public market tools register.

### Install from source (optional)

To run an unreleased commit or your own fork instead of the PyPI release:

```bash
uvx --from git+https://github.com/delta-exchange/delta-exchange-mcp.git delta-exchange-mcp
```

Append `@<branch-or-sha>` to the URL to pin to a specific commit.

## API keys (optional, for account tools)

1. Create a key at [delta.exchange/app/account/manageapikeys](https://www.delta.exchange/app/account/manageapikeys) (testnet: [demo.delta.exchange](https://demo.delta.exchange/app/account/manageapikeys)).
2. Both `api_key` and `api_secret` are shown **once at creation**. Save the secret immediately; it can't be re-derived.
3. **Read Data** permission is enough. Trading permission is not required and not used.
4. Recommended: whitelist your IP on the key. Delta blocks non-whitelisted IPs and surfaces your current IP in the error message if it fires.
5. **Match the environment**: prod keys with `DELTA_MCP_ENV=india_prod`, demo keys with `DELTA_MCP_ENV=india_testnet`. Mixing them returns `InvalidApiKey`.

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `DELTA_MCP_ENV` | `india_prod` | `india_prod` or `india_testnet`. |
| `DELTA_API_KEY` | _(unset)_ | API key. Optional; when set with `DELTA_API_SECRET`, account tools register. |
| `DELTA_API_SECRET` | _(unset)_ | API secret matching `DELTA_API_KEY`. |

## Tools

### Public market data (always available)

| Tool | What it returns |
|---|---|
| `list_products` / `get_product` | Catalog of tradable instruments and per-product metadata. |
| `list_tickers` / `get_ticker` | Last price, 24h stats, mark price, OI. |
| `get_options_chain` | Option chain snapshot for a given underlying / expiry. |
| `get_orderbook` | Bid/ask depth for a symbol. |
| `get_recent_trades` | Last N public trades. |
| `get_candles` | OHLC candles by resolution. |
| `get_reference_data` | Assets + indices reference. |

### Account read-only (requires `DELTA_API_KEY` + `DELTA_API_SECRET`)

| Tool | What it returns |
|---|---|
| `get_positions` / `get_margined_positions` | Open positions, sizes, entry, unrealized PnL. |
| `get_wallet_balances` / `get_wallet_transactions` | Per-asset balances + ledger. |
| `get_open_orders` / `get_order_history` / `get_order_by_id` | Active and historical orders. |
| `get_fills` | Trade fills (own). |
| `get_product_leverage` | Per-product leverage setting. |
| `get_trading_stats` / `get_trading_preferences` / `get_profile` | Account-level stats, preferences, profile. |

## Example prompts

Once connected, you can ask things like:

- "What's the current BTCUSD mark price and 24h range?"
- "Show the options chain for BTC expiring this Friday."
- "What positions do I have open and what's my total unrealized PnL?"
- "List my fills from the last 24 hours grouped by symbol."
- "How much USDT do I have free vs blocked in margin?"

The assistant picks the right tool based on the question. You don't need to name the tool.

## Development

```bash
uv sync                       # install deps
uv run pytest                 # run tests (no network, respx-mocked)
uv run ruff check src tests   # lint
uv run delta-exchange-mcp     # run server (stdio)
```

### Testing with MCP Inspector

```bash
# stdio CLI mode
bash scripts/inspect.sh --cli --method tools/list
bash scripts/inspect.sh --cli --method tools/call \
  --tool-name get_ticker --tool-arg symbol=BTCUSD

# with auth
DELTA_API_KEY=... DELTA_API_SECRET=... \
  bash scripts/inspect.sh --cli --method tools/call --tool-name get_wallet_balances

# web UI
bash scripts/inspect.sh        # → http://localhost:6274
```

Maintainers: see [`RELEASING.md`](RELEASING.md) for the release procedure.

## Roadmap

- **Now**: 9 public market-data + 12 authenticated read-only account tools.
- **Next**: trading mutations (place / edit / cancel / close, leverage change) gated behind an explicit `DELTA_MCP_MODE=trade` flag, with an audit log and basic guardrails.

## Feedback & issues

This is the first public cut and we want to make it better. Please file:

- Bugs (incorrect data, signing/auth errors, crashes)
- Missing tools or fields you'd want exposed
- Rough edges in setup, docs, or error messages
- Anything you'd build on top of this if a primitive existed

→ [github.com/delta-exchange/delta-exchange-mcp/issues](https://github.com/delta-exchange/delta-exchange-mcp/issues)

Please redact `api_key` / `api_secret` from any logs or screenshots before attaching.

## Safety

- **No mutations.** Every tool is GET. The server cannot place, edit, or cancel orders.
- **Local stdio only.** Per-user keys never leave your machine; no shared hosted endpoint.
- **Read the code.** It's a financial-tool MCP; treat it like one.
