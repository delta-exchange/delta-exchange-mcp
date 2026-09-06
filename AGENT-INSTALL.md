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

Do not give the user a credential command or a credential file to edit.

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
tool result. Do not replace this path with terminal or file-edit instructions.

After the user finishes, call `get_connection_status`. Report the environment, credential
source, validation state, account ID when present, client name, and trading state. Never ask
the user to send a credential so you can diagnose the result.

If the selected environment reports `reconnect_required`, open Manage Connection again.
This means the OS record comes from the earlier draft or another metadata location. The
server preserves that record but does not use it. Let the user enter the pair in the browser
and approve trading again. Do not try to copy the old record or request the pair in chat.

## 4. Keep trading as a separate decision

The browser page offers trading after account connection. A blocked real trading call can
also open the same page. Only the user enables trading.

Browser approval does not execute the pending trade. Retry the trading tool only after the
user asks to continue. A dry run needs no trading approval because it sends no mutation to
Delta.
