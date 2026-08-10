"""A credential pair: checked against Delta, then saved where every client reads it.

Two front-ends fill the same store — `login` for someone already at a terminal, and the
in-chat form in `form` for someone who never opens one. Both need the same check before
saving and both write the same three keys, so neither owns that; this does.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

from delta_exchange_mcp import store
from delta_exchange_mcp.client import DeltaClient
from delta_exchange_mcp.config import BASE_URLS, DEFAULT_MODE, Config, load, mode_key
from delta_exchange_mcp.errors import DeltaApiError, is_auth_failure


@dataclass(frozen=True)
class Check:
    """Outcome of asking Delta whether the credentials work.

    `reachable` is separate from `ok` because they call for opposite responses: a key
    Delta rejected must not be saved, while a key we could not ask about must be, or a
    flaky connection costs someone a credential they typed correctly.
    """

    ok: bool
    reachable: bool
    detail: str
    # Delta's own error code when it rejected the key, and the IP it says it saw. Kept
    # beside the rendered message so a caller can write its own copy for the failures its
    # users can act on, rather than matching on that message's text.
    code: str = ""
    ip: str = ""


def overridden_by_client(
    client: str = "", shared: dict[str, str] | None = None
) -> list[str]:
    """Which settings in the shared file the process environment is overriding.

    `config` resolves the process environment before the file, so a client that passes its
    own key or environment outranks whatever is saved here — on every launch, which is why
    restarting cannot help. Saying nothing produces the worst version of this: a save
    verifies one account against Delta, reports it by name, and the server goes on signing
    with a different one.

    Compares what `config` actually resolves against what the file holds, rather than
    restating the precedence rules, so this cannot drift from them. Presence alone is the
    wrong test twice over. A client pinning the environment to the value the user chose
    overrides nothing that matters, and the Cursor install link sets DELTA_MCP_ENV for
    everyone — so that test would tell every Cursor user their working key was ignored.
    A file with nothing in it is not being overridden either.
    """
    stored = store.read() if shared is None else shared
    live = load(stored)
    effective = {
        "DELTA_MCP_ENV": live.env,
        "DELTA_API_KEY": live.api_key,
        "DELTA_API_SECRET": live.api_secret,
    }
    overridden = [
        name
        for name, in_use in effective.items()
        if (saved := (stored.get(name) or "").strip()) and saved != in_use
    ]
    if client and (scoped := mode_key(client)):
        saved_mode = (stored.get(scoped) or "").strip().lower() or DEFAULT_MODE
        process_mode = (os.environ.get("DELTA_MCP_MODE") or "").strip().lower()
        if process_mode and process_mode != saved_mode:
            overridden.append("DELTA_MCP_MODE")
    return overridden


async def check(env: str, key: str, secret: str) -> Check:
    """One authenticated call, so four documented failures surface here and not later.

    A wrong environment for the key, an unwhitelisted IP, a key without Read Data, and
    a truncated paste are all invisible until something signs a request. Doing it while
    the person is still holding the key turns each into a message they can act on.
    """
    cfg = Config(env=env, base_url=BASE_URLS[env], api_key=key, api_secret=secret)  # type: ignore[arg-type]
    client = DeltaClient(cfg)
    try:
        profile = await client.get("/profile", auth=True)
    except DeltaApiError as exc:
        return Check(
            ok=False,
            reachable=is_auth_failure(exc),
            detail=str(exc),
            code=exc.code,
            ip=exc.ip or "",
        )
    except httpx.HTTPError as exc:
        return Check(ok=False, reachable=False, detail=f"could not reach Delta: {exc}")
    else:
        # The client hands back Delta's envelope rather than unwrapping it, so the account
        # lives under "result". Reading the top level instead silently yields no name at
        # all, which is the one thing that distinguishes this from saving the wrong
        # account's key.
        body = profile.get("result") if isinstance(profile, dict) else None
        who = str(body.get("email") or body.get("id") or "") if isinstance(body, dict) else ""
        return Check(ok=True, reachable=True, detail=who)
    finally:
        await client.aclose()


def save(env: str, key: str, secret: str, client: str = "", mode: str = "") -> str | None:
    """Write the pair and its environment, returning a message if anything failed.

    The environment goes in alongside them deliberately. It is not a separate preference
    but part of what makes the key usable at all, and saving a testnet key while the file
    still says india_prod produces InvalidApiKey on every call.

    `client` and `mode` travel together or not at all: a trading mode is only meaningful
    scoped to the client that chose it, and writing one unscoped would arm order placement
    in every client on the machine. `login` passes neither, because a terminal has no
    client to scope to.
    """
    values = {"DELTA_MCP_ENV": env, "DELTA_API_KEY": key, "DELTA_API_SECRET": secret}
    if client and mode:
        scoped = mode_key(client)
        if scoped:
            values[scoped] = mode
    return store.write(values)


def save_mode(client: str, mode: str) -> str | None:
    """Change only one client's scoped mode, without reading or rewriting credentials."""
    scoped = mode_key(client)
    if not scoped:
        return "this client did not provide a usable name for a scoped mode"
    return store.write({scoped: mode})
