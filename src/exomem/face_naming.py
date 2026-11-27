"""The owner's names and corrections on face groups (design D6 and D7).

A person owns one or more groups. Naming a group with a name that a person already has, compared by
Unicode NFKC case folding, adds the group to that person; the groups keep their own centroids. Renaming a
person to another person's name joins the two. Clearing a name leaves the groups unnamed. Moving a face
out of its group records an exclusion keyed by page, frame and box, so the face never joins that group
again, even after the face index is rebuilt.

Naming a group moves it from the face index into the people store, with its id, centroid and exclusions;
clearing its name moves it back. The people store therefore holds only people and their named groups, and
a backup never carries an unnamed group's centroid, which for a one-face group is a stranger's face.

Every change advances the face index generation, so a cached search never outlives it. A person left
with no group is removed: a name exists only while it names a group.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np

from . import face_store
from .face_groups import Writing, new_group_id


class NameRefused(ValueError):
    """The change names a group, person or face that does not exist, or gives an empty name."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _folded(name: str) -> tuple[str, str]:
    display = name.strip()
    if not display:
        raise NameRefused("EMPTY_NAME", "Give the person a name that is not empty.")
    return display, face_store.fold_name(display)


def _person(people: sqlite3.Connection, name: str) -> int:
    _, folded = _folded(name)
    row = people.execute("SELECT id FROM people WHERE folded_name = ?", (folded,)).fetchone()
    if row is None:
        raise NameRefused("UNKNOWN_PERSON", f"No person is named {name.strip()!r}.")
    return int(row[0])


def _group(writing: Writing, group_id: int) -> int | None:
    """The person a group belongs to, or None for an unnamed group. Refuses a group no store holds."""
    row = writing.people.execute("SELECT person_id FROM group_people WHERE group_id = ?", (group_id,)).fetchone()
    if row is not None:
        return int(row[0])
    if writing.index.execute("SELECT 1 FROM groups WHERE id = ?", (group_id,)).fetchone() is None:
        raise NameRefused("UNKNOWN_GROUP", f"No face group has id {group_id}.")
    return None


def _drop_if_unused(people: sqlite3.Connection, person_id: int | None) -> None:
    if person_id is not None:
        people.execute(
            "DELETE FROM people WHERE id = ? AND NOT EXISTS (SELECT 1 FROM group_people WHERE person_id = ?)",
            (person_id, person_id),
        )


_GROUP_COLUMNS = "id, model_id, centroid_sum, face_count"
_EXCLUSION_COLUMNS = "group_id, page, frame_ts, x, y, width, height"


def _move(source: sqlite3.Connection, target: sqlite3.Connection, group_ids: list[int]) -> None:
    """Move groups with their exclusions from one store to the other, keeping their ids.

    Four statements whatever the number of groups. Deleting the groups in the source removes their
    exclusions there too.
    """
    wanted = json.dumps(group_ids)
    groups = source.execute(
        f"SELECT {_GROUP_COLUMNS} FROM groups WHERE id IN (SELECT value FROM json_each(?))", (wanted,)
    ).fetchall()
    exclusions = source.execute(
        f"SELECT {_EXCLUSION_COLUMNS} FROM exclusions WHERE group_id IN (SELECT value FROM json_each(?))", (wanted,)
    ).fetchall()
    target.executemany(f"INSERT OR REPLACE INTO groups ({_GROUP_COLUMNS}) VALUES (?, ?, ?, ?)", groups)
    target.executemany(f"INSERT INTO exclusions ({_EXCLUSION_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)", exclusions)
    source.execute("DELETE FROM groups WHERE id IN (SELECT value FROM json_each(?))", (wanted,))


def _unname(writing: Writing, group_ids: list[int]) -> None:
    """Take the name off groups: they move back to the face index, out of what backups keep."""
    writing.people.execute(
        "DELETE FROM group_people WHERE group_id IN (SELECT value FROM json_each(?))", (json.dumps(group_ids),)
    )
    _move(writing.people, writing.index, group_ids)


def name_group(vault_root: Path, group_id: int, name: str) -> int:
    """Give a group a person's name, creating the person or joining an existing one. Returns the person id."""
    display, folded = _folded(name)
    with Writing(vault_root) as writing:
        people = writing.people
        previous = _group(writing, group_id)
        row = people.execute("SELECT id FROM people WHERE folded_name = ?", (folded,)).fetchone()
        person_id = int(row[0]) if row is not None else people.execute(
            "INSERT INTO people (display_name, folded_name) VALUES (?, ?)", (display, folded)
        ).lastrowid
        if previous is None:
            # A named group belongs to the owner's records, which backups keep.
            _move(writing.index, people, [group_id])
        people.execute(
            "INSERT INTO group_people (group_id, person_id) VALUES (?, ?) "
            "ON CONFLICT (group_id) DO UPDATE SET person_id = excluded.person_id",
            (group_id, person_id),
        )
        if previous != person_id:
            # The group's earlier person goes when it named no other group; its name must leave the log too.
            writing.scrub = previous is not None
            _drop_if_unused(people, previous)
        face_store.advance_generation(writing.index)
    return person_id


def rename_person(vault_root: Path, name: str, new_name: str) -> int:
    """Rename a person. A new name that another person has joins both into that person. Returns its id."""
    display, folded = _folded(new_name)
    with Writing(vault_root, scrub=True) as writing:
        people = writing.people
        person_id = _person(people, name)
        row = people.execute("SELECT id FROM people WHERE folded_name = ?", (folded,)).fetchone()
        if row is None or int(row[0]) == person_id:
            people.execute(
                "UPDATE people SET display_name = ?, folded_name = ? WHERE id = ?", (display, folded, person_id)
            )
            target = person_id
        else:
            target = int(row[0])
            people.execute("UPDATE group_people SET person_id = ? WHERE person_id = ?", (target, person_id))
            people.execute("DELETE FROM people WHERE id = ?", (person_id,))
        face_store.advance_generation(writing.index)
    return target


def clear_group(vault_root: Path, group_id: int) -> None:
    """Take the name off one group; its person goes when it names no other group."""
    with Writing(vault_root, scrub=True) as writing:
        previous = _group(writing, group_id)
        if previous is not None:
            _unname(writing, [group_id])
            _drop_if_unused(writing.people, previous)
        face_store.advance_generation(writing.index)


def clear_person(vault_root: Path, name: str) -> None:
    """Remove a person's name from all of its groups."""
    with Writing(vault_root, scrub=True) as writing:
        people = writing.people
        person_id = _person(people, name)
        groups = [row[0] for row in people.execute("SELECT group_id FROM group_people WHERE person_id = ?", (person_id,))]
        _unname(writing, groups)
        people.execute("DELETE FROM people WHERE id = ?", (person_id,))
        face_store.advance_generation(writing.index)


def move_face_out(vault_root: Path, face_id: int) -> int:
    """Move one face out of its group into a new unnamed group of its own. Returns the new group's id.

    The old group's centroid loses the face, and an exclusion, kept in the store that holds that group,
    keeps the face out of it for good.
    """
    with Writing(vault_root) as writing:
        index = writing.index
        row = index.execute(
            "SELECT page, frame_ts, x, y, width, height, model_id, vector, group_id FROM faces WHERE id = ?",
            (face_id,),
        ).fetchone()
        if row is None or row[8] is None:
            raise NameRefused("UNKNOWN_FACE", f"No grouped face has id {face_id}.")
        page, frame_ts, x, y, width, height, model_id, blob, group_id = row
        named = writing.people.execute("SELECT 1 FROM group_people WHERE group_id = ?", (group_id,)).fetchone()
        holder = writing.people if named is not None else index
        held = holder.execute("SELECT centroid_sum, face_count FROM groups WHERE id = ?", (group_id,)).fetchone()
        if held is not None:
            holder.execute(
                f"INSERT INTO exclusions ({_EXCLUSION_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (group_id, page, frame_ts, x, y, width, height),
            )
            vector = face_store.unpack_vector(blob)
            count = max(0, int(held[1]) - 1)
            # The owner said the face does not belong: a group it leaves empty has no direction, so no face joins it.
            remaining = face_store.unpack_vector(held[0]) - vector if count else np.zeros_like(vector)
            holder.execute(
                "UPDATE groups SET centroid_sum = ?, face_count = ? WHERE id = ?",
                (face_store.pack_vector(remaining), count, group_id),
            )
        new_group = new_group_id(writing.people, index)
        index.execute(
            "INSERT INTO groups (id, model_id, centroid_sum, face_count) VALUES (?, ?, ?, 1)",
            (new_group, model_id, blob),
        )
        index.execute("UPDATE faces SET group_id = ? WHERE id = ?", (new_group, face_id))
        face_store.advance_generation(index)
    return int(new_group)
