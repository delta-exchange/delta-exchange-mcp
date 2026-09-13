"""Browser and terminal entry points for the shared connection service."""

import argparse
import getpass
import os
import sys
import warnings
import webbrowser
from typing import Literal

import anyio

from delta_exchange_mcp.auth.connection import (
    BROWSER_MANAGED_ENVIRONMENTS,
    ConnectionService,
)
from delta_exchange_mcp.auth.store import CredentialSource

Mode = Literal["auto", "browser", "terminal"]


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Declare login modes and direct credential arguments."""
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--browser",
        dest="login_mode",
        action="store_const",
        const="browser",
        help="use the browser connection page",
    )
    modes.add_argument(
        "--device",
        "--no-browser",
        dest="login_mode",
        action="store_const",
        const="terminal",
        help="enter credentials directly in the terminal",
    )
    parser.set_defaults(login_mode="auto")
    parser.add_argument("--api-key", help="API key for direct login")
    parser.add_argument("--api-secret", help="API secret for direct login")
    parser.add_argument(
        "--env",
        choices=BROWSER_MANAGED_ENVIRONMENTS,
        help="terminal/direct login environment; defaults to the current environment",
    )
    parser.add_argument(
        "--client",
        default="",
        help="exact MCP client name for trading approval",
    )


def browser_available() -> bool:
    """Select terminal entry for SSH and sessions without a graphical browser."""
    if any(
        os.environ.get(name) for name in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")
    ):
        return False
    if sys.platform not in {"darwin", "win32"} and not any(
        os.environ.get(name) for name in ("DISPLAY", "WAYLAND_DISPLAY")
    ):
        return False
    try:
        webbrowser.get()
    except webbrowser.Error:
        return False
    return True


def run(
    *,
    mode: Mode = "auto",
    api_key: str | None = None,
    api_secret: str | None = None,
    environment: str | None = None,
    client_name: str = "",
) -> int:
    """Connect an account without exposing credentials in tool arguments or output."""
    direct = api_key is not None or api_secret is not None
    if direct and (
        not api_key or not api_key.strip() or not api_secret or not api_secret.strip()
    ):
        return _error(
            "Supply both --api-key and --api-secret with non-empty values.", 2
        )
    if direct and mode == "browser":
        return _error("Credential arguments cannot be combined with --browser.", 2)
    terminal = (
        direct or mode == "terminal" or (mode == "auto" and not browser_available())
    )
    if not terminal and environment is not None:
        return _error(
            "Choose the environment on the browser page, or use --no-browser with --env.",
            2,
        )
    if terminal and not direct and not sys.stdin.isatty():
        return _error(
            "Terminal login needs an interactive terminal. Supply both credential arguments for direct login.",
            2,
        )

    connection = ConnectionService.open()
    try:
        if connection.credentials.source is not CredentialSource.OS_STORE:
            return _error(
                "Login needs a persistent credential store. Install or unlock macOS Keychain, "
                "Windows Credential Manager, or Linux Secret Service, then retry. "
                "Process-memory credentials would disappear when this command exits."
            )
        if not terminal:
            try:
                page = connection.open_page(client_name, open_browser=False)
            except OSError:
                if mode == "browser":
                    return _error("The local browser connection page could not start.")
                page = None
            opened = False
            if page is not None:
                try:
                    opened = webbrowser.open(page.url)
                except (OSError, webbrowser.Error):
                    pass
            if page is not None and (opened or mode == "browser"):
                print(
                    f"[delta-exchange-mcp] Manage Connection: {page.url}",
                    file=sys.stderr,
                )
                if not client_name:
                    print(
                        "Use --client NAME for trading approval that survives this command.",
                        file=sys.stderr,
                    )
                completed = page.wait()
                status = connection.actions(client_name)("status", {}, 0).content
                if not completed and not status["credentials_configured"]:
                    return _error("Browser login closed without a connected account.")
                return 0
            connection.close()
            print(
                "The browser could not open. Continuing in the terminal.",
                file=sys.stderr,
            )
        if not direct and not sys.stdin.isatty():
            return _error(
                "The browser could not open, and terminal login needs an interactive terminal.",
                2,
            )
        return _terminal(
            connection, api_key, api_secret, environment, client_name, direct
        )
    except (KeyboardInterrupt, EOFError):
        return _error("Login cancelled.", 130)
    except getpass.GetPassWarning:
        return _error("This terminal cannot hide credential input. Login stopped.")
    finally:
        connection.close()
        anyio.run(connection.client.aclose)


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
        return _error("Choose india_prod or india_testnet.", 2)
    if environment != current and before.content["environment_externally_managed"]:
        return _error(
            "DELTA_MCP_ENV fixes the active environment. Unset it or choose the same environment.",
            2,
        )
    if not direct:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            api_key = getpass.getpass("API key (hidden): ").strip()
            api_secret = getpass.getpass("API secret (hidden): ").strip()
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
        return _error(str(result.content["message"]))
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
        result.revision,
    )
    if result.stale or result.content.get("status") != "enabled":
        return _error(str(result.content["message"]))
    print(f"Trading approved for {client_name!r} on {environment}.")
    return 0


def _error(message: str, code: int = 1) -> int:
    print(f"[delta-exchange-mcp] {message}", file=sys.stderr)
    return code
