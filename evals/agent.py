"""MCP stdio session and Anthropic tool-use loop for the eval harness."""

import json
import os
import tempfile
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

# type-only so the dry-run boundary stays importable (and testable) from the
# plain dev environment, which doesn't install the evals group
if TYPE_CHECKING:
    import anthropic

from mcp import StdioServerParameters
from mcp.client import Client
from mcp.client.stdio import get_default_environment
from mcp.types import CallToolResult, Implementation, Tool

REPO_ROOT = Path(__file__).resolve().parent.parent

_ALLOWED_ENVS = {"india_testnet", "india_devnet"}

# Neutral on tool choice — the tool descriptions are what's under test. The
# simulation note is needed because the harness forces dry_run on mutations:
# without it the agent treats the dry-run echo as a failed order and retries
# with fallback tools, corrupting the tool-selection signal.
_SYSTEM = (
    "You are a trading assistant for Delta Exchange India, connected to its MCP tools. "
    "Use the tools to answer; do not ask clarifying questions, act on the request as given. "
    "This is a simulation environment: order-mutating tools execute in dry-run and respond "
    "by echoing the validated payload with dry_run: true. That response means the request "
    "was accepted — treat it as success, do not retry or attempt workarounds."
)

_RESULT_CHAR_CAP = 8000
_CLIENT_INFO = Implementation(name="delta-mcp-evals", version="1")
_MODERN_PROTOCOL = "2026-07-28"


class ToolCaller(Protocol):
    """The one MCP client operation used by the dry-run boundary."""

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> CallToolResult: ...


def resolve_env() -> str:
    env = os.environ.get("DELTA_MCP_ENV", "").strip().lower() or "india_testnet"
    if env not in _ALLOWED_ENVS:
        raise SystemExit(f"evals only run against {sorted(_ALLOWED_ENVS)}, got {env!r}")
    return env


def mutating_tools(tools: Sequence[Tool]) -> frozenset[str]:
    return frozenset(
        tool.name
        for tool in tools
        if "dry_run" in (tool.input_schema.get("properties") or {})
    )


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, Any]
    result: Any
    is_error: bool


@dataclass
class TurnRecord:
    prompt: str
    reply: str
    calls: list[ToolCall]


@dataclass
class Transcript:
    available_tools: list[Tool]
    turns: list[TurnRecord]

    @property
    def calls(self) -> list[ToolCall]:
        return [c for t in self.turns for c in t.calls]

    @property
    def final_text(self) -> str:
        return self.turns[-1].reply if self.turns else ""


def server_environment(config_file: Path | None = None) -> dict[str, str]:
    """Build the safe child environment without legacy trading authorization."""
    # stdio_client replaces the child env wholesale when env= is given, so PATH/HOME
    # must be merged back in or `uv` won't resolve.
    env = {
        **get_default_environment(),
        "DELTA_MCP_ENV": resolve_env(),
    }
    env.pop("DELTA_MCP_MODE", None)
    for key in ("DELTA_API_KEY", "DELTA_API_SECRET"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    if config_file is not None:
        env["DELTA_MCP_CONFIG_FILE"] = str(config_file)
    return env


@asynccontextmanager
async def mcp_session() -> AsyncIterator[Client]:
    """Connect through server/discover and require the current MCP protocol."""
    with tempfile.TemporaryDirectory(prefix="delta-mcp-evals-") as state_dir:
        env = server_environment(Path(state_dir) / "config.env")
        params = StdioServerParameters(
            command="uv",
            args=["run", "delta-exchange-mcp"],
            env=env,
            cwd=str(REPO_ROOT),
        )
        async with Client(
            params,
            mode="auto",
            client_info=_CLIENT_INFO,
        ) as client:
            if client.protocol_version != _MODERN_PROTOCOL:
                raise RuntimeError(
                    "evals require MCP 2026 server/discover; "
                    f"negotiated {client.protocol_version}"
                )
            yield client


def _parse_result(res: CallToolResult) -> Any:
    if res.structured_content is not None:
        return res.structured_content
    text = "\n".join(c.text for c in res.content if getattr(c, "text", None))
    try:
        return json.loads(text)
    except ValueError:
        return text


async def _call(
    session: ToolCaller,
    mutating: frozenset[str],
    name: str,
    args: dict[str, Any],
) -> ToolCall:
    wire_args = {**args, "dry_run": True} if name in mutating else args
    res = await session.call_tool(name, wire_args)
    parsed = _parse_result(res)
    if (
        name in mutating
        and not res.is_error
        and not (isinstance(parsed, dict) and parsed.get("dry_run") is True)
    ):
        raise RuntimeError(
            f"{name} did not honour dry_run, refusing to continue: {parsed!r}"
        )
    # Recorded args are the model's own (pre-forcing) — asserts and the judge
    # should score its intent, not the harness's safety override.
    recorded = (
        {k: v for k, v in args.items() if k != "dry_run"} if name in mutating else args
    )
    return ToolCall(
        name=name,
        args=recorded,
        result=parsed,
        is_error=bool(res.is_error),
    )


async def run_case(
    session: Client,
    llm: "anthropic.AsyncAnthropic",
    prompts: Sequence[str],
    *,
    model: str,
    max_iters: int = 8,
) -> Transcript:
    tool_list = (await session.list_tools()).tools
    mutating = mutating_tools(tool_list)
    anthropic_tools = [
        {
            "name": tool.name,
            "description": tool.description or "",
            "input_schema": tool.input_schema,
        }
        for tool in tool_list
    ]

    messages: list[dict] = []
    turns: list[TurnRecord] = []
    for prompt in prompts:
        messages.append({"role": "user", "content": prompt})
        calls: list[ToolCall] = []
        reply = ""
        for _ in range(max_iters):
            resp = await llm.messages.create(
                model=model,
                max_tokens=2048,
                system=_SYSTEM,
                tools=anthropic_tools,
                messages=messages,
            )
            messages.append({"role": "assistant", "content": resp.content})
            reply = "\n".join(b.text for b in resp.content if b.type == "text")
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:
                break
            results = []
            for block in tool_uses:
                call = await _call(session, mutating, block.name, block.input)
                calls.append(call)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(call.result, default=str)[
                            :_RESULT_CHAR_CAP
                        ],
                        "is_error": call.is_error,
                    }
                )
            messages.append({"role": "user", "content": results})
        turns.append(TurnRecord(prompt=prompt, reply=reply, calls=calls))
    return Transcript(available_tools=list(tool_list), turns=turns)
