"""Connect an account through a browser or asterisk-masked terminal prompts."""

import argparse
import sys

from delta_exchange_mcp import connection_cli, setup
from delta_exchange_mcp.auth.connection import (
    BROWSER_MANAGED_ENVIRONMENTS,
    ConnectionService,
)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    connection_cli.add_mode_arguments(parser)
    parser.add_argument("--api-key", help="API key for direct login")
    parser.add_argument("--api-secret", help="API secret for direct login")
    parser.add_argument(
        "--env",
        choices=BROWSER_MANAGED_ENVIRONMENTS,
        help="terminal/direct login environment; defaults to the current environment",
    )
    parser.add_argument(
        "--client", default="", help="exact MCP client name for trading approval"
    )


def run(
    *,
    mode: connection_cli.Mode = "auto",
    api_key: str | None = None,
    api_secret: str | None = None,
    environment: str | None = None,
    client_name: str = "",
) -> int:
    direct = api_key is not None or api_secret is not None
    if direct and (
        not api_key or not api_key.strip() or not api_secret or not api_secret.strip()
    ):
        return connection_cli.error(
            "Supply both --api-key and --api-secret with non-empty values.", 2
        )
    return connection_cli.run(
        command="login",
        mode=mode,
        client_name=client_name,
        terminal_requested=direct,
        needs_tty=not direct,
        environment=environment,
        terminal_action=lambda connection: _terminal(
            connection, api_key, api_secret, environment, client_name, direct
        ),
    )


def _terminal(
    connection: ConnectionService,
    api_key: str | None,
    api_secret: str | None,
    environment: str | None,
    client_name: str,
    direct: bool,
) -> int:
    if not direct and not client_name:
        client_name = input(
            "MCP client name for trading approval (Enter skips): "
        ).strip()
    actions = connection.actions(client_name)
    before = actions("status", {}, 0)
    current = str(before.content["environment"])
    if environment is None:
        environment = (
            current
            if direct
            else (
                input(
                    f"Environment ({'/'.join(BROWSER_MANAGED_ENVIRONMENTS)}) [{current}]: "
                )
                .strip()
                .lower()
                or current
            )
        )
    if environment not in BROWSER_MANAGED_ENVIRONMENTS:
        return connection_cli.error("Choose india_prod or india_testnet.", 2)
    if environment != current and before.content["environment_externally_managed"]:
        return connection_cli.error(
            "DELTA_MCP_ENV fixes the active environment. Unset it or choose the same environment.",
            2,
        )
    if not direct:
        api_key = connection_cli.read_secret("API key (masked): ")
        api_secret = connection_cli.read_secret("API secret (masked): ")
    result = actions(
        "credentials",
        {
            "operation": "replace",
            "environment": environment,
            "api_key": api_key,
            "api_secret": api_secret,
        },
        before.revision,
    )
    if result.stale or result.content.get("status") not in {"saved", "unverified"}:
        return connection_cli.error(str(result.content["message"]))
    print(
        f"Credentials saved for {environment} in the operating-system credential store."
    )
    if result.content["status"] == "unverified":
        print(str(result.content["message"]), file=sys.stderr)
    print("Trading is off until you explicitly approve it for an MCP client.")
    if direct:
        return 0
    if not client_name:
        return 0
    return approve_trading(actions, result.revision, environment, client_name)


def approve_trading(
    actions: setup.ActionHandler,
    revision: setup.Revision,
    environment: str,
    client_name: str,
) -> int:
    """Collect explicit consent for the exact snapshot displayed to the user."""
    answer = (
        input(
            f"Enable all 13 trading tools for {client_name!r} on {environment}? "
            "There are no built-in order-size limits. Type 'yes' to approve: "
        )
        .strip()
        .lower()
    )
    if answer != "yes":
        return 0
    acknowledged = (
        environment != "india_prod"
        or input("Production trading places real orders. Type 'yes' to confirm: ")
        .strip()
        .lower()
        == "yes"
    )
    if not acknowledged:
        return 0
    result = actions(
        "consent",
        {"environment": environment, "enabled": True, "acknowledged": acknowledged},
        revision,
    )
    if result.stale or result.content.get("status") != "enabled":
        return connection_cli.error(str(result.content["message"]))
    print(f"Trading approved for {client_name!r} on {environment}.")
    return 0
