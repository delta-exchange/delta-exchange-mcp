import logging
from collections.abc import Callable

import httpx
import pytest

from delta_exchange_mcp.client import DeltaClient
from delta_exchange_mcp.config import INDIA_TESTNET_REST, Config
from scripts import permission_matrix as matrix


READ_KEY = "read-key-value"
READ_SECRET = "read-secret-value"
TRADING_KEY = "trading-key-value"
TRADING_SECRET = "trading-secret-value"
BODY_MARKER = "private-response-body-marker"

COMPLETE_ENVIRONMENT = {
    "DELTA_MCP_TESTNET_READ_DATA_API_KEY": READ_KEY,
    "DELTA_MCP_TESTNET_READ_DATA_API_SECRET": READ_SECRET,
    "DELTA_MCP_TESTNET_TRADING_API_KEY": TRADING_KEY,
    "DELTA_MCP_TESTNET_TRADING_API_SECRET": TRADING_SECRET,
}


def success(path: str) -> httpx.Response:
    result: object = (
        {"user_id": 123, "private_marker": BODY_MARKER}
        if path == "/users/trading_preferences"
        else [{"private_marker": BODY_MARKER}]
    )
    return httpx.Response(200, json={"success": True, "result": result})


def api_error(code: str) -> httpx.Response:
    return httpx.Response(
        403,
        json={
            "success": False,
            "error": {"code": code, "context": {"private": BODY_MARKER}},
        },
    )


Handler = Callable[[httpx.Request], httpx.Response]


class MatrixClients:
    def __init__(self) -> None:
        self.handlers: dict[tuple[str, str], Handler] = {}
        self.requests: list[httpx.Request] = []

    def set(self, api_key: str, path: str, handler: Handler) -> None:
        self.handlers[(api_key, path)] = handler

    def __call__(self, config: Config) -> DeltaClient:
        async def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            path = request.url.path.removeprefix("/v2")
            handler = self.handlers.get((config.api_key or "", path))
            return handler(request) if handler is not None else success(path)

        http = httpx.AsyncClient(
            base_url=INDIA_TESTNET_REST,
            transport=httpx.MockTransport(handle),
        )
        return DeltaClient(config, http=http)


async def test_complete_matrix_calls_only_the_four_get_endpoints_and_hides_data(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clients = MatrixClients()
    caplog.set_level(logging.INFO)

    report = await matrix.collect(
        COMPLETE_ENVIRONMENT,
        client_factory=clients,
    )
    output = matrix.render(report)

    assert report.exit_code == matrix.COMPLETE
    assert all(
        outcome.status == "allowed"
        for outcomes in report.outcomes.values()
        for outcome in outcomes.values()
    )
    assert len(clients.requests) == 8
    assert {request.method for request in clients.requests} == {"GET"}
    assert {
        request.url.path.removeprefix("/v2") for request in clients.requests
    } == {endpoint.path for endpoint in matrix.ENDPOINTS}
    assert [request.headers["api-key"] for request in clients.requests] == [
        *([READ_KEY] * 4),
        *([TRADING_KEY] * 4),
    ]
    for private_value in (
        READ_KEY,
        READ_SECRET,
        TRADING_KEY,
        TRADING_SECRET,
        BODY_MARKER,
    ):
        assert private_value not in output
        assert private_value not in caplog.text
    assert "signature" not in output.lower()
    assert "digest" not in output.lower()
    assert "run=complete exit=0" in output


async def test_missing_and_partial_pairs_are_clear_not_run_results() -> None:
    clients = MatrixClients()
    report = await matrix.collect(
        {
            "DELTA_MCP_TESTNET_READ_DATA_API_KEY": READ_KEY,
            "DELTA_MCP_TESTNET_READ_DATA_API_SECRET": READ_SECRET,
            "DELTA_MCP_TESTNET_TRADING_API_KEY": TRADING_KEY,
        },
        client_factory=clients,
    )
    output = matrix.render(report)

    assert report.exit_code == matrix.NOT_RUN
    assert clients.requests == []
    assert output.count("not_run") >= 9
    assert "trading: not run (incomplete pair)" in output
    assert "DELTA_MCP_TESTNET_TRADING_API_SECRET" in output
    assert "run=not_run exit=2" in output


async def test_same_key_cannot_stand_in_for_two_permission_profiles() -> None:
    clients = MatrixClients()
    environ = dict(COMPLETE_ENVIRONMENT)
    environ["DELTA_MCP_TESTNET_TRADING_API_KEY"] = READ_KEY

    report = await matrix.collect(environ, client_factory=clients)
    output = matrix.render(report)

    assert report.exit_code == matrix.NOT_RUN
    assert clients.requests == []
    assert "use two different testnet API keys" in output
    assert "run=not_run exit=2" in output


async def test_permission_denials_are_conclusive_matrix_outcomes() -> None:
    clients = MatrixClients()
    for endpoint in matrix.ENDPOINTS:
        clients.set(
            READ_KEY,
            endpoint.path,
            lambda request: api_error("UnauthorizedApiAccess"),
        )

    report = await matrix.collect(
        COMPLETE_ENVIRONMENT,
        client_factory=clients,
    )
    output = matrix.render(report)

    assert report.exit_code == matrix.COMPLETE
    assert {
        outcome.status for outcome in report.outcomes["read_data"].values()
    } == {"permission_denied"}
    assert {
        outcome.status for outcome in report.outcomes["trading"].values()
    } == {"allowed"}
    assert "permission_denied" in output
    assert "invalid_key" not in output


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("InvalidApiKey", "invalid_key"),
        ("Signature Mismatch", "invalid_signature"),
        ("SignatureExpired", "clock_error"),
        ("ip_not_whitelisted_for_api_key", "ip_restricted"),
        (f"unknown\n{READ_SECRET}", "api_error:unknown"),
    ],
)
async def test_non_permission_api_errors_fail_without_exposing_context(
    code: str,
    expected: str,
) -> None:
    clients = MatrixClients()
    clients.set(
        READ_KEY,
        "/users/trading_preferences",
        lambda request: api_error(code),
    )

    report = await matrix.collect(
        COMPLETE_ENVIRONMENT,
        client_factory=clients,
    )
    output = matrix.render(report)

    assert report.exit_code == matrix.FAILED
    assert (
        report.outcomes["read_data"]["/users/trading_preferences"].status
        == expected
    )
    assert BODY_MARKER not in output
    assert READ_SECRET not in output
    assert "run=failed exit=1" in output


async def test_transport_failure_fails_the_matrix() -> None:
    clients = MatrixClients()

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private network detail", request=request)

    clients.set(READ_KEY, "/users/trading_preferences", fail)
    report = await matrix.collect(
        COMPLETE_ENVIRONMENT,
        client_factory=clients,
    )

    assert report.exit_code == matrix.FAILED
    assert (
        report.outcomes["read_data"]["/users/trading_preferences"].status
        == "transport_failure"
    )
    assert "private network detail" not in matrix.render(report)


async def test_schema_failure_fails_the_matrix_without_printing_the_body() -> None:
    clients = MatrixClients()
    clients.set(
        READ_KEY,
        "/users/trading_preferences",
        lambda request: httpx.Response(
            200,
            json={"success": True, "result": [BODY_MARKER]},
        ),
    )

    report = await matrix.collect(
        COMPLETE_ENVIRONMENT,
        client_factory=clients,
    )
    output = matrix.render(report)

    assert report.exit_code == matrix.FAILED
    assert (
        report.outcomes["read_data"]["/users/trading_preferences"].status
        == "schema_failure"
    )
    assert BODY_MARKER not in output


async def test_malformed_error_code_is_a_schema_failure() -> None:
    clients = MatrixClients()
    clients.set(
        READ_KEY,
        "/users/trading_preferences",
        lambda request: httpx.Response(
            403,
            json={
                "success": False,
                "error": {"code": {"private": BODY_MARKER}},
            },
        ),
    )

    report = await matrix.collect(
        COMPLETE_ENVIRONMENT,
        client_factory=clients,
    )
    output = matrix.render(report)

    assert report.exit_code == matrix.FAILED
    assert (
        report.outcomes["read_data"]["/users/trading_preferences"].status
        == "schema_failure"
    )
    assert BODY_MARKER not in output


def test_credential_pair_repr_is_secret_free() -> None:
    value = matrix.CredentialPair("read_data", READ_KEY, READ_SECRET)

    assert repr(value) == "CredentialPair(permission='read_data')"
