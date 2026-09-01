"""Cross-process consent-health race regressions."""

import asyncio
from collections.abc import Callable

import pytest

from delta_exchange_mcp import store
from delta_exchange_mcp.auth.connection import ConnectionService
from delta_exchange_mcp.auth.consent import (
    ConsentBackend,
    ConsentBinding,
    ConsentRevocationError,
    ConsentState,
    ConsentStorageError,
)
from tests.connection_support import (
    action,
    assert_place_order_blocked,
    context,
    service,
    verified,
)


def test_environment_revocation_expires_for_an_external_credential_successor(
    monkeypatch,
) -> None:
    connection = service(verified)
    connection.credentials.replace("india_prod", "old-key", "old-secret")
    connection.credentials.replace("india_testnet", "test-key", "test-secret")
    arguments = {
        "environment": "india_prod",
        "enabled": True,
        "acknowledged": True,
    }
    action(connection, "Codex", "consent", arguments)
    action(connection, "Claude", "consent", arguments)
    captured = asyncio.run(connection.access_state(context("Codex")))
    original_environment_revoke = connection.consent.revoke_environment

    def fail_environment(environment: str) -> frozenset[ConsentBackend]:
        if environment == "india_prod":
            raise ConsentRevocationError(
                "persistent consent metadata is read-only",
                failed_backend=ConsentBackend.PERSISTENT,
                written=frozenset(),
                checked=frozenset({ConsentBackend.MEMORY}),
            )
        return original_environment_revoke(environment)

    monkeypatch.setattr(
        connection.consent,
        "revoke_environment",
        fail_environment,
    )
    failed = action(
        connection,
        "Codex",
        "credentials",
        {"operation": "activate", "environment": "india_testnet"},
    )
    monkeypatch.setattr(
        connection.consent,
        "revoke_environment",
        original_environment_revoke,
    )
    recovered_backend = action(
        connection,
        "Claude",
        "consent",
        {**arguments, "enabled": False},
    )
    same_identity = asyncio.run(connection.access_state(context("Codex")))

    assert failed.content["status"] == "rejected"
    assert recovered_backend.content["status"] == "disabled"
    assert same_identity.trading_enabled is False
    assert captured.final_trading_check() is False
    assert store.environment_state("india_prod") == ("india_prod", 0)

    connection.credentials.replace("india_prod", "new-key", "new-secret")
    original_identity_revoke = connection.consent.revoke_identity

    def fail_identity(binding: ConsentBinding) -> frozenset[ConsentBackend]:
        del binding
        raise ConsentRevocationError(
            "persistent consent metadata is read-only",
            failed_backend=ConsentBackend.PERSISTENT,
            written=frozenset(),
            checked=frozenset({ConsentBackend.MEMORY}),
        )

    monkeypatch.setattr(connection.consent, "revoke_identity", fail_identity)
    rotated = connection.status(context("Codex"))
    monkeypatch.setattr(
        connection.consent,
        "revoke_identity",
        original_identity_revoke,
    )
    enabled = action(connection, "Codex", "consent", arguments)
    successor = asyncio.run(connection.access_state(context("Codex")))

    assert rotated["trading"]["enabled"] is False
    assert rotated["consent_error"] == "consent_store_unavailable"
    assert enabled.content["status"] == "enabled"
    assert successor.trading_enabled is True
    assert successor.final_trading_check() is True
    assert captured.final_trading_check() is False
    assert connection.status(context("Codex"))["consent_error"] == ""
    assert store.environment_state("india_prod") == ("india_prod", 0)


def test_environment_revocation_expires_for_the_first_external_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = service(verified)
    connection.credentials.replace("india_testnet", "test-key", "test-secret")
    original_environment_revoke = connection.consent.revoke_environment

    def fail_prod(environment: str) -> frozenset[ConsentBackend]:
        if environment == "india_prod":
            raise ConsentRevocationError(
                "persistent consent metadata is read-only",
                failed_backend=ConsentBackend.PERSISTENT,
                written=frozenset(),
                checked=frozenset({ConsentBackend.MEMORY}),
            )
        return original_environment_revoke(environment)

    monkeypatch.setattr(connection.consent, "revoke_environment", fail_prod)
    failed = action(
        connection,
        "Codex",
        "credentials",
        {"operation": "activate", "environment": "india_testnet"},
    )
    monkeypatch.setattr(
        connection.consent,
        "revoke_environment",
        original_environment_revoke,
    )
    credential = connection.credentials.replace(
        "india_prod",
        "new-key",
        "new-secret",
    )
    arguments = {
        "environment": "india_prod",
        "enabled": True,
        "acknowledged": True,
    }
    enabled = action(connection, "Codex", "consent", arguments)
    current = asyncio.run(connection.access_state(context("Codex")))

    assert failed.content["status"] == "rejected"
    assert credential.generation == 1
    assert enabled.content["status"] == "enabled"
    assert current.trading_enabled is True
    assert current.final_trading_check() is True
    assert connection.status(context("Codex"))["consent_error"] == ""
    assert store.environment_state("india_prod") == ("india_prod", 0)


def test_environment_failure_denies_the_identity_present_at_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = service(verified)
    connection.credentials.replace("india_prod", "old-key", "old-secret")
    connection.credentials.replace("india_testnet", "test-key", "test-secret")
    concurrent = ConnectionService.open(
        credentials=connection.credentials,
        consent=connection.consent,
        validator=verified,
    )
    arguments = {
        "environment": "india_prod",
        "enabled": True,
        "acknowledged": True,
    }
    action(connection, "Codex", "consent", arguments)
    captured = asyncio.run(connection.access_state(context("Codex")))
    original_environment_revoke = connection.consent.revoke_environment

    def rotate_then_fail(environment: str) -> frozenset[ConsentBackend]:
        if environment != "india_prod":
            return original_environment_revoke(environment)
        metadata = connection.credentials.metadata(environment)
        connection.credentials.replace(
            environment,
            "new-key",
            "new-secret",
            expected_revision=metadata.revision,
            expected_generation=metadata.generation,
        )
        enabled = action(concurrent, "Codex", "consent", arguments)
        assert enabled.content["status"] == "enabled"
        raise ConsentRevocationError(
            "persistent consent metadata is read-only",
            failed_backend=ConsentBackend.PERSISTENT,
            written=frozenset(),
            checked=frozenset({ConsentBackend.MEMORY}),
        )

    monkeypatch.setattr(
        connection.consent,
        "revoke_environment",
        rotate_then_fail,
    )
    failed = action(
        connection,
        "Codex",
        "credentials",
        {"operation": "activate", "environment": "india_testnet"},
    )
    monkeypatch.setattr(
        connection.consent,
        "revoke_environment",
        original_environment_revoke,
    )
    recovered_backend = action(
        connection,
        "Claude",
        "consent",
        {**arguments, "enabled": False},
    )
    current = asyncio.run(connection.access_state(context("Codex")))

    assert failed.content["status"] == "rejected"
    assert recovered_backend.content["status"] == "disabled"
    assert current.trading_enabled is False
    assert current.final_trading_check() is False
    assert captured.final_trading_check() is False
    assert connection.status(context("Codex"))["consent_error"] == (
        "consent_store_unavailable"
    )
    assert_place_order_blocked(
        monkeypatch,
        connection,
        current.final_trading_check,
    )

    metadata = connection.credentials.metadata("india_prod")
    connection.credentials.replace(
        "india_prod",
        "successor-key",
        "successor-secret",
        expected_revision=metadata.revision,
        expected_generation=metadata.generation,
    )
    enabled_successor = action(connection, "Codex", "consent", arguments)
    successor = asyncio.run(connection.access_state(context("Codex")))

    assert enabled_successor.content["status"] == "enabled"
    assert successor.trading_enabled is True
    assert successor.final_trading_check() is True
    assert connection.status(context("Codex"))["consent_error"] == ""


def test_process_identity_is_not_retired_by_an_environment_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DELTA_MCP_ENV", "india_prod")
    monkeypatch.setenv("DELTA_API_KEY", "process-key")
    monkeypatch.setenv("DELTA_API_SECRET", "process-secret")
    connection = service(verified)
    arguments = {
        "environment": "india_prod",
        "enabled": True,
        "acknowledged": True,
    }
    action(connection, "Codex", "consent", arguments)
    original_disable = connection.consent.disable

    def fail_disable(
        binding: ConsentBinding,
        *,
        expected_generation: int,
        check_current: Callable[[], bool] | None = None,
    ) -> ConsentState:
        del binding, expected_generation, check_current
        raise ConsentStorageError("memory consent backend is unavailable")

    monkeypatch.setattr(connection.consent, "disable", fail_disable)
    failed_disable = action(
        connection,
        "Codex",
        "consent",
        {**arguments, "enabled": False},
    )
    monkeypatch.setattr(connection.consent, "disable", original_disable)
    original_identity_revoke = connection.consent.revoke_identity

    def fail_identity(binding: ConsentBinding) -> frozenset[ConsentBackend]:
        del binding
        raise ConsentRevocationError(
            "memory consent backend is unavailable",
            failed_backend=ConsentBackend.MEMORY,
            written=frozenset(),
            checked=frozenset(),
        )

    monkeypatch.setenv("DELTA_MCP_ENV", "india_testnet")
    monkeypatch.setattr(connection.consent, "revoke_identity", fail_identity)
    recovered_backend = action(
        connection,
        "Claude",
        "consent",
        {
            "environment": "india_testnet",
            "enabled": True,
            "acknowledged": True,
        },
    )
    monkeypatch.setattr(
        connection.consent,
        "revoke_identity",
        original_identity_revoke,
    )
    monkeypatch.setenv("DELTA_MCP_ENV", "india_prod")
    current = asyncio.run(connection.access_state(context("Codex")))

    assert failed_disable.content["status"] == "rejected"
    assert recovered_backend.content["status"] == "enabled"
    assert current.trading_enabled is False
    assert current.final_trading_check() is False
    assert connection.status(context("Codex"))["consent_error"] == (
        "consent_store_unavailable"
    )
    assert_place_order_blocked(
        monkeypatch,
        connection,
        current.final_trading_check,
    )
