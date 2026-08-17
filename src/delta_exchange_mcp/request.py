"""Who is being served right now.

The SDK hands the protocol session to middleware and to handlers that declare a `Context`
parameter, and nowhere else. A shared decorator cannot declare a parameter for the
function it wraps, so `DeltaMCP` publishes the session here for the trade gate to read.
Unset means there is no protocol session — an in-process call, as the tests make.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass

from mcp.server.session import ServerSession

session: ContextVar[ServerSession | None] = ContextVar(
    "delta_request_session", default=None
)

# The tool being served, for the analytics header on whatever calls Delta. Published the
# same way and for the same reason as the session: the outbound request is made deep inside
# the shared HTTP client, which has no argument for it and should not grow one on every one
# of the forty-six tools.
tool: ContextVar[str] = ContextVar("delta_request_tool", default="")


@dataclass(frozen=True)
class Client:
    """How the connected host names itself in the handshake.

    Self-reported and unauthenticated: a client may claim any name. It scopes convenience
    settings and labels analytics. It must never gate anything that carries a safety
    consequence.

    `title` is the host's display name and **may be set by the person using it**, so it is
    never used as a key — two people running the same client would otherwise land in
    different places, and one who renamed theirs would lose whatever was stored under the
    old name. It is forwarded to Delta with the rest of the handshake, which is why the
    "never sent anywhere" that used to be written here no longer holds.

    `name` is reported exactly as sent, proxy suffix and all, because which bridge a
    request came through is worth counting. `config.stable_name` strips that suffix, and
    only for keying a stored setting.
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
