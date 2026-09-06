import json
import threading

import pytest

from delta_exchange_mcp.auth import backend as auth_backend
from delta_exchange_mcp.auth import store as auth_store
from delta_exchange_mcp.auth.backend import (
    BackendOperationError,
    CredentialCorruptError,
    CredentialState,
    CredentialStoreError,
    FileMetadata,
)
from delta_exchange_mcp.auth.store import (
    CredentialActivationError,
    CredentialConflictError,
    CredentialSource,
    CredentialStore,
    IncompleteCredentialError,
)
from tests.credentials import (
    FailingMetadata,
    FakeSecretBackend,
    SimulatedProcessDeath,
    make_store,
    record_name,
)


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

    monkeypatch.setattr(auth_backend, "_sync_directory", fail_directory_sync)

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


def test_a_readback_error_after_set_removes_the_reserved_record(tmp_path):
    backend = FakeSecretBackend()
    credentials, _ = make_store(tmp_path, backend)
    name = record_name(credentials, 1)
    backend.fail_get.add(name)

    with pytest.raises(BackendOperationError, match="read failed"):
        credentials.replace("india_prod", "key", "secret")

    metadata = credentials.metadata("india_prod")
    assert metadata.revision is None
    assert metadata.pending_revisions == ()
    assert backend.values == {}


def test_failed_readback_cleanup_remains_retryable_after_restart(
    tmp_path,
    monkeypatch,
):
    backend = FakeSecretBackend()
    credentials, _ = make_store(tmp_path, backend)
    name = record_name(credentials, 1)
    backend.fail_get.add(name)
    backend.fail_delete.add(name)

    with pytest.raises(CredentialStoreError, match="cleanup remains pending"):
        credentials.replace("india_prod", "key", "secret")

    pending = credentials.metadata("india_prod")
    assert pending.revision is None
    assert pending.pending_revisions == (1,)
    assert set(backend.values) == {name}

    monkeypatch.setattr(auth_store, "SystemKeyringBackend", lambda: backend)
    restarted = CredentialStore.open(tmp_path / "credentials.json")

    assert restarted.metadata("india_prod").pending_revisions == ()
    assert restarted.get("india_prod") is None
    assert backend.values == {}


def test_process_death_during_rotation_readback_preserves_the_previous_record(
    tmp_path,
    monkeypatch,
):
    backend = FakeSecretBackend()
    credentials, _ = make_store(tmp_path, backend)
    first = credentials.replace("india_prod", "old-key", "old-secret")
    old_name = record_name(credentials, 1)
    name = record_name(credentials, 2)
    backend.crash_get.add(name)

    with pytest.raises(SimulatedProcessDeath):
        credentials.replace(
            "india_prod",
            "new-key",
            "new-secret",
            expected_revision=first.revision,
        )

    pending = credentials.metadata("india_prod")
    assert (pending.revision, pending.generation) == (1, 1)
    assert pending.pending_revisions == (2,)
    assert set(backend.values) == {old_name, name}

    monkeypatch.setattr(auth_store, "SystemKeyringBackend", lambda: backend)
    restarted = CredentialStore.open(tmp_path / "credentials.json")

    assert restarted.metadata("india_prod").pending_revisions == ()
    assert restarted.get("india_prod") == first
    assert set(backend.values) == {old_name}


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


@pytest.mark.parametrize("persistent", [False, True])
@pytest.mark.parametrize("failure", ["activation", "retirement"])
def test_rollback_continues_when_metadata_cannot_be_restored(
    tmp_path, monkeypatch, persistent, failure
):
    credentials, backend = make_store(tmp_path)
    first = credentials.replace("india_prod", "old-key", "old-secret")
    metadata = credentials._metadata
    write = metadata.write
    writes_blocked = False
    observed = []

    def fail_write(values):
        nonlocal writes_blocked
        if writes_blocked:
            writes_blocked = persistent
            raise auth_store.MetadataError("metadata write failed")
        write(values)

    def activate(credential):
        nonlocal writes_blocked
        observed.append(credential.revision if credential else None)
        if credential and credential.revision == 2:
            writes_blocked = True
            if failure == "activation":
                raise RuntimeError("rebind failed")
            backend.fail_delete.add(record_name(credentials, 1))

    monkeypatch.setattr(metadata, "write", fail_write)
    with pytest.raises(CredentialStoreError):
        credentials.replace("india_prod", "new-key", "new-secret", activate=activate)

    assert observed == [2, 1]
    assert set(backend.values) == {record_name(credentials, 1)}
    restarted = CredentialStore(
        backend, FileMetadata(tmp_path / "credentials.json"), CredentialSource.OS_STORE
    )
    if persistent:
        # The pointer cannot be repaired while metadata is unwritable. A later read
        # must refuse the missing candidate without deleting the previous secret.
        with pytest.raises(CredentialCorruptError, match="missing revision 2"):
            restarted.get("india_prod")
        assert set(backend.values) == {record_name(credentials, 1)}
    else:
        assert restarted.get("india_prod") == first


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


def test_devnet_accepts_only_process_credentials(tmp_path):
    credentials, backend = make_store(tmp_path)

    resolved = credentials.resolve(
        "india_devnet",
        {"DELTA_API_KEY": "dev-key", "DELTA_API_SECRET": "dev-secret"},
    )

    assert resolved is not None
    assert resolved.environment == "india_devnet"
    assert resolved.source is CredentialSource.PROCESS
    assert resolved.session_generation == 1
    assert resolved.revision is None
    assert resolved.generation is None
    assert credentials.process_generation("india_devnet") == 1
    assert credentials.resolve("india_devnet", {}) is None
    assert backend.values == {}


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
