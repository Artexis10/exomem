from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra" / "scripts" / "secret_handoff.py"
MATRIX = ROOT / "infra" / "contracts" / "secret-destinations-v1.json"
ANSIBLE_SECRET_RUNNER = ROOT / "infra" / "scripts" / "ansible_with_sops.sh"
EXPECTED_VERCEL_ORG_ID = "team_k2Mvk25habQVgRnZuzF5peEu"
EXPECTED_VERCEL_PROJECT_ID = "prj_uMt1uqSUP5ALo0zLJvcKLBCJ7HUs"


def _load_module():
    spec = importlib.util.spec_from_file_location("secret_handoff", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_ciphertext_validator():
    path = ROOT / "infra" / "scripts" / "validate_sops_ciphertext.py"
    spec = importlib.util.spec_from_file_location("validate_sops_ciphertext_round_trip", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _matrix() -> dict[str, object]:
    return json.loads(MATRIX.read_text(encoding="utf-8"))


def _link_vercel_project(
    root: Path,
    *,
    org_id: str = EXPECTED_VERCEL_ORG_ID,
    project_id: str = EXPECTED_VERCEL_PROJECT_ID,
    project_name: str = "substrate",
) -> None:
    link_dir = root / ".vercel"
    link_dir.mkdir(parents=True)
    (link_dir / "project.json").write_text(
        json.dumps(
            {
                "orgId": org_id,
                "projectId": project_id,
                "projectName": project_name,
            }
        ),
        encoding="utf-8",
    )


def _fake_ciphertext(value: object) -> object:
    if isinstance(value, dict):
        return {key: _fake_ciphertext(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_fake_ciphertext(item) for item in value]
    if isinstance(value, str):
        return "ENC[test]"
    return value


def _run_fake_sops(
    command: list[str],
    kwargs: dict[str, object],
    plaintext_by_path: dict[Path, dict[str, object]],
) -> subprocess.CompletedProcess[bytes] | None:
    if len(command) < 2 or command[1] not in {"encrypt", "decrypt"}:
        return None
    if command[1] == "encrypt":
        document = json.loads(kwargs["input"])
        output_path = Path(command[command.index("--output") + 1])
        plaintext_by_path[output_path] = document
        ciphertext = _fake_ciphertext(document)
        assert isinstance(ciphertext, dict)
        ciphertext["sops"] = {"mac": "ENC[test]"}
        output_path.write_text(json.dumps(ciphertext), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    encrypted_path = Path(command[-1])
    return subprocess.CompletedProcess(
        command,
        0,
        stdout=json.dumps(plaintext_by_path[encrypted_path]).encode("utf-8"),
        stderr=b"",
    )


def test_destination_matrix_enforces_named_secret_boundaries() -> None:
    document = _matrix()
    assert document["schema_version"] == 1
    assert document["vercel_projects"] == {
        "substrate.production": {
            "org_id": EXPECTED_VERCEL_ORG_ID,
            "project_id": EXPECTED_VERCEL_PROJECT_ID,
            "project_name": "substrate",
            "receipt_policy": {
                "content": "destination-binding-only",
                "mode": "immutable-strictly-monotonic",
                "partial_write_recovery": "new-version",
                "scope": "destination",
                "target": (
                    "infra/secrets/receipts/vercel/{secret}/{destination}.{version}.receipt.json"
                ),
            },
        }
    }
    secrets = document["secrets"]
    assert isinstance(secrets, dict)

    vercel_destinations = [
        destination
        for secret in secrets.values()
        for destination in secret["destinations"].values()
        if destination["kind"] == "vercel_env"
    ]
    assert vercel_destinations
    assert {destination["project"] for destination in vercel_destinations} == {
        "substrate.production"
    }

    access = {
        destination["kind"]
        for name in ("cloudflare_access_client_id", "cloudflare_access_client_secret")
        for destination in secrets[name]["destinations"].values()
    }
    assert access == {"vercel_env"}

    tunnel = secrets["cloudflare_tunnel_token"]["destinations"]
    assert set(tunnel) == {"k3s.cloudflared.active"}
    assert {destination["kind"] for destination in tunnel.values()} == {"sops_k8s_secret"}

    scheduler = secrets["hosted_scheduler_secret"]["destinations"]
    assert set(scheduler) == {
        "k3s.scheduler.active",
        "vercel.substrate.production.scheduler.active",
        "vercel.substrate.production.scheduler.previous",
    }
    assert scheduler["k3s.scheduler.active"]["slot"] == "active"
    assert scheduler["vercel.substrate.production.scheduler.previous"]["slot"] == "previous"

    global_cron = secrets["global_cron_secret"]["destinations"]
    assert set(global_cron) == {"vercel.substrate.production.global-cron.active"}
    assert all(destination["kind"] == "vercel_env" for destination in global_cron.values())

    wrapping = secrets["provisioner_wrapping_key"]["destinations"]
    assert set(wrapping) == {
        "escrow.provisioner-wrapping-key.active",
        "k3s.provisioner.wrapping-key.active",
    }
    assert {destination["kind"] for destination in wrapping.values()} == {
        "sops_escrow",
        "sops_k8s_secret",
    }

    database_backup = secrets["database_backup_key"]["destinations"]
    assert set(database_backup) == {
        "ansible.hosted-node.etcd-s3-secret-key.active",
        "k3s.provisioner.database-backup-key.active",
    }

    assert "cell_credential" not in secrets


def test_matrix_has_unique_sops_targets_and_no_plaintext_file_target() -> None:
    targets: list[str] = []
    kubernetes_objects: list[tuple[str, str]] = []
    for secret in _matrix()["secrets"].values():
        for destination in secret["destinations"].values():
            if not destination["kind"].startswith("sops_"):
                continue
            target = destination["target"]
            assert target.endswith(".sops.json")
            assert "{version}" in target
            assert "/decrypted/" not in target
            targets.append(target)
            if destination["kind"] == "sops_k8s_secret":
                kubernetes_objects.append(
                    (destination["namespace"], destination["kubernetes_secret"])
                )
    assert len(targets) == len(set(targets))
    assert len(kubernetes_objects) == len(set(kubernetes_objects))


def test_rejects_cross_destination_before_reading_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_module()

    def _unexpected_read(*_args, **_kwargs):
        raise AssertionError("secret source must not be read for a rejected route")

    monkeypatch.setattr(module, "_read_secret", _unexpected_read)
    with pytest.raises(module.HandoffError, match="not allowed"):
        module.execute_handoff(
            matrix_path=MATRIX,
            repository_root=ROOT,
            secret_name="global_cron_secret",
            version="v1",
            destination_ids=("k3s.scheduler.active",),
            source_kind="stdin",
            terraform_bin="terraform",
            sops_bin="sops",
            vercel_bin="vercel",
            vercel_project=None,
            dry_run=False,
        )


def test_rejects_wrong_vercel_project_before_reading_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()
    _link_vercel_project(tmp_path, project_id="prj_wrong")

    def _unexpected_read(*_args, **_kwargs):
        raise AssertionError("secret source must not be read for a mismatched project")

    monkeypatch.setattr(module, "_read_secret", _unexpected_read)
    with pytest.raises(module.HandoffError, match="Vercel project identity does not match"):
        module.execute_handoff(
            matrix_path=MATRIX,
            repository_root=tmp_path,
            secret_name="hosted_scheduler_secret",
            version="v1",
            destination_ids=("vercel.substrate.production.scheduler.active",),
            source_kind="stdin",
            terraform_bin="terraform",
            sops_bin="sops",
            vercel_bin="vercel",
            vercel_project=tmp_path,
            dry_run=False,
        )


def test_rejects_existing_sops_version_before_reading_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()
    target = tmp_path / "infra/secrets/platform/cloudflared-token.v3.sops.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"sops":{}}', encoding="utf-8")

    def _unexpected_read(*_args, **_kwargs):
        raise AssertionError("secret source must not be read for a reused version")

    monkeypatch.setattr(module, "_read_secret", _unexpected_read)
    with pytest.raises(module.HandoffError, match="SOPS version must be new and increasing"):
        module.execute_handoff(
            matrix_path=MATRIX,
            repository_root=tmp_path,
            secret_name="cloudflare_tunnel_token",
            version="v3",
            destination_ids=("k3s.cloudflared.active",),
            source_kind="stdin",
            terraform_bin="terraform",
            sops_bin="sops",
            vercel_bin="vercel",
            vercel_project=None,
            dry_run=False,
        )


def test_k3s_handoff_seals_atomically_without_plaintext_or_output_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_module()
    sentinel = b"handoff-sentinel-never-persist"
    history = tmp_path / "operator.history"
    history.write_text(
        "secret_handoff.py --secret cloudflare_tunnel_token --source stdin\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SOPS_AGE_RECIPIENTS", "age1testrecipient")
    monkeypatch.setattr(module, "_read_secret", lambda **_kwargs: sentinel)

    calls: list[tuple[list[str], bytes | None]] = []
    plaintext_by_path: dict[Path, dict[str, object]] = {}

    def _runner(command, **kwargs):
        calls.append((list(command), kwargs.get("input")))
        result = _run_fake_sops(list(command), kwargs, plaintext_by_path)
        assert result is not None
        return result

    monkeypatch.setattr(module.subprocess, "run", _runner)
    module.execute_handoff(
        matrix_path=MATRIX,
        repository_root=tmp_path,
        secret_name="cloudflare_tunnel_token",
        version="v7",
        destination_ids=("k3s.cloudflared.active",),
        source_kind="stdin",
        terraform_bin="terraform",
        sops_bin="sops",
        vercel_bin="vercel",
        vercel_project=None,
        dry_run=False,
    )

    target = tmp_path / "infra/secrets/platform/cloudflared-token.v7.sops.json"
    assert target.is_file()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert sentinel not in target.read_bytes()
    assert not any(sentinel in path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())
    assert sentinel not in history.read_bytes()
    assert all(sentinel.decode() not in argument for command, _ in calls for argument in command)
    assert capsys.readouterr().out == ""


def test_vercel_handoff_uses_stdin_and_discards_cli_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_module()
    sentinel = b"vercel-sentinel-never-in-argv"
    _link_vercel_project(tmp_path)
    monkeypatch.setattr(module, "_read_secret", lambda **_kwargs: sentinel)
    calls: list[tuple[list[str], bytes | None]] = []

    def _runner(command, **kwargs):
        calls.append((list(command), kwargs.get("input")))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=b"provider accidentally echoed secret",
            stderr=b"provider accidentally echoed secret",
        )

    monkeypatch.setattr(module.subprocess, "run", _runner)
    module.execute_handoff(
        matrix_path=MATRIX,
        repository_root=tmp_path,
        secret_name="hosted_scheduler_secret",
        version="v2",
        destination_ids=("vercel.substrate.production.scheduler.active",),
        source_kind="stdin",
        terraform_bin="terraform",
        sops_bin="sops",
        vercel_bin="vercel",
        vercel_project=tmp_path,
        dry_run=False,
    )

    assert len(calls) == 1
    command, input_bytes = calls[0]
    assert input_bytes == sentinel + b"\n"
    assert all(sentinel.decode() not in argument for argument in command)
    assert "EXOMEM_HOSTED_SCHEDULER_SECRET" in command
    assert "--sensitive" in command
    receipt = (
        tmp_path
        / "infra/secrets/receipts/vercel/hosted_scheduler_secret"
        / "vercel.substrate.production.scheduler.active.v2.receipt.json"
    )
    assert receipt.is_file()
    receipt_document = json.loads(receipt.read_text(encoding="utf-8"))
    assert receipt_document["status"] == "delivered"
    assert receipt_document["destination_version"] == "v2"
    assert receipt_document["project_id"] == EXPECTED_VERCEL_PROJECT_ID
    assert "secret_value" not in receipt_document
    assert "digest" not in receipt_document
    assert sentinel not in receipt.read_bytes()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_one_read_can_handoff_scheduler_value_to_vercel_and_k3s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()
    sentinel = b"one-read-two-destinations"
    reads = 0

    def _reader(**_kwargs):
        nonlocal reads
        reads += 1
        return sentinel

    _link_vercel_project(tmp_path)
    plaintext_by_path: dict[Path, dict[str, object]] = {}

    def _runner(command, **kwargs):
        result = _run_fake_sops(list(command), kwargs, plaintext_by_path)
        if result is not None:
            return result
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(module, "_read_secret", _reader)
    monkeypatch.setattr(module.subprocess, "run", _runner)
    monkeypatch.setenv("SOPS_AGE_RECIPIENTS", "age1testrecipient")
    module.execute_handoff(
        matrix_path=MATRIX,
        repository_root=tmp_path,
        secret_name="hosted_scheduler_secret",
        version="v4",
        destination_ids=(
            "vercel.substrate.production.scheduler.active",
            "k3s.scheduler.active",
        ),
        source_kind="stdin",
        terraform_bin="terraform",
        sops_bin="sops",
        vercel_bin="vercel",
        vercel_project=tmp_path,
        dry_run=False,
    )
    assert reads == 1
    assert (tmp_path / "infra/secrets/platform/hosted-scheduler.v4.sops.json").is_file()


def test_local_sops_destination_is_durable_before_any_vercel_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()
    sentinel = b"local-first-shared-value"
    target = tmp_path / "infra/secrets/platform/hosted-scheduler.v8.sops.json"
    _link_vercel_project(tmp_path)
    monkeypatch.setattr(module, "_read_secret", lambda **_kwargs: sentinel)
    monkeypatch.setenv("SOPS_AGE_RECIPIENTS", "age1testrecipient")
    plaintext_by_path: dict[Path, dict[str, object]] = {}
    events: list[str] = []

    def _runner(command, **kwargs):
        sops_result = _run_fake_sops(list(command), kwargs, plaintext_by_path)
        if sops_result is not None:
            events.append(command[1])
            return sops_result
        events.append("vercel")
        assert target.is_file()
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(module.subprocess, "run", _runner)
    module.execute_handoff(
        matrix_path=MATRIX,
        repository_root=tmp_path,
        secret_name="hosted_scheduler_secret",
        version="v8",
        destination_ids=(
            "vercel.substrate.production.scheduler.active",
            "k3s.scheduler.active",
        ),
        source_kind="stdin",
        terraform_bin="terraform",
        sops_bin="sops",
        vercel_bin="vercel",
        vercel_project=tmp_path,
        dry_run=False,
    )
    assert events == ["encrypt", "decrypt", "vercel"]


def test_local_sops_failure_blocks_vercel_and_consumes_reserved_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()
    _link_vercel_project(tmp_path)
    monkeypatch.setattr(module, "_read_secret", lambda **_kwargs: b"never-sent")
    monkeypatch.setenv("SOPS_AGE_RECIPIENTS", "age1testrecipient")
    calls: list[list[str]] = []

    def _runner(command, **_kwargs):
        calls.append(list(command))
        assert command[1] == "encrypt"
        return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=b"provider detail")

    monkeypatch.setattr(module.subprocess, "run", _runner)
    with pytest.raises(module.HandoffError, match="SOPS encryption failed"):
        module.execute_handoff(
            matrix_path=MATRIX,
            repository_root=tmp_path,
            secret_name="hosted_scheduler_secret",
            version="v10",
            destination_ids=(
                "vercel.substrate.production.scheduler.active",
                "k3s.scheduler.active",
            ),
            source_kind="stdin",
            terraform_bin="terraform",
            sops_bin="sops",
            vercel_bin="vercel",
            vercel_project=tmp_path,
            dry_run=False,
        )
    assert len(calls) == 1
    pending = (
        tmp_path
        / "infra/secrets/receipts/vercel/hosted_scheduler_secret"
        / "vercel.substrate.production.scheduler.active.v10.receipt.pending.json"
    )
    assert pending.is_file()


def test_partial_vercel_write_leaves_content_free_receipts_and_forces_new_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()
    sentinel = b"partial-write-sentinel"
    _link_vercel_project(tmp_path)
    reads = 0
    vercel_calls = 0

    def _reader(**_kwargs):
        nonlocal reads
        reads += 1
        return sentinel

    def _runner(command, **_kwargs):
        nonlocal vercel_calls
        vercel_calls += 1
        return subprocess.CompletedProcess(
            command,
            0 if vercel_calls == 1 else 1,
            stdout=sentinel,
            stderr=sentinel,
        )

    monkeypatch.setattr(module, "_read_secret", _reader)
    monkeypatch.setattr(module.subprocess, "run", _runner)
    with pytest.raises(module.HandoffError, match="Vercel secret handoff failed"):
        module.execute_handoff(
            matrix_path=MATRIX,
            repository_root=tmp_path,
            secret_name="hosted_scheduler_secret",
            version="v9",
            destination_ids=(
                "vercel.substrate.production.scheduler.active",
                "vercel.substrate.production.scheduler.previous",
            ),
            source_kind="stdin",
            terraform_bin="terraform",
            sops_bin="sops",
            vercel_bin="vercel",
            vercel_project=tmp_path,
            dry_run=False,
        )

    receipt_dir = tmp_path / "infra/secrets/receipts/vercel/hosted_scheduler_secret"
    delivered = receipt_dir / "vercel.substrate.production.scheduler.active.v9.receipt.json"
    uncertain = (
        receipt_dir / "vercel.substrate.production.scheduler.previous.v9.receipt.pending.json"
    )
    assert delivered.is_file()
    assert uncertain.is_file()
    assert sentinel not in delivered.read_bytes() + uncertain.read_bytes()
    assert reads == 1
    assert vercel_calls == 2

    with pytest.raises(module.HandoffError, match="Vercel version must be new and increasing"):
        module.execute_handoff(
            matrix_path=MATRIX,
            repository_root=tmp_path,
            secret_name="hosted_scheduler_secret",
            version="v9",
            destination_ids=("vercel.substrate.production.scheduler.previous",),
            source_kind="stdin",
            terraform_bin="terraform",
            sops_bin="sops",
            vercel_bin="vercel",
            vercel_project=tmp_path,
            dry_run=False,
        )
    assert reads == 1
    assert vercel_calls == 2


def test_concurrent_vercel_versions_cannot_regress_one_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()
    _link_vercel_project(tmp_path)
    barrier = threading.Barrier(2)
    thread_state = threading.local()
    provider_calls: list[bytes] = []
    remote_value: list[bytes] = []

    def _reader(**_kwargs):
        barrier.wait(timeout=5)
        return thread_state.secret

    def _runner(command, **kwargs):
        value = kwargs["input"].rstrip(b"\n")
        time.sleep(0.05 if value == b"version-three" else 0.25)
        remote_value[:] = [value]
        provider_calls.append(value)
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(module, "_read_secret", _reader)
    monkeypatch.setattr(module.subprocess, "run", _runner)

    def _handoff(version: str, value: bytes) -> str | None:
        thread_state.secret = value
        try:
            module.execute_handoff(
                matrix_path=MATRIX,
                repository_root=tmp_path,
                secret_name="hosted_scheduler_secret",
                version=version,
                destination_ids=("vercel.substrate.production.scheduler.active",),
                source_kind="stdin",
                terraform_bin="terraform",
                sops_bin="sops",
                vercel_bin="vercel",
                vercel_project=tmp_path,
                dry_run=False,
            )
        except module.HandoffError as exc:
            return str(exc)
        return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        higher = executor.submit(_handoff, "v3", b"version-three")
        lower = executor.submit(_handoff, "v2", b"version-two")
        results = {"v3": higher.result(timeout=10), "v2": lower.result(timeout=10)}

    assert results["v3"] is None
    assert results["v2"] in {None, "Vercel version must be new and increasing"}
    assert remote_value == [b"version-three"]
    assert provider_calls in ([b"version-three"], [b"version-two", b"version-three"])
    assert provider_calls != [b"version-three", b"version-two"]


def test_wrapping_key_is_read_once_for_workload_and_offline_escrow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()
    sentinel = b"wrapping-key-sentinel"
    reads = 0
    plaintext_inputs: list[dict[str, object]] = []
    plaintext_by_path: dict[Path, dict[str, object]] = {}

    def _reader(**_kwargs):
        nonlocal reads
        reads += 1
        return sentinel

    def _runner(command, **kwargs):
        if command[1] == "encrypt":
            plaintext_inputs.append(json.loads(kwargs["input"]))
        result = _run_fake_sops(list(command), kwargs, plaintext_by_path)
        assert result is not None
        return result

    monkeypatch.setattr(module, "_read_secret", _reader)
    monkeypatch.setattr(module.subprocess, "run", _runner)
    monkeypatch.setenv("SOPS_AGE_RECIPIENTS", "age1testrecipient")
    module.execute_handoff(
        matrix_path=MATRIX,
        repository_root=tmp_path,
        secret_name="provisioner_wrapping_key",
        version="v5",
        destination_ids=(
            "k3s.provisioner.wrapping-key.active",
            "escrow.provisioner-wrapping-key.active",
        ),
        source_kind="stdin",
        terraform_bin="terraform",
        sops_bin="sops",
        vercel_bin="vercel",
        vercel_project=None,
        dry_run=False,
    )
    assert reads == 1
    assert len(plaintext_inputs) == 2
    assert any(document.get("kind") == "Secret" for document in plaintext_inputs)
    assert any(
        document.get("secret_name") == "provisioner_wrapping_key" for document in plaintext_inputs
    )
    assert (tmp_path / "infra/secrets/provisioner/wrapping-key.v5.sops.json").is_file()
    assert (tmp_path / "infra/secrets/escrow/provisioner-wrapping-key.v5.sops.json").is_file()
    assert not any(sentinel in path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())


def test_terraform_source_is_exactly_bound_to_contract_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    calls: list[list[str]] = []

    def _runner(command, **_kwargs):
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout=b"terraform-secret", stderr=b"")

    monkeypatch.setattr(module.subprocess, "run", _runner)
    value = module._read_secret(
        source_kind="terraform",
        secret_spec=module.load_matrix(MATRIX).secrets["cloudflare_tunnel_token"],
        repository_root=ROOT,
        terraform_bin="terraform-test",
    )
    assert value == b"terraform-secret"
    assert calls == [
        [
            "terraform-test",
            f"-chdir={ROOT / 'infra/terraform/foundation'}",
            "output",
            "-raw",
            "cloudflare_tunnel_token",
        ]
    ]


@pytest.mark.skipif(
    os.environ.get("EXOMEM_RUN_REAL_SOPS_TESTS") != "1",
    reason="set EXOMEM_RUN_REAL_SOPS_TESTS=1 with pinned SOPS/age binaries",
)
def test_pinned_sops_age_round_trip_preserves_json_escaped_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()
    sops_bin = os.environ.get("SOPS_BIN") or shutil.which("sops")
    age_keygen_bin = os.environ.get("AGE_KEYGEN_BIN") or shutil.which("age-keygen")
    assert sops_bin is not None
    assert age_keygen_bin is not None

    versions = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in (ROOT / "infra/tool-versions.env").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    }
    sops_version = subprocess.run(
        [sops_bin, "--version"], capture_output=True, text=True, check=True
    )
    age_version = subprocess.run(
        [age_keygen_bin, "--version"], capture_output=True, text=True, check=True
    )
    assert versions["SOPS_VERSION"] in sops_version.stdout + sops_version.stderr
    assert versions["AGE_VERSION"] in age_version.stdout + age_version.stderr

    identity = tmp_path / "operator.agekey"
    subprocess.run(
        [age_keygen_bin, "-o", str(identity)],
        capture_output=True,
        check=True,
    )
    recipient_result = subprocess.run(
        [age_keygen_bin, "-y", str(identity)],
        capture_output=True,
        text=True,
        check=True,
    )
    recipient = recipient_result.stdout.strip()
    assert recipient.startswith("age1")
    monkeypatch.setenv("SOPS_AGE_RECIPIENTS", recipient)
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(identity))
    sentinel = 'quoted"-backslash\\-snowman-☃'.encode()
    monkeypatch.setattr(module, "_read_secret", lambda **_kwargs: sentinel)

    module.execute_handoff(
        matrix_path=MATRIX,
        repository_root=tmp_path,
        secret_name="cloudflare_tunnel_token",
        version="v1",
        destination_ids=("k3s.cloudflared.active",),
        source_kind="stdin",
        terraform_bin="terraform",
        sops_bin=sops_bin,
        vercel_bin="vercel",
        vercel_project=None,
        dry_run=False,
    )

    target = tmp_path / "infra/secrets/platform/cloudflared-token.v1.sops.json"
    ciphertext = target.read_bytes()
    escaped = json.dumps(sentinel.decode("utf-8"))[1:-1].encode("utf-8")
    assert sentinel not in ciphertext
    assert escaped not in ciphertext
    encrypted_document = json.loads(ciphertext)
    assert encrypted_document["stringData"]["token"].startswith("ENC[")
    assert isinstance(encrypted_document["sops"], dict)
    validator = _load_ciphertext_validator()
    assert validator.validate(matrix_path=MATRIX, artifacts=[target], root=tmp_path) == 1

    decrypted = subprocess.run(
        [sops_bin, "decrypt", "--input-type", "json", "--output-type", "json", str(target)],
        capture_output=True,
        check=True,
    )
    document = json.loads(decrypted.stdout)
    assert document["stringData"] == {"token": sentinel.decode("utf-8")}
    assert document["metadata"]["labels"]["exomem.io/secret-version"] == "v1"


def test_cli_dry_run_validates_route_without_reading_stdin() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--matrix",
            str(MATRIX),
            "--repository-root",
            str(ROOT),
            "--secret",
            "hosted_scheduler_secret",
            "--version",
            "v3",
            "--destination",
            "k3s.scheduler.active",
            "--source",
            "stdin",
            "--dry-run",
        ],
        input=b"must-not-be-read",
        capture_output=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    assert result.returncode == 0
    assert result.stdout == b"handoff policy accepted\n"
    assert b"must-not-be-read" not in result.stdout + result.stderr


def test_cli_has_no_argument_that_can_carry_a_secret_value() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        check=True,
    )
    assert b"--value" not in result.stdout
    assert b"--credential" not in result.stdout
