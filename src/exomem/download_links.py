"""Short download links: a random id the serving process maps to a signed download grant.

A signed grant carries its principal and artifact descriptor, so it runs to about
2,000 characters. An agent that must copy such a link token by token corrupts it,
and the download then fails. The process that serves `/download` keeps each grant
in memory under a random 128-bit id and hands out that id instead. The grant still
decides every download: an id only names it. The table lives in memory on purpose.
A grant expires within minutes, and a restart that forgets the table only means
that the agent asks for a fresh link. A process that does not serve `/download`
cannot resolve an id, so it keeps handing out the full grant.
"""

from __future__ import annotations

import re
import secrets
import threading
import time

# Bounds memory if a client mints links in a loop; the oldest links go first.
_MAX_LINKS = 4096
# The id format: 16 random bytes in URL-safe base64 (secrets.token_urlsafe), no padding.
ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{22}")

_lock = threading.Lock()
_links: dict[str, tuple[str, int]] = {}
_serving = False


def serve() -> None:
    """Mark this process as the one that serves `/download`, so its ids resolve."""
    global _serving
    _serving = True


def shorten(grant: str, expires_at: int) -> str | None:
    """Return a new id for `grant`, or None when this process cannot resolve one."""
    if not _serving:
        return None
    link_id = secrets.token_urlsafe(16)
    now = int(time.time())
    with _lock:
        for key in [key for key, (_, expiry) in _links.items() if expiry <= now]:
            del _links[key]
        while len(_links) >= _MAX_LINKS:
            del _links[next(iter(_links))]
        _links[link_id] = (grant, expires_at)
    return link_id


def expand(presented: str) -> str | None:
    """The grant an id names, the presented value when it is not an id, or None for an unknown or expired id."""
    if not ID_PATTERN.fullmatch(presented):
        return presented
    with _lock:
        found = _links.get(presented)
    if found is None or found[1] <= int(time.time()):
        return None
    return found[0]
