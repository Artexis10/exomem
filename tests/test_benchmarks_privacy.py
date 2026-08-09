from __future__ import annotations

from pathlib import Path

from exomem.public_artifact_privacy import assert_public_artifacts_clean


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
    files = _artifact_files(Path("benchmarks/epistemic"))
    assert files
    assert_public_artifacts_clean(files, labels={path: str(path) for path in files})
