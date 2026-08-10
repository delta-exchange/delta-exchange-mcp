import pytest


@pytest.fixture(autouse=True)
def isolate_shared_store(tmp_path, monkeypatch):
    """Point the shared settings file at a throwaway path for every test.

    `config.load` falls back to `~/.delta-exchange-mcp/config.env`, so without this a
    developer's own credentials would leak into the suite — `test_defaults_to_india_prod`
    asserts no credentials are present, and would fail on any machine where that file is
    filled in. It would also mean the suite created and wrote a real file in `$HOME`.

    Autouse rather than opt-in: a test that forgets it fails only on some machines, which
    is the worst kind of test to own.
    """
    monkeypatch.setenv("DELTA_MCP_CONFIG_FILE", str(tmp_path / "config.env"))
