"""Embeddings-job acceptance for the multilingual activation fixtures (step 4, T10).

The served encoder, `BAAI/bge-m3` on ONNX Runtime int8, against
`context-activation-multilingual-v1`, semantic evidence on and off: the
design's usefulness bars (STEP-4 §9.3) on real vectors, its hard gates again,
the semantic stage's latency, and the E-arm (the digest-pinned English set,
semantic on, keeps its v1 verdicts). The planted-vector version of the same
cases is the core tier's (`test_context_activation_multilingual_product.py`);
this module is what a real encoder adds.

It loads a real model, so it runs only in the `retrieval-quality` job
(`pytest -m embeddings`, whose Hugging Face cache holds the model and its
host-built artefact) and is skipped wherever ONNX Runtime is not installed.

Promotion of the recent menu is not shipped (the T8 ruling), so the design's
menu bar (gold at rank 1) is reported, not asserted; that no poison is ever
promoted is asserted.
"""

from __future__ import annotations

import dataclasses
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("onnxruntime")
pytest.importorskip("tokenizers")
pytest.importorskip("huggingface_hub")

from epistemic.corpora import context_activation as english_set  # noqa: E402
from epistemic.corpora.context_activation_multilingual import (  # noqa: E402
    CASES,
    TWIN_OF,
    build_corpus,
)
from membench.utility.context_activation import packet_from_dict, score_case  # noqa: E402
from membench.utility.context_activation_multilingual import (  # noqa: E402
    multilingual_report,
    score_multilingual_case,
)

from exomem import (  # noqa: E402
    commands,
    embedding_backend,
    embeddings,
    lexstore,
    ranking_config,
    readiness,
    working_set,
    working_set_index,
    working_set_runtime,
)

pytestmark = [pytest.mark.embeddings, pytest.mark.timeout(3600)]

MODEL = "BAAI/bge-m3"
#: The design's bar for the semantic stage (§9.3), request-thread milliseconds.
SEMANTIC_STAGE_P95_MS = 250.0
#: Latency samples per content turn: every on-arm turn is compiled this many
#: times from a cold packet cache, so the p95 is over more than one pass.
LATENCY_PASSES = 3


@dataclasses.dataclass
class Measured:
    manifest: Any
    packets: dict[str, dict[str, dict]]
    encode_ms: dict[str, float]
    semantic_ms: list[float]
    english: dict[str, dict[str, Any]]
    english_packets: dict[str, dict[str, dict]]
    english_key_to_path: dict[str, str]
    english_state: str
    english_vectors: int
    fingerprint: str
    dim: int


_MEASURED: dict[str, Measured] = {}


def _load_encoder(monkeypatch: pytest.MonkeyPatch) -> tuple[str, int]:
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setenv(embeddings.ACTIVATION_MODEL_ENV, MODEL)
    monkeypatch.setattr(readiness, "should_defer", lambda _component: False)
    assert embedding_backend.served_artifact(MODEL) is not None
    embeddings.get_activation_model()
    fingerprint = embeddings.activation_fingerprint()
    assert fingerprint is not None and fingerprint.startswith(f"{MODEL}|")
    probe = embeddings.embed_activation_query_if_loaded("probe")
    return fingerprint, int(probe.shape[-1])


def _compile_arms(root: Path, turns: list[tuple[str, str, str]], monkeypatch) -> dict[str, dict[str, dict]]:
    """`turns` is (id, prior turn, turn); each is compiled on both arms from a
    cold packet cache."""
    packets: dict[str, dict[str, dict]] = {"on": {}, "off": {}}
    for arm in ("on", "off"):
        if arm == "off":
            monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
        else:
            monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
        for case_id, prior_turn, turn in turns:
            working_set_runtime.reset_caches_for_tests()
            token = commands.op_activate_context(root, turn=prior_turn).get("continuity") if prior_turn else None
            packets[arm][case_id] = commands.op_activate_context(
                root, turn=turn, continuity=token, include_timings=True
            )
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    return packets


def _measure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Measured:
    fingerprint, dim = _load_encoder(monkeypatch)

    root = tmp_path / "multilingual"
    manifest = build_corpus(root)
    real_recent = working_set._recent_mtimes
    monkeypatch.setattr(working_set, "_recent_mtimes", lambda _root: dict(manifest.recency))
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(root).rebuild(load_encoder=True)
    lexstore.ensure_fresh(root)

    encode_ms: dict[str, float] = {}
    real_query = embeddings.embed_activation_query_if_loaded

    def timed_query(text: str):
        started = time.perf_counter()
        try:
            return real_query(text)
        finally:
            encode_ms[text] = (time.perf_counter() - started) * 1000.0

    monkeypatch.setattr(embeddings, "embed_activation_query_if_loaded", timed_query)
    packets = _compile_arms(root, [(case.case_id, case.prior_turn, case.turn) for case in CASES], monkeypatch)
    semantic_ms: list[float] = []
    for _pass in range(LATENCY_PASSES):
        for case in CASES:
            working_set_runtime.reset_caches_for_tests()
            packet = commands.op_activate_context(root, turn=case.turn, include_timings=True)
            semantic_ms.append(float(packet["timings"]["stages"]["working_set.semantic"]["ms"]))

    # The English corpus carries its own recency on disk, and its turns are
    # timed by nobody.
    monkeypatch.setattr(working_set, "_recent_mtimes", real_recent)
    monkeypatch.setattr(embeddings, "embed_activation_query_if_loaded", real_query)
    english_root = tmp_path / "english"
    english_manifest = english_set.build_corpus(english_root)
    working_set_runtime.reset_caches_for_tests()
    index = working_set_index.WorkingSetIndex(english_root)
    index.rebuild(load_encoder=True)
    english_vectors = len(index.vector_matrix(fingerprint)[0])
    index.close()
    lexstore.ensure_fresh(english_root)
    # The English catalogue is smaller than the shipped population floor, where
    # the band would read `uncalibrated` and the arm would prove nothing. The
    # floor is lowered to the catalogue for this arm alone: a smaller N gives a
    # lower chance level, so the band fires more readily than it ever does as
    # shipped, which makes "no v1 verdict moves" the stricter claim.
    shipped = ranking_config.DEFAULT_RANKING
    monkeypatch.setattr(
        ranking_config,
        "DEFAULT_RANKING",
        dataclasses.replace(
            shipped,
            working_set_semantic_min_population=min(shipped.working_set_semantic_min_population, english_vectors),
        ),
    )
    binding = english_set.freeze_reference_binding(english_root, english_manifest, english_manifest.key_to_path)
    english_packets = _compile_arms(
        english_root, [(fixture.case_id, "", fixture.turn) for fixture in english_set.FIXTURES], monkeypatch
    )
    english: dict[str, dict[str, Any]] = {}
    for fixture in english_set.FIXTURES:
        english[fixture.case_id] = {
            arm: score_case(packet_from_dict(english_packets[arm][fixture.case_id]), fixture, reference_binding=binding)
            for arm in ("on", "off")
        }
    english_state = english_packets["on"][english_set.FIXTURES[0].case_id]["generation"]["semantic_evidence"]
    monkeypatch.setattr(ranking_config, "DEFAULT_RANKING", shipped)
    return Measured(
        manifest,
        packets,
        encode_ms,
        semantic_ms,
        english,
        english_packets,
        dict(english_manifest.key_to_path),
        english_state,
        english_vectors,
        fingerprint,
        dim,
    )


@pytest.fixture
def measured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Measured:
    """The real encoder's packets, compiled once for the module. A separate
    activation encoder is dropped afterwards; the shared one is recall's."""
    if "measured" not in _MEASURED:
        try:
            _MEASURED["measured"] = _measure(tmp_path, monkeypatch)
        finally:
            embeddings.unload_activation_model()
    return _MEASURED["measured"]


def _rows(measured: Measured):
    return [
        score_multilingual_case(
            case,
            measured.packets["on"][case.case_id],
            measured.packets["off"][case.case_id],
            key_to_path=measured.manifest.key_to_path,
            encode_ms=measured.encode_ms.get(case.turn),
        )
        for case in CASES
    ]


def _report(measured: Measured) -> dict:
    return multilingual_report(
        _rows(measured),
        encoder={"model": MODEL, "fingerprint": measured.fingerprint, "backend": "onnxruntime-int8", "dim": measured.dim},
    )


def test_the_served_encoder_banded_on_a_calibrated_catalogue(measured: Measured) -> None:
    states = {packet["generation"]["semantic_evidence"] for packet in measured.packets["on"].values()}
    assert states == {"ready"}, states
    assert {packet["generation"]["semantic_evidence"] for packet in measured.packets["off"].values()} == {"disabled"}


def test_every_named_positive_resolves_its_gold(measured: Measured) -> None:
    """M1-de, M1-ru and M5-ja: a rare name plus the band resolves the gold; the
    name alone (semantic off) leaves it partial."""
    report = _report(measured)
    named = {row["case_id"]: row for row in report["per_case"] if row["kind"] in ("named_rare", "cjk_named")}
    assert set(named) == {"M1-de", "M1-ru", "M5-ja"}
    for case_id, row in named.items():
        assert row["gold_resolved"] is True, (case_id, row)
        assert row["observed_status"]["off"] == "partial", (case_id, row)
    assert report["summary"]["named_resolution_rate"] == 1.0


def test_no_poison_is_banded_or_promoted(measured: Measured) -> None:
    report = _report(measured)
    offenders = {
        row["case_id"]: (row["poison_banded"], row["poison_promoted"])
        for row in report["per_case"]
        if row["poison_banded"] or row["poison_promoted"]
    }
    assert offenders == {}
    assert report["summary"]["false_band_rate"] == 0.0


def test_the_hard_gates_hold_on_real_vectors(measured: Measured) -> None:
    """No twin activates anything, no band resolves alone, no guarded key
    resolves, a content-free turn's menu is the off arm's, and the off arm is
    exactly the expected one. The carry-fragment rows are the lexical lane's
    (R-LEX task 1.8) and are pinned in the core tier."""
    gate_words = (
        "twin false activation",
        "vector_band alone",
        "guarded key resolved",
        "content-free",
        "vector_band served with semantic evidence off",
        "status off",
    )
    failures = {
        row.case_id: [reason for reason in row.failure_reasons if any(word in reason for word in gate_words)]
        for row in _rows(measured)
        if row.scored and row.kind != "carry_fragment"
    }
    assert {case_id: reasons for case_id, reasons in failures.items() if reasons} == {}


def test_the_recent_menu_is_never_reordered(measured: Measured) -> None:
    for case in CASES:
        assert (
            measured.packets["on"][case.case_id]["recent_context"]
            == measured.packets["off"][case.case_id]["recent_context"]
        ), case.case_id


def test_semantic_evidence_adds_nothing_but_the_gold_and_band_only_partials(measured: Measured) -> None:
    """Beyond a named positive's gold, turning semantic evidence on may only
    add an anchor at `partial` with `vector_band` as its whole evidence: a
    neighbour the turn never named, which resolves nothing and which the hook
    does not render. Nothing already in the packet changes."""
    for case in CASES:
        on = {item["path"]: (item["status"], frozenset(item["evidence"])) for item in measured.packets["on"][case.case_id]["anchors"]}
        off = {item["path"]: (item["status"], frozenset(item["evidence"])) for item in measured.packets["off"][case.case_id]["anchors"]}
        gold = {measured.manifest.key_to_path[key] for key in case.gold}
        for path, value in on.items():
            if path in gold and case.kind in ("named_rare", "cjk_named"):
                continue
            if path not in off:
                assert value == ("partial", frozenset({"vector_band"})), (case.case_id, path, value)
            else:
                assert value == off[path], (case.case_id, path, value, off[path])
        assert set(off) <= set(on), case.case_id


def test_no_twin_resolves_on_real_vectors(measured: Measured) -> None:
    for case in CASES:
        if case.kind in TWIN_OF:
            anchors = measured.packets["on"][case.case_id]["anchors"]
            assert all(item["status"] != "resolved" for item in anchors), (case.case_id, anchors)


def test_the_semantic_stage_meets_its_latency_bar(measured: Measured) -> None:
    ordered = sorted(measured.semantic_ms)
    p95 = ordered[min(len(ordered) - 1, max(0, -(-95 * len(ordered) // 100) - 1))]
    assert p95 <= SEMANTIC_STAGE_P95_MS, (p95, ordered[-5:])


def test_the_english_set_keeps_its_v1_verdicts_with_semantic_evidence_on(measured: Measured) -> None:
    """The E-arm: the 18 English fixtures, semantic on against semantic off,
    with the band actually running. No case's v1 verdict moves, no gold is
    lost and no poison is gained. Two statuses do move; they are pinned as
    known limits below. As shipped, this 18-anchor catalogue is under the
    band's floor and nothing moves at all."""
    assert measured.english_state == "ready"
    assert len(measured.english) == 18
    moved = {}
    for case_id, scores in measured.english.items():
        on, off = scores["on"], scores["off"]
        if on.passed != off.passed or on.gold_hit < off.gold_hit or on.poison_hit > off.poison_hit:
            moved[case_id] = {
                arm: (score.observed_status, score.gold_hit, score.poison_hit, score.passed)
                for arm, score in scores.items()
            }
    assert moved == {}


def _english_anchors(measured: Measured, arm: str, case_id: str) -> dict[str, tuple[str, frozenset[str]]]:
    return {
        item["path"]: (item["status"], frozenset(item["evidence"]))
        for item in measured.english_packets[arm][case_id]["anchors"]
    }


def test_known_limit_a_rare_word_plus_the_band_resolves_an_adjacent_turns_page(measured: Measured) -> None:
    """KNOWN LIMIT (step-4 ruling, 2026-09-25). T6 asks to convert the grill's
    target temperature, a turn about unit conversion that names the grill.
    "grill" is a rare word naming the grill page and the turn clears the band
    against it, so the page resolves on exactly the pair that resolves the
    multilingual golds (M1-de, M1-ru, M5-ja): tightening the rule would cost
    those. The off arm already hands out the same page as a partial. The agent
    can discount it; step-5 learning from agent picks is the corrective."""
    grill = measured.english_key_to_path["c2_grill_equipment_page"]
    on = _english_anchors(measured, "on", "T6")
    off = _english_anchors(measured, "off", "T6")
    assert measured.english["T6"]["on"].observed_status == "resolved"
    assert [path for path, (status, _e) in on.items() if status == "resolved"] == [grill], on
    assert {"rare_term", "vector_band"} <= on[grill][1], on[grill]
    assert off[grill][0] == "partial" and "vector_band" not in off[grill][1], off.get(grill)


def test_known_limit_the_band_completes_an_ambiguity_between_two_named_senses(measured: Measured) -> None:
    """KNOWN LIMIT (step-4 ruling, 2026-09-25). T4 names "Alex", whom two
    entities share, beside a deployment issue. With the band, both senses
    reach resolution and the packet reports them as `ambiguous`, which is
    T4's own expected status; without it the turn abstains `unresolved`. The
    band adds contact to senses the turn already named; it names none."""
    on = measured.english_packets["on"]["T4"]
    assert measured.english["T4"]["on"].observed_status == "ambiguous", on["anchors"]
    assert measured.english["T4"]["off"].observed_status == "unresolved"
    gold = {measured.english_key_to_path[key] for key in english_set.fixture_by_id("T4").gold}
    ref_to_path = {item["ref"]: item["path"] for item in on["anchors"]}
    senses = {ref_to_path.get(item["ref"], item["ref"]) for item in on["ambiguity"]}
    assert senses == gold, (senses, gold)

