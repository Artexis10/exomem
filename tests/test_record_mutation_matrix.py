from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from record_fixtures import copy_vehicle_maintenance_fixture, copy_x3_fixture

from exomem import record_formats, vault, writer_lease
from exomem import structured_collections as collections


def _activity_log(root: Path, text: bytes = b"# Activity\n") -> Path:
    path = root / "Knowledge Base/log.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text)
    return path


@pytest.fixture(autouse=True)
def _isolated_writer_lease_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    writer_lease.reset_managers_for_tests()
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "lease-state"))
    yield
    writer_lease.reset_managers_for_tests()


def _maintenance_item() -> dict[str, object]:
    return {
        "occurred_on": "2026-08-03",
        "asset": "[[Assets/Vehicle]]",
        "provider": "Northside Garage",
        "services": ["oil change"],
        "amount": 95.0,
        "currency": "GBP",
        "status": "completed",
        "next_due_on": None,
    }


def _x3_item(day: str) -> dict[str, object]:
    return {
        "occurred_on": day,
        "title": "Pull",
        "status": "completed",
        "movements": [{"movement": "Deadlift", "band": "grey", "repetitions": "22"}],
    }


def _item_update_context(root: Path) -> tuple[Path, collections.CollectionManifest, object, Path]:
    fixture = copy_vehicle_maintenance_fixture(root)
    log = _activity_log(root)
    manifest = collections.load_manifest(root, fixture / "_collection.md")
    parsed = record_formats.load_adapter(root, manifest).read()
    record = next(item for item in parsed.records if item.identity.key.startswith("14d2bdca"))
    return fixture, manifest, record, log


@pytest.mark.parametrize("prefix", [1, 2, 3])
def test_bom_crlf_item_update_caught_prefixes_restore_exact_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prefix: int
) -> None:
    """Ordinary replacement denials roll every publication prefix back exactly."""
    from exomem import records

    fixture, manifest, record, log = _item_update_context(tmp_path)
    source = tmp_path / record.source.path
    source.write_bytes(source.read_bytes().replace(b"\n", b"\r\n").rstrip(b"\r\n"))
    parsed = record_formats.load_adapter(tmp_path, manifest).read()
    record = next(item for item in parsed.records if item.identity.key == record.identity.key)
    before = (source.read_bytes(), (fixture / "_collection.md").read_bytes(), log.read_bytes())
    real_replace = vault._BatchWorkspace.replace_artifact
    calls = 0

    def deny(workspace, artifact, target):  # noqa: ANN001
        nonlocal calls
        calls += 1
        if calls == prefix:
            raise PermissionError(13, "Access is denied", str(target))
        return real_replace(workspace, artifact, target)

    monkeypatch.setattr(vault._BatchWorkspace, "replace_artifact", deny)
    with pytest.raises(collections.CollectionError, match="RECORD_PUBLICATION_FAILED"):
        records.update_record(
            tmp_path,
            manifest.path,
            item_key=record.identity.key,
            changes={"status": "scheduled"},
            expected_container_hash=parsed.snapshot,
            expected_item_version=record.source.hash,
            why="exercise exact caught rollback",
        )
    assert (
        source.read_bytes(),
        (fixture / "_collection.md").read_bytes(),
        log.read_bytes(),
    ) == before


@pytest.mark.parametrize("prefix", [1, 2, 3])
def test_bom_crlf_item_update_abrupt_prefixes_report_audit_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prefix: int
) -> None:
    """Abrupt interrupts are not normalized; only a complete publication is clean."""
    from exomem import records

    fixture, manifest, record, _log = _item_update_context(tmp_path)
    source = tmp_path / record.source.path
    source.write_bytes(source.read_bytes().replace(b"\n", b"\r\n").rstrip(b"\r\n"))
    parsed = record_formats.load_adapter(tmp_path, manifest).read()
    record = next(item for item in parsed.records if item.identity.key == record.identity.key)
    real_replace = vault._BatchWorkspace.replace_artifact
    calls = 0

    def interrupt(workspace, artifact, target):  # noqa: ANN001
        nonlocal calls
        result = real_replace(workspace, artifact, target)
        calls += 1
        if calls == prefix:
            raise KeyboardInterrupt("abrupt markdown item publication")
        return result

    monkeypatch.setattr(vault._BatchWorkspace, "replace_artifact", interrupt)
    with pytest.raises(KeyboardInterrupt, match="abrupt markdown item publication"):
        records.update_record(
            tmp_path,
            manifest.path,
            item_key=record.identity.key,
            changes={"status": "scheduled"},
            expected_container_hash=parsed.snapshot,
            expected_item_version=record.source.hash,
            why="exercise abrupt publication",
        )
    assert records.inspect_audit_gap(tmp_path, fixture / "_collection.md")["status"] == (
        "ok" if prefix == 3 else "gap"
    )


def test_record_boundaries_are_per_vault_but_serialize_same_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Barriers prove independent vaults enter together and one vault enters in order."""
    from exomem import records

    first_root, second_root = tmp_path / "first", tmp_path / "second"
    first_fixture, second_fixture = copy_x3_fixture(first_root), copy_x3_fixture(second_root)
    _activity_log(first_root)
    _activity_log(second_root)
    first_manifest = collections.load_manifest(first_root, first_fixture / "_collection.md")
    second_manifest = collections.load_manifest(second_root, second_fixture / "_collection.md")
    real_load = records._load_guarded_manifest
    independent_entry = threading.Barrier(2)
    independent_progress = threading.Barrier(3)

    def together(root, collection):  # noqa: ANN001
        if root in {first_root, second_root}:
            independent_entry.wait(timeout=2)
            independent_progress.wait(timeout=2)
        return real_load(root, collection)

    monkeypatch.setattr(records, "_load_guarded_manifest", together)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                records.append_record,
                root,
                manifest.path,
                item=_x3_item(f"2026-08-0{number + 3}"),
                item_key=f"{number + 1:08d}-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                why="prove per-vault progress",
            )
            for number, (root, manifest) in enumerate(
                ((first_root, first_manifest), (second_root, second_manifest))
            )
        ]
        independent_progress.wait(timeout=2)
        assert {future.result(timeout=2)["outcome"] for future in futures} == {"committed"}

    same_root = tmp_path / "same"
    same_fixture = copy_x3_fixture(same_root)
    _activity_log(same_root)
    same_manifest = collections.load_manifest(same_root, same_fixture / "_collection.md")
    first_entry = threading.Barrier(2)
    release_first = threading.Event()
    second_entry = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def serialized(root, collection):  # noqa: ANN001
        nonlocal calls
        if root == same_root:
            with calls_lock:
                calls += 1
                ordinal = calls
            if ordinal == 1:
                first_entry.wait(timeout=2)
                assert release_first.wait(timeout=2)
            else:
                second_entry.set()
        return real_load(root, collection)

    monkeypatch.setattr(records, "_load_guarded_manifest", serialized)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            records.append_record,
            same_root,
            same_manifest.path,
            item=_x3_item("2026-08-03"),
            item_key="11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            why="hold the same vault boundary",
        )
        first_entry.wait(timeout=2)
        second = pool.submit(
            records.append_record,
            same_root,
            same_manifest.path,
            item=_x3_item("2026-08-04"),
            item_key="22222222-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            why="wait for the same vault boundary",
        )
        assert not second_entry.wait(timeout=0.2)
        release_first.set()
        assert first.result(timeout=2)["outcome"] == "committed"
        assert second.result(timeout=2)["outcome"] == "committed"
        assert second_entry.is_set()


@pytest.mark.parametrize("race", ["content", "duplicate"])
def test_item_update_rejects_snapshot_races_without_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, race: str
) -> None:
    """The commit guards bind item bytes and the entire item candidate census."""
    from exomem import records

    fixture, manifest, record, log = _item_update_context(tmp_path)
    source = tmp_path / record.source.path
    parsed = record_formats.load_adapter(tmp_path, manifest).read()
    before = (source.read_bytes(), (fixture / "_collection.md").read_bytes(), log.read_bytes())
    real_batch = vault.batch_atomic_write

    def inject_then_commit(writes, **kwargs):  # noqa: ANN001
        if race == "content":
            source.write_bytes(source.read_bytes() + b"\nmanual content-only edit\n")
        else:
            duplicate = source.parent / "duplicate-id.md"
            duplicate.write_bytes(source.read_bytes())
        return real_batch(writes, **kwargs)

    monkeypatch.setattr(vault, "batch_atomic_write", inject_then_commit)
    with pytest.raises(collections.CollectionError, match="STALE_RECORD|AMBIGUOUS_RECORD"):
        records.update_record(
            tmp_path,
            manifest.path,
            item_key=record.identity.key,
            changes={"status": "scheduled"},
            expected_container_hash=parsed.snapshot,
            expected_item_version=record.source.hash,
            why="refuse a stale item snapshot",
        )
    if race == "content":
        assert source.read_bytes() == before[0] + b"\nmanual content-only edit\n"
    else:
        assert source.read_bytes() == before[0]
    assert (fixture / "_collection.md").read_bytes() == before[1]
    assert log.read_bytes() == before[2]


@pytest.mark.parametrize("alias", ["new", "nfc-source"])
def test_create_rechecks_portable_aliases_at_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alias: str
) -> None:
    """NFC/casefold absence is guarded through commit rather than preflight only."""
    from exomem import records

    _activity_log(tmp_path)
    records_root = tmp_path / "Knowledge Base/Records"
    records_root.mkdir()
    if alias == "nfc-source":
        (records_root / "New").mkdir()
    manifest_path = "Knowledge Base/Records/New/_collection.md"
    source = "Évents" if alias == "nfc-source" else "Events"
    proposed = f"""---
type: collection
exomem_id: 99999999-9999-4999-8999-999999999999
title: New records
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-items
  source: {source}
  format_version: 1
item_schema:
  natural_key: [occurred_on]
  fields:
    occurred_on:
      type: date
      required: true
---
"""
    real_batch = vault.batch_atomic_write

    def inject_alias(writes, **kwargs):  # noqa: ANN001
        if alias == "new":
            (records_root / "new").mkdir()
        else:
            (records_root / "New/E\u0301vents").mkdir()
        return real_batch(writes, **kwargs)

    monkeypatch.setattr(vault, "batch_atomic_write", inject_alias)
    with pytest.raises(collections.CollectionError, match="STALE_RECORD"):
        records.create_collection(tmp_path, manifest_path, proposed, why="reject commit-time alias")
    assert not (tmp_path / manifest_path).exists()


@pytest.mark.parametrize("target_kind", ["item", "manifest", "live", "archive"])
def test_windows_style_replacement_denials_are_bounded_and_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target_kind: str
) -> None:
    """Linux injection covers Windows open-file replacement denial for every target."""
    from exomem import records

    log_text = b"# Activity\n---\n" if target_kind == "archive" else b"# Activity\n"
    fixture, manifest, record, log = _item_update_context(tmp_path)
    log.write_bytes(log_text)
    if target_kind == "archive":
        monkeypatch.setattr(vault, "LOG_ROTATE_KEEP_ENTRIES", 0)
        monkeypatch.setattr(vault, "_log_rotate_bytes", lambda: 1)
    source = tmp_path / record.source.path
    parsed = record_formats.load_adapter(tmp_path, manifest).read()
    before = (source.read_bytes(), (fixture / "_collection.md").read_bytes(), log.read_bytes())
    real_replace = vault._BatchWorkspace.replace_artifact
    denied = False

    def deny_target(workspace, artifact, target):  # noqa: ANN001
        nonlocal denied
        text = Path(target).as_posix()
        matches = {
            "item": target == source,
            "manifest": target == fixture / "_collection.md",
            "live": target == log,
            "archive": "/Knowledge Base/_archive/logs/" in text,
        }
        if matches[target_kind]:
            denied = True
            raise PermissionError(13, "Access is denied", str(target))
        return real_replace(workspace, artifact, target)

    monkeypatch.setattr(vault._BatchWorkspace, "replace_artifact", deny_target)
    with pytest.raises(collections.CollectionError, match="RECORD_PUBLICATION_FAILED"):
        records.update_record(
            tmp_path,
            manifest.path,
            item_key=record.identity.key,
            changes={"status": "scheduled"},
            expected_container_hash=parsed.snapshot,
            expected_item_version=record.source.hash,
            why="inject Windows replacement denial",
        )
    assert denied
    assert (
        source.read_bytes(),
        (fixture / "_collection.md").read_bytes(),
        log.read_bytes(),
    ) == before
