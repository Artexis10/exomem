"""Coverage flags relative to the caller's view.

A flag that reports a derived structure as stale, lagging or incomplete flips on
any write, including a write to a page a restricted caller may not see. That
makes the flag a one-bit write signal. For such a caller, the lag counts only
when a path that changed since the structure's published generation is one the
caller may see (OpenSpec: bound-request-latency-across-surfaces, task 3.17).
The owner takes no part in this: its callers pass no `visible` and keep the
vault-wide flag.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import freshness

#: The most changed paths one request decides. Above it the lag is reported, so
#: a request's work follows the lag and never the vault. PROVISIONAL: a lag this
#: long is a burst that the publisher is already folding, and its reading is
#: stale for a caller who may see any of it; re-measure, don't hand-tune.
MAX_DECIDED_PATHS = 16


def only_withheld(
    vault_root: Path,
    delta: Any,
    visible: Callable[[str], bool],
    *,
    pending: bool = False,
) -> bool:
    """Whether the lag `delta` names changes nothing this caller may see.

    `delta` is a registry delta (`freshness.delta_since` or `recall_delta_since`)
    from the structure's published generation. `visible` is the release decision
    the caller's request already uses (`egress.visible_page_filter`). With
    `pending`, the external events the registry has observed but not yet
    published count as lag too.

    True only when the delta is bridged, small and made wholly of paths that
    `visible` rejects. Every other case reports the lag: an unbridgeable delta
    says nothing about what changed, so the structure cannot be called complete.
    """
    if not delta.complete or getattr(delta, "requires_source_proof", False):
        return False
    paths = {*delta.changed, *delta.deleted}
    if pending:
        if freshness.external_pending_unscoped(vault_root):
            return False
        paths |= freshness.external_pending_paths(vault_root)
    if len(paths) > MAX_DECIDED_PATHS:
        return False
    for path in paths:
        rel = freshness.registry_rel(vault_root, path)
        # `visible` keeps a path that is no longer a page (a deleted one), and a
        # path that cannot be decided counts as visible: the truthful answer
        # when what the caller could have seen is unknown.
        try:
            if rel is None or visible(rel):
                return False
        except Exception:  # noqa: BLE001 - an undecidable path is counted visible
            return False
    return True
