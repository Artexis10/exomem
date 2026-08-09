"""RM8: pre-result subset selection is deterministic, stratified, and hash-ordered."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

MINI = Path("benchmarks/lme/fixtures/mini.json")
LEAKY = Path("benchmarks/lme/fixtures/leaky.json")


def _rows(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_selection_is_deterministic_for_a_pinned_dataset_checksum() -> None:
    from equivalence.selection import select_question_ids

    rows, checksum = _rows(MINI), _sha(MINI)
    first = select_question_ids(rows, checksum)
    second = select_question_ids(list(reversed(rows)), checksum)
    assert first == second, "selection must not depend on input order"
    assert first["selection"]["dataset_sha256"] == checksum
    assert "sha256(question_id + dataset_sha256)" in first["selection"]["algorithm"]


def test_a_different_dataset_checksum_reorders_the_selection() -> None:
    from equivalence.selection import select_question_ids

    rows = _rows(MINI) + [
        {"question_id": f"synthetic-{index}", "question_type": "multi-session"} for index in range(6)
    ]
    pinned = select_question_ids(rows, "a" * 64)
    other = select_question_ids(rows, "b" * 64)
    assert pinned["question_ids"] != other["question_ids"], "the checksum must salt the hash order"
    assert sorted(pinned["selection"]["strata"]) == sorted(other["selection"]["strata"])


def test_strata_are_reported_and_capped_per_type_on_the_mini_fixture() -> None:
    from equivalence.selection import select_question_ids

    result = select_question_ids(_rows(MINI), _sha(MINI))
    strata = result["selection"]["strata"]
    # The mini fixture holds one question per type plus one abstention, so the
    # small-strata path must report the real counts, never the nominal 3/7.
    assert strata["abstention"] == 1
    assert all(count <= 3 for key, count in strata.items() if key != "abstention")
    assert sum(strata.values()) == len(result["question_ids"])
    assert len(set(result["question_ids"])) == len(result["question_ids"])
    abstentions = [item for item in result["question_ids"] if item.endswith("_abs")]
    assert len(abstentions) == 1


def test_full_strata_take_three_answerable_per_type_and_seven_abstentions() -> None:
    from equivalence.selection import select_question_ids

    rows = [
        {"question_id": f"q-{question_type}-{index}", "question_type": question_type}
        for question_type in ("multi-session", "temporal-reasoning")
        for index in range(5)
    ] + [{"question_id": f"q-abs-{index}_abs"} for index in range(9)]
    result = select_question_ids(rows, "c" * 64)
    strata = result["selection"]["strata"]
    assert strata == {"multi-session": 3, "temporal-reasoning": 3, "abstention": 7}
    assert len(result["question_ids"]) == 13


def test_the_leaky_fixture_selects_only_its_own_questions() -> None:
    from equivalence.selection import select_question_ids

    rows = _rows(LEAKY)
    result = select_question_ids(rows, _sha(LEAKY))
    assert set(result["question_ids"]) <= {row["question_id"] for row in rows}
    assert result["selection"]["strata"]["abstention"] == 0


def test_selection_refuses_rows_without_the_identity_it_orders_by() -> None:
    from equivalence.selection import select_question_ids

    with pytest.raises(ValueError, match="question_id"):
        select_question_ids([{"question_type": "multi-session"}], "d" * 64)
    with pytest.raises(ValueError, match="question_type"):
        select_question_ids([{"question_id": "q-1"}], "d" * 64)
