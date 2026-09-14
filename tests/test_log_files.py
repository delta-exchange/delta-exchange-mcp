"""Log privacy at fallback allocation and every later append."""

import logging
import os
import stat
import tempfile
from pathlib import Path

import pytest

from delta_exchange_mcp import audit_log, debug_log
from delta_exchange_mcp.config import INDIA_TESTNET_REST, Config


@pytest.fixture(autouse=True)
def clear_logs(monkeypatch):
    monkeypatch.setattr(audit_log, "_INSTANCE", None)
    yield
    debug_log.shutdown()


@pytest.mark.parametrize("kind", ["audit", "debug"])
def test_fallback_ignores_preexisting_shared_directory(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    shared = tmp_path / "delta-exchange-mcp"
    shared.mkdir(mode=0o777)
    planted = shared / f"{kind}.log"
    planted.write_text("attacker content\n")
    planted.chmod(0o666)
    monkeypatch.setenv(f"DELTA_MCP_{kind.upper()}_FILE", str(blocked / planted.name))
    cfg = Config(env="india_testnet", base_url=INDIA_TESTNET_REST, mode="trade", debug=True)

    if kind == "audit":
        audit = audit_log.configure(cfg)
        assert audit is not None
        audit.record("place_order", {"private_order": 42})
        path = audit.path
        assert audit_log.configure(cfg) is audit
    else:
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


def test_audit_rejects_path_replacement_before_next_record(tmp_path, capsys):
    path = tmp_path / "audit.log"
    audit = audit_log.AuditLog(path, "india_testnet")
    audit.record("place_order", {"id": 1})
    saved = path.rename(tmp_path / "original.log")
    target = tmp_path / "target.log"
    target.write_text("untouched")
    target.chmod(0o600)
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    audit.record("place_order", {"id": 2})
    assert target.read_text() == "untouched"
    assert '"id": 1' in saved.read_text()
    assert "audit write failed" in capsys.readouterr().err


def test_private_existing_log_appends_and_relative_override_works(tmp_path, monkeypatch):
    from delta_exchange_mcp.log_files import open_log

    monkeypatch.chdir(tmp_path)
    path = Path("logs/audit.log")
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
    path = parent / "audit.log"
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

    path = tmp_path / "audit.log"
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
