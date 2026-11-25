"""Build the one-upload Claude plugin for one self-hosted Evermatter server.

claude.ai cannot ask a plugin for a server address, so each self-hosted server
serves its own plugin: the release's connector edition (every skill and hook)
plus a `.mcp.json` that names this server. The edition archive is the
generator's committed output, so this module only renames the plugin, rewrites
its lead and adds the connection.
"""

from __future__ import annotations

import functools
import io
import json
import re
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from .cloud_plugins import _validate_claude_manifest

ROUTE = "/claude-plugin.zip"
_EDITION = "claude-connector-hooks.zip"
# The wheel force-includes the generated edition here (pyproject.toml).
_PACKAGED = Path(__file__).parent / "_plugins" / _EDITION
# A source checkout or editable install has no packaged copy; it reads the generated tree.
_CHECKOUT = Path(__file__).resolve().parents[2] / "plugins" / "cloud" / "generated" / _EDITION
_MANIFEST = ".claude-plugin/plugin.json"
_README = "README.md"
_SERVER_KEY = "evermatter"
# Plugin names admit lowercase letters, digits and hyphens: this folds a host's
# other characters (dots, a port's colon) into hyphens. It checks shape, not meaning.
_NOT_NAME_CHARACTER = re.compile(r"[^a-z0-9]+")


def edition_archive() -> Path:
    for path in (_PACKAGED, _CHECKOUT):
        if path.is_file():
            return path
    raise FileNotFoundError(
        f"the Claude connector edition is missing from this install ({_PACKAGED})"
    )


def _origin(base_url: str) -> tuple[str, str]:
    origin = base_url.strip().rstrip("/")
    parts = urlsplit(origin)
    if parts.scheme not in ("https", "http") or not parts.netloc or parts.path:
        raise ValueError(
            f"expected the server's public origin, such as https://kb.example.com: {base_url!r}"
        )
    return origin, parts.netloc.lower()


def plugin_name(base_url: str) -> str:
    """The plugin name for one server: stable, so a later upload replaces the earlier one."""
    _, host = _origin(base_url)
    slug = _NOT_NAME_CHARACTER.sub("-", host).strip("-")
    return f"evermatter-{slug}"[:64].rstrip("-")


def archive_filename(base_url: str) -> str:
    return f"{plugin_name(base_url)}.zip"


def build(base_url: str) -> bytes:
    """Return the plugin archive that connects Claude to the server at ``base_url``."""
    archive = edition_archive()
    stat = archive.stat()
    return _build(base_url, str(archive), stat.st_mtime_ns, stat.st_size)


# The archive depends only on the origin and the installed edition; an upgrade changes
# the edition's mtime or size, which is part of the key.
@functools.lru_cache(maxsize=4)
def _build(base_url: str, archive: str, mtime_ns: int, size: int) -> bytes:  # noqa: ARG001
    origin, host = _origin(base_url)
    lead = (
        f"For your own Evermatter server at {host}: connects Claude to it and adds "
        "automatic recall and capture."
    )
    with zipfile.ZipFile(archive) as source:
        manifest = json.loads(source.read(_MANIFEST))
        # The generator writes the description as the edition's lead, a blank line, then
        # the shared summary; this replaces only the lead.
        _, separator, summary = manifest["description"].partition("\n\n")
        readme = source.read(_README).decode("utf-8")
        _, heading, policies = readme.partition("## Data handling")
        if not separator or not heading:
            raise ValueError(f"{archive} does not have the connector edition's layout")
        manifest.update(
            name=plugin_name(origin), displayName="Evermatter", description=f"{lead}\n\n{summary}"
        )
        _validate_claude_manifest(manifest)
        replaced = {
            _MANIFEST: _json_bytes(manifest),
            _README: (
                f"# Evermatter\n\n{lead}\n\n"
                "## Getting started\n\n"
                "1. Upload this file as a plugin in Claude.\n"
                "2. Open the plugin's Connectors tab and connect Evermatter.\n"
                f"3. Sign in on the page that your server at {host} opens.\n\n"
                "If you already added this server as a custom connector, remove that "
                "connector, so Claude does not hold two connections to the same server. "
                "Then chat normally. When something is worth keeping, ask Evermatter to "
                "remember it; in a new chat, ask about what you saved or where you left off.\n\n"
                f"{heading}{policies}"
            ).encode(),
        }
        connection = {"mcpServers": {_SERVER_KEY: {"type": "http", "url": f"{origin}/mcp"}}}
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as target:
            for info in source.infolist():
                target.writestr(info, replaced.get(info.filename) or source.read(info))
            info = zipfile.ZipInfo(".mcp.json", (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            target.writestr(info, _json_bytes(connection))
    return output.getvalue()


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
