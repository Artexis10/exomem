"""Migration audit proof preserves original history and uncaps only its reader."""

from __future__ import annotations

import hashlib
import json
import os
import time

import pytest
from record_fixtures import copy_x3_fixture
from test_planning_profile import _manifest as planning_manifest_text

from exomem import record_formats, records
from exomem import structured_collections as collections
from exomem.collection_store import connection, legacy, legacy_import
from exomem.collection_store.connection import CollectionStoreError


def _hash(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def _manifest(root, head):
    fixture = copy_x3_fixture(root)
    path = fixture / "_collection.md"
    if head is not None:
        path.write_text(
            record_formats.render_manifest_audit_head(
                path.read_text(),
                head,
                semantic_profile="records",
                reader_version=1,
            )
        )
    return collections.load_manifest(root, path)


def _event(manifest, number, *, why="invented migration history"):
    return records._audit_body(
        transition_id=f"{number:024x}",
        parent_id=f"{number - 1:024x}" if number > 1 else "baseline",
        operation="append",
        manifest=manifest,
        item_key=f"{number:08x}-0000-4000-8000-000000000001",
        canonical_path=(
            manifest.storage.source
            if manifest.storage.strategy == "markdown-log"
            else manifest.storage.source + f"/{number}.md"
        ),
        before_manifest_hash=manifest.manifest_version.hash,
        after_manifest_hash=manifest.manifest_version.hash,
        before_item_hash=None,
        after_item_hash=_hash(number),
        before_container_hash=_hash(number - 1),
        after_container_hash=_hash(number),
        payload_hash=_hash(number),
        why=why,
    )


def _inspection(root, manifest, current_hash):
    chain = records._reconstruct_audit_chain(
        root,
        manifest,
        head=manifest.audit_head,
        manifest_hash=manifest.manifest_version.hash,
        current_hash=current_hash,
        markers=(),
    )
    return {"status": chain.status, "gaps": list(chain.gaps)}


def _stage(tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)
    return stage


def _rewrite(line, **changes):
    prefix, _, body = line.partition("{")
    event = json.loads("{" + body)
    event.update(changes)
    return prefix + json.dumps(event)


def _verify(audit, manifest, current_hash, *, historical=None, markers=()):
    return audit.verify(
        manifest=manifest,
        current_container_hash=current_hash,
        markers=iter(markers),
        legacy_inspection=historical or {"status": "ok"},
    )


def test_parent_links_order_events_and_keep_first_original_json(tmp_path):
    # Archive hashes and newest-first lines must not become chronology.
    root = tmp_path / "vault"
    manifest = _manifest(root, f"{3:024x}")
    prefix = records.profile_for("records").activity_prefix
    events = [_event(manifest, number) for number in (1, 2, 3)]
    original = json.dumps(json.loads(events[2][len(prefix) :]), separators=(", ", ": "))
    log = root / "Knowledge Base/log.md"
    log.write_text(prefix + original + "\n" + events[1] + "\n" + _rewrite(events[1]) + "\n")
    archive = root / "Knowledge Base/_archive/logs"
    archive.mkdir(parents=True)
    (archive / ("log-" + "f" * 20 + ".md")).write_text(
        events[2] + "\n" + _rewrite(events[0]) + "\n"
    )
    (archive / ("log-" + "0" * 20 + ".md")).write_text(events[0] + "\n")
    historical = _inspection(root, manifest, _hash(3))
    stage = _stage(tmp_path)
    with legacy.LegacyAuditSpool(stage, deadline=time.monotonic() + 20) as audit:
        audit.scan(root)
        proof = audit.verify(
            manifest=manifest,
            current_container_hash=_hash(3),
            markers=iter(()),
            legacy_inspection=historical,
        )
        assert proof.exhaustive_status == "ok"
        assert proof.legacy_inspection == historical
        assert proof.reachable_head == manifest.audit_head
        assert proof.reachable_count == 3
        captured = list(audit.iter_reachable(proof))
        assert [event.transition_id for event in captured] == [f"{n:024x}" for n in (1, 2, 3)]
        assert captured[-1].original_json == original
        assert captured[0].original_json == events[0][len(prefix) :]
        assert captured[1].original_json == events[1][len(prefix) :]
        assert [
            event.transition_id for event in audit.iter_reachable(proof, oldest_first=False)
        ] == [f"{n:024x}" for n in (3, 2, 1)]
        assert len(proof.ordered_event_digest) == 64
        assert len(proof.input_basis_digest) == 64
    assert list(stage.iterdir()) == []


@pytest.mark.parametrize("influence", ["collision", "child", "foreign-conflict", "unrelated"])
def test_foreign_profile_events_influence_gaps_but_are_not_emitted(tmp_path, influence):
    # Filtering by collection before same-ID selection loses historical defects.
    root = tmp_path / "vault"
    manifest = _manifest(root, f"{1:024x}")
    own = _event(manifest, 1)
    foreign = _rewrite(_event(manifest, 9), collection_id="22222222-2222-4222-8222-222222222222")
    lines = [own]
    if influence == "collision":
        lines.append(_rewrite(foreign, transition_id=f"{1:024x}"))
    elif influence == "child":
        lines.append(_rewrite(foreign, parent_id=f"{1:024x}"))
    elif influence == "foreign-conflict":
        lines.extend((foreign, _rewrite(foreign, rationale="different foreign history")))
    else:
        lines.append(foreign)
    (root / "Knowledge Base/log.md").write_text("\n".join(lines))
    historical = _inspection(root, manifest, _hash(1))
    with legacy.LegacyAuditSpool(_stage(tmp_path), deadline=time.monotonic() + 20) as audit:
        audit.scan(root)
        proof = _verify(audit, manifest, _hash(1), historical=historical)
        assert proof.exhaustive_status == historical["status"]
        assert proof.diagnostic_samples == tuple(historical["gaps"])
        assert [event.transition_id for event in audit.iter_reachable(proof)] == [f"{1:024x}"]


def test_baseline_precedes_foreign_conflicts_and_input_digest_binds_foreign_history(tmp_path):
    # Foreign-only conflicts do not overturn the inspector's early baseline.
    root = tmp_path / "vault"
    manifest = _manifest(root, None)
    foreign = _rewrite(_event(manifest, 9), collection_id="22222222-2222-4222-8222-222222222222")
    log = root / "Knowledge Base/log.md"
    log.write_text(foreign + "\n" + _rewrite(foreign, rationale="foreign conflict"))
    stage = _stage(tmp_path)
    with legacy.LegacyAuditSpool(stage, deadline=time.monotonic() + 20) as audit:
        audit.scan(root)
        first = _verify(audit, manifest, _hash(0))
        assert first.exhaustive_status == "baseline"
        assert first.gap_count == first.reachable_count == 0
    log.write_text(foreign)
    with legacy.LegacyAuditSpool(stage, deadline=time.monotonic() + 20) as audit:
        audit.scan(root)
        second = _verify(audit, manifest, _hash(0))
        assert second.manifest_head == first.manifest_head
        assert second.ordered_event_digest == first.ordered_event_digest
        assert second.input_basis_digest != first.input_basis_digest


@pytest.mark.parametrize(
    "defect", ["missing-parent", "current-container", "discontinuity", "rebaseline"]
)
def test_real_gaps_and_acknowledged_rebaseline_keep_finite_representation(tmp_path, defect):
    # Exhaustive representation must not turn a genuine provenance gap into ok.
    root = tmp_path / "vault"
    manifest = _manifest(root, f"{2:024x}")
    lines = [_event(manifest, 2), _event(manifest, 1)]
    current = _hash(2)
    if defect == "missing-parent":
        lines.pop()
    elif defect == "current-container":
        current = _hash("manual edit")
    elif defect == "discontinuity":
        lines[0] = _rewrite(lines[0], before_container_hash=_hash("manual edit"))
    else:
        lines[0] = records._lifecycle_audit_body(
            transition_id=f"{2:024x}",
            parent_id=f"{1:024x}",
            operation="rebaseline",
            manifest=manifest,
            before_manifest_hash=manifest.manifest_version.hash,
            after_manifest_hash=manifest.manifest_version.hash,
            before_container_hash=_hash("manual edit"),
            after_container_hash=_hash(2),
            payload_hash=_hash("acknowledgement"),
            why="accept the known manual gap",
            continuity=False,
            acknowledged_gap_codes=("current-container-mismatch",),
            gap_fingerprint=_hash("gap"),
            checkpoint_snapshot_hash=_hash("snapshot"),
        )
    (root / "Knowledge Base/log.md").write_text("\n".join(lines))
    historical = _inspection(root, manifest, current)
    with legacy.LegacyAuditSpool(_stage(tmp_path), deadline=time.monotonic() + 20) as audit:
        audit.scan(root)
        proof = _verify(audit, manifest, current, historical=historical)
        assert proof.exhaustive_status == historical["status"]
        assert proof.gap_count == len(historical["gaps"])
        assert proof.discontinuity_count == int(defect == "rebaseline")
        captured = list(audit.iter_reachable(proof))
        assert len(captured) == (1 if defect in {"missing-parent", "discontinuity"} else 2)
        assert (
            json.loads(captured[-1].original_json)["rationale"]
            == json.loads("{" + lines[0].partition("{")[2])["rationale"]
        )


@pytest.mark.parametrize("defect", ["missing-head", "cycle"])
def test_no_proof_for_an_unrepresentable_head_or_cycle(tmp_path, defect):
    root = tmp_path / "vault"
    manifest = _manifest(root, f"{2:024x}")
    lines = [_event(manifest, 1)]
    if defect == "cycle":
        lines = [
            _rewrite(
                _event(manifest, number),
                parent_id=f"{3 - number:024x}",
                before_container_hash=_hash(2),
                after_container_hash=_hash(2),
            )
            for number in (1, 2)
        ]
    (root / "Knowledge Base/log.md").write_text("\n".join(lines))
    stage = _stage(tmp_path)
    with legacy.LegacyAuditSpool(stage, deadline=time.monotonic() + 20) as audit:
        audit.scan(root)
        with pytest.raises(CollectionStoreError, match="UNREPRESENTABLE"):
            _verify(audit, manifest, _hash(2))
    assert list(stage.iterdir()) == []


@pytest.mark.parametrize("malformed", ["json", "escaped-surrogate"])
def test_malformed_event_refuses_only_its_semantic_profile(tmp_path, malformed):
    # One profile's malformed event must not reinterpret or block the other.
    root = tmp_path / "vault"
    manifest = _manifest(root, None)
    planning_path = root / "Knowledge Base/Planning/Tasks/_collection.md"
    planning_path.parent.mkdir(parents=True)
    planning_path.write_text(
        record_formats.render_manifest_audit_head(
            planning_manifest_text(),
            f"{1:024x}",
            semantic_profile="planning",
            reader_version=1,
        )
    )
    planning = collections.load_manifest(root, planning_path)
    bad = "Records audit-v1 {invalid"
    if malformed == "escaped-surrogate":
        bad = _rewrite(_event(manifest, 9), rationale="\ud800")
    (root / "Knowledge Base/log.md").write_text(bad + "\n" + _event(planning, 1))
    with legacy.LegacyAuditSpool(_stage(tmp_path), deadline=time.monotonic() + 20) as audit:
        audit.scan(root)
        with pytest.raises(CollectionStoreError, match="INVALID"):
            _verify(audit, manifest, _hash(0))
        proof = _verify(audit, planning, _hash(1))
        assert proof.exhaustive_status == "ok"
        assert proof.reachable_count == 1


def test_malformed_utf8_and_unsafe_archive_alias_invalidate_shared_scan(tmp_path):
    root = tmp_path / "vault"
    manifest = _manifest(root, f"{1:024x}")
    log = root / "Knowledge Base/log.md"
    original = (_event(manifest, 1) + "\n").encode()
    log.write_bytes(original + b"\xff")
    stage = _stage(tmp_path)
    with legacy.LegacyAuditSpool(stage, deadline=time.monotonic() + 20) as audit:
        with pytest.raises(UnicodeError):
            audit.scan(root)
        with pytest.raises(CollectionStoreError, match="INVALID"):
            _verify(audit, manifest, _hash(1))
    assert log.read_bytes() == original + b"\xff"
    log.write_bytes(original)
    archive = root / "Knowledge Base/_archive/logs"
    archive.mkdir(parents=True)
    try:
        (archive / "not-an-audit-file").symlink_to(log)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with legacy.LegacyAuditSpool(stage, deadline=time.monotonic() + 20) as audit:
        with pytest.raises(legacy.held_fs.HeldFsError):
            audit.scan(root)
    assert log.read_bytes() == original
    assert list(stage.iterdir()) == []


@pytest.mark.parametrize("change", ["bytes", "identity", "census", "manifest"])
def test_changed_inputs_refuse_proof_or_event_exposure_without_rewriting_sources(tmp_path, change):
    root = tmp_path / "vault"
    manifest = _manifest(root, f"{1:024x}")
    log = root / "Knowledge Base/log.md"
    log.write_text(_event(manifest, 1))
    stage = _stage(tmp_path)
    with legacy.LegacyAuditSpool(stage, deadline=time.monotonic() + 20) as audit:
        audit.scan(root)
        proof = _verify(audit, manifest, _hash(1))
        if change == "bytes":
            log.write_text(log.read_text() + "\nmanual activity")
        elif change == "identity":
            replacement = log.with_name("replacement.md")
            replacement.write_bytes(log.read_bytes())
            os.replace(replacement, log)
        elif change == "census":
            archive = root / "Knowledge Base/_archive/logs"
            archive.mkdir(parents=True)
            (archive / "new-ordinary-file.md").write_text("manual activity")
        else:
            path = root / manifest.path
            path.write_text(path.read_text() + "\nmanual edit")
        expected = log.read_bytes()
        with pytest.raises(CollectionStoreError, match="INVALID"):
            next(audit.iter_reachable(proof))
        with pytest.raises(CollectionStoreError, match="INVALID"):
            _verify(audit, manifest, _hash(1))
        assert log.read_bytes() == expected
    assert list(stage.iterdir()) == []


def test_disk_exhaustion_cancellation_and_deadline_leave_sources_and_no_spool(tmp_path):
    root = tmp_path / "vault"
    manifest = _manifest(root, f"{50:024x}")
    log = root / "Knowledge Base/log.md"
    log.write_text("\n".join(_event(manifest, n) for n in range(50, 0, -1)))
    original = log.read_bytes()
    stage = _stage(tmp_path)
    with legacy.LegacyAuditSpool(stage, deadline=time.monotonic() + 20) as audit:
        pages = audit._connection.execute("PRAGMA page_count").fetchone()[0]
        audit._connection.execute(f"PRAGMA max_page_count={pages + 1}")
        with pytest.raises(CollectionStoreError, match="INVALID"):
            audit.scan(root)
    cancelled = False
    with legacy.LegacyAuditSpool(
        stage, deadline=time.monotonic() + 20, cancelled=lambda: cancelled
    ) as audit:
        audit.scan(root)
        cancelled = True
        with pytest.raises(CollectionStoreError, match="CANCELLED"):
            _verify(audit, manifest, _hash(50))
        cancelled = False
        with pytest.raises(CollectionStoreError, match="INVALID"):
            _verify(audit, manifest, _hash(50))
    with pytest.raises(TimeoutError, match="DEADLINE"):
        with legacy.LegacyAuditSpool(stage, deadline=time.monotonic() - 1):
            pass
    assert log.read_bytes() == original
    assert list(stage.iterdir()) == []


def test_single_large_segment_splitline_grammar_and_uncapped_marker_findings(tmp_path):
    # Per-file caps, CRLF chunk splits, and marker truncation each lose evidence.
    root = tmp_path / "vault"
    manifest = _manifest(root, f"{1:024x}")
    body = _event(manifest, 1, why="invented unicode 🧪 history")
    padding = "inert " + "x" * (33 * 65_536 - 7)
    (root / "Knowledge Base/log.md").write_text(padding + "\r\n" + body + "\u2028" + body)

    def markers():
        for number in range(2, 10_004):
            yield records._AuditMarker(
                f"{number:024x}", manifest.storage.source, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            )

    with legacy.LegacyAuditSpool(_stage(tmp_path), deadline=time.monotonic() + 20) as audit:
        audit.scan(root)
        proof = _verify(audit, manifest, _hash(1), markers=markers())
        assert proof.exhaustive_status == "gap"
        assert proof.gap_count == 10_002
        assert len(proof.diagnostic_samples) == 32
        assert proof.reachable_count == 1
        assert (
            next(audit.iter_reachable(proof)).original_json
            == body.partition("{")[1] + body.partition("{")[2]
        )


def test_cleanup_never_removes_a_foreign_replacement(tmp_path):
    # Closing a spool must not unlink a different inode at its former name.
    stage = _stage(tmp_path)
    with legacy.LegacyAuditSpool(stage, deadline=time.monotonic() + 20) as audit:
        path = audit._path
        audit._connection.close()
        os.replace(path, stage / "caller-retained.sqlite")
        path.write_text("foreign replacement")
    assert path.read_text() == "foreign replacement"


def test_unsafe_manifest_capture_invalidates_the_scan_even_if_name_is_repaired(tmp_path):
    # A first manifest-read failure must be terminal, just like unsafe audit files.
    root = tmp_path / "vault"
    manifest = _manifest(root, f"{1:024x}")
    (root / "Knowledge Base/log.md").write_text(_event(manifest, 1))
    path = root / manifest.path
    retained = path.with_name("retained.md")
    with legacy.LegacyAuditSpool(_stage(tmp_path), deadline=time.monotonic() + 20) as audit:
        audit.scan(root)
        path.rename(retained)
        try:
            path.symlink_to(retained)
        except OSError:
            pytest.skip("symlink creation unavailable")
        with pytest.raises(legacy.held_fs.HeldFsError):
            _verify(audit, manifest, _hash(1))
        path.unlink()
        retained.rename(path)
        with pytest.raises(CollectionStoreError, match="INVALID"):
            _verify(audit, manifest, _hash(1))


@pytest.mark.parametrize(
    "count,segments,historical_status",
    [
        (2_100, 16, "gap"),
        (10_010, 130, "history_incomplete"),
    ],
)
def test_migration_history_has_no_legacy_size_count_or_depth_ceiling(
    tmp_path,
    count,
    segments,
    historical_status,
):
    # The old depth gap and bounded history refusal remain historical facts.
    root = tmp_path / "vault"
    manifest = _manifest(root, f"{count:024x}")
    (root / "Knowledge Base/log.md").write_text("# Activity\n")
    archive = root / "Knowledge Base/_archive/logs"
    archive.mkdir(parents=True)
    width = (count + segments - 1) // segments
    for segment in range(segments):
        path = archive / f"log-{_hash(segment)[:20]}.md"
        with path.open("w") as file:
            for number in reversed(
                range(segment * width + 1, min(count, (segment + 1) * width) + 1)
            ):
                file.write(_event(manifest, number, why="invented " + "x" * 490) + "\n")
    historical = _inspection(root, manifest, _hash(count))
    assert historical["status"] == historical_status
    if count > 10_000:
        assert sum(path.stat().st_size for path in archive.iterdir()) > 8_000_000
    stage = _stage(tmp_path)
    with legacy.LegacyAuditSpool(stage, deadline=time.monotonic() + 120) as audit:
        audit.scan(root)
        captured_inspection = audit.inspect_captured(
            manifest=manifest, current_container_hash=_hash(count), markers=iter(())
        )
        assert captured_inspection == historical
        proof = audit.verify(
            manifest=manifest,
            current_container_hash=_hash(count),
            markers=iter(()),
            legacy_inspection=historical,
        )
        assert proof.exhaustive_status == "ok"
        assert proof.scan_complete
        assert proof.legacy_inspection["status"] == historical_status
        assert proof.reachable_count == count
        assert sum(1 for _event_row in audit.iter_reachable(proof)) == count
        # The whole uncapped history imports, and the proof's chain-length check (d) passes.
        captured = legacy_import.capture_legacy_collection(root, manifest.path, audit=audit)
        context = legacy_import.ImportContext("migration-test", "2026-10-02T10:00:00Z", "attempt-one")
        with connection.staging_store(tmp_path / "staging.sqlite") as staging:
            staging.execute("BEGIN IMMEDIATE")
            imported = legacy_import.import_legacy_collection(staging, captured, audit=audit, context=context)
            staging.execute("ROLLBACK")
        assert imported.legacy_event_count == count
    assert list(stage.iterdir()) == []


@pytest.mark.parametrize("bound", ["entries", "segment", "total", "events", "markers", "manifest"])
def test_captured_legacy_limits_do_not_bless_exhaustive_history(tmp_path, monkeypatch, bound):
    # Each legacy refusal remains historical even when finite import proof passes.
    root = tmp_path / "vault"
    manifest = _manifest(root, f"{2:024x}")
    (root / "Knowledge Base/log.md").write_text(_event(manifest, 2))
    archive = root / "Knowledge Base/_archive/logs"
    archive.mkdir(parents=True)
    (archive / ("log-" + "0" * 20 + ".md")).write_text(_event(manifest, 1))
    limit = {
        "entries": "_MAX_AUDIT_ARCHIVE_ENTRIES",
        "segment": "_MAX_AUDIT_SEGMENT_BYTES",
        "total": "_MAX_AUDIT_HISTORY_BYTES",
        "events": "_MAX_AUDIT_EVENTS",
        "markers": "_MAX_AUDIT_MARKERS",
        "manifest": "_MAX_AUDIT_SOURCE_BYTES",
    }[bound]
    if bound == "entries":
        (archive / "ordinary-note.md").write_text("not an activity segment")
    value = 1
    if bound == "total":
        value = (root / "Knowledge Base/log.md").stat().st_size + 1
    monkeypatch.setattr(records, limit, value)
    markers = tuple(
        records._AuditMarker(
            f"{number:024x}",
            manifest.storage.source,
            f"{number:08x}-0000-4000-8000-000000000001",
        )
        for number in (1, 2)
    )
    with legacy.LegacyAuditSpool(_stage(tmp_path), deadline=time.monotonic() + 20) as audit:
        audit.scan(root)
        historical = audit.inspect_captured(
            manifest=manifest, current_container_hash=_hash(2), markers=iter(markers)
        )
        assert historical == {"status": "history_incomplete", "gaps": []}
        proof = _verify(audit, manifest, _hash(2), historical=historical, markers=markers)
        assert proof.exhaustive_status == "ok"
        assert proof.reachable_count == 2
        assert proof.legacy_inspection == historical
