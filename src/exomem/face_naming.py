"""The owner's names and corrections on face groups, in the people store (design D6 and D7).

A person owns one or more groups. Naming a group with a name that a person already has, compared by
Unicode NFKC case folding, adds the group to that person; the groups keep their own centroids. Renaming a
person to another person's name joins the two. Clearing a name leaves the groups unnamed. Moving a face
out of its group records an exclusion keyed by page, frame and box, so the face never joins that group
again, even after the face index is rebuilt.

Every change advances the face index generation, so a cached search never outlives it. A person left
with no group is removed: a name exists only while it names a group.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np

from . import face_store
from .face_groups import Writing


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


def _group(people: sqlite3.Connection, group_id: int) -> int | None:
    row = people.execute("SELECT person_id FROM groups WHERE id = ?", (group_id,)).fetchone()
    if row is None:
        raise NameRefused("UNKNOWN_GROUP", f"No face group has id {group_id}.")
    return row[0]


def _drop_if_unused(people: sqlite3.Connection, person_id: int | None) -> None:
    if person_id is not None:
        people.execute(
            "DELETE FROM people WHERE id = ? AND NOT EXISTS (SELECT 1 FROM groups WHERE person_id = ?)",
            (person_id, person_id),
        )


def name_group(vault_root: Path, group_id: int, name: str) -> int:
    """Give a group a person's name, creating the person or joining an existing one. Returns the person id."""
    display, folded = _folded(name)
    with Writing(vault_root) as writing:
        people = writing.people
        previous = _group(people, group_id)
        row = people.execute("SELECT id FROM people WHERE folded_name = ?", (folded,)).fetchone()
        person_id = int(row[0]) if row is not None else people.execute(
            "INSERT INTO people (display_name, folded_name) VALUES (?, ?)", (display, folded)
        ).lastrowid
        people.execute("UPDATE groups SET person_id = ? WHERE id = ?", (person_id, group_id))
        if previous != person_id:
            _drop_if_unused(people, previous)
        face_store.advance_generation(writing.index)
    return person_id


def rename_person(vault_root: Path, name: str, new_name: str) -> int:
    """Rename a person. A new name that another person has joins both into that person. Returns its id."""
    display, folded = _folded(new_name)
    with Writing(vault_root) as writing:
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
            people.execute("UPDATE groups SET person_id = ? WHERE person_id = ?", (target, person_id))
            people.execute("DELETE FROM people WHERE id = ?", (person_id,))
        face_store.advance_generation(writing.index)
    return target


def clear_group(vault_root: Path, group_id: int) -> None:
    """Take the name off one group; its person goes when it names no other group."""
    with Writing(vault_root) as writing:
        people = writing.people
        previous = _group(people, group_id)
        people.execute("UPDATE groups SET person_id = NULL WHERE id = ?", (group_id,))
        _drop_if_unused(people, previous)
        face_store.advance_generation(writing.index)


def clear_person(vault_root: Path, name: str) -> None:
    """Remove a person's name from all of its groups."""
    with Writing(vault_root) as writing:
        people = writing.people
        person_id = _person(people, name)
        people.execute("UPDATE groups SET person_id = NULL WHERE person_id = ?", (person_id,))
        people.execute("DELETE FROM people WHERE id = ?", (person_id,))
        face_store.advance_generation(writing.index)


def move_face_out(vault_root: Path, face_id: int) -> int:
    """Move one face out of its group into a new group of its own. Returns the new group's id.

    The old group's centroid loses the face, and an exclusion keeps the face out of that group for good.
    """
    with Writing(vault_root) as writing:
        people, index = writing.people, writing.index
        row = index.execute(
            "SELECT page, frame_ts, x, y, width, height, model_id, vector, group_id FROM faces WHERE id = ?",
            (face_id,),
        ).fetchone()
        if row is None or row[8] is None:
            raise NameRefused("UNKNOWN_FACE", f"No grouped face has id {face_id}.")
        page, frame_ts, x, y, width, height, model_id, blob, group_id = row
        people.execute(
            "INSERT INTO exclusions (group_id, page, frame_ts, x, y, width, height) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (group_id, page, frame_ts, x, y, width, height),
        )
        held = people.execute("SELECT centroid_sum, face_count FROM groups WHERE id = ?", (group_id,)).fetchone()
        if held is not None:
            vector = face_store.unpack_vector(blob)
            count = max(0, int(held[1]) - 1)
            # The owner said the face does not belong: a group it leaves empty has no direction, so no face joins it.
            remaining = face_store.unpack_vector(held[0]) - vector if count else np.zeros_like(vector)
            people.execute(
                "UPDATE groups SET centroid_sum = ?, face_count = ? WHERE id = ?",
                (face_store.pack_vector(remaining), count, group_id),
            )
        new_group = people.execute(
            "INSERT INTO groups (model_id, centroid_sum, face_count) VALUES (?, ?, 1)", (model_id, blob)
        ).lastrowid
        index.execute("UPDATE faces SET group_id = ? WHERE id = ?", (new_group, face_id))
        face_store.advance_generation(index)
    return int(new_group)
