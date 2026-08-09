"""Quantitative family (t18): unit-aware Decimal evaluator, family activation,
spec-scenario golden expectations, and t18-only determinism.

Red-first suite for OpenSpec change ``expand-memory-proof-benchmark`` task 3.2
(spec requirement "Procedural And Quantitative Reasoning Families", scenario
"Derived quantity with units").
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from membench import families, quant
from membench.generate import generate_corpus
from membench.schema import (
    ClaimRecord,
    ExpectedRecord,
    QueryRecord,
    SourceRecord,
    TypedValue,
    load_jsonl,
)
from membench.scoring.answer_contract import AnswerRecord
from membench.scoring.gates import (
    GateStatus,
    ScoringContext,
    gate_citations,
    gate_value,
)
from membench.templates import registry

T18 = "t18_quantitative"


def _q(value: str, unit: str | None = None) -> TypedValue:
    return TypedValue(kind="quantity", value=value, unit=unit)


# -- (a) evaluator: exact Decimal conversions, unknown units, tolerance ----


def test_conversions_are_exact_decimals() -> None:
    assert quant.convert_value(Decimal("800"), "g", "kg") == Decimal("0.8")
    assert quant.convert_value(Decimal("3.4"), "kg", "g") == Decimal("3400")
    assert quant.convert_value(Decimal("2500"), "m", "km") == Decimal("2.5")
    assert quant.convert_value(Decimal("1.25"), "km", "m") == Decimal("1250")
    assert quant.convert_value(Decimal("90"), "min", "h") == Decimal("1.5")
    assert quant.convert_value(Decimal("2.5"), "h", "min") == Decimal("150")


def test_unknown_unit_pair_raises() -> None:
    with pytest.raises(quant.QuantityError, match="lb"):
        quant.convert_value(Decimal("1"), "kg", "lb")
    with pytest.raises(quant.QuantityError, match="furlong"):
        quant.derive_sum(_q("1", "furlong"), _q("2", "m"))


def test_cross_dimension_conversion_raises() -> None:
    with pytest.raises(quant.QuantityError, match="dimension"):
        quant.convert_value(Decimal("1"), "kg", "min")
    with pytest.raises(quant.QuantityError, match="dimension"):
        quant.derive_sum(_q("1", "kg"), _q("5", "min"))


def test_same_unit_sum_is_exact_with_zero_tolerance() -> None:
    derived = quant.derive_sum(_q("35", "min"), _q("40", "min"))
    assert (derived.value, derived.unit) == ("75", "min")
    assert isinstance(derived.tolerance, Decimal)
    assert derived.tolerance == Decimal("0")


def test_conversion_sum_matches_spec_scenario_shape() -> None:
    derived = quant.derive_sum(_q("3.4", "kg"), _q("800", "g"), unit="kg")
    assert (derived.value, derived.unit) == ("4.2", "kg")
    assert derived.tolerance == Decimal("0")


def test_difference_ratio_and_scale() -> None:
    diff = quant.derive_difference(_q("2", "km"), _q("400", "m"), unit="m")
    assert (diff.value, diff.unit) == ("1600", "m")
    ratio = quant.derive_ratio(_q("180", "points"), _q("120", "points"), places=1)
    assert (ratio.value, ratio.unit) == ("1.5", None)
    assert ratio.tolerance == Decimal("0.05")
    scaled = quant.derive_scale(_q("1.5", "h"), 4, unit="min")
    assert (scaled.value, scaled.unit) == ("360", "min")


def test_non_terminating_result_requires_places() -> None:
    with pytest.raises(quant.QuantityError, match="places"):
        quant.derive_ratio(_q("1", "kg"), _q("3", "kg"))
    rounded = quant.derive_ratio(_q("1", "kg"), _q("3", "kg"), places=2)
    assert rounded.value == "0.33"
    assert rounded.tolerance == Decimal("0.005")


def test_tolerance_is_honored_inclusively() -> None:
    derived = quant.derive_ratio(_q("100", "m"), _q("40", "m"), places=1)
    assert (derived.value, derived.tolerance) == ("2.5", Decimal("0.05"))
    assert quant.within_tolerance(derived, "2.5")
    assert quant.within_tolerance(derived, "2.52")
    assert quant.within_tolerance(derived, "2.55")  # boundary is inclusive
    assert not quant.within_tolerance(derived, "2.56")
    exact = quant.derive_sum(_q("35", "min"), _q("40", "min"))
    assert quant.within_tolerance(exact, "75")
    assert quant.within_tolerance(exact, "75.0")
    assert not quant.within_tolerance(exact, "75.1")


def test_float_inputs_are_rejected() -> None:
    with pytest.raises(quant.QuantityError, match="float"):
        quant.derive_scale(_q("1.5", "h"), 4.0)  # type: ignore[arg-type]
    derived = quant.derive_sum(_q("35", "min"), _q("40", "min"))
    with pytest.raises(quant.QuantityError, match="float"):
        quant.within_tolerance(derived, 75.0)  # type: ignore[arg-type]


def test_canonical_never_emits_e_notation_for_small_magnitudes() -> None:
    """``_canonical`` must format plain decimal notation across the range it
    can produce, including below the ~1e-6 threshold where default Decimal
    string conversion switches to scientific notation (spurious "1E-7" vs
    "0.0000001" mismatches otherwise)."""

    cases = {
        Decimal("0.0000001"): "0.0000001",  # 1e-7: below the -6 threshold
        Decimal("0.00000012340"): "0.0000001234",  # trailing zeros stripped too
        Decimal("-0.0000001"): "-0.0000001",  # sign preserved
        Decimal("1.234E-9"): "0.000000001234",  # already-scientific input
        Decimal("4.20"): "4.2",  # unaffected case still normalizes as before
        Decimal("0.5"): "0.5",
    }
    for value, expected in cases.items():
        canonical = quant._canonical(value)
        assert canonical == expected
        assert "E" not in canonical and "e" not in canonical


def test_derive_ratio_small_magnitude_result_has_no_e_notation() -> None:
    """End-to-end: a derivation whose quantized result falls below 1e-6 still
    round-trips through Decimal and never leaks scientific notation."""

    derived = quant.derive_ratio(_q("1", "points"), _q("100000000", "points"), places=8)
    assert derived.value == "0.00000001"
    assert "E" not in derived.value and "e" not in derived.value
    assert Decimal(derived.value) == Decimal("1E-8")
    assert quant.within_tolerance(derived, "0.00000001")


# -- (b) family activation: registry flip + t18 registration ---------------


def test_quantitative_family_is_active() -> None:
    entry = families.registry()["quantitative"]
    assert entry.status == "active"
    assert entry.classification == "deterministic-oracle"


def test_t18_is_registered_and_generates(tmp_path: Path) -> None:
    reg = registry()
    assert T18 in reg, "t18_quantitative is not registered"
    template = reg[T18]
    assert template.family == "quantitative"
    assert template.variants == 4
    manifest = generate_corpus(1, tmp_path / "corpus", template_ids=[T18])
    assert manifest.counts["queries"] == 16  # 4 variants x 4 queries
    assert manifest.counts["expected"] == 16


# -- (c) spec-scenario golden: derived quantity with units -----------------


@pytest.fixture(scope="module")
def t18_corpus(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("t18") / "corpus"
    generate_corpus(1, root, template_ids=[T18])
    return root


def _records(
    root: Path,
) -> tuple[
    list[QueryRecord],
    dict[str, ExpectedRecord],
    dict[str, ClaimRecord],
    dict[str, SourceRecord],
]:
    queries = load_jsonl(QueryRecord, root / "queries.jsonl")
    expected = {e.query_id: e for e in load_jsonl(ExpectedRecord, root / "expected.jsonl")}
    claims = {c.claim_id: c for c in load_jsonl(ClaimRecord, root / "claims.jsonl")}
    sources = {s.source_id: s for s in load_jsonl(SourceRecord, root / "sources.jsonl")}
    return queries, expected, claims, sources


def _conversion_queries(queries: list[QueryRecord]) -> list[QueryRecord]:
    picked = [q for q in queries if q.query_kind == "derived_conversion_sum"]
    assert picked, "no conversion-combination query generated"
    return picked


def test_spec_scenario_derived_quantity_with_units(t18_corpus: Path) -> None:
    """Spec scenario: the expected record carries the oracle-computed value,
    unit, and tolerance, and BOTH contributing sources as required citations."""

    queries, expected, claims, _ = _records(t18_corpus)
    for query in _conversion_queries(queries):
        record = expected[query.query_id]
        assert record.answer.kind == "value"
        assert record.answer.unit == "kg"
        assert record.answer.tolerance is not None
        assert len(record.answer.values) == 1
        assert len(set(record.required_citations)) == 2

        # The expectation is recomputable from the two stored claims.
        contributors = [
            c
            for c in claims.values()
            if any(a.source_id in record.required_citations for a in c.assertions)
        ]
        assert len(contributors) == 2
        kg = next(c for c in contributors if c.object.unit == "kg")
        grams = next(c for c in contributors if c.object.unit == "g")
        derived = quant.derive_sum(kg.object, grams.object, unit="kg")
        assert record.answer.values == [derived.value]
        assert record.answer.tolerance == float(derived.tolerance)


def test_right_number_with_missing_citation_fails_citations_gate(
    t18_corpus: Path,
) -> None:
    queries, expected, claims, sources = _records(t18_corpus)
    query = _conversion_queries(queries)[0]
    record = expected[query.query_id]
    value, unit = record.answer.values[0], record.answer.unit
    ctx = ScoringContext(claims_by_id=claims, sources_by_id=sources)

    complete = AnswerRecord(
        query_id=query.query_id,
        answer_text=f"The combined mass is {value} {unit}.",
        citations=list(record.required_citations),
    )
    assert gate_value(query, record, complete, ctx).status is GateStatus.PASS

    # expect_derived_quantity builds required_citations from two claims but
    # leaves required_claims empty, so the scorer has no claim basis and cannot
    # verify citation precision for t18 records. The gate reports UNSUPPORTED
    # rather than banking an unverifiable provenance verdict as a PASS — a
    # contender that shotguns these records must not show a clean provenance
    # sheet. Recall is still measured and still reported.
    citations_item = gate_citations(query, record, complete, ctx)
    assert citations_item.status is GateStatus.UNSUPPORTED
    assert "recall 2/2" in (citations_item.evidence or "")
    assert "precision unverifiable" in (citations_item.evidence or "")

    missing_one = complete.model_copy(
        update={"citations": list(record.required_citations)[:1]}
    )
    assert gate_value(query, record, missing_one, ctx).status is GateStatus.PASS
    # Recall is provable whether or not precision is, so this still fails.
    item = gate_citations(query, record, missing_one, ctx)
    assert item.status is GateStatus.FAIL
    assert "missing citations" in (item.evidence or "")


def test_each_variant_asks_sum_conversion_ratio_and_abstain(t18_corpus: Path) -> None:
    queries, expected, _, _ = _records(t18_corpus)
    by_kind: dict[str, int] = {}
    for query in queries:
        by_kind[query.query_kind] = by_kind.get(query.query_kind, 0) + 1
    assert by_kind == {
        "derived_sum": 4,
        "derived_conversion_sum": 4,
        "derived_ratio": 4,
        "unanswerable": 4,
    }
    for query in queries:
        record = expected[query.query_id]
        if query.query_kind == "unanswerable":
            assert record.abstain
            assert record.answer.kind == "none"
        else:
            assert record.answer.kind == "value"
            assert record.answer.tolerance is not None
            assert len(set(record.required_citations)) == 2


# -- (d) t18-only double-generation determinism ----------------------------


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_t18_double_generation_is_byte_identical(tmp_path: Path) -> None:
    first = generate_corpus(7, tmp_path / "a", template_ids=[T18])
    second = generate_corpus(7, tmp_path / "b", template_ids=[T18])
    assert first == second
    assert _tree(tmp_path / "a") == _tree(tmp_path / "b")
