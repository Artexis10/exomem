"""Serve the one-upload Claude plugin for one self-hosted Evermatter server.

claude.ai cannot ask a plugin for a server address, so each self-hosted server
serves its own plugin. The generator packages a server edition whose connection
points at a placeholder origin; this module replaces that origin with the
server's own and names the plugin after its host.
"""

from __future__ import annotations

import functools
import io
import json
import zipfile
from urllib.parse import urlsplit

from .cloud_plugins import (
    PACKAGED_EDITION_DIR,
    _json_bytes,
    _validate_claude_manifest,
    server_plugin_name,
)

ROUTE = "/claude-plugin.zip"
_TEMPLATE = PACKAGED_EDITION_DIR / "claude-server.zip"
_MANIFEST = ".claude-plugin/plugin.json"
_CONNECTION = ".mcp.json"
# The entries that name the server; every other entry is copied byte for byte.
_PERSONALISED = (_MANIFEST, _CONNECTION, "README.md")


def _origin(base_url: str) -> str:
    origin = base_url.strip().rstrip("/")
    parts = urlsplit(origin)
    if (
        parts.scheme not in ("https", "http")
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ValueError(
            f"expected the server's public origin, such as https://kb.example.com: {base_url!r}"
        )
    return origin


def archive_filename(base_url: str) -> str:
    return f"{server_plugin_name(_origin(base_url))}.zip"


def check(base_url: str) -> None:
    """Raise when this server cannot serve its plugin, so the route is never a dead link."""
    build(base_url)


def build(base_url: str) -> bytes:
    """Return the plugin archive that connects Claude to the server at ``base_url``."""
    stat = _TEMPLATE.stat()
    return _build(_origin(base_url), stat.st_mtime_ns, stat.st_size)


# The archive depends only on the origin and the installed template; an upgrade changes
# the template's mtime or size, which are part of the key.
@functools.lru_cache(maxsize=4)
def _build(origin: str, mtime_ns: int, size: int) -> bytes:  # noqa: ARG001
    with zipfile.ZipFile(_TEMPLATE) as template:
        (server,) = json.loads(template.read(_CONNECTION))["mcpServers"].values()
        placeholder = server["url"].removesuffix("/mcp")
        replacements = (
            (placeholder, origin),
            (urlsplit(placeholder).netloc, urlsplit(origin).netloc),
        )
        personalised = {}
        for name in _PERSONALISED:
            text = template.read(name).decode("utf-8")
            for old, new in replacements:
                text = text.replace(old, new)
            personalised[name] = text.encode()
        # An origin that JSON would have to escape fails here instead of shipping broken.
        json.loads(personalised[_CONNECTION])
        manifest = json.loads(personalised[_MANIFEST])
        manifest["name"] = server_plugin_name(origin)
        _validate_claude_manifest(manifest)
        personalised[_MANIFEST] = _json_bytes(manifest)
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for info in template.infolist():
                archive.writestr(info, personalised.get(info.filename) or template.read(info))
    return output.getvalue()
