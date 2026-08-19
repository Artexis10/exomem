"""RM8: pre-result subset selection is deterministic, stratified, and hash-ordered."""

from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from benchmark_capabilities import has_no_follow_open, has_open_file_replacement

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


def test_canonical_lme_profile_requires_complete_known_strata_and_emits_closed_artifact() -> None:
    from equivalence.selection import (
        CANONICAL_LME_S_SOURCE,
        QUESTION_TYPE_ORDER,
        select_lme_s_25,
    )
    from protocol.models import LmeSelection

    census = CANONICAL_LME_S_SOURCE["type_census"]
    assert CANONICAL_LME_S_SOURCE["abstention_count"] == 30
    assert sum(census.values()) == CANONICAL_LME_S_SOURCE["row_count"] == 500
    rows = [
        {"question_id": f"{question_type}-{index}", "question_type": question_type}
        for question_type in QUESTION_TYPE_ORDER
        for index in range(census[question_type])
    ]
    for index in range(30):
        rows[index]["question_id"] += "_abs"
    artifact = select_lme_s_25(rows, source=CANONICAL_LME_S_SOURCE)

    assert artifact["artifact_type"] == "lme-selection.v1"
    assert artifact["selection_algorithm_version"] == "lme-s-25.sha256-v1"
    assert artifact["question_type_order"] == list(QUESTION_TYPE_ORDER)
    assert len(artifact["target_question_ids"]) == 25
    assert LmeSelection.model_validate(artifact).model_dump(mode="json") == artifact
    with pytest.raises(ValueError, match="row count"):
        select_lme_s_25(rows[:-1], source=CANONICAL_LME_S_SOURCE)


def test_canonical_lme_profile_refuses_abstention_census_drift() -> None:
    from equivalence.selection import CANONICAL_LME_S_SOURCE, QUESTION_TYPE_ORDER, select_lme_s_25

    census = CANONICAL_LME_S_SOURCE["type_census"]
    rows = [
        {"question_id": f"{question_type}-{index}", "question_type": question_type}
        for question_type in QUESTION_TYPE_ORDER
        for index in range(census[question_type])
    ]
    for index in range(CANONICAL_LME_S_SOURCE["abstention_count"] - 1):
        rows[index]["question_id"] += "_abs"

    with pytest.raises(ValueError, match="abstention census"):
        select_lme_s_25(rows, source=CANONICAL_LME_S_SOURCE)


def test_lme_selection_schema_has_exactly_the_model_closed_profile() -> None:
    from protocol.models import LmeSelection

    artifact = json.loads(Path("benchmarks/equivalence/subsets/lme-s-25.json").read_text())
    schema = Draft202012Validator(
        json.loads(Path("benchmarks/protocol/schema/lme-selection.v1.schema.json").read_text())
    )
    mutations: list[tuple[str, dict]] = []
    reordered = deepcopy(artifact)
    reordered["question_type_order"] = list(reversed(reordered["question_type_order"]))
    mutations.append(("type order", reordered))
    quota = deepcopy(artifact)
    quota["quotas"]["extra"] = 1
    mutations.append(("quota map", quota))
    census = deepcopy(artifact)
    census["source_identity"]["type_census"]["extra"] = 1
    mutations.append(("census map", census))
    target_count = deepcopy(artifact)
    target_count["target_question_ids"] = target_count["target_question_ids"][:-1]
    mutations.append(("target count", target_count))
    duplicate = deepcopy(artifact)
    duplicate["target_question_ids"][-1] = duplicate["target_question_ids"][0]
    mutations.append(("duplicate target", duplicate))
    blank = deepcopy(artifact)
    blank["target_question_ids"][-1] = ""
    mutations.append(("blank target", blank))

    for name, payload in mutations:
        model_valid = True
        try:
            LmeSelection.model_validate(payload)
        except ValueError:
            model_valid = False
        assert model_valid is False, name
        assert schema.is_valid(payload) is False, name


def test_frozen_selection_uses_one_no_follow_stable_read_for_bytes_hash_and_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from equivalence import selection

    source = Path("benchmarks/equivalence/subsets/lme-s-25.json")
    artifact = tmp_path / "lme-s-25.json"
    artifact.write_bytes(source.read_bytes())
    monkeypatch.setattr(selection, "CANONICAL_LME_S_25_ARTIFACT_PATH", artifact)
    monkeypatch.setattr(selection, "CANONICAL_LME_S_25_ARTIFACT_SHA256", _sha(artifact))
    loaded, raw = selection.load_frozen_lme_selection()
    assert raw == artifact.read_bytes()
    assert loaded["target_question_ids"]

    artifact.write_bytes(raw + b"\n")
    with pytest.raises(ValueError, match="frozen bytes"):
        selection.load_frozen_lme_selection()

    artifact.unlink()
    if has_no_follow_open():
        artifact.symlink_to(source.resolve())
        with pytest.raises(ValueError, match="no-follow regular"):
            selection.load_frozen_lme_selection()
        artifact.unlink()

    artifact.write_bytes(raw)
    if not has_open_file_replacement():
        return
    replacement = tmp_path / "replacement.json"
    replacement.write_bytes(raw)
    original_read = selection.os.read
    swapped = False

    def swap_after_read(descriptor: int, count: int) -> bytes:
        nonlocal swapped
        value = original_read(descriptor, count)
        if not swapped:
            swapped = True
            os.replace(replacement, artifact)
        return value

    monkeypatch.setattr(selection.os, "read", swap_after_read)
    with pytest.raises(ValueError, match="changed during stable read"):
        selection.load_frozen_lme_selection()


@pytest.mark.parametrize(
    "row, message",
    [
        ({"question_id": "", "question_type": "multi-session"}, "blank"),
        ({"question_id": "unknown_abs", "question_type": "unknown"}, "unknown"),
    ],
)
def test_canonical_lme_profile_refuses_blank_and_unknown_even_for_abstentions(row, message) -> None:
    from equivalence.selection import CANONICAL_LME_S_SOURCE, select_lme_s_25

    with pytest.raises(ValueError, match=message):
        select_lme_s_25([row], source=CANONICAL_LME_S_SOURCE)


def test_canonical_lme_profile_refuses_duplicate_identity_before_selection() -> None:
    from equivalence.selection import CANONICAL_LME_S_SOURCE, select_lme_s_25

    row = {"question_id": "duplicate", "question_type": "multi-session"}
    with pytest.raises(ValueError, match="duplicate"):
        select_lme_s_25([row, row], source=CANONICAL_LME_S_SOURCE)


def test_selection_output_is_idempotent_only_for_identical_regular_bytes(tmp_path: Path) -> None:
    from equivalence.cli import _exclusive_write

    destination = tmp_path / "selection.json"
    _exclusive_write(destination, b"frozen\n")
    _exclusive_write(destination, b"frozen\n")
    with pytest.raises(ValueError, match="different"):
        _exclusive_write(destination, b"altered\n")
    link = tmp_path / "link.json"
    link.symlink_to(destination)
    with pytest.raises(OSError):
        _exclusive_write(link, b"frozen\n")
