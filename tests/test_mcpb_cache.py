"""Executable-cache trust checks, without fetching or executing upstream code."""

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest


PACKAGING = Path(__file__).parents[1] / "packaging" / "mcpb"
SHA = "70fe3b34cd6dff1b3bba046638edc72a6467a4fb"
CLI = Path(SHA) / "dist" / "cli" / "cli.js"
pytestmark = pytest.mark.skipif(
    os.name != "posix" or shutil.which("bash") is None,
    reason="cache ownership regressions need POSIX and Bash",
)


def _write(path, text="fixture CLI"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def cache_helper():
    spec = importlib.util.spec_from_file_location("mcpb_cache", PACKAGING / "cache.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def harness(tmp_path):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    shared = tmp_path / "shared-temp"
    shared.mkdir(mode=0o1777)
    shared.chmod(0o1777)
    commands = tmp_path / "commands.jsonl"
    binaries = tmp_path / "bin"
    binaries.mkdir()
    shim = f"#!{sys.executable}\n" + '''
import json
import os
import sys
from pathlib import Path

command = Path(sys.argv[0]).name
arguments = sys.argv[1:]
if command == "uv":
    assert arguments[:3] == ["run", "--no-project", "python"]
    os.execv(sys.executable, [sys.executable, *arguments[3:]])
with Path(os.environ["TEST_COMMANDS"]).open("a") as log:
    log.write(json.dumps([command, *arguments]) + "\\n")
if command == "git" and arguments[2] == "checkout":
    checkout = Path(arguments[1])
    yarn = checkout / ".yarn/releases/yarn-fixture.cjs"
    yarn.parent.mkdir(parents=True)
    yarn.write_text("fixture yarn")
    tsc = checkout / "node_modules/.bin/tsc"
    tsc.parent.mkdir(parents=True)
    tsc.write_text("#!" + sys.executable + "\\n" + """
import os
from pathlib import Path
output = Path("dist/cli/cli.js")
output.parent.mkdir(parents=True)
if os.environ.get("TEST_UNSAFE_BUILD"):
    output.symlink_to(os.environ["TEST_UNSAFE_BUILD"])
else:
    output.write_text("fixture CLI")
""")
    tsc.chmod(0o700)
'''
    for command in ("uv", "node", "git"):
        _write(binaries / command, shim).chmod(0o700)
    env = {
        "PATH": str(binaries) + os.pathsep + os.defpath,
        "HOME": str(home),
        "TMPDIR": str(shared),
        "TEST_COMMANDS": str(commands),
    }
    return home, shared, commands, env


def _run(harness, cache=None):
    env = harness[3].copy()
    if cache is not None:
        env["MCPB_CLI_CACHE"] = str(cache)
    return subprocess.run(
        ["bash", str(PACKAGING / "mcpb_cli.sh")],
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )


def _commands(harness):
    path = harness[2]
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_default_cache_ignores_preplanted_shared_temp_tree(harness):
    home, shared, _, _ = harness
    planted = _write(shared / "mcpb-cli" / CLI, "attacker fixture")

    result = _run(harness)

    expected = home / ".cache/delta-exchange-mcp/mcpb-cli" / CLI
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == expected
    assert expected.read_text() == "fixture CLI"
    assert planted.read_text() == "attacker fixture"
    assert str(planted) not in json.dumps(_commands(harness))
    assert stat.S_IMODE(expected.parents[3].stat().st_mode) == 0o700
    fetch = next(command for command in _commands(harness) if "fetch" in command)
    assert fetch[-1] == SHA
    assert any(command[-2:] == ["install", "--immutable"] for command in _commands(harness))


def test_owned_override_reuses_cli_and_internal_dependency_symlinks(harness):
    cache = harness[0] / "workspace/.mcpb-cli"
    built = _write(cache / CLI)
    inode = built.stat().st_ino
    _write(cache / SHA / "node_modules/typescript/bin/tsc", "fixture compiler")
    link = cache / SHA / "node_modules/.bin/tsc"
    link.parent.mkdir()
    link.symlink_to("../typescript/bin/tsc")

    result = _run(harness, cache)

    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == built
    assert built.stat().st_ino == inode
    assert _commands(harness) == [["node", str(built), "--version"]]


def test_cold_build_is_reused_on_the_next_invocation(harness):
    cache = harness[0] / "cache with spaces"
    first = _run(harness, cache)
    before = _commands(harness)

    second = _run(harness, cache)

    assert first.returncode == second.returncode == 0, (first.stderr, second.stderr)
    assert first.stdout == second.stdout
    assert _commands(harness) == before + [["node", str(cache / CLI), "--version"]]


@pytest.mark.parametrize("entry", [Path(), Path(SHA), CLI.parent, CLI])
def test_writable_cache_entry_is_rejected_before_commands_or_cleanup(harness, entry):
    cache = harness[0] / "cache"
    built = _write(cache / CLI)
    target = cache / entry
    target.chmod(0o777 if target.is_dir() else 0o666)

    result = _run(harness, cache)

    assert result.returncode != 0
    assert "writable by other users" in result.stderr
    assert _commands(harness) == []
    assert built.read_text() == "fixture CLI"


def test_writable_parent_is_rejected_even_when_cache_is_private(harness):
    parent = harness[0] / "shared"
    cache = parent / "cache"
    built = _write(cache / CLI)
    cache.chmod(0o700)
    parent.chmod(0o777)

    result = _run(harness, cache)

    assert result.returncode != 0
    assert _commands(harness) == []
    assert built.read_text() == "fixture CLI"


@pytest.mark.parametrize("entry", [Path(SHA), CLI.parent, CLI])
def test_cache_symlink_escape_is_rejected_before_commands(harness, entry):
    cache = harness[0] / "cache"
    outside = harness[0] / "outside"
    _write(outside / "kept", "unrelated file")
    destination = outside / "cli.js" if entry == CLI else outside
    if entry == CLI:
        _write(destination)
    alias = cache / entry
    alias.parent.mkdir(parents=True)
    alias.symlink_to(destination, target_is_directory=entry != CLI)

    result = _run(harness, cache)

    assert result.returncode != 0
    assert "symlink leaves the cache" in result.stderr
    assert _commands(harness) == []
    assert (outside / "kept").read_text() == "unrelated file"
    assert alias.is_symlink()


@pytest.mark.parametrize("unsafe_side", ["source", "target"])
def test_cache_alias_checks_both_parent_paths(harness, unsafe_side):
    home = harness[0]
    source = home / "source"
    source.mkdir()
    target = home / "target"
    cache = target / "cache"
    _write(cache / CLI)
    alias = source / "alias"
    alias.symlink_to(cache, target_is_directory=True)
    (source if unsafe_side == "source" else target).chmod(0o777)

    result = _run(harness, alias)

    assert result.returncode != 0
    assert _commands(harness) == []


def test_owned_alias_to_trusted_cache_is_supported(harness):
    cache = harness[0] / "cache"
    built = _write(cache / CLI)
    alias = harness[0] / "alias"
    alias.symlink_to(cache, target_is_directory=True)

    result = _run(harness, alias)

    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == built
    assert _commands(harness) == [["node", str(built), "--version"]]


def test_external_dependency_alias_cannot_return_to_cache(harness):
    cache = harness[0] / "cache"
    _write(cache / CLI)
    dependency = cache / SHA / "node_modules/dependency"
    _write(dependency / "index.js")
    outside = harness[0] / "outside"
    outside.symlink_to(dependency, target_is_directory=True)
    (dependency.parent / "alias").symlink_to(outside, target_is_directory=True)

    result = _run(harness, cache)

    assert result.returncode != 0
    assert "symlink leaves the cache" in result.stderr
    assert _commands(harness) == []


def test_cold_build_output_is_checked_before_cli_execution(harness):
    outside = _write(harness[0] / "outside.js")
    harness[3]["TEST_UNSAFE_BUILD"] = str(outside)

    result = _run(harness)

    assert result.returncode != 0
    assert "symlink leaves the cache" in result.stderr
    assert not any(command[-1] == "--version" for command in _commands(harness))
    assert outside.read_text() == "fixture CLI"


@pytest.mark.parametrize("entry", [Path(), CLI.parent, CLI])
def test_foreign_owned_entries_are_rejected_without_repair(
    tmp_path, monkeypatch, cache_helper, entry
):
    cache = tmp_path / "cache"
    built = _write(cache / CLI)
    foreign = cache / entry
    real_lstat = Path.lstat

    def foreign_lstat(path):
        info = real_lstat(path)
        if path == foreign:
            values = list(info)
            values[4] = os.geteuid() + 1
            return os.stat_result(values)
        return info

    monkeypatch.setattr(Path, "lstat", foreign_lstat)

    with pytest.raises(PermissionError, match="untrusted"):
        cache_helper.prepare_cache(str(cache))

    assert built.read_text() == "fixture CLI"


def test_unsupported_platform_fails_before_creating_cache(tmp_path, monkeypatch, cache_helper):
    requested = str(tmp_path / "cache")
    with monkeypatch.context() as unsupported:
        unsupported.setattr(cache_helper.os, "name", "nt")
        with pytest.raises(OSError, match="require POSIX; use WSL on Windows"):
            cache_helper.prepare_cache(requested)

    assert list(tmp_path.iterdir()) == []
