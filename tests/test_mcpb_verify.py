"""Bundle-verifier orchestration contracts."""

import importlib.util
import zipfile
from pathlib import Path

import pytest


VERIFY_PATH = Path(__file__).parents[1] / "packaging" / "mcpb" / "verify.py"
VERIFY_SPEC = importlib.util.spec_from_file_location("mcpb_verify", VERIFY_PATH)
assert VERIFY_SPEC is not None and VERIFY_SPEC.loader is not None
verify = importlib.util.module_from_spec(VERIFY_SPEC)
VERIFY_SPEC.loader.exec_module(verify)


def test_protocol_handshakes_use_independent_unpack_and_state_directories(
    tmp_path, monkeypatch
) -> None:
    archive_path = tmp_path / "bundle.mcpb"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("manifest.json", "{}")

    calls: list[tuple[Path, bool, bool]] = []

    def fake_handshake(
        extracted: Path,
        *,
        modern: bool,
        env: dict[str, str],
        timeout: float = 240.0,
    ) -> dict[str, dict]:
        del timeout
        config_path = Path(env["DELTA_MCP_CONFIG_FILE"])
        assert config_path.parent == extracted
        hostile = env.get("DELTA_MCP_ENV") == "india_devnet"
        if modern:
            assert not hostile
            assert "DELTA_API_KEY" not in env
            assert "DELTA_API_SECRET" not in env
            assert "DELTA_MCP_MODE" not in env
            config_path.write_text("state initialized by modern discovery")
        else:
            assert hostile
            assert env["DELTA_API_KEY"] == "synthetic-key"
            assert env["DELTA_API_SECRET"] == "synthetic-secret"
            assert env["DELTA_MCP_MODE"] == "trade"
            assert not config_path.exists()
        calls.append((extracted, modern, hostile))
        return {}

    monkeypatch.setattr(verify, "handshake", fake_handshake)
    workdir = tmp_path / "verify"
    workdir.mkdir()

    manifest, modern_dir, legacy_dir = verify.unpack_installations(
        archive_path, workdir
    )
    modern, legacy = verify.discover_tools(modern_dir, legacy_dir)

    assert manifest == {}
    assert modern == legacy == {}
    assert calls == [
        (workdir / "modern", True, False),
        (workdir / "legacy", False, True),
    ]


def test_invalid_manifest_is_rejected_before_protocol_discovery(
    tmp_path, monkeypatch
) -> None:
    archive_path = tmp_path / "bundle.mcpb"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            "manifest.json",
            '{"user_config": {}, "server": {"mcp_config": {}}}',
        )

    def unexpected_discovery(
        modern_dir: Path, legacy_dir: Path
    ) -> tuple[dict[str, dict], dict[str, dict]]:
        del modern_dir, legacy_dir
        raise AssertionError("protocol discovery started before manifest validation")

    monkeypatch.setattr(verify, "check_archive", lambda _: None)
    monkeypatch.setattr(verify, "discover_tools", unexpected_discovery)
    monkeypatch.setattr(verify.sys, "argv", ["verify.py", str(archive_path)])

    with pytest.raises(SystemExit, match="must not declare user_config"):
        verify.main()
