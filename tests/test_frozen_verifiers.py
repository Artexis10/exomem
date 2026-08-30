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
