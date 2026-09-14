"""Private log files shared by audit and debug logging."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from typing import TextIO


def _directory(path: Path) -> None:
    """Reject directory entries another OS user can replace on POSIX."""
    paths = [*reversed(path.absolute().parents), path.absolute()]
    for part in paths:
        try:
            info = part.lstat()
        except FileNotFoundError:
            try:
                part.mkdir(mode=0o700)
            except FileExistsError:
                pass
            info = part.lstat()
        if os.name == "posix":
            if info.st_uid not in {0, os.geteuid()}:
                raise PermissionError(f"log directory is owned by another user: {part}")
            if not stat.S_ISLNK(info.st_mode) and (
                info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX
            ):
                raise PermissionError(f"log directory is writable by other users: {part}")
        if not part.is_dir():
            raise NotADirectoryError(str(part))


def open_log(path: Path) -> TextIO:
    """Append through a checked descriptor, never a link or a shared existing inode.

    POSIX directory ownership checks also cover aliases such as macOS /var. Windows
    retains its user-directory ACL boundary; chmod is not used to claim ACL isolation.
    """
    _directory(path.parent)
    parent = path.parent.resolve(strict=True)
    _directory(parent)
    target = parent / path.name
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(target, flags, 0o600)
    except FileExistsError:
        # Reject links before opening, including Windows reparse points. O_NOFOLLOW
        # makes the final-component check atomic on POSIX.
        info = target.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise PermissionError(f"log path must not be a link: {target}") from None
        fd = os.open(
            target,
            os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PermissionError(f"log path must be an unshared regular file: {target}")
        if os.name == "posix":
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise PermissionError(f"log file is not private to this user: {target}")
            os.fchmod(fd, 0o600)
        stream = os.fdopen(fd, "a", encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise
    return stream


def fallback_path(name: str) -> Path:
    """Allocate a new private directory, never the old predictable shared child."""
    return Path(tempfile.mkdtemp(prefix="delta-exchange-mcp-")) / name
