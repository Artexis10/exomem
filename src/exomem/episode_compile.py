"""The `compile` block of a recorded recap: what the close step has left to cite.

A recap is a Source. A live compiled note that names it in `sources:` covers it
(`episode_coverage`), so the close step is: record the recap, then update the note a
decided item changes, and cite the recap from each compiled note updated. An entity,
plan or record page holds no `sources:` claim, so it is updated without a citation and
its line stays open. This block reports the recap's ref to cite, how many Decided items
it holds and how many no recap cited by a live page holds.

It claims nothing about a note's content. Coverage counts recaps and lines, so
the `outcome` states that result only; a next step rides separately and only
when live policy permits capture, so a recap request never orders a page write
that the policy forbids.

The record path never walks the vault. It reads the episode's own revisions
and the cached pages that cite them, through the writers' resolver snapshot
taken without the disk fallback. The snapshot is probed before any revision is
read: when it is cold, the open count is unchecked (`None`) whatever the recap's
`ingested_into` holds, so the block never tells a withheld citing page from none.
A live revision whose Decided count differs from the count just recorded is
unchecked too, never "already cited".
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import capture_sweep, episode_coverage, find, lifecycle_statuses, note_types
from .governance import egress

log = logging.getLogger(__name__)

# The one next step, sent only when live policy permits capture. It is a
# condition for the agent to judge, because the server cannot tell which note
# a decided item changes.
_NEXT_STEP = (
    "If a decided item changes a note, update that note. Cite this recap in the sources "
    "of each compiled note you update; entity, plan and record pages take no citation."
)


def _outcome(decided: int, open_: int | None) -> str:
    """The result of the coverage count, never an instruction."""
    items = f"{decided} decided item" + ("" if decided == 1 else "s")
    if open_ is None:
        return f"This recap has {items}; whether a live page cites it was not checked."
    held = decided - open_
    count = "none is" if not held else "1 is" if held == 1 else f"{held} are"
    return f"This recap has {items}; {count} already cited by a live page."


def _open(root: Path, revisions: Sequence[str], decided: int) -> int | None:
    """The Decided lines of the newest revision that no cited revision holds, or `None` when unchecked."""
    # Not `find.writer_resolver_snapshot(root)`: a cold cache would cost a walk of the vault.
    snapshot = find.writer_resolver_snapshot(root, allow_disk=False)
    if snapshot is None:
        return None
    pages = [page for page in (find._CACHE.get(root / path, root) for path in revisions) if page is not None]  # noqa: SLF001
    keep = egress.visible_page_filter(root)

    def lookup(path: str) -> Any:
        return None if keep is not None and not keep(path) else find._CACHE.get(root / path, root)  # noqa: SLF001

    coverage = episode_coverage.evaluate(
        root,
        pages,
        lookup=lookup,
        resolver=lambda: snapshot,
        status_basis=lifecycle_statuses.Basis(root),
        type_basis=note_types.Basis(root),
    )
    # The live revision read back holds a different Decided count than was recorded: claim nothing.
    return None if coverage is None or coverage.decided != decided else len(coverage.open)


def block(vault_root: Path, *, ref: str, revisions: Sequence[str], decided: int) -> dict[str, Any] | None:
    """The `compile` block, or `None` for a recap with no Decided item.

    `revisions` are the vault-relative paths of the episode's recap pages, oldest
    first, the newest being the recap just recorded. Never raises: the recap is
    already committed.
    """
    if not decided:
        return None
    try:
        open_ = _open(Path(vault_root), revisions, decided)
        permitted = capture_sweep._proactive_capture_permitted()  # noqa: SLF001
    except Exception:  # noqa: BLE001 - the recap already committed; the block is advisory
        log.debug("episode compile block unavailable", exc_info=True)
        open_, permitted = None, False
    return {
        "outcome": _outcome(decided, open_),
        # A next step only when policy permits capture and a decided item is still uncited.
        **({"next_step": _NEXT_STEP} if permitted and open_ != 0 else {}),
        "cite": ref,
        "decided": decided,
        "open": open_ if open_ is not None else "unchecked",
    }
