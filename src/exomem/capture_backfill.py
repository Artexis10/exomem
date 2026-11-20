"""Add capture metadata to the sidecars of photos and videos that were filed before it was recorded.

The owner starts this job. It reads the originals that sit on the volume and replaces only the capture
block of each sidecar (`preserve.render_capture_block`): the extracted text, the preserved notes and every
other field stay as written. It never fetches an original. A sidecar whose original has no bytes on the
volume, or whose preserved notes have no machine-owned boundary, is skipped and counted, and a rerun
adds what a later arrival of the bytes makes readable. The pages commit in short holds of the mutation
boundary through the sidecar's own writer, so a store-mode vault takes them through its store.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import (
    access,
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
from .vault_store import read

MODE = "capture-metadata"
_OPERATION = "capture_metadata_chunk"
#: Skipped pages that a preview lists by path. The report always carries each reason's exact count.
LISTED_SKIPS = 200

BYTES_NOT_ON_VOLUME = "bytes not on the volume"
AMBIGUOUS_BOUNDARY = "AMBIGUOUS_SIDECAR_BOUNDARY"
READER_UNAVAILABLE = "this host has no decoder for the original's kind"
RETIREMENT_UNCONFIRMED = "the Trash retirement of the original is unconfirmed"


class _Plan:
    """What one pass over the archive found: the pages to rewrite, and each skip with its reason."""

    def __init__(self) -> None:
        self.pages: list[tuple[str, Path, str]] = []
        self.facts: dict[str, str] = {}
        self.already_current = 0
        self.no_metadata = 0
        self.skipped: dict[str, int] = {}
        self.listed: list[tuple[str, str]] = []

    def skip(self, page: str, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1
        if len(self.listed) < LISTED_SKIPS:
            self.listed.append((page, reason))

    def entries(self) -> list[tuple[str, str, str]]:
        """What the `plan_id` binds: each page to rewrite, the text it is rewritten from and the facts it gets."""
        return [(rel, digest, self.facts[rel]) for rel, _path, digest in self.pages]


def backfill(vault_root: Path, *, dry_run: bool = True, plan_id: str | None = None) -> dict:
    """Preview, or add, the capture block of every sidecar whose original states one.

    The owner alone runs it. A write that names a `plan_id` refuses a plan that changed since its preview.
    """
    root = Path(vault_root).resolve()
    maintenance_jobs.require_owner(root)
    maintenance_jobs.checkpoint("planning")
    plan = _plan(root)
    fresh = maintenance_jobs.plan_identity(MODE, {}, plan.entries())
    report: dict[str, Any] = {
        "dry_run": dry_run,
        "plan_id": fresh,
        "would_update": len(plan.pages),
        "already_current": plan.already_current,
        "no_capture_metadata": plan.no_metadata,
        "skipped": dict(plan.skipped),
        "skipped_listed": [{"path": page, "reason": reason} for page, reason in plan.listed],
    }
    if dry_run:
        maintenance_jobs.record_planned((rel, None) for rel, _path, _digest in plan.pages)
        maintenance_jobs.record_planned(plan.listed, kind=maintenance_jobs.SKIPPED_PAGE)
        return report
    if plan_id is not None:
        maintenance_jobs.require_current_plan(MODE, plan_id, fresh, "the sidecars that lack their capture metadata")
    committed, moved = maintenance_jobs.commit_pages(
        root, _OPERATION, plan.pages, lambda rel, text: _rewrite(plan, rel, text), commit=_commit_to_sidecars(root)
    )
    return {**report, "plan_id": plan_id or fresh, "updated": len(committed), "skipped_changed": len(moved)}


def _plan(root: Path) -> _Plan:
    # One store snapshot answers the page census and each original's state, so the plan names one revision.
    with read.request_snapshot(root):
        return _read_plan(root)


def _read_plan(root: Path) -> _Plan:
    plan = _Plan()
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
        _plan_page(root, plan, rel, data.decode("utf-8"), snapshot)
    return plan


def _plan_page(root: Path, plan: _Plan, rel: str, text: str, snapshot: Any) -> None:
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
    with _local_view(snapshot) as view:
        facts = capture_metadata.read(view, kind) if view is not None else None
    if view is None or (facts is None and not _still_local(root, snapshot)):
        plan.skip(rel, BYTES_NOT_ON_VOLUME)  # it left the volume between the census and the read
        return
    if facts is None:
        # A file that states nothing, or one that no decoder reads: a backfill never strips a block on that evidence.
        plan.no_metadata += 1
        return
    try:
        updated = preserve.render_capture_block(text, facts)
    except preserve.PreserveError as refused:
        if refused.code != AMBIGUOUS_BOUNDARY:
            raise
        plan.skip(rel, f"{refused.code}: {refused.reason}")
        return
    if updated == text:
        plan.already_current += 1
        return
    plan.pages.append((rel, root / rel, vault.content_hash(text)))
    plan.facts[rel] = json.dumps(facts.as_payload(), sort_keys=True, separators=(",", ":"))


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
