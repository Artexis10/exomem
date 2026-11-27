"""The face store's backup boundary: names stay in backups, face vectors never do."""

import fnmatch
import shlex
import sqlite3
from contextlib import closing
from pathlib import Path

import numpy as np
import pytest

from exomem import (
    face_groups,
    face_models,
    face_naming,
    face_store,
    media_models,
    reserved_paths,
    vault_backup,
)


def test_a_personal_backup_stages_the_people_store_and_no_face_index_file(vault: Path, tmp_path: Path) -> None:
    """Catches face vectors in a restic snapshot, or names lost on restore."""
    index = face_store.connect_index(vault)
    people = face_store.connect_people(vault)
    with index:
        index.execute(
            "INSERT INTO faces (page, x, y, width, height, score, model_id, vector) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("Photos/picnic.jpg", 0.1, 0.2, 0.3, 0.4, 0.95, "embedder@digest", face_store.pack_vector(np.ones(128))),
        )
    with people:
        people.execute(
            "INSERT INTO people (display_name, folded_name) VALUES (?, ?)",
            ("Rowan Example", face_store.fold_name("Rowan Example")),
        )
    # Both connections stay open, as the service holds them while a backup runs.
    stage = tmp_path / "stage"

    staged, findings = vault_backup.stage_state(vault, stage)

    index.close()
    people.close()
    assert findings == []
    staged_names = {path.relative_to(stage).as_posix() for path in stage.rglob("*") if path.is_file()}
    assert reserved_paths.FACE_PEOPLE_PATH in staged_names
    assert not [name for name in staged_names if reserved_paths.classify_logical(name).descriptor_id == "face-index"]
    with closing(sqlite3.connect(stage / reserved_paths.FACE_PEOPLE_PATH)) as copy:
        assert copy.execute("SELECT display_name FROM people").fetchall() == [("Rowan Example",)]
    assert staged >= 1


def test_a_personal_backup_stages_only_the_named_groups_in_the_people_store(vault: Path, tmp_path: Path) -> None:
    """Catches a stranger's one-face centroid in a restic snapshot."""
    model = face_models.embedder_model_id()
    width = int(media_models.model_spec("face-embed")["dimensions"])

    def face(axis: int) -> face_groups.PageFace:
        vector = np.zeros(width, dtype=np.float32)
        vector[axis] = 1.0
        return face_groups.PageFace(None, (0.1, 0.2, 0.3, 0.3), 96.0, 0.99, vector)

    with closing(face_store.connect_index(vault)) as index:
        face_store.record_opt_in(index, at="2026-01-05T10:00:00+00:00", surface="test")
    groups = [face_groups.record_page(vault, f"Photos/guest-{axis}.jpg", [face(axis)], model_id=model)[0]
              for axis in range(5)]
    [named, renamed_then_cleared, *strangers] = groups
    face_naming.name_group(vault, named, "Rowan Example")
    face_naming.name_group(vault, renamed_then_cleared, "Sky Example")
    face_naming.clear_group(vault, renamed_then_cleared)
    stage = tmp_path / "stage"

    vault_backup.stage_state(vault, stage)

    with closing(sqlite3.connect(stage / reserved_paths.FACE_PEOPLE_PATH)) as copy:
        assert copy.execute("SELECT id FROM groups").fetchall() == [(named,)]
        assert copy.execute("SELECT display_name FROM people").fetchall() == [("Rowan Example",)]
    with closing(sqlite3.connect(face_store.index_path(vault))) as index:
        unnamed = {row[0] for row in index.execute("SELECT id FROM groups")}
    assert unnamed == {renamed_then_cleared, *strangers}


def test_the_cell_backup_excludes_every_face_index_file_and_keeps_the_people_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a Cloud cell backup that keeps face vectors."""
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "infra" / "cellctl" / "src"))
    from cellctl.manifests import FACE_INDEX_BACKUP_EXCLUDES, CellManifestSpec, render_backup_job

    registry = {descriptor.id: descriptor for descriptor in reserved_paths.internal_state_registry()}
    assert registry["face-index"].rebuildable is True
    cell_state = "/data/host/.local/state/exomem/state/*/"
    assert FACE_INDEX_BACKUP_EXCLUDES == tuple(cell_state + name for name in registry["face-index"].exact)

    spec = CellManifestSpec(
        "aaaaaaaaaaaaaaaa", "registry.example/cell@sha256:" + "a" * 64, 1, False,
        hold_started_at="2026-01-01T16:05:00+00:00",
    )
    job = render_backup_job(spec, bucket_name="backups", endpoint="https://storage.example")
    args = shlex.split(job["spec"]["template"]["spec"]["containers"][0]["command"][2])
    excludes = [args[index + 1] for index, arg in enumerate(args) if arg == "--exclude"]

    for name in registry["face-index"].exact:
        state_file = "/data/host/.local/state/exomem/state/vault-key/" + name
        assert any(fnmatch.fnmatchcase(state_file, pattern) for pattern in excludes)
    for name in registry["face-people"].exact:
        state_file = "/data/host/.local/state/exomem/state/vault-key/" + name
        assert not any(fnmatch.fnmatchcase(state_file, pattern) for pattern in excludes)
