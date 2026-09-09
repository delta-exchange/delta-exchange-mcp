import os
import secrets
import uuid

import pytest

from delta_exchange_mcp.auth.backend import (
    SERVICE_NAME,
    FileMetadata,
    SystemKeyringBackend,
)
from delta_exchange_mcp.auth.store import CredentialSource, CredentialStore


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


@pytest.mark.skipif(
    os.environ.get("DELTA_MCP_TEST_SYSTEM_KEYRING") != "1",
    reason="real system keyring test is opt-in",
)
def test_real_system_keyring_isolates_metadata_folders(tmp_path):
    backend = SystemKeyringBackend(
        service_name=f"{SERVICE_NAME}-test-{uuid.uuid4().hex}"
    )
    first = CredentialStore(
        backend, FileMetadata(tmp_path / "first.json"), CredentialSource.OS_STORE
    )
    second = CredentialStore(
        backend, FileMetadata(tmp_path / "second.json"), CredentialSource.OS_STORE
    )
    environments = ("india_prod", "india_testnet")

    try:
        for environment in environments:
            saved = first.replace(
                environment, secrets.token_urlsafe(24), secrets.token_urlsafe(48)
            )
            second.replace(
                environment, secrets.token_urlsafe(24), secrets.token_urlsafe(48)
            )
            assert first.get(environment) == saved
            second.replace(
                environment, secrets.token_urlsafe(24), secrets.token_urlsafe(48)
            )
            assert first.get(environment) == saved
            second.delete(environment)
            assert first.get(environment) == saved
    finally:
        for credentials in (first, second):
            for environment in environments:
                credentials.delete(environment)
