from __future__ import annotations

from pathlib import Path

import pytest

from exomem.public_artifact_privacy import (
    PublicArtifactPrivacyError,
    assert_public_artifacts_clean,
)


def _artifact_files(root: Path) -> list[Path]:
    return [
        path
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    ]


def test_protocol_benchmark_artifacts_are_public_safe() -> None:
    roots = (Path("benchmarks/protocol"), Path("benchmarks/lme/providers"), Path("benchmarks/equivalence"))
    files = [path for root in roots for path in _artifact_files(root)]
    assert files
    assert_public_artifacts_clean(files, labels={path: str(path) for path in files})


def test_epistemic_benchmark_artifacts_are_public_safe() -> None:
    receipt = Path("benchmarks/epistemic/contracts/ratification.v1.json")
    files = [path for path in _artifact_files(Path("benchmarks/epistemic")) if path != receipt]
    assert files
    assert_public_artifacts_clean(files, labels={path: str(path) for path in files})
    # Founder identity is a required signed-contract field, not run evidence or
    # a leaked local path. Keep the narrow exception bound to the immutable receipt.
    assert "Hugo Ander Kivi" in receipt.read_text(encoding="utf-8")


def test_memorybench_benchmark_artifacts_are_public_safe() -> None:
    files = _artifact_files(Path("benchmarks/memorybench"))
    assert files
    assert_public_artifacts_clean(files, labels={path: str(path) for path in files})


def test_suites_benchmark_artifacts_are_public_safe() -> None:
    files = _artifact_files(Path("benchmarks/suites"))
    assert files
    assert_public_artifacts_clean(files, labels={path: str(path) for path in files})


def test_the_suites_privacy_gate_rejects_an_injected_absolute_path(tmp_path: Path) -> None:
    """R6: the gate that covers benchmarks/suites actually catches a leak."""

    # Built by concatenation, not a literal: a contiguous absolute path here
    # would itself trip the repository-wide privacy gate on this test file.
    canary = "/" + "home" + "/" + "maintainer" + "/" + "checkout"
    leaked = tmp_path / "LOCKFILE.json"
    leaked.write_text(f'{{"notes": "captured from {canary}"}}\n', encoding="utf-8")

    with pytest.raises(PublicArtifactPrivacyError):
        assert_public_artifacts_clean(
            (leaked,), labels={leaked: "benchmarks/suites/example/LOCKFILE.json"}
        )
