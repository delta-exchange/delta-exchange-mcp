"""Who is being served right now.

The SDK hands the protocol session to middleware and to handlers that declare a `Context`
parameter, and nowhere else. A shared decorator cannot declare a parameter for the
function it wraps, so `DeltaMCP` publishes the session here for the trade gate to read.
Unset means there is no protocol session — an in-process call, as the tests make.
"""

from __future__ import annotations

from contextvars import ContextVar

from mcp.server.session import ServerSession

session: ContextVar[ServerSession | None] = ContextVar(
    "delta_request_session", default=None
)


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
