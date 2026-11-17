"""Convert an existing archive's ordinary originals into the shared pool, as one owner maintenance job.

New archives start in the shared layout; an existing archive changes only when its owner runs this job.
Each filing keeps its companion page byte for byte, with its references and grants. The job publishes the
filing's verified bytes once as a pool object and only then retires the ordinary file at its logical path.
Progress lives in the archive: a rerun reads which filings still hold an ordinary file, so an interrupted
run leaves readable mixed storage and continues where it stopped.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import (
    held_fs,
    maintenance_jobs,
    media_bindings,
    original_pool,
    originals,
    reserved_paths,
    state_migration,
    trash_metadata,
)
from .cli_ops import OpError

MODE = "convert-originals"
_OPERATION = "convert_originals_batch"
_READ_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class _Filing:
    """One filing the plan converts, as the inventory read it."""

    original: str  # The logical path, which keeps naming the original after conversion.
    companion: str
    companion_hash: str
    sha256: str
    size: int
    #: A store-mode filing whose bytes already reached the pool, which still has a binary record to retire.
    record_only: bool = False

    def entry(self) -> tuple[str, ...]:
        return (self.original, self.companion, self.companion_hash, self.sha256,
                "record" if self.record_only else "bytes")


@dataclass(slots=True)
class _Inventory:
    convert: list[_Filing] = field(default_factory=list)
    shared: int = 0
    store_bound: list[str] = field(default_factory=list)
    unreadable: list[dict[str, str]] = field(default_factory=list)


@dataclass(slots=True)
class _Outcome:
    converted: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    pinned: list[str] = field(default_factory=list)
    unreadable: list[dict[str, str]] = field(default_factory=list)


def convert(vault_root: Path, *, dry_run: bool = True, plan_id: str | None = None) -> dict:
    """Preview, or convert, every filing that still keeps its original as an ordinary file.

    The owner alone runs it: the inventory reads hidden filings too. A write enrols the shared format
    before it relocates any bytes, and a write that names a `plan_id` refuses a plan that changed since
    its preview.
    """
    vault = Path(vault_root).resolve()
    maintenance_jobs.require_owner(vault)
    if not original_pool.enabled(vault) and not original_pool.launcher_supported():
        raise _launcher_update_required()  # A preview offers no plan that its start would refuse.
    maintenance_jobs.checkpoint("planning")
    inventory = _inventory(vault)
    fresh = maintenance_jobs.plan_identity(MODE, {}, (filing.entry() for filing in inventory.convert))
    report = {
        "dry_run": dry_run,
        "plan_id": fresh,
        "would_convert": len(inventory.convert),
        "already_shared": inventory.shared,
        "store_bound": inventory.store_bound,
        "unreadable": inventory.unreadable,
    }
    if dry_run:
        maintenance_jobs.record_planned((filing.original, filing.sha256) for filing in inventory.convert)
        maintenance_jobs.record_planned(
            ((item["path"], item["reason"]) for item in inventory.unreadable), kind=maintenance_jobs.SKIPPED_PAGE)
        return {**report, "layout": "shared" if original_pool.enabled(vault) else "ordinary"}
    if plan_id is not None:
        maintenance_jobs.require_current_plan(MODE, plan_id, fresh, "the originals that keep ordinary files")
    _enable(vault)
    outcome = _apply(vault, inventory.convert)
    return {
        **report,
        "plan_id": plan_id or fresh,
        "layout": "shared",
        "converted": len(outcome.converted),
        "skipped_changed": outcome.changed,
        "kept_pinned": outcome.pinned,
        "unreadable": [*inventory.unreadable, *outcome.unreadable],
    }


def _enable(vault: Path) -> None:
    if original_pool.enabled(vault):
        return
    try:
        original_pool.enable(vault)
    except state_migration.StateMigrationOfflineRequired:
        raise _launcher_update_required() from None


def _launcher_update_required() -> OpError:
    return OpError(
        "ORIGINAL_POOL_LIFECYCLE_REQUIRED",
        "this service's launcher has not proven support for shared originals",
        "Update the managed launcher offline (docs/managed-service-upgrades.md), then run the conversion again.",
        details={"status": "terminal", "committed": False},
    )


def _inventory(vault: Path) -> _Inventory:
    """Read every canonical companion once and sort its original by what conversion does with it.

    The companions are the only catalogue: a store-mode archive's pages come from its store, a file-mode
    archive's from disk. A Trash entry, a note, a dataset and a companion retired to Trash keep their files.
    The whole inventory reads one store snapshot.
    """
    from .vault_store import read

    with read.request_snapshot(vault):
        return _read_inventory(vault)


def _read_inventory(vault: Path) -> _Inventory:
    from .vault import FrontmatterError, parse_frontmatter

    inventory = _Inventory()
    census = trash_metadata.PlacementCensus(vault)
    pooled = original_pool.enabled(vault)
    selected = originals.binding_for(vault)
    candidates: list[_Filing] = []
    for rel, body in media_bindings._pages(vault, join_request=True):  # noqa: SLF001 - the census's page source
        if trash_metadata.is_trash_path(rel):
            continue  # A Trash entry keeps an ordinary file at its own path (`originals.may_pool`).
        data = bytes(body) if not isinstance(body, bytes) else body
        try:
            fields, _body, marker = parse_frontmatter(data.decode("utf-8"), custody=True)
        except (UnicodeError, FrontmatterError):
            continue  # Readers treat such a page as no companion, so its original reads as an ordinary file.
        if marker is None or originals.bound_path(fields) is None:
            continue
        try:
            binary = originals.companion_binding(fields, sidecar_path=rel)
        except ValueError:
            inventory.unreadable.append({"path": rel, "reason": "the page names an original away from its own path"})
            continue
        if binary is None:
            inventory.unreadable.append(
                {"path": rel, "reason": "the page names an original without a complete digest and size"})
            continue
        if originals.is_note_path(binary.path) or "data_file" in fields:
            continue  # Notes and datasets stay ordinary files (design D1), as new filings do.
        digest = hashlib.sha256(data).hexdigest()
        # The verdict that reads take once no file holds the original, so the conversion never pools a filing that
        # reads would then take for deleted.
        try:
            retired, unconfirmed = trash_metadata.retired_companion(vault, rel, fields, digest, census=census), None
        except originals.RetirementUnconfirmed as error:
            retired, unconfirmed = True, error.reason
        if retired:
            if (reason := _kept_reason(vault, rel, fields, binary.path, unconfirmed)) is not None:
                inventory.unreadable.append({"path": binary.path, "reason": reason})
            continue  # A deleted original files nothing, so the conversion leaves its page be.
        filing = _Filing(binary.path, rel, digest, binary.sha256, binary.size)
        try:
            with reserved_paths.open_generic_binary(vault, binary.path) as source:
                size = source.version.size
        except reserved_paths.ReservedPathLeafError as error:
            if error.code != "MISSING":
                inventory.unreadable.append({"path": binary.path, "reason": f"the original cannot be read ({error.code})"})
                continue
        else:
            if size != binary.size:
                inventory.unreadable.append({"path": binary.path, "reason": "the original's size differs from its page"})
            else:
                inventory.convert.append(filing)
            continue
        try:
            shared = pooled and original_pool.locally_present(vault, binary.sha256, binary.size)
        except original_pool.ObjectMismatch:
            inventory.unreadable.append({"path": binary.path, "reason": "the shared copy differs from its page"})
            continue
        reference = originals.stored_reference(fields.get("stored_object"))
        if shared:
            candidates.append(filing)
        elif reference is not None and selected is not None and reference.store == selected.store:
            inventory.store_bound.append(binary.path)  # No local bytes: the stored version needs no relocation.
        else:
            inventory.unreadable.append(
                {"path": binary.path, "reason": "the original is missing: no file, shared copy or stored version"})
    census.require_current()
    records = _store_records(vault, [filing.original for filing in candidates])
    for filing in candidates:
        if filing.original in records:
            inventory.convert.append(_Filing(filing.original, filing.companion, filing.companion_hash,
                                             filing.sha256, filing.size, record_only=True))
        else:
            inventory.shared += 1
    return inventory


def _kept_reason(vault: Path, page: str, fields: dict, original: str, unconfirmed: str | None) -> str | None:
    """Why a filing that reads take for deleted, or refuse, keeps its ordinary file; None for a deleted original."""
    try:
        reserved_paths.inspect_generic_path(vault, original)
    except reserved_paths.ReservedPathLeafError as error:
        if error.code != "MISSING":
            return f"the original cannot be read ({error.code})"
        if originals.stored_reference(fields.get("stored_object")) is None:
            return unconfirmed  # No file and no stored version: only a refused Trash entry is left to name.
    return (f"its page {page} still names the Trash entry of an earlier delete, which reads would follow once the "
            "original is shared: remove the page's original_retirement_meta field, then convert again")


def _store_records(vault: Path, originals_paths: Sequence[str]) -> dict:
    """The page store's binary records at these logical paths, read in a few queries; none in file mode."""
    from .vault_store import mode, read

    if not originals_paths or not mode.store_mode_for_reads(vault):
        return {}
    with read.snapshot(vault) as view:
        return view.binary_records(originals_paths)


def _apply(vault: Path, filings: Sequence[_Filing]) -> _Outcome:
    """Convert in bounded batches, each staged outside the mutation boundary and retired inside one short hold.

    A batch stages every byte first, so the free space a run needs is its largest batch. A filing that a
    reader or a media job pins waits for the run's later pass; one still pinned then stays ordinary, and a
    rerun converts it.
    """
    from . import foreground_priority

    outcome = _Outcome()
    batch = maintenance_jobs.BATCH_PAGES
    queue = list(filings)
    for attempt in range(2):
        if attempt:
            time.sleep(maintenance_jobs.HOLD_SECONDS)  # A pin is a read or a media job in flight: give it a moment.
        deferred: list[_Filing] = []
        for position in range(0, len(queue), batch):
            maintenance_jobs.checkpoint("converting", done=len(outcome.converted), total=len(filings))
            foreground_priority.yield_to_foreground()
            deferred.extend(_convert_batch(vault, queue[position:position + batch], outcome))
        queue = deferred
        if not queue:
            break
    outcome.pinned.extend(filing.original for filing in queue)
    maintenance_jobs.checkpoint("converting", done=len(outcome.converted), total=len(filings))
    return outcome


def _convert_batch(vault: Path, filings: Sequence[_Filing], outcome: _Outcome) -> list[_Filing]:
    """Stage, verify and retire one batch; return the filings a pin held."""
    with ExitStack() as staged:
        ready: list[tuple[_Filing, original_pool.PreparedOriginal | None, list[held_fs.FileVersion]]] = []
        for filing in filings:
            if filing.record_only:
                ready.append((filing, None, []))
                continue
            seen: list[held_fs.FileVersion] = []
            try:
                prepared = staged.enter_context(original_pool.prepare(
                    vault, filing.sha256, filing.size, _chunks(vault, filing.original, seen)))
            except reserved_paths.ReservedPathLeafError as error:
                if error.code == "MISSING":
                    outcome.changed.append(filing.original)
                else:
                    outcome.unreadable.append(
                        {"path": filing.original, "reason": f"the original cannot be read ({error.code})"})
                continue
            except original_pool.ObjectMismatch:
                outcome.unreadable.append({"path": filing.original, "reason": "the shared copy differs from its page"})
                continue
            except ValueError:
                outcome.unreadable.append({"path": filing.original, "reason": "the original's bytes differ from its page"})
                continue
            ready.append((filing, prepared, seen))
        if not ready:
            return []
        held: list[_Filing] = []
        for attempt in range(3):
            with maintenance_jobs.chunk_hold(vault, _OPERATION):
                pinned, raced = _retire(vault, ready, outcome)
            held.extend(pinned)
            if attempt == 2:
                outcome.changed.extend(filing.original for filing, _prepared, _seen in raced)  # Still contended.
                break
            ready = []
            for item in raced:
                try:
                    item[1].confirm_existing()  # Hash the object another filing published, outside the hold.
                except original_pool.ObjectMismatch:
                    outcome.unreadable.append({"path": item[0].original, "reason": "the shared copy differs from its page"})
                    continue
                ready.append(item)
            if not ready:
                break
        return held


def _chunks(vault: Path, original: str, seen: list[held_fs.FileVersion]) -> held_fs.Chunks:
    """Stream the ordinary file through its retained handle, keeping the version each pass read."""

    def read() -> Iterator[bytes]:
        with reserved_paths.open_generic_binary(vault, original) as source:
            seen.append(source.version)
            while chunk := source.stream.read(_READ_BYTES):
                yield chunk

    return held_fs.Chunks(read)


def _retire(vault: Path, ready, outcome: _Outcome) -> tuple[list[_Filing], list]:
    """Under the hold: verify each binding and file unchanged, publish, then retire unpinned ordinary files.

    Returns the filings a pin held, and the staged filings whose object another filing published first.
    """
    published: list[tuple[_Filing, held_fs.FileVersion | None]] = []
    raced = []
    for filing, prepared, seen in ready:
        try:
            if _companion_hash(vault, filing.companion) != filing.companion_hash:
                outcome.changed.append(filing.original)
                continue
            version = None
            if prepared is not None:
                with reserved_paths.open_generic_binary(vault, filing.original) as source:
                    version = source.version
                if not seen or version != seen[-1]:
                    outcome.changed.append(filing.original)  # Edited after it was hashed.
                    continue
                prepared.publish()
        except reserved_paths.ReservedPathLeafError as error:
            if error.code == "MISSING":
                outcome.changed.append(filing.original)
            else:
                outcome.unreadable.append(
                    {"path": filing.original, "reason": f"the original cannot be read ({error.code})"})
            continue
        except original_pool.PublicationRace:
            raced.append((filing, prepared, seen))  # Identical bytes published first, by this batch or an upload.
            continue
        published.append((filing, version))
    retired: list[_Filing] = []
    held: list[_Filing] = []
    paths = [path for filing, _ in published for path in (filing.original, filing.companion)]
    # The binding projection's own transaction runs when this scope ends, after the ledger's below.
    with media_bindings.operation(vault, paths), _pin_exclusion(vault) as (conn, ledger):
        pinned = _pinned(conn, ledger, [filing.original for filing, version in published if version is not None])
        for filing, version in published:
            if filing.original in pinned:
                held.append(filing)
                continue
            if version is not None:
                try:
                    reserved_paths.unlink_generic_file(vault, filing.original, expected_identity=version.identity)
                except reserved_paths.ReservedPathLeafError as error:
                    outcome.unreadable.append(
                        {"path": filing.original, "reason": f"the ordinary file could not be retired ({error.code})"})
                    continue
            retired.append(filing)
    kept = _retire_store_records(vault, retired)
    outcome.changed.extend(sorted(kept))
    converted = [filing.original for filing in retired if filing.original not in kept]
    outcome.converted.extend(converted)
    maintenance_jobs.record_paths(converted, committed=True)
    return held, raced


def _companion_hash(vault: Path, companion: str) -> str:
    return hashlib.sha256(reserved_paths.read_generic_bytes(vault, companion).data).hexdigest()


@contextmanager
def _pin_exclusion(vault: Path) -> Iterator[tuple[sqlite3.Connection, Any]]:
    """Hold the media ledger's write exclusion, which every pin admission takes, through the unlinks."""
    from .media_store import MediaJobStore

    ledger = MediaJobStore(vault)
    with closing(ledger._connect()) as conn:  # noqa: SLF001 - the media ledger's own connection
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn, ledger
        except BaseException:
            conn.rollback()
            raise
        conn.commit()


def _pinned(conn: sqlite3.Connection, ledger: Any, paths: Sequence[str]) -> set[str]:
    """The paths whose ordinary file a reader or a claimed media job holds now, in two indexed reads.

    An ordinary file's physical key is its own path, so a pin on another path never holds it. A dead
    process's pin is cleared first, as eviction clears it.
    """
    from .media_store import _JOB_PINS, _clear_dead_access

    if not paths:
        return set()
    marks = ",".join("?" for _ in paths)
    _clear_dead_access(conn, ledger, conn.execute(
        f"SELECT * FROM media_access WHERE binary_rel IN ({marks}) OR object_key IN ({marks})",
        (*paths, *paths)).fetchall())
    return {row[0] for row in conn.execute(
        f"SELECT binary_rel FROM media_access WHERE binary_rel IN ({marks}) "
        f"UNION SELECT object_key FROM media_access WHERE object_key IN ({marks}) "
        f"UNION SELECT binary_rel FROM jobs WHERE {_JOB_PINS} AND binary_rel IN ({marks})",
        (*paths, *paths, *paths))}


def _retire_store_records(vault: Path, retired: list[_Filing]) -> set[str]:
    """Drop the page store's binary record of each retired filing, as a filing captured pooled has none.

    One commit binds each record's revision, digest and linking pages. A refused commit keeps the records,
    which read the same pooled filings, and returns their paths; the next run retires them.
    """
    from .vault_store import availability as store_availability
    from .vault_store import batch as store_batch
    from .vault_store import read
    from .vault_store import targets as store_targets
    from .vault_store.write import BinaryDelete, Expect, StoreConflict

    writer = store_availability.page_writer(vault)
    if writer is None or not retired:
        return set()
    rels = [filing.original for filing in retired]
    with read.snapshot(vault, join_request=False) as view:
        records = view.binary_records(rels)
    if not records:
        return set()
    links = store_targets.inbound_links(vault, list(records))
    try:
        store_batch.commit_batch(writer, [], vault_root=vault, lifecycle=store_batch.Lifecycle(
            binary_deletes=tuple(BinaryDelete(rel, on_disk=True) for rel in records),
            expect={rel: Expect(revision=record.revision, sha256=record.sha256, linked_from=links[rel].bound)
                    for rel, record in records.items()},
        ))
    except StoreConflict:
        return set(records)
    return set()
