import pytest

from delta_exchange_mcp.errors import DeltaApiError, extract_ip


def test_untrusted_error_fields_do_not_cross_the_tool_boundary() -> None:
    error = DeltaApiError(
        "bad_code\nprivate-value",
        context={"ip": "127.0.0.1\nprivate-value"},
        status=999,
    )

    assert error.code == "unknown_error"
    assert str(error) == "delta api error: unknown_error"
    assert "private-value" not in str(error)
    assert error.status is None
    assert error.ip is None

    non_string = DeltaApiError(["not", "a", "code"])
    assert str(non_string) == "delta api error: unknown_error"


def test_only_a_valid_ip_address_is_exposed_in_an_allowlist_hint() -> None:
    error = DeltaApiError(
        "ip_not_whitelisted_for_api_key",
        context={"request_ip": "2001:0db8::1"},
        status=401,
    )

    assert extract_ip(error.context) == "2001:db8::1"
    assert "request IP: 2001:db8::1" in str(error)


def test_invalid_key_hint_uses_manage_connection() -> None:
    for code in ("InvalidApiKey", "invalid_api_key"):
        message = str(DeltaApiError(code, status=401))

        assert "Open Manage Connection" in message
        assert "environment is externally managed" in message
        assert "DELTA_MCP_ENV" not in message


@pytest.mark.parametrize(
    "code",
    ["UnauthorizedApiAccess", "unauthorized_api_access"],
)
def test_shared_permission_hint_is_endpoint_neutral(code: str) -> None:
    message = str(DeltaApiError(code, status=403))

    assert "permission for this endpoint" in message
    assert "trading preferences" not in message
