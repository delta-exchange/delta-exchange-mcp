"""A credential pair: checked against Delta, then saved where every client reads it.

Two front-ends fill the same store — `login` for someone already at a terminal, and the
in-chat form in `form` for someone who never opens one. Both need the same check before
saving and both write the same three keys, so neither owns that; this does.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from delta_exchange_mcp import store
from delta_exchange_mcp.client import DeltaClient
from delta_exchange_mcp.config import BASE_URLS, Config
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


def save(env: str, key: str, secret: str) -> str | None:
    """Write the pair and its environment, returning a message if anything failed.

    The environment goes in alongside them deliberately. It is not a separate preference
    but part of what makes the key usable at all, and saving a testnet key while the file
    still says india_prod produces InvalidApiKey on every call.
    """
    return store.write(
        {"DELTA_MCP_ENV": env, "DELTA_API_KEY": key, "DELTA_API_SECRET": secret}
    )
