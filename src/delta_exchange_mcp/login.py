"""Interactive `login` for people already sitting at a terminal.

One of several front-ends onto the same shared settings file — the in-chat form in
`form` fills exactly the same three keys for people who never open a terminal, and
hand-editing the file fills them too. The checking and writing live in `credentials`
so all of them behave identically.

This refuses to run without a terminal. `getpass` on its own does not: piping into it
prints a warning and then reads stdin anyway, so `echo $KEY | delta-exchange-mcp login`
would quietly succeed. That is exactly the shape an agent trying to be helpful would
reach for, and it would put the secret into shell history and into the agent's
transcript — the two places this whole design exists to keep it out of.
"""

from __future__ import annotations

import asyncio
import getpass
import sys

from delta_exchange_mcp import credentials, store
from delta_exchange_mcp.config import BASE_URLS, DASHBOARDS, DEFAULT_ENV


def _ask_env(default: str = DEFAULT_ENV) -> str | None:
    prompt = f"Environment {'/'.join(sorted(BASE_URLS))}\n  [{default}]: "
    answer = input(prompt).strip().lower() or default
    if answer not in BASE_URLS:
        print(f"  not an environment: {answer}", file=sys.stderr)
        return None
    return answer


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
        keep = "; Enter keeps saved" if can_keep_saved else ""
        entered_key = getpass.getpass(f"API key (hidden{keep}): ").strip()
        entered_secret = getpass.getpass(f"API secret (hidden{keep}): ").strip()
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

    overridden = credentials.overridden_by_client()
    if overridden:
        # Set in this shell, so any client launched from here inherits it — and a client's
        # own value always wins over the file, so the key just saved would do nothing.
        print(
            f"\nNote: {', '.join(overridden)} is set in this shell and takes precedence "
            "over the file for any client launched from here.",
            file=sys.stderr,
        )
    return 0
