"""Manage saved environments, credentials, and trading consent from one place."""

import argparse
import sys
from typing import Literal

from delta_exchange_mcp import connection_cli, login, setup
from delta_exchange_mcp.auth.connection import (
    BROWSER_MANAGED_ENVIRONMENTS,
    ConnectionService,
)

TradingMode = Literal["read", "trade"]


def add_arguments(parser: argparse.ArgumentParser) -> None:
    connection_cli.add_mode_arguments(parser)
    parser.add_argument(
        "--env",
        choices=BROWSER_MANAGED_ENVIRONMENTS,
        help="activate a saved environment through the terminal",
    )
    parser.add_argument(
        "--mode",
        choices=("read", "trade"),
        help="set this client's trading approval; trade requires interactive confirmation",
    )
    parser.add_argument(
        "--client",
        default="",
        help="exact MCP client name whose trading approval is managed",
    )


def run(
    *,
    mode: connection_cli.Mode = "auto",
    environment: str | None = None,
    trading_mode: TradingMode | None = None,
    client_name: str = "",
) -> int:
    shortcut = environment is not None or trading_mode is not None
    if trading_mode is not None and not client_name and not sys.stdin.isatty():
        return connection_cli.error(
            "Use --client NAME to select whose trading approval to change.", 2
        )
    return connection_cli.run(
        command="config",
        mode=mode,
        client_name=client_name,
        terminal_requested=shortcut,
        needs_tty=not shortcut or trading_mode == "trade",
        terminal_action=lambda connection: _terminal(
            connection, environment, trading_mode, client_name
        ),
    )


def _terminal(
    connection: ConnectionService,
    environment: str | None,
    trading_mode: TradingMode | None,
    client_name: str,
) -> int:
    shortcut = environment is not None or trading_mode is not None
    if not client_name and (not shortcut or trading_mode is not None):
        client_name = input("MCP client name (Enter skips): ").strip()
    if trading_mode is not None and not client_name:
        return connection_cli.error(
            "Select an MCP client name before changing trading approval.", 2
        )
    actions = connection.actions(client_name)
    if shortcut:
        before = actions("status", {}, 0)
        if environment is not None:
            result = _select_environment(actions, before, environment)
            if result:
                return result
        before = actions("status", {}, 0)
        _show(before)
        if trading_mode is not None:
            return _set_mode(actions, before, client_name, trading_mode)
        return 0
    return _menu(connection, client_name)


def _menu(connection: ConnectionService, client_name: str) -> int:
    print(
        "Changes apply immediately. Environment selection is shared; trading approval is per client."
    )
    while True:
        actions = connection.actions(client_name)
        before = actions("status", {}, 0)
        _show(before)
        print(
            "1. Change environment\n2. Change mode\n3. Connect or replace credentials\n4. Disconnect\n5. Change client\n0. Done"
        )
        choice = input("Choose [0]: ").strip() or "0"
        environment = str(before.content["environment"])
        if choice == "0":
            return 0
        if choice == "5":
            client_name = input("MCP client name (Enter clears): ").strip()
            continue
        if choice == "1":
            selected = (
                input(
                    f"Environment ({'/'.join(BROWSER_MANAGED_ENVIRONMENTS)}) [{environment}]: "
                )
                .strip()
                .lower()
                or environment
            )
            result = _select_environment(actions, before, selected)
            if result:
                return result
            current = actions("status", {}, 0)
            _show(current)
            if not current.content["credentials_configured"]:
                if (
                    input("No account connected. Connect now? Type 'yes': ")
                    .strip()
                    .lower()
                    == "yes"
                ):
                    result = _connect(actions, current)
                    if result:
                        return result
        elif choice == "2":
            if not client_name:
                connection_cli.error(
                    "Choose Change client before changing trading approval.", 2
                )
                continue
            current_mode = "trade" if before.content["trading"]["enabled"] else "read"
            selected_mode = (
                input(f"Mode (read/trade) [{current_mode}]: ").strip().lower()
                or current_mode
            )
            if selected_mode not in {"read", "trade"}:
                connection_cli.error("Choose read or trade.", 2)
                continue
            result = _set_mode(actions, before, client_name, selected_mode)
            if result:
                return result
        elif choice == "3":
            result = _connect(actions, before)
            if result:
                return result
        elif choice == "4":
            if (
                input(f"Disconnect {environment}? Type 'yes' to confirm: ")
                .strip()
                .lower()
                == "yes"
            ):
                result = actions(
                    "credentials",
                    {"operation": "disconnect", "environment": environment},
                    before.revision,
                )
                if not _report(result, {"disconnected", "not_connected"}):
                    return 1
        else:
            connection_cli.error("Choose a number from 0 to 5.", 2)


def _show(snapshot: setup.ActionResult) -> None:
    state = snapshot.content
    environment = str(state["environment"])
    account = state["environments"][environment]
    connected = "connected" if account["connected"] else "disconnected"
    mode = "trade" if state["trading"]["enabled"] else "read"
    print(
        f"\nEnvironment: {environment}\nClient: {state['client_name']!r}\nConnection: {connected}\nMode: {mode}"
    )
    for field in ("connection_error", "consent_error"):
        if state[field]:
            print(f"{field}: {state[field]}", file=sys.stderr)


def _select_environment(
    actions: setup.ActionHandler, before: setup.ActionResult, environment: str
) -> int:
    if environment not in BROWSER_MANAGED_ENVIRONMENTS:
        return connection_cli.error("Choose india_prod or india_testnet.", 2)
    if environment == before.content["environment"]:
        return 0
    result = actions(
        "credentials",
        {"operation": "activate", "environment": environment},
        before.revision,
    )
    return 0 if _report(result, {"selected"}) else 1


def _connect(actions: setup.ActionHandler, before: setup.ActionResult) -> int:
    environment = str(before.content["environment"])
    if environment not in BROWSER_MANAGED_ENVIRONMENTS:
        return connection_cli.error(
            "Choose production or testnet before connecting.", 2
        )
    if before.content["environments"][environment]["externally_managed"]:
        return connection_cli.error(
            "Credentials are managed by the process environment. Change them there."
        )
    key = connection_cli.read_secret("API key (masked): ")
    secret = connection_cli.read_secret("API secret (masked): ")
    result = actions(
        "credentials",
        {
            "operation": "replace",
            "environment": environment,
            "api_key": key,
            "api_secret": secret,
        },
        before.revision,
    )
    return 0 if _report(result, {"saved", "unverified"}) else 1


def _set_mode(
    actions: setup.ActionHandler,
    before: setup.ActionResult,
    client_name: str,
    mode: str,
) -> int:
    state = before.content
    environment = str(state["environment"])
    account = state["environments"][environment]
    if not account["connected"]:
        if mode == "read":
            print("Trading is off. Connect an account when you need account access.")
            return 0
        return connection_cli.error(
            "Connect an account before enabling trading. Run config to manage credentials."
        )
    if not account["persistent"]:
        return connection_cli.error(
            "This connection is process-only. Manage its trading approval in the running MCP client."
        )
    if mode == "trade":
        if state["trading"]["enabled"]:
            print("Trading is already enabled for this client.")
            return 0
        return login.approve_trading(actions, before.revision, environment, client_name)
    result = actions(
        "consent", {"environment": environment, "enabled": False}, before.revision
    )
    return 0 if _report(result, {"disabled"}) else 1


def _report(result: setup.ActionResult, accepted: set[str]) -> bool:
    if result.stale or result.content.get("status") not in accepted:
        connection_cli.error(str(result.content["message"]))
        return False
    print(str(result.content["message"]))
    return True
