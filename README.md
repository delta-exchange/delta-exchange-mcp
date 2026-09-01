<div align="center">

<!-- Pinned to a commit, not a branch. PyPI renders this file for every published version, so
     a branch path would make an old version's page show whatever the icon is today, or break
     outright if the file is ever moved. A pinned blob also renders from a fork or a PR ref,
     which is what got it pinned originally, before the file reached main. -->
<img src="https://raw.githubusercontent.com/delta-exchange/delta-exchange-mcp/d49dfba97e57120448bb4e0267abde6d7931e5f1/packaging/mcpb/icon.png" width="88" alt="Delta Exchange">

# delta-exchange-mcp

> *Ask about Delta Exchange India in plain English — live prices, option chains, your own positions — from Claude or your editor.*

![Status: Beta][beta-badge] [![PyPI version][pypi-version-badge]][pypi-version-link]

[![Download for Claude Desktop][claude-desktop-badge]][claude-desktop-link] [![Add to Cursor][cursor-badge]][cursor-link] [![Install in VS Code][vs-code-badge]][vs-code-link]

[![Claude Code][claude-code-jump-badge]][claude-code-jump-link] [![Codex][codex-jump-badge]][codex-jump-link] [![Windsurf][windsurf-jump-badge]][windsurf-jump-link] [![Zed][zed-jump-badge]][zed-jump-link]

</div>

Official MCP (Model Context Protocol) server for **Delta Exchange India**: market data for
everyone, your own account with an API key, and live trading only after browser consent
for the requesting MCP client.

> [!NOTE]
> **Beta.** Functional and used internally, but the tool surface and configuration may still
> change. Please [open an issue](https://github.com/delta-exchange/delta-exchange-mcp/issues)
> for bugs, missing tools, or rough edges — early reports directly shape what ships next.

## Use it — no setup

Hit **Download for Claude Desktop** above and double-click the file. Claude Desktop installs
the server with no configuration form, and **you do not need to install `uv` or Python**:
the app fetches both. Unlike the Cursor and VS Code buttons, this is a file download because
Claude Desktop, Claude Code, and MCP for Windows read bundles directly.

Market data works as soon as the server starts. To use your account, ask the assistant to
connect your Delta Exchange account, call `setup_credentials`, or retry an account tool and
open its **Manage Connection** link. The link opens a short-lived page on `127.0.0.1`. Enter
your key and secret there. The page sends them directly to the local MCP process, outside the
conversation, and stores them in the operating-system credential service when it is available.

Trading tools are always visible, but they cannot send a mutation until you enable trading
on Manage Connection for the exact MCP client, environment, and credential revision. A
production approval requires an explicit warning acknowledgement.

<details>
<summary><b>Claude Desktop — JSON config by hand</b> (also how you pin a specific version)</summary>

Open **Settings → Developer → Edit config**, or edit directly at:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "delta-exchange-mcp": {
      "command": "uvx",
      "args": ["delta-exchange-mcp"]
    }
  }
}
```

Quit and relaunch Claude Desktop for changes to take effect.

</details>

## Wire it into your editor

**Prerequisite:** [`uv`](https://docs.astral.sh/uv/getting-started/installation/) installed
on your machine. Sanity-check:

```bash
uvx delta-exchange-mcp --help
```

The server runs **local stdio only**: your MCP client launches it as a subprocess, and your
API keys never leave your machine except in signed requests to Delta. Manage Connection
stores them in the operating-system credential service, or in process memory when secure
storage is unavailable. `uvx` resolves the latest
published version from PyPI on each launch. To pin a specific version, use `uvx "delta-exchange-mcp==0.4.2"`. Pin `0.4.2`
or newer: earlier versions do not start on a fresh install (see
[Troubleshooting](#troubleshooting)).

**Cursor** and **VS Code** install with one click from the buttons above; every other client
takes a CLI one-liner or a short config block — all covered per client under
[Install in your MCP client](#install-in-your-mcp-client).

## What you can ask

```txt
What's the current BTCUSD mark price and 24h range?
Show the options chain for BTC expiring this Friday.
What positions do I have open and what's my total unrealized PnL?
List my fills from the last 24 hours grouped by symbol.
How much USDT do I have free vs blocked in margin?
```

The assistant picks the right tool based on the question — you never name a tool. A real
exchange, verbatim from a Claude Desktop session against the live API:

> **eth prices, use delta-exchange**
>
> ETHUSD Perpetual (Delta Exchange): $1,856.25 (last) — down 3.15% in 24h (24h range:
> $1,847.25 – $1,936.25). Spot index: $1,857.19. Mark price: $1,856.07. Funding rate:
> −0.284%. Open interest: ~$54.2M.

## Capabilities

The server exposes one stable tool list. Authorization is checked when each tool runs, so a
client does not need a restart after you connect an account or change trading consent.

| Capability | Requirement | Includes |
|---|---|---|
| Market data | none | Prices, order books, option chains, candles, funding and open-interest history, indices |
| Account, read-only | a connected API key | Positions, orders, fills, balances, trading stats, preferences |
| Trading | a connected key plus browser consent for this MCP client | Place, edit and cancel orders, brackets, leverage, margin, close-all |

A partial key and secret pair fails closed. Account and trading calls return a Manage
Connection link until the required authorization is available.

### Trading consent

Open Manage Connection, select the active environment, and enable trading. Consent binds to
the exact client name reported in the MCP request, the environment, and the current
credential identity. Rotating or disconnecting credentials, changing the environment, or
changing a process credential pair invalidates the approval. The client name separates
approvals; it is self-reported and is not authentication.

Consent persists only when the credential is in the operating-system credential service.
If secure storage is unavailable, or the MCP client supplies `DELTA_API_KEY` and
`DELTA_API_SECRET` as process values, consent lasts only for the server process.
`DELTA_MCP_MODE` is a legacy setting and is ignored. It never authorizes trading.

> [!WARNING]
> Trading consent does not cap notional or position size, add another confirmation before
> sending, convert contracts to coins, or judge an order. Test on `india_testnet` first.

Safety controls:

- **Dry run.** Every mutating tool accepts `dry_run=true` and returns the payload without sending it. A dry run does not require trading consent because it does not mutate the account.
- **Audit log.** Each real or dry-run mutation is appended to an owner-only JSONL file under `~/.delta-exchange-mcp/audit/`. Set `DELTA_MCP_AUDIT=off` to disable it. Credentials are never logged.
- **No silent retries.** Mutations are not retried automatically after a timeout or rate limit.
- **API key permission.** Real mutations require Trading permission and any IP whitelist required by Delta.

## Connect your account

Market data needs no key. For account access, use one of these equivalent routes:

- Ask the assistant to connect your Delta Exchange account.
- Call `setup_credentials`.
- Run `uvx delta-exchange-mcp login`.
- Retry an account tool and open the Manage Connection link in the authorization response.

All routes open the same short-lived loopback page. Choose production or testnet, enter the
complete key and secret pair, and submit. The local service validates the pair before it
replaces the active credential. Do not paste credentials into a chat or an ordinary tool
argument.

The service stores credentials in the operating-system credential service. If that service
is unavailable, it uses process memory and reports the connection as session-only. Existing
plaintext credentials in `~/.delta-exchange-mcp/config.env` are migrated to the credential
service when possible; new secrets are never written to that file.

A client can still supply a complete `DELTA_API_KEY` and `DELTA_API_SECRET` pair in its
process environment for compatibility. Such a pair takes precedence, cannot be replaced or
disconnected in Manage Connection, and permits only session consent. `india_devnet` is an
internal environment with no public dashboard. It accepts process credentials only and also
permits only session consent.

> [!IMPORTANT]
> A credential sent as a chat message becomes part of the conversation. Manage Connection
> exists so the model does not receive the key or secret.

### Getting the key itself

1. Create it at [delta.exchange/app/account/manageapikeys](https://www.delta.exchange/app/account/manageapikeys) (testnet: [demo.delta.exchange](https://demo.delta.exchange/app/account/manageapikeys)).
2. Delta shows both `api_key` and `api_secret` once at creation. Save the secret immediately.
3. **Read Data** permission is enough for account reads. Trading permission is required for real mutations.
4. A production key works only with `india_prod`; a demo key works only with `india_testnet`.
5. If the key has an IP whitelist, Delta rejects requests from other addresses.

### Settings reference

Manage Connection owns production and testnet credentials. The shared settings file contains
non-secret settings only. Process settings take precedence over values in that file.

| Variable | Default | Purpose |
|---|---|---|
| `DELTA_MCP_ENV` | `india_prod` | Select `india_prod`, `india_testnet`, or process-only `india_devnet`. Browser selection updates this non-secret setting when the process does not fix it. |
| `DELTA_API_KEY` | unset | Complete process-managed compatibility credential. Never read from the shared file. |
| `DELTA_API_SECRET` | unset | Secret paired with `DELTA_API_KEY`. Never read from the shared file. |
| `DELTA_MCP_MODE` | ignored | Legacy compatibility setting. It never authorizes trading. |
| `DELTA_MCP_DEBUG` | unset | `1`, `true`, `yes`, or `on` writes HTTP request and response details to a local log. |
| `DELTA_MCP_DEBUG_FILE` | automatic | Override the debug log path. |
| `DELTA_MCP_AUDIT` | on for mutations | Set `off`, `false`, `0`, or `no` to disable the trading audit log. |
| `DELTA_MCP_AUDIT_FILE` | automatic | Override the audit log path. |
| `DELTA_MCP_CONFIG_FILE` | `~/.delta-exchange-mcp/config.env` | Move the non-secret shared settings file. |

## Install in your MCP client

None of the snippets below carry credentials. Install and restart the MCP client, then
use Manage Connection when you need account access or trading consent.

### Let your coding agent set it up

Copy this into Claude Code, Codex, Cursor, or any agent that can edit files on your
machine. Claude Desktop's chat is not one of those — it has no access to your filesystem,
so use the **Download for Claude Desktop** bundle at the top of this page instead.

```text
Install the Delta Exchange MCP server into my MCP client: uvx delta-exchange-mcp, local
stdio, entry name delta-exchange-mcp, no env block. Leave my other servers alone, and
never ask me for my API key. Verify with `uvx delta-exchange-mcp --version` — never run
it bare, it serves stdio and won't exit. Then tell me to restart the app and stop there —
don't authenticate me first, don't send me to a terminal, and don't start a new chat. We
carry on in this one after the restart.

If you can't edit files on this machine, say so and read the README below for the
simplest path for my client — don't improvise a config for me to paste.

If you don't know where my client keeps its MCP config, read
https://raw.githubusercontent.com/delta-exchange/delta-exchange-mcp/main/README.md
```

The entry holds no credentials. The server starts with its complete tool list and market
data works immediately. Restart the app after installation, then continue in the same
conversation and ask it to connect your Delta account. The assistant returns a local Manage
Connection link; account authorization does not require another client restart.

### Cursor

[![Add to Cursor][cursor-badge]][cursor-link]

Cursor shows an approval dialog and writes the credential-free server entry. Market data works immediately; use Manage Connection for account access.

<details>
<summary><b>Cursor — JSON config by hand</b></summary>

Global: `~/.cursor/mcp.json` (or `%USERPROFILE%\.cursor\mcp.json` on Windows). Project-scoped alternative: `.cursor/mcp.json` in the repo root.

```json
{
  "mcpServers": {
    "delta-exchange-mcp": {
      "command": "uvx",
      "args": ["delta-exchange-mcp"]
    }
  }
}
```

Restart Cursor or open **Settings → Tools & MCP** to refresh.

</details>

### VS Code (GitHub Copilot)

[![Install in VS Code][vs-code-badge]][vs-code-link]
[![Install in VS Code Insiders][vs-code-insiders-badge]][vs-code-insiders-link]

The install link writes a credential-free stdio entry. Market data works immediately; use
Manage Connection for account access.

<details>
<summary><b>VS Code — JSON config by hand</b></summary>

Add this to `.vscode/mcp.json` in your workspace:

```json
{
  "servers": {
    "delta-exchange-mcp": {
      "type": "stdio",
      "command": "uvx",
      "args": ["delta-exchange-mcp"]
    }
  }
}
```

</details>

### Claude Code

```bash
claude mcp add delta-exchange-mcp \
  --scope user -- uvx delta-exchange-mcp
```

`--scope user` makes the server available across all projects. Verify with `claude mcp list`.

### Codex

```bash
codex mcp add delta-exchange-mcp -- uvx delta-exchange-mcp
```

Verify with `codex mcp list`.

### OpenClaw

```bash
openclaw mcp add delta-exchange-mcp \
  --command uvx \
  --arg delta-exchange-mcp
```

Repeat `--arg` once per value. Writes to `~/.openclaw/openclaw.json`, where MCP servers live
under `mcp.servers` rather than a top-level `mcpServers` key.

<details>
<summary><b>Codex — TOML config, or the desktop app's form</b></summary>

Write `~/.codex/config.toml` by hand:

```toml
[mcp_servers.delta-exchange-mcp]
command = "uvx"
args = ["delta-exchange-mcp"]
```

Desktop app: go to **Plugins → MCPs → Connect to a custom MCP**, leave **Type** as STDIO, and fill in:

| Field | Value |
|---|---|
| Name | `delta-exchange-mcp` |
| Command to launch | `uvx` (`uvx.exe` on Windows) |
| Arguments | `delta-exchange-mcp` |
| Environment variables | leave empty |

Leave the other fields empty, then restart the app.

</details>

### Windsurf

<details>
<summary><b>JSON config</b></summary>

Add to `~/.codeium/windsurf/mcp_config.json` (macOS / Linux) or `%USERPROFILE%\.codeium\windsurf\mcp_config.json` (Windows). UI route: **Settings → Cascade → Plugins (MCP servers) → Manage Plugins → View raw config**.

```json
{
  "mcpServers": {
    "delta-exchange-mcp": {
      "command": "uvx",
      "args": ["delta-exchange-mcp"]
    }
  }
}
```

</details>

### Zed

<details>
<summary><b>JSON config</b></summary>

Add to `~/.config/zed/settings.json` (user-level) or `.zed/settings.json` (project-level). Zed
calls the top-level key `context_servers` rather than `mcpServers`; the entry itself has the
usual shape:

```json
{
  "context_servers": {
    "delta-exchange-mcp": {
      "command": "uvx",
      "args": ["delta-exchange-mcp"]
    }
  }
}
```

</details>

### Google Antigravity

<details>
<summary><b>JSON config</b></summary>

Add to `~/.gemini/config/mcp_config.json` — note the `.gemini` directory, which Antigravity
shares rather than using one of its own. In the IDE you can reach the same file without
typing a path: the `...` menu at the top of the agent panel → **MCP Servers → Manage MCP
Servers → View raw config**. In the CLI, type `/mcp`.

```json
{
  "mcpServers": {
    "delta-exchange-mcp": {
      "command": "uvx",
      "args": ["delta-exchange-mcp"]
    }
  }
}
```

Prefer that global file over a project-local one: a project-level `mcp_config.json` is
reported to be read and then silently ignored. Reopen the MCP panel after editing so the server actually spawns. Then use Manage
Connection for account access.

</details>

## Safety

- **Request-time authorization.** All tools stay visible, but account calls require a current credential and real mutations require browser consent for the exact client, environment, and credential identity.
- **Credential isolation.** Manage Connection sends secrets only to the local loopback service. The model never receives them.
- **Fail-closed changes.** Credential rotation, disconnect, environment changes, consent-store failures, and process-pair changes disable trading until fresh consent is recorded.
- **Auditable mutations.** Mutations are dry-runnable, audit-logged by default, and never retried automatically.
- **Local stdio only.** Each MCP client launches a local subprocess; there is no shared hosted endpoint.

## API request analytics

Delta API requests carry a small set of headers that identify the MCP client and tool that
caused the request. Delta uses these headers to measure client and tool usage. The client
name is self-reported. The authorization layer uses its exact value to partition consent
records, but the name is not proof of identity and cannot grant consent by itself.

| Header | Value |
|---|---|
| `X-Delta-MCP-Version` | This server's version. |
| `X-Delta-MCP-Client` | The exact name reported by the MCP client, when available. |
| `X-Delta-MCP-Client-Version` | The version reported by the MCP client, when available. |
| `X-Delta-MCP-Tool` | The MCP tool that caused the Delta request. |
| `X-Delta-MCP-Protocol` | The MCP protocol version for the request. |
| `X-Delta-MCP-Context` | The client's optional title, description, website, icon count, and capability shape, plus the operating system and Python version. Private extension names and settings are not included. |

The server does not add the Delta environment, trading state, credential source, consent
state, credential or consent revision, account ID, API key, API secret, signature, or a
credential digest to these headers. It also adds no connection or installation identifier.
Untrusted text is encoded, and the complete analytics header set is limited to 4,096 bytes.

## Updating

`uvx` caches the resolved package, so a new PyPI release isn't picked up automatically. To move to the latest version:

1. **If your config pins a version** (`uvx "delta-exchange-mcp==0.4.2"`), bump the pin to the new version, or drop it to float to latest.
2. **Refresh the `uvx` cache** so it fetches the new build:

   ```bash
   uvx --refresh delta-exchange-mcp --help
   ```

3. **Reload the server** so your client respawns the process — in Claude Code, run `/mcp` and reconnect `delta-exchange-mcp`, or restart the client. Other clients: restart the app.

New tools appear only after the respawn. The MCP `list_changed` notification refreshes the tool list of an already-running server; it does **not** swap the underlying package version, which always requires a restart.

## Troubleshooting

### Manage Connection does not open automatically

An authorization response includes a clickable Manage Connection URL even when the client
does not open the browser itself. Open that URL while the MCP server is still running. You
can also call `setup_credentials` or run `uvx delta-exchange-mcp login`; both open the same
loopback page. The page expires after ten minutes.

Do not put credentials in `config.env` as a fallback. That file is for non-secret settings.

### Manage Connection reports an externally managed credential

A complete `DELTA_API_KEY` and `DELTA_API_SECRET` pair supplied by the MCP client or shell
takes precedence over the credential service. Manage Connection cannot replace or disconnect
that pair, and its trading consent lasts only for the current process. Remove both process
values and restart the client if you want Manage Connection to own the credential.

If account calls still fail after connection, verify that the selected environment matches
the site that issued the key. Production and testnet keys are not interchangeable.

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
"delta-exchange-mcp": {
  "command": "uvx",
  "args": ["--with", "mcp<2", "delta-exchange-mcp==0.4.1"]
}
```

## Debugging / reporting a bug

To capture exactly what the server sends and receives — useful when a tool returns
something unexpected — set `DELTA_MCP_DEBUG=1` in your MCP client config:

```jsonc
"delta-exchange-mcp": {
  "command": "uvx",
  "args": ["delta-exchange-mcp"],
  "env": { "DELTA_MCP_DEBUG": "1" }
}
```

Or set it once for every client by adding `DELTA_MCP_DEBUG=1` to the non-secret shared
settings file at `~/.delta-exchange-mcp/config.env`.

Restart the client and re-run the action. Each HTTP call (request URL incl. filter params +
response body + status) is logged to `~/.delta-exchange-mcp/logs/`. The exact path is printed
on startup and you can also just **ask the assistant: _"where is the debug log?"_**.

> The log **never** contains your API key, secret, or request signatures — but response bodies
> **do** contain your account data (balances, positions, transactions). **Review before sharing.**

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
      ]
    }
  }
}
```

**Zed (`context_servers` key):**

```json
{
  "context_servers": {
    "delta-exchange-mcp-dev": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/delta-exchange/delta-exchange-mcp.git@develop",
        "delta-exchange-mcp"
      ]
    }
  }
}
```

Register the dev server under a separate name (e.g. `delta-exchange-mcp-dev`) so it doesn't collide with the PyPI install. The git+URL form rebuilds from source on each launch and is meant for testing unreleased changes — stick with `uvx delta-exchange-mcp` for everyday use.

## Development

Use Python 3.12 or later and Node.js 22 or later for development. The test suite uses
Node.js to parse the generated browser JavaScript. End users do not need Node.js.

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

- **Now**: public market data, authenticated read-only account access, and opt-in trading with dry-run and an audit log.
- **Next**: richer guardrails (notional / position-size caps, confirmation prompts).

## Feedback & issues

This is the first public cut and we want to make it better. Please file:

- Bugs (incorrect data, signing/auth errors, crashes)
- Missing tools or fields you'd want exposed
- Rough edges in setup, docs, or error messages
- Anything you'd build on top of this if a primitive existed

→ [github.com/delta-exchange/delta-exchange-mcp/issues](https://github.com/delta-exchange/delta-exchange-mcp/issues)

Please redact `api_key` / `api_secret` from any logs or screenshots before attaching.

---

<div align="center">

[MIT licence](https://github.com/delta-exchange/delta-exchange-mcp/blob/main/LICENSE) · [Source](https://github.com/delta-exchange/delta-exchange-mcp) · [Issues](https://github.com/delta-exchange/delta-exchange-mcp/issues) · [PyPI](https://pypi.org/project/delta-exchange-mcp/)

</div>

<!-- Badges: defined once, used in the hero and in each client's section. Gray badges jump
     to in-page install sections; colored ones act (download / deeplink). -->
[beta-badge]: https://img.shields.io/badge/status-beta-orange?style=flat-square
[pypi-version-badge]: https://img.shields.io/pypi/v/delta-exchange-mcp?style=flat-square
[pypi-version-link]: https://pypi.org/project/delta-exchange-mcp/
[cursor-badge]: https://img.shields.io/badge/Cursor-Add_Server-0098FF?style=for-the-badge&logo=cursor&logoColor=white
[cursor-link]: https://cursor.com/install-mcp?name=delta-exchange-mcp&config=eyJjb21tYW5kIjoidXZ4IiwiYXJncyI6WyJkZWx0YS1leGNoYW5nZS1tY3AiXX0%3D
[vs-code-badge]: https://img.shields.io/badge/VS_Code-Install_Server-0098FF?style=for-the-badge&logo=githubcopilot&logoColor=white
[vs-code-link]: https://insiders.vscode.dev/redirect/mcp/install?name=delta-exchange-mcp&config=%7B%22type%22%3A%22stdio%22%2C%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22delta-exchange-mcp%22%5D%7D
[vs-code-insiders-badge]: https://img.shields.io/badge/VS_Code_Insiders-Install_Server-24bfa5?style=for-the-badge&logo=githubcopilot&logoColor=white
[vs-code-insiders-link]: https://insiders.vscode.dev/redirect/mcp/install?name=delta-exchange-mcp&config=%7B%22type%22%3A%22stdio%22%2C%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22delta-exchange-mcp%22%5D%7D&quality=insiders
[claude-desktop-badge]: https://img.shields.io/badge/Claude_Desktop-Download_bundle-D97757?style=for-the-badge&logo=claude&logoColor=white
<!-- Downloads the bundle directly. This addresses the unversioned alias rather than the
     versioned filename, because /releases/latest/download/ needs an asset name that does not
     change between releases; the attach job uploads both. It 404s if the newest release
     carries no bundle, which is why RELEASING.md curls this exact URL after publishing. -->
[claude-desktop-link]: https://github.com/delta-exchange/delta-exchange-mcp/releases/latest/download/delta-exchange-mcp.mcpb
[claude-code-jump-badge]: https://img.shields.io/badge/Claude_Code-setup_below-6e7681?style=for-the-badge&logo=claude&logoColor=white
[claude-code-jump-link]: #claude-code
[codex-jump-badge]: https://img.shields.io/badge/Codex-setup_below-6e7681?style=for-the-badge&logo=data%3Aimage%2Fsvg%2Bxml%3Bbase64%2CPHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCIgZmlsbD0id2hpdGUiPjxwYXRoIGQ9Ik0yMi4yODIgOS44MjFhNS45ODUgNS45ODUgMCAwIDAtLjUxNi00LjkxIDYuMDQ2IDYuMDQ2IDAgMCAwLTYuNTEtMi45QTYuMDY1IDYuMDY1IDAgMCAwIDQuOTgxIDQuMThhNS45ODUgNS45ODUgMCAwIDAtMy45OTggMi45IDYuMDQ2IDYuMDQ2IDAgMCAwIC43NDMgNy4wOTcgNS45OCA1Ljk4IDAgMCAwIC41MSA0LjkxMSA2LjA1MSA2LjA1MSAwIDAgMCA2LjUxNSAyLjlBNS45ODUgNS45ODUgMCAwIDAgMTMuMjYgMjRhNi4wNTYgNi4wNTYgMCAwIDAgNS43NzItNC4yMDYgNS45OSA1Ljk5IDAgMCAwIDMuOTk3LTIuOSA2LjA1NiA2LjA1NiAwIDAgMC0uNzQ3LTcuMDczek0xMy4yNiAyMi40M2E0LjQ3NiA0LjQ3NiAwIDAgMS0yLjg3Ni0xLjA0bC4xNDEtLjA4MSA0Ljc3OS0yLjc1OGEuNzk1Ljc5NSAwIDAgMCAuMzkyLS42ODF2LTYuNzM3bDIuMDIgMS4xNjhhLjA3MS4wNzEgMCAwIDEgLjAzOC4wNTJ2NS41ODNhNC41MDQgNC41MDQgMCAwIDEtNC40OTQgNC40OTR6TTMuNiAxOC4zMDRhNC40NyA0LjQ3IDAgMCAxLS41MzUtMy4wMTRsLjE0Mi4wODUgNC43ODMgMi43NTlhLjc3MS43NzEgMCAwIDAgLjc4IDBsNS44NDMtMy4zNjl2Mi4zMzJhLjA4LjA4IDAgMCAxLS4wMzMuMDYyTDkuNzQgMTkuOTVhNC41IDQuNSAwIDAgMS02LjE0LTEuNjQ2ek0yLjM0IDcuODk2YTQuNDg1IDQuNDg1IDAgMCAxIDIuMzY2LTEuOTczVjExLjZhLjc2Ni43NjYgMCAwIDAgLjM4OC42NzdsNS44MTUgMy4zNTQtMi4wMiAxLjE2OGEuMDc2LjA3NiAwIDAgMS0uMDcxIDBsLTQuODMtMi43ODZBNC41MDQgNC41MDQgMCAwIDEgMi4zNCA3Ljg3MnptMTYuNTk3IDMuODU1LTUuODMzLTMuMzg3TDE1LjExOSA3LjJhLjA3Ni4wNzYgMCAwIDEgLjA3MSAwbDQuODMgMi43OTFhNC40OTQgNC40OTQgMCAwIDEtLjY3NiA4LjEwNXYtNS42NzhhLjc5Ljc5IDAgMCAwLS40MDctLjY2N3ptMi4wMS0zLjAyMy0uMTQxLS4wODUtNC43NzQtMi43ODJhLjc3Ni43NzYgMCAwIDAtLjc4NSAwTDkuNDA5IDkuMjNWNi44OTdhLjA2Ni4wNjYgMCAwIDEgLjAyOC0uMDYxbDQuODMtMi43ODdhNC41IDQuNSAwIDAgMSA2LjY4IDQuNjZ6TTguMzA1IDEyLjg2M2wtMi4wMi0xLjE2NGEuMDguMDggMCAwIDEtLjAzOC0uMDU3VjYuMDc1YTQuNSA0LjUgMCAwIDEgNy4zNzUtMy40NTNsLS4xNDIuMDhMOC43IDUuNDZhLjc5NS43OTUgMCAwIDAtLjM5My42ODF6bTEuMDk3LTIuMzY1IDIuNjAyLTEuNSAyLjYwNyAxLjV2Mi45OTlsLTIuNTk3IDEuNS0yLjYwNy0xLjV6Ii8%2BPC9zdmc%2B
[codex-jump-link]: #codex
[windsurf-jump-badge]: https://img.shields.io/badge/Windsurf-setup_below-6e7681?style=for-the-badge&logo=windsurf&logoColor=white
[windsurf-jump-link]: #windsurf
[zed-jump-badge]: https://img.shields.io/badge/Zed-setup_below-6e7681?style=for-the-badge&logo=zedindustries&logoColor=white
[zed-jump-link]: #zed
