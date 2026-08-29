import json

import pytest

from delta_exchange_mcp.auth.consent import (
    ConsentBinding,
    ConsentStorageError,
    ConsentStore,
    StaleConsentError,
)


def binding(
    client_name: str = "Claude Desktop",
    environment: str = "india_prod",
    revision: int | None = 1,
    generation: int | None = 1,
) -> ConsentBinding:
    return ConsentBinding(
        client_name=client_name,
        environment=environment,
        credential_revision=revision,
        credential_generation=generation,
    )


def test_named_consent_persists_across_restart(tmp_path):
    path = tmp_path / "consent.json"
    first = ConsentStore(path, secure_backend_available=True)

    enabled = first.enable(binding(), expected_generation=0)

    second = ConsentStore(path, secure_backend_available=True)
    assert enabled.enabled is True
    assert enabled.persistent is True
    assert second.status(binding()) == enabled


def test_client_name_is_an_exact_partition(tmp_path):
    store = ConsentStore(tmp_path / "consent.json", secure_backend_available=True)
    store.enable(binding("Claude Desktop"), expected_generation=0)

    assert store.status(binding("claude desktop")).enabled is False
    assert store.status(binding("Claude Desktop ")).enabled is False


def test_environment_and_credential_revision_are_exact_partitions(tmp_path):
    store = ConsentStore(tmp_path / "consent.json", secure_backend_available=True)
    store.enable(binding(), expected_generation=0)

    assert store.status(binding(environment="india_testnet")).enabled is False
    assert store.status(binding(revision=2)).enabled is False
    assert store.status(binding(generation=2)).enabled is False


def test_unnamed_client_consent_is_process_only(tmp_path):
    path = tmp_path / "consent.json"
    first = ConsentStore(path, secure_backend_available=True)

    state = first.enable(binding(""), expected_generation=0)

    assert state.enabled is True
    assert state.persistent is False
    assert not path.exists()
    assert ConsentStore(path, secure_backend_available=True).status(binding("")).enabled is False


def test_no_secure_backend_keeps_all_consent_in_memory(tmp_path):
    path = tmp_path / "consent.json"
    first = ConsentStore(path, secure_backend_available=False)

    state = first.enable(binding(), expected_generation=0)

    assert state.persistent is False
    assert not path.exists()
    assert ConsentStore(path, secure_backend_available=False).status(binding()).enabled is False


def test_process_environment_credentials_get_session_only_consent(tmp_path):
    path = tmp_path / "consent.json"
    external = binding(revision=None, generation=None)
    first = ConsentStore(path, secure_backend_available=True)

    state = first.enable(external, expected_generation=0)

    assert state.persistent is False
    assert first.status(external).enabled is True
    assert not path.exists()


def test_manual_disable_invalidates_an_in_flight_lease(tmp_path):
    store = ConsentStore(tmp_path / "consent.json", secure_backend_available=True)
    enabled = store.enable(binding(), expected_generation=0)
    lease = store.lease(binding())
    assert lease is not None

    disabled = store.disable(binding(), expected_generation=enabled.generation)

    assert disabled.enabled is False
    assert store.accepts(lease) is False


def test_stale_browser_cannot_restore_manually_disabled_consent(tmp_path):
    store = ConsentStore(tmp_path / "consent.json", secure_backend_available=True)
    first = store.enable(binding(), expected_generation=0)
    store.disable(binding(), expected_generation=first.generation)

    with pytest.raises(StaleConsentError, match="expected consent generation 1, found 2"):
        store.enable(binding(), expected_generation=first.generation)


def test_revoke_environment_invalidates_all_bound_clients(tmp_path):
    store = ConsentStore(tmp_path / "consent.json", secure_backend_available=True)
    first = binding("Claude Desktop")
    second = binding("Cursor")
    store.enable(first, expected_generation=0)
    store.enable(second, expected_generation=0)
    testnet = binding("Cursor", environment="india_testnet")
    store.enable(testnet, expected_generation=0)

    store.revoke_environment("india_prod")

    assert store.status(first).enabled is False
    assert store.status(second).enabled is False
    assert store.status(testnet).enabled is True


def test_two_instances_reject_a_stale_concurrent_write(tmp_path):
    path = tmp_path / "consent.json"
    first = ConsentStore(path, secure_backend_available=True)
    second = ConsentStore(path, secure_backend_available=True)
    assert first.status(binding()).generation == 0
    assert second.status(binding()).generation == 0

    first.enable(binding(), expected_generation=0)

    with pytest.raises(StaleConsentError, match="expected consent generation 0, found 1"):
        second.disable(binding(), expected_generation=0)


def test_corrupt_metadata_fails_closed_and_is_not_replaced(tmp_path):
    path = tmp_path / "consent.json"
    path.write_text("not json")
    store = ConsentStore(path, secure_backend_available=True)

    with pytest.raises(ConsentStorageError, match="not valid JSON"):
        store.status(binding())
    with pytest.raises(ConsentStorageError, match="not valid JSON"):
        store.enable(binding(), expected_generation=0)
    assert path.read_text() == "not json"


def test_record_binding_tamper_is_rejected(tmp_path):
    path = tmp_path / "consent.json"
    store = ConsentStore(path, secure_backend_available=True)
    store.enable(binding(), expected_generation=0)
    payload = json.loads(path.read_text())
    record = next(iter(payload["records"].values()))
    record["client_name"] = "another client"
    path.write_text(json.dumps(payload))

    with pytest.raises(ConsentStorageError, match="does not match its binding"):
        store.status(binding())


def test_invalid_binding_values_are_rejected():
    with pytest.raises(ValueError, match="environment"):
        binding(environment="india_devnet")
    with pytest.raises(ValueError, match="credential_revision"):
        binding(revision=0)
    with pytest.raises(ValueError, match="both be set or absent"):
        binding(revision=None)
