"""server.json is the MCP registry's own manifest; it goes stale silently since nothing

imports it. These checks catch a version bump that touched pyproject.toml but not
server.json (or vice versa) before it reaches a tag push, where release.yml's guard job
would otherwise be the first thing to notice — see RELEASING.md.
"""

import json
import re
import tomllib
from pathlib import Path

REPO = Path(__file__).parent.parent


def _pyproject() -> dict:
    with (REPO / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)["project"]


def _server_json() -> dict:
    with (REPO / "server.json").open("rb") as f:
        return json.load(f)


def _readme_mcp_name() -> str:
    text = (REPO / "README.md").read_text()
    match = re.search(r"mcp-name:\s*(\S+?)(?=\s|-->)", text)
    assert match, "README.md is missing its `mcp-name:` marker"
    return match.group(1)


def test_server_json_version_matches_pyproject():
    assert _server_json()["version"] == _pyproject()["version"]


def test_server_json_package_version_matches_pyproject():
    packages = _server_json()["packages"]
    assert len(packages) == 1
    assert packages[0]["version"] == _pyproject()["version"]


def test_server_json_name_matches_readme_marker():
    assert _server_json()["name"] == _readme_mcp_name()


def test_server_json_package_identifier_matches_pyproject_name():
    packages = _server_json()["packages"]
    assert packages[0]["identifier"] == _pyproject()["name"]


def test_server_json_package_is_pypi_stdio():
    package = _server_json()["packages"][0]
    assert package["registryType"] == "pypi"
    assert package["transport"]["type"] == "stdio"


def test_server_json_description_fits_the_registry_limit():
    # The official registry schema caps description at 100 characters.
    assert len(_server_json()["description"]) <= 100
