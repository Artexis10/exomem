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
from pathlib import Path

import pytest
import uvicorn

from exomem import init as init_module
from exomem.governance import policy, schema_v4, store
from exomem.lease_coordinator import SQLiteLeaseStore, create_app

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


def _v4_vault(tmp_path: Path) -> tuple[Path, schema_v4.VerifiedActiveGovernanceState]:
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

    compiled = policy.compile_documents(dict(POLICY_DOCUMENTS))
    assert not compiled.empty and not compiled.blocked
    connection = sqlite3.connect(store.sidecar_path(vault))
    try:
        store._migrate(connection)
        connection.commit()
        migration = schema_v4.migrate_v3_connection(
            connection,
            schema_v4.MigrationSeed(
                activation_store_id="activation-store-old-binary-probe",
                logical_vault_id="logical-vault-old-binary-probe",
                activation_epoch=1,
                policy=schema_v4.PolicyGenerationSeed(
                    generation_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
                    source_documents=POLICY_DOCUMENTS,
                    source_fingerprint=compiled.fingerprint,
                    conflict_digest="1" * 64,
                    compiled_policy=policy.canonical_compiled_bytes(compiled),
                    policy_fingerprint=compiled.fingerprint,
                    compiler_schema_version=1,
                    projector_schema_version=1,
                    predecessor_generation_id=None,
                    authoring_event_id="event-old-binary-probe",
                    receipt_event_id="receipt-old-binary-probe",
                    created_at=1_800_000_000,
                ),
                catalog=schema_v4.CatalogGenerationSeed(
                    catalog_generation=1,
                    descriptor=b'{"artifacts":[]}',
                    artifact_count=0,
                    created_at=1_800_000_000,
                ),
                namespace=schema_v4.ProjectionNamespaceSeed(
                    namespace_id="projection-namespace-old-binary-probe",
                    evidence=b'{"ready":true}',
                    ready_at=1_800_000_000,
                ),
                migrated_at=1_800_000_000,
            ),
        )
        active = schema_v4.load_active_state(
            connection,
            expected_logical_vault_id="logical-vault-old-binary-probe",
            expected_activation_store_id="activation-store-old-binary-probe",
            expected_activation_epoch=1,
            expected_activation_state_digest=migration.activation_state_digest,
        )
    finally:
        connection.close()
    return vault, active


def _database_digest(vault: Path) -> str:
    with sqlite3.connect(store.sidecar_path(vault)) as connection:
        payload = "\n".join(connection.iterdump()).encode("utf-8")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    return hashlib.sha256(str(version).encode("ascii") + b"\0" + payload).hexdigest()


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
) -> None:
    old_python = _old_python()
    vault, active = _v4_vault(tmp_path)
    coordinator_database = tmp_path / "coordinator.sqlite"
    lease_store = SQLiteLeaseStore(coordinator_database)
    fence, accepted = lease_store.transition_schema_fence(
        "old-binary-probe",
        expected_generation=0,
        schema_version=4,
    )
    assert accepted and fence["generation"] == 1
    before_database = _database_digest(vault)
    visible = vault / "Knowledge Base" / "Notes" / "Insights" / "visible.md"
    before_visible = visible.read_bytes()

    # Record the actual released behavior on a disposable copy. v0.57 both
    # starts and reads a v4 store, and that nominal read performs governance
    # DML. The live-copy assertion below therefore cannot rely on self-refusal
    # or on a writer-only fence.
    probe = tmp_path / "isolated-old-binary-probe"
    shutil.copytree(vault, probe)
    probe_before = _database_digest(probe)
    probe_env = _old_environment(
        tmp_path,
        probe,
        None,
        state_name="isolated-old-state",
    )
    assert _old_server_starts(old_python, probe_env) is True
    read = _old_cli(old_python, probe_env, "find", "lantern", "--json")
    assert read.returncode == 0, read.stderr
    assert _database_digest(probe) != probe_before
    before_probe_write = _database_digest(probe)
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
    assert _database_digest(probe) == before_probe_write
    assert list((probe / "Knowledge Base" / "Sources").rglob("*unsafe-old-writer-probe.md"))
    with sqlite3.connect(store.sidecar_path(probe)) as connection:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 4

    with _lease_server(coordinator_database) as coordinator_url:
        lease_probe = tmp_path / "isolated-old-binary-lease-probe"
        shutil.copytree(vault, lease_probe)
        lease_probe_before = _database_digest(lease_probe)
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
        assert _database_digest(lease_probe) == lease_probe_before
        assert not list(
            (lease_probe / "Knowledge Base" / "Sources").rglob(
                "*rejected-old-writer-probe.md"
            )
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
        assert _database_digest(vault) == before_database
        assert visible.read_bytes() == before_visible
        assert not list((vault / "Knowledge Base" / "Notes").rglob("old-writer-must-not-land.md"))

        with sqlite3.connect(store.sidecar_path(vault)) as connection:
            result = schema_v4.downmigrate_v4_connection(
                connection,
                expected=active,
                expected_source_documents=POLICY_DOCUMENTS,
                expected_catalog_descriptor=b'{"artifacts":[]}',
                verified_workspace_digest=schema_v4.source_documents_digest(POLICY_DOCUMENTS),
                verified_catalog_digest=schema_v4.catalog_rebuild_digest(b'{"artifacts":[]}'),
                recovery_event_id="d" * 64,
                recovery_plan_digest="e" * 64,
                recovery_target_digest="f" * 64,
                downmigrated_at=1_800_000_100,
            )
        assert result.schema_version == 3
        rollback_fence, accepted = lease_store.transition_schema_fence(
            "old-binary-probe",
            expected_generation=1,
            schema_version=3,
        )
        assert accepted and rollback_fence["generation"] == 2
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
    with sqlite3.connect(store.sidecar_path(vault)) as connection:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == 3
