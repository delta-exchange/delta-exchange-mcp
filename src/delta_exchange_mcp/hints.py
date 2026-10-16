"""What this server tells a client about each tool, in the client's own vocabulary.

A client uses these to decide how to present a tool and whether to confirm before calling
it. They are advisory: the spec says a client must not trust them from an untrusted
server, so nothing here is a safety boundary. The API key's own permissions are.

Both helpers return a fresh model, so no two registrations share one mutable object.

`external` is `openWorldHint`: true when the tool reaches Delta's API, false when it only
reads or writes local state.
"""

from __future__ import annotations

from mcp.types import ToolAnnotations


def reads(title: str, *, external: bool = True) -> ToolAnnotations:
    """A tool that only reports, and changes nothing."""
    return ToolAnnotations(
        title=title,
        read_only_hint=True,
        open_world_hint=external,
    )


def mutates(
    title: str, *, destructive: bool, idempotent: bool, external: bool = True
) -> ToolAnnotations:
    """A tool that changes something.

    `destructive` is the spec's sense of the word: overwriting or removing state rather
    than adding to it. Placing an order adds and is not destructive; cancelling or editing
    one is. `idempotent` asks what a repeat does: cancelling twice leaves the order
    cancelled, placing twice leaves two orders, adding margin twice adds it twice.
    """
    return ToolAnnotations(
        title=title,
        read_only_hint=False,
        destructive_hint=destructive,
        idempotent_hint=idempotent,
        open_world_hint=external,
    )
