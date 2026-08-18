"""What the settings page will and will not answer.

Driven over real HTTP against a real listener rather than by calling the handler, because
every property here is about what reaches the socket: which address it binds, which `Host`
header it accepts, and whether a wrong path is distinguishable from a right one.
"""

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from delta_exchange_mcp import credentials, setup, store


@pytest.fixture
def page(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "path", lambda: tmp_path / "config.env")
    running = setup.serve(client="", open_browser=False)
    yield running
    running.stop()


def fetch(url, host=None, body=None):
    """Returns (status, text). An HTTP error is a result here, not an exception."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as refused:
        return refused.code, refused.read().decode()


def test_the_page_is_only_served_to_whoever_has_the_address(page):
    """The token in the URL is what stands between this and anything else on the machine.

    Loopback is not a boundary on a shared or multi-user machine: any other process can
    reach 127.0.0.1. An unguessable path is what makes the difference.
    """
    ok, body = fetch(page.url)
    assert ok == 200
    assert "Connect your Delta Exchange account" in body

    wrong = page.url.rsplit("/", 1)[0] + "/not-the-token"
    assert fetch(wrong)[0] == 404


def test_a_website_cannot_reach_it_through_the_browser(page):
    """A page on the internet can point a name it controls at 127.0.0.1 and have the
    person's own browser make the request, which loopback alone does not stop.

    The browser sends the name that was typed, so requiring `Host` to be the loopback
    address refuses it. Without this the address being local proves nothing.
    """
    assert fetch(page.url, host="delta-exchange.example.com")[0] == 404
    assert fetch(page.url, host="localhost:1")[0] == 404


def test_it_binds_the_loopback_address_and_not_the_network(page):
    """Bound anywhere else, the settings page would be reachable from the local network."""
    assert page.url.startswith("http://127.0.0.1:")


def test_a_saved_key_reaches_the_file_and_stops_the_page(page, tmp_path, monkeypatch):
    """The whole point of the page: the typed key goes to the file, not through the chat."""

    async def accepted(env, key, secret):
        return credentials.Check(ok=True, reachable=True, detail="someone@delta.exchange")

    monkeypatch.setattr(credentials, "check", accepted)

    status, body = fetch(
        f"{page.url}/rpc",
        body={
            "method": "tools/call",
            "params": {
                "name": "save_credentials",
                "arguments": {
                    "environment": "india_testnet",
                    "api_key": "typed-into-the-page",
                    "api_secret": "typed-into-the-page-secret",
                },
            },
        },
    )
    assert status == 200
    result = json.loads(body)["result"]["structuredContent"]
    assert result["status"] == "saved"
    assert result["account"] == "someone@delta.exchange"

    written = (tmp_path / "config.env").read_text()
    assert "typed-into-the-page" in written
    assert page.saved.is_set()


def test_a_key_delta_rejects_is_never_written(page, tmp_path, monkeypatch):
    """A rejected key must not be saved; an unreachable Delta must not block a good one.

    Those two need opposite answers, which is why `Check` reports them separately.
    """

    async def rejected(env, key, secret):
        return credentials.Check(
            ok=False, reachable=True, detail="Delta rejected it", code="InvalidApiKey"
        )

    monkeypatch.setattr(credentials, "check", rejected)

    _, body = fetch(
        f"{page.url}/rpc",
        body={
            "method": "tools/call",
            "params": {
                "name": "save_credentials",
                "arguments": {
                    "environment": "india_prod",
                    "api_key": "wrong",
                    "api_secret": "wrong",
                },
            },
        },
    )
    assert json.loads(body)["result"]["structuredContent"]["status"] == "rejected"
    assert not (tmp_path / "config.env").exists() or "wrong" not in (
        tmp_path / "config.env"
    ).read_text()
    assert not page.saved.is_set()


def test_trading_cannot_be_turned_on_from_a_terminal(page):
    """Trading is scoped to one client's handshake name, and a terminal has none.

    Arming it without a name would either arm nothing or arm everything; `login` declines
    the same choice for the same reason.
    """
    _, body = fetch(
        f"{page.url}/rpc",
        body={"method": "tools/call", "params": {"name": "save_mode", "arguments": {"mode": "trade"}}},
    )
    answered = json.loads(body)["result"]["structuredContent"]
    assert answered["status"] == "rejected"
    assert "one app at a time" in answered["message"]


def test_the_page_reports_which_settings_a_client_is_overriding(page, monkeypatch):
    """A value the client passes wins on every launch, so saving over it in the file would
    verify one account and leave the server signing with another."""
    monkeypatch.setenv("DELTA_API_KEY", "from-the-clients-own-config")
    monkeypatch.setenv("DELTA_API_SECRET", "also-from-the-client")
    store.write({"DELTA_API_KEY": "from-the-file", "DELTA_API_SECRET": "also-from-the-file"})

    _, body = fetch(
        f"{page.url}/rpc",
        body={"method": "tools/call", "params": {"name": "get_connection_status", "arguments": {}}},
    )
    reported = json.loads(body)["result"]["structuredContent"]
    assert "DELTA_API_KEY" in reported["overridden_by_client"]


def test_an_unknown_request_is_refused_rather_than_guessed(page):
    assert fetch(f"{page.url}/rpc", body={"method": "tools/call", "params": {"name": "rm"}})[0] == 400
    assert fetch(f"{page.url}/rpc", body={"method": "something/else"})[0] == 400


def test_a_page_that_expired_unsaved_knows_it_is_closed(monkeypatch, tmp_path):
    """A page nobody used closes on its own, and must not be handed out again.

    Nothing sets the saved flag in that case, so asking "was it saved?" calls the dead
    listener alive and offers an address that refuses to connect. Driven with a real
    expiry rather than by calling `stop`, because the bug was in the expiry path.
    """
    monkeypatch.setattr(store, "path", lambda: tmp_path / "config.env")
    monkeypatch.setattr(setup, "LIFETIME_SECONDS", 0.3)

    expiring = setup.serve(client="", open_browser=False)
    assert expiring.running
    assert fetch(expiring.url)[0] == 200

    time.sleep(1.0)
    assert not expiring.saved.is_set(), "nothing was saved, which is the point"
    assert not expiring.running, "an expired page must report itself closed"
    with pytest.raises(urllib.error.URLError):
        fetch(expiring.url)


def test_a_saved_page_also_reports_itself_closed(page, monkeypatch):
    """The other way it ends. One question has to answer for every way of closing."""

    async def accepted(env, key, secret):
        return credentials.Check(ok=True, reachable=True, detail="someone@delta.exchange")

    monkeypatch.setattr(credentials, "check", accepted)
    fetch(
        f"{page.url}/rpc",
        body={
            "method": "tools/call",
            "params": {
                "name": "save_credentials",
                "arguments": {
                    "environment": "india_testnet",
                    "api_key": "k",
                    "api_secret": "s",
                },
            },
        },
    )
    deadline = time.time() + 5
    while page.running and time.time() < deadline:
        time.sleep(0.05)
    assert not page.running


def test_a_page_that_has_saved_is_never_offered_again(page):
    """Closing happens on a separate thread, so "has it stopped?" answers late.

    Between the save landing and that thread being scheduled, the page has committed and
    is about to close while still reporting itself usable. The next caller is then handed
    an address that refuses every connection. Set here directly rather than through a real
    save, because the window is too short to reach reliably over HTTP — and a test that
    only passes when the scheduler cooperates proves nothing.
    """
    assert page.running
    page.saved.set()
    assert not page.running, "a committed page must not be handed out again"


def test_a_successful_save_reaches_the_browser_whole(page, monkeypatch):
    """The page closes on the save, so the save must be signalled after the response.

    These request threads are daemon threads that nothing waits for. Signalling before the
    write lets the listener close, and the ten-minute expiry and the terminal command both
    end on that same signal — so a save that is already durable can reach the browser as a
    connection reset, and the person retries something that already worked.
    """

    async def accepted(env, key, secret):
        return credentials.Check(ok=True, reachable=True, detail="someone@delta.exchange")

    monkeypatch.setattr(credentials, "check", accepted)
    status, text = fetch(
        f"{page.url}/rpc",
        body={
            "method": "tools/call",
            "params": {
                "name": "save_credentials",
                "arguments": {
                    "environment": "india_testnet",
                    "api_key": "k",
                    "api_secret": "s",
                },
            },
        },
    )
    assert status == 200
    assert json.loads(text)["result"]["structuredContent"]["status"] == "saved"
    assert page.saved.is_set()


def test_waiting_on_a_page_ends_when_it_expires(monkeypatch, tmp_path):
    """Whoever is waiting is waiting for an answer, and expiry means none is coming.

    Waiting on the save alone never ends on this path, because nothing sets it when a page
    simply runs out — so the caller keeps waiting on a listener that already closed.
    """
    monkeypatch.setattr(store, "path", lambda: tmp_path / "config.env")
    monkeypatch.setattr(setup, "LIFETIME_SECONDS", 0.3)

    expiring = setup.serve(client="", open_browser=False)
    started = time.time()
    finished = expiring.wait(timeout=10)
    waited = time.time() - started

    assert finished is False, "it expired, so nobody saved anything"
    assert waited < 5, f"the wait should end with the page, took {waited:.1f}s"
    assert not expiring.running


def test_the_page_issues_the_save_grant_itself(page):
    """Nothing else can issue it here. The URL token already proved who is asking."""
    _, body = fetch(
        f"{page.url}/rpc",
        body={"method": "tools/call", "params": {"name": "setup_credentials", "arguments": {}}},
    )
    granted = json.loads(body)["result"]["_meta"]["ui"]["saveGrant"]
    assert granted and granted in page.url


def test_the_view_asks_for_that_grant_when_no_host_will_send_it():
    """In a client the grant arrives on its own: the host runs the opener and forwards the
    tool result as a notification. On this page there is no host and no notification.

    Without the request the save button never leaves its disabled state, so the page could
    not save anything at all — and the whole suite passed, because every other test here
    posts to the endpoint with a hand-built body and never runs the view's JavaScript.

    This is a structural check standing in for a browser, and that limit is worth stating:
    it pins the line against removal, it does not prove the button works. Open the page and
    press it.
    """
    served = setup.form.page_html("/a-token/rpc")
    # The boot block only: from the handshake call to the end of its handler. Searching the
    # whole file would match the reopen button, which is reachable only after a save and so
    # cannot supply the first grant — a check that passes on the broken version too.
    start = served.index('request("ui/initialize"')
    boot = served[start : served.index('window.addEventListener("resize"', start)]
    assert 'name: "setup_credentials"' in boot, "boot must obtain a grant with no host present"
    guard = boot.index("if (!IN_APP)")
    assert guard < boot.index('name: "setup_credentials"'), "and only when there is no host"

def save_body(key, mode=""):
    arguments = {
        "environment": "india_testnet",
        "api_key": key,
        "api_secret": f"secret-{key}",
    }
    if mode:
        arguments["mode"] = mode
    return {"method": "tools/call", "params": {"name": "save_credentials", "arguments": arguments}}


def test_only_one_save_can_win_however_many_tabs_are_open(page, tmp_path, monkeypatch):
    """The token says who may ask, not how often, and one page has one save to give.

    The token sits in the address bar for the page's whole life, so a reload, a duplicated
    tab and a double-click all carry a valid one. Both saves used to run to completion: each
    validated its own key, each was told the account that key belongs to, and one of the two
    reached the file — leaving a browser reporting a connected account this machine is not
    using. The file stayed coherent throughout, which is why nothing looked wrong.

    The window is the call to Delta, so the second save is sent while the first is inside it.
    """
    entered, release = threading.Event(), threading.Event()

    async def slow(env, key, secret):
        entered.set()
        release.wait(5)
        return credentials.Check(ok=True, reachable=True, detail=f"account-for-{key}")

    monkeypatch.setattr(credentials, "check", slow)

    first: dict = {}
    caller = threading.Thread(
        target=lambda: first.update(json.loads(fetch(f"{page.url}/rpc", body=save_body("first"))[1]))
    )
    caller.start()
    assert entered.wait(5), "the first save never reached Delta"

    # Sent while the first is still in flight — the race, made deterministic.
    _, refused = fetch(f"{page.url}/rpc", body=save_body("second"))
    release.set()
    caller.join(10)

    assert json.loads(refused)["result"]["structuredContent"]["status"] == "rejected"
    assert first["result"]["structuredContent"]["status"] == "saved"
    # And the file holds the key whose browser was told it was saved.
    written = (tmp_path / "config.env").read_text()
    assert "first" in written and "second" not in written


def test_a_spent_save_is_refused_rather_than_silently_replacing_the_first():
    """The claim survives the save that used it, so nothing arriving later can overwrite it.

    Unit-level because the page closes itself once a save commits, so over HTTP this refusal
    only exists in the moment between the write and the listener shutting down. It still has
    to hold: that moment is exactly when a second tab's request is already on the wire.
    """
    state = setup._Save()

    assert state.claim() == ""
    # A second asker while the first still holds it.
    assert state.claim() == setup._SAVE_IN_FLIGHT
    state.commit()
    assert state.claim() == setup._ALREADY_SAVED
    # A release after the commit must not reopen it.
    state.release()
    assert state.claim() == setup._ALREADY_SAVED


def test_a_claim_is_handed_back_when_the_save_never_reached_the_file():
    """A rejected key must not cost the page its one save, or a typo means reopening it."""
    state = setup._Save()

    assert state.claim() == ""
    state.release()
    assert state.claim() == "", "a failed save should leave the page usable"


def test_a_key_saved_while_delta_is_unreachable_is_not_reported_as_connected(
    page, tmp_path, monkeypatch
):
    """Saving it is deliberate; calling it a connection is not.

    A page rendering "saved" with an account name says "Connected as <account>", so putting
    the transport error into the account field told the person "Connected as could not reach
    Delta: timeout" over a key nothing had checked, and the terminal command exited happy.
    The in-chat form has always separated these two, and the page now gives the same answer.
    """

    async def unreachable(env, key, secret):
        return credentials.Check(ok=False, reachable=False, detail="could not reach Delta: timeout")

    monkeypatch.setattr(credentials, "check", unreachable)

    _, body = fetch(f"{page.url}/rpc", body=save_body("typed-anyway"))
    result = json.loads(body)["result"]["structuredContent"]

    assert result["status"] == "unverified"
    # The key is still stored: a flaky connection must not cost someone a key they got right.
    assert "typed-anyway" in (tmp_path / "config.env").read_text()
    # No account, because Delta never named one.
    assert not result.get("account")
    assert "could not reach Delta" in result["message"]
    # The file was written, so this page's one save is spent and it closes like any other.
    assert page.saved.is_set()
