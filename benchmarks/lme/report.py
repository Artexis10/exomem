"""Per-ability LongMemEval-S reporting with strict bounds gates."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Set

from .dataset import LmeDataset, QUESTION_TYPES


ABSTENTION_ABILITY = "abstention"


def label_is_correct(value: object) -> bool:
    if value is True:
        return True
    if isinstance(value, (int, float)) and value == 1:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"correct", "pass", "passed", "true", "1", "yes"}
    return False


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
) -> str:
    """Render only per-ability rows; missing bounds block the affected row."""

    by_type: dict[str, list[str]] = defaultdict(list)
    for question in dataset.questions:
        ability = ABSTENTION_ABILITY if question.is_abstention else question.question_type
        by_type[ability].append(question.question_id)
    lines = [
        "# LongMemEval-S per-ability report",
        "",
        "> UNVERIFIED judge command: confirm the emitted evaluate_qa.py flags against "
        "the fetched official suite before judging.",
        "",
    ]
    if invalid_reason:
        lines.extend([f"INVALID environment fault: {invalid_reason}", ""])
    lines.extend(
        [
            "| Ability | Questions | Exomem | Gold-evidence ceiling | "
            "Null-abstain floor | Status |",
            "|---|---:|---:|---:|---:|---|",
        ]
    )
    for ability in (*QUESTION_TYPES, ABSTENTION_ABILITY):
        ids = by_type.get(ability, [])
        if not ids:
            if ability in QUESTION_TYPES:
                lines.append(
                    f"| {ability} | 0 | n/a | n/a | n/a | "
                    "no non-abstention questions |"
                )
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
        lines.append(
            "| "
            + " | ".join(
                (
                    ability,
                    str(len(ids)),
                    _score(ids, labels),
                    _score(ids, ceiling_labels or {}),
                    _score(ids, floor_labels or {}),
                    status,
                )
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)
