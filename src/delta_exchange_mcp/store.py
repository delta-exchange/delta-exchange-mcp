"""Non-secret settings shared by every MCP client on this machine."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
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
# Delta Exchange MCP settings shared by every MCP client on this machine.
#
# Manage API credentials and trading consent in the browser page that the MCP opens.
# This file contains non-secret runtime settings only.
DELTA_MCP_ENV=india_prod
DELTA_MCP_ENV_GENERATION=0
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
_LEGACY_CREDENTIAL_NAMES = frozenset({"DELTA_API_KEY", "DELTA_API_SECRET"})
_ENVIRONMENT_KEY = "DELTA_MCP_ENV"
_ENVIRONMENT_GENERATION_KEY = "DELTA_MCP_ENV_GENERATION"
_DEFAULT_ENVIRONMENT = "india_prod"


class SettingsConflictError(RuntimeError):
    """A shared-settings compare-and-swap used an old value."""


def _environment_state(
    values: Mapping[str, str | None], default_environment: str
) -> tuple[str, int]:
    environment = (values.get(_ENVIRONMENT_KEY) or default_environment).strip().lower()
    raw_generation = values.get(_ENVIRONMENT_GENERATION_KEY) or "0"
    try:
        generation = int(raw_generation)
    except ValueError:
        generation = -1
    return environment, generation


def environment_state(default_environment: str) -> tuple[str, int]:
    """Read one atomic environment and generation snapshot."""
    try:
        return _environment_state(dotenv_values(path()), default_environment)
    except OSError:
        return default_environment, 0


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


def _publish(target: Path, values: dict[str, str]) -> str | None:
    """Publish values while the caller holds the shared-settings lock."""
    staged = None
    try:
        # Carry the owner bits across and drop access for group and other users.
        # This also protects a legacy file until automatic migration removes secrets.
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
                return f"the shared settings key {key} could not be updated"
        os.chmod(staged, mode)
        os.replace(staged, target)
        staged = None
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
    return None


def write(values: dict[str, str]) -> str | None:
    """Set non-secret values atomically, or leave the shared file unchanged."""
    if _LEGACY_CREDENTIAL_NAMES.intersection(values):
        return "API credentials must be managed through Manage Connection"

    target = ensure()
    if target is None:
        return "the shared settings could not be updated"

    try:
        with _write_lock(target):
            published = dict(values)
            selected = published.get(_ENVIRONMENT_KEY)
            if selected is not None:
                current, generation = _environment_state(
                    dotenv_values(target), _DEFAULT_ENVIRONMENT
                )
                if generation < 0:
                    return "the shared environment generation is invalid"
                if selected.strip().lower() != current:
                    published[_ENVIRONMENT_GENERATION_KEY] = str(generation + 1)
            return _publish(target, published)
    except OSError:
        # A browser result must not include an operating-system exception or local path.
        return "the shared settings could not be updated"


def compare_and_write_environment(
    expected_environment: str,
    expected_generation: int,
    environment: str,
    *,
    default_environment: str,
    before_publish: Callable[[], None],
) -> str | None:
    """Select an environment only if the shared value still matches the page token."""
    target = ensure()
    if target is None:
        return "the shared settings could not be updated"

    try:
        with _write_lock(target):
            active, generation = _environment_state(
                dotenv_values(target), default_environment
            )
            if (
                generation < 0
                or active != expected_environment
                or generation != expected_generation
            ):
                raise SettingsConflictError("the active environment changed")
            if active == environment:
                return None
            before_publish()
            return _publish(
                target,
                {
                    _ENVIRONMENT_KEY: environment,
                    _ENVIRONMENT_GENERATION_KEY: str(generation + 1),
                },
            )
    except OSError:
        return "the shared settings could not be updated"


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
        f"{target} may contain legacy API credentials until migration completes and is "
        f"readable by other users on this machine. Restrict it with: chmod 600 {target}"
    )
