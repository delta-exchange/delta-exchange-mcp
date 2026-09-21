from __future__ import annotations

import argparse
import sys
from collections.abc import Awaitable, Callable

import anyio
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.lowlevel import NotificationOptions
from mcp.server.models import InitializationOptions
from mcp.server.session import ServerSession
from mcp.server.stdio import stdio_server

from delta_exchange_mcp import config as config_mod
from delta_exchange_mcp import debug_log
from delta_exchange_mcp import form
from delta_exchange_mcp import store
from delta_exchange_mcp.client import DeltaClient
from delta_exchange_mcp.tools import account, market, trading
from delta_exchange_mcp.version import PACKAGE_VERSION

_ENV_HELP = """\
configuration (the settings below, from your MCP client or the shared file):
  DELTA_MCP_ENV         india_prod (default), india_testnet, india_devnet
  DELTA_API_KEY         optional; requires DELTA_API_SECRET for the account and
                        trading tools
  DELTA_API_SECRET      required alongside DELTA_API_KEY
  DELTA_MCP_DEBUG       1/true/yes/on to trace HTTP requests and responses to a file
  DELTA_MCP_DEBUG_FILE  override the debug log path
  DELTA_MCP_CONFIG_FILE override the shared settings file path

Each is read from the environment your MCP client launched this server with, and
falls back to a shared file at ~/.delta-exchange-mcp/config.env that every client
on this machine reads. That file is created with instructions in it on first run,
so an API key is set once rather than pasted into each client's own config.

A key with Trading permission registers the order-placing tools; a key without it
registers them too, and Delta rejects the order. The key's own permissions are the
boundary, so issue a read-only key for a client that should not trade.

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
— positions, orders, fills, balances — and order placement both need an API key.

If the user asks about their own account and no account tool is available, call
setup_credentials: it opens a form they type the key into. Never ask for an API key or
secret in the conversation, and never accept one sent as a message — anything sent that
way is stored in the conversation and visible to you.
get_connection_status reports whether a key is configured and which environment it points
at.

Order placement is real and immediate. There is no rehearsal mode and no confirmation
step, so confirm intent with the user before calling a tool that places, edits, cancels or
closes anything.
"""


def _session_client_name(session: ServerSession) -> str:
    params = session.client_params
    return params.clientInfo.name if params and params.clientInfo else ""


class DeltaMCP(FastMCP):
    """FastMCP with a supported pre-list hook for session-scoped entitlements."""

    def __init__(self) -> None:
        self._before_list_tools: Callable[[ServerSession], Awaitable[None]] | None = None
        self.live_client: DeltaClient | None = None
        super().__init__("delta-exchange", instructions=INSTRUCTIONS)

    def before_list_tools(
        self, callback: Callable[[ServerSession], Awaitable[None]]
    ) -> None:
        self._before_list_tools = callback

    async def list_tools(self):
        """Apply a session entitlement before FastMCP builds the public tool list."""
        if self._before_list_tools is not None:
            try:
                session = self.get_context().session
            except ValueError:
                # Direct in-process inspection has no MCP request or handshake. Startup
                # registration is still complete, so there is no entitlement to apply.
                pass
            else:
                await self._before_list_tools(session)
        return await super().list_tools()

    async def close_live_client(self) -> None:
        if self.live_client is not None:
            await self.live_client.aclose()


def build_server(cfg: config_mod.Config | None = None) -> DeltaMCP:
    cfg = cfg or config_mod.load()
    mcp = DeltaMCP()
    # FastMCP has no version argument, and the server it wraps reports the mcp SDK's own
    # version when this is left unset — so clients would see the SDK version as ours.
    mcp._mcp_server.version = PACKAGE_VERSION

    live = cfg
    client = DeltaClient(live)
    mcp.live_client = client
    log_path = debug_log.configure(cfg)
    market.register(mcp, client)

    authenticated_registered = False

    def surface() -> str:
        names = ["market"]
        if authenticated_registered:
            names.extend(("account", "trade"))
        return "+".join(names)

    def announce_transition(reason: str) -> None:
        print(
            f"[delta-exchange-mcp] runtime transition={reason} env={live.env} "
            f"surface={surface()}",
            file=sys.stderr,
        )

    def arm_authenticated() -> None:
        """Register the account reads and the trading mutations together."""
        nonlocal authenticated_registered
        account.register(mcp, client)
        trading.register(mcp, client)
        authenticated_registered = True

    def disarm_authenticated() -> None:
        nonlocal authenticated_registered
        for name in (*account.TOOL_NAMES, *trading.TOOL_NAMES):
            mcp.remove_tool(name)
        authenticated_registered = False

    def http_identity(config: config_mod.Config) -> tuple[str, str | None, str | None]:
        """The secret-bearing comparison stays internal and is never returned by a tool."""
        return config.env, config.api_key, config.api_secret

    async def reconcile(
        session: ServerSession, *, notify: bool
    ) -> tuple[config_mod.Config, dict[str, str]]:
        """Move every live surface to one coherent next configuration.

        Credentials are the only thing that gates a tool now, so the whole authenticated
        surface moves together and every change can be applied hot.
        """
        nonlocal live
        shared = store.read()
        next_config = config_mod.load(shared)
        identity_changed = http_identity(live) != http_identity(next_config)
        tools_changed = False
        transitions: list[str] = []

        live = next_config
        client.rebind(live)
        if identity_changed:
            transitions.append("identity-rebound")

        if authenticated_registered and not next_config.has_credentials:
            disarm_authenticated()
            tools_changed = True
            transitions.append("authenticated-disarmed")
        elif not authenticated_registered and next_config.has_credentials:
            arm_authenticated()
            tools_changed = True
            transitions.append("authenticated-armed")

        if transitions:
            announce_transition("+".join(transitions))
        if notify and tools_changed:
            await session.send_tool_list_changed()
        return next_config, shared

    if cfg.has_credentials:
        arm_authenticated()

    async def activate(
        session: ServerSession, expected: form.ExpectedState
    ) -> form.Activation:
        """Hot-apply form changes and report whether the authenticated tools are live."""
        _, shared = await reconcile(session, notify=True)
        identity_current = (
            expected.environment is None
            or (
                (shared.get("DELTA_MCP_ENV") or "").strip() == expected.environment
                and (shared.get("DELTA_API_KEY") or "").strip() == expected.api_key
                and (shared.get("DELTA_API_SECRET") or "").strip() == expected.api_secret
            )
        )
        return form.Activation(
            account_ready=authenticated_registered,
            expected_current=identity_current,
        )

    # Registered whether or not credentials are set: someone with none needs to add a
    # first key, and someone with one still rotates it or switches environment.
    form.register(mcp, activate)

    @mcp.tool()
    async def get_connection_status(ctx: Context) -> dict[str, object]:
        """Whether an API key is configured and where it points.

        Reconciles safe external file changes before answering and returns no key, secret,
        or credential fingerprint.
        """
        session = ctx.session
        next_config, _ = await reconcile(session, notify=True)
        return {
            "environment": live.env,
            "credentials_configured": next_config.has_credentials,
            "account_tools_available": authenticated_registered,
            "trading_tools_available": authenticated_registered,
            "client_name": _session_client_name(session),
            "version": PACKAGE_VERSION,
            "view_build": form.build_id(),
        }

    # A settings file edited outside this process should be picked up before the client
    # builds its tool list, so an externally added key does not need a restart.
    async def refresh_before_list(session: ServerSession) -> None:
        await reconcile(session, notify=False)

    mcp.before_list_tools(refresh_before_list)

    if log_path is not None:

        @mcp.tool()
        def get_debug_status() -> dict[str, object]:
            """Whether debug logging is on and the absolute path of the current log file.

            Use this to tell the user where to find / fetch the HTTP debug log.
            """
            return {"enabled": True, "log_path": str(log_path)}

    return mcp


def initialization_options(mcp: FastMCP) -> InitializationOptions:
    """What this server tells a client about itself, declaring a changeable tool list.

    FastMCP's own `run_stdio_async` builds these with every notification flag off, so the
    server would advertise `tools.listChanged: false`. A client told that has no reason to
    re-read the tool list, which makes the notification sent when a saved credential
    brings the account tools up a no-op — leaving the restart it exists to avoid as the
    only way through.
    """
    return mcp._mcp_server.create_initialization_options(
        NotificationOptions(tools_changed=True)
    )


async def serve(mcp: FastMCP) -> None:
    """Serve over stdio, the only transport."""
    try:
        async with stdio_server() as (read_stream, write_stream):
            await mcp._mcp_server.run(
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
    login_parser = sub.add_parser(
        "login",
        help="store your API key in the shared settings file, once for every client",
    )
    login_parser.add_argument(
        "--no-verify",
        action="store_true",
        help="skip the check against Delta and save whatever is entered",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    if args.command == "login":
        from delta_exchange_mcp import login

        raise SystemExit(login.run(verify=not args.no_verify))

    cfg = config_mod.load()
    mcp = build_server(cfg)
    surface = "market+account+trade" if cfg.has_credentials else "market"
    banner = (
        f"[delta-exchange-mcp] startup stdio env={cfg.env} base_url={cfg.base_url} "
        f"surface={surface}"
    )
    if cfg.config_file is not None:
        banner += f" config={cfg.config_file}"
    if cfg.debug:
        log_path = debug_log.configure(cfg)  # idempotent — returns the same path
        if log_path is not None:  # configure returns None if the log file can't be opened
            banner += f" debug=on log={log_path}"
    print(banner, file=sys.stderr)
    anyio.run(serve, mcp)
