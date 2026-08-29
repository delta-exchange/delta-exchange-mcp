import os
import secrets
import uuid

import pytest

from delta_exchange_mcp.auth.store import SERVICE_NAME, SystemKeyringBackend


@pytest.mark.skipif(
    os.environ.get("DELTA_MCP_TEST_SYSTEM_KEYRING") != "1",
    reason="real system keyring test is opt-in",
)
def test_real_system_keyring_round_trip():
    suffix = uuid.uuid4().hex
    backend = SystemKeyringBackend(service_name=f"{SERVICE_NAME}-test-{suffix}")
    username = f"roundtrip-{suffix}"
    value = secrets.token_urlsafe(48)

    try:
        backend.set(username, value)
        if backend.get(username) != value:
            raise AssertionError("system keyring readback did not match")
    finally:
        backend.delete(username)

    if backend.get(username) is not None:
        raise AssertionError("system keyring cleanup left the disposable record behind")
