from __future__ import annotations

import os
from dataclasses import dataclass
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


def _credentials() -> tuple[str | None, str | None]:
    """Read only a process-managed compatibility pair, never file secrets."""
    # Stripped like every other setting, so a whitespace-only field reads as unanswered
    # and falls through. Stripping also absorbs the trailing newline a pasted key
    # usually carries, which would otherwise fail signing and look like a wrong key.
    key_name, secret_name = CREDENTIAL_NAMES
    key = (os.environ.get(key_name) or "").strip() or None
    secret = (os.environ.get(secret_name) or "").strip() or None
    return key, secret


def _load_snapshot(shared: dict[str, str], config_file: Path | None) -> Config:
    """Resolve one Config from one complete settings-file snapshot."""
    env = (setting("DELTA_MCP_ENV", shared) or DEFAULT_ENV).lower()
    if env not in BASE_URLS:
        raise ValueError(
            f"DELTA_MCP_ENV must be one of {sorted(BASE_URLS)}, got {env!r}"
        )

    api_key, api_secret = _credentials()

    return Config(
        env=env,  # type: ignore[arg-type]
        base_url=BASE_URLS[env],
        api_key=api_key,
        api_secret=api_secret,
        debug=(setting("DELTA_MCP_DEBUG", shared) or "").lower() in TRUTHY,
        # Legacy DELTA_MCP_MODE values are intentionally ignored. Only the consent
        # store can authorize a trading mutation.
        mode="read",
        config_file=config_file,
    )


def load(shared: dict[str, str] | None = None) -> Config:
    config_file = store.ensure()
    # `store.write` replaces a complete file atomically. Read it once so one Config
    # cannot combine the environment from the old file with credentials from the new.
    values = store.read() if shared is None else shared
    return _load_snapshot(values, config_file)


def load_without_legacy_credentials(
    shared: dict[str, str] | None = None,
) -> Config:
    """Load non-secret settings and process credentials, but ignore file credentials.

    The connection service calls this only after it attempts automatic migration. A
    migration that is unavailable or conflicts must leave the legacy file unchanged, but
    the running server must still refuse to use its plaintext credential pair.
    """
    config_file = store.ensure()
    values = store.read() if shared is None else shared
    non_secret = {
        name: value for name, value in values.items() if name not in CREDENTIAL_NAMES
    }
    return _load_snapshot(non_secret, config_file)
