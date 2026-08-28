from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.request
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
import uvicorn

from exomem import init as init_module
from exomem import state_migration, state_paths, writer_lease
from exomem.governance import (
    authorization_custody,
    legacy_v3_placement,
    policy,
    schema_downmigration,
    schema_migration,
    schema_v4,
    store,
)
from exomem.lease_coordinator import create_app

OLD_V3_PYTHON_ENV = "EXOMEM_OLD_V3_PYTHON"
OLD_V3_VERSION = "0.57.0"
POLICY_DOCUMENTS = (
    (
        "scopes/old-binary-probe.yaml",
        b"governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\npaths:\n  - Notes/**\n",
    ),
)


def _old_python() -> Path:
    raw = os.environ.get(OLD_V3_PYTHON_ENV, "").strip()
    if not raw:
        pytest.skip(f"{OLD_V3_PYTHON_ENV} does not name the pinned old binary")
    executable = Path(raw)
    if not executable.is_file():
        pytest.fail(f"{OLD_V3_PYTHON_ENV} does not name a file")
    observed = subprocess.run(
        [
            str(executable),
            "-c",
            "import importlib.metadata; print(importlib.metadata.version('exomem'))",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    ).stdout.strip()
    assert observed == OLD_V3_VERSION
    return executable


def _configure_custody(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    external = tmp_path / "external-custody"
    external.mkdir(mode=0o700)
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
    monkeypatch.setenv(authorization_custody.REPLICA_ID_ENV, "old-binary-probe")


def _configure_schema_fence(
    monkeypatch: pytest.MonkeyPatch,
    coordinator_url: str,
) -> None:
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_URL", coordinator_url)
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_VAULT_ID", "old-binary-probe")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_REPLICA_ID", "old-binary-probe")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_TOKEN", "lease-secret")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_PREFERRED", "1")
    monkeypatch.setenv("EXOMEM_LEASE_COORDINATOR_OPERATOR_TOKEN", "operator-secret")
    writer_lease.reset_managers_for_tests()


def _drain_verified_membership(monkeypatch: pytest.MonkeyPatch) -> None:
    original = authorization_custody.load_authorization_custody

    def load(vault_root: Path, *, now: int) -> authorization_custody.AuthorizationCustody:
        custody = original(vault_root, now=now)
        membership = custody.serving_membership
        assert membership is not None
        replicas = tuple(
            replace(
                replica,
                state="DRAINING",
                issuance_stopped=True,
                no_in_flight=True,
            )
            for replica in membership.replicas
        )
        return replace(custody, serving_membership=replace(membership, replicas=replicas))

    monkeypatch.setattr(authorization_custody, "load_authorization_custody", load)


def _v3_vault(
    tmp_path: Path,
    *,
    now: int,
) -> Path:
    vault = tmp_path / "vault"
    init_module.init_vault(vault)
    legacy_schema = vault / "Knowledge Base" / "_Schema"
    legacy_schema.mkdir(parents=True, exist_ok=True)
    legacy_schema.joinpath("SKILL.md").write_bytes(
        (Path(init_module.__file__).parent / "_scaffold" / "_Schema" / "SKILL.md").read_bytes()
    )
    governance = vault / "Knowledge Base" / "_Governance"
    for relative, content in POLICY_DOCUMENTS:
        target = governance / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    note = vault / "Knowledge Base" / "Notes" / "Insights" / "visible.md"
    note.write_text(
        "---\ntype: insight\nstatus: active\n---\n\n# Visible\n\nlantern baseline\n",
        encoding="utf-8",
    )
    state_migration.migrate_vault_state_offline(
        vault,
        authority=state_migration.assert_offline_migration_authority(
            source="old v3 binary fence fixture",
        ),
    )

    connection = sqlite3.connect(store.sidecar_path(vault))
    try:
        store._migrate(connection)
        connection.commit()
    finally:
        connection.close()
    return vault


def _v4_vault(
    tmp_path: Path,
    *,
    now: int,
) -> tuple[Path, schema_v4.VerifiedActiveGovernanceState]:
    vault = _v3_vault(tmp_path, now=now)
    staged = authorization_custody.stage_standalone_v3_custody(vault, now=now)
    snapshot = policy.observe_authoring_snapshot(vault)
    assert snapshot is not None
    compiled = policy.compile_documents(dict(snapshot.documents))
    assert not compiled.empty and not compiled.blocked
    seed = schema_v4.MigrationSeed(
        activation_store_id="activation-store-old-binary-probe",
        logical_vault_id=staged.logical_vault_id,
        activation_epoch=1,
        policy=schema_v4.PolicyGenerationSeed(
            generation_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            source_documents=snapshot.documents,
            source_fingerprint=snapshot.source_fingerprint,
            conflict_digest=snapshot.conflict_set_digest,
            compiled_policy=policy.canonical_compiled_bytes(compiled),
            policy_fingerprint=compiled.fingerprint,
            compiler_schema_version=1,
            projector_schema_version=1,
            predecessor_generation_id=None,
            authoring_event_id="event-old-binary-probe",
            receipt_event_id="receipt-old-binary-probe",
            created_at=now + 1,
        ),
        catalog=schema_v4.CatalogGenerationSeed(
            catalog_generation=1,
            descriptor=b'{"artifacts":[]}',
            artifact_count=0,
            created_at=now + 1,
        ),
        namespace=schema_v4.ProjectionNamespaceSeed(
            namespace_id="projection-namespace-old-binary-probe",
            evidence=b'{"ready":true}',
            ready_at=now + 1,
        ),
        migrated_at=now + 1,
    )
    source = store.open_readonly_connection(vault)
    assert source is not None
    try:
        source_digest = store._v3_snapshot_digest(source)  # noqa: SLF001
    finally:
        source.close()
    target = schema_v4.migration_target(seed)
    authorization_custody.enroll_standalone_v3_migration(vault, target=target, now=now)
    return vault, store.migrate_enrolled_v3_store(
        vault,
        seed=seed,
        expected_source_store_digest=source_digest,
        now=now + 2,
    )


def _backup_restore_vault(
    tmp_path: Path,
    *,
    now: int,
) -> tuple[
    Path,
    schema_migration.ForwardMigrationPlan,
    schema_migration.ForwardMigrationResult,
]:
    vault = _v3_vault(tmp_path, now=now)
    plan = schema_migration.prepare_forward_migration(vault, now=now)
    schema_migration.stage_forward_migration(
        vault,
        expected_plan_digest=plan.plan_digest,
        now=now + 1,
    )
    return vault, plan, schema_migration.commit_forward_migration(
        vault,
        expected_plan_digest=plan.plan_digest,
        now=now + 2,
    )


def _database_digest(path: Path) -> str:
    with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as connection:
        payload = "\n".join(connection.iterdump()).encode("utf-8")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    return hashlib.sha256(str(version).encode("ascii") + b"\0" + payload).hexdigest()


def _snapshot_external_store_at_legacy_path(source_vault: Path, probe: Path) -> Path:
    """Make the old binary probe the source store, never an absent new database."""
    destination = legacy_v3_placement.legacy_v3_path(probe)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(
        f"{store.sidecar_path(source_vault).as_uri()}?mode=ro", uri=True
    ) as source:
        with sqlite3.connect(destination) as snapshot:
            source.backup(snapshot)
    return destination


@contextmanager
def _lease_server(database: Path):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = int(listener.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(
                database=database,
                bearer_token="lease-secret",
                operator_token="operator-secret",
            ),
            log_level="error",
            lifespan="off",
        )
    )
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started and thread.is_alive()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive()


def _old_environment(
    tmp_path: Path,
    vault: Path,
    coordinator_url: str | None,
    *,
    state_name: str,
) -> dict[str, str]:
    state = tmp_path / state_name
    state.mkdir(mode=0o700, exist_ok=True)
    env = {
        **os.environ,
        "EXOMEM_VAULT_PATH": str(vault),
        "EXOMEM_WRITER_LEASE_STATE_DIR": str(state),
        "XDG_STATE_HOME": str(state),
        "EXOMEM_DISABLE_EMBEDDINGS": "1",
        "EXOMEM_DISABLE_MEDIA_EXTRACTION": "1",
        "EXOMEM_DISABLE_CLIP": "1",
        "EXOMEM_DISABLE_RANKING": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for name in (
        "EXOMEM_WRITER_LEASE_URL",
        "EXOMEM_WRITER_LEASE_VAULT_ID",
        "EXOMEM_WRITER_LEASE_REPLICA_ID",
        "EXOMEM_WRITER_LEASE_TOKEN",
        "EXOMEM_WRITER_LEASE_PREFERRED",
    ):
        env.pop(name, None)
    if coordinator_url is not None:
        env.update(
            EXOMEM_WRITER_LEASE_URL=coordinator_url,
            EXOMEM_WRITER_LEASE_VAULT_ID="old-binary-probe",
            EXOMEM_WRITER_LEASE_REPLICA_ID="old-v3",
            EXOMEM_WRITER_LEASE_TOKEN="lease-secret",
            EXOMEM_WRITER_LEASE_PREFERRED="1",
        )
    return env


def _old_cli(
    old_python: Path,
    env: dict[str, str],
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(old_python), "-m", "exomem", *arguments],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _old_receipt_write(
    old_python: Path,
    env: dict[str, str],
    vault: Path,
) -> subprocess.CompletedProcess[str]:
    """Exercise the released v0.57 receipt writer, not a local compatibility shim."""

    return subprocess.run(
        [
            str(old_python),
            "-c",
            (
                "from pathlib import Path\n"
                "from exomem.governance import receipts\n"
                "receipts.append_event(Path(__import__('sys').argv[1]), "
                "event_type='disclosure', phase='recorded', "
                "payload={'outcomes': [{'ref': 'old-v3-rollback-acceptance'}]})\n"
            ),
            str(vault),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _old_server_starts(old_python: Path, env: dict[str, str]) -> bool:
    process = subprocess.Popen(
        [str(old_python), "-m", "exomem", "--transport", "stdio"],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(0.75)
        return process.poll() is None
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def _deployment_admission(coordinator_url: str, schema_version: int) -> dict[str, object]:
    request = urllib.request.Request(
        f"{coordinator_url}/v1/vaults/old-binary-probe/schema-fence/admit",
        data=json.dumps(
            {"replica_id": "old-v3", "schema_version": schema_version},
            separators=(",", ":"),
        ).encode("utf-8"),
        headers={
            "Authorization": "Bearer operator-secret",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        value = json.loads(response.read())
    assert isinstance(value, dict)
    return value


def test_actual_old_v3_binary_is_write_fenced_from_v4_and_reopens_only_after_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_python = _old_python()
    now = 1_800_000_000
    _configure_custody(tmp_path, monkeypatch)
    coordinator_database = tmp_path / "coordinator.sqlite"
    with _lease_server(coordinator_database) as coordinator_url:
        _configure_schema_fence(monkeypatch, coordinator_url)
        vault, active = _v4_vault(tmp_path, now=now)
        before_database = _database_digest(store.sidecar_path(vault))
        visible = vault / "Knowledge Base" / "Notes" / "Insights" / "visible.md"
        before_visible = visible.read_bytes()

        # Record the actual released behavior on a disposable copy. v0.57 both
        # starts and reads a v4 store, and that nominal read performs governance
        # DML. The live-copy assertion below therefore cannot rely on self-refusal
        # or on a writer-only fence.
        probe = tmp_path / "isolated-old-binary-probe"
        shutil.copytree(vault, probe)
        probe_database = _snapshot_external_store_at_legacy_path(vault, probe)
        probe_before = _database_digest(probe_database)
        probe_env = _old_environment(
            tmp_path,
            probe,
            None,
            state_name="isolated-old-state",
        )
        assert _old_server_starts(old_python, probe_env) is True
        read = _old_cli(old_python, probe_env, "find", "lantern", "--json")
        assert read.returncode == 0, read.stderr
        assert _database_digest(probe_database) != probe_before
        before_probe_write = _database_digest(probe_database)
        write = _old_cli(
            old_python,
            probe_env,
            "capture_source",
            "--content",
            "old writer can mutate the isolated v4 probe",
            "--title",
            "Unsafe Old Writer Probe",
            "--source-type",
            "article",
            "--url",
            "https://example.invalid/unsafe-old-writer-probe",
            "--json",
        )
        assert write.returncode == 0, write.stderr
        assert _database_digest(probe_database) == before_probe_write
        assert list((probe / "Knowledge Base" / "Sources").rglob("*unsafe-old-writer-probe.md"))
        with sqlite3.connect(probe_database) as connection:
            assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 4

        lease_probe = tmp_path / "isolated-old-binary-lease-probe"
        shutil.copytree(vault, lease_probe)
        lease_probe_database = _snapshot_external_store_at_legacy_path(vault, lease_probe)
        lease_probe_before = _database_digest(lease_probe_database)
        lease_probe_env = _old_environment(
            tmp_path,
            lease_probe,
            coordinator_url,
            state_name="isolated-old-lease-state",
        )
        rejected_write = _old_cli(
            old_python,
            lease_probe_env,
            "capture_source",
            "--content",
            "the lease fence must reject this old writer",
            "--title",
            "Rejected Old Writer Probe",
            "--source-type",
            "article",
            "--url",
            "https://example.invalid/rejected-old-writer-probe",
            "--json",
        )
        assert rejected_write.returncode != 0
        assert _database_digest(lease_probe_database) == lease_probe_before
        assert not list(
            (lease_probe / "Knowledge Base" / "Sources").rglob("*rejected-old-writer-probe.md")
        )

        env = _old_environment(
            tmp_path,
            vault,
            coordinator_url,
            state_name="live-old-state",
        )
        admission = _deployment_admission(coordinator_url, 3)
        assert admission == {
            "admitted": False,
            "governance_enrolled": True,
            "required_schema_version": 4,
            "schema_fence_generation": 1,
        }
        # Deployment stops here: the old process is never started against the
        # live v4 root, so even its read-path DML cannot occur.
        assert _database_digest(store.sidecar_path(vault)) == before_database
        assert visible.read_bytes() == before_visible
        assert not list((vault / "Knowledge Base" / "Notes").rglob("old-writer-must-not-land.md"))

        _drain_verified_membership(monkeypatch)
        result = schema_downmigration.downmigrate_enrolled_v4_store(
            vault,
            now=now + 3,
        )
        assert result.schema_version == 3
        assert result.active == active
        assert result.replayed is False
        legacy = legacy_v3_placement.legacy_v3_path(vault)
        external_digest = legacy_v3_placement.exact_external_v3_digest(vault)
        with sqlite3.connect(legacy) as connection:
            assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 3
            assert store._v3_snapshot_digest(connection) == external_digest  # noqa: SLF001
        manifest = json.loads(
            (state_paths.vault_state_dir(vault) / state_migration.MANIFEST_NAME).read_text(
                encoding="utf-8"
            )
        )
        marker = manifest["governance_rollback"]
        assert isinstance(marker, dict)
        assert marker["phase"] == "complete"
        assert marker["d1"] == external_digest
        assert marker["schema_fence_generation"] == 1
        assert _deployment_admission(coordinator_url, 3) == {
            "admitted": True,
            "governance_enrolled": True,
            "required_schema_version": 3,
            "schema_fence_generation": 2,
        }

        allowed = _old_cli(
            old_python,
            env,
            "capture_source",
            "--content",
            "old writer is allowed only after proven rollback",
            "--title",
            "Old Writer After Rollback",
            "--source-type",
            "article",
            "--url",
            "https://example.invalid/old-writer-after-rollback",
            "--json",
        )

    assert allowed.returncode == 0, allowed.stderr
    assert list((vault / "Knowledge Base" / "Sources").rglob("*old-writer-after-rollback.md"))
    legacy = legacy_v3_placement.legacy_v3_path(vault)
    before_receipt_write = _database_digest(legacy)
    with sqlite3.connect(legacy) as connection:
        before_head = connection.execute(
            "SELECT observed_seq, observed_hash FROM receipts_head "
            "WHERE instance_id=(SELECT instance_id FROM receipt_instance WHERE singleton=1)"
        ).fetchone()
    assert before_head is not None
    receipt_write = _old_receipt_write(old_python, env, vault)
    assert receipt_write.returncode == 0, receipt_write.stderr
    assert _database_digest(legacy) != before_receipt_write
    with sqlite3.connect(legacy) as connection:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 3
        head = connection.execute(
            "SELECT observed_seq, observed_hash FROM receipts_head "
            "WHERE instance_id=(SELECT instance_id FROM receipt_instance WHERE singleton=1)"
        ).fetchone()
        assert head is not None
        assert int(head[0]) > int(before_head[0])
        assert isinstance(head[1], str) and len(head[1]) == 64


def test_actual_old_v3_binary_writes_receipts_after_production_backup_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_python = _old_python()
    now = 1_800_000_100
    _configure_custody(tmp_path, monkeypatch)
    coordinator_database = tmp_path / "backup-restore-coordinator.sqlite"
    with _lease_server(coordinator_database) as coordinator_url:
        _configure_schema_fence(monkeypatch, coordinator_url)
        vault, plan, committed = _backup_restore_vault(tmp_path, now=now)
        _drain_verified_membership(monkeypatch)
        restored = schema_migration.restore_forward_migration_backup(
            vault,
            expected_plan_digest=plan.plan_digest,
            expected_backup_reference=committed.backup_reference,
            now=now + 3,
        )

        legacy = legacy_v3_placement.legacy_v3_path(vault)
        external_d1 = legacy_v3_placement.exact_external_v3_digest(vault)
        manifest = json.loads(
            (state_paths.vault_state_dir(vault) / state_migration.MANIFEST_NAME).read_text(
                encoding="utf-8"
            )
        )
        marker = manifest["governance_rollback"]
        assert restored.schema_version == 3
        assert isinstance(marker, dict)
        assert marker["operation"] == "governance_schema_v3_backup_restore"
        assert marker["phase"] == "complete"
        assert marker["d1"] == external_d1
        assert marker["schema_fence_generation"] == 1
        with sqlite3.connect(legacy) as connection:
            assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 3
            assert store._v3_snapshot_digest(connection) == external_d1  # noqa: SLF001
        assert _deployment_admission(coordinator_url, 3) == {
            "admitted": True,
            "governance_enrolled": True,
            "required_schema_version": 3,
            "schema_fence_generation": 2,
        }

        env = _old_environment(
            tmp_path,
            vault,
            coordinator_url,
            state_name="backup-restore-old-v3-state",
        )
        before_digest = _database_digest(legacy)
        with sqlite3.connect(legacy) as connection:
            before_head = connection.execute(
                "SELECT observed_seq, observed_hash FROM receipts_head "
                "WHERE instance_id=(SELECT instance_id FROM receipt_instance WHERE singleton=1)"
            ).fetchone()
        assert before_head is not None

        receipt_write = _old_receipt_write(old_python, env, vault)

        assert receipt_write.returncode == 0, receipt_write.stderr
        assert _database_digest(legacy) != before_digest
        with sqlite3.connect(legacy) as connection:
            assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 3
            head = connection.execute(
                "SELECT observed_seq, observed_hash FROM receipts_head "
                "WHERE instance_id=(SELECT instance_id FROM receipt_instance WHERE singleton=1)"
            ).fetchone()
        assert head is not None
        assert int(head[0]) > int(before_head[0])
        assert isinstance(head[1], str) and len(head[1]) == 64
