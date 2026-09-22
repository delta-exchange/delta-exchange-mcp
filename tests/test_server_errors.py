from typing import Any

from mcp.client import Client
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import Implementation

from delta_exchange_mcp.server import DeltaMCP


async def test_pre_call_errors_follow_the_sdk_masking_contract() -> None:
    app = DeltaMCP()

    async def authorize(
        name: str,
        arguments: dict[str, Any],
        context: Context,
    ) -> None:
        if name == "expected_failure":
            raise ToolError("connect the account and retry")
        raise RuntimeError("unknown-private-value")

    app.before_tool_call(authorize)

    @app.tool()
    async def expected_failure() -> None:
        return None

    @app.tool()
    async def unknown_failure() -> None:
        return None

    try:
        async with Client(
            app,
            mode="2026-07-28",
            client_info=Implementation(name="error-contract-test", version="1"),
        ) as client:
            expected = await client.call_tool("expected_failure", {})
            unknown = await client.call_tool("unknown_failure", {})
    finally:
        await app.close_live_client()

    assert expected.is_error is True
    assert expected.content[0].text == "connect the account and retry"
    assert unknown.is_error is True
    assert unknown.content[0].text == "Error executing tool unknown_failure"
    assert "unknown-private-value" not in unknown.content[0].text
