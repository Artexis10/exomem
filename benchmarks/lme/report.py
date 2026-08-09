"""Per-ability LongMemEval-S reporting with strict bounds gates."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Set
from contextlib import contextmanager
import json
import socket
from pathlib import Path

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


@contextmanager
def offline_guard():
    """Make artifact-only report regeneration fail loudly on any network use."""

    original = socket.socket.connect

    def refused(self, address):  # type: ignore[no-untyped-def]
        del self, address
        raise OSError("offline report generation forbids socket.connect")

    socket.socket.connect = refused
    try:
        yield
    finally:
        socket.socket.connect = original


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

    def render() -> str:
        from .judge_io import render_from_artifacts

        banner = manifest_banner(manifest.status, manifest.contamination, manifest.invalid_reason)
        return render_from_artifacts(root, invalid_reason=banner, provider_variant=manifest.provider_variant)

    if offline:
        with offline_guard():
            return render()
    return render()


render_run_report.offline_guard = offline_guard  # type: ignore[attr-defined]
