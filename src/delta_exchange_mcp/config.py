from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from delta_exchange_mcp import store

Env = Literal["india_prod", "india_testnet", "india_devnet"]
Mode = Literal["read", "trade"]

INDIA_PROD_REST = "https://api.india.delta.exchange/v2"
INDIA_TESTNET_REST = "https://cdn-ind.testnet.deltaex.org/v2"
INDIA_DEVNET_REST = "https://cdn-ind.devnet.deltaex.org/v2"

BASE_URLS: dict[str, str] = {
    "india_prod": INDIA_PROD_REST,
    "india_testnet": INDIA_TESTNET_REST,
    "india_devnet": INDIA_DEVNET_REST,
}

# Where someone creates the key for each environment. Beside BASE_URLS because it is the
# same per-environment fact from the user's side, and because every place that asks for a
# credential has to name the right one — a prod key sent to testnet returns InvalidApiKey.
# india_devnet is internal and has no public dashboard, so it is absent by design.
DASHBOARDS: dict[str, str] = {
    "india_prod": "https://www.delta.exchange/app/account/manageapikeys",
    "india_testnet": "https://demo.delta.exchange/app/account/manageapikeys",
}

DEFAULT_ENV = "india_prod"
DEFAULT_MODE = "read"
MODES: set[str] = {"read", "trade"}

# Trading mode is stored per client rather than under one shared name. The shared file is
# read by every MCP client on the machine, so an unscoped DELTA_MCP_MODE=trade in it would
# arm order placement in all of them at once — which is why `load` still refuses to read
# that name from the file at all. Scoping it by the client's own name keeps the choice
# where it was made.
_MODE_PREFIX = "DELTA_MCP_MODE_"


TRUTHY = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    env: Env
    base_url: str
    api_key: str | None = None
    api_secret: str | None = None
    debug: bool = False
    mode: Mode = "read"
    config_file: Path | None = None

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret)

    @property
    def partial_credentials(self) -> bool:
        """One half of the pair supplied without the other — always a misconfiguration.

        Both are needed to sign a request, so this silently yields public-data mode. It is
        reported rather than raised: a stray DELTA_API_KEY in someone's shell should not
        kill an otherwise working market-data server.
        """
        return bool(self.api_key) != bool(self.api_secret)


def setting(name: str, shared: dict[str, str] | None = None) -> str | None:
    """Resolve one setting: the process environment first, then the shared file.

    Empty means unanswered rather than answered-with-nothing. A bundle substitutes
    every variable it declares whether or not the user filled that field in, so a
    cleared input arrives as "" — it has to fall through to the file rather than
    override it, or the shared file could never reach a bundle user at all. `shared`
    lets one caller resolve several settings from the same file snapshot.
    """
    from_env = (os.environ.get(name) or "").strip()
    if from_env:
        return from_env
    values = store.read() if shared is None else shared
    return (values.get(name) or "").strip() or None


# The key and its secret are one setting wearing two names. `_credentials` takes both from
# whichever source holds either, so anything reasoning about where a credential came from
# has to reason about the pair rather than about one name.
CREDENTIAL_NAMES = ("DELTA_API_KEY", "DELTA_API_SECRET")


def _credentials(shared: dict[str, str]) -> tuple[str | None, str | None]:
    """The API key and its secret, always taken from the same source.

    Resolving them independently could pair a leftover DELTA_API_KEY in someone's
    shell with a secret from the shared file. That combination was never issued
    together, so every signed request fails while the server reports the account
    surface as available — the least diagnosable outcome available. Taking both from
    wherever either one appears turns that into the partial-credentials warning,
    which says exactly what is wrong.
    """
    # Stripped like every other setting, so a whitespace-only field reads as unanswered
    # and falls through. Stripping also absorbs the trailing newline a pasted key
    # usually carries, which would otherwise fail signing and look like a wrong key.
    key_name, secret_name = CREDENTIAL_NAMES
    key = (os.environ.get(key_name) or "").strip() or None
    secret = (os.environ.get(secret_name) or "").strip() or None
    if key or secret:
        return key, secret
    return (
        (shared.get(key_name) or "").strip() or None,
        (shared.get(secret_name) or "").strip() or None,
    )


def mode_key(client: str) -> str:
    """The settings-file name carrying the trading mode for one named client.

    The name comes from the MCP handshake, where a client identifies itself as anything
    it likes — "Claude Desktop", "claude-ai", "Visual Studio Code". The readable part is
    only a label; the digest of the exact, self-reported name is the binding. Without it,
    punctuation variants such as ``foo-bar`` and ``foo bar`` collapse to one dotenv key
    and one client can inherit another one's trading choice.

    This is collision-resistant convenience scoping, not authenticated client identity.
    A client can still claim another client's exact name; a future protocol-level host id
    is needed to make that an authorization boundary.
    """
    display = client.strip()
    if not display:
        return ""
    slug = "".join(c if c.isascii() and c.isalnum() else "_" for c in display)
    slug = slug.strip("_").upper()
    while "__" in slug:
        slug = slug.replace("__", "_")
    slug = slug[:32] or "CLIENT"
    digest = hashlib.sha256(client.encode()).hexdigest()[:12].upper()
    return f"{_MODE_PREFIX}{slug}_{digest}"


def mode_for_client(client: str, shared: dict[str, str] | None = None) -> Mode:
    """The mode a named client may run in.

    `DELTA_MCP_MODE` in the process environment still wins, because that is the client's
    own configuration and editing it is the most deliberate thing anyone can do. Below it
    sits this client's own scoped key, which is what the in-chat form writes. An
    unrecognised value reads as `read`: the file is hand-editable, and a typo there must
    not be the thing that arms order placement.
    """
    from_env = (os.environ.get("DELTA_MCP_MODE", "") or "").strip().lower()
    if from_env:
        if from_env not in MODES:
            raise ValueError(f"DELTA_MCP_MODE must be one of {sorted(MODES)}, got {from_env!r}")
        return from_env  # type: ignore[return-value]

    key = mode_key(client)
    if not key:
        return DEFAULT_MODE  # type: ignore[return-value]
    values = store.read() if shared is None else shared
    saved = (values.get(key) or "").strip().lower()
    return saved if saved in MODES else DEFAULT_MODE  # type: ignore[return-value]


def _load_snapshot(shared: dict[str, str], config_file: Path | None) -> Config:
    """Resolve one Config from one complete settings-file snapshot."""
    env = (setting("DELTA_MCP_ENV", shared) or DEFAULT_ENV).lower()
    if env not in BASE_URLS:
        raise ValueError(
            f"DELTA_MCP_ENV must be one of {sorted(BASE_URLS)}, got {env!r}"
        )

    # Mode is the one setting the shared file may not supply. Everything else there is
    # per-machine convenience; this one places real orders, so it stays scoped to the
    # single client whose config was deliberately edited to enable it.
    mode = (os.environ.get("DELTA_MCP_MODE", "") or "").strip().lower() or DEFAULT_MODE
    if mode not in MODES:
        raise ValueError(f"DELTA_MCP_MODE must be one of {sorted(MODES)}, got {mode!r}")

    api_key, api_secret = _credentials(shared)

    return Config(
        env=env,  # type: ignore[arg-type]
        base_url=BASE_URLS[env],
        api_key=api_key,
        api_secret=api_secret,
        debug=(setting("DELTA_MCP_DEBUG", shared) or "").lower() in TRUTHY,
        mode=mode,  # type: ignore[arg-type]
        config_file=config_file,
    )


def load(shared: dict[str, str] | None = None) -> Config:
    config_file = store.ensure()
    # `store.write` replaces a complete file atomically. Read it once so one Config
    # cannot combine the environment from the old file with credentials from the new.
    values = store.read() if shared is None else shared
    return _load_snapshot(values, config_file)


def load_for_client(
    client: str, shared: dict[str, str] | None = None
) -> Config:
    """Resolve HTTP identity and this client's entitlement from one file snapshot."""
    config_file = store.ensure()
    values = store.read() if shared is None else shared
    loaded = _load_snapshot(values, config_file)
    return replace(loaded, mode=mode_for_client(client, values))
