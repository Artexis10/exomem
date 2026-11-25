"""Where Evermatter Cloud answers MCP requests, defined once and cheap to import.

`cloud_import_client` and `evermatter setup --migrate` both need the address, and the first loads
the MCP client stack, so the definition lives here.
"""

from __future__ import annotations

from urllib.parse import urlsplit

#: The address the Cloud plugin used before Release C2 moves it. `evermatter setup --migrate` keeps
#: an `exomem` registration that points here, because the plugin's move carries it and one rename
#: now would sign that user in twice. Registrations made before the move keep this host after it,
#: so the constant stays when `MCP_URL` changes.
LEGACY_MCP_URL = "https://exomem.substratesystems.io/mcp"
#: The current Cloud MCP address: the `resource` of the Cloud plugin definition, which
#: `tests/test_cloud_import_client.py` compares. Release C2 gives this the new address.
MCP_URL = LEGACY_MCP_URL


def is_cloud_url(url: str) -> bool:
    """Whether ``url`` points at an Evermatter Cloud host: the current one or the legacy one."""
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return False
    known = {urlsplit(address).hostname for address in (MCP_URL, LEGACY_MCP_URL)}
    return host is not None and host in known
