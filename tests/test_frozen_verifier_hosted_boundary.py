"""Hosted must not inherit local frozen-verifier admission by accident."""

from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
CELL = ROOT / "infra" / "helm" / "cell"
MODEL_NAME = "MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7"


def test_hosted_image_contains_no_verifier_extra_or_artifact() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    hosted_stage = dockerfile.split("FROM builder-lean AS builder-hosted", 1)[1].split(
        "# Final: ml", 1
    )[0]

    assert '".[embeddings-onnx]"' in hosted_stage
    assert "[nli]" not in hosted_stage
    assert MODEL_NAME not in hosted_stage
    assert "model.safetensors" not in hosted_stage


def test_hosted_cell_has_no_verifier_grant_or_gate() -> None:
    for name in ("values.yaml", "values.initialize.yaml", "values.validation.yaml"):
        values = yaml.safe_load((CELL / name).read_text(encoding="utf-8"))
        grants = values["featureGrants"].split(",")
        assert grants == ["embeddings", "file-watcher"]
        assert not {"nli", "stance-verifier", "claim-polarity"} & set(grants)

    schema = json.loads((CELL / "values.schema.json").read_text(encoding="utf-8"))
    assert schema["properties"]["featureGrants"]["const"] == (
        "embeddings,file-watcher"
    )
    rendered_inputs = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (CELL / "templates").glob("*.yaml")
    )
    assert "EXOMEM_CLAIM_POLARITY_NLI" not in rendered_inputs


def test_hosted_boundary_records_the_rejected_quantized_candidate() -> None:
    boundary = (ROOT / "docs" / "hosted-inference-boundary.md").read_text(
        encoding="utf-8"
    )

    assert "323 MiB" in boundary
    assert "0.29 and 0.50" in boundary
    assert "0.93 symmetric threshold" in boundary
    assert "adds no `nli` extra" in boundary
    assert "Local admission does not grant Hosted admission" in boundary
