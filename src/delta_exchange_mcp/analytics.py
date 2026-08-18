"""What each request tells Delta about the client that caused it.

Nothing in MCP carries this to an API. A tool call arrives over a pipe, and the request the
server then makes to Delta looks identical whichever client asked. So questions as ordinary
as "which clients do people actually use", "is anyone on the old version", and "which tools
get called" have no answer at the other end unless this server puts one there.

The handshake's own description of the client is forwarded, aggregated later rather than
chosen now: recovering a field nobody sent is impossible. The discrete headers carry what
gets filtered on, so a log pipeline can group by them without parsing anything; one JSON
header carries the long tail.

What is forwarded is a closed set, decided here. A client's capability map is not closed —
`experimental` and `extensions` are open dictionaries an extension author fills with
whatever that extension needs, which in the wild has meant tokens and local URLs. Only the
structural answer travels: what kind of thing this client can do, never a key or a value
out of those maps.

Three rules here are about not breaking requests, and they outrank completeness:

* **Bounded.** Gateways commonly cap total header bytes near 8KB and answer 431 over it,
  which fails the person's actual question rather than costing a metric. `description` and
  `icons` are unbounded strings chosen by the client, so the whole set is budgeted and the
  JSON header gives way first.
* **Encoded at the seam.** These strings come from whatever the client chose to call
  itself. A newline in one would split the header and let the rest be read as another. They
  are percent-encoded, which also removes the non-latin-1 values an HTTP library rejects
  outright.
* **Never credential-shaped.** The same invariant the audit log and the debug log already
  hold. No key, no secret, no signature, no fingerprint of any of them.

None of this is authenticated. A client reports whatever name it likes, so these values
label activity and must never gate anything.
"""

from __future__ import annotations

import json
import platform
import secrets
import sys
from urllib.parse import quote
from weakref import WeakKeyDictionary

from mcp.server.session import ServerSession

from delta_exchange_mcp.config import Config
from delta_exchange_mcp.version import PACKAGE_VERSION

PREFIX = "X-Delta-MCP-"
CONTEXT_HEADER = f"{PREFIX}Context"

# Our whole set, well under the ~8KB a gateway typically allows for all headers together,
# leaving room for the signing headers and whatever a proxy adds on the way.
BUDGET_BYTES = 4096

# Per discrete value. A client name longer than this is not a name.
_FIELD_LIMIT = 200

# Printable ASCII that needs no escaping in a header value. Two deliberate choices. `%` is
# absent, so a percent sign in the output always means an escape and the value stays
# unambiguous. `"` is present, so the JSON header arrives as readable JSON a log pipeline
# can parse directly — escaping it would force every consumer to decode twice, and a quote
# cannot split a header the way a newline can.
_SAFE = " !\"#$&'()*+,-./:;<=>?@[]^_`{|}~"


def encode(value: str) -> str:
    """Make one raw string safe to be a header value, changing nothing else about it.

    For the discrete fields only. The JSON header must not come through here — see
    `as_header`.

    Undecodable input is replaced rather than raised on. A Python string can hold an
    unpaired surrogate that no UTF-8 encoder accepts, a client is free to send one in its
    name, and the strict default would then raise here — inside the header build, before
    the request leaves — turning a cosmetic field into a failed tool call.
    """
    return quote(value, safe=_SAFE, errors="replace")


def clean(value: str) -> str:
    """A header-safe rendering of one bounded field the client chose.

    Bounded after encoding rather than before. One emoji is one character to `[:200]` and
    twelve characters once percent-encoded, so bounding the input let a 200-character name
    become a 2400-character header and overrun the budget this limit exists to keep.

    A cut lands anywhere, including inside a `%XX` escape, so it is moved back off a
    partial one: a truncated escape does not decode and some proxies reject it outright.
    """
    encoded = encode(value.strip())
    if len(encoded) <= _FIELD_LIMIT:
        return encoded
    cut = encoded[:_FIELD_LIMIT]
    # An escape is three characters, so only the last two can be part of an unfinished one.
    return cut[: cut.rfind("%")] if "%" in cut[-2:] else cut


def as_header(payload: dict[str, object]) -> str:
    """Serialise the context object for a header value, without breaking the JSON.

    `json.dumps` already produces something safe to put in a header, and this is easy to
    miss: `ensure_ascii` is on by default, so every non-ASCII character becomes `\\uXXXX`
    and every control character becomes `\\n` or similar. The result is printable ASCII
    containing no newline that could end the header early.

    It must therefore **not** be percent-encoded afterwards. Doing that escapes the
    backslashes JSON uses while leaving the quotes those backslashes escape, so a title of
    `he said "hi"` goes out as `{"title":"he said %5C"hi%5C""}` — malformed to anything
    reading the header as JSON, which is the one thing this header is for. It survived a
    round trip in testing only because decoding first happens to put it back together, and
    the whole reason quotes are left unescaped is so no consumer has to decode first.

    The check is a security seam rather than a formatting nicety: a literal newline in a
    header value ends it, and everything after is read as another header. `ensure_ascii`
    already rules that out, and this refuses to send anything if it ever stops being true.

    It asks whether every character is printable ASCII rather than naming the characters
    that are not. That is the whole of what a header value may hold, so the test cannot be
    incomplete — an earlier version compared against 32 and let DEL through, which is the
    kind of gap an enumeration leaves and an invariant does not. Space passes, which JSON
    with these separators never emits anyway.
    """
    text = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    if not (text.isascii() and text.isprintable()):
        return ""
    return text


class Sessions:
    """Stable identifiers for connections, minted here because nothing supplies one.

    The handshake carries no session or installation id, so two requests from one person
    cannot otherwise be recognised as related. This mints one per connection.

    Keyed weakly on the connection, so a connection that closes takes its identifier with
    it and nothing accumulates in a server that runs for days. The identifier is random
    rather than derived from the object, because a memory address is reused after a free
    and would silently merge two people's activity.
    """

    def __init__(self) -> None:
        self._ids: WeakKeyDictionary[object, str] = WeakKeyDictionary()

    def id_for(self, peer: object | None) -> str:
        if peer is None:
            return ""
        try:
            minted = self._ids.get(peer)
            if minted is None:
                minted = secrets.token_hex(8)
                self._ids[peer] = minted
        except TypeError:
            # Not weak-referenceable. Correlating requests is a convenience; refusing to
            # send one over it is not.
            return ""
        return minted


def _params(session: ServerSession | None):
    if session is None:
        return None
    return getattr(session, "client_params", None)


# The four capabilities that are closed types in the protocol, so their presence is a fact
# about the client and nothing an extension author chose to put there. Named here rather
# than read off the model, so a field added to the protocol later cannot start forwarding
# itself: widening this set stays a decision someone makes on purpose.
_CAPABILITY_NAMES = ("sampling", "elicitation", "roots", "tasks")

# The two that are open `dict[str, dict[str, Any]]` maps. Counted, never read. Both the
# keys and the values belong to an extension this server knows nothing about, and an
# extension is free to keep a token or a local address in there.
_OPEN_CAPABILITY_NAMES = ("experimental", "extensions")


def _capabilities(capabilities: object | None) -> dict[str, object]:
    """What kind of client this is, without one string the client chose.

    Presence for the closed capabilities, a count for the open maps. This replaced a
    `model_dump` of the whole map, which forwarded an extension's private settings to
    Delta on every request — an `experimental` entry holding an API key arrived intact.
    """
    if capabilities is None:
        return {}
    out: dict[str, object] = {}
    for name in _CAPABILITY_NAMES:
        if getattr(capabilities, name, None) is not None:
            out[name] = True
    for name in _OPEN_CAPABILITY_NAMES:
        if declared := getattr(capabilities, name, None):
            out[name] = len(declared)
    return out


def context(session: ServerSession | None) -> dict[str, object]:
    """Everything the handshake offered beyond the fields that get filtered on.

    Read defensively rather than by attribute path: this is the one place reading a client's
    self-description, and a client that omits half of it must not take a tool call down.
    """
    params = _params(session)
    if params is None:
        return {}
    info = getattr(params, "client_info", None)
    capabilities = getattr(params, "capabilities", None)
    out: dict[str, object] = {}
    for name in ("title", "description", "website_url"):
        value = getattr(info, name, None)
        if value:
            out[name] = str(value)[:_FIELD_LIMIT]
    icons = getattr(info, "icons", None)
    if icons:
        out["icons"] = len(icons)
    declared = _capabilities(capabilities)
    if declared:
        out["capabilities"] = declared
    out["platform"] = f"{platform.system()} {platform.machine()}"
    out["python"] = f"{sys.version_info.major}.{sys.version_info.minor}"
    return out


def headers(
    session: ServerSession | None,
    tool: str,
    cfg: Config,
    sessions: Sessions,
    peer: object | None,
) -> dict[str, str]:
    """The analytics headers for one outbound request to Delta.

    Assembled per request rather than per connection because the tool name changes with
    every call, and because a form save can rebind the environment and the mode underneath
    a live connection.
    """
    params = _params(session)
    info = getattr(params, "client_info", None)
    discrete = {
        f"{PREFIX}Version": PACKAGE_VERSION,
        f"{PREFIX}Client": clean(getattr(info, "name", "") or ""),
        f"{PREFIX}Client-Version": clean(getattr(info, "version", "") or ""),
        f"{PREFIX}Session": sessions.id_for(peer),
        f"{PREFIX}Env": cfg.env,
        f"{PREFIX}Mode": cfg.mode,
        f"{PREFIX}Tool": clean(tool),
        f"{PREFIX}Protocol": clean(str(getattr(params, "protocol_version", "") or "")),
    }
    out = {name: value for name, value in discrete.items() if value}

    # Bounded by construction, not by hope: every value here is either ours and short —
    # the package version, the minted session id, and `env` and `mode`, both validated
    # against a closed set before a Config exists — or passed through `clean`, which caps
    # it at `_FIELD_LIMIT` encoded characters. Eight such headers cannot approach the
    # budget, which is what leaves the whole of it for the JSON context below.
    spent = sum(len(name) + len(value) + 4 for name, value in out.items())
    extra = context(session)
    # Shed the long tail a field at a time, largest and least filtered on first. Truncating
    # the JSON itself would produce a header no consumer can parse at all, so it is always
    # either whole or one field shorter. The loop retries after every drop, including after
    # the last one, so the smallest useful payload still gets its chance.
    droppable = ["capabilities", "description", "icons", "title", "website_url"]
    while extra:
        encoded = as_header(extra)
        if encoded and spent + len(CONTEXT_HEADER) + len(encoded) + 4 <= BUDGET_BYTES:
            out[CONTEXT_HEADER] = encoded
            break
        if not droppable:
            break  # even the minimum overruns the budget; send none of it
        extra.pop(droppable.pop(0), None)
    return out
