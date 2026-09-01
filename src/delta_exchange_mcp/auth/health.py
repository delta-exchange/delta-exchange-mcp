"""Fail-closed health state for consent reads, writes, and revocations."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from delta_exchange_mcp.auth.consent import (
    ConsentBackend,
    ConsentBinding,
    ConsentIdentity,
    ConsentRevocationError,
    ConsentStorageError,
)


@dataclass(frozen=True)
class EnvironmentRevocationScope:
    """All consent records for one environment."""

    environment: str

    def covers(self, binding: ConsentBinding) -> bool:
        """Return whether this operation covers the binding."""
        return binding.environment == self.environment


@dataclass(frozen=True)
class GenerationRevocationScope:
    """Consent records before one credential generation."""

    environment: str
    generation: int

    def covers(self, binding: ConsentBinding) -> bool:
        """Return whether this operation covers the binding."""
        return bool(
            binding.environment == self.environment
            and binding.credential_generation is not None
            and binding.credential_generation < self.generation
        )


@dataclass(frozen=True)
class IdentityRevocationScope:
    """All client consent records for one credential identity."""

    identity: ConsentIdentity

    @property
    def environment(self) -> str:
        """Return the environment for this identity."""
        return self.identity[0]

    def covers(self, binding: ConsentBinding) -> bool:
        """Return whether this operation covers the binding."""
        return binding.identity == self.identity


type RevocationScope = (
    EnvironmentRevocationScope | GenerationRevocationScope | IdentityRevocationScope
)


@dataclass(frozen=True)
class IdentityRisk:
    """One credential identity exposed by a failed revocation."""

    identity: ConsentIdentity

    @property
    def through_environment_generation(self) -> int:
        """Return the environment generation for this identity."""
        return self.identity[4]


@dataclass(frozen=True)
class CoverageRisk:
    """Every covered binding through one environment generation."""

    through_environment_generation: int
    through_credential_generation: int | None = None
    through_session_generation: int | None = None


type RevocationRisk = IdentityRisk | CoverageRisk


@dataclass(frozen=True)
class _FailedRevocation:
    backend: ConsentBackend
    scope: RevocationScope
    risk: RevocationRisk

    def denies(self, binding: ConsentBinding) -> bool:
        if isinstance(self.risk, IdentityRisk):
            return binding.identity == self.risk.identity
        return bool(
            binding.environment_generation <= self.risk.through_environment_generation
            and self.scope.covers(binding)
        )


@dataclass
class ConsentHealth:
    """Track consent failures and recover only the state proven healthy."""

    backend_for: Callable[[ConsentBinding], ConsentBackend] = field(repr=False)
    _read_unavailable: set[ConsentBackend] = field(default_factory=set, repr=False)
    _write_unavailable: set[ConsentBackend] = field(default_factory=set, repr=False)
    _denied_bindings: set[ConsentBinding] = field(default_factory=set, repr=False)
    _write_proofs: set[ConsentBinding] = field(default_factory=set, repr=False)
    _failed_revocations: set[_FailedRevocation] = field(
        default_factory=set,
        repr=False,
    )
    _write_generation: dict[ConsentBackend, int] = field(
        default_factory=dict,
        repr=False,
    )

    @property
    def unavailable(self) -> bool:
        """Return whether any consent read or write state remains unhealthy."""
        return bool(
            self._read_unavailable
            or self._write_unavailable
            or self._denied_bindings
            or self._failed_revocations
        )

    def read_failed(self, backend: ConsentBackend) -> None:
        """Mark one backend read as unavailable."""
        self._read_unavailable.add(backend)

    def read_succeeded(self, backend: ConsentBackend) -> None:
        """Mark one backend read as available."""
        self._read_unavailable.discard(backend)

    def direct_write_failed(
        self,
        backend: ConsentBackend,
        binding: ConsentBinding,
    ) -> None:
        """Deny the exact binding after its direct write fails."""
        self._write_failed(backend)
        self._write_proofs.discard(binding)
        self._denied_bindings.add(binding)

    def direct_write_succeeded(
        self,
        backend: ConsentBackend,
        binding: ConsentBinding,
    ) -> None:
        """Recover only the exact binding written successfully."""
        self._writes_succeeded(frozenset({backend}))
        self._denied_bindings.discard(binding)
        self._write_proofs.add(binding)

    def revocation_failed(
        self,
        error: ConsentStorageError,
        scope: RevocationScope,
        risks: Mapping[ConsentBackend, RevocationRisk],
    ) -> None:
        """Record completed checks and the failed backend for one revocation."""
        self._clear_write_proofs(scope)
        if isinstance(error, ConsentRevocationError):
            self._writes_succeeded(error.written)
            recovered = error.written | error.checked
            self._clear_bindings(scope, recovered)
            self._clear_revocations(scope, recovered)
        else:
            recovered = frozenset()
        for backend, risk in risks.items():
            if backend in recovered:
                continue
            self._failed_revocations.add(_FailedRevocation(backend, scope, risk))
            self._write_failed(backend)

    def revocation_succeeded(
        self,
        written: frozenset[ConsentBackend],
        checked: frozenset[ConsentBackend],
        scope: RevocationScope,
    ) -> None:
        """Recover every binding checked and each backend written successfully."""
        self._clear_write_proofs(scope)
        self._writes_succeeded(written)
        self._clear_bindings(scope, checked)
        self._clear_revocations(scope, checked)

    def credential_changed(self, scope: GenerationRevocationScope) -> None:
        """Expire failures for credential identities that cannot return."""
        self._clear_write_proofs(scope)
        self._clear_bindings(scope)
        self._failed_revocations = {
            failed
            for failed in self._failed_revocations
            if failed.scope.environment != scope.environment
        }

    def expire(
        self,
        environment_generation: int,
        binding: ConsentBinding | None,
        retired_identity: ConsentIdentity | None = None,
    ) -> None:
        """Expire failures made obsolete by the current environment and identity."""
        self._denied_bindings = {
            denied
            for denied in self._denied_bindings
            if not (
                denied.environment_generation < environment_generation
                or denied.identity == retired_identity
                or (
                    binding is not None
                    and denied.environment == binding.environment
                    and _identity_supersedes(binding.identity, denied.identity)
                )
            )
        }
        self._write_proofs = {
            proven
            for proven in self._write_proofs
            if not (
                proven.environment_generation < environment_generation
                or proven.identity == retired_identity
                or (
                    binding is not None
                    and proven.environment == binding.environment
                    and _identity_supersedes(binding.identity, proven.identity)
                )
            )
        }
        self._failed_revocations = {
            failed
            for failed in self._failed_revocations
            if not _failure_obsolete(
                failed,
                environment_generation,
                binding,
                retired_identity,
            )
        }

    def available(
        self,
        backend: ConsentBackend,
        binding: ConsentBinding,
    ) -> bool:
        """Return whether consent writes are healthy for the exact binding."""
        return bool(
            backend not in self._write_unavailable
            and binding not in self._denied_bindings
            and not any(
                failed.backend is backend
                and failed.denies(binding)
                and binding not in self._write_proofs
                for failed in self._failed_revocations
            )
        )

    def generation(self, backend: ConsentBackend) -> int:
        """Capture the current write-health generation for a final check."""
        return self._write_generation.get(backend, 0)

    def remains_available(
        self,
        backend: ConsentBackend,
        binding: ConsentBinding,
        generation: int,
    ) -> bool:
        """Require unchanged write health and availability for a final check."""
        return bool(
            generation == self._write_generation.get(backend, 0)
            and self.available(backend, binding)
        )

    def _write_failed(self, backend: ConsentBackend) -> None:
        self._write_unavailable.add(backend)
        self._write_generation[backend] = self._write_generation.get(backend, 0) + 1

    def _writes_succeeded(self, backends: frozenset[ConsentBackend]) -> None:
        self._write_unavailable.difference_update(backends)
        for backend in backends:
            self._write_generation[backend] = self._write_generation.get(backend, 0) + 1

    def _clear_bindings(
        self,
        scope: RevocationScope,
        backends: frozenset[ConsentBackend] | None = None,
    ) -> None:
        self._denied_bindings = {
            binding
            for binding in self._denied_bindings
            if not (
                scope.covers(binding)
                and (backends is None or self.backend_for(binding) in backends)
            )
        }

    def _clear_write_proofs(self, scope: RevocationScope) -> None:
        self._write_proofs = {
            binding for binding in self._write_proofs if not scope.covers(binding)
        }

    def _clear_revocations(
        self,
        scope: RevocationScope,
        backends: frozenset[ConsentBackend],
    ) -> None:
        self._failed_revocations = {
            failed
            for failed in self._failed_revocations
            if not (failed.backend in backends and _recovery_covers(scope, failed))
        }


def _failure_obsolete(
    failed: _FailedRevocation,
    environment_generation: int,
    binding: ConsentBinding | None,
    retired_identity: ConsentIdentity | None,
) -> bool:
    if failed.risk.through_environment_generation < environment_generation:
        return True
    if (
        retired_identity is not None
        and isinstance(failed.risk, IdentityRisk)
        and failed.risk.identity == retired_identity
    ):
        return True
    if binding is None or binding.environment != failed.scope.environment:
        return False
    if isinstance(failed.risk, IdentityRisk):
        return _identity_supersedes(binding.identity, failed.risk.identity)
    if isinstance(failed.scope, GenerationRevocationScope):
        return bool(
            binding.credential_generation is not None
            and binding.credential_generation >= failed.scope.generation
        )
    return _coverage_obsolete(failed, binding)


def _coverage_obsolete(
    failed: _FailedRevocation,
    binding: ConsentBinding,
) -> bool:
    risk = failed.risk
    if not isinstance(risk, CoverageRisk):
        return False
    session_generation = binding.credential_session_generation
    if session_generation is not None:
        return bool(
            failed.backend is ConsentBackend.MEMORY
            and risk.through_session_generation is not None
            and session_generation > risk.through_session_generation
            and risk.through_credential_generation is not None
        )
    credential_generation = binding.credential_generation
    return bool(
        credential_generation is not None
        and risk.through_credential_generation is not None
        and credential_generation > risk.through_credential_generation
        and (
            failed.backend is ConsentBackend.PERSISTENT
            or risk.through_session_generation is not None
        )
    )


def _recovery_covers(
    completed: RevocationScope,
    failed: _FailedRevocation,
) -> bool:
    if _scope_subsumes(completed, failed.scope):
        return True
    return bool(
        isinstance(failed.risk, IdentityRisk)
        and _scope_covers_identity(completed, failed.risk.identity)
    )


def _scope_subsumes(
    completed: RevocationScope,
    failed: RevocationScope,
) -> bool:
    if isinstance(completed, EnvironmentRevocationScope):
        return completed.environment == failed.environment
    if isinstance(completed, GenerationRevocationScope):
        if completed.environment != failed.environment:
            return False
        if isinstance(failed, GenerationRevocationScope):
            return completed.generation >= failed.generation
        if isinstance(failed, IdentityRevocationScope):
            generation = failed.identity[2]
            return generation is not None and generation < completed.generation
        return False
    return bool(
        isinstance(failed, IdentityRevocationScope)
        and completed.identity == failed.identity
    )


def _scope_covers_identity(
    scope: RevocationScope,
    identity: ConsentIdentity,
) -> bool:
    if isinstance(scope, EnvironmentRevocationScope):
        return scope.environment == identity[0]
    if isinstance(scope, GenerationRevocationScope):
        generation = identity[2]
        return bool(
            scope.environment == identity[0]
            and generation is not None
            and generation < scope.generation
        )
    return scope.identity == identity


def _identity_supersedes(
    current: ConsentIdentity,
    denied: ConsentIdentity,
) -> bool:
    if current[0] != denied[0]:
        return False
    current_generation = current[2]
    denied_generation = denied[2]
    if current_generation is not None and denied_generation is not None:
        return current_generation > denied_generation
    current_session = current[3]
    denied_session = denied[3]
    return bool(
        current_session is not None
        and denied_session is not None
        and current_session > denied_session
    )
