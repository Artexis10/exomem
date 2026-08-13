from __future__ import annotations

from pathlib import Path

import pytest
from record_presentation_fixtures import ITEM_KEY, manifest_text, setup_collection, values

from exomem import graph_sync, record_formats, record_governance, records, vault, writer_lease
from exomem import structured_collections as collections


@pytest.fixture(autouse=True)
def _writer_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    writer_lease.reset_managers_for_tests()
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "lease-state"))
    yield
    writer_lease.reset_managers_for_tests()


def _append(vault_root: Path, manifest: collections.CollectionManifest) -> dict[str, object]:
    return records.append_record(
        vault_root,
        manifest.path,
        item=values(),
        item_key=ITEM_KEY,
        why="record generic observations",
        body="Authored prose.\n",
    )


def _item(
    root: Path, manifest: collections.CollectionManifest
) -> tuple[record_formats.AdapterSnapshot, record_formats.Record, Path]:
    loaded = collections.load_manifest(root, manifest.path)
    snapshot = record_formats.load_adapter(root, loaded).read()
    record = next(record for record in snapshot.records if record.identity.key == ITEM_KEY)
    return snapshot, record, root / record.source.path


def test_revision_refuses_unrenderable_existing_children_without_writing(tmp_path: Path) -> None:
    manifest = setup_collection(tmp_path, presentation=False)
    bad = values()
    bad["measurements"][0]["value"] = {"not": "scalar"}  # type: ignore[index]
    records.append_record(
        tmp_path, manifest.path, item=bad, item_key=ITEM_KEY, why="seed legacy open child"
    )
    before = (tmp_path / manifest.path).read_bytes()

    with pytest.raises(collections.CollectionError, match="UNRENDERABLE_RECORD_PRESENTATION"):
        records.validate_collection_revision(tmp_path, manifest.path, manifest_text())

    assert (tmp_path / manifest.path).read_bytes() == before


def test_audited_backfill_is_idempotent_and_preserves_canonical_values(tmp_path: Path) -> None:
    manifest = setup_collection(tmp_path, presentation=False)
    _append(tmp_path, manifest)
    before_snapshot, before_record, item_path = _item(tmp_path, manifest)
    before_values = before_record.values
    revision = records.validate_collection_revision(tmp_path, manifest.path, manifest_text())
    revised = records.revise_collection(
        tmp_path,
        manifest.path,
        manifest_text=manifest_text(),
        **revision["lifecycle_guards"],
        why="opt into readable nested records",
    )
    assert revised["operation"] == "revise"
    opted = collections.load_manifest(tmp_path, manifest.path)
    missing = record_governance.inspect_collection(tmp_path, opted.path)
    assert missing["presentation"]["items"][0]["state"] == "missing"
    guard_snapshot, guard_record, _path = _item(tmp_path, opted)

    refreshed = records.update_record(
        tmp_path,
        opted.path,
        item_key=ITEM_KEY,
        changes={},
        expected_container_hash=guard_snapshot.snapshot,
        expected_item_version=guard_record.source.hash,
        why="backfill managed presentation",
        refresh_presentation=True,
    )

    assert refreshed["operation"] == "update"
    after_snapshot, after_record, _path = _item(tmp_path, opted)
    assert after_record.values == before_values
    assert after_record.source.hash != before_record.source.hash
    assert record_governance.inspect_collection(tmp_path, opted.path)["presentation"]["items"] == []
    history = records.agent_audit_history(tmp_path, opted.path)["events"]
    assert [event["operation"] for event in history[:2]] == ["update", "revise"]
    before_noop = item_path.read_bytes()
    with pytest.raises(collections.CollectionError, match="NOOP_RECORD_PRESENTATION"):
        records.update_record(
            tmp_path,
            opted.path,
            item_key=ITEM_KEY,
            changes={},
            expected_container_hash=after_snapshot.snapshot,
            expected_item_version=after_record.source.hash,
            why="do not create a redundant refresh",
            refresh_presentation=True,
        )
    assert item_path.read_bytes() == before_noop
    assert before_snapshot.records[0].values == after_record.values


def test_direct_selected_yaml_edit_rebaseline_then_refresh_keeps_yaml_authoritative(
    tmp_path: Path,
) -> None:
    manifest = setup_collection(tmp_path)
    _append(tmp_path, manifest)
    _snapshot, _record, item_path = _item(tmp_path, manifest)
    direct = item_path.read_text(encoding="utf-8").replace("subject: Sample <A>", "subject: Direct edit", 1)
    item_path.write_text(direct, encoding="utf-8")

    stale = record_governance.inspect_collection(tmp_path, manifest.path)
    assert stale["presentation"]["items"][0]["state"] == "stale"
    assert stale["audit"]["status"] == "gap"
    query = record_formats.query_collection(tmp_path, collections.load_manifest(tmp_path, manifest.path))
    assert query.rows[0]["subject"] == "Direct edit"
    assert "Sample &lt;A&gt;" in item_path.read_text(encoding="utf-8")

    rebaselined = records.rebaseline_collection(
        tmp_path,
        manifest.path,
        **stale["lifecycle_guards"],
        acknowledged_gap_codes=stale["audit"]["gaps"],
        why="acknowledge direct canonical edit",
    )
    assert rebaselined["operation"] == "rebaseline"
    after_rebaseline = record_governance.inspect_collection(tmp_path, manifest.path)
    snapshot, record, _path = _item(tmp_path, manifest)
    refreshed = records.update_record(
        tmp_path,
        manifest.path,
        item_key=ITEM_KEY,
        changes={},
        expected_container_hash=snapshot.snapshot,
        expected_item_version=record.source.hash,
        why="refresh derived view after acknowledged direct edit",
        refresh_presentation=True,
    )

    assert refreshed["operation"] == "update"
    assert after_rebaseline["presentation"]["items"][0]["state"] == "stale"
    assert record_governance.inspect_collection(tmp_path, manifest.path)["presentation"]["items"] == []
    assert "Direct edit" in item_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("damage", ["malformed", "unrenderable"])
def test_refresh_refuses_invalid_presentation_without_any_write(
    tmp_path: Path, damage: str
) -> None:
    manifest = setup_collection(tmp_path)
    _append(tmp_path, manifest)
    _snapshot, _record, item_path = _item(tmp_path, manifest)
    text = item_path.read_text(encoding="utf-8")
    if damage == "malformed":
        item_path.write_text(text + "\n<!-- /exomem-record-presentation -->", encoding="utf-8")
    else:
        item_path.write_text(
            text.replace("value: <5", "value:\n    invalid: object", 1), encoding="utf-8"
        )
    loaded = collections.load_manifest(tmp_path, manifest.path)
    snapshot, record, _path = _item(tmp_path, loaded)
    paths = [item_path, tmp_path / loaded.path, tmp_path / "Knowledge Base/log.md"]
    before = tuple(path.read_bytes() for path in paths)

    with pytest.raises(collections.CollectionError, match="RECORD_PRESENTATION"):
        records.update_record(
            tmp_path,
            loaded.path,
            item_key=ITEM_KEY,
            changes={},
            expected_container_hash=snapshot.snapshot,
            expected_item_version=record.source.hash,
            why="refuse unsafe presentation refresh",
            refresh_presentation=True,
        )

    assert tuple(path.read_bytes() for path in paths) == before


@pytest.mark.parametrize("prefix", [1, 2, 3])
def test_refresh_caught_publication_cuts_roll_back_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prefix: int
) -> None:
    manifest = setup_collection(tmp_path)
    _append(tmp_path, manifest)
    snapshot, record, item_path = _item(tmp_path, manifest)
    paths = [item_path, tmp_path / manifest.path, tmp_path / "Knowledge Base/log.md"]
    before = tuple(path.read_bytes() for path in paths)
    real_block = record_formats._presentation_block
    real_replace = vault._BatchWorkspace.replace_artifact
    calls = 0

    def revised_block(*args, **kwargs):  # noqa: ANN001, ANN202
        return real_block(*args, **kwargs).replace(
            "generated: canonical", "generated: revised canonical", 1
        )

    def deny(workspace, artifact, target):  # noqa: ANN001, ANN202
        nonlocal calls
        calls += 1
        if calls == prefix:
            raise PermissionError(13, "denied", str(target))
        return real_replace(workspace, artifact, target)

    monkeypatch.setattr(record_formats, "_presentation_block", revised_block)
    monkeypatch.setattr(vault._BatchWorkspace, "replace_artifact", deny)
    with pytest.raises(collections.CollectionError, match="RECORD_PUBLICATION_FAILED"):
        records.update_record(
            tmp_path,
            manifest.path,
            item_key=ITEM_KEY,
            changes={},
            expected_container_hash=snapshot.snapshot,
            expected_item_version=record.source.hash,
            why="exercise caught refresh rollback",
            refresh_presentation=True,
        )
    assert tuple(path.read_bytes() for path in paths) == before


@pytest.mark.parametrize("prefix", [1, 2, 3])
def test_refresh_baseexception_cuts_leave_only_auditable_prefixes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prefix: int
) -> None:
    manifest = setup_collection(tmp_path)
    _append(tmp_path, manifest)
    snapshot, record, _item_path = _item(tmp_path, manifest)
    real_block = record_formats._presentation_block
    real_replace = vault._BatchWorkspace.replace_artifact
    calls = 0

    def revised_block(*args, **kwargs):  # noqa: ANN001, ANN202
        return real_block(*args, **kwargs).replace(
            "generated: canonical", "generated: revised canonical", 1
        )

    def interrupt(workspace, artifact, target):  # noqa: ANN001, ANN202
        nonlocal calls
        result = real_replace(workspace, artifact, target)
        if Path(target) not in {graph_sync.floor_path(tmp_path), graph_sync.checkpoint_path(tmp_path)}:
            calls += 1
        if (
            Path(target) not in {graph_sync.floor_path(tmp_path), graph_sync.checkpoint_path(tmp_path)}
            and calls == prefix
        ):
            raise KeyboardInterrupt("abrupt refresh publication")
        return result

    monkeypatch.setattr(record_formats, "_presentation_block", revised_block)
    monkeypatch.setattr(vault._BatchWorkspace, "replace_artifact", interrupt)
    with pytest.raises(KeyboardInterrupt, match="abrupt refresh publication"):
        records.update_record(
            tmp_path,
            manifest.path,
            item_key=ITEM_KEY,
            changes={},
            expected_container_hash=snapshot.snapshot,
            expected_item_version=record.source.hash,
            why="exercise abrupt refresh publication",
            refresh_presentation=True,
        )
    assert records.inspect_audit_gap(tmp_path, manifest.path)["status"] == (
        "ok" if prefix == 3 else "gap"
    )
