"""The `list_inbound_links` Tier 2 op: find files wikilinking to a target.

Read-only. Useful before `move_file` (to know what update_wikilinks will
touch) or `delete_file` (to know what would break). Matches three forms:
- `[[Knowledge Base/Notes/Insights/foo]]`  (full path)
- `[[Notes/Insights/foo]]`                 (KB-stripped path)
- `[[foo]]`                                (bare basename, only when unique)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .vault import (
    VaultPathError,
    find_inbound_wikilinks,
    resolve_under_vault,
)

log = logging.getLogger(__name__)


@dataclass
class ListInboundLinksResult:
    target: str
    inbound: list[dict]
    count: int

    def as_dict(self) -> dict:
        return {
            "target": self.target,
            "inbound": self.inbound,
            "count": self.count,
        }


@dataclass
class ListInboundLinksError(Exception):
    code: str
    reason: str

    def as_dict(self) -> dict:
        return {"code": self.code, "reason": self.reason}


def list_inbound_links(
    vault_root: Path,
    *,
    target: str,
    visible: Callable[[str], bool] | None = None,
) -> ListInboundLinksResult:
    if not target or not target.strip():
        raise ListInboundLinksError(
            code="INVALID_TARGET", reason="target is required"
        )

    # If target looks path-like, resolve it (existence not required — caller
    # may be checking "what links to this file I'm about to delete?").
    raw = str(target).strip()
    if "/" in raw or raw.endswith(".md"):
        from .vault_store import read as store_read

        # A path that the store serves is the page at its store key: resolving the whole path
        # would follow a link at the page's own name. A page that the store no longer holds can
        # still be named by links, so its path stands as given.
        try:
            key = store_read.served_key(vault_root, raw)
        except FileNotFoundError:
            key = raw
        if key is not None:
            target_norm = key
        else:
            try:
                _abs, rel = resolve_under_vault(vault_root, raw)
                target_norm = rel
            except VaultPathError as e:
                raise ListInboundLinksError(code=e.code, reason=e.reason) from e
    else:
        # Bare basename — pass through as-is; find_inbound_wikilinks
        # handles the bare form.
        target_norm = raw

    matches = find_inbound_wikilinks(vault_root, target_norm, visible=visible)
    return ListInboundLinksResult(
        target=target_norm,
        inbound=[m.as_dict() for m in matches],
        count=len(matches),
    )
