"""Run the authenticated Delta testnet permission matrix.

Set these four environment variables with two separate testnet API keys:

* DELTA_MCP_TESTNET_READ_DATA_API_KEY
* DELTA_MCP_TESTNET_READ_DATA_API_SECRET
* DELTA_MCP_TESTNET_TRADING_API_KEY
* DELTA_MCP_TESTNET_TRADING_API_SECRET

The script calls only the four listed GET endpoints for each pair. The shared client can
retry a failed GET. Exit 0 means the matrix reached a conclusive allowed or
permission-denied result. Exit 1 means a request or response failed. Exit 2 means at
least one credential pair was not configured, so the matrix did not run completely.
"""

import asyncio
import logging
import os
import re
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field

import httpx

from delta_exchange_mcp.client import DeltaClient
from delta_exchange_mcp.config import INDIA_TESTNET_REST, Config
from delta_exchange_mcp.errors import DeltaApiError, is_permission_failure


COMPLETE = 0
FAILED = 1
NOT_RUN = 2

_SAFE_ERROR_CODE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_INVALID_KEY_CODES = frozenset({"InvalidApiKey", "invalid_api_key"})
_INVALID_SIGNATURE_CODES = frozenset({"Signature Mismatch", "signature_mismatch"})


@dataclass(frozen=True)
class Endpoint:
    """One GET endpoint and the unwrapped result type required by the MCP."""

    path: str
    result_type: type[object]
    user_id_required: bool = False


@dataclass(frozen=True)
class Permission:
    """One dashboard permission and its dedicated environment variable pair."""

    name: str
    key_variable: str
    secret_variable: str


@dataclass(frozen=True)
class CredentialPair:
    """One complete testnet credential pair with secret-free representation."""

    permission: str
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)


@dataclass(frozen=True)
class Outcome:
    """One matrix cell and whether it invalidates the run."""

    status: str
    failed: bool = False


@dataclass(frozen=True)
class Report:
    """The complete matrix, configuration notices, and process exit code."""

    outcomes: dict[str, dict[str, Outcome]]
    notices: tuple[str, ...]

    @property
    def exit_code(self) -> int:
        if any(
            outcome.failed
            for permission in self.outcomes.values()
            for outcome in permission.values()
        ):
            return FAILED
        if self.notices:
            return NOT_RUN
        return COMPLETE


ENDPOINTS = (
    Endpoint("/users/trading_preferences", dict, user_id_required=True),
    Endpoint("/positions/margined", list),
    Endpoint("/orders", list),
    Endpoint("/wallet/balances", list),
)

PERMISSIONS = (
    Permission(
        "read_data",
        "DELTA_MCP_TESTNET_READ_DATA_API_KEY",
        "DELTA_MCP_TESTNET_READ_DATA_API_SECRET",
    ),
    Permission(
        "trading",
        "DELTA_MCP_TESTNET_TRADING_API_KEY",
        "DELTA_MCP_TESTNET_TRADING_API_SECRET",
    ),
)

ClientFactory = Callable[[Config], DeltaClient]


@contextmanager
def _suppress_logs() -> Iterator[None]:
    """Prevent HTTP response bodies or authentication headers from reaching logs."""
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(previous)


def _credentials(
    permission: Permission,
    environ: Mapping[str, str],
) -> tuple[CredentialPair | None, str | None]:
    api_key = (environ.get(permission.key_variable) or "").strip()
    api_secret = (environ.get(permission.secret_variable) or "").strip()
    if api_key and api_secret:
        return CredentialPair(permission.name, api_key, api_secret), None

    state = "incomplete" if api_key or api_secret else "missing"
    notice = (
        f"{permission.name}: not run ({state} pair); set both "
        f"{permission.key_variable} and {permission.secret_variable}"
    )
    return None, notice


def _validate(endpoint: Endpoint, response: object) -> None:
    if not isinstance(response, dict) or "result" not in response:
        raise ValueError("missing result envelope")
    result = response["result"]
    if not isinstance(result, endpoint.result_type):
        raise ValueError("unexpected result type")
    if endpoint.user_id_required:
        user_id = result.get("user_id")
        if isinstance(user_id, bool) or not isinstance(user_id, int):
            raise ValueError("missing integer user_id")


def _api_error(error: DeltaApiError) -> Outcome:
    if not isinstance(error.code, str):
        return Outcome("schema_failure", failed=True)
    if is_permission_failure(error):
        return Outcome("permission_denied")
    if error.code in _INVALID_KEY_CODES:
        return Outcome("invalid_key", failed=True)
    if error.code in _INVALID_SIGNATURE_CODES:
        return Outcome("invalid_signature", failed=True)
    if error.code == "SignatureExpired":
        return Outcome("clock_error", failed=True)
    if error.code == "ip_not_whitelisted_for_api_key":
        return Outcome("ip_restricted", failed=True)
    if error.code == "invalid_response":
        return Outcome("schema_failure", failed=True)
    safe_code = error.code if _SAFE_ERROR_CODE.fullmatch(error.code) else "unknown"
    return Outcome(f"api_error:{safe_code}", failed=True)


async def _probe(
    credentials: CredentialPair,
    client_factory: ClientFactory,
) -> dict[str, Outcome]:
    config = Config(
        env="india_testnet",
        base_url=INDIA_TESTNET_REST,
        api_key=credentials.api_key,
        api_secret=credentials.api_secret,
    )
    outcomes: dict[str, Outcome] = {}
    with _suppress_logs():
        client = client_factory(config)
        try:
            for endpoint in ENDPOINTS:
                try:
                    response = await client.get(endpoint.path, auth=True)
                    _validate(endpoint, response)
                except DeltaApiError as exc:
                    outcomes[endpoint.path] = _api_error(exc)
                except httpx.HTTPError:
                    outcomes[endpoint.path] = Outcome(
                        "transport_failure",
                        failed=True,
                    )
                except ValueError:
                    outcomes[endpoint.path] = Outcome(
                        "schema_failure",
                        failed=True,
                    )
                else:
                    outcomes[endpoint.path] = Outcome("allowed")
        finally:
            await client.aclose()
    return outcomes


async def collect(
    environ: Mapping[str, str],
    *,
    client_factory: ClientFactory = DeltaClient,
) -> Report:
    """Collect the two-permission matrix without exposing credential material."""
    pairs: dict[str, CredentialPair] = {}
    notices: list[str] = []
    for permission in PERMISSIONS:
        credentials, notice = _credentials(permission, environ)
        if credentials is not None:
            pairs[permission.name] = credentials
        if notice is not None:
            notices.append(notice)

    if len(pairs) == len(PERMISSIONS):
        read_data = pairs["read_data"]
        trading = pairs["trading"]
        if read_data.api_key == trading.api_key:
            notices.append(
                "read_data and trading: not run; use two different testnet API keys"
            )

    if notices:
        not_run = {
            endpoint.path: Outcome("not_run") for endpoint in ENDPOINTS
        }
        return Report(
            outcomes={permission.name: dict(not_run) for permission in PERMISSIONS},
            notices=tuple(notices),
        )

    outcomes = {
        permission.name: await _probe(pairs[permission.name], client_factory)
        for permission in PERMISSIONS
    }
    return Report(outcomes=outcomes, notices=tuple(notices))


def render(report: Report) -> str:
    """Render a compact matrix with no response or credential data."""
    headings = ("endpoint", *(permission.name for permission in PERMISSIONS))
    rows = [
        (
            endpoint.path,
            *(report.outcomes[permission.name][endpoint.path].status for permission in PERMISSIONS),
        )
        for endpoint in ENDPOINTS
    ]
    widths = [
        max(len(headings[index]), *(len(row[index]) for row in rows))
        for index in range(len(headings))
    ]

    def line(values: tuple[str, ...]) -> str:
        return " | ".join(
            value.ljust(widths[index]) for index, value in enumerate(values)
        ).rstrip()

    output = [line(headings), line(tuple("-" * width for width in widths))]
    output.extend(line(row) for row in rows)
    output.extend(report.notices)
    state = {COMPLETE: "complete", FAILED: "failed", NOT_RUN: "not_run"}[
        report.exit_code
    ]
    output.append(f"run={state} exit={report.exit_code}")
    return "\n".join(output)


async def _main() -> int:
    report = await collect(os.environ)
    sys.stdout.write(f"{render(report)}\n")
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
