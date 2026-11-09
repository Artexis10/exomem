"""The derived-row half of one store-mode commit (OpenSpec make-vault-store-canonical D2).

``apply`` runs inside ``write.commit``'s transaction, after the page tables hold the commit's own
bytes. It writes the lexical, graph and identity rows of every page that the commit gives bytes to,
drops the rows of every page that it removes, and re-derives the graph rows of the pages whose
links the commit re-resolves. It reads the transaction's own tables for pages (and the disk for a
page that the store does not own), so a reader that sees the commit sees its rows, and a kill leaves
no row without its page.

The registries (traversal profiles, the type basis) are the exception: the owners' builders read
them from disk, while the schema overlay under ``_Schema`` is store-owned. A batch that writes
``_Schema`` therefore derives its pages with the previous registry, and the background job that
the design gives the registry state re-derives them afterwards.

The owners' builders do the deriving (``lexstore.page_rows``, ``epistemic_graph.page_graph_rows``,
``memory_refs.identity_row``); this module only says which bytes, which resolver and which pages.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Connection
from typing import TYPE_CHECKING

from .. import (
    epistemic_graph,
    find_corpus,
    lexstore,
    memory_refs,
    note_types,
    semantic_index,
    vault,
)
from ..find_types import ParsedPage
from ..semantic_index import SemanticParentIndexState
from . import ownership

if TYPE_CHECKING:
    from .write import StoreChange

#: A page's parse and its parent state: what ``page_rows`` and ``page_graph_rows`` both take.
_Parsed = tuple[ParsedPage | None, SemanticParentIndexState | None]


@dataclass(frozen=True, slots=True)
class _GraphInputs:
    """What a commit's graph derivation holds fixed: the resolver after it, and the registries."""

    resolver: vault.WikilinkResolver
    stamps: tuple[tuple[str, str], ...]
    type_basis: note_types.Basis
    landing: Callable[[str, str], tuple[str, str, dict[str, str]]]


def apply(tx: Connection, root: Path, change: StoreChange, existing: frozenset[str]) -> None:
    """Derive the rows of ``change`` into ``tx``.

    ``existing`` is the set of the change's page paths that the store held before the commit, which
    tells a new page from an edited one.
    """
    _Derivation(tx, Path(root), change, existing).run()


class _Derivation:
    def __init__(
        self, tx: Connection, root: Path, change: StoreChange, existing: frozenset[str]
    ) -> None:
        self.tx, self.root, self.change, self.existing = tx, root, change, existing
        self.written = [*(put.rel for put in change.puts), *(move.dst for move in change.moves)]
        self.removed = [
            *(delete.rel for delete in change.deletes),
            *(move.src for move in change.moves),
        ]
        self.refreshed = list(change.refresh)

    # ------------------------------------------------------------------ sources

    def source(self, rel: str) -> tuple[bytes, int] | None:
        """The exact bytes and modification time of the page at ``rel``, or None when it has none.

        The store holds an owned page; the disk holds any other, as the store does not own it.
        """
        if ownership.classify(rel) is ownership.Ownership.PAGE:
            row = self.tx.execute(
                "SELECT body, mtime_ns FROM store_pages WHERE path = ?", (rel,)
            ).fetchone()
            return None if row is None else (bytes(row[0]), row[1])
        path = self.root / rel
        try:
            raw = vault.read_disk_bytes_without_pinning(path)
            return raw, os.stat(path).st_mtime_ns
        except OSError:
            return None

    def page_bytes(self, rel: str) -> bytes:
        found = self.source(rel)
        if found is None:
            raise FileNotFoundError(rel)
        return found[0]

    def target_exists(self, rel: str) -> bool:
        if ownership.classify(rel) is ownership.Ownership.PAGE:
            return (
                self.tx.execute("SELECT 1 FROM store_pages WHERE path = ?", (rel,)).fetchone()
                is not None
            )
        return (self.root / rel).exists()

    # ------------------------------------------------------------------ run

    def run(self) -> None:
        before = self._resolver_entries()
        derived = self._write_lexical_and_identity()
        after_entries = {
            rel: title for rel, (title, in_resolver) in derived.items() if in_resolver
        }
        resolver = vault.WikilinkResolver.from_entries(self.root, before.items())
        before_resolver = resolver.fork()
        resolver.on_entries_changed(
            after_entries.items(),
            [*self.removed, *(rel for rel in derived if rel not in after_entries)],
        )
        graph = _GraphInputs(
            resolver=resolver,
            stamps=epistemic_graph.graph_registry_stamps(self.root),
            type_basis=epistemic_graph.producer_type_basis(self.root),
            landing=epistemic_graph.landing_from_bytes(self.root, self.page_bytes),
        )
        for rel in self.removed:
            epistemic_graph.drop_page_graph_rows(self.tx, rel)
        for rel in derived:
            self._write_graph(rel, graph)
        for rel in self._dependents(before, after_entries, before_resolver, resolver):
            self._write_graph(rel, graph)

    def _resolver_entries(self) -> dict[str, str | None]:
        """The ``(path, title)`` of every page that the catalogue holds in the vault scope.

        These are the rows that file mode builds its graph resolver from, read before this commit
        changes any of them.
        """
        return dict(self.tx.execute("SELECT path, title FROM pages WHERE in_vault = 1").fetchall())

    # ------------------------------------------------------------------ lexical and identity rows

    def _write_lexical_and_identity(self) -> dict[str, tuple[str | None, bool]]:
        """Replace the lexical and identity rows of each page that the commit gives bytes to.

        Returns each such page's title and whether the resolver holds it, which is the resolver's
        entry for the page after the commit. A page that has no bytes any more loses its rows.
        """
        for rel in self.removed:
            lexstore.delete_page_rows(self.tx, rel)
            memory_refs.replace_identity_row(self.tx, rel, None)
        derived: dict[str, tuple[str | None, bool]] = {}
        for rel in (*self.written, *self.refreshed):
            found = self.source(rel)
            lexstore.delete_page_rows(self.tx, rel)
            if found is None:
                memory_refs.replace_identity_row(self.tx, rel, None)
                derived[rel] = (None, False)
                continue
            raw, mtime_ns = found
            in_kb, in_vault = lexstore.page_membership(rel)
            rows = None
            if in_kb or in_vault:
                page, state = self.parse(rel, raw, mtime_ns)
                rows = lexstore.page_rows(
                    self.root, rel, raw, page=page, state=state, mtime_ns=mtime_ns,
                    in_kb=in_kb, in_vault=in_vault,
                )
            if rows is not None:
                lexstore.write_page_rows(self.tx, rows)
            identity = memory_refs.identity_row(self.root, rel, raw)
            memory_refs.replace_identity_row(self.tx, rel, identity)
            derived[rel] = (
                None if rows is None else rows.columns["title"],
                rows is not None and in_vault,
            )
        return derived

    def parse(self, rel: str, raw: bytes, mtime_ns: int) -> _Parsed:
        """The parsed page and its parent state, as file mode derives them from the same bytes."""
        page = find_corpus.parse_page(
            self.root / rel, mtime_ns / 1e9, self.root, content=raw, resolved_relative=rel
        )
        if page is None:
            return None, None
        try:
            state = semantic_index.current_parent_index_state(
                self.root, rel, source=raw.decode("utf-8")
            )
        except (OSError, UnicodeError, ValueError):
            # Lexical derivation keeps such a page without unit rows; the graph keeps none for it.
            state = None
        return page, state

    # ------------------------------------------------------------------ graph rows

    def _write_graph(self, rel: str, graph: _GraphInputs) -> None:
        """Replace the graph rows of ``rel`` with the ones its bytes derive, or drop them."""
        rows = None
        found = self.source(rel)
        # File mode builds the graph from the Knowledge Base walk alone: a refreshed page outside it
        # has lexical rows and no graph rows, or it would get a node under a path that has no file.
        if found is not None and lexstore.page_membership(rel)[0]:
            raw, mtime_ns = found
            page, state = self.parse(rel, raw, mtime_ns)
            if page is not None and state is not None:
                rows = epistemic_graph.page_graph_rows(
                    self.root,
                    rel,
                    raw,
                    page=page,
                    state=state,
                    resolver=graph.resolver,
                    type_basis=graph.type_basis,
                    target_exists=self.target_exists,
                    landing=graph.landing,
                )
        if rows is None:
            epistemic_graph.drop_page_graph_rows(self.tx, rel)
        else:
            epistemic_graph.write_page_graph_rows(self.tx, rows, graph.stamps)

    # ------------------------------------------------------------------ dependents

    def _dependents(
        self,
        before: dict[str, str | None],
        after: dict[str, str | None],
        before_resolver: vault.WikilinkResolver,
        after_resolver: vault.WikilinkResolver,
    ) -> list[str]:
        """The pages outside the commit whose links re-resolve, from the transaction's own tables.

        A page that appears, goes or changes its title can move any link, in either direction: the
        links that start resolving to it and the links that stop. Any other edit moves only the
        links that name a unit of the page with a ``#`` fragment, as file mode re-derives them.
        """
        moved: set[str] = set(self.removed)
        edited: set[str] = set()
        for rel in (*self.written, *self.refreshed):
            if rel not in self.existing and rel not in before:
                moved.add(rel)
            elif before_resolver.title_key_for_path(rel) != after_resolver.title_key_for_path(
                rel
            ) or (rel in before) != (rel in after):
                moved.add(rel)
            else:
                edited.add(rel)
        resolvers = (before_resolver, after_resolver)
        found = epistemic_graph.dependent_paths(self.tx, moved, resolvers)
        found |= epistemic_graph.dependent_paths(self.tx, edited, resolvers, fragments_only=True)
        # A page of the commit is derived already; the rest follow in path order, so a kill-free
        # replay of the same commit writes the same rows.
        return sorted(found - set(self.written) - set(self.refreshed) - set(self.removed))

