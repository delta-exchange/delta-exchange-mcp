"""Serve the account setup flow on a short-lived loopback listener."""

import asyncio
import hmac
import json
import logging
import secrets
import threading
import webbrowser
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from delta_exchange_mcp import config as config_mod
from delta_exchange_mcp import credentials, form, store

LIFETIME_SECONDS = 10 * 60

_LOOPBACK = "127.0.0.1"
_MAX_BODY = 64 * 1024
_SESSION_COOKIE = "delta_mcp_setup"
_ACTIONS = frozenset({"status", "credentials", "consent"})
_MUTATIONS = frozenset({"credentials", "consent"})
_JSON_CSP = "default-src 'none'; base-uri 'none'; frame-ancestors 'none'"

logger = logging.getLogger(__name__)

type Revision = int | dict[str, int]


@dataclass(frozen=True)
class ActionResult:
    """Return one secret-free action result and its authoritative revision."""

    content: dict[str, Any]
    revision: Revision
    stale: bool = False
    complete: bool = False


type ActionHandler = Callable[[str, Mapping[str, Any], Revision], ActionResult]


@dataclass(frozen=True)
class _Reply:
    status: int
    body: dict[str, Any]
    csrf_token: str = ""
    revision: Revision | None = None
    complete: bool = False


@dataclass
class _Flow:
    """Own the session, CSRF token, revision, and mutation lock for one page."""

    actions: ActionHandler
    revision: Revision = 0
    session: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    _csrf_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def open(self) -> tuple[str, Revision]:
        """Rotate the token when the page loads so an older tab becomes stale."""
        with self._lock:
            self._csrf_token = secrets.token_urlsafe(32)
            return self._csrf_token, _copy_revision(self.revision)

    def accepts_session(self, offered: str) -> bool:
        """Check the browser-only session value without timing-sensitive equality."""
        return bool(offered) and hmac.compare_digest(self.session, offered)

    def dispatch(self, payload: Mapping[str, Any]) -> _Reply:
        """Validate and serialize one direct browser action."""
        action = payload.get("action")
        arguments = payload.get("arguments", {})
        offered = payload.get("csrf_token")
        if action not in _ACTIONS or not isinstance(arguments, dict):
            return _Reply(400, _error("unknown action"))
        if not isinstance(offered, str):
            return _Reply(403, _error("form session is not valid"))

        with self._lock:
            if not hmac.compare_digest(self._csrf_token, offered):
                return _Reply(409, _error("this form is stale; reload it and try again"))

            self._csrf_token = secrets.token_urlsafe(32)
            next_csrf = self._csrf_token

            expected = payload.get("expected_revision")
            if action in _MUTATIONS and not _valid_revision(expected):
                return _Reply(
                    400,
                    _error("expected revision is not valid"),
                    csrf_token=next_csrf,
                    revision=self.revision,
                )
            supplied_revision = expected if _valid_revision(expected) else self.revision

            try:
                result = self.actions(
                    action, arguments, _copy_revision(supplied_revision)
                )
            except Exception as exc:
                logger.error("Browser setup action failed with %s", type(exc).__name__)
                return _Reply(
                    500,
                    _error("the action failed"),
                    csrf_token=next_csrf,
                    revision=self.revision,
                )

            if not isinstance(result, ActionResult):
                logger.error("Browser setup action returned %s", type(result).__name__)
                return _Reply(
                    500,
                    _error("the action failed"),
                    csrf_token=next_csrf,
                    revision=self.revision,
                )

            if not _valid_revision(result.revision):
                logger.error("Browser setup action returned an invalid revision")
                return _Reply(
                    500,
                    _error("the action failed"),
                    csrf_token=next_csrf,
                    revision=self.revision,
                )

            self.revision = _copy_revision(result.revision)
            if result.stale:
                message = str(
                    result.content.get("message")
                    or "the connection changed; reload the page and try again"
                )
                return _Reply(
                    409,
                    _error(message),
                    csrf_token=next_csrf,
                    revision=self.revision,
                )
            return _Reply(
                200,
                {"result": {"structuredContent": result.content}},
                csrf_token=next_csrf,
                revision=self.revision,
                complete=result.complete,
            )


@dataclass
class Page:
    """A running browser setup page and where to find it."""

    url: str
    server: ThreadingHTTPServer
    saved: threading.Event
    _stopped: threading.Event = field(default_factory=threading.Event)
    _done: threading.Event = field(default_factory=threading.Event)
    _stop_lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def running(self) -> bool:
        """Return whether the address can still accept a setup action."""
        return not self._stopped.is_set() and not self.saved.is_set()

    def stop(self) -> None:
        """Close the listener once and wake callers waiting for the flow."""
        with self._stop_lock:
            if self._stopped.is_set():
                return
            self._stopped.set()
            self.server.shutdown()
            self.server.server_close()
            self._done.set()

    def wait(self, timeout: float = LIFETIME_SECONDS) -> bool:
        """Wait for completion or closure and report whether the flow completed."""
        self._done.wait(timeout)
        return self.saved.is_set()


class _Handler(BaseHTTPRequestHandler):
    """Serve one bound flow. ``serve`` sets the class attributes per listener."""

    locator: str = ""
    origin: str = ""
    flow: _Flow
    completed: threading.Event

    def log_message(self, *args: Any) -> None:
        """Keep HTTP request data out of the MCP process standard streams."""

    def do_GET(self) -> None:
        if not self._valid_host() or self.path != f"/{self.locator}":
            self._send_json(404, _error("not found"))
            return

        csrf_token, revision = self.flow.open()
        nonce = secrets.token_urlsafe(24)
        body = form.page_html(
            f"/{self.locator}/rpc",
            csrf_token=csrf_token,
            revision=revision,
            nonce=nonce,
        ).encode()
        self.send_response(200)
        self._send_security_headers(
            "text/html; charset=utf-8",
            len(body),
            csp=(
                "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
                f"style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; "
                "connect-src 'self'"
            ),
        )
        self.send_header(
            "Set-Cookie",
            (
                f"{_SESSION_COOKIE}={self.flow.session}; Path=/{self.locator}; "
                f"Max-Age={int(LIFETIME_SECONDS)}; HttpOnly; SameSite=Strict"
            ),
        )
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if not self._valid_host() or self.path != f"/{self.locator}/rpc":
            self._send_json(404, _error("not found"))
            return
        if self.headers.get("Origin") != f"http://{self.origin}":
            self._send_json(403, _error("origin is not allowed"))
            return
        if not self.flow.accepts_session(self._session_cookie()):
            self._send_json(403, _error("form session is not valid"))
            return
        content_type = self.headers.get("Content-Type", "")
        if content_type.partition(";")[0].strip().lower() != "application/json":
            self._send_json(415, _error("content type must be application/json"))
            return

        length = self._content_length()
        if length is None:
            return
        try:
            payload = json.loads(
                self.rfile.read(length),
                parse_constant=_reject_json_constant,
            )
        except (RecursionError, UnicodeDecodeError, ValueError):
            self._send_json(400, _error("request body must be valid JSON"))
            return
        if not isinstance(payload, dict):
            self._send_json(400, _error("request body must be a JSON object"))
            return

        reply = self.flow.dispatch(payload)
        self._send_json(
            reply.status,
            reply.body,
            csrf_token=reply.csrf_token,
            revision=reply.revision,
        )

        # The browser must receive the complete response before the watchdog closes the
        # listener. A completed credential action can deliberately leave this unset so a
        # separate consent action can follow on the same flow.
        if reply.complete:
            self.completed.set()

    def do_OPTIONS(self) -> None:
        self._send_json(405, _error("method not allowed"))

    def _valid_host(self) -> bool:
        return self.headers.get("Host") == self.origin

    def _session_cookie(self) -> str:
        raw = self.headers.get("Cookie", "")
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except CookieError:
            return ""
        value = cookie.get(_SESSION_COOKIE)
        return value.value if value is not None else ""

    def _content_length(self) -> int | None:
        raw = self.headers.get("Content-Length")
        if raw is None:
            self._send_json(411, _error("content length is required"))
            return None
        try:
            length = int(raw)
        except ValueError:
            self._send_json(400, _error("content length is not valid"))
            return None
        if length <= 0:
            self._send_json(400, _error("request body is empty"))
            return None
        if length > _MAX_BODY:
            self._send_json(413, _error("request body is too large"))
            return None
        return length

    def _send_json(
        self,
        status: int,
        value: dict[str, Any],
        *,
        csrf_token: str = "",
        revision: Revision | None = None,
    ) -> None:
        body_value = value if revision is None else value | {"revision": revision}
        body = json.dumps(body_value).encode()
        self.send_response(status)
        self._send_security_headers("application/json", len(body), csp=_JSON_CSP)
        if csrf_token:
            self.send_header("X-CSRF-Token", csrf_token)
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def _send_security_headers(self, content_type: str, length: int, *, csp: str) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", csp)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")


def _error(message: str) -> dict[str, dict[str, str]]:
    return {"error": {"message": message}}


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"{value} is not valid JSON")


def _valid_revision(value: Any) -> bool:
    if type(value) is int:
        return value >= 0
    return isinstance(value, dict) and all(
        isinstance(name, str) and type(revision) is int and revision >= 0
        for name, revision in value.items()
    )


def _copy_revision(value: Any) -> Revision:
    return dict(value) if isinstance(value, dict) else value


def _status(client: str) -> dict[str, Any]:
    shared = store.read()
    live = config_mod.load(shared)
    return {
        "environment": live.env,
        "credentials_configured": live.has_credentials,
        "mode": config_mod.mode_for_client(client, shared) if client else "read",
        "overridden_by_client": credentials.overridden_by_client(client, shared),
        "path": str(store.path()),
        "client_name": client,
        "mode_settable": bool(client),
    }


def _legacy_actions(client: str) -> ActionHandler:
    """Adapt the branch's file store until the secure store supplies this callback."""

    current_revision = 0

    def run(action: str, args: Mapping[str, Any], revision: Revision) -> ActionResult:
        nonlocal current_revision
        if action == "status":
            return ActionResult(_status(client), revision=current_revision)
        if revision != current_revision:
            return ActionResult(
                {"message": "the connection changed; reload the page and try again"},
                revision=current_revision,
                stale=True,
            )
        if action == "consent":
            result = _legacy_consent(client, args, current_revision)
        else:
            result = _legacy_credentials(client, args, current_revision)
        current_revision = result.revision
        return result

    return run


def _legacy_consent(
    client: str, args: Mapping[str, Any], revision: int
) -> ActionResult:
    if not client:
        return ActionResult(
            {"status": "rejected", "message": _NO_CLIENT}, revision=revision
        )
    failure = credentials.save_mode(client, str(args.get("mode") or "read"))
    if failure:
        return ActionResult(
            {"status": "rejected", "message": failure}, revision=revision
        )
    return ActionResult(
        {"status": "saved", "message": "Saved.", "mode_updated": True},
        revision=revision + 1,
        complete=True,
    )


def _legacy_credentials(
    client: str, args: Mapping[str, Any], revision: int
) -> ActionResult:
    env = str(args.get("environment") or "")
    key = str(args.get("api_key") or "")
    secret = str(args.get("api_secret") or "")
    if not (env and key and secret):
        return ActionResult(
            {"status": "rejected", "message": "Fill in every field."},
            revision=revision,
        )

    checked = asyncio.run(credentials.check(env, key, secret))
    if not checked.ok and checked.reachable:
        return ActionResult(
            {"status": "rejected", "message": form.rejection(env, checked)},
            revision=revision,
        )

    mode = str(args.get("mode") or "")
    failure = credentials.save(env, key, secret, client=client, mode=mode if client else "")
    if failure:
        return ActionResult(
            {"status": "rejected", "message": failure}, revision=revision
        )

    common = {
        "path": str(store.path()),
        "next_step": "Restart your MCP client so it picks up the new settings.",
        "overridden_by_client": credentials.overridden_by_client(client, store.read()),
    }
    if not checked.reachable:
        return ActionResult(
            common
            | {
                "status": "unverified",
                "message": (
                    f"Saved to {store.path()}, but Delta could not be reached to check it. "
                    f"{checked.detail}"
                ),
            },
            revision=revision + 1,
            complete=True,
        )
    return ActionResult(
        common | {"status": "saved", "account": checked.detail},
        revision=revision + 1,
        complete=True,
    )


_NO_CLIENT = (
    "Trading is turned on for one app at a time, and this page was opened from a terminal "
    "rather than from an app. Ask your assistant to connect your Delta account instead."
)


def serve(
    client: str = "",
    open_browser: bool = True,
    *,
    actions: ActionHandler | None = None,
    revision: Revision = 0,
) -> Page:
    """Start a setup flow on an operating-system-selected loopback port.

    Before a mutation, ``actions`` must compare the supplied revision with the current
    credential revision and consent generation in durable metadata. It must return that
    authoritative state in ``ActionResult.revision`` for both success and conflict.
    """
    if not _valid_revision(revision):
        raise ValueError("revision must contain non-negative integers")
    locator = secrets.token_urlsafe(24)
    completed = threading.Event()
    flow = _Flow(actions=actions or _legacy_actions(client), revision=revision)
    server = ThreadingHTTPServer((_LOOPBACK, 0), _Handler)
    server.daemon_threads = True
    origin = f"{_LOOPBACK}:{server.server_address[1]}"
    server.RequestHandlerClass = type(
        "_BoundHandler",
        (_Handler,),
        {
            "locator": locator,
            "origin": origin,
            "flow": flow,
            "completed": completed,
        },
    )

    threading.Thread(target=server.serve_forever, daemon=True).start()
    page = Page(url=f"http://{origin}/{locator}", server=server, saved=completed)

    def close_when_done() -> None:
        completed.wait(LIFETIME_SECONDS)
        page.stop()

    threading.Thread(target=close_when_done, daemon=True).start()

    if open_browser:
        try:
            webbrowser.open(page.url)
        except OSError:
            pass
    return page
