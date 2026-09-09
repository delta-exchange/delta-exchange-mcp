import pytest

from delta_exchange_mcp import server as server_mod
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
    assert "Manage Connection is the normal environment" in out
    assert "advanced externally managed compatibility overrides" in out
    assert "process memory and no plaintext" in out
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


def test_login_opens_manage_connection_without_requesting_secrets(
    monkeypatch, capsys
):
    class FakePage:
        url = "http://127.0.0.1:43123/manage"

        def __init__(self):
            self.waited = False

        def wait(self):
            self.waited = True

    class FakeClient:
        def __init__(self):
            self.closed = False

        async def aclose(self):
            self.closed = True

    class FakeConnection:
        def __init__(self):
            self.page = FakePage()
            self.client = FakeClient()
            self.open_browser = False
            self.closed = False

        def open_page(self, *, open_browser=False):
            self.open_browser = open_browser
            return self.page

        def close(self):
            self.closed = True

    connection = FakeConnection()
    monkeypatch.setattr(
        server_mod.ConnectionService,
        "open",
        staticmethod(lambda: connection),
    )

    main(["login"])

    error = capsys.readouterr().err
    assert connection.open_browser is True
    assert connection.page.waited is True
    assert connection.closed is True
    assert connection.client.closed is True
    assert "Manage Connection: http://127.0.0.1:43123/manage" in error


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
        for name in re.findall(pattern, path.read_text(encoding="utf-8"))
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
