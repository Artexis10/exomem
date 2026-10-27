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
import json
import statistics
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
from test_latency_gate import (  # noqa: E402
    SLICE_ADDED_MS_CEILING,
    SLICE_ADDED_MS_TARGET,
    _compiler_ms,
    _paired_median_interval,
)

from exomem import (  # noqa: E402
    commands,
    context_catalogue,
    embedding_backend,
    embeddings,
    epistemic_graph,
    lexstore,
    ranking_config,
    readiness,
    working_set_heat,
    working_set_index,
    working_set_runtime,
)
from exomem.governance import principal  # noqa: E402

# This gate measures anchor resolution's semantic stage, which only the anchors compile runs.
pytestmark = [pytest.mark.embeddings, pytest.mark.timeout(3600), pytest.mark.usefixtures("anchors_packet")]

MODEL = "BAAI/bge-m3"
#: The design's bar for the semantic stage (§9.3), request-thread milliseconds.
SEMANTIC_STAGE_P95_MS = 250.0
#: Latency samples per content turn: every on-arm turn is compiled this many
#: times from a cold packet cache, so the p95 is over more than one pass.
LATENCY_PASSES = 3
#: The named golds, and one ordinary note each that shares a phrase with the
#: turn but is not what the turn names: a cleaning rota that mentions the
#: glaze firing, a shed roof that leaks after winter, a shopping list for next
#: month's retreat. Written into the multilingual vault after it has been
#: measured, so every other assertion here reads the vault without them.
NAMED_GOLDS = ("M1-de", "M1-ru", "M5-ja")
INCIDENTAL_NOTES = {
    "putzplan.md": (
        "Putzplan Gemeinschaftsraum",
        "Nach dem Glasurbrand am Donnerstag ist die Teeküche dran; Besen und Eimer stehen im Flur.",
    ),
    "saraj.md": (
        "Сарай у озера",
        "Крыша сарая протекает после зимы; ведро стоит у двери, пока не придёт кровельщик.",
    ),
    "kaidashi.md": (
        "備品メモ",
        "来月の合宿でプロジェクターをまた借りられるか確認してくれる人を探す。",
    ),
}
_INCIDENTAL_NOTE = """---
type: note
status: active
tags: [misc]
created: "2026-09-01T02:00:39Z"
updated: "2026-09-01T02:00:39Z"
---
# {title}

## Observations

- [note] {body} #misc
"""


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
    incidental: dict[str, dict[str, dict]]
    incidental_carry: dict[str, tuple[str, ...]]
    #: Each case's `turn_slice` from the maps packet, semantic evidence on.
    slices: dict[str, dict]
    #: The maps packet's time with the slice on minus off, one value per pair (`_slice_added_ms`).
    slice_added_ms: list[float]


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


def _turn_slices(root: Path, turns: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch) -> dict[str, dict]:
    """Each turn's `turn_slice` from the maps packet with the slice on, the
    encoder loaded, and the catalogue the maps packet reads published first."""
    epistemic_graph.EpistemicGraphIndex(root).rebuild_all()
    context_catalogue.ContextCatalogue(root).rebuild()
    slices: dict[str, dict] = {}
    with monkeypatch.context() as scope:
        scope.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
        scope.delenv("EXOMEM_ACTIVATION_TURN_SLICE", raising=False)
        scope.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
        for case_id, turn in turns:
            working_set_runtime.reset_caches_for_tests()
            slices[case_id] = commands.op_activate_context(root, turn=turn)["turn_slice"]
    return slices


def _slice_added_ms(root: Path, turns: list[str], monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The latency gate's paired measurement with the encoder loaded, where the slice's compile also
    encodes the turn: for the owner, each turn's maps packet with the slice off and on, each from a cold
    packet cache, in an order that alternates by pass so drift cancels. `LATENCY_PASSES` pairs per turn."""
    differences: list[float] = []
    with monkeypatch.context() as scope, principal.request_scope(principal.owner_principal(surface="library")):
        scope.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
        scope.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
        for setting in ("off", "on"):
            scope.setenv("EXOMEM_ACTIVATION_TURN_SLICE", setting)
            commands.op_activate_context(root, turn=turns[0])  # warm each path once
        for index in range(LATENCY_PASSES):
            for turn in turns:
                total: dict[str, float] = {}
                for setting in ("off", "on") if index % 2 == 0 else ("on", "off"):
                    scope.setenv("EXOMEM_ACTIVATION_TURN_SLICE", setting)
                    working_set_runtime.reset_caches_for_tests(packets_only=True)
                    packet = commands.op_activate_context(root, turn=turn, include_timings=True)
                    assert ("turn_slice" in packet) == (setting == "on"), packet.get("abstention")
                    total[setting] = _compiler_ms(packet["timings"])
                differences.append(total["on"] - total["off"])
    return differences


def _measure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Measured:
    fingerprint, dim = _load_encoder(monkeypatch)

    root = tmp_path / "multilingual"
    manifest = build_corpus(root)
    # The recent-context block reads the heat projection, not file times
    # (step 5): seed it directly with one `work` event per fixture page, at
    # the manifest's own explicit mtimes-as-ts_ns. Written to THIS root's own
    # sidecar, so -- unlike the old root-agnostic monkeypatch -- it never
    # needs restoring before the English corpus below is built on a
    # different root (review: merge fallout from step 5).
    working_set_heat.reset_for_tests()
    assert working_set_heat.append(
        root,
        sorted(
            working_set_heat.HeatEvent(ts_ns, path, "work", origin="fixture")
            for path, ts_ns in manifest.recency.items()
        ),
    )
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

    slices = _turn_slices(root, [(case.case_id, case.turn) for case in CASES], monkeypatch)
    slice_added_ms = _slice_added_ms(root, [case.turn for case in CASES], monkeypatch)

    # The same vault with an ordinary note beside each named gold that shares
    # a phrase with its turn, so the retrieval carry names that note instead.
    for name, (title, body) in INCIDENTAL_NOTES.items():
        (root / "Knowledge Base" / "Notes" / "Insights" / name).write_text(
            _INCIDENTAL_NOTE.format(title=title, body=body), encoding="utf-8"
        )
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(root).rebuild(load_encoder=True)
    lexstore.ensure_fresh(root)
    named_cases = [case for case in CASES if case.case_id in NAMED_GOLDS]
    incidental_carry = {
        case.case_id: tuple(path for path, _score in working_set_runtime.carry_candidates(root, case.turn)[0])
        for case in named_cases
    }
    incidental = _compile_arms(root, [(case.case_id, case.prior_turn, case.turn) for case in named_cases], monkeypatch)

    # The English corpus carries its own recency on disk, in its own vault
    # root's heat sidecar -- untouched by the seeding above -- and its turns
    # are timed by nobody.
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
        incidental,
        incidental_carry,
        slices,
        slice_added_ms,
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


def test_an_incidental_note_the_carry_names_never_costs_a_named_gold(measured: Measured) -> None:
    """Each named gold's turn also shares a phrase with an ordinary note. The
    carry names that note for the German and Russian turns (an unspaced
    Japanese turn has no phrase the carry can pair, so it names nothing).
    The turn named the gold by its rare name, not the note, so the gold still
    resolves on the band, exactly as it does with no such note in the vault,
    and never resolves without it."""
    notes = {name: f"Knowledge Base/Notes/Insights/{name}" for name in INCIDENTAL_NOTES}
    assert measured.incidental_carry == {
        "M1-de": (notes["putzplan.md"],),
        "M1-ru": (notes["saraj.md"],),
        "M5-ja": (),
    }
    for case in CASES:
        if case.case_id not in NAMED_GOLDS:
            continue
        (gold,) = (measured.manifest.key_to_path[key] for key in case.gold)
        on = {item["path"]: item["status"] for item in measured.incidental["on"][case.case_id]["anchors"]}
        off = {item["path"]: item["status"] for item in measured.incidental["off"][case.case_id]["anchors"]}
        assert on.get(gold) == "resolved", (case.case_id, on)
        assert "carried_by" not in measured.incidental["on"][case.case_id]["generation"], case.case_id
        assert off.get(gold) != "resolved", (case.case_id, off)


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


def test_the_turn_slice_adds_a_bounded_paired_cost_with_the_encoder_loaded(measured: Measured) -> None:
    """The latency gate's bound on what the slice adds, on these turns with the real encoder loaded: the
    upper end of the median paired difference's 95% interval is at most `SLICE_ADDED_MS_CEILING`. The
    target is reported, never asserted."""
    differences = measured.slice_added_ms
    low, high = _paired_median_interval(differences)
    report = {
        "pairs": len(differences),
        "median_added_ms": round(statistics.median(differences), 1),
        "ci95_added_ms": [round(low, 1), round(high, 1)],
        "target_ms": SLICE_ADDED_MS_TARGET,
        "within_target": high <= SLICE_ADDED_MS_TARGET,
    }
    print("turn slice paired latency, encoder loaded " + json.dumps(report, sort_keys=True))
    assert high <= SLICE_ADDED_MS_CEILING, report


def test_the_english_set_keeps_its_v1_verdicts_with_semantic_evidence_on(measured: Measured) -> None:
    """The E-arm: the 18 English fixtures, semantic on against semantic off,
    with the band actually running. No case's v1 verdict moves, no gold is
    lost and no poison is gained. As shipped, this 18-anchor catalogue is
    under the band's floor and nothing moves at all."""
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


def test_scoped_conversion_does_not_carry_unrelated_settings_on_either_arm(measured: Measured) -> None:
    """T6's property-only body match cannot serve saved oven settings, and
    rejecting them must not resurrect the weak ordinary-word grill anchor."""
    grill = measured.english_key_to_path["c2_grill_equipment_page"]
    oven = "Knowledge Base/Notes/Kitchen/oven-temperature-conversions.md"
    for arm in ("off", "on"):
        packet = measured.english_packets[arm]["T6"]
        assert packet["abstained"]
        assert _english_anchors(measured, arm, "T6").get(grill, ("absent",))[0] != "resolved"
        assert not any(unit["provenance"]["path"] == oven for unit in packet["units"])
        assert not any(pointer["ref"] == oven for pointer in packet["pointers"])
    assert measured.english["T6"]["on"].poison_hit <= measured.english["T6"]["off"].poison_hit


def test_a_bare_shared_first_name_is_ambiguous_with_and_without_the_band(measured: Measured) -> None:
    """T4 names "Alex", whom two entities share, beside a deployment issue.
    Since the activation-quality round a bare name two people share is asked
    about on its own (`working_set_resolve._bare_name_groups`), so the turn is
    `ambiguous` between both senses on the off arm as well, which is T4's own
    expected status. It was a known limit while only the band completed the
    ambiguity (the off arm abstained `unresolved`); the band no longer adds
    anything here, and it must not change the senses either arm names."""
    on = measured.english_packets["on"]["T4"]
    off = measured.english_packets["off"]["T4"]
    assert measured.english["T4"]["on"].observed_status == "ambiguous", on["anchors"]
    assert measured.english["T4"]["off"].observed_status == "ambiguous", off["anchors"]
    gold = {measured.english_key_to_path[key] for key in english_set.fixture_by_id("T4").gold}
    for packet in (on, off):
        ref_to_path = {item["ref"]: item["path"] for item in packet["anchors"]}
        senses = {ref_to_path.get(item["ref"], item["ref"]) for item in packet["ambiguity"]}
        assert senses == gold, (senses, gold)


#: How a lead was reached, from the packet's own closed set, for the leads that
#: the harness counts as the turn activating a page (`_surfaced`): a resolved
#: anchor or an ambiguity sense. The slice also lists weaker leads (`partial`,
#: `named`, `related`), which it labels as not checked for relevance.
_ACTIVATING_MATCHES = frozenset({"resolved", "ambiguous"})


def _slice_leads(measured: Measured, case_id: str) -> dict[str, dict]:
    block = measured.slices[case_id]
    assert block["state"] in ("ready", "empty"), (case_id, block)
    return {item["ref"]: item for item in block["items"]}


def test_every_named_positive_gold_is_a_resolved_turn_slice_lead(measured: Measured) -> None:
    """The maps packet's slice lists what the anchors packet resolves: the rare
    name plus the band reaches the gold, so the gold leads its slice."""
    for case in CASES:
        if case.case_id not in NAMED_GOLDS:
            continue
        (gold,) = (measured.manifest.key_to_path[key] for key in case.gold)
        lead = _slice_leads(measured, case.case_id).get(gold)
        assert lead is not None and lead["match"] == "resolved", (case.case_id, measured.slices[case.case_id])


def test_no_twin_or_guarded_page_is_an_unsound_turn_slice_lead(measured: Measured) -> None:
    """A twin activates nothing beyond its own gold, and a page the case guards
    (poison, or never resolved) is never a resolved, ambiguous or carried lead
    nor reached by the band. A guarded page that the turn names by its title may
    still appear as a `named` lead: that lead is labelled as unchecked, and the
    anchors packet serves the same page the same way."""
    for case in CASES:
        leads = _slice_leads(measured, case.case_id)
        own_gold = {measured.manifest.key_to_path[key] for key in case.gold}
        guarded = {measured.manifest.key_to_path[key] for key in (*case.poison, *case.never_resolved)}
        if case.kind in TWIN_OF:
            activated = {ref for ref, item in leads.items() if item["match"] in _ACTIVATING_MATCHES}
            assert activated <= own_gold, (case.case_id, activated - own_gold)
        for ref in guarded & set(leads):
            item = leads[ref]
            assert item["match"] not in (*_ACTIVATING_MATCHES, "carried"), (case.case_id, item)
            assert "vector_band" not in item["evidence"], (case.case_id, item)
