"""A persisted, read-through store of per-page `entity_recurrence` evidence.

An explicit `entity_recurrence` sweep used to read and parse every Knowledge Base
page, although most pages have not changed since the last sweep. This module keeps
what a sweep learns from one page (`entity_recurrence.PageEvidence`) in a derived
SQLite file beside the vault's other machine-local state, keyed by the page's file
identity. A sweep walks the Knowledge Base by file metadata, reads again only the
pages whose identity changed, and runs the cross-page half of the sweep
(`entity_recurrence.aggregate`) over the result.

The store is read through and exact at call time:

* Writers do not maintain it. An edit made by any program counts on the next sweep.
* The audience's egress checks run on every walked page before its row is read, and
  only the pages that audience may see reach aggregation. Governance verdicts are
  never stored.
* A page that is not indexable, or whose path is raw-protected, stores no content. It
  is read live on every sweep, as is a page whose status label only its own registry
  instance can classify.
* Any store fault makes the caller run the full sweep for that call.

Dropping the file changes cost only, never an answer.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from . import access, call_spans, entity_recurrence, find_corpus, freshness, state_paths
from .entity_recurrence import (
    CueGrammar,
    EvidenceContext,
    PageEvidence,
    Wikilink,
)
from .entity_types import EntityTypeRegistry
from .kbdir import kb_prefix
from .vault import WikilinkResolver, kb_root

log = logging.getLogger(__name__)

SCHEMA_VERSION = "1"
STORE_NAME = ".entity-evidence.sqlite"
#: A file whose timestamps fall this close to the moment a row was verified may be
#: rewritten later without moving them (coarse timestamps), so its row is re-hashed.
RACY_WINDOW_NS = 2_000_000_000
BUSY_TIMEOUT_SECONDS = 1.0

_STORE_DISABLED: ContextVar[bool] = ContextVar("exomem_entity_evidence_disabled", default=False)


class StoreFault(Exception):
    """The store cannot answer this call; the caller runs the full sweep instead."""


@contextlib.contextmanager
def full_sweep() -> Iterator[None]:
    """Make `audit.audit` read every page for the duration: the oracle for the store."""
    token = _STORE_DISABLED.set(True)
    try:
        yield
    finally:
        _STORE_DISABLED.reset(token)


def enabled() -> bool:
    return not _STORE_DISABLED.get()


def store_path(vault_root: Path) -> Path:
    return state_paths.vault_state_dir(vault_root) / STORE_NAME


def reset(vault_root: Path) -> None:
    """Drop the store; the next sweep rebuilds it."""
    base = store_path(vault_root)
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(OSError):
            Path(f"{base}{suffix}").unlink()


def _ctime_is_change_time() -> bool:
    """Whether `st_ctime` moves when a file's content or metadata changes.

    On native Windows it is the creation time and never moves, so a row there is
    verified by content hash on every sweep.
    """
    return os.name != "nt"


def _meta(vault_root: Path) -> dict[str, str]:
    from . import __version__

    return {
        "schema": SCHEMA_VERSION,
        "package": __version__,
        "grammar": entity_recurrence.GRAMMAR_VERSION,
        "predicates": entity_recurrence.PREDICATE_TABLE_DIGEST,
        "vault": str(Path(vault_root).resolve()),
        "kb_prefix": kb_prefix(),
    }


# ---------------------------------------------------------------- storage

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pages (
    path TEXT PRIMARY KEY,
    mtime_ns INTEGER NOT NULL,
    ctime_ns INTEGER NOT NULL,
    size INTEGER NOT NULL,
    dev INTEGER NOT NULL,
    ino INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    verified_at_ns INTEGER NOT NULL,
    state TEXT NOT NULL,
    cue_key TEXT NOT NULL,
    head TEXT,
    facts TEXT
);
"""

_PARSED = "parsed"
_UNPARSEABLE = "unparseable"

Identity = tuple[int, int, int, int, int]


def _identity(info: os.stat_result) -> Identity:
    mtime_ns, ctime_ns, size = freshness.signature_from_stat(info)
    return (mtime_ns, ctime_ns, size, info.st_dev, info.st_ino)


@dataclass(frozen=True, slots=True)
class _Row:
    path: str
    identity: Identity
    sha256: str
    verified_at_ns: int
    state: str
    cue_key: str
    head: str | None

    @property
    def racy(self) -> bool:
        return max(self.identity[0], self.identity[1]) >= self.verified_at_ns - RACY_WINDOW_NS


def _connect(vault_root: Path, meta: Mapping[str, str]) -> sqlite3.Connection:
    """Open the store, rebuilding it in place when its metadata is not this build's."""
    state_paths.ensure_vault_state_dir(vault_root)
    path = store_path(vault_root)
    try:
        return _open(path, meta)
    except sqlite3.DatabaseError as error:
        if isinstance(error, sqlite3.OperationalError) and "locked" in str(error):
            raise
        # Corruption: unlink and recreate once.
        reset(vault_root)
        return _open(path, meta)


def _open(path: Path, meta: Mapping[str, str]) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        if dict(conn.execute("SELECT key, value FROM meta")) != dict(meta):
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM pages")
            conn.execute("DELETE FROM meta")
            conn.executemany("INSERT INTO meta VALUES (?, ?)", sorted(meta.items()))
            conn.execute("COMMIT")
        return conn
    except BaseException:
        conn.close()
        raise


def _load_rows(conn: sqlite3.Connection) -> dict[str, _Row]:
    return {
        path: _Row(path, (mtime, ctime, size, dev, ino), sha, verified, state, cue_key, head)
        for path, mtime, ctime, size, dev, ino, sha, verified, state, cue_key, head in conn.execute(
            "SELECT path, mtime_ns, ctime_ns, size, dev, ino, sha256, verified_at_ns,"
            " state, cue_key, head FROM pages"
        )
    }


# ---------------------------------------------------------------- evidence <-> JSON


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _encode(evidence: PageEvidence) -> tuple[str, str]:
    head = {
        "title": evidence.title,
        "status": evidence.status,
        "registry": evidence.registry_facts,
    }
    facts = {
        "links": [[link.target, link.name, link.suffix] for link in evidence.links],
        "sources": list(evidence.source_refs),
        "origin": evidence.origin_ref,
        "relations": list(evidence.relation_keys),
        "excerpts": {text: list(keys) for text, keys in evidence.excerpt_keys.items()},
        "clauses": list(evidence.relation_clauses),
        "frames": [
            [
                frame.identity,
                frame.display,
                frame.frame_type,
                frame.predicate_id,
                frame.cue,
                frame.resolved_anchor,
                list(frame.type_cues),
                list(frame.family_cues),
                frame.clause_skeleton,
                frame.facet_hash,
                frame.context_hash,
                frame.excerpt,
            ]
            for frame in evidence.frames
        ],
    }
    return _dumps(head), _dumps(facts)


def _decode_head(path: str, head_json: str) -> PageEvidence:
    head = json.loads(head_json)
    return PageEvidence(
        path=path, title=head["title"], status=head["status"], registry_facts=head["registry"]
    )


def _decode_facts(head: PageEvidence, facts_json: str) -> PageEvidence:
    facts = json.loads(facts_json)
    return replace(
        head,
        links=tuple(Wikilink(*link) for link in facts["links"]),
        source_refs=tuple(facts["sources"]),
        origin_ref=facts["origin"],
        relation_keys=tuple(facts["relations"]),
        excerpt_keys={text: tuple(keys) for text, keys in facts["excerpts"].items()},
        relation_clauses=tuple(facts["clauses"]),
        frames=tuple(
            EvidenceContext(
                identity=identity,
                display=display,
                path=head.path,
                origin="",
                frame_type=frame_type,
                predicate_id=predicate_id,
                cue=cue,
                resolved_anchor=resolved_anchor,
                type_cues=tuple(type_cues),
                family_cues=tuple(family_cues),
                clause_skeleton=skeleton,
                facet_hash=facet_hash,
                context_hash=context_hash,
                excerpt=excerpt,
                registry_fingerprint="",
            )
            for (
                identity,
                display,
                frame_type,
                predicate_id,
                cue,
                resolved_anchor,
                type_cues,
                family_cues,
                skeleton,
                facet_hash,
                context_hash,
                excerpt,
            ) in facts["frames"]
        ),
    )


# ---------------------------------------------------------------- the sweep


@dataclass(slots=True)
class _Visible:
    """One page this audience may see: its evidence, and the page when read this call."""

    rel: str
    indexable: bool
    #: Excluded or raw-protected: never takes a row, read live on every sweep.
    path_volatile: bool
    path: Path
    info: os.stat_result
    row: _Row | None
    evidence: PageEvidence | None = None
    #: The parsed page when this call read it; eligibility reads its frontmatter.
    page: Any = None
    #: Facts still to load from the stored row.
    facts_pending: bool = False


def sweep(
    vault_root: Path,
    *,
    entity_types: EntityTypeRegistry,
    status_basis: Any,
    make_resolver: Callable[[Iterable[tuple[str, str]]], WikilinkResolver],
    attachment_probe: Callable[[str], bool],
) -> list[entity_recurrence.Candidate]:
    """`entity_recurrence.collect` over the pages this audience may see, from the store.

    `make_resolver` receives each visible page's `(path, title)` in walk order.
    Raises `StoreFault` when the store cannot answer; the caller then runs the full
    sweep and calls `reset`.
    """
    cues = entity_recurrence.cue_grammar(entity_types)
    try:
        conn = _connect(vault_root, _meta(vault_root))
    except (sqlite3.Error, OSError) as error:
        raise StoreFault(str(error)) from error
    try:
        try:
            rows = _load_rows(conn)
        except sqlite3.Error as error:
            raise StoreFault(str(error)) from error
        visible, complete, seen = _walk(vault_root, rows)
        _refresh(vault_root, conn, cues, rows, visible, complete, seen)
        return _aggregate(
            vault_root,
            conn,
            entity_types=entity_types,
            cues=cues,
            status_basis=status_basis,
            visible=visible,
            make_resolver=make_resolver,
            attachment_probe=attachment_probe,
        )
    finally:
        conn.close()


def _walk(
    vault_root: Path, rows: Mapping[str, _Row]
) -> tuple[list[_Visible], bool, set[str]]:
    """Walk the Knowledge Base by file metadata and ask egress before any row is read.

    Returns the visible pages in walk order, whether the walk reported no error, and
    every walked path, visible or not, so a row is dropped only when its file is gone.
    """
    from .governance import egress, raw_protection
    from .governance.principal import effective_principal

    who = effective_principal()
    admitted = egress.restricted_release_filter(vault_root)
    errors: list[OSError] = []
    visible: list[_Visible] = []
    seen: set[str] = set()
    with call_spans.span("entity_evidence.walk", {"pages_walked": 0}) as fields:
        for path, info in find_corpus.walk_md_with_stat(kb_root(vault_root), on_error=errors.append):
            rel = path.relative_to(vault_root).as_posix()
            seen.add(rel)
            # The same two checks, in the same order, as the full sweep's page walk.
            if not egress.content_permits(vault_root, rel, who):
                continue
            if admitted is not None and not admitted(rel):
                continue
            indexable = access.is_indexable(vault_root, rel)
            # A raw-protected or excluded path stores no content, so it never takes a row.
            path_volatile = raw_protection.marked(rel) or not indexable
            row = None if path_volatile else rows.get(rel)
            visible.append(_Visible(rel, indexable, path_volatile, path, info, row))
            fields["pages_walked"] += 1
    return visible, not errors, seen


def _refresh(
    vault_root: Path,
    conn: sqlite3.Connection,
    cues: CueGrammar,
    rows: Mapping[str, _Row],
    visible: list[_Visible],
    complete: bool,
    seen: set[str],
) -> None:
    """Read again the pages whose row cannot be trusted, then persist what changed."""
    writes: list[tuple[Any, ...]] = []
    touches: list[tuple[Any, ...]] = []
    scrubs: list[str] = []
    trust_ctime = _ctime_is_change_time()
    with call_spans.span(
        "entity_evidence.refresh",
        {"pages_reextracted": 0, "pages_volatile": 0, "pages_verified": 0},
    ) as fields:
        for entry in visible:
            row, identity = entry.row, _identity(entry.info)
            cue_key = _cue_key(entry.rel, cues)
            matches = row is not None and row.cue_key == cue_key and row.identity == identity
            if row is not None and matches and trust_ctime and not row.racy:
                _use_row(entry, row)
                continue
            if matches:
                fields["pages_verified"] += 1
            # Taken before the read: a later rewrite then cannot hide inside the racy window.
            verified_at = time.time_ns()
            content = find_corpus._read_page_bytes(entry.path, vault_root)
            if content is None:
                # Unreadable now: absent from this sweep, exactly as in the full sweep.
                if entry.rel in rows and entry.path_volatile:
                    scrubs.append(entry.rel)
                continue
            digest = hashlib.sha256(content).hexdigest()
            if row is not None and row.sha256 == digest and row.cue_key == cue_key:
                # The same bytes under a new or racy identity: keep the facts.
                _use_row(entry, row)
                touches.append((*identity, verified_at, entry.rel))
                continue
            page = find_corpus.parse_page(
                entry.path, entry.info.st_mtime, vault_root, content=content
            )
            if page is None:
                # Bytes read but not decodable; the row only spares reading them again.
                fields["pages_reextracted"] += 1
                if not entry.path_volatile and _unchanged(entry):
                    writes.append(_row(entry, digest, verified_at, _UNPARSEABLE, cue_key, None, None))
                continue
            evidence = entity_recurrence.page_evidence(page, cues=cues)
            entry.evidence, entry.page = evidence, page
            if _volatile(entry, evidence):
                fields["pages_volatile"] += 1
                if entry.rel in rows:
                    scrubs.append(entry.rel)
                continue
            fields["pages_reextracted"] += 1
            if _unchanged(entry):
                head, facts = _encode(evidence)
                writes.append(_row(entry, digest, verified_at, _PARSED, cue_key, head, facts))
        _persist(conn, writes, touches, scrubs, rows, seen, complete)


def _unchanged(entry: _Visible) -> bool:
    """Whether the file still has the identity the walk saw, after it was read."""
    try:
        return _identity(entry.path.lstat()) == _identity(entry.info)
    except OSError:
        return False


def _cue_key(rel: str, cues: CueGrammar) -> str:
    """The cue digest a row's frames were extracted under; an Entities page has no frames."""
    return "" if rel.startswith(entity_recurrence.entities_prefix()) else cues.digest


def _volatile(entry: _Visible, evidence: PageEvidence) -> bool:
    """Whether this page's evidence must be re-read every sweep instead of stored."""
    from . import lifecycle_statuses

    return (
        entry.path_volatile
        # Only the page's own registry instance can classify a non-canonical label.
        or lifecycle_statuses.canonical_class(evidence.status) is None
        # A row is keyed by the walked path, so the page must agree with it.
        or evidence.path != entry.rel
    )


def _use_row(entry: _Visible, row: _Row) -> None:
    if row.state == _PARSED and row.head is not None:
        entry.evidence = _decode_head(entry.rel, row.head)
        entry.facts_pending = not entry.rel.startswith(entity_recurrence.entities_prefix())


def _row(
    entry: _Visible,
    digest: str,
    verified_at: int,
    state: str,
    cue_key: str,
    head: str | None,
    facts: str | None,
) -> tuple[Any, ...]:
    return (entry.rel, *_identity(entry.info), digest, verified_at, state, cue_key, head, facts)


def _persist(
    conn: sqlite3.Connection,
    writes: list[tuple[Any, ...]],
    touches: list[tuple[Any, ...]],
    scrubs: list[str],
    rows: Mapping[str, _Row],
    seen: set[str],
    complete: bool,
) -> None:
    """One write transaction for the call. Its failure costs the next call, not this answer."""
    gone = sorted(set(rows) - seen) if complete else []
    if not (writes or touches or scrubs or gone):
        return
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany("INSERT OR REPLACE INTO pages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", writes)
        conn.executemany(
            "UPDATE pages SET mtime_ns=?, ctime_ns=?, size=?, dev=?, ino=?, verified_at_ns=?"
            " WHERE path=?",
            touches,
        )
        conn.executemany("DELETE FROM pages WHERE path=?", ((path,) for path in (*scrubs, *gone)))
        conn.execute("COMMIT")
    except sqlite3.Error as error:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        log.debug("entity evidence store write skipped: %s", error)


def _aggregate(
    vault_root: Path,
    conn: sqlite3.Connection,
    *,
    entity_types: EntityTypeRegistry,
    cues: CueGrammar,
    status_basis: Any,
    visible: list[_Visible],
    make_resolver: Callable[[Iterable[tuple[str, str]]], WikilinkResolver],
    attachment_probe: Callable[[str], bool],
) -> list[entity_recurrence.Candidate]:
    pages = [entry for entry in visible if entry.evidence is not None]
    if not pages:
        return []
    entities = entity_recurrence.entities_prefix()
    with call_spans.span("entity_evidence.aggregate", {"pages_aggregated": 0}) as fields:
        with call_spans.span("entity_evidence.resolver"):
            resolver = make_resolver((entry.rel, entry.evidence.title) for entry in pages)
        registry = entity_recurrence.registry_index(
            (
                SimpleNamespace(rel_path=entry.rel, frontmatter=entry.evidence.registry_facts)
                for entry in pages
                if entry.evidence.registry_facts is not None
            ),
            entity_types=entity_types,
        )
        eligible = [
            entry
            for entry in sorted(pages, key=lambda item: item.rel)
            if not entry.rel.startswith(entities)
            and entity_recurrence.counts_as_evidence(
                entry.page
                if entry.page is not None
                # A stored row holds a canonical label, which classifies without the rest.
                else SimpleNamespace(
                    rel_path=entry.rel, frontmatter={"status": entry.evidence.status}
                ),
                indexable=entry.indexable,
                status_basis=status_basis,
            )
        ]
        try:
            for entry in eligible:
                if entry.facts_pending:
                    # Only a page this audience may see is ever looked up.
                    (facts,) = conn.execute(
                        "SELECT facts FROM pages WHERE path = ?", (entry.rel,)
                    ).fetchone()
                    entry.evidence = _decode_facts(entry.evidence, facts)
        except (sqlite3.Error, TypeError) as error:
            raise StoreFault(str(error)) from error
        fields["pages_aggregated"] = len(eligible)
        return entity_recurrence.aggregate(
            [entry.evidence for entry in eligible],
            vault_root=vault_root,
            resolver=resolver,
            registry=registry,
            entity_types=entity_types,
            attachment_probe=attachment_probe,
            grammar=entity_recurrence.frame_grammar(entity_types, registry, cues=cues),
        )
