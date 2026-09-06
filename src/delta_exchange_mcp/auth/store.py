"""Versioned Delta credentials and their replacement transactions."""

import logging
import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace as replace_fields
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from delta_exchange_mcp import config as config_mod
from delta_exchange_mcp.auth.backend import (
    DEFAULT_DIR as DEFAULT_DIR,
    DEFAULT_METADATA_NAME as DEFAULT_METADATA_NAME,
    METADATA_VERSION as METADATA_VERSION,
    SERVICE_NAME as SERVICE_NAME,
    SECRET_VERSION as SECRET_VERSION,
    SUPPORTED_ENVIRONMENTS as SUPPORTED_ENVIRONMENTS,
    BackendOperationError as BackendOperationError,
    BackendUnavailableError as BackendUnavailableError,
    CredentialCorruptError as CredentialCorruptError,
    CredentialMetadata as CredentialMetadata,
    CredentialState as CredentialState,
    CredentialStoreError as CredentialStoreError,
    EnvironmentState,
    FileMetadata as FileMetadata,
    MemoryMetadata as MemoryMetadata,
    MemorySecretBackend as MemorySecretBackend,
    MetadataBackend,
    MetadataError as MetadataError,
    SecretBackend,
    SystemKeyringBackend as SystemKeyringBackend,
    _decode_secret,
    _detach_records,
    _encode_secret,
    _record_name,
    default_metadata_path as default_metadata_path,
    normalize_environment,
    restore_record,
)
from delta_exchange_mcp.auth.migration import (
    MigrationError as MigrationError,
    MigrationResult as MigrationResult,
    MigrationStatus as MigrationStatus,
    migrate,
)

logger = logging.getLogger(__name__)


class CredentialSource(StrEnum):
    OS_STORE = "os_store"
    MEMORY = "memory"
    PROCESS = "process"


class CredentialConflictError(CredentialStoreError):
    pass


class CredentialActivationError(CredentialStoreError):
    pass


class IncompleteCredentialError(CredentialStoreError):
    pass


@dataclass(frozen=True)
class Credential:
    environment: str
    revision: int | None
    generation: int | None
    state: CredentialState
    source: CredentialSource
    account_id: str
    created_at: str
    updated_at: str
    validated_at: str
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)
    session_generation: int | None = None

    @property
    def externally_managed(self) -> bool:
        return self.source is CredentialSource.PROCESS

    @property
    def session_only(self) -> bool:
        """Whether this credential can support consent only in this process."""
        return self.source is not CredentialSource.OS_STORE


@dataclass(frozen=True)
class _PreparedReplacement:
    backend: SecretBackend
    metadata: MetadataBackend
    values: dict[str, EnvironmentState]
    environment: str
    had_previous: bool
    previous: EnvironmentState
    previous_credential: Credential | None
    previous_payload: str | None
    reserved: EnvironmentState
    current: EnvironmentState
    new_revision: int
    new_name: str
    new_payload: str
    new_credential: Credential
    activate: Callable[[Credential | None], None] | None

    def write_new(self) -> None:
        """Reserve, write, and read back the new secret record."""
        self.values[self.environment] = self.reserved
        self.metadata.write(self.values)
        try:
            self.backend.set(self.new_name, self.new_payload)
            if self.backend.get(self.new_name) != self.new_payload:
                raise BackendOperationError(
                    "the system store did not return credential revision "
                    f"{self.new_revision} after writing it"
                )
        except Exception:
            try:
                self._discard_new()
            except Exception as cleanup_exc:
                raise CredentialStoreError(
                    "credential write verification failed and cleanup remains pending"
                ) from cleanup_exc
            raise

    def publish(self) -> None:
        """Publish the verified record as the active revision."""
        self.values[self.environment] = self.current
        try:
            self.metadata.write(self.values)
        except Exception:
            self.values[self.environment] = self.reserved
            try:
                self._discard_new()
            except Exception as cleanup_exc:
                raise CredentialStoreError(
                    "credential publication failed and cleanup remains pending"
                ) from cleanup_exc
            raise

    def activate_new(self) -> None:
        if self.activate is not None:
            self.activate(self.new_credential)

    def retire_previous(self) -> None:
        revision = self.previous.active_revision
        if revision is None:
            return
        self.backend.delete(
            _record_name(self.metadata.namespace, self.environment, revision)
        )
        cleaned = replace_fields(
            self.current,
            pending_revisions=self.previous.pending_revisions,
        )
        self.values[self.environment] = cleaned
        try:
            self.metadata.write(self.values)
        except Exception as exc:
            self.values[self.environment] = self.current
            logger.warning(
                "activated credential revision %s for %s, but pending cleanup "
                "metadata could not be cleared: %s",
                self.current.active_revision,
                self.environment,
                exc,
            )

    def restore_previous_record(self) -> None:
        revision = self.previous.active_revision
        if revision is None or self.previous_payload is None:
            raise CredentialStoreError(
                "credential retirement failed after removing the old record"
            )
        name = _record_name(self.metadata.namespace, self.environment, revision)
        restore_record(self.backend, name, self.previous_payload)

    def rollback(self) -> None:
        """Restore the previous active record and remove the new record."""
        rollback = replace_fields(
            self.previous,
            next_revision=self.current.next_revision,
            pending_revisions=(
                *self.previous.pending_revisions,
                self.new_revision,
            ),
        )
        self.values[self.environment] = rollback
        try:
            self.metadata.write(self.values)
        except Exception:
            # Still restore the live binding and delete the candidate. Cleanup below
            # retries the pointer write; a persistent failure must not keep a rejected
            # credential usable merely because its metadata could not be restored.
            logger.warning("credential rollback could not publish the previous pointer")

        activation_error: Exception | None = None
        try:
            if self.activate is not None:
                self.activate(self.previous_credential)
        except Exception as exc:
            activation_error = exc

        try:
            self._discard_new()
        except Exception as cleanup_exc:
            raise CredentialStoreError(
                "credential rollback failed and cleanup remains pending"
            ) from cleanup_exc
        if activation_error is not None:
            raise activation_error

    def _discard_new(self) -> None:
        self.backend.delete(self.new_name)
        if self.had_previous:
            self.values[self.environment] = self.previous
        else:
            self.values.pop(self.environment, None)
        self.metadata.write(self.values)


class CredentialStore:
    """Coordinate versioned Delta credentials and their active pointers."""

    def __init__(
        self,
        backend: SecretBackend,
        metadata: MetadataBackend,
        source: CredentialSource,
        fallback_reason: str = "",
    ):
        self._backend = backend
        self._metadata = metadata
        self.source = source
        self.fallback_reason = fallback_reason
        self._process_lock = threading.RLock()
        self._process_pairs: dict[str, tuple[str, str]] = {}
        self._process_generations: dict[str, int] = {}

    @classmethod
    def open(cls, metadata_path: Path | None = None) -> "CredentialStore":
        """Use the system keyring, or fall back to process memory."""
        try:
            backend = SystemKeyringBackend()
        except BackendUnavailableError as exc:
            return cls.memory(str(exc))
        store = cls(
            backend,
            FileMetadata(metadata_path or default_metadata_path()),
            CredentialSource.OS_STORE,
        )
        store._retry_pending_cleanup()
        return store

    @classmethod
    def memory(cls, fallback_reason: str = "") -> "CredentialStore":
        """Create one process-local credential store."""
        return cls(
            MemorySecretBackend(),
            MemoryMetadata(),
            CredentialSource.MEMORY,
            fallback_reason=fallback_reason,
        )

    @property
    def persistent(self) -> bool:
        """Whether credentials survive process exit."""
        return self.source is CredentialSource.OS_STORE

    def metadata(self, environment: str) -> CredentialMetadata:
        """Read active revision and revocation state without reading a secret."""
        env = normalize_environment(environment)
        state = self._metadata.read().get(env, EnvironmentState())
        return state.metadata(env)

    def generation(self, environment: str) -> int:
        """Read the revocation generation without accessing the keyring."""
        return self.metadata(environment).generation

    def process_generation(self, environment: str) -> int:
        """Return the current process-credential session generation."""
        env = _normalize_runtime_environment(environment)
        with self._process_lock:
            return self._process_generations.get(env, 0)

    def get(self, environment: str) -> Credential | None:
        """Read the active credential for an environment."""
        env = normalize_environment(environment)
        with self._metadata.lock():
            values = self._metadata.read()
            self._cleanup_pending_locked(values, env)
            return self._get_locked(env, values)

    def resolve(
        self,
        environment: str,
        environ: Mapping[str, str] | None = None,
    ) -> Credential | None:
        """Prefer a complete external process credential over the stored record."""
        env = _normalize_runtime_environment(environment)
        supplied = os.environ if environ is None else environ
        key = (supplied.get("DELTA_API_KEY") or "").strip()
        secret = (supplied.get("DELTA_API_SECRET") or "").strip()
        if key or secret:
            if not key or not secret:
                self._clear_process_pair(env)
                raise IncompleteCredentialError(
                    "the process environment must supply DELTA_API_KEY and DELTA_API_SECRET together"
                )
            session_generation = self._observe_process_pair(env, key, secret)
            return Credential(
                environment=env,
                revision=None,
                generation=None,
                state=CredentialState.UNVERIFIED,
                source=CredentialSource.PROCESS,
                account_id="",
                created_at="",
                updated_at="",
                validated_at="",
                api_key=key,
                api_secret=secret,
                session_generation=session_generation,
            )
        self._clear_process_pair(env)
        if env not in SUPPORTED_ENVIRONMENTS:
            return None
        return self.get(env)

    def replace(
        self,
        environment: str,
        api_key: str,
        api_secret: str,
        *,
        state: CredentialState = CredentialState.UNVERIFIED,
        account_id: str = "",
        expected_revision: int | None = None,
        expected_generation: int | None = None,
        activate: Callable[[Credential | None], None] | None = None,
    ) -> Credential:
        """Publish, activate, and retire one credential as a transaction."""
        env = normalize_environment(environment)
        key = api_key.strip()
        secret = api_secret.strip()
        if not key or not secret:
            raise IncompleteCredentialError("an API key and secret are both required")

        with self._metadata.lock():
            values = self._metadata.read()
            self._cleanup_pending_locked(values, env)
            transaction = self._prepare_replace_locked(
                values,
                env,
                key,
                secret,
                state=state,
                account_id=account_id,
                expected_revision=expected_revision,
                expected_generation=expected_generation,
                activate=activate,
            )
            transaction.write_new()
            transaction.publish()
            try:
                transaction.activate_new()
            except Exception as exc:
                try:
                    transaction.rollback()
                except Exception as rollback_exc:
                    raise CredentialStoreError(
                        "credential activation failed and its transaction rollback also failed"
                    ) from rollback_exc
                if isinstance(exc, CredentialStoreError):
                    raise
                raise CredentialActivationError(
                    f"could not activate credential revision {transaction.new_revision}"
                ) from exc

            try:
                transaction.retire_previous()
            except Exception as exc:
                try:
                    transaction.restore_previous_record()
                except Exception as restore_exc:
                    raise CredentialStoreError(
                        "credential retirement failed and the old record could not be restored"
                    ) from restore_exc
                try:
                    transaction.rollback()
                except Exception as rollback_exc:
                    raise CredentialStoreError(
                        "credential retirement failed and its transaction rollback also failed"
                    ) from rollback_exc
                raise BackendOperationError(
                    "could not retire credential revision "
                    f"{transaction.previous.active_revision}"
                ) from exc
            return transaction.new_credential

    def delete(
        self,
        environment: str,
        *,
        expected_revision: int | None = None,
        expected_generation: int | None = None,
    ) -> bool:
        """Delete an active credential and advance its revocation generation."""
        env = normalize_environment(environment)
        with self._metadata.lock():
            values = self._metadata.read()
            self._cleanup_pending_locked(values, env)
            return self._delete_locked(
                values,
                env,
                expected_revision,
                expected_generation,
            )

    def migrate(self, config_path: Path) -> MigrationResult:
        """Move one complete legacy file credential into this store."""
        return migrate(self, config_path)

    def _prepare_replace_locked(
        self,
        values: dict[str, EnvironmentState],
        environment: str,
        api_key: str,
        api_secret: str,
        *,
        state: CredentialState,
        account_id: str,
        expected_revision: int | None,
        expected_generation: int | None,
        activate: Callable[[Credential | None], None] | None = None,
    ) -> _PreparedReplacement:
        had_previous = environment in values
        previous = values.get(environment, EnvironmentState())
        _check_revision(previous, expected_revision)
        _check_generation(previous, expected_generation)

        previous_payload = None
        previous_credential = None
        if previous.active_revision is not None:
            previous_name = _record_name(
                self._metadata.namespace,
                environment,
                previous.active_revision,
            )
            previous_payload = self._backend.get(previous_name)
            if previous_payload is None:
                raise CredentialCorruptError(
                    "credential metadata points to missing revision "
                    f"{previous.active_revision} for {environment}"
                )
            previous_credential = _credential_from_payload(
                environment,
                previous,
                previous_payload,
                self.source,
            )

        revision = previous.next_revision
        now = _now()
        reserved = replace_fields(
            previous,
            next_revision=revision + 1,
            pending_revisions=(*previous.pending_revisions, revision),
        )
        current = EnvironmentState(
            active_revision=revision,
            next_revision=revision + 1,
            generation=previous.generation + 1,
            state=state,
            account_id=account_id.strip(),
            created_at=now,
            updated_at=now,
            validated_at=now if state is CredentialState.VERIFIED else "",
            pending_revisions=(
                *previous.pending_revisions,
                *(
                    (previous.active_revision,)
                    if previous.active_revision is not None
                    else ()
                ),
            ),
            preserved_records=previous.preserved_records,
        )
        new_payload = _encode_secret(api_key, api_secret)
        return _PreparedReplacement(
            backend=self._backend,
            metadata=self._metadata,
            values=values,
            environment=environment,
            had_previous=had_previous,
            previous=previous,
            previous_credential=previous_credential,
            previous_payload=previous_payload,
            reserved=reserved,
            current=current,
            new_revision=revision,
            new_name=_record_name(self._metadata.namespace, environment, revision),
            new_payload=new_payload,
            new_credential=_credential_from_payload(
                environment,
                current,
                new_payload,
                self.source,
            ),
            activate=activate,
        )

    def _get_locked(
        self,
        environment: str,
        values: dict[str, EnvironmentState],
    ) -> Credential | None:
        state = values.get(environment, EnvironmentState())
        revision = state.active_revision
        if revision is None:
            return None
        payload = self._backend.get(
            _record_name(self._metadata.namespace, environment, revision)
        )
        if payload is None:
            raise CredentialCorruptError(
                f"credential metadata points to missing revision {revision} for {environment}"
            )
        return _credential_from_payload(environment, state, payload, self.source)

    def _delete_locked(
        self,
        values: dict[str, EnvironmentState],
        environment: str,
        expected_revision: int | None,
        expected_generation: int | None,
    ) -> bool:
        previous = values.get(environment, EnvironmentState())
        _check_revision(previous, expected_revision)
        _check_generation(previous, expected_generation)
        if previous.active_revision is None:
            return False
        revision = previous.active_revision
        name = _record_name(self._metadata.namespace, environment, revision)
        payload = self._backend.get(name)

        tombstone = EnvironmentState(
            active_revision=None,
            next_revision=previous.next_revision,
            generation=previous.generation + 1,
            state=None,
            account_id="",
            created_at="",
            updated_at=_now(),
            validated_at="",
            pending_revisions=(
                previous.pending_revisions
                if payload is None
                else (*previous.pending_revisions, revision)
            ),
            preserved_records=previous.preserved_records,
        )
        values[environment] = tombstone
        self._metadata.write(values)

        if payload is None:
            return True

        try:
            self._backend.delete(name)
        except Exception as exc:
            try:
                restore_record(self._backend, name, payload)
            except Exception as restore_exc:
                raise CredentialStoreError(
                    "credential deletion failed after the disconnect was published; "
                    "the server remains disconnected and cleanup is pending"
                ) from restore_exc
            values[environment] = previous
            try:
                self._metadata.write(values)
            except Exception as rollback_exc:
                values[environment] = tombstone
                raise CredentialStoreError(
                    "credential deletion failed after the disconnect was published; "
                    "the record was restored but metadata rollback failed"
                ) from rollback_exc
            raise BackendOperationError(
                f"could not delete credential revision {revision} for {environment}"
            ) from exc

        cleaned = replace_fields(
            tombstone,
            pending_revisions=previous.pending_revisions,
        )
        values[environment] = cleaned
        try:
            self._metadata.write(values)
        except Exception as exc:
            values[environment] = tombstone
            logger.warning(
                "disconnected %s at generation %s, but pending cleanup metadata "
                "could not be cleared: %s",
                environment,
                tombstone.generation,
                exc,
            )
        return True

    def _retry_pending_cleanup(self) -> None:
        try:
            with self._metadata.lock():
                values = self._metadata.read()
                for environment in tuple(values):
                    self._cleanup_pending_locked(values, environment)
        except CredentialStoreError as exc:
            logger.warning("could not inspect pending credential cleanup: %s", exc)

    def _cleanup_pending_locked(
        self,
        values: dict[str, EnvironmentState],
        environment: str,
    ) -> None:
        previous = values.get(environment, EnvironmentState())
        if not previous.pending_revisions:
            return

        if previous.active_revision is not None:
            try:
                self._get_locked(environment, values)
            except CredentialCorruptError:
                # Failed rollback can remove the candidate before metadata recovers.
                # Preserve its old records and require explicit reconnect rather than
                # guessing which credential should regain authorization.
                values[environment] = _detach_records(
                    environment, previous, self._metadata.namespace
                )
                self._metadata.write(values)
                return

        remaining: list[int] = []
        for revision in previous.pending_revisions:
            name = _record_name(self._metadata.namespace, environment, revision)
            try:
                self._backend.delete(name)
            except Exception as exc:
                try:
                    still_present = self._backend.get(name) is not None
                except Exception as read_exc:
                    remaining.append(revision)
                    logger.warning(
                        "could not inspect inactive credential revision %s for %s: %s",
                        revision,
                        environment,
                        read_exc,
                    )
                    continue
                if still_present:
                    remaining.append(revision)
                    logger.warning(
                        "could not clean inactive credential revision %s for %s: %s",
                        revision,
                        environment,
                        exc,
                    )

        pending_revisions = tuple(remaining)
        if pending_revisions == previous.pending_revisions:
            return
        current = replace_fields(previous, pending_revisions=pending_revisions)
        values[environment] = current
        try:
            self._metadata.write(values)
        except Exception as exc:
            values[environment] = previous
            logger.warning(
                "cleaned inactive credentials for %s, but pending cleanup metadata "
                "could not be updated: %s",
                environment,
                exc,
            )

    def _observe_process_pair(
        self,
        environment: str,
        api_key: str,
        api_secret: str,
    ) -> int:
        pair = (api_key, api_secret)
        with self._process_lock:
            if self._process_pairs.get(environment) != pair:
                self._process_pairs[environment] = pair
                self._process_generations[environment] = (
                    self._process_generations.get(environment, 0) + 1
                )
            return self._process_generations[environment]

    def _clear_process_pair(self, environment: str) -> None:
        with self._process_lock:
            if self._process_pairs.pop(environment, None) is not None:
                self._process_generations[environment] = (
                    self._process_generations.get(environment, 0) + 1
                )


def _normalize_runtime_environment(environment: str) -> str:
    """Validate an environment that can receive process credentials."""
    value = environment.strip().lower()
    if value not in config_mod.BASE_URLS:
        raise ValueError(
            f"Delta environment must be one of {sorted(config_mod.BASE_URLS)}, "
            f"got {environment!r}"
        )
    return value


def _credential_from_payload(
    environment: str,
    state: EnvironmentState,
    payload: str,
    source: CredentialSource,
) -> Credential:
    key, secret = _decode_secret(payload)
    if state.state is None or state.active_revision is None:
        raise CredentialCorruptError(
            f"credential revision {state.active_revision} for {environment} "
            "has no validation state"
        )
    return Credential(
        environment=environment,
        revision=state.active_revision,
        generation=state.generation,
        state=state.state,
        source=source,
        account_id=state.account_id,
        created_at=state.created_at,
        updated_at=state.updated_at,
        validated_at=state.validated_at,
        api_key=key,
        api_secret=secret,
    )


def _check_revision(state: EnvironmentState, expected: int | None) -> None:
    if expected is None:
        return
    current = state.active_revision or 0
    if current != expected:
        raise CredentialConflictError(
            f"expected credential revision {expected}, but the active revision is {current}"
        )


def _check_generation(state: EnvironmentState, expected: int | None) -> None:
    if expected is None:
        return
    if state.generation != expected:
        raise CredentialConflictError(
            f"expected credential generation {expected}, but the current generation "
            f"is {state.generation}"
        )


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
