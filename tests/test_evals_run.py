"""Report-writing contracts for the manual evaluation runner."""

import argparse
import asyncio
import json
import os
import stat
import sys
import threading
from datetime import datetime
from types import SimpleNamespace

import pytest

from evals.agent import ToolCall, TurnRecord
from evals import run as run_mod
from evals.scoring import CaseResult


def _args(path, *, no_report: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        json_path=str(path),
        model="agent-model",
        no_judge=True,
        judge_model="judge-model",
        no_report=no_report,
        list=False,
        mode=None,
        cases=None,
    )


def _result() -> CaseResult:
    return CaseResult(
        case_id="balances",
        mode="read",
        passed=True,
        turns=[
            TurnRecord(
                prompt="How much money do I have?",
                reply="Done.",
                calls=[
                    ToolCall(
                        name="get_wallet_balances",
                        args={},
                        result={"balance": "123.45", "account_id": 99},
                        is_error=False,
                    )
                ],
            )
        ],
    )


@pytest.mark.skipif(os.name == "nt", reason="Windows has no POSIX owner-only mode")
def test_new_report_is_owner_only_under_a_permissive_umask(tmp_path) -> None:
    path = tmp_path / "report.json"
    previous = os.umask(0)
    try:
        run_mod.write_report([_result()], _args(path))
    finally:
        os.umask(previous)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    payload = json.loads(path.read_text())
    result = json.loads(payload["results"][0]["turns"][0]["calls"][0]["result"])
    assert result == {"balance": "123.45", "account_id": 99}


@pytest.mark.skipif(os.name == "nt", reason="Windows has no POSIX owner-only mode")
def test_existing_permissive_report_is_replaced_before_write(tmp_path) -> None:
    path = tmp_path / "report.json"
    path.write_text("old account data")
    path.chmod(0o644)

    run_mod.write_report([_result()], _args(path))

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    contents = path.read_text()
    assert "old account data" not in contents
    payload = json.loads(contents)
    result = json.loads(payload["results"][0]["turns"][0]["calls"][0]["result"])
    assert result == {"balance": "123.45", "account_id": 99}


@pytest.mark.skipif(os.name == "nt", reason="Windows has no POSIX owner-only mode")
def test_existing_reader_cannot_observe_replacement_report(tmp_path) -> None:
    path = tmp_path / "report.json"
    path.write_text("old public report")
    path.chmod(0o644)

    with path.open() as existing_reader:
        run_mod.write_report([_result()], _args(path))
        existing_reader.seek(0)
        assert existing_reader.read() == "old public report"

    assert "old public report" not in path.read_text()


@pytest.mark.skipif(os.name == "nt", reason="Windows has no POSIX owner-only mode")
def test_default_reports_do_not_replace_another_run_from_the_same_instant(
    tmp_path, monkeypatch
) -> None:
    instant = datetime.fromisoformat("2026-09-01T02:30:00+00:00")

    class FixedDatetime:
        @classmethod
        def now(cls, timezone):
            del cls, timezone
            return instant

    tokens = iter(("first-report", "first-temp", "second-report", "second-temp"))
    monkeypatch.setattr(run_mod, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(run_mod, "datetime", FixedDatetime)
    monkeypatch.setattr(run_mod.secrets, "token_hex", lambda _: next(tokens))
    args = _args(tmp_path / "unused")
    args.json_path = None

    first = run_mod.write_report(
        [CaseResult(case_id="first", mode="read", passed=True)], args
    )
    second = run_mod.write_report(
        [CaseResult(case_id="second", mode="read", passed=True)], args
    )

    assert first != second
    assert json.loads(first.read_text())["results"][0]["id"] == "first"
    assert json.loads(second.read_text())["results"][0]["id"] == "second"


@pytest.mark.skipif(os.name == "nt", reason="Windows has no POSIX owner-only mode")
def test_failed_report_replace_removes_private_temp_file(tmp_path, monkeypatch) -> None:
    path = tmp_path / "report.json"

    def fail_replace(source, destination, **kwargs) -> None:
        del source, destination, kwargs
        raise OSError("replace failed")

    monkeypatch.setattr(run_mod.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        run_mod._write_private_report(path, "private account data")

    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="Windows has no POSIX owner-only mode")
def test_successful_report_replace_preserves_reused_temp_path(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "report.json"
    real_replace = run_mod.os.replace
    recreated_paths = []

    def replace_and_recreate(source, destination, **kwargs) -> None:
        real_replace(source, destination, **kwargs)
        recreated_path = tmp_path / source
        recreated_fd = os.open(
            source,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=kwargs["src_dir_fd"],
        )
        with os.fdopen(recreated_fd, "w", encoding="utf-8") as recreated_file:
            recreated_file.write("unrelated concurrent file")
        recreated_paths.append(recreated_path)

    monkeypatch.setattr(run_mod.os, "replace", replace_and_recreate)

    run_mod._write_private_report(path, "private account data")

    assert path.read_text() == "private account data"
    assert len(recreated_paths) == 1
    assert recreated_paths[0].read_text() == "unrelated concurrent file"


@pytest.mark.skipif(os.name == "nt", reason="Windows has no POSIX owner-only mode")
def test_parent_symlink_retarget_cannot_redirect_report(tmp_path, monkeypatch) -> None:
    original_parent = tmp_path / "original"
    redirected_parent = tmp_path / "redirected"
    original_parent.mkdir()
    redirected_parent.mkdir()
    linked_parent = tmp_path / "reports"
    linked_parent.symlink_to(original_parent, target_is_directory=True)
    report = linked_parent / "report.json"
    temp_created = threading.Event()
    parent_retargeted = threading.Event()
    failures = []
    real_fchmod = run_mod.os.fchmod

    def pause_after_temp_creation(fd, mode) -> None:
        real_fchmod(fd, mode)
        temp_created.set()
        if not parent_retargeted.wait(timeout=5):
            raise TimeoutError("parent symlink was not retargeted")

    monkeypatch.setattr(run_mod.os, "fchmod", pause_after_temp_creation)

    def write_report() -> None:
        try:
            run_mod._write_private_report(report, "private account data")
        except BaseException as exc:
            failures.append(exc)

    writer = threading.Thread(target=write_report)
    writer.start()
    assert temp_created.wait(timeout=5)
    linked_parent.unlink()
    linked_parent.symlink_to(redirected_parent, target_is_directory=True)
    redirected_report = redirected_parent / report.name
    redirected_report.write_text("unrelated concurrent file")
    parent_retargeted.set()
    writer.join(timeout=5)

    assert not writer.is_alive()
    assert failures == []
    assert (original_parent / report.name).read_text() == "private account data"
    assert redirected_report.read_text() == "unrelated concurrent file"
    assert not list(original_parent.glob(f".{report.name}.*.tmp"))


@pytest.mark.parametrize("writer", ["write_report", "_write_private_report"])
@pytest.mark.parametrize("existing", [False, True])
def test_windows_report_writers_refuse_before_touching_target(
    tmp_path,
    monkeypatch,
    writer: str,
    existing: bool,
) -> None:
    path = tmp_path / "report.json"
    if existing:
        path.write_text("existing account data")
    monkeypatch.setattr(run_mod, "_private_reports_supported", lambda: False)

    with pytest.raises(RuntimeError, match="--no-report"):
        if writer == "write_report":
            run_mod.write_report([_result()], _args(path))
        else:
            run_mod._write_private_report(path, "replacement account data")

    if existing:
        assert path.read_text() == "existing account data"
    else:
        assert not path.exists()


def test_windows_report_guard_runs_before_live_setup(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(run_mod, "parse_args", lambda: _args(tmp_path / "report.json"))
    monkeypatch.setattr(run_mod, "_private_reports_supported", lambda: False)

    def unexpected_live_setup() -> str:
        raise AssertionError("live setup started before the report privacy check")

    monkeypatch.setattr(run_mod.agent_mod, "resolve_env", unexpected_live_setup)

    with pytest.raises(SystemExit, match="--no-report"):
        asyncio.run(run_mod.main())


def test_windows_no_report_run_remains_supported(tmp_path, monkeypatch) -> None:
    args = _args(tmp_path / "report.json", no_report=True)
    monkeypatch.setattr(run_mod, "parse_args", lambda: args)
    monkeypatch.setattr(run_mod, "_private_reports_supported", lambda: False)
    monkeypatch.setattr(run_mod.agent_mod, "resolve_env", lambda: "india_testnet")
    monkeypatch.setattr(run_mod, "select_cases", lambda _: [])
    monkeypatch.setattr(run_mod, "print_table", lambda _: None)
    monkeypatch.setattr(
        run_mod,
        "write_report",
        lambda *_: pytest.fail("--no-report must not write a report"),
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "placeholder")
    monkeypatch.setitem(
        sys.modules,
        "anthropic",
        SimpleNamespace(AsyncAnthropic=lambda: object()),
    )

    assert asyncio.run(run_mod.main()) == 0
