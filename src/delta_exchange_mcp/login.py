"""Interactive `login` for people already sitting at a terminal.

One of several front-ends onto the same shared settings file — the in-chat form in
`form` fills exactly the same three keys for people who never open a terminal, and
hand-editing the file fills them too. The checking and writing live in `credentials`
so all of them behave identically.

This refuses to run without a terminal. `getpass`, for one, does not: piping into it
prints a warning and then reads stdin anyway, so `echo $KEY | delta-exchange-mcp login`
would quietly succeed. That is exactly the shape an agent trying to be helpful would
reach for, and it would put the secret into shell history and into the agent's
transcript — the two places this whole design exists to keep it out of.
"""

from __future__ import annotations

import asyncio
import codecs
import os
import select
import sys
from collections.abc import Iterator
from typing import TextIO

from delta_exchange_mcp import credentials, store
from delta_exchange_mcp.config import BASE_URLS, DASHBOARDS, DEFAULT_ENV

if os.name == "nt":
    import msvcrt
else:
    import termios
    import tty

_TTY = "/dev/tty"
# A terminal sends a key's whole escape sequence in one write, so an escape with nothing
# behind it after this long is the Escape key itself. Vim's ttimeoutlen default.
_ESC_WAIT = 0.1


def _ask_env(default: str = DEFAULT_ENV) -> str | None:
    prompt = f"Environment {'/'.join(sorted(BASE_URLS))}\n  [{default}]: "
    answer = input(prompt).strip().lower() or default
    if answer not in BASE_URLS:
        print(f"  not an environment: {answer}", file=sys.stderr)
        return None
    return answer


def _ask_secret(prompt: str) -> str:
    """Read a credential from the terminal, echoing one * per character.

    getpass echoes nothing, so a paste that never landed looks exactly like one that did.
    Like getpass, it talks to the terminal itself, so a redirected stdout cannot hide the
    prompt.
    """
    if os.name == "nt":
        with open("CONOUT$", "w") as console:
            console.write(prompt)
            console.flush()
            return _collect(_windows_chars(), console)
    try:
        fd = os.open(_TTY, os.O_RDWR | os.O_NOCTTY)
    except OSError:  # no controlling terminal; getpass falls back the same way
        return _ask_posix(sys.stdin.fileno(), sys.stderr, prompt)
    try:
        with open(fd, "w", closefd=False) as terminal:
            return _ask_posix(fd, terminal, prompt)
    finally:
        os.close(fd)


def _ask_posix(fd: int, out: TextIO, prompt: str) -> str:
    out.write(prompt)
    out.flush()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        return _collect(_posix_chars(fd), out)
    finally:
        termios.tcsetattr(fd, termios.TCSAFLUSH, saved)


def _posix_chars(fd: int) -> Iterator[str]:
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    while True:
        byte = os.read(fd, 1)
        if not byte:
            raise EOFError
        if byte == b"\x1b" and not select.select([fd], [], [], _ESC_WAIT)[0]:
            continue
        yield from decoder.decode(byte)


def _windows_chars() -> Iterator[str]:
    while True:
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):  # first half of an arrow or function key
            msvcrt.getwch()
            continue
        if ch == "\x1b":  # the console never sends sequences, so this is Escape itself
            continue
        yield ch


def _collect(chars: Iterator[str], out: TextIO) -> str:
    """Build the credential from keystrokes, writing * for each character kept.

    The readers pass on an escape only when a sequence follows it.
    """
    typed: list[str] = []
    for ch in chars:
        if ch in ("\r", "\n"):
            break
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch == "\x04":
            if not typed:
                raise EOFError
        elif ch in ("\x7f", "\b"):
            if typed:
                typed.pop()
                out.write("\b \b")
        elif ch == "\x15":
            out.write("\b \b" * len(typed))
            typed.clear()
        elif ch == "\x1b":
            _skip_escape(chars)
        elif ch >= " ":
            typed.append(ch)
            out.write("*")
        out.flush()
    out.write("\n")
    out.flush()
    return "".join(typed)


def _skip_escape(chars: Iterator[str]) -> None:
    """Drop an escape sequence: arrow keys, and the markers around a bracketed paste."""
    kind = next(chars, "")
    if kind == "[":
        for ch in chars:
            if "@" <= ch <= "~":
                return
    elif kind == "O":
        next(chars, "")


def run(verify: bool = True) -> int:
    """Prompt for credentials and write them to the shared settings file."""
    if not sys.stdin.isatty():
        print(
            "login needs a terminal. Run it yourself rather than through a pipe or an "
            "assistant — piping would put your API secret in shell history.",
            file=sys.stderr,
        )
        return 2

    path = store.ensure()
    if path is None:
        print(f"cannot write {store.path()}", file=sys.stderr)
        return 1

    shared = store.read()
    saved_env_value = (shared.get("DELTA_MCP_ENV") or "").strip().lower()
    saved_env = saved_env_value if saved_env_value in BASE_URLS else None
    default_env = saved_env or DEFAULT_ENV
    saved_key = (shared.get("DELTA_API_KEY") or "").strip()
    saved_secret = (shared.get("DELTA_API_SECRET") or "").strip()
    has_saved_pair = bool(saved_key and saved_secret)
    can_keep_saved = has_saved_pair and saved_env is not None

    print(f"Storing credentials in {path}")
    print("Every MCP client on this machine reads it.")
    if can_keep_saved:
        print("Press Enter to keep the environment and saved credential pair.\n")
    else:
        print("Choose an environment and enter both parts of a credential pair.\n")

    try:
        env = _ask_env(default_env)
        if env is None:
            return 1
        print(f"  create a key at {DASHBOARDS.get(env, DASHBOARDS[DEFAULT_ENV])}")
        print("  the key must have permission for trading preferences")
        print(
            "  current Delta documentation does not establish whether Read Data alone "
            "is sufficient\n"
        )
        keep = " (Enter keeps saved)" if can_keep_saved else ""
        entered_key = _ask_secret(f"API key{keep}: ").strip()
        entered_secret = _ask_secret(f"API secret{keep}: ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\ncancelled, nothing written", file=sys.stderr)
        return 1

    if bool(entered_key) != bool(entered_secret):
        print(
            "both a key and its secret are needed. Enter both to replace the saved "
            "pair, or leave both blank to keep it.",
            file=sys.stderr,
        )
        return 1

    if entered_key:
        key, secret = entered_key, entered_secret
    elif not has_saved_pair:
        print(
            "No complete credential pair is saved. Enter both the API key and secret.",
            file=sys.stderr,
        )
        return 1
    elif saved_env is None:
        print(
            "The saved credential pair has no valid environment. Choose an environment "
            "and enter a new API key and secret.",
            file=sys.stderr,
        )
        return 1
    elif env != saved_env:
        print(
            f"The saved credentials belong to {saved_env}; changing environments "
            "requires a new API key and secret.",
            file=sys.stderr,
        )
        return 1
    else:
        key, secret = saved_key, saved_secret

    if verify:
        print(f"\nChecking against {BASE_URLS[env]} ...")
        result = asyncio.run(credentials.check(env, key, secret))
        if not result.reachable:
            # A flaky connection must not cost someone a key they typed correctly.
            print(f"  {result.detail}\n  saving anyway, unverified", file=sys.stderr)
        elif not result.ok:
            print(f"  {result.detail}\n\nNothing was saved.", file=sys.stderr)
            return 1
        else:
            print(f"  ok{' — ' + result.detail if result.detail else ''}")

    problem = credentials.save(env, key, secret)
    if problem is not None:
        print(problem, file=sys.stderr)
        return 1

    print(f"\nSaved to {path}. Restart your MCP client.")
    return 0
