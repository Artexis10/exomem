"""The original-fixity rehearsal runs the restore runbook's blocks as written, so it must find exactly those blocks.

The run itself needs a K3s cell and is exercised by the `original-fixity` workflow mode.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from cloud_rehearsal import images, original_fixity
from cloud_rehearsal.report import StepFailure


def test_the_shipped_runbook_yields_the_two_blocks_whose_lines_the_rehearsal_reads():
    # A runbook edit that adds, drops or reorders a block would otherwise surface only in a cluster run.
    check, restore = original_fixity.runbook_blocks(original_fixity.RUNBOOK.read_text())

    assert original_fixity.HASH_MATCHES in check and original_fixity.ORIGINAL_RESTORED not in check
    assert original_fixity.ORIGINAL_RESTORED in restore


def test_a_heading_inside_a_fence_does_not_end_the_section():
    text = (
        "## Restore one original\n\n```bash\n## a shell comment, not a heading\necho first\n```\n\n"
        "```bash\necho second\n```\n\n## Record\n\n```bash\necho another section\n```\n"
    )

    assert original_fixity.runbook_blocks(text) == ["## a shell comment, not a heading\necho first\n", "echo second\n"]


def test_a_section_with_another_block_count_stops_the_rehearsal():
    text = "## Restore one original\n\n```bash\necho only\n```\n\n## Record\n"

    with pytest.raises(StepFailure, match="1 closed bash blocks"):
        original_fixity.runbook_blocks(text)


def test_the_first_block_reaches_kubectl_with_only_the_variables_the_rehearsal_sets(tmp_path):
    # A runbook edit that guards a new variable would otherwise fail only after a 90-minute cluster run.
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "kubectl").write_text("#!/bin/sh\necho kubectl-reached >&2\nexit 9\n")
    (stub / "kubectl").chmod(0o755)
    cell_id = "a" * 16
    variables = {"CELL_ID": cell_id, "SNAPSHOT": "0" * 64, "SCRATCH": f"exo-scratch-{cell_id}-1",
                 "ORIGINAL": "Evidence/a.bin", "KIND": "file", "EXPECTED": "1" * 64}
    env = original_fixity.runbook_env(variables, tmp_path / "kubeconfig")
    env["PATH"] = f"{stub}{os.pathsep}{env['PATH']}"
    blocks = original_fixity.runbook_blocks(original_fixity.RUNBOOK.read_text())

    result = subprocess.run(["bash", "-c", original_fixity.runbook_script(blocks)], cwd=images.REPO_ROOT,
                            env=env, capture_output=True, text=True, timeout=120, check=False)

    assert result.returncode == 3, result.stderr
    assert "kubectl-reached" in result.stderr
