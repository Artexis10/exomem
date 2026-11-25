"""Add capture metadata to the sidecars of photos and videos that were filed before it was recorded.

The owner starts this job. It reads the originals that sit on the volume and replaces only the capture
block of each sidecar (`preserve.render_capture_block`): the extracted text, the preserved notes and every
other field stay as written. It never fetches an original. A sidecar whose original has no bytes on the
volume, or whose preserved notes have no machine-owned boundary, is skipped and counted, and a rerun
adds what a later arrival of the bytes makes readable. The pages commit in short holds of the mutation
boundary through the sidecar's own writer, so a store-mode vault takes them through its store.

A photo or video that states no capture time takes the time its file name states, when a registered
name format reads one. With `recover` the pass also matches a photo to the original that states its
date, among the vault's own camera-dated images and the images in a folder of originals the owner names
(`originals_root`). A match is accepted only when it is unique (`capture_match`). The pass never writes,
moves or copies anything in that folder, and it refuses the folder on Evermatter Cloud, which has none.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from . import (
    access,
    capture_dates,
    capture_match,
    capture_metadata,
    foreground_priority,
    maintenance_jobs,
    media_bindings,
    media_types,
    originals,
    preserve,
    reserved_paths,
    vault,
)
from .capture_metadata import CaptureFacts
from .cli_ops import OpError
from .vault_store import read

MODE = "capture-metadata"
_OPERATION = "capture_metadata_chunk"
#: Skipped pages that a preview lists by path. The report always carries each reason's exact count.
LISTED_SKIPS = 200

BYTES_NOT_ON_VOLUME = "bytes not on the volume"
AMBIGUOUS_BOUNDARY = "AMBIGUOUS_SIDECAR_BOUNDARY"
READER_UNAVAILABLE = "this host has no decoder for the original's kind"
RETIREMENT_UNCONFIRMED = "the Trash retirement of the original is unconfirmed"
UNREADABLE_FOR_MATCHING = "the image could not be decoded for matching"


class _Plan:
    """What one pass over the archive found: the pages to rewrite, and each skip with its reason."""

    def __init__(self) -> None:
        self.pages: list[tuple[str, Path, str]] = []
        self.facts: dict[str, str] = {}
        self.already_current = 0
        self.no_metadata = 0
        self.skipped: dict[str, int] = {}
        self.listed: list[tuple[str, str]] = []
        #: The source of each page's date after the plan is applied, over every photo and video the pass read.
        self.by_source: dict[str, int] = {}
        self.undated = 0
        #: Pages with no accepted match, by reason. A page here may still carry a date from its file name,
        #: and is still updated with what its own file states.
        self.unmatched: dict[str, int] = {}
        self.unmatched_listed: list[tuple[str, str]] = []
        #: What the plan gives each page to rewrite, as the receipt's row shows it: source, time and origin of a match.
        self.gives: dict[str, str] = {}
        #: Pages that would take a date from another file, bounded like the skips.
        self.matches: list[dict[str, str]] = []

    def skip(self, page: str, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1
        if len(self.listed) < LISTED_SKIPS:
            self.listed.append((page, reason))

    def no_match(self, page: str, reason: str) -> None:
        self.unmatched[reason] = self.unmatched.get(reason, 0) + 1
        if len(self.unmatched_listed) < LISTED_SKIPS:
            self.unmatched_listed.append((page, reason))

    def entries(self) -> list[tuple[str, str, str]]:
        """What the `plan_id` binds: each page to rewrite, the text it is rewritten from and the facts it gets."""
        return [(rel, digest, self.facts[rel]) for rel, _path, digest in self.pages]


@dataclass
class _Recovery:
    """What `recover` adds to one pass: the originals a copy may match, and the copies waiting for the pool to be complete."""

    pool: capture_match.Pool
    waiting: list[tuple[str, str, CaptureFacts | None, capture_match.Fingerprint, capture_match.Location]]


def backfill(
    vault_root: Path,
    *,
    dry_run: bool = True,
    plan_id: str | None = None,
    recover: bool | None = None,
    originals_root: str | None = None,
) -> dict:
    """Preview, or add, the capture block of every sidecar whose original states one.

    The owner alone runs it. A write that names a `plan_id` refuses a plan that changed since its preview.
    `recover` also matches photos that state no date to the originals that do; a named `originals_root`
    adds that folder's images to the originals, and implies `recover`.
    """
    root = Path(vault_root).resolve()
    maintenance_jobs.require_owner(root)
    archive = _archive_root(root, originals_root)
    recovering = bool(recover) or archive is not None
    # The flags that change the plan's work, so a plan_id names exactly this recovery and not another.
    flags: dict[str, Any] = {}
    if recovering:
        flags["recover"] = True
    if archive is not None:
        flags["originals_root"] = str(archive)
    maintenance_jobs.checkpoint("planning")
    plan = _plan(root, archive, recovering)
    fresh = maintenance_jobs.plan_identity(MODE, flags, plan.entries())
    report: dict[str, Any] = {
        "dry_run": dry_run,
        "plan_id": fresh,
        "would_update": len(plan.pages),
        "already_current": plan.already_current,
        "no_capture_metadata": plan.no_metadata,
        "dated_by_source": dict(sorted(plan.by_source.items())),
        "undated": plan.undated,
        "skipped": dict(plan.skipped),
        "skipped_listed": [{"path": page, "reason": reason} for page, reason in plan.listed],
    }
    if recovering:
        report["matched_listed"] = plan.matches
        report["unmatched"] = dict(plan.unmatched)
        report["unmatched_listed"] = [{"path": page, "reason": reason} for page, reason in plan.unmatched_listed]
    if dry_run:
        maintenance_jobs.record_planned((rel, plan.gives.get(rel)) for rel, _path, _digest in plan.pages)
        maintenance_jobs.record_planned(plan.listed, kind=maintenance_jobs.SKIPPED_PAGE)
        return report
    if plan_id is not None:
        maintenance_jobs.require_current_plan(MODE, plan_id, fresh, "the sidecars that lack their capture metadata")
    committed, moved = maintenance_jobs.commit_pages(
        root, _OPERATION, plan.pages, lambda rel, text: _rewrite(plan, rel, text), commit=_commit_to_sidecars(root)
    )
    return {**report, "plan_id": plan_id or fresh, "updated": len(committed), "skipped_changed": len(moved)}


def _archive_root(vault_root: Path, named: str | None) -> Path | None:
    """The folder of originals the owner named, checked; None when none was named."""
    if named is None:
        return None
    from . import cloud_cell

    refused = {"status": "terminal", "committed": False}
    if cloud_cell.cloud_mode_enabled() or cloud_cell.hosted_mode_enabled():
        raise OpError(
            "ORIGINALS_ROOT_UNAVAILABLE",
            "originals_root reads a folder on the server's own disk, and Evermatter Cloud has no such folder",
            "Run recover without originals_root: it matches against the originals already in the vault.",
            details=refused,
        )
    folder = Path(named).expanduser()
    if not folder.is_absolute() or not folder.is_dir():
        raise OpError(
            "INVALID_ORIGINALS_ROOT", "originals_root must be the absolute path of an existing folder",
            "Pass the folder that holds the original photos, as an absolute path.", details=refused,
        )
    folder = folder.resolve()
    if folder.is_relative_to(vault_root) or vault_root.is_relative_to(folder):
        raise OpError(
            "INVALID_ORIGINALS_ROOT", "originals_root must be outside the vault, which recover already reads",
            "Pass a folder outside the vault, or omit originals_root.", details=refused,
        )
    return folder


def _plan(root: Path, archive: Path | None, recovering: bool) -> _Plan:
    # One store snapshot answers the page census and each original's state, so the plan names one revision.
    with read.request_snapshot(root):
        return _read_plan(root, archive, recovering)


def _read_plan(root: Path, archive_folder: Path | None, recovering: bool) -> _Plan:
    plan = _Plan()
    recovery = _Recovery(capture_match.Pool(), []) if recovering else None
    if recovery is not None and archive_folder is not None:
        capture_match.add_archive(
            recovery.pool, archive_folder, tick=lambda read_so_far: maintenance_jobs.checkpoint("indexing originals", read_so_far)
        )
    archive = originals.ArchiveReads(root)
    # The canonical pages, which a store-mode vault keeps in its store and may not have projected to disk.
    for rel, body in foreground_priority.yielding_in_bulk(media_bindings._pages(root, join_request=True)):  # noqa: SLF001 - the census's page source
        data = bytes(body)
        try:
            fields, _body, marker = vault.parse_frontmatter(data.decode("utf-8"), custody=True)
        except (UnicodeError, vault.FrontmatterError):
            continue  # readers take such a page for no companion
        original = originals.bound_path(fields) if marker is not None else None
        if original is None or not _reads_capture(original):
            continue
        try:
            snapshot = originals.original_state(root, original, companions={rel: data}, archive=archive)
        except originals.RetirementUnconfirmed as error:
            if error.companion == rel:
                plan.skip(rel, RETIREMENT_UNCONFIRMED)
            continue
        except (ValueError, reserved_paths.ReservedPathLeafError):
            continue  # an unsafe or unreadable binding is no companion here; the media store reports it
        if snapshot.sidecar_path != root / rel or snapshot.binary_sha256 is None:
            continue  # the page names an original whose companion is another page: a history or copy
        _plan_page(root, plan, rel, data.decode("utf-8"), fields, snapshot, recovery)
    if recovery is not None:
        total = len(recovery.waiting)
        for done, (rel, text, facts, copy, location) in enumerate(recovery.waiting):
            if done % 50 == 0:
                maintenance_jobs.checkpoint("matching", done, total)
            _settle_copy(root, plan, recovery.pool, rel, text, facts, copy, location)
    return plan


def _plan_page(
    root: Path, plan: _Plan, rel: str, text: str, fields: dict[str, Any], snapshot: Any, recovery: _Recovery | None,
) -> None:
    kind = media_types.media_type_for(snapshot.path)
    if kind is None or snapshot.state in ("evicted", "missing"):
        plan.skip(rel, BYTES_NOT_ON_VOLUME)
        return
    if (reason := access.writable_reason(root, rel)) is not None:
        plan.skip(rel, reason)
        return
    if not capture_metadata.available(kind, snapshot.path):
        plan.skip(rel, READER_UNAVAILABLE)
        return
    copy: capture_match.Fingerprint | str | None = None
    with _local_view(snapshot) as view:
        facts = capture_metadata.read(view, kind) if view is not None else None
        # The bytes' own place, not the private view, so a candidate can be read again after this block ends.
        location = capture_match.Location(snapshot.local_path or snapshot.path, snapshot.path.suffix)
        if recovery is not None and view is not None and kind == "image":
            copy = _prepare_recovery(recovery, rel, fields, facts, location)
    if view is None or (facts is None and not _still_local(root, snapshot)):
        plan.skip(rel, BYTES_NOT_ON_VOLUME)  # it left the volume between the census and the read
        return
    facts = capture_metadata.with_name_date(root, facts, kind, snapshot.path.name)
    if isinstance(copy, str):
        plan.no_match(rel, copy)
    elif recovery is not None and copy is not None:
        recovery.waiting.append((rel, text, facts, copy, location))
        return
    _finish_page(root, plan, rel, text, facts)


def _prepare_recovery(
    recovery: _Recovery, rel: str, fields: dict[str, Any], facts: CaptureFacts | None, location: capture_match.Location,
) -> capture_match.Fingerprint | str | None:
    """Add a camera-dated image to the pool, or fingerprint an undated one that a match could still improve.

    Returns the fingerprint to match, the reason that none can be made, or None when the page needs no match.
    """
    held = capture_metadata.recorded(fields)
    camera_dated = facts is not None and facts.time and facts.date is not None
    # A date already recorded from a source that a match does not outrank stays: a rerun never swaps a recovered date.
    keeps_recorded = held.time and not capture_dates.outranks(
        capture_dates.source_for("perceptual-match"), capture_dates.recorded_source(held.date_source)
    )
    if not camera_dated and keeps_recorded:
        return None
    try:
        fingerprint = capture_match.fingerprint(location)
    except MemoryError:
        raise
    except Exception:  # noqa: BLE001 - untrusted bytes: an image that no decoder reads cannot be matched
        return None if camera_dated else UNREADABLE_FOR_MATCHING
    if camera_dated:
        if fingerprint is not None:
            recovery.pool.add(
                capture_match.Original(fingerprint, facts.time, facts.date, str(fields.get("exomem_id") or rel), location)
            )
        return None
    return fingerprint or capture_match.NO_STRUCTURE


def _settle_copy(
    root: Path, plan: _Plan, pool: capture_match.Pool, rel: str, text: str, facts: CaptureFacts | None,
    copy: capture_match.Fingerprint, location: capture_match.Location,
) -> None:
    found = pool.find(copy)
    if not isinstance(found, str) and not capture_match.confirm(location, found):
        found = capture_match.NOT_CONFIRMED
    if isinstance(found, str):
        plan.no_match(rel, found)
    else:
        facts = replace(
            facts or CaptureFacts(), time=found.time, date=found.date, date_basis=None,
            date_source=capture_dates.source_for("perceptual-match"), date_matched_from=found.origin,
        )
    _finish_page(root, plan, rel, text, facts)


def _finish_page(root: Path, plan: _Plan, rel: str, text: str, facts: CaptureFacts | None) -> None:
    """Decide what the page gets: the block it would carry, or the reason it stays as it is."""
    if facts is None:
        # A file that states nothing, or one that no decoder reads: a backfill never strips a block on that evidence.
        plan.no_metadata += 1
        _count_source(plan, text)
        return
    try:
        updated = preserve.render_capture_block(text, facts)
    except preserve.PreserveError as refused:
        if refused.code != AMBIGUOUS_BOUNDARY:
            raise
        plan.skip(rel, f"{refused.code}: {refused.reason}")
        return
    dated = _count_source(plan, updated)
    if updated == text:
        plan.already_current += 1
        return
    plan.pages.append((rel, root / rel, vault.content_hash(text)))
    plan.facts[rel] = json.dumps(facts.as_payload(), sort_keys=True, separators=(",", ":"))
    if dated is not None:
        plan.gives[rel] = f"{capture_dates.recorded_source(dated.date_source)} {dated.time}" + (
            f" from {dated.date_matched_from}" if dated.date_matched_from else ""
        )
        if dated.date_matched_from and len(plan.matches) < LISTED_SKIPS:
            plan.matches.append(
                {"path": rel, "time": dated.time, "from": dated.date_matched_from}
            )


def _count_source(plan: _Plan, final_text: str) -> CaptureFacts | None:
    """Count the source of the date that `final_text` carries and return what it records; None when it has no date."""
    try:
        recorded = capture_metadata.recorded(vault.parse_frontmatter(final_text, custody=True)[0])
    except vault.FrontmatterError:
        return None
    if not recorded.time:
        plan.undated += 1
        return None
    source = str(capture_dates.recorded_source(recorded.date_source))
    plan.by_source[source] = plan.by_source.get(source, 0) + 1
    return recorded


def _reads_capture(original: str) -> bool:
    return media_types.media_type_for(original) in capture_metadata.KINDS


def _still_local(root: Path, snapshot: Any) -> bool:
    return originals.original_state(root, snapshot.path).state in ("local", "cached")


@contextmanager
def _local_view(snapshot: Any) -> Iterator[Path | None]:
    """A readable path to the original's bytes on the volume, under its own filename; None when none is.

    A decoder dispatches on the filename, and a shared object has none, so it appears under the logical
    name in a private directory. Nothing is fetched: bytes that are not on the volume have no view.
    """
    if snapshot.local_path is None:
        yield snapshot.path if snapshot.path.is_file() else None
        return
    with reserved_paths.private_copy_directory() as private:
        view = private / snapshot.path.name
        try:
            os.symlink(snapshot.local_path, view)
        except OSError:
            yield None
            return
        yield view


def _rewrite(plan: _Plan, rel: str, text: str) -> str:
    return preserve.render_capture_block(text, CaptureFacts.from_payload(json.loads(plan.facts[rel])))


def _commit_to_sidecars(root: Path):
    def commit(writes: list[vault.PlannedWrite]) -> object:
        return preserve.commit_media_sidecar_writes(root, tuple(writes))

    return commit
