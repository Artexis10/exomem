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
    withheld_before: Callable[[frozenset[str]], frozenset[str]] | None = None,
) -> bool:
    """Whether the lag `delta` names changes nothing this caller may see.

    `delta` is a registry delta (`freshness.delta_since` or `recall_delta_since`)
    from the structure's published generation. `visible` is the release decision
    the caller's request already uses (`egress.visible_page_filter`). With
    `pending`, the external events the registry has observed but not yet
    published count as lag too.

    `visible` keeps a path that is no longer a page, so a deleted page reads as
    one the caller may see. `withheld_before`, when given, names which of the
    deleted paths this caller's earlier verdict already held as withheld before
    the page left; the published structure never served such a page to this
    caller, so its leaving changes nothing the caller was shown.

    True only when the delta is bridged, small and made wholly of paths that
    `visible` rejects or `withheld_before` names. Every other case reports the
    lag: an unbridgeable delta says nothing about what changed, so the structure
    cannot be called complete.
    """
    return (
        withheld_lag(
            vault_root, delta, visible, pending=pending, withheld_before=withheld_before
        )
        is not None
    )


def withheld_lag(
    vault_root: Path,
    delta: Any,
    visible: Callable[[str], bool],
    *,
    pending: bool = False,
    withheld_before: Callable[[frozenset[str]], frozenset[str]] | None = None,
) -> frozenset[str] | None:
    """`only_withheld`, naming what `visible` alone would have kept.

    `None` when the lag shows anything this caller may see. Otherwise the
    deleted paths that `visible` keeps and `withheld_before` proved withheld:
    a request that serves the structure as published must treat them as
    withheld, because `visible` cannot reject a path that is no longer a page.
    """
    if not delta.complete or getattr(delta, "requires_source_proof", False):
        return None
    paths = {*delta.changed, *delta.deleted}
    if pending:
        if freshness.external_pending_unscoped(vault_root):
            return None
        paths |= freshness.external_pending_paths(vault_root)
    if len(paths) > MAX_DECIDED_PATHS:
        return None
    deleted = set(delta.deleted) - set(delta.changed) if withheld_before is not None else set()
    proven: frozenset[str] | None = None
    held: set[str] = set()
    for path in paths:
        rel = freshness.registry_rel(vault_root, path)
        # A path that cannot be decided counts as visible: the truthful answer
        # when what the caller could have seen is unknown.
        try:
            if rel is None:
                return None
            if visible(rel):
                if path not in deleted:
                    return None
                if proven is None:
                    proven = withheld_before(_rels(vault_root, deleted))
                if rel not in proven:
                    return None
                held.add(rel)
        except Exception:  # noqa: BLE001 - an undecidable path is counted visible
            return None
    return frozenset(held)


def _rels(vault_root: Path, paths: set[Any]) -> frozenset[str]:
    return frozenset(
        rel for path in paths if (rel := freshness.registry_rel(vault_root, path)) is not None
    )


def recall_lag_only_withheld(
    vault_root: Path,
    scope: str,
    built: freshness.RecallFreshnessCheckpoint | None,
    visible: Callable[[str], bool],
) -> bool:
    """Whether a structure published at recall checkpoint `built` trails only by withheld pages.

    The lag is read from the live registry and never walks. It counts as visible
    when `built` is unknown, the scope is not live, the access policy moved, or
    the delta is not bridged, because each leaves what changed unknown.
    """
    return recall_withheld_lag(vault_root, scope, built, visible) is not None


def recall_withheld_lag(
    vault_root: Path,
    scope: str,
    built: freshness.RecallFreshnessCheckpoint | None,
    visible: Callable[[str], bool],
) -> frozenset[str] | None:
    """`recall_lag_only_withheld`, with the deleted paths a verdict proved withheld (`withheld_lag`)."""
    current = freshness.live_recall_checkpoint(vault_root, scope)
    if built is None or current is None:
        return None
    # A policy change moves what every page releases, whichever pages were written.
    if (built.policy_version, built.access_policy_fingerprint) != (
        current.policy_version,
        current.access_policy_fingerprint,
    ):
        return None
    return withheld_lag(
        vault_root,
        freshness.recall_delta_since(vault_root, scope, built),
        visible,
        withheld_before=_held_withheld(vault_root, scope, built, visible),
    )


def _held_withheld(
    vault_root: Path,
    scope: str,
    built: freshness.RecallFreshnessCheckpoint,
    visible: Callable[[str], bool],
) -> Callable[[frozenset[str]], frozenset[str]] | None:
    """What the lexical catalogue's published verdicts already hold as withheld for this caller.

    Valid only while the catalogue is at `built`, which the proof itself
    re-checks. A caller that offers no admission identity has no published
    verdicts, so none of its deleted paths is proven.
    """
    from . import lexstore
    from .published_admission import admission_of

    admission = admission_of(visible)
    if admission is None:
        return None
    store = lexstore.get_store(vault_root)
    return lambda paths: store.published_withheld_paths(scope, built, admission, paths)
