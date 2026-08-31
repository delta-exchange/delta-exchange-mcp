"""Manual eval run: score MCP tool-selection quality with a live Claude agent.

Usage:
    uv run --group evals python -m evals.run --list
    uv run --group evals python -m evals.run --case ticker_basic --no-judge
    uv run --group evals python -m evals.run                    # full run, costs tokens

Requires ANTHROPIC_API_KEY. Account-data cases can use a complete testnet
DELTA_API_KEY/DELTA_API_SECRET process pair. Trading calls are always dry runs and do not
need trading consent. Never run in CI.
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import anthropic

from evals import agent as agent_mod
from evals.dataset import CASES, Case
from evals.scoring import CaseResult, check, judge

DEFAULT_MODEL = os.environ.get("DELTA_EVAL_MODEL", "claude-sonnet-5")
DEFAULT_JUDGE_MODEL = os.environ.get("DELTA_EVAL_JUDGE_MODEL", "claude-opus-4-8")
REPORTS_DIR = Path(__file__).resolve().parent / "reports"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--case",
        action="append",
        dest="cases",
        metavar="ID",
        help="run only this case (repeatable)",
    )
    p.add_argument(
        "--mode", choices=["read", "trade"], help="run only cases of this mode"
    )
    p.add_argument(
        "--no-judge", action="store_true", help="skip DeepEval LLM-judge scoring"
    )
    p.add_argument("--list", action="store_true", help="list case ids and exit")
    p.add_argument(
        "--model", default=DEFAULT_MODEL, help=f"agent model (default {DEFAULT_MODEL})"
    )
    p.add_argument(
        "--judge-model",
        default=DEFAULT_JUDGE_MODEL,
        help=f"judge model (default {DEFAULT_JUDGE_MODEL})",
    )
    p.add_argument(
        "--json",
        dest="json_path",
        metavar="PATH",
        help="report path (default evals/reports/eval-<ts>.json)",
    )
    p.add_argument("--no-report", action="store_true", help="don't write a JSON report")
    return p.parse_args()


def select_cases(args: argparse.Namespace) -> list[Case]:
    cases = list(CASES)
    if args.mode:
        cases = [c for c in cases if c.mode == args.mode]
    if args.cases:
        by_id = {c.id: c for c in cases}
        unknown = [i for i in args.cases if i not in by_id]
        if unknown:
            raise SystemExit(f"unknown case id(s): {', '.join(unknown)}")
        cases = [by_id[i] for i in args.cases]
    return cases


async def run_mode_group(
    mode: str,
    cases: list[Case],
    llm: anthropic.AsyncAnthropic,
    args: argparse.Namespace,
) -> list[CaseResult]:
    results = []
    async with agent_mod.mcp_session() as session:
        available = {
            tool.name for tool in (await session.list_tools(cache_mode="refresh")).tools
        }
        for case in cases:
            missing = {e.name for e in case.expect} - available
            if missing:
                results.append(
                    CaseResult(
                        case_id=case.id,
                        mode=mode,
                        passed=False,
                        failures=[
                            (
                                "stable discovery omitted required tool(s): "
                                f"{', '.join(sorted(missing))}"
                            )
                        ],
                    )
                )
                continue
            print(f"  running {case.id} ...", flush=True)
            transcript = await agent_mod.run_case(
                session, llm, case.prompts, model=args.model
            )
            passed, failures = check(case, transcript)
            result = CaseResult(
                case_id=case.id,
                mode=mode,
                passed=passed,
                failures=failures,
                calls=transcript.calls,
                final_text=transcript.final_text,
            )
            if case.judge and not args.no_judge:
                result.judge_scores = judge(case, transcript, args.judge_model)
            results.append(result)
    return results


def print_table(results: list[CaseResult]) -> None:
    print(f"\n{'id':<30} {'mode':<6} {'det':<5} judge")
    for r in results:
        det = "PASS" if r.passed else "FAIL"
        parts = []
        for name, (score, _) in r.judge_scores.items():
            shown = f"{score:.2f}" if score is not None else "ERR"
            flag = " (!)" if score is not None and score < 0.5 else ""
            parts.append(f"{name}={shown}{flag}")
        print(f"{r.case_id:<30} {r.mode:<6} {det:<5} {' '.join(parts) or '-'}")

    for r in results:
        if r.failures:
            print(f"\n{r.case_id}:")
            for f in r.failures:
                print(f"  - {f}")
            if r.calls:
                print(f"  calls: {[c.name for c in r.calls]}")
        for name, (score, reason) in r.judge_scores.items():
            if reason and (score is None or score < 0.5):
                print(f"\n{r.case_id} [{name}] {score}: {reason}")


def write_report(results: list[CaseResult], args: argparse.Namespace) -> Path:
    path = (
        Path(args.json_path)
        if args.json_path
        else REPORTS_DIR / f"eval-{datetime.now(UTC):%Y%m%d-%H%M%S}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": datetime.now(UTC).isoformat(),
        "env": agent_mod.resolve_env(),
        "agent_model": args.model,
        "judge_model": None if args.no_judge else args.judge_model,
        "results": [
            {
                "id": r.case_id,
                "mode": r.mode,
                "passed": r.passed,
                "skipped": None,
                "failures": r.failures,
                "judge_scores": {
                    k: {"score": s, "reason": why}
                    for k, (s, why) in r.judge_scores.items()
                },
                "calls": [
                    {
                        "name": c.name,
                        "args": c.args,
                        "is_error": c.is_error,
                        "result": json.dumps(c.result, default=str)[:2000],
                    }
                    for c in r.calls
                ],
                "final_text": r.final_text,
            }
            for r in results
        ],
    }
    path.write_text(json.dumps(payload, indent=2, default=str))
    return path


async def main() -> int:
    args = parse_args()
    if args.list:
        for c in CASES:
            marks = f"{'judge ' if c.judge else ''}{len(c.prompts)}-turn"
            print(f"{c.id:<30} {c.mode:<6} {marks}")
        return 0

    agent_mod.resolve_env()  # fail fast on india_prod
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY is required")

    cases = select_cases(args)
    llm = anthropic.AsyncAnthropic()
    results: list[CaseResult] = []
    for mode in ("read", "trade"):
        group = [c for c in cases if c.mode == mode]
        if not group:
            continue
        print(f"[{mode}] {len(group)} case(s)")
        results.extend(await run_mode_group(mode, group, llm, args))

    print_table(results)
    if not args.no_report:
        print(f"\nreport: {write_report(results, args)}")
    return sum(1 for r in results if not r.passed)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
