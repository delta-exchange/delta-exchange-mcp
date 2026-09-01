"""Consent health behavior through the connection service."""

import asyncio

import pytest

from delta_exchange_mcp import store
from delta_exchange_mcp.auth.consent import (
    ConsentBackend,
    ConsentBinding,
    ConsentRevocationError,
    ConsentStorageError,
)
from delta_exchange_mcp.auth.store import CredentialState, CredentialStoreError
from delta_exchange_mcp.server import build_server
from tests.connection_support import (
    action,
    assert_place_order_blocked,
    context,
    service,
    verified,
)


@pytest.mark.parametrize("failure", ["malformed", "unreadable"])
def test_broken_consent_store_preserves_account_access_and_reports_error(
    monkeypatch,
    failure: str,
) -> None:
    connection = service(verified)
    connection.credentials.replace(
        "india_prod",
        "key",
        "secret",
        state=CredentialState.VERIFIED,
    )
    if failure == "malformed":
        store.path().with_name("consent.json").write_text("not-json")
    else:

        def unreadable(*args, **kwargs):
            raise ConsentStorageError("cannot read consent metadata")

        monkeypatch.setattr(connection.consent, "status", unreadable)

    access = asyncio.run(connection.access_state(context("Codex")))
    status = connection.status(context("Codex"))
    reads: list[tuple[str, bool]] = []

    async def get(path: str, params=None, *, auth: bool = False):
        reads.append((path, auth))
        return {"success": True, "result": {"id": 42}}

    monkeypatch.setattr(connection.client, "get", get)
    app = build_server(connection_service=connection)
    account_result = asyncio.run(
        app.call_tool("get_wallet_balances", {}, context("Codex"))
    )

    assert access.credentials_ready is True
    assert access.trading_enabled is False
    assert access.final_trading_check() is False
    assert status["credentials_configured"] is True
    assert status["account_tools_available"] is True
    assert status["trading"]["enabled"] is False
    assert status["connection_error"] == "consent_store_unavailable"
    assert status["consent_error"] == "consent_store_unavailable"
    assert account_result.is_error is False
    assert reads == [("/wallet/balances", True)]


def test_consent_write_failure_survives_reads_until_a_write_succeeds(
    monkeypatch,
) -> None:
    connection = service(verified)
    connection.credentials.replace("india_prod", "key", "secret")
    original_enable = connection.consent.enable

    def unavailable(*args, **kwargs):
        raise ConsentStorageError("consent metadata is read-only")

    monkeypatch.setattr(connection.consent, "enable", unavailable)
    failed = action(
        connection,
        "Codex",
        "consent",
        {
            "environment": "india_prod",
            "enabled": True,
            "acknowledged": True,
        },
    )

    assert failed.content["status"] == "rejected"
    assert connection.consent_error == "consent_store_unavailable"
    assert connection.status(context("Codex"))["consent_error"] == (
        "consent_store_unavailable"
    )

    monkeypatch.setattr(connection.consent, "enable", original_enable)
    enabled = action(
        connection,
        "Codex",
        "consent",
        {
            "environment": "india_prod",
            "enabled": True,
            "acknowledged": True,
        },
    )

    assert enabled.content["status"] == "enabled"
    assert connection.status(context("Codex"))["consent_error"] == ""


@pytest.mark.parametrize(
    ("failed_client", "other_client"),
    [("Codex", ""), ("", "Codex")],
    ids=[
        "persistent-failure-survives-memory-write",
        "memory-failure-survives-persistent-write",
    ],
)
def test_consent_write_recovery_is_scoped_to_the_selected_backend(
    monkeypatch,
    failed_client: str,
    other_client: str,
) -> None:
    connection = service(verified)
    connection.credentials.replace("india_prod", "key", "secret")
    original_enable = connection.consent.enable
    failed_persistent = bool(failed_client)

    def fail_selected_backend(binding, **kwargs):
        if binding.persistent is failed_persistent:
            raise ConsentStorageError("selected consent backend is read-only")
        return original_enable(binding, **kwargs)

    monkeypatch.setattr(connection.consent, "enable", fail_selected_backend)
    arguments = {
        "environment": "india_prod",
        "enabled": True,
        "acknowledged": True,
    }

    failed = action(connection, failed_client, "consent", arguments)
    recovered_other = action(connection, other_client, "consent", arguments)

    assert failed.content["status"] == "rejected"
    assert recovered_other.content["status"] == "enabled"
    assert connection.status(context(failed_client))["consent_error"] == (
        "consent_store_unavailable"
    )

    monkeypatch.setattr(connection.consent, "enable", original_enable)
    recovered_failed = action(connection, failed_client, "consent", arguments)

    assert recovered_failed.content["status"] == "enabled"
    assert connection.status(context(failed_client))["consent_error"] == ""


@pytest.mark.parametrize(
    ("failed_client", "other_client"),
    [("Codex", ""), ("", "Codex")],
    ids=["persistent", "memory"],
)
def test_failed_disable_blocks_trading_but_preserves_reads(
    monkeypatch,
    failed_client: str,
    other_client: str,
) -> None:
    connection = service(verified)
    connection.credentials.replace("india_prod", "key", "secret")
    arguments = {
        "environment": "india_prod",
        "enabled": True,
        "acknowledged": True,
    }
    enabled = action(connection, failed_client, "consent", arguments)
    captured = asyncio.run(connection.access_state(context(failed_client)))
    original_disable = connection.consent.disable
    failed_persistent = bool(failed_client)

    def fail_selected_backend(binding, **kwargs):
        if binding.persistent is failed_persistent:
            raise ConsentStorageError("selected consent backend is read-only")
        return original_disable(binding, **kwargs)

    monkeypatch.setattr(connection.consent, "disable", fail_selected_backend)
    failed = action(
        connection,
        failed_client,
        "consent",
        {**arguments, "enabled": False},
    )
    healthy_other = action(connection, other_client, "consent", arguments)
    current = asyncio.run(connection.access_state(context(failed_client)))
    other = asyncio.run(connection.access_state(context(other_client)))
    status = connection.status(context(failed_client))

    assert enabled.content["status"] == "enabled"
    assert captured.trading_enabled is True
    assert failed.content["status"] == "rejected"
    assert healthy_other.content["status"] == "enabled"
    assert current.credentials_ready is True
    assert current.trading_enabled is False
    assert current.final_trading_check() is False
    assert other.trading_enabled is True
    assert status["trading"]["enabled"] is False
    assert status["consent_error"] == "consent_store_unavailable"

    assert_place_order_blocked(
        monkeypatch,
        connection,
        captured.final_trading_check,
    )

    reads: list[tuple[str, bool]] = []

    async def get(path: str, params=None, *, auth: bool = False):
        del params
        reads.append((path, auth))
        return {"success": True, "result": {"id": 42}}

    monkeypatch.setattr(connection.client, "get", get)
    app = build_server(connection_service=connection)

    async def read_tools() -> tuple[object, object]:
        try:
            public = await app.call_tool(
                "get_ticker", {"symbol": "BTCUSD"}, context(failed_client)
            )
            account = await app.call_tool(
                "get_wallet_balances", {}, context(failed_client)
            )
            return public, account
        finally:
            await app.close_live_client()

    public, account = asyncio.run(read_tools())

    assert public.is_error is False
    assert account.is_error is False
    assert reads == [("/tickers/BTCUSD", False), ("/wallet/balances", True)]


@pytest.mark.parametrize(
    "persistent",
    [True, False],
    ids=["persistent-backend", "memory-backend"],
)
def test_failed_disable_stays_denied_after_another_binding_recovers_backend(
    monkeypatch,
    persistent: bool,
) -> None:
    connection = service(verified, persistent=persistent)
    connection.credentials.replace("india_prod", "key", "secret")
    arguments = {
        "environment": "india_prod",
        "enabled": True,
        "acknowledged": True,
    }
    action(connection, "Codex", "consent", arguments)
    action(connection, "Claude", "consent", arguments)
    captured = asyncio.run(connection.access_state(context("Codex")))
    original_disable = connection.consent.disable

    def fail_codex(binding, **kwargs):
        if binding.client_name == "Codex":
            raise ConsentStorageError("Codex consent backend is read-only")
        return original_disable(binding, **kwargs)

    monkeypatch.setattr(connection.consent, "disable", fail_codex)
    failed = action(
        connection,
        "Codex",
        "consent",
        {**arguments, "enabled": False},
    )
    monkeypatch.setattr(connection.consent, "disable", original_disable)
    other = action(
        connection,
        "Claude",
        "consent",
        {**arguments, "enabled": False},
    )

    current = asyncio.run(connection.access_state(context("Codex")))
    status = connection.status(context("Codex"))
    assert failed.content["status"] == "rejected"
    assert other.content["status"] == "disabled"
    assert captured.final_trading_check() is False
    assert current.trading_enabled is False
    assert status["trading"]["enabled"] is False
    assert status["consent_error"] == "consent_store_unavailable"

    assert_place_order_blocked(
        monkeypatch,
        connection,
        captured.final_trading_check,
    )

    recovered = action(
        connection,
        "Codex",
        "consent",
        {**arguments, "enabled": False},
    )
    reenabled = action(connection, "Codex", "consent", arguments)
    allowed = asyncio.run(connection.access_state(context("Codex")))

    assert recovered.content["status"] == "disabled"
    assert reenabled.content["status"] == "enabled"
    assert allowed.trading_enabled is True
    assert allowed.final_trading_check() is True
    assert connection.status(context("Codex"))["consent_error"] == ""


@pytest.mark.parametrize(
    "persistent",
    [True, False],
    ids=["persistent-backend", "memory-backend"],
)
def test_credential_rotation_drops_an_obsolete_binding_denial(
    monkeypatch,
    persistent: bool,
) -> None:
    connection = service(verified, persistent=persistent)
    connection.credentials.replace("india_prod", "old-key", "old-secret")
    arguments = {
        "environment": "india_prod",
        "enabled": True,
        "acknowledged": True,
    }
    original_enable = connection.consent.enable

    def fail_enable(*args, **kwargs):
        raise ConsentStorageError("consent backend is read-only")

    monkeypatch.setattr(connection.consent, "enable", fail_enable)
    failed = action(connection, "Codex", "consent", arguments)
    monkeypatch.setattr(connection.consent, "enable", original_enable)
    rotated = action(
        connection,
        "Codex",
        "credentials",
        {
            "operation": "replace",
            "environment": "india_prod",
            "api_key": "new-key",
            "api_secret": "new-secret",
        },
    )
    enabled = action(
        connection,
        "Codex",
        "consent",
        arguments,
        rotated.revision,
    )

    assert failed.content["status"] == "rejected"
    assert rotated.content["status"] == "saved"
    assert enabled.content["status"] == "enabled"
    assert connection.status(context("Codex"))["consent_error"] == ""


def test_failed_environment_revocation_survives_backend_recovery(
    monkeypatch,
) -> None:
    connection = service(verified)
    connection.credentials.replace("india_prod", "prod-key", "prod-secret")
    connection.credentials.replace("india_testnet", "test-key", "test-secret")
    arguments = {
        "environment": "india_prod",
        "enabled": True,
        "acknowledged": True,
    }
    action(connection, "Codex", "consent", arguments)
    action(connection, "Claude", "consent", arguments)
    captured = asyncio.run(connection.access_state(context("Codex")))
    original_revoke = connection.consent.revoke_environment

    def fail_prod(environment: str) -> frozenset[ConsentBackend]:
        if environment == "india_prod":
            raise ConsentRevocationError(
                "persistent consent metadata is read-only",
                failed_backend=ConsentBackend.PERSISTENT,
                written=frozenset(),
                checked=frozenset({ConsentBackend.MEMORY}),
            )
        return original_revoke(environment)

    monkeypatch.setattr(connection.consent, "revoke_environment", fail_prod)
    failed = action(
        connection,
        "Codex",
        "credentials",
        {"operation": "activate", "environment": "india_testnet"},
    )
    monkeypatch.setattr(connection.consent, "revoke_environment", original_revoke)
    recovered_backend = action(
        connection,
        "Claude",
        "consent",
        {**arguments, "enabled": False},
    )

    current = asyncio.run(connection.access_state(context("Codex")))
    assert failed.content["status"] == "rejected"
    assert recovered_backend.content["status"] == "disabled"
    assert captured.final_trading_check() is False
    assert current.trading_enabled is False
    assert connection.status(context("Codex"))["consent_error"] == (
        "consent_store_unavailable"
    )

    assert_place_order_blocked(
        monkeypatch,
        connection,
        captured.final_trading_check,
    )

    recovered_scope = action(
        connection,
        "Codex",
        "credentials",
        {"operation": "activate", "environment": "india_testnet"},
    )

    assert recovered_scope.content["status"] == "selected"
    assert connection.status(context("Codex"))["consent_error"] == ""


def test_removed_process_identity_does_not_leave_stale_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = service(verified)
    connection.credentials.replace("india_prod", "stored-key", "stored-secret")
    monkeypatch.setenv("DELTA_API_KEY", "process-key")
    monkeypatch.setenv("DELTA_API_SECRET", "process-secret")
    arguments = {
        "environment": "india_prod",
        "enabled": True,
        "acknowledged": True,
    }
    action(connection, "Codex", "consent", arguments)
    original_environment_revoke = connection.consent.revoke_environment

    def fail_environment(environment: str) -> frozenset[ConsentBackend]:
        if environment != "india_prod":
            return original_environment_revoke(environment)
        raise ConsentRevocationError(
            "memory consent backend is unavailable",
            failed_backend=ConsentBackend.MEMORY,
            written=frozenset(),
            checked=frozenset(),
        )

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
    monkeypatch.delenv("DELTA_API_KEY")
    monkeypatch.delenv("DELTA_API_SECRET")
    original_identity_revoke = connection.consent.revoke_identity

    def fail_identity(binding: ConsentBinding) -> frozenset[ConsentBackend]:
        del binding
        raise ConsentRevocationError(
            "memory consent backend is unavailable",
            failed_backend=ConsentBackend.MEMORY,
            written=frozenset(),
            checked=frozenset(),
        )

    monkeypatch.setattr(connection.consent, "revoke_identity", fail_identity)
    connection._reconcile()
    monkeypatch.setattr(
        connection.consent,
        "revoke_identity",
        original_identity_revoke,
    )
    enabled = action(connection, "", "consent", arguments)
    status = connection.status(context(""))

    assert failed.content["status"] == "rejected"
    assert enabled.content["status"] == "enabled"
    assert status["trading"]["enabled"] is True
    assert status["consent_error"] == ""


def test_successful_empty_environment_revocation_clears_failed_scope(
    monkeypatch,
) -> None:
    connection = service(verified)
    connection.credentials.replace("india_prod", "prod-key", "prod-secret")
    connection.credentials.replace("india_testnet", "test-key", "test-secret")
    original_revoke = connection.consent.revoke_environment

    def fail_testnet(environment: str) -> frozenset[ConsentBackend]:
        if environment == "india_testnet":
            raise ConsentRevocationError(
                "persistent consent metadata is read-only",
                failed_backend=ConsentBackend.PERSISTENT,
                written=frozenset(),
                checked=frozenset({ConsentBackend.MEMORY}),
            )
        return original_revoke(environment)

    monkeypatch.setattr(connection.consent, "revoke_environment", fail_testnet)
    failed = action(
        connection,
        "Codex",
        "credentials",
        {"operation": "activate", "environment": "india_testnet"},
    )
    monkeypatch.setattr(connection.consent, "revoke_environment", original_revoke)
    enabled = action(
        connection,
        "Codex",
        "consent",
        {
            "environment": "india_prod",
            "enabled": True,
            "acknowledged": True,
        },
    )

    assert failed.content["status"] == "rejected"
    assert enabled.content["status"] == "enabled"
    assert connection.status(context("Codex"))["consent_error"] == (
        "consent_store_unavailable"
    )

    recovered = action(
        connection,
        "Codex",
        "credentials",
        {"operation": "activate", "environment": "india_testnet"},
    )

    assert recovered.content["status"] == "selected"
    assert connection.status(context("Codex"))["consent_error"] == ""


def test_environment_generation_drops_denials_after_active_binding_was_lost(
    monkeypatch,
) -> None:
    connection = service(verified)
    connection.credentials.replace("india_prod", "prod-key", "prod-secret")
    connection.credentials.replace("india_testnet", "test-key", "test-secret")
    original_enable = connection.consent.enable

    def fail_enable(*args, **kwargs):
        raise ConsentStorageError("persistent consent metadata is read-only")

    monkeypatch.setattr(connection.consent, "enable", fail_enable)
    failed = action(
        connection,
        "Codex",
        "consent",
        {
            "environment": "india_prod",
            "enabled": True,
            "acknowledged": True,
        },
    )
    monkeypatch.setattr(connection.consent, "enable", original_enable)
    connection._active_binding = None

    assert store.write({"DELTA_MCP_ENV": "india_testnet"}) is None
    enabled = action(
        connection,
        "Claude",
        "consent",
        {"environment": "india_testnet", "enabled": True},
    )

    assert failed.content["status"] == "rejected"
    assert enabled.content["status"] == "enabled"
    assert connection.status(context("Claude"))["consent_error"] == ""


def test_environment_scope_expires_after_active_binding_was_unavailable(
    monkeypatch,
) -> None:
    connection = service(verified)
    connection.credentials.replace("india_prod", "prod-key", "prod-secret")
    connection.credentials.replace("india_testnet", "test-key", "test-secret")
    original_revoke_environment = connection.consent.revoke_environment

    def fail_prod(environment: str) -> frozenset[ConsentBackend]:
        if environment == "india_prod":
            raise ConsentRevocationError(
                "persistent consent metadata is read-only",
                failed_backend=ConsentBackend.PERSISTENT,
                written=frozenset(),
                checked=frozenset({ConsentBackend.MEMORY}),
            )
        return original_revoke_environment(environment)

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
        original_revoke_environment,
    )
    original_resolve = connection.credentials.resolve
    original_revoke_identity = connection.consent.revoke_identity

    def fail_resolve(*args, **kwargs):
        raise CredentialStoreError("credential metadata is temporarily unavailable")

    def fail_identity(*args, **kwargs):
        raise ConsentRevocationError(
            "persistent consent metadata is read-only",
            failed_backend=ConsentBackend.PERSISTENT,
            written=frozenset(),
            checked=frozenset({ConsentBackend.MEMORY}),
        )

    monkeypatch.setattr(connection.credentials, "resolve", fail_resolve)
    monkeypatch.setattr(connection.consent, "revoke_identity", fail_identity)
    connection._reconcile()
    monkeypatch.setattr(connection.credentials, "resolve", original_resolve)
    monkeypatch.setattr(connection.consent, "revoke_identity", original_revoke_identity)

    assert store.write({"DELTA_MCP_ENV": "india_testnet"}) is None
    enabled = action(
        connection,
        "Claude",
        "consent",
        {"environment": "india_testnet", "enabled": True},
    )

    assert failed.content["status"] == "rejected"
    assert enabled.content["status"] == "enabled"
    assert connection.status(context("Claude"))["consent_error"] == ""


def test_environment_generation_expires_a_failed_disconnect_scope(
    monkeypatch,
) -> None:
    connection = service(verified)
    connection.credentials.replace("india_prod", "prod-key", "prod-secret")
    connection.credentials.replace("india_testnet", "test-key", "test-secret")
    original_revoke = connection.consent.revoke_before

    def fail_revoke(*args, **kwargs):
        raise ConsentRevocationError(
            "persistent consent metadata is read-only",
            failed_backend=ConsentBackend.PERSISTENT,
            written=frozenset(),
            checked=frozenset({ConsentBackend.MEMORY}),
        )

    monkeypatch.setattr(connection.consent, "revoke_before", fail_revoke)
    disconnected = action(
        connection,
        "Codex",
        "credentials",
        {"operation": "disconnect", "environment": "india_prod"},
    )
    monkeypatch.setattr(connection.consent, "revoke_before", original_revoke)

    assert store.write({"DELTA_MCP_ENV": "india_testnet"}) is None
    enabled = action(
        connection,
        "Claude",
        "consent",
        {"environment": "india_testnet", "enabled": True},
    )

    assert disconnected.content["status"] == "disconnected"
    assert enabled.content["status"] == "enabled"
    assert connection.status(context("Claude"))["consent_error"] == ""


def test_inactive_credential_change_expires_its_failed_generation_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = service(verified)
    connection.credentials.replace("india_prod", "prod-key", "prod-secret")
    original_revoke = connection.consent.revoke_before

    def fail_testnet(
        environment: str,
        generation: int,
    ) -> frozenset[ConsentBackend]:
        if environment != "india_testnet":
            return original_revoke(environment, generation)
        raise ConsentRevocationError(
            "persistent consent metadata is read-only",
            failed_backend=ConsentBackend.PERSISTENT,
            written=frozenset(),
            checked=frozenset({ConsentBackend.MEMORY}),
        )

    monkeypatch.setattr(connection.consent, "revoke_before", fail_testnet)
    saved = action(
        connection,
        "Codex",
        "credentials",
        {
            "operation": "replace",
            "environment": "india_testnet",
            "api_key": "test-key",
            "api_secret": "test-secret",
        },
    )
    monkeypatch.setattr(connection.consent, "revoke_before", original_revoke)
    recovered = action(
        connection,
        "Claude",
        "consent",
        {
            "environment": "india_prod",
            "enabled": True,
            "acknowledged": True,
        },
    )
    status = connection.status(context("Claude"))

    assert saved.content["status"] == "rejected"
    assert recovered.content["status"] == "enabled"
    assert status["trading"]["enabled"] is True
    assert connection.credentials.metadata("india_testnet").generation == 1
    assert status["consent_error"] == ""


def test_first_process_identity_expires_empty_memory_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = service(verified)
    original_revoke = connection.consent.revoke_environment

    def fail_memory(environment: str) -> frozenset[ConsentBackend]:
        if environment != "india_prod":
            return original_revoke(environment)
        raise ConsentRevocationError(
            "memory consent backend is unavailable",
            failed_backend=ConsentBackend.MEMORY,
            written=frozenset(),
            checked=frozenset(),
        )

    monkeypatch.setattr(connection.consent, "revoke_environment", fail_memory)
    failed = action(
        connection,
        "Codex",
        "credentials",
        {"operation": "activate", "environment": "india_testnet"},
    )
    monkeypatch.setattr(connection.consent, "revoke_environment", original_revoke)
    monkeypatch.setenv("DELTA_API_KEY", "first-process-key")
    monkeypatch.setenv("DELTA_API_SECRET", "first-process-secret")
    enabled = action(
        connection,
        "Codex",
        "consent",
        {
            "environment": "india_prod",
            "enabled": True,
            "acknowledged": True,
        },
    )
    access = asyncio.run(connection.access_state(context("Codex")))
    status = connection.status(context("Codex"))

    assert failed.content["status"] == "rejected"
    assert enabled.content["status"] == "enabled"
    assert access.trading_enabled is True
    assert access.final_trading_check() is True
    assert status["consent_error"] == ""


def test_completed_process_identity_expires_incomplete_memory_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DELTA_API_KEY", "completed-process-key")
    connection = service(verified)
    original_revoke = connection.consent.revoke_environment

    def fail_memory(environment: str) -> frozenset[ConsentBackend]:
        if environment != "india_prod":
            return original_revoke(environment)
        raise ConsentRevocationError(
            "memory consent backend is unavailable",
            failed_backend=ConsentBackend.MEMORY,
            written=frozenset(),
            checked=frozenset(),
        )

    monkeypatch.setattr(connection.consent, "revoke_environment", fail_memory)
    failed = action(
        connection,
        "Codex",
        "credentials",
        {"operation": "activate", "environment": "india_testnet"},
    )
    monkeypatch.setattr(connection.consent, "revoke_environment", original_revoke)
    monkeypatch.setenv("DELTA_API_SECRET", "completed-process-secret")
    enabled = action(
        connection,
        "Codex",
        "consent",
        {
            "environment": "india_prod",
            "enabled": True,
            "acknowledged": True,
        },
    )
    access = asyncio.run(connection.access_state(context("Codex")))
    status = connection.status(context("Codex"))

    assert failed.content["status"] == "rejected"
    assert enabled.content["status"] == "enabled"
    assert access.trading_enabled is True
    assert access.final_trading_check() is True
    assert status["consent_error"] == ""
