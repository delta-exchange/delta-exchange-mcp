"""Skills: written procedures shipped as package data and served over MCP.

A tool exposes one endpoint. A skill teaches the multi-step job — which tools to
call in which order, the formulas to apply, and the shape of the answer. Each one
lives in `skills_data/<name>/SKILL.md` with optional `references/` and `assets/`
files alongside it.

Every skill is published three ways, because clients differ in what they read:

* as resources under `skill://delta/<name>`, for clients that browse resources;
* through the `list_skills` / `get_skill` tools, for clients that only call tools;
* as a prompt per skill, which surfaces as a slash command in Claude Code.

A skill whose frontmatter declares `requires: credentials` describes account access
it will tell the model to use, not a lock on its own text — but its resources and
prompt are not registered until a key is present, so a client browsing before setup
sees only what it can actually run. `server.py` arms and disarms that gated set in
the same place it arms and disarms the account and trading tools, so a credential
saved through the in-chat form brings the matching skills up hot, with no restart.
"""

from dataclasses import dataclass, field
from importlib import resources
from importlib.resources.abc import Traversable

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.resources import TextResource

DATA_DIR = "skills_data"
URI_PREFIX = "skill://delta/"

# Requirements describe the tools a procedure calls; they do not hide its text once its
# surfaces are registered.
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
    """The packaged procedures, their supporting files, and which gated ones are live."""

    def __init__(self, shipped: list[Skill]) -> None:
        self._shipped = tuple(shipped)
        self._by_name = {skill.name: skill for skill in shipped}
        # Names of `requires: credentials` skills currently registered as resources and
        # a prompt. Empty until `arm_gated_skills` runs; a public skill needs no entry
        # here because it is always registered.
        self._armed_gated: set[str] = set()

    @property
    def shipped(self) -> tuple[Skill, ...]:
        """Every packaged skill in discovery order, gated or not."""
        return self._shipped

    @property
    def public(self) -> tuple[Skill, ...]:
        return tuple(s for s in self._shipped if s.requires != CREDENTIALS)

    @property
    def gated(self) -> tuple[Skill, ...]:
        return tuple(s for s in self._shipped if s.requires == CREDENTIALS)

    def visible(self) -> tuple[Skill, ...]:
        """Every skill `list_skills` and `get_skill` may serve right now."""
        return tuple(
            s
            for s in self._shipped
            if s.requires != CREDENTIALS or s.name in self._armed_gated
        )

    def get(self, name: str) -> Skill | None:
        """Find a currently-visible packaged procedure by its exact name."""
        skill = self._by_name.get(name)
        if skill is None:
            return None
        if skill.requires == CREDENTIALS and skill.name not in self._armed_gated:
            return None
        return skill


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


def _read_files(skill_dir: Traversable) -> dict[str, str]:
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


def _mime_for(path: str) -> str:
    """Markdown by default: a skill's own URI carries no file extension."""
    dot = path.rfind(".")
    return _MIME.get(path[dot:], "text/plain") if dot != -1 else "text/markdown"


def _add_resource(
    mcp: FastMCP, uri: str, name: str, description: str, text: str
) -> None:
    mcp.add_resource(
        TextResource(
            uri=uri,
            name=name,
            description=description,
            mime_type=_mime_for(uri),
            text=text,
        )
    )


def _remove_resource(mcp: FastMCP, uri: str) -> None:
    # FastMCP 1.27's ResourceManager has no public removal call — only ToolManager does
    # (`mcp.remove_tool`, used the same way in server.py's `disarm_authenticated`).
    # Resources are stored in a plain dict keyed by the string URI, so popping it is the
    # whole operation and cannot leave a partial resource behind.
    mcp._resource_manager._resources.pop(uri, None)


def _remove_prompt(mcp: FastMCP, name: str) -> None:
    # Same gap as _remove_resource, for PromptManager's `_prompts` dict.
    mcp._prompt_manager._prompts.pop(name, None)


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


def _register_skill_surfaces(mcp: FastMCP, skill: Skill) -> None:
    """Publish one skill's resource, its supporting files, and its prompt."""
    _add_resource(mcp, skill.uri, skill.name, skill.description, skill.body)
    for rel, text in skill.files.items():
        _add_resource(
            mcp,
            f"{skill.uri}/{rel}",
            f"{skill.name}/{rel}",
            f"Supporting file for the {skill.name} skill.",
            text,
        )
    _register_prompt(mcp, skill)


def _remove_skill_surfaces(mcp: FastMCP, skill: Skill) -> None:
    _remove_resource(mcp, skill.uri)
    for rel in skill.files:
        _remove_resource(mcp, f"{skill.uri}/{rel}")
    _remove_prompt(mcp, skill.prompt_name)


def register(mcp: FastMCP) -> Catalog:
    """Publish every ungated skill as resources, tools, and prompts.

    Called once at startup, before credentials are known. Gated skills join later
    through `arm_gated_skills`, which the caller wires to the same reconciliation that
    arms the account and trading tools.
    """
    catalog = Catalog(discover())

    for skill in catalog.public:
        _register_skill_surfaces(mcp, skill)

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
                    "requires": s.requires,
                    "uri": s.uri,
                    "files": sorted(s.files),
                }
                for s in catalog.visible()
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
            raise ToolError(
                f"unknown skill {name!r}; available: "
                f"{sorted(s.name for s in catalog.visible()) or 'none'}"
            )
        if path is None:
            return skill.body
        if path not in skill.files:
            raise ToolError(
                f"{name!r} has no file {path!r}; available: {sorted(skill.files) or 'none'}"
            )
        return skill.files[path]

    return catalog


def arm_gated_skills(mcp: FastMCP, catalog: Catalog) -> bool:
    """Register every `requires: credentials` skill's resources and prompt.

    Idempotent: a skill already armed is left alone. Returns whether anything
    changed, so the caller only sends a resource/prompt list-changed notification
    when one is warranted.
    """
    changed = False
    for skill in catalog.gated:
        if skill.name in catalog._armed_gated:
            continue
        _register_skill_surfaces(mcp, skill)
        catalog._armed_gated.add(skill.name)
        changed = True
    return changed


def disarm_gated_skills(mcp: FastMCP, catalog: Catalog) -> bool:
    """Remove every currently-armed gated skill's resources and prompt.

    Mirrors `arm_gated_skills`. Returns whether anything changed.
    """
    changed = False
    for skill in catalog.gated:
        if skill.name not in catalog._armed_gated:
            continue
        _remove_skill_surfaces(mcp, skill)
        catalog._armed_gated.discard(skill.name)
        changed = True
    return changed
