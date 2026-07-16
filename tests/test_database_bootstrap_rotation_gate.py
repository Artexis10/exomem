from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra/scripts/database_bootstrap_rotation_gate.py"


def _receipt(
    path: Path,
    *,
    attempt: str = "attempt-current",
    version: str = "credential-v2",
    rotated_at: datetime | None = None,
) -> None:
    timestamp = rotated_at or datetime.now(UTC)
    path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "kind": "exomem-database-admin-rotation",
                "attemptId": attempt,
                "credentialVersion": version,
                "rotatedOrRevokedAt": timestamp.isoformat().replace("+00:00", "Z"),
            }
        ),
        encoding="utf-8",
    )
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    timestamp_ns = int(timestamp.timestamp() * 1_000_000_000)
    os.utime(path, ns=(timestamp_ns, timestamp_ns))


def _run(
    receipt: Path,
    *,
    job_status: int = 0,
    attempt: str = "attempt-current",
    version: str = "credential-v2",
    boundary_ns: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(SCRIPT),
            "--receipt",
            str(receipt),
            "--attempt-id",
            attempt,
            "--credential-version",
            version,
            "--attempt-start-ns",
            str(boundary_ns),
            "--job-status",
            str(job_status),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("job_status", [0, 1, 124, 130, 143])
def test_valid_current_rotation_receipt_returns_original_job_outcome(
    tmp_path: Path,
    job_status: int,
) -> None:
    receipt = tmp_path / "receipt.json"
    boundary_ns = time.time_ns()
    time.sleep(0.001)
    _receipt(receipt)

    completed = _run(receipt, boundary_ns=boundary_ns, job_status=job_status)

    assert completed.returncode == job_status
    assert "attempt-current" not in completed.stdout + completed.stderr
    assert "credential-v2" not in completed.stdout + completed.stderr


@pytest.mark.parametrize(
    ("case", "attempt", "version"),
    [
        ("wrong-attempt", "attempt-other", "credential-v2"),
        ("wrong-version", "attempt-current", "credential-v1"),
    ],
)
def test_rotation_receipt_must_match_current_attempt_and_credential_version(
    tmp_path: Path,
    case: str,
    attempt: str,
    version: str,
) -> None:
    del case
    receipt = tmp_path / "receipt.json"
    boundary_ns = time.time_ns()
    time.sleep(0.001)
    _receipt(receipt, attempt=attempt, version=version)

    completed = _run(receipt, boundary_ns=boundary_ns, job_status=124)

    assert completed.returncode not in {0, 124}


def test_rotation_receipt_rejects_stale_file_even_when_readable(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.json"
    stale = datetime.now(UTC) - timedelta(hours=1)
    _receipt(receipt, rotated_at=stale)
    old_ns = int(stale.timestamp() * 1_000_000_000)
    os.utime(receipt, ns=(old_ns, old_ns))

    completed = _run(receipt, boundary_ns=old_ns + 1, job_status=1)

    assert completed.returncode != 1


def test_rotation_receipt_rejects_symlink_and_non_private_mode(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    boundary_ns = time.time_ns()
    time.sleep(0.001)
    _receipt(target)
    link = tmp_path / "receipt.json"
    link.symlink_to(target)

    assert _run(link, boundary_ns=boundary_ns, job_status=0).returncode != 0

    target.chmod(0o640)
    assert _run(target, boundary_ns=boundary_ns, job_status=0).returncode != 0


def test_rotation_gate_module_is_importable_without_executing_cli() -> None:
    spec = importlib.util.spec_from_file_location("rotation_gate_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.RECEIPT_KIND == "exomem-database-admin-rotation"
