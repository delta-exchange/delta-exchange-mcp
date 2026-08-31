import json

import pytest
from keyring.backend import KeyringBackend

from delta_exchange_mcp.auth import backend as auth_backend
from delta_exchange_mcp.auth import store as auth_store
from delta_exchange_mcp.auth.backend import (
    BackendUnavailableError,
    FileMetadata,
    MemoryMetadata,
    SystemKeyringBackend,
)
from delta_exchange_mcp.auth.migration import MigrationStatus
from delta_exchange_mcp.auth.store import (
    CredentialActivationError,
    CredentialConflictError,
    CredentialSource,
    CredentialStore,
)
from tests.credentials import (
    FakeSecretBackend,
    SimulatedProcessDeath,
    legacy_metadata,
    make_store,
    record_name,
)


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


@pytest.mark.parametrize("alias_kind", ["relative", "symlink", "case"])
def test_metadata_path_aliases_share_one_record_and_revision_lock(tmp_path, alias_kind):
    directory = tmp_path / "original"
    directory.mkdir()
    if alias_kind == "symlink":
        alias = tmp_path / "alias"
        try:
            alias.symlink_to(directory, target_is_directory=True)
        except OSError:
            pytest.skip("directory symlinks are unavailable on this system")
    elif alias_kind == "case":
        alias = tmp_path / "ORIGINAL"
        if not alias.exists():
            pytest.skip("this filesystem has case-sensitive directory names")
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


def test_record_names_follow_filesystem_case_rules_before_the_first_save(tmp_path):
    backend = FakeSecretBackend()
    first_path = tmp_path / "credentials.json"
    second_path = tmp_path / "CREDENTIALS.JSON"
    first = CredentialStore(
        backend, FileMetadata(first_path), CredentialSource.OS_STORE
    )
    second = CredentialStore(
        backend, FileMetadata(second_path), CredentialSource.OS_STORE
    )
    saved = first.replace("india_prod", "first-key", "secret")

    if second_path.exists() and first_path.samefile(second_path):
        assert second.get("india_prod") == saved
        updated = second.replace(
            "india_prod", "second-key", "secret", expected_revision=1
        )
        assert first.get("india_prod") == updated
        assert len(backend.values) == 1
    else:
        assert second.get("india_prod") is None
        second.replace("india_prod", "second-key", "secret", expected_revision=0)
        assert first.get("india_prod") == saved
        assert len(backend.values) == 2


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

    monkeypatch.setattr(auth_backend.keyring, "get_keyring", fail_discovery)

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
