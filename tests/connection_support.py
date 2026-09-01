"""Shared fixtures for connection-service integration tests."""

import asyncio
from collections.abc import Callable, Mapping
from types import SimpleNamespace
from typing import Any, cast

import pytest
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CLIENT_INFO_META_KEY, Implementation

from delta_exchange_mcp import credentials as credential_check
from delta_exchange_mcp import setup, store
from delta_exchange_mcp.auth.connection import ConnectionService
from delta_exchange_mcp.auth.consent import ConsentStore, MemoryConsentBackend
from delta_exchange_mcp.auth.store import (
    CredentialSource,
    CredentialStore,
    MemoryMetadata,
    MemorySecretBackend,
)
from delta_exchange_mcp.tools import trading


def context(name: str, version: str = "1") -> Context:
    request = SimpleNamespace(
        meta={
            CLIENT_INFO_META_KEY: Implementation(name=name, version=version),
        },
        protocol_version="2026-07-28",
    )
    return Context(request_context=cast(Any, request))


def stores(*, persistent: bool = True) -> tuple[CredentialStore, ConsentStore]:
    source = CredentialSource.OS_STORE if persistent else CredentialSource.MEMORY
    credentials = CredentialStore(
        MemorySecretBackend(),
        MemoryMetadata(),
        source,
    )
    consent = ConsentStore(
        store.path().with_name("consent.json"),
        secure_backend_available=persistent,
        memory_backend=MemoryConsentBackend(),
    )
    return credentials, consent


def service(
    validator: Any,
    *,
    persistent: bool = True,
) -> ConnectionService:
    credentials, consent = stores(persistent=persistent)
    return ConnectionService.open(
        credentials=credentials,
        consent=consent,
        validator=validator,
    )


def action(
    connection: ConnectionService,
    client_name: str,
    name: str,
    arguments: Mapping[str, Any],
    revision: setup.Revision | None = None,
) -> setup.ActionResult:
    expected = connection._revision(client_name) if revision is None else revision
    return connection._actions(client_name)(name, arguments, expected)


async def verified(
    environment: str,
    api_key: str,
    api_secret: str,
) -> credential_check.Check:
    del environment, api_key, api_secret
    return credential_check.Check(
        ok=True,
        reachable=True,
        detail="42",
    )


def assert_place_order_blocked(
    monkeypatch: pytest.MonkeyPatch,
    connection: ConnectionService,
    final_check: Callable[[], bool],
) -> None:
    gate = trading.TradeGate()
    mcp = MCPServer("blocked-place-order")
    trading.register(mcp, connection.client, None, gate)
    mutations: list[str] = []

    async def post(
        path: str,
        payload: dict[str, Any],
        *,
        auth: bool = False,
    ) -> dict[str, Any]:
        del payload, auth
        mutations.append(path)
        return {}

    monkeypatch.setattr(connection.client, "post", post)

    async def invoke() -> object:
        gate.bind_final_check(final_check)
        return await mcp.call_tool(
            "place_order",
            {
                "product_id": 27,
                "size": 1,
                "side": "buy",
                "order_type": "market_order",
            },
        )

    with pytest.raises(ToolError, match="trading was disabled"):
        asyncio.run(invoke())
    assert mutations == []
