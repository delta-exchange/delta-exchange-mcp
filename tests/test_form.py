import json
import re
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver import MCPServer

from delta_exchange_mcp import config as config_mod
from delta_exchange_mcp import credentials, form, store

KEY = "typed-into-the-form-key"
SECRET = "typed-into-the-form-secret"


@pytest.fixture
def server():
    mcp = MCPServer("test")
    form.register(mcp)
    return mcp


async def opened(mcp):
    """A server whose form has been opened, which is the precondition for saving."""
    result = await mcp.call_tool("setup_credentials", {})
    mcp._test_save_grant = result.meta["ui"]["saveGrant"]
    return mcp


async def save(mcp, **overrides):
    args = {
        "environment": "india_testnet",
        "api_key": KEY,
        "api_secret": SECRET,
        "grant": getattr(mcp, "_test_save_grant", ""),
    }
    args.update(overrides)
    result = await mcp.call_tool("save_credentials", args)
    text = "".join(getattr(block, "text", "") for block in result.content)
    return result.structured_content, text


def checking(**kwargs):
    async def fake(env, key, secret):
        return credentials.Check(**kwargs)

    return fake


# --- the MCP Apps wiring -------------------------------------------------------------


async def test_the_opener_names_a_resource_the_server_actually_serves(server):
    """The association is two strings in two files; a rename in one shows up only here."""
    tool = next(t for t in await server.list_tools() if t.name == "setup_credentials")
    assert tool.meta["ui"]["resourceUri"] == form.VIEW_URI

    served = {str(r.uri) for r in await server.list_resources()}
    assert form.VIEW_URI in served


async def test_the_view_is_declared_as_an_app_rather_than_a_document(server):
    """Without the profile parameter a client treats the HTML as content, not a view."""
    resource = next(r for r in await server.list_resources() if str(r.uri) == form.VIEW_URI)
    assert resource.mime_type == "text/html;profile=mcp-app"


async def test_the_saving_tools_are_hidden_from_the_model(server):
    """The model must not be able to call either mutation behind the form."""
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert tools["save_credentials"].meta["ui"]["visibility"] == ["app"]
    assert tools["save_mode"].meta["ui"]["visibility"] == ["app"]


async def test_the_form_is_available_without_credentials(server):
    """Someone with no key is exactly who needs this, so it cannot be gated on having one."""
    assert config_mod.load().has_credentials is False
    assert "setup_credentials" in {t.name for t in await server.list_tools()}


# --- the view itself -----------------------------------------------------------------


def test_the_view_fetches_nothing_from_outside_itself():
    """Both hosts block external subresources; one CDN reference renders a blank frame."""
    offenders = re.findall(r"""(?:src|href)\s*=|@import|url\(""", form.VIEW_HTML)
    assert offenders == [], f"the view would try to load something: {offenders}"


def test_the_view_completes_the_handshake_a_host_waits_for():
    """A view that stops after ui/initialize stays collapsed with nothing rendered."""
    for step in ("ui/initialize", "ui/notifications/initialized", "ui/notifications/size-changed"):
        assert step in form.VIEW_HTML


def test_the_view_never_pushes_anything_into_the_model_context():
    """These two methods hand text to the model. Either one would defeat the whole point."""
    assert "ui/update-model-context" not in form.VIEW_HTML
    assert "ui/message" not in form.VIEW_HTML


def test_the_view_does_not_feature_test_on_the_ui_capability():
    """Claude Desktop 1.0.0 rendered MCP Apps without advertising the extension.

    Build 1.30096.5 does advertise it, read 2026-08-16, so the declaration is no longer
    missing everywhere. The rule stands anyway: gating would still turn the form off for
    anyone on the older build, and gating buys nothing. A view that is asked for and cannot
    be shown costs a collapsed frame; a view that is never asked for costs the feature.
    """
    assert "io.modelcontextprotocol/ui" not in form.VIEW_HTML


def test_the_view_measures_content_rather_than_the_frame_it_sits_in():
    """`documentElement.scrollHeight` in an iframe never returns less than the frame.

    So reporting it echoes back the current size and the frame can only ever grow: at
    frame heights 200/560/1200/2000 it reported 535/560/1200/2000 for content that was
    really 535. One long rejection message would leave the frame tall for the rest of the
    conversation. Forcing intrinsic sizing for the measurement is what lets it shrink.
    """
    assert "documentElement.scrollHeight" not in form.VIEW_HTML
    assert 'root.style.height = "max-content"' in form.VIEW_HTML
    assert "getBoundingClientRect().height" in form.VIEW_HTML


def test_the_view_reads_the_host_context_it_asked_for():
    """The theme and the palette arrive in the `ui/initialize` result and in a notification.

    A listener written only for replies and for host-initiated requests drops the
    notification, because it has a method and no id and so matches neither.
    """
    assert "hostContext" in form.VIEW_HTML
    assert "ui/notifications/host-context-changed" in form.VIEW_HTML


def test_the_view_restores_the_live_environment_and_can_save_mode_without_a_key():
    """Reopening must not default to prod or require resubmitting the stored secret."""
    assert "selectEnv(now.environment)" in form.VIEW_HTML
    assert "currentEnvironment = now.environment" in form.VIEW_HTML
    assert 'name: modeOnly ? "save_mode" : "save_credentials"' in form.VIEW_HTML


async def test_the_resource_says_who_draws_the_box(server):
    """Omitting this left Claude Desktop drawing one and the view drawing a second inside.

    `preferredSize` is not a field in the spec at all; the height is what the view reports,
    so declaring it did nothing while reading as though it set the frame.
    """
    resource = next(r for r in await server.list_resources() if str(r.uri) == form.VIEW_URI)
    assert resource.meta["ui"]["prefersBorder"] is True
    assert "preferredSize" not in json.dumps(resource.meta)


def test_the_view_installs_the_font_rules_the_host_hands_it():
    """`hostContext.styles.css.fonts` carries the host's own `@font-face` rules.

    The spec makes installing them the app's job. A view that reads the palette but drops
    these ends up with `--font-sans` naming a family its frame never loaded, so it silently
    renders in a substituted face — and every measurement it reports is taken against that.
    """
    assert "styles.css && styles.css.fonts" in form.VIEW_HTML
    assert "hostFonts.textContent = fonts" in form.VIEW_HTML


def test_the_view_names_no_type_size_of_its_own():
    """Sizes tuned against an assumed base are what made the form look wrong in Codex.

    Every size comes from the host's typography tokens, so spacing expressed in em follows
    whatever the client actually runs. A px literal in a font declaration is that assumption
    coming back.
    """
    style = re.search(r"<style>(.*?)</style>", form.VIEW_HTML, re.S).group(1)
    sized = re.findall(r"font(?:-size)?\s*:[^;}]*", style)
    offenders = [d.strip() for d in sized if "px" in d]
    assert offenders == [], f"these name a size instead of asking for one: {offenders}"


def _contrast(one: str, two: str) -> float:
    """WCAG relative-luminance ratio between two `#rrggbb` colours."""

    def luminance(colour: str) -> float:
        raw = [int(colour.lstrip("#")[i : i + 2], 16) / 255 for i in (0, 2, 4)]
        lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in raw]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

    high, low = sorted((luminance(one), luminance(two)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_the_view_carries_delta_s_own_brand_colours():
    """Surfaces follow the host, but the accent has to be Delta's or it is a generic form.

    The mark paints its own fills rather than reading a variable, so it is asserted by the
    colour it actually draws. Delta's undarkened #fe6c02 is deliberately not a variable:
    white text on it measures 2.85:1, and a declared variable is exactly how it would find
    its way back onto a control. The stylesheet records the value in a comment instead.
    """
    # The official mark, inlined because nothing may be fetched.
    assert 'viewBox="0 0 53 52"' in form.VIEW_HTML
    assert "#FD7D02" in form.VIEW_HTML
    # The interactive accent: --brand-india-bg-primary walked toward black until white text
    # is readable on it. The ratio itself is enforced by the contrast test below.
    assert "--brand-strong: #c45302" in form.VIEW_HTML
    assert re.search(r"--brand:\s*#", form.VIEW_HTML) is None
    assert "var(--brand)" not in form.VIEW_HTML


def test_every_colour_that_carries_text_is_readable_in_light_mode():
    """Delta's orange is the mark's colour, and it is too light to put white text on.

    Measured against white it is 2.85:1, short of the 4.5:1 that text needs and even of the
    3:1 a control's edge needs. A logo is exempt from both, so the mark keeps it and every
    interactive fill takes a darkened stop instead. Reading the values out of the stylesheet
    rather than restating them here is what makes this catch a change to either one.
    """
    tokens = dict(re.findall(r"(--(?:brand-strong|brand-strong-hover)):\s*(#[0-9a-f]{6})", form.VIEW_HTML))
    assert set(tokens) == {"--brand-strong", "--brand-strong-hover"}, tokens
    for name, colour in tokens.items():
        ratio = _contrast(colour, "#ffffff")
        assert ratio >= 4.5, f"{name} is {colour}, only {ratio:.2f}:1 against white text"

    # The light stop of each semantic pair, which is prose on the form's own background.
    for name in ("positive", "negative"):
        light = re.search(rf"--{name}:[^;]*light-dark\((#[0-9a-f]{{6}}),", form.VIEW_HTML)
        assert light, f"--{name} no longer names a light stop"
        ratio = _contrast(light.group(1), "#ffffff")
        assert ratio >= 4.5, f"--{name} light is {light.group(1)}, only {ratio:.2f}:1 on white"


def test_the_view_carries_the_dashboards_the_rest_of_the_package_uses():
    """The key page URL is opened from here and printed by `login`; one copy, not two."""
    injected = json.loads(
        re.search(r"var CONFIG = (\{.*?\});", form.VIEW_HTML, re.S).group(1)
    )
    assert injected["dashboards"] == config_mod.DASHBOARDS
    assert injected["default_environment"] == config_mod.DEFAULT_ENV
    # Every choice offered must be one the "Open the API keys page" button can act on.
    assert {e["value"] for e in injected["environments"]} <= set(config_mod.DASHBOARDS)


# --- saving --------------------------------------------------------------------------


async def test_saving_is_refused_until_the_form_has_been_opened(server):
    """A client that ignores ui.visibility exposes this tool to the model.

    Requiring the form first means the model cannot overwrite a stored key without the
    user watching a form appear.
    """
    structured, _ = await save(server)
    assert structured["status"] == "refused"
    assert config_mod.load().has_credentials is False


async def test_the_save_grant_is_metadata_only(server):
    """The one-use capability reaches the app but never the model-visible result."""
    result = await server.call_tool("setup_credentials", {})
    grant = result.meta["ui"]["saveGrant"]
    visible = json.dumps(
        {
            "content": [block.model_dump(mode="json") for block in result.content],
            "structuredContent": result.structured_content,
        }
    )
    assert grant
    assert grant not in visible


async def test_a_wrong_grant_does_not_consume_the_real_one(server, monkeypatch):
    monkeypatch.setattr(credentials, "check", checking(ok=True, reachable=True, detail=""))
    await opened(server)
    refused, _ = await save(server, grant="not-the-issued-grant")
    assert refused["status"] == "refused"

    accepted, _ = await save(server)
    assert accepted["status"] == "saved"


async def test_a_successful_save_consumes_its_grant(server, monkeypatch):
    monkeypatch.setattr(credentials, "check", checking(ok=True, reachable=True, detail=""))
    await opened(server)
    accepted, _ = await save(server)
    replayed, _ = await save(server)
    assert accepted["status"] == "saved"
    assert replayed["status"] == "refused"


async def test_an_expired_grant_is_refused(server, monkeypatch):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(form, "time", SimpleNamespace(monotonic=lambda: clock.now))
    await opened(server)
    clock.now += 601

    structured, _ = await save(server)
    assert structured["status"] == "refused"


async def test_a_key_delta_rejects_is_not_saved(server, monkeypatch):
    monkeypatch.setattr(
        credentials,
        "check",
        checking(ok=False, reachable=True, detail="delta api error: InvalidApiKey"),
    )
    structured, _ = await save(await opened(server))
    assert structured["status"] == "rejected"
    assert "InvalidApiKey" in structured["message"]
    assert config_mod.load().has_credentials is False


async def test_an_unreachable_api_still_saves(server, monkeypatch):
    """A flaky connection must not cost someone a key they typed correctly."""
    monkeypatch.setattr(
        credentials, "check", checking(ok=False, reachable=False, detail="timeout")
    )
    structured, _ = await save(await opened(server))
    assert structured["status"] == "unverified"
    assert config_mod.load().has_credentials is True


async def test_half_a_pair_is_refused(server, monkeypatch):
    monkeypatch.setattr(credentials, "check", checking(ok=True, reachable=True, detail=""))
    structured, _ = await save(await opened(server), api_secret="   ")
    assert structured["status"] == "invalid"
    assert config_mod.load().has_credentials is False


async def test_an_unknown_environment_is_refused(server, monkeypatch):
    monkeypatch.setattr(credentials, "check", checking(ok=True, reachable=True, detail=""))
    structured, _ = await save(await opened(server), environment="mainnet")
    assert structured["status"] == "invalid"
    assert config_mod.load().has_credentials is False


async def test_a_verified_key_is_saved_with_its_environment(server, monkeypatch):
    """The environment is part of what makes the key work, so it is written with it."""
    monkeypatch.setattr(
        credentials, "check", checking(ok=True, reachable=True, detail="someone@delta.exchange")
    )
    structured, _ = await save(await opened(server))
    assert structured["status"] == "saved"

    cfg = config_mod.load()
    assert (cfg.api_key, cfg.api_secret) == (KEY, SECRET)
    assert cfg.env == "india_testnet"


async def test_a_clean_save_reports_its_facts_as_fields_not_only_as_a_sentence(
    server, monkeypatch
):
    """The view renders its own connected state, so it needs the parts, not the paragraph.

    `message` stays alongside them because a client that renders no view has nothing else
    to show, and neither may ever carry the key or the secret.
    """
    monkeypatch.setattr(
        credentials, "check", checking(ok=True, reachable=True, detail="someone@delta.exchange")
    )
    structured, _ = await save(await opened(server))

    assert structured["account"] == "someone@delta.exchange"
    assert structured["path"] == str(store.path())
    # This fixture registers the form with no `activate`, which is the branch that still
    # needs a restart; `test_activation.py` covers the one that does not.
    assert structured["next_step"] == (
        "Restart this client to use your account. Trading stays off for this client."
    )
    assert "someone@delta.exchange" in structured["message"]

    blob = json.dumps(structured)
    assert KEY not in blob and SECRET not in blob


async def test_saving_keeps_the_template_and_its_instructions(server, monkeypatch):
    """The file has to stay hand-editable after the form has written to it."""
    monkeypatch.setattr(credentials, "check", checking(ok=True, reachable=True, detail=""))
    await save(await opened(server))

    body = store.path().read_text()
    assert "Read Data" in body
    assert "DELTA_MCP_MODE=trade" in body  # the commented-out explanation survives


async def test_the_credentials_never_appear_in_anything_the_tool_returns(server, monkeypatch):
    """Tool results are the one channel from the view back towards the host.

    Echoing what was typed — even in an error — would undo the reason for typing it into
    a frame rather than into the chat.
    """
    for check in (
        checking(ok=True, reachable=True, detail="someone@delta.exchange"),
        checking(ok=False, reachable=True, detail="delta api error: InvalidApiKey"),
        checking(ok=False, reachable=False, detail="timeout"),
    ):
        monkeypatch.setattr(credentials, "check", check)
        structured, text = await save(await opened(server))
        assert KEY not in text and SECRET not in text
        assert KEY not in json.dumps(structured) and SECRET not in json.dumps(structured)


async def test_opening_the_form_reveals_nothing_and_offers_a_fallback(server):
    """The result is model-visible, so it must carry instructions and no secret."""
    result = await server.call_tool("setup_credentials", {})
    text = "".join(getattr(block, "text", "") for block in result.content)
    assert str(store.path()) in text
    assert "delta-exchange-mcp login" in text
    # The one instruction that matters: a model asked for help with a key will otherwise
    # offer to take it in the chat, and people accept.
    assert "never ask them to send a key" in text


def test_the_harness_can_still_find_the_touch_rules_it_injects():
    """`(pointer: coarse)` answers to the device, so no page can ask to be measured as one.

    `scripts/host.py` therefore lifts these declarations out of the view and injects them
    unwrapped, which is the only way to see the touch layout's height — and the touch
    layout is the one at risk, because larger targets are what make it taller. It was 517px
    against a 500px ceiling before anything could measure it. If this block is renamed or
    reformatted past the harness's pattern, the harness reports the mouse height under a
    control that says touch, which is worse than not having the control at all.

    It calls the harness rather than restating its pattern. A copy of the pattern here
    would still pass after the harness's own copy stopped matching, which is the one
    outcome this test exists to prevent.
    """
    path = Path(__file__).resolve().parent.parent / "scripts" / "host.py"
    assert "min-height: 44px" in runpy.run_path(str(path))["coarse_rules"]()
