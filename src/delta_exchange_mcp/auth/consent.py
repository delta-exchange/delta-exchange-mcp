"""Persistent trading consent for one MCP client and credential revision."""

import hashlib
import json
import os
import stat
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


SCHEMA_VERSION = 1
SUPPORTED_ENVIRONMENTS = frozenset({"india_prod", "india_testnet"})
_LOCK_TIMEOUT_SECONDS = 10.0
_LOCK_POLL_SECONDS = 0.05


class ConsentError(RuntimeError):
    """Base error for trading-consent operations."""


class StaleConsentError(ConsentError):
    """A browser action used an old consent generation."""


class ConsentStorageError(ConsentError):
    """The non-secret consent metadata could not be read or written safely."""


@dataclass(frozen=True)
class ConsentBinding:
    """The exact values that one trading approval authorizes."""

    client_name: str
    environment: str
    credential_revision: int | None
    credential_generation: int | None

    def __post_init__(self) -> None:
        if self.environment not in SUPPORTED_ENVIRONMENTS:
            raise ValueError(
                f"environment must be one of {sorted(SUPPORTED_ENVIRONMENTS)}"
            )
        versions = (self.credential_revision, self.credential_generation)
        if (versions[0] is None) != (versions[1] is None):
            raise ValueError(
                "credential_revision and credential_generation must both be set or absent"
            )
        if any(
            value is not None
            and (not isinstance(value, int) or isinstance(value, bool) or value < 1)
            for value in versions
        ):
            raise ValueError(
                "credential_revision and credential_generation must be positive integers"
            )

    @property
    def persistent(self) -> bool:
        """Whether this binding can persist beyond the current process."""
        return bool(
            self.client_name
            and self.credential_revision is not None
            and self.credential_generation is not None
        )


@dataclass(frozen=True)
class ConsentState:
    """The current approval state and its optimistic-concurrency generation."""

    enabled: bool
    generation: int
    persistent: bool


@dataclass(frozen=True)
class ConsentLease:
    """A point-in-time approval that must be checked again before a mutation."""

    binding: ConsentBinding
    generation: int


@dataclass(frozen=True)
class _Record:
    client_name: str
    environment: str
    credential_revision: int | None
    credential_generation: int | None
    enabled: bool
    generation: int

    @classmethod
    def from_value(cls, value: object) -> "_Record":
        if not isinstance(value, dict):
            raise ConsentStorageError("consent record must be an object")
        client_name = value.get("client_name")
        environment = value.get("environment")
        credential_revision = value.get("credential_revision")
        credential_generation = value.get("credential_generation")
        enabled = value.get("enabled")
        generation = value.get("generation")
        if not isinstance(client_name, str):
            raise ConsentStorageError("consent client_name must be a string")
        if environment not in SUPPORTED_ENVIRONMENTS:
            raise ConsentStorageError("consent environment is not supported")
        for name, item in (
            ("credential_revision", credential_revision),
            ("credential_generation", credential_generation),
        ):
            if not isinstance(item, int) or isinstance(item, bool) or item < 1:
                raise ConsentStorageError(
                    f"consent {name} must be a positive integer"
                )
        if not isinstance(enabled, bool):
            raise ConsentStorageError("consent enabled must be a boolean")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            raise ConsentStorageError("consent generation must be a non-negative integer")
        return cls(
            client_name=client_name,
            environment=environment,
            credential_revision=credential_revision,
            credential_generation=credential_generation,
            enabled=enabled,
            generation=generation,
        )

    @property
    def binding(self) -> ConsentBinding:
        return ConsentBinding(
            client_name=self.client_name,
            environment=self.environment,
            credential_revision=self.credential_revision,
            credential_generation=self.credential_generation,
        )


def _record_key(binding: ConsentBinding) -> str:
    """Create a stable opaque key without normalizing the client-provided name."""
    encoded = json.dumps(
        [
            binding.client_name,
            binding.environment,
            binding.credential_revision,
            binding.credential_generation,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class ConsentStore:
    """Store trading consent in atomic non-secret metadata or process memory."""

    def __init__(self, path: Path, *, secure_backend_available: bool) -> None:
        self._path = path
        self._secure_backend_available = secure_backend_available
        self._memory: dict[str, _Record] = {}
        self._memory_lock = threading.RLock()

    def status(self, binding: ConsentBinding) -> ConsentState:
        """Return current consent for the exact binding."""
        record = self._record(binding)
        return ConsentState(
            enabled=record.enabled if record is not None else False,
            generation=record.generation if record is not None else 0,
            persistent=self._is_persistent(binding),
        )

    def enable(
        self, binding: ConsentBinding, *, expected_generation: int
    ) -> ConsentState:
        """Enable all trading tools if the browser still has current state."""
        return self._set(binding, enabled=True, expected_generation=expected_generation)

    def disable(
        self, binding: ConsentBinding, *, expected_generation: int
    ) -> ConsentState:
        """Disable trading if the browser still has current state."""
        return self._set(binding, enabled=False, expected_generation=expected_generation)

    def lease(self, binding: ConsentBinding) -> ConsentLease | None:
        """Capture current consent for a later point-of-use check."""
        state = self.status(binding)
        if not state.enabled:
            return None
        return ConsentLease(binding=binding, generation=state.generation)

    def accepts(self, lease: ConsentLease) -> bool:
        """Check consent again immediately before a real Delta mutation."""
        state = self.status(lease.binding)
        return state.enabled and state.generation == lease.generation

    def revoke_environment(self, environment: str) -> None:
        """Revoke all stored approvals for an environment after disconnect or rotation."""
        if environment not in SUPPORTED_ENVIRONMENTS:
            raise ValueError(
                f"environment must be one of {sorted(SUPPORTED_ENVIRONMENTS)}"
            )
        with self._memory_lock:
            for key, record in list(self._memory.items()):
                if record.environment == environment:
                    self._memory[key] = _Record(
                        client_name=record.client_name,
                        environment=record.environment,
                        credential_revision=record.credential_revision,
                        credential_generation=record.credential_generation,
                        enabled=False,
                        generation=record.generation + 1,
                    )
        if not self._secure_backend_available:
            return
        with self._write_lock():
            records = self._read_file()
            changed = False
            for key, record in list(records.items()):
                if record.environment == environment:
                    records[key] = _Record(
                        client_name=record.client_name,
                        environment=record.environment,
                        credential_revision=record.credential_revision,
                        credential_generation=record.credential_generation,
                        enabled=False,
                        generation=record.generation + 1,
                    )
                    changed = True
            if changed:
                self._write_file(records)

    def _set(
        self,
        binding: ConsentBinding,
        *,
        enabled: bool,
        expected_generation: int,
    ) -> ConsentState:
        if expected_generation < 0:
            raise ValueError("expected_generation must be non-negative")
        if not self._is_persistent(binding):
            with self._memory_lock:
                key = _record_key(binding)
                current = self._memory.get(key)
                actual = current.generation if current is not None else 0
                if actual != expected_generation:
                    raise StaleConsentError(
                        f"expected consent generation {expected_generation}, found {actual}"
                    )
                next_record = _Record(
                    client_name=binding.client_name,
                    environment=binding.environment,
                    credential_revision=binding.credential_revision,
                    credential_generation=binding.credential_generation,
                    enabled=enabled,
                    generation=actual + 1,
                )
                self._memory[key] = next_record
            return ConsentState(
                enabled=next_record.enabled,
                generation=next_record.generation,
                persistent=False,
            )

        with self._write_lock():
            records = self._read_file()
            key = _record_key(binding)
            current = records.get(key)
            actual = current.generation if current is not None else 0
            if actual != expected_generation:
                raise StaleConsentError(
                    f"expected consent generation {expected_generation}, found {actual}"
                )
            next_record = _Record(
                client_name=binding.client_name,
                environment=binding.environment,
                credential_revision=binding.credential_revision,
                credential_generation=binding.credential_generation,
                enabled=enabled,
                generation=actual + 1,
            )
            records[key] = next_record
            self._write_file(records)
        return ConsentState(
            enabled=next_record.enabled,
            generation=next_record.generation,
            persistent=True,
        )

    def _record(self, binding: ConsentBinding) -> _Record | None:
        key = _record_key(binding)
        if not self._is_persistent(binding):
            with self._memory_lock:
                return self._memory.get(key)
        return self._read_file().get(key)

    def _is_persistent(self, binding: ConsentBinding) -> bool:
        return self._secure_backend_available and binding.persistent

    def _read_file(self) -> dict[str, _Record]:
        try:
            raw = self._path.read_text()
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise ConsentStorageError(f"cannot read consent metadata: {exc}") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConsentStorageError("consent metadata is not valid JSON") from exc
        if not isinstance(payload, dict) or payload.get("version") != SCHEMA_VERSION:
            raise ConsentStorageError("consent metadata has an unsupported version")
        values = payload.get("records")
        if not isinstance(values, dict):
            raise ConsentStorageError("consent metadata records must be an object")
        records: dict[str, _Record] = {}
        for key, value in values.items():
            if not isinstance(key, str):
                raise ConsentStorageError("consent record key must be a string")
            record = _Record.from_value(value)
            if key != _record_key(record.binding):
                raise ConsentStorageError("consent record key does not match its binding")
            records[key] = record
        return records

    def _write_file(self, records: dict[str, _Record]) -> None:
        try:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(
                dir=self._path.parent,
                prefix=f".{self._path.name}-",
                suffix=".tmp",
            )
            staged = Path(name)
            try:
                with os.fdopen(fd, "w") as handle:
                    json.dump(
                        {
                            "version": SCHEMA_VERSION,
                            "records": {
                                key: asdict(record) for key, record in records.items()
                            },
                        },
                        handle,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(staged, stat.S_IRUSR | stat.S_IWUSR)
                os.replace(staged, self._path)
            finally:
                staged.unlink(missing_ok=True)
        except OSError as exc:
            raise ConsentStorageError(f"cannot write consent metadata: {exc}") from exc

    @contextmanager
    def _write_lock(self) -> Iterator[None]:
        try:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            lock_path = self._path.with_name(f".{self._path.name}.lock")
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise ConsentStorageError(f"cannot open consent metadata lock: {exc}") from exc
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
                            raise ConsentStorageError(
                                "timed out waiting for consent metadata lock"
                            ) from exc
                        time.sleep(_LOCK_POLL_SECONDS)
            else:
                while not locked:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        locked = True
                    except BlockingIOError as exc:
                        if time.monotonic() >= deadline:
                            raise ConsentStorageError(
                                "timed out waiting for consent metadata lock"
                            ) from exc
                        time.sleep(_LOCK_POLL_SECONDS)
            yield
        finally:
            if locked:
                if os.name == "nt":
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
