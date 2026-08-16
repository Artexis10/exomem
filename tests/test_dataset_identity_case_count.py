"""`case_count` describes the pinned dataset, not what our parser accepted.

`DatasetIdentity` binds a `sha256` computed over the whole source file to a
`case_count`. Deriving that count from the rows we successfully loaded makes the
pair self-contradictory the moment any row is deferred: a digest of 500 rows
labelled as 493 cases.

This is fallout from scoped deferral. Before it, an unparseable row raised, so
"rows loaded" and "rows in the file" were necessarily equal and either could
stand in for the other. Once a row could be carried instead of raised, the two
diverged silently — and the census exists precisely because the frozen source,
not our parse of it, is the thing being identified.

The guest lane found this: MemoryBench validates the plan's `case_count` against
the real file and refused, correctly. Had we matched the plan to the run instead,
`dataset_identity` would have differed across the two lanes under an equivalence
comparison, for a reason that has nothing to do with retrieval.

Deferral only survives the canonical-selection path, which requires the real
frozen digest, so the divergence cannot be reached from a fixture end-to-end.
These tests pin the derivation itself and guard the run that must not change.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from lme.dataset import LmeDataset, load_dataset_bytes
from lme.reader import StubReader
from lme.runner import RunConfig, _dataset_case_count, execute_run


def _row(question_id: str, **overrides):
    row = {
        "question_id": question_id,
        "question_type": "single-session-user",
        "question": "which lantern?",
        "answer": "the violet one",
        "question_date": "2026-01-01",
        "haystack_session_ids": ["s1"],
        "haystack_sessions": [[{"role": "user", "content": "a turn"}]],
        "haystack_dates": ["2026-01-01"],
        "answer_session_ids": ["s1"],
    }
    row.update(overrides)
    return row


def test_a_deferred_row_still_counts_toward_the_dataset_identity() -> None:
    """The number must match the file the sha256 was taken over."""
    rows = [_row("keep"), _row("broken", question_type="not-a-real-type")]
    dataset = load_dataset_bytes(json.dumps(rows).encode("utf-8"))

    assert len(dataset.questions) == 1
    assert _dataset_case_count(dataset) == 2


def test_the_count_is_unchanged_when_nothing_is_deferred() -> None:
    rows = [_row("keep-1"), _row("keep-2")]
    dataset = load_dataset_bytes(json.dumps(rows).encode("utf-8"))

    assert _dataset_case_count(dataset) == len(dataset.questions) == 2


def test_a_dataset_built_without_a_census_falls_back_to_its_questions() -> None:
    """Pilot slices and fixtures are constructed directly, not loaded.

    They carry no census, and for them the questions *are* every row there is,
    so the fallback must not report zero.
    """
    rows = [_row("keep-1"), _row("keep-2")]
    loaded = load_dataset_bytes(json.dumps(rows).encode("utf-8"))
    detached = LmeDataset(loaded.questions)

    assert detached.census == ()
    assert _dataset_case_count(detached) == 2


def test_a_normal_run_records_the_source_row_count(tmp_path: Path) -> None:
    """End-to-end guard: the healthy path keeps the value it always had."""
    rows = [_row("keep-1"), _row("keep-2")]
    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text(json.dumps(rows), encoding="utf-8")

    execute_run(
        RunConfig(
            dataset=dataset_path,
            out=tmp_path / "out",
            run_id="run",
            dataset_sha256=hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
            dataset_revision="test-pin",
            pilot=1,
        ),
        reader=StubReader(),
    )

    run_dir = next((tmp_path / "out").iterdir())
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    # A pilot scores one case, but identity still describes the whole source.
    assert manifest["dataset"]["case_count"] == 2
