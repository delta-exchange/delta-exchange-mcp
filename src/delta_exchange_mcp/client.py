from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from delta_exchange_mcp import analytics, request
from delta_exchange_mcp.config import Config
from delta_exchange_mcp.errors import DeltaApiError
from delta_exchange_mcp.version import PACKAGE_VERSION

logger = logging.getLogger("delta_exchange_mcp")

# Cap on how much of a response body we log, so a huge paginated payload can't bloat the file.
_BODY_LOG_CAP = 50_000

USER_AGENT = f"delta-exchange-mcp/{PACKAGE_VERSION}"


def sign(secret: str, method: str, timestamp: str, path: str, query: str, body: str) -> str:
    payload = f"{method}{timestamp}{path}{query}{body}"
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


@dataclass
class _ClientState:
    """One coherent HTTP destination and signing identity for a request."""

    config: Config
    base_path: str
    http: httpx.AsyncClient
    active: int = 0
    retired: bool = False


class DeltaClient:
    def __init__(self, config: Config, http: httpx.AsyncClient | None = None):
        # Held on the client rather than at module scope so two servers in one process do
        # not share identifiers, and so nothing outlives the client that minted them.
        self.sessions = analytics.Sessions()
        self._state = self._new_state(config, http)
        self._pinned_state: ContextVar[_ClientState | None] = ContextVar(
            f"delta_client_state_{id(self)}", default=None
        )
        self._retired: dict[int, _ClientState] = {}
        self._closing: set[asyncio.Task[None]] = set()

    @staticmethod
    def _new_state(config: Config, http: httpx.AsyncClient | None = None) -> _ClientState:
        # Delta signs the FULL path including the `/v2` prefix; httpx joins base_url+path
        # at request time, but `sign()` only sees the relative path we pass in. Capture
        # the prefix once so authed calls can produce the documented payload shape.
        base_path = urlparse(config.base_url).path.rstrip("/")
        transport = http or httpx.AsyncClient(
            base_url=config.base_url,
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=30.0),
            headers={
                "User-Agent": USER_AGENT,
                "Source": USER_AGENT,
                "Accept": "application/json",
            },
        )
        return _ClientState(config=config, base_path=base_path, http=transport)

    @property
    def config(self) -> Config:
        return self._state.config

    def rebind(self, config: Config) -> None:
        """Atomically move future calls to ``config`` without disrupting current calls.

        Tool closures keep this one client for the process lifetime. Swapping one immutable
        state means market and account tools move environments and credential pairs together;
        a request already in progress finishes on the state it captured.
        """
        old = self._state
        identity = (config.base_url, config.api_key, config.api_secret)
        old_identity = (old.config.base_url, old.config.api_key, old.config.api_secret)
        if identity == old_identity:
            # HTTP identity is unchanged, so mutating the non-request metadata cannot make
            # an in-flight signature incoherent and avoids two states sharing one transport.
            old.config = config
            return
        self._state = self._new_state(config)
        old.retired = True
        self._retired[id(old)] = old
        if old.active == 0:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # A synchronous caller has no place to await cleanup; aclose remains the
                # backstop. Runtime reconciliation always has a loop and closes promptly.
                return
            task = loop.create_task(self._close_retired(old))
            self._closing.add(task)
            task.add_done_callback(self._closing_done)

    def _closing_done(self, task: asyncio.Task[None]) -> None:
        self._closing.discard(task)
        try:
            task.result()
        except Exception as exc:  # noqa: BLE001 - cleanup failure is logged and retried at shutdown
            logger.warning("could not close retired HTTP transport: %s", exc)

    @asynccontextmanager
    async def pin(self) -> AsyncIterator[None]:
        """Keep a multi-request operation on one destination and signing identity.

        A trading tool may await a product or profile lookup before sending its mutation.
        Reconciliation can happen during that await; task-local pinning prevents the later
        request from jumping to the newly rebound account.
        """
        existing = self._pinned_state.get()
        if existing is not None:
            yield
            return
        state = self._state
        state.active += 1
        token = self._pinned_state.set(state)
        try:
            yield
        finally:
            self._pinned_state.reset(token)
            await self._release(state)

    async def _release(self, state: _ClientState) -> None:
        state.active -= 1
        if state.retired and state.active == 0:
            await self._close_retired(state)

    async def _close_retired(self, state: _ClientState) -> None:
        if state.active != 0 or self._retired.get(id(state)) is not state:
            return
        await state.http.aclose()
        if self._retired.get(id(state)) is state:
            del self._retired[id(state)]

    async def aclose(self) -> None:
        if self._closing:
            await asyncio.gather(*tuple(self._closing), return_exceptions=True)
        clients = [self._state.http, *(state.http for state in self._retired.values())]
        self._retired.clear()
        for client in dict.fromkeys(clients):
            await client.aclose()

    async def get(self, path: str, params: dict[str, Any] | None = None, *, auth: bool = False) -> Any:
        return await self._request("GET", path, params=params, auth=auth)

    async def get_raw(self, path: str, params: dict[str, Any] | None = None, *, auth: bool = False) -> bytes:
        """Like get(), but returns the response body bytes without JSON unwrap.

        Used for binary/CSV endpoints (e.g. /fills/history/download/csv). JSON error
        envelopes are still inspected — a {success: false, ...} body raises DeltaApiError.
        """
        return await self._request("GET", path, params=params, auth=auth, raw=True)

    async def post(self, path: str, json_body: Any = None, *, auth: bool = False) -> Any:
        return await self._request("POST", path, json_body=json_body, auth=auth)

    async def put(self, path: str, json_body: Any = None, *, auth: bool = False) -> Any:
        return await self._request("PUT", path, json_body=json_body, auth=auth)

    async def delete(self, path: str, json_body: Any = None, *, auth: bool = False) -> Any:
        return await self._request("DELETE", path, json_body=json_body, auth=auth)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        auth: bool = False,
        raw: bool = False,
    ) -> Any:
        pinned = self._pinned_state.get()
        state = pinned or self._state
        if pinned is not None:
            return await self._request_state(
                state,
                method,
                path,
                params=params,
                json_body=json_body,
                auth=auth,
                raw=raw,
            )
        state.active += 1
        try:
            return await self._request_state(
                state,
                method,
                path,
                params=params,
                json_body=json_body,
                auth=auth,
                raw=raw,
            )
        finally:
            await self._release(state)

    async def _request_state(
        self,
        state: _ClientState,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        auth: bool = False,
        raw: bool = False,
    ) -> Any:
        # Captured once: a form save may rebind the process while this request is awaiting
        # the network, and its base URL, signing prefix and credential pair must stay one tuple.
        config = state.config
        # Who asked, and for what. Built per request because the tool changes with every
        # call and a form save can rebind the environment and mode under a live connection.
        # Never signed: the payload is method, timestamp, path, query and body only, so
        # these carry no authentication weight and adding one cannot break a signature.
        current = request.session.get()
        headers: dict[str, str] = analytics.headers(
            current,
            request.tool.get(),
            config,
            self.sessions,
            request.peer(current),
        )
        # Delta signs the EXACT request body bytes. Serialize once (compact, no spaces) and
        # feed the same string to both sign() and httpx via content= — using json= would let
        # httpx re-serialize with different spacing and break the signature. Mirrors the
        # "filter params once, feed both signing and the request" pattern below.
        body_str = ""
        content: bytes | None = None
        if json_body is not None:
            body_str = json.dumps(json_body, separators=(",", ":"))
            content = body_str.encode()
            headers["Content-Type"] = "application/json"
        query_str = ""
        filtered_params = {k: v for k, v in (params or {}).items() if v is not None} or None
        if filtered_params:
            query_str = "?" + httpx.QueryParams(filtered_params).__str__()

        if auth:
            if not config.has_credentials:
                raise DeltaApiError("credentials_missing", context="set DELTA_API_KEY and DELTA_API_SECRET")
            ts = str(int(time.time()))
            signature = sign(
                config.api_secret or "",  # guarded by has_credentials
                method,
                ts,
                f"{state.base_path}{path}",
                query_str,
                body_str,
            )
            headers["api-key"] = config.api_key or ""
            headers["signature"] = signature
            headers["timestamp"] = ts

        # body_str carries no credentials (those live only in headers, never logged).
        logger.info(
            "→ %s %s params=%s auth=%s body=%s",
            method, f"{state.base_path}{path}", filtered_params, auth, body_str[:_BODY_LOG_CAP],
        )

        last_error: Exception | None = None
        for attempt in range(3):
            try:
                resp = await state.http.request(
                    method, path, params=filtered_params, content=content, headers=headers
                )
            except httpx.HTTPError as e:
                last_error = e
                if attempt == 2:
                    raise
                continue

            if resp.status_code == 429 and method == "GET" and attempt < 2:
                reset_ms = int(resp.headers.get("X-RATE-LIMIT-RESET", "1000"))
                await asyncio.sleep(min(reset_ms, 5000) / 1000.0)
                continue
            if 500 <= resp.status_code < 600 and method == "GET" and attempt < 2:
                await asyncio.sleep(0.5 * (2**attempt))
                continue

            if raw:
                ctype = resp.headers.get("content-type", "")
                # Log a capped textual body for text/CSV/JSON so a bad export (e.g. the
                # /fills/history/download/csv account export) leaves usable wire evidence;
                # only fall back to byte-count for genuinely binary payloads.
                if any(t in ctype.lower() for t in ("text", "csv", "json")):
                    logger.info(
                        "← %s %s %s content-type=%s body=%s",
                        method, path, resp.status_code, ctype, resp.text[:_BODY_LOG_CAP],
                    )
                else:
                    logger.info(
                        "← %s %s %s content-type=%s bytes=%s",
                        method, path, resp.status_code, ctype, len(resp.content),
                    )
            else:
                logger.info(
                    "← %s %s %s body=%s", method, path, resp.status_code, resp.text[:_BODY_LOG_CAP]
                )
            try:
                return self._unwrap_raw(resp) if raw else self._unwrap(resp)
            except DeltaApiError as e:
                logger.info("✗ %s %s code=%s status=%s", method, path, e.code, e.status)
                raise

        assert last_error is not None
        raise last_error

    @staticmethod
    def _unwrap_raw(resp: httpx.Response) -> bytes:
        # Even raw endpoints may return a JSON error envelope on failure — inspect that
        # path before handing the caller a bytes blob.
        ctype = resp.headers.get("content-type", "")
        if "json" in ctype.lower():
            try:
                data = resp.json()
            except ValueError:
                data = None
            if isinstance(data, dict) and data.get("success") is False:
                err = data.get("error") or {}
                raise DeltaApiError(
                    code=err.get("code", "unknown"),
                    context=err.get("context"),
                    status=resp.status_code,
                )
        if resp.status_code >= 400:
            raise DeltaApiError("http_error", context=resp.text[:500], status=resp.status_code)
        return resp.content

    @staticmethod
    def _unwrap(resp: httpx.Response) -> Any:
        try:
            data = resp.json()
        except ValueError:
            raise DeltaApiError("invalid_response", context=resp.text[:500], status=resp.status_code)

        if isinstance(data, dict) and data.get("success") is False:
            err = data.get("error") or {}
            raise DeltaApiError(
                code=err.get("code", "unknown"),
                context=err.get("context"),
                status=resp.status_code,
            )
        if resp.status_code >= 400:
            raise DeltaApiError("http_error", context=data, status=resp.status_code)
        if isinstance(data, dict) and "result" in data:
            return {"result": data["result"], "meta": data.get("meta")}
        return data
