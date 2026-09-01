<div align="center">

<img src="https://raw.githubusercontent.com/delta-exchange/delta-exchange-mcp/d49dfba97e57120448bb4e0267abde6d7931e5f1/packaging/mcpb/icon.png" width="88" alt="Delta Exchange">

# delta-exchange-mcp

> Ask about Delta Exchange India in plain English from Claude or your editor.

![Status: Beta][beta-badge] [![PyPI version][pypi-version-badge]][pypi-version-link]

[![Download for Claude Desktop][claude-desktop-badge]][claude-desktop-link] [![Add to Cursor][cursor-badge]][cursor-link] [![Install in VS Code][vs-code-badge]][vs-code-link]

</div>

This is the official local MCP server for Delta Exchange India. Market data works without
an account connection. Account tools use a Delta API key. Trading tools need separate
browser approval.

> [!NOTE]
> This project is in beta. The tool definitions and setup flow can still change. Please
> [open an issue](https://github.com/delta-exchange/delta-exchange-mcp/issues) when a tool
> returns incorrect data or the connection page does not work in your MCP client.

## Install

For Claude Desktop, select **Download for Claude Desktop** above and open the downloaded
`.mcpb` file. The bundle asks for no API key, secret, environment, or trading mode. Claude
Desktop installs the local server and obtains `uv` and Python when needed.

For Cursor or VS Code, install [`uv`](https://docs.astral.sh/uv/getting-started/installation/)
and select the install button above. The generated server entry contains only this command:

```text
uvx delta-exchange-mcp
```

Other MCP clients are listed under [Install in your MCP client](#install-in-your-mcp-client).

## Connect your Delta account

After installation, tell the assistant:

```text
Connect my Delta Exchange account
```

The server calls `setup_credentials` with no arguments. It opens a short-lived Manage
Connection page in your browser. If the client cannot open it, the tool result includes a
clickable link.

On that page:

1. Choose production or testnet.
2. Enter the API key and secret for that environment.
3. Submit the connection.
4. Enable trading only if you want that MCP client to send real mutations.

The browser sends the credential directly to a loopback listener at `127.0.0.1`. The value
does not enter the conversation and is not an MCP tool argument. Do not send an API key or
secret in chat, even if an assistant asks for one.

Create a production key under [Delta Exchange API Keys][prod-keys] or a testnet key under
[Delta Exchange Testnet API Keys][testnet-keys]. Use only the permissions that the account
calls need. A real trading key must have trading permission and meet Delta's IP rules. The
current Delta documentation does not establish whether Read Data alone covers every account
endpoint. This project does not make that claim.

### Where the credential is stored

The server stores one credential record for production and one for testnet. It uses:

- macOS Keychain on macOS
- Windows Credential Manager on Windows
- Secret Service on Linux

The non-secret metadata file records the active revision, validation state, account ID,
timestamps, and revocation generation. It never contains the API key or secret.

Each metadata location uses separate OS credential records. Two clients that use the same
metadata location share its records. Copying metadata to a different location does not
connect that location to the existing account.

If no approved credential service is available, the server keeps the credential in memory
for that process. The connection and trading approval then end when the process stops. The
server does not use a plaintext fallback.

### Existing installations

On the first compatible start, the server checks the old
`~/.delta-exchange-mcp/config.env` file. If it finds a complete key and secret pair, it writes
the pair to the operating-system credential service and reads it back. It then removes only
the two secret lines. A failed migration leaves the file unchanged. The old trade-mode value
does not become trading approval.

If you used the earlier browser-authorization draft, reconnect once through Manage
Connection. The old OS record does not identify its metadata location, so the server cannot
confirm that it belongs to this installation. The server leaves that record unchanged and
saves the pair you enter as a separate OS record. Approve trading again if you need it.
Moving a metadata file to a different location also requires this reconnect.

A complete credential pair supplied by the MCP client's process environment remains
supported for compatibility. The status tool reports it as externally managed. The browser
cannot remove or replace that source. `india_devnet` accepts only this process-managed
credential source, and its trading approval lasts only for the current server process.

## What you can ask

```text
What is the current BTCUSD mark price and 24-hour range?
Show the options chain for BTC expiring this Friday.
What positions do I have open?
List my fills from the last 24 hours grouped by symbol.
How much USDT is free and how much is blocked in margin?
Place this order as a dry run.
```

The assistant selects the tool. You do not need to name one.

## Authorization behavior

The server always advertises the same market, account, export, status, and trading tools.
Connecting an account or enabling trading does not add or remove tools.

When a call needs input:

- An account call without a connection returns `input_required` and opens Manage Connection.
- A real trading call without approval returns `input_required` and opens the same page.
- A resumed request reports authorization state and never executes the blocked trade. Submit
  a new call only if the user still wants the trade.
- A call with `dry_run=true` sends no mutation and needs no trading approval.

Call `get_connection_status` to see the selected environment, credential source, validation
state, account ID when available, exact client name, and trading state. It never returns a
key, secret, signature, or credential digest.

### Trading approval

One approval enables all 13 trading tools for the exact client-provided name, selected
environment, and current credential revision. Approval persists across restarts when the
operating-system credential service is available. An unnamed client gets approval only for
the current process.

Production approval requires a separate acknowledgement that real orders can be placed.
The checkbox starts clear. Trading tools have no built-in notional or position-size cap.
Delta sizes orders in contracts, not coins.

Approval has no time expiry in this version. The server revokes it after credential rotation,
automatic migration, environment change, disconnect, manual disable, or a changed client
name. `DELTA_MCP_MODE=trade` has no authorization effect.

The client name is self-reported. It partitions consent records but does not authenticate a
local MCP client. A local client can copy another client's name. This threat is outside the
current design boundary. The operating-system user account is the security boundary.

### Mutation safeguards

- All 13 trading tools support `dry_run=true`.
- A real mutation checks current consent immediately before the request.
- A tool that performs a read preflight checks consent again after the preflight.
- `POST`, `PUT`, and `DELETE` requests are never retried automatically after a transport
  failure.
- Real and dry-run mutations are recorded in an owner-only audit log by default. Credential
  material and request signatures are not logged.

## Install in your MCP client

Every entry below starts the same local stdio server. Do not add an environment block for
credentials, environment selection, or trading mode. Manage those settings in the browser
after the server starts.

### Let your coding agent install it

Send the agent this instruction:

```text
Install the Delta Exchange MCP server by following
https://raw.githubusercontent.com/delta-exchange/delta-exchange-mcp/main/AGENT-INSTALL.md
Do not ask me for an API key or secret.
```

The canonical instructions are in [`AGENT-INSTALL.md`](AGENT-INSTALL.md).

### Cursor

[![Add to Cursor][cursor-badge]][cursor-link]

For a manual global entry, use `~/.cursor/mcp.json` on macOS or Linux, or
`%USERPROFILE%\.cursor\mcp.json` on Windows:

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

### VS Code

[![Install in VS Code][vs-code-badge]][vs-code-link]

For `.vscode/mcp.json`:

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

### Claude Code

```bash
claude mcp add delta-exchange-mcp --scope user -- uvx delta-exchange-mcp
```

### Codex

```bash
codex mcp add delta-exchange-mcp -- uvx delta-exchange-mcp
```

For the Codex desktop app, open **Plugins**, choose **MCPs**, and select **Connect to a
custom MCP**. Keep the type as STDIO. Use `uvx` as the command and
`delta-exchange-mcp` as the only argument. Leave environment values empty.

### Windsurf

Add the same `mcpServers` entry shown for Cursor to
`~/.codeium/windsurf/mcp_config.json` on macOS or Linux, or
`%USERPROFILE%\.codeium\windsurf\mcp_config.json` on Windows.

### Zed

Zed uses `context_servers` in `~/.config/zed/settings.json`:

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

## Safety

- The MCP transport is local stdio. There is no shared hosted Delta MCP endpoint.
- The browser setup listener binds to `127.0.0.1` on a random port. It closes after ten
  minutes, after the user enables trading, or when the MCP process stops.
- The listener checks the exact Host and Origin, requires JSON, limits the request body,
  uses an HTTP-only session cookie, and rotates a one-use CSRF value.
- Browser responses disable caching and framing and use a restrictive content security
  policy.
- The server serializes browser mutations. A stale page cannot replace a newer credential
  or restore revoked trading approval.

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

`uvx` caches resolved packages. Refresh the package, then reconnect the server or restart
the MCP client:

```bash
uvx --refresh delta-exchange-mcp --version
```

If the client pins an exact package version, update that version first.

## Troubleshooting

### The browser did not open

Ask the assistant to call `setup_credentials`. Open the clickable Manage Connection link in
the result. The link expires after ten minutes. Request a new link if it has expired.

### The connection is not persistent

Call `get_connection_status` and inspect the credential source. `process_memory` means the
server found no approved operating-system credential service. Install or unlock the native
credential service and reconnect. The server does not write a plaintext fallback.

### The browser cannot replace the credential

The status can report `process_environment`. This means the MCP client or its launcher
supplied the credential. Remove it from that external source, restart the server, and use
Manage Connection. The browser does not overwrite externally managed credentials.

### A trading call still reports input required

Approval binds to the exact client name, environment, and credential revision. Rotation,
migration, environment change, disconnect, manual disable, or a different client name ends
the old approval. Open Manage Connection and approve the current binding.

### An old installation has plaintext credentials

Start the current server once and call `get_connection_status`. A successful automatic
migration reports the operating-system credential source and removes only the key and secret
lines from the old file. If migration cannot complete, it leaves the file unchanged. The
server reports the problem and does not use the plaintext pair.

### `ModuleNotFoundError: No module named 'mcp.server.fastmcp'`

Version 0.4.1 and earlier allowed an incompatible MCP SDK release. Refresh to the current
package:

```bash
uvx --refresh delta-exchange-mcp --version
```

## Debugging

Set `DELTA_MCP_DEBUG=1` in the MCP process environment and restart the client. The server
writes each Delta request URL, response status, and response body under
`~/.delta-exchange-mcp/logs/`. Ask the assistant to call `get_debug_status` for the exact
path.

The log excludes API keys, secrets, signatures, and signing timestamps. Response bodies can
contain account data. Review a log before you share it.

## Development

Use Python 3.12 or later and Node.js 22 or later for development. The test suite uses
Node.js to parse the generated browser JavaScript. End users do not need Node.js.

```bash
uv sync --locked
uv run pytest
uv run ruff check src tests scripts packaging
bash scripts/inspect.sh --cli --method tools/list
bash scripts/inspect.sh --cli --method tools/call --tool-name get_ticker --tool-arg symbol=BTCUSD
```

To test an unreleased branch, use:

```bash
uvx --refresh --from git+https://github.com/delta-exchange/delta-exchange-mcp.git@develop \
  delta-exchange-mcp --version
```

Register an unreleased build under a different MCP server name so it does not replace the
published package during testing. Maintainers can find the release procedure in
[`RELEASING.md`](RELEASING.md).

## Feedback and issues

Please file bugs, missing tools, incorrect fields, and setup failures in
[GitHub Issues](https://github.com/delta-exchange/delta-exchange-mcp/issues). Redact API keys,
secrets, and account data from screenshots and logs.

---

<div align="center">

[MIT licence](https://github.com/delta-exchange/delta-exchange-mcp/blob/main/LICENSE) · [Source](https://github.com/delta-exchange/delta-exchange-mcp) · [Issues](https://github.com/delta-exchange/delta-exchange-mcp/issues) · [PyPI](https://pypi.org/project/delta-exchange-mcp/)

</div>

[beta-badge]: https://img.shields.io/badge/status-beta-orange?style=flat-square
[pypi-version-badge]: https://img.shields.io/pypi/v/delta-exchange-mcp?style=flat-square
[pypi-version-link]: https://pypi.org/project/delta-exchange-mcp/
[cursor-badge]: https://img.shields.io/badge/Cursor-Add_Server-0098FF?style=for-the-badge&logo=cursor&logoColor=white
[cursor-link]: https://cursor.com/install-mcp?name=delta-exchange-mcp&config=eyJjb21tYW5kIjoidXZ4IiwiYXJncyI6WyJkZWx0YS1leGNoYW5nZS1tY3AiXX0=
[vs-code-badge]: https://img.shields.io/badge/VS_Code-Install_Server-007ACC?style=for-the-badge&logo=visualstudiocode&logoColor=white
[vs-code-link]: https://insiders.vscode.dev/redirect/mcp/install?name=delta-exchange-mcp&config=%7B%22type%22%3A%22stdio%22%2C%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22delta-exchange-mcp%22%5D%7D
[claude-desktop-badge]: https://img.shields.io/badge/Claude_Desktop-Download-CC785C?style=for-the-badge&logo=anthropic&logoColor=white
[claude-desktop-link]: https://github.com/delta-exchange/delta-exchange-mcp/releases/latest/download/delta-exchange-mcp.mcpb
[prod-keys]: https://www.delta.exchange/app/account/manageapikeys
[testnet-keys]: https://demo.delta.exchange/app/account/manageapikeys
