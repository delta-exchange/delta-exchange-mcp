"""Consent health transitions that do not require credential or file stores."""

from delta_exchange_mcp.auth.consent import (
    ConsentBackend,
    ConsentBinding,
    ConsentRevocationError,
)
from delta_exchange_mcp.auth.health import (
    ConsentHealth,
    CoverageRisk,
    EnvironmentRevocationScope,
    IdentityRisk,
)


def _persistent_binding(client_name: str, generation: int) -> ConsentBinding:
    return ConsentBinding(
        client_name=client_name,
        environment="india_prod",
        credential_revision=generation,
        credential_generation=generation,
        credential_session_generation=None,
    )


def _process_binding(client_name: str, generation: int) -> ConsentBinding:
    return ConsentBinding(
        client_name=client_name,
        environment="india_prod",
        credential_revision=None,
        credential_generation=None,
        credential_session_generation=generation,
    )


def _backend_for(binding: ConsentBinding) -> ConsentBackend:
    return ConsentBackend.PERSISTENT if binding.persistent else ConsentBackend.MEMORY


def _failed_persistent_revocation() -> ConsentRevocationError:
    return ConsentRevocationError(
        "persistent consent metadata is read-only",
        failed_backend=ConsentBackend.PERSISTENT,
        written=frozenset(),
        checked=frozenset({ConsentBackend.MEMORY}),
    )


def _failed_memory_revocation() -> ConsentRevocationError:
    return ConsentRevocationError(
        "memory consent backend is unavailable",
        failed_backend=ConsentBackend.MEMORY,
        written=frozenset(),
        checked=frozenset(),
    )


def test_temporary_process_identity_does_not_expire_a_persistent_risk() -> None:
    denied = _persistent_binding("Codex", 1)
    other = _persistent_binding("Claude", 1)
    temporary = _process_binding("Codex", 1)
    health = ConsentHealth(_backend_for)
    scope = EnvironmentRevocationScope("india_prod")

    health.revocation_failed(
        _failed_persistent_revocation(),
        scope,
        {ConsentBackend.PERSISTENT: IdentityRisk(denied.identity)},
    )
    health.direct_write_succeeded(ConsentBackend.PERSISTENT, other)
    health.expire(0, temporary)

    assert health.available(ConsentBackend.MEMORY, temporary) is True
    assert health.available(ConsentBackend.PERSISTENT, denied) is False

    health.expire(0, denied)

    assert health.available(ConsentBackend.PERSISTENT, denied) is False


def test_environment_retry_recovers_a_prior_identity_risk() -> None:
    denied = _persistent_binding("Codex", 1)
    successor = _persistent_binding("Codex", 2)
    health = ConsentHealth(_backend_for)
    scope = EnvironmentRevocationScope("india_prod")

    health.revocation_failed(
        _failed_persistent_revocation(),
        scope,
        {ConsentBackend.PERSISTENT: IdentityRisk(denied.identity)},
    )
    health.revocation_succeeded(
        frozenset(),
        frozenset({ConsentBackend.PERSISTENT}),
        scope,
    )
    health.direct_write_succeeded(ConsentBackend.PERSISTENT, successor)

    assert health.available(ConsentBackend.PERSISTENT, successor) is True
    assert health.unavailable is False


def test_new_stored_generation_is_outside_bounded_coverage() -> None:
    successor = _persistent_binding("", 1)
    other = _persistent_binding("Claude", 1)
    temporary = _process_binding("Codex", 1)
    health = ConsentHealth(_backend_for)
    scope = EnvironmentRevocationScope("india_prod")

    health.revocation_failed(
        _failed_persistent_revocation(),
        scope,
        {ConsentBackend.PERSISTENT: CoverageRisk(0, 0)},
    )
    health.direct_write_succeeded(ConsentBackend.PERSISTENT, other)
    health.expire(0, temporary)

    assert health.available(ConsentBackend.PERSISTENT, successor) is True

    health.expire(0, successor)

    assert health.available(ConsentBackend.PERSISTENT, successor) is True
    assert health.unavailable is True


def test_unbounded_coverage_survives_credential_store_recovery() -> None:
    successor = _persistent_binding("", 1)
    other = _persistent_binding("Claude", 1)
    health = ConsentHealth(_backend_for)

    health.revocation_failed(
        _failed_persistent_revocation(),
        EnvironmentRevocationScope("india_prod"),
        {ConsentBackend.PERSISTENT: CoverageRisk(0)},
    )
    health.direct_write_succeeded(ConsentBackend.PERSISTENT, other)
    health.expire(0, successor)

    assert health.available(ConsentBackend.PERSISTENT, successor) is False
    assert health.unavailable is True


def test_exact_write_proof_does_not_clear_unbounded_coverage() -> None:
    proven = _process_binding("Codex", 1)
    other = _process_binding("Claude", 1)
    stored = _persistent_binding("", 1)
    health = ConsentHealth(_backend_for)
    scope = EnvironmentRevocationScope("india_prod")
    risks = {ConsentBackend.MEMORY: CoverageRisk(0)}

    health.revocation_failed(_failed_memory_revocation(), scope, risks)
    health.direct_write_succeeded(ConsentBackend.MEMORY, proven)
    health.expire(0, proven)

    assert health.available(ConsentBackend.MEMORY, proven) is True
    assert health.available(ConsentBackend.MEMORY, other) is False
    assert health.available(ConsentBackend.MEMORY, stored) is False
    assert health.unavailable is True

    health.revocation_failed(_failed_memory_revocation(), scope, risks)
    health.direct_write_succeeded(ConsentBackend.MEMORY, other)

    assert health.available(ConsentBackend.MEMORY, proven) is False
    assert health.available(ConsentBackend.MEMORY, other) is True
