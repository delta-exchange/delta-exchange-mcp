"""Who is being served right now: the handshake identity and the connection behind a session."""

from __future__ import annotations

from dataclasses import dataclass

from mcp.server.session import ServerSession


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


def peer(current: ServerSession | None) -> object | None:
    """What stays the same across one client's requests.

    A session is built per request, so it cannot key anything that has to outlive one
    call, such as the form's one-use grant.
    The connection behind it is that thing, and this is where the SDK keeps it — its
    public `connection` accessor hangs off a context class the runner does not build yet.
    Falling back to the session fails closed: a per-request identity refuses a grant rather
    than sharing it.
    """
    if current is None:
        return None
    return getattr(current, "_connection", current)
