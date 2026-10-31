"""Removal of a staged artifact once the Knowledge Base holds its identical copy.

Adoption removes a staged file only when a stage manifest (`import_stage`) lists it, the staged
bytes still match that entry and the plan, and the durable copy matches the plan. The staged file
is held while it is hashed and unlinked, so the file that goes is the file that was hashed. Every
refusal keeps the staged copy: a copy that stays costs disk, and a copy that goes wrongly costs data.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from . import adoption_plan, held_fs, import_stage

# The outcome statuses of an item that is in the Knowledge Base; a run, its residual report and a discard all read them here.
FILED_STATUSES = ("applied", "already-applied")  # nosemgrep: ep-word-set -- the statuses that `apply` writes: a closed protocol of the run object.
#: The reasons that mean the member is in the Knowledge Base although its staged copy stays; a residual record leaves them out.
FILED_REASONS = ("text", "kept")  # nosemgrep: ep-word-set -- two of the closed result keys that `_page_classifier` returns.
#: Why a staged file no scan of the run covers stays.
OUTSIDE_RUN = "outside_run"


def recorded(root: Path, run: dict) -> import_stage.Stage | None:
    """The manifest of the stage this run started in, as it is now; None when the run has no recorded stage.

    The manifest is read from the state folder each time it is needed, never copied into the run, so a
    discarded stage stops being consumable at once.
    """
    recorded_stage = run.get("stage")
    if not isinstance(recorded_stage, dict):
        return None
    return import_stage.read(root, recorded_stage.get("stage", ""))


def _member_path(stage: import_stage.Stage, original_path: str) -> str | None:
    """A staged file's path relative to its stage, or None when the file is not in this stage."""
    prefix = f"{import_stage.STAGE_DIR}/{stage.import_id}/"
    return original_path[len(prefix):] if original_path.startswith(prefix) else None


def entry_of(stage: import_stage.Stage, original_path: str) -> tuple[int, str] | None:
    """The manifest's (size, SHA-256) for a staged file given by its vault-relative path, or None if unlisted."""
    member_path = _member_path(stage, original_path)
    return None if member_path is None else stage.members.get(member_path)


def identity_of(stage: import_stage.Stage, original_path: str) -> import_stage.Identity | None:
    """The manifest's identity for a staged file, or None when it is unlisted or has none."""
    member_path = _member_path(stage, original_path)
    return None if member_path is None else stage.identities.get(member_path)


def vouched(stage: import_stage.Stage | None, original_path: str) -> tuple[import_stage.Identity, str] | None:
    """The identity and SHA-256 that a stage's manifest records for a file, or None when it records no identity.

    A file whose stat equals that identity holds the manifest's bytes: plan and scan take the SHA-256 from
    here, skip the ceiling and read nothing.
    """
    if stage is None:
        return None
    identity, entry = identity_of(stage, original_path), entry_of(stage, original_path)
    return None if identity is None or entry is None or entry[0] != identity.size else (identity, entry[1])


def _sha256_of(file: held_fs.HeldFile) -> str:
    with os.fdopen(os.dup(file.descriptor), "rb") as stream:
        stream.seek(0)
        return hashlib.file_digest(stream, "sha256").hexdigest()


def matches(file: held_fs.HeldFile, size: int, sha256: str) -> bool:
    """Whether the held file has these bytes, and was not written while it was read."""
    version = file.version()
    return version.size == size and _sha256_of(file) == sha256 and file.version() == version


def durable_matches(root: Path, artifact_path: str | None, planned: dict) -> bool:
    """Whether the Knowledge Base artifact has the planned bytes, read back in a stream."""
    if not artifact_path:
        return False
    parent, _, leaf = artifact_path.rpartition("/")
    try:
        with held_fs.acquire(root).require() as filesystem:
            with filesystem.parent(parent or ".").require() as directory:
                with filesystem.file(directory, leaf).require() as durable:
                    return matches(durable, planned["original_bytes"], planned["original_sha256"])
    except (held_fs.HeldFsError, OSError):
        return False


def remove_staged(root: Path, stage: import_stage.Stage, planned: dict) -> bool:
    """Unlink the item's staged copy when it is the file the manifest and the plan describe; True when it went.

    The caller has already shown that the durable copy matches. A copy that does not match, is
    unlisted, is already gone or cannot be unlinked stays, and the item stays applied.
    """
    entry = entry_of(stage, planned["original_path"])
    if entry != (planned["original_bytes"], planned["original_sha256"]):
        return False
    parent, _, leaf = planned["original_path"].rpartition("/")
    try:
        with held_fs.acquire(root).require() as filesystem:
            with filesystem.parent(parent, access="mutate").require() as directory:
                with filesystem.file(directory, leaf, access="mutate").require() as staged:
                    if not matches(staged, *entry):
                        return False
                    filesystem.unlink(staged).require()
    except (held_fs.HeldFsError, OSError):
        return False
    return True


def _page_classifier(page: dict, filed_statuses: tuple[str, ...]):
    """Why a staged file stays, as one page of the scan sees it; the classifier returns None for a file the page did not scan."""
    inventory = {row["path"]: row for row in page.get("inventory") or []}
    plan = page.get("plan") or {}
    items = {item["original_path"]: item for item in plan.get("items") or []}
    skipped = {row.get("path"): row.get("code") for row in plan.get("skipped") or []}
    selected = set((page.get("selection") or {}).get("paths") or [])
    outcomes = page.get("outcomes") or {}

    def classify(path: str) -> str | None:
        item = items.get(path)
        if item is not None:
            outcome = outcomes.get(path) or {}
            status = outcome.get("status")
            if status == "failed":
                return "over_ceiling" if outcome.get("code") == adoption_plan.OVER_CEILING else "failed"
            if status in filed_statuses:
                return "text" if item["form"] == "text" else "kept"
            return "pending"
        if path in skipped:
            return "over_ceiling" if skipped[path] == adoption_plan.OVER_CEILING else "ineligible"
        row = inventory.get(path)
        if row is not None:
            if not row.get("eligible"):
                return "over_ceiling" if row.get("reason") == adoption_plan.OVER_CEILING else "ineligible"
            return "pending" if path in selected else "unselected"
        ancestors = (path.rsplit("/", depth)[0] for depth in range(1, path.count("/") + 1))
        if any((inventory.get(ancestor) or {}).get("kind") == "folder" for ancestor in ancestors):
            return "ineligible"  # a hidden folder that the scan did not enter
        return None

    return classify


def stage_reasons(
    root: Path, import_id: str, pages: tuple[dict, ...], filed_statuses: tuple[str, ...]
) -> list[tuple[str, int, str]]:
    """Each file of the stage as (path relative to the stage, bytes, why it stays); `pages` are the scans, nearest first.

    A file that no page scanned (hidden, added after the scan, on a page not yet scanned) is `OUTSIDE_RUN`.
    """
    classifiers = [_page_classifier(page, filed_statuses) for page in pages]
    prefix = f"{import_stage.STAGE_DIR}/{import_id}/"

    def why(path: str) -> str:
        for classify in classifiers:
            reason = classify(prefix + path)
            if reason is not None:
                return reason
        return OUTSIDE_RUN

    return [(path, size, why(path)) for path, size in import_stage.staged_files(root, import_id)]


def residual(root: Path, run: dict, filed_statuses: tuple[str, ...], earlier_pages: tuple[dict, ...] = ()) -> dict | None:
    """What is still in the run's stage, by why it stays, with counts and bytes; None for a run with no stage.

    `filed_statuses` are the outcome statuses of an item that is in the Knowledge Base. `earlier_pages` are
    the earlier pages of a paged scan, nearest first; each file is classified by the page that scanned it, so
    the last page's report covers the whole stage. The reasons are a closed set of result keys that this
    report defines, not vocabulary: `failed`, `text`, `kept`, `pending`, `unselected`, `ineligible`,
    `over_ceiling` (above the adoption ceiling, which `ceiling_bytes` then reports) and `outside_run` (hidden
    or added after the scan, or on a later page not yet scanned). The Cloud route discards a stage with
    `transfer_memory`; a local import has no Exomem command, so its stage goes by hand. Both name the whole
    stage folder, which holds the residual and nothing else. A page that later pages follow names no discard
    route, because the folder still holds their files: `more_pages` says so.
    """
    recorded_stage = run.get("stage")
    if not isinstance(recorded_stage, dict):
        return None
    folder = f"{import_stage.STAGE_DIR}/{recorded_stage['stage']}"
    by_reason: dict[str, dict[str, int]] = {}
    for _path, size, reason in stage_reasons(root, recorded_stage["stage"], (run, *earlier_pages), filed_statuses):
        entry = by_reason.setdefault(reason, {"files": 0, "bytes": 0})
        entry["files"] += 1
        entry["bytes"] += size
    files = sum(entry["files"] for entry in by_reason.values())
    route = (
        f"call transfer_memory with action import_discard, import_id {recorded_stage['stage']} and confirm=true"
        if recorded_stage.get("proof") == import_stage.CLOUD_PROOF
        else f"delete the folder {folder} by hand; no Exomem command discards a local import"
    )
    more_pages = bool((run.get("paging") or {}).get("more"))
    report = {
        "folder": folder, "files": files, "bytes": sum(entry["bytes"] for entry in by_reason.values()),
        "by_reason": by_reason, "more_pages": more_pages, "discard": route if files and not more_pages else None,
    }
    if "over_ceiling" in by_reason:
        from . import client_artifacts

        report["ceiling_bytes"] = client_artifacts.adoption_ceiling()
    return report


def remove_empty_folders(root: Path, run: dict) -> int:
    """Remove the empty folders consuming left in the run's stage, deepest first; returns how many went.

    A folder goes through the held filesystem and only while it is empty, so a file that arrives
    during the walk keeps its folder. The `_Imports` folder itself is never removed.
    """
    recorded_stage = run.get("stage")
    if not isinstance(recorded_stage, dict):
        return 0
    base = root / import_stage.STAGE_DIR / recorded_stage["stage"]
    removed = 0
    try:
        with held_fs.acquire(root).require() as filesystem:
            for directory, _names, _files in os.walk(base, topdown=False):
                found = filesystem.parent(Path(directory).relative_to(root).as_posix(), access="mutate")
                if not found.ok:
                    continue
                with found.require() as held:
                    removed += int(filesystem.unlink_directory(held).ok)
    except (held_fs.HeldFsError, OSError):
        pass
    return removed
