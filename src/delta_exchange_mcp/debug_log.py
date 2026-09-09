"""Opt-in file logging for `DELTA_MCP_DEBUG`.

When enabled, HTTP request URLs (incl. filter params) and response bodies are written to a
file so a user on a vanilla `uvx` install can capture wire-level evidence for a bug report.
Credentials (api-key / api_secret / signature / timestamp) are NEVER logged; response bodies
may contain account data, so the file is "review before sharing", not "always safe to share".

NB: this module is intentionally NOT named `logging.py` — that would shadow the stdlib
`logging` module on `import logging` elsewhere in the package.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import time
from pathlib import Path

from delta_exchange_mcp.config import Config, setting
from delta_exchange_mcp.version import PACKAGE_VERSION

LOGGER_NAMES = ("delta_exchange_mcp", "httpx")
_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"
# Marker so we don't attach a second handler if build_server runs twice in one process.
_MARKER = "_delta_debug_handler"
_STATE_MARKER = "_delta_debug_logger_states"


def _resolve_path() -> Path:
    override = setting("DELTA_MCP_DEBUG_FILE")
    if override:
        return Path(override).expanduser()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"debug-{stamp}-{os.getpid()}.log"
    return Path.home() / ".delta-exchange-mcp" / "logs" / name


def _make_handler(path: Path) -> logging.FileHandler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    # Owner read/write only — the log may contain account data (balances, fills,
    # transactions). FileHandler creates the file empty under the process umask
    # (often 644), so tighten before any body is written. Best-effort: no-op on
    # platforms without POSIX permissions (e.g. Windows).
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return handler


def _open_handler(path: Path) -> tuple[logging.FileHandler, Path]:
    """Create the dir and a FileHandler, falling back to a tempdir on OSError."""
    try:
        return _make_handler(path), path
    except OSError:
        fallback = Path(tempfile.gettempdir()) / "delta-exchange-mcp" / path.name
        return _make_handler(fallback), fallback


def configure(cfg: Config) -> Path | None:
    """Attach a file log handler when debug is on. Idempotent. Returns the path or None."""
    if not cfg.debug:
        return None

    root = logging.getLogger(LOGGER_NAMES[0])
    for h in root.handlers:
        if getattr(h, _MARKER, False):
            return Path(h.baseFilename)  # already configured this process

    try:
        handler, path = _open_handler(_resolve_path())
    except OSError as e:
        print(f"[delta-exchange-mcp] debug logging disabled: {e}", file=sys.stderr)
        return None

    setattr(handler, _MARKER, True)
    prior_states = {
        name: (logging.getLogger(name).level, logging.getLogger(name).propagate)
        for name in LOGGER_NAMES
    }
    setattr(handler, _STATE_MARKER, prior_states)
    handler.setFormatter(logging.Formatter(_FORMAT))
    for name in LOGGER_NAMES:
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        logger.propagate = False  # never leak to a root/stdout handler

    surface = "market+account" if cfg.has_credentials else "market"
    logging.getLogger(LOGGER_NAMES[0]).info(
        "debug log start: delta-exchange-mcp/%s env=%s base_url=%s surface=%s | "
        "credentials (api-key/secret/signature) are never logged; "
        "response bodies may contain account data",
        PACKAGE_VERSION, cfg.env, cfg.base_url, surface,
    )
    return path


def shutdown() -> None:
    """Detach and close the debug handler attached by this module, if any."""
    handlers: set[logging.Handler] = set()
    prior_states: dict[str, tuple[int, bool]] = {}
    for name in LOGGER_NAMES:
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            if getattr(handler, _MARKER, False):
                prior_states.update(getattr(handler, _STATE_MARKER, {}))
                logger.removeHandler(handler)
                handlers.add(handler)

    for name, (level, propagate) in prior_states.items():
        logger = logging.getLogger(name)
        logger.setLevel(level)
        logger.propagate = propagate

    # The same handler is attached to both module loggers. Close it only after it has been
    # detached everywhere, and only once, so Windows can remove its containing directory.
    for handler in handlers:
        handler.close()
