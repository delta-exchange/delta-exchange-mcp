"""Credential, consent, and browser state for one local MCP server process."""

import asyncio
import os
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from mcp.server.mcpserver import Context

from delta_exchange_mcp import config as config_mod
from delta_exchange_mcp import credentials as credential_check
from delta_exchange_mcp import request
from delta_exchange_mcp import setup
from delta_exchange_mcp import store as legacy_store
from delta_exchange_mcp.auth.consent import (
    ConsentBinding,
    ConsentLease,
    ConsentState,
    ConsentStorageError,
    ConsentStore,
    MemoryConsentBackend,
    StaleConsentError,
)
from delta_exchange_mcp.auth.store import (
    Credential,
    CredentialConflictError,
    CredentialMetadata,
    CredentialSource,
    CredentialState,
    CredentialStore,
    CredentialStoreError,
    IncompleteCredentialError,
    MigrationResult,
    MigrationStatus,
    default_metadata_path,
)
from delta_exchange_mcp.authorization import Access, AccessState
from delta_exchange_mcp.client import DeltaClient


SUPPORTED_ENVIRONMENTS = ("india_prod", "india_testnet")
DEFAULT_CONSENT_NAME = "consent.json"

_ENVIRONMENT_TOKEN = {"india_prod": 1, "india_testnet": 2, "india_devnet": 3}
_CONSENT_STORE_UNAVAILABLE = "consent_store_unavailable"
_DECISIVE_REJECTION_CODES = frozenset(
    {
        "InvalidApiKey",
        "invalid_api_key",
        "Signature Mismatch",
        "signature_mismatch",
    }
)

RevisionToken = dict[str, int]
Validator = Callable[[str, str, str], Awaitable[credential_check.Check]]
PageFactory = Callable[..., setup.Page]


@dataclass(frozen=True)
class _CredentialCandidate:
    environment: str
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)


@dataclass(frozen=True)
class ConnectionStatus:
    """One secret-free snapshot for an MCP result or the browser page."""

    environment: str
    client_name: str
    client_version: str
    environments: dict[str, dict[str, object]]
    trading: dict[str, object]
    migration_status: str
    environment_externally_managed: bool
    connection_error: str
    consent_error: str

    def as_dict(self) -> dict[str, object]:
        """Return the stable status payload."""
        return {
            "environment": self.environment,
            "client_name": self.client_name,
            "client_version": self.client_version,
            "environments": self.environments,
            "trading": self.trading,
            "migration_status": self.migration_status,
            "environment_externally_managed": self.environment_externally_managed,
            "connection_error": self.connection_error,
            "consent_error": self.consent_error,
            "credentials_configured": bool(
                self.environments.get(self.environment, {}).get("connected")
            ),
            "account_tools_available": bool(
                self.environments.get(self.environment, {}).get("connected")
            ),
        }


@dataclass
class ConnectionService:
    """Coordinate secure credentials, exact-client consent, and one browser page."""

    credentials: CredentialStore
    consent: ConsentStore
    client: DeltaClient
    migration: MigrationResult
    validator: Validator = field(repr=False)
    page_factory: PageFactory = field(repr=False, default=setup.serve)
    fixed_config: config_mod.Config | None = field(default=None, repr=False)
    credential_environ: Mapping[str, str] | None = field(default=None, repr=False)
    migration_error: str = ""
    credential_error: str = ""
    store_error: str = ""
    _consent_read_unavailable: bool = field(default=False, repr=False)
    _consent_write_unavailable: bool = field(default=False, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _page: setup.Page | None = field(default=None, repr=False)
    _page_client_name: str = ""
    _active_binding: ConsentBinding | None = field(default=None, repr=False)

    @classmethod
    def open(
        cls,
        cfg: config_mod.Config | None = None,
        *,
        credentials: CredentialStore | None = None,
        consent: ConsentStore | None = None,
        memory_consent: MemoryConsentBackend | None = None,
        validator: Validator = credential_check.check,
        page_factory: PageFactory = setup.serve,
    ) -> "ConnectionService":
        """Open each store once, migrate plaintext, and create the live client."""
        config_path = legacy_store.path()
        store_error = ""
        if credentials is not None:
            credential_store = credentials
        else:
            try:
                credential_store = CredentialStore.open(
                    config_path.with_name(default_metadata_path().name)
                )
            except CredentialStoreError as exc:
                store_error = "credential_store_unavailable"
                credential_store = CredentialStore.memory(str(exc))
        shared_memory = memory_consent or MemoryConsentBackend()
        consent_store = consent or ConsentStore(
            config_path.with_name(DEFAULT_CONSENT_NAME),
            secure_backend_available=credential_store.persistent,
            memory_backend=shared_memory,
        )

        migration_error = ""
        try:
            migration = credential_store.migrate(config_path)
        except (CredentialStoreError, OSError, ValueError) as exc:
            migration = MigrationResult(MigrationStatus.ABSENT, "")
            migration_error = type(exc).__name__
        consent_write_unavailable = False
        if migration.status is MigrationStatus.MIGRATED and migration.environment:
            if migration.credential is None or migration.credential.generation is None:
                raise CredentialStoreError(
                    "migration did not publish a credential generation"
                )
            try:
                consent_store.revoke_before(
                    migration.environment, migration.credential.generation
                )
            except ConsentStorageError:
                consent_write_unavailable = True

        fixed = replace(cfg, mode="read") if cfg is not None else None
        environ: Mapping[str, str] | None = None
        if fixed is not None:
            values: dict[str, str] = {}
            if fixed.api_key:
                values["DELTA_API_KEY"] = fixed.api_key
            if fixed.api_secret:
                values["DELTA_API_SECRET"] = fixed.api_secret
            environ = values

        base = (
            replace(fixed, api_key=None, api_secret=None)
            if fixed is not None
            else _load_base_config()
        )
        credential, credential_error = _resolve_credential(
            credential_store, base.env, environ
        )
        live = _bind_config(base, credential)
        client = DeltaClient(live)
        service = cls(
            credentials=credential_store,
            consent=consent_store,
            client=client,
            migration=migration,
            validator=validator,
            page_factory=page_factory,
            fixed_config=fixed,
            credential_environ=environ,
            migration_error=migration_error,
            credential_error=credential_error,
            store_error=store_error,
            _consent_write_unavailable=consent_write_unavailable,
        )
        service._active_binding = service._binding("", credential)
        return service

    @property
    def consent_error(self) -> str:
        """Report unavailable consent reads or mutations through one public status."""
        return (
            _CONSENT_STORE_UNAVAILABLE
            if self._consent_read_unavailable or self._consent_write_unavailable
            else ""
        )

    async def access_state(self, ctx: Context) -> AccessState:
        """Return authorization and a point-of-use checker for this request."""
        client_name, _ = _client_identity(ctx)
        with self._lock:
            base, credential = self._reconcile()
            binding = self._binding(client_name, credential)
            lease = self._consent_lease(binding)
            checker = self._final_checker(lease)
            return AccessState(
                credentials_ready=credential is not None,
                trading_enabled=lease is not None,
                client_name=client_name,
                final_trading_check=checker,
            )

    async def manage_url(self, ctx: Context, required: Access) -> str:
        """Return one live loopback page for the exact request client."""
        del required
        client_name, _ = _client_identity(ctx)
        return self.open_page(client_name).url

    def open_page(
        self, client_name: str = "", *, open_browser: bool = False
    ) -> setup.Page:
        """Open or reuse the one live browser page for an exact client name."""
        with self._lock:
            if (
                self._page is not None
                and self._page.running
                and self._page_client_name == client_name
            ):
                return self._page
            if self._page is not None:
                self._page.stop()
            actions = self._actions(client_name)
            revision = self._revision(client_name)
            self._page = self.page_factory(
                open_browser=open_browser,
                actions=actions,
                revision=revision,
            )
            self._page_client_name = client_name
            return self._page

    def status(self, ctx: Context) -> dict[str, object]:
        """Return connection and trading state without credential material."""
        client_name, client_version = _client_identity(ctx)
        with self._lock:
            return self._status(client_name, client_version).as_dict()

    def close(self) -> None:
        """Stop the current loopback page."""
        with self._lock:
            if self._page is not None:
                self._page.stop()
                self._page = None
                self._page_client_name = ""

    def _actions(self, client_name: str) -> setup.ActionHandler:
        def run(
            action: str,
            arguments: Mapping[str, Any],
            expected: setup.Revision,
        ) -> setup.ActionResult:
            operation = str(arguments.get("operation") or "replace")
            if action == "credentials" and operation == "replace":
                with self._lock:
                    stale = self._stale_action(client_name, expected)
                    if stale is not None:
                        return stale
                    candidate = self._credential_candidate(client_name, arguments)
                    if isinstance(candidate, setup.ActionResult):
                        return candidate
                checked = self._validate(
                    candidate.environment,
                    candidate.api_key,
                    candidate.api_secret,
                )
                with self._lock:
                    stale = self._stale_action(client_name, expected)
                    if stale is not None:
                        return stale
                    return self._replace_credential(
                        client_name,
                        candidate,
                        checked,
                        expected,
                    )

            with self._lock:
                current = self._revision(client_name)
                if action == "status":
                    return setup.ActionResult(
                        self._status(client_name, "").as_dict(),
                        revision=current,
                    )
                if not isinstance(expected, dict) or expected != current:
                    return setup.ActionResult(
                        {"message": "The connection changed. Reload this page."},
                        revision=current,
                        stale=True,
                    )
                if action == "credentials":
                    return self._credential_action(client_name, arguments, expected)
                if action == "consent":
                    return self._consent_action(client_name, arguments, expected)
                return setup.ActionResult(
                    {"status": "rejected", "message": "Unknown action."},
                    revision=current,
                )

        return run

    def _stale_action(
        self,
        client_name: str,
        expected: setup.Revision,
    ) -> setup.ActionResult | None:
        current = self._revision(client_name)
        if isinstance(expected, dict) and expected == current:
            return None
        return setup.ActionResult(
            {"message": "The connection changed. Reload this page."},
            revision=current,
            stale=True,
        )

    def _credential_candidate(
        self,
        client_name: str,
        arguments: Mapping[str, Any],
    ) -> _CredentialCandidate | setup.ActionResult:
        environment = str(arguments.get("environment") or "").strip().lower()
        if environment not in SUPPORTED_ENVIRONMENTS:
            return self._rejected(client_name, "Choose production or testnet.")
        if self._process_credentials_present():
            return self._rejected(
                client_name,
                "Credentials are managed by this MCP client's environment. Change them there.",
            )
        api_key = str(arguments.get("api_key") or "").strip()
        api_secret = str(arguments.get("api_secret") or "").strip()
        if not api_key or not api_secret:
            return self._rejected(client_name, "Enter both the API key and API secret.")
        return _CredentialCandidate(environment, api_key, api_secret)

    def _credential_action(
        self,
        client_name: str,
        arguments: Mapping[str, Any],
        expected: RevisionToken,
    ) -> setup.ActionResult:
        operation = str(arguments.get("operation") or "replace")
        environment = str(arguments.get("environment") or "").strip().lower()
        if environment not in SUPPORTED_ENVIRONMENTS:
            return self._rejected(client_name, "Choose production or testnet.")
        if operation == "activate":
            return self._activate_environment(client_name, environment, expected)
        if operation == "disconnect":
            return self._disconnect(client_name, environment, expected)
        return self._rejected(client_name, "Unknown credential action.")

    def _replace_credential(
        self,
        client_name: str,
        candidate: _CredentialCandidate,
        checked: credential_check.Check,
        expected: RevisionToken,
    ) -> setup.ActionResult:
        environment = candidate.environment
        if not checked.ok and checked.code in _DECISIVE_REJECTION_CODES:
            return self._rejected(
                client_name,
                "Delta rejected the API key or signature. The current connection is unchanged.",
            )

        state = CredentialState.VERIFIED if checked.ok else CredentialState.UNVERIFIED
        account_id = checked.detail if checked.ok else ""
        expected_revision = expected[f"{environment}_credential_revision"]
        expected_generation = expected[f"{environment}_credential_generation"]
        try:
            self.credentials.replace(
                environment,
                candidate.api_key,
                candidate.api_secret,
                state=state,
                account_id=account_id,
                expected_revision=expected_revision,
                expected_generation=expected_generation,
                activate=self._activate_credential,
            )
        except CredentialConflictError:
            return setup.ActionResult(
                {"message": "The credential changed. Reload this page."},
                revision=self._revision(client_name),
                stale=True,
            )
        except CredentialStoreError:
            return self._rejected(
                client_name,
                "The secure credential service could not update the connection.",
            )

        status = "saved" if checked.ok else "unverified"
        try:
            self.consent.revoke_before(environment, expected_generation + 1)
            self._consent_write_unavailable = False
            environment_result = self._set_environment(environment, expected)
        except legacy_store.SettingsConflictError:
            self._reconcile()
            return setup.ActionResult(
                {
                    "status": status,
                    "message": (
                        "The credential was saved, but the active environment changed. "
                        "Reload this page."
                    ),
                },
                revision=self._revision(client_name),
                stale=True,
            )
        except ConsentStorageError:
            self._consent_write_unavailable = True
            self._reconcile()
            return self._rejected(
                client_name,
                "The credential was saved, but trading consent could not be revoked. "
                "The active environment is unchanged.",
            )
        self._reconcile()
        warning = _validation_warning(checked)
        message = (
            "Connection updated. Trading remains off until you enable it below."
            if checked.ok
            else warning
        )
        if environment_result:
            message = f"{message} {environment_result}"
        return setup.ActionResult(
            {
                "status": status,
                "message": message,
                "account_id": account_id,
            },
            revision=self._revision(client_name),
        )

    def _disconnect(
        self,
        client_name: str,
        environment: str,
        expected: RevisionToken,
    ) -> setup.ActionResult:
        if self._process_credentials_present():
            return self._rejected(
                client_name,
                "This credential is managed by the MCP client's environment and cannot be removed here.",
            )
        expected_revision = expected[f"{environment}_credential_revision"]
        expected_generation = expected[f"{environment}_credential_generation"]
        try:
            removed = self.credentials.delete(
                environment,
                expected_revision=expected_revision,
                expected_generation=expected_generation,
            )
        except CredentialConflictError:
            return setup.ActionResult(
                {"message": "The credential changed. Reload this page."},
                revision=self._revision(client_name),
                stale=True,
            )
        except CredentialStoreError:
            return self._rejected(
                client_name,
                "The secure credential service could not disconnect this account.",
            )
        try:
            self.consent.revoke_before(environment, expected_generation + 1)
            self._consent_write_unavailable = False
        except ConsentStorageError:
            self._consent_write_unavailable = True
        self._reconcile()
        return setup.ActionResult(
            {
                "status": "disconnected" if removed else "not_connected",
                "message": "Disconnected."
                if removed
                else "No credential was connected.",
            },
            revision=self._revision(client_name),
        )

    def _activate_environment(
        self,
        client_name: str,
        environment: str,
        expected: RevisionToken,
    ) -> setup.ActionResult:
        try:
            message = self._set_environment(environment, expected)
        except legacy_store.SettingsConflictError:
            self._reconcile()
            return setup.ActionResult(
                {"message": "The active environment changed. Reload this page."},
                revision=self._revision(client_name),
                stale=True,
            )
        except ConsentStorageError:
            self._consent_write_unavailable = True
            return self._rejected(
                client_name,
                "Trading consent could not be revoked. The active environment is unchanged.",
            )
        self._reconcile()
        return setup.ActionResult(
            {
                "status": "selected" if not message else "rejected",
                "message": message or "Environment changed. Trading is off.",
            },
            revision=self._revision(client_name),
        )

    def _consent_action(
        self,
        client_name: str,
        arguments: Mapping[str, Any],
        expected: RevisionToken,
    ) -> setup.ActionResult:
        base, credential = self._reconcile()
        environment = str(arguments.get("environment") or "").strip().lower()
        if environment != base.env:
            return self._rejected(
                client_name, "Select this environment before changing trading consent."
            )
        if not self._matches_expected_credential(environment, credential, expected):
            return setup.ActionResult(
                {"message": "The credential changed. Reload this page."},
                revision=self._revision(client_name),
                stale=True,
            )
        binding = self._binding(client_name, credential)
        if binding is None:
            return self._rejected(
                client_name, "Connect an account before enabling trading."
            )
        enabled = arguments.get("enabled") is True
        if (
            enabled
            and environment == "india_prod"
            and arguments.get("acknowledged") is not True
        ):
            return self._rejected(
                client_name,
                "Confirm that production trading can place real orders.",
            )

        def check_current() -> bool:
            return self._matches_expected_credential(environment, credential, expected)

        try:
            state = (
                self.consent.enable(
                    binding,
                    expected_generation=expected["consent_generation"],
                    check_current=check_current,
                )
                if enabled
                else self.consent.disable(
                    binding,
                    expected_generation=expected["consent_generation"],
                    check_current=check_current,
                )
            )
        except StaleConsentError:
            return setup.ActionResult(
                {"message": "Trading consent changed. Reload this page."},
                revision=self._revision(client_name),
                stale=True,
            )
        except ConsentStorageError:
            self._consent_write_unavailable = True
            return self._rejected(
                client_name,
                "The trading consent service is unavailable. Trading remains disabled.",
            )
        self._consent_write_unavailable = False
        return setup.ActionResult(
            {
                "status": "enabled" if state.enabled else "disabled",
                "message": (
                    "Trading enabled for this client."
                    if state.enabled
                    else "Trading disabled."
                ),
                "persistent": state.persistent,
                "connection": self._status(client_name, "").as_dict(),
            },
            revision=self._revision(client_name),
            complete=state.enabled,
        )

    def _matches_expected_credential(
        self,
        environment: str,
        credential: Credential | None,
        expected: RevisionToken,
    ) -> bool:
        """Recheck the active credential identity immediately before consent changes."""
        if expected.get("active_environment") != _ENVIRONMENT_TOKEN[environment]:
            return False
        if self.fixed_config is not None or _process_setting("DELTA_MCP_ENV"):
            if expected.get("active_environment_generation") != 0:
                return False
        else:
            active_environment, active_generation = legacy_store.environment_state(
                config_mod.DEFAULT_ENV
            )
            if (
                active_environment != environment
                or expected.get("active_environment_generation") != active_generation
            ):
                return False
        if self._process_credentials_present():
            current, error = _resolve_credential(
                self.credentials, environment, self.credential_environ
            )
            return bool(
                not error
                and current is not None
                and credential is not None
                and credential.source is CredentialSource.PROCESS
                and expected.get("active_credential_session_generation")
                == current.session_generation
            )
        if credential is not None and credential.source is CredentialSource.PROCESS:
            return False
        metadata = self._metadata(environment)
        if metadata is None:
            return False
        return (
            expected.get(f"{environment}_credential_revision")
            == (metadata.revision or 0)
            and expected.get(f"{environment}_credential_generation")
            == metadata.generation
        )

    def _rejected(self, client_name: str, message: str) -> setup.ActionResult:
        return setup.ActionResult(
            {"status": "rejected", "message": message},
            revision=self._revision(client_name),
        )

    def _validate(
        self, environment: str, api_key: str, api_secret: str
    ) -> credential_check.Check:
        try:
            return asyncio.run(self.validator(environment, api_key, api_secret))
        except Exception:
            return credential_check.Check(
                ok=False,
                reachable=False,
                detail="validation failed",
            )

    def _set_environment(
        self,
        environment: str,
        expected: RevisionToken,
    ) -> str:
        base = self._base_config()
        expected_token = expected.get("active_environment", 0)
        expected_generation = expected.get("active_environment_generation", -1)
        expected_environment = next(
            (
                name
                for name, token in _ENVIRONMENT_TOKEN.items()
                if token == expected_token
            ),
            "",
        )
        if expected_environment not in SUPPORTED_ENVIRONMENTS:
            raise legacy_store.SettingsConflictError(
                "the active environment token is invalid"
            )
        if self.fixed_config is not None or _process_setting("DELTA_MCP_ENV"):
            if base.env != expected_environment or expected_generation != 0:
                raise legacy_store.SettingsConflictError(
                    "the active environment changed"
                )
            return (
                "The active environment is managed by this MCP client's configuration."
            )

        def revoke() -> None:
            self.consent.revoke_environment(expected_environment)
            self.consent.revoke_environment(environment)
            self._consent_write_unavailable = False

        problem = legacy_store.compare_and_write_environment(
            expected_environment,
            expected_generation,
            environment,
            default_environment=config_mod.DEFAULT_ENV,
            before_publish=revoke,
        )
        if problem is not None:
            return "The active environment could not be changed."
        return ""

    def _activate_credential(self, credential: Credential | None) -> None:
        base = self._base_config()
        if credential is not None and credential.environment != base.env:
            return
        active = credential
        if credential is None:
            active, self.credential_error = _resolve_credential(
                self.credentials, base.env, self.credential_environ
            )
        self.client.rebind(_bind_config(base, active))
        self._active_binding = self._binding("", active)

    def _reconcile(self) -> tuple[config_mod.Config, Credential | None]:
        base = self._base_config()
        credential, error = _resolve_credential(
            self.credentials, base.env, self.credential_environ
        )
        binding = self._binding("", credential)
        previous = self._active_binding
        if previous is not None and previous != binding:
            try:
                self.consent.revoke_identity(previous)
                self._consent_write_unavailable = False
            except ConsentStorageError:
                self._consent_write_unavailable = True
        self.client.rebind(_bind_config(base, credential))
        self._active_binding = binding
        self.credential_error = error
        return base, credential

    def _base_config(self) -> config_mod.Config:
        if self.fixed_config is not None:
            return replace(
                self.fixed_config,
                api_key=None,
                api_secret=None,
                mode="read",
            )
        return _load_base_config()

    def _binding(
        self, client_name: str, credential: Credential | None
    ) -> ConsentBinding | None:
        if credential is None:
            return None
        environment, generation = self._environment_state()
        if credential.environment != environment:
            return None
        return ConsentBinding(
            client_name=client_name,
            environment=credential.environment,
            credential_revision=credential.revision,
            credential_generation=credential.generation,
            credential_session_generation=credential.session_generation,
            environment_generation=generation,
        )

    def _environment_state(self) -> tuple[str, int]:
        if self.fixed_config is not None:
            return self.fixed_config.env, 0
        environment = _process_setting("DELTA_MCP_ENV")
        if environment:
            return environment.lower(), 0
        return legacy_store.environment_state(config_mod.DEFAULT_ENV)

    def _consent_lease(self, binding: ConsentBinding | None) -> ConsentLease | None:
        """Read one approval, or fail only the trading capability closed."""
        if binding is None:
            return None
        try:
            lease = self.consent.lease(binding)
        except ConsentStorageError:
            self._consent_read_unavailable = True
            return None
        self._consent_read_unavailable = False
        return lease

    def _consent_status(self, binding: ConsentBinding | None) -> ConsentState | None:
        """Read consent for status without making account access depend on its store."""
        if binding is None:
            return None
        try:
            state = self.consent.status(binding)
        except ConsentStorageError:
            self._consent_read_unavailable = True
            return None
        self._consent_read_unavailable = False
        return state

    def _final_checker(self, lease: ConsentLease | None) -> Callable[[], bool]:
        if lease is None:
            return _deny

        def current() -> bool:
            try:
                base = self._base_config()
                if base.env != lease.binding.environment:
                    return False
                environment, generation = self._environment_state()
                if (environment, generation) != (
                    lease.binding.environment,
                    lease.binding.environment_generation,
                ):
                    return False
                credential, _ = _resolve_credential(
                    self.credentials, base.env, self.credential_environ
                )
                if credential is None:
                    return False
                if self._binding(lease.binding.client_name, credential) != lease.binding:
                    return False
                confirmed_credential, _ = _resolve_credential(
                    self.credentials, base.env, self.credential_environ
                )
                confirmed = self._binding(
                    lease.binding.client_name,
                    confirmed_credential,
                )
                if confirmed != lease.binding:
                    return False
                accepted = self.consent.accepts(
                    lease,
                    current_credential_revision=confirmed.credential_revision,
                    current_credential_generation=confirmed.credential_generation,
                    current_credential_session_generation=(
                        confirmed.credential_session_generation
                    ),
                    current_environment_generation=confirmed.environment_generation,
                )
                self._consent_read_unavailable = False
                return accepted
            except ConsentStorageError:
                self._consent_read_unavailable = True
                return False
            except Exception:
                return False

        return current

    def _revision(self, client_name: str) -> RevisionToken:
        _, credential = self._reconcile()
        token: RevisionToken = {}
        for environment in SUPPORTED_ENVIRONMENTS:
            metadata = self._metadata(environment)
            token[f"{environment}_credential_revision"] = (
                metadata.revision or 0 if metadata is not None else 0
            )
            token[f"{environment}_credential_generation"] = (
                metadata.generation if metadata is not None else 0
            )
        active_environment, environment_generation = self._environment_state()
        token["active_environment"] = _ENVIRONMENT_TOKEN.get(active_environment, 0)
        token["active_environment_generation"] = environment_generation
        token["active_credential_session_generation"] = (
            credential.session_generation
            if credential is not None and credential.session_generation is not None
            else 0
        )
        binding = self._binding(client_name, credential)
        consent_state = self._consent_status(binding)
        token["consent_generation"] = (
            consent_state.generation if consent_state is not None else 0
        )
        return token

    def _status(self, client_name: str, client_version: str) -> ConnectionStatus:
        base, active = self._reconcile()
        environments: dict[str, dict[str, object]] = {}
        process_override = self._process_credentials_present()
        for environment in SUPPORTED_ENVIRONMENTS:
            metadata = self._metadata(environment)
            is_active = environment == base.env
            is_active_override = environment == base.env and process_override
            is_active_process = bool(
                is_active_override
                and active is not None
                and active.source is CredentialSource.PROCESS
            )
            connected = (
                active is not None
                if is_active
                else metadata is not None and metadata.revision is not None
            )
            metadata_present = metadata is not None and metadata.revision is not None
            source = (
                CredentialSource.PROCESS
                if is_active_override
                else active.source
                if is_active and active is not None
                else self.credentials.source
                if metadata_present
                else None
            )
            environments[environment] = {
                "connected": connected,
                "active": is_active,
                "credential_metadata_present": metadata_present,
                "credential_source": _source_name(source),
                "reconnect_required": bool(
                    not is_active_override
                    and metadata is not None
                    and metadata.reconnect_required
                ),
                "validation_state": (
                    active.state.value
                    if is_active and active is not None
                    else "incomplete"
                    if is_active_override
                    else "unavailable"
                    if is_active and metadata_present and self.credential_error
                    else metadata.state.value
                    if metadata is not None and metadata.state is not None
                    else "unavailable"
                    if metadata is None and self.credential_error
                    else "not_connected"
                ),
                "account_id": (
                    active.account_id
                    if is_active and active is not None
                    else ""
                    if is_active
                    else metadata.account_id
                    if metadata is not None
                    else ""
                ),
                "externally_managed": is_active_override,
                "persistent": bool(
                    not is_active_process
                    and metadata is not None
                    and metadata.revision is not None
                    and self.credentials.persistent
                ),
            }
        binding = self._binding(client_name, active)
        consent_state = self._consent_status(binding)
        return ConnectionStatus(
            environment=base.env,
            client_name=client_name,
            client_version=client_version,
            environments=environments,
            trading={
                "enabled": consent_state.enabled
                if consent_state is not None
                else False,
                "persistent": consent_state.persistent
                if consent_state is not None
                else False,
                "session_only": bool(
                    consent_state is not None and not consent_state.persistent
                ),
            },
            migration_status=(
                "failed" if self.migration_error else self.migration.status.value
            ),
            environment_externally_managed=(
                self.fixed_config is not None or bool(_process_setting("DELTA_MCP_ENV"))
            ),
            connection_error=(
                self.store_error or self.credential_error or self.consent_error
            ),
            consent_error=self.consent_error,
        )

    def _process_credentials_present(self) -> bool:
        environ = self.credential_environ
        supplied = os.environ if environ is None else environ
        return bool(
            (supplied.get("DELTA_API_KEY") or "").strip()
            or (supplied.get("DELTA_API_SECRET") or "").strip()
        )

    def _metadata(self, environment: str) -> CredentialMetadata | None:
        try:
            return self.credentials.metadata(environment)
        except CredentialStoreError:
            self.credential_error = "credential_store_unavailable"
            return None


def _load_base_config() -> config_mod.Config:
    loaded = config_mod.load_without_legacy_credentials()
    return replace(loaded, api_key=None, api_secret=None, mode="read")


def _resolve_credential(
    store: CredentialStore,
    environment: str,
    environ: Mapping[str, str] | None,
) -> tuple[Credential | None, str]:
    if environment not in SUPPORTED_ENVIRONMENTS:
        return None, "unsupported_environment"
    try:
        return store.resolve(environment, environ), ""
    except IncompleteCredentialError:
        return None, "incomplete_process_credentials"
    except CredentialStoreError:
        return None, "credential_store_unavailable"


def _bind_config(
    base: config_mod.Config, credential: Credential | None
) -> config_mod.Config:
    return replace(
        base,
        api_key=credential.api_key if credential is not None else None,
        api_secret=credential.api_secret if credential is not None else None,
        mode="read",
    )


def _client_identity(ctx: Context) -> tuple[str, str]:
    """Read the exact client-provided 2026 request identity from Context."""
    client = request.context_client(ctx)
    return client.name, client.version


def _source_name(source: CredentialSource | None) -> str:
    if source is CredentialSource.OS_STORE:
        return "operating_system"
    if source is CredentialSource.MEMORY:
        return "process_memory"
    if source is CredentialSource.PROCESS:
        return "process_environment"
    return "not_connected"


def _validation_warning(result: credential_check.Check) -> str:
    if result.code in {"UnauthorizedApiAccess", "unauthorized_api_access"}:
        return (
            "Saved as unverified because this key cannot access the validation endpoint. "
            "Trading remains off."
        )
    if result.code == "ip_not_whitelisted_for_api_key":
        return (
            "Saved as unverified because the IP whitelist blocked validation. "
            "Trading remains off."
        )
    if result.code == "SignatureExpired":
        return (
            "Saved as unverified because the system clock prevented validation. "
            "Trading remains off."
        )
    return (
        "Saved as unverified because Delta could not validate it. Trading remains off."
    )


def _process_setting(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _deny() -> bool:
    return False
