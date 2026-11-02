"""The question registry, the relation label map and the reading record (pure)."""

from __future__ import annotations

from pathlib import Path

import pytest
import sensing_fixture as sx

from exomem import sensing

MAP = sensing.label_map("relation-v1")
COVERAGE = sensing.label_map("coverage-v1")


def _v(entail: float, neutral: float, contra: float) -> list[float]:
    return [entail, neutral, contra]


@pytest.mark.parametrize(
    ("ab", "ba", "label", "direction", "reason"),
    [
        (_v(0.01, 0.02, 0.97), _v(0.02, 0.03, 0.95), "contradicts", None, None),
        (_v(0.97, 0.02, 0.01), _v(0.96, 0.03, 0.01), "restates", None, None),
        # A one-way contradiction is its own state, never neutral or contradicts.
        (_v(0.01, 0.02, 0.97), _v(0.30, 0.30, 0.40), "abstain", None, "directional_asymmetry"),
        (_v(0.30, 0.60, 0.10), _v(0.98, 0.01, 0.01), "refines", "ba", None),
        (_v(0.98, 0.01, 0.01), _v(0.30, 0.60, 0.10), "refines", "ab", None),
        (_v(0.20, 0.70, 0.10), _v(0.30, 0.60, 0.10), "neutral", None, None),
        # Incoherent: one direction contradicts, the other entails.
        (_v(0.01, 0.01, 0.98), _v(0.98, 0.01, 0.01), "abstain", None, "directional_asymmetry"),
    ],
)
def test_relation_map_rules_in_order(ab, ba, label, direction, reason) -> None:
    verdict = MAP.apply(ab, ba)
    assert verdict is not None
    assert (verdict.label, verdict.direction, verdict.reason) == (label, direction, reason)


def test_thresholds_are_inclusive_on_stored_values() -> None:
    assert MAP.apply(_v(0, 0.07, 0.93), _v(0, 0.07, 0.93)).label == "contradicts"
    assert MAP.apply(_v(0, 0.071, 0.929999), _v(0, 0.07, 0.93)).label == "abstain"


def test_unreadable_vectors_refuse_rather_than_label() -> None:
    assert MAP.apply([0.5, 0.5], [0.5, 0.5]) is None
    assert MAP.apply(_v(float("nan"), 0.5, 0.5), _v(0.3, 0.3, 0.4)) is None
    assert MAP.apply(None, _v(0.3, 0.3, 0.4)) is None
    assert MAP.apply(None, None, abstain_reason="input_too_long").reason == "input_too_long"


def test_unknown_label_map_and_question_refuse() -> None:
    with pytest.raises(ValueError):
        sensing.label_map("")
    with pytest.raises(ValueError):
        sensing.label_map("relation-v0")
    with pytest.raises(ValueError):
        sensing.question("mention.same_referent")


def test_the_setting_defaults_off_and_ignores_typos() -> None:
    assert sensing.resolve_setting(None, None) == "off"
    assert sensing.resolve_setting("ON ", None) == "on"
    assert sensing.resolve_setting("yes", {"sensing": "on"}) == "on"
    assert sensing.resolve_setting("banana", {"sensing": "maybe"}) == "off"


def test_unit_scope_uses_registry_keys() -> None:
    scope = sensing.QUESTIONS[sensing.PAIR_RELATION].unit_scope
    assert scope.admits(kind="observation", category="finding")
    assert not scope.admits(kind="observation", category="question")
    assert scope.admits(kind="claim", category=None)
    assert not scope.admits(kind="timeline_event", category=None)


def test_extractor_normalises_and_hashes_the_exact_text() -> None:
    assert sensing.extract_text("  Café\n  opens\tlate ") == "Café opens late"
    text = sensing.extract_text("A  b")
    assert sensing.text_sha256(text) == sensing.text_sha256("A b")


def _identity(**changes) -> sensing.InstrumentIdentity:
    base = dict(
        model="m",
        revision="r",
        weights_sha256="w",
        runtime="torch",
        runtime_version="2.0",
        template_version="nli-pair-v1",
        label_map_version="relation-v1",
        fixture_set="relation-v1-multilingual",
    )
    base.update(changes)
    return sensing.InstrumentIdentity(**base)


def test_label_map_and_fixture_versions_are_outside_the_instrument_id() -> None:
    first = _identity()
    assert _identity(label_map_version="relation-v2").instrument_id == first.instrument_id
    assert _identity(fixture_set="other").instrument_id == first.instrument_id
    for change in (
        {"weights_sha256": "x"},
        {"weights_sha256": None},
        {"runtime_version": "2.1"},
        {"template_version": "nli-pair-v2"},
        {"revision": "r2"},
    ):
        assert _identity(**change).instrument_id != first.instrument_id, change
    assert _identity(weights_sha256=None).unpinned_weights is True


def test_reading_ids_are_unordered_and_bound_to_the_instrument() -> None:
    a, b = sensing.text_sha256("a"), sensing.text_sha256("b")
    q = sensing.PAIR_RELATION
    assert sensing.reading_id(q, "i", [a, b]) == sensing.reading_id(q, "i", [b, a])
    assert sensing.reading_id(q, "i", [a, b]) != sensing.reading_id(q, "j", [a, b])
    assert sensing.input_key(q, [a, b]) == sensing.input_key(q, [b, a])


def test_make_reading_derives_the_verdict_from_rounded_vectors() -> None:
    texts = sorted(["x one", "y two"], key=sensing.text_sha256)
    inputs = [
        sensing.InputUnit(f"ref-{t}", f"p/{t}.md", sensing.text_sha256(t)) for t in texts
    ]
    reading = sensing.make_reading(
        sensing.PAIR_RELATION,
        _identity(),
        inputs,
        ab=[0.0000001, 0.0699999, 0.9299996],
        ba=[0.0, 0.07, 0.93],
        abstain_reason=None,
        sensed_at="2026-09-28T00:00:00Z",
    )
    assert reading is not None
    assert reading.vectors["ab"] == [0.0, 0.07, 0.93]
    assert reading.verdict.label == "contradicts"
    with pytest.raises(ValueError):
        sensing.make_reading(
            sensing.PAIR_RELATION,
            _identity(),
            list(reversed(inputs)),
            ab=[0, 0, 1],
            ba=[0, 0, 1],
            abstain_reason=None,
            sensed_at="t",
        )


def test_rederive_under_a_new_map_needs_no_sensing() -> None:
    stored = sensing.Verdict("neutral", 0.5)
    vectors = {"ab": _v(0.96, 0.02, 0.02), "ba": _v(0.2, 0.7, 0.1)}
    assert sensing.rederive(vectors, stored, "relation-v1").label == "refines"
    abstained = sensing.Verdict("abstain", None, None, "input_too_long")
    assert sensing.rederive(None, abstained, "relation-v1") == abstained


def test_fixture_set_covers_every_decisive_label_with_a_twin() -> None:
    fixtures = sensing.RELATION_FIXTURES["relation-v1-multilingual"]
    labels = {item.expected for item in fixtures}
    assert {"contradicts", "refines", "restates", "neutral"} <= labels
    twinned = {item.twin_of for item in fixtures if item.twin_of}
    assert twinned == {"contradicts", "refines", "restates", "neutral"}
    for item in fixtures:
        if item.twin_of:
            assert item.expected != item.twin_of
        assert (item.expected == "refines") == (item.refining in {"a", "b"})
    shapes = {item.language_shape for item in fixtures}
    assert "en/en" in shapes and any("/" in s and s.split("/")[0] != s.split("/")[1] for s in shapes)
    assert sensing.fixture_precision("relation-v1-multilingual", "contradicts")["total"] >= 3


def test_admission_evidence_is_keyed_by_the_fixtures_and_label_map(monkeypatch) -> None:
    from exomem import sensing_nli

    identity = _identity()
    before = sensing_nli._fixture_digest(identity)
    fixtures = sensing.RELATION_FIXTURES["relation-v1-multilingual"]
    monkeypatch.setitem(sensing.RELATION_FIXTURES, "relation-v1-multilingual", fixtures[:-1])
    assert sensing_nli._fixture_digest(identity) != before


def test_the_nli_identity_needs_no_model_import() -> None:
    import sys

    from exomem import sensing_nli

    loaded = set(sys.modules)
    identity = sensing_nli.identity()
    assert "sentence_transformers" not in set(sys.modules) - loaded
    assert "torch" not in set(sys.modules) - loaded
    if identity is not None:
        assert identity.placement == "local-cpu"
        assert identity.template_version == "nli-pair-v1"


def test_sensing_off_adds_no_per_read_config_io(monkeypatch) -> None:
    """The env var wins without touching the config file, and the config read is memoised."""
    from exomem import mode

    reads: list[int] = []
    real = mode.read_config
    monkeypatch.setattr(mode, "read_config", lambda: reads.append(1) or real())
    monkeypatch.setenv("EXOMEM_SENSING", "off")
    sensing.clear_memo()
    for _ in range(50):
        assert sensing.enabled() is False
    assert reads == []
    monkeypatch.delenv("EXOMEM_SENSING")
    sensing.clear_memo()
    for _ in range(50):
        assert sensing.enabled() is False
    assert len(reads) == 1


def test_the_config_memo_follows_the_config_path(tmp_path: Path, monkeypatch) -> None:
    """Recheck INFO 4: a memoised reading belongs to one config file."""
    on, off = tmp_path / "on.json", tmp_path / "off.json"
    on.write_text('{"sensing": "on"}', encoding="utf-8")
    off.write_text('{"sensing": "off"}', encoding="utf-8")
    monkeypatch.delenv("EXOMEM_SENSING", raising=False)
    sensing.clear_memo()
    monkeypatch.setenv("EXOMEM_CONFIG_PATH", str(on))
    assert sensing.enabled() is True
    monkeypatch.setenv("EXOMEM_CONFIG_PATH", str(off))
    assert sensing.enabled() is False
    monkeypatch.setenv("EXOMEM_CONFIG_PATH", str(on))
    assert sensing.enabled() is True
    sensing.clear_memo()


@pytest.mark.parametrize(
    ("ab", "ba", "label"),
    [
        (_v(0.97, 0.02, 0.01), _v(0.30, 0.60, 0.10), "entailed"),
        (_v(0.30, 0.60, 0.10), _v(0.98, 0.01, 0.01), "partial"),
        (_v(0.20, 0.70, 0.10), _v(0.30, 0.60, 0.10), "absent"),
        # A contradiction is never read as absence, even when the claim also
        # entails the unit: the first rule wins.
        (_v(0.01, 0.02, 0.97), _v(0.98, 0.01, 0.01), "contradicted"),
    ],
)
def test_coverage_map_rules_in_order(ab, ba, label) -> None:
    verdict = COVERAGE.apply(ab, ba)
    assert verdict is not None and verdict.label == label


def test_coverage_thresholds_are_the_relation_ones_not_a_copy() -> None:
    assert COVERAGE.contradicted_min == MAP.contradicts_min
    assert COVERAGE.entailed_min == MAP.refines_min
    assert COVERAGE.partial_min == MAP.refines_min
    assert COVERAGE.apply(_v(0, 0.07, 0.93), _v(0, 1, 0)).label == "contradicted"
    assert COVERAGE.apply(_v(0.949999, 0.05, 0.000001), _v(0.2, 0.7, 0.1)).label == "absent"
    assert COVERAGE.apply(None, _v(0.3, 0.3, 0.4)) is None
    assert COVERAGE.apply(None, None, abstain_reason="input_too_long").label == "abstain"


def test_pair_relation_ids_are_unchanged_and_slotted_ids_are_not_unordered() -> None:
    """Golden values taken from the build before slots existed: a stored
    `pair.relation` reading must keep its id. A slotted question binds each
    hash to its slot, so a claim read as premise never collides with the same
    text read as hypothesis."""
    a, b = "a" * 64, "b" * 64
    q = sensing.PAIR_RELATION
    assert sensing.reading_id(q, "inst", [a, b]) == (
        "c708d42c6b7275683e6d36093ede91e0e37bac39969434c79bcaa2677d3ff259"
    )
    assert sensing.input_key(q, [a, b]) == (
        "7006a129716bbb043a4bedeadc4538964b1e3f12b426553ef3128b425427a212"
    )
    assert sensing.edge_fingerprint(q, [a, b], sensing.Verdict("refines", 0.99, "ab")) == (
        "ee18253a6399d21cf58f3772"
    )
    assert _identity(
        model="m", revision="r", weights_sha256="w" * 64, runtime="rt", runtime_version="1"
    ).instrument_id == "2cce82d7f88c231d64c503f879b96c118e0f295d1421fad8d468b7bb7987da1a"
    cover = sensing.RECAP_COVERED_BY
    assert sensing.reading_id(cover, "inst", [a, b]) != sensing.reading_id(cover, "inst", [b, a])
    assert sensing.input_key(cover, [a, b]) != sensing.input_key(cover, [b, a])


def test_a_slotted_reading_keeps_slot_order_whatever_the_hash_order() -> None:
    premise, claim = "Seal wear doubles.", "Seals wear faster."
    inputs = [
        sensing.InputUnit("u1", "p.md", sensing.text_sha256(premise), slot="premise"),
        sensing.InputUnit("recap", "r.md", sensing.text_sha256(claim), extractor="recap-claim-v1", slot="hypothesis"),
    ]
    reading = sensing.make_reading(
        sensing.RECAP_COVERED_BY,
        sx.COVERAGE_STUB,
        inputs,
        ab=sx.ENTAIL,
        ba=sx.WEAK,
        abstain_reason=None,
        sensed_at="2026-11-02T00:00:00Z",
    )
    assert reading is not None and reading.verdict.label == "entailed"
    assert [item["slot"] for item in (i.as_dict() for i in reading.inputs)] == ["premise", "hypothesis"]
    with pytest.raises(ValueError):
        sensing.make_reading(
            sensing.RECAP_COVERED_BY, sx.COVERAGE_STUB, list(reversed(inputs)),
            ab=sx.ENTAIL, ba=sx.WEAK, abstain_reason=None, sensed_at="t",
        )
    assert "slot" not in sensing.InputUnit("u", "p.md", "h").as_dict()


def test_coverage_fixtures_are_read_in_slot_order_and_a_miss_is_red() -> None:
    from exomem import sensing_nli

    green, detail, results = sensing_nli.judge_fixtures(sx.CoverageStubInstrument(sx.coverage_table()), "recap-covered-v1")
    assert green, detail
    assert len(results) == len(sensing.COVERAGE_FIXTURES["recap-covered-v1"])
    # A reader that swaps premise and hypothesis (the pair.relation hash order) reads
    # `partial` as `entailed`: one miss is red.
    swapped = {(h, p): label for (p, h), label in sx.coverage_table().items()}
    red, detail, _ = sensing_nli.judge_fixtures(sx.CoverageStubInstrument(swapped), "recap-covered-v1")
    assert not red and "expected" in detail


def test_coverage_fixtures_cover_every_label_in_three_language_shapes_with_a_twin() -> None:
    fixtures = sensing.COVERAGE_FIXTURES["recap-covered-v1"]
    for label in ("entailed", "partial", "absent", "contradicted"):
        own = [item for item in fixtures if item.expected == label]
        assert any(item.language_shape == "en/en" and not item.twin_of for item in own)
        assert any(item.language_shape.split("/")[0] != item.language_shape.split("/")[1] for item in own)
        assert any(item.twin_of for item in own)
        assert all(item.expected != item.twin_of for item in own)
    assert len({(i.premise, i.hypothesis) for i in fixtures}) == len(fixtures)


def _stub_admission(monkeypatch, green_sets: set[str]) -> list[str]:
    """Admission with no model: a counted loader and fixture runs that go green for `green_sets`."""
    from exomem import claims, sensing_nli

    built: list[str] = []
    monkeypatch.setattr(sensing_nli, "runtime_version", lambda: "stub=1")
    monkeypatch.setattr(claims, "_resolve_weights", lambda pin: (pin.weights_sha256, "snapshot"))
    monkeypatch.setattr(sensing_nli, "_build_model", lambda snapshot: built.append(snapshot) or object())
    for cache in ("_ADMITTED", "_LOADED", "_MODELS"):
        monkeypatch.setattr(sensing_nli, cache, {})
    monkeypatch.setattr(
        sensing_nli, "judge_fixtures", lambda _instrument, fixture_set: (fixture_set in green_sets, "stub", [])
    )
    return built


def test_a_coverage_fixture_miss_refuses_coverage_only_and_the_model_loads_once(tmp_path, monkeypatch) -> None:
    from exomem import sensing_nli

    built = _stub_admission(monkeypatch, {"relation-v1-multilingual"})
    evidence = tmp_path / "admission.json"
    relation = sensing_nli.admit(evidence_path=evidence)
    with pytest.raises(sensing_nli.Refused) as refused:
        sensing_nli.admit(evidence_path=evidence, question_type=sensing.RECAP_COVERED_BY)
    assert refused.value.reason == "fixtures-failed"
    assert sensing_nli.admit(evidence_path=evidence) is relation
    assert len(built) == 1


def test_the_admission_memo_keeps_every_admitted_question(tmp_path, monkeypatch) -> None:
    import json

    from exomem import sensing_nli

    _stub_admission(monkeypatch, {"relation-v1-multilingual", "recap-covered-v1"})
    evidence = tmp_path / "admission.json"
    sensing_nli.admit(evidence_path=evidence)
    sensing_nli.admit(evidence_path=evidence, question_type=sensing.RECAP_COVERED_BY)
    ids = {sensing_nli.identity(q).instrument_id for q in (sensing.PAIR_RELATION, sensing.RECAP_COVERED_BY)}
    assert len(ids) == 2
    assert {key.split(":")[0] for key in json.loads(evidence.read_text())} == ids
