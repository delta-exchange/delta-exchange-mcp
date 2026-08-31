from __future__ import annotations

import ipaddress
import re
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

# Documented in slate `_authentication.md`. Lookup of server error code → human hint.
_AUTH_HINTS: dict[str, str] = {
    "SignatureExpired": (
        "request signature expired (>5s drift). Sync your system clock via NTP."
    ),
    "InvalidApiKey": (
        "API key not found. Prod and testnet keys are separate — confirm DELTA_MCP_ENV "
        "matches the dashboard the key was created on."
    ),
    "invalid_api_key": (
        "API key not found. Prod and testnet keys are separate — confirm DELTA_MCP_ENV "
        "matches the dashboard the key was created on."
    ),
    "UnauthorizedApiAccess": (
        "API key lacks permission for this endpoint. Update the key's permissions in "
        "Delta API management."
    ),
    "unauthorized_api_access": (
        "API key lacks permission for this endpoint. Update the key's permissions in "
        "Delta API management."
    ),
    "ip_not_whitelisted_for_api_key": (
        "request IP is not allowed for this API key. Update the IP allowlist in Delta "
        "API management."
    ),
    "Signature Mismatch": (
        "signature mismatch — usually clock skew or a path/query encoding bug."
    ),
    "signature_mismatch": (
        "signature mismatch — usually clock skew or a path/query encoding bug."
    ),
}

_OPERATION_HINTS: dict[str, str] = {
    "credentials_missing": "Connect a Delta account in Manage Connection and retry.",
    "execution_outcome_unknown": (
        "Delta may have processed this mutation, but its response was lost. Check open "
        "orders and current account state before you submit it again."
    ),
    "upstream_unreachable": (
        "The mutation did not reach Delta. It is safe to retry after connectivity returns."
    ),
}

_SAFE_CODE = re.compile(r"[A-Za-z0-9_. -]{1,128}\Z")

# A permission response proves that Delta received an authenticated request. It does
# not prove that the same key can access the trading-preferences validation endpoint.
_PERMISSION_FAILURE_CODES = frozenset(
    {"UnauthorizedApiAccess", "unauthorized_api_access"}
)

# Keep permission separate so candidate validation can distinguish authentication-layer
# responses from a valid key that lacks access to this one endpoint.
_AUTH_FAILURE_CODES = frozenset(_AUTH_HINTS) - _PERMISSION_FAILURE_CODES


def extract_ip(context: Any) -> str | None:
    """The IP Delta says it saw, which is the one thing that makes a whitelist error fixable."""
    if not isinstance(context, dict):
        return None
    for key in ("ip", "client_ip", "whitelisted_ip", "request_ip"):
        v = context.get(key)
        if not isinstance(v, str):
            continue
        try:
            return str(ipaddress.ip_address(v))
        except ValueError:
            continue
    return None


def normalize_error_code(code: Any) -> str:
    """Return one bounded, single-line error code safe for an MCP result."""
    return code if isinstance(code, str) and _SAFE_CODE.fullmatch(code) else "unknown_error"


class DeltaApiError(ToolError):
    """An anticipated Delta API failure that an MCP client can act on.

    The public message omits the upstream response context. Delta controls that
    context, so it can contain data that must stay in local diagnostics rather than
    cross the MCP boundary.
    """

    def __init__(self, code: Any, context: Any = None, status: int | None = None):
        self.code = normalize_error_code(code)
        self.context = context
        self.status = status if type(status) is int and 100 <= status <= 599 else None
        self.hint = _AUTH_HINTS.get(self.code) or _OPERATION_HINTS.get(self.code)
        # Kept as a field, not only interpolated into the message, so a caller writing its
        # own copy can use it without parsing the sentence back apart.
        self.ip = extract_ip(context)

        msg = f"delta api error: {self.code}"
        if self.hint:
            extra_ip = (
                self.ip if self.code == "ip_not_whitelisted_for_api_key" else None
            )
            msg += f" — {self.hint}"
            if extra_ip:
                msg += f" (request IP: {extra_ip})"
        if self.status:
            msg += f" [http {self.status}]"
        super().__init__(msg)


def is_auth_failure(error: DeltaApiError) -> bool:
    """Whether Delta returned an authentication-layer error."""
    return error.code in _AUTH_FAILURE_CODES


def is_permission_failure(error: DeltaApiError) -> bool:
    """Whether valid credentials lack permission for the requested endpoint."""
    return error.code in _PERMISSION_FAILURE_CODES


class ConfigError(Exception):
    pass
