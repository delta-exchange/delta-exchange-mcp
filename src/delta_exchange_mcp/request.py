"""Who is being served right now.

The SDK hands the protocol session to middleware and to handlers that declare a `Context`
parameter, and nowhere else. A shared decorator cannot declare a parameter for the
function it wraps, so `DeltaMCP` publishes the session here for the trade gate to read.
Unset means there is no protocol session — an in-process call, as the tests make.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass

from mcp.server.mcpserver import Context
from mcp.server.session import ServerSession
from mcp_types import CLIENT_INFO_META_KEY, Implementation
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

session: ContextVar[ServerSession | None] = ContextVar(
    "delta_request_session", default=None
)


@dataclass(frozen=True)
class Client:
    """How the connected host names itself in the handshake.

    Self-reported and unauthenticated: a client may claim any name. It scopes convenience
    settings and labels analytics. It must never gate anything that carries a safety
    consequence.

    `title` is the host's display name and may be set by the person using it, so it is
    read for completeness but never sent anywhere or used as a key.
    """

    name: str = ""
    title: str = ""
    version: str = ""


UNKNOWN = Client()


def client(current: ServerSession | None) -> Client:
    """Read the handshake identity behind a session.

    Empty fields mean no handshake reached this call — an in-process call, as the tests
    make. A caller treats an empty name as "this cannot be scoped to a client" and
    declines to write anything keyed on it, rather than writing under a name that every
    client on the machine would then share.
    """
    if current is None:
        return UNKNOWN
    params = current.client_params
    if params is None:
        return UNKNOWN
    info = params.client_info
    return Client(name=info.name, title=info.title or "", version=info.version)


def context_client(ctx: Context) -> Client:
    """Read the exact client identity carried by this MCP request.

    MCP 2026 is request-scoped and has no initialization session. Its client identity
    therefore comes from request ``_meta``. Older protocol versions keep the session
    fallback for compatibility.
    """
    try:
        meta = ctx.request_context.meta
    except ValueError:
        meta = None
    raw = meta.get(CLIENT_INFO_META_KEY) if meta is not None else None
    try:
        info = (
            raw
            if isinstance(raw, Implementation)
            else Implementation.model_validate(raw)
        )
    except (TypeError, ValueError):
        if ctx.protocol_version in MODERN_PROTOCOL_VERSIONS:
            return UNKNOWN
        try:
            return client(ctx.session)
        except ValueError:
            return UNKNOWN
    return Client(name=info.name, title=info.title or "", version=info.version)


def peer(current: ServerSession | None) -> object | None:
    """What stays the same across one client's requests.

    A session is built per request, so it cannot key anything that has to outlive one
    call: the trade lease, the form's one-use grant, the once-per-connection entitlement.
    The connection behind it is that thing, and this is where the SDK keeps it — its
    public `connection` accessor hangs off a context class the runner does not build yet.
    Falling back to the session fails closed: a per-request identity refuses a lease and
    a grant rather than sharing either.
    """
    if current is None:
        return None
    return getattr(current, "_connection", current)
