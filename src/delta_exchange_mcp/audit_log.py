"""Append-only audit log for trading mutations (`DELTA_MCP_MODE=trade`).

Every mutating tool call — including dry-runs — is recorded as one JSON line so the user
has a durable record of what the assistant placed, edited, cancelled or closed. Request
bodies carry NO credentials (api-key / secret / signature live only in HTTP headers, which
are never written here), so the recorded params are safe; they may still reference order
ids and sizes, so the file is created owner-only (0600).

This module is separate from `debug_log.py`: debug logging is opt-in wire tracing, the audit
log is an always-on safety record whenever trading is enabled.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from delta_exchange_mcp.config import Config, setting


def _resolve_path() -> Path:
    override = setting("DELTA_MCP_AUDIT_FILE")
    if override:
        return Path(override).expanduser()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"audit-{stamp}-{os.getpid()}.log"
    return Path.home() / ".delta-exchange-mcp" / "audit" / name


class AuditLog:
    def __init__(self, path: Path, env: str):
        self.path = path
        self._env = env

    def record(
        self,
        tool: str,
        params: dict[str, Any],
        *,
        result: Any = None,
        error: str | None = None,
        dry_run: bool = False,
    ) -> None:
        """Append one JSON line describing a mutation attempt. Best-effort; never raises."""
        entry: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "env": self._env,
            "tool": tool,
            "dry_run": dry_run,
            "params": params,
        }
        if error is not None:
            entry["error"] = error
        elif result is not None:
            entry["result"] = _summarize(result)
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except OSError as e:
            print(f"[delta-exchange-mcp] audit write failed: {e}", file=sys.stderr)


def _summarize(result: Any) -> Any:
    """Keep the audit line compact: pull the order id/state out of a Delta result envelope."""
    if isinstance(result, dict):
        inner = result.get("result", result)
        if isinstance(inner, dict):
            return {k: inner[k] for k in ("id", "state", "client_order_id") if k in inner} or inner
    return result


# Explicit kill switch: the audit log is ON by default in trade mode, but a user who
# really doesn't want a file on disk can set DELTA_MCP_AUDIT to one of these.
_DISABLE = {"off", "false", "0", "no"}

# One audit file per process — build_server and main() both call configure(); cache so
# they share the same file (the resolved path uses a timestamp that would otherwise drift).
_INSTANCE: AuditLog | None = None


def configure(cfg: Config) -> AuditLog | None:
    """Create the audit log file when trading is enabled. Returns the AuditLog or None.

    On by default in trade mode; DELTA_MCP_AUDIT=off|false|0|no disables it. Idempotent
    within a process.
    """
    global _INSTANCE
    if cfg.mode != "trade":
        return None
    if (setting("DELTA_MCP_AUDIT") or "").lower() in _DISABLE:
        return None
    if _INSTANCE is not None:
        return _INSTANCE

    path = _resolve_path()
    try:
        path = _open(path)
    except OSError:
        fallback = Path(tempfile.gettempdir()) / "delta-exchange-mcp" / path.name
        try:
            path = _open(fallback)
        except OSError as e:
            print(f"[delta-exchange-mcp] audit logging disabled: {e}", file=sys.stderr)
            return None
    _INSTANCE = AuditLog(path, cfg.env)
    return _INSTANCE


def _open(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create empty + tighten to owner-only before any entry is written (umask is often 0644).
    path.touch(exist_ok=True)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path
