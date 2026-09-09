"""Authenticated account identity from Delta's trading preferences."""

from dataclasses import dataclass
from typing import Any

from delta_exchange_mcp.client import DeltaClient


class InvalidIdentityResponse(ValueError):
    """Delta returned no usable account identity."""


@dataclass(frozen=True)
class AccountIdentity:
    """The account id and the validated upstream response."""

    user_id: int
    response: dict[str, Any]


async def fetch_account_identity(client: DeltaClient) -> AccountIdentity:
    """Fetch and validate the API-key account identity."""
    response = await client.get("/users/trading_preferences", auth=True)
    if not isinstance(response, dict):
        raise InvalidIdentityResponse(
            "Delta returned a non-object trading-preferences response"
        )

    result = response.get("result")
    if not isinstance(result, dict):
        raise InvalidIdentityResponse(
            "Delta returned no trading-preferences result object"
        )

    user_id = result.get("user_id")
    if isinstance(user_id, bool) or not isinstance(user_id, int):
        raise InvalidIdentityResponse(
            "Delta returned trading preferences without an integer result.user_id"
        )
    return AccountIdentity(user_id=user_id, response=response)
