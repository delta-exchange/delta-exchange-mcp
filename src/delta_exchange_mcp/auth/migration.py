"""Legacy dotenv migration and publication."""

import io
import logging
import os
import stat
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from dotenv import dotenv_values
from dotenv.parser import parse_stream

from delta_exchange_mcp.auth.backend import (
    CredentialState,
    CredentialStoreError,
    EnvironmentState,
    _file_lock,
    _sync_after_publication,
    normalize_environment,
)

if TYPE_CHECKING:
    from delta_exchange_mcp.auth.store import Credential, CredentialStore

logger = logging.getLogger(__name__)

_CREDENTIAL_NAMES = frozenset({"DELTA_API_KEY", "DELTA_API_SECRET"})


class MigrationStatus(StrEnum):
    ABSENT = "absent"
    INCOMPLETE = "incomplete"
    UNAVAILABLE = "unavailable"
    MIGRATED = "migrated"
    CONFLICT = "conflict"


class MigrationError(CredentialStoreError):
    pass


@dataclass(frozen=True)
class MigrationResult:
    status: MigrationStatus
    environment: str
    credential: "Credential | None" = None


def migrate(store: "CredentialStore", config_path: Path) -> MigrationResult:
    """Move one complete legacy file credential into a persistent store."""
    with _file_lock(config_path):
        if config_path.is_symlink():
            return MigrationResult(MigrationStatus.UNAVAILABLE, "")
        try:
            original = config_path.read_text()
        except FileNotFoundError:
            return MigrationResult(MigrationStatus.ABSENT, "")
        except OSError as exc:
            raise MigrationError(f"could not read {config_path}: {exc}") from exc

        parsed = dotenv_values(stream=io.StringIO(original))
        key = (parsed.get("DELTA_API_KEY") or "").strip()
        secret = (parsed.get("DELTA_API_SECRET") or "").strip()
        environment = (parsed.get("DELTA_MCP_ENV") or "india_prod").strip().lower()
        env = normalize_environment(environment)
        if not key and not secret:
            return MigrationResult(MigrationStatus.ABSENT, env)
        if not key or not secret:
            return MigrationResult(MigrationStatus.INCOMPLETE, env)
        if not store.persistent:
            return MigrationResult(MigrationStatus.UNAVAILABLE, env)

        staged = _stage_replacement(config_path, _without_credentials(original))
        try:
            with store._metadata.lock():
                values = store._metadata.read()
                store._cleanup_pending_locked(values, env)
                current = store._get_locked(env, values)
                if current is not None and (
                    current.api_key != key or current.api_secret != secret
                ):
                    return MigrationResult(MigrationStatus.CONFLICT, env, current)

                previous = values.get(env, EnvironmentState())
                transaction = store._prepare_replace_locked(
                    values,
                    env,
                    key,
                    secret,
                    state=(
                        current.state
                        if current is not None
                        else CredentialState.UNVERIFIED
                    ),
                    account_id=current.account_id if current is not None else "",
                    expected_revision=current.revision if current is not None else 0,
                    expected_generation=previous.generation,
                )
                transaction.write_new()
                transaction.publish()
                try:
                    os.replace(staged, config_path)
                except OSError as exc:
                    try:
                        transaction.rollback()
                    except Exception as rollback_exc:
                        raise MigrationError(
                            f"could not publish the migrated {config_path}; "
                            "credential rollback also failed"
                        ) from rollback_exc
                    raise MigrationError(
                        f"could not publish the migrated {config_path}: {exc}"
                    ) from exc
                staged = None
                _sync_after_publication(config_path.parent)

                try:
                    transaction.retire_previous()
                except Exception as exc:
                    logger.warning(
                        "could not retire inactive credential revision %s for %s "
                        "after migration publication; cleanup remains pending: %s",
                        transaction.previous.active_revision,
                        env,
                        exc,
                    )
                return MigrationResult(
                    MigrationStatus.MIGRATED,
                    env,
                    transaction.new_credential,
                )
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)


def _stage_replacement(path: Path, body: str) -> Path:
    staged: Path | None = None
    try:
        mode = stat.S_IMODE(path.stat().st_mode) & 0o700
        fd, name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}-",
            suffix=".tmp",
        )
        staged = Path(name)
        with os.fdopen(fd, "w") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(staged, mode)
        return staged
    except OSError as exc:
        if staged is not None:
            staged.unlink(missing_ok=True)
        raise MigrationError(
            f"could not stage the migration for {path}: {exc}"
        ) from exc


def _without_credentials(body: str) -> str:
    return "".join(
        binding.original.string
        for binding in parse_stream(io.StringIO(body))
        if binding.key not in _CREDENTIAL_NAMES
    )
