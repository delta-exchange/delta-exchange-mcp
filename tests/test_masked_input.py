"""Exercise actual password rendering and editing without plaintext echo or history."""

from io import StringIO
from unittest.mock import Mock

import pytest
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output

from delta_exchange_mcp import connection_cli


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("example-secret\r", "example-secret"),
        ("\x12example-secret\r\r", "example-secret"),
        ("\x13example-secret\r\r", "example-secret"),
        ("example-secrex\x7ft\r", "example-secret"),
        ("discard-me\x15example-secret\r", "example-secret"),
        ("\x1b[200~example-secret\x1b[201~\r", "example-secret"),
    ],
)
def test_masked_prompt_renders_asterisks_and_supports_editing(
    monkeypatch, typed, expected
):
    output = StringIO()
    terminal_output = Vt100_Output(
        output, lambda: Size(rows=24, columns=80), enable_cpr=False
    )
    monkeypatch.setattr(connection_cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(connection_cli.sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(
        connection_cli, "create_output", lambda **arguments: terminal_output
    )
    with create_pipe_input() as terminal_input:
        monkeypatch.setattr(
            connection_cli,
            "create_input",
            lambda **arguments: terminal_input,
        )
        terminal_input.send_text(typed)
        assert connection_cli.read_secret("API secret (masked): ") == expected
    rendered = output.getvalue()
    assert "*" * len(expected) in rendered
    assert expected not in rendered
    assert "discard-me" not in rendered


@pytest.mark.parametrize(
    ("typed", "exception"), [("\x03", KeyboardInterrupt), ("\x04", EOFError)]
)
def test_masked_prompt_can_be_cancelled(monkeypatch, typed, exception):
    output = StringIO()
    terminal_output = Vt100_Output(
        output, lambda: Size(rows=24, columns=80), enable_cpr=False
    )
    monkeypatch.setattr(connection_cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(connection_cli.sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(
        connection_cli, "create_output", lambda **arguments: terminal_output
    )
    with create_pipe_input() as terminal_input:
        monkeypatch.setattr(
            connection_cli,
            "create_input",
            lambda **arguments: terminal_input,
        )
        terminal_input.send_text(typed)
        with pytest.raises(exception):
            connection_cli.read_secret("API secret (masked): ")


@pytest.mark.parametrize(("stdin_tty", "stderr_tty"), [(False, True), (True, False)])
def test_masked_prompt_never_falls_back_to_visible_input(
    monkeypatch, stdin_tty, stderr_tty
):
    monkeypatch.setattr(connection_cli.sys.stdin, "isatty", lambda: stdin_tty)
    monkeypatch.setattr(connection_cli.sys.stderr, "isatty", lambda: stderr_tty)
    prompt = Mock(side_effect=AssertionError("must not start input"))
    monkeypatch.setattr(connection_cli, "PromptSession", prompt)
    with pytest.raises(connection_cli.TerminalInputError):
        connection_cli.read_secret("API key (masked): ")
    prompt.assert_not_called()


def test_masked_session_does_not_retain_history_or_clipboard(monkeypatch):
    monkeypatch.setattr(connection_cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(connection_cli.sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(
        connection_cli, "create_input", lambda **arguments: Mock()
    )
    monkeypatch.setattr(connection_cli, "create_output", lambda **arguments: None)
    sessions = []

    def session(**options):
        history = options["history"]
        history.append_string("example-secret")
        assert list(history.load_history_strings()) == []
        clipboard = options["clipboard"]
        clipboard.set_text("example-secret")
        assert clipboard.get_data().text == ""
        assert options["is_password"] is True
        assert options["enable_open_in_editor"] is False
        sessions.append(options)
        return Mock(prompt=Mock(return_value="example-secret"))

    monkeypatch.setattr(connection_cli, "PromptSession", session)
    assert connection_cli.read_secret("API secret (masked): ") == "example-secret"
    assert len(sessions) == 1
