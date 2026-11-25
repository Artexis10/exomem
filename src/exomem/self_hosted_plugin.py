"""Serve the one-upload Claude plugin for one self-hosted Evermatter server.

claude.ai cannot ask a plugin for a server address, so each self-hosted server
serves its own plugin. The generator packages a server edition whose connection
points at a placeholder origin and whose release is a placeholder; this module
fills in the server's origin and installed release and names the plugin after
its host.
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
from .package_skills import RELEASE_PLACEHOLDER, release_version

ROUTE = "/claude-plugin.zip"
_TEMPLATE = PACKAGED_EDITION_DIR / "claude-server.zip"
_MANIFEST = ".claude-plugin/plugin.json"
_CONNECTION = ".mcp.json"
_README = "README.md"


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


def build(base_url: str) -> bytes:
    """Return the plugin archive that connects Claude to the server at ``base_url``."""
    stat = _TEMPLATE.stat()
    return _build(_origin(base_url), release_version(), stat.st_mtime_ns, stat.st_size)


# The archive depends only on the origin, the release and the installed template; an
# upgrade changes the template's mtime or size, which are part of the key.
@functools.lru_cache(maxsize=4)
def _build(origin: str, release: str, mtime_ns: int, size: int) -> bytes:  # noqa: ARG001
    host = urlsplit(origin).netloc
    with zipfile.ZipFile(_TEMPLATE) as template:
        connection = json.loads(template.read(_CONNECTION))
        (server,) = connection["mcpServers"].values()
        placeholder = urlsplit(server["url"]).netloc
        server["url"] = f"{origin}/mcp"
        manifest = json.loads(template.read(_MANIFEST))
        manifest.update(
            name=server_plugin_name(origin),
            version=release,
            description=manifest["description"].replace(placeholder, host),
        )
        _validate_claude_manifest(manifest)
        personalised = {
            _CONNECTION: _json_bytes(connection),
            _MANIFEST: _json_bytes(manifest),
            _README: template.read(_README).decode("utf-8").replace(placeholder, host).encode(),
        }
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for info in template.infolist():
                data = personalised.get(info.filename) or template.read(info)
                if info.filename.startswith("skills/") and info.filename.endswith(".md"):
                    data = data.decode("utf-8").replace(RELEASE_PLACEHOLDER, release).encode()
                archive.writestr(info, data)
    return output.getvalue()
