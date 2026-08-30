"""Versioned credentials backed by an operating-system credential store."""

import hashlib
import io
import json
import logging
import os
import re
import secrets
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field, replace as replace_fields
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

import keyring
from dotenv import dotenv_values
from dotenv.parser import parse_stream
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

SERVICE_NAME = "delta-exchange-mcp"
METADATA_VERSION = 2
SECRET_VERSION = 1
DEFAULT_DIR = Path.home() / ".delta-exchange-mcp"
DEFAULT_METADATA_NAME = "credentials.json"
SUPPORTED_ENVIRONMENTS = frozenset({"india_prod", "india_testnet"})

logger = logging.getLogger(__name__)

_CREDENTIAL_NAMES = frozenset({"DELTA_API_KEY", "DELTA_API_SECRET"})
_LOCK_TIMEOUT_SECONDS = 10.0
_LOCK_POLL_SECONDS = 0.05
_SECURE_KEYRING_TYPES = frozenset(
    {
        "keyring.backends.macOS.Keyring",
        "keyring.backends.SecretService.Keyring",
        "keyring.backends.libsecret.Keyring",
        "keyring.backends.Windows.WinVaultKeyring",
    }
)

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class CredentialState(StrEnum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"


class CredentialSource(StrEnum):
    OS_STORE = "os_store"
    MEMORY = "memory"
    PROCESS = "process"


class MigrationStatus(StrEnum):
    ABSENT = "absent"
    INCOMPLETE = "incomplete"
    UNAVAILABLE = "unavailable"
    MIGRATED = "migrated"
    CONFLICT = "conflict"


class CredentialStoreError(Exception):
    pass


class BackendUnavailableError(CredentialStoreError):
    pass


class BackendOperationError(CredentialStoreError):
    pass


class CredentialConflictError(CredentialStoreError):
    pass


class CredentialCorruptError(CredentialStoreError):
    pass


class CredentialActivationError(CredentialStoreError):
    pass


class IncompleteCredentialError(CredentialStoreError):
    pass


class MetadataError(CredentialStoreError):
    pass


class MigrationError(CredentialStoreError):
    pass


@dataclass(frozen=True)
class CredentialMetadata:
    environment: str
    revision: int | None
    generation: int
    state: CredentialState | None
    account_id: str
    created_at: str
    updated_at: str
    validated_at: str
    pending_revisions: tuple[int, ...]
    reconnect_required: bool = False


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
class MigrationResult:
    status: MigrationStatus
    environment: str
    credential: Credential | None = None


@dataclass(frozen=True)
class _EnvironmentState:
    active_revision: int | None = None
    next_revision: int = 1
    generation: int = 0
    state: CredentialState | None = None
    account_id: str = ""
    created_at: str = ""
    updated_at: str = ""
    validated_at: str = ""
    pending_revisions: tuple[int, ...] = ()
    preserved_records: tuple[str, ...] = ()
    reconnect_required: bool = False

    def metadata(self, environment: str) -> CredentialMetadata:
        return CredentialMetadata(
            environment=environment,
            revision=self.active_revision,
            generation=self.generation,
            state=self.state,
            account_id=self.account_id,
            created_at=self.created_at,
            updated_at=self.updated_at,
            validated_at=self.validated_at,
            pending_revisions=self.pending_revisions,
            reconnect_required=self.reconnect_required,
        )


class SecretBackend(Protocol):
    def get(self, name: str) -> str | None: ...

    def set(self, name: str, value: str) -> None: ...

    def delete(self, name: str) -> None: ...


class MetadataBackend(Protocol):
    @property
    def namespace(self) -> str: ...

    def lock(self) -> AbstractContextManager[None]: ...

    def read(self) -> dict[str, _EnvironmentState]: ...

    def write(self, values: dict[str, _EnvironmentState]) -> None: ...


def default_metadata_path() -> Path:
    """Return the path for non-secret credential metadata."""
    return DEFAULT_DIR / DEFAULT_METADATA_NAME


def _backend_name(backend: KeyringBackend) -> str:
    kind = type(backend)
    return f"{kind.__module__}.{kind.__qualname__}"


class SystemKeyringBackend:
    """Store secrets only in an approved operating-system keyring backend."""

    def __init__(
        self,
        backend: KeyringBackend | None = None,
        service_name: str = SERVICE_NAME,
    ):
        try:
            selected = backend if backend is not None else keyring.get_keyring()
        except Exception as exc:
            raise BackendUnavailableError(
                f"could not discover a system keyring: {exc}"
            ) from exc
        name = _backend_name(selected)
        try:
            priority = selected.priority
        except Exception as exc:
            raise BackendUnavailableError(
                f"keyring backend {name} is unavailable: {exc}"
            ) from exc
        if name not in _SECURE_KEYRING_TYPES or priority <= 0:
            raise BackendUnavailableError(
                f"keyring backend {name} is not an approved system credential store"
            )
        self._backend = selected
        self._service_name = service_name

    def get(self, name: str) -> str | None:
        try:
            return self._backend.get_password(self._service_name, name)
        except Exception as exc:
            raise BackendOperationError(
                f"could not read {name} from the system store"
            ) from exc

    def set(self, name: str, value: str) -> None:
        try:
            self._backend.set_password(self._service_name, name, value)
        except Exception as exc:
            raise BackendOperationError(
                f"could not write {name} to the system store"
            ) from exc

    def delete(self, name: str) -> None:
        try:
            self._backend.delete_password(self._service_name, name)
        except PasswordDeleteError as exc:
            if self.get(name) is None:
                return
            raise BackendOperationError(
                f"could not delete {name} from the system store"
            ) from exc
        except Exception as exc:
            raise BackendOperationError(
                f"could not delete {name} from the system store"
            ) from exc


class MemorySecretBackend:
    """Process-local fallback used when no approved system keyring is available."""

    def __init__(self):
        self._values: dict[str, str] = {}
        self._lock = threading.RLock()

    def get(self, name: str) -> str | None:
        with self._lock:
            return self._values.get(name)

    def set(self, name: str, value: str) -> None:
        with self._lock:
            self._values[name] = value

    def delete(self, name: str) -> None:
        with self._lock:
            self._values.pop(name, None)


@contextmanager
def _file_lock(target: Path) -> Iterator[tuple[int, Path]]:
    lock_path = target.with_name(f".{target.name}.lock")
    try:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise MetadataError(f"could not open the lock for {target}: {exc}") from exc

    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    locked = False
    try:
        if os.name == "nt":
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            while not locked:
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    locked = True
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise MetadataError(
                            f"timed out waiting for {lock_path}"
                        ) from exc
                    time.sleep(_LOCK_POLL_SECONDS)
        else:
            while not locked:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise MetadataError(
                            f"timed out waiting for {lock_path}"
                        ) from exc
                    time.sleep(_LOCK_POLL_SECONDS)
        yield fd, lock_path
    finally:
        if locked:
            if os.name == "nt":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


class FileMetadata:
    """Read and atomically replace the non-secret credential metadata file."""

    def __init__(self, path: Path):
        # The persistent lock exists before the first metadata write. Resolve its
        # filesystem spelling so case aliases use the same namespace from startup.
        with _file_lock(path.resolve()) as (fd, lock_path):
            if os.name != "nt" and hasattr(fcntl, "F_GETPATH"):
                raw = fcntl.fcntl(fd, fcntl.F_GETPATH, b"\0" * 1024)
                lock_path = Path(os.fsdecode(raw.split(b"\0", 1)[0]))
            else:
                lock_path = lock_path.resolve()
            self.path = lock_path.with_name(
                lock_path.name.removeprefix(".").removesuffix(".lock")
            )
        self.namespace = hashlib.sha256(os.fsencode(self.path)).hexdigest()
        self._thread_lock = threading.RLock()

    @contextmanager
    def lock(self) -> Iterator[None]:
        with self._thread_lock:
            with _file_lock(self.path):
                yield

    def read(self) -> dict[str, _EnvironmentState]:
        try:
            raw = self.path.read_text()
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise MetadataError(f"could not read {self.path}: {exc}") from exc
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MetadataError(
                f"credential metadata in {self.path} is not valid JSON"
            ) from exc
        if (
            not isinstance(document, dict)
            or type(document.get("version")) is not int
            or document["version"] not in (1, METADATA_VERSION)
        ):
            raise MetadataError(
                f"credential metadata in {self.path} has an unsupported version"
            )
        environments = document.get("environments")
        if not isinstance(environments, dict):
            raise MetadataError(
                f"credential metadata in {self.path} has no environment map"
            )
        namespace = document.get("namespace")
        if document["version"] == METADATA_VERSION and (
            not isinstance(namespace, str)
            or re.fullmatch(r"[0-9a-f]{64}", namespace) is None
        ):
            raise MetadataError(
                f"credential metadata in {self.path} has an invalid namespace"
            )
        values = {
            _normalize_environment(name): _state_from_json(value, self.path)
            for name, value in environments.items()
        }
        if document["version"] == 1 or namespace != self.namespace:
            # Old global names and copied metadata cannot establish ownership.
            # Retain their record names for recovery, never read or delete them.
            return {
                environment: _detach_records(
                    environment,
                    state,
                    namespace if document["version"] == METADATA_VERSION else None,
                )
                for environment, state in values.items()
            }
        return values

    def write(self, values: dict[str, _EnvironmentState]) -> None:
        document = {
            "version": METADATA_VERSION,
            "namespace": self.namespace,
            "environments": {
                environment: _state_to_json(state)
                for environment, state in sorted(values.items())
            },
        }
        body = f"{json.dumps(document, indent=2, sort_keys=True)}\n"
        _atomic_replace(self.path, body, 0o600)


class MemoryMetadata:
    """Keep metadata in this process when no secure keyring is available."""

    def __init__(self):
        self.namespace = secrets.token_hex(32)
        self._values: dict[str, _EnvironmentState] = {}
        self._lock = threading.RLock()

    @contextmanager
    def lock(self) -> Iterator[None]:
        with self._lock:
            yield

    def read(self) -> dict[str, _EnvironmentState]:
        with self._lock:
            return dict(self._values)

    def write(self, values: dict[str, _EnvironmentState]) -> None:
        with self._lock:
            self._values = dict(values)


class CredentialStore:
    """Own versioned Delta credentials and their non-secret active pointers."""

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
            return cls(
                MemorySecretBackend(),
                MemoryMetadata(),
                CredentialSource.MEMORY,
                fallback_reason=str(exc),
            )
        store = cls(
            backend,
            FileMetadata(metadata_path or default_metadata_path()),
            CredentialSource.OS_STORE,
        )
        store._retry_pending_cleanup()
        return store

    @property
    def persistent(self) -> bool:
        """Whether credentials survive process exit."""
        return self.source is CredentialSource.OS_STORE

    def metadata(self, environment: str) -> CredentialMetadata:
        """Read active revision and revocation state without reading a secret."""
        env = _normalize_environment(environment)
        state = self._metadata.read().get(env, _EnvironmentState())
        return state.metadata(env)

    def generation(self, environment: str) -> int:
        """Read the revocation generation without accessing the keyring."""
        return self.metadata(environment).generation

    def get(self, environment: str) -> Credential | None:
        """Read the active credential for an environment."""
        env = _normalize_environment(environment)
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
        env = _normalize_environment(environment)
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
        env = _normalize_environment(environment)
        key = api_key.strip()
        secret = api_secret.strip()
        if not key or not secret:
            raise IncompleteCredentialError("an API key and secret are both required")

        with self._metadata.lock():
            values = self._metadata.read()
            self._cleanup_pending_locked(values, env)
            return self._replace_locked(
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

    def delete(
        self,
        environment: str,
        *,
        expected_revision: int | None = None,
        expected_generation: int | None = None,
    ) -> bool:
        """Delete an active credential and advance its revocation generation."""
        env = _normalize_environment(environment)
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
        with _file_lock(config_path):
            if config_path.is_symlink():
                return MigrationResult(MigrationStatus.UNAVAILABLE, "")
            try:
                original = config_path.read_text()
            except FileNotFoundError:
                return MigrationResult(MigrationStatus.ABSENT, "")
            except OSError as exc:
                raise MigrationError(f"could not read {config_path}: {exc}") from exc

            parsed = dotenv_values(stream=io.StringIO(original))
            key = (parsed.get("DELTA_API_KEY") or "").strip()
            secret = (parsed.get("DELTA_API_SECRET") or "").strip()
            environment = (parsed.get("DELTA_MCP_ENV") or "india_prod").strip().lower()
            env = _normalize_environment(environment)
            if not key and not secret:
                return MigrationResult(MigrationStatus.ABSENT, env)
            if not key or not secret:
                return MigrationResult(MigrationStatus.INCOMPLETE, env)
            if not self.persistent:
                return MigrationResult(MigrationStatus.UNAVAILABLE, env)

            rewritten = _without_credentials(original)
            staged = _stage_replacement(config_path, rewritten)
            try:
                with self._metadata.lock():
                    values = self._metadata.read()
                    self._cleanup_pending_locked(values, env)
                    current = self._get_locked(env, values)
                    if current is not None and (
                        current.api_key != key or current.api_secret != secret
                    ):
                        return MigrationResult(
                            MigrationStatus.CONFLICT,
                            env,
                            current,
                        )

                    expected_revision = current.revision if current is not None else 0
                    state = (
                        current.state
                        if current is not None
                        else CredentialState.UNVERIFIED
                    )
                    account_id = current.account_id if current is not None else ""

                    def publish_config(_credential: Credential) -> None:
                        nonlocal staged
                        try:
                            os.replace(staged, config_path)
                        except OSError as exc:
                            raise MigrationError(
                                f"could not publish the migrated {config_path}: {exc}"
                            ) from exc
                        staged = None
                        _sync_after_publication(config_path.parent)

                    migrated = self._replace_locked(
                        values,
                        env,
                        key,
                        secret,
                        state=state,
                        account_id=account_id,
                        expected_revision=expected_revision,
                        expected_generation=values.get(
                            env,
                            _EnvironmentState(),
                        ).generation,
                        publish=publish_config,
                        keep_active_on_retirement_failure=True,
                    )
                    return MigrationResult(MigrationStatus.MIGRATED, env, migrated)
            finally:
                if staged is not None:
                    staged.unlink(missing_ok=True)

    def _get_locked(
        self,
        environment: str,
        values: dict[str, _EnvironmentState],
    ) -> Credential | None:
        return self._credential(
            environment,
            values.get(environment, _EnvironmentState()),
        )

    def _replace_locked(
        self,
        values: dict[str, _EnvironmentState],
        environment: str,
        api_key: str,
        api_secret: str,
        *,
        state: CredentialState,
        account_id: str,
        expected_revision: int | None,
        expected_generation: int | None,
        activate: Callable[[Credential | None], None] | None = None,
        publish: Callable[[Credential], None] | None = None,
        keep_active_on_retirement_failure: bool = False,
    ) -> Credential:
        had_previous = environment in values
        previous = values.get(environment, _EnvironmentState())
        _check_revision(previous, expected_revision)
        _check_generation(previous, expected_generation)
        previous_credential = self._credential(environment, previous)
        previous_payload = None
        if previous.active_revision is not None:
            previous_payload = self._backend.get(
                self._record_name(environment, previous.active_revision)
            )
            if previous_payload is None:
                raise CredentialCorruptError(
                    f"credential metadata points to missing revision "
                    f"{previous.active_revision} for {environment}"
                )

        revision = previous.next_revision
        now = _now()
        current = _EnvironmentState(
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
        name = self._record_name(environment, revision)
        payload = _encode_secret(api_key, api_secret)
        self._backend.set(name, payload)
        if self._backend.get(name) != payload:
            self._delete_new(name)
            raise BackendOperationError(
                f"the system store did not return credential revision {revision} after writing it"
            )

        values[environment] = current
        try:
            self._metadata.write(values)
        except Exception:
            self._delete_new(name)
            raise

        try:
            credential = self._credential(environment, current)
            if credential is None:
                raise CredentialCorruptError(
                    "the new credential has no active revision"
                )
            if activate is not None:
                activate(credential)
        except Exception as exc:
            self._rollback_replace(
                values,
                environment,
                previous,
                had_previous,
                name,
                activate,
                previous_credential,
            )
            if isinstance(exc, CredentialStoreError):
                raise
            raise CredentialActivationError(
                f"could not activate credential revision {revision}"
            ) from exc

        try:
            if publish is not None:
                publish(credential)
        except Exception:
            self._rollback_replace(
                values,
                environment,
                previous,
                had_previous,
                name,
                activate,
                previous_credential,
            )
            raise

        if previous.active_revision is not None:
            old_name = self._record_name(environment, previous.active_revision)
            try:
                self._backend.delete(old_name)
            except Exception as exc:
                if keep_active_on_retirement_failure:
                    logger.warning(
                        "could not retire inactive credential revision %s for %s "
                        "after migration publication; cleanup remains pending: %s",
                        previous.active_revision,
                        environment,
                        exc,
                    )
                    return credential
                if previous_payload is None:
                    raise CredentialStoreError(
                        "credential retirement failed after removing the old record"
                    ) from exc
                try:
                    self._restore_record(old_name, previous_payload)
                except Exception as restore_exc:
                    raise CredentialStoreError(
                        "credential retirement failed and the old record could not be restored"
                    ) from restore_exc
                try:
                    self._rollback_replace(
                        values,
                        environment,
                        previous,
                        had_previous,
                        name,
                        activate,
                        previous_credential,
                    )
                except Exception as rollback_exc:
                    raise CredentialStoreError(
                        "credential retirement failed and its transaction rollback also failed"
                    ) from rollback_exc
                raise BackendOperationError(
                    f"could not retire credential revision {previous.active_revision}"
                ) from exc
            cleaned = replace_fields(
                current,
                pending_revisions=previous.pending_revisions,
            )
            values[environment] = cleaned
            try:
                self._metadata.write(values)
            except Exception as exc:
                values[environment] = current
                logger.warning(
                    "activated credential revision %s for %s, but pending cleanup "
                    "metadata could not be cleared: %s",
                    revision,
                    environment,
                    exc,
                )
        return credential

    def _delete_locked(
        self,
        values: dict[str, _EnvironmentState],
        environment: str,
        expected_revision: int | None,
        expected_generation: int | None,
    ) -> bool:
        previous = values.get(environment, _EnvironmentState())
        _check_revision(previous, expected_revision)
        _check_generation(previous, expected_generation)
        if previous.active_revision is None:
            return False
        revision = previous.active_revision
        name = self._record_name(environment, revision)
        payload = self._backend.get(name)

        tombstone = _EnvironmentState(
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
                self._restore_record(name, payload)
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
        values: dict[str, _EnvironmentState],
        environment: str,
    ) -> None:
        previous = values.get(environment, _EnvironmentState())
        if not previous.pending_revisions:
            return

        remaining: list[int] = []
        for revision in previous.pending_revisions:
            name = self._record_name(environment, revision)
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
        current = replace_fields(
            previous,
            pending_revisions=pending_revisions,
        )
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

    def _credential(
        self, environment: str, state: _EnvironmentState
    ) -> Credential | None:
        revision = state.active_revision
        if revision is None:
            return None
        payload = self._backend.get(self._record_name(environment, revision))
        if payload is None:
            raise CredentialCorruptError(
                f"credential metadata points to missing revision {revision} for {environment}"
            )
        key, secret = _decode_secret(payload)
        if state.state is None:
            raise CredentialCorruptError(
                f"credential revision {revision} for {environment} has no validation state"
            )
        return Credential(
            environment=environment,
            revision=revision,
            generation=state.generation,
            state=state.state,
            source=self.source,
            account_id=state.account_id,
            created_at=state.created_at,
            updated_at=state.updated_at,
            validated_at=state.validated_at,
            api_key=key,
            api_secret=secret,
        )

    def _record_name(self, environment: str, revision: int) -> str:
        return _record_name(self._metadata.namespace, environment, revision)

    def _delete_new(self, name: str) -> None:
        try:
            self._backend.delete(name)
        except Exception as exc:
            raise CredentialStoreError(
                f"credential write failed and temporary record {name} could not be removed"
            ) from exc

    def _restore_record(self, name: str, payload: str) -> None:
        if self._backend.get(name) == payload:
            return
        self._backend.set(name, payload)
        if self._backend.get(name) != payload:
            raise CredentialStoreError(
                f"credential record {name} could not be restored"
            )

    def _rollback_replace(
        self,
        values: dict[str, _EnvironmentState],
        environment: str,
        previous: _EnvironmentState,
        had_previous: bool,
        new_name: str,
        activate: Callable[[Credential | None], None] | None,
        previous_credential: Credential | None,
    ) -> None:
        if had_previous:
            values[environment] = previous
        else:
            values.pop(environment, None)
        self._metadata.write(values)
        try:
            if activate is not None:
                activate(previous_credential)
        finally:
            self._delete_new(new_name)


def _normalize_environment(environment: str) -> str:
    value = environment.strip().lower()
    if value not in SUPPORTED_ENVIRONMENTS:
        raise ValueError(
            f"Delta environment must be one of {sorted(SUPPORTED_ENVIRONMENTS)}, "
            f"got {environment!r}"
        )
    return value


def _record_name(namespace: str, environment: str, revision: int) -> str:
    return f"credential:{namespace}:{environment}:{revision}"


def _detach_records(
    environment: str, state: _EnvironmentState, namespace: str | None
) -> _EnvironmentState:
    revisions = (
        *state.pending_revisions,
        *((state.active_revision,) if state.active_revision is not None else ()),
    )
    records = tuple(
        _record_name(namespace, environment, revision)
        if namespace is not None
        else f"credential:{environment}:{revision}"
        for revision in revisions
    )
    return _EnvironmentState(
        next_revision=state.next_revision,
        generation=state.generation + 1,
        updated_at=state.updated_at,
        preserved_records=tuple(dict.fromkeys((*state.preserved_records, *records))),
        reconnect_required=state.active_revision is not None
        or state.reconnect_required,
    )


def _encode_secret(api_key: str, api_secret: str) -> str:
    return json.dumps(
        {"version": SECRET_VERSION, "api_key": api_key, "api_secret": api_secret},
        separators=(",", ":"),
        sort_keys=True,
    )


def _decode_secret(payload: str) -> tuple[str, str]:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise CredentialCorruptError("the credential record is not valid JSON") from exc
    if not isinstance(value, dict) or value.get("version") != SECRET_VERSION:
        raise CredentialCorruptError("the credential record has an unsupported version")
    key = value.get("api_key")
    secret = value.get("api_secret")
    if not isinstance(key, str) or not key or not isinstance(secret, str) or not secret:
        raise CredentialCorruptError(
            "the credential record has no complete credential pair"
        )
    return key, secret


def _check_revision(state: _EnvironmentState, expected: int | None) -> None:
    if expected is None:
        return
    current = state.active_revision or 0
    if current != expected:
        raise CredentialConflictError(
            f"expected credential revision {expected}, but the active revision is {current}"
        )


def _check_generation(state: _EnvironmentState, expected: int | None) -> None:
    if expected is None:
        return
    if state.generation != expected:
        raise CredentialConflictError(
            f"expected credential generation {expected}, but the current generation "
            f"is {state.generation}"
        )


def _state_from_json(value: object, path: Path) -> _EnvironmentState:
    if not isinstance(value, dict):
        raise MetadataError(
            f"credential metadata in {path} has an invalid environment entry"
        )
    try:
        active = value["active_revision"]
        next_revision = value["next_revision"]
        generation = value["generation"]
        raw_state = value["state"]
        state = CredentialState(raw_state) if raw_state is not None else None
        account_id = value["account_id"]
        created_at = value["created_at"]
        updated_at = value["updated_at"]
        validated_at = value["validated_at"]
        raw_pending = value.get("pending_revisions", [])
        raw_preserved = value.get("preserved_records", [])
        reconnect_required = value.get("reconnect_required", False)
    except (KeyError, TypeError, ValueError) as exc:
        raise MetadataError(
            f"credential metadata in {path} has an invalid entry"
        ) from exc
    if active is not None and (type(active) is not int or active < 1):
        raise MetadataError(
            f"credential metadata in {path} has an invalid active revision"
        )
    if type(next_revision) is not int or next_revision < 1:
        raise MetadataError(
            f"credential metadata in {path} has an invalid next revision"
        )
    if type(generation) is not int or generation < 0:
        raise MetadataError(f"credential metadata in {path} has an invalid generation")
    if type(reconnect_required) is not bool or (
        reconnect_required and active is not None
    ):
        raise MetadataError(
            f"credential metadata in {path} has an invalid reconnect state"
        )
    if not isinstance(raw_preserved, list) or any(
        not isinstance(name, str)
        or re.fullmatch(
            r"credential:(?:[0-9a-f]{64}:)?india_(?:prod|testnet):[1-9][0-9]*", name
        )
        is None
        for name in raw_preserved
    ):
        raise MetadataError(
            f"credential metadata in {path} has invalid preserved records"
        )
    if not isinstance(raw_pending, list) or any(
        type(revision) is not int or revision < 1 for revision in raw_pending
    ):
        raise MetadataError(
            f"credential metadata in {path} has invalid pending revisions"
        )
    pending_revisions = tuple(raw_pending)
    if len(set(pending_revisions)) != len(pending_revisions):
        raise MetadataError(
            f"credential metadata in {path} has duplicate pending revisions"
        )
    if any(revision >= next_revision for revision in pending_revisions):
        raise MetadataError(
            f"credential metadata in {path} has a pending revision that was not issued"
        )
    if active in pending_revisions:
        raise MetadataError(
            f"credential metadata in {path} marks its active revision for cleanup"
        )
    if active is not None and next_revision <= active:
        raise MetadataError(
            f"credential metadata in {path} would reuse an active revision"
        )
    if active is not None and state is None:
        raise MetadataError(f"credential metadata in {path} has no validation state")
    if not all(
        isinstance(item, str)
        for item in (account_id, created_at, updated_at, validated_at)
    ):
        raise MetadataError(f"credential metadata in {path} has an invalid text field")
    return _EnvironmentState(
        active_revision=active,
        next_revision=next_revision,
        generation=generation,
        state=state,
        account_id=account_id,
        created_at=created_at,
        updated_at=updated_at,
        validated_at=validated_at,
        pending_revisions=pending_revisions,
        preserved_records=tuple(raw_preserved),
        reconnect_required=reconnect_required,
    )


def _state_to_json(
    state: _EnvironmentState,
) -> dict[str, int | str | list[int] | list[str] | None]:
    return {
        "active_revision": state.active_revision,
        "next_revision": state.next_revision,
        "generation": state.generation,
        "state": state.state.value if state.state is not None else None,
        "account_id": state.account_id,
        "created_at": state.created_at,
        "updated_at": state.updated_at,
        "validated_at": state.validated_at,
        "pending_revisions": list(state.pending_revisions),
        "preserved_records": list(state.preserved_records),
        "reconnect_required": state.reconnect_required,
    }


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _atomic_replace(path: Path, body: str, mode: int) -> None:
    staged: Path | None = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}-", suffix=".tmp"
        )
        staged = Path(name)
        with os.fdopen(fd, "w") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(staged, mode)
        os.replace(staged, path)
        staged = None
        _sync_after_publication(path.parent)
    except OSError as exc:
        raise MetadataError(f"could not write {path}: {exc}") from exc
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def _stage_replacement(path: Path, body: str) -> Path:
    staged: Path | None = None
    try:
        mode = stat.S_IMODE(path.stat().st_mode) & 0o700
        fd, name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}-", suffix=".tmp"
        )
        staged = Path(name)
        with os.fdopen(fd, "w") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(staged, mode)
        return staged
    except OSError as exc:
        if staged is not None:
            staged.unlink(missing_ok=True)
        raise MigrationError(
            f"could not stage the migration for {path}: {exc}"
        ) from exc


def _without_credentials(body: str) -> str:
    return "".join(
        binding.original.string
        for binding in parse_stream(io.StringIO(body))
        if binding.key not in _CREDENTIAL_NAMES
    )


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sync_after_publication(path: Path) -> None:
    try:
        _sync_directory(path)
    except OSError as exc:
        # The replacement is already visible. A durability warning must not make
        # callers delete the secret that the published pointer now names.
        logger.warning("could not sync published directory %s: %s", path, exc)
