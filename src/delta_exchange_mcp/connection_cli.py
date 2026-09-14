"""Shared browser selection, terminal input, and lifecycle for connection commands."""

import argparse
import os
import sys
import webbrowser
from collections.abc import Callable
from contextlib import closing
from typing import Literal

import anyio
from prompt_toolkit import PromptSession
from prompt_toolkit.clipboard import DummyClipboard
from prompt_toolkit.history import DummyHistory
from prompt_toolkit.input.defaults import create_input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.output.defaults import create_output

from delta_exchange_mcp.auth.connection import ConnectionService
from delta_exchange_mcp.auth.store import CredentialSource

Mode = Literal["auto", "browser", "terminal"]


class TerminalInputError(RuntimeError):
    """Masked input could not be collected from an interactive terminal."""


def add_mode_arguments(parser: argparse.ArgumentParser) -> None:
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
        help="use the terminal with asterisk-masked secret input",
    )
    parser.set_defaults(login_mode="auto")


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


def read_secret(label: str) -> str:
    """Show asterisks while typing, without history, clipboard, or plaintext fallback."""
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise TerminalInputError(
            "Masked credential input needs an interactive terminal."
        )
    bindings = KeyBindings()

    @bindings.add("c-r", eager=True)
    @bindings.add("c-s", eager=True)
    def ignore_search(event: KeyPressEvent) -> None:
        # History search has its own unmasked buffer, even with DummyHistory.
        del event

    try:
        with closing(create_input(stdin=sys.stdin)) as terminal_input:
            session = PromptSession(
                is_password=True,
                key_bindings=bindings,
                history=DummyHistory(),
                clipboard=DummyClipboard(),
                input=terminal_input,
                output=create_output(stdout=sys.stderr),
                enable_suspend=False,
                enable_system_prompt=False,
                enable_open_in_editor=False,
            )
            return session.prompt(label, set_exception_handler=False).strip()
    except (OSError, ValueError) as exc:
        raise TerminalInputError("This terminal cannot mask credential input.") from exc


def run(
    *,
    command: Literal["login", "config"],
    mode: Mode,
    client_name: str,
    terminal_action: Callable[[ConnectionService], int],
    terminal_requested: bool = False,
    needs_tty: bool = True,
    environment: str | None = None,
) -> int:
    """Keep both commands on the same native-store and browser/terminal boundaries."""
    if terminal_requested and mode == "browser":
        return error(
            "These arguments cannot be combined with --browser. Use the browser page to make changes.",
            2,
        )
    terminal = (
        terminal_requested
        or mode == "terminal"
        or (mode == "auto" and not browser_available())
    )
    if not terminal and environment is not None:
        return error(
            "Choose the environment on the browser page, or use --no-browser with --env.",
            2,
        )
    if terminal and needs_tty and not sys.stdin.isatty():
        return error(f"Terminal {command} needs an interactive terminal.", 2)

    connection = ConnectionService.open()
    try:
        if connection.credentials.source is not CredentialSource.OS_STORE:
            return error(
                "This command needs a persistent credential store. Install or unlock macOS Keychain, "
                "Windows Credential Manager, or Linux Secret Service, then retry. "
                "Process-memory credentials and approval would disappear when this command exits."
            )
        if not terminal:
            try:
                page = connection.open_page(client_name, open_browser=False)
            except OSError:
                if mode == "browser":
                    return error("The local browser connection page could not start.")
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
                if (
                    command == "login"
                    and not completed
                    and not status["credentials_configured"]
                ):
                    return error("Browser login closed without a connected account.")
                return 0
            connection.close()
            print(
                "The browser could not open. Continuing in the terminal.",
                file=sys.stderr,
            )
        if needs_tty and not sys.stdin.isatty():
            return error(
                f"The browser could not open, and terminal {command} needs an interactive terminal.",
                2,
            )
        return terminal_action(connection)
    except (KeyboardInterrupt, EOFError):
        return error(
            f"{command.capitalize()} cancelled. Any completed changes remain saved.",
            130,
        )
    except TerminalInputError as exc:
        return error(str(exc))
    finally:
        connection.close()
        anyio.run(connection.client.aclose)


def error(message: str, code: int = 1) -> int:
    print(f"[delta-exchange-mcp] {message}", file=sys.stderr)
    return code
