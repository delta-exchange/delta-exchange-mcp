"""Browser-only connection page behavior."""

import json
import re
import subprocess
from pathlib import Path

from delta_exchange_mcp import config, form


def test_browser_script_parses_in_strict_mode() -> None:
    html = form.page_html("/rpc", nonce="test-nonce")
    script = re.search(r"<script[^>]*>(.*?)</script>", html, re.DOTALL)
    assert script is not None

    result = subprocess.run(
        ["node", "--check"],
        input=script.group(1),
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr


def test_browser_script_uses_the_final_response_after_listener_shutdown() -> None:
    result = subprocess.run(
        ["node", str(Path(__file__).with_name("browser_flow.cjs"))],
        input=form.page_html("/rpc", nonce="test-nonce"),
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr


def test_browser_page_posts_secrets_only_to_the_loopback_action_endpoint() -> None:
    html = form.page_html(
        "/one-use/rpc",
        csrf_token="csrf-value",
        revision={"credential": 3, "consent": 4},
    )

    assert 'endpoint": "/one-use/rpc"' in html
    assert 'headers: { "Content-Type": "application/json" }' in html
    assert "api_key: key.value.trim()" in html
    assert "api_secret: secret.value.trim()" in html
    assert 'request("tools/call"' not in html
    assert "save_credentials" not in html
    assert "save_mode" not in html


def test_page_configuration_is_secret_free_and_uses_shared_environment_urls() -> None:
    html = form.page_html(
        "/rpc",
        csrf_token="csrf-value",
        revision={"credential": 1},
    )
    match = re.search(r"var CONFIG = (\{.*?\});", html)
    assert match is not None
    settings = json.loads(match.group(1))

    assert settings["endpoint"] == "/rpc"
    assert settings["csrf_token"] == "csrf-value"
    assert settings["revision"] == {"credential": 1}
    assert settings["dashboards"] == config.DASHBOARDS
    assert "api_key" not in settings
    assert "api_secret" not in settings


def test_devnet_is_hidden_until_an_external_devnet_connection_is_active() -> None:
    html = form.page_html("/rpc", nonce="test-nonce")

    assert '"value": "india_devnet"' in html
    assert '"hidden": true' in html
    assert "environmentLabels[environment].hidden = false" in html


def test_page_has_labelled_keyboard_accessible_controls_and_live_status() -> None:
    html = form.VIEW_HTML

    for control in ("key", "secret"):
        assert f'for="{control}"' in html
        assert f'id="{control}"' in html
    assert 'role="status"' in html
    assert 'aria-live="polite"' in html
    assert 'type="button"' in html
    assert ":focus-visible" in html
    assert "@media (max-width: 34rem)" in html


def test_production_acknowledgement_starts_unchecked_and_is_required_twice() -> None:
    html = form.VIEW_HTML

    assert '<input id="acknowledge" type="checkbox">' in html
    assert "acknowledge.checked = false" in html
    assert 'selectedEnvironment() === "india_prod" && !acknowledge.checked' in html
    assert "acknowledged: acknowledge.checked" in html


def test_selecting_inactive_testnet_does_not_snap_back_to_production() -> None:
    """The local radio change renders details without syncing from active status."""
    html = form.VIEW_HTML

    render = re.search(
        r"function render\(status, syncSelection\) \{(.*?)\n  \}",
        html,
        re.DOTALL,
    )
    assert render is not None
    assert "if (syncSelection && status.environment)" in render.group(1)

    change = re.search(
        r'envs\.addEventListener\("change", function \(\) \{(.*?)\n  \}\);',
        html,
        re.DOTALL,
    )
    assert change is not None
    assert "render(current, false)" in change.group(1)
    assert "selectEnvironment(status.environment)" not in change.group(1)


def test_trading_controls_are_disabled_for_an_inactive_selection() -> None:
    html = form.VIEW_HTML

    assert "var selectedIsActive = selected.active === true" in html
    assert (
        'disabled = busy || !selectedIsActive || !selected.connected || trading.enabled'
        in html
    )
    assert "Use this environment before you enable trading." in html
    assert "(!selected.connected && !selected.credential_metadata_present)" in html


def test_page_has_rotate_disconnect_and_no_legacy_setup_language() -> None:
    html = form.VIEW_HTML

    assert "Connect or rotate" in html
    assert ">Disconnect<" in html
    assert "file edit" not in html.lower()
    assert "restart" not in html.lower()
    assert "trading mode" not in html.lower()


def test_nonce_is_applied_to_the_only_style_and_script_blocks() -> None:
    html = form.page_html("/rpc", nonce="nonce-value")

    assert html.count('nonce="nonce-value"') == 2
    assert "__NONCE_ATTR__" not in html


def test_build_id_is_a_short_content_digest() -> None:
    assert re.fullmatch(r"[0-9a-f]{10}", form.build_id())
