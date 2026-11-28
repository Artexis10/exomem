"""The derived rows of store-mode pages (OpenSpec make-vault-store-canonical D2).

``derive_paths`` runs on the store thread, in a drain transaction of its own after the commit that
queued the paths (``derivation.drain``). It writes the lexical, graph and identity rows of every page
that the store holds at a path, drops the rows of every path that it no longer holds, and re-derives
the graph rows of the pages whose links those paths re-resolve. It reads the transaction's own tables
for pages (and the disk for a page that the store does not own), so the rows always follow the bytes
that the store holds when the drain runs, and a kill leaves the intent to derive them again.

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
from copy import copy
from dataclasses import dataclass, field, replace
from pathlib import Path
from sqlite3 import Connection
from typing import NamedTuple

import yaml

from .. import (
    epistemic_graph,
    find_corpus,
    lexstore,
    memory_refs,
    note_types,
    recall_policy,
    semantic_index,
    semantic_units,
    vault,
)
from ..find_types import ParsedPage
from ..governance import page_admission
from ..semantic_index import SemanticParentIndexState
from . import dependency_pass, read, resident_catalogue

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
class _GraphPage:
    raw: bytes
    page: ParsedPage
    state: SemanticParentIndexState
    lexical: lexstore.PageRows | None
    dependencies: tuple[tuple[str, str, bool], ...]


class _FragmentLanding:
    """One drain's proof of current target summaries and their neutral landings."""

    def __init__(self, tx: Connection, root: Path, page_bytes: Callable[[str], bytes]) -> None:
        self.tx = tx
        self.root = root
        self.byte_landing = epistemic_graph.landing_from_bytes(root, page_bytes)
        self.structures: dict[str, semantic_units.SelectedStructure | None] = {}
        self.landed: dict[tuple[str, str], tuple[str, str, dict[str, str]]] = {}

    def prime(self, paths: set[str]) -> None:
        paths = paths.difference(self.structures)
        if not paths:
            return
        self.structures.update(dict.fromkeys(paths))
        paths = recall_policy.admitted_among(self.root, paths)
        if not paths:
            return
        rows = self.tx.execute(
            "SELECT stored.path, stored.sha256, pages.structure_json FROM store_pages AS stored "
            "JOIN pages ON pages.path = stored.path WHERE pages.structure_json IS NOT NULL "
            "AND stored.path IN (SELECT value FROM json_each(?))",
            (json.dumps(sorted(paths)),),
        )
        for path, digest, raw_summary in rows:
            try:
                summary = json.loads(raw_summary)
                if (
                    summary["parent_source_hash"] == digest
                    and summary["parser_version"] == semantic_index.PARSER_VERSION
                    and summary["structural_complete"] is True
                ):
                    self.structures[path] = semantic_index.interpret_neutral_summary(path, summary)
            except (AttributeError, IndexError, KeyError, TypeError, ValueError, yaml.YAMLError):
                continue  # An older or incomplete summary still has its committed source.

    def __call__(self, canonical: str, fragment: str) -> tuple[str, str, dict[str, str]]:
        key = canonical, fragment
        answer = self.landed.get(key)
        if answer is None:
            path = epistemic_graph._page_rel(canonical)
            structure = self.structures.get(path)
            if structure is not None and fragment:
                answer = epistemic_graph.neutral_summary_landing(path, fragment, structure)
            if answer is None:
                answer = self.byte_landing(canonical, fragment)
            self.landed[key] = answer
        return answer


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
    pending_dependencies: frozenset[str] = frozenset()


def derive_paths(
    tx: Connection,
    root: Path,
    *,
    written: list[str],
    removed: list[str],
    refreshed: list[str],
    before: tuple | None = None,
    prepared_lexical: Mapping[str, _LexicalPage] | None = None,
    reserve_graph: ReserveGraph | None = None,
    metered: frozenset[str] = frozenset(),
    dependency_revisions: Mapping[str, int] | None = None,
    dependency_limit: int = 32,
) -> ResolverDelta:
    """Derive the rows of the paths that a drain batch names into ``tx``, and return its resolver delta.

    ``written`` are the paths that the store holds a page at, ``removed`` the ones that it does not
    any more, ``refreshed`` the files that it does not own. ``before`` is the vault triple that the
    batch starts at; a caller that gives it derives from the catalogue resolver that the last batch
    left and carries the new one out (``resident_catalogue.remember``).

    ``reserve_graph`` charges the graph rows of the ``metered`` paths (an original, and the source a
    move of it vacates) and of the pages outside the batch that re-derive because they link to them,
    directly or through a prior path that the original's lineage claims. Other pages of the batch draw
    on no room.

    The derived catalogue tells a new page from an edited one: no path counts as held before, so a
    page that the catalogue does not hold re-resolves every link that may name it.
    """
    return _Derivation(
        tx,
        Path(root),
        written=written,
        removed=removed,
        refreshed=refreshed,
        existing=frozenset(),
        before=before,
        prepared_lexical=prepared_lexical,
        reserve_graph=reserve_graph,
        metered=metered,
        dependency_revisions=dependency_revisions,
        dependency_limit=dependency_limit,
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


def resume_dependencies(
    tx: Connection, root: Path, before: tuple, cursor: dependency_pass.DependencyPass, *, limit: int,
) -> bool:
    return _Derivation(
        tx, root, written=[], removed=[], refreshed=[], existing=frozenset(),
        before=before, dependency_limit=limit,
    ).resume_dependencies(cursor)


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
        metered: frozenset[str] = frozenset(),
        dependency_revisions: Mapping[str, int] | None = None,
        dependency_limit: int = 32,
    ) -> None:
        self.tx, self.root, self.existing = tx, root, existing
        self.before_triple = before
        self.prepared_lexical = dict(prepared_lexical or {})
        self.reserve_graph = reserve_graph
        self.metered = metered if reserve_graph is not None else frozenset()
        self.written, self.removed, self.refreshed = written, removed, refreshed
        self.dependency_revisions = dependency_revisions or {}
        self.dependency_limit = dependency_limit
        self.pending_dependencies: set[str] = set()
        # A store that an older build set up has no link tables until the registry pass creates
        # them and derives every page's rows (``rederive``), so a commit before then writes none.
        # Released owner1 stores stay writable before the owner2 job creates its optional projections.
        self.projections = frozenset(name for (name,) in tx.execute("SELECT name FROM sqlite_master WHERE type = 'table'"))
        self.links = "inbound_links" in self.projections
        self.unit_links = "graph_unit_links" in self.projections
        self.stamp_unit_links = self.unit_links and epistemic_graph.unit_links_generation_current(tx)
        # Both columns arrive together in the owner pass's DDL; a store lacking either writes neither.
        self.graph_derived_columns = all(
            any(row[1] == column for row in tx.execute(f"PRAGMA table_info({table})"))
            for table, column in (("graph_dependencies", "body_link"), ("graph_edges", "raw_target"))
        )

        self.admission = tx.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'page_admission'"
        ).fetchone() is not None
        # Likewise the prior-path index: the registry pass creates it and derives every page's rows.
        self.priors = tx.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'prior_paths'"
        ).fetchone() is not None
        self._relayout: object | None = None
        #: The prior paths whose claimants this commit changes; links naming them re-resolve.
        self.prior_names: dict[str, set[str]] = {}
        self.prior_changed: set[str] = set()
        # The prior paths that a metered page claims or gives up: links that name one re-resolve to it.
        # One basis for the commit, so that the registry loads once and not once per page parse.
        self.type_basis = epistemic_graph.producer_type_basis(root)
        self.fragment_landing = _FragmentLanding(tx, root, self.page_bytes)

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
            after = {row[0] for row in rows}
            if before or after:
                self.prior_names[rel] = before | after
            if before != after:
                self.prior_changed.add(rel)

    # ------------------------------------------------------------------ sources

    def source(self, rel: str) -> tuple[bytes, int] | None:
        """The exact bytes and modification time of the page at ``rel`` (``read.page_source``)."""
        return read.page_source(self.tx, self.root, rel)

    def page_bytes(self, rel: str) -> bytes:
        if not self.target_exists(rel):
            raise FileNotFoundError(rel)
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
            landing=self.fragment_landing,
        )
        for rel in self.removed:
            epistemic_graph.drop_page_graph_rows(self.tx, rel, unit_links=self.unit_links)
        self._write_graph_batch(list(derived), graph, charge=self.metered)
        dependents, linking = self._dependents(before, after_entries, before_resolver, resolver)
        self._write_graph_batch(dependents, graph, charge=linking, require_live=True)
        if self.stamp_unit_links:
            epistemic_graph.stamp_unit_links_generation(self.tx)
        return replace(delta, pending_dependencies=frozenset(self.pending_dependencies))

    def resume_dependencies(self, cursor: dependency_pass.DependencyPass) -> bool:
        """Re-derive one bounded batch of linkers and advance the cursor in this transaction."""
        held = resident_catalogue.held(self.root, self.before_triple)
        # The continuation only binds claims and never edits the shared name maps.
        resolver = (
            copy(held[1]) if held is not None else
            vault.WikilinkResolver.from_entries(self.root, self._resolver_entries().items())
        )
        if self.priors:
            from .. import prior_claims

            resolver.bound_claims = prior_claims.from_connection(self.root, self.tx)
        inputs = _GraphInputs(
            resolver, epistemic_graph.graph_registry_stamps(self.root), self.type_basis, self.fragment_landing,
        )
        paths, following = cursor.batch(self.tx, self.dependency_limit)
        self._write_graph_batch(sorted(set(paths) - {cursor.path}), inputs, require_live=True)
        if self.stamp_unit_links:
            epistemic_graph.stamp_unit_links_generation(self.tx)
        if following is not None:
            dependency_pass.save(self.tx, following)
            return False
        return True

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
            lexstore.delete_page_rows(self.tx, rel, projections=self.projections)
            memory_refs.replace_identity_row(self.tx, rel, None)
            self._replace_priors(rel, None)
            if self.admission:
                page_admission.replace_row(self.tx, rel, None)
        derived: dict[str, tuple[str | None, bool]] = {}
        for rel in (*self.written, *self.refreshed):
            found = self.source(rel)
            lexstore.delete_page_rows(self.tx, rel, projections=self.projections)
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
                self.prepared_lexical[rel] = prepared
            page, rows = prepared.page, prepared.rows
            if rows is not None:
                lexstore.write_page_rows(self.tx, rows, projections=self.projections)
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

    def _graph_page(self, rel: str) -> _GraphPage | None:
        # File mode builds the graph from the Knowledge Base walk alone: a refreshed page outside it
        # has lexical rows and no graph rows, or it would get a node under a path that has no file.
        if not lexstore.page_membership(rel)[0] or (found := self.source(rel)) is None:
            return None
        raw, mtime_ns = found
        prepared = self.prepared_lexical.get(rel)
        if prepared is None:
            page, state = self.parse(rel, raw, mtime_ns)
            lexical = None
        else:
            page, state, lexical = prepared
        if page is None or state is None:
            return None
        return _GraphPage(raw, page, state, lexical, epistemic_graph.graph_dependencies(state))

    def _write_graph_batch(
        self, paths: list[str], graph: _GraphInputs, *,
        charge: set[str] | frozenset[str] = frozenset(),
        require_live: bool = False,
    ) -> None:
        for start in range(0, len(paths), self.dependency_limit):
            batch = paths[start:start + self.dependency_limit]
            if require_live:
                # A committed deletion can leave projected source bytes until its turn.
                owned = read.owned_as_pages(self.root, batch)
                held = {rel for (rel,) in self.tx.execute(
                    "SELECT path FROM store_pages WHERE path IN (SELECT value FROM json_each(?))",
                    (json.dumps([rel for rel in batch if owned[rel]]),),
                )}
                batch = [rel for rel in batch if not owned[rel] or rel in held]
            pages = [(rel, self._graph_page(rel)) for rel in batch]
            targets = epistemic_graph.fragment_dependency_paths(
                self.root, graph.resolver,
                (dependency for _, page in pages if page is not None for dependency in page.dependencies),
            )
            self.fragment_landing.prime(targets)
            for rel, page in pages:
                self._write_graph(rel, page, graph, charge=rel in charge)

    def _write_graph(
        self, rel: str, page: _GraphPage | None, graph: _GraphInputs, *, charge: bool,
    ) -> None:
        """Replace the graph rows of ``rel`` with the ones its bytes derive, or drop them."""
        rows = None
        if page is not None:
            lexical = page.lexical
            rows = epistemic_graph.page_graph_rows(
                self.root, rel, page.raw, page=page.page, state=page.state,
                resolver=graph.resolver, type_basis=graph.type_basis,
                target_exists=self.target_exists, landing=graph.landing,
                prepared_structure=(
                    json.loads(lexical.structure_json) if lexical and lexical.structure_json else None
                ),
                prepared_unit_rows=lexical.unit_rows if lexical else None,
                prepared_dependencies=page.dependencies, unit_links=self.unit_links,
            )
        if rows is None:
            if charge and self.reserve_graph is not None:
                self.reserve_graph(rel, ())  # the page's own rows go; their leaves draw on the reservation
            epistemic_graph.drop_page_graph_rows(self.tx, rel, unit_links=self.unit_links)
        else:
            epistemic_graph.write_page_graph_rows(
                self.tx, rows, graph.stamps, derived_columns=self.graph_derived_columns,
                unit_links=self.unit_links,
                # Pages that link to an original re-derive in the same transaction, so their
                # graph rows draw on the same reservation as the original's own.
                before_write=self.reserve_graph if charge else None,
            )

    # ------------------------------------------------------------------ dependents

    def _paths_and_priors(self, paths: set[str]) -> set[str]:
        return paths | {prior for path in paths for prior in self.prior_names.get(path, ())}

    def _dependents(
        self,
        before: dict[str, str | None],
        after: dict[str, str | None],
        before_resolver: vault.WikilinkResolver,
        after_resolver: vault.WikilinkResolver,
    ) -> tuple[list[str], set[str]]:
        """The pages outside the commit whose links re-resolve, from the transaction's own tables.

        A page that appears, goes or changes its title can move any link, in either direction: the
        links that start resolving to it and the links that stop. Any other edit moves only the
        links that name a unit of the page with a ``#`` fragment, as file mode re-derives them.
        Also returns the ones among them that re-resolve because of a metered path.
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
        edited -= moved
        resolvers = (before_resolver, after_resolver)
        chunked = ((edited | moved) & self.dependency_revisions.keys()) - self.metered
        if "graph_dependency_pass" not in self.projections:
            chunked.clear()
        found = epistemic_graph.dependent_paths(
            self.tx, self._paths_and_priors(moved - chunked), resolvers,
        )
        found |= epistemic_graph.dependent_paths(
            self.tx, self._paths_and_priors(edited - chunked), resolvers, fragments_only=True,
        )
        previous = dependency_pass.for_paths(self.tx, sorted(chunked))
        covered = set(self.written) | set(self.refreshed) | set(self.removed)
        scheduled = covered | found
        assigned: set[tuple[str, bool]] = set()
        remaining = self.dependency_limit
        for target in sorted(chunked, key=lambda path: (self.dependency_revisions[path], path)):
            keys = epistemic_graph._dependency_changed_keys(
                self._paths_and_priors({target}), *resolvers,
            )
            fragments_only = target not in moved
            if older := previous.get(target):
                # A newer edit keeps the dropped names the earlier receipt still has to re-land.
                keys.update(older.keys)
                fragments_only = fragments_only and older.fragments_only
            # One receipt owns each shared lookup key for the transaction. The oldest receipt
            # keeps reads of this revision waiting even when later pages share its unfinished key.
            unassigned = tuple(key for key in sorted(keys) if (key, fragments_only) not in assigned)
            assigned.update((key, fragments_only) for key in unassigned)
            cursor = dependency_pass.DependencyPass(
                target, self.dependency_revisions[target], unassigned, fragments_only,
            )
            # A folder restore already derived its own links against all restored pages.
            # Spend the continuation budget only on sources that still need to run.
            paths, following = cursor.batch(self.tx, remaining, covered=scheduled)
            remaining -= len(paths)
            found.update(paths)
            scheduled.update(paths)
            if following is not None:
                dependency_pass.save(self.tx, following)
                self.pending_dependencies.add(target)
        linking: set[str] = set()
        if self.metered:
            # Only the pages that link to an original re-derive on its account; the dependents of the
            # batch's other pages draw on no room, as in a batch that carries no original.
            linking = epistemic_graph.dependent_paths(
                self.tx, self._paths_and_priors(moved & self.metered), resolvers,
            )
            linking |= epistemic_graph.dependent_paths(
                self.tx, self._paths_and_priors(edited & self.metered), resolvers, fragments_only=True,
            )
        # A page of the commit is derived already; the rest follow in path order, so a kill-free
        # replay of the same commit writes the same rows.
        return sorted(found - covered), linking

