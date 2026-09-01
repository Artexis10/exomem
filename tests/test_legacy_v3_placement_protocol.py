from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from exomem import held_fs, sidecar_store
from exomem.governance import legacy_v3_placement, store

EVENT_ID = "a" * 64


def test_rollback_stage_leaf_is_a_direct_child_bound_to_the_event() -> None:
    assert legacy_v3_placement.rollback_stage_leaf(EVENT_ID) == (
        f".governance-v3-rollback-{EVENT_ID}.sqlite"
    )


@pytest.mark.parametrize("event_id", ("", ".", "../escape", "slash/name", "A" * 64, "a" * 63))
def test_rollback_stage_leaf_refuses_nonopaque_event_ids(event_id: str) -> None:
    with pytest.raises(legacy_v3_placement.LegacyV3PublicationUnavailable):
        legacy_v3_placement.rollback_stage_leaf(event_id)


def _v3_snapshot(vault: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[bytes, str]:
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(vault.parent / "machine-state"))
    path = store.sidecar_path(vault)
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    snapshot = sqlite3.connect(":memory:")
    try:
        store._migrate(connection)  # noqa: SLF001 - frozen v3 test fixture seam
        sidecar_store.ensure_meta_table(connection, store.DATA_TABLE, "placement-test")
        connection.commit()
        digest = store._v3_snapshot_digest(connection)  # noqa: SLF001 - canonical digest seam
        connection.backup(snapshot)
        snapshot.execute("VACUUM")
        return snapshot.serialize(), digest
    finally:
        snapshot.close()
        connection.close()


def _publish(
    vault: Path,
    *,
    event_id: str,
    snapshot: bytes,
    digest: str,
    barrier: Callable[[str], None] | None = None,
) -> str:
    acquired = held_fs.acquire(vault)
    with acquired.require() as filesystem:
        parent = filesystem.parent("Knowledge Base", create=True, access="mutate").require()
        with parent:
            return legacy_v3_placement.publish_exact_v3_bytes(
                filesystem,
                parent,
                event_id=event_id,
                snapshot_bytes=snapshot,
                expected_digest=digest,
                barrier=barrier,
            )


@pytest.mark.parametrize("cut", ("after_stage_write", "after_destination_link", "after_stage_unlink"))
def test_publication_resumes_each_durable_crash_cut(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cut: str,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    snapshot, digest = _v3_snapshot(vault, monkeypatch)

    def crash(point: str) -> None:
        if point == cut:
            raise RuntimeError(point)

    with pytest.raises(RuntimeError, match=cut):
        _publish(vault, event_id=EVENT_ID, snapshot=snapshot, digest=digest, barrier=crash)

    assert _publish(vault, event_id=EVENT_ID, snapshot=snapshot, digest=digest) == digest
    assert not (vault / "Knowledge Base" / legacy_v3_placement.rollback_stage_leaf(EVENT_ID)).exists()


def test_publication_refuses_an_extra_stage_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    snapshot, digest = _v3_snapshot(vault, monkeypatch)

    def crash(point: str) -> None:
        if point == "after_stage_write":
            raise RuntimeError(point)

    with pytest.raises(RuntimeError):
        _publish(vault, event_id=EVENT_ID, snapshot=snapshot, digest=digest, barrier=crash)
    stage = vault / "Knowledge Base" / legacy_v3_placement.rollback_stage_leaf(EVENT_ID)
    os.link(stage, stage.with_name("untrusted-extra-alias.sqlite"))

    with pytest.raises(legacy_v3_placement.LegacyV3PublicationUnavailable):
        _publish(vault, event_id=EVENT_ID, snapshot=snapshot, digest=digest)


def test_recovery_flushes_owned_two_link_state_before_unlinking_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    snapshot, digest = _v3_snapshot(vault, monkeypatch)

    def crash_after_original_link(point: str) -> None:
        if point == "after_destination_link":
            raise RuntimeError(point)

    with pytest.raises(RuntimeError, match="after_destination_link"):
        _publish(
            vault,
            event_id=EVENT_ID,
            snapshot=snapshot,
            digest=digest,
            barrier=crash_after_original_link,
        )

    def crash_after_recovery_flush(point: str) -> None:
        if point == "after_recovery_link_flush":
            raise RuntimeError(point)

    with pytest.raises(RuntimeError, match="after_recovery_link_flush"):
        _publish(
            vault,
            event_id=EVENT_ID,
            snapshot=snapshot,
            digest=digest,
            barrier=crash_after_recovery_flush,
        )
    parent = vault / "Knowledge Base"
    assert (parent / legacy_v3_placement.rollback_stage_leaf(EVENT_ID)).is_file()
    assert (parent / ".governance.sqlite").is_file()
    assert _publish(vault, event_id=EVENT_ID, snapshot=snapshot, digest=digest) == digest


def test_publication_refuses_a_mutated_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    snapshot, digest = _v3_snapshot(vault, monkeypatch)

    def crash(point: str) -> None:
        if point == "after_stage_write":
            raise RuntimeError(point)

    with pytest.raises(RuntimeError):
        _publish(vault, event_id=EVENT_ID, snapshot=snapshot, digest=digest, barrier=crash)
    stage = vault / "Knowledge Base" / legacy_v3_placement.rollback_stage_leaf(EVENT_ID)
    stage.write_bytes(b"not a v3 database")

    with pytest.raises(legacy_v3_placement.LegacyV3PublicationUnavailable):
        _publish(vault, event_id=EVENT_ID, snapshot=snapshot, digest=digest)


def test_publication_refuses_a_destination_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    snapshot, digest = _v3_snapshot(vault, monkeypatch)
    parent = vault / "Knowledge Base"
    parent.mkdir()
    target = parent / "different.sqlite"
    target.write_bytes(snapshot)
    destination = parent / ".governance.sqlite"
    try:
        destination.symlink_to(target.name)
    except OSError:
        pytest.skip("the test filesystem does not permit symlink creation")

    with pytest.raises(legacy_v3_placement.LegacyV3PublicationUnavailable):
        _publish(vault, event_id=EVENT_ID, snapshot=snapshot, digest=digest)


def _receipt_endpoint_pair() -> tuple[sqlite3.Connection, sqlite3.Connection, str]:
    legacy = sqlite3.connect(":memory:")
    store._migrate(legacy)  # noqa: SLF001 - frozen v3 test fixture seam
    sidecar_store.ensure_meta_table(legacy, store.DATA_TABLE, "receipt-endpoint")
    legacy.execute(
        "INSERT INTO receipt_instance(singleton, instance_id) VALUES (1, 'instance')"
    )
    legacy.execute(
        "INSERT INTO receipt_secrets(name, value) VALUES ('label_hmac', X'01')"
    )
    legacy.execute(
        "INSERT INTO receipts_head(instance_id, durable_seq, durable_hash, observed_seq, observed_hash, path, byte_offset) "
        "VALUES ('instance', 1, ?, 1, ?, 'Knowledge Base/_Governance/events/instance/2026-08.jsonl', 0)",
        ("a" * 64, "a" * 64),
    )
    legacy.commit()
    d0 = store._v3_snapshot_digest(legacy)  # noqa: SLF001 - canonical digest seam
    external = sqlite3.connect(":memory:")
    legacy.backup(external)
    external.execute(
        "UPDATE receipts_head SET durable_seq=2, durable_hash=?, observed_seq=2, observed_hash=?, "
        "path='Knowledge Base/_Governance/events/instance/2026-08.jsonl', byte_offset=123 "
        "WHERE instance_id='instance'",
        ("b" * 64, "b" * 64),
    )
    external.commit()
    return legacy, external, d0


def test_d1_normalization_and_alignment_change_only_the_active_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy, external, d0 = _receipt_endpoint_pair()
    fixture_root = Path("fixture-vault").resolve()
    monkeypatch.setattr(
        legacy_v3_placement,
        "_prove_receipt_transition",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        legacy_v3_placement,
        "_records_for_active",
        lambda *_args, **_kwargs: [
            {
                "event_id": EVENT_ID,
                "phase": "intent",
                "seq": 1,
                "hash": "a" * 64,
                "_path": str(
                    fixture_root
                    / "Knowledge Base/_Governance/events/instance/2026-08.jsonl"
                ),
                "_offset": 0,
            }
        ],
    )
    try:
        d1 = legacy_v3_placement._prove_d1_connections(  # noqa: SLF001 - protocol unit seam
            fixture_root,
            legacy,
            external,
            event_id=EVENT_ID,
            d0_digest=d0,
            expected_outcome="schema-v3-restored",
            allow_aligned_legacy=False,
        )
        assert legacy_v3_placement._align_legacy_connection(  # noqa: SLF001 - protocol unit seam
            fixture_root,
            legacy,
            external,
            event_id=EVENT_ID,
            d0_digest=d0,
            expected_outcome="schema-v3-restored",
        ) == d1
        assert legacy_v3_placement._align_legacy_connection(  # noqa: SLF001 - idempotence seam
            fixture_root,
            legacy,
            external,
            event_id=EVENT_ID,
            d0_digest=d0,
            expected_outcome="schema-v3-restored",
        ) == d1
    finally:
        external.close()
        legacy.close()


def test_d1_proof_refuses_nonactive_head_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    legacy, external, d0 = _receipt_endpoint_pair()
    fixture_root = Path("fixture-vault").resolve()
    legacy.execute(
        "INSERT INTO receipts_head(instance_id, durable_seq, durable_hash, observed_seq, observed_hash, path, byte_offset) "
        "VALUES ('other', 0, ?, 0, ?, '', 0)",
        ("0" * 64, "0" * 64),
    )
    external.execute(
        "INSERT INTO receipts_head(instance_id, durable_seq, durable_hash, observed_seq, observed_hash, path, byte_offset) "
        "VALUES ('other', 1, ?, 1, ?, 'elsewhere', 1)",
        ("c" * 64, "c" * 64),
    )
    legacy.commit()
    external.commit()
    d0 = store._v3_snapshot_digest(legacy)  # noqa: SLF001 - canonical digest seam
    monkeypatch.setattr(
        legacy_v3_placement,
        "_prove_receipt_transition",
        lambda *_args, **_kwargs: None,
    )
    try:
        with pytest.raises(legacy_v3_placement.LegacyV3PublicationUnavailable):
            legacy_v3_placement._prove_d1_connections(  # noqa: SLF001 - protocol unit seam
                fixture_root,
                legacy,
                external,
                event_id=EVENT_ID,
                d0_digest=d0,
                expected_outcome="schema-v3-restored",
                allow_aligned_legacy=False,
            )
    finally:
        external.close()
        legacy.close()


def _persisted_legacy_pair(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, str]:
    snapshot, d0 = _v3_snapshot(vault, monkeypatch)
    legacy = legacy_v3_placement.legacy_v3_path(vault)
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(snapshot)
    return legacy, d0


def test_d1_proof_refuses_legacy_path_replacement_during_sqlite_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    legacy, d0 = _persisted_legacy_pair(vault, monkeypatch)
    replacement = legacy.with_name("replacement.sqlite")
    replacement.write_bytes(legacy.read_bytes())

    def replace_while_proving(*_args: object, **_kwargs: object) -> str:
        replacement.replace(legacy)
        return "b" * 64

    monkeypatch.setattr(legacy_v3_placement, "_prove_d1_connections", replace_while_proving)

    with pytest.raises(legacy_v3_placement.LegacyV3PublicationUnavailable):
        legacy_v3_placement.prove_d1_against_legacy(
            vault,
            event_id=EVENT_ID,
            d0_digest=d0,
        )


def test_d1_alignment_refuses_legacy_hardlink_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    legacy, d0 = _persisted_legacy_pair(vault, monkeypatch)
    os.link(legacy, legacy.with_name("untrusted-legacy-alias.sqlite"))
    monkeypatch.setattr(
        legacy_v3_placement,
        "_align_legacy_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not open alias")),
    )

    with pytest.raises(legacy_v3_placement.LegacyV3PublicationUnavailable):
        legacy_v3_placement.align_legacy_to_d1(
            vault,
            event_id=EVENT_ID,
            d0_digest=d0,
        )
