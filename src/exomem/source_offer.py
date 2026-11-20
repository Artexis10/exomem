"""The AGPL-3.0 section 13 source offer that Exomem Cloud shows the people who use it.

One place holds the two repository URLs and the sentence. The MCP server instructions of a
cloud cell carry the sentence, and the control plane's app pages show the same two links.
"""

from __future__ import annotations

from . import __version__

SERVER_REPOSITORY_URL = "https://github.com/Artexis10/exomem"
CONTROL_PLANE_REPOSITORY_URL = "https://github.com/substrate-systems/substrate"


def offer_text() -> str:
    """The offer, naming the release that this process runs."""
    return (
        f"Source code (AGPL-3.0): {SERVER_REPOSITORY_URL} and {CONTROL_PLANE_REPOSITORY_URL}. "
        f"Running release: v{__version__}."
    )
