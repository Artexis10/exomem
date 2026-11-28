"""Rebuildable physical binding facts in the existing media ledger.

Canonical companions remain authoritative. An incomplete projection can never
authorize eviction or an automatic policy copy.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, replace
from itertools import islice
from pathlib import Path

from . import held_fs, media_capacity, original_pool, originals, reserved_paths, trash_metadata
from .collection_store import connection as store_connection

# These keys and the schema fields below belong to the versioned derived ledger.
_STATE = "physical-bindings-v1"
# Separate keys keep the checkpoint's format, which older code parses strictly and resets on an unknown field.
_SIDE = "physical-bindings-side-v1"
_IDENTITY = "physical-bindings-identity-v1"
_FAILED = "physical-bindings-failed-v1"
_POLICY_PAGE = 256
_BINDING_COLUMNS = "binary_rel, companion_rel, companion_hash, object_key, sha256, size, resident_only, reference"
_SIDE_COLUMNS = ("companion_rel, counted, flagged, displaced, object, version, sha256, size, reference, "
                 "binary_rel, owned, reason")
#: The most audit rows that one follow round replays, so the most that a hold replays (design D2).
REPLAY_ROWS = 256
#: Offload's follow before a hold stops after this many rounds, a burst of 2,048 writes, and answers busy.
FOLLOW_ROUNDS = 8
#: Upload admission applies at most this audit suffix in memory; past it the upload is answered busy (D5).
UPLOAD_SUFFIX_ROWS = REPLAY_ROWS * FOLLOW_ROUNDS
#: Names this process in a failed rebuild's record, so a record that an earlier process wrote is retried once.
_PROCESS = uuid.uuid4().hex
log = logging.getLogger(__name__)

# The race and read-failure classes (design D3): a rebuild that meets one fails as a whole, and a follow lags.
# A deterministic fault of one page raises `ValueError` or `UnicodeError` instead. `PlacementChanged` is a
# `ValueError`, so a handler tests this tuple first.
RACES = (
    OSError, sqlite3.Error, held_fs.HeldFsError, RecursionError, store_connection.CollectionStoreError,
    store_connection.CollectionStoreUnavailable, reserved_paths.ReservedPathLeafError, trash_metadata.PlacementChanged,
)
# The closed outcomes of one follow round (`Followed.state`) and the kinds of `current`.
HEAD, ADVANCED, LAGGING, RACED, REBUILD = "head", "advanced", "lagging", "raced", "rebuild"
EVICTION, ALLOWANCE, CURSOR = "eviction", "allowance", "cursor"


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
    policy_refs: str | None = None


@dataclass(frozen=True, slots=True)
class PolicyFiling:
    binary_rel: str
    companion_rel: str
    companion_hash: str
    object_key: str
    sha256: str
    size: int
    resident_only: bool
    references: tuple[originals.StoredObject, ...]


@dataclass(frozen=True, slots=True)
class Checkpoint:
    generation: int = 0
    complete: bool = False
    revision: int | None = None
    seq: int | None = None
    policy: str | None = None
    policy_projection: bool = False


@dataclass(frozen=True, slots=True)
class SideRow:
    """What the census's rule says of one page when its binding rows cannot hold it (design D3).

    A counted row names a version that the census counts: a confirmed displaced page, or a page whose
    binding `from_page` does not derive. An unknown row makes the owner total unknown and names the page;
    `flagged` is set exactly where the rebuild before the side table raised on the page, and stops eviction.
    `owned` is false for a page that the store does not own, which the store sweep checks.
    """

    companion_rel: str
    counted: bool
    flagged: bool = False
    displaced: bool = False
    object: str | None = None
    version: str | None = None
    sha256: str | None = None
    size: int | None = None
    reference: str | None = None
    binary_rel: str | None = None
    owned: bool = True
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class AuditRow:
    """The identity of one audit row: a restored store can reuse a `seq`, never with the same row."""

    seq: int
    revision: int | None = None
    at: str | None = None
    path: str | None = None


@dataclass(frozen=True, slots=True)
class FailedRebuild:
    """A rebuild that failed as a whole: the generation it left, up to five notes or files, and its process."""

    generation: int
    blockers: tuple[tuple[str, str], ...]
    process: str


@dataclass(frozen=True, slots=True)
class Followed:
    """What one follow round did: `state` is one of HEAD, ADVANCED, LAGGING, RACED and REBUILD."""

    state: str
    reached: int | None = None
    lag: int | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class Basis:
    pooled: bool
    bounds: media_capacity.Bounds | None
    store: str | None
    policy_projection: bool = False


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
    #: One filing that holds each reference, in the same order: the first by path.
    owners: tuple[str, ...]
    checkpoint: Checkpoint


def basis(vault: Path) -> Basis:
    config = originals.configuration(vault)
    return Basis(original_pool.enabled(vault),
                 media_capacity.bounds_for(config, vault) if config is not None else None,
                 None if config is None else str(config["store"]), _policy_enabled(config))


def _policy_enabled(config: dict[str, object] | None) -> bool:
    from .media_protection import POLICY_FIELD

    return config is not None and POLICY_FIELD in config


def _tracks_bindings(vault: Path, ledger) -> bool:
    if original_pool.enabled(vault):
        return True
    try:
        config = originals.configuration(vault)
        if _policy_enabled(config):
            return True
        if config is not None and counts_allowance(vault, config):
            return True  # the owner total reads the projection in store mode (count-media-allowance-from-bindings D4)
        # A file write while protection is off must invalidate a prior protected frontier.
        with closing(ledger._connect(readonly=True)) as conn:
            state = checkpoint(conn)
            return state.complete and state.policy_projection
    except sqlite3.OperationalError:
        return True  # A busy or locked ledger may still hold a complete frontier, so withdraw it.
    except sqlite3.DatabaseError:
        # No reader, eviction included, can trust a corrupt ledger, so it must not refuse the file write.
        return False
    except (OSError, ValueError, RecursionError):
        # A temporary configuration failure must withdraw any old proof without refusing the file write.
        return True


def counts_allowance(vault: Path, config: dict[str, object]) -> bool:
    """Whether the owner total comes from this projection: a store-mode vault with an allowance (D1, D4)."""
    from .media_allowance import configured
    from .vault_store import mode

    return configured(config.get("allowance_bytes")) is not None and mode.store_mode_for_reads(vault)


def initialize(conn: sqlite3.Connection) -> None:
    """Add derived facts and captured pin identities without rewriting canonical state."""
    conn.execute("CREATE TABLE IF NOT EXISTS media_bindings ("
                 "binary_rel TEXT PRIMARY KEY, companion_rel TEXT NOT NULL, companion_hash TEXT NOT NULL, "
                 "object_key TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL, "
                 "resident_only INTEGER NOT NULL, reference TEXT)")
    conn.execute("CREATE INDEX IF NOT EXISTS media_bindings_object ON media_bindings(object_key, binary_rel)")
    conn.execute("CREATE INDEX IF NOT EXISTS media_bindings_companion ON media_bindings(companion_rel)")
    # The owner total groups the bindings by version in index order, with no sort and no table lookup (D1).
    conn.execute("CREATE INDEX IF NOT EXISTS media_bindings_version ON media_bindings("
                 "json_extract(reference, '$.object'), json_extract(reference, '$.version'), size, sha256, reference) "
                 "WHERE reference IS NOT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS media_bindings_digest ON media_bindings(sha256, size)")
    conn.execute("CREATE TABLE IF NOT EXISTS media_reference_rows ("
                 "companion_rel TEXT PRIMARY KEY, counted INTEGER NOT NULL, flagged INTEGER NOT NULL, "
                 "displaced INTEGER NOT NULL, object TEXT, version TEXT, sha256 TEXT, size INTEGER, reference TEXT, "
                 "binary_rel TEXT, owned INTEGER NOT NULL, reason TEXT)")
    conn.execute("CREATE INDEX IF NOT EXISTS media_reference_rows_version ON media_reference_rows(object, version) "
                 "WHERE counted = 1")
    conn.execute("CREATE INDEX IF NOT EXISTS media_reference_rows_binary ON media_reference_rows(binary_rel) "
                 "WHERE binary_rel IS NOT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS media_reference_rows_unknown ON media_reference_rows(flagged, companion_rel) "
                 "WHERE counted = 0")
    conn.execute("CREATE TABLE IF NOT EXISTS media_policy_filings ("
                 "binary_rel TEXT PRIMARY KEY REFERENCES media_bindings(binary_rel) ON DELETE CASCADE, "
                 "companion_hash TEXT NOT NULL, refs TEXT NOT NULL)")
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
                or type(state.policy_projection) is not bool
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


def _meta(conn: sqlite3.Connection, key: str) -> object:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row[0])
    except (TypeError, ValueError, RecursionError):
        return None  # a record that cannot be read is no record


def _put_meta(conn: sqlite3.Connection, key: str, value: object) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, json.dumps(value, separators=(",", ":"))))


def side_generation(conn: sqlite3.Connection) -> int | None:
    """The checkpoint generation that the side table matches, or None when no delta or rebuild wrote one."""
    value = _meta(conn, _SIDE)
    return value if type(value) is int else None


def _identity(conn: sqlite3.Connection) -> AuditRow | None:
    value = _meta(conn, _IDENTITY)
    try:
        row = AuditRow(**value) if isinstance(value, dict) else None
    except TypeError:
        return None
    if row is None or type(row.seq) is not int:
        return None
    return row


def failed_rebuild(conn: sqlite3.Connection) -> FailedRebuild | None:
    """The last rebuild that failed as a whole, as the ledger records it for every process (D4), or None."""
    value = _meta(conn, _FAILED)
    if not isinstance(value, dict) or type(value.get("generation")) is not int:
        return None
    blockers = value.get("blockers")
    named = tuple((str(item[0]), str(item[1])) for item in blockers
                  if isinstance(item, list) and len(item) == 2) if isinstance(blockers, list) else ()
    return FailedRebuild(value["generation"], named, str(value.get("process")))


def _record_failure(vault: Path, blockers: Iterable[tuple[str, str]]) -> None:
    from .media_allowance import BLOCKERS_SHOWN

    ledger = _ledger(vault, create=False)
    if not ledger.path.exists():
        return
    named = sorted(dict.fromkeys(blockers))[:BLOCKERS_SHOWN]
    with closing(ledger._connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if _created(conn):
            _put_meta(conn, _FAILED, {"generation": checkpoint(conn).generation,
                                      "blockers": [list(item) for item in named], "process": _PROCESS})


def _side_table(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' "
                        "AND name = 'media_reference_rows'").fetchone() is not None


def flagged_pages(conn: sqlite3.Connection, *, limit: int = 5) -> tuple[tuple[str, str], ...]:
    """Up to `limit` pages whose unknown row stops eviction, with their reasons, in path order."""
    if not _side_table(conn):
        return ()
    return tuple((row[0], row[1] or "") for row in conn.execute(
        "SELECT companion_rel, reason FROM media_reference_rows WHERE counted = 0 AND flagged = 1 "
        "ORDER BY companion_rel LIMIT ?", (limit,)))


def _head(view) -> int:
    return view.connection.execute("SELECT COALESCE(MAX(seq), 0) FROM store_audit").fetchone()[0]


def _audit_row(store: sqlite3.Connection, seq: int) -> AuditRow:
    row = store.execute("SELECT seq, revision, at, path FROM store_audit WHERE seq = ?", (seq,)).fetchone()
    return AuditRow(seq) if row is None else AuditRow(*row)


def _created(conn: sqlite3.Connection) -> bool:
    # The ledger file exists before its creator commits the schema; until then it holds no bindings.
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'media_bindings'").fetchone() is not None


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
            if not _created(conn):
                return None  # A ledger still being created holds no complete projection to withdraw.
            before = checkpoint(conn)
            _record(conn, replace(before, generation=before.generation + 1, complete=False))
        return before


def policy_key(vault: Path) -> str:
    from .media_protection import POLICY_FIELD

    config = originals.configuration(vault)
    placement: object = config
    if config is not None and POLICY_FIELD in config:
        # Placement needs to know whether to capture references. Goals and allowance use the live policy.
        placement = (
            "policy-filings-v1", original_pool.enabled(vault),
            {name: value for name, value in config.items() if name != POLICY_FIELD},
        )
    return hashlib.sha256(json.dumps(placement, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


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
    if pooled and not trash_metadata.binary_exists(vault, binary_rel, inspect=reserved_paths.inspect_generic_file):
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
    fields, _body, marker = _parse(data)
    return _derive(vault, rel, data, fields, marker, selected=selected, placements=placements,
                   include_retired=include_retired, displaced=displaced)


def _parse(data: bytes) -> tuple[dict[str, object], str, object]:
    from .vault import parse_frontmatter

    return parse_frontmatter(data.decode("utf-8"), custody=True)


def _derive(vault: Path, rel: str, data: bytes, fields: dict[str, object], marker: object, *,
            selected: Basis | None, placements: trash_metadata.PlacementCensus | None, include_retired: bool,
            displaced: bool) -> Binding | None:
    from .vault import canonical_vault_rel

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
    policy_refs = _listed_refs(fields, reference) if selected.policy_projection else None
    if reference is not None and reference.store != selected.store:
        reference = None
    bounds = selected.bounds
    resident = (originals.is_note_path(binary.path) or "data_file" in fields
                or bounds is None or bounds.classify(binary.size) == media_capacity.RESIDENT)
    key = object_key(vault, binary.path, binary.sha256, pooled=selected.pooled, bound=originals.bound_path(fields))
    return Binding(binary.path, rel, digest, key, binary.sha256,
                   binary.size, resident, None if reference is None else json.dumps(reference.fields(), sort_keys=True),
                   policy_refs)


def _listed_refs(fields: dict[str, object], reference: originals.StoredObject | None) -> str | None:
    listed = fields.get(originals.REPLICAS, [])
    if not isinstance(listed, list):
        return None
    references = originals.replica_references(listed)
    if len(references) != len(listed):
        return None
    if reference is not None:
        references = (reference, *references)
    return json.dumps([ref.fields() for ref in references], sort_keys=True, separators=(",", ":"))


def _binding_values(binding: Binding) -> tuple:
    return (binding.binary_rel, binding.companion_rel, binding.companion_hash, binding.object_key,
            binding.sha256, binding.size, int(binding.resident_only), binding.reference)


def _put(conn: sqlite3.Connection, bindings: Iterable[Binding]) -> None:
    bindings = tuple(bindings)
    changed = conn.executemany("INSERT INTO media_bindings VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                 "ON CONFLICT(binary_rel) DO UPDATE SET companion_rel=excluded.companion_rel, "
                 "companion_hash=excluded.companion_hash, object_key=excluded.object_key, sha256=excluded.sha256, "
                 "size=excluded.size, resident_only=excluded.resident_only, reference=excluded.reference "
                 "WHERE media_bindings.companion_rel = excluded.companion_rel",
                 (_binding_values(binding) for binding in bindings)).rowcount
    if changed != len(bindings):
        # A new companion must not erase another companion's retention requirement.
        raise ValueError("canonical companions disagree about one filing")
    conn.executemany(
        "INSERT INTO media_policy_filings VALUES (?, ?, ?) "
        "ON CONFLICT(binary_rel) DO UPDATE SET companion_hash=excluded.companion_hash, refs=excluded.refs",
        ((binding.binary_rel, binding.companion_hash, binding.policy_refs)
         for binding in bindings if binding.policy_refs is not None),
    )


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
            complete = expected.complete and (not expected.policy_projection or expected.policy == policy_key(vault))
            _record(conn, replace(expected, generation=current.generation, complete=complete, revision=revision, seq=seq))
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

    before = (withdraw(vault) if ledger.path.exists() and _tracks_bindings(vault, ledger)
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
    if not ledger.path.exists() or not _tracks_bindings(vault, ledger):
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
        # A staged ordinary binary wins over a same-digest pooled object in a mixed archive.
        forward = PreparedDelta(forward.paths, tuple(
            replace(binding, object_key=binding.binary_rel) if binding.binary_rel in paths else binding
            for binding in forward.bindings))
    except (OSError, ValueError, RecursionError, reserved_paths.ReservedPathLeafError):
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
            for start in range(0, len(paths), 256) if _created(conn) else ():
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


@dataclass(frozen=True, slots=True)
class PageFacts:
    """What the per-page rule gives one page: its binding row, its side row, or a displaced reference to confirm.

    ``displaced`` holds the page's `DisplacedReference`, the binding that the page names (None when it names
    none), and whether today's rebuild raises when the media ledger does not confirm it.
    """

    binding: Binding | None = None
    side: SideRow | None = None
    displaced: tuple[originals.DisplacedReference, Binding | None, bool] | None = None


class _KeptChanging(ValueError):
    """A page that the store does not own changed again when the walk read it once more (D3)."""


class _Displaced(Exception):
    """A follow or a delta met a displaced page: the trail cannot express it as binding rows (design D7)."""

    def __init__(self, path: str) -> None:
        super().__init__("a page away from its original changed")
        self.path = path


def _reason(error: BaseException) -> str:
    from .media_allowance import REASON_MAX_CHARS

    return str(error)[:REASON_MAX_CHARS] or type(error).__name__


def _unknown(rel: str, error: BaseException, *, flagged: bool, owned: bool = True,
             binary_rel: str | None = None) -> SideRow:
    return SideRow(rel, False, flagged=flagged and owned, binary_rel=binary_rel, owned=owned, reason=_reason(error))


def classify(vault: Path, rel: str, data: bytes, *, selected: Basis,
             placements: trash_metadata.PlacementCensus, include_retired: bool = False,
             owned: bool = True) -> PageFacts:
    """The per-page rule that the owner census and the projection share (design D3), for one page's bytes.

    A page that the store does not own (``owned`` false) gets no binding row and no eviction flag. Raises a class
    in `RACES` for a race or a read failure; a deterministic fault of the page itself is its side row.
    """
    from .media_allowance import page_reference

    try:
        fields, _body, marker = _parse(data)
    except RACES:
        raise
    except (ValueError, UnicodeError) as error:
        return PageFacts(side=_unknown(rel, error, flagged=True, owned=owned))
    binding = None
    if owned:
        try:
            binding = _derive(vault, rel, data, fields, marker, selected=selected, placements=placements,
                              include_retired=include_retired, displaced=False)
        except originals.DisplacedReference as error:
            try:
                found = _derive(vault, rel, data, fields, marker, selected=selected, placements=placements,
                                include_retired=include_retired, displaced=True)
            except RACES:
                raise
            except (ValueError, UnicodeError) as fault:
                return PageFacts(side=_unknown(rel, fault, flagged=True))
            return PageFacts(displaced=(error, found, found is not None))
        except RACES:
            raise
        except (ValueError, UnicodeError) as error:
            return PageFacts(side=_unknown(rel, error, flagged=True))
    try:
        counted = None if selected.store is None else page_reference(rel, fields, selected.store, placements)
    except originals.DisplacedReference as error:
        return PageFacts(binding=binding, displaced=(error, None, False))
    except RACES:
        raise
    except (ValueError, UnicodeError) as error:
        return PageFacts(binding=binding, side=_unknown(rel, error, flagged=False, owned=owned))
    if counted is None:
        return PageFacts(binding=binding)
    side = _counted(rel, *counted, owned=owned)
    if binding is not None and (binding.binary_rel, binding.sha256, binding.size, binding.reference) == (
            side.binary_rel, side.sha256, side.size, side.reference):
        return PageFacts(binding=binding)
    return PageFacts(binding=binding, side=side)


def _counted(rel: str, binary: originals.BinaryBinding, reference: originals.StoredObject, *, owned: bool = True,
             displaced: bool = False) -> SideRow:
    return SideRow(rel, True, displaced=displaced, object=reference.object, version=reference.version,
                   sha256=binary.sha256, size=binary.size, reference=json.dumps(reference.fields(), sort_keys=True),
                   binary_rel=binary.path, owned=owned)


def _owned_page(rel: str) -> bool:
    from .vault_store import ownership

    return ownership.classify(rel) is ownership.Ownership.PAGE


def _skipped(rel: str) -> bool:
    """A page that the census walk never reads: not Markdown, or in a reserved family (D3)."""
    return not rel.lower().endswith(".md") or reserved_paths.classify_logical(rel).blocked


@dataclass(frozen=True, slots=True)
class _Round:
    """The rows that one follow round, or an upload's suffix, derives: what it replaces and what it writes."""

    paths: tuple[str, ...]
    companions: tuple[str, ...]
    bindings: tuple[Binding, ...]
    sides: tuple[SideRow, ...]


def _rows_naming(conn: sqlite3.Connection, binaries: Iterable[str]) -> set[str]:
    """The pages whose binding row or duplicate-filing row names one of ``binaries``, in batches of 256."""
    binaries = tuple(dict.fromkeys(binaries))
    found: set[str] = set()
    if not binaries or not _created(conn):
        return found
    side = _side_table(conn)
    for start in range(0, len(binaries), 256):
        batch = binaries[start:start + 256]
        marks = ",".join("?" for _ in batch)
        found.update(row[0] for row in conn.execute(
            f"SELECT companion_rel FROM media_bindings WHERE binary_rel IN ({marks})", batch))
        if side:
            found.update(row[0] for row in conn.execute(
                "SELECT companion_rel FROM media_reference_rows WHERE counted = 0 AND flagged = 1 AND owned = 1 "
                f"AND binary_rel IN ({marks})", batch))
    return found


def _published_binaries(conn: sqlite3.Connection, companions: Iterable[str]) -> set[str]:
    companions = tuple(companions)
    found: set[str] = set()
    if not companions or not _created(conn):
        return found
    side = _side_table(conn)
    for start in range(0, len(companions), 256):
        batch = companions[start:start + 256]
        marks = ",".join("?" for _ in batch)
        found.update(row[0] for row in conn.execute(
            f"SELECT binary_rel FROM media_bindings WHERE companion_rel IN ({marks})", batch))
        if side:
            found.update(row[0] for row in conn.execute(
                "SELECT binary_rel FROM media_reference_rows WHERE counted = 0 AND flagged = 1 "
                f"AND binary_rel IS NOT NULL AND companion_rel IN ({marks})", batch))
    return found


def _derive_round(vault: Path, conn: sqlite3.Connection, view, paths: Iterable[str], selected: Basis) -> _Round:
    """Derive the rows of each page that ``paths`` names or changes, from the pinned store snapshot ``view``.

    The caller pins ``view`` (`vault_store.read.pinned`), so every page and binary that the store owns is read from
    it. Two companions that bind one original are read together: each page whose row names an original that a
    read page names is read too, and each of them gets a flagged unknown row. It writes nothing. Raises
    `_Displaced` for a displaced page, and a class in `RACES` for a race or a read failure.
    """
    paths = tuple(dict.fromkeys(paths))
    read_pages: dict[str, PageFacts] = {}
    placements = trash_metadata.PlacementCensus(vault)
    pending = {rel for rel in paths if rel.lower().endswith(".md")} | _rows_naming(conn, paths)
    while pending:
        bodies = view.bodies(sorted(rel for rel in pending if not _skipped(rel)))
        for rel in sorted(pending):
            data = bodies.get(rel)
            facts = PageFacts() if data is None or _skipped(rel) else classify(
                vault, rel, data, selected=selected, placements=placements)
            if facts.displaced is not None:
                raise _Displaced(rel)
            read_pages[rel] = facts
        named = {facts.binding.binary_rel for rel in pending if (facts := read_pages[rel]).binding is not None}
        named |= _published_binaries(conn, pending)
        pending = _rows_naming(conn, named) - read_pages.keys()
    placements.require_current()
    filings: dict[str, list[str]] = defaultdict(list)
    for rel, facts in read_pages.items():
        if facts.binding is not None:
            filings[facts.binding.binary_rel].append(rel)
    bindings, sides = [], []
    for rel, facts in sorted(read_pages.items()):
        binding, side = facts.binding, facts.side
        if binding is not None and len(filings[binding.binary_rel]) > 1:
            binding, side = None, _unknown(rel, ValueError("canonical companions disagree about one filing"),
                                           flagged=True, binary_rel=facts.binding.binary_rel)
        if binding is not None:
            bindings.append(binding)
        if side is not None:
            sides.append(side)
    return _Round(tuple(sorted(set(paths) | read_pages.keys())), tuple(sorted(read_pages)), tuple(bindings),
                  tuple(sides))


def _side_values(row: SideRow) -> tuple:
    return (row.companion_rel, int(row.counted), int(row.flagged), int(row.displaced), row.object, row.version,
            row.sha256, row.size, row.reference, row.binary_rel, int(row.owned), row.reason)


def _put_sides(conn: sqlite3.Connection, rows: Iterable[SideRow], *, table: str = "media_reference_rows") -> None:
    conn.executemany(f"INSERT OR REPLACE INTO {table} ({_SIDE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (_side_values(row) for row in rows))


def _write_round(conn: sqlite3.Connection, derived: _Round) -> None:
    conn.executemany("DELETE FROM media_bindings WHERE companion_rel = ? OR binary_rel = ?",
                     ((rel, rel) for rel in derived.paths))
    _put(conn, derived.bindings)
    conn.executemany("DELETE FROM media_reference_rows WHERE companion_rel = ?", ((rel,) for rel in derived.companions))
    _put_sides(conn, derived.sides)
    coalesce_cache(conn, (binding.object_key for binding in derived.bindings))


def _publish(conn: sqlite3.Connection, before: Checkpoint, state: Checkpoint, reached: AuditRow) -> None:
    """Record a publication's checkpoint and the row it reached; the side table keeps step only if it was in step."""
    _record(conn, state)
    _put_meta(conn, _IDENTITY, asdict(reached))
    if side_generation(conn) == before.generation:
        _put_meta(conn, _SIDE, state.generation)


def _still(vault: Path, row: AuditRow) -> bool:
    """Whether the store's trail still holds ``row`` at its position; the head may have moved past it."""
    from .vault_store import read

    with read.snapshot(vault, join_request=False) as view:
        return _audit_row(view.connection, row.seq) == row


def follow(vault: Path, *, batch: int = REPLAY_ROWS) -> Followed:
    """Replay one round of at most ``batch`` audit rows in store mode, and publish it in one ledger transaction.

    It leaves a projection that is not complete, records another policy or no audit position alone, for a
    rebuild (state REBUILD). It withdraws on a restore, a broken trail or a row without a path. A displaced page
    withdraws too (D7). A race or a read failure leaves the projection lagging (LAGGING) and names its cause.
    """
    from .vault_store import mode, read

    vault = Path(vault).resolve()
    ledger = _ledger(vault, create=False)
    if not ledger.path.exists() or not mode.store_mode_for_reads(vault):
        return Followed(REBUILD, reason="the projection follows only a vault in store mode")
    with closing(ledger._connect(readonly=True)) as conn:
        before, identity = checkpoint(conn), _identity(conn)
    if not before.complete or before.seq is None or before.policy != policy_key(vault):
        return Followed(REBUILD, reason="the projection needs a rebuild")
    with read.snapshot(vault, join_request=False) as view:
        head = _head(view)
        if before.seq > head or before.revision is None or before.revision > view.revision:
            withdraw(vault)
            return Followed(REBUILD, reason="the store is behind the projection")
        if identity is None or identity.seq != before.seq or _audit_row(view.connection, before.seq) != identity:
            withdraw(vault)
            return Followed(REBUILD, reason="the audit trail no longer holds the row that the projection followed")
        if before.seq == head:
            return Followed(HEAD, head, 0)
        rows = view.connection.execute(
            "SELECT seq, revision, at, path, from_path FROM store_audit WHERE seq > ? ORDER BY seq LIMIT ?",
            (before.seq, batch)).fetchall()
        if any(row[3] is None for row in rows):
            withdraw(vault)
            return Followed(REBUILD, reason="a migration or a rollback replaced the store's pages")
        reached = AuditRow(*rows[-1][:4])
        paths = tuple({path for row in rows for path in row[3:] if path is not None})
        try:
            with read.pinned(vault, view), closing(ledger._connect(readonly=True)) as conn:
                derived = _derive_round(vault, conn, view, paths, basis(vault))
        except _Displaced as error:
            log.info("media binding follow met a displaced page %s; the projection waits for a rebuild", error.path)
            withdraw(vault)
            return Followed(REBUILD, reason="a page away from its original changed")
        except RACES as error:
            where = getattr(error, "path", None)
            log.info("media binding follow lags at %s: %s", where or "the store", _reason(error))
            return Followed(LAGGING, before.seq, head - before.seq, _reason(error) if where is None
                            else f"{where}: {_reason(error)}")
        from .media_store import _guard

        with _guard(vault, "media_binding_audit_publish"), closing(ledger._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if (checkpoint(conn) != before or _identity(conn) != identity or policy_key(vault) != before.policy
                    or not _still(vault, reached)):
                return Followed(RACED, before.seq, head - before.seq, "another writer moved the projection")
            _write_round(conn, derived)
            _publish(conn, before, replace(before, generation=before.generation + 1, revision=view.revision,
                                           seq=reached.seq), reached)
        return Followed(ADVANCED if reached.seq < head else HEAD, reached.seq, head - reached.seq)


def follow_store(vault: Path, *, batch: int = REPLAY_ROWS) -> bool:
    """Replay one bounded audit suffix in store mode; True when the projection reached the head it read."""
    return follow(vault, batch=batch).state == HEAD


def follow_until(vault: Path, *, lag: int = 0, rounds: int | None = None) -> Followed:
    """Follow round after round until the projection lags the head by at most ``lag`` rows.

    It stops at a round that cannot advance, and after ``rounds`` rounds when given. ``recover`` passes none, and
    follows to the head that it read when it started: rows that land meanwhile are the next pass's.
    """
    from .vault_store import read

    with read.snapshot(Path(vault).resolve(), join_request=False) as view:
        target = _head(view)
    count = 0
    while True:
        followed = follow(vault)
        count += 1
        if followed.state not in (ADVANCED, HEAD) or followed.lag is None:
            return followed
        if followed.lag <= lag or (followed.reached is not None and followed.reached >= target):
            return followed
        if rounds is not None and count >= rounds:
            return followed


def _pages(
    vault: Path, *, join_request: bool = False, headers_only: bool = False,
    changed: Callable[[str, str], None] | None = None,
):
    """Every canonical page under the managed root. A census reads a fresh snapshot; a request may join its own.

    A disk walk refuses a tree that changes while it reads it, unless the caller passes `changed`: then
    a page added or removed elsewhere is no failure, and a page that is replaced, removed or edited while
    the walk reads it is read once more after the walk, and so is a page that a save gave a second name for a
    moment. A page that is gone by then is no page. A page that changes again during that read, and a folder
    that left during the walk, go to `changed` with their path and kind ("file" or "directory") instead of
    being yielded; the folder's pages may sit under another name now. Each page it yields was
    confirmed unchanged after its read. Either way the walk refuses a page it cannot read safely, such as a
    linked file or folder, rather than leave it out.
    """
    from contextlib import nullcontext

    from .kbdir import managed_join, managed_root_rel
    from .runtime_resources import SEMANTIC_SOURCE_MAX_BYTES
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
        flagged: dict[str, str] = {}  # each entry the walk saw change, with its kind, in walk order

        def is_page(rel: str) -> bool:
            return rel.lower().endswith(".md")

        with reserved_paths.open_generic_tree(
            vault, managed_root_rel(), require_complete=True,
            select=lambda candidates: (rel for rel, kind in candidates if kind == "file" and is_page(rel)),
            markdown_header_limit=SEMANTIC_SOURCE_MAX_BYTES if headers_only else None,
            entry_changed=None if changed is None else (
                lambda relative, kind: flagged.setdefault(managed_join(relative), kind)
            ),
        ) as entries:
            if changed is None:
                for entry in entries:
                    rel = managed_join(entry.relative_path)
                    if not read.serves(vault, rel):
                        if not headers_only:
                            yield rel, reserved_paths.read_generic_bytes(vault, rel).data
                        elif entry.markdown is None:
                            raise ValueError("a companion header cannot be read")
                        else:
                            yield rel, entry.markdown
                return
            # Each page waits until the walk moves on, which rechecks it, so the caller never takes a stale read.
            waiting: tuple[str, bytes] | None = None
            for entry in entries:
                if waiting is not None and waiting[0] not in flagged:
                    yield waiting
                rel = managed_join(entry.relative_path)
                waiting = None
                if not read.serves(vault, rel):
                    if entry.markdown is None:
                        raise ValueError("a page cannot be read")
                    waiting = (rel, entry.markdown)
            if waiting is not None and waiting[0] not in flagged:
                yield waiting
        for rel, kind in flagged.items():
            if kind == "directory":
                changed(rel, kind)
                continue
            if read.serves(vault, rel):
                continue
            page = is_page(rel)
            try:
                with reserved_paths.open_generic_binary(vault, rel, physical=True) as source:
                    data = source.stream.read() if page else b""
            except reserved_paths.ReservedPathLeafError as error:
                if error.code == "RESERVED_PATH":
                    # It kept the second name that the walk saw: the walk's own refusal of a linked file.
                    raise reserved_paths.ReservedPathLeafError("UNSAFE_PATH") from error
                if error.code not in reserved_paths.CHANGE_CODES:
                    raise
                if error.code == "IDENTITY_CHANGED" and page:
                    changed(rel, kind)  # it changed again while it was read
                continue  # a page that left is no page, and a photo is counted through its page
            if page:
                yield rel, data


def _store_bindings(vault: Path) -> Iterator[tuple[str | None, tuple[str, ...]]]:
    """Read canonical stored-object and replica names, including retired and moved companions."""
    from .vault import parse_frontmatter

    with closing(_pages(vault, headers_only=True)) as pages:
        for _path, data in pages:
            fields = parse_frontmatter(data.decode("utf-8"), custody=True)[0]
            primary = None
            if "stored_object" in fields:
                pointer = originals.stored_reference(fields["stored_object"])
                if pointer is None:
                    raise ValueError("a stored original reference cannot be read")
                primary = pointer.store
            replicas = fields.get("replicas", [])
            if not isinstance(replicas, list):
                raise ValueError("a replica list cannot be read")
            stores = []
            for raw in replicas:
                replica = originals.stored_reference(raw)
                if replica is None:
                    raise ValueError("a replica reference cannot be read")
                stores.append(replica.store)
            yield primary, tuple(stores)


def store_role_conflict(vault: Path, selected: str, target: str) -> str | None:
    with closing(_store_bindings(vault)) as bindings:
        for primary, replicas in bindings:
            if target != selected and primary == selected:
                return "the selected store still serves a stored original"
            if target in replicas:
                return "the requested store already holds a replica"
    return None


def store_is_bound(vault: Path, name: str) -> bool:
    with closing(_store_bindings(vault)) as bindings:
        return any(name == primary or name in replicas for primary, replicas in bindings)


def source_checkpoint(vault: Path) -> tuple[int | None, int | None]:
    from .vault_store import mode, read

    if not mode.store_mode_for_reads(vault):
        return None, None
    with read.snapshot(vault, join_request=False) as view:
        return view.revision, view.connection.execute("SELECT COALESCE(MAX(seq), 0) FROM store_audit").fetchone()[0]


def current(vault: Path, conn: sqlite3.Connection, *, kind: str = EVICTION) -> bool:
    """Check the source frontier under the caller's canonical guard before using object facts.

    In store mode the frontier is the audit head (design D8), and ``kind`` adds what its caller needs: EVICTION no
    flagged unknown row, ALLOWANCE a side table in step with the checkpoint, and CURSOR (for `recover` and upload
    admission) that side table and the followed row's identity, without the head. In file mode it is unchanged.
    """
    from . import freshness
    from .vault_store import mode, read

    state = checkpoint(conn)
    if not mode.store_mode_for_reads(vault):
        return (state.complete and not freshness.external_pending(vault) and state.policy == policy_key(vault)
                and (state.revision, state.seq) == source_checkpoint(vault))
    if not state.complete or state.seq is None or state.policy != policy_key(vault):
        return False
    identity = _identity(conn)
    if identity is None or identity.seq != state.seq:
        return False
    if kind in (ALLOWANCE, CURSOR) and side_generation(conn) != state.generation:
        return False
    if kind == EVICTION and flagged_pages(conn, limit=1):
        return False
    with read.snapshot(vault, join_request=False) as view:
        if _audit_row(view.connection, state.seq) != identity:
            return False
        return kind == CURSOR or _head(view) == state.seq


def external_change(vault: Path) -> None:
    """Fence an observed outside edit before its existing watcher debounce. In store mode the trail names it (D7)."""
    from .vault_store import mode

    if mode.store_mode_for_reads(vault):
        return
    ledger = _ledger(vault, create=False)
    if ledger.path.exists() and _tracks_bindings(vault, ledger):
        withdraw(vault)


def repair_paths(vault: Path, paths: Iterable[str]) -> None:
    """Repair exact receipt paths; an incomplete inventory still needs its background census.

    It never raises into the custody seam (D7): an error here costs only this optional projection. In store mode
    the follow replays the trail; in file mode a failed derivation withdraws completeness.
    """
    active = _operation.get()
    if active is not None and active.vault == Path(vault).resolve():
        active.paths.update(paths)
        return  # The operation's own index receipts join its one delta instead of withdrawing again.
    try:
        ledger = _ledger(vault, create=False)
        if not ledger.path.exists() or not _tracks_bindings(vault, ledger):
            return
        from .vault_store import mode

        if mode.store_mode_for_reads(vault):
            follow(vault)
            return
    except Exception:  # noqa: BLE001 - a follow that fails lags; the next follow replays the same rows
        log.warning("physical binding follow failed; the next follow replays the rows", exc_info=True)
        return
    try:
        _repair_files(vault, ledger, paths)
    except Exception:  # noqa: BLE001 - derived failure must not fail the custody seam
        log.warning("physical binding repair failed; background recovery will rebuild", exc_info=True)
        try:
            withdraw(vault)
        except Exception:  # noqa: BLE001 - a ledger that cannot record the withdrawal keeps its old generation
            log.warning("physical binding withdrawal failed", exc_info=True)


def _repair_files(vault: Path, ledger, paths: Iterable[str]) -> None:
    with closing(ledger._connect(readonly=True)) as conn:
        before = checkpoint(conn)
    try:
        placement = basis(vault)
    except (OSError, ValueError, RecursionError):
        if before.complete:
            withdraw(vault)
        return
    delta = prepare_delta(vault, paths, selected=placement)
    with closing(ledger._connect(readonly=True)) as conn:
        selected = []
        refs = (", p.refs AS policy_refs FROM media_bindings b LEFT JOIN media_policy_filings p "
                "ON p.binary_rel = b.binary_rel AND p.companion_hash = b.companion_hash"
                if placement.policy_projection and _has_policy_projection(conn) else " FROM media_bindings b")
        for start in range(0, len(delta.paths), 256):
            batch = delta.paths[start:start + 256]
            placeholders = ",".join("?" for _ in batch)
            selected.extend(Binding(**dict(row)) for row in conn.execute(
                f"SELECT b.*{refs} WHERE b.companion_rel IN ({placeholders})", batch))
        if sorted(selected, key=lambda item: item.binary_rel) == sorted(delta.bindings, key=lambda item: item.binary_rel):
            return
    withdrawn = withdraw(vault)
    if withdrawn == before:
        apply_delta(vault, delta.paths, delta.bindings, expected=before)


def rebuild(vault: Path) -> bool | None:
    """A startup/background census; no request or pin admission calls this walk.

    True when it publishes a complete projection. None when a write, an outside change or a configuration change
    raced it: it publishes nothing and leaves the checkpoint as the race left it. False when it failed: it then
    withdraws completeness, which moves the generation, and records the failure in the media ledger (D4).
    """
    from .vault_store import mode

    vault = Path(vault).resolve()
    ledger = _ledger(vault)
    if mode.store_mode_for_reads(vault):
        return _rebuild_store(vault, ledger)
    published = _rebuild_files(vault, ledger)
    if published is False:
        _record_failure(vault, ())
    return published


def _rebuild_files(vault: Path, ledger) -> bool | None:
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
        conn.execute("CREATE TEMP TABLE binding_census AS "
                     "SELECT *, CAST(NULL AS TEXT) AS policy_refs FROM media_bindings WHERE 0")
        conn.execute("CREATE INDEX temp.binding_census_companion ON binding_census(companion_rel)")
        conn.execute("CREATE TEMP TABLE displaced_census AS SELECT * FROM binding_census WHERE 0")
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
                    conn.execute("INSERT INTO binding_census VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                 (*_binding_values(binding), binding.policy_refs))
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
                conn.execute(f"INSERT INTO media_bindings SELECT {_BINDING_COLUMNS} FROM binding_census")
                if selected.policy_projection:
                    conn.execute("INSERT INTO media_policy_filings SELECT binary_rel, companion_hash, policy_refs "
                                 "FROM binding_census WHERE policy_refs IS NOT NULL")
                coalesce_cache(conn)
                _record(conn, Checkpoint(before.generation + 1, True, *source, policy,
                                         policy_projection=selected.policy_projection))
                conn.execute("DELETE FROM meta WHERE key = ?", (_FAILED,))
                conn.commit()
                return True
        except (ValueError, UnicodeError, OSError, reserved_paths.ReservedPathLeafError, sqlite3.Error):
            conn.rollback()
    withdraw(vault)
    return False


def _census_difference(conn: sqlite3.Connection, *, filings: bool) -> None:
    """Take the difference between a store-mode census and the published rows, outside the hold (design, 3.4).

    A whole replacement under the hold costs one pass over every binding and every index, which measured 1.4 s
    and 3.0 s at 100,000 bindings on 2026-11-28 against a budget of 0.5 s. The temp tables hold the published
    rows that the census drops or replaces, and the census rows that are new or changed. A binding that is
    replaced drops its policy filing by the cascade, so `filing_touch` names it for writing again.
    """
    census_filings = ("SELECT binary_rel, companion_hash, policy_refs FROM binding_census WHERE policy_refs IS NOT NULL"
                      if filings else "SELECT binary_rel, companion_hash, refs FROM media_policy_filings WHERE 0")
    published_filings = "SELECT binary_rel, companion_hash, refs FROM media_policy_filings"
    conn.execute("BEGIN")  # one read of the published rows
    for table in ("binding_gone", "binding_new", "side_gone", "side_new", "filing_touch"):
        conn.execute(f"DROP TABLE IF EXISTS temp.{table}")
    conn.execute(f"CREATE TEMP TABLE binding_gone AS SELECT binary_rel, object_key FROM (SELECT {_BINDING_COLUMNS} "
                 f"FROM media_bindings EXCEPT SELECT {_BINDING_COLUMNS} FROM binding_census)")
    conn.execute(f"CREATE TEMP TABLE binding_new AS SELECT {_BINDING_COLUMNS} FROM binding_census "
                 f"EXCEPT SELECT {_BINDING_COLUMNS} FROM media_bindings")
    conn.execute(f"CREATE TEMP TABLE side_gone AS SELECT companion_rel FROM (SELECT {_SIDE_COLUMNS} "
                 f"FROM media_reference_rows EXCEPT SELECT {_SIDE_COLUMNS} FROM side_census)")
    conn.execute(f"CREATE TEMP TABLE side_new AS SELECT {_SIDE_COLUMNS} FROM side_census "
                 f"EXCEPT SELECT {_SIDE_COLUMNS} FROM media_reference_rows")
    conn.execute(f"CREATE TEMP TABLE filing_touch AS SELECT binary_rel FROM ({published_filings} EXCEPT {census_filings}) "
                 f"UNION SELECT binary_rel FROM ({census_filings} EXCEPT {published_filings}) "
                 "UNION SELECT binary_rel FROM binding_gone")
    conn.commit()


def _write_difference(conn: sqlite3.Connection, *, filings: bool) -> None:
    """Write the difference that `_census_difference` took, so the published rows equal the census's."""
    conn.execute("DELETE FROM media_bindings WHERE binary_rel IN (SELECT binary_rel FROM binding_gone)")
    conn.execute("DELETE FROM media_policy_filings WHERE binary_rel IN (SELECT binary_rel FROM filing_touch)")
    conn.execute(f"INSERT INTO media_bindings SELECT {_BINDING_COLUMNS} FROM binding_new")
    if filings:
        conn.execute("INSERT INTO media_policy_filings SELECT binary_rel, companion_hash, policy_refs "
                     "FROM binding_census WHERE policy_refs IS NOT NULL "
                     "AND binary_rel IN (SELECT binary_rel FROM filing_touch)")
    conn.execute("DELETE FROM media_reference_rows WHERE companion_rel IN (SELECT companion_rel FROM side_gone)")
    conn.execute(f"INSERT INTO media_reference_rows ({_SIDE_COLUMNS}) SELECT {_SIDE_COLUMNS} FROM side_new")
    coalesce_cache(conn)


def _rebuild_store(vault: Path, ledger) -> bool | None:
    """Rebuild in store mode from one store snapshot, outside the boundary, and publish at its audit position (D6).

    A deterministic fault of one page is that page's unknown row, and the rebuild publishes with it (D3). A race
    or a read failure fails the whole rebuild, which withdraws and records up to five blockers (D4).
    """
    from .kbdir import managed_join
    from .media_store import _guard
    from .vault_store import read

    with closing(ledger._connect(readonly=True)) as conn:
        before = checkpoint(conn)
    reading: list[str] = []  # the page or file being read, which a whole failure names
    try:
        policy, selected = policy_key(vault), basis(vault)
        with (read.snapshot(vault, join_request=False) as view, read.pinned(vault, view),
              closing(ledger._connect()) as conn):
            head = _head(view)
            reached = _audit_row(view.connection, head)
            conn.execute("CREATE TEMP TABLE binding_census AS "
                         "SELECT *, CAST(NULL AS TEXT) AS policy_refs FROM media_bindings WHERE 0")
            conn.execute("CREATE INDEX temp.binding_census_companion ON binding_census(companion_rel)")
            conn.execute("CREATE TEMP TABLE displaced_census AS SELECT * FROM binding_census WHERE 0")
            conn.execute(f"CREATE TEMP TABLE side_census AS SELECT {_SIDE_COLUMNS} FROM media_reference_rows WHERE 0")
            conn.execute("CREATE UNIQUE INDEX temp.side_census_companion ON side_census(companion_rel)")
            placements = trash_metadata.PlacementCensus(vault)
            displaced: list[tuple[str, originals.DisplacedReference, Binding | None, bool, bool]] = []

            def take(rel: str, facts: PageFacts, *, owned: bool) -> None:
                if facts.binding is not None:
                    conn.execute("INSERT INTO binding_census VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                 (*_binding_values(facts.binding), facts.binding.policy_refs))
                if facts.side is not None:
                    _put_sides(conn, [facts.side], table="side_census")
                if facts.displaced is not None:
                    displaced.append((rel, *facts.displaced, owned))

            def fault(rel: str, binary: str | None, error: Exception) -> None:
                _put_sides(conn, [_unknown(rel, error, flagged=True, binary_rel=binary)], table="side_census")

            prefix = managed_join("")
            for rel, data in view.connection.execute(
                    "SELECT path, body FROM store_pages WHERE path >= ? AND path < ? ORDER BY path",
                    (prefix, prefix + "\U0010ffff")):
                if _skipped(rel):
                    continue
                reading[:] = [rel]
                take(rel, classify(vault, rel, data, selected=selected, placements=placements, include_retired=True),
                     owned=True)
            for rel, data, error in _left_out(vault, reading):
                if data is None:
                    _put_sides(conn, [_unknown(rel, error, flagged=False, owned=False)], table="side_census")
                else:
                    take(rel, classify(vault, rel, data, selected=selected, placements=placements, owned=False),
                         owned=False)
            reading.clear()
            _confirm_pages(vault, conn, displaced, selected)
            _retire_census(vault, conn, placements, fault=fault)
            _fill_displaced(conn)  # A retired filing that a confirmed page still names stays counted.
            for (companion,) in conn.execute(
                    "SELECT DISTINCT b.companion_rel FROM media_bindings b "
                    "WHERE NOT EXISTS(SELECT 1 FROM binding_census n "
                    "WHERE n.companion_rel = b.companion_rel AND n.binary_rel = b.binary_rel) "
                    "AND NOT EXISTS(SELECT 1 FROM displaced_census d "
                    "WHERE d.companion_rel = b.companion_rel AND d.binary_rel = b.binary_rel) "
                    "AND NOT EXISTS(SELECT 1 FROM retired_census r "
                    "WHERE r.companion_rel = b.companion_rel AND r.binary_rel = b.binary_rel) "
                    "AND NOT EXISTS(SELECT 1 FROM side_census s "
                    "WHERE s.companion_rel = b.companion_rel AND s.counted = 0)").fetchall():
                if _page_exists(vault, companion):
                    fault(companion, None, ValueError("an extant companion lost its original binding"))
            for (binary,) in conn.execute("SELECT binary_rel FROM binding_census GROUP BY binary_rel "
                                          "HAVING COUNT(*) > 1").fetchall():
                for (companion,) in conn.execute("SELECT companion_rel FROM binding_census WHERE binary_rel = ?",
                                                 (binary,)).fetchall():
                    fault(companion, binary, ValueError("canonical companions disagree about one filing"))
                conn.execute("DELETE FROM binding_census WHERE binary_rel = ?", (binary,))
            placements.require_current()
            conn.commit()
            _census_difference(conn, filings=selected.policy_projection)
            with _guard(vault, "media_binding_census_publish"):
                conn.execute("BEGIN IMMEDIATE")
                # Every writer of the rows moves the checkpoint, so an unmoved checkpoint proves that the rows
                # are still the ones that the difference was taken from.
                if checkpoint(conn) != before or policy_key(vault) != policy or not _still(vault, reached):
                    conn.rollback()
                    return None
                _write_difference(conn, filings=selected.policy_projection)
                state = Checkpoint(before.generation + 1, True, view.revision, head, policy,
                                   policy_projection=selected.policy_projection)
                _record(conn, state)
                _put_meta(conn, _IDENTITY, asdict(reached))
                _put_meta(conn, _SIDE, state.generation)
                conn.execute("DELETE FROM meta WHERE key = ?", (_FAILED,))
                conn.commit()
    except (*RACES, ValueError, UnicodeError) as error:
        where = getattr(error, "path", None) or (reading[0] if reading else "")
        log.warning("media binding rebuild failed at %s: %s", where or "the store", _reason(error))
        withdraw(vault)
        _record_failure(vault, [(where, _reason(error))] if where else [])
        return False
    follow_until(vault)
    return True


def _confirm_pages(vault: Path, conn: sqlite3.Connection,
                   displaced: list[tuple[str, originals.DisplacedReference, Binding | None, bool, bool]],
                   selected: Basis) -> None:
    """Count each displaced page that the media ledger confirms, and give each other one its unknown row (D3).

    The flag is set where today's rebuild raises: on a page whose own binding is displaced. A page that the
    store does not own never binds a row.
    """
    if not displaced:
        return
    from .media_store import recorded_versions

    confirmed = recorded_versions(vault, [(error.binary, error.reference) for _rel, error, *_rest in displaced])
    for (rel, error, found, flag, owned), recorded in zip(displaced, confirmed, strict=True):
        if not recorded:
            _put_sides(conn, [_unknown(rel, ValueError("a page away from its original binds a version that the "
                                                       "media ledger does not record"), flagged=flag, owned=owned)],
                       table="side_census")
            continue
        if found is not None and owned:
            conn.execute("INSERT INTO displaced_census VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (*_binding_values(found), found.policy_refs))
        if error.reference.store == selected.store:
            _put_sides(conn, [_counted(rel, error.binary, error.reference, owned=owned, displaced=True)],
                       table="side_census")
    _fill_displaced(conn)


def _page_bytes(vault: Path, rel: str) -> bytes:
    """A page's bytes: from the pinned store snapshot for a page that the store owns (D3), else from the disk."""
    from .vault_store import read

    view = read.pinned_view(vault)
    if view is not None and _owned_page(rel):
        data = view.bodies([rel]).get(rel)
        if data is None:
            raise reserved_paths.ReservedPathLeafError("MISSING")
        return data
    return reserved_paths.read_generic_bytes(vault, rel).data


def _page_exists(vault: Path, rel: str) -> bool:
    try:
        _page_bytes(vault, rel)
    except reserved_paths.ReservedPathLeafError as error:
        if error.code != "MISSING":
            raise
        return False
    return True


def _left_out(vault: Path, reading: list[str]) -> Iterator[tuple[str, bytes | None, BaseException | None]]:
    """Each page under the Knowledge Base that the store does not own, read from the disk by the census's reader.

    It yields a page's header, or the error that makes it unknown: a header that cannot be read, or a page that
    changed again when it was read once more after the walk. A change to a page or a folder that the store owns
    is the trail's to report. Raises a class in `RACES` when the walk fails, or a folder that it does not own
    changes during the walk.
    """
    from .kbdir import managed_join, managed_root_rel
    from .runtime_resources import SEMANTIC_SOURCE_MAX_BYTES
    from .vault_store import ownership

    failed: list[tuple[str, BaseException]] = []
    changed: dict[str, str] = {}

    def outside(rel: str) -> bool:
        return rel.lower().endswith(".md") and not _owned_page(rel)

    def select(candidates: list[tuple[str, str]]) -> Iterator[str]:
        return (relative for relative, kind in candidates if kind == "file" and outside(managed_join(relative)))

    def entry_changed(relative: str, kind: str) -> None:
        rel = managed_join(relative)
        if (not ownership.owns_folder(rel)) if kind == "directory" else outside(rel):
            changed.setdefault(rel, kind)

    with reserved_paths.open_generic_tree(
        vault, managed_root_rel(), select=select, require_complete=True,
        markdown_header_limit=SEMANTIC_SOURCE_MAX_BYTES,
        markdown_header_failed=lambda relative, error: failed.append((managed_join(relative), error)),
        entry_changed=entry_changed,
    ) as entries:
        waiting: tuple[str, bytes] | None = None  # held until the walk moves on, which rechecks it
        for entry in entries:
            if waiting is not None and waiting[0] not in changed:
                reading[:] = [waiting[0]]
                yield waiting[0], waiting[1], None
            rel = managed_join(entry.relative_path)
            waiting = None if entry.markdown is None else (rel, entry.markdown)
            if entry.markdown is None:
                failed.append((rel, ValueError("a page header cannot be read")))
        if waiting is not None and waiting[0] not in changed:
            reading[:] = [waiting[0]]
            yield waiting[0], waiting[1], None
    for rel, error in failed:
        yield rel, None, error
    for rel, kind in changed.items():
        reading[:] = [rel]
        if kind == "directory":
            raise reserved_paths.ReservedPathLeafError("IDENTITY_CHANGED")
        try:
            with reserved_paths.open_generic_binary(vault, rel, physical=True) as source:
                data = source.stream.read()
                source.require_current()
        except reserved_paths.ReservedPathLeafError as error:
            if error.code == "RESERVED_PATH":
                raise reserved_paths.ReservedPathLeafError("UNSAFE_PATH") from error
            if error.code not in reserved_paths.CHANGE_CODES:
                raise
            if error.code == "IDENTITY_CHANGED":
                yield rel, None, _KeptChanging("the page kept changing while it was read")
            continue  # a page that left is no page
        yield rel, data, None


def check_outside(vault: Path) -> bool:
    """Read the pages that the store does not own again, and publish the side rows that differ (D3, D4).

    The store sweep calls it. A check that fails, because a page kept changing or a read failed, keeps the
    projection as it is and runs again at the next sweep. True when the published rows match the pages.
    """
    vault = Path(vault).resolve()
    ledger = _ledger(vault, create=False)
    if not ledger.path.exists():
        return False
    with closing(ledger._connect(readonly=True)) as conn:
        before = checkpoint(conn)
        if not before.complete or not _side_table(conn):
            return False
        published = {row[0]: SideRow(row[0], bool(row[1]), bool(row[2]), bool(row[3]), *row[4:10], bool(row[10]),
                                     row[11])
                     for row in conn.execute(f"SELECT {_SIDE_COLUMNS} FROM media_reference_rows WHERE owned = 0")}
    try:
        selected = basis(vault)
        placements = trash_metadata.PlacementCensus(vault)
        rows: dict[str, SideRow] = {}
        displaced: list[tuple[str, originals.DisplacedReference, Binding | None, bool, bool]] = []
        for rel, data, error in _left_out(vault, []):
            if data is None:
                if isinstance(error, _KeptChanging):
                    return False
                rows[rel] = _unknown(rel, error, flagged=False, owned=False)
                continue
            facts = classify(vault, rel, data, selected=selected, placements=placements, owned=False)
            if facts.side is not None:
                rows[rel] = facts.side
            if facts.displaced is not None:
                displaced.append((rel, *facts.displaced, False))
        if displaced:
            from .media_store import recorded_versions

            confirmed = recorded_versions(vault, [(item[1].binary, item[1].reference) for item in displaced])
            for (rel, error, *_rest), recorded in zip(displaced, confirmed, strict=True):
                if not recorded:
                    rows[rel] = _unknown(rel, ValueError("a page away from its original binds a version that the "
                                                         "media ledger does not record"), flagged=False, owned=False)
                elif error.reference.store == selected.store:
                    rows[rel] = _counted(rel, error.binary, error.reference, owned=False, displaced=True)
        placements.require_current()
    except (*RACES, ValueError, UnicodeError) as error:
        log.info("media binding check of pages outside the store failed; the next sweep runs it: %s", _reason(error))
        return False
    if rows == published:
        return True
    from .media_store import _guard

    with _guard(vault, "media_binding_outside_publish"), closing(ledger._connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if checkpoint(conn) != before:
            return False
        conn.executemany("DELETE FROM media_reference_rows WHERE companion_rel = ? AND owned = 0",
                         ((rel,) for rel in published.keys() - rows.keys()))
        _put_sides(conn, (row for rel, row in rows.items() if published.get(rel) != row))
        state = replace(before, generation=before.generation + 1)
        _record(conn, state)
        if side_generation(conn) == before.generation:
            _put_meta(conn, _SIDE, state.generation)
    return True


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
    conn.executemany("INSERT INTO displaced_census VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     ((*_binding_values(binding), binding.policy_refs) for _error, binding in displaced))
    _fill_displaced(conn)


def _fill_displaced(conn: sqlite3.Connection) -> None:
    """Give each original that only confirmed displaced pages name one binding, the first in path order."""
    conn.execute("INSERT INTO binding_census SELECT d.* FROM displaced_census d "
                 "WHERE NOT EXISTS(SELECT 1 FROM binding_census b WHERE b.binary_rel = d.binary_rel) "
                 "AND d.companion_rel = (SELECT MIN(x.companion_rel) FROM displaced_census x "
                 "WHERE x.binary_rel = d.binary_rel)")


def _retire_census(vault: Path, conn: sqlite3.Connection, placements: trash_metadata.PlacementCensus, *,
                   fault: Callable[[str, str | None, Exception], None] | None = None) -> None:
    """Exclude only a completed, version-witnessed transfer to a current Trash binding.

    In store mode the rebuild passes ``fault``, which takes the page whose witness no longer holds instead of the
    raise, and pins its snapshot, from which the source companion and its original's presence are read (D3).
    """
    from .kbdir import managed_join

    def broken(rel: str, binary: str | None, error: ValueError) -> None:
        if fault is None:
            raise error
        fault(rel, binary, error)

    conn.execute("CREATE TEMP TABLE retired_census (binary_rel TEXT PRIMARY KEY, companion_rel TEXT NOT NULL)")
    prefix = managed_join(f"{trash_metadata.TRASH_SUBPATH}/")
    for row in conn.execute("SELECT * FROM binding_census WHERE binary_rel >= ? AND binary_rel < ? "
                            "AND reference IS NOT NULL ORDER BY binary_rel", (prefix, prefix + "\U0010ffff")).fetchall():
        retirement = placements.retirement(row["binary_rel"])
        if retirement is None:
            continue
        trash = retirement.trash
        if ((row["binary_rel"], row["sha256"], row["size"]) != (trash.path, trash.sha256, trash.size)
                or originals.stored_reference(json.loads(row["reference"])) != retirement.reference):
            broken(row["companion_rel"], None, ValueError("Trash retirement no longer has its exact recovery binding"))
            continue
        source = conn.execute("SELECT * FROM binding_census WHERE binary_rel = ? AND companion_rel = ?",
                              (retirement.source.path, retirement.companion)).fetchone()
        if trash_metadata.binary_exists(vault, retirement.source.path):
            continue  # An ordinary original at the old path is a new lifetime.
        if source is None:
            continue  # Canonical absence leaves no historical binding to exclude.
        if (source["sha256"], source["size"], source["companion_hash"], source["reference"]) != (
                retirement.source.sha256, retirement.source.size, retirement.after, None):
            broken(retirement.companion, None,
                   ValueError("the source retirement has not reached its witnessed companion state"))
            continue
        data = _page_bytes(vault, retirement.companion)
        fields = _parse(data)[0]
        if (hashlib.sha256(data).hexdigest() != retirement.after
                or fields.get("original_retirement_meta") != trash.path + trash_metadata.METADATA_SUFFIX):
            broken(retirement.companion, None, ValueError("the source companion has no reciprocal retirement witness"))
            continue
        conn.execute("INSERT INTO retired_census VALUES (?, ?)", (source["binary_rel"], source["companion_rel"]))
    conn.execute("DELETE FROM binding_census WHERE binary_rel IN (SELECT binary_rel FROM retired_census)")


# Per process: one census at a time. The media ledger records a failed one for every process (D4).
_census_lock = threading.Lock()
_rebuilds_refused = False


def refuse_rebuilds() -> None:
    """Mark this process as one that never rebuilds the projection: the media worker process (D4)."""
    global _rebuilds_refused
    _rebuilds_refused = True


def recover(vault: Path, *, sweep: bool = False) -> bool:
    """Make the physical projection current; True when eviction may use it.

    Background passes call this outside any guard, since the census takes its own; a request or an eviction never
    does. In store mode it first follows the trail to the head that it read when it started, and the store sweep
    passes ``sweep``, which also checks the pages that the store does not own. It rebuilds only for a reason that
    the projection owns (D4), never for lag or an unknown row, and never in the media worker process. A census
    that lost a race runs again on the next pass. A failed census runs again when the generation moves, once
    after start for a record from an earlier process, and in store mode at the next store sweep.
    """
    from .vault_store import mode

    vault = Path(vault).resolve()
    ledger = _ledger(vault, create=False)
    if not ledger.path.exists() or not _tracks_bindings(vault, ledger):
        return False
    with _census_lock:
        store = mode.store_mode_for_reads(vault)
        if store:
            follow_until(vault)
        with closing(ledger._connect(readonly=True)) as conn:
            ready = current(vault, conn, kind=CURSOR if store else EVICTION)
            generation, record = checkpoint(conn).generation, failed_rebuild(conn)
        if not ready:
            if _rebuilds_refused:
                return False
            if (record is not None and record.generation == generation and record.process == _PROCESS
                    and not (store and sweep)):
                return False
            if not rebuild(vault):  # A raced census (None) is not a failure: the next pass runs it again.
                return False
        elif store and sweep:
            check_outside(vault)
        with closing(ledger._connect(readonly=True)) as conn:
            return current(vault, conn)


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


def _has_policy_projection(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'media_policy_filings'").fetchone() is not None


def _policy_ready(vault: Path, conn: sqlite3.Connection) -> bool:
    try:
        return (_policy_enabled(originals.configuration(vault))
                and checkpoint(conn).policy_projection and current(vault, conn)
                and _has_policy_projection(conn))
    except (*RACES, ValueError):  # ValueError: a malformed configuration
        return False


def policy_keys(vault: Path, conn: sqlite3.Connection, *, after: str = "", limit: int) -> tuple[str, ...] | None:
    """Read one page of physical object keys from the current opted-in projection."""
    if not 0 < limit <= _POLICY_PAGE or not _policy_ready(vault, conn):
        return None
    # Each step seeks past all aliases of the previous key in the existing physical binding index.
    rows = conn.execute(
        "WITH RECURSIVE keys(object_key, ordinal) AS ("
        " SELECT (SELECT MIN(object_key) FROM media_bindings WHERE object_key > ?), 1"
        " UNION ALL SELECT (SELECT MIN(b.object_key) FROM media_bindings b WHERE b.object_key > keys.object_key), "
        " ordinal + 1 FROM keys WHERE object_key IS NOT NULL AND ordinal < ?"
        ") SELECT object_key FROM keys WHERE object_key IS NOT NULL ORDER BY ordinal",
        (after, limit),
    )
    return tuple(row[0] for row in rows)


def policy_filings(
    vault: Path, conn: sqlite3.Connection, keys: Iterable[str], *, limit: int = _POLICY_PAGE,
) -> dict[str, tuple[PolicyFiling, ...] | None] | None:
    """Read exact per-filing facts in a bounded page. The caller guards any admission that uses them.

    An unknown object returns None for its key. When the page fills, the first
    unread key and later keys are unknown. An absent object has an empty tuple.
    An unusable source frontier returns None for the whole page.
    """
    requested = tuple(islice(keys, _POLICY_PAGE + 1))
    if not 0 < limit <= _POLICY_PAGE or len(requested) > _POLICY_PAGE or not _policy_ready(vault, conn):
        return None
    if not requested:
        return {}
    rows = conn.execute(
        "SELECT b.*, p.refs FROM media_bindings b LEFT JOIN media_policy_filings p "
        "ON p.binary_rel = b.binary_rel AND p.companion_hash = b.companion_hash "
        "WHERE b.object_key IN (SELECT value FROM json_each(?)) "
        "ORDER BY b.object_key, b.binary_rel LIMIT ?",
        (json.dumps(requested), limit + 1),
    ).fetchall()
    first_incomplete = rows[limit]["object_key"] if len(rows) > limit else None
    filings: dict[str, list[PolicyFiling] | None] = {
        key: [] if first_incomplete is None or key < first_incomplete else None for key in requested
    }
    for row in rows[:limit]:
        group = filings[row["object_key"]]
        if group is None:
            continue
        filing = _policy_filing(row)
        if filing is None or (group and (group[0].sha256, group[0].size) != (filing.sha256, filing.size)):
            filings[row["object_key"]] = None
        else:
            group.append(filing)
    return {key: None if group is None else tuple(group) for key, group in filings.items()}


def _policy_filing(row: sqlite3.Row) -> PolicyFiling | None:
    if row["refs"] is None:
        return None
    try:
        raw = json.loads(row["refs"])
    except (ValueError, TypeError, RecursionError):
        return None
    references = originals.replica_references(raw)
    if not isinstance(raw, list) or len(references) != len(raw):
        return None
    return PolicyFiling(*row[:6], bool(row["resident_only"]), references)


class ObjectBindingProofLimit(Exception):
    """The allowed input cannot establish every binding of this object."""


def object_facts(conn: sqlite3.Connection, key: str, *, binding_limit: int | None = None) -> ObjectFacts | None:
    """Aggregate one object's bindings and captured pins with indexed ledger reads."""
    state = checkpoint(conn)
    if not state.complete:
        return None
    parameters: dict[str, str | int] = {"key": key}
    bindings = "media_bindings"
    if binding_limit is not None:
        # The indexed seek and sentinel precede both aggregation and deduplication.
        bindings = "(SELECT * FROM media_bindings WHERE object_key = :key ORDER BY binary_rel LIMIT :limit)"
        parameters["limit"] = binding_limit + 1
    row = conn.execute(
        "SELECT COUNT(*), MIN(b.sha256), MAX(b.sha256), MIN(b.size), MAX(b.size), "
        "MAX(b.resident_only), MIN(b.reference IS NOT NULL), MAX(c.owner_read) "
        f"FROM {bindings} b LEFT JOIN media_cache c ON c.binary_rel = b.object_key WHERE b.object_key = :key",
        parameters).fetchone()
    if binding_limit is not None and row[0] > binding_limit:
        raise ObjectBindingProofLimit()
    if row[0] == 0 or row[1] != row[2] or row[3] != row[4]:
        return None
    # Legacy NULL pins retain bytes until startup recovery resolves them. A claimed
    # pending job pins; an unclaimed pending job deliberately does not.
    pinned = bool(conn.execute(
        "SELECT EXISTS(SELECT 1 FROM media_access WHERE object_key = ? OR object_key IS NULL) "
        "OR EXISTS(SELECT 1 FROM jobs WHERE pinned = 1 AND state IN ('pending', 'running') "
        "AND (object_key = ? OR object_key IS NULL)) "
        "OR EXISTS(SELECT 1 FROM media_drain WHERE latched = 1 AND proof_version = 1)", (key, key)).fetchone()[0])
    pointers = conn.execute(
        f"SELECT reference, MIN(binary_rel) FROM {bindings} b WHERE object_key = :key AND reference IS NOT NULL "
        "GROUP BY reference ORDER BY reference", parameters).fetchall()
    references = tuple(originals.stored_reference(json.loads(item[0])) for item in pointers)
    if any(reference is None for reference in references):
        return None
    return ObjectFacts(key, row[1], row[3], row[0], bool(row[5]), bool(row[6]), row[7], pinned, references,
                       tuple(item[1] for item in pointers), state)


# The owner total (count-media-allowance-from-bindings D1). Binding rows hold a reference only in the selected
# store, so the aggregate needs no store filter; the partial covering index `media_bindings_version` serves it.
_AGGREGATE = (
    "SELECT COALESCE(SUM(size), 0), COALESCE(SUM(conflict), 0) FROM ("
    "SELECT MIN(size) AS size, MIN(size) <> MAX(size) OR MIN(sha256) <> MAX(sha256) "
    "OR MIN(reference) <> MAX(reference) AS conflict FROM media_bindings WHERE reference IS NOT NULL "
    "GROUP BY json_extract(reference, '$.object'), json_extract(reference, '$.version'))")
_CONFLICTING = (
    "SELECT json_extract(reference, '$.object'), json_extract(reference, '$.version') FROM media_bindings "
    "WHERE reference IS NOT NULL GROUP BY 1, 2 HAVING MIN(size) <> MAX(size) OR MIN(sha256) <> MAX(sha256) "
    "OR MIN(reference) <> MAX(reference) LIMIT ?")
#: The message that names a page whose version another page names with other bytes, as the census names it.
_CONFLICT = "one stored version has conflicting bindings"
#: Up to this many versions of equal bytes come back for reuse (`media_store._REUSE_CANDIDATE_LIMIT`).
REUSE_LIMIT = 8
_VersionKey = tuple[str, str]
#: One row that names a version: its kind ("binding" or "side"), its page, its original, size, digest and reference.
_VersionRow = tuple[str, str, str | None, int, str, str]


@dataclass(frozen=True, slots=True)
class Figure:
    """What the rows that name one version count: the size the aggregate adds, and whether they disagree."""

    size: int
    sha256: str
    reference: str
    conflict: bool


def _figure(rows: Iterable[_VersionRow]) -> Figure | None:
    """The figure of one version's rows, as the aggregate and the side table's sum count it together.

    The aggregate adds the binding rows' least size; a version that only counted side rows name adds theirs.
    Any two rows that disagree make the version a conflict, as `media_allowance._consistent` refuses it.
    """
    rows = tuple(rows)
    if not rows:
        return None
    bound = [row[3] for row in rows if row[0] == "binding"]
    sizes, digests, references = ({row[index] for row in rows} for index in (3, 4, 5))
    return Figure(min(bound or sizes), min(digests), min(references),
                  len(sizes) > 1 or len(digests) > 1 or len(references) > 1)


def _version_rows(conn: sqlite3.Connection, version: _VersionKey, *, side: bool) -> list[_VersionRow]:
    rows: list[_VersionRow] = [("binding", *row) for row in conn.execute(
        "SELECT companion_rel, binary_rel, size, sha256, reference FROM media_bindings "
        "WHERE json_extract(reference, '$.object') = ? AND json_extract(reference, '$.version') = ? "
        "AND reference IS NOT NULL", version)]
    if side:
        rows.extend(("side", *row) for row in conn.execute(
            "SELECT companion_rel, binary_rel, size, sha256, reference FROM media_reference_rows "
            "WHERE counted = 1 AND object = ? AND version = ?", version))
    return rows


@dataclass(frozen=True, slots=True)
class OwnerTotal:
    """The owner total that the projection gives, read in one ledger transaction (D1).

    ``unknown`` names up to five pages that make it unknown, with their reasons: unknown rows, then the pages of
    conflicting versions. ``conflicts`` counts the versions that rows name with two sizes, digests or references.
    ``figures`` holds each requested version's figure, and ``reuse`` up to 8 versions of the requested bytes,
    each as (original path, reference).
    """

    checkpoint: Checkpoint
    side_generation: int | None
    identity: AuditRow | None
    failed: FailedRebuild | None
    used_bytes: int
    conflicts: int
    unknown: tuple[tuple[str, str], ...]
    figures: dict[_VersionKey, Figure | None]
    reuse: tuple[tuple[str, str], ...]


def _side_versions(conn: sqlite3.Connection) -> tuple[int, int, list[_VersionKey]]:
    """What the counted side rows add to the binding rows' aggregate: bytes, conflicts and the conflicting versions."""
    added = conflicts = 0
    conflicting: list[_VersionKey] = []
    for key in conn.execute("SELECT DISTINCT object, version FROM media_reference_rows WHERE counted = 1").fetchall():
        rows = _version_rows(conn, tuple(key), side=True)
        bound = _figure(row for row in rows if row[0] == "binding")
        union = _figure(rows)
        assert union is not None
        if bound is None:
            added += union.size
        if union.conflict and (bound is None or not bound.conflict):
            conflicts += 1
            conflicting.append(tuple(key))
    return added, conflicts, conflicting


def _conflicting_pages(conn: sqlite3.Connection, side_keys: Iterable[_VersionKey], *, side: bool,
                       skip: frozenset[_VersionKey] = frozenset()) -> list[tuple[str, str]]:
    """Up to five pages that name a version in conflict, in path order, leaving out the versions in ``skip``."""
    keys = [tuple(row) for row in conn.execute(_CONFLICTING, (5 + len(skip),))]
    pages = {row[1] for key in dict.fromkeys([*keys, *side_keys]) if key not in skip
             for row in _version_rows(conn, key, side=side)}
    return [(page, _CONFLICT) for page in sorted(pages)[:5]]


def _read_total(conn: sqlite3.Connection, *, versions: Iterable[_VersionKey] = (),
                reuse: tuple[str, int, str] | None = None, previous: OwnerTotal | None = None,
                unknown_limit: int = 5) -> OwnerTotal:
    state, side = checkpoint(conn), side_generation(conn)
    has_side = _side_table(conn)
    side_keys: list[_VersionKey] = []
    if previous is not None and (previous.checkpoint, previous.side_generation) == (state, side):
        used, conflicts = previous.used_bytes, previous.conflicts
        if conflicts:
            side_keys = _side_versions(conn)[2] if has_side else []
    else:
        used, conflicts = conn.execute(_AGGREGATE).fetchone()
        if has_side:
            added, conflicting, side_keys = _side_versions(conn)
            used, conflicts = used + added, conflicts + conflicting
    unknown: list[tuple[str, str]] = []
    if has_side:
        unknown.extend((row[0], row[1] or "") for row in conn.execute(
            "SELECT companion_rel, reason FROM media_reference_rows WHERE counted = 0 "
            "ORDER BY companion_rel LIMIT ?", (unknown_limit,)))
    if conflicts and len(unknown) < 5:
        unknown.extend(_conflicting_pages(conn, side_keys, side=has_side)[:5 - len(unknown)])
    figures = {version: _figure(_version_rows(conn, version, side=has_side)) for version in versions}
    candidates = _reuse_rows(conn, reuse, side=has_side) if reuse is not None else ()
    return OwnerTotal(state, side, _identity(conn), failed_rebuild(conn), used, conflicts, tuple(unknown), figures,
                      candidates)


def _reuse_rows(conn: sqlite3.Connection, reuse: tuple[str, int, str], *, side: bool) -> tuple[tuple[str, str], ...]:
    """Up to 8 versions of these (digest, size, key version), one live filing each: (original path, reference)."""
    from .kbdir import managed_join

    trash = managed_join(f"{trash_metadata.TRASH_SUBPATH}/")
    # A displaced page has a counted side row, and a Trash page sits under the Trash folder: neither is reused.
    displaced = (" AND NOT EXISTS(SELECT 1 FROM media_reference_rows r WHERE r.companion_rel = b.companion_rel "
                 "AND r.counted = 1)") if side else ""
    return tuple((row[0], row[1]) for row in conn.execute(
        "SELECT MIN(b.binary_rel), MIN(b.reference) FROM media_bindings b "
        "WHERE b.sha256 = ? AND b.size = ? AND b.reference IS NOT NULL "
        "AND json_extract(b.reference, '$.key_version') = ? AND substr(b.companion_rel, 1, ?) <> ?"
        f"{displaced} GROUP BY json_extract(b.reference, '$.object'), json_extract(b.reference, '$.version') "
        "ORDER BY MIN(b.binary_rel) LIMIT ?", (*reuse, len(trash), trash, REUSE_LIMIT)))


def reuse_versions(vault: Path, digest: str, size: int, key_version: str) -> tuple[tuple[str, str], ...]:
    """The published reuse slice of these bytes (D1), for a reader that shares one total across originals."""
    ledger = _ledger(vault, create=False)
    if not ledger.path.exists():
        return ()
    with closing(ledger._connect(readonly=True)) as conn:
        if not _created(conn):
            return ()
        return _reuse_rows(conn, (digest, size, key_version), side=_side_table(conn))


def owner_total(vault: Path) -> OwnerTotal | None:
    """Read the published owner total in one ledger read transaction, outside any boundary (D1); None with no ledger."""
    ledger = _ledger(vault, create=False)
    if not ledger.path.exists():
        return None
    with closing(ledger._connect(readonly=True)) as conn:
        if not _created(conn):
            return None
        conn.execute("BEGIN")
        try:
            return _read_total(conn)
        finally:
            conn.rollback()


# The closed outcomes of a total at the store's head (`HeadTotal.state`).
COUNTED, UNKNOWN, WAIT, BUSY = "counted", "unknown", "wait", "busy"


@dataclass(frozen=True, slots=True)
class HeadTotal:
    """The owner total at the store's head, read without the boundary and without a write (D5).

    ``state`` is COUNTED with ``used_bytes``; UNKNOWN with up to five ``blockers`` (unknown rows, conflicts or a
    failed rebuild that no rebuild has retried); WAIT for a projection that needs a rebuild or follow; and BUSY when
    the audit suffix holds more than the bound, ``rows`` long. ``figure`` is the requested version's figure.
    """

    state: str
    used_bytes: int | None = None
    figure: Figure | None = None
    blockers: tuple[tuple[str, str], ...] = ()
    rows: int = 0
    reason: str | None = None
    reuse: tuple[tuple[str, str], ...] = ()
    total: OwnerTotal | None = None


def head_total(vault: Path, *, version: _VersionKey | None = None, reuse: tuple[str, int, str] | None = None,
               limit: int = UPLOAD_SUFFIX_ROWS, previous: OwnerTotal | None = None) -> HeadTotal:
    """The owner total at the store's head: the projection's total, corrected in memory by the audit suffix (D5).

    It reads the checkpoint and the D1 total in one ledger read transaction, then one store snapshot, and derives
    the suffix's pages from that snapshot as a follow does. With ``previous`` read at the same checkpoint, the
    aggregate is not read again. It writes nothing. Raises a class in `RACES` for a race or a read failure.
    """
    from .vault_store import mode, read

    vault = Path(vault).resolve()
    ledger = _ledger(vault, create=False)
    if not ledger.path.exists() or not mode.store_mode_for_reads(vault):
        return HeadTotal(WAIT, reason="the projection needs a rebuild")
    with closing(ledger._connect(readonly=True)) as conn:
        if not _created(conn):
            return HeadTotal(WAIT, reason="the projection needs a rebuild")
        conn.execute("BEGIN")
        try:
            total = _read_total(conn, versions=() if version is None else (version,), reuse=reuse,
                                previous=previous)
            state, identity = total.checkpoint, total.identity
            if (not state.complete or state.seq is None or state.policy != policy_key(vault)
                    or total.side_generation != state.generation or identity is None or identity.seq != state.seq):
                if total.failed is not None and total.failed.generation == state.generation:
                    return HeadTotal(UNKNOWN, blockers=total.failed.blockers, total=total,
                                     reason="the last rebuild could not read the notes")
                return HeadTotal(WAIT, total=total, reason="the projection needs a rebuild")
            with read.snapshot(vault, join_request=False) as view:
                head = _head(view)
                if state.seq > head or _audit_row(view.connection, state.seq) != identity:
                    return HeadTotal(WAIT, total=total, reason="the audit trail no longer holds the followed row")
                if head - state.seq > limit:
                    return HeadTotal(BUSY, rows=head - state.seq, total=total,
                                     reason="the projection lags the store")
                rows = view.connection.execute("SELECT path, from_path FROM store_audit WHERE seq > ? AND seq <= ? "
                                               "ORDER BY seq", (state.seq, head)).fetchall()
                if any(row[0] is None for row in rows):
                    return HeadTotal(WAIT, total=total, reason="a migration or a rollback replaced the store's pages")
                derived = None
                if rows:
                    paths = tuple(dict.fromkeys(path for row in rows for path in row if path is not None))
                    try:
                        with read.pinned(vault, view):
                            derived = _derive_round(vault, conn, view, paths, basis(vault))
                    except _Displaced:
                        return HeadTotal(WAIT, total=total, reason="a page away from its original changed")
                return _corrected(total, derived, conn, version, len(rows))
        finally:
            conn.rollback()


def _corrected(total: OwnerTotal, derived: _Round | None, conn: sqlite3.Connection,
               version: _VersionKey | None, rows: int) -> HeadTotal:
    """Correct the published total for the pages that the suffix derives (D5 step 5)."""
    figure = total.figures.get(version) if version is not None else None
    if derived is None:
        if total.unknown or total.conflicts:
            return HeadTotal(UNKNOWN, blockers=total.unknown[:5], total=total, reason="a note cannot be counted")
        return HeadTotal(COUNTED, total.used_bytes, figure, reuse=total.reuse, total=total)
    has_side = _side_table(conn)
    paths, companions = set(derived.paths), set(derived.companions)
    new_rows: dict[_VersionKey, list[_VersionRow]] = defaultdict(list)
    for binding in derived.bindings:
        if binding.reference is not None:
            named = json.loads(binding.reference)
            new_rows[(named["object"], named["version"])].append(
                ("binding", binding.companion_rel, binding.binary_rel, binding.size, binding.sha256, binding.reference))
    for side in derived.sides:
        if side.counted:
            new_rows[(side.object, side.version)].append(
                ("side", side.companion_rel, side.binary_rel, side.size, side.sha256, side.reference))
    # A follow deletes the binding rows of each path as a page or as an original, and the side rows of each page.
    touched = set(new_rows)
    ordered = sorted(paths)
    for start in range(0, len(ordered), 256):
        batch = ordered[start:start + 256]
        marks = ",".join("?" for _ in batch)
        touched.update(tuple(row) for row in conn.execute(
            "SELECT json_extract(reference, '$.object'), json_extract(reference, '$.version') FROM media_bindings "
            f"WHERE reference IS NOT NULL AND (companion_rel IN ({marks}) OR binary_rel IN ({marks}))", batch * 2))
        if has_side:
            touched.update(tuple(row) for row in conn.execute(
                f"SELECT object, version FROM media_reference_rows WHERE counted = 1 AND companion_rel IN ({marks})",
                batch))
    used, conflicts = total.used_bytes, total.conflicts
    conflicting: set[str] = set()
    for key in touched:
        published = _version_rows(conn, key, side=has_side)
        kept = [row for row in published
                if not (row[1] in companions or (row[0] == "binding" and (row[1] in paths or row[2] in paths)))]
        after_rows = [*kept, *new_rows.get(key, ())]
        before, after = _figure(published), _figure(after_rows)
        used += (after.size if after is not None else 0) - (before.size if before is not None else 0)
        conflicts += int(after is not None and after.conflict) - int(before is not None and before.conflict)
        if after is not None and after.conflict:
            conflicting.update(row[1] for row in after_rows)
        if key == version:
            figure = after
    # At most every derived page drops out of the published unknown rows, so this many still name five.
    published_unknown = conn.execute(
        "SELECT companion_rel, reason FROM media_reference_rows WHERE counted = 0 ORDER BY companion_rel LIMIT ?",
        (5 + len(companions),)).fetchall() if has_side else []
    unknown = [*((row[0], row[1] or "") for row in published_unknown if row[0] not in companions),
               *((side.companion_rel, side.reason or "") for side in derived.sides if not side.counted)]
    if conflicts:
        side_keys = _side_versions(conn)[2] if has_side else []
        unknown.extend((page, _CONFLICT) for page in conflicting)
        unknown.extend(row for row in _conflicting_pages(conn, side_keys, side=has_side, skip=frozenset(touched))
                       if row[0] not in companions)
    if unknown or conflicts:
        return HeadTotal(UNKNOWN, blockers=tuple(sorted(dict.fromkeys(unknown))[:5]), rows=rows, total=total,
                         reason="a note cannot be counted")
    return HeadTotal(COUNTED, used, figure, rows=rows, reuse=total.reuse, total=total)
