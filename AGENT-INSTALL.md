# Install the Delta Exchange MCP server

Instructions for an AI agent that has been asked to install this server. Follow them in
order.

Nothing here is addressed to a person reading the file. The prompt that sends agents here
is in the [README](README.md#let-your-coding-agent-set-it-up) and on
[the docs site](https://mcp.delta.exchange/docs), and both point at this one copy, so an
agent follows the current steps rather than whatever was pasted months ago.

---

Install the Delta Exchange MCP server for me. Work out which case below you are in and do
that one. Do not ask me to choose, and do not ask me what I can see on screen.

STEP 0 — Check whether you can run commands and write files on this machine.

If you cannot, stop and tell me which of these applies:
* If I am in Claude Desktop, tell me to download and double-click this bundle. It needs no
  Python and no other tool — the app fetches everything:
  https://github.com/delta-exchange/delta-exchange-mcp/releases/latest/download/delta-exchange-mcp.mcpb
* Otherwise, tell me this server runs locally on my own machine, so it cannot be installed
  from this chat, and name what does work: Claude Desktop, or any coding agent that can
  edit files, such as Claude Code, Codex, Cursor or VS Code.
Do not improvise a config block for me to paste, and do not send me to a terminal.

STEP 1 — Make sure `uv` is available. Check with `uv --version`.

If it is missing, install it with my platform's package manager first — `brew install uv`,
`winget install astral-sh.uv`, or `pipx install uv`. Use the official installer at
https://astral.sh/uv/install.sh only if none of those exist, and tell me before you do.

STEP 2 — Add the server to my client's config.

Command `uvx`, one argument `delta-exchange-mcp`, local stdio, entry name
`delta-exchange-mcp`, no environment block. Leave my other servers untouched.

* If you are Claude Code and Claude Desktop is installed here, write Desktop's config file
  rather than using `claude mcp add`:
    macOS    ~/Library/Application Support/Claude/claude_desktop_config.json
    Windows  %APPDATA%\Claude\claude_desktop_config.json
    Linux    ~/.config/Claude/claude_desktop_config.json
  Both work, but Desktop's file is served by a client that can also draw the settings form
  in the chat, which the other one cannot.
* Any other client — Cursor, VS Code, Windsurf, Zed, Codex — write your own config file.
* If you do not know where my client keeps it, read this first:
  https://mcp.delta.exchange/llms-context.md

Check it resolves with `uvx delta-exchange-mcp --version`. Never run
`uvx delta-exchange-mcp` with no arguments — it serves stdio and will not exit.

STEP 3 — Set up my account now, without waiting for a restart.

Run `uvx delta-exchange-mcp setup` in the background. It prints a http://127.0.0.1 address
on its first line and opens it in my browser. Show me that address in case it did not open.

I fill in the page myself. Never ask me for my API key, and never accept one sent as a chat
message.

STEP 4 — Tell me to quit and reopen the app, and stop there.

Do not start a new chat. We carry on in this one.
