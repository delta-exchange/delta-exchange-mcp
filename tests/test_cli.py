import pytest

from delta_exchange_mcp.config import INDIA_TESTNET_REST, Config
from delta_exchange_mcp.server import build_parser, build_server, main
from delta_exchange_mcp.version import PACKAGE_VERSION


def _cfg():
    return Config(env="india_testnet", base_url=INDIA_TESTNET_REST)


def test_help_exits_zero_and_prints_usage(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "usage: delta-exchange-mcp" in out
    # The env vars are the whole configuration surface, so help is useless without them.
    assert "DELTA_MCP_ENV" in out
    assert "DELTA_API_KEY" in out
    assert "DELTA_MCP_MODE" in out


def test_version_flag_reports_the_package_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"delta-exchange-mcp {PACKAGE_VERSION}"


def test_unknown_option_is_rejected():
    with pytest.raises(SystemExit) as exc:
        main(["--not-an-option"])
    assert exc.value.code == 2


def test_parser_help_is_not_empty():
    assert "stdio" in build_parser().format_help()


def test_help_documents_every_environment_variable_the_code_reads():
    """Help is the only configuration reference, so a new env var must not slip in undocumented."""
    import pathlib
    import re

    src = pathlib.Path(__file__).resolve().parents[1] / "src"
    read_by_code = {
        name
        for path in src.rglob("*.py")
        for name in re.findall(r"""os\.environ\.get\(["'](DELTA_[A-Z_]+)["']""", path.read_text())
    }
    documented = set(re.findall(r"DELTA_[A-Z_]+", build_parser().format_help()))
    assert read_by_code, "env var scan found nothing — the pattern above stopped matching"
    assert read_by_code <= documented, f"undocumented in --help: {sorted(read_by_code - documented)}"


def test_handshake_reports_our_version_not_the_sdk_version():
    """Left unset, MCPServer reports an empty version, and 1.x reported the SDK's as ours."""
    from importlib.metadata import version

    server_version = build_server(_cfg()).version
    assert server_version == PACKAGE_VERSION
    assert server_version != version("mcp")
