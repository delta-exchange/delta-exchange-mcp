"""Report-writing contracts for the manual evaluation runner."""

import argparse
import asyncio
import json
import os
import stat
import sys
from pathlib import Path
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
def test_failed_report_replace_removes_private_temp_file(tmp_path, monkeypatch) -> None:
    path = tmp_path / "report.json"

    def fail_replace(source, destination) -> None:
        del source, destination
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

    def replace_and_recreate(source, destination) -> None:
        real_replace(source, destination)
        recreated_path = Path(source)
        recreated_path.write_text("unrelated concurrent file")
        recreated_paths.append(recreated_path)

    monkeypatch.setattr(run_mod.os, "replace", replace_and_recreate)

    run_mod._write_private_report(path, "private account data")

    assert path.read_text() == "private account data"
    assert len(recreated_paths) == 1
    assert recreated_paths[0].read_text() == "unrelated concurrent file"


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
