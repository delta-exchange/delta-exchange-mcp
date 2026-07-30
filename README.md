# delta-exchange-mcp

![Status: Beta](https://img.shields.io/badge/status-beta-orange)
[![PyPI version](https://img.shields.io/pypi/v/delta-exchange-mcp)](https://pypi.org/project/delta-exchange-mcp/)

Official MCP (Model Context Protocol) server for **Delta Exchange India**. Lets AI assistants (Claude Desktop, Claude Code, Cursor, Zed, Codex) query Delta Exchange market data and your own account (read-only) through standardized tools.

> **Status:** Beta. Functional and used internally, but the tool surface and configuration may still change. Please [open an issue](https://github.com/delta-exchange/delta-exchange-mcp/issues) for bugs, missing tools, or rough edges. Early reports directly shape what ships next.

**What you get:** 14 public market-data tools + 13 authenticated read-only account tools (positions, orders, fills, wallet, stats, leverage, preferences, profile). Trading mutations (place / edit / cancel orders, brackets, leverage, margin, close-all) are available but **off by default** — they register only when you opt in with `DELTA_MCP_MODE=trade` (see [Trading](#trading-opt-in)).

---

## Quick start

**Prerequisite:** [`uv`](https://docs.astral.sh/uv/getting-started/installation/) installed on your machine.

Sanity-check the install:

```bash
uvx delta-exchange-mcp --help
```

The server runs **local stdio only**: your MCP client launches it as a subprocess, and your API keys never leave your machine. `uvx` resolves the latest published version from PyPI on each launch. To pin a specific version, use `uvx "delta-exchange-mcp==0.4.2"`. Pin `0.4.2` or newer: earlier versions do not start on a fresh install (see [Troubleshooting](#troubleshooting)).

## Install in your MCP client

`DELTA_API_KEY` / `DELTA_API_SECRET` are **optional** in every snippet below — drop them for public-data-only mode. Set `DELTA_MCP_ENV=india_testnet` for testnet.

### Let your coding agent set it up

Copy this into Claude Code, Codex, Cursor, or any agent that can edit files on your machine:

```text
Set up the Delta Exchange MCP server for me.

Read https://raw.githubusercontent.com/delta-exchange/delta-exchange-mcp/main/README.md
and follow the "Install in your MCP client" section for the client I am using. If you
cannot tell which client that is, ask me.

Rules:
- Add a server entry named delta-exchange-mcp. Do not modify or remove any other MCP
  server I already have configured.
- Write the literal placeholders your-api-key and your-api-secret. Do not ask me for my
  real API keys, and do not repeat them back to me. I will fill them in myself.
- Use DELTA_MCP_ENV=india_prod unless I tell you testnet.
- Check your work by running: uvx delta-exchange-mcp --version
  Do not run the server without arguments. It serves over stdio and will not exit.
- If that command fails with an import error, follow the README's Troubleshooting section.
- Tell me which file you changed, and that I need to restart the client.
```

Your agent writes placeholder credentials, never your real ones. Open the file it names,
replace `your-api-key` and `your-api-secret` with your keys from the Delta dashboard, then
restart the client. To set it up by hand instead, follow the steps for your client below.

### Claude Code

```bash
claude mcp add delta-exchange-mcp \
  --scope user \
  --env DELTA_MCP_ENV=india_prod \
  --env DELTA_API_KEY=your-api-key \
  --env DELTA_API_SECRET=your-api-secret \
  -- uvx delta-exchange-mcp
```

`--scope user` makes the server available across all projects. Verify with `claude mcp list`.

### Cursor

Global: `~/.cursor/mcp.json` (or `%USERPROFILE%\.cursor\mcp.json` on Windows). Project-scoped alternative: `.cursor/mcp.json` in the repo root.

```json
{
  "mcpServers": {
    "delta-exchange-mcp": {
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

Restart Cursor or open **Settings → Tools & MCP** to refresh.

### Codex CLI

Add to `~/.codex/config.toml`:

```toml
[mcp_servers.delta-exchange-mcp]
command = "uvx"
args = ["delta-exchange-mcp"]
env = { DELTA_MCP_ENV = "india_prod", DELTA_API_KEY = "your-api-key", DELTA_API_SECRET = "your-api-secret" }
```

### Codex desktop app

To add the server from the app's UI, go to **Plugins → MCPs → Connect to a custom MCP**, leave **Type** as STDIO, and fill in:

| Field | Value |
|---|---|
| Name | `delta-exchange-mcp` |
| Command to launch | `uvx` (`uvx.exe` on Windows) |
| Arguments | `delta-exchange-mcp` |
| Environment variables | `DELTA_MCP_ENV` = `india_prod`, `DELTA_API_KEY` = your-api-key, `DELTA_API_SECRET` = your-api-secret |

Leave the other fields empty, then restart the app.

### Windsurf

Add to `~/.codeium/windsurf/mcp_config.json` (macOS / Linux) or `%USERPROFILE%\.codeium\windsurf\mcp_config.json` (Windows). UI route: **Settings → Cascade → Plugins (MCP servers) → Manage Plugins → View raw config**.

```json
{
  "mcpServers": {
    "delta-exchange-mcp": {
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

### Zed

Add to `~/.config/zed/settings.json` (user-level) or `.zed/settings.json` (project-level). Zed uses the top-level key `context_servers` and nests `command` as an object — note the shape difference from other clients:

```json
{
  "context_servers": {
    "delta-exchange-mcp": {
      "command": {
        "path": "uvx",
        "args": ["delta-exchange-mcp"],
        "env": {
          "DELTA_MCP_ENV": "india_prod",
          "DELTA_API_KEY": "your-api-key",
          "DELTA_API_SECRET": "your-api-secret"
        }
      }
    }
  }
}
```

### VS Code (GitHub Copilot)

Add to `.vscode/mcp.json` in your workspace. The top-level key is `servers` and each entry needs an explicit `"type": "stdio"`:

```json
{
  "servers": {
    "delta-exchange-mcp": {
      "type": "stdio",
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

### Claude Desktop

Open **Settings → Developer → Edit config**, or edit directly at:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "delta-exchange-mcp": {
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

Quit and relaunch Claude Desktop for changes to take effect.

## Running a dev / unreleased branch

To test an unreleased commit, branch, or fork before it's on PyPI, swap `uvx delta-exchange-mcp` for `uvx --from git+<repo-url>@<ref> delta-exchange-mcp`. `<ref>` can be a branch, tag, or commit SHA.

CLI sanity check:

```bash
uvx --from git+https://github.com/delta-exchange/delta-exchange-mcp.git@develop delta-exchange-mcp --help
```

`uv` caches the git resolution, so to pick up new commits on the same branch:

```bash
uvx --refresh --from git+https://github.com/delta-exchange/delta-exchange-mcp.git@develop delta-exchange-mcp --help
```

### In your MCP client config

Replace `args` in any snippet above with the `git+` form. Three flavours:

**Claude Code:**

```bash
claude mcp add delta-exchange-mcp-dev \
  --scope user \
  --env DELTA_MCP_ENV=india_prod \
  -- uvx --from git+https://github.com/delta-exchange/delta-exchange-mcp.git@develop delta-exchange-mcp
```

**Cursor / Windsurf / Claude Desktop (any `mcpServers` JSON):**

```json
{
  "mcpServers": {
    "delta-exchange-mcp-dev": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/delta-exchange/delta-exchange-mcp.git@develop",
        "delta-exchange-mcp"
      ],
      "env": {
        "DELTA_MCP_ENV": "india_prod"
      }
    }
  }
}
```

**Zed (nested `command` object):**

```json
{
  "context_servers": {
    "delta-exchange-mcp-dev": {
      "command": {
        "path": "uvx",
        "args": [
          "--from",
          "git+https://github.com/delta-exchange/delta-exchange-mcp.git@develop",
          "delta-exchange-mcp"
        ],
        "env": {
          "DELTA_MCP_ENV": "india_prod"
        }
      }
    }
  }
}
```

Register the dev server under a separate name (e.g. `delta-exchange-mcp-dev`) so it doesn't collide with the PyPI install. The git+URL form rebuilds from source on each launch and is meant for testing unreleased changes — stick with `uvx delta-exchange-mcp` for everyday use.

## Updating

`uvx` caches the resolved package, so a new PyPI release isn't picked up automatically. To move to the latest version:

1. **If your config pins a version** (`uvx "delta-exchange-mcp==0.4.2"`), bump the pin to the new version, or drop it to float to latest.
2. **Refresh the `uvx` cache** so it fetches the new build:

   ```bash
   uvx --refresh delta-exchange-mcp --help
   ```

3. **Reload the server** so your client respawns the process — in Claude Code, run `/mcp` and reconnect `delta-exchange-mcp`, or restart the client. Other clients: restart the app.

New tools appear only after the respawn. The MCP `list_changed` notification refreshes the tool list of an already-running server; it does **not** swap the underlying package version, which always requires a restart.

1. Create a key at [delta.exchange/app/account/manageapikeys](https://www.delta.exchange/app/account/manageapikeys) (testnet: [demo.delta.exchange](https://demo.delta.exchange/app/account/manageapikeys)).
2. Both `api_key` and `api_secret` are shown **once at creation**. Save the secret immediately; it can't be re-derived.
3. **Read Data** permission is enough. Trading permission is not required and not used.
4. Recommended: whitelist your IP on the key. Delta blocks non-whitelisted IPs and surfaces your current IP in the error message if it fires.
5. **Match the environment**: prod keys with `DELTA_MCP_ENV=india_prod`, demo keys with `DELTA_MCP_ENV=india_testnet`. Mixing them returns `InvalidApiKey`.

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `DELTA_MCP_ENV` | `india_prod` | `india_prod`, `india_testnet`, or `india_devnet`. |
| `DELTA_API_KEY` | _(unset)_ | API key. Optional; when set with `DELTA_API_SECRET`, account tools register. |
| `DELTA_API_SECRET` | _(unset)_ | API secret matching `DELTA_API_KEY`. |
| `DELTA_MCP_MODE` | `read` | `trade` registers the trading tools (requires API key + secret). Default `read` is read-only. See [Trading](#trading-opt-in). |
| `DELTA_MCP_DEBUG` | _(unset)_ | `1`/`true`/`yes`/`on` writes HTTP request URLs and response bodies to a log file (see [Debugging](#debugging--reporting-a-bug)). |
| `DELTA_MCP_DEBUG_FILE` | _(auto)_ | Override the debug log path. Default: `~/.delta-exchange-mcp/logs/debug-<timestamp>-<pid>.log`. |
| `DELTA_MCP_AUDIT` | _(on in trade mode)_ | Set `off`/`false`/`0`/`no` to disable the trading audit log. On by default whenever `DELTA_MCP_MODE=trade`. |
| `DELTA_MCP_AUDIT_FILE` | _(auto)_ | Override the audit log path. Default: `~/.delta-exchange-mcp/audit/audit-<timestamp>-<pid>.log`. |

## Troubleshooting

### `ModuleNotFoundError: No module named 'mcp.server.fastmcp'`

Your client shows the server as failed, and the process writes this traceback to stderr.

Version 0.4.1 and all earlier versions declare `mcp>=1.12.4` with no upper limit. The `mcp`
package published 2.0.0 on 28 July 2026, and 2.0.0 removed the `mcp.server.fastmcp` module.
`uvx` resolves from the declared range and ignores `uv.lock`, so a fresh install gets 2.0.0
and the server stops at import. Existing installs and `uv sync` checkouts are not affected.

Move to 0.4.2 or a newer version. Those releases pin the SDK to a range that works:

```bash
uvx --refresh delta-exchange-mcp --help
```

To stay on an older version, set the limit yourself:

```bash
uvx --with "mcp<2" "delta-exchange-mcp==0.4.1"
```

Add the same `--with` argument to your MCP client config if you pin an older version there:

```jsonc
"delta-exchange": {
  "command": "uvx",
  "args": ["--with", "mcp<2", "delta-exchange-mcp==0.4.1"]
}
```

## Debugging / reporting a bug

To capture exactly what the server sends and receives — useful when a tool returns
something unexpected — set `DELTA_MCP_DEBUG=1` in your MCP client config:

```jsonc
"delta-exchange": {
  "command": "uvx",
  "args": ["delta-exchange-mcp"],
  "env": {
    "DELTA_MCP_ENV": "india_prod",
    "DELTA_MCP_DEBUG": "1"
  }
}
```

Restart the client and re-run the action. Each HTTP call (request URL incl. filter params +
response body + status) is logged to `~/.delta-exchange-mcp/logs/`. The exact path is printed
on startup and you can also just **ask the assistant: _"where is the debug log?"_** (the
`get_debug_status` tool returns it).

> The log **never** contains your API key, secret, or request signatures — but response bodies
> **do** contain your account data (balances, positions, transactions). **Review before sharing.**

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
| `get_funding_history` / `get_mark_price_history` / `get_oi_history` | Historical funding-rate, mark-price, and open-interest candles. |
| `get_settlement_prices` | Settlement prices for expired / settled derivatives. |
| `get_indices` | Spot price indices Delta builds from multiple exchanges. |
| `get_reference_data` | Assets + indices reference. |

### Account read-only (requires `DELTA_API_KEY` + `DELTA_API_SECRET`)

| Tool | What it returns |
|---|---|
| `get_positions` / `get_margined_positions` | Open positions, sizes, entry, unrealized PnL. |
| `get_wallet_balances` / `get_wallet_transactions` | Per-asset balances + ledger. |
| `get_open_orders` / `get_order_history` / `get_order_by_id` | Active and historical orders. |
| `get_fills` / `bulk_fills_export` | Own trade fills; bulk CSV export of fills to disk. |
| `get_product_leverage` | Per-product leverage setting. |
| `get_trading_stats` / `get_trading_preferences` / `get_profile` | Account-level stats, preferences, profile. |

### Trading (opt-in)

Trading tools register **only** when `DELTA_MCP_MODE=trade` is set alongside valid credentials. Without it the server stays read-only.

| Tool | Action |
|---|---|
| `place_order` / `edit_order` / `cancel_order` | Single limit/market/stop order lifecycle. |
| `cancel_all_orders` | Cancel open orders (optionally filtered by product / contract type). |
| `place_batch_orders` / `edit_batch_orders` / `cancel_batch_orders` | Up to 50 orders on one contract per request. |
| `place_bracket_order` / `edit_bracket_order` | Attach / edit a take-profit + stop-loss bracket. |
| `set_product_leverage` | Set order leverage for a product. |
| `adjust_position_margin` | Add / remove isolated margin on a position. |
| `close_all_positions` | Close all open positions (your `user_id` is resolved automatically from your profile). |
| `configure_auto_topup` | Toggle per-position auto top-up. |

Enable it in your client config:

```jsonc
"delta-exchange": {
  "command": "uvx",
  "args": ["delta-exchange-mcp"],
  "env": {
    "DELTA_MCP_ENV": "india_prod",
    "DELTA_API_KEY": "your-api-key",
    "DELTA_API_SECRET": "your-api-secret",
    "DELTA_MCP_MODE": "trade"
  }
}
```

Safety features:

- **Dry run.** Every mutating tool takes a `dry_run` flag. When `true`, the tool validates and returns the exact payload it *would* send, without sending it. Ask the assistant to "place the order as a dry run first."
- **Audit log.** Every mutation (real or dry-run) is appended as one JSON line to `~/.delta-exchange-mcp/audit/` (owner-only `0600`). On by default in trade mode; disable with `DELTA_MCP_AUDIT=off`. The log records the tool, params, and result/order id — **never** credentials. Ask the assistant "where is the audit log?" (the `get_trading_status` tool returns the path).
- **No silent retries.** Unlike GET reads, mutations are never auto-retried on timeout or rate-limit — a failure is surfaced, not re-sent.
- **API key permission.** The key must have Trading enabled in Delta API management, and the requesting IP whitelisted.

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

- **Now**: 14 public market-data + 13 authenticated read-only account tools + 13 trading tools (opt-in via `DELTA_MCP_MODE=trade`, with dry-run and an audit log).
- **Next**: richer guardrails (notional / position-size caps, confirmation prompts).

## Feedback & issues

This is the first public cut and we want to make it better. Please file:

- Bugs (incorrect data, signing/auth errors, crashes)
- Missing tools or fields you'd want exposed
- Rough edges in setup, docs, or error messages
- Anything you'd build on top of this if a primitive existed

→ [github.com/delta-exchange/delta-exchange-mcp/issues](https://github.com/delta-exchange/delta-exchange-mcp/issues)

Please redact `api_key` / `api_secret` from any logs or screenshots before attaching.

## Safety

- **Read-only by default.** Trading tools register only with the explicit `DELTA_MCP_MODE=trade` opt-in; otherwise every tool is a GET and the server cannot place, edit, or cancel orders.
- **Auditable mutations.** When trading is on, every mutation is dry-runnable and written to an owner-only audit log; mutations are never auto-retried.
- **Local stdio only.** Per-user keys never leave your machine; no shared hosted endpoint.
- **Read the code.** It's a financial-tool MCP; treat it like one.
