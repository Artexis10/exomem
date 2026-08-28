"""Durable desktop stop-window receipt contract."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts" / "service-transition-receipt.py"


def _base_args(tmp_path: Path) -> list[str]:
    return [
        "--path",
        str(tmp_path / "transition" / "exomem.json"),
        "--service-id",
        "exomem",
        "--binding-path",
        str(tmp_path / "service" / ".env"),
        "--state-root",
        str(tmp_path / "state"),
        "--vault",
        str(tmp_path / "vault"),
        "--target-port",
        "8765",
    ]


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_receipt_create_verify_phase_and_clear_are_durable_and_exact(
    tmp_path: Path,
) -> None:
    base = _base_args(tmp_path)
    created = _run(
        "create",
        *base,
        "--port",
        "8765",
        "--worker-pid",
        "4101",
        "--listener-pid",
        "4101",
        "--listener-pid",
        "4102",
    )
    assert created.returncode == 0, created.stderr
    receipt_path = tmp_path / "transition" / "exomem.json"
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert payload["phase"] == "captured"
    assert payload["worker_pid"] == 4101
    assert payload["listener_pids"] == [4101, 4102]
    assert payload["captured_pids"] == [4101, 4102]
    if os.name != "nt":
        assert stat_mode(receipt_path) & 0o077 == 0

    verified = _run("verify", *base, "--json")
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["proof_pids"] == [4101, 4102]

    phased = _run(
        "phase",
        *base,
        "--phase",
        "failed",
        "--observed-pid",
        "4200",
    )
    assert phased.returncode == 0, phased.stderr
    verified = _run("verify", *base, "--json")
    assert json.loads(verified.stdout)["proof_pids"] == [4101, 4102, 4200]

    cleared = _run("clear", *base)
    assert cleared.returncode == 0, cleared.stderr
    assert not receipt_path.exists()


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode


def test_receipt_missing_corrupt_or_mismatched_fails_closed(tmp_path: Path) -> None:
    base = _base_args(tmp_path)
    missing = _run("verify", *base, "--json")
    assert missing.returncode != 0
    assert "missing" in missing.stderr.lower()

    receipt_path = tmp_path / "transition" / "exomem.json"
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_text("{not json", encoding="utf-8")
    corrupt = _run("verify", *base, "--json")
    assert corrupt.returncode != 0
    assert "invalid" in corrupt.stderr.lower()

    receipt_path.unlink()
    created = _run(
        "create",
        *base,
        "--port",
        "8765",
        "--worker-pid",
        "4101",
    )
    assert created.returncode == 0, created.stderr
    mismatched = list(base)
    mismatched[mismatched.index("exomem")] = "exomem-suffixed"
    mismatch = _run("verify", *mismatched, "--json")
    assert mismatch.returncode != 0
    assert "does not match" in mismatch.stderr.lower()


def test_receipt_and_state_root_must_resolve_outside_the_vault(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    base = _base_args(tmp_path)
    base[base.index(str(tmp_path / "transition" / "exomem.json"))] = str(
        vault / ".transition" / "exomem.json"
    )
    refused_receipt = _run(
        "create",
        *base,
        "--port",
        "8765",
        "--worker-pid",
        "4101",
    )
    assert refused_receipt.returncode != 0
    assert "outside the vault" in refused_receipt.stderr.lower()

    base = _base_args(tmp_path)
    base[base.index(str(tmp_path / "state"))] = str(vault / ".machine-local")
    refused_state = _run(
        "create",
        *base,
        "--port",
        "8765",
        "--worker-pid",
        "4101",
    )
    assert refused_state.returncode != 0
    assert "outside the vault" in refused_state.stderr.lower()
