"""Secret backends and non-secret credential metadata persistence."""

import hashlib
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

import keyring
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

SERVICE_NAME = "delta-exchange-mcp"
METADATA_VERSION = 2
SECRET_VERSION = 1
DEFAULT_DIR = Path.home() / ".delta-exchange-mcp"
DEFAULT_METADATA_NAME = "credentials.json"
SUPPORTED_ENVIRONMENTS = frozenset({"india_prod", "india_testnet"})

logger = logging.getLogger(__name__)

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


class CredentialStoreError(Exception):
    pass


class BackendUnavailableError(CredentialStoreError):
    pass


class BackendOperationError(CredentialStoreError):
    pass


class CredentialCorruptError(CredentialStoreError):
    pass


class MetadataError(CredentialStoreError):
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
class EnvironmentState:
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

    def read(self) -> dict[str, EnvironmentState]: ...

    def write(self, values: dict[str, EnvironmentState]) -> None: ...


def restore_record(backend: SecretBackend, name: str, payload: str) -> None:
    """Restore and verify one secret record."""
    if backend.get(name) == payload:
        return
    backend.set(name, payload)
    if backend.get(name) != payload:
        raise CredentialStoreError(f"credential record {name} could not be restored")


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

    def read(self) -> dict[str, EnvironmentState]:
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
            normalize_environment(name): _state_from_json(value, self.path)
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

    def write(self, values: dict[str, EnvironmentState]) -> None:
        document = {
            "version": METADATA_VERSION,
            "namespace": self.namespace,
            "environments": {
                environment: asdict(state)
                for environment, state in sorted(values.items())
            },
        }
        body = f"{json.dumps(document, indent=2, sort_keys=True)}\n"
        _atomic_replace(self.path, body, 0o600)


class MemoryMetadata:
    """Keep metadata in this process when no secure keyring is available."""

    def __init__(self):
        self.namespace = secrets.token_hex(32)
        self._values: dict[str, EnvironmentState] = {}
        self._lock = threading.RLock()

    @contextmanager
    def lock(self) -> Iterator[None]:
        with self._lock:
            yield

    def read(self) -> dict[str, EnvironmentState]:
        with self._lock:
            return dict(self._values)

    def write(self, values: dict[str, EnvironmentState]) -> None:
        with self._lock:
            self._values = dict(values)


def normalize_environment(environment: str) -> str:
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
    environment: str, state: EnvironmentState, namespace: str | None
) -> EnvironmentState:
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
    return EnvironmentState(
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


def _state_from_json(value: object, path: Path) -> EnvironmentState:
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
    return EnvironmentState(
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
