"""Settings shared by every MCP client on this machine.

An MCP client's own config is an awkward home for an API key. The shape differs per
client, it is JSON or TOML that someone non-technical has to edit without breaking,
the same key has to be pasted again for every client, and the project-scoped variants
(`.cursor/mcp.json`, `.vscode/mcp.json`) are files people commit. This module offers
one plain `KEY=value` file instead, read by whichever client launched the server.

The client's own environment still wins, so the Claude Desktop bundle form and
VS Code's masked prompts keep working exactly as they do today. See `config.setting`
for the precedence rule.

Parsing goes through `python-dotenv` rather than a hand-rolled split, because the
mistakes a non-developer makes in this file are the ones that fail silently:
`KEY="quoted"` keeping its quotes, `export KEY=v`, a trailing `# comment`, or CRLF
line endings all corrupt a credential in ways that surface later as a signing error
indistinguishable from a bad key.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from dotenv import dotenv_values, set_key

if os.name == "nt":
    import msvcrt
else:
    import fcntl

DEFAULT_DIR = Path.home() / ".delta-exchange-mcp"
DEFAULT_NAME = "config.env"

TEMPLATE = """\
# Delta Exchange MCP - settings shared by every MCP client on this machine.
#
# Market data works with this file empty. Fill these in to let an assistant read
# your own account. Create a key under Account -> API Keys at
# https://www.delta.exchange/app/account/manageapikeys. The key must have
# permission for trading preferences. Current Delta documentation does not
# establish whether Read Data alone is sufficient.
#
# Match the environment to where the key came from: a key from delta.exchange
# works only with india_prod, one from demo.delta.exchange only with
# india_testnet. Mixing them returns InvalidApiKey.
#
# Your MCP client's own settings take precedence over this file.

DELTA_API_KEY=
DELTA_API_SECRET=
DELTA_MCP_ENV=india_prod

# Trading is stored per client, never under the shared name below, so switching it
# on in one place cannot arm every assistant on this machine at once. The in-chat
# form writes the scoped name for you; the client name comes from its handshake.
# Ask get_connection_status for this client's exact mode_setting, or use the form.
# DELTA_MCP_MODE_<READABLE>_<DIGEST>=trade
#
# This unscoped name is read only from a client's own environment, never from here:
# DELTA_MCP_MODE=trade
"""


def path() -> Path:
    """Where the shared settings file lives.

    `DELTA_MCP_CONFIG_FILE` overrides it, and is read from the process environment
    only — a file cannot name itself.
    """
    override = (os.environ.get("DELTA_MCP_CONFIG_FILE") or "").strip()
    return Path(override).expanduser() if override else DEFAULT_DIR / DEFAULT_NAME


def read() -> dict[str, str]:
    """Values from the shared file, dropping any left blank.

    A missing file is the ordinary first-run state, and an unreadable one must not
    stop market data from working, so neither is an error.
    """
    try:
        return {key: value for key, value in dotenv_values(path()).items() if value}
    except OSError:
        return {}


def ensure() -> Path | None:
    """Create the file from a commented template when it is absent.

    Returns the path once the file exists, or None when it could not be created —
    a read-only or sandboxed filesystem must not stop the server from starting.
    The template carries the instructions someone needs at the moment they open it,
    which is the whole reason the file is written before anyone asks for it.
    """
    target = path()
    try:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        # Includes FileExistsError when the parent path is an ordinary file. That means
        # the location is unusable, which is the opposite of what the same exception
        # means one step below, so the two cannot share a handler.
        return None
    try:
        # Exclusive create rather than a prior exists() check: two clients launching
        # the server at the same moment is normal, and one must not truncate the
        # other's file in the gap between checking and writing.
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return target
    except OSError:
        return None
    with os.fdopen(fd, "w") as handle:
        handle.write(TEMPLATE)
    return target


_LOCK_TIMEOUT_SECONDS = 10.0
_LOCK_POLL_SECONDS = 0.05


@contextmanager
def _write_lock(target: Path) -> Iterator[None]:
    """Serialize copy-modify-replace writers across processes.

    The hidden lock file contains no settings and stays beside the config deliberately.
    Removing a lock path after release is unsafe: a waiter may still hold the old inode
    while a third process creates and locks a new one. The kernel releases the advisory
    lock automatically if a writer crashes, so no stale-lock deletion is needed.
    """
    lock_path = target.with_name(f".{target.name}.lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    locked = False
    try:
        if os.name == "nt":
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            while not locked:
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    locked = True
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"timed out waiting for {lock_path}") from exc
                    time.sleep(_LOCK_POLL_SECONDS)
        else:
            while not locked:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"timed out waiting for {lock_path}") from exc
                    time.sleep(_LOCK_POLL_SECONDS)
        yield
    finally:
        if locked:
            if os.name == "nt":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def write(values: dict[str, str]) -> str | None:
    """Set all of `values` in the shared file at once, or leave the file as it was.

    The all-at-once part is the point. `set_key` rewrites the whole file and renames it
    into place for every single key, so writing a credential one key at a time publishes
    three successive versions of the file. A failure on the third leaves a new API key
    sitting beside the previous secret — a pair that was never issued together, which
    still reads as complete, still registers the account tools, and fails every signed
    request. Staging every change on a copy and renaming once makes it all or nothing.

    `set_key` still does the line editing rather than a hand-rolled rewrite, because it
    already preserves the template's comments and quotes whatever someone can type:
    spaces, quotes, or a stray `=` that a naive `NAME=value` append would turn into a
    second setting.
    """
    target = ensure()
    if target is None:
        return f"cannot write {path()}"

    staged = None
    try:
        with _write_lock(target):
            # Owner bits are carried across, group and other are dropped. A write is the one
            # moment a *new* secret enters this file, and publishing it into a group- or
            # world-readable file would hand it to every other account on the machine — with
            # nothing said, because the permission warning only runs at startup. Masking here
            # rather than forcing 0600 leaves an owner who chose 0400 with the mode they chose.
            mode = stat.S_IMODE(target.stat().st_mode) & 0o700
            handle, name = tempfile.mkstemp(
                dir=target.parent, prefix=".config-", suffix=".tmp"
            )
            os.close(handle)
            staged = Path(name)
            shutil.copyfile(target, staged)
            for key, value in values.items():
                written, _, _ = set_key(str(staged), key, value)
                if not written:
                    return f"could not write {key} to {target}"
            os.chmod(staged, mode)
            os.replace(staged, target)
            staged = None
    except OSError as exc:
        # Reported rather than raised because a caller may be a tool answering a form,
        # where an exception becomes a protocol error the person cannot act on.
        return f"could not write {', '.join(values)} to {target}: {exc}"
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
    return None


def insecure_permissions() -> str | None:
    """A warning when the file is readable by users other than its owner.

    Reported rather than raised. Refusing to start would take away market data,
    which needs no credentials at all, over a file the user may not even have
    filled in.
    """
    target = path()
    try:
        mode = target.stat().st_mode
    except OSError:
        return None
    if not mode & (stat.S_IRGRP | stat.S_IROTH):
        return None
    return (
        f"{target} can hold API credentials but is readable by other users on this "
        f"machine. Restrict it with: chmod 600 {target}"
    )
