from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from importlib.metadata import PackageNotFoundError, version
from typing import Any
from urllib.parse import urlparse

import httpx

from delta_exchange_mcp.config import Config
from delta_exchange_mcp.errors import DeltaApiError

try:
    USER_AGENT = f"delta-exchange-mcp/{version('delta-exchange-mcp')}"
except PackageNotFoundError:
    USER_AGENT = "delta-exchange-mcp/0+unknown"


def sign(secret: str, method: str, timestamp: str, path: str, query: str, body: str) -> str:
    payload = f"{method}{timestamp}{path}{query}{body}"
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


class DeltaClient:
    def __init__(self, config: Config, http: httpx.AsyncClient | None = None):
        self.config = config
        # Delta signs the FULL path including the `/v2` prefix; httpx joins base_url+path
        # at request time, but `sign()` only sees the relative path we pass in. Capture
        # the prefix once so authed calls can produce the documented payload shape.
        self._base_path = urlparse(config.base_url).path.rstrip("/")
        self._http = http or httpx.AsyncClient(
            base_url=config.base_url,
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=30.0),
            headers={
                "User-Agent": USER_AGENT,
                "Source": USER_AGENT,
                "Accept": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get(self, path: str, params: dict[str, Any] | None = None, *, auth: bool = False) -> Any:
        return await self._request("GET", path, params=params, auth=auth)

    async def get_raw(self, path: str, params: dict[str, Any] | None = None, *, auth: bool = False) -> bytes:
        """Like get(), but returns the response body bytes without JSON unwrap.

        Used for binary/CSV endpoints (e.g. /fills/history/download/csv). JSON error
        envelopes are still inspected — a {success: false, ...} body raises DeltaApiError.
        """
        return await self._request("GET", path, params=params, auth=auth, raw=True)

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
        headers: dict[str, str] = {}
        body_str = ""  # TODO when POST lands in v2
        query_str = ""
        filtered_params = {k: v for k, v in (params or {}).items() if v is not None} or None
        if filtered_params:
            query_str = "?" + httpx.QueryParams(filtered_params).__str__()

        if auth:
            if not self.config.has_credentials:
                raise DeltaApiError("credentials_missing", context="set DELTA_API_KEY and DELTA_API_SECRET")
            ts = str(int(time.time()))
            signature = sign(
                self.config.api_secret or "",  # guarded by has_credentials
                method,
                ts,
                f"{self._base_path}{path}",
                query_str,
                body_str,
            )
            headers["api-key"] = self.config.api_key or ""
            headers["signature"] = signature
            headers["timestamp"] = ts

        last_error: Exception | None = None
        for attempt in range(3):
            try:
                resp = await self._http.request(
                    method, path, params=filtered_params, json=json_body, headers=headers
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

            return self._unwrap_raw(resp) if raw else self._unwrap(resp)

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
