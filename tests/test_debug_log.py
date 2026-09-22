import logging

import httpx
import pytest
import respx

from delta_exchange_mcp import debug_log
from delta_exchange_mcp.client import DeltaClient
from delta_exchange_mcp.config import INDIA_TESTNET_REST, Config


@pytest.fixture(autouse=True)
def _clear_handlers():
    """Each test attaches a FileHandler to module loggers; remove them after so the open
    file doesn't leak into the next test (and the idempotency guard starts fresh)."""
    yield
    debug_log.shutdown()
    for name in debug_log.LOGGER_NAMES:
        logger = logging.getLogger(name)
        logger.setLevel(logging.NOTSET)
        logger.propagate = True


def _cfg(tmp_path, **kw):
    return Config(env="india_testnet", base_url=INDIA_TESTNET_REST, **kw)


@pytest.mark.asyncio
@respx.mock
async def test_logs_request_and_body_but_no_secrets(tmp_path, monkeypatch):
    log_file = tmp_path / "d.log"
    monkeypatch.setenv("DELTA_MCP_DEBUG_FILE", str(log_file))
    cfg = _cfg(tmp_path, api_key="APIKEY123", api_secret="SUPERSECRET", debug=True)
    path = debug_log.configure(cfg)
    assert path == log_file

    route = respx.get(f"{INDIA_TESTNET_REST}/wallet/transactions").mock(
        return_value=httpx.Response(
            200,
            json={"success": True, "result": [{"transaction_type": "deposit"}], "meta": {"total_count": 3}},
        )
    )
    client = DeltaClient(cfg)
    await client.get("/wallet/transactions", params={"transaction_types": "deposit"}, auth=True)
    await client.aclose()

    for h in logging.getLogger("delta_exchange_mcp").handlers:
        h.flush()
    text = log_file.read_text()

    # Request + body are captured.
    assert "wallet/transactions" in text
    assert "transaction_types=deposit" in text
    assert "total_count" in text
    assert "200" in text
    # Credentials are never written.
    assert "SUPERSECRET" not in text
    assert "APIKEY123" not in text
    signature = route.calls[0].request.headers["signature"]
    assert signature not in text


@pytest.mark.asyncio
@respx.mock
async def test_logs_csv_body_for_raw_text_response(tmp_path, monkeypatch):
    log_file = tmp_path / "d.log"
    monkeypatch.setenv("DELTA_MCP_DEBUG_FILE", str(log_file))
    cfg = _cfg(tmp_path, api_key="k", api_secret="s", debug=True)
    debug_log.configure(cfg)

    csv_body = b"Time,Contract,Side\n2026-01-01,BTCUSD,buy\n"
    respx.get(f"{INDIA_TESTNET_REST}/fills/history/download/csv").mock(
        return_value=httpx.Response(200, content=csv_body, headers={"content-type": "text/csv"})
    )
    client = DeltaClient(cfg)
    await client.get_raw("/fills/history/download/csv", auth=True)
    await client.aclose()

    for h in logging.getLogger("delta_exchange_mcp").handlers:
        h.flush()
    text = log_file.read_text()
    assert "Time,Contract,Side" in text  # raw CSV body captured, not just byte count


def test_debug_off_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("DELTA_MCP_DEBUG_FILE", str(tmp_path / "d.log"))
    assert debug_log.configure(_cfg(tmp_path, debug=False)) is None
    assert not (tmp_path / "d.log").exists()


def test_shutdown_detaches_and_closes_shared_handler(tmp_path, monkeypatch):
    monkeypatch.setenv("DELTA_MCP_DEBUG_FILE", str(tmp_path / "d.log"))
    debug_log.configure(_cfg(tmp_path, debug=True))

    handler = logging.getLogger("delta_exchange_mcp").handlers[0]
    assert all(handler in logging.getLogger(name).handlers for name in debug_log.LOGGER_NAMES)
    assert handler.stream is not None

    debug_log.shutdown()

    assert all(handler not in logging.getLogger(name).handlers for name in debug_log.LOGGER_NAMES)
    assert handler.stream is None
    debug_log.shutdown()  # idempotent


def test_shutdown_restores_each_logger_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("DELTA_MCP_DEBUG_FILE", str(tmp_path / "d.log"))
    expected = {
        "delta_exchange_mcp": (logging.WARNING, True),
        "httpx": (logging.ERROR, False),
    }
    for name, (level, propagate) in expected.items():
        logger = logging.getLogger(name)
        logger.setLevel(level)
        logger.propagate = propagate

    debug_log.configure(_cfg(tmp_path, debug=True))
    debug_log.shutdown()

    assert {
        name: (logging.getLogger(name).level, logging.getLogger(name).propagate)
        for name in expected
    } == expected


def test_log_file_is_owner_only(tmp_path, monkeypatch):
    import os
    import stat

    if os.name == "nt":
        pytest.skip("no POSIX file permissions on Windows")
    log_file = tmp_path / "d.log"
    monkeypatch.setenv("DELTA_MCP_DEBUG_FILE", str(log_file))
    debug_log.configure(_cfg(tmp_path, api_key="k", api_secret="s", debug=True))
    assert stat.S_IMODE(log_file.stat().st_mode) == 0o600
