"""CLI login against the real connection, credential, and consent services."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from delta_exchange_mcp import credentials as credential_check
from delta_exchange_mcp import connection_cli, login
from delta_exchange_mcp.auth.connection import ConnectionService
from delta_exchange_mcp.auth.store import (
    BackendOperationError,
    CredentialSource,
    CredentialState,
    CredentialStore,
    FileMetadata,
    MemorySecretBackend,
)
from delta_exchange_mcp.server import main
from tests.connection_support import action, context, verified


@pytest.fixture
def connections(monkeypatch, tmp_path):
    for name in ("DELTA_API_KEY", "DELTA_API_SECRET", "DELTA_MCP_ENV"):
        monkeypatch.delenv(name, raising=False)
    backend = MemorySecretBackend()
    opened = []
    original_open = ConnectionService.open

    def open_connection():
        connection = original_open(
            credentials=CredentialStore(
                backend,
                FileMetadata(tmp_path / "credentials.json"),
                CredentialSource.OS_STORE,
            ),
            validator=verified,
        )
        opened.append(connection)
        return connection

    monkeypatch.setattr(ConnectionService, "open", staticmethod(open_connection))
    monkeypatch.setattr(connection_cli, "browser_available", lambda: False)
    monkeypatch.setattr(connection_cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        connection_cli.webbrowser,
        "open",
        Mock(side_effect=AssertionError("unexpected browser")),
    )
    yield opened
    for connection in opened:
        connection.close()
        connection_cli.anyio.run(connection.client.aclose)


def enter(monkeypatch, answers, *, secrets=("example-key", "example-secret")):
    answers = iter(answers)
    secrets = iter(secrets)
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    hidden = Mock(side_effect=lambda prompt: next(secrets))
    monkeypatch.setattr(connection_cli, "read_secret", hidden)
    return hidden


def test_direct_login_survives_reopen_without_prompts_or_trading(
    connections,
    monkeypatch,
    tmp_path,
    capsys,
):
    monkeypatch.setattr(connection_cli.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(
        "builtins.input", Mock(side_effect=AssertionError("unexpected prompt"))
    )
    main(
        [
            "login",
            "--api-key",
            "example-key",
            "--api-secret",
            "example-secret",
            "--env",
            "india_testnet",
            "--client",
            "Codex",
        ]
    )

    reopened = ConnectionService.open()
    status = reopened.status(context("Codex"))
    assert status["environment"] == "india_testnet"
    assert status["credentials_configured"] is True
    assert status["trading"]["enabled"] is False
    credential = reopened.credentials.get("india_testnet")
    assert (credential.api_key, credential.api_secret) == (
        "example-key",
        "example-secret",
    )
    assert credential.state is CredentialState.VERIFIED
    output = capsys.readouterr()
    assert "saved" in output.out
    for secret in ("example-key", "example-secret"):
        assert secret not in output.out + output.err
        assert all(
            secret not in path.read_text()
            for path in tmp_path.rglob("*")
            if path.is_file()
        )


@pytest.mark.parametrize("flag", [None, "--device", "--no-browser"])
def test_headless_login_and_explicit_terminal_modes_use_hidden_input(
    connections,
    monkeypatch,
    flag,
):
    hidden = enter(monkeypatch, ["", "india_testnet"])
    if flag:
        monkeypatch.setattr(connection_cli, "browser_available", lambda: True)
    main(["login", *([flag] if flag else [])])
    assert hidden.call_count == 2
    assert all("masked" in call.args[0] for call in hidden.call_args_list)
    assert connections[0].credentials.get("india_testnet").api_key == "example-key"
    assert connections[0].status(context("Codex"))["trading"]["enabled"] is False


@pytest.mark.parametrize(
    ("environment", "answers", "enabled"),
    [
        ("india_prod", ["yes", "yes"], True),
        ("india_prod", ["yes", "no"], False),
        ("india_prod", [""], False),
        ("india_testnet", ["yes"], True),
        ("india_testnet", ["no"], False),
    ],
)
def test_terminal_consent_requires_explicit_approval_and_survives_restart(
    connections,
    monkeypatch,
    environment,
    answers,
    enabled,
):
    enter(monkeypatch, answers)
    main(["login", "--device", "--env", environment, "--client", "Codex"])
    reopened = ConnectionService.open()
    assert reopened.status(context("Codex"))["trading"]["enabled"] is enabled
    assert reopened.status(context("Claude"))["trading"]["enabled"] is False


def test_prompted_client_uses_its_own_consent_revision(connections, monkeypatch):
    enter(monkeypatch, ["Codex", "", "yes", "yes"])
    main(["login"])
    assert (
        ConnectionService.open().status(context("Codex"))["trading"]["enabled"] is True
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {"api_key": "example-key"},
        {"api_secret": "example-secret"},
        {"api_key": " ", "api_secret": "example-secret"},
        {"api_key": "example-key", "api_secret": ""},
        {"mode": "browser", "api_key": "example-key", "api_secret": "example-secret"},
    ],
)
def test_bad_credential_arguments_fail_before_opening_store(
    connections, arguments, capsys
):
    assert login.run(**arguments) == 2
    assert connections == []
    output = capsys.readouterr()
    assert "example-key" not in output.out + output.err
    assert "example-secret" not in output.out + output.err


def test_no_tty_does_not_read_piped_credentials(connections, monkeypatch):
    monkeypatch.setattr(connection_cli.sys.stdin, "isatty", lambda: False)
    assert login.run() == 2
    assert connections == []


def test_memory_only_store_cannot_claim_persistent_login(
    connections, monkeypatch, capsys
):
    connection = ConnectionService.open()
    connection.credentials = CredentialStore.memory()
    monkeypatch.setattr(ConnectionService, "open", staticmethod(lambda: connection))
    monkeypatch.setattr(
        "builtins.input", Mock(side_effect=AssertionError("unexpected prompt"))
    )
    assert login.run(mode="terminal") == 1
    assert connection.credentials.get("india_prod") is None
    assert "persistent credential store" in capsys.readouterr().err


@pytest.mark.parametrize(
    "failure", [KeyboardInterrupt, EOFError, connection_cli.TerminalInputError]
)
def test_interrupted_or_unhidden_input_does_not_save(connections, monkeypatch, failure):
    enter(monkeypatch, ["", ""])

    def fail(prompt):
        raise failure

    monkeypatch.setattr(connection_cli, "read_secret", fail)
    assert login.run() == (1 if failure is connection_cli.TerminalInputError else 130)
    assert connections[0].credentials.get("india_prod") is None


@pytest.mark.parametrize(
    ("code", "exit_code", "expected_key"),
    [
        ("InvalidApiKey", 1, "old-key"),
        ("UnauthorizedApiAccess", 0, "example-key"),
        ("", 0, "example-key"),
    ],
)
def test_validation_preserves_rejected_key_and_reports_unverified(
    connections,
    monkeypatch,
    capsys,
    code,
    exit_code,
    expected_key,
):
    connection = ConnectionService.open()
    action(
        connection,
        "Codex",
        "credentials",
        {
            "environment": "india_prod",
            "api_key": "old-key",
            "api_secret": "old-secret",
        },
    )
    action(
        connection,
        "Codex",
        "consent",
        {
            "environment": "india_prod",
            "enabled": True,
            "acknowledged": True,
        },
    )

    async def validate(environment, api_key, api_secret):
        return credential_check.Check(
            ok=False, reachable=bool(code), code=code, detail="not verified"
        )

    connection.validator = validate
    monkeypatch.setattr(ConnectionService, "open", staticmethod(lambda: connection))
    assert login.run(api_key="example-key", api_secret="example-secret") == exit_code
    credential = connection.credentials.get("india_prod")
    assert credential.api_key == expected_key
    assert connection.status(context("Codex"))["trading"]["enabled"] is (exit_code == 1)
    output = capsys.readouterr()
    if exit_code == 0:
        assert credential.state is CredentialState.UNVERIFIED
        assert output.err


def test_concurrent_replacement_during_terminal_approval_is_not_authorized(
    connections,
    monkeypatch,
):
    enter(monkeypatch, [])

    def approve(prompt):
        action(
            connections[0],
            "Codex",
            "credentials",
            {
                "environment": "india_testnet",
                "api_key": "new-key",
                "api_secret": "new-secret",
            },
        )
        return "yes"

    monkeypatch.setattr("builtins.input", approve)
    assert (
        login.run(mode="terminal", environment="india_testnet", client_name="Codex")
        == 1
    )
    assert connections[0].credentials.get("india_testnet").api_key == "new-key"
    assert connections[0].status(context("Codex"))["trading"]["enabled"] is False


def test_concurrent_replacement_during_input_is_not_overwritten(
    connections, monkeypatch
):
    enter(monkeypatch, [])

    def secret(prompt):
        if "API key" in prompt:
            action(
                connections[0],
                "Codex",
                "credentials",
                {
                    "environment": "india_testnet",
                    "api_key": "new-key",
                    "api_secret": "new-secret",
                },
            )
        return "example-key" if "API key" in prompt else "example-secret"

    monkeypatch.setattr(connection_cli, "read_secret", secret)
    assert (
        login.run(mode="terminal", environment="india_testnet", client_name="Codex")
        == 1
    )
    assert connections[0].credentials.get("india_testnet").api_key == "new-key"


def browser_page(monkeypatch, connection, *, complete=True, save=False):
    page = SimpleNamespace(
        url="http://127.0.0.1:43123/manage", running=True, stop=Mock()
    )

    def factory(*, actions, revision, open_browser):
        assert open_browser is False

        def wait():
            if save:
                result = actions(
                    "credentials",
                    {
                        "environment": "india_prod",
                        "api_key": "browser-key",
                        "api_secret": "browser-secret",
                    },
                    revision,
                )
                assert result.content["status"] == "saved"
            return complete

        page.wait = Mock(side_effect=wait)
        return page

    connection.page_factory = factory
    monkeypatch.setattr(ConnectionService, "open", staticmethod(lambda: connection))
    return page


@pytest.mark.parametrize(
    ("mode", "opened"), [("auto", True), ("browser", True), ("browser", False)]
)
def test_browser_mode_preserves_page_and_cleanup(
    connections, monkeypatch, capsys, mode, opened
):
    page = browser_page(monkeypatch, ConnectionService.open())
    monkeypatch.setattr(connection_cli, "browser_available", lambda: mode == "auto")
    opener = Mock(return_value=opened)
    monkeypatch.setattr(connection_cli.webbrowser, "open", opener)
    assert login.run(mode=mode) == 0
    opener.assert_called_once_with(page.url)
    page.wait.assert_called_once()
    page.stop.assert_called_once()
    assert page.url in capsys.readouterr().err


def test_browser_credential_only_save_is_success_after_page_closes(
    connections, monkeypatch
):
    browser_page(monkeypatch, ConnectionService.open(), complete=False, save=True)
    monkeypatch.setattr(connection_cli.webbrowser, "open", Mock(return_value=True))
    assert login.run(mode="browser") == 0


def test_browser_closure_without_login_returns_failure(connections, monkeypatch):
    browser_page(monkeypatch, ConnectionService.open(), complete=False)
    monkeypatch.setattr(connection_cli.webbrowser, "open", Mock(return_value=True))
    assert login.run(mode="browser") == 1


@pytest.mark.parametrize(
    "failure",
    [False, OSError("cannot launch"), connection_cli.webbrowser.Error("no browser")],
)
def test_failed_automatic_browser_launch_falls_back_to_terminal(
    connections, monkeypatch, failure
):
    page = browser_page(monkeypatch, ConnectionService.open())
    monkeypatch.setattr(connection_cli, "browser_available", lambda: True)
    opener = (
        Mock(side_effect=failure)
        if isinstance(failure, Exception)
        else Mock(return_value=False)
    )
    monkeypatch.setattr(connection_cli.webbrowser, "open", opener)
    enter(monkeypatch, ["", ""])
    assert login.run() == 0
    page.wait.assert_not_called()
    page.stop.assert_called_once()
    assert connections[0].credentials.get("india_prod").api_key == "example-key"


def test_failed_browser_without_tty_returns_actionable_error(connections, monkeypatch):
    browser_page(monkeypatch, ConnectionService.open())
    monkeypatch.setattr(connection_cli, "browser_available", lambda: True)
    monkeypatch.setattr(connection_cli.webbrowser, "open", Mock(return_value=False))
    monkeypatch.setattr(connection_cli.sys.stdin, "isatty", lambda: False)
    assert login.run() == 2
    assert connections[0].credentials.get("india_prod") is None


@pytest.mark.parametrize(
    ("platform", "variables", "expected"),
    [
        ("darwin", {}, True),
        ("darwin", {"SSH_CONNECTION": "remote"}, False),
        ("win32", {}, True),
        ("win32", {"SSH_CLIENT": "remote"}, False),
        ("linux", {}, False),
        ("linux", {"DISPLAY": ":0"}, True),
        ("linux", {"WAYLAND_DISPLAY": "wayland-0"}, True),
        ("linux", {"DISPLAY": ":10", "SSH_TTY": "/dev/pts/1"}, False),
    ],
)
def test_browser_detection(monkeypatch, platform, variables, expected):
    for name in (
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "SSH_CONNECTION",
        "SSH_CLIENT",
        "SSH_TTY",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in variables.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(connection_cli.sys, "platform", platform)
    monkeypatch.setattr(connection_cli.webbrowser, "get", Mock())
    assert connection_cli.browser_available() is expected


def test_missing_browser_controller_selects_terminal(monkeypatch):
    for name in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(connection_cli.sys, "platform", "darwin")
    monkeypatch.setattr(
        connection_cli.webbrowser,
        "get",
        Mock(side_effect=connection_cli.webbrowser.Error),
    )
    assert connection_cli.browser_available() is False


@pytest.mark.parametrize("mode", ["auto", "browser"])
def test_loopback_start_failure_falls_back_only_in_auto_mode(
    connections, monkeypatch, mode
):
    connection = ConnectionService.open()
    connection.page_factory = Mock(side_effect=OSError("cannot bind"))
    monkeypatch.setattr(ConnectionService, "open", staticmethod(lambda: connection))
    monkeypatch.setattr(connection_cli, "browser_available", lambda: True)
    enter(monkeypatch, ["", ""])
    assert login.run(mode=mode) == (0 if mode == "auto" else 1)
    assert (connection.credentials.get("india_prod") is not None) is (mode == "auto")


def test_fixed_environment_conflict_fails_before_saving(
    connections, monkeypatch, capsys
):
    monkeypatch.setenv("DELTA_MCP_ENV", "india_prod")
    assert (
        login.run(
            api_key="example-key",
            api_secret="example-secret",
            environment="india_testnet",
        )
        == 2
    )
    assert connections[0].credentials.get("india_testnet") is None
    assert "DELTA_MCP_ENV" in capsys.readouterr().err


def test_secure_store_write_failure_does_not_report_success(
    connections, monkeypatch, capsys
):
    connection = ConnectionService.open()
    connection.credentials._backend.set = Mock(
        side_effect=BackendOperationError("store locked")
    )
    monkeypatch.setattr(ConnectionService, "open", staticmethod(lambda: connection))
    assert login.run(api_key="example-key", api_secret="example-secret") == 1
    assert connection.credentials.get("india_prod") is None
    output = capsys.readouterr()
    assert "saved" not in output.out
    assert "could not update" in output.err


def test_concurrent_revocation_during_approval_stays_disabled(connections, monkeypatch):
    enter(monkeypatch, [])

    def approve(prompt):
        action(
            connections[0],
            "Codex",
            "consent",
            {
                "environment": "india_testnet",
                "enabled": False,
            },
        )
        return "yes"

    monkeypatch.setattr("builtins.input", approve)
    assert (
        login.run(mode="terminal", environment="india_testnet", client_name="Codex")
        == 1
    )
    assert connections[0].status(context("Codex"))["trading"]["enabled"] is False
