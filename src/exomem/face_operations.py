"""The owner's face operations: `faces.status`, `faces.set` and `faces.name` (design D9).

Each action first asks `face_search.require_owner`, which decides from the caller's principal alone, so
no other caller ever opens the face store. Turning faces on happens only in `exomem faces on`, run by a
person who reads the disclosure and types the confirmation; `faces.set` with on answers every agent
surface with that disclosure and the command, and changes nothing.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import face_groups, face_naming, face_scan, face_search, face_store
from .cli_ops import OpError

#: What a person reads before turning faces on, on every surface that mentions turning them on.
DISCLOSURE = (
    "Turning faces on finds the faces in your pictures and video keyframes and stores, for each face, its box "
    "and a face vector that can recognise the person. Similar faces form groups, and the names you give "
    "groups let a search show the pictures of a named person. All of it stays in this account's state "
    "directory, outside the vault: it is never written into a page, an export or a search result, and it "
    "never leaves this account. Only you, the owner, can read it or search by person. "
    "`exomem faces off` stops it and keeps what is stored; `exomem faces delete` erases it."
)
#: The text a person types, or passes with `--confirm`, to turn faces on.
CONFIRMATION = "turn faces on"
TURN_ON_COMMAND = "exomem faces on"
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
    if action == "set":
        return change_setting(vault_root, state=state, thresholds=thresholds)
    return change_name(vault_root, group=group, person=person, face=face, name=name, clear=clear)


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
    status never creates the face store.
    """
    setting = face_store.read_setting(vault_root)
    on = setting is not None and setting.enabled
    unavailable = face_store.unavailable_reason(setting)
    counts = face_groups.counts(vault_root)
    if not on:
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
    if not on:
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
    """Refuse a change to groups, names or thresholds unless faces are on and may run on this deployment."""
    setting = face_store.read_setting(vault_root)
    if setting is None or not setting.enabled:
        raise OpError(
            "FACES_OFF", "Faces are off for this account, so nothing changed.",
            f"The owner turns faces on with `{TURN_ON_COMMAND}` in a terminal.",
        )
    refusal = face_store.deployment_refusal()
    if refusal is not None:
        raise face_search.faces_unavailable(refusal)


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
    known = face_groups.defaults()
    changes: dict[str, float | None] = {}
    for name, value in thresholds.items():
        if name not in known:
            raise OpError("INVALID_ARGUMENT", f"There is no face threshold named {name!r}, so nothing changed.",
                          f"Use one of: {', '.join(sorted(known))}.")
        if value is None:
            changes[name] = None
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise OpError("INVALID_ARGUMENT", f"The threshold {name} must be a number of zero or more, so nothing changed.",
                          "Pass a number, or null to return to the default.")
        changes[name] = float(value)
    return changes


def turn_off(vault_root: Path) -> dict[str, Any]:
    """Turn faces off: the setting first, so no new face work starts, then stop the backfill and unload.

    Stored faces, groups and names stay. A store that was never created stays uncreated.
    """
    if not face_store.enabled(vault_root):
        return {"outcome": "Faces are already off.", "state": "off"}
    with closing(face_store.connect_index(vault_root)) as index:
        face_store.record_opt_out(index)
    face_scan.stop_backfill(vault_root, timeout=30)
    face_scan.unload()
    return {
        "outcome": "Faces are off. Stored faces and names are kept until the owner deletes them.",
        "state": "off",
        "changed": True,
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
