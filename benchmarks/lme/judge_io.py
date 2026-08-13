"""Official LongMemEval judge handoff and label ingestion."""

from __future__ import annotations

import json
import shlex
import shutil
from pathlib import Path

from .dataset import load_dataset
from .report import manifest_banner, render_report


LANE_FILES = {
    "main": ("hypotheses.jsonl", "judge-labels.jsonl"),
    "ceiling": (
        "bounds/gold-evidence-ceiling.jsonl",
        "bounds/gold-evidence-ceiling-labels.jsonl",
    ),
    "floor": (
        "bounds/null-abstain-floor.jsonl",
        "bounds/null-abstain-floor-labels.jsonl",
    ),
}


def official_judge_commands(run_dir: Path, *, judge_model: str = "gpt-4o") -> str:
    """Return user-run commands; this package never invokes the official judge."""

    run_dir = Path(run_dir).resolve()
    dataset = shlex.quote(str(run_dir / "dataset.json"))
    commands = [
        "# UNVERIFIED: confirm these flags against the fetched official "
        "evaluate_qa.py before judging."
    ]
    for input_name, output_name in LANE_FILES.values():
        commands.append(
            "python evaluate_qa.py "
            f"--dataset_file {dataset} "
            f"--hypothesis_file {shlex.quote(str(run_dir / input_name))} "
            f"--output_file {shlex.quote(str(run_dir / output_name))} "
            f"--model {shlex.quote(judge_model)}"
        )
    return "\n".join(commands) + "\n"


def _verdict_value(value: object) -> object:
    if not isinstance(value, dict):
        return value
    for key in ("autoeval_label", "label", "judgment", "correct", "value"):
        if key in value:
            return _verdict_value(value[key])
    boolean_values = [item for item in value.values() if isinstance(item, bool)]
    if len(boolean_values) == 1:
        return boolean_values[0]
    return value


def load_labels(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    labels: dict[str, object] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
        if not isinstance(row, dict) or not isinstance(row.get("question_id"), str):
            raise ValueError(f"{path}:{line_number}: label row requires question_id")
        question_id = row["question_id"]
        if question_id in labels:
            raise ValueError(f"{path}:{line_number}: duplicate question_id {question_id!r}")
        for key in ("autoeval_label", "label", "judgment", "correct"):
            if key in row:
                labels[question_id] = _verdict_value(row[key])
                break
        else:
            raise ValueError(f"{path}:{line_number}: label row has no verdict field")
    return labels


def _ids(path: Path) -> set[str]:
    ids: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            ids.add(row["question_id"])
    return ids


def _bound_ids(run_dir: Path, lane: str) -> set[str]:
    ids = _ids(run_dir / LANE_FILES[lane][0])
    phase = {
        "ceiling": "gold-evidence-ceiling-reader",
        "floor": "null-abstain-floor-reader",
    }[lane]
    failures_path = run_dir / "failures.jsonl"
    if not failures_path.is_file():
        return ids
    for line in failures_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        failure = json.loads(line)
        if failure.get("phase") == phase:
            ids.discard(str(failure.get("question_id")))
    return ids


def render_from_artifacts(run_dir: Path | str, *, invalid_reason: str | None, provider_variant: str | None) -> str:
    """The single renderer every report entry point uses.

    Labels come from the real judge artifacts when they exist; a lane with no
    verdict renders "awaiting official judge" rather than a fabricated False.
    """

    run_dir = Path(run_dir)
    from .report import validate_selection_evidence
    validate_selection_evidence(run_dir)
    ceiling_labels_path = run_dir / LANE_FILES["ceiling"][1]
    floor_labels_path = run_dir / LANE_FILES["floor"][1]
    return render_report(
        load_dataset(run_dir / "dataset.json"),
        labels=load_labels(run_dir / LANE_FILES["main"][1]),
        ceiling_question_ids=_bound_ids(run_dir, "ceiling"),
        floor_question_ids=_bound_ids(run_dir, "floor"),
        ceiling_labels=load_labels(ceiling_labels_path) if ceiling_labels_path.is_file() else None,
        floor_labels=load_labels(floor_labels_path) if floor_labels_path.is_file() else None,
        invalid_reason=invalid_reason,
        provider_variant=provider_variant,
    )


def rerender_report(run_dir: Path) -> None:
    """Re-render from the finalized protocol manifest, the authoritative terminal record.

    Reading the manifest rather than run.json (its legacy mirror) is what keeps
    this entry point byte-identical to the runner's own report.md and to
    render_run_report; every run this package writes finalizes a manifest, so a
    missing or non-terminal one is a real fault and is raised, not papered over.
    """

    run_dir = Path(run_dir)
    from protocol.manifest import load_manifest

    manifest = load_manifest(run_dir)
    (run_dir / "report.md").write_text(
        render_from_artifacts(
            run_dir,
            invalid_reason=manifest_banner(manifest.status, manifest.contamination, manifest.invalid_reason),
            provider_variant=manifest.provider_variant,
        ),
        encoding="utf-8",
    )


def ingest_judge_labels(run_dir: Path | str, labels: Path | str, *, lane: str = "main") -> Path:
    """Validate and preserve one official label JSONL, then refresh the report."""

    if lane not in LANE_FILES:
        raise ValueError(f"unknown judge lane {lane!r}; choose from {sorted(LANE_FILES)}")
    run_dir = Path(run_dir)
    from .report import validate_selection_evidence
    validate_selection_evidence(run_dir)
    source = Path(labels)
    parsed = load_labels(source)
    dataset_ids = {
        question.question_id
        for question in load_dataset(run_dir / "dataset.json").questions
    }
    unknown = set(parsed) - dataset_ids
    if unknown:
        raise ValueError(f"judge labels contain unknown question ids: {sorted(unknown)}")
    destination = run_dir / LANE_FILES[lane][1]
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"judge artifact is immutable and already exists: {destination}")
    shutil.copyfile(source, destination)
    rerender_report(run_dir)
    return destination
