"""Skills load from package data and publish on all three surfaces."""

import asyncio

import pytest

from delta_exchange_mcp import config as config_mod
from delta_exchange_mcp import skills
from delta_exchange_mcp.server import build_server


def _cfg(**over):
    base = {"env": "india_prod", "base_url": config_mod.INDIA_PROD_REST}
    return config_mod.Config(**{**base, **over})


PUBLIC_CFG = _cfg()
AUTH_CFG = _cfg(api_key="k", api_secret="s")


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


def test_credential_skills_are_hidden_without_keys():
    gated = {s.name for s in skills.discover() if s.requires == skills.CREDENTIALS}
    public_names = {s.name for s in skills.available(PUBLIC_CFG)}
    auth_names = {s.name for s in skills.available(AUTH_CFG)}

    assert gated.isdisjoint(public_names)
    assert gated.issubset(auth_names)
    assert public_names.issubset(auth_names)


def test_at_least_one_skill_needs_no_credentials():
    """A public-data-only install must still get something."""
    assert skills.available(PUBLIC_CFG)


# --- server wiring -------------------------------------------------------


def _server(cfg):
    return build_server(cfg)


def test_instructions_are_sent_to_the_client():
    mcp = _server(PUBLIC_CFG)
    assert mcp.instructions
    assert "list_skills" in mcp.instructions


def test_every_available_skill_has_a_resource():
    mcp = _server(AUTH_CFG)
    uris = {str(r.uri) for r in asyncio.run(mcp.list_resources())}
    for skill in skills.available(AUTH_CFG):
        assert skill.uri in uris
        for rel in skill.files:
            assert f"{skill.uri}/{rel}" in uris


def test_gated_skill_resources_are_absent_without_keys():
    uris = {str(r.uri) for r in asyncio.run(_server(PUBLIC_CFG).list_resources())}
    for skill in skills.discover():
        if skill.requires == skills.CREDENTIALS:
            assert skill.uri not in uris


def test_each_skill_gets_a_prompt():
    mcp = _server(AUTH_CFG)
    names = {p.name for p in asyncio.run(mcp.list_prompts())}
    assert names == {s.prompt_name for s in skills.available(AUTH_CFG)}
    assert all("-" not in n for n in names), "prompt names must be slash-command safe"


def test_resources_declare_useful_mime_types():
    """A skill URI has no extension; it must still announce itself as markdown."""
    # list_resources returns protocol objects, whose field is mimeType.
    by_uri = {
        str(r.uri): r.mimeType for r in asyncio.run(_server(AUTH_CFG).list_resources())
    }
    for skill in skills.available(AUTH_CFG):
        assert by_uri[skill.uri] == "text/markdown"
        for rel in skill.files:
            expected = "text/html" if rel.endswith(".html") else "text/markdown"
            assert by_uri[skill.uri + "/" + rel] == expected


def test_skill_tools_are_registered():
    names = {t.name for t in asyncio.run(_server(PUBLIC_CFG).list_tools())}
    assert {"list_skills", "get_skill"}.issubset(names)


# --- get_skill -----------------------------------------------------------


def _get_skill_fn(cfg):
    """Pull the registered closure back out so error paths are testable."""
    mcp = _server(cfg)
    return mcp._tool_manager.get_tool("get_skill").fn


def test_get_skill_returns_the_body():
    fn = _get_skill_fn(PUBLIC_CFG)
    first = skills.available(PUBLIC_CFG)[0]
    assert fn(name=first.name) == first.body


def test_get_skill_rejects_unknown_name():
    fn = _get_skill_fn(PUBLIC_CFG)
    with pytest.raises(ValueError, match="unknown skill"):
        fn(name="no-such-skill")


def test_get_skill_rejects_traversal_path():
    """`path` is a key into a dict built at discovery, so traversal cannot resolve."""
    fn = _get_skill_fn(PUBLIC_CFG)
    first = skills.available(PUBLIC_CFG)[0]
    for attempt in ("../../config.py", "/etc/passwd", "references/../../server.py"):
        with pytest.raises(ValueError, match="has no file"):
            fn(name=first.name, path=attempt)


def test_get_skill_rejects_gated_skill_without_keys():
    """A hidden skill is not reachable by guessing its name."""
    gated = [s for s in skills.discover() if s.requires == skills.CREDENTIALS]
    if not gated:
        pytest.skip("no credential-gated skills shipped")
    fn = _get_skill_fn(PUBLIC_CFG)
    with pytest.raises(ValueError, match="unknown skill"):
        fn(name=gated[0].name)


def test_list_skills_matches_what_is_available():
    mcp = _server(AUTH_CFG)
    listed = mcp._tool_manager.get_tool("list_skills").fn()
    assert {s["name"] for s in listed["skills"]} == {
        s.name for s in skills.available(AUTH_CFG)
    }
    for entry in listed["skills"]:
        assert entry["uri"].startswith(skills.URI_PREFIX)


def test_supporting_files_are_reachable():
    fn = _get_skill_fn(AUTH_CFG)
    for skill in skills.available(AUTH_CFG):
        for rel, text in skill.files.items():
            assert fn(name=skill.name, path=rel) == text
