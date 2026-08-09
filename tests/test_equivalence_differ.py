from __future__ import annotations

import json
from pathlib import Path


def test_differ_classifies_the_perturbed_twin_and_accepts_identical_runs(tmp_path: Path) -> None:
    from equivalence.differ import compare_runs

    fixture = Path("benchmarks/equivalence/fixtures/perturbed-twin")
    result = compare_runs(fixture / "left", fixture / "right", mode="blocking", out=tmp_path)
    assert result.blocking
    assert {diff.field for diff in result.diffs} == {"dataset_identity", "top_k", "retrieved_text"}
    identical = compare_runs(fixture / "left", fixture / "left", mode="blocking", out=tmp_path / "same")
    assert not identical.blocking
    artifact = json.loads((tmp_path / "equivalence-diff.v1.json").read_text(encoding="utf-8"))
    assert artifact["schema_version"] == 1
