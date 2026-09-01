import hashlib
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from delta_exchange_mcp.auth import consent as consent_mod
from delta_exchange_mcp.auth import backend as auth_backend
from delta_exchange_mcp.auth.consent import (
    ConsentBinding,
    ConsentStorageError,
    ConsentStore,
    MemoryConsentBackend,
    StaleConsentError,
)


def binding(
    client_name: str = "Claude Desktop",
    environment: str = "india_prod",
    revision: int | None = 1,
    generation: int | None = 1,
    session_generation: int | None = None,
    environment_generation: int = 0,
) -> ConsentBinding:
    return ConsentBinding(
        client_name=client_name,
        environment=environment,
        credential_revision=revision,
        credential_generation=generation,
        credential_session_generation=session_generation,
        environment_generation=environment_generation,
    )


def new_store(
    path: Path,
    *,
    secure_backend_available: bool = True,
    memory_backend: MemoryConsentBackend | None = None,
) -> ConsentStore:
    return ConsentStore(
        path,
        secure_backend_available=secure_backend_available,
        memory_backend=memory_backend or MemoryConsentBackend(),
    )


class _CredentialStoreView:
    """Simulate separate credential-store processes over authoritative metadata."""

    def __init__(self, metadata: dict[str, int | None]) -> None:
        self._metadata = metadata

    def current(self) -> tuple[int | None, int | None, int | None]:
        return (
            self._metadata["revision"],
            self._metadata["generation"],
            self._metadata["session_generation"],
        )

    def rotate(self) -> None:
        revision = self._metadata["revision"]
        generation = self._metadata["generation"]
        assert isinstance(revision, int)
        assert isinstance(generation, int)
        self._metadata.update(revision=revision + 1, generation=generation + 1)

    def disconnect(self) -> None:
        generation = self._metadata["generation"]
        assert isinstance(generation, int)
        self._metadata.update(revision=None, generation=generation + 1)


def test_named_consent_persists_across_restart(tmp_path):
    path = tmp_path / "consent.json"
    first = new_store(path)

    enabled = first.enable(binding(), expected_generation=0)

    second = new_store(path)
    assert enabled.enabled is True
    assert enabled.persistent is True
    assert second.status(binding()) == enabled


def test_client_name_is_an_exact_partition(tmp_path):
    store = new_store(tmp_path / "consent.json")
    store.enable(binding("Claude Desktop"), expected_generation=0)

    assert store.status(binding("claude desktop")).enabled is False
    assert store.status(binding("Claude Desktop ")).enabled is False


def test_environment_and_credential_revision_are_exact_partitions(tmp_path):
    store = new_store(tmp_path / "consent.json")
    store.enable(binding(), expected_generation=0)

    assert store.status(binding(environment="india_testnet")).enabled is False
    assert store.status(binding(revision=2)).enabled is False
    assert store.status(binding(generation=2)).enabled is False


def test_environment_generation_partitions_consent_and_survives_restart(tmp_path):
    path = tmp_path / "consent.json"
    store = new_store(path)
    earlier = binding()
    current = binding(environment_generation=2)
    store.enable(earlier, expected_generation=0)
    lease = store.lease(earlier)
    assert lease is not None

    assert store.status(current).enabled is False
    assert (
        store.accepts(
            lease,
            current_credential_revision=1,
            current_credential_generation=1,
            current_credential_session_generation=None,
            current_environment_generation=2,
        )
        is False
    )

    approved = store.enable(current, expected_generation=0)
    assert new_store(path).status(current) == approved


@pytest.mark.parametrize("persistent", [True, False])
def test_a_current_state_check_rejects_stale_first_approval(tmp_path, persistent):
    store = new_store(tmp_path / "consent.json", secure_backend_available=persistent)

    with pytest.raises(StaleConsentError, match="connection changed"):
        store.enable(binding(), expected_generation=0, check_current=lambda: False)

    assert store.status(binding()).enabled is False
    assert store.status(binding()).generation == 0
    assert not (tmp_path / "consent.json").exists()
    assert (
        store.enable(
            binding(), expected_generation=0, check_current=lambda: True
        ).enabled
        is True
    )


@pytest.mark.parametrize("persistent", [True, False])
def test_delayed_identity_revocation_keeps_newer_environment_approval(
    tmp_path, persistent
):
    store = new_store(tmp_path / "consent.json", secure_backend_available=persistent)
    old = binding()
    current = binding(environment_generation=2)
    store.enable(old, expected_generation=0)
    approved = store.enable(current, expected_generation=0)

    store.revoke_identity(old)

    assert store.status(old).enabled is False
    assert store.status(current) == approved


@pytest.mark.parametrize("persistent", [True, False])
def test_delayed_credential_completion_keeps_newer_approval(tmp_path, persistent):
    store = new_store(tmp_path / "consent.json", secure_backend_available=persistent)
    old = binding()
    current = binding(revision=2, generation=2)
    store.enable(old, expected_generation=0)
    approved = store.enable(current, expected_generation=0)

    store.revoke_before("india_prod", 2)

    assert store.status(old).enabled is False
    assert store.status(current) == approved


def test_unnamed_client_consent_is_process_only(tmp_path):
    path = tmp_path / "consent.json"
    first = new_store(path)

    state = first.enable(binding(""), expected_generation=0)

    assert state.enabled is True
    assert state.persistent is False
    assert not path.exists()
    assert new_store(path).status(binding("")).enabled is False


def test_no_secure_backend_keeps_all_consent_in_memory(tmp_path):
    path = tmp_path / "consent.json"
    first = new_store(path, secure_backend_available=False)

    state = first.enable(binding(), expected_generation=0)

    assert state.persistent is False
    assert not path.exists()
    assert (
        new_store(path, secure_backend_available=False).status(binding()).enabled
        is False
    )


def test_process_environment_credentials_get_session_only_consent(tmp_path):
    path = tmp_path / "consent.json"
    external = binding(revision=None, generation=None, session_generation=1)
    first = new_store(path)

    state = first.enable(external, expected_generation=0)
    lease = first.lease(external)

    assert state.persistent is False
    assert first.status(external).enabled is True
    assert lease is not None
    assert (
        first.accepts(
            lease,
            current_credential_revision=None,
            current_credential_generation=None,
            current_credential_session_generation=1,
        )
        is True
    )
    assert (
        first.accepts(
            lease,
            current_credential_revision=None,
            current_credential_generation=None,
            current_credential_session_generation=None,
        )
        is False
    )
    assert not path.exists()


def test_process_pair_change_invalidates_old_lease_and_requires_new_consent(tmp_path):
    path = tmp_path / "consent.json"
    memory_backend = MemoryConsentBackend()
    store = new_store(path, memory_backend=memory_backend)
    pair_a = binding(revision=None, generation=None, session_generation=1)
    pair_b = binding(revision=None, generation=None, session_generation=2)
    state_a = store.enable(pair_a, expected_generation=0)
    lease_a = store.lease(pair_a)
    assert state_a.persistent is False
    assert lease_a is not None

    assert (
        store.accepts(
            lease_a,
            current_credential_revision=None,
            current_credential_generation=None,
            current_credential_session_generation=2,
        )
        is False
    )
    assert store.status(pair_b).enabled is False

    store.enable(pair_b, expected_generation=0)
    lease_b = store.lease(pair_b)
    assert lease_b is not None
    assert (
        store.accepts(
            lease_b,
            current_credential_revision=None,
            current_credential_generation=None,
            current_credential_session_generation=2,
        )
        is True
    )
    assert not path.exists()


def test_manual_disable_invalidates_an_in_flight_lease(tmp_path):
    store = new_store(tmp_path / "consent.json")
    enabled = store.enable(binding(), expected_generation=0)
    lease = store.lease(binding())
    assert lease is not None

    disabled = store.disable(binding(), expected_generation=enabled.generation)

    assert disabled.enabled is False
    assert (
        store.accepts(
            lease,
            current_credential_revision=1,
            current_credential_generation=1,
            current_credential_session_generation=None,
        )
        is False
    )


def test_current_credential_and_consent_accept_an_in_flight_lease(tmp_path):
    store = new_store(tmp_path / "consent.json")
    store.enable(binding(), expected_generation=0)
    lease = store.lease(binding())
    assert lease is not None

    assert (
        store.accepts(
            lease,
            current_credential_revision=1,
            current_credential_generation=1,
            current_credential_session_generation=None,
        )
        is True
    )


@pytest.mark.parametrize(
    "change",
    [_CredentialStoreView.rotate, _CredentialStoreView.disconnect],
    ids=["rotation", "disconnect"],
)
def test_another_credential_store_invalidates_an_old_lease(
    tmp_path,
    change: Callable[[_CredentialStoreView], None],
):
    metadata = {"revision": 1, "generation": 1, "session_generation": None}
    reader = _CredentialStoreView(metadata)
    writer = _CredentialStoreView(metadata)
    store = new_store(tmp_path / "consent.json")
    store.enable(binding(), expected_generation=0)
    lease = store.lease(binding())
    assert lease is not None

    change(writer)
    revision, generation, session_generation = reader.current()

    assert (
        store.accepts(
            lease,
            current_credential_revision=revision,
            current_credential_generation=generation,
            current_credential_session_generation=session_generation,
        )
        is False
    )


def test_process_memory_is_shared_by_all_consent_services(tmp_path):
    path = tmp_path / "consent.json"
    memory_backend = MemoryConsentBackend()
    management = new_store(
        path,
        secure_backend_available=False,
        memory_backend=memory_backend,
    )
    trading = new_store(
        path,
        secure_backend_available=False,
        memory_backend=memory_backend,
    )
    enabled = management.enable(binding(), expected_generation=0)
    lease = trading.lease(binding())
    assert trading.status(binding()) == enabled
    assert lease is not None

    management.revoke_environment("india_prod")

    assert (
        trading.accepts(
            lease,
            current_credential_revision=1,
            current_credential_generation=1,
            current_credential_session_generation=None,
        )
        is False
    )


def test_stale_browser_cannot_restore_manually_disabled_consent(tmp_path):
    store = new_store(tmp_path / "consent.json")
    first = store.enable(binding(), expected_generation=0)
    store.disable(binding(), expected_generation=first.generation)

    with pytest.raises(
        StaleConsentError, match="expected consent generation 1, found 2"
    ):
        store.enable(binding(), expected_generation=first.generation)


def test_revoke_environment_invalidates_all_bound_clients(tmp_path):
    store = new_store(tmp_path / "consent.json")
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
    first = new_store(path)
    second = new_store(path)
    assert first.status(binding()).generation == 0
    assert second.status(binding()).generation == 0

    first.enable(binding(), expected_generation=0)

    with pytest.raises(
        StaleConsentError, match="expected consent generation 0, found 1"
    ):
        second.disable(binding(), expected_generation=0)


def test_consent_write_times_out_under_contention_and_succeeds_after_release(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "nested" / "consent.json"
    store = new_store(path)
    monkeypatch.setattr(auth_backend, "_LOCK_TIMEOUT_SECONDS", 0)

    with auth_backend.file_lock(path):
        with pytest.raises(ConsentStorageError, match="timed out") as raised:
            store.enable(binding(), expected_generation=0)
        assert isinstance(raised.value.__cause__, auth_backend.MetadataError)
        assert not path.exists()

    assert store.enable(binding(), expected_generation=0).enabled is True


@pytest.mark.parametrize("phase", ["acquire", "body", "release"])
def test_consent_translates_only_lock_acquisition_errors(
    tmp_path, monkeypatch, phase: str
) -> None:
    store = new_store(tmp_path / "consent.json")
    failure = auth_backend.MetadataError(phase)
    monkeypatch.setattr(auth_backend, "_LOCK_TIMEOUT_SECONDS", 0)

    @contextmanager
    def failing_lock(path: Path) -> Iterator[tuple[int, Path]]:
        if phase == "acquire":
            raise failure
        with auth_backend.file_lock(path) as locked:
            yield locked
        if phase == "release":
            raise failure

    def check_current() -> bool:
        if phase == "body":
            raise failure
        return True

    monkeypatch.setattr(consent_mod, "file_lock", failing_lock)
    expected = ConsentStorageError if phase == "acquire" else auth_backend.MetadataError
    with pytest.raises(expected) as raised:
        store.enable(binding(), expected_generation=0, check_current=check_current)
    if phase == "acquire":
        assert raised.value.__cause__ is failure
    else:
        assert raised.value is failure
    with auth_backend.file_lock(tmp_path / "consent.json"):
        pass


def test_corrupt_metadata_fails_closed_and_is_not_replaced(tmp_path):
    path = tmp_path / "consent.json"
    path.write_text("not json")
    store = new_store(path)

    with pytest.raises(ConsentStorageError, match="not valid JSON"):
        store.status(binding())
    with pytest.raises(ConsentStorageError, match="not valid JSON"):
        store.enable(binding(), expected_generation=0)
    assert path.read_text() == "not json"


def test_record_binding_tamper_is_rejected(tmp_path):
    path = tmp_path / "consent.json"
    store = new_store(path)
    store.enable(binding(), expected_generation=0)
    payload = json.loads(path.read_text())
    record = next(iter(payload["records"].values()))
    record["client_name"] = "another client"
    path.write_text(json.dumps(payload))

    with pytest.raises(ConsentStorageError, match="does not match its binding"):
        store.status(binding())


def test_existing_persistent_record_key_and_shape_remain_readable(tmp_path):
    path = tmp_path / "consent.json"
    store = new_store(path)
    enabled = store.enable(binding(), expected_generation=0)
    payload = json.loads(path.read_text())
    expected_key = hashlib.sha256(
        json.dumps(
            ["Claude Desktop", "india_prod", 1, 1], separators=(",", ":")
        ).encode()
    ).hexdigest()
    record = payload["records"][expected_key]
    record.pop("credential_session_generation")
    record.pop("environment_generation")
    path.write_text(json.dumps(payload))

    assert new_store(path).status(binding()) == enabled


def test_invalid_binding_values_are_rejected():
    with pytest.raises(ValueError, match="environment"):
        binding(environment="other")
    with pytest.raises(ValueError, match="credential_revision"):
        binding(revision=0)
    with pytest.raises(ValueError, match="both be set or absent"):
        binding(revision=None)
    with pytest.raises(ValueError, match="requires a credential revision"):
        binding(revision=None, generation=None)
    with pytest.raises(ValueError, match="session_generation"):
        binding(session_generation=1)
    with pytest.raises(ValueError, match="positive integer"):
        binding(revision=None, generation=None, session_generation=0)
    with pytest.raises(ValueError, match="environment_generation"):
        binding(environment_generation=-1)


def test_devnet_process_binding_is_session_only(tmp_path):
    path = tmp_path / "consent.json"
    store = new_store(path)
    devnet = binding(
        environment="india_devnet",
        revision=None,
        generation=None,
        session_generation=1,
    )

    enabled = store.enable(devnet, expected_generation=0)

    assert enabled.enabled is True
    assert enabled.persistent is False
    assert store.backend(devnet).value == "memory"
    assert path.exists() is False
    assert binding(environment="india_devnet").persistent is False


def test_unpaired_surrogate_in_exact_client_name_cannot_crash_authorization(
    tmp_path,
) -> None:
    store = new_store(tmp_path / "consent.json")
    malformed = binding("client-\ud800")

    enabled = store.enable(malformed, expected_generation=0)

    assert enabled.enabled is True
    assert store.status(malformed) == enabled


def test_directory_sync_failure_keeps_published_revocation(
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    path = tmp_path / "consent.json"
    store = new_store(path)
    approved = store.enable(binding(), expected_generation=0)

    def fail_sync(path: Path) -> None:
        raise OSError("directory sync failed")

    monkeypatch.setattr(consent_mod, "_sync_directory", fail_sync)
    disabled = store.disable(
        binding(),
        expected_generation=approved.generation,
    )

    assert disabled.enabled is False
    assert new_store(path).status(binding()).enabled is False
    assert "directory sync failed: OSError" in caplog.text
