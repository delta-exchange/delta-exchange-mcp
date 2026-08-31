"""Deterministic asserts (the gate) + DeepEval LLM-judge scores (advisory)."""

import json
import os
from dataclasses import dataclass, field

from mcp.types import CallToolResult, TextContent

from evals.agent import ToolCall, Transcript, TurnRecord
from evals.dataset import ANY, MUTATING_TOOLS, Case, Expect


@dataclass
class CaseResult:
    case_id: str
    mode: str
    passed: bool  # deterministic verdict only; judge is advisory
    failures: list[str] = field(default_factory=list)
    judge_scores: dict[str, tuple[float | None, str]] = field(default_factory=dict)
    turns: list[TurnRecord] = field(default_factory=list)

    @property
    def calls(self) -> list[ToolCall]:
        return [call for turn in self.turns for call in turn.calls]

    @property
    def final_text(self) -> str:
        return self.turns[-1].reply if self.turns else ""


def _value_matches(expected: object, actual: object) -> bool:
    if expected is ANY:
        return actual is not None
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _value_matches(value, actual[key])
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(expected) == len(actual)
            and all(
                _value_matches(expected_item, actual_item)
                for expected_item, actual_item in zip(expected, actual)
            )
        )
    return expected == actual


def _args_match(expected: dict[str, object], actual: dict[str, object]) -> bool:
    for key, value in expected.items():
        if key not in actual:
            return False
        if not _value_matches(value, actual[key]):
            return False
    return True


def _fmt(exp: Expect) -> str:
    if not exp.args:
        return exp.name
    args = ", ".join(
        f"{k}=<any>" if v is ANY else f"{k}={v!r}" for k, v in exp.args.items()
    )
    return f"{exp.name}({args})"


def check(case: Case, transcript: Transcript) -> tuple[bool, list[str]]:
    failures: list[str] = []

    if len(transcript.turns) != len(case.turns):
        failures.append(
            f"expected {len(case.turns)} transcript turn(s), got {len(transcript.turns)}"
        )

    for index, expected_turn in enumerate(case.turns):
        number = index + 1
        if index >= len(transcript.turns):
            failures.append(f"turn {number}: no transcript record")
            continue
        actual_turn = transcript.turns[index]
        if actual_turn.prompt != expected_turn.prompt:
            failures.append(f"turn {number}: transcript prompt does not match the case")

        matched: set[int] = set()
        cursor = 0
        for exp in expected_turn.expect:
            for call_index in range(cursor, len(actual_turn.calls)):
                call = actual_turn.calls[call_index]
                if call.name == exp.name and _args_match(exp.args, call.args):
                    matched.add(call_index)
                    cursor = call_index + 1
                    break
            else:
                near = [
                    call.args for call in actual_turn.calls if call.name == exp.name
                ]
                detail = f" (called with {near})" if near else ""
                failures.append(
                    f"turn {number}: expected {_fmt(exp)}, not called{detail}"
                )

        expected_reads = {
            exp.name
            for exp in expected_turn.expect
            if exp.name not in MUTATING_TOOLS
        }
        permitted_reads = expected_reads | expected_turn.allowed_reads
        for call_index, call in enumerate(actual_turn.calls):
            if call.name in MUTATING_TOOLS:
                if (
                    call.name in expected_turn.forbidden_mutations
                    or call_index not in matched
                ):
                    failures.append(
                        f"turn {number}: forbidden mutation called: {call.name}"
                    )
            elif call.name in expected_turn.forbidden_reads:
                failures.append(f"turn {number}: forbidden read called: {call.name}")
            elif call.name not in permitted_reads:
                failures.append(
                    f"turn {number}: unexpected supporting read called: {call.name}"
                )

    return not failures, failures


def judge(
    case: Case, transcript: Transcript, judge_model: str
) -> dict[str, tuple[float | None, str]]:
    os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")
    from deepeval.metrics import (
        MCPTaskCompletionMetric,
        MCPUseMetric,
        MultiTurnMCPUseMetric,
    )
    from deepeval.models import AnthropicModel
    from deepeval.test_case import ConversationalTestCase, LLMTestCase, Turn
    from deepeval.test_case.mcp import MCPServer, MCPToolCall

    model = AnthropicModel(model=judge_model, temperature=0)
    server = MCPServer(
        server_name="delta-exchange",
        transport="stdio",
        available_tools=list(transcript.available_tools),
    )

    def tool_calls(calls: list[ToolCall]) -> list:
        # deepeval requires result to be an mcp CallToolResult, but the raw one
        # must be shrunk — full payloads (e.g. an options chain) blow past the
        # judge's context window.
        shrunk = []
        for c in calls:
            text = json.dumps(c.result, default=str)[:4000]
            shrunk.append(
                MCPToolCall(
                    name=c.name,
                    args=c.args,
                    result=CallToolResult(
                        content=[TextContent(type="text", text=text)],
                        # deepeval's multi-turn text builder reads
                        # result.structured_content["result"] unguarded
                        structured_content={"result": text},
                        is_error=c.is_error,
                    ),
                )
            )
        return shrunk

    if len(case.turns) == 1:
        test_case = LLMTestCase(
            input=case.turns[0].prompt,
            actual_output=transcript.final_text or "(no reply)",
            mcp_servers=[server],
            mcp_tools_called=tool_calls(transcript.turns[0].calls),
        )
        metrics = [MCPUseMetric(model=model)]
    else:
        # One assistant Turn per tool call + a final reply Turn: deepeval's
        # multi-turn MCP metrics drop any user→assistant interaction of ≤2 turns,
        # so a single aggregated assistant turn scores 0 with "no data".
        turns = []
        for rec in transcript.turns:
            turns.append(Turn(role="user", content=rec.prompt))
            for call, mcp_call in zip(rec.calls, tool_calls(rec.calls)):
                turns.append(
                    Turn(
                        role="assistant",
                        content=f"Tool call: {call.name} with args {call.args}",
                        mcp_tools_called=[mcp_call],
                    )
                )
            turns.append(Turn(role="assistant", content=rec.reply or "(no reply)"))
        test_case = ConversationalTestCase(turns=turns, mcp_servers=[server])
        metrics = [
            MultiTurnMCPUseMetric(model=model),
            MCPTaskCompletionMetric(model=model),
        ]

    scores: dict[str, tuple[float | None, str]] = {}
    for metric in metrics:
        name = type(metric).__name__
        try:
            metric.measure(test_case)
            scores[name] = (metric.score, metric.reason or "")
        except Exception as exc:  # noqa: BLE001 - external judge errors are advisory
            scores[name] = (None, f"judge error: {exc}")
    return scores
