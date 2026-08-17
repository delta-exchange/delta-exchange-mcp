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
def test_blank_env_and_mode_fall_back_to_defaults(monkeypatch, value):
    """A bundle substitutes every declared variable, so a cleared form field arrives blank.

    Treating that as invalid stopped the server from starting at all, which is a worse
    outcome than the default — and for mode the default is also the safe direction.
    """
    monkeypatch.setenv("DELTA_MCP_ENV", value)
    monkeypatch.setenv("DELTA_MCP_MODE", value)
    cfg = config_mod.load()
    assert cfg.env == "india_prod"
    assert cfg.mode == "read"


@pytest.mark.parametrize("value", ["", "   ", "\n", "\t "])
def test_blank_credentials_read_as_absent(monkeypatch, value):
    """Whitespace is truthy, which would make an unfilled form field look like a key.

    The account tools would register, `partial_credentials` would stay false so the startup
    warning never fires, and every signed call would fail — the server insisting it can read
    your account while nothing works.
    """
    monkeypatch.setenv("DELTA_API_KEY", value)
    monkeypatch.setenv("DELTA_API_SECRET", value)
    cfg = config_mod.load()
    assert (cfg.api_key, cfg.api_secret) == (None, None)
    assert cfg.has_credentials is False
    assert cfg.partial_credentials is False


def test_a_pasted_credential_keeps_its_trailing_newline_out(monkeypatch):
    """Copying from the dashboard brings a newline, which breaks signing, not the load."""
    monkeypatch.setenv("DELTA_API_KEY", "  a-real-key\n")
    monkeypatch.setenv("DELTA_API_SECRET", "a-real-secret\n")
    cfg = config_mod.load()
    assert (cfg.api_key, cfg.api_secret) == ("a-real-key", "a-real-secret")


def test_a_whitespace_only_key_is_not_a_half_supplied_pair(monkeypatch):
    """The case the warning was added for, arriving as whitespace rather than as unset."""
    monkeypatch.setenv("DELTA_API_KEY", "a-real-key")
    monkeypatch.setenv("DELTA_API_SECRET", "   ")
    cfg = config_mod.load()
    assert cfg.has_credentials is False
    # Reported, not silent: this is exactly what the startup warning exists to say.
    assert cfg.partial_credentials is True


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


@pytest.mark.parametrize(
    ("key", "secret", "partial"),
    [
        ("k", "s", False),
        (None, None, False),
        ("k", None, True),
        (None, "s", True),
    ],
)
def test_partial_credentials_detects_a_half_supplied_pair(monkeypatch, key, secret, partial):
    """A key without its secret yields public-data mode; the config has to say so."""
    for name, value in (("DELTA_API_KEY", key), ("DELTA_API_SECRET", secret)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    cfg = config_mod.load()
    assert cfg.partial_credentials is partial
    assert cfg.has_credentials is (key is not None and secret is not None)


def test_scoped_mode_keys_bind_the_exact_reported_client_name():
    variants = ["foo-bar", "foo bar", "foo.bar", " foo-bar", "foo-bar "]
    keys = {config_mod.mode_key(name) for name in variants}
    assert len(keys) == len(variants)
    assert all(key.startswith("DELTA_MCP_MODE_") for key in keys)


def test_reaching_the_server_through_a_proxy_keeps_one_trading_key():
    """`mcp-remote` puts itself *and its own version* inside the client's name.

    It reports `claude-ai (via mcp-remote 0.1.37)` rather than `claude-ai`. With that in
    the key, the same person on the same machine gets one entitlement natively and another
    through the bridge — and upgrading the bridge, which has nothing to do with trading,
    moves the key again and silently switches trading off. Someone who turned it on once
    should not lose it to an unrelated tool upgrade.
    """
    native = config_mod.mode_key("claude-ai")
    old_bridge = config_mod.mode_key("claude-ai (via mcp-remote 0.1.37)")
    new_bridge = config_mod.mode_key("claude-ai (via mcp-remote 0.2.0)")
    assert native == old_bridge == new_bridge

    # Only a trailing proxy marker is removed. A parenthesis a client chose for itself is
    # part of who it says it is, and two clients that genuinely differ must keep differing.
    assert config_mod.mode_key("Some Client (beta)") != config_mod.mode_key("Some Client")
    assert config_mod.mode_key("claude-ai") != config_mod.mode_key("claude-code")


def test_the_name_a_proxy_rewrote_is_still_reported_in_full():
    """Analytics wants the bridge visible; only the stored key is normalised.

    Which proxy a request came through, and which version of it, is exactly the sort of
    thing worth counting — so the normalisation must not reach the name itself.
    """
    reported = "claude-ai (via mcp-remote 0.1.37)"
    assert config_mod.stable_name(reported) == "claude-ai"
    assert reported == "claude-ai (via mcp-remote 0.1.37)"


def test_punctuation_only_client_names_still_make_legal_distinct_keys():
    first = config_mod.mode_key("!!!")
    second = config_mod.mode_key("???")
    assert first != second
    assert first.startswith("DELTA_MCP_MODE_CLIENT_")
    assert first.isascii() and first.replace("_", "").isalnum()


@pytest.mark.parametrize("client", ["", " ", "\n\t"])
def test_an_absent_or_whitespace_only_client_has_no_mode_binding(client):
    assert config_mod.mode_key(client) == ""
