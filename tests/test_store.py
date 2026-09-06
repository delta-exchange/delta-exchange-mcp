import os
import stat
import threading

import pytest

from delta_exchange_mcp import config as config_mod
from delta_exchange_mcp import store


@pytest.fixture(autouse=True)
def no_ambient_settings(monkeypatch):
    """Start every test from an environment that supplies nothing.

    The suite inherits the developer's shell, and a stray DELTA_API_KEY there would
    silently win over the file these tests are about.
    """
    for name in (
        "DELTA_MCP_ENV",
        "DELTA_MCP_MODE",
        "DELTA_MCP_DEBUG",
        "DELTA_API_KEY",
        "DELTA_API_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)


def write_store(text):
    path = store.path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_template_is_created_on_first_load_owner_only():
    cfg = config_mod.load()
    path = store.path()
    assert cfg.config_file == path
    assert path.exists()
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # The instructions someone needs are in the file, because the moment they open it
    # is the moment they are asking these exact questions.
    body = path.read_text()
    assert "Read Data" in body
    assert "india_testnet" in body
    assert "DELTA_API_KEY=" in body


def test_existing_file_is_never_overwritten():
    written = write_store("DELTA_API_KEY=mine\nDELTA_API_SECRET=also-mine\n")
    config_mod.load()
    assert written.read_text() == "DELTA_API_KEY=mine\nDELTA_API_SECRET=also-mine\n"


def test_unwritable_location_does_not_stop_the_server(tmp_path, monkeypatch):
    """Market data needs no credentials, so an unusable settings file is not fatal."""
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory")
    monkeypatch.setenv("DELTA_MCP_CONFIG_FILE", str(blocker / "config.env"))
    cfg = config_mod.load()
    assert cfg.config_file is None
    assert cfg.env == "india_prod"
    assert cfg.has_credentials is False


def test_store_supplies_settings_when_the_environment_is_silent():
    write_store("DELTA_API_KEY=k\nDELTA_API_SECRET=s\nDELTA_MCP_ENV=india_testnet\n")
    cfg = config_mod.load()
    assert cfg.env == "india_testnet"
    assert cfg.base_url == config_mod.INDIA_TESTNET_REST
    assert (cfg.api_key, cfg.api_secret) == ("k", "s")


def test_one_load_uses_one_complete_store_snapshot(monkeypatch):
    """An atomic replacement must not be split across one Config instance."""
    before = {
        "DELTA_MCP_ENV": "india_testnet",
        "DELTA_API_KEY": "before-key",
        "DELTA_API_SECRET": "before-secret",
        "DELTA_MCP_DEBUG": "0",
    }
    after = {
        "DELTA_MCP_ENV": "india_prod",
        "DELTA_API_KEY": "after-key",
        "DELTA_API_SECRET": "after-secret",
        "DELTA_MCP_DEBUG": "1",
    }
    reads = 0

    def changing_store():
        nonlocal reads
        reads += 1
        return before if reads == 1 else after

    monkeypatch.setattr(store, "read", changing_store)

    cfg = config_mod.load()

    assert reads == 1
    assert (cfg.env, cfg.api_key, cfg.api_secret, cfg.debug) == (
        "india_testnet",
        "before-key",
        "before-secret",
        False,
    )


def test_client_entitlement_uses_the_same_snapshot_as_its_identity(monkeypatch):
    """A concurrent replace cannot pair an old account with a new trade grant."""
    client = "Claude Desktop"
    scoped = config_mod.mode_key(client)
    before = {
        "DELTA_MCP_ENV": "india_testnet",
        "DELTA_API_KEY": "before-key",
        "DELTA_API_SECRET": "before-secret",
        scoped: "read",
    }
    after = {
        "DELTA_MCP_ENV": "india_prod",
        "DELTA_API_KEY": "after-key",
        "DELTA_API_SECRET": "after-secret",
        scoped: "trade",
    }
    reads = 0

    def changing_store():
        nonlocal reads
        reads += 1
        return before if reads == 1 else after

    monkeypatch.setattr(store, "read", changing_store)

    cfg = config_mod.load_for_client(client)

    assert reads == 1
    assert (cfg.env, cfg.api_key, cfg.api_secret, cfg.mode) == (
        "india_testnet",
        "before-key",
        "before-secret",
        "read",
    )


def test_client_environment_beats_the_store(monkeypatch):
    write_store("DELTA_API_KEY=from-file\nDELTA_API_SECRET=from-file\n")
    monkeypatch.setenv("DELTA_API_KEY", "from-client")
    monkeypatch.setenv("DELTA_API_SECRET", "from-client")
    cfg = config_mod.load()
    assert (cfg.api_key, cfg.api_secret) == ("from-client", "from-client")


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_client_values_fall_through_to_the_store(monkeypatch, blank):
    """A bundle substitutes every variable it declares, filled in or not.

    Leaving the API key field empty in the Claude Desktop form puts "" in the
    environment. Treating that as an answer would mean the shared file could never
    reach a bundle user at all.
    """
    write_store("DELTA_API_KEY=k\nDELTA_API_SECRET=s\nDELTA_MCP_ENV=india_testnet\n")
    monkeypatch.setenv("DELTA_API_KEY", blank)
    monkeypatch.setenv("DELTA_API_SECRET", blank)
    monkeypatch.setenv("DELTA_MCP_ENV", blank)
    cfg = config_mod.load()
    assert cfg.env == "india_testnet"
    assert (cfg.api_key, cfg.api_secret) == ("k", "s")


def test_a_stray_key_never_pairs_with_the_stores_secret(monkeypatch):
    """The key and secret always come from the same source.

    Resolving them independently would pair a leftover key from someone's shell with
    a secret from the file. That pair was never issued together, so every signed call
    would fail while the server advertised the account surface as working. Taking both
    from wherever either appears turns it into the partial-credentials warning instead.
    """
    write_store("DELTA_API_KEY=file-key\nDELTA_API_SECRET=file-secret\n")
    monkeypatch.setenv("DELTA_API_KEY", "leftover-shell-key")
    cfg = config_mod.load()
    assert cfg.api_key == "leftover-shell-key"
    assert cfg.api_secret is None
    assert cfg.partial_credentials is True
    assert cfg.has_credentials is False


def test_trade_mode_is_never_read_from_the_store():
    """The one setting the shared file may not supply.

    Everything else there is per-machine convenience. This one places real orders, so
    it stays scoped to the single client whose config was deliberately edited.
    """
    write_store("DELTA_API_KEY=k\nDELTA_API_SECRET=s\nDELTA_MCP_MODE=trade\n")
    cfg = config_mod.load()
    assert cfg.mode == "read"
    assert cfg.has_credentials is True


def test_trade_mode_still_works_from_the_client(monkeypatch):
    write_store("DELTA_API_KEY=k\nDELTA_API_SECRET=s\n")
    monkeypatch.setenv("DELTA_MCP_MODE", "trade")
    assert config_mod.load().mode == "trade"


def test_debug_and_path_overrides_come_from_the_store(tmp_path):
    write_store(
        "DELTA_MCP_DEBUG=1\n"
        f"DELTA_MCP_AUDIT_FILE={tmp_path / 'audit.log'}\n"
        f"DELTA_MCP_DEBUG_FILE={tmp_path / 'debug.log'}\n"
    )
    assert config_mod.load().debug is True
    assert config_mod.setting("DELTA_MCP_AUDIT_FILE") == str(tmp_path / "audit.log")
    assert config_mod.setting("DELTA_MCP_DEBUG_FILE") == str(tmp_path / "debug.log")


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ('DELTA_API_KEY="quoted"', "quoted"),
        ("export DELTA_API_KEY=exported", "exported"),
        ("DELTA_API_KEY=plain  # trailing note", "plain"),
        ("DELTA_API_KEY = spaced", "spaced"),
        ("DELTA_API_KEY=windows\r", "windows"),
    ],
)
def test_a_hand_edited_file_survives_the_usual_mistakes(line, expected):
    """Each of these silently corrupts a credential under a naive KEY=value split.

    Three of the five then fail as a signature error indistinguishable from a wrong
    key, which is the worst outcome for the people this file exists to help.
    """
    write_store(f"{line}\nDELTA_API_SECRET=s\n")
    assert config_mod.load().api_key == expected


def test_blank_entries_in_the_template_are_not_credentials():
    """The shipped template has empty values; they must read as absent."""
    config_mod.load()  # writes the template
    cfg = config_mod.load()  # reads it back
    assert cfg.has_credentials is False
    assert cfg.partial_credentials is False
    assert cfg.env == "india_prod"


@pytest.mark.skipif(os.name == "nt", reason="Windows does not expose POSIX mode bits")
def test_world_readable_file_is_reported_not_fatal():
    path = write_store("DELTA_API_KEY=k\nDELTA_API_SECRET=s\n")
    os.chmod(path, 0o644)
    warning = store.insecure_permissions()
    assert warning is not None
    assert "chmod 600" in warning
    assert config_mod.load().has_credentials is True

    os.chmod(path, 0o600)
    assert store.insecure_permissions() is None


def test_missing_file_reports_no_permission_warning(tmp_path, monkeypatch):
    monkeypatch.setenv("DELTA_MCP_CONFIG_FILE", str(tmp_path / "absent.env"))
    assert store.insecure_permissions() is None


def test_write_creates_the_file_it_writes_into():
    """Every front-end writes through here, and the first one to run finds no file."""
    assert store.write({"DELTA_API_KEY": "k", "DELTA_API_SECRET": "s"}) is None
    assert config_mod.load().has_credentials is True


def test_write_leaves_settings_it_was_not_given_alone():
    """Two settings written at different moments must not erase each other."""
    write_store("DELTA_MCP_ENV=india_testnet\nDELTA_MCP_DEBUG=1\n")
    assert store.write({"DELTA_API_KEY": "k", "DELTA_API_SECRET": "s"}) is None

    cfg = config_mod.load()
    assert (cfg.env, cfg.debug) == ("india_testnet", True)
    assert cfg.has_credentials is True


def test_concurrent_writers_preserve_another_clients_trade_deescalation(monkeypatch):
    """A disjoint save cannot republish a stale trade grant from its staging copy."""
    first_mode = config_mod.mode_key("first-client")
    second_mode = config_mod.mode_key("second-client")
    write_store(f"{first_mode}=trade\n{second_mode}=read\n")

    first_inside = threading.Event()
    release_first = threading.Event()
    second_inside = threading.Event()
    real_set_key = store.set_key

    def interleaved_set_key(target, key, value, *args, **kwargs):
        if threading.current_thread().name == "first-writer" and key == first_mode:
            first_inside.set()
            assert release_first.wait(2)
        if threading.current_thread().name == "second-writer":
            second_inside.set()
        return real_set_key(target, key, value, *args, **kwargs)

    monkeypatch.setattr(store, "set_key", interleaved_set_key)
    outcomes: dict[str, str | None] = {}

    first = threading.Thread(
        target=lambda: outcomes.setdefault("first", store.write({first_mode: "read"})),
        name="first-writer",
    )
    second = threading.Thread(
        target=lambda: outcomes.setdefault("second", store.write({second_mode: "trade"})),
        name="second-writer",
    )

    first.start()
    assert first_inside.wait(2)
    second.start()
    assert not second_inside.wait(0.1)
    release_first.set()
    first.join(2)
    second.join(2)

    assert not first.is_alive() and not second.is_alive()
    assert outcomes == {"first": None, "second": None}
    assert store.read()[first_mode] == "read"
    assert store.read()[second_mode] == "trade"


@pytest.mark.parametrize(
    "value",
    ["has spaces", "quotes'and\"more", "hash#inside", "newline\ninjected", "DELTA_MCP_MODE=trade"],
)
def test_a_written_value_survives_being_read_back(value):
    """A value carrying a newline or an `=` must come back as one string.

    The last case would otherwise define a second setting and arm trading.
    """
    store.write({"DELTA_API_KEY": value, "DELTA_API_SECRET": "s"})
    cfg = config_mod.load()
    assert cfg.api_key == value
    assert cfg.mode == "read"


def test_a_failed_write_leaves_the_previous_credential_untouched(monkeypatch):
    """A new key beside the old secret is worse than no write at all.

    That pair was never issued together, so it still reads as complete, still registers
    the account tools, and fails every signed request.
    """
    write_store("DELTA_API_KEY=old-key\nDELTA_API_SECRET=old-secret\n")
    real = store.set_key

    def fail_on_the_secret(target, key, value, *args, **kwargs):
        if key == "DELTA_API_SECRET":
            raise OSError("no space left on device")
        return real(target, key, value, *args, **kwargs)

    monkeypatch.setattr(store, "set_key", fail_on_the_secret)
    assert store.write({"DELTA_API_KEY": "new-key", "DELTA_API_SECRET": "new-secret"}) is not None

    cfg = config_mod.load()
    assert (cfg.api_key, cfg.api_secret) == ("old-key", "old-secret")


def test_a_failed_write_leaves_nothing_behind_beside_the_config(monkeypatch):
    """The staging copy holds a secret, so a failure must not strand it in the directory."""
    path = write_store("DELTA_API_KEY=old\n")

    def boom(*args, **kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(store, "set_key", boom)
    assert store.write({"DELTA_API_KEY": "new"}) is not None
    assert {entry.name for entry in path.parent.iterdir()} == {
        path.name,
        f".{path.name}.lock",
    }


@pytest.mark.skipif(os.name == "nt", reason="Windows does not expose POSIX mode bits")
def test_write_never_publishes_a_secret_into_a_file_others_can_read():
    """Saving is the one moment a new secret enters this file.

    Publishing it into a group- or world-readable file would hand it to every other
    account on the machine, and silently — the permission warning only runs at startup.
    """
    path = write_store("DELTA_API_KEY=old\nDELTA_API_SECRET=old\n")
    os.chmod(path, 0o644)
    assert store.write({"DELTA_API_KEY": "fresh", "DELTA_API_SECRET": "fresh"}) is None

    assert stat.S_IMODE(path.stat().st_mode) & (stat.S_IRGRP | stat.S_IROTH) == 0
    assert store.insecure_permissions() is None


@pytest.mark.skipif(os.name == "nt", reason="Windows does not expose POSIX mode bits")
def test_write_keeps_the_owner_bits_the_file_already_had():
    """Masking group and other, not forcing 0600 — an owner who chose 0400 keeps it."""
    path = write_store("DELTA_API_KEY=old\n")
    os.chmod(path, 0o400)
    store.write({"DELTA_API_KEY": "new"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o400


@pytest.mark.skipif(os.name == "nt", reason="chmod does not make a Windows directory read-only")
def test_write_reports_a_read_only_directory_rather_than_raising():
    """A caller may be a tool answering a form, where an exception is not actionable."""
    path = write_store("DELTA_API_KEY=\n")
    os.chmod(path.parent, 0o500)
    try:
        problem = store.write({"DELTA_API_KEY": "k"})
    finally:
        os.chmod(path.parent, 0o700)
    assert problem is not None
    assert "DELTA_API_KEY" in problem

def test_the_template_points_at_the_dashboard_the_rest_of_the_package_uses():
    """store cannot import config — config imports store — so this is the only check."""
    assert config_mod.DASHBOARDS["india_prod"] in store.TEMPLATE

def test_write_reports_a_location_it_cannot_use(tmp_path, monkeypatch):
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory")
    monkeypatch.setenv("DELTA_MCP_CONFIG_FILE", str(blocker / "config.env"))
    assert store.write({"DELTA_API_KEY": "k"}) is not None
