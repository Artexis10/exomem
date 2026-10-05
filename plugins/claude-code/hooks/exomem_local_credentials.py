"""Local-service credentials for self-hosted hooks, never shipped in Cloud archives.

Native-MCP hooks do not load this module. The local installer deploys it beside
the canonical hook scripts for their existing REST and loopback transports.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path


def service_env_path() -> Path | None:
    explicit = os.environ.get("EXOMEM_SERVICE_ENV", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    if os.name == "nt":
        return None
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Exomem" / "service.env"
    base = os.environ.get("XDG_CONFIG_HOME", "").strip() or str(Path.home() / ".config")
    return Path(base) / "exomem" / "service.env"


def service_env_value(name: str, path: Path | None) -> str:
    """Read the first dotenv value, preserving the managed install's quoting."""
    if path is None:
        return ""
    try:
        text = path.read_text(encoding="utf-8").lstrip("\ufeff")
    except Exception:  # noqa: BLE001 — hooks must fail softly
        return ""
    for line in text.splitlines():
        match = re.match(rf"^\s*{re.escape(name)}\s*=\s*(.*)$", line)
        if not match:
            continue
        value = match.group(1).strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        elif len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1]
        return value.strip()
    return ""


def rest_key(read_value) -> tuple[str, str]:
    """Return the key and its origin without logging either."""
    key = os.environ.get("EXOMEM_REST_API_KEY", "").strip()
    if key:
        return key, "env"
    key = read_value("EXOMEM_REST_API_KEY")
    return (key, "file") if key else ("", "")


def local_credential(read_port) -> tuple[str, int | None]:
    """Read the explicitly issued loopback token, never a provider OAuth token."""
    path = os.environ.get("EXOMEM_LOCAL_TOKEN_FILE", "").strip()
    if not path:
        return "", None
    try:
        token = Path(path).expanduser().read_text(encoding="ascii").strip()
    except Exception:  # noqa: BLE001 — hooks must fail softly
        return "", None
    if not token or any(character.isspace() for character in token):
        return "", None
    port = read_port()
    return (token, port) if port is not None else ("", None)
