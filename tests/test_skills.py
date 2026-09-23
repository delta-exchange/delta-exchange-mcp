"""Skills load from package data and publish on all three surfaces.

`requires: public` skills are there from the first `tools/list`; `requires: credentials`
skills join only once a key is present, and — the point of `test_credential_gating.py`'s
sibling below — join *hot*, mid-session, the same way the account and trading tools do.
"""

import ast
import re

import mcp.types as types
import pytest

from delta_exchange_mcp import config as config_mod
from delta_exchange_mcp import skills
from delta_exchange_mcp.server import build_server
from tests.test_activation import connected, save
from tests.test_activation import accepted as accepted  # re-exported fixture


# --- frontmatter parsing -------------------------------------------------


def test_splits_frontmatter_from_body():
    meta, body = skills._split_frontmatter(
        "---\nname: demo\ndescription: A thing\nrequires: public\n---\n\n# Demo\n\ntext\n"
    )
    assert meta == {"name": "demo", "description": "A thing", "requires": "public"}
    assert body.startswith("# Demo")


def test_body_without_frontmatter_is_returned_whole():
    meta, body = skills._split_frontmatter("# Demo\n\ntext\n")
    assert meta == {}
    assert body == "# Demo\n\ntext\n"


def test_unterminated_frontmatter_is_not_swallowed():
    """A missing closing fence must not eat the whole file."""
    raw = "---\nname: demo\n\n# Demo\n"
    meta, body = skills._split_frontmatter(raw)
    assert meta == {}
    assert body == raw


def test_colons_in_values_survive():
    meta, _ = skills._split_frontmatter(
        "---\ndescription: Ranks carry: annualised\n---\nx"
    )
    assert meta["description"] == "Ranks carry: annualised"


# --- discovery -------------------------------------------------------------


def test_every_shipped_skill_is_well_formed():
    found = skills.discover()
    assert found, "no skills discovered — is skills_data missing from the package?"
    for skill in found:
        assert skill.name and skill.name == skill.name.strip()
        assert skill.description, f"{skill.name} has no description"
        assert skill.requires in (skills.PUBLIC, skills.CREDENTIALS)
        assert skill.body.lstrip().startswith("#"), f"{skill.name} body has no heading"
        assert skill.uri == f"skill://delta/{skill.name}"


def test_at_least_one_skill_needs_no_credentials():
    assert any(skill.requires == skills.PUBLIC for skill in skills.discover())


def test_at_least_one_skill_needs_credentials():
    assert any(skill.requires == skills.CREDENTIALS for skill in skills.discover())


def test_pnl_skill_uses_the_shipped_calculator_contract():
    skill = next(s for s in skills.discover() if s.name == "pnl-analytics")
    assert "delta-exchange-pnl" in skill.body
    assert "references/contract.md" in skill.files
    assert "delta.pnl.input.v1" in skill.files["references/contract.md"]


def test_position_risk_uses_delta_for_option_direction():
    skill = next(s for s in skills.discover() if s.name == "position-risk")
    assert "index_price * delta" in skill.body
    assert "report directional net as `n/a`" in skill.body


def test_no_skill_mentions_removed_trading_controls():
    """DEA-881 removed dry runs, the audit log, and DELTA_MCP_MODE — a skill that still
    tells the model to reach for one of them would send it looking for a tool that does
    not exist."""
    for skill in skills.discover():
        corpus = skill.body.lower() + " ".join(skill.files.values()).lower()
        for banned in (
            "dry_run",
            "dry run",
            "audit log",
            "delta_mcp_mode",
            "trade mode",
        ):
            assert banned not in corpus, f"{skill.name} still mentions {banned!r}"


# --- catalog gating (no server involved) ------------------------------------


def test_catalog_hides_gated_skills_until_armed():
    catalog = skills.Catalog(skills.discover())
    gated = next(s for s in catalog.gated for _ in [0])
    assert gated.name not in {s.name for s in catalog.visible()}
    assert catalog.get(gated.name) is None


def test_catalog_arm_and_disarm_toggle_visibility():
    catalog = skills.Catalog(skills.discover())
    gated_names = {s.name for s in catalog.gated}
    assert gated_names, "no requires: credentials skill to test gating with"

    catalog._armed_gated.update(gated_names)
    assert gated_names <= {s.name for s in catalog.visible()}
    for name in gated_names:
        assert catalog.get(name) is not None

    catalog._armed_gated.clear()
    assert gated_names.isdisjoint({s.name for s in catalog.visible()})


# --- serving without a session (mirrors test_credential_gating.py) ---------


async def test_public_skill_surfaces_exist_with_no_credentials(monkeypatch):
    monkeypatch.delenv("DELTA_API_KEY", raising=False)
    monkeypatch.delenv("DELTA_API_SECRET", raising=False)
    mcp = build_server(config_mod.load())
    try:
        resources = {str(r.uri) for r in await mcp.list_resources()}
        prompts = {p.name for p in await mcp.list_prompts()}
        for skill in skills.discover():
            if skill.requires != skills.PUBLIC:
                continue
            assert skill.uri in resources
            assert skill.prompt_name in prompts
    finally:
        await mcp.close_live_client()


async def test_gated_skill_surfaces_are_absent_with_no_credentials(monkeypatch):
    monkeypatch.delenv("DELTA_API_KEY", raising=False)
    monkeypatch.delenv("DELTA_API_SECRET", raising=False)
    mcp = build_server(config_mod.load())
    try:
        resources = {str(r.uri) for r in await mcp.list_resources()}
        prompts = {p.name for p in await mcp.list_prompts()}
        for skill in skills.discover():
            if skill.requires != skills.CREDENTIALS:
                continue
            assert skill.uri not in resources
            assert skill.prompt_name not in prompts
    finally:
        await mcp.close_live_client()


async def test_gated_skill_surfaces_are_present_with_credentials_at_startup(
    monkeypatch,
):
    monkeypatch.setenv("DELTA_API_KEY", "k")
    monkeypatch.setenv("DELTA_API_SECRET", "s")
    mcp = build_server(config_mod.load())
    try:
        resources = {str(r.uri) for r in await mcp.list_resources()}
        prompts = {p.name for p in await mcp.list_prompts()}
        for skill in skills.discover():
            if skill.requires != skills.CREDENTIALS:
                continue
            assert skill.uri in resources
            assert skill.prompt_name in prompts
    finally:
        await mcp.close_live_client()


async def test_list_skills_hides_gated_skills_with_no_credentials(monkeypatch):
    monkeypatch.delenv("DELTA_API_KEY", raising=False)
    monkeypatch.delenv("DELTA_API_SECRET", raising=False)
    mcp = build_server(config_mod.load())
    try:
        result = await mcp.call_tool("list_skills", {})
        listed = {entry["name"] for entry in result[1]["skills"]}
        for skill in skills.discover():
            if skill.requires == skills.CREDENTIALS:
                assert skill.name not in listed
            else:
                assert skill.name in listed
    finally:
        await mcp.close_live_client()


async def test_get_skill_rejects_a_gated_skill_with_no_credentials(monkeypatch):
    monkeypatch.delenv("DELTA_API_KEY", raising=False)
    monkeypatch.delenv("DELTA_API_SECRET", raising=False)
    mcp = build_server(config_mod.load())
    try:
        gated = next(s for s in skills.discover() if s.requires == skills.CREDENTIALS)
        with pytest.raises(Exception, match="unknown skill"):
            await mcp.call_tool("get_skill", {"name": gated.name})
    finally:
        await mcp.close_live_client()


async def test_get_skill_returns_every_public_skill_with_no_credentials(monkeypatch):
    monkeypatch.delenv("DELTA_API_KEY", raising=False)
    monkeypatch.delenv("DELTA_API_SECRET", raising=False)
    mcp = build_server(config_mod.load())
    try:
        for skill in skills.discover():
            if skill.requires != skills.PUBLIC:
                continue
            content, _ = await mcp.call_tool("get_skill", {"name": skill.name})
            assert content[0].text == skill.body
    finally:
        await mcp.close_live_client()


async def test_get_skill_rejects_traversal_path(monkeypatch):
    monkeypatch.delenv("DELTA_API_KEY", raising=False)
    monkeypatch.delenv("DELTA_API_SECRET", raising=False)
    mcp = build_server(config_mod.load())
    try:
        first = next(s for s in skills.discover() if s.requires == skills.PUBLIC)
        with pytest.raises(Exception, match="has no file"):
            await mcp.call_tool(
                "get_skill", {"name": first.name, "path": "../../server.py"}
            )
    finally:
        await mcp.close_live_client()


async def test_instructions_mention_the_skill_tools(monkeypatch):
    monkeypatch.delenv("DELTA_API_KEY", raising=False)
    monkeypatch.delenv("DELTA_API_SECRET", raising=False)
    mcp = build_server(config_mod.load())
    try:
        assert "list_skills" in mcp.instructions
        assert "get_skill" in mcp.instructions
    finally:
        await mcp.close_live_client()


async def test_funding_procedure_call_satisfies_the_tool_schema(monkeypatch):
    """The example call embedded in the skill text must stay a real, valid call."""
    monkeypatch.delenv("DELTA_API_KEY", raising=False)
    monkeypatch.delenv("DELTA_API_SECRET", raising=False)
    mcp = build_server(config_mod.load())
    try:
        skill = next(s for s in skills.discover() if s.name == "funding-carry")
        example = re.search(r"`(get_funding_history\([^`]+\))`", skill.body)
        assert example is not None
        call = ast.parse(example.group(1), mode="eval").body
        assert isinstance(call, ast.Call)
        assert not call.args
        end = 1_789_000_000
        values = {"symbol": "BTCUSD", "start": end - 604800, "end": end}
        arguments = {
            keyword.arg: values[keyword.value.id]
            if isinstance(keyword.value, ast.Name)
            else ast.literal_eval(keyword.value)
            for keyword in call.keywords
        }
        tools = {t.name: t for t in await mcp.list_tools()}
        schema = tools["get_funding_history"].inputSchema
        for required in schema.get("required", []):
            assert required in arguments, f"example call is missing {required!r}"
        for key in arguments:
            assert key in schema["properties"], f"{key!r} is not a real parameter"
    finally:
        await mcp.close_live_client()


# --- hot arm/disarm over the real protocol ----------------------------------


async def test_a_saved_key_arms_the_gated_skills_without_a_restart(accepted):
    """The whole point: resources and a prompt appear that did not exist at connect time."""
    async with connected() as session:
        before_resources = {
            str(r.uri) for r in (await session.client.list_resources()).resources
        }
        before_prompts = {p.name for p in (await session.client.list_prompts()).prompts}
        assert "skill://delta/pnl-analytics" not in before_resources
        assert "position_risk" not in before_prompts

        result = await save(session)
        assert result["status"] == "saved"

        after_resources = {
            str(r.uri) for r in (await session.client.list_resources()).resources
        }
        after_prompts = {p.name for p in (await session.client.list_prompts()).prompts}
        assert "skill://delta/pnl-analytics" in after_resources
        assert "skill://delta/position-risk" in after_resources
        assert "pnl_analytics" in after_prompts
        assert "position_risk" in after_prompts

        listed = await session.call("list_skills")
        assert {"pnl-analytics", "position-risk", "funding-carry", "daily-market-brief"} <= {
            entry["name"] for entry in listed["skills"]
        }


async def test_a_saved_key_sends_resource_and_prompt_list_changed(accepted):
    async with connected() as session:
        await save(session)
        kinds = {type(n) for n in session.notifications}
        assert types.ResourceListChangedNotification in kinds
        assert types.PromptListChangedNotification in kinds


async def test_the_server_declares_that_resources_and_prompts_can_change():
    async with connected() as session:
        capabilities = session.initialized.capabilities
        assert capabilities.resources.listChanged is True
        assert capabilities.prompts.listChanged is True
