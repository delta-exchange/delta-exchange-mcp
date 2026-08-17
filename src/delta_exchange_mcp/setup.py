"""The settings page, served to a browser on this machine only.

The third front-end onto `credentials`, beside `login` for a terminal and `form` for a host
that draws the view inline. It exists because the other two each have a surface they cannot
reach. `login` needs a terminal, which is the thing a person installing from a chat is
trying to avoid. The in-chat form needs a host that renders MCP Apps, and whether one does
turns out to depend on which configuration file the server was registered in — so it cannot
be relied on. A link works everywhere: every host renders one, including a terminal.

**This is not the HTTP transport the architecture guide rules out.** That rule is about a
shared server holding other people's keys, and the reasoning is that per-user credentials
must not route through one. Nothing here is shared or reachable: the listener binds the
loopback address, serves one person on their own machine, and stops as soon as they are
done. The key still goes only into the file it always went into.

What keeps it closed:

* **Loopback only.** Bound to 127.0.0.1, so nothing off this machine can open a connection
  in the first place.
* **An unguessable path.** The URL carries a random token, and a request that does not
  present it is refused. Anything else on the machine would have to guess it.
* **A checked `Host` header.** A page on the internet can point a name it controls at
  127.0.0.1 and have the person's own browser make the request. Requiring the header to be
  the loopback address refuses that, because the browser sends the name that was typed.
* **A short life.** It stops on the first successful save, after ten minutes, or when the
  process exits, whichever comes first.

The typed key reaches this process directly from the browser and goes straight to the file.
It never passes through the conversation, so it is not in a model's context and not in the
stored transcript — the same property the in-chat form has, for the same reason.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import threading
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from delta_exchange_mcp import config as config_mod
from delta_exchange_mcp import credentials, form, store

# Long enough to find a key in the Delta dashboard and paste it, short enough that a
# forgotten tab is not an open door. The in-chat form uses the same ten minutes.
LIFETIME_SECONDS = 10 * 60

_LOOPBACK = "127.0.0.1"
_MAX_BODY = 64 * 1024


@dataclass
class Page:
    """A running settings page and where to find it."""

    url: str
    server: ThreadingHTTPServer
    saved: threading.Event

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def wait(self, timeout: float = LIFETIME_SECONDS) -> bool:
        """Block until the settings are saved or the page expires. True if saved."""
        return self.saved.wait(timeout)


def _status(client: str) -> dict[str, Any]:
    """What the page shows before anything is typed.

    The same three-layer answer `get_connection_status` gives a model: what the settings
    resolve to now, and which of them this client is overriding from its own configuration
    — because a value the client passes wins on every launch, so saving over it in the file
    would verify one account and leave the server using another.
    """
    shared = store.read()
    live = config_mod.load(shared)
    return {
        "environment": live.env,
        "credentials_configured": live.has_credentials,
        "mode": config_mod.mode_for_client(client, shared) if client else "read",
        "overridden": credentials.overridden_by_client(client, shared),
        "path": str(store.path()),
        "client_name": client,
        # No live client means no handshake name, so there is nothing to scope a mode to.
        # `login` declines the same choice for the same reason.
        "mode_settable": bool(client),
    }


class _Handler(BaseHTTPRequestHandler):
    # Set per server instance in `serve`.
    token: str = ""
    client: str = ""
    saved: threading.Event
    origin: str = ""

    def log_message(self, *args: Any) -> None:
        """Silence the default stderr line: this shares stdio with the MCP protocol."""

    def _refuse(self, code: int, why: str) -> None:
        body = json.dumps({"error": {"message": why}}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _allowed(self) -> bool:
        """Refuse anything that is not this browser, on this machine, with this token."""
        host = self.headers.get("Host", "")
        if host != self.origin:
            return False
        return self.path.split("?", 1)[0].startswith(f"/{self.token}")

    def do_GET(self) -> None:
        if not self._allowed():
            self._refuse(404, "not found")
            return
        body = form.page_html(f"/{self.token}/rpc").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # The page needs nothing from anywhere else, and saying so keeps it that way.
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if not self._allowed():
            self._refuse(404, "not found")
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > _MAX_BODY:
            self._refuse(413, "too large")
            return
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._refuse(400, "not json")
            return

        result = self._dispatch(payload)
        if result is None:
            self._refuse(400, "unknown request")
            return
        body = json.dumps({"result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        if payload.get("method") != "tools/call":
            return None
        params = payload.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}

        if name == "get_connection_status":
            return {"structuredContent": _status(self.client)}
        if name == "setup_credentials":
            # The URL token already established who is asking; the view only needs a value
            # here to enable its save button, and the same token is that value.
            return {"_meta": {"ui": {"saveGrant": self.token}}}
        if name == "save_mode":
            return {"structuredContent": self._save_mode(args)}
        if name == "save_credentials":
            return {"structuredContent": self._save(args)}
        return None

    def _save_mode(self, args: dict[str, Any]) -> dict[str, Any]:
        if not self.client:
            return {"status": "rejected", "message": _NO_CLIENT}
        failure = credentials.save_mode(self.client, str(args.get("mode") or "read"))
        if failure:
            return {"status": "rejected", "message": failure}
        self.saved.set()
        return {"status": "saved", "message": "Saved."}

    def _save(self, args: dict[str, Any]) -> dict[str, Any]:
        env = str(args.get("environment") or "")
        key = str(args.get("api_key") or "")
        secret = str(args.get("api_secret") or "")
        if not (env and key and secret):
            return {"status": "rejected", "message": "Fill in every field."}

        # `check` is a coroutine and this handler is not. Each request already runs on its
        # own thread, so a loop here is private to it and cannot disturb the MCP server's.
        checked = asyncio.run(credentials.check(env, key, secret))
        # A key Delta rejected must not be saved. A key it could not be asked about must
        # be, or a flaky connection costs someone a credential they typed correctly.
        if not checked.ok and checked.reachable:
            return {"status": "rejected", "message": form.rejection(env, checked)}

        mode = str(args.get("mode") or "")
        failure = credentials.save(
            env, key, secret, client=self.client, mode=mode if self.client else ""
        )
        if failure:
            return {"status": "rejected", "message": failure}

        self.saved.set()
        return {
            "status": "saved",
            "account": checked.detail,
            "path": str(store.path()),
            "next_step": "Restart your MCP client so it picks up the new settings.",
            "overridden": credentials.overridden_by_client(self.client, store.read()),
        }


_NO_CLIENT = (
    "Trading is turned on for one app at a time, and this page was opened from a terminal "
    "rather than from an app. Ask your assistant to connect your Delta account instead, or "
    "set DELTA_MCP_MODE=trade in that app's own configuration."
)


def serve(client: str = "", open_browser: bool = True) -> Page:
    """Start the settings page and return where it is. The caller decides how long to wait.

    Port zero, so the operating system picks one that is free rather than this guessing and
    colliding with whatever else the person is running.
    """
    token = secrets.token_urlsafe(24)
    saved = threading.Event()
    server = ThreadingHTTPServer((_LOOPBACK, 0), _Handler)
    origin = f"{_LOOPBACK}:{server.server_address[1]}"

    # A subclass per server rather than attributes on `_Handler`, which every server in the
    # process would share: the second page opened would silently take over the first one's
    # token and write its result into the first one's event. The address is only known once
    # the socket is bound, so the class is built here and swapped in after.
    server.RequestHandlerClass = type(
        "_BoundHandler",
        (_Handler,),
        {"token": token, "client": client, "saved": saved, "origin": origin},
    )

    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://{origin}/{token}"
    if open_browser:
        # Best effort. A machine with no browser, or one reached over SSH, still has the
        # printed address, which is why the caller is given the URL rather than a promise.
        try:
            webbrowser.open(url)
        except OSError:
            pass
    return Page(url=url, server=server, saved=saved)
