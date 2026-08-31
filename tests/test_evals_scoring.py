"""Offline contracts for the deterministic tool-selection gate."""

import pytest

from evals.agent import ToolCall, Transcript, TurnRecord
from evals.dataset import ANY, CASES, MUTATING_TOOLS, Case, Expect, Turn
from evals.scoring import check
from delta_exchange_mcp.server import build_server


def _case(case_id: str) -> Case:
    return next(case for case in CASES if case.id == case_id)


def _expect(case_id: str, tool: str) -> Expect:
    return next(
        expected
        for turn in _case(case_id).turns
        for expected in turn.expect
        if expected.name == tool
    )


def _call(name: str, args: dict[str, object]) -> ToolCall:
    return ToolCall(name=name, args=args, result={}, is_error=False)


def _transcript(
    case: Case,
    calls_by_turn: tuple[tuple[ToolCall, ...], ...],
) -> Transcript:
    return Transcript(
        available_tools=[],
        turns=[
            TurnRecord(prompt=turn.prompt, reply="done", calls=list(calls))
            for turn, calls in zip(case.turns, calls_by_turn, strict=True)
        ],
    )


def test_every_case_uses_typed_turn_policies() -> None:
    assert len(CASES) == 35
    for case in CASES:
        assert case.turns
        assert all(isinstance(turn, Turn) for turn in case.turns)
        for turn in case.turns:
            expected_mutations = {
                expected.name
                for expected in turn.expect
                if expected.name in MUTATING_TOOLS
            }
            assert turn.allowed_reads.isdisjoint(MUTATING_TOOLS)
            assert turn.forbidden_mutations == MUTATING_TOOLS - expected_mutations
            if case.mode == "read":
                assert turn.forbidden_mutations == MUTATING_TOOLS


def test_read_case_model_rejects_an_expected_mutation() -> None:
    with pytest.raises(ValueError, match="read cases cannot permit mutations"):
        Case(
            id="unsafe-read",
            mode="read",
            turns=(
                Turn(
                    prompt="read only",
                    expect=(Expect("place_order"),),
                    forbidden_mutations=MUTATING_TOOLS,
                ),
            ),
        )


async def test_dataset_contracts_match_registered_tool_schemas() -> None:
    app = build_server()
    try:
        tools = {tool.name: tool for tool in await app.list_tools()}
    finally:
        await app.close_live_client()

    for case in CASES:
        for turn in case.turns:
            policy_names = (
                turn.allowed_reads
                | turn.forbidden_reads
                | turn.forbidden_mutations
            )
            assert policy_names <= tools.keys(), case.id
            for expected in turn.expect:
                assert expected.name in tools, case.id
                properties = tools[expected.name].input_schema.get("properties", {})
                assert expected.args.keys() <= properties.keys(), case.id


def test_prompt_literals_are_exact_dataset_contracts() -> None:
    assert _expect("candles_basic", "get_candles").args["resolution"] == "15m"
    assert _expect("products_perps", "list_products").args["contract_types"] == [
        "perpetual_futures"
    ]
    assert _expect("settlement_prices", "get_settlement_prices").args[
        "contract_types"
    ] == ["futures"]
    assert _expect("post_only_limit", "place_order").args["limit_price"] == "50000"
    assert _expect("margin_add", "adjust_position_margin").args["delta_margin"] == "5"
    assert _expect("set_leverage", "set_product_leverage").args["leverage"] == "10"
    assert _expect("positions_single", "get_positions").args["product_id"] is ANY
    assert _expect("fills_not_history", "get_fills").args["product_ids"] == [ANY]
    assert _expect("entry_with_bracket", "place_order").args[
        "bracket_stop_loss_price"
    ] == "60000"
    assert _expect("flow_leverage_check_then_set", "set_product_leverage").args[
        "leverage"
    ] == "5"
    assert _expect("flow_book_then_post_only", "place_order").args[
        "limit_price"
    ] is ANY


@pytest.mark.parametrize("wrong_margin", ["500", "-5"])
def test_wrong_prompt_literal_does_not_satisfy_a_mutation(wrong_margin: str) -> None:
    case = _case("margin_add")
    transcript = _transcript(
        case,
        (
            (
                _call(
                    "adjust_position_margin",
                    {"product_id": 27, "delta_margin": wrong_margin},
                ),
            ),
        ),
    )

    passed, failures = check(case, transcript)

    assert not passed
    assert any("delta_margin='5'" in failure for failure in failures)
    assert any("forbidden mutation" in failure for failure in failures)


def test_null_does_not_satisfy_a_derived_argument() -> None:
    case = _case("set_leverage")
    transcript = _transcript(
        case,
        (
            (
                _call(
                    "set_product_leverage",
                    {"product_id": None, "leverage": "10"},
                ),
            ),
        ),
    )

    passed, failures = check(case, transcript)

    assert not passed
    assert any("product_id=<any>" in failure for failure in failures)


def test_nested_price_contract_allows_extra_valid_fields() -> None:
    case = _case("bracket_on_position")
    transcript = _transcript(
        case,
        (
            (
                _call(
                    "place_bracket_order",
                    {
                        "product_symbol": "ETHUSD",
                        "take_profit_order": {
                            "order_type": "market_order",
                            "stop_price": "3000",
                        },
                        "stop_loss_order": {
                            "order_type": "market_order",
                            "stop_price": "2000",
                        },
                    },
                ),
            ),
        ),
    )

    assert check(case, transcript) == (True, [])


def test_wrong_nested_price_does_not_satisfy_a_bracket_mutation() -> None:
    case = _case("bracket_on_position")
    transcript = _transcript(
        case,
        (
            (
                _call(
                    "place_bracket_order",
                    {
                        "product_symbol": "ETHUSD",
                        "take_profit_order": {"stop_price": "3000"},
                        "stop_loss_order": {"stop_price": "2100"},
                    },
                ),
            ),
        ),
    )

    passed, failures = check(case, transcript)

    assert not passed
    assert any("stop_price': '2000'" in failure for failure in failures)
    assert "turn 1: forbidden mutation called: place_bracket_order" in failures


@pytest.mark.parametrize("mutation", sorted(MUTATING_TOOLS))
def test_read_case_rejects_every_unrelated_mutation(mutation: str) -> None:
    case = _case("ticker_basic")
    transcript = _transcript(
        case,
        (
            (
                _call("get_ticker", {"symbol": "BTCUSD"}),
                _call(mutation, {}),
            ),
        ),
    )

    passed, failures = check(case, transcript)

    assert not passed
    assert f"turn 1: forbidden mutation called: {mutation}" in failures


def test_only_declared_supporting_reads_are_permitted() -> None:
    case = _case("options_chain")
    permitted = _transcript(
        case,
        (
            (
                _call("list_products", {"contract_types": ["call_options"]}),
                _call(
                    "get_options_chain",
                    {"underlying": "BTC", "expiry_date": "05-09-2026"},
                ),
            ),
        ),
    )
    unexpected = _transcript(
        case,
        (
            (
                _call("get_ticker", {"symbol": "BTCUSD"}),
                _call(
                    "get_options_chain",
                    {"underlying": "BTC", "expiry_date": "05-09-2026"},
                ),
            ),
        ),
    )

    assert check(case, permitted) == (True, [])
    passed, failures = check(case, unexpected)
    assert not passed
    assert "turn 1: unexpected supporting read called: get_ticker" in failures


def test_mutation_on_an_earlier_turn_cannot_satisfy_a_later_request() -> None:
    case = _case("flow_check_then_reduce")
    transcript = _transcript(
        case,
        (
            (
                _call("get_ticker", {"symbol": "BTCUSD"}),
                _call(
                    "place_order",
                    {
                        "product_symbol": "BTCUSD",
                        "side": "sell",
                        "order_type": "market_order",
                        "size": 2,
                        "reduce_only": True,
                    },
                ),
            ),
            (),
        ),
    )

    passed, failures = check(case, transcript)

    assert not passed
    assert "turn 1: forbidden mutation called: place_order" in failures
    assert any("turn 2: expected place_order" in failure for failure in failures)


def test_correct_calls_pass_on_their_own_turns() -> None:
    case = _case("flow_check_then_reduce")
    transcript = _transcript(
        case,
        (
            (_call("get_ticker", {"symbol": "BTCUSD"}),),
            (
                _call(
                    "place_order",
                    {
                        "product_symbol": "BTCUSD",
                        "side": "sell",
                        "order_type": "market_order",
                        "size": 2,
                        "reduce_only": True,
                    },
                ),
            ),
        ),
    )

    assert check(case, transcript) == (True, [])


def test_duplicate_mutation_is_not_permitted_by_one_expectation() -> None:
    case = _case("margin_add")
    mutation = _call(
        "adjust_position_margin",
        {"product_id": 27, "delta_margin": "5"},
    )
    transcript = _transcript(case, ((mutation, mutation),))

    passed, failures = check(case, transcript)

    assert not passed
    assert failures == [
        "turn 1: forbidden mutation called: adjust_position_margin"
    ]
