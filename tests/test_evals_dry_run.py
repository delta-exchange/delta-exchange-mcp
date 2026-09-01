"""Keep eval tool selection separate from live mutation authorization."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp import types
from mcp.client import Client
from mcp.server.mcpserver import Context
from mcp.types import (
    CallToolResult,
    Implementation,
    InputRequiredResult,
    TextContent,
    Tool,
)

from delta_exchange_mcp import authorization
from delta_exchange_mcp.config import INDIA_TESTNET_REST, Config
from delta_exchange_mcp.server import build_server
from delta_exchange_mcp.tools import account, trading
from evals.agent import (
    _call,
    blocked_tools,
    mutating_tools,
    run_case,
    server_environment,
)
from evals.dataset import CASES, Turn

MANAGE_URL = "http://127.0.0.1:43123/manage"
CLIENT_INFO = Implementation(name="delta-mcp-evals", version="1")


class FakeSession:
    def __init__(self, result):
        self.result = result
        self.sent = None

    async def call_tool(self, name, args):
        self.sent = (name, args)
        return self.result


class FakeRunnerSession:
    async def list_tools(self) -> SimpleNamespace:
        return SimpleNamespace(tools=[])


class FakeMessages:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def create(
        self,
        *,
        model: str,
        max_tokens: int,
        system: str,
        tools: list[dict[str, object]],
        messages: list[dict[str, object]],
    ) -> SimpleNamespace:
        del model, max_tokens, system, tools
        prompt = messages[-1]["content"]
        assert isinstance(prompt, str)
        self.prompts.append(prompt)
        number = len(self.prompts)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=f"reply {number}")]
        )


class FakeLLM:
    def __init__(self) -> None:
        self.messages = FakeMessages()


def _tool(name, properties):
    return Tool(name=name, input_schema={"type": "object", "properties": properties})


def _echo(payload):
    return CallToolResult(content=[], structured_content=payload, is_error=False)


MUTATING = frozenset({"place_order"})


async def test_runner_executes_and_records_each_typed_turn() -> None:
    session = FakeRunnerSession()
    llm = FakeLLM()
    turns = (Turn(prompt="first prompt"), Turn(prompt="second prompt"))

    transcript = await run_case(session, llm, turns, model="offline")

    assert llm.messages.prompts == ["first prompt", "second prompt"]
    assert [turn.prompt for turn in transcript.turns] == [
        "first prompt",
        "second prompt",
    ]
    assert [turn.reply for turn in transcript.turns] == ["reply 1", "reply 2"]


def test_mutating_set_derived_from_dry_run_property():
    tools = [
        _tool("place_order", {"size": {}, "dry_run": {}}),
        _tool("get_ticker", {"symbol": {}}),
    ]
    assert mutating_tools(tools) == {"place_order"}


def test_tools_without_read_only_or_dry_run_are_blocked():
    tools = [
        Tool(
            name="place_order",
            input_schema={"type": "object", "properties": {"dry_run": {}}},
            annotations=types.ToolAnnotations(read_only_hint=False),
        ),
        Tool(
            name="bulk_fills_export",
            input_schema={"type": "object", "properties": {"output_path": {}}},
            annotations=types.ToolAnnotations(read_only_hint=False),
        ),
        Tool(
            name="get_ticker",
            input_schema={"type": "object", "properties": {"symbol": {}}},
            annotations=types.ToolAnnotations(read_only_hint=True),
        ),
        Tool(
            name="unannotated_tool",
            input_schema={"type": "object", "properties": {}},
        ),
    ]

    assert blocked_tools(tools) == {"bulk_fills_export", "unannotated_tool"}


def test_child_environment_isolates_settings_and_legacy_trading_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("DELTA_MCP_ENV", "india_testnet")
    monkeypatch.setenv("DELTA_MCP_MODE", "trade")
    config_file = tmp_path / "config.env"
    environment = server_environment(config_file)

    assert "DELTA_MCP_MODE" not in environment
    assert environment["DELTA_MCP_CONFIG_FILE"] == str(config_file)


async def test_modern_discovery_lists_every_trade_tool_without_consent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def disconnected(ctx: Context) -> authorization.AccessState:
        del ctx
        return authorization.AccessState(
            credentials_ready=False,
            trading_enabled=False,
            client_name=CLIENT_INFO.name,
            final_trading_check=lambda: False,
        )

    app = build_server(
        Config(env="india_testnet", base_url=INDIA_TESTNET_REST),
        access_state=disconnected,
    )

    async def fail_if_called(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("dry-run evaluation sent a mutation request")

    monkeypatch.setattr(app.live_client, "post", fail_if_called)
    monkeypatch.setattr(app.live_client, "put", fail_if_called)
    monkeypatch.setattr(app.live_client, "delete", fail_if_called)
    try:
        async with Client(
            app,
            mode="auto",
            client_info=CLIENT_INFO,
        ) as client:
            tools = (await client.list_tools(cache_mode="refresh")).tools
            result = await client.call_tool(
                "place_order",
                {
                    "product_symbol": "BTCUSD",
                    "size": 1,
                    "side": "buy",
                    "order_type": "market_order",
                    "dry_run": True,
                },
            )
            protocol_version = client.protocol_version
    finally:
        await app.close_live_client()

    assert protocol_version == "2026-07-28"
    assert account.TOOL_NAMES <= {tool.name for tool in tools}
    assert mutating_tools(tools) == trading.TOOL_NAMES
    blocked = blocked_tools(tools)
    assert blocked == {
        "bulk_fills_export",
        "setup_credentials",
    }
    expected = {
        expectation.name
        for case in CASES
        for turn in case.turns
        for expectation in turn.expect
    }
    assert expected.isdisjoint(blocked)
    assert result.structured_content["dry_run"] is True


@pytest.mark.parametrize("dry_run", [None, False])
async def test_selected_real_trade_requires_input_without_sending_a_mutation(
    dry_run: bool | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def credentials_without_consent(ctx: Context) -> authorization.AccessState:
        del ctx
        return authorization.AccessState(
            credentials_ready=True,
            trading_enabled=False,
            client_name=CLIENT_INFO.name,
            final_trading_check=lambda: False,
        )

    async def manage_url(ctx: Context, access: authorization.Access) -> str:
        del ctx, access
        return MANAGE_URL

    async def elicit(ctx: object, params: object) -> types.ElicitResult:
        del ctx, params
        return types.ElicitResult(action="accept")

    app = build_server(
        Config(env="india_testnet", base_url=INDIA_TESTNET_REST),
        access_state=credentials_without_consent,
        manage_url=manage_url,
    )
    mutations: list[str] = []

    async def record_mutation(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        mutations.append("sent")
        return {}

    monkeypatch.setattr(app.live_client, "post", record_mutation)
    monkeypatch.setattr(app.live_client, "put", record_mutation)
    monkeypatch.setattr(app.live_client, "delete", record_mutation)
    arguments: dict[str, object] = {
        "product_symbol": "BTCUSD",
        "size": 1,
        "side": "buy",
        "order_type": "market_order",
    }
    if dry_run is not None:
        arguments["dry_run"] = dry_run

    try:
        async with Client(
            app,
            mode="auto",
            client_info=CLIENT_INFO,
            elicitation_callback=elicit,
        ) as client:
            result = await client.session.call_tool(
                "place_order",
                arguments,
                allow_input_required=True,
            )
    finally:
        await app.close_live_client()

    assert isinstance(result, InputRequiredResult)
    request = result.input_requests["delta_exchange_authorization"]
    assert "Enable trading" in request.params.message
    assert mutations == []


async def test_explicit_dry_run_false_is_overridden():
    session = FakeSession(_echo({"dry_run": True, "method": "POST", "path": "/orders"}))
    call = await _call(session, MUTATING, "place_order", {"size": 1, "dry_run": False})
    assert session.sent[1]["dry_run"] is True
    # recorded args are the model's intent, without the harness override
    assert "dry_run" not in call.args
    assert call.args == {"size": 1}


async def test_success_without_dry_run_echo_is_rejected():
    session = FakeSession(_echo({"id": 42, "state": "open"}))
    with pytest.raises(RuntimeError, match="did not honour dry_run"):
        await _call(session, MUTATING, "place_order", {"size": 1})


async def test_non_dry_run_mutation_is_rejected_before_the_tool_call():
    session = FakeSession(_echo({"path": "fills.csv"}))

    with pytest.raises(RuntimeError, match="not read-only and has no dry_run"):
        await _call(
            session,
            frozenset(),
            "bulk_fills_export",
            {"output_path": "fills.csv"},
            blocked=frozenset({"bulk_fills_export"}),
        )

    assert session.sent is None


async def test_error_results_skip_the_echo_check():
    session = FakeSession(
        CallToolResult(
            content=[
                TextContent(type="text", text="delta api error: insufficient_margin")
            ],
            is_error=True,
        )
    )
    call = await _call(session, MUTATING, "place_order", {"size": 1})
    assert call.is_error
    assert session.sent[1]["dry_run"] is True


async def test_non_mutating_tools_pass_through_untouched():
    session = FakeSession(_echo({"symbol": "BTCUSD"}))
    call = await _call(session, frozenset(), "get_ticker", {"symbol": "BTCUSD"})
    assert session.sent == ("get_ticker", {"symbol": "BTCUSD"})
    assert call.args == {"symbol": "BTCUSD"}
