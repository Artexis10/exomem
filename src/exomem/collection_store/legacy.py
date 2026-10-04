"""Disposable migration audit spool; neither canonical import nor activation.

History-sized state lives on disk. Python retains one event and fixed I/O
buffers; SQLite uses a bounded cache and file-backed temporary work. Collection
adapters and the eventual migration are not thereby constant-memory.
"""

from __future__ import annotations

import codecs
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from .. import held_fs, records, reserved_paths, vault
from .. import structured_collections as collections
from .connection import CollectionStoreError

_PREFIX = ".exomem-collection-audit-"
_COMPANIONS = ("-wal", "-shm", "-journal")
_INPUT_FAILURES = (OSError, held_fs.HeldFsError, CollectionStoreError, UnicodeError)
_BREAKS = re.compile(r"\r\n|[\r\n\v\f\x1c-\x1e\x85\u2028\u2029]")
_PROFILES = tuple(
    (name, records.profile_for(name).activity_prefix) for name in ("records", "planning")
)
_SCHEMA = """
CREATE TABLE segments (
    path TEXT PRIMARY KEY, role TEXT NOT NULL, identity TEXT,
    size INTEGER, mtime INTEGER, ctime INTEGER, digest TEXT, seen INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TABLE occurrences (
    id INTEGER PRIMARY KEY, profile TEXT NOT NULL, transition TEXT NOT NULL,
    parent TEXT NOT NULL, collection TEXT NOT NULL, path TEXT NOT NULL,
    rank INTEGER NOT NULL, line INTEGER NOT NULL, original TEXT NOT NULL,
    canonical BLOB NOT NULL, chosen INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX occurrence_order ON occurrences(profile, transition, rank, path, line);
CREATE UNIQUE INDEX chosen_transition ON occurrences(profile, transition) WHERE chosen=1;
CREATE INDEX chosen_collection ON occurrences(profile, collection, parent) WHERE chosen=1;
CREATE TABLE walk (
    proof TEXT NOT NULL, depth INTEGER NOT NULL, occurrence INTEGER NOT NULL,
    transition TEXT NOT NULL, PRIMARY KEY(proof, depth), UNIQUE(proof, transition)
) WITHOUT ROWID;
CREATE TABLE findings (
    proof TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL, detail TEXT,
    PRIMARY KEY(proof, kind, key)
) WITHOUT ROWID;
"""


def _invalid() -> CollectionStoreError:
    return CollectionStoreError("COLLECTION_LEGACY_AUDIT_INVALID", "audit input is unusable")


def _identity(identity: held_fs.StableIdentity) -> str:
    return json.dumps((identity.device, identity.inode, identity.kind, identity.link_count))


def _frame(digest, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def _profile_lines(chunks: Iterable[str]) -> Iterator[tuple[int, str, str]]:
    """Match splitlines grammar without retaining arbitrarily long inert lines."""
    position = 1
    pieces: list[str] = []
    probe = ""
    profile = None
    ignored = False
    after_cr = False
    for chunk in chunks:
        if after_cr and chunk.startswith("\n"):
            chunk = chunk[1:]
        after_cr = False
        start = 0
        for boundary in _BREAKS.finditer(chunk):
            piece = chunk[start : boundary.start()]
            if not ignored:
                if profile is None:
                    probe += piece
                    match = next(
                        ((name, prefix) for name, prefix in _PROFILES if probe.startswith(prefix)),
                        None,
                    )
                    if match is not None:
                        profile, prefix = match
                        pieces.append(probe[len(prefix) :])
                else:
                    pieces.append(piece)
                if profile is not None:
                    yield position, profile, "".join(pieces)
            pieces.clear()
            probe, profile, ignored = "", None, False
            position += 1
            start = boundary.end()
            after_cr = boundary.group() == "\r" and start == len(chunk)
        piece = chunk[start:]
        if not ignored and piece:
            if profile is not None:
                pieces.append(piece)
            else:
                probe += piece
                match = next(
                    ((name, prefix) for name, prefix in _PROFILES if probe.startswith(prefix)), None
                )
                if match is not None:
                    profile, prefix = match
                    pieces.append(probe[len(prefix) :])
                    probe = ""
                elif not any(prefix.startswith(probe) for _, prefix in _PROFILES):
                    probe, ignored = "", True
    if profile is not None:
        yield position, profile, "".join(pieces)


@dataclass(frozen=True, slots=True)
class LegacyAuditProof:
    """Finite representation of captured input, not a claim of continuous provenance."""

    collection_id: str
    legacy_inspection: dict
    exhaustive_status: str
    scan_complete: bool
    manifest_head: str | None
    reachable_head: str | None
    reachable_count: int
    ordered_event_digest: str
    input_basis_digest: str
    gap_count: int
    discontinuity_count: int
    diagnostic_samples: tuple[str, ...]
    _spool_id: str = field(repr=False)
    _verification_id: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class CapturedAuditInspection:
    inspection: dict
    influencing_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LegacyAuditEvent:
    transition_id: str
    parent_id: str
    original_json: str


class LegacyAuditSpool:
    """Own one exclusive scratch family in a caller-owned private directory.

    This trusted internal API does not authorize a later destination or modify
    vault inputs. Deadline/cancellation are cooperative, including SQLite
    progress callbacks; an OS I/O call can block beyond the absolute deadline.
    Proofs and iterators are usable only inside this context.
    """

    def __init__(
        self,
        staging_directory: Path,
        *,
        deadline: float,
        cancelled: Callable[[], bool] | None = None,
    ):
        self._staging_directory = Path(staging_directory)
        self._deadline = deadline
        self._cancelled = cancelled
        self._token = secrets.token_hex(16)
        self._path = self._staging_directory / f"{_PREFIX}{self._token}.sqlite"
        self._connection = None
        self._filesystem = self._parent = self._owned_file = None
        self._owns_companions = False
        self._failed = False
        self._scanned = False
        self._entered = False
        self._interruption = None

    def _check(self) -> None:
        if self._failed:
            raise _invalid()
        if self._cancelled is not None and self._cancelled():
            self._failed = True
            raise CollectionStoreError("COLLECTION_LEGACY_AUDIT_CANCELLED", "audit scan cancelled")
        if time.monotonic() >= self._deadline:
            self._failed = True
            raise TimeoutError("COLLECTION_LEGACY_AUDIT_DEADLINE: audit deadline elapsed")

    def _progress(self) -> int:
        try:
            self._check()
        except (CollectionStoreError, TimeoutError) as error:
            self._interruption = error
            return 1
        return 0

    def _sql(self, statement: str, parameters=()):
        self._check()
        if self._connection is None:
            raise _invalid()
        try:
            return self._connection.execute(statement, parameters)
        except sqlite3.DatabaseError as error:
            self._failed = True
            raise self._interruption or _invalid() from error

    def _rows(self, statement: str, parameters=()):
        try:
            for row in self._sql(statement, parameters):
                self._check()
                yield row
        except sqlite3.DatabaseError as error:
            self._failed = True
            raise self._interruption or _invalid() from error

    def __enter__(self) -> LegacyAuditSpool:
        if self._entered:
            raise ValueError("an audit spool context is single-use")
        self._entered = True
        self._check()
        try:
            self._filesystem = held_fs.acquire(self._staging_directory).require()
            self._parent = self._filesystem.parent(".").require()
            with reserved_paths._subsystem_authority_scope("collection_store.legacy"):
                self._owned_file = self._filesystem.file(
                    self._parent,
                    self._path.name,
                    create=True,
                    exclusive=True,
                ).require()
            for suffix in _COMPANIONS:
                result = self._filesystem.file(self._parent, self._path.name + suffix)
                if result.ok:
                    result.require().close()
                    raise _invalid()
                if result.error.code != "MISSING":
                    raise result.error
            self._owns_companions = True
            self._connection = sqlite3.connect(self._path, isolation_level=None, timeout=0.05)
            self._connection.set_progress_handler(self._progress, 1000)
            for pragma in (
                "cache_size=-2048",
                "temp_store=FILE",
                "mmap_size=0",
                "journal_mode=DELETE",
                "synchronous=OFF",
            ):
                self._sql("PRAGMA " + pragma)
            self._connection.executescript(_SCHEMA)
            return self
        except BaseException:
            self.__exit__()
            raise

    def __exit__(self, *_exc) -> None:
        try:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            if self._owned_file is not None:
                current = self._filesystem.file(self._parent, self._path.name, access="mutate")
                if current.ok:
                    with current.require() as file:
                        if held_fs._same_file_identity(file.identity, self._owned_file.identity):
                            if self._owns_companions:
                                for suffix in _COMPANIONS:
                                    companion = self._filesystem.file(
                                        self._parent,
                                        self._path.name + suffix,
                                        access="mutate",
                                    )
                                    if companion.ok:
                                        with companion.require() as member:
                                            self._filesystem.unlink(member).require()
                                    elif companion.error.code != "MISSING":
                                        raise companion.error
                            self._filesystem.unlink(file).require()
                elif current.error.code != "MISSING":
                    raise current.error
        finally:
            for handle in (self._owned_file, self._parent, self._filesystem):
                if handle is not None:
                    handle.close()
            self._owned_file = self._parent = self._filesystem = None
            self._scanned = False

    def _observe(self, path, role, identity, metadata=None, digest=None, *, recheck):
        values = (
            _identity(identity) if identity is not None else None,
            *(metadata or (None, None, None)),
            digest,
        )
        if recheck:
            prior = self._sql(
                "SELECT identity,size,mtime,ctime,digest FROM segments WHERE path=?",
                (path,),
            ).fetchone()
            if prior != values:
                raise _invalid()
            self._sql("UPDATE segments SET seen=1 WHERE path=?", (path,))
        else:
            self._sql("INSERT INTO segments VALUES (?,?,?,?,?,?,?,1)", (path, role, *values))

    def _file(self, filesystem, path, role, *, recheck, ingest=False):
        parent_path, _, leaf = path.rpartition("/")
        with filesystem.parent(parent_path or ".").require() as parent:
            with filesystem.file(parent, leaf).require() as file:
                before = os.fstat(file.descriptor)
                metadata = (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                digest = hashlib.sha256()

                def chunks():
                    decoder = codecs.getincrementaldecoder("utf-8")("strict")
                    while chunk := os.read(file.descriptor, 64 * 1024):
                        self._check()
                        digest.update(chunk)
                        if ingest:
                            yield decoder.decode(chunk)
                    if ingest:
                        yield decoder.decode(b"", final=True)

                if ingest:
                    for line, profile, original in _profile_lines(chunks()):
                        self._check()
                        try:
                            event = json.loads(original)
                            valid = records._valid_audit_event(event, profile)
                            canonical = records._canonical_json(event) if valid else None
                        except (ValueError, TypeError, UnicodeError, RecursionError):
                            valid = False
                        if not valid:
                            self._finding("scan:" + profile, "malformed", f"{path}:{line}")
                            continue
                        self._sql(
                            "INSERT INTO occurrences(profile,transition,parent,collection,path,"
                            "rank,line,original,canonical) VALUES (?,?,?,?,?,?,?,?,?)",
                            (
                                profile,
                                event["transition_id"],
                                event["parent_id"],
                                event["collection_id"],
                                path,
                                int(role != "live"),
                                line,
                                original,
                                canonical,
                            ),
                        )
                else:
                    for _ in chunks():
                        pass
                after = os.fstat(file.descriptor)
                if metadata != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise _invalid()
                with filesystem.file(parent, leaf).require() as named:
                    if named.identity != file.identity:
                        raise _invalid()
                self._observe(
                    path, role, file.identity, metadata, digest.hexdigest(), recheck=recheck
                )

    def _directory(self, filesystem, path, *, recheck):
        result = filesystem.parent(path)
        if result.ok:
            with result.require() as directory:
                identity = directory.identity
        elif result.error.code == "MISSING":
            identity = None
        else:
            raise result.error
        self._observe(path, "anchor", identity, recheck=recheck)
        return identity is not None

    def _source_pass(self, *, recheck):
        try:
            with held_fs.acquire(self._root).require() as filesystem:
                self._observe(".", "anchor", filesystem.root_identity, recheck=recheck)
                kb = vault.kb_prefix().rstrip("/")
                self._directory(filesystem, kb, recheck=recheck)
                self._directory(filesystem, kb + "/_archive", recheck=recheck)
                archive = kb + "/_archive/logs"
                exists = self._directory(filesystem, archive, recheck=recheck)
                self._file(filesystem, kb + "/log.md", "live", recheck=recheck, ingest=not recheck)
                if exists:
                    with (
                        filesystem.parent(archive).require() as parent,
                        closing(filesystem.iter_names(parent)) as names,
                    ):
                        for name in names:
                            self._check()
                            path = archive + "/" + name
                            if records._AUDIT_ARCHIVE_NAME.fullmatch(name):
                                self._file(
                                    filesystem, path, "archive", recheck=recheck, ingest=not recheck
                                )
                                continue
                            result = filesystem.file(parent, name)
                            if result.ok:
                                with result.require() as file:
                                    stat = os.fstat(file.descriptor)
                                    self._observe(
                                        path,
                                        "census",
                                        file.identity,
                                        (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns),
                                        recheck=recheck,
                                    )
                            else:
                                with filesystem.parent(path).require() as directory:
                                    self._observe(
                                        path, "census", directory.identity, recheck=recheck
                                    )
                if recheck:
                    for (path,) in self._rows("SELECT path FROM segments WHERE role='manifest'"):
                        self._file(filesystem, path, "manifest", recheck=True)
                    if self._sql("SELECT 1 FROM segments WHERE seen=0 LIMIT 1").fetchone():
                        raise _invalid()
        except _INPUT_FAILURES:
            self._failed = True
            raise

    def _recheck(self):
        self._sql("UPDATE segments SET seen=0")
        self._source_pass(recheck=True)

    def require_source_root(self, vault_root: Path) -> None:
        """Bind a consumer's file capture to this spool's physical vault anchor."""
        if not self._scanned:
            raise _invalid()
        recorded = self._sql(
            "SELECT identity FROM segments WHERE path='.' AND role='anchor'"
        ).fetchone()
        with held_fs.acquire(Path(vault_root)).require() as filesystem:
            identity = _identity(filesystem.root_identity)
        if recorded is None or identity != recorded[0]:
            raise CollectionStoreError(
                "COLLECTION_LEGACY_AUDIT_ROOT_MISMATCH",
                "file capture and audit capture belong to different vault roots",
            )

    def scan(self, vault_root: Path) -> None:
        """Capture all profiles once, then validate the captured census and bytes."""
        if self._scanned:
            raise ValueError("an audit spool scans only once")
        self._root = Path(vault_root)
        self._sql("BEGIN")
        self._source_pass(recheck=False)
        self._sql(
            "UPDATE occurrences AS candidate SET chosen=1 WHERE NOT EXISTS ("
            "SELECT 1 FROM occurrences AS earlier WHERE earlier.profile=candidate.profile "
            "AND earlier.transition=candidate.transition AND (earlier.rank,earlier.path,earlier.line)"
            " < (candidate.rank,candidate.path,candidate.line))",
        )
        self._recheck()
        self._sql("COMMIT")
        self._scanned = True

    def _finding(self, proof, kind, key, detail=None):
        self._sql("INSERT OR IGNORE INTO findings VALUES (?,?,?,?)", (proof, kind, key, detail))

    def _event(self, profile, collection_id, transition):
        row = self._sql(
            "SELECT id,original FROM occurrences WHERE profile=? AND transition=? "
            "AND chosen=1 AND collection=?",
            (profile, transition, collection_id),
        ).fetchone()
        return (row[0], json.loads(row[1])) if row is not None else None

    def _capture_manifest(self, manifest):
        try:
            with held_fs.acquire(self._root).require() as filesystem:
                prior = self._sql(
                    "SELECT digest FROM segments WHERE path=?", (manifest.path,)
                ).fetchone()
                self._file(filesystem, manifest.path, "manifest", recheck=prior is not None)
        except _INPUT_FAILURES:
            self._failed = True
            raise
        digest, size = self._sql(
            "SELECT digest,size FROM segments WHERE path=?", (manifest.path,)
        ).fetchone()
        if digest != manifest.manifest_version.hash:
            self._failed = True
            raise _invalid()
        return size

    def _bounded_history(self, profile):
        """Apply the legacy limits to captured occurrences, not fresh file reads."""
        ordinary = 0
        for (path,) in self._rows("SELECT path FROM segments WHERE role IN ('archive','census')"):
            if Path(path).name.startswith(vault._BATCH_RESIDUE_PREFIX):
                # The spool captures this directory's identity, not the child
                # census used by the legacy residue classifier. Do not invent
                # a historical classification from a later live observation.
                raise CollectionStoreError(
                    "COLLECTION_LEGACY_AUDIT_UNREPRESENTABLE",
                    "captured archive residue has no legacy classification",
                )
            if not vault._is_registered_internal_state_artifact(path):
                ordinary += 1
            if ordinary > records._MAX_AUDIT_ARCHIVE_ENTRIES:
                return records._AuditEvents((), False)
        total = 0
        for (size,) in self._rows("SELECT size FROM segments WHERE role IN ('live','archive')"):
            total += size
            if size > records._MAX_AUDIT_SEGMENT_BYTES or total > records._MAX_AUDIT_HISTORY_BYTES:
                return records._AuditEvents((), False)
        if self._sql(
            "SELECT 1 FROM findings WHERE proof=? LIMIT 1", ("scan:" + profile,)
        ).fetchone():
            return records._AuditEvents((), False)
        events = []
        for (original,) in self._rows(
            "SELECT original FROM occurrences WHERE profile=? ORDER BY rank,path,line LIMIT ?",
            (profile, records._MAX_AUDIT_EVENTS + 1),
        ):
            events.append(json.loads(original))
        return records._AuditEvents(
            tuple(events) if len(events) <= records._MAX_AUDIT_EVENTS else (),
            len(events) <= records._MAX_AUDIT_EVENTS,
        )

    def inspect_captured(self, *, manifest, current_container_hash, markers):
        return self.capture_inspection(
            manifest=manifest, current_container_hash=current_container_hash, markers=markers
        ).inspection

    def capture_inspection(self, *, manifest, current_container_hash, markers):
        """Preserve bounded historical status without rereading event bodies.

        Manifest acquisition and source rechecks bind the caller's captured
        collection to this spool. Whole collection capture remains its caller's
        responsibility; exhaustive representation uses verify separately.
        """
        if not self._scanned:
            raise _invalid()
        self._recheck()
        manifest_size = self._capture_manifest(manifest)
        bounded_markers = []
        for marker in markers:
            self._check()
            bounded_markers.append(marker)
            if len(bounded_markers) > records._MAX_AUDIT_MARKERS:
                break
        paths = ()
        if (
            manifest_size > records._MAX_AUDIT_SOURCE_BYTES
            or len(bounded_markers) > records._MAX_AUDIT_MARKERS
        ):
            chain = records._incomplete_audit_chain()
        else:
            history = self._bounded_history(manifest.semantic_profile)
            chain = records._reconstruct_captured_audit_chain(
                manifest,
                history=history,
                head=manifest.audit_head,
                current_hash=current_container_hash,
                markers=tuple(bounded_markers),
            )
            influencing = records._audit_influencing_events(
                history.events,
                [event for event in history.events if event["collection_id"] == manifest.collection_id],
                collection_id=manifest.collection_id, head=manifest.audit_head,
            )
            paths = tuple(sorted({
                event[name] for event in influencing
                for name in ("manifest_path", "source_path", "canonical_path")
            }))
        self._recheck()
        return CapturedAuditInspection(records._audit_inspection_payload(chain), paths)

    def verify(
        self,
        *,
        manifest: collections.CollectionManifest,
        current_container_hash: str,
        markers: Iterable[records._AuditMarker],
        legacy_inspection: dict,
    ) -> LegacyAuditProof:
        """Keep the supplied bounded report separate from this uncapped assessment."""
        if not self._scanned:
            raise _invalid()
        self._recheck()
        profile, collection_id = manifest.semantic_profile, manifest.collection_id
        if self._sql(
            "SELECT 1 FROM findings WHERE proof=? LIMIT 1", ("scan:" + profile,)
        ).fetchone():
            raise _invalid()
        self._capture_manifest(manifest)
        verification = secrets.token_hex(16)
        marker_iterator = iter(markers)
        first_marker = next(marker_iterator, None)
        relevant = (
            self._sql(
                "SELECT 1 FROM occurrences WHERE profile=? AND collection=? LIMIT 1",
                (profile, collection_id),
            ).fetchone()
            is not None
        )
        baseline = manifest.audit_head is None and first_marker is None and not relevant
        if not baseline:
            self._sql(
                "INSERT OR IGNORE INTO findings SELECT ?, 'gap', 'conflicting-transition:' || "
                "first.transition, NULL FROM occurrences first JOIN occurrences other ON "
                "other.profile=first.profile AND other.transition=first.transition "
                "WHERE first.profile=? AND first.chosen=1 AND other.canonical!=first.canonical",
                (verification, profile),
            )
            self._sql(
                "INSERT INTO findings SELECT ?, 'gap', 'transition-fork:' || parent, NULL "
                "FROM occurrences WHERE profile=? AND collection=? AND chosen=1 "
                "GROUP BY parent HAVING count(*)>1",
                (verification, profile, collection_id),
            )
            selected = self._event(profile, collection_id, manifest.audit_head or "")
            if selected is None:
                raise CollectionStoreError(
                    "COLLECTION_LEGACY_AUDIT_UNREPRESENTABLE",
                    "audit head has no finite representation",
                )
            _, cursor = selected
            if not records._event_matches_transition(cursor, manifest):
                self._finding(verification, "gap", "head-collection-mismatch")
            if cursor["after_container_hash"] != current_container_hash:
                self._finding(verification, "gap", "current-container-mismatch")
            if cursor["after_manifest_hash"] != manifest.manifest_version.hash:
                self._finding(verification, "gap", "current-manifest-mismatch")
            depth = 0
            while selected is not None:
                self._check()
                occurrence, cursor = selected
                if self._sql(
                    "SELECT 1 FROM walk WHERE proof=? AND transition=?",
                    (verification, cursor["transition_id"]),
                ).fetchone():
                    raise CollectionStoreError(
                        "COLLECTION_LEGACY_AUDIT_UNREPRESENTABLE",
                        "audit cycle has no finite representation",
                    )
                self._sql(
                    "INSERT INTO walk VALUES (?,?,?,?)",
                    (verification, depth, occurrence, cursor["transition_id"]),
                )
                if not records._event_matches_transition(cursor, manifest):
                    self._finding(
                        verification, "gap", "invalid-transition:" + cursor["transition_id"]
                    )
                    break
                parent = cursor["parent_id"]
                if parent in {"baseline", "absent"}:
                    break
                predecessor = self._event(profile, collection_id, parent)
                if predecessor is None:
                    self._finding(verification, "gap", "missing-parent:" + parent)
                    break
                if (
                    cursor["operation"] in {"rebaseline", "plan_rebaseline"}
                    and cursor.get("continuity") is False
                ):
                    detail = records._canonical_json(
                        {
                            "provenance_continuity": False,
                            "prior_head": parent,
                            "acknowledged_gap_codes": cursor["acknowledged_gap_codes"],
                            "rationale": cursor["rationale"],
                            "checkpoint_transition": cursor["transition_id"],
                            "gap_fingerprint": cursor["gap_fingerprint"],
                            "checkpoint_snapshot_hash": cursor["checkpoint_snapshot_hash"],
                        }
                    ).decode()
                    self._finding(verification, "discontinuity", cursor["transition_id"], detail)
                elif (
                    cursor["before_container_hash"] != predecessor[1]["after_container_hash"]
                    or cursor["before_manifest_hash"] != predecessor[1]["after_manifest_hash"]
                ):
                    self._finding(
                        verification, "gap", "transition-discontinuity:" + cursor["transition_id"]
                    )
                    break
                depth += 1
                selected = predecessor
            self._sql(
                "INSERT INTO findings SELECT ?, 'gap', 'unreachable-transition:' || transition, NULL "
                "FROM occurrences o WHERE profile=? AND collection=? AND chosen=1 AND NOT EXISTS "
                "(SELECT 1 FROM walk w WHERE w.proof=? AND w.transition=o.transition)",
                (verification, profile, collection_id, verification),
            )
            self._sql(
                "INSERT INTO findings SELECT ?, 'gap', 'foreign-child:' || transition, NULL "
                "FROM occurrences o WHERE profile=? AND collection!=? AND chosen=1 AND EXISTS "
                "(SELECT 1 FROM walk w WHERE w.proof=? AND w.transition=o.parent)",
                (verification, profile, collection_id, verification),
            )
        marker_digest = hashlib.sha256(b"legacy-audit-markers-v1\0")
        marker = first_marker
        while marker is not None:
            self._check()
            _frame(
                marker_digest,
                records._canonical_json(
                    (marker.transition_id, marker.canonical_path, marker.item_key),
                ),
            )
            selected = self._event(profile, collection_id, marker.transition_id)
            if (
                selected is None
                or not self._sql(
                    "SELECT 1 FROM walk WHERE proof=? AND transition=?",
                    (verification, marker.transition_id),
                ).fetchone()
                or not records._event_matches_transition(selected[1], manifest)
                or selected[1]["canonical_path"] != marker.canonical_path
                or selected[1]["item_key"] != marker.item_key
            ):
                self._finding(verification, "gap", "unmatched-marker:" + marker.transition_id)
            marker = next(marker_iterator, None)
        ordered = self._ordered_digest(verification)
        basis = hashlib.sha256(b"legacy-audit-input-v1\0")
        for row in self._rows(
            "SELECT path,role,identity,size,mtime,ctime,digest FROM segments "
            "WHERE role!='manifest' OR path=? ORDER BY path",
            (manifest.path,),
        ):
            _frame(basis, records._canonical_json(row))
        _frame(
            basis,
            records._canonical_json(
                (
                    profile,
                    collection_id,
                    manifest.path,
                    manifest.manifest_version.hash,
                    current_container_hash,
                    manifest.audit_head,
                    marker_digest.hexdigest(),
                )
            ),
        )
        gaps = self._sql(
            "SELECT count(*) FROM findings WHERE proof=? AND kind='gap'", (verification,)
        ).fetchone()[0]
        discontinuities = self._sql(
            "SELECT count(*) FROM findings WHERE proof=? AND kind='discontinuity'",
            (verification,),
        ).fetchone()[0]
        samples = tuple(
            row[0]
            for row in self._sql(
                "SELECT key FROM findings WHERE proof=? ORDER BY kind,key LIMIT 32",
                (verification,),
            )
        )
        count = self._sql("SELECT count(*) FROM walk WHERE proof=?", (verification,)).fetchone()[0]
        self._recheck()
        return LegacyAuditProof(
            collection_id,
            dict(legacy_inspection),
            "baseline"
            if baseline
            else "gap"
            if gaps
            else "acknowledged_gap"
            if discontinuities
            else "ok",
            True,
            manifest.audit_head,
            manifest.audit_head if count else None,
            count,
            ordered,
            basis.hexdigest(),
            gaps,
            discontinuities,
            samples,
            self._token,
            verification,
        )

    def _ordered_digest(self, verification):
        """Domain-separated, length-framed original UTF-8 JSON in ancestor order."""
        digest = hashlib.sha256(b"legacy-audit-ordered-v1\0")
        for (original,) in self._rows(
            "SELECT o.original FROM walk w JOIN occurrences o ON o.id=w.occurrence "
            "WHERE w.proof=? ORDER BY w.depth DESC",
            (verification,),
        ):
            _frame(digest, original.encode("utf-8"))
        return digest.hexdigest()

    def iter_reachable(
        self, proof: LegacyAuditProof, *, oldest_first: bool = True
    ) -> Iterator[LegacyAuditEvent]:
        if not self._scanned or proof._spool_id != self._token:
            raise _invalid()
        self._recheck()
        direction = "DESC" if oldest_first else "ASC"
        for transition, parent, original in self._rows(
            "SELECT o.transition,o.parent,o.original FROM walk w JOIN occurrences o "
            "ON o.id=w.occurrence WHERE w.proof=? ORDER BY w.depth " + direction,
            (proof._verification_id,),
        ):
            yield LegacyAuditEvent(transition, parent, original)
        self._recheck()
