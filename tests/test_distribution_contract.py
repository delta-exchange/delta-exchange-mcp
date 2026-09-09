"""End-user surfaces must describe the request-time authorization contract."""

import base64
import json
import re
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


REPO = Path(__file__).parents[1]


def _reference(readme: str, name: str) -> str:
    match = re.search(rf"^\[{re.escape(name)}\]: (.+)$", readme, re.MULTILINE)
    assert match is not None
    return match.group(1)


def _assert_credential_free(config: Mapping[str, object]) -> None:
    assert config.get("env") in (None, {})


def test_readme_uses_manage_connection_instead_of_legacy_mode() -> None:
    readme = (REPO / "README.md").read_text()

    assert "The server always advertises the same" in readme
    assert "one credential record for production and one for testnet" in readme
    assert "`DELTA_MCP_MODE=trade` has no authorization effect" in readme
    assert '"DELTA_MCP_MODE": "trade"' not in readme
    assert "--env DELTA_MCP_MODE=trade" not in readme
    assert "DELTA_MCP_MODE_<" not in readme
    assert "the key goes in **one file" not in readme


def test_bundle_has_no_install_time_secret_or_authorization_settings() -> None:
    manifest = json.loads((REPO / "packaging/mcpb/manifest.json").read_text())
    launch = manifest["server"]["mcp_config"]

    assert not manifest.get("user_config")
    assert not {
        "DELTA_API_KEY",
        "DELTA_API_SECRET",
        "DELTA_MCP_ENV",
        "DELTA_MCP_MODE",
    }.intersection(launch.get("env", {}))
    assert "Trading needs separate browser approval" in manifest["long_description"]


def test_install_links_do_not_request_credentials_or_mode() -> None:
    readme = (REPO / "README.md").read_text()
    cursor_url = _reference(readme, "cursor-link")
    cursor_payload = unquote(urlparse(cursor_url).query.partition("config=")[2])
    cursor = json.loads(base64.b64decode(cursor_payload))
    _assert_credential_free(cursor)

    query = parse_qs(urlparse(_reference(readme, "vs-code-link")).query)
    assert "inputs" not in query
    _assert_credential_free(json.loads(query["config"][0]))
