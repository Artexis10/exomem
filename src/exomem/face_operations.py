"""The owner's face operations: `faces.status`, `faces.set`, `faces.name` and `faces.delete` (design D9, D11).

Each action first asks `face_search.require_owner`, which decides from the caller's principal alone, so
no other caller ever opens the face store. Turning faces on happens only in `exomem faces on`, run by a
person who reads the disclosure and types the confirmation; `faces.set` with on answers every agent
surface with that disclosure and the command, and changes nothing.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import face_groups, face_naming, face_scan, face_search, face_store
from .cli_ops import OpError

log = logging.getLogger(__name__)

#: What a person reads before turning faces on, on every surface that mentions turning them on.
DISCLOSURE = (
    "Turning faces on finds the faces in your pictures and video keyframes and stores, for each face, its box "
    "and a face vector that can recognise the person. Similar faces form groups, and the names you give "
    "groups let a search show the pictures of a named person. All of it stays in this account's state "
    "directory, outside the vault: it is never written into a page, an export or a search result. "
    "Only you, the owner, can read it or search by person. The names you give, with each named group's "
    "averaged face vector, go into this account's backups, and a backup made before a delete keeps them "
    "until its retention expires it. "
    "`evermatter faces off` stops it and keeps what is stored; `evermatter faces delete` erases it."
)
#: The text a person types at a terminal to turn faces on.
CONFIRMATION = "turn faces on"
TURN_ON_COMMAND = "evermatter faces on"
#: What a delete cannot reach on every deployment (design D11). `residues()` adds the backups this deployment keeps.
_FREED_BLOCKS = {
    "residue": "freed disk blocks",
    "holds": "the bytes of the removed files",
    "ends": "when the filesystem reuses them; whether they are encrypted until then depends on the disk",
}


def residues() -> list[dict[str, str]]:
    """What a delete on this deployment cannot reach, and when each ends. The delete result and status show them.

    Retention lives with each deployment's backup (the owner's keep settings, a Cloud cell's backup schedule),
    so no count is repeated here.
    """
    from . import cloud_cell

    people = "the people store: named people and their groups' centroids"
    if cloud_cell.cloud_mode_enabled():
        return [
            {"residue": "older backups", "holds": people,
             "ends": "when the Cloud cell's backup retention expires the backups made before the delete"},
            {"residue": "a running Cloud cell backup's volume snapshot",
             "holds": "the state volume as it was when that backup started",
             "ends": "when that backup ends and deletes its snapshot"},
            _FREED_BLOCKS,
        ]
    if cloud_cell.hosted_mode_enabled():
        return [
            {"residue": "older backups", "holds": people,
             "ends": "when the operator's backup retention removes the backups made before the delete"},
            _FREED_BLOCKS,
        ]
    return [
        {"residue": "older backups", "holds": people,
         "ends": "when the keep settings of the owner's backup prune the backups made before the delete"},
        _FREED_BLOCKS,
    ]


#: Groups per status page by default, and at most.
GROUPS_PER_PAGE, MAX_GROUPS_PER_PAGE = 20, 100
SAMPLES_PER_GROUP = 3


def run(
    vault_root: Path,
    action: str,
    *,
    state: str | None = None,
    thresholds: Mapping[str, Any] | None = None,
    page: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    group: int | None = None,
    person: str | None = None,
    face: int | None = None,
    name: str | None = None,
    clear: bool = False,
) -> dict[str, Any]:
    face_search.require_owner(vault_root)
    if action == "status":
        return status(vault_root, page=page, limit=limit, cursor=cursor)
    if action == "delete":
        return delete(vault_root)
    try:
        if action == "set":
            return change_setting(vault_root, state=state, thresholds=thresholds)
        return change_name(vault_root, group=group, person=person, face=face, name=name, clear=clear)
    except sqlite3.DatabaseError as error:
        if face_store.is_busy(error):
            raise  # Another connection holds the store: it is readable, and the caller retries.
        raise face_search.faces_unreadable(error) from error


# --- status ---------------------------------------------------------------------------------------


def _after(cursor: str | None) -> tuple[int, int] | None:
    if not cursor:
        return None
    count, _, group = cursor.partition(":")
    if not (count.isdigit() and group.isdigit()):
        raise OpError("INVALID_ARGUMENT", "The cursor is not one that faces.status returned.",
                      "Pass the `next_cursor` of the previous status, or no cursor for the first page.")
    return int(count), int(group)


def _sample(sample: face_groups.Sample) -> dict[str, Any]:
    shown: dict[str, Any] = {"face": sample.face, "page": sample.page, "region": list(sample.region)}
    if sample.frame_ts is not None:
        shown["frame_ts"] = sample.frame_ts
    return shown


def status(vault_root: Path, *, page: str | None, limit: int | None, cursor: str | None) -> dict[str, Any]:
    """On or off, availability, the consent record, progress, counts, thresholds and one page of groups.

    With `page`, the faces on that picture and their groups instead of the group list. Reads only: a
    status never creates the face store. A face store this release cannot read answers `unreadable`.
    """
    try:
        return _status(vault_root, page=page, limit=limit, cursor=cursor)
    except sqlite3.DatabaseError as error:
        if face_store.is_busy(error):
            raise  # Another connection holds the store: it is readable, and the caller retries.
        return _unreadable(error)


def _unreadable(error: sqlite3.DatabaseError) -> dict[str, Any]:
    # The face step skips every picture and a removal keeps its CLIP rows until the store goes, so the owner
    # must learn the fix here.
    newer = isinstance(error, face_store.NewerStore)
    cause = "a newer release of Evermatter wrote it" if newer else "it is damaged"
    return {
        "outcome": f"The face data cannot be read: {cause}. No picture is scanned for faces until it is fixed.",
        "state": "unreadable",
        "available": False,
        "unavailable_reason": cause,
        "next_action": (
            ("Run the release that wrote it, or the" if newer else "The")
            + " owner erases the face data with faces.delete, and turns faces on again to scan the pictures anew."
        ),
        "diagnostics": {"error": str(error)},
    }


def _status(vault_root: Path, *, page: str | None, limit: int | None, cursor: str | None) -> dict[str, Any]:
    setting = face_store.read_setting(vault_root)
    on = setting is not None and setting.enabled
    unavailable = face_store.unavailable_reason(setting)
    counts = face_groups.counts(vault_root)
    if face_store.deployment_refusal() is not None:
        outcome = f"Faces cannot run on this deployment: {unavailable}."
    elif not on:
        outcome = "Faces are off." + (" Stored faces and names are kept." if counts.faces or counts.people else "")
    elif unavailable is not None:
        outcome = f"Faces are on but cannot run here: {unavailable}."
    else:
        outcome = "Faces are on."
    progress = face_scan.progress(vault_root) if setting is not None else None
    result: dict[str, Any] = {
        "outcome": outcome,
        "state": "on" if on else "off",
        "available": unavailable is None,
        "unavailable_reason": unavailable,
        "consent": (
            {"at": setting.consent_at, "surface": setting.consent_surface}
            if setting is not None and setting.consent_at else None
        ),
        "progress": None if progress is None else {
            "pictures_scanned": progress.processed,
            "pictures_total": "unknown" if progress.total is None else progress.total,
            "running": face_scan.backfill_running(vault_root),
        },
        "counts": {"faces": counts.faces, "groups": counts.groups, "people": counts.people},
        "thresholds": _thresholds(vault_root),
    }
    receipt = face_store.read_deletion_receipt(vault_root)
    if receipt is not None:
        result["last_deletion"] = receipt
        result["residues"] = residues()
    refusal = face_store.deployment_refusal()
    if refusal is not None:
        # The setting's name is operator configuration, documented in design D9a; the customer's agent cannot set it.
        result["next_action"] = (f"Faces cannot run here: {refusal}. Search without people; only the "
                                 "deployment's operator can enable faces.")
    elif not on:
        result["next_action"] = f"To turn faces on, the owner runs `{TURN_ON_COMMAND}` in a terminal."
    if page is not None:
        faces = face_groups.page_faces(vault_root, page)
        result["page"] = page
        result["faces"] = [
            {**_sample(sample), "group": group_id, "person": person} for sample, group_id, person in faces
        ]
        result["outcome"] += f" {len(faces)} face{'s' if len(faces) != 1 else ''} on this picture."
        return result
    size = GROUPS_PER_PAGE if limit is None else max(1, min(int(limit), MAX_GROUPS_PER_PAGE))
    groups = face_groups.groups_by_size(vault_root, limit=size, after=_after(cursor), per_group=SAMPLES_PER_GROUP)
    result["groups"] = [
        {"group": entry.group, "face_count": entry.face_count, "person": entry.person,
         "samples": [_sample(sample) for sample in entry.samples]}
        for entry in groups
    ]
    result["next_cursor"] = f"{groups[-1].face_count}:{groups[-1].group}" if len(groups) == size else None
    return result


def _thresholds(vault_root: Path) -> dict[str, Any]:
    defaults = face_groups.defaults()
    overrides = face_groups.overrides(vault_root)
    return {
        "in_force": {name: overrides.get(name, value) for name, value in defaults.items()},
        "defaults": defaults,
        "overrides": {name: value for name, value in overrides.items() if name in defaults},
        "note": "A changed threshold applies to faces grouped afterwards; existing groups stay as they are.",
    }


# --- set --------------------------------------------------------------------------------------------


def _require_on(vault_root: Path) -> None:
    """Refuse a change to groups, names or thresholds unless faces may run on this deployment and are on."""
    refusal = face_store.deployment_refusal()
    if refusal is not None:
        raise face_search.faces_unavailable(refusal)
    setting = face_store.read_setting(vault_root)
    if setting is None or not setting.enabled:
        raise OpError(
            "FACES_OFF", "Faces are off for this account, so nothing changed.",
            f"The owner turns faces on with `{TURN_ON_COMMAND}` in a terminal.",
        )


def turn_on_answer() -> dict[str, Any]:
    """What `faces.set` with on answers an agent: the disclosure and the command. It changes nothing."""
    return {
        "outcome": (
            f"Faces stay off: only the owner turns them on, by running `{TURN_ON_COMMAND}` in a terminal "
            "after reading what it stores."
        ),
        "state": "off",
        "changed": False,
        "disclosure": DISCLOSURE,
        "next_action": f"Show the owner the disclosure and the command `{TURN_ON_COMMAND}`; do not run it for them.",
    }


def change_setting(vault_root: Path, *, state: str | None, thresholds: Mapping[str, Any] | None) -> dict[str, Any]:
    if state is None and not thresholds:
        raise OpError("INVALID_ARGUMENT", "faces.set needs a state or thresholds, so nothing changed.",
                      "Pass state off, or thresholds such as {\"join_margin\": 0.08}.")
    if state == "on":
        refusal = face_store.deployment_refusal()
        if refusal is not None:
            raise face_search.faces_unavailable(refusal)
        if face_store.enabled(vault_root):
            return {"outcome": "Faces are already on.", "state": "on", "changed": False}
        return turn_on_answer()
    if state is not None and state != "off":
        raise OpError("INVALID_ARGUMENT", f"faces.set takes state on or off, not {state!r}.", "Pass state off.")
    result: dict[str, Any] = {"changed": False}
    if thresholds:
        _require_on(vault_root)
        changes = _threshold_changes(thresholds)
        face_groups.set_overrides(vault_root, changes)
        result.update(changed=True, thresholds=_thresholds(vault_root))
    if state == "off":
        result.update(turn_off(vault_root))
    else:
        result["outcome"] = "Thresholds saved. Faces grouped from now on use them; existing groups stay as they are."
        result["state"] = "on"
    return result


def _threshold_changes(thresholds: Mapping[str, Any]) -> dict[str, float | None]:
    known = face_groups.ranges()
    changes: dict[str, float | None] = {}
    for name, value in thresholds.items():
        if name not in known:
            raise OpError("INVALID_ARGUMENT", f"There is no face threshold named {name!r}, so nothing changed.",
                          f"Use one of: {', '.join(sorted(known))}.")
        if value is None:
            changes[name] = None
            continue
        low, high = known[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
                or not low <= value <= high:
            allowed = f"from {low:g} to {high:g}" if math.isfinite(high) else f"of {low:g} or more"
            raise OpError("INVALID_ARGUMENT", f"The threshold {name} must be a number {allowed}, so nothing changed.",
                          "Pass a number in that range, or null to return to the default.")
        changes[name] = float(value)
    return changes


def turn_off(vault_root: Path) -> dict[str, Any]:
    """Turn faces off: the setting first, so no new face work starts, then stop the backfill and unload.

    Stored faces, groups and names stay. A store that was never created stays uncreated.
    """
    try:
        enabled = face_store.enabled(vault_root)
    except sqlite3.DatabaseError as error:
        if face_store.is_busy(error):
            raise  # Another connection holds the store: it is readable, and the caller retries.
        if isinstance(error, face_store.NewerStore):
            # The release that wrote the store still reads faces as on; an off recorded nowhere would be lost there.
            raise OpError(
                "FACES_UNREADABLE",
                "A newer release of Exomem wrote the face data, so this release cannot turn faces off. Nothing changed.",
                "Turn faces off in the release that wrote the face data, or the owner erases it with faces.delete.",
                details={"error": str(error)},
            ) from error
        # No face work runs on a damaged store, so off is already in effect.
        answer = _unreadable(error)
        answer["outcome"] = "Faces are already off in effect: the face data cannot be read, so no picture is scanned."
        answer["next_action"] = "To erase the damaged face data, the owner runs faces.delete. Nothing else is needed."
        return answer
    if not enabled:
        return {"outcome": "Faces are already off.", "state": "off"}
    with closing(face_store.connect_index(vault_root)) as index:
        face_store.record_opt_out(index)
    face_scan.stop_backfill(vault_root, timeout=30)
    face_scan.unload()
    face_groups.release(vault_root)
    return {
        "outcome": "Faces are off. Stored faces and names are kept until the owner deletes them.",
        "state": "off",
        "changed": True,
    }


def delete(vault_root: Path) -> dict[str, Any]:
    """Erase the face store: turn faces off, wait for face work in flight, count, then remove every file.

    Removes both databases, their `-wal`, `-shm` and `-journal` files and the directory. A file that cannot
    be removed makes the outcome `partial`, naming it; a repeat finishes the delete. The receipt keeps the
    time and counts for `faces.status`, never face data.
    """
    from . import find

    try:
        turn_off(vault_root)
    except (sqlite3.DatabaseError, OpError) as error:
        # A damaged or newer face index cannot record "off". Erasing it below turns faces off all the same.
        # Any other refusal is not about the store's readability, so the delete stops on it.
        if isinstance(error, OpError) and error.code != "FACES_UNREADABLE":
            raise
        log.warning("faces: the face index could not record off before its delete", exc_info=True)
    # Faces may have been off already, with an earlier backfill still finishing its picture.
    face_scan.stop_backfill(vault_root, timeout=30)
    face_scan.unload()
    earlier = face_store.read_deletion_receipt(vault_root)
    finishing = earlier is not None and earlier.get("deletion") == "partial"
    if not face_store.store_dir(vault_root).exists() and not finishing:
        already = earlier is not None
        return {
            "outcome": "The face store was already deleted; nothing remains." if already
            else "Nothing to delete: this account has no face store.",
            "state": "off",
            "deletion": "already_deleted" if already else "nothing_stored",
            "last_deletion": earlier,
            "residues": residues(),
        }
    # A repeat that finds no face index finishes an earlier partial delete: nothing was scanned since, so the
    # counts are the first attempt's. With a face index, faces were turned on again and the count is fresh.
    carried = finishing and not face_store.index_path(vault_root).is_file()
    try:
        found, remaining = face_groups.erase(vault_root)
    except sqlite3.OperationalError:
        raise OpError("FACES_BUSY", "Face work kept the face store busy, so nothing was deleted.",
                      "Repeat faces.delete in a few seconds.", details={"retry_after_ms": 5000}) from None
    # Cached searches by person hold pages found through faces. A new face store starts a new generation epoch.
    find.reset_result_cache()
    counted: dict[str, int] | str | None
    if carried:
        counted = earlier.get("counts")
    elif found is None:
        counted = "unknown"
    else:
        counted = {"faces": found.faces, "groups": found.groups, "people": found.people, "pages": found.pages}
    deletion = "partial" if remaining else "deleted"
    face_store.write_deletion_receipt(vault_root, {
        "deleted_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "deletion": deletion,
        "counts": counted,
        "remaining": remaining,
    })
    if remaining:
        from . import media_worker

        # On Windows a media child keeps its store connections, and so the files, open until it exits idle.
        minutes = max(1, math.ceil(media_worker.idle_seconds_setting() / 60))
        sentence = (f"Faces are off, but the delete is partial: {', '.join(remaining)} could not be removed. "
                    "Repeat faces.delete to finish it. If it stays partial, a running Evermatter process still holds "
                    f"the files: repeat it once no picture has been processed for {minutes} "
                    f"minute{'s' if minutes != 1 else ''}, when the media worker closes them, or after the "
                    "service restarts if it still stays partial.")
    else:
        sentence = "Faces are off, and every face, group and name in this account's face store is deleted."
    return {
        "outcome": sentence,
        "state": "off",
        "deletion": deletion,
        "counts": counted,
        "remaining": remaining,
        "residues": residues(),
        "next_action": f"Search by person stays off until the owner runs `{TURN_ON_COMMAND}` again.",
    }


def turn_on(vault_root: Path) -> bool:
    """Record the person's consent, given in the CLI after the disclosure, and start the backfill.

    Returns whether the backfill started; it does not when the face models cannot run here. Raises
    `FACES_UNAVAILABLE` on a deployment that does not run faces, before anything is stored.
    """
    refusal = face_store.deployment_refusal()
    if refusal is not None:
        raise face_search.faces_unavailable(refusal)
    with closing(face_store.connect_index(vault_root)) as index:
        face_store.record_opt_in(index, at=datetime.now(UTC).isoformat(timespec="seconds"), surface="cli")
    return face_scan.start_backfill(vault_root)


# --- name -------------------------------------------------------------------------------------------


def change_name(
    vault_root: Path, *, group: int | None, person: str | None, face: int | None, name: str | None, clear: bool
) -> dict[str, Any]:
    targets = [target for target in (group, person, face) if target is not None]
    if len(targets) != 1:
        raise OpError("INVALID_ARGUMENT", "faces.name changes one group, person or face per call, so nothing changed.",
                      "Pass exactly one of group, person or face.")
    if face is None and clear == (name is not None):
        raise OpError("INVALID_ARGUMENT", "faces.name needs either a name or clear, so nothing changed.",
                      "Pass name to name or rename, or clear true to remove the name.")
    _require_on(vault_root)
    try:
        if face is not None:
            new_group = face_naming.move_face_out(vault_root, int(face))
            return {"outcome": f"The face is out of its group and in a new group {new_group} of its own; "
                               "it never rejoins the old group.", "group": new_group, "changed": True}
        if group is not None and clear:
            face_naming.clear_group(vault_root, int(group))
            return {"outcome": f"Group {group} has no name now.", "group": int(group), "changed": True}
        if group is not None:
            face_naming.name_group(vault_root, int(group), str(name))
            return {"outcome": f"Group {group} is now {str(name).strip()}.", "group": int(group), "changed": True}
        if clear:
            face_naming.clear_person(vault_root, str(person))
            return {"outcome": f"{str(person).strip()} is no longer a name on any group.", "changed": True}
        face_naming.rename_person(vault_root, str(person), str(name))
        return {"outcome": f"{str(person).strip()} is now {str(name).strip()}.", "changed": True}
    except face_naming.NameRefused as refusal:
        raise OpError(refusal.code, f"{refusal} Nothing changed.",
                      "faces.status lists the groups with their faces and people.") from None
