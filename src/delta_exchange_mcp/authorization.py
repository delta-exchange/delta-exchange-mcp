"""Request-scoped authorization for account and trading tool calls."""

import json
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from mcp import types
from mcp.server.apps import APP_MIME_TYPE, EXTENSION_ID
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, InputRequiredResult, TextContent
from mcp_types import CLIENT_CAPABILITIES_META_KEY, ClientCapabilities
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

from delta_exchange_mcp.connection_app import VIEW_URI
from delta_exchange_mcp.tools import account, trading

Access = Literal["account", "trading"]
FinalTradingCheck = Callable[[], bool]


@dataclass(frozen=True)
class AccessState:
    """The authorization state that applies to one tool request."""

    credentials_ready: bool
    trading_enabled: bool
    client_name: str
    final_trading_check: FinalTradingCheck


StateProvider = Callable[[Context], Awaitable[AccessState]]
ManageUrlProvider = Callable[[Context, Access], Awaitable[str | None]]

_INPUT_KEY = "delta_exchange_authorization"
_STATE_VERSION = 1
_LEGACY_PROTOCOL = "2025-11-25"


class ToolAuthorization:
    """Apply account and trading gates without changing the tool registry."""

    def __init__(
        self,
        state: StateProvider,
        manage_url: ManageUrlProvider | None = None,
    ) -> None:
        self._state = state
        self._manage_url = manage_url

    async def before_call(
        self, name: str, arguments: dict[str, object], ctx: Context
    ) -> CallToolResult | InputRequiredResult | None:
        """Return a gate result, or None when the tool call may continue."""
        if name == "setup_credentials" and self._manage_url is not None:
            return await self.setup(ctx)
        if name not in account.TOOL_NAMES and name not in trading.TOOL_NAMES:
            return None

        current = await self._state(ctx)
        resumed = self._resumed_access(ctx)

        if name in account.TOOL_NAMES:
            if current.credentials_ready:
                return None
            return await self._blocked(ctx, "account", resumed)

        if arguments.get("dry_run") is True:
            return None

        if current.credentials_ready and current.trading_enabled:
            if resumed in {"account", "trading"}:
                return self._result(
                    "Trading is enabled. The pending trade was not sent. Submit a new "
                    "tool call when the user still wants it.",
                    status="authorization_complete",
                )
            return None

        required: Access = "account" if not current.credentials_ready else "trading"
        return await self._blocked(ctx, required, resumed)

    async def setup(self, ctx: Context) -> CallToolResult | InputRequiredResult | None:
        """Open the browser setup flow when a URL provider is installed."""
        if self._manage_url is None:
            return None
        if self._resumed_access(ctx) == "account":
            if self._response_action(ctx) in {"decline", "cancel"}:
                return self._result(
                    "Authorization was cancelled.",
                    status="authorization_cancelled",
                    error=True,
                )
            return self._result(
                "The browser flow is open. Finish it, then call "
                "get_connection_status.",
                status="authorization_pending",
            )
        return await self._prompt(ctx, "account")

    async def _blocked(
        self, ctx: Context, required: Access, resumed: Access | None
    ) -> CallToolResult | InputRequiredResult:
        if resumed is not None:
            action = self._response_action(ctx)
            if action in {"decline", "cancel"}:
                return self._result(
                    "Authorization was cancelled. No request was sent to Delta.",
                    status="authorization_cancelled",
                    error=True,
                )
            if resumed == required:
                return self._result(
                    "Authorization is not complete. No request was sent to Delta. "
                    "Finish the browser flow, then retry the tool call.",
                    status="authorization_pending",
                    error=True,
                )
        return await self._prompt(ctx, required)

    async def _prompt(
        self, ctx: Context, required: Access
    ) -> CallToolResult | InputRequiredResult:
        url = await self._url(ctx, required)
        message = self._message(required)

        if url is not None and self._supports_url(ctx):
            if ctx.protocol_version in MODERN_PROTOCOL_VERSIONS:
                request = types.ElicitRequest(
                    params=types.ElicitRequestURLParams(
                        message=message,
                        url=url,
                        elicitationId=secrets.token_urlsafe(16),
                    )
                )
                return InputRequiredResult(
                    inputRequests={_INPUT_KEY: request},
                    requestState=self._encode_state(required),
                )

            if ctx.protocol_version == _LEGACY_PROTOCOL:
                response = await ctx.elicit_url(
                    message=message,
                    url=url,
                    elicitation_id=secrets.token_urlsafe(16),
                )
                if response.action in {"decline", "cancel"}:
                    return self._result(
                        "Authorization was cancelled. No request was sent to Delta.",
                        status="authorization_cancelled",
                        error=True,
                    )
                return self._result(
                    "The browser flow is open. No request was sent to Delta. Finish it, "
                    "then retry the tool call.",
                    status="authorization_pending",
                )

        if url is not None and self._supports_apps(ctx):
            text = f"{message} Open the [Manage Connection page]({url})."
            return CallToolResult(
                content=[TextContent(type="text", text=text)],
                structuredContent={"status": "input_required", "message": text},
                _meta={"ui": {"resourceUri": VIEW_URI, "manageUrl": url}},
                isError=False,
            )

        if url is not None:
            text = f"{message} Open the [Manage Connection page]({url}), then retry."
            return self._result(text, status="input_required", error=True)

        return self._result(
            f"{message} Call setup_credentials to open the connection form.",
            status="input_required",
            error=True,
        )

    async def _url(self, ctx: Context, required: Access) -> str | None:
        if self._manage_url is None:
            return None
        return await self._manage_url(ctx, required)

    @staticmethod
    def _message(required: Access) -> str:
        if required == "trading":
            return "Enable trading in the Delta Exchange connection page to continue."
        return "Connect your Delta Exchange account to continue."

    @staticmethod
    def _supports_url(ctx: Context) -> bool:
        capabilities = ToolAuthorization._request_capabilities(ctx)
        elicitation = capabilities.elicitation if capabilities is not None else None
        return elicitation is not None and elicitation.url is not None

    @staticmethod
    def _supports_apps(ctx: Context) -> bool:
        capabilities = ToolAuthorization._request_capabilities(ctx)
        extensions = capabilities.extensions if capabilities is not None else None
        settings = extensions.get(EXTENSION_ID) if extensions else None
        if settings is None:
            return False
        mime_types = settings.get("mimeTypes")
        return isinstance(mime_types, list | tuple) and APP_MIME_TYPE in mime_types

    @staticmethod
    def _request_capabilities(ctx: Context) -> ClientCapabilities | None:
        try:
            meta = ctx.request_context.meta
        except ValueError:
            return None
        raw = meta.get(CLIENT_CAPABILITIES_META_KEY) if meta is not None else None
        try:
            return (
                raw
                if isinstance(raw, ClientCapabilities)
                else ClientCapabilities.model_validate(raw)
            )
        except (TypeError, ValueError):
            if ctx.protocol_version in MODERN_PROTOCOL_VERSIONS:
                return None
            try:
                return ctx.client_capabilities
            except ValueError:
                return None

    @staticmethod
    def _encode_state(required: Access) -> str:
        return json.dumps(
            {"access": required, "version": _STATE_VERSION},
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _resumed_access(ctx: Context) -> Access | None:
        if ctx.request_state is None:
            return None
        try:
            state = json.loads(ctx.request_state)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(state, dict) or state.get("version") != _STATE_VERSION:
            return None
        access = state.get("access")
        return access if access in {"account", "trading"} else None

    @staticmethod
    def _response_action(ctx: Context) -> str | None:
        responses = ctx.input_responses
        if responses is None:
            return None
        response = responses.get(_INPUT_KEY)
        return getattr(response, "action", None)

    @staticmethod
    def _result(
        message: str, *, status: str, error: bool = False
    ) -> CallToolResult:
        return CallToolResult(
            content=[TextContent(type="text", text=message)],
            structuredContent={"status": status, "message": message},
            isError=error,
        )
