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
    # Both spellings: os.environ.get for the few reads that deliberately bypass the
    # shared settings file, and config.setting for everything that falls back to it.
    # Scanning only the first would let a new setting slip in undocumented.
    pattern = r"""(?:os\.environ\.get|setting)\(["'](DELTA_[A-Z_]+)["']"""
    read_by_code = {
        name
        for path in src.rglob("*.py")
        for name in re.findall(pattern, path.read_text())
    }
    documented = set(re.findall(r"DELTA_[A-Z_]+", build_parser().format_help()))
    assert read_by_code, "env var scan found nothing — the pattern above stopped matching"
    assert read_by_code <= documented, f"undocumented in --help: {sorted(read_by_code - documented)}"


def test_handshake_reports_our_version_not_the_sdk_version():
    """A client has to be told this package's version, not the SDK's and not an empty one."""
    from importlib.metadata import version

    server_version = build_server(_cfg()).version
    assert server_version == PACKAGE_VERSION
    assert server_version != version("mcp")


def test_setting_up_is_one_command_with_two_ways_in():
    """Two commands writing the same settings left people guessing which to run.

    The browser is the default because the person this is for may have no terminal at all;
    `--terminal` is for the machine that has it the other way round.
    """
    parsed = build_parser().parse_args(["setup"])
    assert parsed.command == "setup"
    assert parsed.terminal is False

    assert build_parser().parse_args(["setup", "--terminal"]).terminal is True


def test_the_old_login_command_still_works():
    """It is published and people have it in their own notes; breaking it costs them.

    Kept out of `--help` so nobody learns it now, and it announces its replacement when
    run. Parsing it is the contract — that it still resolves to a command rather than
    exiting with "invalid choice".
    """
    parsed = build_parser().parse_args(["login"])
    assert parsed.command == "login"

    help_text = build_parser().format_help()
    assert "setup" in help_text
    assert "store your API key in the shared settings file" not in help_text
