# AGENTS.md

Guidance for coding agents that work in this repository. `CLAUDE.md` is a symlink to this
file, so keep all repository guidance here.

## Project in one line

This is a local stdio MCP server for Delta Exchange India. It always discovers market,
account, export, status, and trading tools. It checks credentials and trading consent when
a tool is called.

## Style

Do not add typing that hides the real interface.

- Do not annotate pytest fixtures such as `tmp_path` or `monkeypatch`.
- Do not add `**kwargs: Any` or `-> Any` to an internal test helper when no type is useful.
- Use `Any` only for a public JSON boundary or a genuinely heterogeneous result.
- Keep authorization, credential storage, consent, and HTTP transport as separate domain
  boundaries.

## Commands

```bash
uv sync --locked
uv run pytest
uv run ruff check src tests scripts packaging
actionlint

uv run delta-exchange-mcp
uv run delta-exchange-mcp login             # optional browser-opening convenience

bash scripts/inspect.sh --cli --method tools/list
bash scripts/inspect.sh --cli --method tools/call --tool-name get_ticker --tool-arg symbol=BTCUSD
bash packaging/mcpb/build.sh
```

Run Python 3.12 and 3.13 before a release. Re-run `uv sync` after `pyproject.toml` or an
entry point changes.

## MCP protocol

The server uses the current MCP 2026 protocol and keeps legacy compatibility where the SDK
supports it.

- The modern connection starts with `server/discover`. Do not add an initialize-only
  dependency.
- Modern client information and capabilities are request metadata. Read them from the
  request `Context`. Do not treat a request session as a durable client identity.
- `request.context_client(ctx)` is the shared reader for the exact client-provided name and
  version. An older protocol can use the session fallback.
- The client name is self-reported. It partitions consent records. It does not authenticate
  a client. Client-name impersonation by another local process is outside this threat model.
- The setup URL is a capability that the MCP returns to the local client for URL elicitation,
  MCP Apps, and the clickable-link fallback. A malicious local MCP client that uses this URL
  without separate browser user presence is outside this threat model. Host, Origin, cookie,
  and CSRF checks protect the browser boundary; they do not authenticate a local URL holder.
- `setup_credentials` has no secret arguments. It opens Manage Connection through URL
  elicitation, an MCP App open-link result, or a clickable text link.
- A resumed authorization request reports state. It never executes the pending trade.
- Register every tool before serving. Credentials and consent must not change `tools/list`.
  An account or trading call that lacks authorization returns `input_required`.
- Keep `get_connection_status`, `get_trading_status`, and `get_debug_status` stable. Their
  output must not contain a key, secret, signature, digest, or credential fingerprint.

Primary regression tests are in `tests/test_activation.py`.

## Connection service

`auth/connection.py::ConnectionService` coordinates the active environment, credentials,
consent, the rebindable `DeltaClient`, and one Manage Connection page. It is the composition
boundary. Keep storage transactions in `auth/store.py` and consent transactions in
`auth/consent.py`.

The service supports one credential record for production and one for testnet. A browser
environment change selects which record is active. The request-pinned client configuration
must stay constant through a tool call, including preflight requests and the final mutation.

Process environment credentials remain a compatibility source. A complete
`DELTA_API_KEY` and `DELTA_API_SECRET` pair is externally managed. The MCP can use it but
cannot rotate or remove its source. A partial pair fails closed for account access.

`DELTA_MCP_MODE` never authorizes trading. Do not add it back to runtime authorization,
the bundle manifest, or installation instructions.

## Credential storage and migration

`auth/store.py::CredentialStore` stores secrets only in an approved operating-system
credential service:

- macOS Keychain;
- Windows Credential Manager;
- Linux Secret Service.

Reject null, fail, and plaintext keyring backends. If no approved backend is available,
keep credentials and consent in process memory. Never add a plaintext fallback.

The non-secret metadata file holds active revisions, validation state, account labels,
timestamps, pending cleanup, and revocation generations. Writes are atomic and serialized.
A replacement validates the candidate, writes a new version, reads it back, publishes the
active pointer, rebinds the client, and then retires the old version. A crash or a missing
keyring record must leave recoverable metadata.

OS record names are scoped to the canonical metadata path. Metadata copied to a different
path and records from the old draft format require a browser reconnect. Keep their record
names in `preserved_records`, which is recovery information, not a cleanup queue. Never
read, adopt, or delete a record whose metadata location cannot establish ownership.

The first connection automatically migrates a complete legacy `config.env` credential
pair. Migration writes and reads the OS record before it removes only the key and secret
lines. A failure before publication leaves the file unchanged. Reject a symlink migration
target. Never migrate legacy trading mode into consent.

The validation endpoint is `GET /v2/users/trading_preferences`. It supplies the account
`user_id` used by `close_all_positions`. `UnauthorizedApiAccess` is a permission failure,
not proof of an invalid key. Only explicit invalid-key and invalid-signature responses
reject a candidate as invalid. An unreachable candidate can be stored as `unverified`.

## Manage Connection browser

`setup.py` owns the loopback listener. `form.py` owns the shared inline HTML. The listener
binds only to `127.0.0.1` on an operating-system-selected port and stops after ten minutes,
completion, or explicit close.

Keep these controls together:

- exact `Host` and `Origin` checks;
- JSON-only POST requests with a bounded body;
- an HTTP-only session cookie and rotating one-use CSRF values;
- `Cache-Control: no-store`, a restrictive CSP, no-referrer policy, frame protection, and
  content-type protection;
- serialized mutations with an expected credential and consent revision;
- no key or secret in a URL, MCP tool argument, result, log, or model context.

A stale or duplicate tab cannot replace newer credentials or restore revoked consent.
Browser action tests in `tests/test_setup_page.py` must cover replay, hostile origins,
invalid content types and JSON, oversized bodies, stale revisions, expiry, response
headers, and secret-free logs.

The view is an MCP App with all assets inline. Complete the `ui/initialize` handshake. Do
not call `ui/message` or `ui/update-model-context`; either call can expose typed credentials
to the model. Use `uv run python scripts/host.py --open` to inspect the host layout.

## Trading consent and mutations

One approval enables all 13 trading tools for one exact client name, environment, and
credential revision. Production also requires an unchecked real-orders acknowledgement
before the user can enable trading.

Persistent consent has no time expiry. Rotation, migration, environment selection,
disconnect, manual disable, a credential generation change, or a changed client name
revokes it. An unnamed client receives process-session consent only.

Every trading tool has `dry_run`. A dry run validates and returns the request payload. It
does not need credentials or consent and must send no POST, PUT, or DELETE request.

Register mutations through the shared `mutation_tool` decorator. It publishes
`_meta["delta.exchange/mutating"] = true` and pins the client state. A real mutation needs a
current `TradeGate` lease. Check the consent generation again immediately before the
mutation and after any preflight request. Revocation during preflight must stop the request.

Mutations never retry automatically. A lost mutation response has an unknown execution
outcome and requires reconciliation before a user retries.

## Delta client

`client.py::DeltaClient` owns HTTP behavior for every tool.

- Strip `None` query values once and use the same query for signing and sending.
- Include `/v2` in the HMAC signing path. Tool callers pass paths such as `/orders`.
- Serialize a mutation body once. Sign and send the same bytes.
- Keep the required User-Agent header.
- Retry rate limits, server failures, and transport failures for GET requests only.
- Capture one immutable client state for each request. `rebind()` replaces the complete
  state and retires the old transport after its active requests finish.
- Convert a valid Delta error envelope into `DeltaApiError`. Treat a malformed envelope as
  `invalid_response` without putting its body in a credential or permission decision.

Account tools use authenticated GET requests. The `get_profile` tool and `/v2/profile`
API-key call are retired and must not return.

## Logs

Debug logs can contain account response data, but never authentication headers or secrets.
`debug_log.shutdown()` must restore the prior logger levels and propagation settings.

Audit logs are partitioned by environment and use the request-pinned client configuration.
They contain mutation payloads and summarized outcomes, never credentials. Keep the cache
partitioned; a testnet audit writer must not label a production request or the reverse.

## Distribution and release gates

This project is local stdio only. Do not add a shared hosted MCP, HTTP transport, Docker
image, or OAuth flow without a separate design review.

The MCPB manifest is generated from the live stable tool list. It must not ask for an API
key, secret, environment, or trading mode. The browser is the configuration interface.

Before release:

- run tests on Ubuntu, macOS, and Windows with Python 3.12 and 3.13;
- run the opt-in real system-keyring contract test on each operating system;
- build and verify the MCPB, including modern discovery and the legacy compatibility path;
- run the authenticated testnet permission matrix with separate Read Data and Trading
  keys. Do not claim Read Data compatibility until each Read Data cell reports `allowed`.

## Tests

`respx` mocks Delta HTTP calls. Add a regression test for each observed failure. Keep live
credentials out of the unit suite and CI logs. The testnet permission matrix is an explicit
manual release gate, not a CI secret.
