"""A persisted, read-through store of per-page `entity_recurrence` evidence.

An explicit `entity_recurrence` sweep used to read and parse every Knowledge Base
page, although most pages have not changed since the last sweep. This module keeps
what a sweep learns from one page (`entity_recurrence.PageEvidence`) in a derived
SQLite file beside the vault's other machine-local state, keyed by the page's file
identity. A sweep walks the Knowledge Base by file metadata, reads again only the
pages whose identity changed, and runs the cross-page half of the sweep
(`entity_recurrence.collect_evidence`) over the result.

The store is read through and exact at call time:

* Writers do not maintain it. An edit made by any program counts on the next sweep.
* The audience's egress checks run on every walked page before its row is read. Only
  file-identity columns load first. After egress admits a page and its identity holds,
  one query bound to the verified row reads the page's head and facts. If a concurrent
  worker replaced that row, the page is read live and its evidence is used wholly.
  Only the pages that audience may see reach aggregation. Governance verdicts are
  never stored.
* A page that is not indexable, or whose path is raw-protected, stores no content. It
  is read live on every sweep.
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
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from . import access, call_spans, entity_recurrence, find_corpus, freshness, state_paths, vault
from .entity_recurrence import (
    CueGrammar,
    EvidenceContext,
    PageEvidence,
    Wikilink,
)
from .entity_types import EntityTypeRegistry
from .kbdir import kb_prefix
from .vault import (
    WikilinkResolver,
    kb_root,
    relative_posix,
    yaml_safe_dump_for_parse,
    yaml_safe_load,
)

log = logging.getLogger(__name__)

SCHEMA_VERSION = "1"
STORE_NAME = ".entity-evidence.sqlite"
#: A file whose timestamps fall this close to the moment a row was verified may be
#: rewritten later without moving them (coarse timestamps), so its row is re-hashed.
#: The same 2 s window as `record_item_cache`, `working_set_index`, `adoption_plan` and
#: `artifact_census`; none of them exports it.
RACY_WINDOW_NS = 2_000_000_000
BUSY_TIMEOUT_SECONDS = 1.0

#: What a stored row that does not decode raises: JSON text that is not JSON
#: (ValueError), a missing or mistyped field (KeyError, TypeError, AttributeError),
#: or YAML that does not parse.
_BAD_ROW = (ValueError, KeyError, TypeError, AttributeError, yaml.YAMLError)


class StoreFault(Exception):
    """The store cannot answer this call; the caller runs the full sweep instead.

    `reset` is True when the store itself is bad (corrupt, or a row that does not
    decode), and the caller then drops it. It is False when the store is only busy or
    unreachable, because another worker may be using a sound store.
    """

    def __init__(self, message: str, *, reset: bool) -> None:
        super().__init__(message)
        self.reset = reset


def store_path(vault_root: Path) -> Path:
    return state_paths.vault_state_dir(vault_root) / STORE_NAME


def reset(vault_root: Path) -> None:
    """Drop the store; the next sweep rebuilds it."""
    base = store_path(vault_root)
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(OSError):
            Path(f"{base}{suffix}").unlink()


def _is_busy(error: sqlite3.Error) -> bool:
    """Whether SQLite said only that another connection holds the file."""
    # `sqlite_errorcode` is the extended result code; its low byte is the primary code.
    code = getattr(error, "sqlite_errorcode", None)
    return code is not None and code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


def _fault(error: sqlite3.Error) -> StoreFault:
    return StoreFault(str(error), reset=not _is_busy(error))


def _bad_row(path: str, reason: str) -> StoreFault:
    return StoreFault(f"stored row of {path} does not decode: {reason}", reset=True)


def _meta(vault_root: Path) -> dict[str, str]:
    from . import __version__

    return {
        "schema": SCHEMA_VERSION,
        "package": __version__,
        "grammar": entity_recurrence.GRAMMAR_VERSION,
        "predicates": entity_recurrence.PREDICATE_TABLE_DIGEST,
        "vault": str(Path(vault_root).resolve()),
        "kb_prefix": kb_prefix(),
        # Identities are NFKC-casefolded; a new Unicode database can fold a name differently.
        "unicode": unicodedata.unidata_version,
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
    """The identity columns of one stored page; its content is read only after egress."""

    path: str
    identity: Identity
    sha256: str
    verified_at_ns: int
    state: str
    cue_key: str

    @property
    def racy(self) -> bool:
        return max(self.identity[0], self.identity[1]) >= self.verified_at_ns - RACY_WINDOW_NS


def _connect(vault_root: Path, meta: Mapping[str, str]) -> sqlite3.Connection:
    """Open the store, rebuilding it in place when its metadata is not this build's."""
    state_paths.ensure_vault_state_dir(vault_root)
    path = store_path(vault_root)
    try:
        return _open(path, meta)
    except sqlite3.Error as error:
        if _is_busy(error):
            raise _fault(error) from error
        # Corruption: unlink and recreate once.
        reset(vault_root)
        try:
            return _open(path, meta)
        except sqlite3.Error as again:
            raise _fault(again) from again


def _open(path: Path, meta: Mapping[str, str]) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        if _stored_meta(conn) != dict(meta):
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM pages")
            conn.execute("DELETE FROM meta")
            conn.executemany("INSERT INTO meta VALUES (?, ?)", sorted(meta.items()))
            conn.execute("COMMIT")
        return conn
    except BaseException:
        conn.close()
        raise


def _stored_meta(conn: sqlite3.Connection) -> dict[str, str]:
    return dict(conn.execute("SELECT key, value FROM meta"))


def _load_rows(conn: sqlite3.Connection) -> dict[str, _Row]:
    """Every row's identity columns. No head, facts or frontmatter loads before egress."""
    try:
        return {
            path: _Row(path, (mtime, ctime, size, dev, ino), sha, verified, state, cue_key)
            for path, mtime, ctime, size, dev, ino, sha, verified, state, cue_key in conn.execute(
                "SELECT path, mtime_ns, ctime_ns, size, dev, ino, sha256, verified_at_ns,"
                " state, cue_key FROM pages"
            )
        }
    except sqlite3.Error as error:
        raise _fault(error) from error


# ---------------------------------------------------------------- evidence <-> JSON


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _encode(evidence: PageEvidence) -> tuple[str, str]:
    head = {
        "title": evidence.title,
        "status": evidence.status,
        "registry": evidence.registry_facts,
        "frontmatter": (
            None
            if evidence.frontmatter is None
            else yaml_safe_dump_for_parse(dict(evidence.frontmatter))
        ),
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
        path=path,
        title=head["title"],
        status=head["status"],
        registry_facts=head["registry"],
        frontmatter=None if head["frontmatter"] is None else yaml_safe_load(head["frontmatter"]),
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
    """One page this audience may see, and what this call knows of its evidence."""

    rel: str
    indexable: bool
    #: Excluded or raw-protected: never takes a row, read live on every sweep.
    path_volatile: bool
    path: Path
    info: os.stat_result
    row: _Row | None
    #: The head decoded from the verified row, or the whole evidence of a live read.
    evidence: PageEvidence | None = None
    #: `evidence` came from reading the page this call, so it already holds the facts.
    live: bool = False
    #: The verified row's facts, decoded only when the page is aggregated.
    facts: str | None = None


def sweep(
    vault_root: Path,
    *,
    entity_types: EntityTypeRegistry,
    status_basis: Any,
    make_resolver: Callable[[Iterable[tuple[str, str]]], WikilinkResolver],
    attachment_probe: Callable[[str], bool],
) -> list[entity_recurrence.Candidate]:
    """`entity_recurrence.collect_pages` over the pages this audience may see, from the store.

    `make_resolver` receives each visible page's `(path, title)`. Raises `StoreFault` when
    the store cannot answer; the caller then runs the full sweep and, when
    `StoreFault.reset` is set, calls `reset`.
    """
    cues = entity_recurrence.cue_grammar(entity_types)
    try:
        meta = _meta(vault_root)
        conn = _connect(vault_root, meta)
    except (OSError, ValueError) as error:
        # ValueError: a state root the vault refuses; the full sweep needs no state.
        raise StoreFault(str(error), reset=False) from error
    try:
        rows = _load_rows(conn)
        visible, complete, seen = _walk(vault_root, rows)
        _refresh(vault_root, conn, meta, cues, rows, visible, complete, seen)
        return _aggregate(
            vault_root,
            entity_types=entity_types,
            cues=cues,
            status_basis=status_basis,
            visible=visible,
            make_resolver=make_resolver,
            attachment_probe=attachment_probe,
        )
    finally:
        conn.close()


def _walk(vault_root: Path, rows: Mapping[str, _Row]) -> tuple[list[_Visible], bool, set[str]]:
    """Walk the Knowledge Base by file metadata and ask egress before any row is read.

    Returns the visible pages in walk order, whether the walk reported no error, and
    every walked path, visible or not, so a row is dropped only when its file is gone.
    `rows` holds identity columns only.
    """
    from .governance import egress, raw_protection
    from .governance.principal import effective_principal

    who = effective_principal()
    admitted = egress.restricted_release_filter(vault_root)
    errors: list[OSError] = []
    visible: list[_Visible] = []
    seen: set[str] = set()
    with call_spans.span("entity_evidence.walk", {"pages_walked": 0, "pages_visible": 0}) as fields:
        for path, info in find_corpus.walk_md_with_stat(
            kb_root(vault_root), on_error=errors.append
        ):
            rel = relative_posix(path, vault_root)
            seen.add(rel)
            fields["pages_walked"] += 1
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
            fields["pages_visible"] += 1
    return visible, not errors, seen


def _refresh(
    vault_root: Path,
    conn: sqlite3.Connection,
    meta: Mapping[str, str],
    cues: CueGrammar,
    rows: Mapping[str, _Row],
    visible: list[_Visible],
    complete: bool,
    seen: set[str],
) -> None:
    """Take each visible page's evidence from its verified row, or from the page itself
    when the row cannot be trusted, then persist what changed."""
    writes: list[tuple[Any, ...]] = []
    touches: list[tuple[Any, ...]] = []
    scrubs: set[str] = set()
    trust_ctime = vault.STAT_GENERATION_TRUSTED
    with call_spans.span(
        "entity_evidence.refresh",
        {"pages_reextracted": 0, "pages_volatile": 0, "pages_verified": 0},
    ) as fields:
        for entry in visible:
            row, identity = entry.row, _identity(entry.info)
            cue_key = _cue_key(entry.rel, cues)
            if entry.path_volatile and entry.rel in rows:
                # A page that became excluded or raw-protected loses any older row, decodable or not.
                scrubs.add(entry.rel)
            matches = row is not None and row.cue_key == cue_key and row.identity == identity
            if row is not None and matches and trust_ctime and not row.racy:
                if _take_row(conn, entry, row):
                    continue
                # A concurrent worker replaced the row: read the page and use that read wholly.
                row, matches = None, False
            if matches:
                fields["pages_verified"] += 1
            # Taken before the read: a later rewrite then cannot hide inside the racy window.
            verified_at = time.time_ns()
            content = find_corpus.read_page_bytes(entry.path, vault_root)
            if content is None:
                # Unreadable now: absent from this sweep, exactly as in the full sweep.
                continue
            digest = hashlib.sha256(content).hexdigest()
            if (
                row is not None
                and row.sha256 == digest
                and row.cue_key == cue_key
                and _take_row(conn, entry, row)
            ):
                # The same bytes under a new or racy identity: keep the facts.
                if identity != row.identity or (trust_ctime and row.racy):
                    touches.append((*identity, verified_at, entry.rel))
                continue
            page = find_corpus.parse_page(
                entry.path, entry.info.st_mtime, vault_root, content=content
            )
            if page is None:
                # Bytes read but not decodable; the row only spares reading them again.
                fields["pages_reextracted"] += 1
                if not entry.path_volatile and _unchanged(entry):
                    writes.append(
                        _row(entry, digest, verified_at, _UNPARSEABLE, cue_key, None, None)
                    )
                continue
            evidence = entity_recurrence.page_evidence(page, cues=cues)
            entry.evidence, entry.live = evidence, True
            # A row is keyed by the walked path, so a page that names another path never takes one.
            if entry.path_volatile or evidence.path != entry.rel:
                fields["pages_volatile"] += 1
                if entry.rel in rows:
                    scrubs.add(entry.rel)
                continue
            fields["pages_reextracted"] += 1
            if _unchanged(entry):
                head, facts = _encode(evidence)
                writes.append(_row(entry, digest, verified_at, _PARSED, cue_key, head, facts))
        _persist(conn, meta, writes, touches, sorted(scrubs), rows, seen, complete)


def _take_row(conn: sqlite3.Connection, entry: _Visible, row: _Row) -> bool:
    """Read the verified row's head and facts in one query bound to what the walk verified.

    Returns False when no row of that content and cue grammar stands any more, because
    a concurrent worker replaced it. The caller then reads the page and uses that read
    for both head and facts, so an old head never meets new facts.
    """
    if row.state != _PARSED:
        # Bytes that did not decode: the page has no evidence, and the row only spares reading them.
        return True
    try:
        found = conn.execute(
            "SELECT head, facts FROM pages WHERE path = ? AND sha256 = ? AND cue_key = ?",
            (entry.rel, row.sha256, row.cue_key),
        ).fetchone()
    except sqlite3.Error as error:
        raise _fault(error) from error
    if found is None:
        return False
    head_json, entry.facts = found
    try:
        entry.evidence = _decode_head(entry.rel, head_json)
    except _BAD_ROW as error:
        # The error class only: its message can quote the row's content.
        raise _bad_row(entry.rel, type(error).__name__) from error
    return True


def _unchanged(entry: _Visible) -> bool:
    """Whether the file still has the identity the walk saw, after it was read."""
    try:
        return _identity(entry.path.lstat()) == _identity(entry.info)
    except OSError:
        return False


def _cue_key(rel: str, cues: CueGrammar) -> str:
    """The cue digest a row's frames were extracted under; an Entities page has no frames."""
    return "" if rel.startswith(entity_recurrence.entities_prefix()) else cues.digest


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
    meta: Mapping[str, str],
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
        if _stored_meta(conn) != dict(meta):
            # Another build re-stamped the store after this call opened it. Its rows are not
            # ours to extend; this call's answer came from its own reads.
            conn.execute("ROLLBACK")
            log.debug("entity evidence store write skipped: another build owns the store")
            return
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
    *,
    entity_types: EntityTypeRegistry,
    cues: CueGrammar,
    status_basis: Any,
    visible: list[_Visible],
    make_resolver: Callable[[Iterable[tuple[str, str]]], WikilinkResolver],
    attachment_probe: Callable[[str], bool],
) -> list[entity_recurrence.Candidate]:
    entries = {id(entry.evidence): entry for entry in visible if entry.evidence is not None}
    walked_indexable = {entry.rel: entry.indexable for entry in visible}

    def indexable(path: str) -> bool:
        # The walk answered for each walked path; a page that names another path asks here.
        answer = walked_indexable.get(path)
        return access.is_indexable(vault_root, path) if answer is None else answer

    with call_spans.span("entity_evidence.aggregate", {"pages_aggregated": 0}) as fields:

        def timed_resolver(titles: Iterable[tuple[str, str]]) -> WikilinkResolver:
            with call_spans.span("entity_evidence.resolver"):
                return make_resolver(titles)

        def complete(head: PageEvidence) -> PageEvidence:
            # Only an eligible page is asked, so only its facts are decoded.
            entry = entries[id(head)]
            fields["pages_aggregated"] += 1
            if entry.live:
                return head
            if entry.facts is None:
                raise _bad_row(entry.rel, "no facts")
            try:
                return _decode_facts(head, entry.facts)
            except _BAD_ROW as error:
                raise _bad_row(entry.rel, type(error).__name__) from error

        return entity_recurrence.collect_evidence(
            [entry.evidence for entry in entries.values()],
            vault_root=vault_root,
            entity_types=entity_types,
            cues=cues,
            status_basis=status_basis,
            indexable=indexable,
            make_resolver=timed_resolver,
            complete=complete,
            attachment_probe=attachment_probe,
        )
