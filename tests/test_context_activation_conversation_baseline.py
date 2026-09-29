"""Product runner and arm-(a) baseline for the `conversation` group (S0).

Every case of `context-activation-conversation-v1` runs through
`commands.op_activate_context` on the built corpus, on the arms its `arms`
name. The runner has ONE request builder (:func:`build_request`) and ONE call
helper (:func:`run_arm`):

* arm (a) sends the turn alone. It runs on any compiler and is the
  mechanism-removal control: the cases that need the conversation must fail it;
* arms (b), (c) and (d) send `conversation`. A product whose
  `op_activate_context` does not take that argument answers
  ``refused: unknown argument`` for the arm, recorded and never raised. Once
  the product accepts the argument the same helper passes it through, so the
  arms start running with no change here. Their floors are the acceptance run
  of the change (S5), not asserted here.

The baseline manifest (`baseline-arm-a.json` in the change directory) records
what the compiler on `main` served for arm (a) before any product change: the
fixture digest and corpus hash it references, every case's per-anchor-kind
result, which incident classes fail, and that the attachment cases abstain.
It carries no timestamp and no path. Editing a fixture after it exists voids
it: the digest test below goes red, and the run must be redone, never
rescored. Re-record it with ``EXOMEM_WRITE_CONVERSATION_BASELINE=1``.

Embeddings are disabled (the compiler under test is the model-free one).
"""

from __future__ import annotations

import base64
import copy
import dataclasses
import inspect
import json
import os
from pathlib import Path
from typing import Any

import pytest
from epistemic.corpora.context_activation_conversation import (
    ARMS,
    CASES,
    CORPUS_ID,
    FIXTURE_SET_ID,
    KEY_KINDS,
    RESTRICTED_AUDIENCE,
    ConversationCase,
    build_corpus,
    case_by_id,
    conversation_for_arm,
    fixture_set_digest,
)
from membench.utility.context_activation_conversation import (
    CONVERSATION_ARMS,
    ConversationRow,
    arm_floors,
    incident_classes,
    mechanism_removal,
    observed_status,
    row_to_dict,
    score_conversation_case,
    score_withheld_pair,
    served_keys,
)

from exomem import commands, lexstore, working_set_index, working_set_runtime
from exomem.governance.principal import RequestPrincipal, owner_principal, request_scope

pytestmark = pytest.mark.timeout(1800)

BASELINE_PATH = (
    Path(__file__).resolve().parents[1]
    / "openspec"
    / "changes"
    / "add-thread-aware-compilation"
    / "baseline-arm-a.json"
)
REFUSED = "refused: unknown argument"


# --------------------------------------------------------------------------- #
# The request builder and the call helper (the one place `conversation` flows)
# --------------------------------------------------------------------------- #


def product_accepts_conversation() -> bool:
    return "conversation" in inspect.signature(commands.op_activate_context).parameters


def build_request(case: ConversationCase, arm: str, key_to_ref: dict[str, str]) -> dict[str, Any]:
    """The keyword arguments one (case, arm) sends: the turn, and `conversation`
    when the arm carries one. Nothing else in this module builds a request."""

    request: dict[str, Any] = {"turn": case.turn}
    conversation = conversation_for_arm(case, arm, key_to_ref)
    if conversation is not None:
        request["conversation"] = conversation
    return request


def principal_for(case: ConversationCase) -> RequestPrincipal:
    if case.caller == "restricted":
        return RequestPrincipal(audience_id=RESTRICTED_AUDIENCE, surface="mcp", purpose=None)
    return owner_principal()


def run_arm(root: Path, case: ConversationCase, arm: str, key_to_ref: dict[str, str]) -> dict[str, Any] | str:
    """One packet, or ``REFUSED`` when the arm sends `conversation` and the
    product does not take it. A product refusal never crashes the run."""

    request = build_request(case, arm, key_to_ref)
    if "conversation" in request and not product_accepts_conversation():
        return REFUSED
    working_set_runtime.reset_caches_for_tests()
    with request_scope(principal_for(case)):
        try:
            return commands.op_activate_context(root, **request)
        except TypeError as error:
            if "conversation" in str(error) and "unexpected keyword" in str(error):
                return REFUSED
            raise


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #


@dataclasses.dataclass
class Run:
    manifest: Any
    key_to_ref: dict[str, str]
    #: (case_id, arm) -> packet dict, or REFUSED
    packets: dict[tuple[str, str], dict[str, Any] | str]
    rows: list[ConversationRow]


_RUN: dict[str, Run] = {}


def _execute(root: Path, monkeypatch: pytest.MonkeyPatch) -> Run:
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    manifest = build_corpus(root)
    lexstore.ensure_fresh(root)
    working_set_runtime.reset_caches_for_tests()
    index = working_set_index.WorkingSetIndex(root)
    index.rebuild()
    by_path = {anchor.path: anchor.ref for anchor in index.anchors() if anchor.path}
    index.close()
    key_to_ref = {key: by_path.get(path) or path for key, path in manifest.key_to_path.items()}
    packets: dict[tuple[str, str], dict[str, Any] | str] = {}
    for case in CASES:
        for arm in case.arms:
            packets[(case.case_id, arm)] = run_arm(root, case, arm, key_to_ref)
    scored = {
        pair: packet for pair, packet in packets.items() if not isinstance(packet, str)
    }
    emitted = any(
        "origin" in anchor for packet in scored.values() for anchor in packet.get("anchors") or ()
    )
    rows = [
        score_conversation_case(
            case_by_id(case_id),
            arm,
            packet,
            key_to_path=manifest.key_to_path,
            key_to_ref=key_to_ref,
            origin_emitted=emitted,
        )
        for (case_id, arm), packet in scored.items()
    ]
    return Run(manifest, key_to_ref, packets, rows)


@pytest.fixture
def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Run:
    """Every packet of the group, compiled once for the module."""

    if "run" not in _RUN:
        _RUN["run"] = _execute(tmp_path / "vault", monkeypatch)
    return _RUN["run"]


def _row(run: Run, case_id: str, arm: str = "a") -> ConversationRow:
    (row,) = [item for item in run.rows if item.case_id == case_id and item.arm == arm]
    return row


def baseline_manifest(run: Run) -> dict[str, Any]:
    """The deterministic arm-(a) manifest: no timestamp, no path, no random id."""

    arm_a = [row for row in run.rows if row.arm == "a"]
    unsent = sorted(
        (case_id, arm)
        for (case_id, arm), packet in run.packets.items()
        if arm != "a"
        and not isinstance(packet, str)
        and conversation_for_arm(case_by_id(case_id), arm, run.key_to_ref) is None
    )
    attachments = [case for case in CASES if case.kind == "attachment"]
    pair = [case for case in CASES if case.kind in {"withheld_ref", "absent_ref"}]
    withheld = next(case for case in pair if case.kind == "withheld_ref")
    absent = next(case for case in pair if case.kind == "absent_ref")
    return {
        "manifest": "context-activation-conversation-baseline-arm-a",
        "fixture_set_id": FIXTURE_SET_ID,
        "fixture_set_digest": fixture_set_digest(),
        "corpus_id": CORPUS_ID,
        "corpus_hash": run.manifest.logical_hash,
        "corpus_hash_note": (
            "sha256 over the corpus pages with the product writers' minted ids and audit "
            "trailers removed; the exact-bytes hash differs on every build"
        ),
        "compiler": {
            "revision": "main before add-thread-aware-compilation",
            "accepts_conversation_argument": product_accepts_conversation(),
            "embeddings": "disabled",
        },
        "arms_run": ["a"],
        "arm_a": [row_to_dict(row) for row in sorted(arm_a, key=lambda item: item.case_id)],
        "conversation_arms": {
            "refused": [
                {"case_id": case_id, "arm": arm, "result": REFUSED}
                for case_id, arm in sorted(
                    (case_id, arm) for (case_id, arm), packet in run.packets.items() if packet == REFUSED
                )
            ],
            "sent_no_conversation_so_identical_to_arm_a": [
                {"case_id": case_id, "arm": arm} for case_id, arm in unsent
            ],
        },
        "verdicts": {
            "mechanism_removal": mechanism_removal(run.rows),
            "incident_classes_failing_on_arm_a": incident_classes(run.rows),
            "attachment_cases_abstain_on_arm_a": {
                case.case_id: _row(run, case.case_id).observed_status == "unresolved" for case in attachments
            },
            "twins_failing_on_arm_a": sorted(
                row.case_id for row in arm_a if row.is_twin and not row.passed
            ),
            "drowning_cases_failing_on_arm_a": sorted(
                row.case_id for row in arm_a if row.kind == "drowning" and not row.passed
            ),
            "withheld_pair_byte_identical_on_arm_a": score_withheld_pair(
                withheld,
                absent,
                "a",
                run.packets[(withheld.case_id, "a")],
                run.packets[(absent.case_id, "a")],
            ).identical,
            "origin_label": (
                "not_emitted" if not any(row.origin_verdict in {"match", "mismatch"} for row in arm_a) else "emitted"
            ),
        },
    }


# --------------------------------------------------------------------------- #
# The scorer, on synthetic packets (pure)
# --------------------------------------------------------------------------- #

_PATHS = {key: f"Knowledge Base/{key}.md" for key in KEY_KINDS}
_REFS = {key: f"exomem://memory/{key}" for key in KEY_KINDS}


def _anchor(key: str, status: str = "resolved", **extra) -> dict[str, Any]:
    return {"ref": _REFS[key], "path": _PATHS[key], "title": key, "status": status, "evidence": ["exact_alias"], **extra}


def _unit(key: str, text: str) -> dict[str, Any]:
    return {"ref": _REFS[key], "text": text, "provenance": {"path": _PATHS[key], "anchor": _REFS[key]}}


def _packet(anchors=(), units=(), *, abstained=False, ambiguity=(), **generation) -> dict[str, Any]:
    return {
        "anchors": list(anchors),
        "units": list(units),
        "pointers": [],
        "current_state": [],
        "ambiguity": list(ambiguity),
        "abstained": abstained,
        "abstention": {"reason": "unresolved"} if abstained else None,
        "generation": dict(generation),
    }


def _score(case_id: str, arm: str, packet: dict[str, Any], **kwargs) -> ConversationRow:
    return score_conversation_case(
        case_by_id(case_id), arm, packet, key_to_path=_PATHS, key_to_ref=_REFS, **kwargs
    )


def _served_packet(case: ConversationCase, *, origin: str | None = None) -> dict[str, Any]:
    extra = {} if origin is None else {"origin": origin}
    return _packet(
        [_anchor(key, **extra) for key in case.gold],
        [_unit(key, " ".join(case.must_include)) for key in case.gold],
    )


def test_a_packet_that_serves_the_case_as_registered_passes_with_full_recall() -> None:
    case = case_by_id("V3")
    row = _score("V3", "a", _served_packet(case, origin="turn"))
    assert row.passed, row.failure_reasons
    assert row.observed_status == "resolved"
    assert {kind: metrics.recall for kind, metrics in row.per_kind.items()} == {
        "entity": 1.0,
        "hub": 1.0,
        "resource": None,
    }
    assert row.per_kind["resource"].poison_served == 0 and row.origin_verdict == "match"


def test_a_missing_second_domain_fails_its_kind_and_the_recall_floor() -> None:
    case = case_by_id("V13")
    packet = _packet(
        [_anchor("e_perpetua_holt"), _anchor("h_tidewater", "partial")],
        [_unit("e_perpetua_holt", "approves any spend above five hundred")],
    )
    row = _score("V13", "a", packet)
    assert not row.passed
    assert row.per_kind["hub"].recall == 0.0 and row.per_kind["entity"].recall == 1.0
    assert any("gold not served" in reason for reason in row.failure_reasons)
    assert arm_floors([dataclasses.replace(row, arm="b")], "b")["recall_below_floor"] == [
        {"case_id": "V13", "kind": "hub"}
    ]
    assert case.gold == ("e_perpetua_holt", "h_tidewater")


def test_a_drowning_case_fails_outright_however_good_its_recall() -> None:
    case = case_by_id("V22")
    packet = _served_packet(case, origin="turn")
    packet["units"].append(_unit("h_harbor_lantern", "The Harbor Lantern budget is drowning the turn."))
    row = _score("V22", "b", packet)
    assert row.per_kind["entity"].recall == 1.0
    assert row.drowned and not row.passed
    assert "drowning" in " ".join(row.failure_reasons)
    assert arm_floors([row], "b")["drowning_failures"] == ["V22"]
    # The same material on a case that is not a drowning case is poison, not drowning.
    other = _score("V24", "b", _served_packet(case_by_id("V24"), origin="turn"))
    assert not other.drowned


def test_a_must_exclude_fact_in_the_text_is_poison_even_when_no_poison_key_is_served() -> None:
    case = case_by_id("V22")
    packet = _served_packet(case)
    packet["units"].append(_unit("e_wilhelmina_pryce", "the Harbor Lantern budget caps the signal-repair spend at forty thousand"))
    row = _score("V22", "a", packet)
    assert row.drowned and any("must-exclude" in reason for reason in row.failure_reasons)


def test_a_twin_that_resolves_an_anchor_beyond_its_gold_is_a_false_activation() -> None:
    row = _score("W22", "b", _packet([_anchor("h_harbor_lantern")], [_unit("h_harbor_lantern", "x")]))
    assert row.is_twin and row.other_resolved == ("h_harbor_lantern",)
    assert not row.passed and arm_floors([row], "b")["twin_false_activation"] == ["W22"]
    assert _score("W22", "b", _packet(abstained=True)).passed


def test_a_carry_is_read_from_carried_by_and_the_single_partial_anchor() -> None:
    case = case_by_id("V18")
    packet = _packet(
        [_anchor("e_marigold_tenby", "partial", origin="conversation")],
        [_unit("e_marigold_tenby", " ".join(case.must_include))],
        carried_by="conversation",
    )
    row = _score("V18", "b", packet)
    assert row.passed, row.failure_reasons
    assert row.observed_status == "partial" and row.origin_verdict == "match"
    wrong = _score("V18", "b", _packet([_anchor("e_marigold_tenby", "partial")], carried_by="retrieval"))
    assert not wrong.passed
    # An unresolved abstention is not the carry, and lists nothing as served.
    assert _score("V18", "a", _packet(abstained=True)).failure_reasons


def test_origin_is_compared_only_where_the_product_emits_it() -> None:
    case = case_by_id("V1")
    unlabeled = _score("V1", "a", _served_packet(case))
    assert unlabeled.passed and unlabeled.origin_verdict == "not_emitted"
    wrong = _score("V1", "a", _served_packet(case, origin="focus"))
    assert wrong.origin_verdict == "mismatch" and not wrong.passed
    forced = _score("V1", "a", _served_packet(case), origin_emitted=True)
    assert forced.origin_verdict == "mismatch"


def test_the_arm_expectation_overrides_and_the_attachment_control_arm_must_abstain() -> None:
    case = case_by_id("V26")
    assert case.expectation("a")["status"] == "unresolved" and case.expectation("c")["status"] == "resolved"
    assert case.expectation("d")["origin"] == {"e_ottilie_marsh": "focus", "h_tidewater": "focus"}
    assert _score("V26", "a", _packet(abstained=True)).passed
    assert not _score("V26", "c", _packet(abstained=True)).passed
    with pytest.raises(ValueError):
        _score("V26", "b", _packet(abstained=True))


def test_the_withheld_pair_is_scored_for_byte_identity_generation_included() -> None:
    withheld, absent = case_by_id("V28"), case_by_id("W28")
    packet = _packet([_anchor("h_kestrel", "partial")], carried_by="conversation", conversation="applied")
    twin = copy.deepcopy(packet)
    assert score_withheld_pair(withheld, absent, "b", packet, twin).identical
    twin["generation"]["conversation"] = "truncated"
    verdict = score_withheld_pair(withheld, absent, "b", packet, twin)
    assert not verdict.identical and verdict.differs_in == ("generation.conversation",)
    twin = copy.deepcopy(packet)
    twin["units"].append(_unit("h_kestrel", "extra"))
    assert score_withheld_pair(withheld, absent, "b", packet, twin).differs_in == ("units",)


def _token(refs: list[str], *, nonce: int) -> str:
    body = {
        "v": 1,
        "refs": refs,
        "generation": 2,
        "minted_ns": nonce,
        "thread": f"thread-{nonce}",
        "thread_ns": nonce,
        "mac": f"mac-{nonce}",
    }
    return base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=") + ".sig"


def test_the_pair_compares_the_token_by_content_not_by_its_per_call_bytes() -> None:
    """Two calls of one request never return the same token bytes: it carries its
    own mint time and a fresh thread. The refs it names ARE the packet's content."""

    withheld, absent = case_by_id("V28"), case_by_id("W28")
    left = _packet([_anchor("h_kestrel", "partial")], continuity=_token(["a"], nonce=1))
    right = _packet([_anchor("h_kestrel", "partial")], continuity=_token(["a"], nonce=2))
    left["continuity"], right["continuity"] = left["generation"].pop("continuity"), right["generation"].pop("continuity")
    assert left["continuity"] != right["continuity"]
    assert score_withheld_pair(withheld, absent, "b", left, right).identical
    right["continuity"] = _token(["a", "leaked"], nonce=2)
    verdict = score_withheld_pair(withheld, absent, "b", left, right)
    assert not verdict.identical and verdict.differs_in == ("continuity",)


def test_the_control_arm_is_red_when_half_of_the_multi_turn_cases_fail() -> None:
    def rows(failing: int) -> list[ConversationRow]:
        multi = [case for case in CASES if case.group == "multi_turn" and not case.kind.endswith("_twin")]
        out = []
        for index, case in enumerate(multi):
            row = _score(case.case_id, "a", _packet(abstained=True))
            broken = index < failing
            out.append(dataclasses.replace(row, failure_reasons=("x",) if broken else ()))
        return out

    total = len([case for case in CASES if case.group == "multi_turn" and not case.kind.endswith("_twin")])
    assert mechanism_removal(rows(total))["red"] is True
    assert mechanism_removal(rows((total + 1) // 2))["red"] is True
    assert mechanism_removal(rows(total // 2 - 1 if total % 2 == 0 else total // 2))["red"] is False


def test_the_report_has_no_aggregate() -> None:
    row = _score("V1", "a", _served_packet(case_by_id("V1"), origin="turn"))
    text = json.dumps(row_to_dict(row))
    assert "mean" not in text and "average" not in text and "overall" not in text
    floors = arm_floors([dataclasses.replace(row, arm="b")], "b")
    assert set(floors) == {
        "arm",
        "cases",
        "recall_below_floor",
        "poison_served",
        "twin_false_activation",
        "drowning_failures",
        "packet_size",
        "passed",
    }


def test_served_keys_reads_units_ambiguity_and_a_carried_anchor() -> None:
    identity = {**{path: key for key, path in _PATHS.items()}, **{ref: key for key, ref in _REFS.items()}}
    packet = _packet(
        [_anchor("h_kestrel", "partial")],
        [_unit("h_saltmarsh", "x")],
        ambiguity=[{"ref": _REFS["h_copperfield"], "title": "t"}],
        abstained=False,
        carried_by="conversation",
    )
    assert served_keys(packet, identity) == {"h_kestrel", "h_saltmarsh", "h_copperfield"}
    uncarried = _packet([_anchor("h_kestrel", "partial")])
    assert served_keys(uncarried, identity) == set()
    assert observed_status(uncarried) == "unresolved" or observed_status(uncarried) == "partial"


# --------------------------------------------------------------------------- #
# The request builder, arms and refusal
# --------------------------------------------------------------------------- #


def test_the_request_builder_sends_conversation_only_on_the_arms_that_carry_one() -> None:
    refs = {key: f"ref:{key}" for key in KEY_KINDS}
    case = case_by_id("V13")
    assert build_request(case, "a", refs) == {"turn": case.turn}
    assert set(build_request(case, "b", refs)["conversation"]) == {"recent"}
    assert set(build_request(case, "c", refs)["conversation"]) == {"focus"}
    assert set(build_request(case, "d", refs)["conversation"]) == {"recent", "focus"}
    refs_case = case_by_id("V14")
    assert build_request(refs_case, "b", refs)["conversation"]["refs"] == ["ref:h_brindle"]
    rich = case_by_id("V1")
    assert all(build_request(rich, arm, refs) == {"turn": rich.turn} for arm in ARMS)


def test_a_product_without_the_argument_is_recorded_as_refused_not_raised(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        commands,
        "op_activate_context",
        lambda vault_root, turn="", max_chars=4000: {"turn": turn},
    )
    refs = {key: f"ref:{key}" for key in KEY_KINDS}
    assert not product_accepts_conversation()
    assert run_arm(tmp_path, case_by_id("V13"), "b", refs) == REFUSED
    assert run_arm(tmp_path, case_by_id("V13"), "a", refs) == {"turn": case_by_id("V13").turn}
    # The arm that sends no conversation runs even where the argument is refused.
    assert run_arm(tmp_path, case_by_id("V1"), "d", refs) == {"turn": case_by_id("V1").turn}


def test_a_product_with_the_argument_receives_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []

    def accepting(vault_root, turn="", conversation=None, max_chars=4000):
        seen.append({"turn": turn, "conversation": conversation})
        return {"anchors": []}

    monkeypatch.setattr(commands, "op_activate_context", accepting)
    refs = {key: f"ref:{key}" for key in KEY_KINDS}
    assert product_accepts_conversation()
    assert run_arm(tmp_path, case_by_id("V13"), "d", refs) == {"anchors": []}
    assert seen[0]["conversation"]["focus"] == case_by_id("V13").conversation["focus"]


# --------------------------------------------------------------------------- #
# The run on the compiler under test
# --------------------------------------------------------------------------- #


def test_every_case_ran_on_every_arm_it_names(run: Run) -> None:
    for case in CASES:
        for arm in case.arms:
            packet = run.packets[(case.case_id, arm)]
            assert packet == REFUSED or isinstance(packet, dict), (case.case_id, arm)


def test_conversation_arms_are_refused_until_the_product_takes_the_argument(run: Run) -> None:
    for case in CASES:
        for arm in case.arms:
            if arm == "a":
                continue
            packet = run.packets[(case.case_id, arm)]
            carries = conversation_for_arm(case, arm, run.key_to_ref) is not None
            if carries and not product_accepts_conversation():
                assert packet == REFUSED, (case.case_id, arm)
            else:
                assert isinstance(packet, dict), (case.case_id, arm)
    assert CONVERSATION_ARMS == ("b", "c", "d")


def test_arm_a_is_scored_for_every_case_and_names_no_conversation(run: Run) -> None:
    arm_a = {row.case_id for row in run.rows if row.arm == "a"}
    assert arm_a == {case.case_id for case in CASES}
    for case in CASES:
        assert isinstance(run.packets[(case.case_id, "a")], dict)
        assert "conversation" not in build_request(case, "a", run.key_to_ref)


def test_the_incident_classes_fail_with_the_conversation_stripped(run: Run) -> None:
    """The half-a-decision, the ambiguous tie, the anaphoric follow-up and the
    topic switch: a compiler without the mechanism cannot serve them."""

    failing = incident_classes(run.rows)
    for kind in ("promotion", "tie_break", "anaphoric_carry", "topic_switch"):
        every = sorted(case.case_id for case in CASES if case.kind == kind)
        assert failing.get(kind) == every, (kind, failing.get(kind), every)


def test_the_mechanism_removal_arm_is_red(run: Run) -> None:
    verdict = mechanism_removal(run.rows)
    assert verdict["red"] is True, verdict
    assert verdict["failed_count"] * 2 >= verdict["multi_turn_cases"]


def test_the_attachment_cases_abstain_on_the_turn_alone(run: Run) -> None:
    for case in CASES:
        if case.kind != "attachment":
            continue
        packet = run.packets[(case.case_id, "a")]
        assert packet["abstained"] is True and observed_status(packet) == "unresolved", case.case_id
        assert _row(run, case.case_id).passed


def test_the_blind_attachment_twin_and_every_other_twin_serve_nothing_on_the_turn_alone(run: Run) -> None:
    failing = [row.case_id for row in run.rows if row.arm == "a" and row.is_twin and not row.passed]
    assert failing == [], failing


def test_no_drowning_case_serves_its_earlier_subject_when_the_turn_names_another(run: Run) -> None:
    drowned = [row.case_id for row in run.rows if row.arm == "a" and row.drowned]
    assert drowned == [], drowned


def test_a_refs_only_conversation_never_carries_and_the_turn_alone_abstains(run: Run) -> None:
    packet = run.packets[("V25", "a")]
    assert packet["abstained"] is True and "carried_by" not in packet["generation"]
    assert _row(run, "V25").passed


def test_the_withheld_pair_is_byte_identical_on_the_turn_alone(run: Run) -> None:
    verdict = score_withheld_pair(
        case_by_id("V28"), case_by_id("W28"), "a", run.packets[("V28", "a")], run.packets[("W28", "a")]
    )
    assert verdict.identical, verdict.differs_in


def test_the_rich_single_turns_serve_their_gold_on_the_turn_alone(run: Run) -> None:
    failing = {
        row.case_id: row.failure_reasons
        for row in run.rows
        if row.arm == "a" and row.group == "rich_turn" and not row.is_twin and not row.passed
    }
    assert failing == {}, failing


def test_no_packet_exceeds_its_size_bound(run: Run) -> None:
    floors = arm_floors(run.rows, "a")
    assert floors["packet_size"]["within_bounds"] is True, floors["packet_size"]
    assert all(row.packet_chars <= 4000 for row in run.rows)


# --------------------------------------------------------------------------- #
# The baseline manifest
# --------------------------------------------------------------------------- #


def test_the_baseline_manifest_is_deterministic_and_carries_no_time_or_path(run: Run) -> None:
    manifest = baseline_manifest(run)
    text = json.dumps(manifest, sort_keys=True, indent=2)
    assert text == json.dumps(baseline_manifest(run), sort_keys=True, indent=2)
    for forbidden in ("/tmp", "/home", "T00:", "timestamp", "exomem://memory/"):
        assert forbidden not in text, forbidden
    if os.environ.get("EXOMEM_WRITE_CONVERSATION_BASELINE") == "1":
        BASELINE_PATH.write_text(text + "\n", encoding="utf-8")


def test_the_committed_baseline_references_the_pinned_fixture_digest(run: Run) -> None:
    """Editing a fixture after the baseline exists voids it: this goes red and
    the run is redone, never rescored."""

    committed = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    assert committed["fixture_set_digest"] == fixture_set_digest()
    assert committed["fixture_set_id"] == FIXTURE_SET_ID
    assert committed["corpus_id"] == CORPUS_ID
    assert committed["corpus_hash"] == run.manifest.logical_hash


def test_the_committed_baseline_records_the_control_arm_verdicts(run: Run) -> None:
    committed = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    verdicts = committed["verdicts"]
    assert committed["arms_run"] == ["a"]
    assert verdicts["mechanism_removal"]["red"] is True
    assert set(verdicts["incident_classes_failing_on_arm_a"]) >= {
        "promotion",
        "tie_break",
        "anaphoric_carry",
        "topic_switch",
    }
    assert all(verdicts["attachment_cases_abstain_on_arm_a"].values())
    assert {row["case_id"] for row in committed["arm_a"]} == {case.case_id for case in CASES}
    if not product_accepts_conversation():
        assert committed["compiler"]["accepts_conversation_argument"] is False
        assert {item["result"] for item in committed["conversation_arms"]["refused"]} == {REFUSED}
