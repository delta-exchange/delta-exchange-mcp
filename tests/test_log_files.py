"""Log privacy at fallback allocation and every later append."""

import logging
import os
import stat
import tempfile
from pathlib import Path

import pytest

from delta_exchange_mcp import debug_log
from delta_exchange_mcp.config import INDIA_TESTNET_REST, Config


@pytest.fixture(autouse=True)
def clear_logs():
    yield
    debug_log.shutdown()


def test_fallback_ignores_preexisting_shared_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    shared = tmp_path / "delta-exchange-mcp"
    shared.mkdir(mode=0o777)
    planted = shared / "debug.log"
    planted.write_text("attacker content\n")
    planted.chmod(0o666)
    monkeypatch.setenv("DELTA_MCP_DEBUG_FILE", str(blocked / planted.name))
    cfg = Config(env="india_testnet", base_url=INDIA_TESTNET_REST, debug=True)

    path = debug_log.configure(cfg)
    logging.getLogger("delta_exchange_mcp").info("private_order 42")
    assert debug_log.configure(cfg) == path

    assert path is not None
    assert path.parent != shared
    assert "private_order" in path.read_text()
    assert planted.read_text() == "attacker content\n"
    if os.name == "posix":
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_private_existing_log_appends_and_relative_override_works(tmp_path, monkeypatch):
    from delta_exchange_mcp.log_files import open_log

    monkeypatch.chdir(tmp_path)
    path = Path("logs/debug.log")
    with open_log(path) as stream:
        stream.write("first\n")
    with open_log(path) as stream:
        stream.write("second\n")
    assert path.read_text() == "first\nsecond\n"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
@pytest.mark.parametrize("attack", ["shared_file", "shared_parent", "hardlink", "fifo"])
def test_rejects_unsafe_existing_log_without_writing(tmp_path, attack):
    from delta_exchange_mcp.log_files import open_log

    parent = tmp_path / "logs"
    parent.mkdir()
    path = parent / "debug.log"
    if attack == "fifo":
        os.mkfifo(path, 0o600)
    else:
        path.write_text("untouched")
        path.chmod(0o600)
        if attack == "shared_file":
            path.chmod(0o644)
        elif attack == "shared_parent":
            parent.chmod(0o777)
        else:
            os.link(path, parent / "alias.log")
    with pytest.raises(OSError):
        with open_log(path) as stream:
            stream.write("private data")
    if attack != "fifo":
        assert path.read_text() == "untouched"


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership")
def test_rejects_foreign_owner_before_any_data(tmp_path, monkeypatch):
    from delta_exchange_mcp import log_files

    path = tmp_path / "debug.log"
    path.touch(mode=0o600)
    original = os.fstat

    def foreign_owner(fd):
        values = list(original(fd))
        values[4] = os.geteuid() + 1
        return os.stat_result(values)

    monkeypatch.setattr(os, "fstat", foreign_owner)
    with pytest.raises(PermissionError):
        log_files.open_log(path)
    assert path.read_bytes() == b""


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership")
def test_directory_alias_checks_intermediate_symlink_owners(tmp_path, monkeypatch):
    from delta_exchange_mcp.log_files import open_log

    cache = tmp_path / "private"
    deep = cache / "deep"
    deep.mkdir(parents=True)
    log = deep / "debug.log"
    log.write_text("untouched")
    log.chmod(0o600)
    (deep / "jump").symlink_to(cache, target_is_directory=True)
    outside = tmp_path / "back"
    outside.symlink_to(deep, target_is_directory=True)
    alias = deep / "alias"
    alias.symlink_to("jump/../back", target_is_directory=True)
    original = Path.lstat

    def foreign_intermediate(path):
        info = original(path)
        if path == outside:
            values = list(info)
            values[4] = os.geteuid() + 1
            return os.stat_result(values)
        return info

    monkeypatch.setattr(Path, "lstat", foreign_intermediate)
    with pytest.raises(PermissionError, match="owned by another user"):
        with open_log(alias / "debug.log") as stream:
            stream.write("private data")
    assert log.read_text() == "untouched"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_permission_failure_closes_descriptor_and_disables_logging(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DELTA_MCP_DEBUG_FILE", str(tmp_path / "debug.log"))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    opened = []
    original = os.open

    def remember_open(*args, **kwargs):
        fd = original(*args, **kwargs)
        opened.append(fd)
        return fd

    def denied(fd, mode):
        raise PermissionError("permission update failed")

    monkeypatch.setattr(os, "open", remember_open)
    monkeypatch.setattr(os, "fchmod", denied)
    cfg = Config(env="india_testnet", base_url=INDIA_TESTNET_REST, debug=True)
    assert debug_log.configure(cfg) is None
    assert "debug logging disabled" in capsys.readouterr().err
    assert opened
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)
