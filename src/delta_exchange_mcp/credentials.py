"""Validate a candidate credential pair without storing or returning its secrets."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from delta_exchange_mcp.account_identity import (
    InvalidIdentityResponse,
    fetch_account_identity,
)
from delta_exchange_mcp.client import DeltaClient
from delta_exchange_mcp.config import BASE_URLS, Config
from delta_exchange_mcp.errors import (
    DeltaApiError,
    is_auth_failure,
    is_permission_failure,
)

TRADING_PREFERENCES_PERMISSION_MESSAGE = (
    "API key lacks permission for trading preferences. Update its account-data "
    "permissions in Delta API management. Current Delta documentation does not "
    "establish whether Read Data alone is sufficient."
)


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

    A wrong environment for the key, an unwhitelisted IP, a key without permission for
    trading preferences, and a truncated paste are all invisible until something signs
    a request. Doing it while the person is still holding the key turns each into a
    message they can act on.
    """
    cfg = Config(env=env, base_url=BASE_URLS[env], api_key=key, api_secret=secret)  # type: ignore[arg-type]
    client = DeltaClient(cfg)
    try:
        identity = await fetch_account_identity(client)
    except DeltaApiError as exc:
        return Check(
            ok=False,
            reachable=is_auth_failure(exc) or is_permission_failure(exc),
            detail=(
                TRADING_PREFERENCES_PERMISSION_MESSAGE
                if is_permission_failure(exc)
                else str(exc)
            ),
            code=exc.code,
            ip=exc.ip or "",
        )
    except httpx.HTTPError as exc:
        return Check(ok=False, reachable=False, detail=f"could not reach Delta: {exc}")
    except InvalidIdentityResponse as exc:
        return Check(ok=False, reachable=False, detail=f"invalid account response: {exc}")
    else:
        return Check(ok=True, reachable=True, detail=str(identity.user_id))
    finally:
        await client.aclose()
