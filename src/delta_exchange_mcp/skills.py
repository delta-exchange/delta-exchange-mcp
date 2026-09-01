"""Skills: written procedures shipped as package data and served over MCP.

A tool exposes one endpoint. A skill teaches the multi-step job — which tools to
call in which order, the formulas to apply, and the shape of the answer. Each one
lives in `skills_data/<name>/SKILL.md` with optional `references/` and `assets/`
files alongside it.

Every skill is published three ways, because clients differ in what they read:

* as resources under `skill://delta/<name>`, for clients that browse resources;
* through the `list_skills` / `get_skill` tools, for clients that only call tools;
* as a prompt per skill, which surfaces as a slash command in Claude Code.

All three surfaces live in this one module on purpose. The `mcp` 2.x migration
moves the resource and prompt decorators, and one file is cheaper to fix than
four.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.resources import FunctionResource
from pydantic import AnyUrl

from delta_exchange_mcp import config as config_mod

DATA_DIR = "skills_data"
URI_PREFIX = "skill://delta/"

# Skills that read the user's account are hidden without credentials, mirroring
# how `account.register` is gated. A skill nobody can run is worse than absent:
# the model will try it and blame the failure on the exchange.
PUBLIC = "public"
CREDENTIALS = "credentials"

_MIME = {".md": "text/markdown", ".html": "text/html", ".json": "application/json"}


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    requires: str
    body: str
    # Relative POSIX path ("references/algorithm.md") -> file text. Built once at
    # discovery, so `get_skill` is a dict lookup and path traversal cannot happen.
    files: dict[str, str] = field(default_factory=dict)

    @property
    def uri(self) -> str:
        return f"{URI_PREFIX}{self.name}"

    @property
    def prompt_name(self) -> str:
        return self.name.replace("-", "_")


class Catalog:
    """The skill definitions and their live credential entitlement."""

    def __init__(self, shipped: list[Skill], has_credentials: bool) -> None:
        self._shipped = tuple(shipped)
        self._by_name = {skill.name: skill for skill in shipped}
        self._by_prompt = {skill.prompt_name: skill for skill in shipped}
        self._has_credentials = has_credentials

    def set_credentials(self, present: bool) -> bool:
        """Update the entitlement and report whether the visible catalog changed."""
        changed = present != self._has_credentials
        self._has_credentials = present
        return changed

    def available(self) -> list[Skill]:
        """Skills allowed by the current credential entitlement."""
        return [
            skill
            for skill in self._shipped
            if skill.requires != CREDENTIALS or self._has_credentials
        ]

    @property
    def shipped(self) -> tuple[Skill, ...]:
        """Every packaged skill, independent of the live entitlement."""
        return self._shipped

    def get(self, name: str) -> Skill | None:
        skill = self._by_name.get(name)
        if skill is None or (
            skill.requires == CREDENTIALS and not self._has_credentials
        ):
            return None
        return skill

    def allows_uri(self, uri: str) -> bool:
        """Whether a skill resource URI is visible in the current entitlement."""
        if not uri.startswith(URI_PREFIX):
            return True
        name = uri.removeprefix(URI_PREFIX).split("/", 1)[0]
        return self.get(name) is not None

    def allows_prompt(self, name: str) -> bool:
        """Whether a registered prompt is visible in the current entitlement."""
        skill = self._by_prompt.get(name)
        return skill is None or self.get(skill.name) is not None


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split leading `---` frontmatter from the body.

    Deliberately not a YAML parser. Skill frontmatter is flat `key: value` lines,
    and the package has no YAML dependency worth adding for three keys.
    """
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    meta: dict[str, str] = {}
    for line in text[4:end].splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip().strip("'\"")
    body = text[end + len("\n---") :].lstrip("\n")
    return meta, body


def _read_files(skill_dir) -> dict[str, str]:
    """Read `references/` and `assets/` one level deep, in stable order."""
    out: dict[str, str] = {}
    for sub in sorted(skill_dir.iterdir(), key=lambda p: p.name):
        if not sub.is_dir() or sub.name not in ("references", "assets"):
            continue
        for item in sorted(sub.iterdir(), key=lambda p: p.name):
            if item.is_file():
                out[f"{sub.name}/{item.name}"] = item.read_text(encoding="utf-8")
    return out


def discover() -> list[Skill]:
    """Every skill shipped in the package, sorted by name."""
    root = resources.files("delta_exchange_mcp").joinpath(DATA_DIR)
    if not root.is_dir():
        return []

    skills: list[Skill] = []
    for skill_dir in sorted(root.iterdir(), key=lambda p: p.name):
        entry = skill_dir.joinpath("SKILL.md")
        if not skill_dir.is_dir() or not entry.is_file():
            continue
        meta, body = _split_frontmatter(entry.read_text(encoding="utf-8"))
        name = meta.get("name") or skill_dir.name
        skills.append(
            Skill(
                name=name,
                description=meta.get("description", ""),
                requires=meta.get("requires", PUBLIC),
                body=body,
                files=_read_files(skill_dir),
            )
        )
    return skills


def available(cfg: config_mod.Config) -> list[Skill]:
    """Skills the current configuration can actually run."""
    return [s for s in discover() if s.requires != CREDENTIALS or cfg.has_credentials]


def _mime_for(path: str) -> str:
    """Markdown by default: a skill's own URI carries no file extension."""
    dot = path.rfind(".")
    return _MIME.get(path[dot:], "text/plain") if dot != -1 else "text/markdown"


def _add_resource(
    mcp: FastMCP, uri: str, name: str, description: str, text: str
) -> None:
    mcp.add_resource(
        FunctionResource(
            uri=AnyUrl(uri),
            name=name,
            description=description,
            mime_type=_mime_for(uri),
            # Default-argument capture: a bare closure over the loop variable
            # would give every resource the last skill's text.
            fn=lambda captured=text: captured,
        )
    )


def register(mcp: FastMCP, cfg: config_mod.Config) -> Catalog:
    """Publish the available skills as resources, tools, and prompts."""
    catalog = Catalog(discover(), cfg.has_credentials)

    for skill in catalog.shipped:
        _add_resource(mcp, skill.uri, skill.name, skill.description, skill.body)
        for rel, text in skill.files.items():
            _add_resource(
                mcp,
                f"{skill.uri}/{rel}",
                f"{skill.name}/{rel}",
                f"Supporting file for the {skill.name} skill.",
                text,
            )

    @mcp.tool()
    def list_skills() -> dict[str, object]:
        """The procedures this server knows how to run, and when to use each.

        Call this before answering any multi-step question about trading
        performance, open positions, risk, or funding. Then call `get_skill` on
        the match and follow it — the skill carries the method, the formulas, and
        the output shape.
        """
        return {
            "skills": [
                {
                    "name": s.name,
                    "description": s.description,
                    "uri": s.uri,
                    "files": sorted(s.files),
                }
                for s in catalog.available()
            ],
            "hint": "Call get_skill(name) for the full procedure.",
        }

    @mcp.tool()
    def get_skill(name: str, path: str | None = None) -> str:
        """The full text of a skill, or of one of its supporting files.

        Pass `name` alone for the procedure itself. Pass `path` — one of the
        entries in that skill's `files` list, such as `references/algorithm.md` —
        for a supporting file. Read the skill first; it says which files matter.
        """
        skill = catalog.get(name)
        if skill is None:
            raise ValueError(
                f"unknown skill {name!r}; available: "
                f"{sorted(s.name for s in catalog.available()) or 'none'}"
            )
        if path is None:
            return skill.body
        if path not in skill.files:
            raise ValueError(
                f"{name!r} has no file {path!r}; available: {sorted(skill.files) or 'none'}"
            )
        return skill.files[path]

    for skill in catalog.shipped:
        _register_prompt(mcp, skill)
    return catalog


def _register_prompt(mcp: FastMCP, skill: Skill) -> None:
    """One slash command per skill. The prompt is a doorway, not a copy.

    Duplicating the skill text here would double the maintenance and put a long
    block in the client's prompt list, so it points at the resource instead.
    """

    def run() -> str:
        return (
            f"Run the Delta Exchange `{skill.name}` skill. "
            f"First call get_skill(name='{skill.name}') to load the procedure, "
            "then follow it exactly, including its output format."
        )

    run.__name__ = skill.prompt_name
    run.__doc__ = skill.description
    mcp.prompt(name=skill.prompt_name, description=skill.description)(run)
