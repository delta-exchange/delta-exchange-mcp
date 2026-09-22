import pytest

from delta_exchange_mcp import config as config_mod


def test_defaults_to_india_prod(monkeypatch):
    monkeypatch.delenv("DELTA_MCP_ENV", raising=False)
    monkeypatch.delenv("DELTA_API_KEY", raising=False)
    monkeypatch.delenv("DELTA_API_SECRET", raising=False)
    cfg = config_mod.load()
    assert cfg.env == "india_prod"
    assert cfg.base_url == config_mod.INDIA_PROD_REST
    assert cfg.has_credentials is False


def test_testnet_override(monkeypatch):
    monkeypatch.setenv("DELTA_MCP_ENV", "india_testnet")
    cfg = config_mod.load()
    assert cfg.env == "india_testnet"
    assert cfg.base_url == config_mod.INDIA_TESTNET_REST


def test_invalid_env_rejected(monkeypatch):
    monkeypatch.setenv("DELTA_MCP_ENV", "mainnet")  # old alias no longer accepted
    with pytest.raises(ValueError, match="DELTA_MCP_ENV"):
        config_mod.load()


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_env_falls_back_to_the_default(monkeypatch, value):
    """A bundle substitutes every declared variable, so a cleared form field arrives blank.

    Treating that as invalid stopped the server from starting at all, which is a worse
    outcome than the default.
    """
    monkeypatch.setenv("DELTA_MCP_ENV", value)
    cfg = config_mod.load()
    assert cfg.env == "india_prod"


@pytest.mark.parametrize("value", ["", "   ", "\n", "\t "])
def test_blank_credentials_read_as_absent(monkeypatch, value):
    """Whitespace is truthy, which would make an unfilled form field look like a key.

    The account and trading tools would register and every signed call would fail — the
    server insisting it can reach your account while nothing works.
    """
    monkeypatch.setenv("DELTA_API_KEY", value)
    monkeypatch.setenv("DELTA_API_SECRET", value)
    cfg = config_mod.load()
    assert (cfg.api_key, cfg.api_secret) == (None, None)
    assert cfg.has_credentials is False


def test_a_pasted_credential_keeps_its_trailing_newline_out(monkeypatch):
    """Copying from the dashboard brings a newline, which breaks signing, not the load."""
    monkeypatch.setenv("DELTA_API_KEY", "  a-real-key\n")
    monkeypatch.setenv("DELTA_API_SECRET", "a-real-secret\n")
    cfg = config_mod.load()
    assert (cfg.api_key, cfg.api_secret) == ("a-real-key", "a-real-secret")


def test_a_whitespace_only_secret_leaves_the_pair_incomplete(monkeypatch):
    """Whitespace is truthy, so this has to read as a missing secret, not a present one."""
    monkeypatch.setenv("DELTA_API_KEY", "a-real-key")
    monkeypatch.setenv("DELTA_API_SECRET", "   ")
    cfg = config_mod.load()
    assert cfg.has_credentials is False


def test_credentials_loaded_from_env(monkeypatch):
    monkeypatch.setenv("DELTA_API_KEY", "k")
    monkeypatch.setenv("DELTA_API_SECRET", "s")
    cfg = config_mod.load()
    assert cfg.api_key == "k"
    assert cfg.api_secret == "s"
    assert cfg.has_credentials is True


def test_partial_credentials_do_not_count(monkeypatch):
    monkeypatch.setenv("DELTA_API_KEY", "k")
    monkeypatch.delenv("DELTA_API_SECRET", raising=False)
    cfg = config_mod.load()
    assert cfg.has_credentials is False


def test_debug_off_by_default(monkeypatch):
    monkeypatch.delenv("DELTA_MCP_DEBUG", raising=False)
    assert config_mod.load().debug is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "ON", " True "])
def test_debug_truthy_values(monkeypatch, value):
    monkeypatch.setenv("DELTA_MCP_DEBUG", value)
    assert config_mod.load().debug is True


@pytest.mark.parametrize("value", ["0", "false", "", "no"])
def test_debug_falsy_values(monkeypatch, value):
    monkeypatch.setenv("DELTA_MCP_DEBUG", value)
    assert config_mod.load().debug is False
