"""Bounded analytics headers for outbound Delta API requests."""

import json
import os
import platform
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from urllib.parse import quote

from mcp.server.mcpserver import Context
from mcp_types import (
    CLIENT_CAPABILITIES_META_KEY,
    ClientCapabilities,
)
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

from delta_exchange_mcp import request
from delta_exchange_mcp.version import PACKAGE_VERSION

PREFIX = "X-Delta-MCP-"
CONTEXT_HEADER = f"{PREFIX}Context"
BUDGET_BYTES = 4096
FIELD_LIMIT = 200

_SAFE = "!\"#$&'()*+,-./:;<=>?@[]^_`{|}~"
_CLOSED_CAPABILITIES = ("sampling", "elicitation", "roots", "tasks")
_OPEN_CAPABILITIES = ("experimental", "extensions")
_PLATFORM = f"{platform.system()} {platform.machine()}"
_PYTHON = f"{sys.version_info.major}.{sys.version_info.minor}"
_OFF = frozenset({"off", "false", "0", "no"})


@dataclass(frozen=True)
class _Call:
    """The bounded analytics fields copied from one MCP tool request."""

    client_name: str = ""
    client_version: str = ""
    capabilities: tuple[tuple[str, bool | int], ...] = ()
    tool: str = ""
    protocol: str = ""


_current: ContextVar[_Call | None] = ContextVar("delta_analytics_call", default=None)


def encode(value: str) -> str:
    """Encode an untrusted string as printable ASCII for an HTTP header."""
    return quote(value, safe=_SAFE, errors="replace")


def clean(value: str) -> str:
    """Encode and bound one discrete header value."""
    encoded = encode(value)
    if len(encoded) <= FIELD_LIMIT:
        return encoded
    cut = encoded[:FIELD_LIMIT]
    if "%" in cut[-2:]:
        cut = cut[: cut.rfind("%")]
    return cut


def as_header(payload: dict[str, object]) -> str:
    """Serialize a context object as safe, directly parseable JSON."""
    value = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    if value.isascii() and value.isprintable():
        return value
    return ""


def _capabilities(capabilities: ClientCapabilities | None) -> dict[str, bool | int]:
    """Project capabilities without forwarding extension names or settings."""
    if capabilities is None:
        return {}
    result: dict[str, bool | int] = {}
    for name in _CLOSED_CAPABILITIES:
        if getattr(capabilities, name, None) is not None:
            result[name] = True
    for name in _OPEN_CAPABILITIES:
        if declared := getattr(capabilities, name, None):
            result[name] = len(declared)
    return result


def _modern_capabilities(ctx: Context) -> ClientCapabilities | None:
    try:
        meta = ctx.request_context.meta
    except ValueError:
        return None
    if meta is None:
        return None

    raw_capabilities = meta.get(CLIENT_CAPABILITIES_META_KEY)
    try:
        return ClientCapabilities.model_validate(raw_capabilities)
    except (TypeError, ValueError):
        return None


def _legacy_capabilities(ctx: Context) -> ClientCapabilities | None:
    try:
        params = ctx.session.client_params
    except ValueError:
        return None
    if params is None:
        return None
    return params.capabilities


def _snapshot(ctx: Context, tool: str) -> _Call:
    reported = request.context_client(ctx)
    if ctx.protocol_version in MODERN_PROTOCOL_VERSIONS:
        capabilities = _modern_capabilities(ctx)
    else:
        capabilities = _legacy_capabilities(ctx)
    return _Call(
        client_name=reported.name,
        client_version=reported.version,
        capabilities=tuple(_capabilities(capabilities).items()),
        tool=tool,
        protocol=ctx.protocol_version or "",
    )


@contextmanager
def scope(ctx: Context, tool: str) -> Iterator[None]:
    """Bind analytics to the current task for one tool call."""
    token = _current.set(_snapshot(ctx, tool))
    try:
        yield
    finally:
        _current.reset(token)


def headers() -> dict[str, str]:
    """Build the analytics headers for the current outbound request."""
    if os.environ.get("DELTA_MCP_ANALYTICS", "on").strip().lower() in _OFF:
        return {}
    call = _current.get()
    result = {f"{PREFIX}Version": PACKAGE_VERSION}
    if call is None:
        return result

    discrete = {
        f"{PREFIX}Client": clean(call.client_name),
        f"{PREFIX}Client-Version": clean(call.client_version),
        f"{PREFIX}Tool": clean(call.tool),
        f"{PREFIX}Protocol": clean(call.protocol),
    }
    result.update({name: value for name, value in discrete.items() if value})

    extra: dict[str, object] = {
        "platform": _PLATFORM,
        "python": _PYTHON,
    }
    if call.capabilities:
        extra["capabilities"] = dict(call.capabilities)

    spent = sum(len(name) + len(value) + 4 for name, value in result.items())
    droppable = ["capabilities"]
    while extra:
        value = as_header(extra)
        if value and spent + len(CONTEXT_HEADER) + len(value) + 4 <= BUDGET_BYTES:
            result[CONTEXT_HEADER] = value
            break
        if not droppable:
            break
        extra.pop(droppable.pop(0), None)
    return result
