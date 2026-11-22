"""The derived-row half of one store-mode commit (OpenSpec make-vault-store-canonical D2).

``apply`` runs inside ``write.commit``'s transaction, after the page tables hold the commit's own
bytes. It writes the lexical, graph and identity rows of every page that the commit gives bytes to,
drops the rows of every page that it removes, and re-derives the graph rows of the pages whose
links the commit re-resolves. It reads the transaction's own tables for pages (and the disk for a
page that the store does not own), so a reader that sees the commit sees its rows, and a kill leaves
no row without its page.

The registries (traversal profiles, the type basis) and the access policy are the exception: the
owners' builders read them from disk, while the schema overlay under ``_Schema`` is store-owned. A
batch that writes the overlay therefore derives its pages with the previous registry, and the
background job in ``rederive.py`` settles the rows afterwards, one ``rederive_pages`` batch at a
time.

The owners' builders do the deriving (``lexstore.page_rows``, ``epistemic_graph.page_graph_rows``,
``memory_refs.identity_row``); this module only says which bytes, which resolver and which pages.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from sqlite3 import Connection
from typing import TYPE_CHECKING, NamedTuple

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
from ..governance import page_admission
from ..semantic_index import SemanticParentIndexState
from . import read, resident_catalogue

if TYPE_CHECKING:
    from .write import StoreChange

#: A page's parse and its parent state: what ``page_rows`` and ``page_graph_rows`` both take.
_Parsed = tuple[ParsedPage | None, SemanticParentIndexState | None]


class _LexicalPage(NamedTuple):
    page: ParsedPage | None
    state: SemanticParentIndexState | None
    rows: lexstore.PageRows | None


ReserveGraph = Callable[[str, tuple[epistemic_graph.PageGraphRecords, ...]], None]


def prepare_lexical_page(
    root: Path, rel: str, raw: bytes, mtime_ns: int, type_basis: note_types.Basis,
) -> _LexicalPage:
    """Prepare exactly the lexical rows the commit writes for an original."""
    in_kb, in_vault = lexstore.page_membership(rel)
    if not (in_kb or in_vault):
        return _LexicalPage(None, None, None)
    page, state = _parse(root, rel, raw, mtime_ns, type_basis)
    rows = lexstore.page_rows(
        root, rel, raw, page=page, state=state, mtime_ns=mtime_ns, in_kb=in_kb, in_vault=in_vault,
    )
    return _LexicalPage(page, state, rows)


def _parse(
    root: Path, rel: str, raw: bytes, mtime_ns: int, type_basis: note_types.Basis,
) -> _Parsed:
    page = find_corpus.parse_page(
        root / rel, mtime_ns / 1e9, root, content=raw, resolved_relative=rel,
    )
    if page is None:
        return None, None
    try:
        state = semantic_index.current_parent_index_state(
            root, rel, source=raw.decode("utf-8"), owner_type_basis=type_basis,
        )
    except (OSError, UnicodeError, ValueError):
        # Lexical derivation keeps such a page without unit rows; the graph keeps none for it.
        state = None
    return page, state


@dataclass(frozen=True, slots=True)
class _GraphInputs:
    """What a commit's graph derivation holds fixed: the resolver after it, and the registries."""

    resolver: vault.WikilinkResolver
    stamps: tuple[tuple[str, str], ...]
    type_basis: note_types.Basis
    landing: Callable[[str, str], tuple[str, str, dict[str, str]]]


@dataclass(frozen=True, slots=True)
class ResolverDelta:
    """How a commit changes the vault's resolver entries: each page that the resolver holds after it,
    with its title, and each page that it held or that the commit derived and that it does not hold."""

    entries: tuple[tuple[str, str | None], ...]
    deleted: tuple[str, ...]
    #: The catalogue's ``(path, title)`` rows and the resolver over them as the commit leaves them, for
    #: ``resident_catalogue.remember``. Set when the derivation was given the vault triple that it started from.
    catalogue: tuple[dict[str, str | None], vault.WikilinkResolver] | None = field(
        default=None, compare=False, repr=False
    )


def apply(
    tx: Connection,
    root: Path,
    change: StoreChange,
    existing: frozenset[str],
    *,
    before: tuple | None = None,
    prepared_lexical: Mapping[str, _LexicalPage] | None = None,
    reserve_graph: ReserveGraph | None = None,
) -> ResolverDelta:
    """Derive the rows of ``change`` into ``tx``, and return the commit's resolver delta.

    ``existing`` is the set of the change's page paths that the store held before the commit, which
    tells a new page from an edited one. ``before`` is the vault triple that the commit starts at; a
    caller that gives it lets the commit derive from the catalogue resolver that the last commit left
    and carry the new one out (``resident_catalogue.remember``).
    """
    return _Derivation(
        tx,
        Path(root),
        written=[*(put.rel for put in change.puts), *(move.dst for move in change.moves)],
        removed=[
            *(delete.rel for delete in change.deletes),
            *(move.src for move in change.moves),
        ],
        refreshed=list(change.refresh),
        existing=existing,
        before=before,
        prepared_lexical=prepared_lexical,
        reserve_graph=reserve_graph,
    ).run()


def rederive_pages(tx: Connection, root: Path, rels: list[str]) -> None:
    """Derive the rows of the stored pages at ``rels`` again from their own bytes.

    The registry job calls it once the registries and the access policy that the builders read have
    changed. Every page counts as one that the store already held, so a page whose title is the same
    re-resolves only the links that name one of its units, as an edit of its bytes would.
    """
    _Derivation(
        tx, Path(root), written=[], removed=[], refreshed=list(rels), existing=frozenset(rels)
    ).run()


class _Derivation:
    def __init__(
        self,
        tx: Connection,
        root: Path,
        *,
        written: list[str],
        removed: list[str],
        refreshed: list[str],
        existing: frozenset[str],
        before: tuple | None = None,
        prepared_lexical: Mapping[str, _LexicalPage] | None = None,
        reserve_graph: ReserveGraph | None = None,
    ) -> None:
        self.tx, self.root, self.existing = tx, root, existing
        self.before_triple = before
        self.prepared_lexical = prepared_lexical or {}
        self.reserve_graph = reserve_graph
        self.written, self.removed, self.refreshed = written, removed, refreshed
        # A store that an older build set up has no link tables until the registry pass creates
        # them and derives every page's rows (``rederive``), so a commit before then writes none.
        self.links = tx.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'inbound_links'"
        ).fetchone() is not None

        self.admission = tx.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'page_admission'"
        ).fetchone() is not None
        # Likewise the prior-path index: the registry pass creates it and derives every page's rows.
        self.priors = tx.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'prior_paths'"
        ).fetchone() is not None
        self._relayout: object | None = None
        #: The prior paths whose claimants this commit changes; links naming them re-resolve.
        self.prior_changed: set[str] = set()
        # One basis for the commit, so that the registry loads once and not once per page parse.
        self.type_basis = epistemic_graph.producer_type_basis(root)

    def relayout(self) -> object:
        """The relayout record as this transaction sees it, read once per commit."""
        if self._relayout is None:
            from .. import relayout_record

            def record_bytes(rel: str) -> bytes | None:
                found = self.source(rel)
                return None if found is None else found[0]

            self._relayout = relayout_record.load(self.root, read_bytes=record_bytes)
        return self._relayout

    def _replace_priors(self, rel: str, raw: bytes | None) -> None:
        if self.priors:
            before = {
                row[0]
                for row in self.tx.execute(
                    "SELECT prior_path FROM prior_paths WHERE path = ?", (rel,)
                ).fetchall()
            }
            rows = () if raw is None else memory_refs.prior_path_rows(rel, raw, self.relayout())
            memory_refs.replace_prior_rows(self.tx, rel, rows)
            self.prior_changed |= before ^ {row[0] for row in rows}

    # ------------------------------------------------------------------ sources

    def source(self, rel: str) -> tuple[bytes, int] | None:
        """The exact bytes and modification time of the page at ``rel`` (``read.page_source``)."""
        return read.page_source(self.tx, self.root, rel)

    def page_bytes(self, rel: str) -> bytes:
        found = self.source(rel)
        if found is None:
            raise FileNotFoundError(rel)
        return found[0]

    def target_exists(self, rel: str) -> bool:
        return read.link_target_exists(self.tx, self.root, rel)

    # ------------------------------------------------------------------ run

    def run(self) -> ResolverDelta:
        held = resident_catalogue.held(self.root, self.before_triple)
        # The rows and resolver that the catalogue held before this commit: kept ones are read here, never edited.
        before, before_resolver = held if held is not None else (self._resolver_entries(), None)
        derived = self._write_lexical_and_identity()
        if self.links:
            self._write_inbound()
        after_entries = {
            rel: title for rel, (title, in_resolver) in derived.items() if in_resolver
        }
        delta = ResolverDelta(
            tuple(after_entries.items()),
            (*self.removed, *(rel for rel in derived if rel not in after_entries)),
        )
        if before_resolver is None:
            before_resolver = vault.WikilinkResolver.from_entries(self.root, before.items())
        resolver = before_resolver.fork()
        resolver.on_entries_changed(delta.entries, delta.deleted)
        if self.before_triple is not None:
            after_rows = {**before, **dict(delta.entries)}
            for rel in delta.deleted:
                after_rows.pop(rel, None)
            # A fork, so that the claims bound below stay with this transaction's graph and not the resident.
            delta = replace(delta, catalogue=(after_rows, resolver))
            resolver = resolver.fork()
        if self.priors:
            from .. import prior_claims

            # The graph resolves prior paths by the claims this commit leaves, as a cold build does.
            resolver.bound_claims = prior_claims.from_connection(self.root, self.tx)
        graph = _GraphInputs(
            resolver=resolver,
            stamps=epistemic_graph.graph_registry_stamps(self.root),
            type_basis=self.type_basis,
            landing=epistemic_graph.landing_from_bytes(self.root, self.page_bytes),
        )
        for rel in self.removed:
            epistemic_graph.drop_page_graph_rows(self.tx, rel)
        for rel in derived:
            self._write_graph(rel, graph)
        for rel in self._dependents(before, after_entries, before_resolver, resolver):
            self._write_graph(rel, graph)
        return delta

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
            self._replace_priors(rel, None)
            if self.admission:
                page_admission.replace_row(self.tx, rel, None)
        derived: dict[str, tuple[str | None, bool]] = {}
        for rel in (*self.written, *self.refreshed):
            found = self.source(rel)
            lexstore.delete_page_rows(self.tx, rel)
            if found is None:
                memory_refs.replace_identity_row(self.tx, rel, None)
                self._replace_priors(rel, None)
                if self.admission:
                    page_admission.replace_row(self.tx, rel, None)
                derived[rel] = (None, False)
                continue
            raw, mtime_ns = found
            in_kb, in_vault = lexstore.page_membership(rel)
            prepared = self.prepared_lexical.get(rel)
            if prepared is None:
                prepared = prepare_lexical_page(
                    self.root, rel, raw, mtime_ns, self.type_basis,
                )
            page, rows = prepared.page, prepared.rows
            if rows is not None:
                lexstore.write_page_rows(self.tx, rows)
            if self.admission:
                if page is None and not (in_kb or in_vault) and rel.lower().endswith(".md"):
                    page = find_corpus.parse_page(
                        self.root / rel, mtime_ns / 1e9, self.root, content=raw, resolved_relative=rel,
                    )
                page_admission.replace_row(self.tx, rel, raw, page)
            identity = memory_refs.identity_row(self.root, rel, raw)
            memory_refs.replace_identity_row(self.tx, rel, identity)
            self._replace_priors(rel, raw if identity is not None else None)
            derived[rel] = (
                None if rows is None else rows.columns["title"],
                rows is not None and in_vault,
            )
        return derived

    def _write_inbound(self) -> None:
        """Replace the inbound-link rows of each page that the commit gives bytes to or removes.

        A page's link rows depend on its own bytes alone, so no dependent page is touched. The
        index holds every page that the vault walk reaches, inside the Knowledge Base or not.
        """
        for rel in self.removed:
            vault.replace_inbound_rows(self.tx, rel, None)
        for rel in (*self.written, *self.refreshed):
            found = self.source(rel) if read.walk_member(rel) else None
            vault.replace_inbound_rows(self.tx, rel, None if found is None else found[0])

    def parse(self, rel: str, raw: bytes, mtime_ns: int) -> _Parsed:
        """The parsed page and its parent state, as file mode derives them from the same bytes."""
        return _parse(self.root, rel, raw, mtime_ns, self.type_basis)

    # ------------------------------------------------------------------ graph rows

    def _write_graph(self, rel: str, graph: _GraphInputs) -> None:
        """Replace the graph rows of ``rel`` with the ones its bytes derive, or drop them."""
        rows = None
        prepared = self.prepared_lexical.get(rel)
        found = self.source(rel)
        # File mode builds the graph from the Knowledge Base walk alone: a refreshed page outside it
        # has lexical rows and no graph rows, or it would get a node under a path that has no file.
        if found is not None and lexstore.page_membership(rel)[0]:
            raw, mtime_ns = found
            if prepared is None:
                page, state = self.parse(rel, raw, mtime_ns)
                structure = None
            else:
                page, state = prepared.page, prepared.state
                summary = prepared.rows.structure_json if prepared.rows is not None else None
                structure = json.loads(summary) if summary is not None else None
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
                    prepared_structure=structure,
                )
        if rows is None:
            if self.reserve_graph is not None:
                self.reserve_graph(rel, ())  # the page's own rows go; their leaves draw on the reservation
            epistemic_graph.drop_page_graph_rows(self.tx, rel)
        else:
            epistemic_graph.write_page_graph_rows(
                self.tx, rows, graph.stamps,
                # Pages that link to an original re-derive in the same transaction, so their
                # graph rows draw on the same reservation as the original's own.
                before_write=self.reserve_graph,
            )

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
        # A prior path that gains or loses a claimant moves the links that name it.
        moved: set[str] = set(self.removed) | self.prior_changed
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

