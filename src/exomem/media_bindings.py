"""Rebuildable physical binding facts in the existing media ledger.

Canonical companions remain authoritative. An incomplete projection can never
authorize eviction; it does not participate in upload or read admission.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from collections.abc import Iterable, Iterator, Mapping
from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

from . import media_capacity, original_pool, originals, reserved_paths, trash_metadata

# This key and the schema fields below belong to the versioned derived ledger.
_STATE = "physical-bindings-v1"
log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Binding:
    binary_rel: str
    companion_rel: str
    companion_hash: str
    object_key: str
    sha256: str
    size: int
    resident_only: bool
    reference: str | None


@dataclass(frozen=True, slots=True)
class Checkpoint:
    generation: int = 0
    complete: bool = False
    revision: int | None = None
    seq: int | None = None
    policy: str | None = None


@dataclass(frozen=True, slots=True)
class Basis:
    pooled: bool
    bounds: media_capacity.Bounds | None
    store: str | None


@dataclass(frozen=True, slots=True)
class ObjectFacts:
    object_key: str
    sha256: str
    size: int
    bindings: int
    resident_only: bool
    recoverable: bool
    owner_read: float | None
    pinned: bool
    references: tuple[originals.StoredObject, ...]
    checkpoint: Checkpoint


def basis(vault: Path) -> Basis:
    config = originals.configuration(vault)
    return Basis(original_pool.enabled(vault),
                 media_capacity.bounds_for(config, vault) if config is not None else None,
                 None if config is None else str(config["store"]))


def initialize(conn: sqlite3.Connection) -> None:
    """Add derived facts and captured pin identities without rewriting canonical state."""
    conn.execute("CREATE TABLE IF NOT EXISTS media_bindings ("
                 "binary_rel TEXT PRIMARY KEY, companion_rel TEXT NOT NULL, companion_hash TEXT NOT NULL, "
                 "object_key TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL, "
                 "resident_only INTEGER NOT NULL, reference TEXT)")
    conn.execute("CREATE INDEX IF NOT EXISTS media_bindings_object ON media_bindings(object_key, binary_rel)")
    conn.execute("CREATE INDEX IF NOT EXISTS media_bindings_companion ON media_bindings(companion_rel)")
    # A pin captures the acquired object's identity. Joining its current filing later
    # would silently move that pin when a companion changes.
    for table in ("media_access", "jobs"):
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if columns and "object_key" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN object_key TEXT")
        if columns:
            conn.execute(f"CREATE INDEX IF NOT EXISTS {table}_object_key ON {table}(object_key)")
        if table == "media_access":
            # These are fixed acquisition-proof columns in the media ledger schema.
            for field in ("object_identity", "object_sha256"):
                if field not in columns:
                    conn.execute(f"ALTER TABLE media_access ADD COLUMN {field} TEXT")


def checkpoint(conn: sqlite3.Connection) -> Checkpoint:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (_STATE,)).fetchone()
    if row is None:
        return Checkpoint()
    try:
        state = Checkpoint(**json.loads(row[0]))
        if (type(state.generation) is not int or state.generation < 0 or type(state.complete) is not bool
                or any(value is not None and (type(value) is not int or value < 0)
                       for value in (state.revision, state.seq))
                or state.policy is not None and not originals.is_sha256(state.policy)):
            return Checkpoint()
        return state
    except (TypeError, ValueError):
        return Checkpoint()  # A lost or older projection is unknown, never an empty archive.


def _record(conn: sqlite3.Connection, state: Checkpoint) -> None:
    from dataclasses import asdict

    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (_STATE, json.dumps(asdict(state), separators=(",", ":"))))


def _ledger(vault: Path, *, create: bool = True):
    from .media_jobs import MediaJobStore

    return MediaJobStore(vault, create=create)


def withdraw(vault: Path) -> Checkpoint | None:
    """Durably withdraw completeness before a canonical install or external debounce."""
    ledger = _ledger(vault, create=False)
    if not ledger.path.exists():
        return None  # With no ledger, no eviction has a complete projection to consume.
    with closing(ledger._connect()) as conn:
        conn.execute("PRAGMA synchronous=FULL")
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            before = checkpoint(conn)
            _record(conn, Checkpoint(before.generation + 1, False, before.revision, before.seq, before.policy))
        return before


def policy_key(vault: Path) -> str:
    config = originals.configuration(vault)
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def object_key(vault: Path, binary_rel: str, digest: str | None = None, *, pooled: bool | None = None,
               bound: str | None = None) -> str:
    """An ordinary file's location stays distinct, even when another file has equal bytes.

    ``bound`` is the path that the binding's companion names, when a Trash placement maps it to ``binary_rel``.
    A Trash entry's bytes stay at its own path (`originals.may_pool`).
    """
    if digest is None:
        snapshot = originals.original_state(vault, binary_rel)
        digest = snapshot.binary_sha256
        if snapshot.local_path is not None:
            return snapshot.local_path.relative_to(vault).as_posix()
    if digest is None or not originals.may_pool(binary_rel if bound is None else bound):
        return binary_rel
    if pooled is None:
        pooled = original_pool.enabled(vault)
    if pooled:
        try:
            reserved_paths.inspect_generic_file(vault, binary_rel)
        except reserved_paths.ReservedPathLeafError as error:
            if error.code != "MISSING":
                raise
            return original_pool.object_path(vault, digest).relative_to(vault).as_posix()
    return binary_rel


def snapshot_key(vault: Path, snapshot: originals.OriginalSnapshot) -> str:
    """Name the physical cache row without borrowing another filing's recovery reference."""
    return (snapshot.local_path or snapshot.path).relative_to(vault).as_posix()


def coalesce_cache(conn: sqlite3.Connection, keys: Iterable[str] | None = None) -> None:
    """Merge affected physical rows, retaining read age but never inventing byte verification.

    The caller owns the ledger transaction. A full merge belongs only to the
    background census; publication passes indexed affected object keys.
    """
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS cache_objects (object_key TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM cache_objects")
    if keys is not None:
        conn.executemany("INSERT OR IGNORE INTO cache_objects VALUES (?)", ((key,) for key in keys))
    scope = "" if keys is None else (
        " WHERE c.binary_rel IN (SELECT object_key FROM cache_objects) OR c.binary_rel IN "
        "(SELECT b.binary_rel FROM media_bindings b JOIN cache_objects k ON k.object_key = b.object_key)")
    conn.execute("DROP TABLE IF EXISTS temp.cache_merge")
    conn.execute(
        "CREATE TEMP TABLE cache_merge AS SELECT c.binary_rel AS previous, "
        "CASE WHEN c.sha256 = b.sha256 AND (c.size IS NULL OR c.size = b.size) "
        "THEN b.object_key ELSE c.binary_rel END AS physical, c.* "
        "FROM media_cache c LEFT JOIN media_bindings b ON b.binary_rel = c.binary_rel" + scope)
    if conn.execute("SELECT 1 FROM cache_merge GROUP BY physical "
                    "HAVING MIN(sha256) != MAX(sha256) OR MIN(size) != MAX(size) LIMIT 1").fetchone():
        raise ValueError("physical cache rows disagree about one original")
    conn.execute("DELETE FROM media_cache WHERE binary_rel IN (SELECT previous FROM cache_merge)")
    conn.execute(
        "INSERT INTO media_cache(binary_rel, reference, sha256, identity, last_read, size, evicted, owner_read) "
        "SELECT physical, MIN(reference), MIN(sha256), "
        # Moving a row is not a byte verification: only an existing physical proof survives.
        "MAX(CASE WHEN previous = physical THEN identity ELSE '' END), MAX(last_read), "
        "CASE WHEN MIN(size) = MAX(size) THEN MAX(size) ELSE NULL END, MIN(evicted), MAX(owner_read) "
        "FROM cache_merge GROUP BY physical")
    conn.execute("DROP TABLE cache_merge")


def from_page(vault: Path, rel: str, data: bytes, *, selected: Basis | None = None,
              placements: trash_metadata.PlacementCensus | None = None, include_retired: bool = False,
              displaced: bool = False) -> Binding | None:
    """Derive one companion's facts without reading any original bytes.

    A page that binds a reference away from its original's companion path raises
    `originals.DisplacedReference`. A caller that has confirmed that reference from the media ledger
    passes ``displaced`` to get the facts of the binding that the page names.
    """
    from .vault import canonical_vault_rel, parse_frontmatter

    fields, _body, marker = parse_frontmatter(data.decode("utf-8"), custody=True)
    binary = originals.binary_binding(fields)
    if marker is None or binary is None:
        if originals.bound_path(fields) is not None:
            raise ValueError("an original companion has incomplete identity")
        return None
    own_placements = placements is None
    if own_placements:
        placements = trash_metadata.PlacementCensus(vault)
    try:
        binary = placements.resolve(rel, fields)
    except originals.DisplacedReference as error:
        if not displaced:
            raise
        binary = error.binary
    digest = hashlib.sha256(data).hexdigest()
    retired = not include_retired and placements.retired_source(rel, fields, digest)
    if own_placements:
        placements.require_current()
    if retired:
        return None
    if binary is None:
        raise ValueError("an original companion has incomplete identity")
    if canonical_vault_rel(vault, binary.path) != binary.path or reserved_paths.classify_logical(binary.path).blocked:
        raise ValueError("an original companion has an unsafe path")
    reference = originals.stored_reference(fields.get("stored_object"))
    if "stored_object" in fields and reference is None:
        raise ValueError("an original companion has an incomplete recovery reference")
    selected = selected if selected is not None else basis(vault)
    if reference is not None and reference.store != selected.store:
        reference = None
    bounds = selected.bounds
    resident = (originals.is_note_path(binary.path) or "data_file" in fields
                or bounds is None or bounds.classify(binary.size) == media_capacity.RESIDENT)
    key = object_key(vault, binary.path, binary.sha256, pooled=selected.pooled, bound=originals.bound_path(fields))
    return Binding(binary.path, rel, digest, key, binary.sha256,
                   binary.size, resident, None if reference is None else json.dumps(reference.fields(), sort_keys=True))


def _put(conn: sqlite3.Connection, bindings: Iterable[Binding]) -> None:
    bindings = tuple(bindings)
    changed = conn.executemany("INSERT INTO media_bindings VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                 "ON CONFLICT(binary_rel) DO UPDATE SET companion_rel=excluded.companion_rel, "
                 "companion_hash=excluded.companion_hash, object_key=excluded.object_key, sha256=excluded.sha256, "
                 "size=excluded.size, resident_only=excluded.resident_only, reference=excluded.reference "
                 "WHERE media_bindings.companion_rel = excluded.companion_rel",
                 ((binding.binary_rel, binding.companion_rel, binding.companion_hash, binding.object_key,
                   binding.sha256, binding.size, int(binding.resident_only), binding.reference) for binding in bindings)).rowcount
    if changed != len(bindings):
        # A new companion must not erase another companion's retention requirement.
        raise ValueError("canonical companions disagree about one filing")


def apply_delta(vault: Path, paths: Iterable[str], bindings: Iterable[Binding], *,
                expected: Checkpoint, revision: int | None = None, seq: int | None = None) -> bool:
    """Replace exact path facts and their source checkpoint in one ledger transaction."""
    ledger = _ledger(vault)
    bindings = tuple(bindings)
    with closing(ledger._connect()) as conn:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            current = checkpoint(conn)
            if current.generation != expected.generation + 1 or current.complete:
                return False
            conn.executemany("DELETE FROM media_bindings WHERE companion_rel = ? OR binary_rel = ?",
                             ((rel, rel) for rel in paths))
            _put(conn, bindings)
            coalesce_cache(conn, (binding.object_key for binding in bindings))
            _record(conn, Checkpoint(current.generation, expected.complete, revision, seq, expected.policy))
    return True


@dataclass(frozen=True, slots=True)
class PreparedDelta:
    paths: tuple[str, ...]
    bindings: tuple[Binding, ...]


@dataclass(frozen=True, slots=True)
class FileChange:
    before: Checkpoint
    forward: PreparedDelta
    inverse: PreparedDelta


@dataclass(slots=True)
class _Operation:
    vault: Path
    before: Checkpoint
    paths: set[str] = field(default_factory=set)


_operation: ContextVar[_Operation | None] = ContextVar("media_binding_operation", default=None)


@contextmanager
def operation(vault: Path, paths: Iterable[str]) -> Iterator[None]:
    """Withdraw completeness once around a compound file-mode change, then apply one exact delta.

    A move or Trash transition renames files before its own batch runs. The batch alone would repair only
    its paths and leave the renamed source's row behind. Inside this scope a nested batch adds its paths
    instead of withdrawing again. Success applies one delta over every path; any exception leaves the
    projection incomplete for the background census. Store mode has its own boundary (`follow_store`).
    """
    vault = Path(vault).resolve()
    active = _operation.get()
    if active is not None and active.vault == vault:
        active.paths.update(paths)
        yield
        return
    ledger = _ledger(vault, create=False)
    from .vault_store import mode

    before = (withdraw(vault) if ledger.path.exists() and original_pool.enabled(vault)
              and not mode.store_mode_for_reads(vault) else None)
    if before is None:
        yield
        return
    scope = _Operation(vault, before, set(paths))
    token = _operation.set(scope)
    try:
        yield
    finally:
        _operation.reset(token)
    try:
        delta = prepare_delta(vault, scope.paths)
        apply_delta(vault, delta.paths, delta.bindings, expected=before)
    except Exception:  # noqa: BLE001 - derived failure cannot undo an acknowledged canonical outcome
        log.warning("physical binding update remains incomplete; background recovery will retry", exc_info=True)


def begin_file_change(vault: Path, images: Mapping[str, bytes], installed: Iterable[str]) -> FileChange | None:
    """Prepare exact facts, then withdraw completeness before the first ordinary-file install."""
    active = _operation.get()
    if active is not None and active.vault == Path(vault).resolve():
        active.paths.update(installed)
        return None  # The enclosing operation already withdrew completeness and applies one delta.
    ledger = _ledger(vault, create=False)
    if not ledger.path.exists() or not original_pool.enabled(vault):
        return None
    from .vault_store import mode

    if mode.store_mode_for_reads(vault):
        return None  # The canonical store's revision supplies its publication boundary.
    paths = tuple(installed)
    try:
        selected = basis(vault)
        inverse = prepare_delta(vault, paths, selected=selected)
        forward = prepare_delta(vault, paths, images=images, selected=selected)
        if not inverse.bindings and not forward.bindings:
            return None
        from dataclasses import replace

        # A staged ordinary binary wins over a same-digest pooled object in a mixed archive.
        forward = PreparedDelta(forward.paths, tuple(
            replace(binding, object_key=binding.binary_rel) if binding.binary_rel in paths else binding
            for binding in forward.bindings))
    except (OSError, ValueError, UnicodeError, reserved_paths.ReservedPathLeafError):
        withdraw(vault)
        return None  # Unknown inventory retains bytes; it does not refuse a filing.
    # If this durable withdrawal fails, publishing changed bindings would make
    # previously complete eviction facts unsafe. Only that publication must retry.
    before = withdraw(vault)
    return None if before is None else FileChange(before, forward, inverse)


def finish_file_change(vault: Path, change: FileChange | None, *, committed: bool) -> None:
    """A failed derived update keeps eviction disabled and leaves canonical acknowledgement alone."""
    if change is None:
        return
    try:
        # A companion edit can carry its retirement metadata in this same batch.
        # Resolve the committed pair, rather than the pre-install metadata image.
        delta = prepare_delta(vault, change.forward.paths) if committed else change.inverse
        apply_delta(vault, delta.paths, delta.bindings, expected=change.before)
    except Exception:  # noqa: BLE001 - derived failure cannot undo an acknowledged canonical outcome
        log.warning("physical binding update remains incomplete; background recovery will retry", exc_info=True)


def prepare_delta(vault: Path, paths: Iterable[str], *, images: Mapping[str, bytes | None] | None = None,
                  selected: Basis | None = None) -> PreparedDelta:
    """Read exact affected companions, including those whose ordinary binary changed."""
    paths = tuple(dict.fromkeys(paths))
    companions = {rel for rel in paths if rel.lower().endswith(".md")}
    ledger = _ledger(vault, create=False)
    if ledger.path.exists():
        with closing(ledger._connect(readonly=True)) as conn:
            # Parameter batches bound SQL size; none enumerates the archive.
            for start in range(0, len(paths), 256):
                batch = paths[start:start + 256]
                placeholders = ",".join("?" for _ in batch)
                companions.update(row[0] for row in conn.execute(
                    f"SELECT companion_rel FROM media_bindings WHERE binary_rel IN ({placeholders})", batch))
    selected = selected if selected is not None else basis(vault)
    bindings = []
    for rel in sorted(companions):
        if images is not None and rel in images:
            data = images[rel]
        else:
            try:
                data = reserved_paths.read_generic_bytes(vault, rel).data
            except reserved_paths.ReservedPathLeafError as error:
                if error.code != "MISSING":
                    raise
                data = None
        if data is not None and (binding := from_page(vault, rel, data, selected=selected)) is not None:
            bindings.append(binding)
    return PreparedDelta(tuple(sorted(set(paths) | companions)), tuple(bindings))


def follow_store(vault: Path, *, batch: int = 256) -> bool:
    """Replay one bounded canonical audit suffix; a broken trail requires background recovery."""
    from .vault_store import mode, read

    ledger = _ledger(vault, create=False)
    if not ledger.path.exists() or not mode.store_mode_for_reads(vault):
        return False
    with closing(ledger._connect(readonly=True)) as conn:
        before = checkpoint(conn)
    if not before.complete or before.seq is None or before.policy != policy_key(vault):
        return False
    with read.snapshot(vault, join_request=False) as view:
        head = view.connection.execute("SELECT COALESCE(MAX(seq), 0) FROM store_audit").fetchone()[0]
        if before.seq > head or before.revision is None or before.revision > view.revision:
            withdraw(vault)
            return False
        if before.seq == head:
            return before.revision == view.revision
        rows = view.connection.execute(
            "SELECT seq, path, from_path FROM store_audit WHERE seq > ? ORDER BY seq LIMIT ?",
            (before.seq, batch)).fetchall()
        if any(row[1] is None for row in rows):
            withdraw(vault)
            return False
        paths = tuple({path for row in rows for path in row[1:] if path is not None})
        # The pinned snapshot supplies current bodies for both sides of a move.
        bodies = view.bodies(rel for rel in paths if rel.lower().endswith(".md"))
        images = {rel: bodies.get(rel) for rel in paths if rel.lower().endswith(".md")}
        delta = prepare_delta(vault, paths, images=images)
        reached = rows[-1][0]
        from .media_store import _guard

        with _guard(vault, "media_binding_audit_publish"):
            if source_checkpoint(vault) != (view.revision, head):
                return False
            with closing(ledger._connect(readonly=True)) as conn:
                if checkpoint(conn) != before:
                    return False
            withdrawn = withdraw(vault)
            if withdrawn != before:
                return False
            applied = apply_delta(vault, delta.paths, delta.bindings, expected=before,
                                  revision=view.revision, seq=reached)
            # `current` refuses the partial cursor; the next background slice follows it.
            return applied and reached == head


def _pages(vault: Path, *, join_request: bool = False):
    """Every canonical page under the managed root. A census reads a fresh snapshot; a request may join its own."""
    from contextlib import nullcontext

    from .kbdir import managed_join, managed_root_rel
    from .vault_store import mode, read

    with (read.snapshot(vault, join_request=join_request) if mode.store_mode_for_reads(vault)
          else nullcontext(None)) as view:
        if view is not None:
            # Authoritative store pages include companions not projected to disk yet.
            # One streaming query avoids a body lookup per companion in a large archive.
            prefix = managed_join("")
            yield from view.connection.execute(
                "SELECT path, body FROM store_pages WHERE path >= ? AND path < ? ORDER BY path",
                (prefix, prefix + "\U0010ffff"))
            return  # Every ordinary companion is a store-owned page; disk is only its projection.
        with reserved_paths.open_generic_tree(
            vault, managed_root_rel(), require_complete=True,
            select=lambda candidates: (rel for rel, kind in candidates if kind == "file" and rel.lower().endswith(".md")),
        ) as entries:
            for entry in entries:
                rel = managed_join(entry.relative_path)
                if not read.serves(vault, rel):
                    yield rel, reserved_paths.read_generic_bytes(vault, rel).data


def source_checkpoint(vault: Path) -> tuple[int | None, int | None]:
    from .vault_store import mode, read

    if not mode.store_mode_for_reads(vault):
        return None, None
    with read.snapshot(vault, join_request=False) as view:
        return view.revision, view.connection.execute("SELECT COALESCE(MAX(seq), 0) FROM store_audit").fetchone()[0]


def current(vault: Path, conn: sqlite3.Connection) -> bool:
    """Check the source frontier under the caller's canonical guard before using object facts."""
    from . import freshness

    state = checkpoint(conn)
    return (state.complete and not freshness.external_pending(vault) and state.policy == policy_key(vault)
            and (state.revision, state.seq) == source_checkpoint(vault))


def external_change(vault: Path) -> None:
    """Fence an observed outside edit before its existing watcher debounce."""
    ledger = _ledger(vault, create=False)
    if ledger.path.exists() and original_pool.enabled(vault):
        withdraw(vault)


def repair_paths(vault: Path, paths: Iterable[str]) -> None:
    """Repair exact receipt paths; an incomplete inventory still needs its background census."""
    active = _operation.get()
    if active is not None and active.vault == Path(vault).resolve():
        active.paths.update(paths)
        return  # The operation's own index receipts join its one delta instead of withdrawing again.
    ledger = _ledger(vault, create=False)
    if not ledger.path.exists() or not original_pool.enabled(vault):
        return
    from .vault_store import mode

    if mode.store_mode_for_reads(vault):
        follow_store(vault)
        return
    with closing(ledger._connect(readonly=True)) as conn:
        before = checkpoint(conn)
    delta = prepare_delta(vault, paths)
    with closing(ledger._connect(readonly=True)) as conn:
        selected = []
        for start in range(0, len(delta.paths), 256):
            batch = delta.paths[start:start + 256]
            placeholders = ",".join("?" for _ in batch)
            selected.extend(Binding(**dict(row)) for row in conn.execute(
                f"SELECT * FROM media_bindings WHERE companion_rel IN ({placeholders})", batch))
        if sorted(selected, key=lambda item: item.binary_rel) == sorted(delta.bindings, key=lambda item: item.binary_rel):
            return
    withdrawn = withdraw(vault)
    if withdrawn == before:
        apply_delta(vault, delta.paths, delta.bindings, expected=before)


def rebuild(vault: Path) -> bool | None:
    """A startup/background census; no request or pin admission calls this walk.

    True when it publishes a complete projection. None when a write, an outside change or a configuration change
    raced it: it publishes nothing and leaves the checkpoint as the race left it. False when it failed: it then
    withdraws completeness, which moves the generation.
    """
    vault = Path(vault).resolve()
    ledger = _ledger(vault)
    from .writer_lease import get_manager

    # Starting mid-publication could scan half a compound move and later bless it.
    with get_manager().consistency_guard(vault, operation="media_binding_census_start", holder_kind="background"):
        with closing(ledger._connect(readonly=True)) as conn:
            before = checkpoint(conn)
        source = source_checkpoint(vault)
        policy = policy_key(vault)
        selected = basis(vault)
    # A separate SQLite transaction permits streaming the census without holding the
    # canonical writer. A changed generation discards it before publication.
    with closing(ledger._connect()) as conn:
        conn.execute("CREATE TEMP TABLE binding_census AS SELECT * FROM media_bindings WHERE 0")
        conn.execute("CREATE INDEX temp.binding_census_companion ON binding_census(companion_rel)")
        conn.execute("CREATE TEMP TABLE displaced_census AS SELECT * FROM media_bindings WHERE 0")
        placements = trash_metadata.PlacementCensus(vault)
        try:
            displaced: list[tuple[originals.DisplacedReference, Binding]] = []
            for rel, data in _pages(vault):
                try:
                    binding = from_page(vault, rel, data, selected=selected, placements=placements, include_retired=True)
                except originals.DisplacedReference as error:
                    # A page away from its original, as a confined move leaves one, counts once the ledger confirms it.
                    found = from_page(vault, rel, data, selected=selected, placements=placements,
                                      include_retired=True, displaced=True)
                    if found is not None:
                        displaced.append((error, found))
                    continue
                if binding is not None:
                    conn.execute("INSERT INTO binding_census VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                 (binding.binary_rel, binding.companion_rel, binding.companion_hash,
                                  binding.object_key, binding.sha256, binding.size,
                                  int(binding.resident_only), binding.reference))
            _confirm_displaced(vault, conn, displaced)
            _retire_census(vault, conn, placements)
            _fill_displaced(conn)  # A retired filing that a confirmed page still names stays counted.
            # An extant damaged companion cannot erase a previously acquired witness.
            # Only canonical absence proves release; a replacement binding is already in the census.
            for row in conn.execute("SELECT DISTINCT b.companion_rel FROM media_bindings b "
                                    "WHERE NOT EXISTS(SELECT 1 FROM binding_census n "
                                    "WHERE n.companion_rel = b.companion_rel AND n.binary_rel = b.binary_rel) "
                                    "AND NOT EXISTS(SELECT 1 FROM displaced_census d "
                                    "WHERE d.companion_rel = b.companion_rel AND d.binary_rel = b.binary_rel) "
                                    "AND NOT EXISTS(SELECT 1 FROM retired_census r "
                                    "WHERE r.companion_rel = b.companion_rel AND r.binary_rel = b.binary_rel)"):
                try:
                    reserved_paths.read_generic_bytes(vault, row[0])
                except reserved_paths.ReservedPathLeafError as error:
                    if error.code != "MISSING":
                        raise
                else:
                    raise ValueError("an extant companion lost its original binding")
            placements.require_current()
            conn.commit()
            from .media_store import _guard

            with _guard(vault, "media_binding_census_publish"):
                conn.execute("BEGIN IMMEDIATE")
                if checkpoint(conn) != before or source_checkpoint(vault) != source or policy_key(vault) != policy:
                    conn.rollback()
                    return None
                if conn.execute("SELECT 1 FROM binding_census GROUP BY binary_rel HAVING COUNT(*) > 1 LIMIT 1").fetchone():
                    raise ValueError("canonical companions disagree about one filing")
                conn.execute("DELETE FROM media_bindings")
                conn.execute("INSERT INTO media_bindings SELECT * FROM binding_census")
                coalesce_cache(conn)
                _record(conn, Checkpoint(before.generation + 1, True, *source, policy))
                conn.commit()
                return True
        except (ValueError, UnicodeError, OSError, reserved_paths.ReservedPathLeafError, sqlite3.Error):
            conn.rollback()
    withdraw(vault)
    return False


def _confirm_displaced(vault: Path, conn: sqlite3.Connection,
                       displaced: list[tuple[originals.DisplacedReference, Binding]]) -> None:
    """Count each displaced page that the media ledger confirms, once per original; any other is unknown.

    Such a page holds no original beside it, and several pages may name one original. A canonical
    companion's binding wins, then the first displaced page in path order, as `media_store.evicted_members`
    lists an original once; the census keeps every confirmed page, so none reads as a lost binding.
    """
    if not displaced:
        return
    from .media_store import recorded_versions

    if not all(recorded_versions(vault, [(error.binary, error.reference) for error, _binding in displaced])):
        raise ValueError("a page away from its original binds a version that the media ledger does not record")
    conn.executemany("INSERT INTO displaced_census VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     ((binding.binary_rel, binding.companion_rel, binding.companion_hash, binding.object_key,
                       binding.sha256, binding.size, int(binding.resident_only), binding.reference)
                      for _error, binding in displaced))
    _fill_displaced(conn)


def _fill_displaced(conn: sqlite3.Connection) -> None:
    """Give each original that only confirmed displaced pages name one binding, the first in path order."""
    conn.execute("INSERT INTO binding_census SELECT d.* FROM displaced_census d "
                 "WHERE NOT EXISTS(SELECT 1 FROM binding_census b WHERE b.binary_rel = d.binary_rel) "
                 "AND d.companion_rel = (SELECT MIN(x.companion_rel) FROM displaced_census x "
                 "WHERE x.binary_rel = d.binary_rel)")


def _retire_census(vault: Path, conn: sqlite3.Connection, placements: trash_metadata.PlacementCensus) -> None:
    """Exclude only a completed, version-witnessed transfer to a current Trash binding."""
    from .kbdir import managed_join

    conn.execute("CREATE TEMP TABLE retired_census (binary_rel TEXT PRIMARY KEY, companion_rel TEXT NOT NULL)")
    prefix = managed_join(f"{trash_metadata.TRASH_SUBPATH}/")
    for row in conn.execute("SELECT * FROM binding_census WHERE binary_rel >= ? AND binary_rel < ? "
                            "AND reference IS NOT NULL ORDER BY binary_rel", (prefix, prefix + "\U0010ffff")):
        retirement = placements.retirement(row["binary_rel"])
        if retirement is None:
            continue
        trash = retirement.trash
        if ((row["binary_rel"], row["sha256"], row["size"]) != (trash.path, trash.sha256, trash.size)
                or originals.stored_reference(json.loads(row["reference"])) != retirement.reference):
            raise ValueError("Trash retirement no longer has its exact recovery binding")
        source = conn.execute("SELECT * FROM binding_census WHERE binary_rel = ? AND companion_rel = ?",
                              (retirement.source.path, retirement.companion)).fetchone()
        try:
            reserved_paths.inspect_generic_path(vault, retirement.source.path)
        except reserved_paths.ReservedPathLeafError as error:
            if error.code != "MISSING":
                raise
        else:
            continue  # An ordinary original at the old path is a new lifetime.
        if source is None:
            continue  # Canonical absence leaves no historical binding to exclude.
        if (source["sha256"], source["size"], source["companion_hash"], source["reference"]) != (
                retirement.source.sha256, retirement.source.size, retirement.after, None):
            raise ValueError("the source retirement has not reached its witnessed companion state")
        from .vault import parse_frontmatter

        data = reserved_paths.read_generic_bytes(vault, retirement.companion).data
        fields = parse_frontmatter(data.decode("utf-8"), custody=True)[0]
        if (hashlib.sha256(data).hexdigest() != retirement.after
                or fields.get("original_retirement_meta") != trash.path + trash_metadata.METADATA_SUFFIX):
            raise ValueError("the source companion has no reciprocal retirement witness")
        conn.execute("INSERT INTO retired_census VALUES (?, ?)", (source["binary_rel"], source["companion_rel"]))
    conn.execute("DELETE FROM binding_census WHERE binary_rel IN (SELECT binary_rel FROM retired_census)")


# Per process: one census at a time, and the checkpoint generation that each vault's last failed census left.
_census_lock = threading.Lock()
_failed_census: dict[Path, int] = {}


def recover(vault: Path) -> bool:
    """Make a shared archive's projection current with a background census; True when it is current.

    Background passes call this outside any guard, since the census takes its own; a request or an eviction never
    does. A census that lost a race runs again on the next pass. A failed census is not repeated until the
    projection's generation moves again, so a persistent failure costs one census, not one per pass.
    """
    vault = Path(vault).resolve()
    ledger = _ledger(vault, create=False)
    if not ledger.path.exists() or not original_pool.enabled(vault):
        return False
    with _census_lock:
        with closing(ledger._connect(readonly=True)) as conn:
            if current(vault, conn):
                return True
            generation = checkpoint(conn).generation
        if _failed_census.get(vault) == generation:
            return False
        published = rebuild(vault)
        if published is not False:  # A raced census (None) is not a failure: the next pass runs it again.
            _failed_census.pop(vault, None)
            return bool(published)
        # The failure withdrew completeness, which moved the generation past the one read above. Keying on the
        # generation after that withdrawal is what stops a persistent failure from running once per pass.
        with closing(ledger._connect(readonly=True)) as conn:
            _failed_census[vault] = checkpoint(conn).generation
        return False


def unreferenced(vault: Path, *, batch: int = 1024) -> Iterator[tuple[str, str]] | None:
    """The (logical path, companion path) of each binding without a recovery reference in the selected store.

    These are the bindings that an offload may still need. None when the projection is not current, because only a
    complete census proves that it misses no binding. A row names what the last census or delta saw; a caller
    resolves each one before it acts on it.
    """
    ledger = _ledger(vault, create=False)
    if not ledger.path.exists():
        return None
    with closing(ledger._connect(readonly=True)) as conn:
        if not current(vault, conn):
            return None

    def rows() -> Iterator[tuple[str, str]]:
        after = ""
        while True:
            # Keyset pages bound each read; a connection is held only while one page is read.
            with closing(ledger._connect(readonly=True)) as conn:
                page = conn.execute("SELECT binary_rel, companion_rel FROM media_bindings "
                                    "WHERE binary_rel > ? AND reference IS NULL ORDER BY binary_rel LIMIT ?",
                                    (after, batch)).fetchall()
            yield from ((row[0], row[1]) for row in page)
            if len(page) < batch:
                return
            after = page[-1][0]

    return rows()


def object_facts(conn: sqlite3.Connection, key: str) -> ObjectFacts | None:
    """Aggregate one object's bindings and captured pins with indexed ledger reads."""
    state = checkpoint(conn)
    if not state.complete:
        return None
    row = conn.execute(
        "SELECT COUNT(*), MIN(b.sha256), MAX(b.sha256), MIN(b.size), MAX(b.size), "
        "MAX(b.resident_only), MIN(b.reference IS NOT NULL), MAX(c.owner_read) "
        "FROM media_bindings b LEFT JOIN media_cache c ON c.binary_rel = b.object_key WHERE b.object_key = ?",
        (key,)).fetchone()
    if row[0] == 0 or row[1] != row[2] or row[3] != row[4]:
        return None
    # Legacy NULL pins retain bytes until startup recovery resolves them. A claimed
    # pending job pins; an unclaimed pending job deliberately does not.
    pinned = bool(conn.execute(
        "SELECT EXISTS(SELECT 1 FROM media_access WHERE object_key = ? OR object_key IS NULL) "
        "OR EXISTS(SELECT 1 FROM jobs WHERE pinned = 1 AND state IN ('pending', 'running') "
        "AND (object_key = ? OR object_key IS NULL)) "
        "OR EXISTS(SELECT 1 FROM media_drain WHERE latched = 1 AND proof_version = 1)", (key, key)).fetchone()[0])
    references = tuple(originals.stored_reference(json.loads(item[0])) for item in conn.execute(
        "SELECT DISTINCT reference FROM media_bindings WHERE object_key = ? AND reference IS NOT NULL ORDER BY reference", (key,)))
    if any(reference is None for reference in references):
        return None
    return ObjectFacts(key, row[1], row[3], row[0], bool(row[5]), bool(row[6]), row[7], pinned, references, state)
