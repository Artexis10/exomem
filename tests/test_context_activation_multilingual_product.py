"""Core-tier product acceptance for the multilingual activation fixtures (step 4, T9).

Every case of `context-activation-multilingual-v1` runs through
`commands.op_activate_context` on the built corpus, once per arm:

* ``on``: a resident activation encoder with PLANTED vectors keyed by logical
  key (``ready``);
* ``off``: embeddings disabled (``disabled``);
* ``cold``: embeddings enabled, no encoder resident (``unavailable``);
* ``uncalibrated``: a band floor above the catalogue (``uncalibrated``).

This is not a model test. A planted turn points exactly at the page it is
about, and every other similarity is bounded far below the band's chance
level, so what is measured is the compiler's plumbing end to end: catalogue,
vector pipeline, band, resolver, continuity, carry and egress. Whether a real
encoder puts a German turn next to an English page is the embeddings job's
question (`test_context_activation_multilingual_embeddings.py`, T10).

The design's hard gates (STEP-4 §9.3) run here on every arm. Promotion of the
recent menu is not shipped (the T8 ruling), so the menu is pinned identical
across the arms rather than scored for relevance.
"""

from __future__ import annotations

import dataclasses
import hashlib
import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from epistemic.corpora.context_activation_multilingual import (
    CASES,
    TWIN_OF,
    build_corpus,
    case_by_id,
)
from membench.utility.context_activation_multilingual import (
    WORDED_CONTACT_KINDS,
    multilingual_report,
    packet_shape,
    score_multilingual_case,
)

from exomem import (
    commands,
    embeddings,
    lexstore,
    ranking_config,
    readiness,
    working_set,
    working_set_index,
    working_set_resolve,
    working_set_runtime,
)

pytestmark = pytest.mark.timeout(1800)

DIM = 1024
FINGERPRINT = "planted-multilingual|cls|l2|0000"
#: The share of every planted vector that lies on one shared circle, at an angle
#: drawn from its key. Two different keys meet only there, at `ARC * cos(angle)`
#: (plus a little noise from their own directions): a bounded similarity whose
#: largest value stays far below the band's chance level, where a Gaussian one
#: would clear it at its nominal rate. The key a turn is about meets it at 1.
ARC = 0.2

#: What each turn is about, by logical key. Every other turn (content-free, a
#: topic switch, a name inside an unrelated compound) points at nothing.
TOPICS: dict[str, str] = {
    "M1-de": "r_quillmere_kiln",
    "N1-de": "r_quillmere_kiln",
    "M1-ru": "r_pellimore_dinghy",
    "N1-ru": "r_pellimore_dinghy",
    "M2-en": "n_winter_tyres",
    "N2-en": "n_thermal_curtains",
    "M2-de": "n_sourdough_cold_proof",
    "N2-de": "n_photo_backup",
    "M2-ru": "n_knee_rehab",
    "N2-ru": "n_guitar_practice",
    "M2-ja": "n_tomato_watering",
    "N2-ja": "n_aquarium_water",
    "M4-ru": "n_bathroom_mould",
    "M5-ja": "r_shirakaba_hut",
    "M7-de": "n_de_waste_fees",
    "M7-et": "n_estonian_phrases",
    "M8-de": "n_de_delivery",
    "M9-ja": "n_ja_emergency_bag",
    "N9-ja": "f_travel_adapters",
}
PRIOR_TOPICS: dict[str, str] = {"M6-de": "r_lastenrad_fennholt"}
ARMS = ("on", "off", "cold", "uncalibrated")
DEGRADED = ("cold", "uncalibrated")


def planted(name: str) -> np.ndarray:
    digest = hashlib.sha256(name.encode("utf-8")).digest()
    angle = int.from_bytes(digest[:8], "little") / 2**64 * 2 * math.pi
    own = np.random.default_rng(int.from_bytes(digest[8:16], "little")).standard_normal(DIM - 2)
    own = own / np.linalg.norm(own) * math.sqrt(1 - ARC)
    arc = math.sqrt(ARC) * np.array([math.cos(angle), math.sin(angle)])
    return np.concatenate([arc, own]).astype(np.float32)


@dataclasses.dataclass
class Runs:
    manifest: Any
    packets: dict[str, dict[str, dict]]
    priors: dict[str, dict]
    encodes: dict[str, list[str]]
    refused: list[str]
    vectors: int
    anchors: dict[str, str]


_RUNS: dict[str, Runs] = {}


def _compute(root: Path, monkeypatch: pytest.MonkeyPatch) -> Runs:
    manifest = build_corpus(root)
    monkeypatch.setattr(working_set, "_recent_mtimes", lambda _root: dict(manifest.recency))
    path_to_key = {path: key for key, path in manifest.key_to_path.items()}
    working_set_runtime.reset_caches_for_tests()
    index = working_set_index.WorkingSetIndex(root)
    index.rebuild()
    title_key = {row.title: path_to_key.get(row.path) or f"anchor:{row.title}" for row in index.anchors()}
    anchors = {row.path: row.title for row in index.anchors()}
    index.close()
    lexstore.ensure_fresh(root)

    refused: list[str] = []

    def refuse(name: str):
        def _refused(*_args, **_kwargs):
            refused.append(name)
            raise AssertionError(f"activation reached {name}")

        return _refused

    # Recall's encoder is never activation's business, and the request path
    # never loads activation's own encoder: it is resident or absent.
    monkeypatch.setattr(embeddings, "get_model", refuse("get_model"))
    monkeypatch.setattr(embeddings, "embed_texts", refuse("embed_texts"))
    monkeypatch.setattr(embeddings, "get_activation_model", refuse("get_activation_model"))
    monkeypatch.setattr(readiness, "should_defer", lambda _component: False)
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(
        embeddings,
        "embed_activation_passages",
        lambda texts: np.vstack([planted(title_key.get(text.split("\n", 1)[0].strip(), text)) for text in texts]),
    )
    resident = {"fingerprint": FINGERPRINT}
    monkeypatch.setattr(embeddings, "activation_fingerprint", lambda: resident["fingerprint"])
    turn_topics = {case.turn: TOPICS.get(case.case_id) for case in CASES}
    turn_topics.update({case.prior_turn: PRIOR_TOPICS[case.case_id] for case in CASES if case.prior_turn})
    encodes: dict[str, list[str]] = {arm: [] for arm in ARMS}
    current = {"arm": "on"}

    def query(text: str) -> np.ndarray:
        encodes[current["arm"]].append(text)
        return planted(turn_topics.get(text) or f"turn:{text}")

    monkeypatch.setattr(embeddings, "embed_activation_query_if_loaded", query)
    index = working_set_index.WorkingSetIndex(root)
    index.rebuild(load_encoder=True)
    vectors = len(index.vector_matrix(FINGERPRINT)[0])
    index.close()

    base = ranking_config.DEFAULT_RANKING
    packets: dict[str, dict[str, dict]] = {arm: {} for arm in ARMS}
    priors: dict[str, dict] = {}
    for arm in ARMS:
        current["arm"] = arm
        monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
        resident["fingerprint"] = FINGERPRINT
        monkeypatch.setattr(ranking_config, "DEFAULT_RANKING", base)
        if arm == "off":
            monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
        elif arm == "cold":
            resident["fingerprint"] = None
        elif arm == "uncalibrated":
            monkeypatch.setattr(
                ranking_config,
                "DEFAULT_RANKING",
                dataclasses.replace(base, working_set_semantic_min_population=vectors + 1),
            )
        for case in CASES:
            # A different arm is a different process state that no cache key
            # names (a reaped encoder, a smaller catalogue): start each cold.
            working_set_runtime.reset_caches_for_tests()
            token = None
            if case.prior_turn:
                prior = commands.op_activate_context(root, turn=case.prior_turn)
                priors[f"{arm}:{case.case_id}"] = prior
                token = prior.get("continuity")
            packets[arm][case.case_id] = commands.op_activate_context(
                root, turn=case.turn, continuity=token, include_timings=True
            )
    monkeypatch.setattr(ranking_config, "DEFAULT_RANKING", base)
    return Runs(manifest, packets, priors, encodes, refused, vectors, anchors)


@pytest.fixture
def runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Runs:
    """Every arm's packets, compiled once for the module (they are pure data)."""
    if "runs" not in _RUNS:
        _RUNS["runs"] = _compute(tmp_path / "vault", monkeypatch)
    return _RUNS["runs"]


def _path(runs: Runs, key: str) -> str:
    return runs.manifest.key_to_path[key]


def _anchors(packet: dict) -> dict[str, tuple[str, frozenset[str]]]:
    return {item["path"]: (item["status"], frozenset(item["evidence"])) for item in packet["anchors"]}


def _rows(runs: Runs):
    return [
        score_multilingual_case(
            case,
            runs.packets["on"][case.case_id],
            runs.packets["off"][case.case_id],
            key_to_path=runs.manifest.key_to_path,
            degraded=[runs.packets[arm][case.case_id] for arm in DEGRADED],
        )
        for case in CASES
    ]


# --------------------------------------------------------------------------- #
# The planted space itself
# --------------------------------------------------------------------------- #


def test_planted_vectors_meet_only_the_page_a_turn_is_about() -> None:
    keys = [f"key-{i}" for i in range(80)]
    matrix = np.vstack([planted(key) for key in keys])
    config = ranking_config.DEFAULT_RANKING
    for about in ("key-3", "key-41"):
        bands, state = working_set_resolve.matrix_bands(tuple(keys), matrix, planted(about), config)
        assert state == "ready"
        assert {key for key, banded in bands.items() if banded} == {about}
    bands, _state = working_set_resolve.matrix_bands(tuple(keys), matrix, planted("turn:unrelated"), config)
    assert not any(bands.values())


def test_the_scorer_mirrors_the_compilers_worded_contact_kinds() -> None:
    assert WORDED_CONTACT_KINDS == working_set_resolve.WORDED_CONTACT_KINDS


# --------------------------------------------------------------------------- #
# The arms ran as intended
# --------------------------------------------------------------------------- #


def test_every_arm_reports_its_semantic_state(runs: Runs) -> None:
    assert runs.vectors >= 50, "the catalogue must calibrate the band"
    expected = {"on": "ready", "off": "disabled", "cold": "unavailable", "uncalibrated": "uncalibrated"}
    for arm, state in expected.items():
        observed = {packet["generation"]["semantic_evidence"] for packet in runs.packets[arm].values()}
        assert observed == {state}, (arm, observed)


def test_only_the_ready_arm_encodes_a_turn_and_nothing_loads_a_model(runs: Runs) -> None:
    assert sorted(runs.encodes["on"]) == sorted(
        text for case in CASES for text in (case.turn, case.prior_turn) if text
    )
    assert runs.encodes["off"] == runs.encodes["cold"] == runs.encodes["uncalibrated"] == []
    assert runs.refused == []


# --------------------------------------------------------------------------- #
# Hard gates (§9.3), every arm
# --------------------------------------------------------------------------- #


def test_every_scored_case_meets_the_hard_gates_on_both_arms(runs: Runs) -> None:
    failures = {
        row.case_id: row.failure_reasons
        for row in _rows(runs)
        if row.scored and row.failure_reasons and row.kind != "carry_fragment"
    }
    assert failures == {}


def test_no_twin_activates_anything_on_any_arm(runs: Runs) -> None:
    for case in CASES:
        if case.kind not in TWIN_OF:
            continue
        for arm in ARMS:
            packet = runs.packets[arm][case.case_id]
            resolved = [path for path, (status, _e) in _anchors(packet).items() if status == "resolved"]
            assert resolved == [], (case.case_id, arm, resolved)
            assert not packet["ambiguity"] and not packet["units"], (case.case_id, arm)
            assert not packet["pointers"] and not packet["current_state"], (case.case_id, arm)


def test_a_band_never_resolves_an_anchor_by_itself(runs: Runs) -> None:
    for arm in ARMS:
        priors = [(key, packet) for key, packet in runs.priors.items() if key.startswith(f"{arm}:")]
        for case_id, packet in [*runs.packets[arm].items(), *priors]:
            for path, (status, evidence) in _anchors(packet).items():
                if status == "resolved" and "vector_band" in evidence:
                    assert evidence & (WORDED_CONTACT_KINDS | {"continuity", "agent_choice"}), (arm, case_id, path)


def test_the_recent_menu_is_identical_on_every_arm(runs: Runs) -> None:
    """No promotion ships (the T8 ruling): relevance never reorders the menu,
    so every turn's `recent_context` is byte-identical across the arms, the
    content-free turns among them."""
    for case in CASES:
        menus = {arm: runs.packets[arm][case.case_id]["recent_context"] for arm in ARMS}
        assert all(menu == menus["off"] for menu in menus.values()), case.case_id
        assert menus["off"], case.case_id


def test_every_degraded_state_serves_exactly_the_off_packet(runs: Runs) -> None:
    for case in CASES:
        off = packet_shape(runs.packets["off"][case.case_id])
        for arm in DEGRADED:
            assert packet_shape(runs.packets[arm][case.case_id]) == off, (case.case_id, arm)
        for arm in ("off", *DEGRADED):
            for _path, (_status, evidence) in _anchors(runs.packets[arm][case.case_id]).items():
                assert "vector_band" not in evidence, (case.case_id, arm)


def test_semantic_evidence_changes_only_the_anchor_the_turn_is_about(runs: Runs) -> None:
    """With nothing but the planted topic in reach, the on arm is the off arm
    plus `vector_band` on that one anchor, and whatever resolution the band
    completes: no other anchor gains, loses or changes anything."""
    for case in CASES:
        on, off = _anchors(runs.packets["on"][case.case_id]), _anchors(runs.packets["off"][case.case_id])
        topic = TOPICS.get(case.case_id)
        about = _path(runs, topic) if topic and _path(runs, topic) in runs.anchors else None
        assert {path: value for path, value in on.items() if path != about} == {
            path: value for path, value in off.items() if path != about
        }, case.case_id
        if about is not None:
            assert "vector_band" in on[about][1], case.case_id
            assert on[about][1] - {"vector_band"} == off.get(about, ("", frozenset()))[1], case.case_id


# --------------------------------------------------------------------------- #
# The named and twin rows, exactly
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("case_id", ["M1-de", "M1-ru", "M5-ja"])
def test_a_rare_name_plus_the_band_resolves_and_the_name_alone_stays_partial(runs: Runs, case_id: str) -> None:
    (gold,) = case_by_id(case_id).gold
    on = _anchors(runs.packets["on"][case_id])[_path(runs, gold)]
    off = _anchors(runs.packets["off"][case_id])[_path(runs, gold)]
    assert on == ("resolved", frozenset({"rare_term", "vector_band"}))
    assert off == ("partial", frozenset({"rare_term"}))


@pytest.mark.parametrize(("case_id", "named"), [("N1-de", "e_idris_calloway"), ("N1-ru", "e_hollis_marchetti")])
def test_two_kinds_on_two_anchors_resolve_neither(runs: Runs, case_id: str, named: str) -> None:
    """Risk 4: the turn names anchor B and is about anchor A. B earns the word,
    A earns the band, and neither is resolved."""
    anchors = _anchors(runs.packets["on"][case_id])
    about = _path(runs, TOPICS[case_id])
    assert anchors[_path(runs, named)] == ("partial", frozenset({"rare_term"}))
    assert anchors[about] == ("partial", frozenset({"vector_band"}))


def test_a_name_inside_an_unrelated_compound_stays_partial(runs: Runs) -> None:
    """Risk 5: containment gives the hut its word; nothing else is about it."""
    hut = _path(runs, "r_shirakaba_hut")
    for arm in ARMS:
        assert _anchors(runs.packets[arm]["N5-ja"])[hut] == ("partial", frozenset({"rare_term"})), arm


def test_a_topic_switch_leaves_the_previous_anchor_behind(runs: Runs) -> None:
    """Risk 6: the previous turn resolved its anchor on its own words (and on
    the band where it is on); the next turn, on another subject and carrying
    the token, resolves nothing and the token qualifies nothing."""
    bike = _path(runs, "r_lastenrad_fennholt")
    for arm in ARMS:
        prior = _anchors(runs.priors[f"{arm}:M6-de"])
        assert prior[bike][0] == "resolved", arm
        assert ("vector_band" in prior[bike][1]) is (arm == "on"), arm
        packet = runs.packets[arm]["M6-de"]
        assert packet["abstained"] is True and packet["anchors"] == [], arm
        assert packet["generation"]["continuity"] == "stale", arm


def test_the_language_bias_twin_bands_no_same_language_anchor(runs: Runs) -> None:
    """Risk 2: the Russian turn about an English page never bands, resolves or
    carries the unrelated Russian anchor, and semantic evidence changes
    nothing in its packet on any arm.

    Lexically the turn does name that anchor and the Russian pelmeni note, as
    `retrieval_named` with nothing carried. Since keyword recall reads every
    script (#1366), Cyrillic words are real stems, and in this English-majority
    vault its function words are rare: `в` and `на`, said side by side, are a
    phrase both Russian pages contain. That is the minority-language residual
    (risk 8), and it belongs to the lexical carry, not to the band."""
    motoblok = _path(runs, "r_motoblok_kshatar")
    assert motoblok in runs.anchors
    off = _anchors(runs.packets["off"]["M4-ru"])
    for arm in ARMS:
        anchors = _anchors(runs.packets[arm]["M4-ru"])
        assert anchors == off, arm
        status, evidence = anchors.get(motoblok, ("absent", frozenset()))
        assert status not in {"resolved", "retrieval_carried"}, (arm, status)
        assert "vector_band" not in evidence, arm


def test_the_minority_function_word_residual_is_the_same_on_every_arm(runs: Runs) -> None:
    """M8 is reported, not scored: whatever the carry does with `zur
    Lieferung`, semantic evidence changes none of it."""
    shapes = {arm: packet_shape(runs.packets[arm]["M8-de"]) for arm in ARMS}
    assert len(set(shapes.values())) == 1, shapes


def test_a_single_accented_word_carries_nothing(runs: Runs) -> None:
    """Risk 7: one accented word is one unit, and a unit never pairs with
    itself, so `Gebührenbescheid?` and `Jätka.` carry no page on any arm."""
    for case_id in ("M7-de", "M7-et"):
        for arm in ARMS:
            packet = runs.packets[arm][case_id]
            assert "carried_by" not in packet["generation"], (case_id, arm)
            assert all(item["status"] != "retrieval_carried" for item in packet["anchors"]), (case_id, arm)


# --------------------------------------------------------------------------- #
# The report rows
# --------------------------------------------------------------------------- #


def test_the_multilingual_report_block_summarises_the_run(runs: Runs) -> None:
    report = multilingual_report(
        _rows(runs), encoder={"model": "planted", "fingerprint": FINGERPRINT, "backend": "planted", "dim": DIM}
    )
    summary = report["summary"]
    assert [row["case_id"] for row in report["per_case"]] == [case.case_id for case in CASES]
    assert summary["false_band_rate"] == 0.0
    assert summary["named_resolution_rate"] == 1.0
    assert summary["menu_gain"] == 0
    assert summary["degrade_identical"] is True
    assert summary["semantic_ms"]["n"] == len(CASES)
    by_id = {row["case_id"]: row for row in report["per_case"]}
    assert by_id["M1-de"]["observed_status"] == {"on": "resolved", "off": "partial"}
    assert by_id["M2-de"]["gold_menu_rank"] == {"on": None, "off": None}
    assert all(row["semantic_evidence"] == "ready" for row in report["per_case"])
