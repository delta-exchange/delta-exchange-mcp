import os
import stat

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
    # New files keep only non-secret settings. The browser owns credential setup.
    body = path.read_text()
    assert "browser" in body
    assert "DELTA_API_KEY" not in body
    assert "DELTA_API_SECRET" not in body


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


def test_store_supplies_environment_but_not_legacy_credentials():
    write_store("DELTA_API_KEY=k\nDELTA_API_SECRET=s\nDELTA_MCP_ENV=india_testnet\n")
    cfg = config_mod.load()
    assert cfg.env == "india_testnet"
    assert cfg.base_url == config_mod.INDIA_TESTNET_REST
    assert (cfg.api_key, cfg.api_secret) == (None, None)


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
        None,
        None,
        False,
    )


def test_client_environment_beats_the_store(monkeypatch):
    write_store("DELTA_API_KEY=from-file\nDELTA_API_SECRET=from-file\n")
    monkeypatch.setenv("DELTA_API_KEY", "from-client")
    monkeypatch.setenv("DELTA_API_SECRET", "from-client")
    cfg = config_mod.load()
    assert (cfg.api_key, cfg.api_secret) == ("from-client", "from-client")


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_process_credentials_do_not_restore_file_credentials(monkeypatch, blank):
    """Blank process values do not make a legacy plaintext pair active."""
    write_store("DELTA_API_KEY=k\nDELTA_API_SECRET=s\nDELTA_MCP_ENV=india_testnet\n")
    monkeypatch.setenv("DELTA_API_KEY", blank)
    monkeypatch.setenv("DELTA_API_SECRET", blank)
    monkeypatch.setenv("DELTA_MCP_ENV", blank)
    cfg = config_mod.load()
    assert cfg.env == "india_testnet"
    assert (cfg.api_key, cfg.api_secret) == (None, None)


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
    assert cfg.has_credentials is False


def test_trade_mode_is_also_ignored_from_the_client(monkeypatch):
    write_store("DELTA_API_KEY=k\nDELTA_API_SECRET=s\n")
    monkeypatch.setenv("DELTA_MCP_MODE", "trade")
    assert config_mod.load().mode == "read"


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
def test_a_hand_edited_legacy_credential_never_becomes_runtime_authority(
    line, expected
):
    """The settings parser can inspect legacy values, but Config does not use them."""
    write_store(f"{line}\nDELTA_API_SECRET=s\n")
    assert store.read()["DELTA_API_KEY"] == expected
    assert config_mod.load().api_key is None


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
    assert config_mod.load().has_credentials is False

    os.chmod(path, 0o600)
    assert store.insecure_permissions() is None


def test_missing_file_reports_no_permission_warning(tmp_path, monkeypatch):
    monkeypatch.setenv("DELTA_MCP_CONFIG_FILE", str(tmp_path / "absent.env"))
    assert store.insecure_permissions() is None


def test_write_creates_the_file_it_writes_into():
    assert store.write({"DELTA_MCP_DEBUG": "1"}) is None
    assert config_mod.load().debug is True


def test_write_leaves_settings_it_was_not_given_alone():
    """Two settings written at different moments must not erase each other."""
    write_store("DELTA_MCP_ENV=india_testnet\nDELTA_MCP_DEBUG=1\n")
    assert store.write({"DELTA_MCP_ENV": "india_prod"}) is None

    cfg = config_mod.load()
    assert (cfg.env, cfg.debug) == ("india_prod", True)


@pytest.mark.parametrize(
    "value",
    ["has spaces", "quotes'and\"more", "hash#inside", "newline\ninjected", "DELTA_MCP_MODE=trade"],
)
def test_a_non_secret_written_value_survives_being_read_back(value):
    """A value carrying a newline or an `=` must come back as one string.

    The last case would otherwise define a second setting and arm trading.
    """
    store.write({"CUSTOM_SETTING": value})
    assert store.read()["CUSTOM_SETTING"] == value


def test_a_failed_write_leaves_the_previous_settings_untouched(monkeypatch):
    write_store("SETTING_A=old-a\nSETTING_B=old-b\n")
    real = store.set_key

    def fail_on_second_setting(target, key, value, *args, **kwargs):
        if key == "SETTING_B":
            raise OSError("no space left on device")
        return real(target, key, value, *args, **kwargs)

    monkeypatch.setattr(store, "set_key", fail_on_second_setting)
    assert store.write({"SETTING_A": "new-a", "SETTING_B": "new-b"}) is not None

    assert store.read() == {"SETTING_A": "old-a", "SETTING_B": "old-b"}


def test_a_failed_write_leaves_nothing_behind_beside_the_config(monkeypatch):
    path = write_store("CUSTOM_SETTING=old\n")

    def boom(*args, **kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(store, "set_key", boom)
    assert store.write({"CUSTOM_SETTING": "new"}) is not None
    assert {entry.name for entry in path.parent.iterdir()} == {
        path.name,
        f".{path.name}.lock",
    }


@pytest.mark.skipif(os.name == "nt", reason="Windows does not expose POSIX mode bits")
def test_write_tightens_permissions_on_the_shared_settings_file():
    path = write_store("CUSTOM_SETTING=old\n")
    os.chmod(path, 0o644)
    assert store.write({"CUSTOM_SETTING": "fresh"}) is None

    assert stat.S_IMODE(path.stat().st_mode) & (stat.S_IRGRP | stat.S_IROTH) == 0
    assert store.insecure_permissions() is None


@pytest.mark.skipif(os.name == "nt", reason="Windows does not expose POSIX mode bits")
def test_write_keeps_the_owner_bits_the_file_already_had():
    """Masking group and other, not forcing 0600 — an owner who chose 0400 keeps it."""
    path = write_store("CUSTOM_SETTING=old\n")
    os.chmod(path, 0o400)
    store.write({"CUSTOM_SETTING": "new"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o400


@pytest.mark.skipif(os.name == "nt", reason="chmod does not make a Windows directory read-only")
def test_write_reports_a_read_only_directory_rather_than_raising():
    """A caller may be a tool answering a form, where an exception is not actionable."""
    path = write_store("CUSTOM_SETTING=\n")
    os.chmod(path.parent, 0o500)
    try:
        problem = store.write({"CUSTOM_SETTING": "value"})
    finally:
        os.chmod(path.parent, 0o700)
    assert problem is not None
    assert "write" in problem
    assert str(path) in problem


def test_the_template_points_at_the_dashboard_the_rest_of_the_package_uses():
    """Dashboard links belong to the browser, not the non-secret template."""
    assert config_mod.DASHBOARDS["india_prod"] not in store.TEMPLATE
    assert "API key" not in store.TEMPLATE


def test_write_reports_a_location_it_cannot_use(tmp_path, monkeypatch):
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory")
    monkeypatch.setenv("DELTA_MCP_CONFIG_FILE", str(blocker / "config.env"))
    assert store.write({"CUSTOM_SETTING": "value"}) is not None


def test_write_rejects_plaintext_credentials() -> None:
    problem = store.write({"DELTA_API_KEY": "key", "DELTA_API_SECRET": "secret"})
    assert problem == "API credentials must be managed through Manage Connection"
    assert not store.path().exists()
