import httpx
import pytest
import respx

from delta_exchange_mcp import config as config_mod
from delta_exchange_mcp import credentials, login, store


class FakeTty:
    def isatty(self):
        return True


@pytest.fixture
def terminal(monkeypatch):
    """Answer the prompts as a person at a keyboard would."""
    monkeypatch.setattr(login.sys, "stdin", FakeTty())
    monkeypatch.setattr("builtins.input", lambda prompt="": "india_testnet")
    secrets = iter(["a-real-key", "a-real-secret"])
    monkeypatch.setattr(login.getpass, "getpass", lambda prompt="": next(secrets))


def check_returning(**kwargs):
    async def fake(env, key, secret):
        return credentials.Check(**kwargs)

    return fake


@pytest.mark.parametrize(
    ("status", "code"),
    [(429, "rate_limit_exceeded"), (503, "service_unavailable")],
)
@respx.mock
async def test_api_unavailability_is_not_a_credential_rejection(
    monkeypatch, status, code
):
    """A terminal retry response says nothing about whether the credentials work."""

    async def no_sleep(_delay):
        pass

    monkeypatch.setattr("delta_exchange_mcp.client.asyncio.sleep", no_sleep)
    route = respx.get(f"{config_mod.INDIA_TESTNET_REST}/users/trading_preferences").mock(
        return_value=httpx.Response(
            status,
            json={"success": False, "error": {"code": code}},
        )
    )

    result = await credentials.check("india_testnet", "key", "secret")

    assert route.call_count == 3
    assert result.ok is False
    assert result.reachable is False


@respx.mock
async def test_api_key_rejection_is_a_credential_rejection():
    """A documented authentication failure is decisive and must prevent a save."""
    route = respx.get(f"{config_mod.INDIA_TESTNET_REST}/users/trading_preferences").mock(
        return_value=httpx.Response(
            401,
            json={"success": False, "error": {"code": "InvalidApiKey"}},
        )
    )

    result = await credentials.check("india_testnet", "key", "secret")

    assert route.call_count == 1
    assert result.ok is False
    assert result.reachable is True


def test_refuses_without_a_terminal(monkeypatch, capsys):
    """getpass alone would read a pipe and echo it.

    `echo $KEY | delta-exchange-mcp login` is what an agent trying to help would run,
    and it would put the secret in shell history and in that agent's transcript.
    """

    class NotATty:
        def isatty(self):
            return False

    monkeypatch.setattr(login.sys, "stdin", NotATty())
    assert login.run() == 2
    assert "needs a terminal" in capsys.readouterr().err
    assert not store.path().exists()


def test_saves_after_a_successful_check(terminal, monkeypatch):
    monkeypatch.setattr(credentials, "check", check_returning(ok=True, reachable=True, detail=""))
    assert login.run() == 0

    cfg = config_mod.load()
    assert (cfg.api_key, cfg.api_secret) == ("a-real-key", "a-real-secret")
    assert cfg.env == "india_testnet"


def test_saving_keeps_the_template_and_its_instructions(terminal, monkeypatch):
    """The file has to stay hand-editable after login has written to it."""
    monkeypatch.setattr(credentials, "check", check_returning(ok=True, reachable=True, detail=""))
    login.run()

    body = store.path().read_text()
    assert "permission for trading preferences" in body
    assert "Read Data alone is sufficient" in body
    assert "DELTA_MCP_MODE=trade" in body  # the commented-out explanation survives


def test_login_does_not_claim_read_data_is_sufficient(terminal, monkeypatch, capsys):
    monkeypatch.setattr(credentials, "check", check_returning(ok=True, reachable=True, detail=""))
    login.run()

    body = capsys.readouterr().out
    assert "permission for trading preferences" in body
    assert "does not establish whether Read Data alone is sufficient" in body
    assert "permission is enough" not in body


def test_a_rejected_key_is_not_saved(terminal, monkeypatch, capsys):
    """Saving a key that does not work would register the account tools and fail every call.

    That is the state placeholder credentials used to produce, and the reason the check
    exists at all.
    """
    monkeypatch.setattr(
        credentials,
        "check",
        check_returning(
            ok=False,
            reachable=True,
            detail="delta api error: InvalidApiKey — API key not found.",
        ),
    )
    assert login.run() == 1
    assert "Nothing was saved" in capsys.readouterr().err
    assert config_mod.load().has_credentials is False


def test_an_unreachable_api_still_saves(terminal, monkeypatch, capsys):
    """A flaky connection must not cost someone a key they typed correctly."""
    monkeypatch.setattr(
        credentials,
        "check",
        check_returning(ok=False, reachable=False, detail="could not reach Delta: timeout"),
    )
    assert login.run() == 0
    assert "unverified" in capsys.readouterr().err
    assert config_mod.load().has_credentials is True


def test_no_verify_skips_the_call(terminal, monkeypatch):
    async def explode(env, key, secret):
        raise AssertionError("--no-verify must not reach the API")

    monkeypatch.setattr(credentials, "check", explode)
    assert login.run(verify=False) == 0
    assert config_mod.load().has_credentials is True


def test_blank_answers_keep_a_saved_pair_and_its_environment(monkeypatch, capsys):
    """Enter should mean keep, without displaying either half of the saved pair."""
    saved_key = "saved-key-never-print"
    saved_secret = "saved-secret-never-print"
    assert (
        store.write(
            {
                "DELTA_MCP_ENV": "india_testnet",
                "DELTA_API_KEY": saved_key,
                "DELTA_API_SECRET": saved_secret,
            }
        )
        is None
    )
    monkeypatch.setattr(login.sys, "stdin", FakeTty())
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or "")
    secret_prompts = []
    answers = iter(["", ""])
    monkeypatch.setattr(
        login.getpass,
        "getpass",
        lambda prompt="": secret_prompts.append(prompt) or next(answers),
    )
    checked = []

    async def successful_check(env, key, secret):
        checked.append((env, key, secret))
        return credentials.Check(ok=True, reachable=True, detail="")

    monkeypatch.setattr(credentials, "check", successful_check)

    assert login.run() == 0

    assert "[india_testnet]" in prompts[0]
    assert all("keep" in prompt.lower() for prompt in secret_prompts)
    assert checked == [("india_testnet", saved_key, saved_secret)]
    assert store.read() == {
        "DELTA_API_KEY": saved_key,
        "DELTA_API_SECRET": saved_secret,
        "DELTA_MCP_ENV": "india_testnet",
    }
    output = capsys.readouterr()
    rendered = output.out + output.err
    assert saved_key not in rendered
    assert saved_secret not in rendered


def test_changing_environment_requires_a_new_pair_even_without_verify(
    monkeypatch, capsys
):
    """An old key must not be rebound to another environment by blank answers."""
    original = {
        "DELTA_MCP_ENV": "india_prod",
        "DELTA_API_KEY": "prod-key",
        "DELTA_API_SECRET": "prod-secret",
    }
    assert store.write(original) is None
    monkeypatch.setattr(login.sys, "stdin", FakeTty())
    monkeypatch.setattr("builtins.input", lambda prompt="": "india_testnet")
    answers = iter(["", ""])
    monkeypatch.setattr(login.getpass, "getpass", lambda prompt="": next(answers))

    assert login.run(verify=False) == 1

    assert "changing environments" in capsys.readouterr().err
    assert store.read() == original


def test_a_new_pair_can_move_the_saved_environment(monkeypatch):
    original = {
        "DELTA_MCP_ENV": "india_prod",
        "DELTA_API_KEY": "prod-key",
        "DELTA_API_SECRET": "prod-secret",
    }
    assert store.write(original) is None
    monkeypatch.setattr(login.sys, "stdin", FakeTty())
    monkeypatch.setattr("builtins.input", lambda prompt="": "india_testnet")
    answers = iter(["testnet-key", "testnet-secret"])
    monkeypatch.setattr(login.getpass, "getpass", lambda prompt="": next(answers))

    assert login.run(verify=False) == 0
    assert store.read() == {
        "DELTA_API_KEY": "testnet-key",
        "DELTA_API_SECRET": "testnet-secret",
        "DELTA_MCP_ENV": "india_testnet",
    }


def test_blank_credentials_without_a_saved_pair_are_explained(monkeypatch, capsys):
    monkeypatch.setattr(login.sys, "stdin", FakeTty())
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    answers = iter(["", ""])
    monkeypatch.setattr(login.getpass, "getpass", lambda prompt="": next(answers))

    assert login.run(verify=False) == 1

    assert "no complete credential pair is saved" in capsys.readouterr().err.lower()
    assert config_mod.load().has_credentials is False


def test_a_saved_pair_without_a_valid_environment_cannot_be_kept(monkeypatch, capsys):
    original = {"DELTA_API_KEY": "old-key", "DELTA_API_SECRET": "old-secret"}
    path = store.path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("DELTA_API_KEY=old-key\nDELTA_API_SECRET=old-secret\n")
    monkeypatch.setattr(login.sys, "stdin", FakeTty())
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    prompts = []
    answers = iter(["", ""])
    monkeypatch.setattr(
        login.getpass,
        "getpass",
        lambda prompt="": prompts.append(prompt) or next(answers),
    )

    assert login.run(verify=False) == 1

    assert all("keep" not in prompt.lower() for prompt in prompts)
    assert "no valid environment" in capsys.readouterr().err.lower()
    assert store.read() == original


def test_a_partial_replacement_never_mixes_with_the_saved_pair(monkeypatch, capsys):
    original = {
        "DELTA_MCP_ENV": "india_testnet",
        "DELTA_API_KEY": "old-key",
        "DELTA_API_SECRET": "old-secret",
    }
    assert store.write(original) is None
    monkeypatch.setattr(login.sys, "stdin", FakeTty())
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    answers = iter(["new-key", ""])
    monkeypatch.setattr(login.getpass, "getpass", lambda prompt="": next(answers))

    assert login.run(verify=False) == 1

    assert "enter both to replace" in capsys.readouterr().err.lower()
    assert store.read() == original


def test_half_a_pair_is_refused(monkeypatch, capsys):
    monkeypatch.setattr(login.sys, "stdin", FakeTty())
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    secrets = iter(["only-a-key", ""])
    monkeypatch.setattr(login.getpass, "getpass", lambda prompt="": next(secrets))

    assert login.run() == 1
    assert "both a key and its secret" in capsys.readouterr().err
    assert config_mod.load().has_credentials is False


def test_an_unknown_environment_is_refused(monkeypatch, capsys):
    monkeypatch.setattr(login.sys, "stdin", FakeTty())
    monkeypatch.setattr("builtins.input", lambda prompt="": "mainnet")

    assert login.run() == 1
    assert "not an environment" in capsys.readouterr().err


def test_a_shell_export_that_would_shadow_the_file_is_reported(terminal, monkeypatch, capsys):
    """A client launched from this shell inherits the export, and the client always wins.

    Without this the key just saved would appear to do nothing at all.
    """
    monkeypatch.setenv("DELTA_API_KEY", "exported-in-the-shell")
    monkeypatch.setattr(credentials, "check", check_returning(ok=True, reachable=True, detail=""))
    assert login.run() == 0
    assert "takes precedence over the file" in capsys.readouterr().err
