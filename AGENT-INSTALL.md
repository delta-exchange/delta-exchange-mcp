# Install the Delta Exchange MCP server

Use these instructions when a user asks you to install this server. Identify the MCP client
before you choose an installation method. Do not ask the user to choose a method when you can
identify the client yourself.

## 1. Check the client

If you cannot run commands or edit the MCP client configuration on this computer, use the
client's installation interface when one exists.

- For Claude Desktop, ask the user to download and open the current bundle:
  <https://github.com/delta-exchange/delta-exchange-mcp/releases/latest/download/delta-exchange-mcp.mcpb>
- For another chat-only client, explain that this server runs on the user's computer. The
  client must be able to launch a local stdio MCP server.

Never include actual credentials in a command or ask the user to edit a credential file.
The user can run the login CLI in their own terminal as described below.

## 2. Install the server entry

For a client that does not use the bundle, check for `uv` with `uv --version`. If it is
missing, use the platform package manager. Prefer `brew install uv` on macOS,
`winget install astral-sh.uv` on Windows, or `pipx install uv` when `pipx` is already
available. Tell the user before you run an official installer script.

Add this local stdio server to the current MCP client:

- Name: `delta-exchange-mcp`
- Command: `uvx`
- Arguments: `delta-exchange-mcp`
- Environment values: none

Keep every existing MCP server entry. If you do not know the client's configuration path,
read <https://mcp.delta.exchange/llms-context.md> before you edit anything.

Check the package command with:

```bash
uvx --refresh delta-exchange-mcp --version
```

Do not run `uvx delta-exchange-mcp` without an argument. A bare process serves MCP over
stdio and waits for a client.

## 3. Hand account connection to the user

Do not request, accept, paste, or store the user's Delta API key or secret. Do not put either
value in an MCP configuration, shell command, tool argument, chat message, or file.

Use the connected server to call `setup_credentials`, or ask it to connect the Delta
account. If the client has not loaded the new server entry, ask the user to restart that
client and continue in the same conversation. The server opens a short-lived Manage
Connection page. The user selects production or testnet and enters the credential directly
on that page.

If the client does not open the browser, show the clickable Manage Connection link from the
tool result. For an SSH or headless session, hand the user this command to run in their own
interactive terminal on the MCP machine:

```bash
uvx delta-exchange-mcp login
```

The CLI automatically chooses terminal entry when a browser is unavailable. `--no-browser`
or `--device` forces asterisk-masked terminal prompts; `--browser` forces the local browser page.
Let the user enter the secrets directly. Do not collect them through an agent tool or chat.
The user can supply the exact MCP client name reported by `get_connection_status`, either
at the terminal prompt or using `--client NAME`, if they want to approve trading.

Standalone login requires an unlocked native credential service, including Secret Service
on a headless Linux server. The CLI and MCP process must run as the same OS user and use the
same metadata location. If that service is unavailable, explain the requirement; do not
write a plaintext credential file.

After the user finishes, call `get_connection_status`. Report the environment, credential
source, validation state, account ID when present, client name, and trading state. Never ask
the user to send a credential so you can diagnose the result.

If the selected environment reports `reconnect_required`, open Manage Connection again.
This means the OS record comes from the earlier draft or another metadata location. The
server preserves that record but does not use it. Let the user enter the pair in the browser
or their own terminal and approve trading again. Do not try to copy the old record or request the pair in chat.

For changes to an existing connection, hand the user `uvx delta-exchange-mcp config`.
It manages environment, read/trade mode, saved credentials, disconnect, and client selection
from the same browser page or a terminal menu. Environment changes reuse a saved key for
that environment; the user only enters credentials when connecting or replacing a pair.
Terminal secrets appear as asterisks while typing. Changes apply immediately.

`config --env india_testnet` selects the saved testnet environment. `config --mode read
--client NAME` revokes approval for the exact client while retaining its credential.
`config --mode trade --client NAME` asks the user for approval in their own interactive
terminal; production requires a separate acknowledgement. Do not enter that confirmation
for the user. Environment changes are shared across clients at the same settings location,
while trading approval is scoped to the exact client binding.

## 4. Keep trading as a separate decision

The browser page and interactive terminal login offer trading after account connection. A
blocked real trading call can also open the page. Only the user enables trading. Terminal
approval requires an explicit `yes`, plus a separate production acknowledgement. Direct
credential arguments alone never enable trading.

Approval does not execute the pending trade. Retry the trading tool only after the
user asks to continue. A dry run needs no trading approval because it sends no mutation to
Delta.
