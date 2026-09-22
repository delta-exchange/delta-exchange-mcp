"""Configuration uses saved credentials and the same revision-bound consent service."""

from unittest.mock import Mock

import pytest

from delta_exchange_mcp import config_cli, connection_cli
from delta_exchange_mcp.auth.connection import ConnectionService
from delta_exchange_mcp.auth.store import CredentialStore
from delta_exchange_mcp.server import main
from tests.connection_support import action, context
from tests.test_login import browser_page, enter
from tests.test_login import connections as connections


def seed(environment="india_prod"):
    connection = ConnectionService.open()
    for selected in ("india_testnet", "india_prod"):
        action(
            connection,
            "Codex",
            "credentials",
            {
                "environment": selected,
                "api_key": f"{selected}-key",
                "api_secret": f"{selected}-secret",
            },
        )
    action(
        connection,
        "Codex",
        "credentials",
        {"operation": "activate", "environment": environment},
    )
    return connection


def approve(connection, client="Codex"):
    action(
        connection,
        client,
        "consent",
        {
            "environment": connection.status(context(client))["environment"],
            "enabled": True,
            "acknowledged": True,
        },
    )


def test_switch_environment_uses_saved_pair_and_revokes_approval(
    connections, monkeypatch
):
    connection = seed()
    approve(connection)
    saved = connection.credentials.get("india_testnet")
    monkeypatch.setattr(connection_cli.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(
        connection_cli,
        "read_secret",
        Mock(side_effect=AssertionError("unexpected credential prompt")),
    )
    main(["config", "--env", "india_testnet"])
    reopened = ConnectionService.open()
    status = reopened.status(context("Codex"))
    assert status["environment"] == "india_testnet"
    assert reopened.credentials.get("india_testnet") == saved
    assert status["trading"]["enabled"] is False


def test_selecting_current_environment_keeps_approval(connections):
    connection = seed()
    approve(connection)
    main(["config", "--env", "india_prod"])
    assert (
        ConnectionService.open().status(context("Codex"))["trading"]["enabled"] is True
    )


def test_read_mode_revokes_only_selected_client_without_rotating_keys(
    connections, monkeypatch
):
    connection = seed()
    approve(connection)
    approve(connection, "Claude")
    saved = connection.credentials.get("india_prod")
    monkeypatch.setattr(connection_cli.sys.stdin, "isatty", lambda: False)
    main(["config", "--mode", "read", "--client", "Codex"])
    reopened = ConnectionService.open()
    assert reopened.credentials.get("india_prod") == saved
    assert reopened.status(context("Codex"))["trading"]["enabled"] is False
    assert reopened.status(context("Claude"))["trading"]["enabled"] is True


@pytest.mark.parametrize(
    ("environment", "answers", "enabled"),
    [
        ("india_testnet", ["yes"], True),
        ("india_testnet", ["no"], False),
        ("india_prod", ["yes", "yes"], True),
        ("india_prod", ["yes", "no"], False),
    ],
)
def test_trade_mode_uses_saved_pair_and_persists_explicit_approval(
    connections,
    monkeypatch,
    environment,
    answers,
    enabled,
):
    connection = seed()
    saved = connection.credentials.get(environment)
    hidden = enter(monkeypatch, answers)
    main(["config", "--env", environment, "--mode", "trade", "--client", "Codex"])
    hidden.assert_not_called()
    reopened = ConnectionService.open()
    assert reopened.credentials.get(environment) == saved
    assert reopened.status(context("Codex"))["trading"]["enabled"] is enabled


def test_noninteractive_trade_refuses_before_environment_change(
    connections, monkeypatch
):
    connection = seed()
    monkeypatch.setattr(connection_cli.sys.stdin, "isatty", lambda: False)
    assert (
        config_cli.run(
            environment="india_testnet", trading_mode="trade", client_name="Codex"
        )
        == 2
    )
    assert connection.status(context("Codex"))["environment"] == "india_prod"
    assert len(connections) == 1


def test_noninteractive_mode_needs_client_before_changing_environment(
    connections, monkeypatch
):
    connection = seed()
    monkeypatch.setattr(connection_cli.sys.stdin, "isatty", lambda: False)
    assert config_cli.run(environment="india_testnet", trading_mode="read") == 2
    assert connection.status(context("Codex"))["environment"] == "india_prod"


def test_environment_override_is_not_silently_changed(connections, monkeypatch):
    connection = seed()
    monkeypatch.setenv("DELTA_MCP_ENV", "india_prod")
    assert config_cli.run(environment="india_testnet") == 1
    assert connection.status(context("Codex"))["environment"] == "india_prod"


def test_menu_switches_environment_without_prompting_for_saved_secrets(
    connections, monkeypatch
):
    connection = seed()
    saved = connection.credentials.get("india_testnet")
    hidden = enter(monkeypatch, ["1", "india_testnet", "0"])
    main(["config", "--no-browser", "--client", "Codex"])
    hidden.assert_not_called()
    reopened = ConnectionService.open()
    assert reopened.credentials.get("india_testnet") == saved
    assert reopened.status(context("Codex"))["environment"] == "india_testnet"


@pytest.mark.parametrize("connect", ["yes", "no"])
def test_menu_can_connect_when_target_environment_has_no_saved_account(
    connections, monkeypatch, connect
):
    hidden = enter(monkeypatch, ["1", "india_testnet", connect, "0"])
    main(["config", "--device", "--client", "Codex"])
    reopened = ConnectionService.open()
    status = reopened.status(context("Codex"))
    assert status["environment"] == "india_testnet"
    assert status["credentials_configured"] is (connect == "yes")
    assert status["trading"]["enabled"] is False
    assert hidden.call_count == (2 if connect == "yes" else 0)


def test_menu_replaces_credentials_and_revokes_previous_approval(
    connections, monkeypatch, capsys
):
    connection = seed()
    approve(connection)
    enter(monkeypatch, ["3", "0"])
    main(["config", "--device", "--client", "Codex"])
    reopened = ConnectionService.open()
    assert reopened.credentials.get("india_prod").api_key == "example-key"
    assert reopened.status(context("Codex"))["trading"]["enabled"] is False
    output = capsys.readouterr()
    assert "example-key" not in output.out + output.err
    assert "example-secret" not in output.out + output.err


@pytest.mark.parametrize("confirm", ["yes", "no"])
def test_disconnect_requires_confirmation(connections, monkeypatch, confirm):
    seed()
    enter(monkeypatch, ["4", confirm, "0"])
    main(["config", "--device", "--client", "Codex"])
    assert (ConnectionService.open().credentials.get("india_prod") is None) is (
        confirm == "yes"
    )


def test_menu_changes_client_then_manages_its_mode(connections, monkeypatch):
    connection = seed()
    approve(connection)
    enter(monkeypatch, ["", "5", "Codex", "2", "read", "0"])
    main(["config"])
    assert (
        ConnectionService.open().status(context("Codex"))["trading"]["enabled"] is False
    )


def test_stale_mode_prompt_does_not_restore_revoked_consent(connections, monkeypatch):
    connection = seed("india_testnet")

    def approve_after_revocation(prompt):
        action(
            connection,
            "Codex",
            "consent",
            {"environment": "india_testnet", "enabled": False},
        )
        return "yes"

    monkeypatch.setattr("builtins.input", approve_after_revocation)
    assert config_cli.run(trading_mode="trade", client_name="Codex") == 1
    assert (
        ConnectionService.open().status(context("Codex"))["trading"]["enabled"] is False
    )


def test_stale_masked_input_does_not_overwrite_concurrent_rotation(
    connections, monkeypatch
):
    connection = seed()
    enter(monkeypatch, ["3"])

    def secret(prompt):
        if "API key" in prompt:
            action(
                connection,
                "Codex",
                "credentials",
                {
                    "environment": "india_prod",
                    "api_key": "new-key",
                    "api_secret": "new-secret",
                },
            )
        return "example-key" if "API key" in prompt else "example-secret"

    monkeypatch.setattr(connection_cli, "read_secret", secret)
    assert config_cli.run(mode="terminal", client_name="Codex") == 1
    assert ConnectionService.open().credentials.get("india_prod").api_key == "new-key"


def test_stale_environment_menu_does_not_overwrite_new_selection(
    connections, monkeypatch
):
    connection = seed()
    answers = iter(["1", "india_testnet"])

    def answer(prompt):
        text = next(answers)
        if text == "india_testnet":
            action(
                connection,
                "Codex",
                "credentials",
                {"operation": "activate", "environment": "india_testnet"},
            )
            action(
                connection,
                "Codex",
                "credentials",
                {"operation": "activate", "environment": "india_prod"},
            )
        return text

    monkeypatch.setattr("builtins.input", answer)
    assert config_cli.run(mode="terminal", client_name="Codex") == 1
    assert (
        ConnectionService.open().status(context("Codex"))["environment"] == "india_prod"
    )


def test_browser_config_can_finish_without_connected_credentials(
    connections, monkeypatch
):
    page = browser_page(monkeypatch, ConnectionService.open(), complete=False)
    monkeypatch.setattr(connection_cli.webbrowser, "open", Mock(return_value=True))
    assert config_cli.run(mode="browser") == 0
    page.wait.assert_called_once()
    page.stop.assert_called_once()


def test_automatic_config_browser_failure_opens_terminal_menu(connections, monkeypatch):
    page = browser_page(monkeypatch, ConnectionService.open())
    monkeypatch.setattr(connection_cli, "browser_available", lambda: True)
    monkeypatch.setattr(connection_cli.webbrowser, "open", Mock(return_value=False))
    enter(monkeypatch, ["", "0"])
    assert config_cli.run() == 0
    page.wait.assert_not_called()
    page.stop.assert_called_once()


def test_config_refuses_memory_only_store(connections, monkeypatch):
    connection = ConnectionService.open()
    connection.credentials = CredentialStore.memory()
    monkeypatch.setattr(ConnectionService, "open", staticmethod(lambda: connection))
    assert config_cli.run(environment="india_testnet") == 1


@pytest.mark.parametrize("mode", ["read", "trade"])
def test_config_does_not_claim_persistent_approval_for_process_credentials(
    connections, monkeypatch, mode
):
    seed()
    monkeypatch.setenv("DELTA_API_KEY", "process-key")
    monkeypatch.setenv("DELTA_API_SECRET", "process-secret")
    assert config_cli.run(trading_mode=mode, client_name="Codex") == 1


def test_browser_and_configuration_arguments_cannot_be_mixed(connections):
    assert config_cli.run(mode="browser", environment="india_testnet") == 2
    assert connections == []
