from __future__ import annotations

import argparse
import sys

from mcp.server.mcpserver import MCPServer

from delta_exchange_mcp import audit_log
from delta_exchange_mcp import config as config_mod
from delta_exchange_mcp import debug_log
from delta_exchange_mcp.client import DeltaClient
from delta_exchange_mcp.tools import account, market, trading
from delta_exchange_mcp.version import PACKAGE_VERSION

_ENV_HELP = """\
configuration (environment variables only — this server takes no options):
  DELTA_MCP_ENV         india_prod (default), india_testnet, india_devnet
  DELTA_API_KEY         optional; requires DELTA_API_SECRET for the account tools
  DELTA_API_SECRET      required alongside DELTA_API_KEY
  DELTA_MCP_MODE        read (default) or trade; trade registers the mutating tools
                        when both credentials are set
  DELTA_MCP_DEBUG       1/true/yes/on to trace HTTP requests and responses to a file
  DELTA_MCP_DEBUG_FILE  override the debug log path
  DELTA_MCP_AUDIT       off/false/0/no to disable the trade-mode audit log
  DELTA_MCP_AUDIT_FILE  override the audit log path

Prod and testnet API keys are separate; DELTA_MCP_ENV must match the dashboard the
key was created on. The server speaks MCP over stdio and is normally launched by a
client rather than by hand.
"""


def build_server(cfg: config_mod.Config | None = None) -> MCPServer:
    cfg = cfg or config_mod.load()
    mcp = MCPServer("delta-exchange", version=PACKAGE_VERSION)
    client = DeltaClient(cfg)

    log_path = debug_log.configure(cfg)

    market.register(mcp, client)
    if cfg.has_credentials:
        account.register(mcp, client)

    trade_audit = None
    if cfg.has_credentials and cfg.mode == "trade":
        trade_audit = audit_log.configure(cfg)
        trading.register(mcp, client, trade_audit)

        @mcp.tool()
        def get_trading_status() -> dict[str, object]:
            """Trading mode status and the audit log path (None if auditing is disabled).

            Use this to tell the user that mutations are enabled and where the audit log lives.
            """
            return {
                "mode": cfg.mode,
                "audit_log_path": str(trade_audit.path) if trade_audit else None,
            }

    if log_path is not None:

        @mcp.tool()
        def get_debug_status() -> dict[str, object]:
            """Whether debug logging is on and the absolute path of the current log file.

            Use this to tell the user where to find / fetch the HTTP debug log.
            """
            return {"enabled": True, "log_path": str(log_path)}

    return mcp


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
    return parser


def main(argv: list[str] | None = None) -> None:
    build_parser().parse_args(argv)

    cfg = config_mod.load()
    mcp = build_server(cfg)
    surface = "market+account" if cfg.has_credentials else "market"
    trade_on = cfg.has_credentials and cfg.mode == "trade"
    if trade_on:
        surface += "+trade"
    banner = (
        f"[delta-exchange-mcp] stdio env={cfg.env} base_url={cfg.base_url} "
        f"mode={cfg.mode} surface={surface}"
    )
    if trade_on:
        audit = audit_log.configure(cfg)  # idempotent: appends to the same file path
        banner += f" audit={audit.path if audit else 'off'}"
    if cfg.debug:
        log_path = debug_log.configure(cfg)  # idempotent — returns the same path
        if log_path is not None:  # configure returns None if the log file can't be opened
            banner += f" debug=on log={log_path}"
    print(banner, file=sys.stderr)
    mcp.run()
