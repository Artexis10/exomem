"""cellctl's backup Job asks the cell image's own `exomem.vault` whether its
source holds a vault, before restic reads it (move-cloud-cells-to-local-storage
D5). The command lives in cellctl and names this package's function, so only
this suite can see the two drift apart: a rename here would make every backup
of a real vault refuse, and block every upgrade, whose pre-upgrade backup runs
on the cell's current image.

The command is loaded from cellctl's stdlib-only module by path and run under
sh with this package as the `python3` on PATH, as the Job runs it.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

from exomem.init import init_vault

BACKUP_SOURCE = Path(__file__).resolve().parents[1] / "infra" / "cellctl" / "src" / "cellctl" / "backup_source.py"


def _run_vault_check(vault: Path, tmp_path: Path) -> tuple[int, str]:
    spec = importlib.util.spec_from_file_location("cellctl_backup_source", BACKUP_SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    termination_log = tmp_path / "termination-log"
    script = module.VAULT_CHECK_COMMAND.replace("/data/vault", str(vault)).replace(
        "/dev/termination-log", str(termination_log)
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    python3 = bin_dir / "python3"
    python3.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    python3.chmod(0o755)
    result = subprocess.run(["sh", "-c", script], env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"})
    message = termination_log.read_text(encoding="utf-8") if termination_log.exists() else ""
    return result.returncode, message


def test_a_vault_made_by_init_vault_passes_the_backup_check(tmp_path: Path) -> None:
    vault = tmp_path / "data" / "vault"
    init_vault(vault, initialize_state=False)

    assert _run_vault_check(vault, tmp_path) == (0, "")


def test_a_vault_with_only_the_legacy_schema_passes_the_backup_check(tmp_path: Path) -> None:
    # A vault from before the schema moved to .exomem/ (#488) still serves.
    vault = tmp_path / "data" / "vault"
    sentinel = vault / "Knowledge Base" / "_Schema" / "SKILL.md"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("# Schema\n", encoding="utf-8")

    assert _run_vault_check(vault, tmp_path) == (0, "")


def test_an_emptied_volume_is_refused(tmp_path: Path) -> None:
    # cell-init leaves /data/vault and /data/host empty when it refuses.
    vault = tmp_path / "data" / "vault"
    vault.mkdir(parents=True)
    (tmp_path / "data" / "host").mkdir()

    assert _run_vault_check(vault, tmp_path) == (1, "BACKUP_SOURCE_NOT_A_VAULT")
