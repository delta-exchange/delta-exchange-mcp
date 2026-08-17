from __future__ import annotations

import argparse
import sys
from collections.abc import Awaitable, Callable
from dataclasses import replace

import anyio
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.lowlevel import NotificationOptions
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.models import InitializationOptions
from mcp.server.session import ServerSession
from mcp.server.stdio import stdio_server

from delta_exchange_mcp import audit_log
from delta_exchange_mcp import config as config_mod
from delta_exchange_mcp import credentials, debug_log
from delta_exchange_mcp import form
from delta_exchange_mcp import hints
from delta_exchange_mcp import identity
from delta_exchange_mcp import request
from delta_exchange_mcp import store
from delta_exchange_mcp.client import DeltaClient
from delta_exchange_mcp.tools import account, market, trading
from delta_exchange_mcp.version import PACKAGE_VERSION

_ENV_HELP = """\
configuration (the settings below, from your MCP client or the shared file):
  DELTA_MCP_ENV         india_prod (default), india_testnet, india_devnet
  DELTA_API_KEY         optional; requires DELTA_API_SECRET for the account tools
  DELTA_API_SECRET      required alongside DELTA_API_KEY
  DELTA_MCP_MODE        read (default) or trade; trade registers the mutating tools
                        when both credentials are set
  DELTA_MCP_DEBUG       1/true/yes/on to trace HTTP requests and responses to a file
  DELTA_MCP_DEBUG_FILE  override the debug log path
  DELTA_MCP_AUDIT       off/false/0/no to disable the trade-mode audit log
  DELTA_MCP_AUDIT_FILE  override the audit log path
  DELTA_MCP_CONFIG_FILE override the shared settings file path

Each is read from the environment your MCP client launched this server with, and
falls back to a shared file at ~/.delta-exchange-mcp/config.env that every client
on this machine reads. That file is created with instructions in it on first run,
so an API key is set once rather than pasted into each client's own config.
DELTA_MCP_MODE is the exception. That name is never read from the shared file, because a
value there would arm order placement in every client on the machine at once. Trading is
stored per client instead, under DELTA_MCP_MODE_<READABLE>_<DIGEST> keyed on the exact
name the client gives in the handshake — which is what the in-chat form writes, and what
the first tools/list of a session applies. This is convenience scope, not authenticated
client identity.

Prod and testnet API keys are separate; DELTA_MCP_ENV must match the dashboard the
key was created on. The server speaks MCP over stdio and is normally launched by a
client rather than by hand.
"""

# Sent to the model once per session, which is the only channel that reaches it in the
# state that matters most: no key configured, so no account tool exists to carry a hint
# on its own description. Without this, "what is my BTC position" on a fresh install gets
# answered with "I have no tool for that" and no offer to fix it.
INSTRUCTIONS = """\
Delta Exchange India. Market data needs no setup and always works. The user's own account
— positions, orders, fills, balances — is readable only when an API key is configured,
and placing orders additionally requires trading to be turned on for this client.

If the user asks about their own account and no account tool is available, call
setup_credentials: it opens a form they type the key into. The same form is where trading
is turned on, so call it for that too rather than telling them to edit a config file.
Never ask for an API key or secret in the conversation, and never accept one sent as a
message — anything sent that way is stored in the conversation and visible to you.
get_connection_status reports whether a key is configured, which environment it points
at, what this client may do now, what it may do after a restart, and whether one is due.
"""


class DeltaMCP(MCPServer):
    """MCPServer with a pre-list hook for session-scoped entitlements."""

    def __init__(self) -> None:
        self._before_list_tools: Callable[[ServerSession], Awaitable[None]] | None = None
        self.live_client: DeltaClient | None = None
        super().__init__(
            "delta-exchange",
            # What a client shows a person, as against `name`, which is what it keys on.
            # These are the same strings the bundle's install dialog uses, read from the
            # one place that holds them.
            title=identity.DISPLAY_NAME,
            description=identity.SHORT_DESCRIPTION,
            website_url=identity.HOMEPAGE,
            version=PACKAGE_VERSION,
            instructions=INSTRUCTIONS,
            middleware=[self._serve],
        )

    def before_list_tools(
        self, callback: Callable[[ServerSession], Awaitable[None]]
    ) -> None:
        self._before_list_tools = callback

    async def _serve(
        self, ctx: ServerRequestContext, call_next: CallNext
    ) -> HandlerResult:
        """Publish the session being served, and entitle it before the tool list is built.

        Middleware is the one place both are in reach: `MCPServer.list_tools()` takes no
        context, and the session reaches a handler only through a declared `Context`
        parameter, which the shared mutation decorator cannot add for what it wraps.
        """
        token = request.session.set(ctx.session)
        # The tool name rides along for the analytics header. Read from the raw params
        # rather than a typed field, because the same middleware sees every method and
        # only `tools/call` carries one; anything else leaves it empty.
        called = ""
        if ctx.method == "tools/call":
            params = ctx.params
            called = (
                params.get("name", "")
                if isinstance(params, dict)
                else getattr(params, "name", "")
            ) or ""
        tool_token = request.tool.set(called)
        try:
            if ctx.method == "tools/list" and self._before_list_tools is not None:
                await self._before_list_tools(ctx.session)
            return await call_next(ctx)
        finally:
            request.tool.reset(tool_token)
            request.session.reset(token)

    async def close_live_client(self) -> None:
        if self.live_client is not None:
            await self.live_client.aclose()


def build_server(cfg: config_mod.Config | None = None) -> DeltaMCP:
    cfg = cfg or config_mod.load()
    mcp = DeltaMCP()

    # Mode controls which tools exist, not how HTTP is sent. Start the shared client in
    # read mode and let the one runtime transition below arm mutations when appropriate.
    live = replace(cfg, mode="read")
    client = DeltaClient(live)
    mcp.live_client = client
    log_path = debug_log.configure(cfg)
    market.register(mcp, client)

    account_registered = False
    trade_audit = None
    trade_gate: trading.TradeGate | None = None

    def surface() -> str:
        names = ["market"]
        if account_registered:
            names.append("account")
        if trade_audit is not None or live.mode == "trade":
            names.append("trade")
        return "+".join(names)

    def announce_transition(reason: str) -> None:
        audit = str(trade_audit.path) if trade_audit else "off"
        print(
            f"[delta-exchange-mcp] runtime transition={reason} env={live.env} "
            f"mode={live.mode} surface={surface()} audit={audit}",
            file=sys.stderr,
        )

    def arm_trading(
        armed: config_mod.Config, session: ServerSession | None = None
    ) -> None:
        """Register mutations against the same rebindable client as every other tool."""
        nonlocal trade_audit, trade_gate, live
        client.rebind(armed)
        trade_audit = audit_log.configure(armed)
        trade_gate = trading.TradeGate()
        if session is not None:
            trade_gate.bind(request.peer(session))
        trading.register(mcp, client, trade_audit, trade_gate)
        live = armed

        @mcp.tool(annotations=hints.reads("Trading status", external=False))
        def get_trading_status() -> dict[str, object]:
            """Trading mode status and the audit log path (None if auditing is disabled).

            Use this to tell the user that mutations are enabled and where the audit log lives.
            """
            return {
                "mode": "trade",
                "audit_log_path": str(trade_audit.path) if trade_audit else None,
            }

    def disarm_trading() -> None:
        """Remove mutations before credentials, environment, or entitlement can move."""
        nonlocal trade_audit, trade_gate, live
        if trade_gate is not None:
            trade_gate.revoke()
        for name in (*trading.TOOL_NAMES, "get_trading_status"):
            mcp.remove_tool(name)
        trade_audit = None
        trade_gate = None
        live = replace(live, mode="read")
        client.rebind(live)

    def http_identity(config: config_mod.Config) -> tuple[str, str | None, str | None]:
        """The secret-bearing comparison stays internal and is never returned by a tool."""
        return config.env, config.api_key, config.api_secret

    def restart_required(next_config: config_mod.Config) -> bool:
        return (
            next_config.has_credentials
            and next_config.mode == "trade"
            and live.mode != "trade"
        )

    async def reconcile(
        session: ServerSession, *, allow_trade: bool, notify: bool
    ) -> tuple[config_mod.Config, dict[str, str]]:
        """Move every live surface to one coherent next configuration.

        Read surfaces can move immediately. Trading is removed before an identity change and
        can only be armed by the first tools/list of a new session, never by the form save
        that requested it.
        """
        nonlocal account_registered, live
        client_name = request.client(session).name
        shared = store.read()
        next_config = config_mod.load_for_client(client_name, shared)
        identity_changed = http_identity(live) != http_identity(next_config)
        tools_changed = False
        transitions: list[str] = []

        if live.mode == "trade" and (identity_changed or next_config.mode != "trade"):
            transitions.append(
                "trade-disarmed-identity-change"
                if identity_changed
                else "trade-disarmed-read-mode"
            )
            disarm_trading()
            tools_changed = True

        if identity_changed:
            client.rebind(replace(next_config, mode="read"))
            transitions.append("identity-rebound")

        if account_registered and not next_config.has_credentials:
            for name in account.TOOL_NAMES:
                mcp.remove_tool(name)
            account_registered = False
            tools_changed = True
            transitions.append("account-disarmed")
        elif not account_registered and next_config.has_credentials:
            account.register(mcp, client)
            account_registered = True
            tools_changed = True
            transitions.append("account-armed")

        if (
            allow_trade
            and next_config.has_credentials
            and next_config.mode == "trade"
            and live.mode != "trade"
        ):
            arm_trading(next_config, session)
            tools_changed = True
            transitions.append("trade-armed")
        elif (
            allow_trade
            and next_config.has_credentials
            and next_config.mode == "trade"
            and live.mode == "trade"
            and trade_gate is not None
        ):
            trade_gate.bind(request.peer(session))
        else:
            runtime_mode = "trade" if live.mode == "trade" else "read"
            live = replace(next_config, mode=runtime_mode)
            client.rebind(live)

        if transitions:
            announce_transition("+".join(transitions))
        if notify and tools_changed:
            await session.send_tool_list_changed()
        return next_config, shared

    if cfg.has_credentials:
        account.register(mcp, client)
        account_registered = True
    if cfg.has_credentials and cfg.mode == "trade":
        arm_trading(cfg)

    # Retain the connection object rather than its integer id. Python may reuse an id after
    # a connection closes; carrying that integer forward could make a later client skip its
    # own entitlement check. Stdio has one live connection, so this set stays trivially small.
    entitlement_checked: set[object] = set()

    async def activate(
        session: ServerSession, expected: form.ExpectedState
    ) -> form.Activation:
        """Hot-apply safe form changes and report whether account reads are live."""
        # A form save must never become the event that arms trading. Mark the decision made
        # even for a protocol peer that called the opener directly before its first list.
        entitlement_checked.add(request.peer(session))
        next_config, shared = await reconcile(session, allow_trade=False, notify=True)
        identity_current = (
            expected.environment is None
            or (
                (shared.get("DELTA_MCP_ENV") or "").strip() == expected.environment
                and (shared.get("DELTA_API_KEY") or "").strip() == expected.api_key
                and (shared.get("DELTA_API_SECRET") or "").strip() == expected.api_secret
            )
        )
        mode_current = (
            not expected.mode_setting
            or (shared.get(expected.mode_setting) or "").strip().lower() == expected.mode
        )
        return form.Activation(
            account_ready=account_registered,
            mode=live.mode,
            effective_mode=next_config.mode,
            expected_current=identity_current and mode_current,
        )

    # Registered whether or not credentials are set: someone with none needs to add a
    # first key, and someone with one still rotates it, switches environment, or changes mode.
    form.register(mcp, activate)

    # Read-only despite reconciling: it changes what this process believes to match the
    # settings file, rather than changing anything outside the process. Nobody needs to
    # hesitate before asking whether they are connected.
    @mcp.tool(annotations=hints.reads("Connection status", external=False))
    async def get_connection_status(ctx: Context) -> dict[str, object]:
        """Whether an API key is configured, where it points, and if a restart is due.

        Reconciles safe external file changes before answering and returns no key, secret,
        or credential fingerprint.
        """
        session = ctx.session
        who = request.client(session)
        client_name = who.name
        next_config, shared = await reconcile(session, allow_trade=False, notify=True)
        overridden = credentials.overridden_by_client(client_name, shared)
        binding = config_mod.mode_key(client_name)
        return {
            "environment": live.env,
            "credentials_configured": next_config.has_credentials,
            "account_tools_available": account_registered,
            "mode": live.mode,
            "mode_after_restart": next_config.mode,
            # Credential/environment overrides cannot be repaired by restarting. A mode
            # override is different: if it says trade, restart is exactly what will arm it,
            # so retain that warning while also naming the override below.
            "restart_required": not (
                set(overridden) - {"DELTA_MCP_MODE"}
            )
            and restart_required(next_config),
            "overridden_by_client": overridden,
            "client_name": client_name,
            # The build behind the name. A report of "the form did not render" is only
            # actionable with it: the same client name covers versions that differ in
            # whether they render an MCP App at all.
            "client_version": who.version,
            "mode_setting": binding,
            "client_identity": "self-reported name; convenience scope, not authentication",
            "version": PACKAGE_VERSION,
            "view_build": form.build_id(),
        }

    # A client identifies itself only during the handshake. Apply its scoped entitlement
    # before the first tools/list, through the SDK's own middleware chain rather than by
    # replacing its private request handler table. It is checked once per session so
    # choosing trade through the form cannot arm mutations in that same session later.
    async def apply_session_entitlement(session: ServerSession) -> None:
        connection = request.peer(session)
        if connection in entitlement_checked:
            return
        await reconcile(session, allow_trade=True, notify=False)
        entitlement_checked.add(connection)

    mcp.before_list_tools(apply_session_entitlement)

    if log_path is not None:

        @mcp.tool(annotations=hints.reads("Debug status", external=False))
        def get_debug_status() -> dict[str, object]:
            """Whether debug logging is on and the absolute path of the current log file.

            Use this to tell the user where to find / fetch the HTTP debug log.
            """
            return {"enabled": True, "log_path": str(log_path)}

    return mcp


def initialization_options(mcp: MCPServer) -> InitializationOptions:
    """What this server tells a client about itself, declaring a changeable tool list.

    The SDK's own `run_stdio_async` builds these with every notification flag off, so the
    server would advertise `tools.listChanged: false`. A client told that has no reason to
    re-read the tool list, which makes the notification sent when a saved credential
    brings the account tools up a no-op — leaving the restart it exists to avoid as the
    only way through.
    """
    return mcp._lowlevel_server.create_initialization_options(
        NotificationOptions(tools_changed=True)
    )


async def serve(mcp: MCPServer) -> None:
    """Serve over stdio, the only transport."""
    try:
        async with stdio_server() as (read_stream, write_stream):
            await mcp._lowlevel_server.run(
                read_stream, write_stream, initialization_options(mcp)
            )
    finally:
        if isinstance(mcp, DeltaMCP):
            await mcp.close_live_client()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="delta-exchange-mcp",
        description=(
            "MCP server for Delta Exchange India: market data and account reads, "
            "served to an MCP client over stdio."
        ),
        epilog=_ENV_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"delta-exchange-mcp {PACKAGE_VERSION}",
    )
    # Optional, so a bare invocation still means "serve" — that is how every MCP client
    # launches this, and it must never become a subcommand.
    sub = parser.add_subparsers(dest="command")
    setup_parser = sub.add_parser(
        "setup",
        help="set up your API key, once for every client on this machine",
        description=(
            "Opens a settings page in your browser. The page runs on this machine only "
            "and what you type goes straight to the settings file."
        ),
    )
    setup_parser.add_argument(
        "--terminal",
        action="store_true",
        help="type the key here instead, for a machine with no browser",
    )
    setup_parser.add_argument(
        "--no-browser",
        action="store_true",
        help="print the address instead of opening it",
    )
    setup_parser.add_argument(
        "--no-verify",
        action="store_true",
        help="with --terminal, skip the check against Delta and save whatever is entered",
    )
    # The old name for the terminal route, kept working because it is published and people
    # have it in their own notes and scripts. Out of the help so nobody learns it now.
    login_parser = sub.add_parser("login", add_help=False)
    login_parser.add_argument("--no-verify", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    if args.command == "login":
        print(
            "`login` is now `setup --terminal`, and plain `setup` opens a page in your "
            "browser instead. This still works.",
            file=sys.stderr,
        )

    if args.command in ("login", "setup"):
        # One command with two ways in, rather than two commands doing the same thing. The
        # browser is the default because the person this is for has a browser and may have
        # no terminal at all; the flag is for a machine that has it the other way round.
        if args.command == "login" or args.terminal:
            from delta_exchange_mcp import login

            raise SystemExit(login.run(verify=not args.no_verify))

        from delta_exchange_mcp import setup as setup_page

        # Needs no MCP client running, which is the point of it: an agent can install this
        # server and configure the account in one turn, with no restart in between.
        page = setup_page.serve(open_browser=not args.no_browser)
        # First line, and always printed. Whoever called this may be an agent capturing
        # stdout, and the browser may not have opened — over SSH, or on a machine with
        # none. The address is the answer either way.
        print(page.url, flush=True)
        print(
            "Waiting for you to finish. This page closes itself once saved, "
            f"or after {setup_page.LIFETIME_SECONDS // 60} minutes.",
            file=sys.stderr,
            flush=True,
        )
        try:
            saved = page.wait()
        except KeyboardInterrupt:
            saved = False
        finally:
            page.stop()
        if saved:
            print("Saved. Restart your MCP client so it reads the new settings.", file=sys.stderr)
        else:
            print("Nothing was saved.", file=sys.stderr)
        raise SystemExit(0 if saved else 1)

    cfg = config_mod.load()
    mcp = build_server(cfg)
    surface = "market+account" if cfg.has_credentials else "market"
    trade_on = cfg.has_credentials and cfg.mode == "trade"
    if trade_on:
        surface += "+trade"
    banner = (
        f"[delta-exchange-mcp] startup stdio env={cfg.env} base_url={cfg.base_url} "
        f"mode={cfg.mode} surface={surface}"
    )
    if cfg.config_file is not None:
        banner += f" config={cfg.config_file}"
    if trade_on:
        audit = audit_log.configure(cfg)  # idempotent: appends to the same file path
        banner += f" audit={audit.path if audit else 'off'}"
    if cfg.debug:
        log_path = debug_log.configure(cfg)  # idempotent — returns the same path
        if log_path is not None:  # configure returns None if the log file can't be opened
            banner += f" debug=on log={log_path}"
    print(banner, file=sys.stderr)
    insecure = store.insecure_permissions()
    if insecure is not None:
        print(f"[delta-exchange-mcp] {insecure}", file=sys.stderr)
    if cfg.partial_credentials:
        supplied = "DELTA_API_KEY" if cfg.api_key else "DELTA_API_SECRET"
        missing = "DELTA_API_SECRET" if cfg.api_key else "DELTA_API_KEY"
        print(
            f"[delta-exchange-mcp] {supplied} is set but {missing} is not. Both are "
            "required to sign a request, so the account tools are NOT available and only "
            "market data will work.",
            file=sys.stderr,
        )
    anyio.run(serve, mcp)
