"""Face grouping and naming on real face stores: what the owner's names and corrections depend on."""

import sqlite3
from contextlib import closing
from pathlib import Path

import numpy as np
import pytest

from exomem import face_groups, face_models, face_naming, face_scan, face_store, media_models

# The id the face step records, so faces stored by `face_scan.commit` meet the same groups.
MODEL = face_models.embedder_model_id()
WIDTH = int(media_models.model_spec("face-embed")["dimensions"])


def unit(*weights: float) -> np.ndarray:
    """A unit face vector whose first coordinates are `weights`."""
    vector = np.zeros(WIDTH, dtype=np.float32)
    vector[: len(weights)] = weights
    return vector / np.linalg.norm(vector)


def face(vector: np.ndarray, region=(0.1, 0.1, 0.2, 0.2), frame_ts=None) -> face_groups.PageFace:
    return face_groups.PageFace(frame_ts, region, 64.0, 0.99, vector)


def turn_on(vault: Path) -> None:
    with closing(face_store.connect_index(vault)) as index:
        face_store.record_opt_in(index, at="2026-01-05T10:00:00+00:00", surface="test")


def faces_of(vault: Path, page: str) -> list[tuple[tuple[float, ...], int]]:
    with closing(sqlite3.connect(face_store.index_path(vault))) as index:
        return [
            ((x, y, width, height), group_id)
            for x, y, width, height, group_id in index.execute(
                "SELECT x, y, width, height, group_id FROM faces WHERE page = ? ORDER BY id", (page,)
            )
        ]


def test_a_face_within_the_margin_of_two_groups_starts_a_new_group(vault: Path) -> None:
    """Catches a doubtful face merged into the wrong person."""
    turn_on(vault)
    [first] = face_groups.record_page(vault, "Photos/first.jpg", [face(unit(1, 0))], model_id=MODEL)
    [second] = face_groups.record_page(vault, "Photos/second.jpg", [face(unit(0, 1))], model_id=MODEL)

    # Equally similar to both groups, and well above join_similarity for each.
    [doubtful] = face_groups.record_page(vault, "Photos/both.jpg", [face(unit(1, 1))], model_id=MODEL)
    [clear] = face_groups.record_page(vault, "Photos/clear.jpg", [face(unit(1, 0.1))], model_id=MODEL)

    assert doubtful not in (first, second)
    assert clear == first


def test_the_same_faces_in_the_same_order_give_identical_groups(tmp_path: Path) -> None:
    """Catches non-deterministic grouping that moves names between runs."""
    rng = np.random.default_rng(7)
    people = [unit(*rng.normal(size=WIDTH)) for _ in range(3)]
    pages = []
    for number in range(12):
        faces = []
        for slot, person in enumerate(rng.choice(3, size=2, replace=False)):
            vector = people[person] + rng.normal(scale=0.05, size=WIDTH)
            faces.append(face(vector / np.linalg.norm(vector), region=(0.1 + 0.4 * slot, 0.2, 0.3, 0.3)))
        pages.append((f"Photos/picnic-{number:02d}.jpg", faces))

    def group_everything(vault: Path) -> list[tuple[str, list[int]]]:
        vault.mkdir()
        turn_on(vault)
        return [(page, face_groups.record_page(vault, page, faces, model_id=MODEL)) for page, faces in pages]

    def partition(run: list[tuple[str, list[int]]]) -> list[list[tuple[str, int]]]:
        """Which faces share a group; ids are random, so two runs agree on the partition, not the ids."""
        members: dict[int, list[tuple[str, int]]] = {}
        for page, groups in run:
            for slot, group in enumerate(groups):
                members.setdefault(group, []).append((page, slot))
        return sorted(members.values())

    def centroids(vault: Path) -> list[tuple[bytes, int]]:
        with closing(sqlite3.connect(face_store.index_path(vault))) as index:
            return sorted(index.execute("SELECT centroid_sum, face_count FROM groups").fetchall())

    first_run = group_everything(tmp_path / "first")
    second_run = group_everything(tmp_path / "second")

    assert partition(first_run) == partition(second_run)
    assert len(partition(first_run)) >= 3
    # Nothing is named, so every group is in the face index.
    assert centroids(tmp_path / "first") == centroids(tmp_path / "second")


def test_a_face_moved_out_of_a_group_does_not_rejoin_it_when_more_faces_arrive(vault: Path) -> None:
    """Catches owner corrections undone by the next backfill."""
    turn_on(vault)
    [group] = face_groups.record_page(vault, "Photos/garden.jpg", [face(unit(1, 0))], model_id=MODEL)
    face_groups.record_page(vault, "Photos/porch.jpg", [face(unit(1, 1), region=(0.5, 0.5, 0.2, 0.2))], model_id=MODEL)
    face_naming.name_group(vault, group, "Rowan Example")
    with closing(sqlite3.connect(face_store.index_path(vault))) as index:
        [(porch_face,)] = index.execute("SELECT id FROM faces WHERE page = 'Photos/porch.jpg'").fetchall()

    own_group = face_naming.move_face_out(vault, porch_face)
    # The porch picture is scanned again, its face found with a slightly different box, and more faces arrive.
    face_groups.record_page(vault, "Photos/porch.jpg", [face(unit(1, 1), region=(0.51, 0.5, 0.2, 0.2))], model_id=MODEL)
    [later] = face_groups.record_page(vault, "Photos/lawn.jpg", [face(unit(1, 0.05))], model_id=MODEL)

    [(_, porch_group)] = faces_of(vault, "Photos/porch.jpg")
    assert porch_group == own_group != group
    assert later == group


def test_a_named_persons_next_picture_keeps_the_name_after_their_only_picture_is_removed(vault: Path) -> None:
    """Catches a name lost, or a stranger named, when a named group's last face goes and a new one arrives."""
    turn_on(vault)
    [group] = face_groups.record_page(vault, "Photos/garden.jpg", [face(unit(1, 0))], model_id=MODEL)
    face_naming.name_group(vault, group, "Rowan Example")

    face_groups.forget_pages(vault, ["Photos/garden.jpg"])
    [later] = face_groups.record_page(vault, "Photos/lawn.jpg", [face(unit(1, 0.05))], model_id=MODEL)
    [stranger] = face_groups.record_page(vault, "Photos/street.jpg", [face(unit(0, 1))], model_id=MODEL)

    assert later == group
    assert stranger != group
    with closing(sqlite3.connect(face_store.people_path(vault))) as people:
        assert people.execute("SELECT id, face_count FROM groups").fetchall() == [(group, 1)]


def test_a_restored_people_store_names_rescanned_faces_once_faces_are_turned_on_again(vault: Path) -> None:
    """Catches a restore that turns faces on, or loses names."""
    with closing(face_store.connect_index(vault)) as index:
        face_store.record_opt_in(index, at="2026-01-05T10:00:00+00:00", surface="test")
    [group] = face_groups.record_page(vault, "Photos/garden.jpg", [face(unit(1, 0))], model_id=MODEL)
    face_naming.name_group(vault, group, "Rowan Example")
    # The restore brings the people store back; the face index is rebuildable and never backed up.
    for suffix in ("", "-wal", "-shm"):
        Path(str(face_store.index_path(vault)) + suffix).unlink(missing_ok=True)
    garden = [face(unit(1, 0.02))]

    stored_while_off = face_scan.commit(vault, "Photos/garden.jpg", garden)
    assert stored_while_off is False
    assert face_store.enabled(vault) is False
    with closing(face_store.connect_index(vault)) as index:
        face_store.record_opt_in(index, at="2026-02-01T09:00:00+00:00", surface="test")
    stored_after_opt_in = face_scan.commit(vault, "Photos/garden.jpg", garden)

    assert stored_after_opt_in is True
    [(_, regrouped)] = faces_of(vault, "Photos/garden.jpg")
    with closing(sqlite3.connect(face_store.people_path(vault))) as people:
        [(name,)] = people.execute(
            "SELECT p.display_name FROM group_people g JOIN people p ON p.id = g.person_id WHERE g.group_id = ?",
            (regrouped,),
        ).fetchall()
    assert name == "Rowan Example"


def back_up_people_store(vault: Path, copy: Path) -> None:
    with closing(sqlite3.connect(face_store.people_path(vault))) as people, closing(sqlite3.connect(copy)) as saved:
        people.backup(saved)


def restore_people_store(vault: Path, copy: Path) -> None:
    with closing(sqlite3.connect(copy)) as saved, closing(sqlite3.connect(face_store.people_path(vault))) as people:
        saved.backup(people)


def remove_face_store(vault: Path) -> None:
    for path in (face_store.index_path(vault), face_store.people_path(vault)):
        for suffix in ("", "-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)


def test_a_people_store_restored_after_a_delete_does_not_name_a_stranger(vault: Path, tmp_path: Path) -> None:
    """Catches a restored name landing on another person's group that reused the named group's id."""
    turn_on(vault)
    [named] = face_groups.record_page(vault, "Photos/garden.jpg", [face(unit(1, 0))], model_id=MODEL)
    face_naming.name_group(vault, named, "Rowan Example")
    back_up_people_store(vault, tmp_path / "people-backup.sqlite")
    remove_face_store(vault)  # The owner deletes faces, then turns them on again.
    turn_on(vault)
    [stranger] = face_groups.record_page(vault, "Photos/street.jpg", [face(unit(0, 1))], model_id=MODEL)

    restore_people_store(vault, tmp_path / "people-backup.sqlite")

    with closing(sqlite3.connect(face_store.people_path(vault))) as people:
        names_of_stranger = people.execute(
            "SELECT p.display_name FROM group_people g JOIN people p ON p.id = g.person_id WHERE g.group_id = ?",
            (stranger,),
        ).fetchall()
    assert names_of_stranger == []


def test_a_restored_people_store_counts_each_rescanned_face_once(vault: Path, tmp_path: Path) -> None:
    """Catches a named group counting a re-scanned picture twice, which drags its centroid off the person."""
    turn_on(vault)
    [group] = face_groups.record_page(vault, "Photos/garden.jpg", [face(unit(1, 0))], model_id=MODEL)
    face_naming.name_group(vault, group, "Rowan Example")
    back_up_people_store(vault, tmp_path / "people-backup.sqlite")
    remove_face_store(vault)
    restore_people_store(vault, tmp_path / "people-backup.sqlite")
    turn_on(vault)

    [again] = face_groups.record_page(vault, "Photos/garden.jpg", [face(unit(1, 0.02))], model_id=MODEL)

    assert again == group
    with closing(sqlite3.connect(face_store.people_path(vault))) as people:
        [(blob, count)] = people.execute("SELECT centroid_sum, face_count FROM groups").fetchall()
    assert count == 1
    np.testing.assert_allclose(face_store.unpack_vector(blob), unit(1, 0.02), atol=1e-6)


@pytest.fixture(autouse=True)
def _no_loaded_face_models():
    face_scan.unload()
    yield
    face_scan.unload()
