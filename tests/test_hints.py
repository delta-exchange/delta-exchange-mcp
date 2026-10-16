"""What the tool annotations promise a client before it calls anything."""

import asyncio

from delta_exchange_mcp.config import INDIA_TESTNET_REST, Config
from delta_exchange_mcp.server import build_server
from delta_exchange_mcp.tools import trading

# Everything that changes something a person would notice besides the trading mutations:
# the fills export writes a CSV over whatever is at that path, opening the form mints a
# grant, and the form's save writes the settings file.
WRITES = frozenset({"bulk_fills_export", "setup_credentials", "save_credentials"})


def _tools():
    cfg = Config(
        env="india_testnet", base_url=INDIA_TESTNET_REST, api_key="k", api_secret="s"
    )
    return {t.name: t for t in asyncio.run(build_server(cfg).list_tools())}


def test_every_tool_is_annotated():
    for name, tool in _tools().items():
        assert tool.annotations is not None, name
        assert tool.annotations.title, name
        assert tool.annotations.open_world_hint is not None, name


def test_exactly_the_tools_that_change_something_say_so():
    """Naming the writers rather than the readers makes a new unannotated write fail here."""
    changes = {n for n, t in _tools().items() if not t.annotations.read_only_hint}
    expected = WRITES | trading.TOOL_NAMES
    assert changes == expected, (
        f"claiming to write but should not: {sorted(changes - expected)}; "
        f"claiming read-only but writes: {sorted(expected - changes)}"
    )


def test_no_tool_claims_a_repeat_is_free_when_it_is_not():
    """A wrong idempotent hint invites a client to retry an order or a margin change."""
    tools = _tools()
    for name in (
        "setup_credentials",
        "place_order",
        "place_batch_orders",
        "place_bracket_order",
        "adjust_position_margin",
    ):
        assert tools[name].annotations.idempotent_hint is False, name


def test_placing_an_order_is_not_destructive_and_cancelling_one_is():
    tools = _tools()
    for name in ("place_order", "place_batch_orders", "place_bracket_order"):
        assert tools[name].annotations.destructive_hint is False, name
    for name in trading.TOOL_NAMES - {"place_order", "place_batch_orders", "place_bracket_order"}:
        assert tools[name].annotations.destructive_hint is True, name


def test_only_local_tools_are_closed_world():
    closed = {n for n, t in _tools().items() if t.annotations.open_world_hint is False}
    assert closed == {"get_connection_status", "setup_credentials"}
