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
everyone, your own account read-only with an API key, and live trading only behind an
explicit opt-in flag.

> [!NOTE]
> **Beta.** Functional and used internally, but the tool surface and configuration may still
> change. Please [open an issue](https://github.com/delta-exchange/delta-exchange-mcp/issues)
> for bugs, missing tools, or rough edges — early reports directly shape what ships next.

## Use it — no setup

Hit **Download for Claude Desktop** above and double-click the file. Claude Desktop shows a
form, then installs the server — no config file, and **no need to install `uv` or Python
first**: the app fetches both itself. Unlike the Cursor and VS Code buttons, this one is a
file download rather than a link that configures your editor; bundles are read only by Claude
Desktop, Claude Code, and MCP for Windows.

The form has four fields:

- **Environment** — which exchange to talk to: `india_prod` for the real one, or
  `india_testnet` for the practice site at demo.delta.exchange.
- **API key** and **API secret** — fill them in to let the assistant read your own account.
  Create them under [Account → API Keys](https://www.delta.exchange/app/account/manageapikeys)
  with the **Read Data** permission, which is the one that allows viewing but not trading.
  Leaving them empty gives you market data only, unless you have already put a key in the
  [shared file](#add-your-api-key), in which case that one is used.
- **Mode** — defaults to `read`, which cannot place, change or cancel orders. Setting it to
  `trade` adds the tools that do.

> [!WARNING]
> `trade` mode places **real orders with no size cap**, sized in **contracts rather than
> coins**. It also needs an API key with trading permission, not just Read Data. Leave Mode
> on `read` unless placing live orders is exactly what you want.

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
API keys never leave your machine — they live in one file that every client reads, not in
each client's config. See [Add your API key](#add-your-api-key). `uvx` resolves the latest
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

What the server can do is decided entirely by settings — three tiers, each a strict superset
of the one above it:

| Tier | You set | Unlocks | Audit-logged |
|---|---|---|---|
| Market data | nothing | Prices, order books, option chains, candles, funding / OI history, indices | — |
| Account, read-only | an API key — see [Add your API key](#add-your-api-key) | Your positions, orders, fills, balances, trading stats, profile | — |
| Trading | a key plus `DELTA_MCP_MODE=trade` in one client's config | Place / edit / cancel orders, brackets, leverage, margin, close-all | Yes |

A key without its matching secret is ignored and you stay on market data — the two are
always used together.

### Trading (opt-in)

The flag is checked before the first tool list of a session, so the tools are absent from
that list entirely rather than present and refusing — an assistant that never sees them
cannot be talked into using them. Turning trading off removes those tools immediately;
turning it on still requires a new session.

> [!WARNING]
> What trade mode will **not** do: cap notional or position size, ask you to confirm before
> sending, convert between contracts and coins, or judge whether an order makes sense. Those
> are your responsibility. Try `DELTA_MCP_ENV=india_testnet` first.

The quickest way is the credential form: ask your assistant to connect your Delta account,
pick **Read and trade** in "What should the assistant be able to do?", and restart that app.
Trading turns on for that app alone — every other client on the machine stays read-only,
because the form stores the choice under that client's own name rather than a shared one.

Or set it yourself, in the config of the one client you mean to trade from:

```jsonc
"delta-exchange-mcp": {
  "command": "uvx",
  "args": ["delta-exchange-mcp"],
  "env": { "DELTA_MCP_MODE": "trade" }
}
```

This is the one setting that is never read from the shared file described in
[Add your API key](#add-your-api-key) under its own name. Everything else there is
convenience; this one places real orders, so it is always tied to one client rather than
arming every assistant on the machine at once. The form does not change that — it writes
`DELTA_MCP_MODE_<READABLE>_<DIGEST>`, keyed on the exact name the client gives during its
handshake, and that key is read only by a client reporting that exact name. The readable
part is just a label; the digest keeps punctuation variants from collapsing onto one key.
This is convenience scoping, not authentication — a client can claim the same name.
`DELTA_MCP_MODE` in a client's own config still wins over it.

Safety features:

- **Dry run.** Every mutating tool takes a `dry_run` flag. When `true`, the tool validates and returns the exact payload it *would* send, without sending it. Ask the assistant to "place the order as a dry run first."
- **Audit log.** Every mutation (real or dry-run) is appended as one JSON line to `~/.delta-exchange-mcp/audit/` (owner-only `0600`). On by default in trade mode; disable with `DELTA_MCP_AUDIT=off`. The log records the tool, params, and result/order id — **never** credentials. Ask the assistant "where is the audit log?".
- **No silent retries.** Unlike GET reads, mutations are never auto-retried on timeout or rate-limit — a failure is surfaced, not re-sent.
- **API key permission.** The key must have Trading enabled in Delta API management, and the requesting IP whitelisted.

## Add your API key

**You may not need one.** Market data works with no key at all, so if you only want prices,
option chains or funding history, skip this section entirely.

To let an assistant read your own account, the key goes in **one file, once** — not into
each client's config:

```
~/.delta-exchange-mcp/config.env
```

Every MCP client on your machine reads it, so a key set here works in Claude Desktop,
Cursor, Claude Code and everything else at the same time. The server creates the file the
first time it runs, with instructions inside it, and prints the path in its startup line.
It is created owner-only (`0600`).

Three ways to fill it in. Pick one — they all write the same file.

**In the conversation**, without leaving it. Ask your assistant to *connect my Delta
account* and a small form appears inline:

```txt
Connect my Delta Exchange account
```

It asks four things: which site the key was made on (delta.exchange or the practice site at
demo.delta.exchange), what the assistant should be allowed to do (read only, or read and
trade), then the key and the secret. It checks the key against Delta before saving, and once
it saves it replaces itself with the account it connected, so you can see which one you got.

What you type in that form goes straight to the file. It is not part of the conversation
and the assistant cannot read it, because the form runs in its own frame rather than in
the chat. This needs a client that renders in-chat forms — Claude Desktop and the Codex
desktop app do today; if nothing appears, yours doesn't, and the assistant will tell you
so and point you at one of the other two ways.

> [!IMPORTANT]
> Sending your key to the assistant as an ordinary chat message is not the same thing. A
> message is in the assistant's context and is stored in the conversation. The form exists
> precisely so it isn't. If an assistant offers to take the key that way, decline.

**At a terminal**, which prompts with the input hidden and checks the key works before
saving anything:

```bash
uvx delta-exchange-mcp login
```

**By hand**, if you'd rather. Open the file and fill in the three lines already waiting
there — plain `NAME=value`, no punctuation to get wrong:

```dotenv
DELTA_API_KEY=your-key-here
DELTA_API_SECRET=your-secret-here
DELTA_MCP_ENV=india_prod
```

The terminal and by-hand routes normally need your MCP client restarted afterwards; asking
for the connection status can also make a running server reconcile safe external changes.
The in-chat form usually needs neither: it atomically moves market and account calls to the
saved environment and key, registers the account tools there, and tells the client its tool
list changed — so first-time setup, environment changes, and key rotation become usable in
the same conversation. Enabling trade mode still needs a new session. Turning it off takes
effect immediately.

If your client has its own place to put credentials — the Claude Desktop bundle's form, VS
Code's prompts, the Codex desktop app's fields — those still work and take precedence over
this file.

### Getting the key itself

1. Create it at [delta.exchange/app/account/manageapikeys](https://www.delta.exchange/app/account/manageapikeys) (testnet: [demo.delta.exchange](https://demo.delta.exchange/app/account/manageapikeys)).
2. Both `api_key` and `api_secret` are shown **once at creation**. Save the secret immediately; it can't be re-derived.
3. **Read Data** permission is enough for the read tiers. Trading permission is needed only for trade mode.
4. **IP whitelisting is only for trading.** Delta requires whitelisted IPs to create a key with Trading permission; a read-only key needs none. If a key does carry a whitelist, Delta blocks other IPs and names the one it saw in the error.
5. **Match the environment**: a key from delta.exchange works only with `india_prod`, one from demo.delta.exchange only with `india_testnet`. Mixing them returns `InvalidApiKey`.

The in-chat form and `login` both check all four for you and refuse to save a key Delta
rejects, so you find out while you still have the key in front of you rather than the next
time you ask a question. Both also write `DELTA_MCP_ENV` alongside the key, so point 5
takes care of itself.

### Settings reference

Each setting is read from your MCP client first, then from `~/.delta-exchange-mcp/config.env`.
A value your client sets always wins; leaving it empty there means "not answered" and falls
through to the file.

| Var | Default | Shared file? | Purpose |
|---|---|---|---|
| `DELTA_MCP_ENV` | `india_prod` | yes | `india_prod`, `india_testnet`, or `india_devnet`. |
| `DELTA_API_KEY` | _(unset)_ | yes | API key. Optional; when set with `DELTA_API_SECRET`, account tools register. |
| `DELTA_API_SECRET` | _(unset)_ | yes | API secret matching `DELTA_API_KEY`. |
| `DELTA_MCP_MODE` | `read` | **no** | `trade` registers the trading tools (requires API key + secret). Per client on purpose — see [Trading](#trading-opt-in). The credential form writes a per-client `DELTA_MCP_MODE_<READABLE>_<DIGEST>` into the shared file instead; this name still wins over it. |
| `DELTA_MCP_DEBUG` | _(unset)_ | yes | `1`/`true`/`yes`/`on` writes HTTP request URLs and response bodies to a log file (see [Debugging](#debugging--reporting-a-bug)). |
| `DELTA_MCP_DEBUG_FILE` | _(auto)_ | yes | Override the debug log path. Default: `~/.delta-exchange-mcp/logs/debug-<timestamp>-<pid>.log`. |
| `DELTA_MCP_AUDIT` | _(on in trade mode)_ | yes | Set `off`/`false`/`0`/`no` to disable the trading audit log. On by default whenever `DELTA_MCP_MODE=trade`. |
| `DELTA_MCP_AUDIT_FILE` | _(auto)_ | yes | Override the audit log path. Default: `~/.delta-exchange-mcp/audit/audit-<timestamp>-<pid>.log`. |
| `DELTA_MCP_CONFIG_FILE` | _(auto)_ | n/a | Move the shared file itself. Default: `~/.delta-exchange-mcp/config.env`. |

The key and its secret are always taken from the same place. If either is set in your
client, both come from there; otherwise both come from the file. That prevents pairing a
stale key from your shell with a secret from the file — a combination that was never issued
together, and which fails every signed call while the server reports the account tools as
available.

## Install in your MCP client

None of the snippets below carry credentials, because they don't need to — put the key in
[the shared file](#add-your-api-key) once instead. Set `DELTA_MCP_ENV=india_testnet` there
for testnet.

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

The entry it writes holds no credentials and needs none — the server comes up on market
data and works the moment you restart. Your key goes in
[the shared file](#add-your-api-key) instead, which is why the agent never has to touch it.
To set it up by hand, follow the steps for your client below.

Restart the app, then carry on in the same conversation: ask it to connect your Delta
account and the form opens there. The restart is needed because a client builds its list
of tools when it starts, so a server added after that isn't connected yet — which is also
why an assistant asked to authenticate you before restarting will reach for a terminal
instead. It has nothing else to offer at that point.

### Cursor

[![Add to Cursor][cursor-badge]][cursor-link]

Cursor shows an approval dialog and writes the entry itself. `DELTA_API_KEY` and `DELTA_API_SECRET` arrive **empty**, so the market-data tools work immediately — fill them in, in the entry it creates, to reach your account. They are empty rather than placeholder text on purpose: a non-empty key registers the account tools and then fails every call, whereas an empty one keeps the server cleanly in public-data mode.

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

VS Code prompts for the key and secret with the input masked, offers the environment as a
dropdown, and writes the entry itself — nothing to edit afterwards. Leave both credential
prompts empty for public-data-only mode.

<details>
<summary><b>VS Code — JSON config by hand</b></summary>

Add to `.vscode/mcp.json` in your workspace. The top-level key is `servers` and each entry needs an explicit `"type": "stdio"`. Declaring the credentials as `inputs` means VS Code prompts for them once and stores them itself, so your secret never lands in a file you might commit — and `pickString` makes the environment a dropdown rather than free text you could typo:

```json
{
  "inputs": [
    {
      "type": "promptString",
      "id": "delta-api-key",
      "description": "Delta API key (leave empty for market data only)",
      "password": true
    },
    {
      "type": "promptString",
      "id": "delta-api-secret",
      "description": "Delta API secret (must match the key)",
      "password": true
    },
    {
      "type": "pickString",
      "id": "delta-env",
      "description": "Delta Exchange environment",
      "options": ["india_prod", "india_testnet"],
      "default": "india_prod"
    }
  ],
  "servers": {
    "delta-exchange-mcp": {
      "type": "stdio",
      "command": "uvx",
      "args": ["delta-exchange-mcp"],
      "env": {
        "DELTA_MCP_ENV": "${input:delta-env}",
        "DELTA_API_KEY": "${input:delta-api-key}",
        "DELTA_API_SECRET": "${input:delta-api-secret}"
      }
    }
  }
}
```

The `env` block is what makes those prompts happen — VS Code only asks for an input that
something references, so without it the three declarations above are inert and you are
never asked for a key.

Leave both prompts empty for public-data-only mode. A key entered here is passed by VS Code
on every launch and takes precedence over the [shared file](#add-your-api-key), so the
in-chat form cannot replace it — clear these prompts first if you want to manage the key
there instead.

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
reported to be read and then silently ignored. Reopen the MCP panel after editing so the
server actually spawns, and check the credentials really arrived — there are reports of `env`
values not reaching the process, in which case the market-data tools work but account tools
stay absent.

</details>

## Safety

- **Read-only by default.** Trading tools register only with the explicit `DELTA_MCP_MODE=trade` opt-in; otherwise every tool is a GET and the server cannot place, edit, or cancel orders.
- **Auditable mutations.** When trading is on, every mutation is dry-runnable and written to an owner-only audit log; mutations are never auto-retried.
- **Local stdio only.** Per-user keys never leave your machine; no shared hosted endpoint.
- **Read the code.** It's a financial-tool MCP; treat it like one.

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

### The key form doesn't appear in the chat

You asked the assistant to connect your account, and it replied with a file path and a
command instead of showing a form.

That is the intended fallback, not a failure: rendering a form inline is an optional part
of MCP that only some clients implement, and the server cannot tell in advance which ones
do — Claude Desktop, for instance, renders these forms without announcing that it can. So
the server always returns working instructions alongside the form, and on a client that
shows nothing you see only the instructions.

Use [one of the other two ways](#add-your-api-key): `uvx delta-exchange-mcp login`, or open
`~/.delta-exchange-mcp/config.env` and fill in the three lines. Both write the same file
the form would have.

### The form saved my key, but the assistant still can't see my account

The form says it saved, and names a setting your client sets in its own configuration.

That is the whole failure, stated plainly: a value in your client's MCP entry — or in the
fields it asked you to fill in when you installed it — is read before the shared file and
wins over it. So the form verified your key, saved it correctly, and the server carries on
signing with whatever your client passes instead. Restarting does not help, because the
client passes its own value again every time it starts.

Clear those fields from that client's entry, then restart it. Which setting is at fault is
named in the message the form shows. `uvx delta-exchange-mcp login` runs the same check and
warns on stderr, though there it reports what your *shell* is setting, since that is the
environment it can see.

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

Or set it once for every client by adding `DELTA_MCP_DEBUG=1` to
[the shared file](#add-your-api-key).

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
[cursor-link]: https://cursor.com/install-mcp?name=delta-exchange-mcp&config=eyJjb21tYW5kIjoidXZ4IiwiYXJncyI6WyJkZWx0YS1leGNoYW5nZS1tY3AiXSwiZW52Ijp7IkRFTFRBX01DUF9FTlYiOiJpbmRpYV9wcm9kIiwiREVMVEFfQVBJX0tFWSI6IiIsIkRFTFRBX0FQSV9TRUNSRVQiOiIifX0%3D
[vs-code-badge]: https://img.shields.io/badge/VS_Code-Install_Server-0098FF?style=for-the-badge&logo=githubcopilot&logoColor=white
[vs-code-link]: https://insiders.vscode.dev/redirect/mcp/install?name=delta-exchange-mcp&config=%7B%22type%22%3A%22stdio%22%2C%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22delta-exchange-mcp%22%5D%2C%22env%22%3A%7B%22DELTA_MCP_ENV%22%3A%22%24%7Binput%3Adelta-env%7D%22%2C%22DELTA_API_KEY%22%3A%22%24%7Binput%3Adelta-api-key%7D%22%2C%22DELTA_API_SECRET%22%3A%22%24%7Binput%3Adelta-api-secret%7D%22%7D%7D&inputs=%5B%7B%22type%22%3A%22promptString%22%2C%22id%22%3A%22delta-api-key%22%2C%22description%22%3A%22Delta%20API%20key%20%28leave%20empty%20for%20market%20data%20only%29%22%2C%22password%22%3Atrue%7D%2C%7B%22type%22%3A%22promptString%22%2C%22id%22%3A%22delta-api-secret%22%2C%22description%22%3A%22Delta%20API%20secret%20%28must%20match%20the%20key%29%22%2C%22password%22%3Atrue%7D%2C%7B%22type%22%3A%22pickString%22%2C%22id%22%3A%22delta-env%22%2C%22description%22%3A%22Delta%20Exchange%20environment%22%2C%22options%22%3A%5B%22india_prod%22%2C%22india_testnet%22%5D%2C%22default%22%3A%22india_prod%22%7D%5D
[vs-code-insiders-badge]: https://img.shields.io/badge/VS_Code_Insiders-Install_Server-24bfa5?style=for-the-badge&logo=githubcopilot&logoColor=white
[vs-code-insiders-link]: https://insiders.vscode.dev/redirect/mcp/install?name=delta-exchange-mcp&config=%7B%22type%22%3A%22stdio%22%2C%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22delta-exchange-mcp%22%5D%2C%22env%22%3A%7B%22DELTA_MCP_ENV%22%3A%22%24%7Binput%3Adelta-env%7D%22%2C%22DELTA_API_KEY%22%3A%22%24%7Binput%3Adelta-api-key%7D%22%2C%22DELTA_API_SECRET%22%3A%22%24%7Binput%3Adelta-api-secret%7D%22%7D%7D&inputs=%5B%7B%22type%22%3A%22promptString%22%2C%22id%22%3A%22delta-api-key%22%2C%22description%22%3A%22Delta%20API%20key%20%28leave%20empty%20for%20market%20data%20only%29%22%2C%22password%22%3Atrue%7D%2C%7B%22type%22%3A%22promptString%22%2C%22id%22%3A%22delta-api-secret%22%2C%22description%22%3A%22Delta%20API%20secret%20%28must%20match%20the%20key%29%22%2C%22password%22%3Atrue%7D%2C%7B%22type%22%3A%22pickString%22%2C%22id%22%3A%22delta-env%22%2C%22description%22%3A%22Delta%20Exchange%20environment%22%2C%22options%22%3A%5B%22india_prod%22%2C%22india_testnet%22%5D%2C%22default%22%3A%22india_prod%22%7D%5D&quality=insiders
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
