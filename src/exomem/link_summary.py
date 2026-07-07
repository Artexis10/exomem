"""Inbound/outbound wikilink summaries for read surfaces."""

from __future__ import annotations

from pathlib import Path

from . import vault
from .vault import find_body_wikilinks


def outbound_link_targets(body: str) -> list[str]:
    """De-duplicated outbound wikilink targets from a markdown body."""
    outbound: list[str] = []
    seen: set[str] = set()
    for m in find_body_wikilinks(body):
        target = m.group(0)[2:-2].split("|", 1)[0].split("#", 1)[0].strip()
        if target and target not in seen:
            seen.add(target)
            outbound.append(target)
    return outbound


def link_summary(vault_root: Path, rel_path: str, body: str) -> dict:
    """Inbound + outbound wikilink summary for the `get(links=True)` option."""
    inbound = (
        [m.as_dict() for m in vault.find_inbound_wikilinks(vault_root, rel_path)]
        if rel_path
        else []
    )
    return {"inbound": inbound, "outbound": outbound_link_targets(body)}
