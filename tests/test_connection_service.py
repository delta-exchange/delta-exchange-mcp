"""Secure connection composition and browser action behavior."""

import asyncio
import threading
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any, cast

import pytest
from mcp.server.mcpserver import Context
from mcp.server.mcpserver import MCPServer
from mcp_types import CLIENT_INFO_META_KEY, Implementation

from delta_exchange_mcp import credentials as credential_check
from delta_exchange_mcp import setup, store
from delta_exchange_mcp.auth import connection as connection_mod
from delta_exchange_mcp.auth.connection import ConnectionService
from delta_exchange_mcp.auth.consent import ConsentStore, MemoryConsentBackend
from delta_exchange_mcp.auth.store import (
    CredentialConflictError,
    CredentialSource,
    CredentialState,
    CredentialStore,
    MemoryMetadata,
    MemorySecretBackend,
)
from delta_exchange_mcp.tools import trading


def context(name: str, version: str = "1") -> Context:
    request = SimpleNamespace(
        meta={
            CLIENT_INFO_META_KEY: Implementation(name=name, version=version),
        },
        protocol_version="2026-07-28",
    )
    return Context(request_context=cast(Any, request))


def stores(*, persistent: bool = True) -> tuple[CredentialStore, ConsentStore]:
    source = CredentialSource.OS_STORE if persistent else CredentialSource.MEMORY
    credentials = CredentialStore(
        MemorySecretBackend(),
        MemoryMetadata(),
        source,
    )
    consent = ConsentStore(
        store.path().with_name("consent.json"),
        secure_backend_available=persistent,
        memory_backend=MemoryConsentBackend(),
    )
    return credentials, consent


def service(
    validator: Any,
    *,
    persistent: bool = True,
) -> ConnectionService:
    credentials, consent = stores(persistent=persistent)
    return ConnectionService.open(
        credentials=credentials,
        consent=consent,
        validator=validator,
    )


def action(
    connection: ConnectionService,
    client_name: str,
    name: str,
    arguments: Mapping[str, Any],
    revision: setup.Revision | None = None,
) -> setup.ActionResult:
    expected = connection._revision(client_name) if revision is None else revision
    return connection._actions(client_name)(name, arguments, expected)


async def verified(
    environment: str, api_key: str, api_secret: str
) -> credential_check.Check:
    return credential_check.Check(
        ok=True,
        reachable=True,
        detail="42",
    )


@pytest.mark.parametrize(
    ("code", "reachable", "expected_status"),
    [
        ("UnauthorizedApiAccess", True, "unverified"),
        ("ip_not_whitelisted_for_api_key", True, "unverified"),
        ("SignatureExpired", True, "unverified"),
        ("", False, "unverified"),
        ("InvalidApiKey", True, "rejected"),
        ("Signature Mismatch", True, "rejected"),
    ],
)
def test_candidate_validation_only_rejects_decisive_credential_errors(
    code: str,
    reachable: bool,
    expected_status: str,
) -> None:
    async def validator(
        environment: str, api_key: str, api_secret: str
    ) -> credential_check.Check:
        return credential_check.Check(
            ok=False,
            reachable=reachable,
            detail="candidate validation failed",
            code=code,
        )

    connection = service(validator)
    before = connection.credentials.metadata("india_prod")
    result = action(
        connection,
        "Codex",
        "credentials",
        {
            "operation": "replace",
            "environment": "india_prod",
            "api_key": "candidate-key",
            "api_secret": "candidate-secret",
        },
    )

    assert result.content["status"] == expected_status
    after = connection.credentials.metadata("india_prod")
    if expected_status == "rejected":
        assert after == before
    else:
        assert after.state is CredentialState.UNVERIFIED
        assert connection.credentials.get("india_prod").api_key == "candidate-key"


def test_rotation_disconnect_and_environment_round_trip_revoke_consent() -> None:
    connection = service(verified)
    client_name = "Codex"

    prod = action(
        connection,
        client_name,
        "credentials",
        {
            "operation": "replace",
            "environment": "india_prod",
            "api_key": "prod-key",
            "api_secret": "prod-secret",
        },
    )
    consent = action(
        connection,
        client_name,
        "consent",
        {
            "environment": "india_prod",
            "enabled": True,
            "acknowledged": True,
        },
        prod.revision,
    )
    assert consent.content["status"] == "enabled"
    assert consent.complete is True
    final_status = consent.content["connection"]
    assert final_status["credentials_configured"] is True
    assert final_status["trading"]["enabled"] is True
    assert final_status["client_name"] == client_name

    testnet = action(
        connection,
        client_name,
        "credentials",
        {
            "operation": "replace",
            "environment": "india_testnet",
            "api_key": "test-key",
            "api_secret": "test-secret",
        },
        consent.revision,
    )
    testnet_consent = action(
        connection,
        client_name,
        "consent",
        {"environment": "india_testnet", "enabled": True},
        testnet.revision,
    )
    assert testnet_consent.content["status"] == "enabled"

    back_to_prod = action(
        connection,
        client_name,
        "credentials",
        {"operation": "activate", "environment": "india_prod"},
        testnet_consent.revision,
    )
    status = connection._status(client_name, "").as_dict()
    assert back_to_prod.content["status"] == "selected"
    assert status["environment"] == "india_prod"
    assert status["trading"]["enabled"] is False

    disconnected = action(
        connection,
        client_name,
        "credentials",
        {"operation": "disconnect", "environment": "india_prod"},
        back_to_prod.revision,
    )
    assert disconnected.content["status"] == "disconnected"
    assert connection._status(client_name, "").as_dict()["trading"]["enabled"] is False


def test_two_services_serialize_environment_selection_with_page_cas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DELTA_MCP_ENV", raising=False)
    first = service(verified)
    second = service(verified)
    client_name = "Codex"
    first_revision = first._revision(client_name)
    second_revision = second._revision(client_name)
    entered = threading.Event()
    release = threading.Event()
    original = first._activate_environment
    results: list[setup.ActionResult] = []

    def paused_activate(
        name: str,
        environment: str,
        expected: connection_mod.RevisionToken,
    ) -> setup.ActionResult:
        entered.set()
        assert release.wait(5)
        return original(name, environment, expected)

    monkeypatch.setattr(first, "_activate_environment", paused_activate)

    def select_prod_from_stale_page() -> None:
        results.append(
            action(
                first,
                client_name,
                "credentials",
                {"operation": "activate", "environment": "india_prod"},
                first_revision,
            )
        )

    worker = threading.Thread(target=select_prod_from_stale_page)
    worker.start()
    assert entered.wait(5)

    selected = action(
        second,
        client_name,
        "credentials",
        {"operation": "activate", "environment": "india_testnet"},
        second_revision,
    )
    release.set()
    worker.join(5)

    assert worker.is_alive() is False
    assert selected.content["status"] == "selected"
    assert len(results) == 1
    assert results[0].stale is True
    assert store.environment_state("india_prod") == ("india_testnet", 1)


def test_environment_generation_rejects_a_stale_page_after_an_aba_change() -> None:
    connection = service(verified)
    client_name = "Codex"
    original_page = connection._revision(client_name)

    testnet = action(
        connection,
        client_name,
        "credentials",
        {"operation": "activate", "environment": "india_testnet"},
        original_page,
    )
    prod = action(
        connection,
        client_name,
        "credentials",
        {"operation": "activate", "environment": "india_prod"},
        testnet.revision,
    )
    stale_consent = connection._consent_action(
        client_name,
        {
            "environment": "india_prod",
            "enabled": True,
            "acknowledged": True,
        },
        original_page,
    )
    stale = connection._activate_environment(
        client_name,
        "india_testnet",
        original_page,
    )

    assert prod.content["status"] == "selected"
    assert stale_consent.stale is True
    assert stale.stale is True
    assert connection._status(client_name, "").trading["enabled"] is False
    assert store.environment_state("india_prod") == ("india_prod", 2)


@pytest.mark.parametrize("persistent", [True, False])
def test_first_approval_rejects_an_environment_round_trip_during_publication(
    monkeypatch,
    persistent: bool,
) -> None:
    first = service(verified, persistent=persistent)
    first.credentials.replace("india_prod", "prod-key", "prod-secret")
    first.credentials.replace("india_testnet", "test-key", "test-secret")
    second = ConnectionService.open(
        credentials=first.credentials,
        consent=first.consent,
        validator=verified,
    )
    expected = first._revision("Codex")
    original = first.consent.enable

    def change_environment_before_publication(
        binding, *, expected_generation, check_current
    ):
        for environment in ("india_testnet", "india_prod"):
            selected = action(
                second,
                "Another client",
                "credentials",
                {"operation": "activate", "environment": environment},
            )
            assert selected.content["status"] == "selected"
        return original(
            binding,
            expected_generation=expected_generation,
            check_current=check_current,
        )

    monkeypatch.setattr(first.consent, "enable", change_environment_before_publication)
    result = action(
        first,
        "Codex",
        "consent",
        {"environment": "india_prod", "enabled": True, "acknowledged": True},
        expected,
    )

    assert result.stale is True
    assert result.complete is False
    assert first.status(context("Codex"))["trading"]["enabled"] is False
    assert result.revision["active_environment_generation"] == (
        expected["active_environment_generation"] + 2
    )


@pytest.mark.parametrize("read", ["status", "access"])
def test_lagging_instance_does_not_revoke_fresh_consent_after_rotation(
    read: str,
) -> None:
    first = service(verified)
    first.credentials.replace("india_prod", "first-key", "first-secret")
    second = ConnectionService.open(
        credentials=first.credentials,
        consent=ConsentStore(
            store.path().with_name("consent.json"),
            secure_backend_available=True,
            memory_backend=MemoryConsentBackend(),
        ),
        validator=verified,
    )
    saved = action(
        first,
        "Codex",
        "credentials",
        {
            "environment": "india_prod",
            "api_key": "new-key",
            "api_secret": "new-secret",
        },
    )
    approved = action(
        first,
        "Codex",
        "consent",
        {"environment": "india_prod", "enabled": True, "acknowledged": True},
        saved.revision,
    )
    assert approved.content["status"] == "enabled"
    access = asyncio.run(first.access_state(context("Codex")))
    assert access.trading_enabled is True

    if read == "status":
        second.status(context("Another client"))
    else:
        asyncio.run(second.access_state(context("Another client")))

    assert first.status(context("Codex"))["trading"]["enabled"] is True
    assert access.final_trading_check() is True
    assert second.client.config.api_key == "new-key"


def test_a_shared_environment_round_trip_invalidates_existing_approval() -> None:
    connection = service(verified)
    connection.credentials.replace("india_prod", "prod-key", "prod-secret")
    action(
        connection,
        "Codex",
        "consent",
        {"environment": "india_prod", "enabled": True, "acknowledged": True},
    )
    approved = asyncio.run(connection.access_state(context("Codex")))
    assert approved.trading_enabled is True

    assert store.write({"DELTA_MCP_ENV": "india_testnet"}) is None
    assert store.write({"DELTA_MCP_ENV": "india_prod"}) is None

    assert approved.final_trading_check() is False
    assert connection.status(context("Codex"))["trading"]["enabled"] is False


def test_a_final_check_rejects_environment_changes_during_credential_resolution(
    monkeypatch,
) -> None:
    connection = service(verified)
    connection.credentials.replace("india_prod", "prod-key", "prod-secret")
    action(
        connection,
        "Codex",
        "consent",
        {"environment": "india_prod", "enabled": True, "acknowledged": True},
    )
    approved = asyncio.run(connection.access_state(context("Codex")))
    original = connection.credentials.resolve
    changed = False

    def resolve_after_environment_change(environment, environ):
        nonlocal changed
        credential = original(environment, environ)
        if not changed:
            changed = True
            assert store.write({"DELTA_MCP_ENV": "india_testnet"}) is None
            assert store.write({"DELTA_MCP_ENV": "india_prod"}) is None
        return credential

    monkeypatch.setattr(
        connection.credentials, "resolve", resolve_after_environment_change
    )

    assert approved.final_trading_check() is False
    assert changed is True


def test_returning_from_process_credentials_requires_fresh_stored_consent(
    monkeypatch,
) -> None:
    connection = service(verified)
    connection.credentials.replace("india_prod", "stored-key", "stored-secret")
    action(
        connection,
        "Codex",
        "consent",
        {"environment": "india_prod", "enabled": True, "acknowledged": True},
    )
    assert connection.status(context("Codex"))["trading"]["enabled"] is True

    monkeypatch.setenv("DELTA_API_KEY", "process-key")
    monkeypatch.setenv("DELTA_API_SECRET", "process-secret")
    assert connection.status(context("Codex"))["trading"]["enabled"] is False
    monkeypatch.delenv("DELTA_API_KEY")
    monkeypatch.delenv("DELTA_API_SECRET")

    assert connection.status(context("Codex"))["trading"]["enabled"] is False
    approved = action(
        connection,
        "Codex",
        "consent",
        {"environment": "india_prod", "enabled": True, "acknowledged": True},
    )
    assert approved.content["status"] == "enabled"


def test_inactive_environment_cannot_receive_trading_consent() -> None:
    connection = service(verified)
    connected = action(
        connection,
        "Codex",
        "credentials",
        {
            "operation": "replace",
            "environment": "india_prod",
            "api_key": "prod-key",
            "api_secret": "prod-secret",
        },
    )

    result = action(
        connection,
        "Codex",
        "consent",
        {"environment": "india_testnet", "enabled": True},
        connected.revision,
    )

    assert result.content["status"] == "rejected"
    assert "Select this environment" in result.content["message"]


def test_credential_cas_detects_a_disconnect_tombstone() -> None:
    credentials, consent = stores()
    connection = ConnectionService.open(
        credentials=credentials,
        consent=consent,
        validator=verified,
    )
    stale = connection._revision("Codex")

    current = credentials.replace(
        "india_prod",
        "other-key",
        "other-secret",
        expected_revision=0,
        expected_generation=0,
    )
    credentials.delete(
        "india_prod",
        expected_revision=current.revision,
        expected_generation=current.generation,
    )
    result = action(
        connection,
        "Codex",
        "credentials",
        {
            "operation": "replace",
            "environment": "india_prod",
            "api_key": "stale-key",
            "api_secret": "stale-secret",
        },
        stale,
    )

    assert result.stale is True
    with pytest.raises(CredentialConflictError):
        credentials.replace(
            "india_prod",
            "stale-key",
            "stale-secret",
            expected_revision=0,
            expected_generation=0,
        )


def test_process_partial_pair_fails_closed_even_with_a_stored_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials, consent = stores()
    credentials.replace("india_prod", "stored-key", "stored-secret")
    monkeypatch.setenv("DELTA_API_KEY", "external-key")
    monkeypatch.delenv("DELTA_API_SECRET", raising=False)
    connection = ConnectionService.open(
        credentials=credentials,
        consent=consent,
        validator=verified,
    )

    status = connection.status(context("Codex"))
    access = asyncio.run(connection.access_state(context("Codex")))

    assert status["credentials_configured"] is False
    assert status["account_tools_available"] is False
    assert status["environments"]["india_prod"]["credential_source"] == (
        "process_environment"
    )
    assert status["environments"]["india_prod"]["validation_state"] == "incomplete"
    assert access.credentials_ready is False
    assert access.trading_enabled is False


def test_process_pair_change_invalidates_session_only_consent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DELTA_API_KEY", "external-key-1")
    monkeypatch.setenv("DELTA_API_SECRET", "external-secret-1")
    connection = service(verified)
    client_name = "Codex"
    revision = connection._revision(client_name)
    enabled = action(
        connection,
        client_name,
        "consent",
        {
            "environment": "india_prod",
            "enabled": True,
            "acknowledged": True,
        },
        revision,
    )
    assert enabled.content["persistent"] is False

    before = asyncio.run(connection.access_state(context(client_name)))
    monkeypatch.setenv("DELTA_API_KEY", "external-key-2")
    after = asyncio.run(connection.access_state(context(client_name)))

    assert before.trading_enabled is True
    assert before.final_trading_check() is False
    assert after.credentials_ready is True
    assert after.trading_enabled is False


def test_process_environment_credential_cannot_be_disconnected_in_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DELTA_API_KEY", "external-key")
    monkeypatch.setenv("DELTA_API_SECRET", "external-secret")
    connection = service(verified)

    result = action(
        connection,
        "Codex",
        "credentials",
        {"operation": "disconnect", "environment": "india_prod"},
    )

    assert result.content["status"] == "rejected"
    assert "managed by the MCP client's environment" in result.content["message"]
    assert connection.status(context("Codex"))["credentials_configured"] is True


def test_exact_request_client_names_partition_concurrent_access() -> None:
    connection = service(verified)
    connected = action(
        connection,
        "client-a",
        "credentials",
        {
            "operation": "replace",
            "environment": "india_prod",
            "api_key": "prod-key",
            "api_secret": "prod-secret",
        },
    )
    action(
        connection,
        "client-a",
        "consent",
        {
            "environment": "india_prod",
            "enabled": True,
            "acknowledged": True,
        },
        connected.revision,
    )

    async def states() -> tuple[Any, Any]:
        return await asyncio.gather(
            connection.access_state(context("client-a")),
            connection.access_state(context("client-b")),
        )

    first, second = asyncio.run(states())
    assert first.client_name == "client-a"
    assert first.trading_enabled is True
    assert second.client_name == "client-b"
    assert second.trading_enabled is False


def test_unnamed_client_consent_is_session_only() -> None:
    connection = service(verified)
    connected = action(
        connection,
        "",
        "credentials",
        {
            "operation": "replace",
            "environment": "india_prod",
            "api_key": "prod-key",
            "api_secret": "prod-secret",
        },
    )
    enabled = action(
        connection,
        "",
        "consent",
        {
            "environment": "india_prod",
            "enabled": True,
            "acknowledged": True,
        },
        connected.revision,
    )
    assert enabled.content["status"] == "enabled"
    assert enabled.content["persistent"] is False


def test_status_and_results_never_contain_credential_material() -> None:
    connection = service(verified)
    result = action(
        connection,
        "Codex",
        "credentials",
        {
            "operation": "replace",
            "environment": "india_prod",
            "api_key": "visible-only-to-store-key",
            "api_secret": "visible-only-to-store-secret",
        },
    )
    rendered = (
        repr(result) + repr(connection.status(context("Codex"))) + repr(connection)
    )

    assert "visible-only-to-store-key" not in rendered
    assert "visible-only-to-store-secret" not in rendered


@pytest.mark.parametrize("change", ["credential", "environment"])
def test_final_checker_rejects_cross_process_changes_before_mutation(
    monkeypatch,
    change: str,
) -> None:
    connection = service(verified)
    connected = action(
        connection,
        "Codex",
        "credentials",
        {
            "operation": "replace",
            "environment": "india_prod",
            "api_key": "first-key",
            "api_secret": "first-secret",
        },
    )
    action(
        connection,
        "Codex",
        "consent",
        {
            "environment": "india_prod",
            "enabled": True,
            "acknowledged": True,
        },
        connected.revision,
    )
    access = asyncio.run(connection.access_state(context("Codex")))
    assert access.trading_enabled is True

    entered = threading.Event()
    release = threading.Event()
    calls = 0
    original = connection_mod._resolve_credential

    def paused_resolve(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(connection_mod, "_resolve_credential", paused_resolve)
    gate = trading.TradeGate()
    mcp = MCPServer("point-of-use")
    trading.register(mcp, connection.client, None, gate)
    mutations: list[str] = []

    async def post(
        path: str,
        payload: dict[str, Any],
        *,
        auth: bool = False,
    ) -> dict[str, Any]:
        mutations.append(path)
        return {}

    monkeypatch.setattr(connection.client, "post", post)
    outcome: list[BaseException | object] = []

    def invoke() -> None:
        async def run() -> object:
            gate.bind_final_check(access.final_trading_check)
            return await mcp.call_tool(
                "place_order",
                {
                    "product_id": 27,
                    "size": 1,
                    "side": "buy",
                    "order_type": "market_order",
                },
            )

        try:
            outcome.append(asyncio.run(run()))
        except BaseException as exc:
            outcome.append(exc)

    worker = threading.Thread(target=invoke)
    worker.start()
    assert entered.wait(5)
    if change == "environment":
        for environment in ("india_testnet", "india_prod"):
            store.write({"DELTA_MCP_ENV": environment})
    else:
        current = connection.credentials.metadata("india_prod")
        connection.credentials.replace(
            "india_prod",
            "rotated-key",
            "rotated-secret",
            expected_revision=current.revision,
            expected_generation=current.generation,
        )
    release.set()
    worker.join(5)

    assert worker.is_alive() is False
    assert len(outcome) == 1
    assert "trading was disabled" in str(outcome[0])
    assert mutations == []


def test_final_checker_rejects_process_session_generation_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DELTA_API_KEY", "process-key-1")
    monkeypatch.setenv("DELTA_API_SECRET", "process-secret-1")
    connection = service(verified)
    revision = connection._revision("Codex")
    action(
        connection,
        "Codex",
        "consent",
        {
            "environment": "india_prod",
            "enabled": True,
            "acknowledged": True,
        },
        revision,
    )
    access = asyncio.run(connection.access_state(context("Codex")))

    original = connection_mod._resolve_credential
    calls = 0

    def change_after_first_read(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 1:
            monkeypatch.setenv("DELTA_API_KEY", "process-key-2")
            monkeypatch.setenv("DELTA_API_SECRET", "process-secret-2")
        return result

    monkeypatch.setattr(
        connection_mod,
        "_resolve_credential",
        change_after_first_read,
    )
    assert access.final_trading_check() is False
