"""The face store's backup boundary: names stay in backups, face vectors never do."""

import fnmatch
import shlex
import sqlite3
from contextlib import closing
from pathlib import Path

import numpy as np
import pytest

from exomem import face_store, reserved_paths, vault_backup


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
