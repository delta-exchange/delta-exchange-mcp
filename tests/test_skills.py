"""Skills load from package data and publish on all three surfaces."""

import ast
import re

import pytest
from jsonschema import validate
from mcp.client import Client
from mcp.types import TextContent

from delta_exchange_mcp import skills
from delta_exchange_mcp.auth.store import CredentialState
from delta_exchange_mcp.server import build_server
from tests.connection_support import service, verified


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


# --- discovery -----------------------------------------------------------


def test_every_shipped_skill_is_well_formed():
    found = skills.discover()
    assert found, "no skills discovered — is skills_data missing from the package?"
    for skill in found:
        assert skill.name and skill.name == skill.name.strip()
        assert skill.description, f"{skill.name} has no description"
        assert skill.requires in (skills.PUBLIC, skills.CREDENTIALS)
        assert skill.body.lstrip().startswith("#"), f"{skill.name} body has no heading"
        assert skill.uri == f"skill://delta/{skill.name}"


def test_pnl_analytics_dropped_views_stay_dropped():
    """Expiry/DTE, what-ifs, projections and achievements were cut on purpose.

    The views live in three files that must stay coherent — a formula returning
    in metrics.md without its dashboard panel (or the reverse) ships a skill
    that promises what it cannot render.
    """
    skill = next(s for s in skills.discover() if s.name == "pnl-analytics")
    corpus = {"SKILL.md": skill.body, **skill.files}
    for name, text in corpus.items():
        low = text.lower()
        for banned in (
            "p-expiry",
            "what_ifs",
            "what-if",
            "## projections",
            "dte bucket",
        ):
            assert banned not in low, f"{name} still mentions {banned!r}"
    assert "seven views" in corpus["references/metrics.md"].lower()


def test_pnl_skill_uses_the_shipped_calculator_contract() -> None:
    skill = next(s for s in skills.discover() if s.name == "pnl-analytics")
    assert "delta-exchange-pnl --input" in skill.body
    assert "references/contract.md" in skill.files
    assert "delta.pnl.input.v1" in skill.files["references/contract.md"]


def test_position_risk_uses_delta_for_option_direction() -> None:
    skill = next(s for s in skills.discover() if s.name == "position-risk")
    assert "index_price * delta" in skill.body
    assert "report directional net as `n/a`" in skill.body


@pytest.fixture
async def app(monkeypatch):
    monkeypatch.delenv("DELTA_API_KEY", raising=False)
    monkeypatch.delenv("DELTA_API_SECRET", raising=False)
    server = build_server(connection_service=service(verified))
    try:
        yield server
    finally:
        await server.close_live_client()




async def test_funding_procedure_call_satisfies_the_tool_schema(app):
    async with Client(app, mode="auto") as client:
        skill = next(item for item in skills.discover() if item.name == "funding-carry")
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
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        validate(arguments, tools["get_funding_history"].input_schema)


def test_at_least_one_skill_needs_no_credentials():
    assert any(skill.requires == skills.PUBLIC for skill in skills.discover())


async def test_instructions_are_sent_to_the_client(app):
    assert "list_skills" in app.instructions


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_every_skill_and_supporting_file_is_readable_without_keys(app, mode):
    async with Client(app, mode=mode) as client:
        listed = {
            str(resource.uri): resource
            for resource in (await client.list_resources()).resources
        }
        for skill in skills.discover():
            expected_files = {"": skill.body, **skill.files}
            for path, text in expected_files.items():
                uri = f"{skill.uri}/{path}" if path else skill.uri
                expected_mime = "text/html" if path.endswith(".html") else "text/markdown"
                assert listed[uri].mime_type == expected_mime
                result = await client.read_resource(uri)
                assert result.contents[0].text == text


async def test_each_skill_has_a_usable_prompt_without_keys(app):
    async with Client(app, mode="auto") as client:
        names = {prompt.name for prompt in (await client.list_prompts()).prompts}
        assert names == {skill.prompt_name for skill in skills.discover()}
        assert all("-" not in name for name in names)
        for skill in skills.discover():
            result = await client.get_prompt(skill.prompt_name)
            assert skill.name in result.messages[0].content.text
            assert "get_skill" in result.messages[0].content.text


async def test_skill_tools_are_read_only_and_local(app):
    async with Client(app, mode="auto") as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        for name in ("list_skills", "get_skill"):
            assert tools[name].annotations.read_only_hint is True
            assert tools[name].annotations.open_world_hint is False


async def test_get_skill_returns_every_procedure_without_keys(app):
    async with Client(app, mode="auto") as client:
        for skill in skills.discover():
            result = await client.call_tool("get_skill", {"name": skill.name})
            assert not result.is_error
            assert result.content == [TextContent(type="text", text=skill.body)]


async def test_get_skill_rejects_unknown_name(app):
    async with Client(app, mode="auto") as client:
        result = await client.call_tool("get_skill", {"name": "no-such-skill"})
        assert result.is_error
        assert "unknown skill" in result.content[0].text


@pytest.mark.parametrize(
    "path", ["../../config.py", "/etc/passwd", "references/../../server.py"]
)
async def test_get_skill_rejects_traversal_path(app, path):
    async with Client(app, mode="auto") as client:
        first = skills.discover()[0]
        result = await client.call_tool("get_skill", {"name": first.name, "path": path})
        assert result.is_error
        assert "has no file" in result.content[0].text


async def test_list_skills_includes_the_requirement_for_each_procedure(app):
    async with Client(app, mode="auto") as client:
        result = await client.call_tool("list_skills", {})
        listed = result.structured_content["skills"]
        assert {item["name"] for item in listed} == {item.name for item in skills.discover()}
        for entry in listed:
            assert entry["uri"].startswith(skills.URI_PREFIX)
            expected = next(skill for skill in skills.discover() if skill.name == entry["name"])
            assert entry["requires"] == expected.requires


async def test_supporting_files_are_reachable_by_tool(app):
    async with Client(app, mode="auto") as client:
        for skill in skills.discover():
            for path, text in skill.files.items():
                result = await client.call_tool("get_skill", {"name": skill.name, "path": path})
                assert not result.is_error
                assert result.content == [TextContent(type="text", text=text)]


async def test_account_setup_does_not_change_the_skill_catalog(app):
    async with Client(app, mode="auto") as client:
        async def catalog() -> tuple[set[str], set[str], dict[str, object]]:
            resources = await client.list_resources(cache_mode="refresh")
            prompts = await client.list_prompts(cache_mode="refresh")
            listed = await client.call_tool("list_skills", {})
            return (
                {str(resource.uri) for resource in resources.resources},
                {prompt.name for prompt in prompts.prompts},
                listed.structured_content,
            )

        before = await catalog()
        connection = app.connection_service
        environment = connection.client.config.env
        connection.credentials.replace(
            environment, "test-key", "test-secret", state=CredentialState.VERIFIED
        )
        connected = await client.call_tool("get_connection_status", {})
        assert connected.structured_content["credentials_configured"] is True
        assert await catalog() == before
        connection.credentials.delete(environment)
        disconnected = await client.call_tool("get_connection_status", {})
        assert disconnected.structured_content["credentials_configured"] is False
        assert await catalog() == before
