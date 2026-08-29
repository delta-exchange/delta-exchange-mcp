"""Bounded analytics headers for outbound Delta API requests."""

import json
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
    CLIENT_INFO_META_KEY,
    ClientCapabilities,
    Implementation,
)
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

from delta_exchange_mcp.version import PACKAGE_VERSION

PREFIX = "X-Delta-MCP-"
CONTEXT_HEADER = f"{PREFIX}Context"
BUDGET_BYTES = 4096
FIELD_LIMIT = 200

_SAFE = " !\"#$&'()*+,-./:;<=>?@[]^_`{|}~"
_CLOSED_CAPABILITIES = ("sampling", "elicitation", "roots", "tasks")
_OPEN_CAPABILITIES = ("experimental", "extensions")
_PLATFORM = f"{platform.system()} {platform.machine()}"
_PYTHON = f"{sys.version_info.major}.{sys.version_info.minor}"


@dataclass(frozen=True)
class _Call:
    """The non-sensitive analytics fields copied from one MCP tool request."""

    client_name: str = ""
    client_version: str = ""
    title: str = ""
    description: str = ""
    website_url: str = ""
    icon_count: int = 0
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


def _modern_models(
    ctx: Context,
) -> tuple[Implementation | None, ClientCapabilities | None]:
    try:
        meta = ctx.request_context.meta
    except ValueError:
        return None, None
    if meta is None:
        return None, None

    raw_info = meta.get(CLIENT_INFO_META_KEY)
    raw_capabilities = meta.get(CLIENT_CAPABILITIES_META_KEY)
    try:
        info = (
            raw_info
            if isinstance(raw_info, Implementation)
            else Implementation.model_validate(raw_info)
        )
    except (TypeError, ValueError):
        info = None
    try:
        capabilities = (
            raw_capabilities
            if isinstance(raw_capabilities, ClientCapabilities)
            else ClientCapabilities.model_validate(raw_capabilities)
        )
    except (TypeError, ValueError):
        capabilities = None
    return info, capabilities


def _legacy_models(
    ctx: Context,
) -> tuple[Implementation | None, ClientCapabilities | None]:
    try:
        params = ctx.session.client_params
    except ValueError:
        return None, None
    if params is None:
        return None, None
    return params.client_info, params.capabilities


def _snapshot(ctx: Context, tool: str) -> _Call:
    if ctx.protocol_version in MODERN_PROTOCOL_VERSIONS:
        info, capabilities = _modern_models(ctx)
    else:
        info, capabilities = _legacy_models(ctx)
    return _Call(
        client_name=info.name if info is not None else "",
        client_version=info.version if info is not None else "",
        title=(info.title or "")[:FIELD_LIMIT] if info is not None else "",
        description=(info.description or "")[:FIELD_LIMIT] if info is not None else "",
        website_url=(info.website_url or "")[:FIELD_LIMIT] if info is not None else "",
        icon_count=len(info.icons or ()) if info is not None else 0,
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
    for name in ("title", "description", "website_url"):
        if value := getattr(call, name):
            extra[name] = value
    if call.icon_count:
        extra["icons"] = call.icon_count
    if call.capabilities:
        extra["capabilities"] = dict(call.capabilities)

    spent = sum(len(name) + len(value) + 4 for name, value in result.items())
    droppable = ["description", "title", "website_url", "capabilities", "icons"]
    while extra:
        value = as_header(extra)
        if value and spent + len(CONTEXT_HEADER) + len(value) + 4 <= BUDGET_BYTES:
            result[CONTEXT_HEADER] = value
            break
        if not droppable:
            break
        extra.pop(droppable.pop(0), None)
    return result
