"""Headers that tell Delta which MCP client and tool caused each API request.

The client names itself, so these values label traffic and must never gate anything.
"""

from __future__ import annotations

import json
import platform
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import quote

import mcp.types as types
from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS

from delta_exchange_mcp.version import PACKAGE_VERSION

PREFIX = "X-Delta-MCP-"
FIELD_LIMIT = 200

_HOST = {
    "platform": f"{platform.system()} {platform.machine()}",
    "python": f"{sys.version_info.major}.{sys.version_info.minor}",
}
_call: ContextVar[dict[str, str] | None] = ContextVar("delta_mcp_call", default=None)


def headers() -> dict[str, str]:
    """Return a fresh dict of the headers for one outbound request."""
    return {f"{PREFIX}Version": PACKAGE_VERSION, **(_call.get() or {})}


@contextmanager
def scope(client: types.InitializeRequestParams | None, tool: str) -> Iterator[None]:
    """Label every request made while one tool call runs."""
    token = _call.set(_describe(client, tool))
    try:
        yield
    finally:
        _call.reset(token)


def _describe(client: types.InitializeRequestParams | None, tool: str) -> dict[str, str]:
    fields = {"Tool": tool}
    context: dict[str, object] = dict(_HOST)
    if client is not None:
        requested = client.protocolVersion
        fields["Client"] = client.clientInfo.name
        fields["Client-Version"] = client.clientInfo.version
        fields["Protocol"] = str(
            requested if requested in SUPPORTED_PROTOCOL_VERSIONS else types.LATEST_PROTOCOL_VERSION
        )
        context["capabilities"] = [
            name
            for name in types.ClientCapabilities.model_fields
            if getattr(client.capabilities, name) is not None
        ]
    result = {PREFIX + name: _clean(value) for name, value in fields.items() if value}
    result[f"{PREFIX}Context"] = json.dumps(context, separators=(",", ":"), sort_keys=True)
    return result


def _clean(value: str) -> str:
    """Percent-encode an untrusted value and cut it to FIELD_LIMIT, never mid-escape."""
    encoded = quote(value)[:FIELD_LIMIT]
    if "%" in encoded[-2:]:
        encoded = encoded[: encoded.rfind("%")]
    return encoded
