"""Static contract for the path-scoped real frozen-verifier workflow."""

from __future__ import annotations

from pathlib import Path

from exomem import claims


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "frozen-verifier.yml"


def test_real_verifier_workflow_is_revision_keyed_and_downloads_the_pin_manifest() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    pin = claims.VERIFIER_PINS[0]

    assert pin.model_revision in text
    assert "snapshot_download" in text
    assert "allow_patterns=list(pin.artifact_files)" in text
    assert "_hash_resident_snapshot(pin)" in text
    assert "--extra nli" in text
    assert "EXOMEM_RUN_REAL_NLI: \"1\"" in text
    assert "tests/test_frozen_verifier_real.py" in text


def test_real_verifier_workflow_is_scoped_to_identity_and_behavior_changes() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    for path in (
        "src/exomem/claims.py",
        "tests/test_frozen_verifier_real.py",
        "tests/test_frozen_verifiers.py",
        "pyproject.toml",
        "uv.lock",
        ".github/workflows/frozen-verifier.yml",
    ):
        assert path in text
    assert "schedule:" in text
    assert "workflow_dispatch:" in text
