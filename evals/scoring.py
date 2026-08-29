"""Deterministic asserts (the gate) + DeepEval LLM-judge scores (advisory)."""

import json
import os
from dataclasses import dataclass, field

from mcp.types import CallToolResult, TextContent

from evals.agent import ToolCall, Transcript
from evals.dataset import ANY, Case, Expect


@dataclass
class CaseResult:
    case_id: str
    mode: str
    passed: bool  # deterministic verdict only; judge is advisory
    failures: list[str] = field(default_factory=list)
    judge_scores: dict[str, tuple[float | None, str]] = field(default_factory=dict)
    calls: list[ToolCall] = field(default_factory=list)
    final_text: str = ""
    skipped: str | None = None


def _args_match(expected: dict, actual: dict) -> bool:
    for key, value in expected.items():
        if key not in actual:
            return False
        if value is not ANY and actual[key] != value:
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
    calls = transcript.calls

    # Expected calls must appear in order (greedy scan) — multi-turn cases rely on it.
    cursor = 0
    for exp in case.expect:
        for i in range(cursor, len(calls)):
            if calls[i].name == exp.name and _args_match(exp.args, calls[i].args):
                cursor = i + 1
                break
        else:
            near = [c.args for c in calls if c.name == exp.name]
            detail = f" (called with {near})" if near else ""
            failures.append(f"expected {_fmt(exp)}, not called{detail}")

    for name in sorted(case.forbid):
        if any(c.name == name for c in calls):
            failures.append(f"forbidden tool called: {name}")

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
                        is_error=c.raw.is_error,
                    ),
                )
            )
        return shrunk

    if len(case.prompts) == 1:
        test_case = LLMTestCase(
            input=case.prompts[0],
            actual_output=transcript.final_text or "(no reply)",
            mcp_servers=[server],
            mcp_tools_called=tool_calls(transcript.calls),
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
