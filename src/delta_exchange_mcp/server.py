from __future__ import annotations

import argparse
import sys
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

import anyio
from mcp.server.apps import Apps
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, InputRequiredResult, TextContent

from delta_exchange_mcp import audit_log
from delta_exchange_mcp import authorization
from delta_exchange_mcp import connection_app
from delta_exchange_mcp import config as config_mod
from delta_exchange_mcp import credentials, debug_log
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
  DELTA_MCP_MODE        legacy compatibility value; it does not authorize trading
  DELTA_MCP_DEBUG       1/true/yes/on to trace HTTP requests and responses to a file
  DELTA_MCP_DEBUG_FILE  override the debug log path
  DELTA_MCP_AUDIT       off/false/0/no to disable the trade-mode audit log
  DELTA_MCP_AUDIT_FILE  override the audit log path
  DELTA_MCP_CONFIG_FILE override the shared settings file path

Each is read from the environment your MCP client launched this server with, and
falls back to a shared file at ~/.delta-exchange-mcp/config.env that every client
on this machine reads. That file is created with instructions in it on first run,
so an API key is set once rather than pasted into each client's own config.
Legacy DELTA_MCP_MODE values do not authorize order placement. Trading requires browser
consent for the exact client name, environment, and credential revision.

Prod and testnet API keys are separate; DELTA_MCP_ENV must match the dashboard the
key was created on. The server speaks MCP over stdio and is normally launched by a
client rather than by hand.
"""

INSTRUCTIONS = """\
Delta Exchange India. Market data needs no setup. Account and trading tools stay visible,
but a call that needs authorization opens the browser connection flow. Never ask for an
API key or secret in the conversation, and never accept one in a tool argument. Use
setup_credentials to open the same browser flow directly. A trading dry run does not need
trading consent because it sends no mutation to Delta.
"""


BeforeToolCall = Callable[
    [str, dict[str, Any], Context],
    Awaitable[CallToolResult | InputRequiredResult | None],
]


class DeltaMCP(MCPServer):
    """MCP server with one injectable request-scoped authorization hook."""

    def __init__(self) -> None:
        self._before_tool_call: BeforeToolCall | None = None
        self.live_client: DeltaClient | None = None
        apps = Apps()

        @apps.tool(
            resource_uri=connection_app.VIEW_URI,
            visibility=["model", "app"],
            annotations=hints.mutates(
                "Manage Delta Exchange connection",
                destructive=False,
                idempotent=False,
                external=False,
            ),
            description="Open the browser page for credentials and trading consent.",
        )
        async def setup_credentials(ctx: Context) -> CallToolResult:
            message = (
                "The browser connection service is not available in this build. Do not "
                "send an API key or secret in the conversation."
            )
            return CallToolResult(
                content=[TextContent(type="text", text=message)],
                structuredContent={"status": "unavailable", "message": message},
                isError=True,
            )

        apps.add_html_resource(
            connection_app.VIEW_URI,
            connection_app.VIEW_HTML,
            name="Delta Exchange connection",
            title="Manage Delta Exchange connection",
            description="Open the browser page for account connection and trading consent.",
            prefers_border=True,
        )
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
            extensions=[apps],
        )

    def before_tool_call(self, callback: BeforeToolCall) -> None:
        """Install the call-time account and trading authorization provider."""
        self._before_tool_call = callback

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Context | None = None,
    ) -> CallToolResult | InputRequiredResult:
        """Authorize through the public SDK call path before dispatching a tool."""
        if context is None:
            context = Context(mcp_server=self, subscriptions=self._subscriptions)
        try:
            if self._before_tool_call is not None:
                blocked = await self._before_tool_call(name, arguments, context)
                if blocked is not None:
                    return blocked
            return await super().call_tool(name, arguments, context)
        except (MCPError, ToolError):
            raise
        except Exception as exc:
            # MCPServer.call_tool normally converts a tool crash to this safe wrapper.
            # Our authorization hook runs before that SDK boundary, so preserve the same
            # masking contract for failures in the hook.
            raise UnexpectedToolError(f"Error executing tool {name}") from exc

    async def close_live_client(self) -> None:
        if self.live_client is not None:
            await self.live_client.aclose()


def build_server(
    cfg: config_mod.Config | None = None,
    *,
    manage_url: authorization.ManageUrlProvider | None = None,
    access_state: authorization.StateProvider | None = None,
) -> DeltaMCP:
    """Build a server whose tool list does not depend on authorization state."""
    cfg = cfg or config_mod.load()
    mcp = DeltaMCP()
    live = replace(cfg, mode="read")
    client = DeltaClient(live)
    mcp.live_client = client
    log_path = debug_log.configure(cfg)
    trade_gate = trading.TradeGate(armed=False)
    trade_audit: audit_log.AuditLog | None = None

    market.register(mcp, client)
    account.register(mcp, client)
    trading.register(mcp, client, lambda: trade_audit, trade_gate)

    def http_identity(config: config_mod.Config) -> tuple[str, str | None, str | None]:
        return config.env, config.api_key, config.api_secret

    async def reconcile(
        ctx: Context,
    ) -> tuple[config_mod.Config, dict[str, str], authorization.AccessState]:
        """Refresh the client binding and authorization for one request."""
        nonlocal live, trade_audit
        who = request.context_client(ctx)
        shared = store.read()
        next_config = config_mod.load_for_client(who.name, shared)
        identity_changed = http_identity(live) != http_identity(next_config)
        if identity_changed:
            trade_gate.revoke()

        client.rebind(next_config)
        live = next_config
        if trade_gate.armed:
            trade_gate.revoke()
        trade_audit = None

        return next_config, shared, authorization.AccessState(
            credentials_ready=next_config.has_credentials,
            # DELTA_MCP_MODE is a legacy preference, not proof of browser consent.
            trading_enabled=False,
            client_name=who.name,
        )

    async def state_for(ctx: Context) -> authorization.AccessState:
        nonlocal trade_audit
        current = (
            await access_state(ctx)
            if access_state is not None
            else (await reconcile(ctx))[2]
        )
        trade_gate.bind_final_check(current.final_trading_check)
        if current.credentials_ready and current.trading_enabled:
            trade_gate.arm()
            trade_audit = audit_log.configure(replace(client.config, mode="trade"))
        else:
            if trade_gate.armed:
                trade_gate.revoke()
            trade_audit = None
        return current

    authorizer = authorization.ToolAuthorization(state_for, manage_url)
    mcp.before_tool_call(authorizer.before_call)

    @mcp.tool(annotations=hints.reads("Connection status", external=False))
    async def get_connection_status(ctx: Context) -> dict[str, object]:
        """Report connection and trading state without returning credential material."""
        who = request.context_client(ctx)
        if access_state is None:
            next_config, shared, current = await reconcile(ctx)
        else:
            current = await state_for(ctx)
            next_config = client.config
            shared = store.read()
        overridden = credentials.overridden_by_client(who.name, shared)
        return {
            "environment": next_config.env,
            "credentials_configured": current.credentials_ready,
            "account_tools_available": current.credentials_ready,
            "mode": "trade" if current.trading_enabled else "read",
            "mode_after_restart": "read",
            "restart_required": False,
            "overridden_by_client": overridden,
            "client_name": who.name,
            "client_version": who.version,
            "mode_setting": config_mod.mode_key(who.name),
            "client_identity": "self-reported name; convenience scope, not authentication",
            "version": PACKAGE_VERSION,
            "view": connection_app.VIEW_URI,
        }

    @mcp.tool(annotations=hints.reads("Trading status", external=False))
    async def get_trading_status(ctx: Context) -> dict[str, object]:
        """Report whether this client can send trading mutations."""
        current = await state_for(ctx)
        return {
            "mode": "trade" if current.trading_enabled else "read",
            "enabled": current.trading_enabled,
            "audit_log_path": str(trade_audit.path) if trade_audit else None,
        }

    @mcp.tool(annotations=hints.reads("Debug status", external=False))
    def get_debug_status() -> dict[str, object]:
        """Report whether debug logging is on and where its log is stored."""
        return {
            "enabled": log_path is not None,
            "log_path": str(log_path) if log_path is not None else None,
        }

    return mcp


async def serve(mcp: MCPServer) -> None:
    """Serve over stdio, the only transport."""
    try:
        await mcp.run_stdio_async()
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
    surface = "market+account+trade"
    banner = (
        f"[delta-exchange-mcp] startup stdio env={cfg.env} base_url={cfg.base_url} "
        f"mode=read surface={surface}"
    )
    if cfg.config_file is not None:
        banner += f" config={cfg.config_file}"
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
