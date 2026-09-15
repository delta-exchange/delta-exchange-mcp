#!/usr/bin/env python
"""Single source of truth for the delta-exchange-mcp fine-tune Q&A dataset.

Holds every (question, answer) pair once and renders two artifacts:
  - delta-exchange-mcp-qna.md    human-readable, grouped by section
  - delta-exchange-mcp-qna.jsonl Claude messages-format, one pair per line

Questions follow Simplified Technical English (ASD-STE100): short, one idea,
active voice, approved verbs. Answers are comprehensive and grounded in the
recorded repository source commit. External client instructions cite their docs.

Run:  python finetune/generate_qna.py
"""

import json
import logging
from pathlib import Path

CONTRACT_ID = "delta.mcp-2026-browser-skills.v1"
SOURCE_COMMIT = "45855cf043340ba1df29dc52e7bfcc3fcb2b4f8c"
MCP_PROTOCOL_VERSION = "2026-07-28"
TOOL_SCHEMA_SHA256 = "c5bcfc41680e14b89a3e4bab4ea9357a63e3578dcbb5fdda33b30e723cda82bc"
TOOL_COUNT = 45

# System prompt prepended to every training record. Sets the assistant persona
# so the fine-tune answers stay in-domain and consistent.
SYSTEM = (
    "You are an expert on delta-exchange-mcp, the official Model Context Protocol "
    "(MCP) server for Delta Exchange India. You help users install, configure, "
    "authenticate, and use its market-data, account, and trading tools. Answer "
    "accurately and concisely, and never invent tools, parameters, or behavior "
    "that the server does not have. "
    f"This dataset describes the unreleased {CONTRACT_ID} contract at source commit "
    f"{SOURCE_COMMIT}. Do not claim that a published package has this contract."
)

# Each section is (title, [(question, answer), ...]).
SECTIONS: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "Overview and concepts",
        [
            (
                "What is delta-exchange-mcp?",
                "delta-exchange-mcp is the official Model Context Protocol (MCP) server for Delta "
                "Exchange India. It lets AI assistants such as Claude Desktop, Claude Code, Cursor, "
                "Zed, and Codex query Delta Exchange market data and your own account through "
                "standardized MCP tools. It wraps Delta Exchange India's REST API and exposes it as "
                "tools the assistant can call.",
            ),
            (
                "What is MCP?",
                "MCP (Model Context Protocol) is an open standard that connects AI assistants to "
                "external tools and data. The assistant discovers a set of tools from an MCP server, "
                "then calls them to read data or perform actions. delta-exchange-mcp is one such "
                "server: it presents Delta Exchange endpoints as tools that any MCP-capable client "
                "can use.",
            ),
            (
                "What do I get with this server?",
                "This development contract exposes 45 stable tools: 14 public market tools, 12 account tools, 13 trading tools, four setup and status tools, and two skill tools. Market data and written procedures need no key. Account calls require credentials. Real trading calls also require consent in Manage Connection. Tool discovery stays the same when authorization changes.",
            ),
            (
                "How many tools does the server have?",
                "The integrated development branch exposes 45 tools. The package's published version can have a different contract. Check the dataset contract marker before training and use tools/list to inspect the running server. Credentials, trading consent, and debug settings do not change this branch's tool list.",
            ),
            (
                "Which exchange does the server support?",
                "The server supports Delta Exchange India. Its API hosts are api.india.delta.exchange "
                "for production and the testnet host for demo. This is why the environment values are "
                "named india_prod, india_testnet, and india_devnet.",
            ),
            (
                "Is the server production-ready?",
                "This dataset describes an unreleased development contract. Source tests and bundle checks do not establish production readiness. A release still needs the operating-system matrix, real keyring checks, the authenticated testnet permission matrix, and acceptance in each supported MCP client. Check the published release and the running source ref before relying on a feature.",
            ),
            (
                "What framework does the server use?",
                "The server uses MCPServer from the mcp 2.x Python SDK. It supports MCP 2026 discovery and the legacy protocol paths supplied by that SDK. Each tool group registers decorated functions with the server before serving requests.",
            ),
            (
                "How is the server distributed?",
                "The server is distributed as the delta-exchange-mcp package on PyPI and as a Claude "
                "Desktop MCP bundle. Editor clients normally launch the PyPI package with uvx. The "
                "Claude Desktop bundle installs its own uv and Python runtime. There is no Docker image "
                "or hosted endpoint.",
            ),
            (
                "What transport does the server use?",
                "The server uses local stdio only. Your MCP client launches it as a subprocess and "
                "talks to it over standard input and output. There is intentionally no HTTP transport, "
                "no Docker image, and no shared hosted endpoint. Per-user API keys cannot safely route "
                "through a shared HTTP server, and users should be able to read the code that runs "
                "against their account.",
            ),
            (
                "Why is there no HTTP transport?",
                "Per-user API keys cannot safely route through a shared HTTP server, and the "
                "financial-tool nature of the server means each user should run and read the code that "
                "acts on their own account. So the server runs as a local stdio subprocess of your "
                "MCP client. Do not add streamable-http, a transport flag, or a Dockerfile without "
                "discussing it first.",
            ),
            (
                "Who is this server for?",
                "The server is for traders who want to query markets and their account through an AI "
                "assistant, for quants and developers who build on the tools, and for anyone who "
                "reconciles P&L or tax records from their own fills and transactions.",
            ),
            (
                "What is on the roadmap?",
                "This branch provides public market data, account reads, trading tools with dry-run, browser connection management, secure credential storage, and written procedures. It does not implement order-size caps or a separate confirmation for every order. Do not treat a proposed feature as an available control.",
            ),
            (
                "What license does the project use?",
                "The project ships a LICENSE file in the repository root. Check that file for the "
                "exact terms before you redistribute or build on the code.",
            ),
            (
                "Do I need an account to use market data?",
                "No. The 14 public market-data tools work with no API key. You need a Delta Exchange "
                "account and an API key only for the account read-only tools and the trading tools.",
            ),
        ],
    ),
    (
        "Install and setup",
        [
            (
                "What do I need before I install?",
                "For Claude Desktop, download and open the MCP bundle; it fetches uv and Python, so you "
                "do not install either one first. For an editor or a manual client configuration, "
                "install uv from the Astral docs so the client can launch the server with uvx.",
            ),
            (
                "How do I sanity-check the install?",
                "Run `uvx delta-exchange-mcp --help`. uvx resolves the package from PyPI or uses its "
                "cached resolution, then prints the CLI help. This confirms uv works and the package "
                "starts before you wire it into a client.",
            ),
            (
                "How do I add the server to Claude Code?",
                "Run `claude mcp add delta-exchange-mcp --scope user -- uvx delta-exchange-mcp`. This installs the published package for all projects. Verify with `claude mcp list`, then ask the assistant to connect your Delta account. Use the local Manage Connection page to enter credentials. A development contract requires an explicit source ref in the launch command.",
            ),
            (
                "How do I verify the server in Claude Code?",
                "Run `claude mcp list`. The command lists registered MCP servers so you can confirm "
                "delta-exchange-mcp is present. Use `/mcp` inside a session to view and reconnect it.",
            ),
            (
                "How do I add the server to Cursor?",
                "Edit `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` in the repo root "
                '(project-scoped) and add an mcpServers entry:\n\n```json\n{\n  "mcpServers": {\n    "delta-exchange-mcp": {\n      "command": "uvx",\n      "args": ["delta-exchange-mcp"]\n    }\n  }\n}\n```\n\n'
                "The README also has an Add to Cursor button. Keep credentials out of the normal "
                "install entry. Restart Cursor or open Settings then Tools & MCP to refresh, and then "
                "configure account access through a supported credential route.",
            ),
            (
                "How do I add the server to Codex?",
                "Run `codex mcp add delta-exchange-mcp -- uvx delta-exchange-mcp`, then verify with "
                "`codex mcp list`. A manual `~/.codex/config.toml` entry needs only command `uvx` and "
                "args `[\"delta-exchange-mcp\"]`. Leave its environment variables empty and configure "
                "account access after the server starts.",
            ),
            (
                "How do I add the server to OpenClaw?",
                "Run `openclaw mcp add delta-exchange-mcp --command uvx --arg "
                "delta-exchange-mcp`. Repeat `--arg` once per argument. OpenClaw writes the entry under "
                "`mcp.servers` in `~/.openclaw/openclaw.json`. Keep credentials out of the install "
                "command and configure account access after the server starts.",
            ),
            (
                "How do I add the server to Windsurf?",
                "Edit `~/.codeium/windsurf/mcp_config.json` (macOS/Linux) or the Windows equivalent "
                "under `%USERPROFILE%`. Use an mcpServers entry with command `uvx` and args "
                "`[\"delta-exchange-mcp\"]`; do not add a credential env block. The UI route is Settings "
                "then Cascade then Plugins (MCP servers) then Manage Plugins then View raw config.",
            ),
            (
                "How do I add the server to Zed?",
                "Edit `~/.config/zed/settings.json` (user) or `.zed/settings.json` (project). Zed uses "
                'the top-level key `context_servers`:\n\n```json\n{\n  "context_servers": {\n    "delta-exchange-mcp": {\n      "command": "uvx",\n      "args": ["delta-exchange-mcp"]\n    }\n  }\n}\n```\n\n'
                "The entry itself uses the usual flat command and args fields. Configure credentials "
                "after the server starts.",
            ),
            (
                "How do I add the server to Google Antigravity?",
                "Add an mcpServers entry with command uvx and args [\"delta-exchange-mcp\"] to the global `~/.gemini/config/mcp_config.json` or the workspace `.agents/mcp_config.json`. In Antigravity IDE, open MCP Servers, Manage MCP Servers, then View raw config. In the CLI, use `/mcp` to inspect or reload. Keep credentials out of the entry. These paths follow the current [Google Antigravity MCP documentation](https://antigravity.google/docs/mcp). Use the reviewed source ref to test this unreleased contract.",
            ),
            (
                "How do I add the server to VS Code with GitHub Copilot?",
                "Add a stdio server to `.vscode/mcp.json` under `servers` with command `uvx` and args `[\"delta-exchange-mcp\"]`. Keep credentials out of this entry. Start the server, then ask to connect your Delta account and open Manage Connection. The published package must support this browser flow; pin the reviewed development ref when testing unreleased behavior.",
            ),
            (
                "How do I add the server to Claude Desktop?",
                "Open the matching MCPB release asset in Claude Desktop. The bundle installs its own uv and Python runtime. This branch's manifest does not request an API key, secret, environment, or trading mode. Connect through Manage Connection after installation. Use a bundle built from the same development contract when testing an unreleased branch.",
            ),
            (
                "Where is the Claude Desktop config file?",
                "macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`. Windows: "
                "`%APPDATA%\\Claude\\claude_desktop_config.json`. Linux: "
                "`~/.config/Claude/claude_desktop_config.json`. Open it from Settings then Developer "
                "then Edit config, or edit the path directly.",
            ),
            (
                "Are the API key and secret required in the config?",
                "No. The install entry starts the server with public data and a stable tool list. For account calls, open Manage Connection and enter the key and secret there. A complete process-environment credential pair remains a compatibility source, but the browser cannot rotate or remove that source. Do not paste credentials into the conversation or shared settings file.",
            ),
            (
                "How do I run the testnet instead of production?",
                "Open Manage Connection, select the practice environment `india_testnet`, and connect a key created at demo.delta.exchange. You can also pin `DELTA_MCP_ENV=india_testnet` in the MCP client's process environment. A pinned environment takes precedence over browser selection. A production key does not work on testnet.",
            ),
            (
                "How do I pin a specific version?",
                "Pin a published package with `uvx delta-exchange-mcp==<version>`, replacing `<version>` with the intended release. This dataset describes an unreleased contract, so no published version is implied by its metadata. To reproduce it, use the recorded source commit in a git-based uvx launch. Use `--refresh` when you need uv to refresh a cached resolution.",
            ),
            (
                "How do I run an unreleased branch or fork?",
                "Launch `uvx --from git+https://github.com/delta-exchange/delta-exchange-mcp@<ref> delta-exchange-mcp`, replacing `<ref>` with a reviewed branch or immutable commit. Restart the server process after changing the launch entry. uv can cache a resolved ref; use its refresh option when checking new commits on the same branch.",
            ),
            (
                "How do I pick up new commits on the same dev branch?",
                "Add `--refresh` to the uvx command, because uv caches the git resolution. Example: "
                "`uvx --refresh --from git+https://github.com/delta-exchange/delta-exchange-mcp.git@develop delta-exchange-mcp --help`.",
            ),
            (
                "How do I keep a dev server separate from the release?",
                "Register the dev server under a different name, such as delta-exchange-mcp-dev, so it "
                "does not collide with the PyPI install. Use the git+URL form in its args and keep "
                "`uvx delta-exchange-mcp` for everyday use.",
            ),
            (
                "How do I update to the latest version?",
                "1. If your config pins a version, bump the pin or drop it to float to latest. "
                "2. Refresh the uvx cache with `uvx --refresh delta-exchange-mcp --help`. "
                "3. Reload the server so the client respawns the process: in Claude Code run `/mcp` and "
                "reconnect, or restart the client. New tools appear only after the respawn.",
            ),
            (
                "Why does a new release not appear automatically?",
                "uvx caches the resolved package, so a new PyPI release is not picked up on the next "
                "launch. Run `uvx --refresh delta-exchange-mcp --help` to fetch the new build, then "
                "reload the server in your client.",
            ),
            (
                "Does the list_changed notification update the package version?",
                "No. A protocol notification does not replace the running Python package. Update the launch source or cached package and restart the server process. This branch keeps its tool list stable across credential and consent changes.",
            ),
            (
                "What does --scope user do in Claude Code?",
                "`--scope user` registers the server for all your projects, not just the current one. "
                "Use it when you want delta-exchange-mcp available everywhere. Verify the registration "
                "with `claude mcp list`.",
            ),
            (
                "How do I install dependencies for development?",
                "Clone the repo and run `uv sync`. It installs runtime and dev dependencies. Rerun "
                "`uv sync` after you change pyproject.toml or entry points, because uv run caches the "
                "build.",
            ),
            (
                "How do I run the server from source?",
                "Run `uv run delta-exchange-mcp` from the repo. It starts the server over stdio, the "
                "only transport. For a live check against your DELTA_MCP_ENV, run "
                "`uv run python scripts/smoke.py`.",
            ),
            (
                "Which clients does the server support?",
                "Any compatible local stdio MCP client can call the tools. Setup uses URL elicitation when supported, an MCP App that asks the host to open a link, or a clickable text link. Browser or host acceptance must be tested for the exact client version; protocol support alone does not prove that each client renders the setup flow.",
            ),
        ],
    ),
    (
        "Authentication and API keys",
        [
            (
                "How do I create an API key?",
                "Create a key at delta.exchange/app/account/manageapikeys for production, or at "
                "demo.delta.exchange for testnet. Both the api_key and the api_secret are shown once "
                "at creation. Save the secret immediately.",
            ),
            (
                "Can I recover a lost API secret?",
                "Delta shows the secret when it creates the key. If you lose it, create a new pair and replace the stored pair in Manage Connection. If the MCP client's process environment supplies credentials, update that client configuration and restart its server process. The browser cannot change an externally managed source.",
            ),
            (
                "Which permission does the API key need?",
                "The key needs permission for each endpoint that you call. Account identity validation uses GET /v2/users/trading_preferences. Current repository evidence does not establish that Read Data alone permits that endpoint. Real trading requires Trading permission. The release check uses separate Read Data and Trading testnet keys and records each endpoint's result.",
            ),
            (
                "Should I whitelist my IP on the key?",
                "A read-only key needs no IP whitelist. Delta requires an IP whitelist when the key has "
                "Trading permission. If a key carries a whitelist, Delta blocks requests from other "
                "addresses and the server reports the IP that Delta saw so you can correct the key.",
            ),
            (
                "How do I match the key to the environment?",
                "Select production for a production key and testnet for a demo key in Manage Connection. The process setting DELTA_MCP_ENV can fix the environment and takes precedence over browser selection. A key from one environment does not authenticate to another.",
            ),
            (
                "Why do prod and testnet keys not interchange?",
                "API keys are scoped to the environment they are created in. A prod key from "
                "delta.exchange works only against india_prod; a demo key from demo.delta.exchange "
                "works only against india_testnet. A mismatch returns InvalidApiKey.",
            ),
            (
                "How does the server sign requests?",
                "The server signs each authenticated request with HMAC-SHA256. It concatenates method, "
                "timestamp, path, query, and body into the signing payload, then signs it with your "
                "api_secret. The signing path must include the /v2 prefix, which the client adds; "
                "callers pass relative paths like /orders.",
            ),
            (
                "What is the signature timestamp window?",
                "Delta accepts a signature timestamp within about 5 seconds of its server clock. If "
                "your system clock drifts past that window, the request fails with SignatureExpired. "
                "Sync your clock via NTP to fix it.",
            ),
            (
                "How does the server sign a POST body?",
                "The signed body must be the exact bytes sent on the wire. The client serializes the "
                "JSON body once with compact separators, signs that string, and sends the same string. "
                "It never re-serializes, because different spacing would break the signature.",
            ),
            (
                "Why does the server send a User-Agent header?",
                "Delta requires a User-Agent header. A missing one returns HTTP 403. The server always "
                "sets it, and you should not remove it.",
            ),
            (
                "Do my API keys leave my machine?",
                "The secret stays in the local signing process and the native credential service, or process memory when no approved service is available. Authenticated requests send the API key and an HMAC signature to the selected Delta API. They do not send the API secret. The local MCP client and operating-system user are trusted by this design.",
            ),
            (
                "Does the AI model ever see my credentials?",
                "The supported browser flow does not pass typed credentials through MCP tool arguments, tool results, or model context. setup_credentials has no key or secret parameter. Enter credentials only in Manage Connection. This assumes a trusted local MCP client; the server does not provide independent authentication of a client that holds the connection URL.",
            ),
            (
                "How do I register the account read-only tools?",
                "Account tools are registered before the server starts, even without credentials. An unauthorized account call returns a connection request. Open Manage Connection and connect the correct environment, then make a new account call. You do not register a second set of tools or enable DELTA_MCP_MODE.",
            ),
            (
                "What does setup_credentials do?",
                "setup_credentials starts the local Manage Connection flow. It has no API key or secret arguments. The server uses URL elicitation when the client supports it, an MCP App that opens the URL, or a clickable link. The page manages credentials, environment selection, and trading consent outside model context.",
            ),
            (
                "Can the assistant call save_credentials or save_mode?",
                "No. save_credentials and save_mode are removed from this development contract. The browser sends its actions directly to the loopback listener. The assistant calls setup_credentials or follows the authorization request from an account or trading tool.",
            ),
            (
                "What happens without credentials?",
                "All 45 tools remain discoverable. Public market calls, written procedures, status calls, and trading dry runs work without credentials. An account call requests connection. A real trading call also needs consent for the exact client name, environment, and credential revision.",
            ),
        ],
    ),
    (
        "Security and safety",
        [
            (
                "Is the server read-only by default?",
                "Real trading starts disabled because there is no consent. The trading tools are still visible and support dry_run=true. Account reads need credentials; public market data does not. Enabling consent allows real mutations, so inspect the requested action and account before enabling it.",
            ),
            (
                "How do I enable trading?",
                "Open Manage Connection, select the environment, and connect the account. Enable trading for the exact MCP client name. Production also requires the unchecked real-orders acknowledgement. The server then checks consent on each real mutation. A resumed authorization request only reports status; submit a new trade call after authorization. DELTA_MCP_MODE never grants consent.",
            ),
            (
                "What is dry run?",
                "Pass dry_run=true to a trading tool to validate and return the request payload without a POST, PUT, or DELETE. Dry runs require no credential or trading consent. A dry-run result does not prove that the exchange accepts the same live order.",
            ),
            (
                "What does the audit log record?",
                "The audit log records real and dry-run trading attempts that reach the shared execution function. Each JSON line includes the environment, tool, request parameters, dry-run flag, and a summarized result or error. It excludes authentication headers and secrets. Logging is on by default and uses an owner-only file. A local write failure is reported to stderr, so the log is a best-effort record.",
            ),
            (
                "Where is the audit log?",
                "The default audit path is `~/.delta-exchange-mcp/audit/audit-<environment>-<timestamp>-<pid>.log`. The server creates it with owner-only permissions. DELTA_MCP_AUDIT_FILE overrides the path. get_trading_status reports audit information for the selected environment.",
            ),
            (
                "How do I disable the audit log?",
                "Set DELTA_MCP_AUDIT to off, false, 0, or no to disable audit logging. Otherwise the execution function records both real and dry-run trading attempts. The environment variable DELTA_MCP_MODE does not control authorization or enable this log.",
            ),
            (
                "Does the server retry a failed mutation?",
                "The server never automatically retries a POST, PUT, or DELETE. A lost response, server failure, or malformed mutation response can have an unknown outcome. Use the state checks named in the error to establish what happened before another attempt. A connection failure can report that nothing was sent. Automatic retries are limited to GET requests.",
            ),
            (
                "Can the server withdraw funds?",
                "No. The server has no withdrawal functionality. Its trading tools place, edit, and "
                "cancel orders and manage positions and margin, but they cannot move funds off the "
                "exchange.",
            ),
            (
                "What must I redact before I share logs?",
                "Redact api_key and api_secret from any logs or screenshots. The debug log never "
                "contains credentials or signatures, but response bodies contain your account data "
                "such as balances, positions, and transactions. Review before sharing.",
            ),
            (
                "Why should I read the code?",
                "This is a financial-tool MCP that acts against your account. Read the code so you know "
                "exactly what runs. The local-only, no-hosted-endpoint design exists so you can audit "
                "the code that uses your keys.",
            ),
            (
                "How does the server protect the CSV export path?",
                "The bulk_fills_export tool restricts output_path to the current working directory or "
                "your home directory, and expands `~`. It resolves the path and rejects anything "
                "outside those roots. This guards against path traversal and unexpected writes.",
            ),
            (
                "Are mutations auditable in dry-run too?",
                "Yes. The audit log records dry-run calls as well as real ones, each marked as a dry "
                "run. This gives you a full record of what the assistant tried, whether or not it was "
                "sent.",
            ),
        ],
    ),
    (
        "Environment variables",
        [
            (
                "What does DELTA_MCP_ENV do?",
                "DELTA_MCP_ENV selects the Delta environment. Valid values are india_prod, "
                "india_testnet, and india_devnet. The default is india_prod, because users who ask for "
                "a price usually mean production.",
            ),
            (
                "What does DELTA_API_KEY do?",
                "DELTA_API_KEY supplies an externally managed compatibility credential from the MCP client's process environment. It must have a matching DELTA_API_SECRET. The browser cannot replace or remove a credential supplied this way. Prefer the native credential service through Manage Connection for normal setup.",
            ),
            (
                "What does DELTA_API_SECRET do?",
                "DELTA_API_SECRET is the signing secret paired with DELTA_API_KEY in the MCP client's process environment. A partial pair fails closed for account access. A complete process pair takes precedence over a stored pair and supports consent only for that server process. Never put the secret in a prompt or log.",
            ),
            (
                "What does DELTA_MCP_MODE do?",
                "DELTA_MCP_MODE is ignored by the current authorization contract. It does not expose or hide tools and never authorizes a real trade. Enable trading through Manage Connection for the exact client, environment, and credential revision.",
            ),
            (
                "What does DELTA_MCP_DEBUG do?",
                "DELTA_MCP_DEBUG turns on debug logging. Set it to 1, true, yes, or on to write HTTP "
                "request URLs and response bodies to a log file. It is unset by default.",
            ),
            (
                "What does DELTA_MCP_DEBUG_FILE do?",
                "DELTA_MCP_DEBUG_FILE overrides the debug log path. The default is "
                "`~/.delta-exchange-mcp/logs/debug-<timestamp>-<pid>.log`.",
            ),
            (
                "What does DELTA_MCP_AUDIT do?",
                "DELTA_MCP_AUDIT controls the audit log for trading attempts, including dry runs. It is on by default. Set off, false, 0, or no to disable it. Audit configuration does not grant trading consent.",
            ),
            (
                "What does DELTA_MCP_AUDIT_FILE do?",
                "DELTA_MCP_AUDIT_FILE overrides the audit log path. The default is `~/.delta-exchange-mcp/audit/audit-<environment>-<timestamp>-<pid>.log`. Each record uses the environment pinned to the request.",
            ),
            (
                "What does DELTA_MCP_CONFIG_FILE do?",
                "DELTA_MCP_CONFIG_FILE selects the non-secret shared settings file. Its default is `~/.delta-exchange-mcp/config.env`. The browser can write environment selection there. Credential secrets belong in the approved native credential service or process memory. A complete legacy key pair can be migrated, but legacy trading mode never becomes consent.",
            ),
            (
                "Which environment variables are required?",
                "No environment variable is required for public market data or tool discovery. Use Manage Connection for account credentials and trading consent. A complete DELTA_API_KEY and DELTA_API_SECRET pair is an optional process compatibility source. Do not use DELTA_MCP_MODE to authorize trading.",
            ),
            (
                "What is the default environment?",
                "The default environment is india_prod. A process override or a browser-saved environment selection can change it. All 45 tools remain discoverable. A real mutation requires credentials and trading consent; no initial mode setting authorizes it.",
            ),
        ],
    ),
    (
        "Market-data tools",
        [
            (
                "What does list_products do?",
                "list_products lists tradable products on Delta Exchange with optional filters and "
                "returns a paginated result plus meta cursors. Filter by contract_types "
                "(perpetual_futures, call_options, put_options, futures, spot), by states (live, "
                "upcoming, expired, settled), or by expiry in YYYY-MM-DD. Page with page_size (1-500, "
                "default 100) and the after cursor.",
            ),
            (
                "What does get_product do?",
                "get_product returns full product details for one symbol, such as BTCUSD or an option "
                "symbol like C-BTC-66400-010824. Pass the symbol as the only argument.",
            ),
            (
                "What does get_ticker do?",
                "get_ticker returns the 24-hour ticker for one symbol: last price, volume, open "
                "interest, and mark and spot price. Pass the symbol, for example BTCUSD.",
            ),
            (
                "What does list_tickers do?",
                "list_tickers returns tickers across many products. Filter by contract_types "
                "(perpetual_futures, futures, call_options, put_options) and by "
                "underlying_asset_symbols such as BTC, ETH, or SOL.",
            ),
            (
                "What does get_orderbook do?",
                "get_orderbook returns an L2 orderbook snapshot for a symbol: bid and ask depth. Set "
                "depth to choose levels per side, up to 100.",
            ),
            (
                "What does get_recent_trades do?",
                "get_recent_trades returns the recent public trades for a symbol. Pass the symbol as "
                "the only argument.",
            ),
            (
                "What does get_candles do?",
                "get_candles returns OHLC candles for a symbol. Pass symbol, resolution (1m, 3m, 5m, "
                "15m, 30m, 1h, 2h, 4h, 6h, 1d, 1w), and start and end as inclusive Unix timestamps in "
                "seconds. For funding, mark, or open-interest history use the dedicated tools instead.",
            ),
            (
                "What resolutions does get_candles accept?",
                "get_candles accepts 1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 6h, 1d, and 1w. The same "
                "resolution set applies to get_funding_history, get_mark_price_history, and "
                "get_oi_history.",
            ),
            (
                "What does get_funding_history do?",
                "get_funding_history returns historical funding-rate candles for a perpetual, as OHLC "
                "over the funding rate. Pass a perpetual symbol such as BTCUSD, a resolution "
                "(default 1h), and start and end timestamps. Use it for basis-trade analysis or to "
                "compute realized funding over a holding period.",
            ),
            (
                "What does get_mark_price_history do?",
                "get_mark_price_history returns historical mark-price candles for a product. Pass the "
                "symbol, a resolution (default 1m), and start and end timestamps. Use it to "
                "reconstruct P&L curves or compare your fill price to fair value.",
            ),
            (
                "What does get_oi_history do?",
                "get_oi_history returns historical open-interest candles for a product, as OHLC over "
                "open interest. Pass the symbol, a resolution (default 1h), and start and end "
                "timestamps. Use it to detect positioning extremes or OI build-up around events.",
            ),
            (
                "What does get_options_chain do?",
                "get_options_chain returns all call and put tickers for one underlying on one expiry. "
                "Pass underlying (for example BTC or ETH) and expiry_date. Note the expiry format is "
                "DD-MM-YYYY here, which differs from the YYYY-MM-DD used by list_products.",
            ),
            (
                "What does get_settlement_prices do?",
                "get_settlement_prices returns historical settlement prices for expired or settled "
                "derivatives, paginated. Each product carries settlement_time and settlement_price. "
                'Under the hood it is list_products(states=["expired"]). Use it for post-expiry P&L '
                "reconciliation or backtesting against realized settlements.",
            ),
            (
                "What does get_indices do?",
                "get_indices returns the spot price indices Delta builds by combining prices from "
                "several exchanges. Each index returns its constituent exchanges and weights, its "
                "index_type (spot_pair, fixed_interest_rate, or floating_interest_rate), tick_size, and "
                "the underlying and quoting asset. Use it to audit how a mark or settlement price is "
                "built.",
            ),
            (
                "What does get_reference_data do?",
                "get_reference_data returns a merged assets and indices listing, useful for symbol and "
                "asset metadata lookups. For index-only queries such as composition or weights, prefer "
                "get_indices.",
            ),
            (
                "How do I get the mark price and 24h range for BTCUSD?",
                "Ask the assistant for the BTCUSD mark price and 24h range. It calls get_ticker with "
                "symbol BTCUSD, which returns last price, 24-hour stats, mark price, and open interest. "
                "You do not name the tool; the assistant picks it.",
            ),
            (
                "How do I see the option chain for a Friday expiry?",
                'Ask for the options chain for the underlying and expiry, for example "the BTC options '
                'chain for this Friday." The assistant calls get_options_chain with underlying BTC and '
                "the expiry_date in DD-MM-YYYY.",
            ),
            (
                "How do I list only live perpetuals?",
                "Ask for live perpetual products. The assistant calls list_products with "
                'contract_types=["perpetual_futures"] and states=["live"], then pages with the after '
                "cursor if there are more than page_size results.",
            ),
        ],
    ),
    (
        "Account read-only tools",
        [
            (
                "What does get_positions do?",
                "get_positions returns open position(s). Pass exactly one of product_id or "
                "underlying_asset_symbol. It returns only entry_price and size. For analytical fields "
                "such as unrealized_pnl, margin, mark_price, and liquidation_price, use "
                "get_margined_positions instead.",
            ),
            (
                "What does get_margined_positions do?",
                "get_margined_positions returns all open margined positions, optionally filtered by "
                "product_ids (max 10) or contract_types. It includes size (signed: positive long, "
                "negative short), entry and mark price, margin, and unrealized P&L. It also fixes the "
                "short-option P&L bug client-side.",
            ),
            (
                "How do I compute notional exposure for a position?",
                "Use notional_usd = abs(size) * contract_value * index_price. Use index_price, the spot "
                "of the underlying, not mark_price. For an option, mark_price is the premium, so "
                "multiplying by it gives the premium value, not the underlying exposure. Example: a "
                "short BTC call with size 10, contract_value 0.001, and BTC index 54270 has notional "
                "10 * 0.001 * 54270 = $542.70.",
            ),
            (
                "Why does the server patch short-option P&L?",
                "The upstream API returns an unsigned unrealized_pnl (the premium value) for short "
                "option positions, ignoring direction. get_margined_positions recomputes the signed "
                "P&L client-side as (mark_price - entry_price) * size * contract_value, with size "
                "signed. Futures and long options pass through unchanged. This is GitHub issue #9.",
            ),
            (
                "What does get_wallet_balances do?",
                "get_wallet_balances returns balances across all assets. Fields: asset_symbol, balance, "
                "available_balance, position_margin, and strategy_blocked_amount.",
            ),
            (
                "What is strategy_blocked_amount?",
                "strategy_blocked_amount is collateral reserved by an active Algo Marketplace strategy "
                "subscription. It is normal and expected, not a risk or anomaly. To release it, stop "
                "or unsubscribe from the strategy.",
            ),
            (
                "What does get_wallet_transactions do?",
                "get_wallet_transactions returns paginated wallet transaction history with microsecond "
                "timestamps. Filter by asset_ids, transaction_types (deposit, withdrawal, funding, "
                "settlement, commission, and more), and a time window. Default page_size is 50 (max "
                "200).",
            ),
            (
                "What does get_fills do?",
                "get_fills returns your executed trade fills, paginated, with microsecond timestamps. "
                "Filter by product_ids, contract_types, and a time window. Default page_size is 50 "
                "(max 200). For full-history analysis prefer bulk_fills_export.",
            ),
            (
                "Why does a fills or transactions query look empty?",
                "If you omit start_time_us, the API returns only the last ~90 days, so older records "
                "are not included and a short result is not proof that nothing exists. Pass an explicit "
                "start_time_us in microseconds to reach older history. When you omit it, the result "
                "carries a notice field saying so. This is GitHub issue #18.",
            ),
            (
                "What time unit do the account tools use?",
                "The account history tools use microseconds epoch, not milliseconds. start_time_us and "
                "end_time_us are microseconds. This applies to get_fills, get_wallet_transactions, "
                "get_order_history, and bulk_fills_export.",
            ),
            (
                "What does get_open_orders do?",
                "get_open_orders returns current open and pending orders, paginated via meta.after and "
                "meta.before. Filter by product_ids (max 10), states (open, pending), and "
                "contract_types. Default page_size is 50 (max 200).",
            ),
            (
                "What does get_order_history do?",
                "get_order_history returns closed and cancelled orders, filterable and paginated, with "
                "microsecond timestamps. Filter by product_ids, contract_types, and order_types "
                "(market, limit, stop_market, stop_limit, all_stop) plus a time window.",
            ),
            (
                "What does get_order_by_id do?",
                "get_order_by_id fetches a single order. Pass exactly one of order_id (the "
                "Delta-assigned id) or client_order_id (your own id). It resolves the correct endpoint "
                "for each.",
            ),
            (
                "What does get_product_leverage do?",
                "get_product_leverage returns the configured order leverage for a product. Pass the "
                "product_id. This is a read; to change leverage you need the trading tool "
                "set_product_leverage.",
            ),
            (
                "What does get_trading_stats do?",
                "get_trading_stats returns account-level trading volume and statistics. It takes no "
                "arguments.",
            ),
            (
                "What does get_trading_preferences do?",
                "get_trading_preferences returns account trading preferences from GET /v2/users/trading_preferences. It takes no arguments and requires account authorization. The shared identity check also validates the integer user_id in this endpoint's response.",
            ),
            (
                "What does get_profile do?",
                "get_profile is retired and is not registered. Read account trading preferences with get_trading_preferences. Credential validation and close_all_positions use the shared identity check for the integer user_id from GET /v2/users/trading_preferences. The server does not call GET /v2/profile for an API key.",
            ),
            (
                "What does bulk_fills_export do?",
                "bulk_fills_export writes your fills to a CSV file on disk and returns "
                "{path, row_count, size_bytes}. Use it for full-history analysis, tax reports, or "
                "backtesting, where paginated get_fills would need many round-trips. output_path must "
                "be inside the current working directory or home directory.",
            ),
            (
                "How do I export a full year of fills for tax?",
                "Call bulk_fills_export with an explicit start_time_us and usually end_time_us in "
                "microseconds. Without start_time_us the export covers only the last ~90 days and "
                "silently misses older trades. Set output_path inside your cwd or home directory.",
            ),
            (
                "How do I get unrealized P&L for my positions?",
                "Use get_margined_positions, not get_positions. get_positions returns only entry_price "
                "and size, while get_margined_positions returns unrealized_pnl, margin, mark_price, and "
                "liquidation_price, with short-option P&L corrected.",
            ),
        ],
    ),
    (
        "Trading tools (opt-in)",
        [
            (
                "When do the trading tools register?",
                "All 13 trading tools register before serving requests. Their presence in tools/list does not authorize a trade. The server allows dry runs without consent and requires a current consent check immediately before a real mutation, including after any preflight request.",
            ),
            (
                "What does place_order do?",
                "place_order places a single order. Pass size, side (buy or sell), order_type "
                "(limit_order or market_order), and exactly one of product_id or product_symbol. "
                "limit_price is required for limit_order and rejected on market_order. For stop orders "
                "set stop_order_type and stop_price or trail_amount. You can attach a bracket with the "
                "bracket_* params.",
            ),
            (
                "What does edit_order do?",
                "edit_order edits an open order. Pass id, the new total size, and exactly one of "
                "product_id or product_symbol. You can update limit_price, stop_price, trail_amount, "
                "and post_only. order_type cannot change on an edit. Prices are rounded to the "
                "product's tick.",
            ),
            (
                "What does cancel_order do?",
                "cancel_order cancels a single order. Pass product_id plus exactly one of id or "
                "client_order_id.",
            ),
            (
                "What does cancel_all_orders do?",
                "cancel_all_orders cancels open orders. With no filters it cancels ALL of your open "
                "orders. Narrow it with product_id or contract_types, and with the "
                "cancel_limit_orders, cancel_stop_orders, and cancel_reduce_only_orders flags. When you "
                'set none of the three flags, the tool defaults all three to true so "cancel all" '
                "actually cancels everything.",
            ),
            (
                "What does place_batch_orders do?",
                "place_batch_orders places up to 50 orders on one contract in a single request. Pass "
                "orders as a list, each {size, side, order_type, limit_price?, time_in_force?, "
                "post_only?, client_order_id?}, plus one of product_id or product_symbol. All orders "
                "must be on the same contract. IOC and stop orders are not allowed in a batch, and "
                "each client_order_id must be unique within the batch.",
            ),
            (
                "What does edit_batch_orders do?",
                "edit_batch_orders edits up to 50 orders on one contract in a single request. Pass "
                "orders as a list, each {id, size, order_type, limit_price?, post_only?}, plus one of "
                "product_id or product_symbol.",
            ),
            (
                "What does cancel_batch_orders do?",
                "cancel_batch_orders cancels up to 50 orders on one contract in a single request. Pass "
                "orders as a list, each {id} or {client_order_id}, plus one of product_id or "
                "product_symbol.",
            ),
            (
                "What is the batch size limit?",
                "The batch limit is 50 orders per request, for place_batch_orders, edit_batch_orders, "
                "and cancel_batch_orders. A larger list is rejected with a clear error before any call "
                "is sent. All orders in a batch must be on the same contract.",
            ),
            (
                "How does a batch report a partial failure?",
                "Delta's batch endpoints return only the processed orders with no per-index error. When "
                "fewer orders come back than were sent, the tool attaches a partial_failure block with "
                "requested, succeeded, dropped counts, and the dropped ids or client_order_ids it can "
                "identify. It still returns the orders that succeeded. This is BUG-2.",
            ),
            (
                "What does place_bracket_order do?",
                "place_bracket_order attaches a take-profit and stop-loss bracket to a position. Pass "
                "one of product_id or product_symbol and at least one of stop_loss_order or "
                "take_profit_order. These legs are not editable via edit_bracket_order; cancel and "
                "re-place to change them.",
            ),
            (
                "What does edit_bracket_order do?",
                "edit_bracket_order edits the bracket TP/SL params on an existing order. id is the "
                "entry-order id, an order created with bracket_* params (for example via place_order "
                "with bracket_take_profit_price). It does not accept the leg ids of a position bracket "
                "created by place_bracket_order.",
            ),
            (
                "What is the difference between the two bracket tools?",
                "place_bracket_order attaches a bracket to an open position; those legs are not "
                "editable and you cancel and re-place to change them. An entry-order bracket, created "
                "by passing bracket_* params to place_order, is editable via edit_bracket_order using "
                "the returned order id. Pick the entry-order bracket when you want to edit later.",
            ),
            (
                "What does set_product_leverage do?",
                "set_product_leverage sets the order leverage for a product. Pass product_id and "
                "leverage as a string, for example '10'. Read the current value with the account tool "
                "get_product_leverage.",
            ),
            (
                "What does adjust_position_margin do?",
                "adjust_position_margin adds or removes isolated margin on a position. Pass product_id "
                "and delta_margin as a string: positive adds margin, negative removes it, for example "
                "'5.0' or '-5.0'.",
            ),
            (
                "What does close_all_positions do?",
                "close_all_positions closes positions only in the scopes set to true: close_all_portfolio for cross or portfolio margin, and close_all_isolated for isolated margin. Both default to false. The server resolves user_id through trading preferences and does not accept it as a tool argument. A dry run performs no identity request or mutation.",
            ),
            (
                "Do I pass user_id to close_all_positions?",
                "No. close_all_positions resolves user_id through the shared trading-preferences identity helper. The helper requires an integer result.user_id. Its cache is separated by the client binding generation, so changing credentials or environment does not reuse a previous account's user_id. A dry run does not fetch an account identity.",
            ),
            (
                "What does configure_auto_topup do?",
                "configure_auto_topup overrides auto top-up for a single position. Pass product_id and "
                "auto_topup as a boolean. Without an override, the position inherits the account "
                "setting.",
            ),
            (
                "How do I preview an order before I send it?",
                "Set dry_run to true, or ask the assistant to place the order as a dry run first. The "
                "tool validates the request and returns {dry_run, method, path, payload} without "
                "sending it. Every mutating tool supports dry_run.",
            ),
            (
                "Does the server round my order price?",
                "The trading tools can round order and bracket prices to the product's tick size. The product metadata cache is separated by the client binding generation, so environment or credential changes do not reuse the prior lookup. The response reports adjustments. A metadata lookup failure can skip rounding, but a missing or revoked trading consent still blocks the real request.",
            ),
            (
                "How are boolean order flags encoded?",
                "Order-level flags such as post_only, reduce_only, and the cancel_* flags are Delta "
                'string enums, so the tool sends "true" or "false" strings. Position-level flags such '
                "as auto_topup and the close_all_* flags are real JSON booleans.",
            ),
            (
                "What does the time_in_force parameter accept?",
                "time_in_force on place_order accepts gtc (good till cancelled) or ioc (immediate or "
                "cancel). Note that IOC and stop orders are not allowed inside a batch order request.",
            ),
            (
                "What does post_only do?",
                "post_only rejects an order if it would take liquidity, keeping you a maker. It is a "
                "boolean on place_order and edit_order. The server sends it as a Delta string enum.",
            ),
            (
                "What does reduce_only do?",
                "reduce_only makes an order only reduce an existing position, never increase or flip "
                "it. It is a boolean on place_order, sent as a Delta string enum.",
            ),
            (
                "What stop-trigger methods are available?",
                "Stop orders and brackets accept stop_trigger_method values mark_price, "
                "last_traded_price, and spot_price. Use it on place_order stop orders and on the "
                "bracket tools via bracket_stop_trigger_method.",
            ),
            (
                "Can a bracket stop-loss use both a price and a trailing amount?",
                "No. A bracket stop-loss is either a fixed trigger price or a trailing amount, never "
                "both. The tool guards this client-side and fails fast, even in dry-run, with a clear "
                "message instead of spending a live round-trip on a rejection.",
            ),
        ],
    ),
    (
        "Debugging and troubleshooting",
        [
            (
                "How do I turn on debug logging?",
                "Set DELTA_MCP_DEBUG=1 in the shared settings file or in one client's environment, "
                "restart the client, and re-run the action. Each HTTP call, its request URL with filter "
                "params, response body, and status logs to `~/.delta-exchange-mcp/logs/`. The exact path "
                "prints on startup.",
            ),
            (
                "Where is the debug log?",
                "The debug log is at `~/.delta-exchange-mcp/logs/debug-<timestamp>-<pid>.log`, or the "
                "path in DELTA_MCP_DEBUG_FILE. The path prints in the stderr startup banner. You can "
                'also ask the assistant "where is the debug log?"; the get_debug_status tool returns it.',
            ),
            (
                "Does the debug log contain my secrets?",
                "The debug logger does not record authentication headers, API keys, API secrets, or signatures. The API secret stays local for signing; authenticated requests send the API key and signature in headers. Debug response bodies can contain balances, positions, and transactions. Inspect and redact account data before sharing a log.",
            ),
            (
                "What does get_debug_status do?",
                "get_debug_status is always registered. It reports whether debug logging is enabled and the local log path when available. It does not return credentials. Enable debug logging only when needed, because response bodies can contain private account data.",
            ),
            (
                "What does get_trading_status do?",
                "get_trading_status is always registered. It reports the current connection's trading state and audit information without secrets. A displayed tool or an old status response is not permission to mutate; the final consent check still applies at request time.",
            ),
            (
                "What does get_connection_status do?",
                "get_connection_status reconciles the current environment and credential state, then reports connection readiness, storage and validation status, client information, and trading consent without secrets. A missing or damaged native record can require reconnect. The tool never returns an API key, secret, signature, or credential fingerprint.",
            ),
            (
                "How do I fix a SignatureExpired error?",
                "SignatureExpired means the request signature drifted more than about 5 seconds from "
                "Delta's clock. Sync your system clock via NTP. The signature timestamp must fall "
                "within Delta's roughly 5-second window.",
            ),
            (
                "How do I fix an InvalidApiKey error?",
                "InvalidApiKey means that Delta does not find the key in the selected environment. Open Manage Connection and select the environment where the key was created. If the status reports externally managed credentials or environment, correct that MCP client's launch configuration. A permission failure is a separate error.",
            ),
            (
                "How do I fix an UnauthorizedApiAccess error?",
                "UnauthorizedApiAccess means that the key lacks permission for the requested endpoint. It does not prove that the key itself is invalid. Check the permission required by that endpoint in Delta API management. The identity check uses trading preferences; Read Data compatibility needs the recorded testnet permission matrix.",
            ),
            (
                "How do I fix an ip_not_whitelisted_for_api_key error?",
                "This error means your request IP is not whitelisted for the key. Add the IP shown in "
                "the error context under Delta API management. The server extracts and reports that IP "
                "in the error message.",
            ),
            (
                "How do I fix a Signature Mismatch error?",
                "A signature mismatch means that Delta cannot verify the signature with the supplied key. Check the selected environment and the key and secret pair. If they are correct, inspect signing of the exact path, query, timestamp, and body. Share only sanitized diagnostic evidence; do not expose the secret while investigating.",
            ),
            (
                "Why does a tool return HTTP 403?",
                "HTTP 403 alone does not establish the cause. Check the Delta error code and the server's bounded hint. Possible checks include endpoint permissions, the IP allowlist, and the required User-Agent header. The standard client supplies that header. Do not replace credentials solely because a generic 403 occurred.",
            ),
            (
                "Why do new tools not appear after an update?",
                "Check the package version and configured source ref, then restart the MCP server process after an update. Credentials and consent do not alter this development branch's tool list. A tool absent from the advertised list can indicate a different running release, a cached client list, or an unsupported tool name.",
            ),
            (
                "Why does the trading tool set not appear?",
                "Trading tools remain visible even when trading is disabled. Inspect get_trading_status and open Manage Connection to grant consent for the correct client and environment. If the tools are absent, check the running version and source ref. DELTA_MCP_MODE does not enable them in this contract.",
            ),
            (
                "Why does an account tool not appear?",
                "Account tools remain visible without credentials. An account call requests connection if authorization is missing. Open Manage Connection and connect a matching key, then make a new call. If the tool itself is absent, inspect the running version and source ref.",
            ),
            (
                "How do I report a bug?",
                "Report a normal bug at github.com/delta-exchange/delta-exchange-mcp/issues. Include the package version or source commit, MCP client, operating system, reproduction steps, and sanitized diagnostics. Remove credentials, connection URLs, and private account data. Send security reports privately to security@delta.exchange instead of publishing an unfixed exploit.",
            ),
            (
                "How do I test tools with MCP Inspector?",
                "Use scripts/inspect.sh. For a CLI call: "
                "`bash scripts/inspect.sh --cli --method tools/list`, or "
                "`bash scripts/inspect.sh --cli --method tools/call --tool-name get_ticker "
                "--tool-arg symbol=BTCUSD`. Run `bash scripts/inspect.sh` with no args for the web UI "
                "on http://localhost:6274.",
            ),
            (
                "How do I run the test suite?",
                "Run `uv run pytest`. The suite uses respx to mock httpx, so it needs no network. Run a "
                "single test by node id, for example "
                "`uv run pytest tests/test_market_tools.py::test_429_retries_then_succeeds`.",
            ),
            (
                "How do I lint the code?",
                "Run `uv run ruff check src tests scripts packaging` in the checkout. For changes to the evaluation harness, also check `evals`. Run actionlint when workflows change. A successful lint result does not replace tests, bundle verification, or live permission checks.",
            ),
            (
                "How do I run a live smoke test?",
                "Run `uv run python scripts/smoke.py`. It hits the real environment set in "
                "DELTA_MCP_ENV. Live checks are run manually, not in CI, because the unit tests are "
                "network-free.",
            ),
            (
                "How does the server handle rate limits?",
                "GET requests retry rate limits, server failures, and transport failures up to three total attempts. Rate-limit waits use X-RATE-LIMIT-RESET in milliseconds; server failures use exponential backoff. POST, PUT, and DELETE do not retry automatically.",
            ),
            (
                "How does the server surface a Delta API error?",
                "The shared client validates Delta's error envelope and raises DeltaApiError with a bounded code and HTTP status. The model-visible message excludes the upstream context. Known errors have application-owned instructions, and an IP allowlist error can include a validated IP address. A malformed mutation rejection requires reconciliation because its execution outcome is unknown.",
            ),
            (
                "Why does a filter like empty expiry fail?",
                "Delta's API rejects an empty query param such as `?expiry=` as an invalid date. The "
                "client strips None-valued params before both signing and sending, so an unset filter "
                "is dropped rather than sent empty. This keeps the signed payload and the wire request "
                "identical.",
            ),
        ],
    ),
    (
        "Skills and local trust",
        [
            (
                "What does list_skills do?",
                "list_skills lists the packaged procedures for P&L, position risk, and funding "
                "carry. Each entry includes its name, description, resource URI, supporting "
                "files, and requires value. The requires value describes account access needed "
                "to run the procedure. It does not hide the procedure before account setup.",
            ),
            (
                "What does get_skill do?",
                "get_skill reads a packaged procedure by name. Pass path to read one of its "
                "listed supporting files. Every procedure is readable without credentials. "
                "Account tool calls still require authorization. The path must be a key in the "
                "packaged file map; it cannot select an arbitrary file on the computer.",
            ),
            (
                "Does the connection page prove that a person approved an action?",
                "No. The server trusts the local MCP client. A process with the Manage Connection "
                "URL can obtain the cookie and CSRF token and request connection or consent "
                "changes. The page does not independently verify user identity or presence. "
                "This is an accepted exception to the MCP URL elicitation requirements. The "
                "local client must enforce the user's instructions. Read docs/security.md for "
                "the controls and the trust boundary.",
            ),
            (
                "What does DELTA_MCP_ANALYTICS do?",
                "DELTA_MCP_ANALYTICS controls the six X-Delta-MCP-* request headers. They report "
                "the server version, client name and version, tool, protocol, and bounded "
                "operating-system and capability details. They exclude client title, "
                "description, website, icons, credentials, and account identity. Set off, "
                "false, 0, or no in the MCP client's process environment to omit all six "
                "headers. This setting does not change the authentication headers or local "
                "consent identity. Upstream analytics retention is outside this package.",
            ),
        ],
    ),
]


def build_pairs() -> list[tuple[str, str, str]]:
    """Flatten sections into (category, question, answer) triples."""
    out: list[tuple[str, str, str]] = []
    for title, pairs in SECTIONS:
        for q, a in pairs:
            out.append((title, q, a))
    return out


def render_markdown(pairs: list[tuple[str, str, str]]) -> str:
    lines: list[str] = []
    lines.append("# Delta Exchange MCP Q&A dataset")
    lines.append("")
    lines.append(
        "Question-and-answer pairs for fine-tuning a Claude model on "
        "[delta-exchange-mcp](https://github.com/delta-exchange/delta-exchange-mcp), the "
        "official MCP server for Delta Exchange India."
    )
    lines.append("")
    lines.append(
        "Questions use Simplified Technical English. Answers describe the recorded repository "
        "source commit. External client instructions cite their documentation. The training file is "
        "`delta-exchange-mcp-qna.jsonl` (Claude messages format). Regenerate both with "
        "`python finetune/generate_qna.py`."
    )
    lines.append("")
    lines.extend([
        f"Contract: `{CONTRACT_ID}`. Release status: **unreleased**.",
        "",
        f"Source: [`{SOURCE_COMMIT}`](https://github.com/delta-exchange/delta-exchange-mcp/commit/{SOURCE_COMMIT}).",
        f"MCP protocol: `{MCP_PROTOCOL_VERSION}`. Stable tool count: {TOOL_COUNT}.",
        f"Tool input-schema SHA-256: `{TOOL_SCHEMA_SHA256}`.",
        "",
        "Use this dataset only with that development contract. The package version alone "
        "does not identify the contract. The schema check fails when the registered tool "
        "arguments change, so maintainers must review the answers before updating this marker.",
        "",
    ])
    lines.append(f"**{len(pairs)} pairs.**")
    lines.append("")
    lines.append("## Contents")
    lines.append("")
    for title, _ in SECTIONS:
        anchor = title.lower().replace(" ", "-").replace("(", "").replace(")", "")
        lines.append(f"- [{title}](#{anchor})")
    lines.append("")

    idx = 0
    for title, section_pairs in SECTIONS:
        lines.append(f"## {title}")
        lines.append("")
        for q, a in section_pairs:
            idx += 1
            lines.append(f"### {idx}. {q}")
            lines.append("")
            lines.append(a)
            lines.append("")
    return "\n".join(lines)


def render_jsonl(pairs: list[tuple[str, str, str]]) -> str:
    rows: list[str] = []
    for category, q, a in pairs:
        record = {
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": q},
                {"role": "assistant", "content": a},
            ],
            "metadata": {
                "category": category,
                "contract_id": CONTRACT_ID,
                "release_status": "unreleased",
                "source_commit": SOURCE_COMMIT,
                "mcp_protocol_version": MCP_PROTOCOL_VERSION,
                "tool_count": TOOL_COUNT,
                "tool_schema_sha256": TOOL_SCHEMA_SHA256,
            },
        }
        rows.append(json.dumps(record, ensure_ascii=False))
    return "\n".join(rows) + "\n"


def main() -> None:
    pairs = build_pairs()
    here = Path(__file__).parent
    (here / "delta-exchange-mcp-qna.md").write_text(
        render_markdown(pairs), encoding="utf-8"
    )
    (here / "delta-exchange-mcp-qna.jsonl").write_text(
        render_jsonl(pairs), encoding="utf-8"
    )
    logging.info("Wrote %s pairs to delta-exchange-mcp-qna.md and .jsonl", len(pairs))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
