"""RM2 / RM3: the twelve-key differ, its tiers, its nulls, and its exceptions."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

import yaml

FIXTURE = Path("benchmarks/equivalence/fixtures/perturbed-twin")


def _exceptions_file(tmp_path: Path, **overrides) -> Path:
    entry = {
        "case_id": "twin-all-keys",
        "field": "answer_judge_prompt_model_config",
        "compare_as": "same-shape",
        "evidence": "upstream reader model differs by design between the two rows",
        "approver": "benchmark-owner",
        "expires_at": "2099-01-01",
    }
    entry.update(overrides)
    path = tmp_path / "exceptions.yaml"
    path.write_text(yaml.safe_dump([entry], sort_keys=True), encoding="utf-8")
    return path


def test_the_gate_enumerates_all_twelve_classifications_on_the_perturbed_twin(tmp_path: Path) -> None:
    from equivalence.differ import EQUIVALENCE_KEYS, KEY_CLASSIFICATION, compare_runs

    result = compare_runs(FIXTURE / "left", FIXTURE / "right", mode="blocking", out=tmp_path)
    planted = [diff for diff in result.diffs if diff.case_id == "twin-all-keys"]
    assert {diff.field for diff in planted} == set(EQUIVALENCE_KEYS)
    assert len(planted) == 12
    assert {diff.field: diff.classification for diff in planted} == KEY_CLASSIFICATION
    assert sum(1 for tier in KEY_CLASSIFICATION.values() if tier == "blocking") == 9
    assert sum(1 for tier in KEY_CLASSIFICATION.values() if tier == "reported") == 3
    assert all(diff.explanation_required for diff in planted)
    assert result.blocking


def test_null_never_equals_null_and_demands_an_explanation(tmp_path: Path) -> None:
    from equivalence.differ import compare_runs

    result = compare_runs(FIXTURE / "left", FIXTURE / "right", mode="blocking", out=tmp_path)
    null_case = [diff for diff in result.diffs if diff.case_id == "twin-null"]
    assert [diff.field for diff in null_case] == ["packed_context"]
    only = null_case[0]
    assert (only.expected, only.actual) == (None, None), "both sides are null and still differ"
    assert only.explanation_required
    assert only.classification == "reported"


def test_identical_runs_produce_no_difference(tmp_path: Path) -> None:
    from equivalence.differ import compare_runs

    result = compare_runs(FIXTURE / "left", FIXTURE / "left", mode="blocking", out=tmp_path)
    assert [diff.field for diff in result.diffs] == ["packed_context"], "only the deliberate null case remains"
    assert not result.blocking


@pytest.mark.parametrize(("mode", "expected_exit"), [("report", 0), ("blocking", 1)])
def test_both_modes_run_and_only_blocking_refuses(tmp_path: Path, mode: str, expected_exit: int) -> None:
    """RB1: the CLI's vocabulary reaches the differ in both directions."""

    from equivalence import cli

    out = tmp_path / mode
    exit_code = cli.main(
        ["gate", "--left", str(FIXTURE / "left"), "--right", str(FIXTURE / "right"), "--mode", mode, "--out", str(out)]
    )
    assert exit_code == expected_exit
    artifact = json.loads((out / "equivalence-diff.v1.json").read_text(encoding="utf-8"))
    assert artifact["mode"] == ("reported" if mode == "report" else "blocking")
    assert len(artifact["diffs"]) == 13, "report mode records every mismatch and proceeds"
    assert all(diff["explanation_required"] for diff in artifact["diffs"])
    assert (out / "equivalence-diff.md").is_file()


def test_the_gate_writes_beside_the_left_run_when_no_out_is_given(tmp_path: Path) -> None:
    """Minor: --out defaults to the run dir, never the operator's cwd."""

    from equivalence import cli

    left, right = tmp_path / "left", tmp_path / "right"
    for run in (left, right):
        run.mkdir()
        (run / "equivalence.json").write_bytes((FIXTURE / run.name / "equivalence.json").read_bytes())
    assert cli.main(["gate", "--left", str(left), "--right", str(right), "--mode", "report"]) == 0
    assert (left / "equivalence-diff.v1.json").is_file()


def test_an_active_exception_applies_its_weaker_predicate_and_clears_the_explanation(tmp_path: Path) -> None:
    from equivalence.differ import compare_runs

    result = compare_runs(
        FIXTURE / "left", FIXTURE / "right", mode="blocking", out=tmp_path,
        exceptions_path=_exceptions_file(tmp_path), today=dt.date(2026, 8, 9),
    )
    rescued = next(diff for diff in result.diffs if diff.field == "answer_judge_prompt_model_config")
    assert rescued.compare_as == "same-shape"
    assert rescued.classification == "reported", "an approved weaker predicate downgrades, never erases"
    assert rescued.explanation_required is False
    assert any(diff.field == "top_k" and diff.classification == "blocking" for diff in result.diffs)


def test_a_registered_exception_whose_weaker_predicate_fails_stays_unexplained(tmp_path: Path) -> None:
    from equivalence.differ import compare_runs

    result = compare_runs(
        FIXTURE / "left", FIXTURE / "right", mode="blocking", out=tmp_path,
        exceptions_path=_exceptions_file(tmp_path, field="top_k", compare_as="numeric-within-1"),
        today=dt.date(2026, 8, 9),
    )
    attempted = next(diff for diff in result.diffs if diff.field == "top_k")
    assert attempted.compare_as == "numeric-within-1"
    assert attempted.classification == "blocking"
    assert attempted.explanation_required is True


def test_an_expired_exception_leaves_the_difference_unexplained(tmp_path: Path) -> None:
    from equivalence.differ import compare_runs

    result = compare_runs(
        FIXTURE / "left", FIXTURE / "right", mode="blocking", out=tmp_path,
        exceptions_path=_exceptions_file(tmp_path, expires_at="2026-01-01"), today=dt.date(2026, 8, 9),
    )
    expired = next(diff for diff in result.diffs if diff.field == "answer_judge_prompt_model_config")
    assert expired.compare_as is None
    assert expired.explanation_required is True
    assert expired.classification == "blocking"


def test_applying_exceptions_without_a_date_is_refused(tmp_path: Path) -> None:
    from equivalence.differ import compare_runs

    with pytest.raises(ValueError, match="today is required"):
        compare_runs(FIXTURE / "left", FIXTURE / "right", mode="blocking", out=tmp_path, exceptions_path=_exceptions_file(tmp_path))


def test_the_register_refuses_skip_and_unknown_predicates(tmp_path: Path) -> None:
    from equivalence.exceptions import load_exceptions

    with pytest.raises(ValueError, match="never skip"):
        load_exceptions(_exceptions_file(tmp_path, compare_as="skip"))
    with pytest.raises(ValueError, match="unknown compare_as"):
        load_exceptions(_exceptions_file(tmp_path, compare_as="whatever-i-feel-like"))
    path = tmp_path / "incomplete.yaml"
    path.write_text(yaml.safe_dump([{"case_id": "c", "field": "top_k"}]), encoding="utf-8")
    with pytest.raises(ValueError, match="require case_id"):
        load_exceptions(path)
    assert load_exceptions(Path("benchmarks/equivalence/exceptions.yaml")) == []


def test_expiry_requires_the_callers_date_and_never_assumes_today(tmp_path: Path) -> None:
    """Minor: library code must not default to date.today()."""

    from equivalence.exceptions import load_exceptions

    entry = load_exceptions(_exceptions_file(tmp_path))[0]
    with pytest.raises(TypeError, match="requires the caller's date"):
        entry.active("2026-08-09")  # type: ignore[arg-type]
    assert entry.active(dt.date(2026, 8, 9)) is True
    assert load_exceptions(_exceptions_file(tmp_path, expires_at="2026-01-01"))[0].active(dt.date(2026, 8, 9)) is False


def test_set_keys_are_order_insensitive_while_ordered_keys_are_not(tmp_path: Path) -> None:
    """RM2: case_set and retrieved_ids compare as sets; retrieved_text keeps its order."""

    from equivalence.differ import compare_runs

    payload = json.loads((FIXTURE / "left" / "equivalence.json").read_text(encoding="utf-8"))
    left, right = tmp_path / "left", tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "equivalence.json").write_text(json.dumps(payload), encoding="utf-8")
    shuffled = json.loads(json.dumps(payload))
    case = shuffled["cases"][0]
    case["case_set"] = list(reversed(case["case_set"]))
    case["retrieved_ids"] = ["chunk-b", "chunk-a"]
    case["retrieved_text"] = ["second", "first"]
    payload["cases"][0]["retrieved_ids"] = ["chunk-a", "chunk-b"]
    payload["cases"][0]["retrieved_text"] = ["first", "second"]
    (left / "equivalence.json").write_text(json.dumps(payload), encoding="utf-8")
    (right / "equivalence.json").write_text(json.dumps(shuffled), encoding="utf-8")
    result = compare_runs(left, right, mode="report", out=tmp_path / "out")
    fields = {diff.field for diff in result.diffs if diff.case_id == "twin-all-keys"}
    assert "case_set" not in fields, "case_set must compare order-insensitively"
    assert "retrieved_ids" not in fields, "retrieved_ids must compare order-insensitively"
    assert "retrieved_text" in fields, "retrieved_text order is part of the measurement"


def test_whitespace_is_normalized_before_text_keys_are_compared(tmp_path: Path) -> None:
    from equivalence.differ import compare_runs

    payload = json.loads((FIXTURE / "left" / "equivalence.json").read_text(encoding="utf-8"))
    left, right = tmp_path / "left", tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "equivalence.json").write_text(json.dumps(payload), encoding="utf-8")
    respaced = json.loads(json.dumps(payload))
    respaced["cases"][0]["exact_query"] = "  Which   city hosted\tthe autumn workshop?  "
    (right / "equivalence.json").write_text(json.dumps(respaced), encoding="utf-8")
    result = compare_runs(left, right, mode="report", out=tmp_path / "out")
    assert "exact_query" not in {diff.field for diff in result.diffs if diff.case_id == "twin-all-keys"}


def test_the_namespace_normalizer_surfaces_a_malformed_namespace(tmp_path: Path) -> None:
    from equivalence.differ import compare_runs

    left, right = tmp_path / "left", tmp_path / "right"
    payload = json.loads((FIXTURE / "left" / "equivalence.json").read_text(encoding="utf-8"))
    for run, namespace in ((left, "hybrid-run-24hex"), (right, "Hybrid Run/24hex")):
        run.mkdir()
        clone = json.loads(json.dumps(payload))
        clone["cases"][0]["namespace"] = namespace
        (run / "equivalence.json").write_text(json.dumps(clone), encoding="utf-8")
    result = compare_runs(left, right, mode="report", out=tmp_path / "out")
    namespace_diff = next(diff for diff in result.diffs if diff.field == "namespace")
    assert "invalid_namespace" in str(namespace_diff.actual)
