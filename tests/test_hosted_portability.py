from __future__ import annotations

import hashlib
import json
import os
import stat
import struct
import warnings
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

from exomem import __version__
from exomem import hosted_portability as portability

CREATED_AT = "2026-07-12T12:00:00+00:00"


def _context(**overrides) -> portability.PortabilityContext:
    values = {
        "cell_id": "cell-alpha-7f3c",
        "vault_id": "vault-alpha-91d2",
        "operation_id": "operation-export-001",
        "created_at": CREATED_AT,
        "operator_authorized": True,
        "lifecycle_state": "quiesced",
        "routing_stopped": True,
        "active_mutations": 0,
        "background_writers_stopped": True,
        "reads_allowed": True,
    }
    values.update(overrides)
    return portability.PortabilityContext(**values)


def _write(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")


def _seed_vault(root: Path, sentinel: str) -> None:
    _write(root / "Knowledge Base/index.md", f"# Index\n\n{sentinel}\n")
    _write(root / "Knowledge Base/log.md", "# Activity\n\nGoverned history.\n")
    _write(
        root / "Knowledge Base/Notes/Insights/portable.md",
        f"---\ntype: insight\n---\n# Portable\n\n{sentinel}\n",
    )
    _write(root / "Knowledge Base/Evidence/Case/receipt.pdf", b"%PDF portable receipt")
    _write(root / "Knowledge Base/Evidence/Case/photo.png", b"\x89PNG portable image")
    _write(root / "Knowledge Base/_Schema/project-keys.yaml", "projects: [personal]\n")
    _write(
        root / "Knowledge Base/.review-state.json",
        json.dumps({"schema_version": 1, "records": {"review": "dismissed"}}),
    )
    _write(root / "Knowledge Base/_trash/2026-07-12/old.md", "recoverable history\n")

    # Rebuildable or sensitive state must never enter an export.
    _write(root / "Knowledge Base/.embeddings.sqlite", b"embedding sentinel secret")
    _write(root / "Knowledge Base/.embeddings.sqlite-wal", b"wal sentinel secret")
    _write(root / "Knowledge Base/.clip.sqlite", b"clip sentinel secret")
    _write(root / "Knowledge Base/.voice_profiles.json", '{"voice": "secret"}')
    _write(root / "Knowledge Base/Evidence/Case/video.mp4.frames/frame-0001.jpg", b"derived")
    _write(root / "logs/queries.jsonl", '{"query":"private question"}\n')
    _write(root / ".env", "CELL_SECRET=do-not-export\n")
    _write(root / "mutation.lock", "123\n")
    _write(root / "Knowledge Base/Notes/Insights/interrupted.tmp", "partial\n")


def _error_code(exc: pytest.ExceptionInfo[portability.PortabilityError]) -> str:
    return exc.value.code


def _raw_zip(
    path: Path,
    entries: list[tuple[str, bytes, int | None]],
) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, body, mode in entries:
                info = zipfile.ZipInfo(name)
                if mode is not None:
                    info.create_system = 3
                    info.external_attr = mode << 16
                archive.writestr(info, body)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("Knowledge Base/Notes/Insights/note.md", "canonical"),
        ("Knowledge Base/Evidence/Case/receipt.pdf", "canonical"),
        ("Knowledge Base/log.md", "canonical"),
        ("Knowledge Base/_Schema/project-keys.yaml", "canonical"),
        ("Knowledge Base/_trash/2026-07-12/note.md", "canonical"),
        ("Knowledge Base/.review-state.json", "portable-derived"),
        ("Knowledge Base/.embeddings.sqlite", "disposable-runtime"),
        ("Knowledge Base/.embeddings.sqlite-wal", "disposable-runtime"),
        ("Knowledge Base/.lexical.sqlite-shm", "disposable-runtime"),
        ("Knowledge Base/.refs.sqlite", "disposable-runtime"),
        ("Knowledge Base/.refs.sqlite-wal", "disposable-runtime"),
        ("Knowledge Base/.refs.sqlite-shm", "disposable-runtime"),
        ("Knowledge Base/.voice_profiles.json", "disposable-runtime"),
        (".exomem-hosted-cell.json", "disposable-runtime"),
        ("writer-leases.sqlite", "disposable-runtime"),
        ("writer-leases.sqlite-wal", "disposable-runtime"),
        ("writer-leases.sqlite-shm", "disposable-runtime"),
        ("idempotency-command.sqlite", "disposable-runtime"),
        ("Knowledge Base/Evidence/a.mp4.frames/frame-1.jpg", "disposable-runtime"),
        ("logs/queries.jsonl", "disposable-runtime"),
        ("logs/provider.log", "disposable-runtime"),
        (".env", "disposable-runtime"),
        ("private.pem", "disposable-runtime"),
        ("service-credential.json", "disposable-runtime"),
        ("Knowledge Base/Notes/Research/master-key-rotation.md", "canonical"),
        ("Knowledge Base/Notes/Patterns/service-credential-design.md", "canonical"),
        ("mutation.lock", "disposable-runtime"),
        ("Knowledge Base/Notes/a.md.tmp", "disposable-runtime"),
    ],
)
def test_versioned_artifact_classification_registry(path: str, expected: str) -> None:
    classification = portability.classify_artifact(path)
    assert classification.artifact_class.value == expected
    assert portability.classification_registry()["version"] == 1
    assert portability.classification_registry()["rules"]


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"operator_authorized": False}, "UNAUTHORIZED_PORTABILITY"),
        ({"lifecycle_state": "ready"}, "CELL_NOT_QUIESCED"),
        ({"routing_stopped": False}, "ROUTING_NOT_STOPPED"),
        ({"active_mutations": 1}, "QUIESCENCE_INCOMPLETE"),
        ({"background_writers_stopped": False}, "QUIESCENCE_INCOMPLETE"),
    ],
)
def test_export_requires_an_authorized_quiescence_proof(
    tmp_path: Path, overrides: dict, code: str
) -> None:
    vault = tmp_path / "vault"
    _seed_vault(vault, "alpha sentinel")

    with pytest.raises(portability.PortabilityError) as exc:
        portability.export_quiesced_vault(
            vault,
            tmp_path / "artifacts",
            context=_context(**overrides),
        )

    assert _error_code(exc) == code


def test_repeat_export_is_deterministic_complete_and_excludes_runtime_state(
    tmp_path: Path,
) -> None:
    vault_a = tmp_path / "tenant-a"
    vault_b = tmp_path / "tenant-b"
    _seed_vault(vault_a, "ALPHA-ONLY-SENTINEL")
    _seed_vault(vault_b, "BETA-MUST-NOT-LEAK")

    first = portability.export_quiesced_vault(
        vault_a,
        tmp_path / "artifacts-1",
        context=_context(),
        exomem_release="9.9.9",
    )
    second = portability.export_quiesced_vault(
        vault_a,
        tmp_path / "artifacts-2",
        context=_context(),
        exomem_release="9.9.9",
    )

    assert first.archive_path.read_bytes() == second.archive_path.read_bytes()
    assert first.archive_sha256 == second.archive_sha256
    assert first.artifact_reference.startswith("exomem-export://sha256/")
    assert str(tmp_path) not in first.artifact_reference

    manifest = first.manifest
    assert manifest["schema_version"] == 1
    assert manifest["classification_version"] == 1
    assert manifest["cell_id"] == "cell-alpha-7f3c"
    assert manifest["vault_id"] == "vault-alpha-91d2"
    assert manifest["created_at"] == CREATED_AT
    assert manifest["exomem_release"] == "9.9.9"
    assert manifest["overall_digest"]["algorithm"] == "sha256"
    assert manifest["signature"]["value"] is None
    records = manifest["files"]
    paths = [record["path"] for record in records]
    assert paths == sorted(paths)
    assert "Knowledge Base/log.md" in paths
    assert "Knowledge Base/.review-state.json" in paths
    assert "Knowledge Base/Evidence/Case/receipt.pdf" in paths
    assert not any("sqlite" in path for path in paths)
    assert not any(path.startswith("logs/") for path in paths)
    assert ".env" not in paths
    assert not any(path.endswith((".lock", ".tmp")) for path in paths)
    review = next(record for record in records if record["path"].endswith(".review-state.json"))
    assert review["classification"] == "portable-derived"

    payload = first.archive_path.read_bytes()
    assert b"ALPHA-ONLY-SENTINEL" in payload
    assert b"BETA-MUST-NOT-LEAK" not in payload
    assert b"private question" not in payload
    assert b"do-not-export" not in payload

    verified = portability.verify_export_archive(
        first.archive_path,
        expected_cell_id="cell-alpha-7f3c",
        expected_vault_id="vault-alpha-91d2",
    )
    assert verified.manifest == manifest
    assert verified.archive_sha256 == first.archive_sha256


def test_export_holds_the_injected_mutation_guard_through_verification(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _seed_vault(vault, "guarded")
    events: list[str] = []

    @contextmanager
    def guard():
        events.append("enter")
        _write(vault / "Knowledge Base/inside-guard.md", "inside\n")
        try:
            yield
        finally:
            events.append("exit")
            _write(vault / "Knowledge Base/after-guard.md", "after\n")

    exported = portability.export_quiesced_vault(
        vault,
        tmp_path / "artifacts",
        context=_context(),
        mutation_guard=guard(),
    )

    assert events == ["enter", "exit"]
    paths = {record["path"] for record in exported.manifest["files"]}
    assert exported.manifest["exomem_release"] == __version__
    assert "Knowledge Base/inside-guard.md" in paths
    assert "Knowledge Base/after-guard.md" not in paths


def test_concurrent_same_operation_exports_adopt_one_verified_artifact(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _seed_vault(vault, "CONCURRENT-EXPORT")
    artifact_root = tmp_path / "artifacts"

    def run_export(_index: int) -> portability.ExportResult:
        return portability.export_quiesced_vault(
            vault,
            artifact_root,
            context=_context(),
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        exports = list(pool.map(run_export, range(12)))

    assert len({result.archive_sha256 for result in exports}) == 1
    assert len({result.archive_path for result in exports}) == 1
    assert len(list(artifact_root.glob("*.zip"))) == 1
    assert not list(artifact_root.glob(".operation-export-001.*.zip.partial"))


def test_artifact_and_checkpoint_publication_fsync_their_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced: list[Path] = []
    monkeypatch.setattr(portability, "_fsync_directory", lambda path: synced.append(Path(path)))
    vault = tmp_path / "vault"
    _seed_vault(vault, "durable")
    artifact_root = tmp_path / "artifacts"
    exported = portability.export_quiesced_vault(vault, artifact_root, context=_context())

    store = portability.LifecycleCheckpointStore(tmp_path / "state")
    store.release_export(
        context=_context(operation_id="durable-release"),
        artifact_reference=exported.artifact_reference,
        reason_code="EXPORT_DELIVERED",
        export_root=artifact_root,
    )

    assert artifact_root in synced
    assert tmp_path / "state/release-export/cell-alpha-7f3c" in synced


def test_export_rejects_a_symlink_without_following_it(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _seed_vault(vault, "safe")
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("must not leak", encoding="utf-8")
    link = vault / "Knowledge Base/Evidence/outside.txt"
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")

    with pytest.raises(portability.PortabilityError) as exc:
        portability.export_quiesced_vault(
            vault,
            tmp_path / "artifacts",
            context=_context(),
        )

    assert _error_code(exc) == "UNSAFE_SYMLINK"
    assert not (tmp_path / "artifacts").exists() or not list((tmp_path / "artifacts").glob("*.zip"))


def test_archive_verification_rejects_tampering(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _seed_vault(vault, "ORIGINAL")
    exported = portability.export_quiesced_vault(
        vault,
        tmp_path / "artifacts",
        context=_context(),
    )
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(exported.archive_path) as source:
        entries = [(info.filename, source.read(info), None) for info in source.infolist()]
    entries = [
        (name, b"TAMPERED" if name.endswith("portable.md") else body, mode)
        for name, body, mode in entries
    ]
    _raw_zip(tampered, entries)

    with pytest.raises(portability.PortabilityError) as exc:
        portability.verify_export_archive(tampered)

    assert _error_code(exc) in {"ARCHIVE_DIGEST_MISMATCH", "ARCHIVE_SIZE_MISMATCH"}


@pytest.mark.parametrize(
    ("entries", "code"),
    [
        ([("../escape.md", b"x", None)], "UNSAFE_ARCHIVE_PATH"),
        ([("/absolute.md", b"x", None)], "UNSAFE_ARCHIVE_PATH"),
        ([("C:/drive.md", b"x", None)], "UNSAFE_ARCHIVE_PATH"),
        (
            [("Knowledge Base/link.md", b"outside", stat.S_IFLNK | 0o777)],
            "UNSAFE_ARCHIVE_ENTRY",
        ),
        (
            [("Knowledge Base/device", b"device", stat.S_IFCHR | 0o600)],
            "UNSAFE_ARCHIVE_ENTRY",
        ),
        (
            [
                ("Knowledge Base/A.md", b"a", None),
                ("knowledge base/a.md", b"b", None),
            ],
            "CASE_COLLISION",
        ),
        (
            [
                ("Knowledge Base/A/one.md", b"a", None),
                ("Knowledge Base/a/two.md", b"b", None),
            ],
            "CASE_COLLISION",
        ),
        (
            [
                ("Knowledge Base/a.md", b"a", None),
                ("Knowledge Base/a.md", b"b", None),
            ],
            "DUPLICATE_ARCHIVE_PATH",
        ),
        (
            [
                ("Knowledge Base/a", b"file", None),
                ("Knowledge Base/a/child.md", b"child", None),
            ],
            "PREFIX_PATH_COLLISION",
        ),
    ],
)
def test_archive_verification_rejects_unsafe_entry_shapes(
    tmp_path: Path,
    entries: list[tuple[str, bytes, int | None]],
    code: str,
) -> None:
    archive = tmp_path / "unsafe.zip"
    entries.insert(0, (portability.MANIFEST_NAME, b"{}", None))
    _raw_zip(archive, entries)

    with pytest.raises(portability.PortabilityError) as exc:
        portability.verify_export_archive(archive)

    assert _error_code(exc) == code


def test_archive_verification_rejects_unsupported_manifest_version(tmp_path: Path) -> None:
    archive = tmp_path / "future.zip"
    manifest = {"schema_version": 999, "files": []}
    _raw_zip(
        archive,
        [(portability.MANIFEST_NAME, json.dumps(manifest).encode(), None)],
    )

    with pytest.raises(portability.PortabilityError) as exc:
        portability.verify_export_archive(archive)

    assert _error_code(exc) == "UNSUPPORTED_MANIFEST_VERSION"


def test_archive_verification_rejects_unix_hardlink_extension_metadata(tmp_path: Path) -> None:
    archive_path = tmp_path / "hardlink-extension.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(portability.MANIFEST_NAME, b"{}")
        hardlink = zipfile.ZipInfo("Knowledge Base/link.md")
        hardlink.create_system = 3
        hardlink.external_attr = (stat.S_IFREG | 0o600) << 16
        hardlink.extra = struct.pack("<HH", 0x756E, 0)
        archive.writestr(hardlink, b"linked")

    with pytest.raises(portability.PortabilityError) as exc:
        portability.verify_export_archive(archive_path)

    assert _error_code(exc) == "UNSAFE_ARCHIVE_ENTRY"


def test_archive_verification_enforces_configured_resource_limits(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _seed_vault(source, "bounded")
    exported = portability.export_quiesced_vault(
        source,
        tmp_path / "artifacts",
        context=_context(),
    )

    with pytest.raises(portability.PortabilityError) as exc:
        portability.verify_export_archive(
            exported.archive_path,
            limits=portability.PortabilityLimits(max_files=1),
        )

    assert _error_code(exc) == "RESOURCE_LIMIT_EXCEEDED"


def test_restore_round_trip_publishes_before_rebuild_and_preserves_canonical_bytes(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "source"
    _seed_vault(vault, "ROUND-TRIP-SENTINEL")
    exported = portability.export_quiesced_vault(
        vault,
        tmp_path / "artifacts",
        context=_context(),
    )
    restore_context = _context(
        operation_id="operation-restore-001",
        lifecycle_state="restore-staging",
    )
    prepared = portability.prepare_restore(
        exported.archive_path,
        tmp_path / "restore-staging",
        context=restore_context,
    )
    expected = {record["path"]: record["sha256"] for record in exported.manifest["files"]}
    assert not (prepared.staging_root / "Knowledge Base/.embeddings.sqlite").exists()

    events: list[str] = []

    def publish(staging: Path, live: Path) -> None:
        events.append("publish")
        os.replace(staging, live)

    def rebuild(live: Path) -> None:
        events.append("rebuild")
        _write(live / "Knowledge Base/.embeddings.sqlite", b"rebuilt derived state")

    result = portability.publish_prepared_restore(
        prepared,
        tmp_path / "live-vault",
        publish=publish,
        rebuild_derived=rebuild,
    )

    assert events == ["publish", "rebuild"]
    assert result.state == "published"
    assert result.lexical_ready is True
    assert result.derived_state == "ready"
    assert (result.live_root / "Knowledge Base/.embeddings.sqlite").exists()
    for relative, digest in expected.items():
        assert hashlib.sha256((result.live_root / relative).read_bytes()).hexdigest() == digest


def test_restore_can_replace_a_cell_without_rebinding_the_source_archive(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _seed_vault(source, "PORTABLE-TO-A-NEW-CELL")
    exported = portability.export_quiesced_vault(
        source,
        tmp_path / "artifacts",
        context=_context(),
    )
    target_context = _context(
        cell_id="cell-replacement-2c8e",
        operation_id="operation-restore-replacement",
        lifecycle_state="restore-staging",
    )

    prepared = portability.prepare_restore(
        exported.archive_path,
        tmp_path / "replacement-staging",
        context=target_context,
        expected_source_cell_id="cell-alpha-7f3c",
    )

    assert prepared.context.cell_id == "cell-replacement-2c8e"
    assert prepared.manifest["cell_id"] == "cell-alpha-7f3c"
    assert (
        b"PORTABLE-TO-A-NEW-CELL"
        in (prepared.staging_root / "Knowledge Base/Notes/Insights/portable.md").read_bytes()
    )

    with pytest.raises(portability.PortabilityError) as exc:
        portability.prepare_restore(
            exported.archive_path,
            tmp_path / "wrong-vault-staging",
            context=_context(
                cell_id="cell-replacement-2c8e",
                vault_id="vault-other-tenant",
                operation_id="operation-restore-wrong-vault",
                lifecycle_state="restore-staging",
            ),
        )
    assert _error_code(exc) == "VAULT_BINDING_MISMATCH"


def test_restore_failure_never_partially_mutates_the_live_vault(tmp_path: Path) -> None:
    vault = tmp_path / "source"
    _seed_vault(vault, "SAFE-SOURCE")
    exported = portability.export_quiesced_vault(
        vault,
        tmp_path / "artifacts",
        context=_context(),
    )
    prepared = portability.prepare_restore(
        exported.archive_path,
        tmp_path / "restore-staging",
        context=_context(
            operation_id="operation-restore-002",
            lifecycle_state="restore-staging",
        ),
    )
    live = tmp_path / "live-vault"

    def fail_partway(staging: Path, destination: Path) -> None:
        destination.mkdir()
        _write(destination / "partial.md", "partial")
        raise OSError("simulated publication failure")

    with pytest.raises(portability.PortabilityError) as exc:
        portability.publish_prepared_restore(prepared, live, publish=fail_partway)

    assert _error_code(exc) == "PUBLICATION_FAILED"
    assert not live.exists()
    assert prepared.staging_root.exists()


def test_restore_refuses_to_overlay_an_existing_live_vault(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _seed_vault(source, "SOURCE")
    exported = portability.export_quiesced_vault(
        source,
        tmp_path / "artifacts",
        context=_context(),
    )
    prepared = portability.prepare_restore(
        exported.archive_path,
        tmp_path / "restore-staging",
        context=_context(
            operation_id="operation-restore-003",
            lifecycle_state="restore-staging",
        ),
    )
    live = tmp_path / "live"
    _write(live / "existing.md", "EXISTING-MUST-SURVIVE")

    with pytest.raises(portability.PortabilityError) as exc:
        portability.publish_prepared_restore(prepared, live)

    assert _error_code(exc) == "LIVE_VAULT_EXISTS"
    assert (live / "existing.md").read_text(encoding="utf-8") == "EXISTING-MUST-SURVIVE"


def test_rebuild_failure_keeps_lexical_restore_available(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _seed_vault(source, "LEXICAL-STILL-WORKS")
    exported = portability.export_quiesced_vault(
        source,
        tmp_path / "artifacts",
        context=_context(),
    )
    prepared = portability.prepare_restore(
        exported.archive_path,
        tmp_path / "staging",
        context=_context(
            operation_id="operation-restore-004",
            lifecycle_state="restore-staging",
        ),
    )

    def broken_rebuild(_live: Path) -> None:
        raise RuntimeError("embedding backend unavailable")

    result = portability.publish_prepared_restore(
        prepared,
        tmp_path / "live",
        rebuild_derived=broken_rebuild,
    )

    assert result.lexical_ready is True
    assert result.derived_state == "degraded"
    assert result.derived_error_code == "DERIVED_REBUILD_FAILED"
    assert (
        b"LEXICAL-STILL-WORKS"
        in (result.live_root / "Knowledge Base/Notes/Insights/portable.md").read_bytes()
    )


def test_rebuild_canonical_mutation_is_repaired_and_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _seed_vault(source, "CANONICAL-MUST-SURVIVE")
    exported = portability.export_quiesced_vault(
        source,
        tmp_path / "artifacts",
        context=_context(),
    )
    prepared = portability.prepare_restore(
        exported.archive_path,
        tmp_path / "staging",
        context=_context(
            operation_id="operation-restore-integrity",
            lifecycle_state="restore-staging",
        ),
    )
    relative = "Knowledge Base/Notes/Insights/portable.md"
    expected = next(
        record["sha256"] for record in exported.manifest["files"] if record["path"] == relative
    )
    live = tmp_path / "live"

    def corrupt_canonical(root: Path) -> None:
        _write(root / relative, "corrupted by rebuild\n")

    with pytest.raises(portability.PortabilityError) as exc:
        portability.publish_prepared_restore(
            prepared,
            live,
            rebuild_derived=corrupt_canonical,
        )

    assert _error_code(exc) == "CANONICAL_INTEGRITY_VIOLATION"
    assert hashlib.sha256((live / relative).read_bytes()).hexdigest() == expected


def test_export_release_checkpoint_is_private_content_minimal_and_idempotent(
    tmp_path: Path,
) -> None:
    store = portability.LifecycleCheckpointStore(tmp_path / "state")
    context = _context(operation_id="release-op-1")
    export_root = tmp_path / "exports"
    export_root.mkdir()
    artifact = export_root / f"exomem-export-{'a' * 64}.zip"
    artifact.write_bytes(b"verified export")

    first = store.release_export(
        context=context,
        artifact_reference=f"exomem-export://sha256/{'a' * 64}",
        reason_code="EXPORT_DELIVERED",
        export_root=export_root,
    )
    assert not artifact.exists()
    replay = store.release_export(
        context=context,
        artifact_reference=f"exomem-export://sha256/{'a' * 64}",
        reason_code="EXPORT_DELIVERED",
        export_root=export_root,
    )

    assert first.state == "export-released"
    assert replay.checkpoint_digest == first.checkpoint_digest
    assert replay.replayed is True
    audit_text = json.dumps(replay.audit_record(), sort_keys=True)
    assert "private question" not in audit_text
    assert str(tmp_path) not in audit_text
    assert "backup" not in audit_text.lower()
    assert "kms" not in audit_text.lower()

    with pytest.raises(portability.PortabilityError) as exc:
        store.release_export(
            context=context,
            artifact_reference=f"exomem-export://sha256/{'b' * 64}",
            reason_code="EXPORT_DELIVERED",
            export_root=export_root,
        )
    assert _error_code(exc) == "CHECKPOINT_CONFLICT"


def test_deletion_seal_requires_stopped_routing_and_disclaims_external_deletion(
    tmp_path: Path,
) -> None:
    store = portability.LifecycleCheckpointStore(tmp_path / "state")
    with pytest.raises(portability.PortabilityError) as exc:
        store.seal_for_deletion(
            context=_context(operation_id="delete-op-1", routing_stopped=False),
            reason_code="ACCOUNT_DELETION_REQUESTED",
        )
    assert _error_code(exc) == "ROUTING_NOT_STOPPED"

    context = _context(operation_id="delete-op-1")
    first = store.seal_for_deletion(
        context=context,
        reason_code="ACCOUNT_DELETION_REQUESTED",
    )
    replay = store.seal_for_deletion(
        context=context,
        reason_code="ACCOUNT_DELETION_REQUESTED",
    )
    assert first.state == "deletion-sealed"
    assert first.external_deletion_performed is False
    assert first.external_deletion_owner == "control-plane"
    assert replay.checkpoint_digest == first.checkpoint_digest
    assert replay.replayed is True


def test_export_release_replay_finishes_cleanup_after_unlink_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = portability.LifecycleCheckpointStore(tmp_path / "state")
    context = _context(operation_id="release-cleanup-retry")
    export_root = tmp_path / "exports"
    export_root.mkdir()
    artifact = export_root / f"exomem-export-{'e' * 64}.zip"
    artifact.write_bytes(b"verified export")
    original_unlink = Path.unlink
    failed = False

    def fail_artifact_once(path: Path, *args, **kwargs):
        nonlocal failed
        if path == artifact and not failed:
            failed = True
            raise OSError("simulated cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_artifact_once)
    with pytest.raises(portability.PortabilityError) as exc:
        store.release_export(
            context=context,
            artifact_reference=f"exomem-export://sha256/{'e' * 64}",
            reason_code="EXPORT_DELIVERED",
            export_root=export_root,
        )
    assert _error_code(exc) == "EXPORT_RELEASE_CLEANUP_FAILED"
    assert artifact.exists()

    replay = store.release_export(
        context=context,
        artifact_reference=f"exomem-export://sha256/{'e' * 64}",
        reason_code="EXPORT_DELIVERED",
        export_root=export_root,
    )
    assert replay.replayed is True
    assert not artifact.exists()


def test_portability_hooks_reject_unauthorized_operator_context(tmp_path: Path) -> None:
    store = portability.LifecycleCheckpointStore(tmp_path / "state")
    unauthorized = _context(operator_authorized=False)
    with pytest.raises(portability.PortabilityError) as release_exc:
        store.release_export(
            context=unauthorized,
            artifact_reference=f"exomem-export://sha256/{'a' * 64}",
            reason_code="EXPORT_DELIVERED",
            export_root=tmp_path,
        )
    with pytest.raises(portability.PortabilityError) as delete_exc:
        store.seal_for_deletion(
            context=unauthorized,
            reason_code="ACCOUNT_DELETION_REQUESTED",
        )
    assert _error_code(release_exc) == "UNAUTHORIZED_PORTABILITY"
    assert _error_code(delete_exc) == "UNAUTHORIZED_PORTABILITY"

    source = tmp_path / "source"
    _seed_vault(source, "private")
    exported = portability.export_quiesced_vault(
        source,
        tmp_path / "artifacts",
        context=_context(),
    )
    with pytest.raises(portability.PortabilityError) as restore_exc:
        portability.prepare_restore(
            exported.archive_path,
            tmp_path / "unauthorized-staging",
            context=_context(
                operator_authorized=False,
                lifecycle_state="restore-staging",
                operation_id="unauthorized-restore",
            ),
        )
    assert _error_code(restore_exc) == "UNAUTHORIZED_PORTABILITY"
    assert not (tmp_path / "unauthorized-staging").exists()


def test_interrupted_export_and_checkpoint_files_are_retryable(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _seed_vault(vault, "RETRY-SENTINEL")
    artifact_root = tmp_path / "artifacts"
    first = portability.export_quiesced_vault(vault, artifact_root, context=_context())
    unrelated_partial = artifact_root / "another-operation.zip.partial"
    unrelated_partial.write_bytes(b"another writer owns this")

    second = portability.export_quiesced_vault(vault, artifact_root, context=_context())
    assert second.archive_sha256 == first.archive_sha256
    assert unrelated_partial.read_bytes() == b"another writer owns this"

    store = portability.LifecycleCheckpointStore(tmp_path / "state")
    stale_checkpoint = store.pending_path("release-export", "cell-alpha-7f3c", "retry-op")
    stale_checkpoint.parent.mkdir(parents=True, exist_ok=True)
    stale_checkpoint.write_text("interrupted checkpoint", encoding="utf-8")
    checkpoint = store.release_export(
        context=_context(operation_id="retry-op"),
        artifact_reference=first.artifact_reference,
        reason_code="EXPORT_DELIVERED",
        export_root=artifact_root,
    )
    assert checkpoint.state == "export-released"
    assert stale_checkpoint.read_text(encoding="utf-8") == "interrupted checkpoint"


def test_concurrent_checkpoint_commit_adopts_one_atomic_result(tmp_path: Path) -> None:
    store = portability.LifecycleCheckpointStore(tmp_path / "state")
    context = _context(operation_id="concurrent-release-op")
    artifact_reference = f"exomem-export://sha256/{'c' * 64}"
    export_root = tmp_path / "exports"
    export_root.mkdir()
    artifact = export_root / f"exomem-export-{'c' * 64}.zip"
    artifact.write_bytes(b"verified export")
    unrelated = export_root / f"exomem-export-{'d' * 64}.zip"
    unrelated.write_bytes(b"another export")

    def release(_index: int) -> portability.LifecycleCheckpoint:
        return store.release_export(
            context=context,
            artifact_reference=artifact_reference,
            reason_code="EXPORT_DELIVERED",
            export_root=export_root,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        checkpoints = list(pool.map(release, range(24)))

    assert len({checkpoint.checkpoint_digest for checkpoint in checkpoints}) == 1
    assert sum(not checkpoint.replayed for checkpoint in checkpoints) == 1
    assert not artifact.exists()
    assert unrelated.read_bytes() == b"another export"
    assert not list((tmp_path / "state").rglob(".*.json.partial"))
