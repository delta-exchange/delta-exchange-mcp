"""Report-writing contracts for the manual evaluation runner."""

import argparse
import json
import os
import stat

import pytest

from evals.agent import ToolCall, TurnRecord
from evals.run import write_report
from evals.scoring import CaseResult


def _args(path) -> argparse.Namespace:
    return argparse.Namespace(
        json_path=str(path),
        model="agent-model",
        no_judge=True,
        judge_model="judge-model",
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
        write_report([_result()], _args(path))
    finally:
        os.umask(previous)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    payload = json.loads(path.read_text())
    result = json.loads(payload["results"][0]["turns"][0]["calls"][0]["result"])
    assert result == {"balance": "123.45", "account_id": 99}


@pytest.mark.skipif(os.name == "nt", reason="Windows has no POSIX owner-only mode")
def test_existing_permissive_report_is_tightened_before_write(tmp_path) -> None:
    path = tmp_path / "report.json"
    path.write_text("old account data")
    path.chmod(0o644)

    write_report([_result()], _args(path))

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    contents = path.read_text()
    assert "old account data" not in contents
    payload = json.loads(contents)
    result = json.loads(payload["results"][0]["turns"][0]["calls"][0]["result"])
    assert result == {"balance": "123.45", "account_id": 99}
