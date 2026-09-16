"""What this server tells a client about each tool, in the client's own vocabulary.

A client uses these to decide how to present a tool and how loudly to confirm before
calling it. They are advisory: the spec is explicit that a client must not trust them from
an untrusted server, so nothing here is a safety boundary. The real boundaries are
elsewhere — the trading surface is absent from the tool list unless it was armed, and every
mutation carries `_meta["delta.exchange/mutating"]`, which is what the bundle verifier reads
rather than inferring safety from a tool's name.

Both helpers return a fresh model rather than exposing a shared constant. One instance
handed to forty-six registrations would be forty-six references to one mutable object.

`external` is `openWorldHint`: true when the tool reaches Delta's API, false when it only
reads or writes local state, such as the settings file. Almost every tool here is external;
the status tools and the mode save are the exceptions.
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

    `destructive` follows the spec's own sense of the word: overwriting or removing state
    rather than adding to it. Placing an order is additive and so not destructive, while
    cancelling or editing one is. That reading looks too mild for a tool that spends real
    money, which is why the stronger signal lives in `_meta` instead of being smuggled in
    here — a hint that means something other than what it says is worse than no hint.

    `idempotent` asks what a repeat does. Cancelling an order twice leaves it cancelled;
    placing twice leaves two orders; adding margin twice adds it twice.
    """
    return ToolAnnotations(
        title=title,
        read_only_hint=False,
        destructive_hint=destructive,
        idempotent_hint=idempotent,
        open_world_hint=external,
    )
