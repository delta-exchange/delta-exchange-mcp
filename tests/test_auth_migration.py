import os
import threading

import pytest

from delta_exchange_mcp.auth import backend as auth_backend
from delta_exchange_mcp.auth import migration as auth_migration
from delta_exchange_mcp.auth.backend import (
    BackendOperationError,
    CredentialState,
    FileMetadata,
)
from delta_exchange_mcp.auth.migration import MigrationError, MigrationStatus
from delta_exchange_mcp.auth.store import (
    CredentialConflictError,
    CredentialSource,
    CredentialStore,
)
from tests.credentials import FakeSecretBackend, make_store, record_name


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

    monkeypatch.setattr(auth_migration.os, "replace", fail_config_publish)

    with pytest.raises(MigrationError, match="read-only config"):
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

    monkeypatch.setattr(auth_migration.os, "replace", fail_config_publish)

    with pytest.raises(MigrationError, match="read-only config"):
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
    real_sync = auth_backend._sync_directory
    config_sync_failed = False

    def fail_config_directory_sync(path):
        nonlocal config_sync_failed
        if path == config_path.parent:
            config_sync_failed = True
            raise OSError("config directory sync failed")
        real_sync(path)

    monkeypatch.setattr(auth_backend, "_sync_directory", fail_config_directory_sync)

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

    monkeypatch.setattr(auth_migration.os, "replace", pause_config_publish)
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
    real_stage = auth_migration._stage_replacement

    def stage_after_browser_write(path, body):
        staged = real_stage(path, body)
        other_process_view.replace("india_prod", "browser-key", "browser-secret")
        return staged

    monkeypatch.setattr(auth_migration, "_stage_replacement", stage_after_browser_write)

    result = credentials.migrate(config_path)

    assert result.status is MigrationStatus.CONFLICT
    assert result.credential is not None
    assert result.credential.api_key == "browser-key"
    assert config_path.read_text() == original
    assert set(backend.values) == {record_name(credentials, 1)}
