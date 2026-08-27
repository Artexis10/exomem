from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from exomem import mutation_lock, writer_lease
from exomem.governance import (
    authorization_custody,
    policy,
    schema_downmigration,
    schema_v4,
    store,
)

ACTIVE_DOCUMENTS = (
    (
        "scopes/first.yaml",
        b"governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\npaths:\n  - Notes/**\n",
    ),
    (
        "scopes/second.yaml",
        b"governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAW\npaths:\n  - Sources/**\n",
    ),
)
PENDING_DOCUMENTS = tuple(
    (relative, content + b"# pending direct edit\n") for relative, content in ACTIVE_DOCUMENTS
)


@pytest.fixture(autouse=True)
def _custody_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    external = tmp_path / "external"
    external.mkdir(mode=0o700)
    lease_state = tmp_path / "lease-state"
    lease_state.mkdir(mode=0o700)
    if os.name == "nt":  # pragma: no cover - exercised by Windows CI
        sid = mutation_lock._windows_current_user_sid()
        mutation_lock._windows_apply_private_dacl(external, sid)
        mutation_lock._windows_apply_private_dacl(lease_state, sid)
    monkeypatch.setenv(
        authorization_custody.KEYRING_FILE_ENV,
        str(external / "authorization-keyring.json"),
    )
    monkeypatch.setenv(
        authorization_custody.CONTROL_FILE_ENV,
        str(external / "authorization-control.json"),
    )
    monkeypatch.setenv(
        authorization_custody.MEMBERSHIP_FILE_ENV,
        str(external / "authorization-serving-membership.json"),
    )
    monkeypatch.setenv(authorization_custody.REPLICA_ID_ENV, "standalone")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(lease_state))
    writer_lease.reset_managers_for_tests()
    yield
    writer_lease.reset_managers_for_tests()


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    governance = vault / "Knowledge Base" / "_Governance"
    for relative, content in ACTIVE_DOCUMENTS:
        target = governance / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    connection = store.open_connection(vault)
    connection.close()
    return vault


def _migrate(vault: Path, *, now: int) -> schema_v4.VerifiedActiveGovernanceState:
    staged = authorization_custody.stage_standalone_v3_custody(vault, now=now)
    snapshot = policy.observe_authoring_snapshot(vault)
    assert snapshot is not None
    compiled = policy.compile_documents(dict(snapshot.documents))
    assert not compiled.empty and not compiled.blocked
    seed = schema_v4.MigrationSeed(
        activation_store_id="activation-store-downmigration",
        logical_vault_id=staged.logical_vault_id,
        activation_epoch=1,
        policy=schema_v4.PolicyGenerationSeed(
            generation_id="01ARZ3NDEKTSV4RRFFQ69G5FAX",
            source_documents=snapshot.documents,
            source_fingerprint=snapshot.source_fingerprint,
            conflict_digest=snapshot.conflict_set_digest,
            compiled_policy=policy.canonical_compiled_bytes(compiled),
            policy_fingerprint=compiled.fingerprint,
            compiler_schema_version=1,
            projector_schema_version=1,
            predecessor_generation_id=None,
            authoring_event_id="event-downmigration-policy",
            receipt_event_id="receipt-downmigration-policy",
            created_at=now + 1,
        ),
        catalog=schema_v4.CatalogGenerationSeed(
            catalog_generation=1,
            descriptor=b'{"artifacts":[]}',
            artifact_count=0,
            created_at=now + 1,
        ),
        namespace=schema_v4.ProjectionNamespaceSeed(
            namespace_id="projection-namespace-downmigration",
            evidence=b'{"ready":true}',
            ready_at=now + 1,
        ),
        migrated_at=now + 1,
    )
    target = schema_v4.migration_target(seed)
    authorization_custody.enroll_standalone_v3_migration(
        vault,
        target=target,
        now=now,
    )
    return store.migrate_enrolled_v3_store(vault, seed=seed, now=now + 2)


def _set_pending_workspace(vault: Path) -> None:
    governance = vault / "Knowledge Base" / "_Governance"
    for relative, content in PENDING_DOCUMENTS:
        (governance / relative).write_bytes(content)


def _drain_verified_membership(monkeypatch: pytest.MonkeyPatch) -> None:
    original = authorization_custody.load_authorization_custody

    def load(vault_root: Path, *, now: int) -> authorization_custody.AuthorizationCustody:
        custody = original(vault_root, now=now)
        membership = custody.serving_membership
        assert membership is not None
        replicas = tuple(
            replace(
                item,
                state="DRAINING",
                issuance_stopped=True,
                no_in_flight=True,
            )
            for item in membership.replicas
        )
        return replace(custody, serving_membership=replace(membership, replicas=replicas))

    monkeypatch.setattr(authorization_custody, "load_authorization_custody", load)


def _schema_version(vault: Path) -> int:
    connection = sqlite3.connect(store.sidecar_path(vault))
    try:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()


def _receipt_records(vault: Path) -> list[dict[str, object]]:
    root = vault / "Knowledge Base" / "_Governance" / "events"
    return [
        json.loads(line)
        for path in sorted(root.rglob("*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def test_offline_downmigration_mirrors_active_source_and_commits_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    active = _migrate(vault, now=now)
    _set_pending_workspace(vault)
    _drain_verified_membership(monkeypatch)

    result = schema_downmigration.downmigrate_enrolled_v4_store(
        vault,
        now=now + 3,
    )

    assert result.schema_version == 3
    assert result.replayed is False
    assert result.active == active
    assert _schema_version(vault) == 3
    snapshot = policy.observe_authoring_snapshot(vault)
    assert snapshot is not None and snapshot.documents == ACTIVE_DOCUMENTS
    records = _receipt_records(vault)
    assert [(item["phase"], item.get("causation_id")) for item in records[-2:]] == [
        ("intent", None),
        ("committed", result.recovery_event_id),
    ]
    custody = authorization_custody.load_authorization_custody(vault, now=now + 3)
    assert custody.control.governance_enrolled is True
    assert custody.control.activation_state_digest == active.activation_state_digest


def test_offline_downmigration_refuses_until_every_replica_is_drained(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    _migrate(vault, now=now)
    before = store.sidecar_path(vault).read_bytes()

    with pytest.raises(schema_downmigration.DownmigrationUnavailable):
        schema_downmigration.downmigrate_enrolled_v4_store(vault, now=now + 3)

    assert _schema_version(vault) == 4
    assert store.sidecar_path(vault).read_bytes() == before
    assert not (vault / "Knowledge Base" / "_Governance" / "events").exists()


def test_offline_downmigration_replays_receipt_after_post_commit_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    _migrate(vault, now=now)
    _set_pending_workspace(vault)
    _drain_verified_membership(monkeypatch)

    def crash(point: str) -> None:
        if point == "after_store_commit":
            raise RuntimeError("injected post-commit crash")

    monkeypatch.setattr(schema_downmigration, "_downmigration_barrier", crash)
    with pytest.raises(RuntimeError, match="post-commit"):
        schema_downmigration.downmigrate_enrolled_v4_store(vault, now=now + 3)

    assert _schema_version(vault) == 3
    assert [item["phase"] for item in _receipt_records(vault)[-1:]] == ["intent"]

    monkeypatch.setattr(schema_downmigration, "_downmigration_barrier", lambda _point: None)
    replay = schema_downmigration.downmigrate_enrolled_v4_store(vault, now=now + 4)

    assert replay.replayed is True
    assert [item["phase"] for item in _receipt_records(vault)[-2:]] == [
        "intent",
        "committed",
    ]


def test_offline_downmigration_reuses_intent_after_prepared_plan_was_not_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    _migrate(vault, now=now)
    _set_pending_workspace(vault)
    _drain_verified_membership(monkeypatch)

    def crash(point: str) -> None:
        if point == "after_receipt_intent":
            raise RuntimeError("injected intent-only crash")

    monkeypatch.setattr(schema_downmigration, "_downmigration_barrier", crash)
    with pytest.raises(RuntimeError, match="intent-only"):
        schema_downmigration.downmigrate_enrolled_v4_store(vault, now=now + 3)

    first = _receipt_records(vault)
    assert [item["phase"] for item in first[-1:]] == ["intent"]

    monkeypatch.setattr(schema_downmigration, "_downmigration_barrier", lambda _point: None)
    result = schema_downmigration.downmigrate_enrolled_v4_store(vault, now=now + 4)

    assert result.replayed is False
    records = _receipt_records(vault)
    assert [item["phase"] for item in records[-2:]] == ["intent", "committed"]
    assert records[-2]["event_id"] == result.recovery_event_id


def test_offline_downmigration_resumes_a_partially_mirrored_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    _migrate(vault, now=now)
    _set_pending_workspace(vault)
    _drain_verified_membership(monkeypatch)
    writes = 0

    def crash(point: str) -> None:
        nonlocal writes
        if point.startswith("mirror:after_write:"):
            writes += 1
            if writes == 1:
                raise RuntimeError("injected partial mirror crash")

    monkeypatch.setattr(schema_downmigration, "_downmigration_barrier", crash)
    with pytest.raises(RuntimeError, match="partial mirror"):
        schema_downmigration.downmigrate_enrolled_v4_store(vault, now=now + 3)

    assert _schema_version(vault) == 4
    current = policy.observe_authoring_snapshot(vault)
    assert current is not None
    assert current.documents not in {ACTIVE_DOCUMENTS, PENDING_DOCUMENTS}

    monkeypatch.setattr(schema_downmigration, "_downmigration_barrier", lambda _point: None)
    result = schema_downmigration.downmigrate_enrolled_v4_store(vault, now=now + 4)

    assert result.replayed is False
    assert _schema_version(vault) == 3
    final = policy.observe_authoring_snapshot(vault)
    assert final is not None and final.documents == ACTIVE_DOCUMENTS


def test_v3_replay_never_backfills_a_missing_receipt_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _vault(tmp_path)
    now = int(time.time())
    _migrate(vault, now=now)
    _set_pending_workspace(vault)
    _drain_verified_membership(monkeypatch)
    custody = schema_downmigration._require_drained_custody(vault, now=now + 3)
    plan = schema_downmigration._load_or_prepare_plan(
        vault,
        custody=custody,
        now=now + 3,
    )
    schema_downmigration._stage_plan(vault, plan)
    schema_downmigration._mirror_workspace(vault, plan)
    schema_downmigration._commit_database(vault, plan, now=now + 3)
    assert _schema_version(vault) == 3
    assert not _receipt_records(vault)

    with pytest.raises(schema_downmigration.DownmigrationUnavailable):
        schema_downmigration.downmigrate_enrolled_v4_store(vault, now=now + 4)

    assert not _receipt_records(vault)
