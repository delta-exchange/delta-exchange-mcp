import json
import os
import threading
from contextlib import contextmanager

import pytest
from keyring.backend import KeyringBackend

from delta_exchange_mcp.auth import store as auth_store
from delta_exchange_mcp.auth.store import (
    BackendOperationError,
    BackendUnavailableError,
    CredentialActivationError,
    CredentialConflictError,
    CredentialCorruptError,
    CredentialSource,
    CredentialState,
    CredentialStore,
    FileMetadata,
    IncompleteCredentialError,
    MemoryMetadata,
    MigrationStatus,
    SystemKeyringBackend,
)


class SimulatedProcessDeath(BaseException):
    pass


class FakeSecretBackend:
    def __init__(self):
        self.values: dict[str, str] = {}
        self.reads: list[str] = []
        self.deleted: list[str] = []
        self.mismatch: set[str] = set()
        self.fail_delete: set[str] = set()
        self.fail_delete_after: set[str] = set()
        self.crash_delete: set[str] = set()
        self.crash_delete_after: set[str] = set()
        self._lock = threading.RLock()

    def get(self, name):
        with self._lock:
            self.reads.append(name)
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
            raise auth_store.MetadataError("metadata write failed")
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
        # These global records could belong to a different metadata folder.
        backend.values[f"credential:india_prod:{revision}"] = "unowned secret record"
    return path


def test_replace_publishes_one_active_revision_without_secrets_in_metadata(tmp_path):
    credentials, backend = make_store(tmp_path)

    saved = credentials.replace(
        "india_prod",
        "the-api-key",
        "the-api-secret",
        state=CredentialState.VERIFIED,
        account_id="account-123",
        expected_revision=0,
    )

    assert saved.revision == 1
    assert saved.generation == 1
    assert saved.state is CredentialState.VERIFIED
    assert saved.account_id == "account-123"
    assert saved.validated_at
    assert saved.source is CredentialSource.OS_STORE
    assert saved.session_only is False
    assert credentials.get("india_prod") == saved
    assert set(backend.values) == {record_name(credentials, 1)}

    body = (tmp_path / "credentials.json").read_text()
    assert "the-api-key" not in body
    assert "the-api-secret" not in body
    metadata = json.loads(body)["environments"]["india_prod"]
    assert metadata["active_revision"] == 1
    assert metadata["generation"] == 1


def test_metadata_without_pending_revisions_remains_readable(tmp_path):
    credentials, _ = make_store(tmp_path)
    saved = credentials.replace("india_prod", "key", "secret")
    metadata_path = tmp_path / "credentials.json"
    document = json.loads(metadata_path.read_text())
    document["environments"]["india_prod"].pop("pending_revisions")
    metadata_path.write_text(json.dumps(document))

    assert credentials.get("india_prod") == saved
    assert credentials.metadata("india_prod").pending_revisions == ()


def test_rotation_advances_revision_and_generation_then_deletes_the_old_record(
    tmp_path,
):
    credentials, backend = make_store(tmp_path)
    first = credentials.replace("india_prod", "old-key", "old-secret")

    second = credentials.replace(
        "india_prod",
        "new-key",
        "new-secret",
        expected_revision=first.revision,
    )

    assert (second.revision, second.generation) == (2, 2)
    assert (second.api_key, second.api_secret) == ("new-key", "new-secret")
    assert record_name(credentials, 1) not in backend.values
    assert set(backend.values) == {record_name(credentials, 2)}


def test_directory_sync_failure_after_metadata_publication_keeps_new_record_active(
    tmp_path,
    monkeypatch,
):
    credentials, backend = make_store(tmp_path)
    first = credentials.replace("india_prod", "old-key", "old-secret")
    directory_sync_failed = False

    def fail_directory_sync(path):
        nonlocal directory_sync_failed
        directory_sync_failed = True
        raise OSError(f"could not sync {path}")

    monkeypatch.setattr(auth_store, "_sync_directory", fail_directory_sync)

    second = credentials.replace(
        "india_prod",
        "new-key",
        "new-secret",
        expected_revision=first.revision,
    )

    assert (second.revision, second.generation) == (2, 2)
    assert directory_sync_failed is True
    assert credentials.get("india_prod") == second
    assert set(backend.values) == {record_name(credentials, 2)}


def test_each_environment_has_its_own_active_record(tmp_path):
    credentials, backend = make_store(tmp_path)

    prod = credentials.replace("india_prod", "prod-key", "prod-secret")
    testnet = credentials.replace("india_testnet", "test-key", "test-secret")

    assert (prod.revision, testnet.revision) == (1, 1)
    assert set(backend.values) == {
        record_name(credentials, 1),
        record_name(credentials, 1, "india_testnet"),
    }


@pytest.mark.parametrize("environment", ["india_prod", "india_testnet"])
def test_separate_metadata_folders_keep_their_own_credentials(tmp_path, environment):
    backend = FakeSecretBackend()
    first, _ = make_store(tmp_path / "first", backend)
    second, _ = make_store(tmp_path / "second", backend)

    first_saved = first.replace(
        environment, "first-key", "first-secret", account_id="first"
    )
    second_saved = second.replace(
        environment, "second-key", "second-secret", account_id="second"
    )

    assert first.get(environment) == first_saved
    assert second.get(environment) == second_saved
    assert len(backend.values) == 2


@pytest.mark.parametrize("operation", ["rotate", "disconnect"])
def test_other_metadata_folders_cannot_replace_or_delete_a_credential(
    tmp_path, operation
):
    backend = FakeSecretBackend()
    first, _ = make_store(tmp_path / "first", backend)
    second, _ = make_store(tmp_path / "second", backend)
    first_saved = first.replace("india_prod", "first-key", "first-secret")
    second.replace("india_prod", "second-key", "second-secret")

    if operation == "rotate":
        second.replace("india_prod", "rotated-key", "rotated-secret")
    else:
        second.delete("india_prod")

    assert first.get("india_prod") == first_saved


def test_metadata_files_in_the_same_folder_have_separate_records(tmp_path):
    backend = FakeSecretBackend()
    first, _ = make_store(tmp_path, backend)
    second = CredentialStore(
        backend, FileMetadata(tmp_path / "other.json"), CredentialSource.OS_STORE
    )
    saved = first.replace("india_prod", "first-key", "first-secret")
    second.replace("india_prod", "second-key", "second-secret")

    assert first.get("india_prod") == saved
    assert len(backend.values) == 2


@pytest.mark.parametrize("alias_kind", ["relative", "symlink"])
def test_metadata_path_aliases_share_one_record_and_revision_lock(tmp_path, alias_kind):
    directory = tmp_path / "original"
    directory.mkdir()
    if alias_kind == "symlink":
        alias = tmp_path / "alias"
        try:
            alias.symlink_to(directory, target_is_directory=True)
        except OSError:
            pytest.skip("directory symlinks are unavailable on this system")
    else:
        alias = directory / ".." / "original"
    backend = FakeSecretBackend()
    first, _ = make_store(directory, backend)
    second, _ = make_store(alias, backend)
    saved = first.replace("india_prod", "key", "secret")

    assert second.get("india_prod") == saved
    updated = second.replace("india_prod", "new-key", "secret", expected_revision=1)
    with pytest.raises(CredentialConflictError):
        first.replace("india_prod", "stale-key", "secret", expected_revision=1)
    assert first.get("india_prod") == updated
    assert len(backend.values) == 1


def test_memory_metadata_instances_do_not_share_secret_names():
    backend = FakeSecretBackend()
    first = CredentialStore(backend, MemoryMetadata(), CredentialSource.MEMORY)
    second = CredentialStore(backend, MemoryMetadata(), CredentialSource.MEMORY)
    saved = first.replace("india_prod", "first-key", "secret")
    second.replace("india_prod", "second-key", "secret")
    second.delete("india_prod")

    assert first.get("india_prod") == saved


def test_legacy_metadata_requires_reconnect_without_accessing_old_os_records(
    tmp_path, monkeypatch
):
    backend = FakeSecretBackend()
    path = legacy_metadata(tmp_path, backend)
    original = path.read_bytes()
    original_records = dict(backend.values)
    monkeypatch.setattr(auth_store, "SystemKeyringBackend", lambda: backend)

    for _ in range(2):
        credentials = CredentialStore.open(path)
        assert credentials.get("india_prod") is None
        metadata = credentials.metadata("india_prod")
        assert metadata.revision is None
        assert metadata.generation == 3
        assert metadata.reconnect_required is True
        assert metadata.pending_revisions == ()
        assert metadata.account_id == ""
        assert credentials.delete("india_prod") is False

    assert path.read_bytes() == original
    assert backend.values == original_records
    assert backend.reads == []
    assert backend.deleted == []


@pytest.mark.parametrize("operation", ["replace", "migrate"])
def test_reconnect_keeps_unowned_records_out_of_rotation_and_cleanup(
    tmp_path, operation
):
    backend = FakeSecretBackend()
    path = legacy_metadata(tmp_path, backend)
    original_records = dict(backend.values)
    credentials, _ = make_store(tmp_path, backend)

    if operation == "migrate":
        config_path = tmp_path / "config.env"
        config_path.write_text(
            "DELTA_API_KEY=new-key\nDELTA_API_SECRET=new-secret\nDELTA_MCP_MODE=trade\n"
        )
        migrated = credentials.migrate(config_path)
        assert migrated.status is MigrationStatus.MIGRATED
        assert config_path.read_text() == "DELTA_MCP_MODE=trade\n"
    else:
        credentials.replace(
            "india_prod",
            "new-key",
            "new-secret",
            expected_revision=0,
            expected_generation=3,
        )
    saved = credentials.get("india_prod")
    assert saved is not None
    assert (saved.api_key, saved.api_secret) == ("new-key", "new-secret")
    assert (saved.revision, saved.generation) == (3, 4)
    assert credentials.metadata("india_prod").reconnect_required is False
    credentials.replace("india_prod", "rotated-key", "secret")
    assert credentials.delete("india_prod") is True

    assert backend.values == original_records
    assert set(original_records).isdisjoint(backend.reads)
    assert set(original_records).isdisjoint(backend.deleted)
    document = json.loads(path.read_text())
    assert document["version"] == 2
    metadata = document["environments"]["india_prod"]
    assert set(metadata["preserved_records"]) == set(original_records)
    assert metadata["pending_revisions"] == []
    assert metadata["reconnect_required"] is False


def test_reconnect_activation_failure_preserves_recovery_metadata(tmp_path):
    backend = FakeSecretBackend()
    path = legacy_metadata(tmp_path, backend)
    original_records = dict(backend.values)
    credentials, _ = make_store(tmp_path, backend)

    def fail_activation(credential):
        if credential is not None:
            raise RuntimeError("cannot activate")

    with pytest.raises(CredentialActivationError):
        credentials.replace("india_prod", "new-key", "secret", activate=fail_activation)

    assert credentials.get("india_prod") is None
    assert credentials.metadata("india_prod").reconnect_required is True
    assert backend.values == original_records
    metadata = json.loads(path.read_text())["environments"]["india_prod"]
    assert set(metadata["preserved_records"]) == set(original_records)


def test_copied_metadata_cannot_read_or_clean_the_original_records(tmp_path):
    backend = FakeSecretBackend()
    original, _ = make_store(tmp_path / "original", backend)
    original.replace("india_prod", "old-key", "secret")
    backend.crash_delete.add(record_name(original, 1))
    with pytest.raises(SimulatedProcessDeath):
        original.replace("india_prod", "current-key", "secret")
    source_records = dict(backend.values)
    copied_path = tmp_path / "copy" / "credentials.json"
    copied_path.parent.mkdir()
    copied_path.write_bytes((tmp_path / "original" / "credentials.json").read_bytes())
    copied, _ = make_store(copied_path.parent, backend)
    backend.reads.clear()

    assert copied.get("india_prod") is None
    assert copied.metadata("india_prod").reconnect_required is True
    copied.replace("india_prod", "copy-key", "secret")
    copied.delete("india_prod")

    assert backend.values == source_records
    assert not set(source_records).intersection(backend.reads)
    preserved = json.loads(copied_path.read_text())["environments"]["india_prod"]
    assert set(preserved["preserved_records"]) == set(source_records)
    assert preserved["pending_revisions"] == []


@pytest.mark.parametrize("namespace", [None, 42, "", "a" * 63, "A" * 64])
def test_invalid_namespace_never_reads_or_deletes_credentials(tmp_path, namespace):
    credentials, backend = make_store(tmp_path)
    credentials.replace("india_prod", "key", "secret")
    path = tmp_path / "credentials.json"
    document = json.loads(path.read_text())
    document["namespace"] = namespace
    path.write_text(json.dumps(document))
    backend.reads.clear()
    backend.deleted.clear()

    with pytest.raises(auth_store.MetadataError, match="namespace"):
        credentials.get("india_prod")
    assert backend.reads == []
    assert backend.deleted == []


def test_a_stale_replace_never_writes_a_new_secret(tmp_path):
    credentials, backend = make_store(tmp_path)
    credentials.replace("india_prod", "key", "secret")

    with pytest.raises(CredentialConflictError):
        credentials.replace(
            "india_prod", "stale-key", "stale-secret", expected_revision=0
        )

    assert set(backend.values) == {record_name(credentials, 1)}


def test_a_failed_readback_removes_the_new_record_and_keeps_metadata_empty(tmp_path):
    backend = FakeSecretBackend()
    credentials, _ = make_store(tmp_path, backend)
    backend.mismatch.add(record_name(credentials, 1))

    with pytest.raises(BackendOperationError, match="did not return"):
        credentials.replace("india_prod", "key", "secret")

    assert backend.values == {}
    assert credentials.metadata("india_prod").revision is None


def test_old_record_delete_failure_rolls_the_rotation_back(tmp_path):
    credentials, backend = make_store(tmp_path)
    first = credentials.replace("india_prod", "old-key", "old-secret")
    backend.fail_delete.add(record_name(credentials, 1))

    with pytest.raises(BackendOperationError, match="could not retire"):
        credentials.replace(
            "india_prod", "new-key", "new-secret", expected_revision=first.revision
        )

    current = credentials.get("india_prod")
    assert current is not None
    assert (current.revision, current.generation) == (1, 1)
    assert (current.api_key, current.api_secret) == ("old-key", "old-secret")
    assert set(backend.values) == {record_name(credentials, 1)}


def test_activation_runs_after_publication_and_before_old_record_retirement(tmp_path):
    credentials, backend = make_store(tmp_path)
    first = credentials.replace("india_prod", "old-key", "old-secret")
    observed: list[int | None] = []

    def activate(credential):
        assert credentials.metadata("india_prod").revision == 2
        assert set(backend.values) == {
            record_name(credentials, 1),
            record_name(credentials, 2),
        }
        observed.append(credential.revision if credential is not None else None)

    second = credentials.replace(
        "india_prod",
        "new-key",
        "new-secret",
        expected_revision=first.revision,
        activate=activate,
    )

    assert second.revision == 2
    assert observed == [2]
    assert set(backend.values) == {record_name(credentials, 2)}


def test_activation_failure_restores_metadata_and_removes_the_new_record(tmp_path):
    credentials, backend = make_store(tmp_path)
    first = credentials.replace("india_prod", "old-key", "old-secret")
    observed: list[int | None] = []

    def activate(credential):
        revision = credential.revision if credential is not None else None
        observed.append(revision)
        if revision == 2:
            raise RuntimeError("rebind failed")

    with pytest.raises(CredentialActivationError, match="could not activate"):
        credentials.replace(
            "india_prod",
            "new-key",
            "new-secret",
            expected_revision=first.revision,
            activate=activate,
        )

    assert observed == [2, 1]
    assert credentials.get("india_prod") == first
    assert set(backend.values) == {record_name(credentials, 1)}


def test_retirement_failure_restores_a_record_deleted_before_the_error(tmp_path):
    credentials, backend = make_store(tmp_path)
    first = credentials.replace("india_prod", "old-key", "old-secret")
    backend.fail_delete_after.add(record_name(credentials, 1))
    observed: list[int | None] = []

    def activate(credential):
        observed.append(credential.revision if credential is not None else None)

    with pytest.raises(BackendOperationError, match="could not retire"):
        credentials.replace(
            "india_prod",
            "new-key",
            "new-secret",
            expected_revision=first.revision,
            activate=activate,
        )

    assert observed == [2, 1]
    assert credentials.get("india_prod") == first
    assert set(backend.values) == {record_name(credentials, 1)}


def test_delete_advances_the_generation_and_leaves_a_tombstone(tmp_path):
    credentials, backend = make_store(tmp_path)
    saved = credentials.replace("india_prod", "key", "secret")

    assert credentials.delete("india_prod", expected_revision=saved.revision) is True

    metadata = credentials.metadata("india_prod")
    assert credentials.get("india_prod") is None
    assert metadata.revision is None
    assert metadata.generation == 2
    assert metadata.state is None
    assert metadata.pending_revisions == ()
    assert backend.values == {}


def test_delete_failure_after_removal_restores_the_active_record(tmp_path):
    credentials, backend = make_store(tmp_path)
    saved = credentials.replace("india_prod", "key", "secret")
    backend.fail_delete_after.add(record_name(credentials, 1))

    with pytest.raises(BackendOperationError, match="could not delete credential"):
        credentials.delete("india_prod", expected_revision=saved.revision)

    assert credentials.get("india_prod") == saved
    metadata = credentials.metadata("india_prod")
    assert (metadata.revision, metadata.generation) == (1, 1)
    assert set(backend.values) == {record_name(credentials, 1)}


@pytest.mark.parametrize("crash_point", ["before_delete", "after_delete"])
def test_process_death_during_disconnect_leaves_a_retryable_tombstone(
    tmp_path,
    crash_point,
):
    credentials, backend = make_store(tmp_path)
    saved = credentials.replace("india_prod", "key", "secret")
    name = record_name(credentials, 1)
    if crash_point == "before_delete":
        backend.crash_delete.add(name)
    else:
        backend.crash_delete_after.add(name)

    with pytest.raises(SimulatedProcessDeath):
        credentials.delete(
            "india_prod",
            expected_revision=saved.revision,
            expected_generation=saved.generation,
        )

    tombstone = credentials.metadata("india_prod")
    assert tombstone.revision is None
    assert tombstone.generation == 2
    assert tombstone.pending_revisions == (1,)

    restarted = CredentialStore(
        backend,
        FileMetadata(tmp_path / "credentials.json"),
        CredentialSource.OS_STORE,
    )
    assert restarted.get("india_prod") is None
    cleaned = restarted.metadata("india_prod")
    assert cleaned.generation == 2
    assert cleaned.pending_revisions == ()
    assert backend.values == {}


def test_startup_retries_pending_disconnect_cleanup(tmp_path, monkeypatch):
    credentials, backend = make_store(tmp_path)
    saved = credentials.replace("india_prod", "key", "secret")
    backend.crash_delete.add(record_name(credentials, 1))

    with pytest.raises(SimulatedProcessDeath):
        credentials.delete(
            "india_prod",
            expected_revision=saved.revision,
            expected_generation=saved.generation,
        )

    monkeypatch.setattr(auth_store, "SystemKeyringBackend", lambda: backend)
    restarted = CredentialStore.open(tmp_path / "credentials.json")

    assert restarted.metadata("india_prod").pending_revisions == ()
    assert restarted.get("india_prod") is None
    assert backend.values == {}


def test_failed_pending_cleanup_stays_disconnected_and_retries(tmp_path):
    credentials, backend = make_store(tmp_path)
    saved = credentials.replace("india_prod", "key", "secret")
    name = record_name(credentials, 1)
    backend.crash_delete.add(name)

    with pytest.raises(SimulatedProcessDeath):
        credentials.delete(
            "india_prod",
            expected_revision=saved.revision,
            expected_generation=saved.generation,
        )

    backend.fail_delete.add(name)
    restarted = CredentialStore(
        backend,
        FileMetadata(tmp_path / "credentials.json"),
        CredentialSource.OS_STORE,
    )
    assert restarted.get("india_prod") is None
    assert restarted.metadata("india_prod").pending_revisions == (1,)
    assert set(backend.values) == {name}

    assert restarted.get("india_prod") is None
    assert restarted.metadata("india_prod").pending_revisions == ()
    assert backend.values == {}


def test_generation_cas_rejects_a_stale_absent_page_after_disconnect(tmp_path):
    credentials, backend = make_store(tmp_path)
    initial = credentials.metadata("india_prod")
    saved = credentials.replace(
        "india_prod",
        "key",
        "secret",
        expected_revision=0,
        expected_generation=initial.generation,
    )

    with pytest.raises(CredentialConflictError, match="generation"):
        credentials.delete(
            "india_prod",
            expected_revision=saved.revision,
            expected_generation=initial.generation,
        )

    assert credentials.delete(
        "india_prod",
        expected_revision=saved.revision,
        expected_generation=saved.generation,
    )

    with pytest.raises(CredentialConflictError, match="generation"):
        credentials.replace(
            "india_prod",
            "stale-key",
            "stale-secret",
            expected_revision=0,
            expected_generation=initial.generation,
        )

    tombstone = credentials.metadata("india_prod")
    assert (tombstone.revision, tombstone.generation) == (None, 2)
    assert backend.values == {}


def test_a_tombstone_publication_failure_prevents_secret_deletion():
    backend = FakeSecretBackend()
    metadata = FailingMetadata()
    credentials = CredentialStore(backend, metadata, CredentialSource.OS_STORE)
    saved = credentials.replace("india_prod", "key", "secret")
    metadata.fail_next_write = True

    with pytest.raises(auth_store.MetadataError, match="metadata write failed"):
        credentials.delete("india_prod", expected_revision=saved.revision)

    current = credentials.get("india_prod")
    assert current is not None
    assert current.revision == 1
    assert (current.api_key, current.api_secret) == ("key", "secret")
    assert backend.deleted == []


def test_expected_revision_serializes_concurrent_rotations(tmp_path):
    credentials, backend = make_store(tmp_path)
    first = credentials.replace("india_prod", "first", "secret")
    other_process_view = CredentialStore(
        backend,
        FileMetadata(tmp_path / "credentials.json"),
        CredentialSource.OS_STORE,
    )
    barrier = threading.Barrier(3)
    outcomes: list[int | str] = []

    writers = (credentials, other_process_view)

    def rotate_from(writer, key):
        barrier.wait()
        try:
            saved = writer.replace(
                "india_prod", key, "secret", expected_revision=first.revision
            )
            outcomes.append(saved.revision or 0)
        except CredentialConflictError:
            outcomes.append("conflict")

    threads = [
        threading.Thread(target=rotate_from, args=(writer, key))
        for writer, key in zip(writers, ("a", "b"), strict=True)
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(2)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(outcomes, key=str) == [2, "conflict"]
    assert credentials.metadata("india_prod").generation == 2


def test_missing_keyring_record_is_reported_as_corrupt_metadata(tmp_path):
    credentials, backend = make_store(tmp_path)
    credentials.replace("india_prod", "key", "secret")
    backend.values.clear()

    with pytest.raises(CredentialCorruptError, match="missing revision 1"):
        credentials.get("india_prod")


def test_disconnect_repairs_metadata_for_an_already_missing_keyring_record(tmp_path):
    credentials, backend = make_store(tmp_path)
    saved = credentials.replace("india_prod", "key", "secret")
    backend.values.clear()

    deleted = credentials.delete(
        "india_prod",
        expected_revision=saved.revision,
        expected_generation=saved.generation,
    )

    assert deleted is True
    assert credentials.get("india_prod") is None
    metadata = credentials.metadata("india_prod")
    assert (metadata.revision, metadata.generation) == (None, 2)
    assert metadata.pending_revisions == ()


def test_process_credentials_remain_external_and_have_no_persistent_revision(tmp_path):
    credentials, _ = make_store(tmp_path)
    credentials.replace("india_prod", "stored-key", "stored-secret")

    resolved = credentials.resolve(
        "india_prod",
        {"DELTA_API_KEY": "process-key", "DELTA_API_SECRET": "process-secret"},
    )

    assert resolved is not None
    assert resolved.source is CredentialSource.PROCESS
    assert resolved.externally_managed is True
    assert resolved.session_only is True
    assert resolved.session_generation == 1
    assert resolved.revision is None
    assert resolved.generation is None
    assert "process-key" not in repr(resolved)
    assert "process-secret" not in repr(resolved)


def test_process_pair_changes_advance_a_process_only_generation(tmp_path):
    credentials, _ = make_store(tmp_path)
    first_values = {
        "DELTA_API_KEY": "process-key",
        "DELTA_API_SECRET": "first-secret",
    }
    second_values = {
        "DELTA_API_KEY": "process-key",
        "DELTA_API_SECRET": "second-secret",
    }

    first = credentials.resolve("india_prod", first_values)
    unchanged = credentials.resolve("india_prod", first_values)
    changed = credentials.resolve("india_prod", second_values)
    assert first is not None
    assert unchanged is not None
    assert changed is not None
    assert unchanged.session_generation == first.session_generation
    assert changed.session_generation == (first.session_generation or 0) + 1

    assert credentials.resolve("india_prod", {}) is None
    restored = credentials.resolve("india_prod", second_values)
    assert restored is not None
    assert (restored.session_generation or 0) > (changed.session_generation or 0)


def test_a_partial_process_pair_fails_without_falling_through_to_the_store(tmp_path):
    credentials, _ = make_store(tmp_path)
    credentials.replace("india_prod", "stored-key", "stored-secret")

    with pytest.raises(IncompleteCredentialError, match="must supply"):
        credentials.resolve("india_prod", {"DELTA_API_KEY": "only-a-key"})


def test_no_approved_system_backend_uses_process_local_memory(monkeypatch, tmp_path):
    def unavailable():
        raise BackendUnavailableError("no secure backend")

    monkeypatch.setattr(auth_store, "SystemKeyringBackend", unavailable)
    credentials = CredentialStore.open(tmp_path / "credentials.json")

    saved = credentials.replace("india_prod", "key", "secret")

    assert credentials.persistent is False
    assert credentials.source is CredentialSource.MEMORY
    assert credentials.fallback_reason == "no secure backend"
    assert saved.session_only is True
    assert credentials.get("india_prod") == saved
    assert not (tmp_path / "credentials.json").exists()


def test_keyring_discovery_failure_uses_process_local_memory(monkeypatch, tmp_path):
    def fail_discovery():
        raise RuntimeError("backend discovery failed")

    monkeypatch.setattr(auth_store.keyring, "get_keyring", fail_discovery)

    credentials = CredentialStore.open(tmp_path / "credentials.json")

    assert credentials.source is CredentialSource.MEMORY
    assert "backend discovery failed" in credentials.fallback_reason
    assert not (tmp_path / "credentials.json").exists()


def test_memory_fallback_never_removes_the_only_persistent_copy(tmp_path):
    credentials = CredentialStore(
        auth_store.MemorySecretBackend(),
        MemoryMetadata(),
        CredentialSource.MEMORY,
    )
    config_path = tmp_path / "config.env"
    original = "DELTA_API_KEY=key\nDELTA_API_SECRET=secret\n"
    config_path.write_text(original)

    result = credentials.migrate(config_path)

    assert result.status is MigrationStatus.UNAVAILABLE
    assert config_path.read_text() == original
    assert credentials.get("india_prod") is None


class NullKeyring(KeyringBackend):
    priority = 0

    def get_password(self, service, username):
        return None

    def set_password(self, service, username, password):
        return None

    def delete_password(self, service, username):
        return None


class PlaintextKeyring(NullKeyring):
    __module__ = "keyrings.alt.file"
    priority = 1


@pytest.mark.parametrize("backend", [NullKeyring(), PlaintextKeyring()])
def test_null_and_plaintext_keyrings_are_rejected(backend):
    with pytest.raises(BackendUnavailableError, match="not an approved"):
        SystemKeyringBackend(backend)


def test_successful_migration_removes_only_credential_lines(tmp_path):
    credentials, _ = make_store(tmp_path)
    config_path = tmp_path / "config.env"
    original = (
        "# keep this comment\n"
        'export DELTA_API_KEY = "legacy-key"\n'
        "DELTA_API_SECRET='legacy-secret'\n"
        "DELTA_MCP_ENV=india_testnet\n"
        "DELTA_MCP_MODE=trade\n"
        "UNRELATED=value\n"
    )
    config_path.write_text(original)

    result = credentials.migrate(config_path)

    assert result.status is MigrationStatus.MIGRATED
    assert result.environment == "india_testnet"
    assert result.credential is not None
    assert result.credential.state is CredentialState.UNVERIFIED
    assert (result.credential.api_key, result.credential.api_secret) == (
        "legacy-key",
        "legacy-secret",
    )
    assert config_path.read_text() == (
        "# keep this comment\n"
        "DELTA_MCP_ENV=india_testnet\n"
        "DELTA_MCP_MODE=trade\n"
        "UNRELATED=value\n"
    )


def test_migration_rejects_a_symlink_without_leaving_secrets_in_its_target(
    tmp_path,
) -> None:
    credentials, backend = make_store(tmp_path)
    target = tmp_path / "real-config.env"
    target.write_text(
        "DELTA_API_KEY=legacy-key\n"
        "DELTA_API_SECRET=legacy-secret\n"
        "DELTA_MCP_ENV=india_prod\n"
    )
    config_path = tmp_path / "config.env"
    config_path.symlink_to(target)

    result = credentials.migrate(config_path)

    assert result.status is MigrationStatus.UNAVAILABLE
    assert "DELTA_API_KEY=legacy-key" in target.read_text()
    assert "DELTA_API_SECRET=legacy-secret" in target.read_text()
    assert backend.values == {}


def test_migration_removes_a_multiline_quoted_secret_as_one_setting(tmp_path):
    credentials, _ = make_store(tmp_path)
    config_path = tmp_path / "config.env"
    config_path.write_text(
        "DELTA_API_KEY=key\n"
        "DELTA_API_SECRET='first line\nsecond line'\n"
        "UNRELATED=value\n"
    )

    result = credentials.migrate(config_path)

    assert result.status is MigrationStatus.MIGRATED
    assert config_path.read_text() == "UNRELATED=value\n"
    saved = credentials.get("india_prod")
    assert saved is not None
    assert saved.api_secret == "first line\nsecond line"


def test_migration_of_an_incomplete_pair_leaves_the_file_unchanged(tmp_path):
    credentials, _ = make_store(tmp_path)
    config_path = tmp_path / "config.env"
    original = "DELTA_API_KEY=only-a-key\nDELTA_MCP_MODE=trade\n"
    config_path.write_text(original)

    result = credentials.migrate(config_path)

    assert result.status is MigrationStatus.INCOMPLETE
    assert config_path.read_text() == original
    assert credentials.get("india_prod") is None


def test_migration_readback_failure_leaves_the_file_unchanged(tmp_path):
    backend = FakeSecretBackend()
    credentials, _ = make_store(tmp_path, backend)
    backend.mismatch.add(record_name(credentials, 1))
    config_path = tmp_path / "config.env"
    original = "DELTA_API_KEY=key\nDELTA_API_SECRET=secret\nDELTA_MCP_MODE=trade\n"
    config_path.write_text(original)

    with pytest.raises(BackendOperationError, match="did not return"):
        credentials.migrate(config_path)

    assert config_path.read_text() == original
    assert credentials.get("india_prod") is None


def test_migration_publish_failure_rolls_back_the_new_record(tmp_path, monkeypatch):
    credentials, backend = make_store(tmp_path)
    config_path = tmp_path / "config.env"
    original = "DELTA_API_KEY=key\nDELTA_API_SECRET=secret\nDELTA_MCP_MODE=trade\n"
    config_path.write_text(original)
    real_replace = os.replace

    def fail_config_publish(source, target):
        if target == config_path:
            raise OSError("read-only config")
        return real_replace(source, target)

    monkeypatch.setattr(auth_store.os, "replace", fail_config_publish)

    with pytest.raises(auth_store.MigrationError, match="read-only config"):
        credentials.migrate(config_path)

    assert config_path.read_text() == original
    assert credentials.get("india_prod") is None
    assert backend.values == {}


def test_migration_publish_failure_restores_the_prior_active_record(
    tmp_path,
    monkeypatch,
):
    credentials, backend = make_store(tmp_path)
    first = credentials.replace("india_prod", "same-key", "same-secret")
    config_path = tmp_path / "config.env"
    original = (
        "DELTA_API_KEY=same-key\nDELTA_API_SECRET=same-secret\nDELTA_MCP_MODE=trade\n"
    )
    config_path.write_text(original)
    real_replace = os.replace

    def fail_config_publish(source, target):
        if target == config_path:
            raise OSError("read-only config")
        return real_replace(source, target)

    monkeypatch.setattr(auth_store.os, "replace", fail_config_publish)

    with pytest.raises(auth_store.MigrationError, match="read-only config"):
        credentials.migrate(config_path)

    assert config_path.read_text() == original
    assert credentials.get("india_prod") == first
    assert set(backend.values) == {record_name(credentials, 1)}


def test_config_directory_sync_failure_does_not_rollback_published_migration(
    tmp_path,
    monkeypatch,
):
    backend = FakeSecretBackend()
    credentials = CredentialStore(
        backend,
        FileMetadata(tmp_path / "metadata" / "credentials.json"),
        CredentialSource.OS_STORE,
    )
    config_path = tmp_path / "config.env"
    config_path.write_text(
        "DELTA_API_KEY=key\nDELTA_API_SECRET=secret\nDELTA_MCP_MODE=trade\n"
    )
    real_sync = auth_store._sync_directory
    config_sync_failed = False

    def fail_config_directory_sync(path):
        nonlocal config_sync_failed
        if path == config_path.parent:
            config_sync_failed = True
            raise OSError("config directory sync failed")
        real_sync(path)

    monkeypatch.setattr(auth_store, "_sync_directory", fail_config_directory_sync)

    result = credentials.migrate(config_path)

    assert result.status is MigrationStatus.MIGRATED
    assert config_sync_failed is True
    assert config_path.read_text() == "DELTA_MCP_MODE=trade\n"
    assert credentials.get("india_prod") == result.credential
    assert set(backend.values) == {record_name(credentials, 1)}


def test_migration_of_the_active_pair_advances_revision_and_generation(tmp_path):
    credentials, backend = make_store(tmp_path)
    first = credentials.replace(
        "india_prod",
        "same-key",
        "same-secret",
        state=CredentialState.VERIFIED,
        account_id="account-123",
    )
    config_path = tmp_path / "config.env"
    config_path.write_text(
        "DELTA_API_KEY=same-key\nDELTA_API_SECRET=same-secret\nDELTA_MCP_MODE=trade\n"
    )

    result = credentials.migrate(config_path)

    assert result.status is MigrationStatus.MIGRATED
    assert result.credential is not None
    assert (result.credential.revision, result.credential.generation) == (2, 2)
    assert result.credential.state is CredentialState.VERIFIED
    assert result.credential.account_id == "account-123"
    assert first.revision == 1
    assert config_path.read_text() == "DELTA_MCP_MODE=trade\n"
    assert set(backend.values) == {record_name(credentials, 2)}


@pytest.mark.parametrize("failure_mode", ["before_delete", "after_delete"])
def test_migration_stays_complete_when_inactive_record_retirement_fails(
    tmp_path,
    caplog,
    failure_mode,
):
    credentials, backend = make_store(tmp_path)
    first = credentials.replace("india_prod", "same-key", "same-secret")
    old_name = record_name(credentials, 1)
    if failure_mode == "before_delete":
        backend.fail_delete.add(old_name)
        expected_records = {old_name, record_name(credentials, 2)}
    else:
        backend.fail_delete_after.add(old_name)
        expected_records = {record_name(credentials, 2)}
    config_path = tmp_path / "config.env"
    config_path.write_text(
        "DELTA_API_KEY=same-key\nDELTA_API_SECRET=same-secret\nDELTA_MCP_MODE=trade\n"
    )

    result = credentials.migrate(config_path)

    assert result.status is MigrationStatus.MIGRATED
    assert result.credential is not None
    assert (result.credential.revision, result.credential.generation) == (2, 2)
    assert config_path.read_text() == "DELTA_MCP_MODE=trade\n"
    assert set(backend.values) == expected_records
    assert credentials.metadata("india_prod").pending_revisions == (1,)
    assert credentials.get("india_prod") == result.credential
    assert credentials.metadata("india_prod").pending_revisions == ()
    assert set(backend.values) == {record_name(credentials, 2)}
    assert first.revision == 1
    assert "could not retire inactive credential revision 1" in caplog.text
    assert "same-key" not in caplog.text
    assert "same-secret" not in caplog.text


@pytest.mark.parametrize("operation", ["delete", "rotate"])
def test_migration_serializes_concurrent_credential_changes(
    tmp_path,
    monkeypatch,
    operation,
):
    credentials, backend = make_store(tmp_path)
    first = credentials.replace("india_prod", "same-key", "same-secret")
    other_process_view = CredentialStore(
        backend,
        FileMetadata(tmp_path / "credentials.json"),
        CredentialSource.OS_STORE,
    )
    config_path = tmp_path / "config.env"
    config_path.write_text("DELTA_API_KEY=same-key\nDELTA_API_SECRET=same-secret\n")
    config_publish_started = threading.Event()
    release_config_publish = threading.Event()
    credential_change_started = threading.Event()
    migration_results = []
    outcomes: list[str] = []
    errors: list[Exception] = []
    real_replace = os.replace

    def pause_config_publish(source, target):
        if target == config_path:
            config_publish_started.set()
            if not release_config_publish.wait(5):
                raise OSError("timed out waiting to publish config")
        return real_replace(source, target)

    def run_migration():
        try:
            migration_results.append(credentials.migrate(config_path))
        except Exception as exc:
            errors.append(exc)

    def change_credential():
        credential_change_started.set()
        try:
            if operation == "delete":
                other_process_view.delete(
                    "india_prod",
                    expected_revision=first.revision,
                )
            else:
                other_process_view.replace(
                    "india_prod",
                    "rotated-key",
                    "rotated-secret",
                    expected_revision=first.revision,
                )
            outcomes.append("changed")
        except CredentialConflictError:
            outcomes.append("conflict")
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(auth_store.os, "replace", pause_config_publish)
    migration_thread = threading.Thread(target=run_migration)
    migration_thread.start()
    assert config_publish_started.wait(2)

    change_thread = threading.Thread(target=change_credential)
    change_thread.start()
    assert credential_change_started.wait(2)
    change_thread.join(0.1)
    try:
        assert change_thread.is_alive()
    finally:
        release_config_publish.set()

    migration_thread.join(2)
    change_thread.join(2)
    assert not migration_thread.is_alive()
    assert not change_thread.is_alive()
    assert errors == []
    assert outcomes == ["conflict"]
    assert len(migration_results) == 1
    migrated = migration_results[0]
    assert migrated.status is MigrationStatus.MIGRATED
    assert migrated.credential is not None
    assert (migrated.credential.revision, migrated.credential.generation) == (2, 2)
    assert credentials.get("india_prod") == migrated.credential
    assert set(backend.values) == {record_name(credentials, 2)}


def test_migration_never_overwrites_a_different_active_credential(tmp_path):
    credentials, _ = make_store(tmp_path)
    active = credentials.replace("india_prod", "current-key", "current-secret")
    config_path = tmp_path / "config.env"
    original = "DELTA_API_KEY=old-key\nDELTA_API_SECRET=old-secret\n"
    config_path.write_text(original)

    result = credentials.migrate(config_path)

    assert result.status is MigrationStatus.CONFLICT
    assert result.credential == active
    assert config_path.read_text() == original
    assert credentials.get("india_prod") == active


def test_migration_observes_a_concurrent_create_before_its_transaction(
    tmp_path,
    monkeypatch,
):
    credentials, backend = make_store(tmp_path)
    other_process_view = CredentialStore(
        backend,
        FileMetadata(tmp_path / "credentials.json"),
        CredentialSource.OS_STORE,
    )
    config_path = tmp_path / "config.env"
    original = "DELTA_API_KEY=file-key\nDELTA_API_SECRET=file-secret\n"
    config_path.write_text(original)
    real_stage = auth_store._stage_replacement

    def stage_after_browser_write(path, body):
        staged = real_stage(path, body)
        other_process_view.replace("india_prod", "browser-key", "browser-secret")
        return staged

    monkeypatch.setattr(auth_store, "_stage_replacement", stage_after_browser_write)

    result = credentials.migrate(config_path)

    assert result.status is MigrationStatus.CONFLICT
    assert result.credential is not None
    assert result.credential.api_key == "browser-key"
    assert config_path.read_text() == original
    assert set(backend.values) == {record_name(credentials, 1)}


@pytest.mark.parametrize("environment", ["india_devnet", "other", "../india_prod"])
def test_store_rejects_unsupported_environments(tmp_path, environment):
    credentials, backend = make_store(tmp_path)

    with pytest.raises(ValueError, match="must be one of"):
        credentials.replace(environment, "key", "secret")

    assert backend.values == {}


def test_generation_is_a_metadata_only_read(tmp_path, monkeypatch):
    credentials, backend = make_store(tmp_path)
    credentials.replace("india_prod", "key", "secret")

    monkeypatch.setattr(
        backend,
        "get",
        lambda name: pytest.fail(f"generation read touched secret {name}"),
    )

    assert credentials.generation("india_prod") == 1
