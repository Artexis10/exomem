from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra" / "scripts" / "secret_handoff.py"
MATRIX = ROOT / "infra" / "contracts" / "secret-destinations-v1.json"


def _load_module():
    spec = importlib.util.spec_from_file_location("secret_handoff", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _matrix() -> dict[str, object]:
    return json.loads(MATRIX.read_text(encoding="utf-8"))


def test_destination_matrix_enforces_named_secret_boundaries() -> None:
    document = _matrix()
    assert document["schema_version"] == 1
    secrets = document["secrets"]
    assert isinstance(secrets, dict)

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

    def _runner(command, **kwargs):
        calls.append((list(command), kwargs.get("input")))
        output_index = command.index("--output") + 1
        output_path = Path(command[output_index])
        output_path.write_text(
            json.dumps({"sops": {"mac": "ENC[test]"}, "data": "ENC[test]"}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout=b"ignored", stderr=b"ignored")

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

    def _runner(command, **_kwargs):
        if "--output" in command:
            Path(command[command.index("--output") + 1]).write_text(
                '{"data":"ENC[test]","sops":{"mac":"ENC[test]"}}',
                encoding="utf-8",
            )
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


def test_wrapping_key_is_read_once_for_workload_and_offline_escrow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()
    sentinel = b"wrapping-key-sentinel"
    reads = 0
    plaintext_inputs: list[dict[str, object]] = []

    def _reader(**_kwargs):
        nonlocal reads
        reads += 1
        return sentinel

    def _runner(command, **kwargs):
        plaintext_inputs.append(json.loads(kwargs["input"]))
        Path(command[command.index("--output") + 1]).write_text(
            '{"data":"ENC[test]","sops":{"mac":"ENC[test]"}}',
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

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
