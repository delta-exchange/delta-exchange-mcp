"""How this server names itself to a person, and where that name points.

The bundle's install dialog and the MCP handshake describe one product, so the display
copy lives here once and both read it from here. Restating either string in the packaging
script is what let them drift apart before.

This copy is deliberately not the PyPI summary. That one is written for a developer
choosing a dependency; this one is written for the person installing a connector, who
sees it in a dialog or a client's server list.

URLs are not restated here either. They are canonical in `[project.urls]`, and a running
process reads them back from the installed package metadata, the same way `version.py`
reads the version.
"""

from importlib.metadata import PackageNotFoundError, metadata

DISPLAY_NAME = "Delta Exchange"

SHORT_DESCRIPTION = "Live market data and your Delta Exchange India account."


def project_url(label: str) -> str | None:
    """A URL declared in `[project.urls]`, or None when it is not declared.

    None rather than an empty string, because that is what the handshake field takes: a
    declared empty URL is a worse answer than an absent one.
    """
    try:
        declared = metadata("delta-exchange-mcp").get_all("Project-URL") or []
    except PackageNotFoundError:  # a source tree that was never installed
        return None
    for entry in declared:
        name, _, url = entry.partition(",")
        if name.strip() == label:
            return url.strip()
    return None


HOMEPAGE = project_url("Homepage")
