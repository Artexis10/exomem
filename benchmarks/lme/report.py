"""Per-ability LongMemEval-S reporting with strict bounds gates."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Set
from pathlib import Path

from protocol.offline import offline_guard

from .dataset import QUESTION_TYPES, LmeDataset, load_dataset

ABSTENTION_ABILITY = "abstention"


def label_is_correct(value: object) -> bool:
    if value is True:
        return True
    if isinstance(value, (int, float)) and value == 1:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"correct", "pass", "passed", "true", "1", "yes"}
    return False


def manifest_banner(status: str, contamination: str | None, invalid_reason: str | None) -> str | None:
    """The one banner every report entry point renders, or None when the run is usable.

    Deriving it from the finalized terminal state — rather than from whichever
    artifact a given entry point happens to read — is what keeps the runner's
    report.md, the artifact-only regeneration, and the judge re-render one text.

    A VALID run whose canary state is merely ``unverifiable`` is not an
    environment fault: it stands on its own and is blocked only from a
    comparative table, which ``protocol.cli validate --strict`` enforces.
    """

    if status == "VALID" and contamination != "contaminated":
        return None
    parts = [f"manifest status={status}", f"contamination={contamination}"]
    if invalid_reason:
        parts.append(f"reason={invalid_reason}")
    return "; ".join(parts)


def _score(ids: list[str], labels: Mapping[str, object]) -> str:
    present = [question_id for question_id in ids if question_id in labels]
    if len(present) != len(ids):
        return "awaiting official judge"
    correct = sum(label_is_correct(labels[question_id]) for question_id in present)
    return f"{correct}/{len(ids)}"


def render_report(
    dataset: LmeDataset,
    *,
    labels: Mapping[str, object],
    ceiling_question_ids: Set[str],
    floor_question_ids: Set[str],
    ceiling_labels: Mapping[str, object] | None = None,
    floor_labels: Mapping[str, object] | None = None,
    invalid_reason: str | None = None,
    provider_variant: str | None = None,
) -> str:
    """Render only per-ability rows; missing bounds block the affected row."""

    from .judge_io import verified_judge_banner

    by_type: dict[str, list[str]] = defaultdict(list)
    for question in dataset.questions:
        ability = ABSTENTION_ABILITY if question.is_abstention else question.question_type
        by_type[ability].append(question.question_id)
    lines = [
        "# LongMemEval-S per-ability report",
        "",
        f"> {verified_judge_banner()}",
        "",
    ]
    if invalid_reason:
        lines.extend([f"INVALID environment fault: {invalid_reason}", ""])
    lines.extend(
        [
            ("| Ability | Variant | Questions | Score | Gold-evidence ceiling | Null-abstain floor | Status |"
             if provider_variant else "| Ability | Questions | Exomem | Gold-evidence ceiling | Null-abstain floor | Status |"),
            ("|---|---|---:|---:|---:|---:|---|" if provider_variant else "|---|---:|---:|---:|---:|---|"),
        ]
    )
    for ability in (*QUESTION_TYPES, ABSTENTION_ABILITY):
        ids = by_type.get(ability, [])
        if not ids:
            if ability in QUESTION_TYPES:
                lines.append(f"| {ability} | {provider_variant} | 0 | n/a | n/a | n/a | no non-abstention questions |" if provider_variant else f"| {ability} | 0 | n/a | n/a | n/a | no non-abstention questions |")
            continue
        missing_ceiling = [
            question_id for question_id in ids if question_id not in ceiling_question_ids
        ]
        missing_floor = [
            question_id for question_id in ids if question_id not in floor_question_ids
        ]
        if missing_ceiling or missing_floor:
            missing = []
            if missing_ceiling:
                missing.append("gold-evidence ceiling")
            if missing_floor:
                missing.append("null-abstain floor")
            status = "blocked: missing " + " and ".join(missing)
        elif invalid_reason:
            status = "invalid environment"
        elif not all(question_id in labels for question_id in ids):
            status = "awaiting official judge"
        elif ceiling_labels is None or floor_labels is None:
            status = "awaiting official judge for bounds"
        elif not all(
            question_id in ceiling_labels and question_id in floor_labels
            for question_id in ids
        ):
            status = "awaiting official judge for bounds"
        elif any(not label_is_correct(ceiling_labels[question_id]) for question_id in ids):
            status = "harness-bounded"
        else:
            status = "bounded result"
        row = (ability, str(len(ids)), _score(ids, labels), _score(ids, ceiling_labels or {}), _score(ids, floor_labels or {}), status)
        lines.append("| " + " | ".join((ability, provider_variant, *row[1:]) if provider_variant else row) + " |")
    lines.append("")
    return "\n".join(lines)


def render_run_report(run_dir: Path | str, *, offline: bool = False) -> str:
    """Regenerate an LME report solely from a terminal protocol manifest and artifacts.

    A non-VALID or contaminated manifest renders its status prominently; a
    hypothesis is an answer record, never a judge verdict, so the stub path
    renders "awaiting official judge" instead of a fabricated label.
    """

    from protocol.manifest import ManifestError, load_manifest

    try:
        manifest = load_manifest(run_dir)
    except ManifestError as exc:
        raise ValueError(str(exc)) from exc

    root = Path(run_dir)
    validate_selection_evidence(root, manifest=manifest)

    def render() -> str:
        from .judge_io import render_from_artifacts

        banner = manifest_banner(manifest.status, manifest.contamination, manifest.invalid_reason)
        return render_from_artifacts(root, invalid_reason=banner, provider_variant=manifest.provider_variant)

    if offline:
        with offline_guard():
            return render()
    return render()


def validate_selection_evidence(run_dir: Path | str, *, manifest=None) -> None:
    """Fail closed for every 25-row artifact, independent of mutable claims."""
    from protocol.manifest import load_manifest

    root = Path(run_dir)
    manifest = manifest or load_manifest(root)
    dataset = load_dataset(root / "dataset.json")
    if len(dataset.questions) != 25:
        return
    environment_path = root / "environment.json"
    environment = json.loads(environment_path.read_text(encoding="utf-8")) if environment_path.is_file() else {}
    lme = environment.get("lme") if isinstance(environment, dict) else None
    ids = [question.question_id for question in dataset.questions]
    from equivalence.selection import load_frozen_lme_selection
    artifact, raw = load_frozen_lme_selection()
    canonical_set = set(artifact["target_question_ids"])
    if set(ids) == canonical_set:
        if lme.get("selection_mode") != "canonical":
            raise ValueError("canonical selection cannot be downgraded")
    elif lme.get("selection_mode") == "generic-pilot":
        if lme.get("pilot", {}).get("size") != 25 or lme.get("pilot", {}).get("question_ids") != ids:
            raise ValueError("generic selection evidence differs")
        return
    if not isinstance(lme, dict) or lme.get("selection_mode") not in {"canonical", "generic-pilot"}:
        raise ValueError("25-question selection evidence is missing")
    if lme.get("canonical_selection") is not True:
        raise ValueError("canonical selection evidence mode differs")
    from equivalence.selection import CANONICAL_LME_S_SOURCE
    from protocol.models import DatasetIdentity
    expected_identity = DatasetIdentity(
        id="longmemeval", variant="LongMemEval-S cleaned September 2025",
        source="xiaowu0162/longmemeval-cleaned", revision=CANONICAL_LME_S_SOURCE["revision"],
        sha256=CANONICAL_LME_S_SOURCE["sha256"], case_count=CANONICAL_LME_S_SOURCE["row_count"],
    ).model_dump(mode="json")
    if manifest.dataset.model_dump(mode="json") != expected_identity:
        raise ValueError("canonical selection manifest dataset identity differs")
    expected = {
        "selection_artifact_path": "benchmarks/equivalence/subsets/lme-s-25.json",
        "selection_artifact_sha256": hashlib.sha256(raw).hexdigest(),
        "selection_algorithm_version": artifact["selection_algorithm_version"],
    }
    if manifest.pins != expected or lme.get("selection") != expected:
        raise ValueError("canonical selection evidence pins differ")
    if ids != artifact["target_question_ids"]:
        raise ValueError("canonical selection dataset IDs differ")


render_run_report.offline_guard = offline_guard  # type: ignore[attr-defined]
