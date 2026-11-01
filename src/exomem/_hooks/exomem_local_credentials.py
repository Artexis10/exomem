"""Local-service credentials for self-hosted hooks, never shipped in Cloud archives.

Native-MCP hooks do not load this module. The local installer deploys it beside
the canonical hook scripts for their existing REST and loopback transports; the
retrieve and capture hooks load it only on those transports, and a missing copy
reads as "no credential configured".

This module owns every read of a credential from the user's machine and every
request that carries one. A hook receives a `Lane`, an opaque route that builds
the authenticated request for a URL and body, and never holds the secret itself.

Sources, in the order a lane resolves them:

- `EXOMEM_LOCAL_TOKEN_FILE` names a local client token (`exomem auth
  issue-local`). With a local port (`EXOMEM_LOCAL_PORT`, else the managed
  install's `service.env`) it yields the `local` lane: the managed service's
  local listener on `127.0.0.1`.
- `EXOMEM_REST_API_KEY` in the environment, else the same name in the managed
  install's `service.env` (`EXOMEM_SERVICE_ENV` overrides its path), yields the
  `rest` lane against the hook's configured REST origin.

A key read from `service.env` travels only to loopback: an `EXOMEM_HOST` that
points anywhere else yields no `rest` lane for it, so the hook cannot become the
primitive that posts a key it lifted off disk, plus the prompt, to another host.
A key the user exported into the environment keeps its configured host, since
the user placed both variables.
"""

from __future__ import annotations

import os
import re
import sys
import urllib.request
from collections.abc import Iterator
from pathlib import Path

# The loopback spellings fixed by the network standards, not a vocabulary a user extends.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


class Lane:
    """One credentialed route to the managed service.

    `origin` is the base URL the lane fixes (`local`), or `None` when the hook's
    configured REST origin applies (`rest`).
    """

    __slots__ = ("name", "origin", "_token")

    def __init__(self, name: str, origin: str | None, token: str) -> None:
        self.name = name
        self.origin = origin
        self._token = token

    def __repr__(self) -> str:
        return f"Lane({self.name!r})"  # never the token: tracebacks and logs print this

    def request(self, url: str, body: bytes) -> urllib.request.Request:
        """The authenticated JSON POST for `url`."""
        return urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._token}",
            },
        )


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


def rest_key() -> tuple[str, str]:
    """Return the key and its origin without logging either."""
    key = os.environ.get("EXOMEM_REST_API_KEY", "").strip()
    if key:
        return key, "env"
    key = service_env_value("EXOMEM_REST_API_KEY", service_env_path())
    return (key, "file") if key else ("", "")


def local_port() -> int | None:
    """The managed service's local listener port: this env, else `service.env`."""
    value = os.environ.get("EXOMEM_LOCAL_PORT", "").strip() or service_env_value(
        "EXOMEM_LOCAL_PORT", service_env_path()
    )
    if not value.isascii() or not value.isdigit():
        return None
    port = int(value)
    return port if 1 <= port <= 65535 else None


def local_credential() -> tuple[str, int | None]:
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
    port = local_port()
    return (token, port) if port is not None else ("", None)


def local_lane() -> Lane | None:
    token, port = local_credential()
    if token and port is not None:
        return Lane("local", f"http://127.0.0.1:{port}", token)
    return None


def rest_lane(host: str) -> Lane | None:
    """The key's lane for the REST origin on `host`; none for a disk-read key off loopback."""
    key, source = rest_key()
    if not key or (source == "file" and host not in LOOPBACK_HOSTS):
        return None
    return Lane("rest", None, key)


def lanes(host: str) -> Iterator[Lane]:
    """The local lane, then the key's lane. Each resolves only when asked for it."""
    local = local_lane()
    if local is not None:
        yield local
    rest = rest_lane(host)
    if rest is not None:
        yield rest
