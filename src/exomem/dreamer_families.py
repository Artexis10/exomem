"""The dreamer's candidate families and the per-page dispatch that feeds them.

A family turns one changed page into zero or more upkeep proposals, each an
existing governed action the agent may take. Every family has three hooks:

* `on_page(ctx, rel_path)` for a changed page that still exists;
* `on_delete(ctx, rel_path)` for a page that is gone;
* `revalidate(ctx, candidate)` for an open candidate whose subject or evidence
  page just changed. It reads only that candidate's own pages.

Contributions are per page and idempotent: a page's proposals are recomputed
from that page (and bounded graph lookups) alone and replace what it proposed
before, so nothing here needs a vault-wide snapshot.

Families never write the vault, never take the writer lease, never enqueue graph
debt, never build or repair an index and never load a model. They hold no
writer handle; the route each proposal carries names an existing governed leaf
the agent calls under that leaf's own authority.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import dreamer_store, find_corpus

#: Every served item says this, the vocabulary advisory's exact phrase.
PERMISSION = "consideration does not authorize mutation"

#: The producer name for detections made by the dreamer itself.
PRODUCER = "dreamer"


@dataclass
class Context:
    """One page's processing context: the store transaction and small memos."""

    vault_root: Path
    store: dreamer_store.DreamerStore
    conn: sqlite3.Connection
    now: float
    _pages: dict[str, Any] = field(default_factory=dict)
    _review: dict[str, Any] = field(default_factory=dict)

    def page(self, rel_path: str) -> Any | None:
        """One parsed page through the shared parse cache, or None when gone.

        Lazy: a family that needs no Markdown never pays a parse.
        """
        if rel_path not in self._pages:
            path = Path(self.vault_root) / rel_path
            try:
                self._pages[rel_path] = find_corpus.CACHE.get(path, Path(self.vault_root))
            except OSError:
                self._pages[rel_path] = None
        return self._pages[rel_path]

    def review_payload(self) -> dict[str, Any] | None:
        """The review-state payload, read once per context; None when unreadable."""
        if "payload" not in self._review:
            from . import review_state

            store = review_state.ReviewStateStore(self.vault_root)
            try:
                self._review["payload"] = store.load()
            except ValueError:
                self._review["payload"] = None
            self._review["store"] = store
        return self._review["payload"]

    def review_store(self) -> Any:
        self.review_payload()
        return self._review["store"]


@dataclass(frozen=True)
class Family:
    name: str
    kinds: tuple[str, ...]
    on_page: Callable[[Context, str], None]
    on_delete: Callable[[Context, str], None]
    revalidate: Callable[[Context, dict[str, Any]], None]
    #: Needs vault-wide counts, so stays silent until a reseed drains.
    global_counts: bool = False


#: The families this build implements, in registry order.
REGISTRY: list[Family] = []


def family_names() -> tuple[str, ...]:
    return tuple(family.name for family in REGISTRY)


def family_for(name: str) -> Family | None:
    return next((family for family in REGISTRY if family.name == name), None)


def process_page(ctx: Context, rel_path: str, *, exists: bool) -> None:
    """Run every family over one changed page, then revalidate what it touches.

    `on_page`/`on_delete` own every proposal whose SUBJECT is this page: they
    recompute them and resolve the ones that no longer hold. A proposal that
    merely cites this page as evidence belongs to another subject, so it is
    revalidated from its own pages instead.
    """
    cited_by = set(ctx.store.candidates_for_path(ctx.conn, rel_path))
    for family in REGISTRY:
        if exists:
            family.on_page(ctx, rel_path)
        else:
            family.on_delete(ctx, rel_path)
    for cid in sorted(cited_by):
        row = ctx.store.candidate(ctx.conn, cid)
        if row is None or row.get("state") != "open" or row.get("subject_path") == rel_path:
            continue
        family = family_for(str(row.get("family") or ""))
        if family is not None:
            family.revalidate(ctx, row)
