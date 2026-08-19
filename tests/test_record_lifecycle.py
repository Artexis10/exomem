from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from record_fixtures import copy_x3_fixture

from exomem import record_formats, records
from exomem import structured_collections as collections


def _activity_log(root: Path) -> None:
    path = root / "Knowledge Base/log.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Activity\n", encoding="utf-8")


def _item() -> dict[str, object]:
    return {
        "occurred_on": "2026-08-03",
        "title": "Pull",
        "status": "completed",
        "movements": [{"movement": "Deadlift", "band": "grey", "repetitions": "22"}],
    }


def test_lifecycle_digest_vectors_are_canonical() -> None:
    assert records.lifecycle_gap_fingerprint(
        prior_head="0123456789abcdef01234567",
        acknowledged_gap_codes=("current-container-mismatch", "current-manifest-mismatch"),
        before_manifest_hash="a" * 64,
        before_container_hash="b" * 64,
    ) == "e96fe3ac9d4704d04c6d583795c68e9ac544f3be4061641b3c6d61aeb81a3c2e"
    assert records.lifecycle_checkpoint_fingerprint(
        (
            ("Knowledge Base/Records/Test/Items/item.md", "c" * 64),
            ("Knowledge Base/Records/Test/_collection.md", "a" * 64),
        )
    ) == "de55aff9ce1c3a75dbd045461fd2b9a95a415cb509a0c67d671e6ad42b24e478"
    assert records.lifecycle_request_hash(
        action="rebaseline",
        collection_id="11111111-1111-4111-8111-111111111111",
        before_manifest_hash="a" * 64,
        before_container_hash="b" * 64,
        proposed_manifest_hash=None,
        acknowledged_gap_codes=("current-container-mismatch", "current-manifest-mismatch"),
        rationale="Acknowledge direct edit",
    ) == "349c5a30baf4922922c42512efbfee05607c18888e27ccd39a37deefd9358f01"


def test_revision_and_rebaseline_upgrade_audit_without_rewriting_items(tmp_path: Path) -> None:
    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    records.append_record(
        tmp_path,
        manifest.path,
        item=_item(),
        item_key="11111111-1111-4111-8111-111111111111",
        expected_container_hash=snapshot.source_versions[-1].hash,
        why="record a session",
    )
    current = collections.load_manifest(tmp_path, manifest.path)
    current_snapshot = record_formats.load_adapter(tmp_path, current).read()
    proposed = (tmp_path / current.path).read_text(encoding="utf-8").replace(
        "title:", "title: Revised", 1
    )

    revised = records.revise_collection(
        tmp_path,
        current.path,
        manifest_text=proposed,
        expected_manifest_hash=current.manifest_version.hash,
        expected_container_hash=records.lifecycle_guards(current, current_snapshot)["expected_container_hash"],
        why="clarify collection title",
    )

    assert revised["operation"] == "revise"
    assert revised["outcome"] == "committed"
    assert "version: 2" in (tmp_path / current.path).read_text(encoding="utf-8")
    assert records.inspect_audit_gap(tmp_path, current.path)["status"] == "ok"

    direct = tmp_path / current.path
    direct.write_text(direct.read_text(encoding="utf-8").replace("title:", "title: Direct", 1), encoding="utf-8")
    changed = collections.load_manifest(tmp_path, current.path)
    changed_snapshot = record_formats.load_adapter(tmp_path, changed).read()
    report = records.inspect_audit_gap(tmp_path, changed.path)

    rebaselined = records.rebaseline_collection(
        tmp_path,
        changed.path,
        expected_manifest_hash=changed.manifest_version.hash,
        expected_container_hash=records.lifecycle_guards(changed, changed_snapshot)["expected_container_hash"],
        acknowledged_gap_codes=tuple(report["gaps"]),
        why="acknowledge direct title correction",
    )

    assert rebaselined["operation"] == "rebaseline"
    assert records.inspect_audit_gap(tmp_path, changed.path)["status"] == "acknowledged_gap"


def test_lifecycle_rejects_topology_gaps_and_duplicate_items(tmp_path: Path) -> None:
    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    manifest_text = (tmp_path / manifest.path).read_text(encoding="utf-8")
    with __import__("pytest").raises(collections.CollectionError, match="RECORD_AUDIT_GAP"):
        records.rebaseline_collection(
            tmp_path,
            manifest.path,
            expected_manifest_hash=manifest.manifest_version.hash,
            expected_container_hash=records.lifecycle_guards(manifest, snapshot)["expected_container_hash"],
            acknowledged_gap_codes=("missing-head-event",),
            why="must not bless missing history",
        )
    item = fixture / "Training Log.md"
    item.write_text(item.read_text(encoding="utf-8") + "\nmanual edit\n", encoding="utf-8")
    changed = collections.load_manifest(tmp_path, manifest.path)
    changed_snapshot = record_formats.load_adapter(tmp_path, changed).read()
    with __import__("pytest").raises(collections.CollectionError, match="RECORD_AUDIT_GAP"):
        records.rebaseline_collection(
            tmp_path,
            changed.path,
            expected_manifest_hash=changed.manifest_version.hash,
            expected_container_hash=records.lifecycle_guards(changed, changed_snapshot)["expected_container_hash"],
            acknowledged_gap_codes=("missing-parent:bad",),
            why="must not bless topology",
        )
    assert manifest_text


def test_v2_history_and_targeted_inspection_keep_closed_lifecycle_facts(tmp_path: Path) -> None:
    from exomem import record_governance

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    records.append_record(
        tmp_path, manifest.path, item=_item(), item_key="22222222-2222-4222-8222-222222222222",
        expected_container_hash=snapshot.source_versions[-1].hash, why="record session",
    )
    current = collections.load_manifest(tmp_path, manifest.path)
    current_snapshot = record_formats.load_adapter(tmp_path, current).read()
    records.revise_collection(
        tmp_path, current.path,
        manifest_text=(tmp_path / current.path).read_text(encoding="utf-8").replace("title:", "title: Revised", 1),
        expected_manifest_hash=current.manifest_version.hash,
        expected_container_hash=records.lifecycle_guards(current, current_snapshot)["expected_container_hash"],
        why="clarify title",
    )
    history = records.agent_audit_history(tmp_path, current.path)
    assert history["events"][0]["operation"] == "revise"
    assert history["events"][0]["minimum_reader_version"] == 2
    inspection = record_governance.inspect_collection(tmp_path, current.path)
    assert set(inspection["lifecycle_guards"]) == {
        "expected_manifest_hash", "expected_container_hash"
    }


def test_lifecycle_refuses_archive_change_after_audit_recheck(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    records.append_record(
        tmp_path, manifest.path, item=_item(), item_key="33333333-3333-4333-8333-333333333333",
        expected_container_hash=snapshot.source_versions[-1].hash, why="record session",
    )
    current = collections.load_manifest(tmp_path, manifest.path)
    current_snapshot = record_formats.load_adapter(tmp_path, current).read()
    archive = tmp_path / "Knowledge Base/_archive/logs"
    archive.mkdir(parents=True)
    archived = archive / "log-00000000000000000000.md"
    archived.write_text("# archive\n", encoding="utf-8")
    before = (tmp_path / current.path).read_bytes()
    real_precommit = records.record_governance.precommit_authorize_mutation

    def mutate_archive(*args, **kwargs):  # noqa: ANN002,ANN003
        archived.write_text("# archive\nforeign audit change\n", encoding="utf-8")
        return real_precommit(*args, **kwargs)

    monkeypatch.setattr(records.record_governance, "precommit_authorize_mutation", mutate_archive)
    with __import__("pytest").raises(collections.CollectionError, match="STALE_RECORD"):
        records.revise_collection(
            tmp_path, current.path,
            manifest_text=before.decode("utf-8").replace("title:", "title: Revised", 1),
            expected_manifest_hash=current.manifest_version.hash,
            expected_container_hash=records.lifecycle_guards(current, current_snapshot)["expected_container_hash"],
            why="exercise archive cut",
        )
    assert (tmp_path / current.path).read_bytes() == before


def test_lifecycle_receipt_is_closed_and_projects_payload_hash(tmp_path: Path) -> None:
    from exomem import mutation_terminal, record_governance

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    records.append_record(
        tmp_path, manifest.path, item=_item(), item_key="44444444-4444-4444-8444-444444444444",
        expected_container_hash=snapshot.source_versions[-1].hash, why="record session",
    )
    current = collections.load_manifest(tmp_path, manifest.path)
    current_snapshot = record_formats.load_adapter(tmp_path, current).read()
    receipt = records.revise_collection(
        tmp_path, current.path,
        manifest_text=(tmp_path / current.path).read_text(encoding="utf-8").replace("title:", "title: Revised", 1),
        expected_manifest_hash=current.manifest_version.hash,
        expected_container_hash=records.lifecycle_guards(current, current_snapshot)["expected_container_hash"],
        why="clarify title",
    )
    assert mutation_terminal.valid_record_receipt(receipt)
    assert record_governance.project_mutation_receipt(receipt)["payload_hash"] == receipt["payload_hash"]
    tampered = {**receipt, "unexpected": True}
    assert not mutation_terminal.valid_record_receipt(tampered)


def test_v1_append_after_revision_keeps_v2_marker_and_v1_event_shape(tmp_path: Path) -> None:
    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    start = record_formats.load_adapter(tmp_path, manifest).read()
    records.append_record(
        tmp_path,
        manifest.path,
        item=_item(),
        item_key="66666666-6666-4666-8666-666666666666",
        expected_container_hash=start.source_versions[-1].hash,
        why="first",
    )
    current = collections.load_manifest(tmp_path, manifest.path)
    snapshot = record_formats.load_adapter(tmp_path, current).read()
    records.revise_collection(
        tmp_path,
        current.path,
        # Read as UTF-8 bytes: the manifest is re-encoded as UTF-8 downstream, and
        # a bare `read_text()` decodes with the locale encoding and normalizes
        # newlines, so the round trip proposed a manifest that differed from the
        # committed one and the immutability check refused it as a migration.
        manifest_text=(tmp_path / current.path)
        .read_bytes()
        .decode("utf-8")
        .replace("title:", "title: Revised", 1),
        expected_manifest_hash=current.manifest_version.hash,
        expected_container_hash=records.lifecycle_guards(current, snapshot)["expected_container_hash"],
        why="revise",
    )
    revised = collections.load_manifest(tmp_path, current.path)
    revised_snapshot = record_formats.load_adapter(tmp_path, revised).read()
    receipt = records.append_record(
        tmp_path,
        revised.path,
        item={**_item(), "occurred_on": "2026-08-04"},
        item_key="77777777-7777-4777-8777-777777777777",
        expected_container_hash=revised_snapshot.source_versions[-1].hash,
        why="later",
    )
    assert receipt["receipt_version"] == 1
    assert "version: 2" in (tmp_path / revised.path).read_text()
    assert records.inspect_audit_gap(tmp_path, revised.path)["status"] == "ok"


@pytest.mark.parametrize(
    ("before", "after", "code"),
    (
        ("exomem_id: 9ba8d1cf-d1e7-4309-95ae-cb28d7a6eea8", "exomem_id: 8ba8d1cf-d1e7-4309-95ae-cb28d7a6eea8", "IMMUTABLE_COLLECTION_REPRESENTATION"),
        ("semantic_profile: records", "semantic_profile: planning", "INVALID_COLLECTION_PATH"),
        ("source: Training Log.md", "source: Replacement.md", "IMMUTABLE_COLLECTION_REPRESENTATION"),
        ("strategy: markdown-log", "strategy: markdown-items", "IMMUTABLE_COLLECTION_REPRESENTATION"),
    ),
)
def test_revision_refuses_immutable_collection_representation(
    tmp_path: Path, before: str, after: str, code: str
) -> None:
    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    current = collections.load_manifest(tmp_path, fixture / "_collection.md")
    snapshot = record_formats.load_adapter(tmp_path, current).read()
    original = (tmp_path / current.path).read_bytes()

    with pytest.raises(collections.CollectionError, match=code):
        records.revise_collection(
            tmp_path,
            current.path,
            manifest_text=original.decode("utf-8").replace(before, after, 1),
            expected_manifest_hash=current.manifest_version.hash,
            expected_container_hash=records.lifecycle_guards(current, snapshot)["expected_container_hash"],
            why="must not migrate a collection",
        )

    assert (tmp_path / current.path).read_bytes() == original


def test_revision_refuses_schema_that_invalidates_a_current_item(tmp_path: Path) -> None:
    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    current = collections.load_manifest(tmp_path, fixture / "_collection.md")
    snapshot = record_formats.load_adapter(tmp_path, current).read()
    original = (tmp_path / current.path).read_bytes()
    incompatible = original.decode("utf-8").replace(
        "enum: [completed, partial, aborted]", "enum: [completed]", 1
    )

    with pytest.raises(collections.CollectionError, match="SCHEMA_ENUM"):
        records.revise_collection(
            tmp_path,
            current.path,
            manifest_text=incompatible,
            expected_manifest_hash=current.manifest_version.hash,
            expected_container_hash=records.lifecycle_guards(current, snapshot)["expected_container_hash"],
            why="schema must retain every current item",
        )

    assert (tmp_path / current.path).read_bytes() == original


def test_lifecycle_hidden_manifest_matches_missing_without_reading_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    denied = manifest.path
    original_read = records._read_record_bytes

    monkeypatch.setattr(
        records.record_governance,
        "full_release_filter",
        lambda _root: lambda path: path != denied,
    )

    def deny_hidden_read(root: Path, path: str):
        assert path != denied
        return original_read(root, path)

    monkeypatch.setattr(records, "_read_record_bytes", deny_hidden_read)
    errors: list[collections.CollectionError] = []
    for selector in (denied, "Knowledge Base/Records/missing/_collection.md"):
        with pytest.raises(collections.CollectionError) as raised:
            records.validate_collection_revision(tmp_path, selector, "---\n")
        errors.append(raised.value)

    assert [(error.code, error.reason) for error in errors] == [
        ("COLLECTION_NOT_FOUND", "collection was not found"),
        ("COLLECTION_NOT_FOUND", "collection was not found"),
    ]


def test_lifecycle_refuses_mixed_release_and_hidden_proposed_reference_content_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    current = collections.load_manifest(tmp_path, fixture / "_collection.md")
    snapshot = record_formats.load_adapter(tmp_path, current).read()
    original = (tmp_path / current.path).read_bytes()
    hidden_path = "Knowledge Base/Templates/Records/Health/X3/X3 Pull.md"
    original_filter = records.record_governance.full_release_filter
    monkeypatch.setattr(
        records.record_governance,
        "full_release_filter",
        lambda root: lambda path: original_filter(root)(path) and path != hidden_path,
    )

    with pytest.raises(collections.CollectionError) as mixed:
        records.revise_collection(
            tmp_path,
            current.path,
            manifest_text=original.decode("utf-8"),
            expected_manifest_hash=current.manifest_version.hash,
            expected_container_hash=records.lifecycle_guards(current, snapshot)["expected_container_hash"],
            why="mixed release must not disclose content",
        )
    assert (mixed.value.code, mixed.value.reason) == ("COLLECTION_NOT_FOUND", "collection was not found")
    assert hidden_path not in str(mixed.value)

    proposed = original.decode("utf-8").replace(
        "X3 Pull.md", "../../Evidence/secret.md", 1
    )
    with pytest.raises(collections.CollectionError) as hidden_proposed:
        records.revise_collection(
            tmp_path,
            current.path,
            manifest_text=proposed,
            expected_manifest_hash=current.manifest_version.hash,
            expected_container_hash=records.lifecycle_guards(current, snapshot)["expected_container_hash"],
            why="hidden proposal must not disclose content",
        )
    assert (hidden_proposed.value.code, hidden_proposed.value.reason) == (
        "COLLECTION_NOT_FOUND", "collection was not found"
    )
    assert "secret.md" not in str(hidden_proposed.value)
    assert (tmp_path / current.path).read_bytes() == original


@pytest.mark.parametrize("cut", ("manifest", "log"))
def test_caught_publication_replacement_failure_rolls_back_without_audit_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cut: str
) -> None:
    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    current = collections.load_manifest(tmp_path, fixture / "_collection.md")
    snapshot = record_formats.load_adapter(tmp_path, current).read()
    manifest_path = tmp_path / current.path
    log_path = tmp_path / "Knowledge Base/log.md"
    before = (manifest_path.read_bytes(), log_path.read_bytes())
    replace = records.vault._BatchWorkspace.replace_artifact
    failed = False
    cut_path = manifest_path if cut == "manifest" else log_path

    def fail_after_manifest_replacement(workspace, artifact, final):  # noqa: ANN001
        nonlocal failed
        installed = replace(workspace, artifact, final)
        if final == cut_path and not failed:
            failed = True
            raise OSError("manifest replacement cut")
        return installed

    monkeypatch.setattr(records.vault._BatchWorkspace, "replace_artifact", fail_after_manifest_replacement)
    with pytest.raises(collections.CollectionError, match="RECORD_PUBLICATION_FAILED"):
        records.revise_collection(
            tmp_path,
            current.path,
            manifest_text=manifest_path.read_text(encoding="utf-8").replace("title:", "title: Revised", 1),
            expected_manifest_hash=current.manifest_version.hash,
            expected_container_hash=records.lifecycle_guards(current, snapshot)["expected_container_hash"],
            why="test caught publication cut",
        )

    assert (manifest_path.read_bytes(), log_path.read_bytes()) == before
    assert records.inspect_audit_gap(tmp_path, current.path)["status"] == "baseline"


@pytest.mark.parametrize("cut", ("manifest", "log"))
def test_abrupt_publication_replacement_cut_never_fabricates_a_lifecycle_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cut: str
) -> None:
    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    current = collections.load_manifest(tmp_path, fixture / "_collection.md")
    snapshot = record_formats.load_adapter(tmp_path, current).read()
    manifest_path = tmp_path / current.path
    log_path = tmp_path / "Knowledge Base/log.md"
    before_log = log_path.read_bytes()
    replace = records.vault._BatchWorkspace.replace_artifact
    cut_path = manifest_path if cut == "manifest" else log_path

    class AbruptCut(BaseException):
        pass

    def interrupt_after_manifest_replacement(workspace, artifact, final):  # noqa: ANN001
        installed = replace(workspace, artifact, final)
        if final == cut_path:
            raise AbruptCut()
        return installed

    monkeypatch.setattr(
        records.vault._BatchWorkspace, "replace_artifact", interrupt_after_manifest_replacement
    )
    with pytest.raises(AbruptCut):
        records.revise_collection(
            tmp_path,
            current.path,
            manifest_text=manifest_path.read_text(encoding="utf-8").replace("title:", "title: Revised", 1),
            expected_manifest_hash=current.manifest_version.hash,
            expected_container_hash=records.lifecycle_guards(current, snapshot)["expected_container_hash"],
            why="test abrupt publication cut",
        )

    report = records.inspect_audit_gap(tmp_path, current.path)
    if cut == "manifest":
        assert log_path.read_bytes() == before_log
        assert report["status"] == "gap"
    else:
        assert log_path.read_bytes() != before_log
        assert report["status"] == "ok"
        assert records.agent_audit_history(tmp_path, current.path)["events"][0]["operation"] == "revise"


@pytest.mark.parametrize("field", ("receipt_version", "minimum_reader_version"))
@pytest.mark.parametrize("invalid", (2.0, True))
def test_lifecycle_receipt_rejects_non_integer_version_fields(field: str, invalid: object) -> None:
    from exomem import mutation_terminal

    receipt = {
        "_record_receipt": "exomem.records-mutation", "receipt_version": 2,
        "operation": "revise", "collection_id": "11111111-1111-4111-8111-111111111111",
        "item_key": None, "before_item_hash": None, "after_item_hash": None,
        "before_manifest_hash": "a" * 64, "after_manifest_hash": "b" * 64,
        "before_container_hash": "c" * 64, "after_container_hash": "d" * 64,
        "affected_paths": ["Knowledge Base/Records/Test/_collection.md"], "payload_hash": "e" * 64,
        "outcome": "committed", "audit_correlation": "f" * 24, "continuity": True,
        "acknowledged_gap_codes": [], "gap_fingerprint": None, "checkpoint_snapshot_hash": None,
        "minimum_reader_version": 2,
    }
    receipt[field] = invalid

    assert not mutation_terminal.valid_record_receipt(receipt)


@pytest.mark.parametrize("field", ("version", "minimum_reader_version"))
@pytest.mark.parametrize("invalid", (2.0, True))
def test_lifecycle_log_event_rejects_non_integer_version_fields(field: str, invalid: object) -> None:
    import json

    event = json.loads(
        records._lifecycle_audit_body(
            transition_id="1" * 24, parent_id="baseline", operation="revise",
            manifest=SimpleNamespace(
                collection_id="11111111-1111-4111-8111-111111111111",
                path="Knowledge Base/Records/Test/_collection.md",
                storage=SimpleNamespace(source="Knowledge Base/Records/Test/log.md"),
            ),
            before_manifest_hash="a" * 64, after_manifest_hash="b" * 64,
            before_container_hash="c" * 64, after_container_hash="d" * 64,
            payload_hash="e" * 64, why="test", continuity=True,
            acknowledged_gap_codes=(), gap_fingerprint=None, checkpoint_snapshot_hash=None,
        ).split(" ", 2)[-1]
    )
    event[field] = invalid

    assert not records._valid_lifecycle_audit_event(event, "records")
