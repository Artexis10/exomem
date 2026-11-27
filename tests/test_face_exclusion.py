"""Face data stays in the face store: one test per path it must never reach (design D10).

Faces are seeded through the grouping and naming code the face step uses. A name no picture, page or file
name holds marks the person, so any file or answer that holds it got it from the face store.
"""
# Imported fixtures are requested by the tests.
# ruff: noqa: F811

from __future__ import annotations

import hashlib
import json
import logging
import zipfile
from pathlib import Path

import numpy as np
import pytest
from test_face_scan import _SquareFaces, clip_job, red_square_photo, turn_faces_on
from test_face_search import album, refresh, wire  # noqa: F401
from test_vault_portability import _context

from exomem import (
    cli_ops,
    commands,
    embeddings,
    face_groups,
    face_models,
    face_naming,
    face_scan,
    face_store,
    index_sync,
    media_models,
    media_worker,
    preserve,
    state_paths,
)
from exomem import find as find_module
from exomem import vault_portability as portability
from exomem.governance import projection_store, projections
from exomem.governance.principal import library_scope

#: The marked person's name, which no picture, page or file name in these vaults holds.
MARKED = "Quillon Vexmoor"


def mark_ada(vault: Path) -> None:
    """Rename the album's Ada to the marked name, then refresh the indexes as a service would."""
    face_naming.rename_person(vault, "Ada Fixture", MARKED)
    refresh(vault)


def files_holding(root: Path, needle: str, *, skip: Path | None = None) -> list[str]:
    """Every file under `root`, outside `skip`, whose bytes hold `needle`."""
    encoded = needle.encode("utf-8")
    return sorted(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() and (skip is None or not path.is_relative_to(skip)) and encoded in path.read_bytes()
    )


def test_detection_and_naming_leave_every_vault_files_bytes_unchanged(vault: Path, monkeypatch) -> None:
    """Catches a name or box written into a page, sidecar or extracted text."""
    pytest.importorskip("PIL")  # The core tier has no image decoder; the face step decodes the picture.
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    monkeypatch.setattr(embeddings, "embed_image", lambda path: np.ones(embeddings.CLIP_DIM, dtype=np.float32))
    worker = media_worker.MediaWorker(vault)
    for name in ("porch", "garden"):
        stored = preserve.preserve_bytes(
            vault, scope="Rental", category="photos", filename=f"{name}.jpg", data=red_square_photo(name), text=name
        )
        worker._process(clip_job(vault, stored, "image"))  # Faces are off: the pictures are indexed first.
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in vault.rglob("*") if path.is_file()}

    monkeypatch.setattr(face_models, "load", _SquareFaces)
    turn_faces_on(vault)
    stored_faces, _ = face_scan.backfill_batch(vault)
    [group] = {group.group for group in face_groups.groups_by_size(vault, limit=5, after=None, per_group=1)}
    face_naming.name_group(vault, group, MARKED)
    face_naming.rename_person(vault, MARKED, "Quillon Vexmoor-Bright")

    assert stored_faces == 2
    after = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in vault.rglob("*") if path.is_file()}
    assert after == before


def test_a_search_for_a_persons_name_finds_no_page_because_of_the_name(wire, album, vault) -> None:
    """Catches names reaching the lexical, embedding or graph projections.

    The suite runs without the embedding model, so the semantic lane is proven by the vault test above:
    it embeds vault text, which faces never change.
    """
    mark_ada(vault)

    reply = wire({"query": MARKED})

    assert reply.get("hits") == [], reply


def test_an_export_of_a_vault_with_faces_on_holds_no_face_store_file_or_name(album, vault, tmp_path) -> None:
    """Catches face data in an export that leaves the account."""
    mark_ada(vault)
    exports = [
        portability.export_quiesced_vault(vault, tmp_path / "exports", context=_context()),
        portability.export_compatible_vault(
            vault, tmp_path / "compatible", context=_context(operation_id="operation-export-002")
        ),
    ]

    for exported in exports:
        with zipfile.ZipFile(exported.archive_path) as archive:
            members = archive.namelist()
            assert not [name for name in members if "faces/" in name or name.endswith(".sqlite")], members
            assert not [name for name in members if MARKED.encode() in archive.read(name)]


def test_a_restricted_search_returns_no_box_embedding_group_or_name(wire, album) -> None:
    """Catches face data echoed to the model provider or into `normalized_filters`."""
    for arguments in (
        {"query": "", "people": ["Ada Fixture"], "detail": "full", "options": {"explain": True}},
        {"query": "lake", "people": ["Ada Fixture"], "deep": True},
    ):
        reply = wire(arguments)
        text = json.dumps(reply)

        assert reply.get("hits") or reply.get("context"), reply
        assert "Ada Fixture" not in text and "ada fixture" not in text
        # The face store's field names; "vector" is left out because the trace names a retrieval lane so.
        for face_field in ("region", "box", "embedding", "group_id", "face_count", "samples", "people"):
            assert f'"{face_field}"' not in text, (face_field, arguments)


def test_the_query_log_of_a_restricted_search_holds_no_person(album, vault, monkeypatch, tmp_path, caplog) -> None:
    """Catches names in local or hosted logs."""
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)  # It also turns the query log off.
    monkeypatch.setenv("EXOMEM_LOG_DIR", str(tmp_path / "logs"))
    caplog.set_level(logging.DEBUG)

    with library_scope():
        commands.op_find(vault, query="", people=["Ada Fixture"], mode="keyword")

    [line] = (tmp_path / "logs" / "queries.jsonl").read_text(encoding="utf-8").splitlines()
    assert "Ada Fixture" not in line and "ada fixture" not in line
    assert "people" not in json.loads(line)["filters"]
    assert "Ada Fixture" not in caplog.text


def test_the_call_ledger_keeps_no_hash_of_the_people_a_search_named(wire, album, tmp_path) -> None:
    """Catches a hash of a guessable name in the call ledger, which confirms a guess and outlives a delete."""
    from exomem.call_ledger import canonical_json

    wire({"query": "", "people": ["Ada Fixture"]})

    ledger = "".join(path.read_text(encoding="utf-8") for path in (tmp_path / "ledger").rglob("*.jsonl"))
    assert '"people"' in ledger  # The call was recorded, so the check below is not vacuous.
    assert hashlib.sha256(canonical_json({"v": ["Ada Fixture"]})).hexdigest() not in ledger


def test_projections_have_no_face_lane_and_a_projected_search_by_person_is_refused(album, vault, monkeypatch) -> None:
    """Catches face data in governed or projected retrieval for another principal."""
    from exomem.governance import projection_runtime

    key = projections.ProjectionNamespaceKey(policy_fingerprint="a" * 64, projector_schema_version=1,
                                             catalog_generation=1)
    with pytest.raises(projections.ProjectionCanonicalizationError):
        projection_store.projection_measurement_family_id(key, lane="face", extractor_version="e", model_version="m")

    monkeypatch.setattr(projection_runtime, "load_active_projection_runtime", lambda root: object())
    with library_scope(), pytest.raises(cli_ops.OpError) as refusal:
        commands.op_find(vault, query="", people=["Ada Fixture"])
    assert refusal.value.code == "FACES_OWNER_ONLY"


def test_published_catalogue_and_graph_hold_no_name_after_naming(album, vault) -> None:
    """Catches names published to a namespace another principal reads."""
    mark_ada(vault)
    pages = sorted(vault.rglob("*.md"))
    index_sync.upsert_after_write(vault, pages)  # The catalogue and the graph publish every page.
    state = state_paths.vault_state_dir(vault)

    assert (state / ".graph.sqlite").is_file()
    assert files_holding(state, MARKED, skip=face_store.store_dir(vault)) == []
    assert files_holding(vault, MARKED) == []


def test_two_accounts_keep_separate_face_stores_and_names(album, vault, tmp_path, monkeypatch) -> None:
    """Catches face data shared across accounts or cells."""
    first_store = face_store.store_dir(vault)
    other = tmp_path / "other-account" / "vault"
    other.mkdir(parents=True)
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "other-account" / "state"))
    turn_faces_on(other)
    width = int(media_models.model_spec("face-embed")["dimensions"])
    stranger = face_groups.PageFace(None, (0.1, 0.1, 0.2, 0.2), 64.0, 0.99, np.eye(width, dtype=np.float32)[0])
    face_groups.record_page(other, "Photos/porch.jpg", [stranger], model_id=face_models.embedder_model_id())

    with library_scope(), pytest.raises(cli_ops.OpError) as refusal:
        find_module.find(other, query="", people=["Ada Fixture"], mode="keyword")

    assert refusal.value.code == "UNKNOWN_PERSON"
    assert not face_store.store_dir(other).is_relative_to(first_store.parent)
    assert (first_store / "people.sqlite").is_file()
