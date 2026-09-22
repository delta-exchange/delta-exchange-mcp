import json
import threading
from contextlib import contextmanager

from delta_exchange_mcp.auth.backend import (
    BackendOperationError,
    FileMetadata,
    MemoryMetadata,
    MetadataError,
)
from delta_exchange_mcp.auth.store import CredentialSource, CredentialStore


class SimulatedProcessDeath(BaseException):
    pass


class FakeSecretBackend:
    def __init__(self):
        self.values: dict[str, str] = {}
        self.reads: list[str] = []
        self.deleted: list[str] = []
        self.mismatch: set[str] = set()
        self.fail_get: set[str] = set()
        self.crash_get: set[str] = set()
        self.fail_delete: set[str] = set()
        self.fail_delete_after: set[str] = set()
        self.crash_delete: set[str] = set()
        self.crash_delete_after: set[str] = set()
        self._lock = threading.RLock()

    def get(self, name):
        with self._lock:
            self.reads.append(name)
            if name in self.fail_get:
                self.fail_get.remove(name)
                raise BackendOperationError("read failed")
            if name in self.crash_get:
                self.crash_get.remove(name)
                raise SimulatedProcessDeath
            if name in self.mismatch and name in self.values:
                return "wrong readback"
            return self.values.get(name)

    def set(self, name, value):
        with self._lock:
            self.values[name] = value

    def delete(self, name):
        with self._lock:
            if name in self.fail_delete:
                self.fail_delete.remove(name)
                raise BackendOperationError("delete failed")
            if name in self.crash_delete:
                self.crash_delete.remove(name)
                raise SimulatedProcessDeath
            self.values.pop(name, None)
            self.deleted.append(name)
            if name in self.fail_delete_after:
                self.fail_delete_after.remove(name)
                raise BackendOperationError("delete failed after removal")
            if name in self.crash_delete_after:
                self.crash_delete_after.remove(name)
                raise SimulatedProcessDeath


class FailingMetadata:
    def __init__(self):
        self.inner = MemoryMetadata()
        self.fail_next_write = False

    @property
    def namespace(self):
        return self.inner.namespace

    @contextmanager
    def lock(self):
        with self.inner.lock():
            yield

    def read(self):
        return self.inner.read()

    def write(self, values):
        if self.fail_next_write:
            self.fail_next_write = False
            raise MetadataError("metadata write failed")
        self.inner.write(values)


def make_store(tmp_path, backend=None):
    secret_backend = backend or FakeSecretBackend()
    return (
        CredentialStore(
            secret_backend,
            FileMetadata(tmp_path / "credentials.json"),
            CredentialSource.OS_STORE,
        ),
        secret_backend,
    )


def record_name(credentials, revision=1, environment="india_prod"):
    return f"credential:{credentials._metadata.namespace}:{environment}:{revision}"


def legacy_metadata(tmp_path, backend):
    path = tmp_path / "credentials.json"
    document = {
        "version": 1,
        "environments": {
            "india_prod": {
                "active_revision": 2,
                "next_revision": 3,
                "generation": 2,
                "state": "verified",
                "account_id": "old-account",
                "created_at": "2026-08-01T00:00:00Z",
                "updated_at": "2026-08-01T00:00:00Z",
                "validated_at": "2026-08-01T00:00:00Z",
                "pending_revisions": [1],
            }
        },
    }
    path.write_text(json.dumps(document))
    for revision in (1, 2):
        backend.values[f"credential:india_prod:{revision}"] = "unowned secret record"
    return path
