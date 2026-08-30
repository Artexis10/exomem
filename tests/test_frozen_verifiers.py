"""Frozen stance verifier: label map, pin registry, admission, and queue enrichment.

The admission rule as tests. A verifier labels a review-queue entry only under a
pinned weights digest, a versioned label map, a green fixture set, and its
opt-in gate; anything else degrades to ABSENCE — never to the lexical heuristic
wearing the verifier's method name.

Torch-free by construction: every test injects a fake predictor, so the suite
runs on a box with no cross-encoder weights and no `nli` extra installed.
"""

from __future__ import annotations

import pytest

from exomem import claims

# ---------------- 1.1 label map v1 (versioned in-repo artifact) ----------------


def test_label_map_v1_is_the_declared_current_version() -> None:
    assert claims.LABEL_MAP_VERSION == "v1"
    assert claims.get_label_map("v1").version == "v1"


def test_label_map_declares_logit_column_semantics_and_order() -> None:
    """D4: the map declares the columns and their ORDER, so a model whose head
    orders entailment/contradiction differently is a different pair."""
    v1 = claims.get_label_map("v1")
    assert v1.columns == ("contradiction", "entailment", "neutral")
    assert v1.labels == frozenset({"contradict", "refine", "duplicate", "unrelated"})


def test_unknown_label_map_version_refuses() -> None:
    with pytest.raises(ValueError, match="unknown label map"):
        claims.get_label_map("v2")


def test_unversioned_label_map_refuses() -> None:
    for bad in ("", None):
        with pytest.raises(ValueError, match="label map"):
            claims.get_label_map(bad)


@pytest.mark.parametrize(
    ("logits", "expected"),
    [
        # (contradiction, entailment, neutral) logits per direction.
        ([[4.0, 0.0, 0.0], [4.0, 0.0, 0.0]], "contradict"),
        ([[0.0, 4.0, 0.0], [0.0, 4.0, 0.0]], "duplicate"),
        ([[0.0, 0.0, 4.0], [0.0, 0.0, 4.0]], "unrelated"),
        ([[0.0, 2.0, 0.6], [0.0, 0.0, 0.6]], "refine"),
    ],
)
def test_label_map_v1_applies_the_promoted_threshold_logic(logits, expected) -> None:
    result = claims.get_label_map("v1").apply(logits)
    assert result is not None
    assert result.label == expected
    assert result.method == "nli"
    assert 0.0 <= result.score <= 1.0


def test_label_map_v1_refuses_a_wrong_shaped_head() -> None:
    """A two-column head is a different (digest, label-map) pair, not a coercion."""
    assert claims.get_label_map("v1").apply([[1.0, 0.0], [1.0, 0.0]]) is None
    assert claims.get_label_map("v1").apply([1.0, 0.0, 0.0]) is None


# ---------------- 1.2 pin registry + admission (repository artifact) ----------------

_FAKE_MODEL = "exomem-test/frozen-stance-fixture"


@pytest.fixture(autouse=True)
def _reset_verifier_state():
    claims.reset_verifier_cache()
    yield
    claims.reset_verifier_cache()


def _plant_weights(tmp_path, monkeypatch, *, model_name: str = _FAKE_MODEL) -> str:
    """Plant a resident snapshot in a throwaway hub cache; return its true digest."""
    from exomem import model_cache

    hub = tmp_path / "hub"
    snapshot = hub / model_cache.snapshot_dirname(model_name) / "snapshots" / "rev0"
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text('{"architectures": ["X"]}', encoding="utf-8")
    (snapshot / "model.safetensors").write_bytes(b"not-real-weights-but-stable-bytes")
    monkeypatch.setenv("HF_HUB_CACHE", str(hub))
    return claims._directory_digest(snapshot)


def _pin(digest: str, *, label_map_version: str = "v1") -> tuple:
    return (
        claims.VerifierPin(
            model_name=_FAKE_MODEL,
            weights_sha256=digest,
            label_map_version=label_map_version,
            fixture_set="stance-v1",
        ),
    )


def test_pin_registry_is_an_immutable_repository_artifact() -> None:
    assert isinstance(claims.VERIFIER_PINS, tuple)
    for pin in claims.VERIFIER_PINS:
        assert isinstance(pin, claims.VerifierPin)
        assert pin.model_name and pin.weights_sha256
        assert pin.label_map_version and pin.fixture_set


def test_no_runtime_configuration_reads_a_model_name_into_a_pin() -> None:
    """D1: no environment value may add, select, or alter a pin.

    Checked on the compiled code object, not the prose: pin selection may read
    the repository registry and nothing else, so neither an environment lookup
    nor an `EXOMEM_*` literal may appear in the only function that chooses one.
    """
    code = claims._active_pin.__code__
    assert not {"environ", "environb", "getenv"} & set(code.co_names)
    assert not [c for c in code.co_consts if isinstance(c, str) and c.startswith("EXOMEM_")]
    assert "VERIFIER_PINS" in code.co_names


def test_gate_off_refuses_to_absence(tmp_path, monkeypatch) -> None:
    digest = _plant_weights(tmp_path, monkeypatch)
    monkeypatch.setattr(claims, "VERIFIER_PINS", _pin(digest))
    monkeypatch.delenv("EXOMEM_CLAIM_POLARITY_NLI", raising=False)

    admission = claims.verifier_admission()
    assert admission.admitted is False
    assert admission.reason == "gate-off"
    assert claims.verifier_polarity("Caching helps", "Caching hurts") is None


def test_empty_registry_refuses_even_with_the_gate_on(monkeypatch) -> None:
    monkeypatch.setattr(claims, "VERIFIER_PINS", ())
    monkeypatch.setenv("EXOMEM_CLAIM_POLARITY_NLI", "1")

    admission = claims.verifier_admission()
    assert admission.admitted is False
    assert admission.reason == "no-pin"


def test_runtime_pin_injection_attempt_does_not_admit(tmp_path, monkeypatch) -> None:
    """An env value naming a model absent from the registry admits nothing, and
    the ignored value is reported on the diagnostic surface."""
    _plant_weights(tmp_path, monkeypatch)
    monkeypatch.setattr(claims, "VERIFIER_PINS", ())
    monkeypatch.setenv("EXOMEM_CLAIM_POLARITY_NLI", "1")
    monkeypatch.setenv("EXOMEM_CLAIM_NLI_MODEL", _FAKE_MODEL)

    admission = claims.verifier_admission()
    assert admission.admitted is False
    assert admission.reason == "no-pin"
    assert admission.model_name is None
    assert admission.ignored_model_env == _FAKE_MODEL
    assert claims.VERIFIER_PINS == ()


def test_wrong_digest_refuses_and_records_the_degradation(tmp_path, monkeypatch) -> None:
    _plant_weights(tmp_path, monkeypatch)
    monkeypatch.setattr(claims, "VERIFIER_PINS", _pin("0" * 64))
    monkeypatch.setenv("EXOMEM_CLAIM_POLARITY_NLI", "1")

    admission = claims.verifier_admission()
    assert admission.admitted is False
    assert admission.reason == "digest-mismatch"
    assert admission.model_digest is not None and admission.model_digest != "0" * 64
    assert admission.as_dict()["reason"] == "digest-mismatch"
    assert claims.verifier_polarity("Caching helps", "Caching hurts") is None


def test_missing_weights_refuse(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "empty-hub"))
    monkeypatch.setattr(claims, "VERIFIER_PINS", _pin("0" * 64))
    monkeypatch.setenv("EXOMEM_CLAIM_POLARITY_NLI", "1")

    admission = claims.verifier_admission()
    assert admission.admitted is False
    assert admission.reason == "weights-missing"


def test_unknown_label_map_in_a_pin_refuses(tmp_path, monkeypatch) -> None:
    digest = _plant_weights(tmp_path, monkeypatch)
    monkeypatch.setattr(claims, "VERIFIER_PINS", _pin(digest, label_map_version="v9"))
    monkeypatch.setenv("EXOMEM_CLAIM_POLARITY_NLI", "1")

    admission = claims.verifier_admission()
    assert admission.admitted is False
    assert admission.reason == "label-map-unknown"


def test_weights_are_hashed_once_per_process_and_cached(tmp_path, monkeypatch) -> None:
    digest = _plant_weights(tmp_path, monkeypatch)
    monkeypatch.setattr(claims, "VERIFIER_PINS", _pin(digest))
    monkeypatch.setenv("EXOMEM_CLAIM_POLARITY_NLI", "1")

    calls = {"n": 0}
    real = claims._directory_digest

    def counting(root):
        calls["n"] += 1
        return real(root)

    monkeypatch.setattr(claims, "_directory_digest", counting)
    for _ in range(4):
        claims.resolve_weights_digest(_FAKE_MODEL)
    assert calls["n"] == 1


def test_a_second_resident_revision_is_ambiguous_and_refuses(tmp_path, monkeypatch) -> None:
    from exomem import model_cache

    digest = _plant_weights(tmp_path, monkeypatch)
    second = (
        tmp_path / "hub" / model_cache.snapshot_dirname(_FAKE_MODEL) / "snapshots" / "rev1"
    )
    second.mkdir(parents=True)
    (second / "model.safetensors").write_bytes(b"a different revision entirely")
    monkeypatch.setattr(claims, "VERIFIER_PINS", _pin(digest))
    monkeypatch.setenv("EXOMEM_CLAIM_POLARITY_NLI", "1")

    admission = claims.verifier_admission()
    assert admission.admitted is False
    assert admission.reason == "weights-missing"
    assert "ambiguous" in admission.detail


def test_refused_verifier_never_lets_the_heuristic_wear_the_nli_name(monkeypatch) -> None:
    monkeypatch.setattr(claims, "VERIFIER_PINS", ())
    monkeypatch.setenv("EXOMEM_CLAIM_LEVEL", "1")
    monkeypatch.setenv("EXOMEM_CLAIM_POLARITY_NLI", "1")

    assert claims.verifier_polarity("Caching improves latency", "Caching degrades latency") is None
    fallback = claims.classify_polarity("Caching improves latency", "Caching degrades latency")
    assert fallback.method == "heuristic"


# ---------------- 1.3 knob fate ----------------


def test_polarity_gate_is_default_off(monkeypatch) -> None:
    monkeypatch.delenv("EXOMEM_CLAIM_POLARITY_NLI", raising=False)
    assert claims._nli_enabled() is False
    assert claims.verifier_admission().reason == "gate-off"


def test_retired_model_knob_is_reported_on_the_diagnostic_surface(monkeypatch) -> None:
    monkeypatch.delenv("EXOMEM_CLAIM_POLARITY_NLI", raising=False)
    monkeypatch.setenv("EXOMEM_CLAIM_NLI_MODEL", "somebody/some-cross-encoder")

    status = claims.verifier_status()
    assert status["retired_model_env"] == "EXOMEM_CLAIM_NLI_MODEL"
    assert status["ignored_model_env"] == "somebody/some-cross-encoder"
    assert status["admitted"] is False
    assert status["reason"] == "gate-off"
    assert status["gate"] == "EXOMEM_CLAIM_POLARITY_NLI"


def test_retired_model_knob_enables_nothing_on_its_own(tmp_path, monkeypatch) -> None:
    """Setting the retired knob does not turn the verifier on, and does not
    become the model even when weights under that name are resident."""
    _plant_weights(tmp_path, monkeypatch)
    monkeypatch.setenv("EXOMEM_CLAIM_NLI_MODEL", _FAKE_MODEL)
    monkeypatch.setenv("EXOMEM_CLAIM_POLARITY_NLI", "1")
    monkeypatch.setattr(claims, "VERIFIER_PINS", ())

    status = claims.verifier_status()
    assert status["admitted"] is False
    assert status["reason"] == "no-pin"
    assert status["pinned_models"] == []
    assert status["model_name"] is None


def test_status_names_the_registry_and_the_shipped_label_maps(tmp_path, monkeypatch) -> None:
    digest = _plant_weights(tmp_path, monkeypatch)
    monkeypatch.setattr(claims, "VERIFIER_PINS", _pin(digest))
    monkeypatch.setenv("EXOMEM_CLAIM_POLARITY_NLI", "1")

    status = claims.verifier_status()
    assert status["pinned_models"] == [_FAKE_MODEL]
    assert status["label_map_versions"] == ["v1"]


# ---------------- 1.4 verification fixture set ----------------

#: Logit blocks that label map v1 maps to each label, used to drive fake
#: verifiers. Shape is exactly what a cross-encoder emits for the two
#: orderings of one pair: (2, 3) over (contradiction, entailment, neutral).
_LOGITS: dict[str, list[list[float]]] = {
    "contradict": [[6.0, 0.0, 0.0], [6.0, 0.0, 0.0]],
    "duplicate": [[0.0, 6.0, 0.0], [0.0, 6.0, 0.0]],
    "unrelated": [[0.0, 0.0, 6.0], [0.0, 0.0, 6.0]],
    "refine": [[0.0, 2.0, 0.6], [0.0, 0.0, 0.6]],
}


def _oracle_predict(*, wrong: str | None = None, raises_on: str | None = None, calls=None):
    """A fake cross-encoder that answers the fixture set correctly.

    `wrong` re-labels the fixture whose `claim_a` matches it; `raises_on` makes
    the forward pass blow up on that one pair.
    """
    answers = {
        (pair.claim_a, pair.claim_b): pair.expected
        for pair in claims.VERIFICATION_FIXTURES["stance-v1"]
    }

    def predict(pairs):
        if calls is not None:
            calls.append(pairs)
        claim_a, claim_b = pairs[0]
        if raises_on is not None and claim_a == raises_on:
            raise RuntimeError("forward pass exploded")
        label = answers.get((claim_a, claim_b), "unrelated")
        if wrong is not None and claim_a == wrong:
            label = "duplicate" if label != "duplicate" else "contradict"
        return _LOGITS[label]

    return predict


def _admit(tmp_path, monkeypatch, predict) -> str:
    """Plant weights, pin their true digest, set the gate, inject `predict`."""
    digest = _plant_weights(tmp_path, monkeypatch)
    monkeypatch.setattr(claims, "VERIFIER_PINS", _pin(digest))
    monkeypatch.setenv("EXOMEM_CLAIM_POLARITY_NLI", "1")
    monkeypatch.setattr(claims, "_load_verifier_predictor", lambda name: predict)
    return digest


def test_fixture_set_covers_the_four_corpus_shapes() -> None:
    fixtures = claims.VERIFICATION_FIXTURES["stance-v1"]
    assert len(fixtures) >= 8
    assert {pair.expected for pair in fixtures} == claims.POLARITY_LABELS
    for pair in fixtures:
        assert pair.claim_a and pair.claim_b and pair.note
    # The heuristic's known failure cases are carried explicitly, not implied.
    assert sum(1 for pair in fixtures if pair.heuristic_fails) >= 3


def test_green_fixtures_admit_the_verifier(tmp_path, monkeypatch) -> None:
    digest = _admit(tmp_path, monkeypatch, _oracle_predict())

    admission = claims.verifier_admission()
    assert admission.admitted is True
    assert admission.reason == "admitted"
    assert admission.model_digest == digest
    assert admission.label_map_version == "v1"
    assert admission.fixture_set == "stance-v1"


def test_one_red_fixture_refuses_the_pair(tmp_path, monkeypatch) -> None:
    first = claims.VERIFICATION_FIXTURES["stance-v1"][0]
    _admit(tmp_path, monkeypatch, _oracle_predict(wrong=first.claim_a))

    admission = claims.verifier_admission()
    assert admission.admitted is False
    assert admission.reason == "fixtures-failed"
    assert first.claim_a in admission.detail
    assert claims.verifier_polarity("Caching helps", "Caching hurts") is None


def test_absent_extra_refuses_as_a_missing_dependency(tmp_path, monkeypatch) -> None:
    digest = _plant_weights(tmp_path, monkeypatch)
    monkeypatch.setattr(claims, "VERIFIER_PINS", _pin(digest))
    monkeypatch.setenv("EXOMEM_CLAIM_POLARITY_NLI", "1")
    monkeypatch.setattr(claims, "_load_verifier_predictor", lambda name: None)

    admission = claims.verifier_admission()
    assert admission.admitted is False
    assert admission.reason == "dependency-missing"


def test_unknown_fixture_set_in_a_pin_refuses(tmp_path, monkeypatch) -> None:
    digest = _plant_weights(tmp_path, monkeypatch)
    monkeypatch.setattr(
        claims,
        "VERIFIER_PINS",
        (
            claims.VerifierPin(
                model_name=_FAKE_MODEL,
                weights_sha256=digest,
                label_map_version="v1",
                fixture_set="no-such-set",
            ),
        ),
    )
    monkeypatch.setenv("EXOMEM_CLAIM_POLARITY_NLI", "1")
    monkeypatch.setattr(claims, "_load_verifier_predictor", lambda name: _oracle_predict())

    admission = claims.verifier_admission()
    assert admission.admitted is False
    assert admission.reason == "fixtures-failed"
    assert "no-such-set" in admission.detail


def test_fixture_verification_runs_once_per_admitted_pair(tmp_path, monkeypatch) -> None:
    calls: list = []
    _admit(tmp_path, monkeypatch, _oracle_predict(calls=calls))

    assert claims.verifier_admission().admitted is True
    after_first = len(calls)
    assert after_first == len(claims.VERIFICATION_FIXTURES["stance-v1"])
    for _ in range(3):
        assert claims.verifier_admission().admitted is True
    assert len(calls) == after_first


def test_admitted_verifier_labels_through_the_label_map(tmp_path, monkeypatch) -> None:
    _admit(tmp_path, monkeypatch, _oracle_predict())
    fixture = next(
        pair for pair in claims.VERIFICATION_FIXTURES["stance-v1"] if pair.expected == "contradict"
    )

    result = claims.verifier_polarity(fixture.claim_a, fixture.claim_b)
    assert result is not None
    assert result.label == "contradict"
    assert result.method == "nli"


# ---------------- 1.5 input-shape pin (design D5) ----------------


def test_verifier_takes_exactly_a_claim_text_pair() -> None:
    import inspect

    signature = inspect.signature(claims.verifier_polarity)
    assert list(signature.parameters) == ["claim_a", "claim_b"]
    for parameter in signature.parameters.values():
        assert parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        assert parameter.annotation == "str"


def test_claim_texts_reach_the_model_verbatim_as_a_classification_pair(
    tmp_path, monkeypatch
) -> None:
    """No prefix, no instruction, no separator token — the two texts and nothing
    else, in both orderings. Vault text can only ever be classified, never read
    as an instruction."""
    seen: list = []
    _admit(tmp_path, monkeypatch, _oracle_predict(calls=seen))
    claims.verifier_admission()
    seen.clear()

    claims.verifier_polarity("ALPHA claim text", "BETA claim text")

    assert len(seen) == 1
    assert seen[0] == [
        ("ALPHA claim text", "BETA claim text"),
        ("BETA claim text", "ALPHA claim text"),
    ]
    flattened = {text for pair in seen[0] for text in pair}
    assert flattened == {"ALPHA claim text", "BETA claim text"}


def test_no_string_assembly_exists_behind_the_verifier_seam() -> None:
    """Structural: the verifier path builds no string at all.

    An f-string, a `.format`, a `%` interpolation, or a join over claim text is
    how a classification seam quietly becomes a prompted generative one. There
    is no such opcode and no such name on this path, so the shape cannot drift
    without this pin going red.
    """
    import dis

    assembly_opcodes = {
        "FORMAT_VALUE",
        "FORMAT_SIMPLE",
        "FORMAT_WITH_SPEC",
        "BUILD_STRING",
        "CONVERT_VALUE",
    }
    template_names = {"format", "format_map", "Template", "join", "substitute"}
    for function in (claims.verifier_polarity, claims.LabelMap.apply):
        opcodes = {instruction.opname for instruction in dis.get_instructions(function)}
        assert not (opcodes & assembly_opcodes), (function.__qualname__, opcodes)
        assert not (set(function.__code__.co_names) & template_names)


def test_verifier_output_is_drawn_from_the_label_maps_closed_set(tmp_path, monkeypatch) -> None:
    _admit(tmp_path, monkeypatch, _oracle_predict())
    for pair in claims.VERIFICATION_FIXTURES["stance-v1"]:
        result = claims.verifier_polarity(pair.claim_a, pair.claim_b)
        assert result is not None
        assert result.label in claims.POLARITY_LABELS
        assert result.method == "nli"


# ---------------- 3.x queue enrichment (design D2, D3) ----------------

from exomem import audit as audit_module  # noqa: E402

#: Two pages whose claims are a fixture pair, so the oracle labels them.
_PAGE_A = "Knowledge Base/Notes/Insights/ttl-reduces.md"
_PAGE_B = "Knowledge Base/Notes/Insights/ttl-increases.md"


def _contradiction_fixture() -> claims.FixturePair:
    return next(
        pair
        for pair in claims.VERIFICATION_FIXTURES["stance-v1"]
        if pair.expected == "contradict" and not pair.heuristic_fails
    )


def _wire_claim_texts(monkeypatch, texts: dict[str, str]) -> None:
    monkeypatch.setattr(
        claims,
        "claim_text_for_page",
        lambda vault_root, rel_path, *, index=None: texts.get(rel_path),
    )


def _proximity_finding(
    a: str = _PAGE_A,
    b: str = _PAGE_B,
    *,
    signal_version: str = "aaaaaaaaaaaaaaaa",
    cosine: float = 0.86,
    priority: float = 0.91,
) -> audit_module.AuditFinding:
    return audit_module.AuditFinding(
        category="corpus_contradictions",
        severity="info",
        path=a,
        detail=(
            f"Active conclusion overlaps active conclusion {b!r} (cosine {cosine}) "
            "— close enough to restate, refine, or contradict. Do they conflict?"
        ),
        proposed_fix="Surfaced for REVIEW only — a proximity measurement.",
        paths=[a, b],
        meta={
            "signal_version": signal_version,
            "cosine": cosine,
            "priority": priority,
            "dormancy": 0.5,
            "same_family": False,
            "provenance": "proximity",
        },
    )


def _asserted_finding(a: str = _PAGE_A, b: str = _PAGE_B) -> audit_module.AuditFinding:
    return audit_module.AuditFinding(
        category="corpus_contradictions",
        severity="info",
        path=a,
        detail=f"Authored `contradicts` edge with {b!r} — you asserted these conflict.",
        proposed_fix="Surfaced for REVIEW only.",
        paths=[a, b],
        meta={
            "signal_version": "bbbbbbbbbbbbbbbb",
            "provenance": "asserted",
            "relation_type": "contradicts",
        },
    )


def _omitted_summary() -> audit_module.AuditFinding:
    return audit_module.AuditFinding(
        category="corpus_contradictions",
        severity="info",
        path="Knowledge Base",
        detail="7 more lower-priority/same-family contradiction pair(s) not shown.",
        proposed_fix="Work the surfaced pairs first.",
    )


def _enrichable(tmp_path, monkeypatch, *, predict=None) -> None:
    """Admit the verifier, wire the two pages' claims, open the claim-level gate."""
    fixture = _contradiction_fixture()
    _admit(tmp_path, monkeypatch, predict or _oracle_predict())
    _wire_claim_texts(monkeypatch, {_PAGE_A: fixture.claim_a, _PAGE_B: fixture.claim_b})
    monkeypatch.setenv("EXOMEM_CLAIM_LEVEL", "1")


# ---- 3.1 the admitted verifier is the only enrichment channel ----


def test_admitted_verifier_writes_the_label_with_digest_and_label_map(
    tmp_path, monkeypatch
) -> None:
    _enrichable(tmp_path, monkeypatch)
    digest = claims.verifier_admission().model_digest
    findings = [_proximity_finding()]

    audit_module._enrich_contradiction_polarity(tmp_path, findings)

    meta = findings[0].meta
    assert meta["polarity"] == "contradict"
    assert meta["polarity_method"] == "nli"
    assert meta["polarity_model_digest"] == digest
    assert meta["polarity_label_map_version"] == "v1"
    assert meta["polarity_signal_version"] == meta["signal_version"]
    assert 0.0 <= meta["polarity_score"] <= 1.0
    assert "CONTRADICT" in findings[0].detail


def test_refused_verifier_writes_no_heuristic_label(tmp_path, monkeypatch) -> None:
    """The heuristic never wears the verifier's name — and never appears at all."""
    fixture = _contradiction_fixture()
    _wire_claim_texts(monkeypatch, {_PAGE_A: fixture.claim_a, _PAGE_B: fixture.claim_b})
    monkeypatch.setenv("EXOMEM_CLAIM_LEVEL", "1")
    monkeypatch.setenv("EXOMEM_CLAIM_POLARITY_NLI", "1")
    monkeypatch.setattr(claims, "VERIFIER_PINS", ())
    findings = [_proximity_finding()]
    before = dict(findings[0].meta)

    audit_module._enrich_contradiction_polarity(tmp_path, findings)

    assert findings[0].meta == before
    assert not any(key.startswith("polarity") for key in findings[0].meta)
    assert "Claim-level check" not in findings[0].detail


def test_claim_level_gate_off_enriches_nothing(tmp_path, monkeypatch) -> None:
    _enrichable(tmp_path, monkeypatch)
    monkeypatch.delenv("EXOMEM_CLAIM_LEVEL", raising=False)
    findings = [_proximity_finding()]
    before = dict(findings[0].meta)

    audit_module._enrich_contradiction_polarity(tmp_path, findings)

    assert findings[0].meta == before


def test_enrichment_is_bounded_by_the_surfaced_set(tmp_path, monkeypatch) -> None:
    """Bounded by construction: it runs over the already-capped list it is given,
    so it can never widen the work the sweep does."""
    calls: list = []
    _enrichable(tmp_path, monkeypatch, predict=_oracle_predict(calls=calls))
    claims.verifier_admission()
    calls.clear()
    findings = [
        _proximity_finding(signal_version=f"sig{index:013d}") for index in range(3)
    ]

    audit_module._enrich_contradiction_polarity(tmp_path, findings)

    assert len(calls) == 3
    assert all(finding.meta["polarity"] == "contradict" for finding in findings)


def test_one_raising_pair_leaves_that_entry_unenriched_and_the_pass_completes(
    tmp_path, monkeypatch
) -> None:
    fixture = _contradiction_fixture()
    _enrichable(tmp_path, monkeypatch)
    claims.verifier_admission()
    monkeypatch.setattr(
        claims,
        "verifier_polarity",
        _exploding_on(fixture.claim_a, "Knowledge Base/Notes/Insights/boom.md"),
    )
    _wire_claim_texts(
        monkeypatch,
        {
            _PAGE_A: fixture.claim_a,
            _PAGE_B: fixture.claim_b,
            "Knowledge Base/Notes/Insights/boom.md": "boom",
        },
    )
    good = _proximity_finding()
    bad = _proximity_finding(a="Knowledge Base/Notes/Insights/boom.md", b=_PAGE_B)

    audit_module._enrich_contradiction_polarity(tmp_path, [bad, good])

    assert "polarity" not in bad.meta
    assert good.meta["polarity"] == "contradict"


def _exploding_on(ok_claim: str, _boom_path: str):
    def verifier_polarity(claim_a, claim_b):
        if claim_a != ok_claim:
            raise RuntimeError("forward pass exploded on this pair")
        return claims.PolarityResult("contradict", 0.99, "nli")

    return verifier_polarity


# ---- 3.3 staleness binding ----


def test_a_stale_label_is_dropped_not_served() -> None:
    finding = _proximity_finding(signal_version="1111111111111111")
    label = {
        "label": "contradict",
        "score": 0.9,
        "method": "nli",
        "model_digest": "d" * 64,
        "label_map_version": "v1",
        "signal_version": "2222222222222222",
    }
    before = dict(finding.meta)

    assert audit_module._attach_polarity_label(finding, label) is False
    assert finding.meta == before
    assert "Claim-level check" not in finding.detail


def test_a_current_label_is_attached() -> None:
    finding = _proximity_finding(signal_version="1111111111111111")
    label = {
        "label": "refine",
        "score": 0.7,
        "method": "nli",
        "model_digest": "d" * 64,
        "label_map_version": "v1",
        "signal_version": "1111111111111111",
    }

    assert audit_module._attach_polarity_label(finding, label) is True
    assert finding.meta["polarity"] == "refine"
    assert finding.meta["polarity_signal_version"] == "1111111111111111"


# ---- 3.4 asserted pairs ----


def test_asserted_pairs_carry_no_model_polarity_label(tmp_path, monkeypatch) -> None:
    """The author's assertion outranks a model's guess."""
    _enrichable(tmp_path, monkeypatch)
    asserted = _asserted_finding()
    before = dict(asserted.meta)

    audit_module._enrich_contradiction_polarity(tmp_path, [asserted])

    assert asserted.meta == before
    assert asserted.meta["provenance"] == "asserted"
    assert "Claim-level check" not in asserted.detail


def test_enrichment_touches_no_competing_alternatives_stance_key(
    tmp_path, monkeypatch
) -> None:
    """The reader-recorded pair stance is a triage disposition under its own
    contract; enrichment writes only into the polarity namespace."""
    _enrichable(tmp_path, monkeypatch)
    finding = _proximity_finding()
    finding.meta["stance"] = "competing"
    before = dict(finding.meta)

    audit_module._enrich_contradiction_polarity(tmp_path, [finding])

    added = set(finding.meta) - set(before)
    assert added == {
        "polarity",
        "polarity_score",
        "polarity_method",
        "polarity_model_digest",
        "polarity_label_map_version",
        "polarity_signal_version",
    }
    assert finding.meta["stance"] == "competing"


# ---- 3.2 invariance pins ----


def test_labelling_changes_no_signal_version_provenance_order_or_cap(
    tmp_path, monkeypatch
) -> None:
    _enrichable(tmp_path, monkeypatch)
    findings = [
        _proximity_finding(signal_version="sig0000000000001", priority=0.95),
        _proximity_finding(signal_version="sig0000000000002", priority=0.80),
        _asserted_finding(),
        _omitted_summary(),
    ]
    before = [
        (
            finding.path,
            tuple(finding.paths or ()),
            (finding.meta or {}).get("signal_version"),
            (finding.meta or {}).get("provenance"),
            (finding.meta or {}).get("priority"),
            finding.proposed_fix,
        )
        for finding in findings
    ]
    omitted_detail = findings[-1].detail

    audit_module._enrich_contradiction_polarity(tmp_path, findings)

    after = [
        (
            finding.path,
            tuple(finding.paths or ()),
            (finding.meta or {}).get("signal_version"),
            (finding.meta or {}).get("provenance"),
            (finding.meta or {}).get("priority"),
            finding.proposed_fix,
        )
        for finding in findings
    ]
    assert after == before                       # order, identity, ranking, provenance
    assert findings[-1].detail == omitted_detail  # cap and omitted-count accounting
    assert len(findings) == 4                     # nothing added, nothing dropped
    # And the labels really did arrive, so the pins above are not vacuous.
    assert findings[0].meta["polarity"] == "contradict"
    assert findings[1].meta["polarity"] == "contradict"


def test_a_dismissed_entry_stays_dismissed_when_a_label_arrives(
    tmp_path, monkeypatch
) -> None:
    """A triage decision binds to the entry's fingerprint, which is composed from
    the signal version the label may not change."""
    from exomem import review_state

    _enrichable(tmp_path, monkeypatch)
    finding = _proximity_finding()

    def fingerprint_of(entry):
        meta = entry.meta or {}
        return review_state.fingerprint(
            target_ref=entry.path,
            categories=[entry.category],
            reasons=[
                {
                    "category": entry.category,
                    "meta": {"signal_version": meta.get("signal_version")},
                }
            ],
            related_refs=list(entry.paths or []),
        )

    before = fingerprint_of(finding)
    audit_module._enrich_contradiction_polarity(tmp_path, [finding])

    assert finding.meta["polarity"] == "contradict"
    assert fingerprint_of(finding) == before


# ---------------- 5.1 fixture-set precision (claimed against fixtures ONLY) ----------------


def test_the_declared_heuristic_failures_are_real() -> None:
    """The `heuristic_fails` flags are checked against the actual heuristic.

    The precision table in tasks.md 5.1 is derived from these flags, so without
    this pin the table could drift into fiction while staying green.
    """
    for pair in claims.VERIFICATION_FIXTURES["stance-v1"]:
        verdict = claims._heuristic_polarity(pair.claim_a, pair.claim_b)
        assert (verdict.label != pair.expected) == pair.heuristic_fails, (
            pair.claim_a,
            pair.expected,
            verdict.label,
            pair.heuristic_fails,
        )


def test_an_admitted_verifier_answers_every_fixture(tmp_path, monkeypatch) -> None:
    """Admission's own bar, and the table's upper arm: every pair or nothing."""
    _admit(tmp_path, monkeypatch, _oracle_predict())
    assert claims.verifier_admission().admitted is True
    for pair in claims.VERIFICATION_FIXTURES["stance-v1"]:
        result = claims.verifier_polarity(pair.claim_a, pair.claim_b)
        assert result is not None
        assert result.label == pair.expected
