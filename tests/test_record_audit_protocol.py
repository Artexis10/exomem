from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from record_fixtures import copy_vehicle_maintenance_fixture, copy_x3_fixture

from exomem import record_formats, vault, writer_lease
from exomem import structured_collections as collections


def _activity_log(vault: Path) -> None:
    path = vault / "Knowledge Base/log.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Activity\n", encoding="utf-8")


@pytest.fixture(autouse=True)
def _isolated_writer_lease_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    writer_lease.reset_managers_for_tests()
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "lease-state"))
    yield
    writer_lease.reset_managers_for_tests()


def _item(day: str, title: str) -> dict[str, object]:
    return {
        "occurred_on": day,
        "title": title,
        "status": "completed",
        "movements": [{"movement": "Deadlift", "band": "grey", "repetitions": "22"}],
    }


def test_audit_manifest_head_and_transition_chain_keep_manual_gap_visible(tmp_path: Path) -> None:
    """A later transition must not bless an earlier direct canonical edit."""
    from exomem import records

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    initial = record_formats.load_adapter(tmp_path, manifest).read()
    first = records.append_record(
        tmp_path,
        manifest.path,
        item=_item("2026-08-03", "Pull"),
        item_key="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        expected_container_hash=initial.source_versions[-1].hash,
        why="record a session",
    )

    manifest_bytes = (fixture / "_collection.md").read_bytes()
    assert b"record_audit:" in manifest_bytes
    assert first["audit_correlation"] is not None
    assert records.inspect_audit_gap(tmp_path, manifest.path)["status"] == "ok"

    source = fixture / "Training Log.md"
    source.write_bytes(source.read_bytes() + b"\nmanual edit\n")
    changed = collections.load_manifest(tmp_path, fixture / "_collection.md")
    current = record_formats.load_adapter(tmp_path, changed).read()
    records.append_record(
        tmp_path,
        changed.path,
        item=_item("2026-08-04", "Push"),
        item_key="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        expected_container_hash=current.source_versions[-1].hash,
        why="record a later session",
    )

    report = records.inspect_audit_gap(tmp_path, changed.path)
    assert report["status"] == "gap"
    assert report["gaps"]


def test_append_without_an_optional_container_guard_serializes_same_vault_writers(
    tmp_path: Path,
) -> None:
    """Append may use the fresh guarded source when the caller has no prior snapshot."""
    from exomem import records

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    start = threading.Barrier(2)

    def append(number: int) -> dict[str, object]:
        start.wait(timeout=2)
        return records.append_record(
            tmp_path,
            manifest.path,
            item=_item(f"2026-08-0{number + 3}", "Pull"),
            item_key=f"{number + 1:08d}-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            expected_container_hash=None,
            why="record concurrent session",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(append, range(2)))

    assert {result["outcome"] for result in results} == {"committed"}
    assert records.inspect_audit_gap(tmp_path, manifest.path)["status"] == "ok"


def test_audit_history_rejects_conflicting_duplicate_and_unsafe_archive(tmp_path: Path) -> None:
    from exomem import records

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    initial = record_formats.load_adapter(tmp_path, manifest).read()
    records.append_record(
        tmp_path,
        manifest.path,
        item=_item("2026-08-03", "Pull"),
        item_key="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        expected_container_hash=initial.source_versions[-1].hash,
        why="record a session",
    )
    log = tmp_path / "Knowledge Base/log.md"
    event_line = next(
        line for line in log.read_text(encoding="utf-8").splitlines() if "audit-v1" in line
    )
    event = json.loads(event_line.removeprefix("Records audit-v1 "))
    event["after_container_hash"] = "0" * 64
    log.write_text(
        log.read_text(encoding="utf-8") + "\nRecords audit-v1 " + json.dumps(event) + "\n",
        encoding="utf-8",
    )
    assert records.inspect_audit_gap(tmp_path, manifest.path)["status"] == "gap"

    archive = tmp_path / "Knowledge Base/_archive/logs"
    archive.mkdir(parents=True)
    try:
        (archive / "log-00000000000000000000.md").symlink_to(log)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    assert records.inspect_audit_gap(tmp_path, manifest.path)["status"] == "history_incomplete"


def test_audit_chain_is_archive_order_independent_and_exact_duplicates_dedupe(
    tmp_path: Path,
) -> None:
    from exomem import records

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    initial = record_formats.load_adapter(tmp_path, manifest).read()
    first = records.append_record(
        tmp_path,
        manifest.path,
        item=_item("2026-08-03", "Pull"),
        item_key="12121212-1212-4212-8212-121212121212",
        expected_container_hash=initial.source_versions[-1].hash,
        why="record first session",
    )
    refreshed = collections.load_manifest(tmp_path, fixture / "_collection.md")
    second = records.append_record(
        tmp_path,
        refreshed.path,
        item=_item("2026-08-04", "Push"),
        item_key="34343434-3434-4434-8434-343434343434",
        expected_container_hash=first["after_container_hash"],
        why="record second session",
    )
    assert second["outcome"] == "committed"
    log = tmp_path / "Knowledge Base/log.md"
    events = [line for line in log.read_text(encoding="utf-8").splitlines() if "audit-v1" in line]
    archive = tmp_path / "Knowledge Base/_archive/logs"
    archive.mkdir(parents=True)
    (archive / "log-11111111111111111111.md").write_text(
        "\n".join(reversed(events)) + "\n", encoding="utf-8"
    )
    (archive / "log-22222222222222222222.md").write_text(events[0] + "\n", encoding="utf-8")
    log.write_text("# Activity\n", encoding="utf-8")
    assert records.inspect_audit_gap(tmp_path, refreshed.path)["status"] == "ok"
    history = records.agent_audit_history(tmp_path, refreshed.path)
    assert history["status"] == "ok"
    assert [event["transition_id"] for event in history["events"]] == [
        second["audit_correlation"],
        first["audit_correlation"],
    ]


@pytest.mark.parametrize("unsafe", ["fifo", "oversize", "over-cap"])
def test_history_caps_and_unsafe_archive_are_bounded_incomplete(
    tmp_path: Path, unsafe: str
) -> None:
    from exomem import records

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    records.append_record(
        tmp_path,
        manifest.path,
        item=_item("2026-08-03", "Pull"),
        item_key="56565656-5656-4565-8565-565656565656",
        expected_container_hash=snapshot.source_versions[-1].hash,
        why="record bounded history fixture",
    )
    archive = tmp_path / "Knowledge Base/_archive/logs"
    archive.mkdir(parents=True)
    if unsafe == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFOs are unsupported")
        os.mkfifo(archive / "log-00000000000000000000.md")
    elif unsafe == "oversize":
        (archive / "log-00000000000000000000.md").write_bytes(b"x" * 2_000_001)
    else:
        for index in range(129):
            (archive / f"log-{index:020x}.md").write_text("# inert\n", encoding="utf-8")
    report = records.inspect_audit_gap(tmp_path, manifest.path)
    assert report["status"] == "history_incomplete"
    assert len(report["gaps"]) <= 32


def test_history_descriptor_drift_is_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import records

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    records.append_record(
        tmp_path,
        manifest.path,
        item=_item("2026-08-03", "Pull"),
        item_key="78787878-7878-4787-8787-787878787878",
        expected_container_hash=snapshot.source_versions[-1].hash,
        why="record drift fixture",
    )
    archive = tmp_path / "Knowledge Base/_archive/logs"
    archive.mkdir(parents=True)
    drifting = archive / "log-00000000000000000000.md"
    drifting.write_text("# inert\n", encoding="utf-8")
    real_read = vault.read_bounded_guarded_bytes

    def drift(root: Path, relative: str, **kwargs):
        result = real_read(root, relative, **kwargs)
        if relative == "Knowledge Base/_archive/logs/log-00000000000000000000.md":
            drifting.write_text("# changed\n", encoding="utf-8")
        return result

    monkeypatch.setattr(vault, "read_bounded_guarded_bytes", drift)
    assert records.inspect_audit_gap(tmp_path, manifest.path)["status"] == "history_incomplete"


def test_agent_audit_history_authorizes_each_file_before_bounded_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import records

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    records.append_record(
        tmp_path,
        manifest.path,
        item=_item("2026-08-03", "Pull"),
        item_key="90909090-9090-4090-8090-909090909090",
        expected_container_hash=snapshot.source_versions[-1].hash,
        why="audit authorization fixture",
    )
    archive = tmp_path / "Knowledge Base/_archive/logs"
    archive.mkdir(parents=True)
    archive_file = archive / "log-00000000000000000000.md"
    archive_file.write_text("# inert\n", encoding="utf-8")
    authorized: list[str] = []
    reads: list[str] = []
    real_read = vault.read_bounded_guarded_bytes

    def guarded_read(root: Path, relative: str, **kwargs: object):
        if relative == "Knowledge Base/log.md" or relative == archive_file.relative_to(root).as_posix():
            assert relative in authorized
            reads.append(relative)
        return real_read(root, relative, **kwargs)

    monkeypatch.setattr(vault, "read_bounded_guarded_bytes", guarded_read)
    result = records.agent_audit_history(
        tmp_path, manifest.path, authorize_path=lambda path: authorized.append(path) or True
    )

    assert result["status"] == "ok"
    assert set(reads) == {"Knowledge Base/log.md", archive_file.relative_to(tmp_path).as_posix()}
    assert all(set(event) == {
        "transition_id",
        "parent_id",
        "operation",
        "item_key",
        "canonical_path",
        "before_manifest_hash",
        "after_manifest_hash",
        "before_item_hash",
        "after_item_hash",
        "before_container_hash",
        "after_container_hash",
        "rationale",
    } for event in result["events"])
    assert "Pull" not in repr(result)


def test_withheld_audit_history_is_incomplete_without_history_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import records

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    records.append_record(
        tmp_path,
        manifest.path,
        item=_item("2026-08-03", "Pull"),
        item_key="91919191-9191-4191-8191-919191919191",
        expected_container_hash=snapshot.source_versions[-1].hash,
        why="withheld audit fixture",
    )
    real_read = vault.read_bounded_guarded_bytes

    def fail_if_live_audit_is_read(root: Path, relative: str, **kwargs: object):
        if relative == "Knowledge Base/log.md":
            pytest.fail("withheld audit file was parsed")
        return real_read(root, relative, **kwargs)

    monkeypatch.setattr(vault, "read_bounded_guarded_bytes", fail_if_live_audit_is_read)
    report = records.inspect_audit_gap(
        tmp_path, manifest.path, authorize_path=lambda _path: False
    )

    assert report == {"status": "history_incomplete", "gaps": []}


def test_audit_authorization_never_parses_a_withheld_malformed_item(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A partial markdown-items snapshot cannot stand in for the canonical one."""
    from exomem import records

    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    source = fixture / "Events" / "withheld" / "2026-06-01-inspection.md"
    source.write_bytes(b"---\nnot: [valid\n---\n" + b"hidden" * 2048)
    hidden = source.relative_to(tmp_path).as_posix()
    read = vault.read_bounded_guarded_bytes

    def no_hidden_read(root: Path, relative: str, **kwargs: object):
        if relative == hidden:
            pytest.fail("withheld item was parsed for audit inspection")
        return read(root, relative, **kwargs)

    monkeypatch.setattr(vault, "read_bounded_guarded_bytes", no_hidden_read)

    report = records.inspect_audit_gap(
        tmp_path, manifest.path, authorize_path=lambda path: path != hidden
    )

    assert report == {"status": "history_incomplete", "gaps": []}


@pytest.mark.parametrize("path_field", ("manifest_path", "source_path", "canonical_path"))
def test_audit_history_withholds_when_a_relevant_historical_path_is_denied(
    tmp_path: Path, path_field: str
) -> None:
    """A hidden historical path must not yield a partially projected audit chain."""
    from exomem import records

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    first = records.append_record(
        tmp_path,
        manifest.path,
        item=_item("2026-08-03", "Pull"),
        item_key="93939393-9393-4393-8393-939393939393",
        expected_container_hash=snapshot.source_versions[-1].hash,
        why="historical audit authorization fixture",
    )
    current = record_formats.load_adapter(tmp_path, manifest).read()
    records.append_record(
        tmp_path,
        manifest.path,
        item=_item("2026-08-04", "Push"),
        item_key="94949494-9494-4494-8494-949494949494",
        expected_container_hash=current.source_versions[-1].hash,
        why="historical audit authorization fixture",
    )
    log = tmp_path / "Knowledge Base" / "log.md"
    hidden = "Knowledge Base/Records/Health/X3/deleted-session.md"
    text = log.read_text(encoding="utf-8")
    first_id = str(first["audit_correlation"])
    lines = []
    for line in text.splitlines():
        if first_id not in line:
            lines.append(line)
            continue
        event = json.loads(line.removeprefix("Records audit-v1 "))
        event[path_field] = hidden
        lines.append("Records audit-v1 " + json.dumps(event, sort_keys=True))
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    history = records.agent_audit_history(
        tmp_path, manifest.path, authorize_path=lambda path: path != hidden
    )

    assert history == {
        "status": "history_incomplete",
        "complete": False,
        "truncated": False,
        "events": [],
    }


def test_audit_history_withholds_for_a_denied_foreign_child_of_the_live_chain(tmp_path: Path) -> None:
    """A foreign child is audit-relevant only once its parent is live."""
    from exomem import records

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    appended = records.append_record(
        tmp_path,
        manifest.path,
        item=_item("2026-08-03", "Pull"),
        item_key="95959595-9595-4595-8595-959595959595",
        expected_container_hash=snapshot.source_versions[-1].hash,
        why="foreign child authorization fixture",
    )
    log = tmp_path / "Knowledge Base" / "log.md"
    line = next(item for item in log.read_text(encoding="utf-8").splitlines() if "audit-v1" in item)
    event = json.loads(line.removeprefix("Records audit-v1 "))
    hidden = "Knowledge Base/Records/Health/X3/foreign-child.md"
    event.update(
        {
            "transition_id": "eeeeeeeeeeeeeeeeeeeeeeee",
            "parent_id": appended["audit_correlation"],
            "collection_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "canonical_path": hidden,
        }
    )
    log.write_text(log.read_text(encoding="utf-8") + "Records audit-v1 " + json.dumps(event) + "\n", encoding="utf-8")

    history = records.agent_audit_history(
        tmp_path, manifest.path, authorize_path=lambda path: path != hidden
    )

    assert history == {"status": "history_incomplete", "complete": False, "truncated": False, "events": []}


def test_audit_history_withholds_for_a_denied_transition_collision(tmp_path: Path) -> None:
    """A hidden collision must not turn into an exposed conflict gap."""
    from exomem import records

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    records.append_record(
        tmp_path,
        manifest.path,
        item=_item("2026-08-03", "Pull"),
        item_key="96969696-9696-4696-8696-969696969696",
        expected_container_hash=snapshot.source_versions[-1].hash,
        why="collision authorization fixture",
    )
    log = tmp_path / "Knowledge Base" / "log.md"
    line = next(item for item in log.read_text(encoding="utf-8").splitlines() if "audit-v1" in item)
    event = json.loads(line.removeprefix("Records audit-v1 "))
    hidden = "Knowledge Base/Records/Health/X3/hidden-collision.md"
    event.update(
        {"collection_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "canonical_path": hidden}
    )
    log.write_text(log.read_text(encoding="utf-8") + "Records audit-v1 " + json.dumps(event) + "\n", encoding="utf-8")

    history = records.agent_audit_history(
        tmp_path, manifest.path, authorize_path=lambda path: path != hidden
    )

    assert history == {"status": "history_incomplete", "complete": False, "truncated": False, "events": []}


def test_agent_audit_history_caps_head_to_predecessor_chain_without_value_projection(
    tmp_path: Path,
) -> None:
    from exomem import records

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    current_hash = record_formats.load_adapter(tmp_path, manifest).read().source_versions[-1].hash
    last: dict[str, object] | None = None
    for index in range(51):
        last = records.append_record(
            tmp_path,
            manifest.path,
            item=_item(f"2026-08-{index % 28 + 1:02d}", "Pull"),
            item_key=f"{index + 1:08x}-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            expected_container_hash=current_hash,
            why=f"audit chain {index}",
        )
        current_hash = str(last["after_container_hash"])

    history = records.agent_audit_history(tmp_path, manifest.path)

    assert history["status"] == "ok"
    assert history["complete"] is True
    assert history["truncated"] is True
    assert len(history["events"]) == 50
    assert history["events"][0]["transition_id"] == last["audit_correlation"]
    assert history["events"][0]["parent_id"] == history["events"][1]["transition_id"]
    assert "occurred_on" not in repr(history)


def test_replay_uses_the_same_rotated_audit_chain(tmp_path: Path) -> None:
    from exomem import records

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    item = _item("2026-08-03", "Pull")
    first = records.append_record(
        tmp_path,
        manifest.path,
        item=item,
        item_key="92929292-9292-4292-8292-929292929292",
        expected_container_hash=snapshot.source_versions[-1].hash,
        why="replay rotation fixture",
    )
    log = tmp_path / "Knowledge Base/log.md"
    archive = tmp_path / "Knowledge Base/_archive/logs"
    archive.mkdir(parents=True)
    (archive / "log-11111111111111111111.md").write_bytes(log.read_bytes())
    log.write_text("# Activity\n", encoding="utf-8")

    replay = records.append_record(
        tmp_path,
        manifest.path,
        item=item,
        item_key="92929292-9292-4292-8292-929292929292",
        expected_container_hash=first["after_container_hash"],
        why="replay rotation fixture",
    )

    assert replay["outcome"] == "replayed"
    assert replay["audit_correlation"] == first["audit_correlation"]


def test_caught_and_abrupt_publication_prefixes_leave_rollback_or_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import records

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    source = fixture / "Training Log.md"
    before = (
        source.read_bytes(),
        (fixture / "_collection.md").read_bytes(),
        (tmp_path / "Knowledge Base/log.md").read_bytes(),
    )
    initial = record_formats.load_adapter(tmp_path, manifest).read()
    real_replace = vault._BatchWorkspace.replace_artifact
    calls = 0

    def fail_first(workspace, artifact, target):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError(13, "Access is denied", str(target))
        return real_replace(workspace, artifact, target)

    monkeypatch.setattr(vault._BatchWorkspace, "replace_artifact", fail_first)
    with pytest.raises(collections.CollectionError, match="RECORD_PUBLICATION_FAILED"):
        records.append_record(
            tmp_path,
            manifest.path,
            item=_item("2026-08-03", "Pull"),
            item_key="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            expected_container_hash=initial.source_versions[-1].hash,
            why="exercise portable replacement failure",
        )
    assert (
        source.read_bytes(),
        (fixture / "_collection.md").read_bytes(),
        (tmp_path / "Knowledge Base/log.md").read_bytes(),
    ) == before

    monkeypatch.setattr(vault._BatchWorkspace, "replace_artifact", real_replace)
    calls = 0

    def interrupt_after_canonical(workspace, artifact, target):
        nonlocal calls
        result = real_replace(workspace, artifact, target)
        calls += 1
        if calls == 1:
            raise KeyboardInterrupt("simulated abrupt publication")
        return result

    monkeypatch.setattr(vault._BatchWorkspace, "replace_artifact", interrupt_after_canonical)
    with pytest.raises(KeyboardInterrupt, match="simulated abrupt publication"):
        records.append_record(
            tmp_path,
            manifest.path,
            item=_item("2026-08-03", "Pull"),
            item_key="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
            expected_container_hash=initial.source_versions[-1].hash,
            why="exercise abrupt publication",
        )
    assert records.inspect_audit_gap(tmp_path, manifest.path)["status"] == "gap"


@pytest.mark.parametrize("prefix", [1, 2, 3])
def test_later_log_update_publication_prefixes_roll_back_or_report_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prefix: int
) -> None:
    from exomem import records

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    first_snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    first = records.append_record(
        tmp_path,
        manifest.path,
        item=_item("2026-08-03", "Pull"),
        item_key="abababab-abab-4aba-8aba-abababababab",
        expected_container_hash=first_snapshot.source_versions[-1].hash,
        why="establish later transition",
    )
    current = collections.load_manifest(tmp_path, fixture / "_collection.md")
    record = next(
        item
        for item in record_formats.load_adapter(tmp_path, current).read().records
        if item.identity.key == "abababab-abab-4aba-8aba-abababababab"
    )
    source = fixture / "Training Log.md"
    before = (
        source.read_bytes(),
        (fixture / "_collection.md").read_bytes(),
        (tmp_path / "Knowledge Base/log.md").read_bytes(),
    )
    real_replace = vault._BatchWorkspace.replace_artifact
    calls = 0

    def fail(workspace, artifact, target):
        nonlocal calls
        calls += 1
        if calls == prefix:
            raise PermissionError(13, "Access is denied", str(target))
        return real_replace(workspace, artifact, target)

    monkeypatch.setattr(vault._BatchWorkspace, "replace_artifact", fail)
    with pytest.raises(collections.CollectionError, match="RECORD_PUBLICATION_FAILED"):
        records.update_record(
            tmp_path,
            current.path,
            item_key=record.identity.key,
            changes={"title": "Push"},
            expected_container_hash=first["after_container_hash"],
            expected_item_version=record.source.hash,
            why="exercise later rollback",
        )
    assert (
        source.read_bytes(),
        (fixture / "_collection.md").read_bytes(),
        (tmp_path / "Knowledge Base/log.md").read_bytes(),
    ) == before


@pytest.mark.parametrize("prefix", [1, 2, 3])
def test_later_log_update_abrupt_prefixes_are_not_normalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prefix: int
) -> None:
    from exomem import records

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    initial = record_formats.load_adapter(tmp_path, manifest).read()
    first = records.append_record(
        tmp_path,
        manifest.path,
        item=_item("2026-08-03", "Pull"),
        item_key="cdcdcdcd-cdcd-4cdc-8cdc-cdcdcdcdcdcd",
        expected_container_hash=initial.source_versions[-1].hash,
        why="establish abrupt transition",
    )
    current = collections.load_manifest(tmp_path, fixture / "_collection.md")
    record = next(
        item
        for item in record_formats.load_adapter(tmp_path, current).read().records
        if item.identity.key == "cdcdcdcd-cdcd-4cdc-8cdc-cdcdcdcdcdcd"
    )
    real_replace = vault._BatchWorkspace.replace_artifact
    calls = 0

    def interrupt(workspace, artifact, target):
        nonlocal calls
        result = real_replace(workspace, artifact, target)
        calls += 1
        if calls == prefix:
            raise KeyboardInterrupt("later abrupt prefix")
        return result

    monkeypatch.setattr(vault._BatchWorkspace, "replace_artifact", interrupt)
    with pytest.raises(KeyboardInterrupt, match="later abrupt prefix"):
        records.update_record(
            tmp_path,
            current.path,
            item_key=record.identity.key,
            changes={"title": "Push"},
            expected_container_hash=first["after_container_hash"],
            expected_item_version=record.source.hash,
            why="exercise later abrupt prefix",
        )
    report = records.inspect_audit_gap(tmp_path, current.path)
    assert report["status"] == ("ok" if prefix == 3 else "gap")


@pytest.mark.parametrize("prefix", [1, 2, 3])
def test_item_append_caught_prefixes_restore_exact_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prefix: int
) -> None:
    from exomem import records

    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    before = (
        (fixture / "_collection.md").read_bytes(),
        (tmp_path / "Knowledge Base/log.md").read_bytes(),
    )
    real_replace = vault._BatchWorkspace.replace_artifact
    calls = 0

    def deny(workspace, artifact, target):
        nonlocal calls
        calls += 1
        if calls == prefix:
            raise PermissionError(13, "Access is denied", str(target))
        return real_replace(workspace, artifact, target)

    monkeypatch.setattr(vault._BatchWorkspace, "replace_artifact", deny)
    key = "98989898-9898-4989-8989-989898989898"
    with pytest.raises(collections.CollectionError, match="RECORD_PUBLICATION_FAILED"):
        records.append_record(
            tmp_path,
            manifest.path,
            item={
                "occurred_on": "2026-08-03",
                "asset": "[[Assets/Vehicle]]",
                "provider": "Garage",
                "services": ["oil change"],
                "amount": 95.0,
                "currency": "GBP",
                "status": "completed",
                "next_due_on": None,
            },
            item_key=key,
            expected_container_hash=snapshot.snapshot,
            why="exercise item rollback",
        )
    assert not (fixture / "Events" / f"{key}.md").exists()
    assert (
        (fixture / "_collection.md").read_bytes(),
        (tmp_path / "Knowledge Base/log.md").read_bytes(),
    ) == before


@pytest.mark.parametrize("prefix", [1, 2, 3])
def test_item_append_abrupt_prefixes_remain_inspectable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prefix: int
) -> None:
    from exomem import records

    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    real_replace, calls = vault._BatchWorkspace.replace_artifact, 0

    def interrupt(workspace, artifact, target):
        nonlocal calls
        result = real_replace(workspace, artifact, target)
        calls += 1
        if calls == prefix:
            raise KeyboardInterrupt("item abrupt prefix")
        return result

    monkeypatch.setattr(vault._BatchWorkspace, "replace_artifact", interrupt)
    with pytest.raises(KeyboardInterrupt, match="item abrupt prefix"):
        records.append_record(
            tmp_path,
            manifest.path,
            item={
                "occurred_on": "2026-08-03",
                "asset": "[[Assets/Vehicle]]",
                "provider": "Garage",
                "services": ["oil change"],
                "amount": 95.0,
                "currency": "GBP",
                "status": "completed",
                "next_due_on": None,
            },
            item_key="67676767-6767-4676-8676-676767676767",
            expected_container_hash=snapshot.snapshot,
            why="exercise abrupt item append",
        )
    assert records.inspect_audit_gap(tmp_path, manifest.path)["status"] == (
        "ok" if prefix == 3 else "gap"
    )


def test_create_rolls_back_caught_failure_and_exposes_abrupt_manifest_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import records

    _activity_log(tmp_path)
    manifest_path = "Knowledge Base/Records/New/_collection.md"
    text = """---
type: collection
exomem_id: ffffffff-ffff-4fff-8fff-ffffffffffff
title: New records
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-items
  source: Events
  format_version: 1
item_schema:
  natural_key: [occurred_on]
  fields:
    occurred_on:
      type: date
      required: true
---
"""
    real_replace = vault._BatchWorkspace.replace_artifact
    calls = 0

    def fail_scaffold(workspace, artifact, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PermissionError(13, "Access is denied", str(target))
        return real_replace(workspace, artifact, target)

    monkeypatch.setattr(vault._BatchWorkspace, "replace_artifact", fail_scaffold)
    with pytest.raises(collections.CollectionError, match="RECORD_PUBLICATION_FAILED"):
        records.create_collection(tmp_path, manifest_path, text, why="exercise create rollback")
    assert not (tmp_path / manifest_path).exists()
    assert not (tmp_path / "Knowledge Base/Records/New/Events").exists()

    monkeypatch.setattr(vault._BatchWorkspace, "replace_artifact", real_replace)
    calls = 0

    def interrupt_after_manifest(workspace, artifact, target):
        nonlocal calls
        result = real_replace(workspace, artifact, target)
        calls += 1
        if calls == 1:
            raise KeyboardInterrupt("simulated abrupt create")
        return result

    monkeypatch.setattr(vault._BatchWorkspace, "replace_artifact", interrupt_after_manifest)
    with pytest.raises(KeyboardInterrupt, match="simulated abrupt create"):
        records.create_collection(tmp_path, manifest_path, text, why="exercise abrupt create")
    assert (tmp_path / manifest_path).is_file()
    assert records.inspect_audit_gap(tmp_path, manifest_path)["status"] == "gap"


def test_create_refuses_nfc_casefold_component_alias(tmp_path: Path) -> None:
    from exomem import records

    _activity_log(tmp_path)
    (tmp_path / "Knowledge Base/Records/new").mkdir(parents=True)
    text = """---
type: collection
exomem_id: 99999999-9999-4999-8999-999999999999
title: New records
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-items
  source: Events
  format_version: 1
item_schema:
  natural_key: [occurred_on]
  fields:
    occurred_on:
      type: date
      required: true
---
"""
    with pytest.raises(collections.CollectionError, match="CREATE_ONLY_CONFLICT"):
        records.create_collection(
            tmp_path, "Knowledge Base/Records/New/_collection.md", text, why="reject alias"
        )


@pytest.mark.parametrize(
    "field,value,status",
    [
        ("canonical_path", "outside.md", "gap"),
        ("item_key", "not-a-uuid", "history_incomplete"),
    ],
)
def test_create_transition_requires_null_item_and_declared_source(
    tmp_path: Path, field: str, value: str, status: str
) -> None:
    from exomem import records

    _activity_log(tmp_path)
    manifest_path = "Knowledge Base/Records/New/_collection.md"
    text = """---
type: collection
exomem_id: 11111111-1111-4111-8111-111111111111
title: New records
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-items
  source: Events
  format_version: 1
item_schema:
  natural_key: [occurred_on]
  fields:
    occurred_on:
      type: date
      required: true
---
"""
    records.create_collection(tmp_path, manifest_path, text, why="create audit fixture")
    log = tmp_path / "Knowledge Base/log.md"
    lines = log.read_text(encoding="utf-8").splitlines()
    index = next(index for index, line in enumerate(lines) if "audit-v1" in line)
    event = json.loads(lines[index].removeprefix("Records audit-v1 "))
    event[field] = value
    lines[index] = "Records audit-v1 " + json.dumps(event)
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert records.inspect_audit_gap(tmp_path, manifest_path)["status"] == status


def test_audit_marker_sources_are_bounded_after_adapter_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A post-adapter source swap never leaks markers or blocks inspection."""
    from exomem import records

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    records.append_record(
        tmp_path,
        manifest.path,
        item=_item("2026-08-03", "Pull"),
        item_key="12121212-1212-4212-8212-121212121212",
        expected_container_hash=snapshot.source_versions[-1].hash,
        why="audit swap fixture",
    )
    source = fixture / "Training Log.md"
    outside = tmp_path / "outside.md"
    outside.write_text("<!-- exomem-record-audit: ffffffffffffffffffffffff -->", encoding="utf-8")
    real_read = record_formats.MarkdownLogAdapter.read
    swapped = False

    def swap(self):  # noqa: ANN001
        nonlocal swapped
        value = real_read(self)
        if not swapped:
            swapped = True
            source.unlink()
            try:
                source.symlink_to(outside)
            except OSError:
                pytest.skip("symlinks unavailable")
        return value

    monkeypatch.setattr(record_formats.MarkdownLogAdapter, "read", swap)
    report = records.inspect_audit_gap(tmp_path, manifest.path)
    assert report == {"status": "history_incomplete", "gaps": []}


@pytest.mark.parametrize("replacement", ["fifo", "oversize", "marker-cap"])
def test_post_adapter_marker_source_failures_are_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    from exomem import records

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    records.append_record(
        tmp_path,
        manifest.path,
        item=_item("2026-08-03", "Pull"),
        item_key="34343434-3434-4434-8434-343434343434",
        expected_container_hash=snapshot.source_versions[-1].hash,
        why="audit source fixture",
    )
    source = fixture / "Training Log.md"
    real_read, changed = record_formats.MarkdownLogAdapter.read, False

    def replace_after_read(self):  # noqa: ANN001
        nonlocal changed
        value = real_read(self)
        if not changed:
            changed = True
            if replacement == "marker-cap":
                monkeypatch.setattr(records, "_MAX_AUDIT_MARKERS", 0)
            else:
                source.unlink()
                if replacement == "fifo":
                    if not hasattr(os, "mkfifo"):
                        pytest.skip("FIFOs are unsupported")
                    os.mkfifo(source)
                else:
                    source.write_bytes(b"x" * (records._MAX_AUDIT_SOURCE_BYTES + 1))
        return value

    monkeypatch.setattr(record_formats.MarkdownLogAdapter, "read", replace_after_read)
    assert records.inspect_audit_gap(tmp_path, manifest.path) == {
        "status": "history_incomplete",
        "gaps": [],
    }


def test_record_create_staging_interrupt_leaves_no_residue_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import records

    _activity_log(tmp_path)
    manifest_path = "Knowledge Base/Records/New/_collection.md"
    text = """---
type: collection
exomem_id: 22222222-2222-4222-8222-222222222222
title: New records
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-items
  source: Events
  format_version: 1
item_schema:
  natural_key: [occurred_on]
  fields:
    occurred_on:
      type: date
      required: true
---
"""
    real_create = vault._BatchWorkspace.create_artifact
    calls = 0

    def interrupt(workspace, name, content):  # noqa: ANN001
        nonlocal calls
        calls += 1
        if calls == 1:
            raise KeyboardInterrupt("stop creating record collection")
        return real_create(workspace, name, content)

    monkeypatch.setattr(vault._BatchWorkspace, "create_artifact", interrupt)
    with pytest.raises(KeyboardInterrupt, match="stop creating record collection"):
        records.create_collection(tmp_path, manifest_path, text, why="interrupt collection staging")
    assert not (tmp_path / manifest_path).exists()
    assert not (tmp_path / "Knowledge Base/Records/New/Events").exists()
    assert not list(tmp_path.rglob(".exomem-batch-*"))
    monkeypatch.setattr(vault._BatchWorkspace, "create_artifact", real_create)
    assert records.create_collection(tmp_path, manifest_path, text, why="retry collection")
