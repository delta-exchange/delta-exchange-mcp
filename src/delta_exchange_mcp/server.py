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

from delta_exchange_mcp import analytics, audit_log
from delta_exchange_mcp import authorization
from delta_exchange_mcp import connection_app
from delta_exchange_mcp import config as config_mod
from delta_exchange_mcp import debug_log
from delta_exchange_mcp import hints
from delta_exchange_mcp import identity
from delta_exchange_mcp.auth.connection import ConnectionService
from delta_exchange_mcp.client import DeltaClient
from delta_exchange_mcp.tools import account, market, trading
from delta_exchange_mcp.version import PACKAGE_VERSION

_ENV_HELP = """\
normal account setup:
  Call setup_credentials from the MCP client. Manage Connection is the normal environment
  selector and credential interface.

advanced externally managed compatibility overrides:
  DELTA_MCP_ENV         force india_prod, india_testnet, or india_devnet from the launcher
  DELTA_API_KEY         externally managed process credential, used with DELTA_API_SECRET
  DELTA_API_SECRET      externally managed process credential, used with DELTA_API_KEY
  DELTA_MCP_MODE        ignored for authorization; browser trading consent is required

non-secret diagnostics and paths:
  DELTA_MCP_DEBUG       1/true/yes/on to trace HTTP requests and responses to a file
  DELTA_MCP_DEBUG_FILE  override the debug log path
  DELTA_MCP_AUDIT       off/false/0/no to disable the trading audit log
  DELTA_MCP_AUDIT_FILE  override the audit log path
  DELTA_MCP_CONFIG_FILE override the shared settings file path

API keys and secrets are managed in the browser. The server uses the operating-system
credential service when available. Otherwise it uses process memory and no plaintext
fallback. Existing complete DELTA_API_KEY and DELTA_API_SECRET process values remain
supported as externally managed compatibility settings. Manage Connection reports these
overrides but cannot change their launcher source. Trading requires browser consent for the
exact client name, environment, and credential revision.

Production and testnet API keys are separate. Select the environment where the key was
created. The server speaks MCP over stdio and is normally launched by a client rather than
by hand.
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
        self.connection_service: ConnectionService | None = None
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
            with analytics.scope(context, name):
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
        if self.connection_service is not None:
            self.connection_service.close()
        if self.live_client is not None:
            await self.live_client.aclose()


def build_server(
    cfg: config_mod.Config | None = None,
    *,
    manage_url: authorization.ManageUrlProvider | None = None,
    access_state: authorization.StateProvider | None = None,
    connection_service: ConnectionService | None = None,
) -> DeltaMCP:
    """Build a server whose tool list does not depend on authorization state."""
    service = connection_service or ConnectionService.open(cfg)
    mcp = DeltaMCP()
    mcp.connection_service = service
    client = service.client
    mcp.live_client = client
    log_path = debug_log.configure(client.config)
    trade_gate = trading.TradeGate(armed=False)

    market.register(mcp, client)
    account.register(mcp, client)
    trading.register(
        mcp,
        client,
        lambda: audit_log.configure(replace(client.binding_config, mode="trade")),
        trade_gate,
    )

    async def state_for(ctx: Context) -> authorization.AccessState:
        current = (
            await access_state(ctx)
            if access_state is not None
            else await service.access_state(ctx)
        )
        trade_gate.bind_final_check(current.final_trading_check)
        if current.credentials_ready and current.trading_enabled:
            trade_gate.arm()
        else:
            if trade_gate.armed:
                trade_gate.revoke()
        return current

    authorizer = authorization.ToolAuthorization(
        state_for,
        manage_url or service.manage_url,
    )
    mcp.before_tool_call(authorizer.before_call)

    @mcp.tool(annotations=hints.reads("Connection status", external=False))
    async def get_connection_status(ctx: Context) -> dict[str, object]:
        """Report connection and trading state without returning credential material."""
        status = service.status(ctx)
        if access_state is not None:
            current = await state_for(ctx)
            status["credentials_configured"] = current.credentials_ready
            status["account_tools_available"] = current.credentials_ready
            trading_status = status.get("trading")
            if isinstance(trading_status, dict):
                trading_status["enabled"] = current.trading_enabled
        status["client_identity"] = (
            "self-reported exact name; consent partition, not authentication"
        )
        status["version"] = PACKAGE_VERSION
        status["view"] = connection_app.VIEW_URI
        return status

    @mcp.tool(annotations=hints.reads("Trading status", external=False))
    async def get_trading_status(ctx: Context) -> dict[str, object]:
        """Report whether this client can send trading mutations."""
        current = await state_for(ctx)
        active_audit = (
            audit_log.configure(replace(client.config, mode="trade"))
            if current.trading_enabled
            else None
        )
        return {
            "mode": "trade" if current.trading_enabled else "read",
            "enabled": current.trading_enabled,
            "audit_log_path": str(active_audit.path) if active_audit else None,
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
    sub.add_parser(
        "login",
        help="open the browser connection page",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    if args.command == "login":
        connection = ConnectionService.open()
        try:
            page = connection.open_page(open_browser=True)
            print(
                f"[delta-exchange-mcp] Manage Connection: {page.url}",
                file=sys.stderr,
            )
            page.wait()
        finally:
            connection.close()
            anyio.run(connection.client.aclose)
        return

    mcp = build_server()
    cfg = mcp.live_client.config
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
    if mcp.connection_service is not None and mcp.connection_service.credential_error:
        print(
            "[delta-exchange-mcp] account authorization is unavailable; market data "
            "remains available and Manage Connection can repair it.",
            file=sys.stderr,
        )
    anyio.run(serve, mcp)
