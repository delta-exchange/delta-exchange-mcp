"""Exercise the browser setup boundary over a real loopback listener."""

import asyncio
import json
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from delta_exchange_mcp import credentials as credential_check
from delta_exchange_mcp import setup
from delta_exchange_mcp.auth.connection import ConnectionService
from delta_exchange_mcp.auth.consent import ConsentStore, MemoryConsentBackend
from delta_exchange_mcp.auth.store import (
    CredentialSource,
    CredentialStore,
    FileMetadata,
    MemorySecretBackend,
)
from delta_exchange_mcp.config import Config


@dataclass(frozen=True)
class Response:
    status: int
    text: str
    headers: Message

    @property
    def body(self) -> dict[str, Any]:
        return json.loads(self.text)


@dataclass
class FakeActions:
    calls: list[tuple[str, dict[str, Any], setup.Revision]] = field(
        default_factory=list
    )
    committed: list[str] = field(default_factory=list)
    credential_revision: int = 0
    consent_generation: int = 0
    entered: threading.Event | None = None
    release: threading.Event | None = None
    fail: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "credential": self.credential_revision,
                "consent": self.consent_generation,
            }

    def __call__(
        self,
        action: str,
        arguments: dict[str, Any],
        revision: setup.Revision,
    ) -> setup.ActionResult:
        with self._lock:
            current = {
                "credential": self.credential_revision,
                "consent": self.consent_generation,
            }
            self.calls.append((action, dict(arguments), revision))
            if self.fail:
                raise RuntimeError("callback failed")
            if action == "status":
                return setup.ActionResult(
                    {
                        "status": "ready",
                        "credentials_configured": self.credential_revision > 0,
                    },
                    revision=current,
                )
            if revision != current:
                return setup.ActionResult(
                    {"message": "the durable connection state changed"},
                    revision=current,
                    stale=True,
                )
            if self.entered is not None:
                self.entered.set()
            if self.release is not None:
                self.release.wait(5)
            self.committed.append(action)
            if action == "credentials":
                self.credential_revision += 1
                return setup.ActionResult(
                    {"status": "saved", "account": "someone@delta.exchange"},
                    revision={
                        "credential": self.credential_revision,
                        "consent": self.consent_generation,
                    },
                )
            self.consent_generation += 1
            return setup.ActionResult(
                {"status": "saved", "trading": "enabled"},
                revision={
                    "credential": self.credential_revision,
                    "consent": self.consent_generation,
                },
                complete=True,
            )


@dataclass
class Browser:
    page: setup.Page
    cookie: str = ""
    csrf_token: str = ""
    revision: setup.Revision = 0

    def open(self, *, host: str = "") -> Response:
        response = fetch(self.page.url, host=host)
        if response.status == 200:
            self.cookie = response.headers["Set-Cookie"].partition(";")[0]
            match = re.search(r"var CONFIG = (\{.*?\});", response.text)
            assert match is not None
            config = json.loads(match.group(1))
            self.csrf_token = config["csrf_token"]
            self.revision = config["revision"]
        return response

    def post(
        self,
        action: str,
        arguments: dict[str, Any] | None = None,
        *,
        csrf_token: str | None = None,
        revision: setup.Revision | None = None,
        origin: str | None = None,
        content_type: str = "application/json",
        raw: bytes | None = None,
        cookie: str | None = None,
        update: bool = True,
    ) -> Response:
        payload = {
            "action": action,
            "arguments": arguments or {},
            "csrf_token": self.csrf_token if csrf_token is None else csrf_token,
            "expected_revision": self.revision if revision is None else revision,
        }
        data = json.dumps(payload).encode() if raw is None else raw
        response = fetch(
            f"{self.page.url}/rpc",
            body=data,
            content_type=content_type,
            origin=self.origin if origin is None else origin,
            cookie=self.cookie if cookie is None else cookie,
        )
        if update:
            next_token = response.headers.get("X-CSRF-Token")
            if next_token:
                self.csrf_token = next_token
            try:
                body = response.body
            except json.JSONDecodeError:
                body = {}
            if isinstance(body.get("revision"), (int, dict)):
                self.revision = body["revision"]
        return response

    @property
    def origin(self) -> str:
        parsed = urlsplit(self.page.url)
        return f"{parsed.scheme}://{parsed.netloc}"


@pytest.fixture
def actions() -> FakeActions:
    return FakeActions()


@pytest.fixture
def page(actions: FakeActions):
    running = setup.serve(
        open_browser=False, actions=actions, revision=actions.snapshot()
    )
    yield running
    running.stop()


@pytest.fixture
def browser(page: setup.Page) -> Browser:
    opened = Browser(page)
    assert opened.open().status == 200
    return opened


def fetch(
    url: str,
    *,
    host: str = "",
    body: bytes | None = None,
    content_type: str = "application/json",
    origin: str = "",
    cookie: str = "",
) -> Response:
    request = urllib.request.Request(url, data=body)
    if host:
        request.add_header("Host", host)
    if body is not None:
        request.add_header("Content-Type", content_type)
    if origin:
        request.add_header("Origin", origin)
    if cookie:
        request.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return Response(response.status, response.read().decode(), response.headers)
    except urllib.error.HTTPError as refused:
        return Response(refused.code, refused.read().decode(), refused.headers)


def assert_security_headers(response: Response) -> None:
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


async def verified_candidate(
    environment: str, api_key: str, api_secret: str
) -> credential_check.Check:
    return credential_check.Check(ok=True, reachable=True, detail=f"account-{api_key}")


def connection_for(directory: Path, backend: MemorySecretBackend) -> ConnectionService:
    return ConnectionService.open(
        Config(env="india_prod", base_url="https://api.invalid/v2"),
        credentials=CredentialStore(
            backend,
            FileMetadata(directory / "credentials.json"),
            CredentialSource.OS_STORE,
        ),
        consent=ConsentStore(
            directory / "consent.json",
            secure_backend_available=True,
            memory_backend=MemoryConsentBackend(),
        ),
        validator=verified_candidate,
    )


@pytest.mark.parametrize("other_action", ["replace", "rotate", "disconnect"])
def test_browser_actions_in_another_folder_cannot_change_an_approved_account(
    tmp_path, other_action
) -> None:
    backend = MemorySecretBackend()
    first = connection_for(tmp_path / "first", backend)
    second = connection_for(tmp_path / "second", backend)
    try:
        browser = Browser(first.open_page("Codex"))
        assert browser.open().status == 200
        saved = browser.post(
            "credentials",
            {
                "environment": "india_prod",
                "api_key": "first-key",
                "api_secret": "first-secret",
            },
        )
        assert saved.status == 200
        approved = browser.post(
            "consent",
            {
                "environment": "india_prod",
                "enabled": True,
                "acknowledged": True,
            },
        )
        assert approved.status == 200
        assert approved.body["complete"] is True

        other = Browser(second.open_page("Codex"))
        assert other.open().status == 200
        changed = other.post(
            "credentials",
            {
                "environment": "india_prod",
                "api_key": "second-key",
                "api_secret": "second-secret",
            },
        )
        assert changed.status == 200
        if other_action != "replace":
            changed = other.post(
                "credentials",
                {
                    "operation": "disconnect"
                    if other_action == "disconnect"
                    else "replace",
                    "environment": "india_prod",
                    "api_key": "rotated-key",
                    "api_secret": "rotated-secret",
                },
            )
            assert changed.status == 200

        browser = Browser(first.open_page("Codex"))
        assert browser.open().status == 200
        status = browser.post("status").body["result"]["structuredContent"]
        assert status["environments"]["india_prod"]["account_id"] == "account-first-key"
        assert status["trading"]["enabled"] is True
        assert first.client.config.api_key == "first-key"
        assert first.client.config.api_secret == "first-secret"
        assert first.credentials.get("india_prod").api_key == "first-key"
    finally:
        for connection in (first, second):
            connection.close()
            asyncio.run(connection.client.aclose())


def test_old_draft_credentials_can_reconnect_through_the_browser(tmp_path) -> None:
    backend = MemorySecretBackend()
    old_name = "credential:india_prod:1"
    backend.set(old_name, "unowned record that must not be read")
    metadata_path = tmp_path / "credentials.json"
    metadata_path.write_text(
        json.dumps(
            {
                "version": 1,
                "environments": {
                    "india_prod": {
                        "active_revision": 1,
                        "next_revision": 2,
                        "generation": 1,
                        "state": "verified",
                        "account_id": "old-account",
                        "created_at": "",
                        "updated_at": "",
                        "validated_at": "",
                    }
                },
            }
        )
    )
    connection = connection_for(tmp_path, backend)
    try:
        browser = Browser(connection.open_page("Codex"))
        opened = browser.open()
        assert opened.status == 200
        assert "Reconnect once after this update" in opened.text
        status = browser.post("status").body["result"]["structuredContent"]
        assert status["credentials_configured"] is False
        assert status["environments"]["india_prod"]["reconnect_required"] is True
        assert status["environments"]["india_prod"]["account_id"] == ""
        assert status["trading"]["enabled"] is False
        saved = browser.post(
            "credentials",
            {
                "environment": "india_prod",
                "api_key": "new-key",
                "api_secret": "new-secret",
            },
        )
        assert saved.status == 200
        status = browser.post("status").body["result"]["structuredContent"]
        assert status["credentials_configured"] is True
        assert status["environments"]["india_prod"]["reconnect_required"] is False
        assert status["environments"]["india_prod"]["account_id"] == "account-new-key"
        assert status["trading"]["enabled"] is False
        assert backend.get(old_name) == "unowned record that must not be read"
    finally:
        connection.close()
        asyncio.run(connection.client.aclose())


def test_the_locator_finds_the_flow_but_does_not_authorize_a_post(
    page: setup.Page,
) -> None:
    browser = Browser(page)
    opened = browser.open()
    assert opened.status == 200
    assert "Manage Delta Exchange connection" in opened.text
    assert "HttpOnly" in opened.headers["Set-Cookie"]
    assert "SameSite=Strict" in opened.headers["Set-Cookie"]
    assert browser.cookie.partition("=")[2] not in page.url
    assert browser.csrf_token not in page.url

    wrong = page.url.rsplit("/", 1)[0] + "/not-the-locator"
    assert fetch(wrong).status == 404
    assert fetch(f"{page.url}/anything").status == 404
    without_cookie = browser.post("credentials", cookie="")
    assert without_cookie.status == 403


def test_the_listener_uses_an_os_selected_loopback_port(page: setup.Page) -> None:
    assert page.url.startswith("http://127.0.0.1:")
    assert urlsplit(page.url).port not in (None, 0)


def test_host_and_origin_must_match_exactly(page: setup.Page, browser: Browser) -> None:
    assert Browser(page).open(host="delta-exchange.example.com").status == 404
    assert Browser(page).open(host="localhost:1").status == 404
    assert browser.post("status", origin="https://attacker.example").status == 403
    assert browser.post("status", origin="").status == 403


def test_posts_require_the_session_cookie(browser: Browser) -> None:
    assert browser.post("status", cookie="delta_mcp_setup=wrong").status == 403


def test_posts_require_application_json(browser: Browser) -> None:
    response = browser.post("status", content_type="text/plain")
    assert response.status == 415
    assert "application/json" in response.body["error"]["message"]


def test_invalid_json_and_non_object_json_are_refused(browser: Browser) -> None:
    invalid = browser.post("status", raw=b"{", update=False)
    assert invalid.status == 400
    array = browser.post("status", raw=b"[]", update=False)
    assert array.status == 400
    payload = {
        "action": "status",
        "arguments": {"value": "not-a-json-number"},
        "csrf_token": browser.csrf_token,
        "expected_revision": browser.revision,
    }
    nonstandard = json.dumps(payload).replace('"not-a-json-number"', "NaN").encode()
    assert browser.post("status", raw=nonstandard, update=False).status == 400


def test_oversized_bodies_are_refused_before_dispatch(
    browser: Browser, actions: FakeActions
) -> None:
    response = browser.post("status", raw=b"x" * (setup._MAX_BODY + 1))
    assert response.status == 413
    assert actions.calls == []


def test_each_valid_post_rotates_the_csrf_token(
    browser: Browser, actions: FakeActions
) -> None:
    used = browser.csrf_token
    accepted = browser.post("status", update=True)
    assert accepted.status == 200
    assert browser.csrf_token != used

    replayed = browser.post("status", csrf_token=used, update=False)
    assert replayed.status == 409
    assert [call[0] for call in actions.calls] == ["status"]


def test_a_stale_expected_revision_cannot_mutate(
    browser: Browser, actions: FakeActions
) -> None:
    assert isinstance(browser.revision, dict)
    stale = dict(browser.revision)
    stale["credential"] += 1
    response = browser.post("credentials", revision=stale)
    assert response.status == 409
    assert response.body["revision"] == {"credential": 0, "consent": 0}
    assert [call[0] for call in actions.calls] == ["credentials"]
    assert actions.committed == []


def test_the_action_layer_rejects_stale_credential_and_consent_state_across_pages(
    actions: FakeActions,
) -> None:
    pages = [
        setup.serve(open_browser=False, actions=actions, revision=actions.snapshot())
        for _ in range(2)
    ]
    try:
        first, stale = (Browser(page) for page in pages)
        assert first.open().status == 200
        assert stale.open().status == 200

        assert first.post("credentials", {"api_key": "first"}).status == 200
        refused = stale.post("credentials", {"api_key": "second"})
        assert refused.status == 409
        assert refused.body["revision"] == {"credential": 1, "consent": 0}
        assert actions.committed == ["credentials"]
    finally:
        for page in pages:
            page.stop()

    pages = [
        setup.serve(open_browser=False, actions=actions, revision=actions.snapshot())
        for _ in range(2)
    ]
    try:
        first, stale = (Browser(page) for page in pages)
        assert first.open().status == 200
        assert stale.open().status == 200

        assert first.post("consent", {"enabled": True}).status == 200
        refused = stale.post("consent", {"enabled": True})
        assert refused.status == 409
        assert refused.body["revision"] == {"credential": 1, "consent": 1}
        assert actions.committed == ["credentials", "consent"]
    finally:
        for page in pages:
            page.stop()


def test_credential_and_consent_actions_can_run_in_sequence(
    browser: Browser, actions: FakeActions
) -> None:
    credential = browser.post("credentials", {"api_key": "key", "api_secret": "secret"})
    assert credential.status == 200
    assert credential.body["revision"] == {"credential": 1, "consent": 0}
    assert credential.body["complete"] is False
    assert browser.page.running

    consent = browser.post("consent", {"enabled": True})
    assert consent.status == 200
    assert consent.body["revision"] == {"credential": 1, "consent": 1}
    assert browser.page.saved.wait(timeout=2)
    assert [call[0] for call in actions.calls] == ["credentials", "consent"]


def test_duplicate_tab_mutations_are_serialized_and_only_one_wins(
    page: setup.Page,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    actions = FakeActions(entered=entered, release=release)
    page.stop()
    page = setup.serve(open_browser=False, actions=actions, revision=actions.snapshot())
    try:
        browser = Browser(page)
        assert browser.open().status == 200
        token = browser.csrf_token
        revision = browser.revision
        first: dict[str, Response] = {}

        thread = threading.Thread(
            target=lambda: first.update(
                response=browser.post(
                    "credentials",
                    {"api_key": "first"},
                    csrf_token=token,
                    revision=revision,
                    update=False,
                )
            )
        )
        thread.start()
        assert entered.wait(5)
        second: dict[str, Response] = {}
        competing = threading.Thread(
            target=lambda: second.update(
                response=browser.post(
                    "credentials",
                    {"api_key": "second"},
                    csrf_token=token,
                    revision=revision,
                    update=False,
                )
            )
        )
        competing.start()
        release.set()
        thread.join(10)
        competing.join(10)

        statuses = {first["response"].status, second["response"].status}
        assert statuses == {200, 409}
        assert len(actions.calls) == 1
    finally:
        page.stop()


def test_completion_is_signalled_after_the_full_response_reaches_the_browser(
    browser: Browser,
) -> None:
    response = browser.post("consent", {"enabled": True})
    assert response.status == 200
    assert response.body["result"]["structuredContent"]["trading"] == "enabled"
    assert response.body["complete"] is True
    assert browser.page.saved.wait(timeout=2)


def test_html_and_json_responses_send_security_headers(browser: Browser) -> None:
    html = Browser(browser.page).open()
    assert_security_headers(html)
    csp = html.headers["Content-Security-Policy"]
    nonce = re.search(r"script-src 'nonce-([^']+)'", csp)
    assert nonce is not None
    assert f'<script nonce="{nonce.group(1)}">' in html.text
    assert "'unsafe-inline'" not in csp

    response = browser.post("status")
    assert_security_headers(response)
    assert response.headers["Content-Type"] == "application/json"

    refused = fetch(browser.page.url.rsplit("/", 1)[0] + "/wrong")
    assert_security_headers(refused)


def test_the_page_uses_direct_actions_instead_of_an_mcp_http_endpoint() -> None:
    served = setup.form.page_html(
        "/flow/rpc", csrf_token="csrf", revision=7, nonce="nonce"
    )
    assert "action: action" in served
    assert "csrf_token: csrfToken" in served
    assert "expected_revision: revision" in served
    assert "body: JSON.stringify({ method: method, params: params })" not in served


def test_the_http_endpoint_rejects_mcp_tool_calls(browser: Browser) -> None:
    old_shape = {
        "method": "tools/call",
        "params": {
            "name": "save_credentials",
            "arguments": {"api_key": "key", "api_secret": "secret"},
        },
    }
    response = browser.post("status", raw=json.dumps(old_shape).encode())
    assert response.status == 400


def test_callback_failures_do_not_log_secret_arguments(
    page: setup.Page, caplog: pytest.LogCaptureFixture
) -> None:
    page.stop()
    actions = FakeActions(fail=True)
    page = setup.serve(open_browser=False, actions=actions)
    try:
        browser = Browser(page)
        assert browser.open().status == 200
        response = browser.post(
            "credentials",
            {"api_key": "key-not-for-logs", "api_secret": "secret-not-for-logs"},
        )
        assert response.status == 500
        assert "key-not-for-logs" not in caplog.text
        assert "secret-not-for-logs" not in caplog.text
    finally:
        page.stop()


def test_an_expired_page_closes_and_wakes_waiters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(setup, "LIFETIME_SECONDS", 0.2)
    page = setup.serve(open_browser=False, actions=FakeActions())
    started = time.monotonic()
    assert page.wait(timeout=5) is False
    elapsed = time.monotonic() - started

    assert elapsed < 2
    assert not page.running
    assert not page.saved.is_set()
    with pytest.raises(urllib.error.URLError):
        fetch(page.url)
