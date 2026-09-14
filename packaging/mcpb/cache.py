"""Establish ownership of the executable MCPB cache before the shell uses it."""

import os
import stat
import sys
from collections import deque
from pathlib import Path


def _directory(path: Path, uid: int) -> Path:
    """Check every traversed directory, including both sides of symlinks."""
    absolute = path if path.is_absolute() else Path.cwd() / path
    current = Path(absolute.anchor)
    pending = deque(absolute.parts[1:])
    links = 0
    while True:
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, uid):
            raise PermissionError(f"untrusted MCPB cache directory: {current}")
        writable = info.st_mode & 0o022
        # A root-owned sticky temporary directory protects the next owned entry.
        sticky_root = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
        if writable and not (sticky_root and pending):
            raise PermissionError(f"MCPB cache directory is writable by other users: {current}")
        if not pending:
            if info.st_uid != uid:
                raise PermissionError(f"MCPB cache must be owned by the current user: {current}")
            return current

        component = pending.popleft()
        if component == "..":
            current = current.parent
            continue
        candidate = current / component
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            candidate.mkdir(mode=0o700)
            info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode):
            if info.st_uid not in (0, uid):
                raise PermissionError(f"untrusted MCPB cache symlink: {candidate}")
            links += 1
            if links > 40:
                raise OSError("too many MCPB cache symlinks")
            target = Path(os.readlink(candidate))
            if target.is_absolute():
                current = Path(target.anchor)
                pending.extendleft(reversed(target.parts[1:]))
            else:
                pending.extendleft(reversed(target.parts))
        else:
            current = candidate


def _tree(root: Path, uid: int) -> None:
    pending = [root]
    while pending:
        path = pending.pop()
        info = path.lstat()
        if info.st_uid != uid:
            raise PermissionError(f"untrusted MCPB cache entry: {path}")
        if stat.S_ISLNK(info.st_mode):
            target = Path(os.readlink(path))
            if not target.is_absolute():
                target = path.parent / target
            # Check the written target as well as its final destination: an external
            # alias that happens to lead back into the cache must not become trusted.
            lexical = Path(os.path.abspath(target))
            if not lexical.is_relative_to(root) or not path.resolve(strict=True).is_relative_to(root):
                raise PermissionError(f"MCPB cache symlink leaves the cache: {path}")
        elif stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode):
            if info.st_mode & 0o022:
                raise PermissionError(f"MCPB cache entry is writable by other users: {path}")
            if stat.S_ISDIR(info.st_mode):
                pending.extend(path.iterdir())
        else:
            raise PermissionError(f"MCPB cache entry is not a regular file or directory: {path}")


def prepare_cache(override: str = "") -> Path:
    if os.name != "posix":
        raise OSError("secure MCPB cache ownership checks require POSIX; use WSL on Windows")
    requested = (
        Path(override).expanduser()
        if override
        else Path.home() / ".cache" / "delta-exchange-mcp" / "mcpb-cli"
    )
    root = _directory(requested, os.geteuid())
    _tree(root, os.geteuid())
    return root


if __name__ == "__main__":
    try:
        print(prepare_cache(sys.argv[1] if len(sys.argv) > 1 else ""))
    except (OSError, RuntimeError) as exc:
        raise SystemExit(f"unsafe MCPB CLI cache: {exc}") from None
