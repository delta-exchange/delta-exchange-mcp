"""Generate the bundle's pyproject.toml and manifest.json from the repo's own metadata.

Anything that must agree with the published package — name, version, licence, URLs, the
Python floor, the dependency ceilings — is read from the repo's `pyproject.toml`, so the
bundle cannot drift from what it packages. Only the copy aimed at the person installing
the bundle is literal here; that is deliberately different text from the PyPI summary,
which is written for developers.
"""

import asyncio
import json
import os
import pathlib
import sys
import tempfile
import tomllib

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]

DISPLAY_NAME = "Delta Exchange"
PUBLISHER = "Delta Exchange"
KEYWORDS = ["trading", "crypto", "options", "futures", "market-data"]


SHORT_DESCRIPTION = "Live market data and your Delta Exchange India account."

LONG_DESCRIPTION = (
    "Ask about Delta Exchange India in plain English: live prices, option chains, "
    "order books, funding and open-interest history, plus your own positions, orders, "
    "fills and balances.\n\n"
    "**Read-only unless you change Mode.** Left at `read`, this cannot place, change or "
    "cancel orders, and can never move funds. Set Mode to `trade` and it can place, edit "
    "and cancel real orders on the environment you selected — there is no size limit, and "
    "orders are sized in contracts rather than coins. Every mutation is written to an "
    "audit log under `~/.delta-exchange-mcp/audit/`. Leave this at `read` unless you "
    "specifically want an assistant trading your account.\n\n"
    "**Both credential fields or neither.** A key without its matching secret is ignored "
    "and you get market data only — the two are always used together. Trading additionally "
    "needs a key with trading permission, not just Read Data.\n\n"
    "**Market data needs no setup** — leave the API key and secret empty and everything "
    "except your own account still works.\n\n"
    "**To see your account**, create a key at delta.exchange under Account → API Keys with "
    "the **Read Data** permission. Both halves are shown only once, at creation. Paste them "
    "into Configure. Your key is stored by this app and is sent only to Delta's API, from "
    "your own machine.\n\n"
    "**Environment** must be `india_prod` for the real exchange, or `india_testnet` for the "
    "practice site at demo.delta.exchange. A key only works against the site it was made on."
)

# Claude Desktop renders each description twice: as help text under the label AND as the
# input placeholder, where anything past ~60 characters is truncated mid-word. Keep these
# short enough to read cleanly in both roles; the detail lives in LONG_DESCRIPTION.
USER_CONFIG = {
    "api_key": {
        "type": "string",
        "title": "API key",
        "description": "Optional — leave empty for market data only.",
        "sensitive": True,
        "required": False,
    },
    "api_secret": {
        "type": "string",
        "title": "API secret",
        "description": "Required if you filled in the key above.",
        "sensitive": True,
        "required": False,
    },
    "environment": {
        "type": "string",
        "title": "Environment",
        "description": "india_prod (real) or india_testnet (practice).",
        "default": "india_prod",
        "required": True,
    },
    # There is no enum type in user_config — only string, number, boolean, directory and
    # file — so this is a string the user edits, exactly like environment above. It
    # defaults to read: arming order placement has to be a thing someone chose to type.
    "mode": {
        "type": "string",
        "title": "Mode",
        "description": "read (default), or trade to allow placing orders.",
        "default": "read",
        "required": True,
    },
}


def project() -> dict:
    """The repo's own [project] table — the single source of truth for shared metadata."""
    with (REPO / "pyproject.toml").open("rb") as f:
        proj = tomllib.load(f)["project"]
    # render_manifest copies this straight through, and the manifest schema wants an SPDX
    # string. PEP 639 spells it that way; the older `{ text = "MIT" }` table would land in
    # the JSON as a nested object and fail validation with a message that names the field
    # and not the cause. Checked here because this is the one place the file is read.
    if not isinstance(proj.get("license"), str):
        raise SystemExit(
            f"[project].license must be an SPDX string, got {proj.get('license')!r} — "
            "the manifest schema has no place for the table form"
        )
    return proj


def wheel_name(proj: dict) -> str:
    return f"{proj['name'].replace('-', '_')}-{proj['version']}-py3-none-any.whl"


def render_pyproject(proj: dict) -> str:
    """The bundle's own project file, pinned to the vendored wheel.

    The dependency ceilings are copied from the repo rather than restated. mcp 2.0 removed
    `mcp.server.fastmcp`, so a bundle that resolved above the repo's ceiling would die at
    import — and one that pinned below a raised ceiling would too.
    """
    deps = [f"{proj['name']}=={proj['version']}", *proj["dependencies"]]
    rendered = ",\n".join(f'    "{d}"' for d in deps)
    return (
        "# Generated by make_bundle.py — edit that, not this.\n"
        "[project]\n"
        f'name = "{proj["name"]}-bundle"\n'
        f'version = "{proj["version"]}"\n'
        f'requires-python = "{proj["requires-python"]}"\n'
        f"dependencies = [\n{rendered},\n]\n"
        "\n[tool.uv.sources]\n"
        f'{proj["name"]} = {{ path = "wheels/{wheel_name(proj)}" }}\n'
    )


async def tool_entries() -> list[dict[str, str]]:
    """Introspect the server to list every tool the bundle can register.

    Every DELTA_ variable is cleared before the ones that matter are set, so the manifest
    depends only on the source being packaged and never on the shell the build ran in.
    Forcing a named few was not enough: a developer with DELTA_MCP_DEBUG=1 exported got a
    different manifest than CI produced, and CI then rejected it as stale. DELTA_MCP_ENV
    leaked the same way, where an invalid value in the shell failed the build inside `load()`.

    Every optional surface is then forced ON, because the declared list has to be the
    *superset*. `tools_generated` is false, which promises the runtime never exposes anything
    beyond this list, so anything a user can switch on has to already be in it:

    * Trade mode, reached through the install form's Mode field.
    * Debug, which registers `get_debug_status`. Nothing in the manifest declares
      DELTA_MCP_DEBUG, and the manifest env is applied *over* the user's environment, so an
      exported DELTA_MCP_DEBUG=1 reaches an installed bundle untouched — measured: 28 tools
      registered against 41 declared, with `get_debug_status` registered but undeclared.

    Declaring DELTA_MCP_DEBUG="" in the manifest would also stop that, but it would take
    away the only way a bundle user can turn debug logging on at all — and that log is how
    they capture wire-level evidence for a bug report, since a bundle has no config file to
    edit. Listing the tool costs nothing by comparison: `tools_generated: false` promises a
    ceiling, not an exact set, which is already how the 13 mutating tools are handled.
    """
    for name in [name for name in os.environ if name.startswith("DELTA_")]:
        del os.environ[name]

    # One scratch directory for everything the introspection writes, removed on the way out.
    # A bare mkdtemp is removed by nobody, so every build would leave a directory behind for
    # the operating system to reap eventually — trading files left in the home directory for
    # files left in /tmp, which is not the fix it looks like.
    with tempfile.TemporaryDirectory(prefix="mcpb-manifest-") as scratch:
        os.environ.update({
            "DELTA_MCP_MODE": "trade",
            "DELTA_MCP_ENV": "india_prod",
            "DELTA_MCP_DEBUG": "1",
            "DELTA_MCP_DEBUG_FILE": str(pathlib.Path(scratch) / "debug.log"),
            "DELTA_API_KEY": "placeholder",
            "DELTA_API_SECRET": "placeholder",
            # Trade mode plus credentials is what opens the audit log, and listing tool
            # names mutates nothing worth auditing. Left on, every manifest build dropped
            # another empty file into ~/.delta-exchange-mcp/audit/, which is where 4,493
            # of them came from.
            "DELTA_MCP_AUDIT": "off",
            # Clearing the variables above is not enough for this one: cleared, it falls
            # back to ~/.delta-exchange-mcp/config.env and the build reads the developer's
            # own settings. Measured: a DELTA_MCP_DEBUG=1 line in that file put
            # get_debug_status into the manifest by a second route, before this listed it.
            # It also stops a build creating a file in a home directory it has no business
            # touching.
            "DELTA_MCP_CONFIG_FILE": str(pathlib.Path(scratch) / "config.env"),
        })
        from delta_exchange_mcp import debug_log
        from delta_exchange_mcp.server import build_server

        try:
            tools = await build_server().list_tools()
        finally:
            # Debug is intentionally on for manifest introspection. Its FileHandler points
            # inside scratch and must be closed before TemporaryDirectory removes it.
            debug_log.shutdown()

    return [
        {
            "name": t.name,
            "description": (t.description or "").strip().splitlines()[0].strip(),
        }
        for t in sorted(tools, key=lambda t: t.name)
    ]


def render_manifest(proj: dict, tools: list[dict[str, str]]) -> dict:
    urls = proj.get("urls", {})
    return {
        "manifest_version": "0.4",
        "name": proj["name"],
        "display_name": DISPLAY_NAME,
        "version": proj["version"],
        "description": SHORT_DESCRIPTION,
        "long_description": LONG_DESCRIPTION,
        "author": {"name": PUBLISHER, "url": urls["Homepage"]},
        "repository": {"type": "git", "url": urls["Repository"]},
        "homepage": urls["Homepage"],
        "documentation": urls["Documentation"],
        "support": urls["Issues"],
        "icon": "icon.png",
        "license": proj["license"],
        "keywords": KEYWORDS,
        "server": {
            "type": "uv",
            "entry_point": "server/main.py",
            "mcp_config": {
                "command": "uv",
                "args": [
                    "run",
                    "--directory",
                    "${__dirname}",
                    "--frozen",
                    "python",
                    "server/main.py",
                ],
                "env": {
                    # Declared, not omitted. The host substitutes this from the form, whose
                    # default is read, so an ambient DELTA_MCP_MODE=trade in the environment
                    # the app was launched with cannot arm trading behind the user's back.
                    "DELTA_MCP_MODE": "${user_config.mode}",
                    "DELTA_MCP_ENV": "${user_config.environment}",
                    "DELTA_API_KEY": "${user_config.api_key}",
                    "DELTA_API_SECRET": "${user_config.api_secret}",
                },
            },
        },
        "tools": tools,
        "tools_generated": False,
        "user_config": USER_CONFIG,
        "compatibility": {
            "claude_desktop": ">=0.10.0",
            "platforms": ["darwin", "win32", "linux"],
            "runtimes": {"python": proj["requires-python"]},
        },
    }


def main() -> None:
    what = sys.argv[1]
    proj = project()

    if what == "version":
        print(proj["version"])
    elif what == "wheel-name":
        print(wheel_name(proj))
    elif what == "pyproject":
        (HERE / "pyproject.toml").write_text(render_pyproject(proj))
        print(f"wrote pyproject.toml (deps from {REPO.name}/pyproject.toml)")
    elif what == "manifest":
        tools = asyncio.run(tool_entries())
        out = HERE / "manifest.json"
        out.write_text(json.dumps(render_manifest(proj, tools), indent=2) + "\n")
        print(f"wrote manifest.json ({len(tools)} tools, version {proj['version']})")
    else:
        raise SystemExit(f"unknown target: {what}")


if __name__ == "__main__":
    main()
