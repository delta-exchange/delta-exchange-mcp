"""Trading tools: body signing, dry-run, validation, user_id caching, audit, mode gating."""

import asyncio
import hashlib
import hmac
import json
from typing import Any

import httpx
import pytest
import respx

from delta_exchange_mcp import audit_log
from delta_exchange_mcp.client import DeltaClient
from delta_exchange_mcp.config import INDIA_PROD_REST, INDIA_TESTNET_REST, Config
from delta_exchange_mcp.errors import DeltaApiError
from delta_exchange_mcp.server import build_server
from delta_exchange_mcp.tools import trading
from mcp.server.fastmcp import FastMCP


def _client() -> DeltaClient:
    cfg = Config(
        env="india_testnet", base_url=INDIA_TESTNET_REST,
        api_key="k1", api_secret="s1", mode="trade",
    )
    return DeltaClient(cfg)


async def _call(
    client: DeltaClient,
    name: str,
    audit=None,
    gate: trading.TradeGate | None = None,
    **kwargs: Any,
) -> Any:
    mcp = FastMCP("test")
    trading.register(mcp, client, audit, gate)
    return await mcp.call_tool(name, kwargs)


def _payload(call_result: Any) -> dict[str, Any]:
    """mcp.call_tool returns (content, structured); pull the structured dict out."""
    structured = call_result[1]
    return structured.get("result", structured) if isinstance(structured, dict) else structured


# --------------------------------------------------------------- body signing (critical)


@pytest.mark.asyncio
@respx.mock
async def test_place_order_signs_exact_body_bytes():
    route = respx.post(f"{INDIA_TESTNET_REST}/orders").mock(
        return_value=httpx.Response(200, json={"success": True, "result": {"id": 7}})
    )
    client = _client()
    await _call(
        client, "place_order",
        product_id=27, size=1, side="buy", order_type="limit_order", limit_price="10000",
    )

    req = route.calls[0].request
    body = req.content.decode()
    # Sent bytes must be compact JSON (no spaces) so the signature matches.
    assert body == json.dumps(json.loads(body), separators=(",", ":"))
    ts = req.headers["timestamp"]
    expected = hmac.new(b"s1", f"POST{ts}/v2/orders{body}".encode(), hashlib.sha256).hexdigest()
    assert req.headers["signature"] == expected
    assert req.headers["api-key"] == "k1"


@pytest.mark.asyncio
@respx.mock
async def test_identity_rebind_revokes_a_trade_still_in_preflight():
    """A lookup cannot finish by mutating either the old or newly rebound account."""
    lookup_started = asyncio.Event()
    release_lookup = asyncio.Event()
    old_requests: list[httpx.Request] = []

    async def old_account(request: httpx.Request) -> httpx.Response:
        old_requests.append(request)
        if request.method == "GET":
            lookup_started.set()
            await release_lookup.wait()
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": {"id": 84, "symbol": "BTCUSD", "tick_size": "0.1"},
                },
            )
        return httpx.Response(200, json={"success": True, "result": {"id": 7}})

    old_http = httpx.AsyncClient(
        base_url=INDIA_TESTNET_REST,
        transport=httpx.MockTransport(old_account),
    )
    client = DeltaClient(
        Config(
            env="india_testnet",
            base_url=INDIA_TESTNET_REST,
            api_key="old-key",
            api_secret="old-secret",
            mode="trade",
        ),
        http=old_http,
    )
    gate = trading.TradeGate()
    new_account = respx.post(f"{INDIA_PROD_REST}/orders").mock(
        return_value=httpx.Response(200, json={"success": True, "result": {"id": 8}})
    )

    call = asyncio.create_task(
        _call(
            client,
            "place_order",
            gate=gate,
            product_symbol="BTCUSD",
            size=1,
            side="buy",
            order_type="limit_order",
            limit_price="62000.07",
        )
    )
    await lookup_started.wait()
    gate.revoke()
    client.rebind(
        Config(
            env="india_prod",
            base_url=INDIA_PROD_REST,
            api_key="new-key",
            api_secret="new-secret",
            mode="read",
        )
    )
    release_lookup.set()
    with pytest.raises(Exception, match="trading was disabled.*no mutation was sent"):
        await call
    await client.aclose()

    assert [(request.method, str(request.url)) for request in old_requests] == [
        ("GET", f"{INDIA_TESTNET_REST}/products/BTCUSD"),
    ]
    assert new_account.called is False


@pytest.mark.asyncio
async def test_trade_to_read_revokes_a_trade_still_in_preflight():
    """Turning trading off wins if an order has not reached its mutation request yet."""
    lookup_started = asyncio.Event()
    release_lookup = asyncio.Event()
    requests: list[httpx.Request] = []

    async def account(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        lookup_started.set()
        await release_lookup.wait()
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {"id": 84, "symbol": "BTCUSD", "tick_size": "0.1"},
            },
        )

    http = httpx.AsyncClient(
        base_url=INDIA_TESTNET_REST,
        transport=httpx.MockTransport(account),
    )
    client = DeltaClient(
        Config(
            env="india_testnet",
            base_url=INDIA_TESTNET_REST,
            api_key="key",
            api_secret="secret",
            mode="trade",
        ),
        http=http,
    )
    gate = trading.TradeGate()
    call = asyncio.create_task(
        _call(
            client,
            "place_order",
            gate=gate,
            product_symbol="BTCUSD",
            size=1,
            side="buy",
            order_type="limit_order",
            limit_price="62000.07",
        )
    )

    await lookup_started.wait()
    gate.revoke()
    release_lookup.set()
    with pytest.raises(Exception, match="trading was disabled.*no mutation was sent"):
        await call
    await client.aclose()

    assert [(request.method, str(request.url)) for request in requests] == [
        ("GET", f"{INDIA_TESTNET_REST}/products/BTCUSD"),
    ]


@pytest.mark.asyncio
@respx.mock
async def test_post_only_bool_becomes_string_enum():
    route = respx.post(f"{INDIA_TESTNET_REST}/orders").mock(
        return_value=httpx.Response(200, json={"success": True, "result": {}})
    )
    client = _client()
    await _call(
        client, "place_order",
        product_id=27, size=1, side="buy", order_type="market_order", post_only=True,
    )
    assert route.calls[0].request.read().__contains__(b'"post_only":"true"')


@pytest.mark.asyncio
@respx.mock
async def test_auto_topup_bool_stays_json_bool():
    route = respx.put(f"{INDIA_TESTNET_REST}/positions/auto_topup").mock(
        return_value=httpx.Response(200, json={"success": True, "result": {}})
    )
    client = _client()
    await _call(client, "configure_auto_topup", product_id=27, auto_topup=True)
    assert b'"auto_topup":true' in route.calls[0].request.content


# --------------------------------------------------------------- dry-run


@pytest.mark.asyncio
@respx.mock
async def test_dry_run_sends_nothing_and_echoes_payload():
    route = respx.post(f"{INDIA_TESTNET_REST}/orders").mock(
        return_value=httpx.Response(200, json={"success": True, "result": {}})
    )
    client = _client()
    out = _payload(await _call(
        client, "place_order",
        product_id=27, size=2, side="sell", order_type="market_order", dry_run=True,
    ))
    assert route.called is False
    assert out["dry_run"] is True
    assert out["method"] == "POST" and out["path"] == "/orders"
    assert out["payload"]["size"] == 2 and out["payload"]["side"] == "sell"


# --------------------------------------------------------------- validation


@pytest.mark.asyncio
async def test_place_order_requires_exactly_one_product_ref():
    client = _client()
    with pytest.raises(Exception, match="exactly one of product_id or product_symbol"):
        await _call(client, "place_order", size=1, side="buy", order_type="market_order")
    with pytest.raises(Exception, match="exactly one of product_id or product_symbol"):
        await _call(
            client, "place_order",
            product_id=1, product_symbol="BTCUSD", size=1, side="buy", order_type="market_order",
        )


@pytest.mark.asyncio
async def test_place_order_rejects_bracket_price_and_trail_together():
    client = _client()
    with pytest.raises(Exception, match="either a fixed price or a trailing amount"):
        await _call(
            client, "place_order",
            product_id=27, size=1, side="buy", order_type="market_order",
            bracket_stop_loss_price="9000", bracket_trail_amount="50", dry_run=True,
        )


@pytest.mark.asyncio
async def test_edit_bracket_rejects_bracket_price_and_trail_together():
    client = _client()
    with pytest.raises(Exception, match="either a fixed price or a trailing amount"):
        await _call(
            client, "edit_bracket_order",
            id=7, product_id=27,
            bracket_stop_loss_price="9000", bracket_trail_amount="50", dry_run=True,
        )


@pytest.mark.asyncio
async def test_place_bracket_rejects_sl_leg_price_and_trail_together():
    client = _client()
    with pytest.raises(Exception, match="either a fixed price or a trailing amount"):
        await _call(
            client, "place_bracket_order",
            product_id=27,
            stop_loss_order={"order_type": "market_order", "stop_price": "9000", "trail_amount": "50"},
            dry_run=True,
        )


@pytest.mark.asyncio
async def test_batch_cap_enforced():
    client = _client()
    orders = [{"size": 1, "side": "buy", "order_type": "limit_order", "limit_price": "1"}] * 51
    with pytest.raises(Exception, match="exceeds max 50"):
        await _call(client, "place_batch_orders", product_id=27, orders=orders)


# --------------------------------------------------------------- user_id auto-fetch


@pytest.mark.asyncio
@respx.mock
async def test_close_all_fetches_and_caches_user_id():
    profile = respx.get(f"{INDIA_TESTNET_REST}/profile").mock(
        return_value=httpx.Response(200, json={"success": True, "result": {"id": 999}})
    )
    close = respx.post(f"{INDIA_TESTNET_REST}/positions/close_all").mock(
        return_value=httpx.Response(200, json={"success": True, "result": {}})
    )
    client = _client()
    mcp = FastMCP("test")
    trading.register(mcp, client, None)
    await mcp.call_tool("close_all_positions", {"close_all_portfolio": True})
    await mcp.call_tool("close_all_positions", {"close_all_portfolio": True})

    assert profile.call_count == 1  # cached after first fetch
    assert close.call_count == 2
    assert b'"user_id":999' in close.calls[0].request.content


# --------------------------------------------------------------- audit log


@pytest.mark.asyncio
@respx.mock
async def test_audit_records_success_and_error_without_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("DELTA_MCP_AUDIT_FILE", str(tmp_path / "audit.log"))
    monkeypatch.setattr(audit_log, "_INSTANCE", None)
    cfg = Config(
        env="india_testnet", base_url=INDIA_TESTNET_REST,
        api_key="k1", api_secret="s1", mode="trade",
    )
    audit = audit_log.configure(cfg)
    assert audit is not None

    respx.post(f"{INDIA_TESTNET_REST}/orders").mock(
        side_effect=[
            httpx.Response(200, json={"success": True, "result": {"id": 5, "state": "open"}}),
            httpx.Response(400, json={"success": False, "error": {"code": "insufficient_margin"}}),
        ]
    )
    client = _client()
    await _call(client, "place_order", audit=audit,
                product_id=27, size=1, side="buy", order_type="market_order")
    # FastMCP wraps the DeltaApiError in a ToolError, but _finish records it first.
    with pytest.raises(Exception, match="insufficient_margin"):
        await _call(client, "place_order", audit=audit,
                    product_id=27, size=1, side="buy", order_type="market_order")

    text = (tmp_path / "audit.log").read_text()
    lines = [json.loads(line) for line in text.splitlines()]
    assert len(lines) == 2
    assert lines[0]["result"] == {"id": 5, "state": "open"}
    assert "insufficient_margin" in lines[1]["error"]
    # Credentials must never appear in the audit file.
    assert "s1" not in text and "signature" not in text and "api-key" not in text


@pytest.mark.asyncio
async def test_audit_kill_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("DELTA_MCP_AUDIT", "off")
    monkeypatch.setattr(audit_log, "_INSTANCE", None)
    cfg = Config(
        env="india_testnet", base_url=INDIA_TESTNET_REST,
        api_key="k1", api_secret="s1", mode="trade",
    )
    assert audit_log.configure(cfg) is None


# --------------------------------------------------------------- mode gating


def test_trade_tools_absent_in_read_mode():
    cfg = Config(
        env="india_testnet", base_url=INDIA_TESTNET_REST,
        api_key="k1", api_secret="s1", mode="read",
    )
    mcp = build_server(cfg)
    names = {t.name for t in mcp._tool_manager.list_tools()}
    assert "place_order" not in names
    assert "get_positions" in names  # account tools still present


def test_trade_tools_present_in_trade_mode(monkeypatch):
    monkeypatch.setenv("DELTA_MCP_AUDIT", "off")  # no file writes during this test
    monkeypatch.setattr(audit_log, "_INSTANCE", None)
    cfg = Config(
        env="india_testnet", base_url=INDIA_TESTNET_REST,
        api_key="k1", api_secret="s1", mode="trade",
    )
    mcp = build_server(cfg)
    names = {t.name for t in mcp._tool_manager.list_tools()}
    for tool in (
        "place_order", "edit_order", "cancel_order", "cancel_all_orders",
        "place_batch_orders", "edit_batch_orders", "cancel_batch_orders",
        "place_bracket_order", "edit_bracket_order", "set_product_leverage",
        "adjust_position_margin", "close_all_positions", "configure_auto_topup",
    ):
        assert tool in names


@pytest.mark.asyncio
async def test_all_trading_tools_declare_mutating_metadata():
    mcp = FastMCP("test")
    client = _client()
    try:
        trading.register(mcp, client)
        tools = await mcp.list_tools()
    finally:
        await client.aclose()

    assert len(tools) == 13
    assert all(
        tool.meta == {trading.MUTATING_TOOL_META_KEY: True}
        for tool in tools
    )


# --------------------------------------------------------------- BUG-1: cancel_all defaults


@pytest.mark.asyncio
@respx.mock
async def test_cancel_all_bare_call_defaults_all_flags_true():
    route = respx.delete(f"{INDIA_TESTNET_REST}/orders/all").mock(
        return_value=httpx.Response(200, json={"success": True, "result": {}})
    )
    client = _client()
    await _call(client, "cancel_all_orders", product_id=84)
    body = route.calls[0].request.content
    assert b'"cancel_limit_orders":"true"' in body
    assert b'"cancel_stop_orders":"true"' in body
    assert b'"cancel_reduce_only_orders":"true"' in body


@pytest.mark.asyncio
@respx.mock
async def test_cancel_all_explicit_flag_stays_narrow():
    route = respx.delete(f"{INDIA_TESTNET_REST}/orders/all").mock(
        return_value=httpx.Response(200, json={"success": True, "result": {}})
    )
    client = _client()
    await _call(client, "cancel_all_orders", product_id=84, cancel_limit_orders=True)
    body = route.calls[0].request.content
    assert b'"cancel_limit_orders":"true"' in body
    # the other two are NOT auto-set when one flag is given explicitly
    assert b"cancel_stop_orders" not in body
    assert b"cancel_reduce_only_orders" not in body


# --------------------------------------------------------------- BUG-3: duplicate coid in batch


@pytest.mark.asyncio
@respx.mock
async def test_batch_rejects_duplicate_client_order_id():
    route = respx.post(f"{INDIA_TESTNET_REST}/orders/batch").mock(
        return_value=httpx.Response(200, json={"success": True, "result": []})
    )
    client = _client()
    orders = [
        {"side": "buy", "order_type": "limit_order", "limit_price": "61000", "size": 1, "client_order_id": "dup"},
        {"side": "buy", "order_type": "limit_order", "limit_price": "61001", "size": 1, "client_order_id": "dup"},
    ]
    with pytest.raises(Exception, match="duplicate client_order_id in batch: dup"):
        await _call(client, "place_batch_orders", product_id=84, orders=orders)
    assert route.called is False


# --------------------------------------------------------------- BUG-2: batch partial failure


@pytest.mark.asyncio
@respx.mock
async def test_place_batch_flags_partial_failure_with_dropped_coids():
    # sent 3, API echoes only 2 — the size:0 item is dropped silently by Delta.
    route = respx.post(f"{INDIA_TESTNET_REST}/orders/batch").mock(
        return_value=httpx.Response(200, json={"success": True, "result": [
            {"id": 1, "client_order_id": "a"},
            {"id": 2, "client_order_id": "c"},
        ]})
    )
    client = _client()
    orders = [
        {"side": "buy", "order_type": "limit_order", "limit_price": "61000", "size": 1, "client_order_id": "a"},
        {"side": "buy", "order_type": "limit_order", "limit_price": "61000", "size": 1, "client_order_id": "b"},
        {"side": "buy", "order_type": "limit_order", "limit_price": "61000", "size": 1, "client_order_id": "c"},
    ]
    out = await _call(client, "place_batch_orders", product_id=84, orders=orders)
    assert route.called
    pf = out[1]["partial_failure"]
    assert pf["requested"] == 3 and pf["succeeded"] == 2 and pf["dropped"] == 1
    assert pf["dropped_client_order_ids"] == ["b"]


@pytest.mark.asyncio
@respx.mock
async def test_cancel_batch_flags_dropped_ids():
    respx.delete(f"{INDIA_TESTNET_REST}/orders/batch").mock(
        return_value=httpx.Response(200, json={"success": True, "result": [{"id": 111}]})
    )
    client = _client()
    out = await _call(
        client, "cancel_batch_orders", product_id=84,
        orders=[{"id": 111}, {"id": 999999999}],
    )
    pf = out[1]["partial_failure"]
    assert pf["requested"] == 2 and pf["succeeded"] == 1
    assert pf["dropped_ids"] == [999999999]


@pytest.mark.asyncio
@respx.mock
async def test_batch_no_partial_flag_when_all_succeed():
    respx.post(f"{INDIA_TESTNET_REST}/orders/batch").mock(
        return_value=httpx.Response(200, json={"success": True, "result": [{"id": 1}, {"id": 2}]})
    )
    client = _client()
    orders = [
        {"side": "buy", "order_type": "limit_order", "limit_price": "61000", "size": 1},
        {"side": "buy", "order_type": "limit_order", "limit_price": "61000", "size": 1},
    ]
    out = await _call(client, "place_batch_orders", product_id=84, orders=orders)
    assert "partial_failure" not in out[1]


# --------------------------------------------------------------- BUG-4: close_all scope


@pytest.mark.asyncio
async def test_close_all_requires_a_scope():
    client = _client()
    with pytest.raises(Exception, match="at least one of close_all_portfolio or close_all_isolated"):
        await _call(client, "close_all_positions")


@pytest.mark.asyncio
@respx.mock
async def test_close_all_explicit_scope_not_broadened():
    respx.get(f"{INDIA_TESTNET_REST}/profile").mock(
        return_value=httpx.Response(200, json={"success": True, "result": {"id": 7}})
    )
    route = respx.post(f"{INDIA_TESTNET_REST}/positions/close_all").mock(
        return_value=httpx.Response(200, json={"success": True, "result": {}})
    )
    client = _client()
    await _call(client, "close_all_positions", close_all_isolated=True)
    body = route.calls[0].request.content
    assert b'"close_all_isolated":true' in body
    assert b'"close_all_portfolio":false' in body


# --------------------------------------------------------------- BUG-5: place_order brackets


@pytest.mark.asyncio
@respx.mock
async def test_place_order_includes_bracket_params():
    route = respx.post(f"{INDIA_TESTNET_REST}/orders").mock(
        return_value=httpx.Response(200, json={"success": True, "result": {"id": 9}})
    )
    client = _client()
    await _call(
        client, "place_order",
        product_id=84, size=1, side="buy", order_type="limit_order", limit_price="61000",
        bracket_take_profit_price="66500", bracket_stop_loss_price="60000",
    )
    body = route.calls[0].request.content
    assert b'"bracket_take_profit_price":"66500"' in body
    assert b'"bracket_stop_loss_price":"60000"' in body


# --------------------------------------------------------------- BUG-6/7: order validation


@pytest.mark.asyncio
@respx.mock
async def test_market_order_rejects_limit_price():
    route = respx.post(f"{INDIA_TESTNET_REST}/orders").mock(
        return_value=httpx.Response(200, json={"success": True, "result": {}})
    )
    client = _client()
    for dry in (False, True):
        with pytest.raises(Exception, match="market_order must not carry a limit_price"):
            await _call(
                client, "place_order",
                product_id=84, size=1, side="buy", order_type="market_order",
                limit_price="50000", dry_run=dry,
            )
    assert route.called is False


@pytest.mark.asyncio
async def test_limit_order_requires_limit_price():
    client = _client()
    with pytest.raises(Exception, match="limit_price is required for limit_order"):
        await _call(
            client, "place_order",
            product_id=84, size=1, side="buy", order_type="limit_order", dry_run=True,
        )


@pytest.mark.asyncio
async def test_non_positive_size_rejected():
    client = _client()
    with pytest.raises(Exception, match="size must be a positive integer"):
        await _call(
            client, "place_order",
            product_id=84, size=-5, side="buy", order_type="limit_order",
            limit_price="61000", dry_run=True,
        )


# --------------------------------------------------------------- BUG-8: tick rounding


@pytest.mark.asyncio
@respx.mock
async def test_off_tick_price_rounded_to_nearest():
    respx.get(f"{INDIA_TESTNET_REST}/products/BTCUSD").mock(
        return_value=httpx.Response(
            200, json={"success": True, "result": {"id": 84, "symbol": "BTCUSD", "tick_size": "0.1"}}
        )
    )
    route = respx.post(f"{INDIA_TESTNET_REST}/orders").mock(
        return_value=httpx.Response(200, json={"success": True, "result": {"id": 1}})
    )
    client = _client()
    out = await _call(
        client, "place_order",
        product_symbol="BTCUSD", size=1, side="buy", order_type="limit_order", limit_price="62000.07",
    )
    assert b'"limit_price":"62000.1"' in route.calls[0].request.content
    structured = out[1]
    assert structured["price_adjustments"] == [
        {"field": "limit_price", "sent": "62000.07", "normalized": "62000.1"}
    ]


@pytest.mark.asyncio
@respx.mock
async def test_tick_rounding_skipped_when_unresolved():
    # product lookup fails — the order must still go through with the price unchanged.
    respx.get(f"{INDIA_TESTNET_REST}/products/BTCUSD").mock(
        return_value=httpx.Response(500, json={"success": False, "error": {"code": "server_error"}})
    )
    route = respx.post(f"{INDIA_TESTNET_REST}/orders").mock(
        return_value=httpx.Response(200, json={"success": True, "result": {"id": 1}})
    )
    client = _client()
    out = await _call(
        client, "place_order",
        product_symbol="BTCUSD", size=1, side="buy", order_type="limit_order", limit_price="62000.07",
    )
    assert b'"limit_price":"62000.07"' in route.calls[0].request.content
    assert "price_adjustments" not in out[1]


# ------------------------------------------------------- transport-failure safety


@pytest.mark.asyncio
@respx.mock
async def test_a_mutation_is_never_resent_after_a_transport_failure():
    """POST accepted, response lost, automatic re-POST — the duplicate-order path.

    The status-code retry paths were always GET-only; the transport-error path was
    not, and would resend a mutation whose outcome is unknown.
    """
    route = respx.post(f"{INDIA_TESTNET_REST}/orders").mock(
        side_effect=[
            httpx.ReadTimeout("response never arrived"),
            httpx.Response(200, json={"success": True, "result": {"id": 7}}),
        ]
    )
    with pytest.raises(DeltaApiError) as err:
        await _client().post("/orders", {"product_id": 27, "size": 1}, auth=True)
    assert route.call_count == 1
    assert err.value.code == "execution_outcome_unknown"


@pytest.mark.asyncio
@respx.mock
async def test_a_mutation_connect_failure_says_nothing_was_sent():
    """A connect failure provably sent nothing, so its error says a retry is safe."""
    route = respx.post(f"{INDIA_TESTNET_REST}/orders").mock(
        side_effect=httpx.ConnectError("no route to host")
    )
    with pytest.raises(DeltaApiError) as err:
        await _client().post("/orders", {"product_id": 27, "size": 1}, auth=True)
    assert route.call_count == 1
    assert err.value.code == "upstream_unreachable"


@pytest.mark.asyncio
@respx.mock
async def test_audit_records_an_unknown_outcome(tmp_path, monkeypatch):
    """An ambiguous transport failure must reach the audit log.

    The raw httpx error bypassed _finish's DeltaApiError catch entirely, so the one
    mutation whose exchange outcome is uncertain was also the one that left no
    audit trace.
    """
    monkeypatch.setenv("DELTA_MCP_AUDIT_FILE", str(tmp_path / "audit.log"))
    monkeypatch.setattr(audit_log, "_INSTANCE", None)
    cfg = Config(
        env="india_testnet", base_url=INDIA_TESTNET_REST,
        api_key="k1", api_secret="s1", mode="trade",
    )
    audit = audit_log.configure(cfg)
    assert audit is not None

    respx.post(f"{INDIA_TESTNET_REST}/orders").mock(
        side_effect=httpx.ReadTimeout("response never arrived")
    )
    with pytest.raises(Exception, match="execution_outcome_unknown"):
        await _call(_client(), "place_order", audit=audit,
                    product_id=27, size=1, side="buy", order_type="market_order")

    lines = (tmp_path / "audit.log").read_text().splitlines()
    assert len(lines) == 1
    assert "execution_outcome_unknown" in json.loads(lines[0])["error"]
