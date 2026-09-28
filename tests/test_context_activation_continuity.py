"""The keyed continuity group of the context-activation benchmark (design
amendment A6): pre-registration and scorer.

Group v3's digest below, which covers the cases, their gold, the checks and a
sha256 of the scorer module's source, was pinned in a commit before v3's first
run. A change to any of them is a new digest and a visible edit here. The
recorded results of v1 and v2 are kept as history.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from membench.utility import context_activation_continuity as continuity

from exomem.public_artifact_privacy import assert_public_artifacts_clean

pytestmark = pytest.mark.timeout(1800)

CONTINUITY_SHA256 = "8c6eb8140e5929f59ed2ff7bd701f85b7dded2fc0c24e0e8c277a406b0a7624a"

REPOSITORY = Path(__file__).resolve().parents[1]
#: v1 (digest ``de5e7900…``, scorer too lenient per the integrity review),
#: kept as history.
V1_GROUP = REPOSITORY / "docs" / "benchmarks" / "context-activation-continuity-2026-09.json"
V1_SHA256 = "de5e79004e85487bd2b5c56cc4082d763c97c077f80bce7d88e087935ccbadc7"
#: v2 (digest ``b208a986…``; its extra-page check ignored units, pointers and
#: current state, per the integrity recheck), kept as history.
V2_GROUP = REPOSITORY / "docs" / "benchmarks" / "context-activation-continuity-2026-09-v2.json"
V2_SHA256 = "b208a986e509ffd4076243dc4b899e2ffe3aad2cdfa708a838143207eb99502a"


def test_the_group_is_the_pre_registered_one() -> None:
    assert continuity.GROUP_ID == "context-activation-continuity-v3"
    assert continuity.continuity_digest() == CONTINUITY_SHA256
    assert [case.case_id for case in continuity.CASES] == ["K1", "K2", "K3", "K4"]
    assert "continue" in {case.turn for case in continuity.CASES}
    assert all(not case.case_id.startswith(("C", "T")) for case in continuity.CASES)


def test_the_digest_covers_the_scorer_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review F4: the checks' logic is pinned, not only their names."""
    before = continuity.continuity_digest()
    monkeypatch.setattr(continuity, "scorer_source_digest", lambda: "0" * 64)
    assert continuity.continuity_digest() != before


def test_the_v1_result_is_kept_as_history() -> None:
    history = json.loads(V1_GROUP.read_text(encoding="utf-8"))
    assert history["group_id"] == "context-activation-continuity-v1"
    assert history["continuity_digest"] == V1_SHA256
    assert (history["passed"], history["total"]) == (4, 4)


def test_the_v2_result_is_kept_as_history() -> None:
    history = json.loads(V2_GROUP.read_text(encoding="utf-8"))
    assert history["group_id"] == "context-activation-continuity-v2"
    assert history["continuity_digest"] == V2_SHA256
    assert (history["passed"], history["total"]) == (4, 4)


def test_every_case_is_a_fresh_session_with_earlier_work() -> None:
    for case in continuity.CASES:
        assert case.prior, case.case_id
        earlier = {act.session for act in case.prior if act.session}
        assert case.session is None or case.session not in earlier, case.case_id
        assert case.recent_includes, case.case_id


# --------------------------------------------------------------------------- #
# The scorer, on packets shaped by hand (never a product run)
# --------------------------------------------------------------------------- #

IDENTITIES = continuity.Identities(
    refs={
        "c5_resource_profile": "Knowledge Base/Systems/Workshop bench.md",
        "t5_available_resource": "Knowledge Base/Systems/Scanner cart.md",
        "c4_entity_profile": "exomem://memory/00000000-0000-4000-8000-00000000000a",
        continuity.ORDINARY_NOTE: continuity.ORDINARY_NOTE,
        continuity.RECAP: "Knowledge Base/Sources/Episodes/recap.md",
    },
    paths={
        "c5_resource_profile": "Knowledge Base/Systems/Workshop bench.md",
        "t5_available_resource": "Knowledge Base/Systems/Scanner cart.md",
        "c4_entity_profile": "Knowledge Base/Entities/People/colleague.md",
        continuity.ORDINARY_NOTE: continuity.ORDINARY_NOTE,
        continuity.RECAP: "Knowledge Base/Sources/Episodes/recap.md",
    },
    canonical={
        "Knowledge Base/Systems/Workshop bench.md": "Knowledge Base/Systems/Workshop bench.md",
        "Knowledge Base/Systems/Scanner cart.md": "Knowledge Base/Systems/Scanner cart.md",
        "Knowledge Base/Entities/People/colleague.md": "Knowledge Base/Entities/People/colleague.md",
        "exomem://memory/00000000-0000-4000-8000-00000000000a": (
            "Knowledge Base/Entities/People/colleague.md"
        ),
        "Knowledge Base/Records/Workshop Bench/_collection.md": (
            "Knowledge Base/Records/Workshop Bench/_collection.md"
        ),
        continuity.ORDINARY_NOTE: continuity.ORDINARY_NOTE,
        "Knowledge Base/Sources/Episodes/recap.md": "Knowledge Base/Sources/Episodes/recap.md",
    },
)


def _case(case_id: str) -> continuity.ContinuityCase:
    return next(case for case in continuity.CASES if case.case_id == case_id)


def _recent(*entries: tuple[str, str]) -> list[dict]:
    return [{"path": IDENTITIES.paths[page], "why": why} for page, why in entries]


def _anchor(page: str, *, status: str = "resolved", evidence=("recency",)) -> dict:
    return {"ref": IDENTITIES.refs[page], "status": status, "evidence": list(evidence)}


def _packets() -> dict[str, dict]:
    return {
        "K1": {
            "abstained": False,
            "anchors": [_anchor("c5_resource_profile")],
            "units": [{"ref": "Knowledge Base/Systems/Workshop bench.md#unit-1"}],
            "generation": {},
            "recent_context": _recent(
                ("c5_resource_profile", "activated"), ("t5_available_resource", "activated")
            ),
        },
        "K2": {
            "abstained": False,
            "anchors": [_anchor("c4_entity_profile")],
            "generation": {},
            "recent_context": _recent((continuity.RECAP, "episode")),
        },
        "K3": {
            "abstained": True,
            "abstention": {"reason": "unresolved"},
            "anchors": [],
            "units": [],
            "generation": {},
            "recent_context": _recent(("c5_resource_profile", "activated")),
        },
        "K4": {
            "abstained": False,
            "anchors": [_anchor(continuity.ORDINARY_NOTE)],
            "generation": {"carried_by": "recency"},
            "recent_context": _recent((continuity.ORDINARY_NOTE, "edited")),
        },
    }


@pytest.mark.parametrize("case_id", ["K1", "K2", "K3", "K4"])
def test_a_packet_that_meets_the_gold_passes(case_id: str) -> None:
    scored = continuity.score(_packets()[case_id], _case(case_id), IDENTITIES)
    assert scored.passed, scored.failure_reasons
    assert [name for name, _ok in scored.checks] == list(continuity.CHECKS)


def test_the_newest_page_from_another_workspace_fails_k1() -> None:
    packet = dict(_packets()["K1"])
    packet["anchors"] = [_anchor("t5_available_resource")]
    packet["recent_context"] = _recent(
        ("t5_available_resource", "activated"), ("c5_resource_profile", "activated")
    )
    scored = continuity.score(packet, _case("K1"), IDENTITIES)
    assert dict(scored.checks) == {
        "status": True,
        "referents": False,
        "carried_by": True,
        "must_not_resolve": False,
        "served_subset": False,
        "serves_nothing": True,
        "recent_lead": False,
        "recent_includes": True,
    }


def test_a_referent_resolved_without_recency_does_not_count() -> None:
    packet = dict(_packets()["K2"])
    packet["anchors"] = [_anchor("c4_entity_profile", evidence=("exact_title",))]
    scored = continuity.score(packet, _case("K2"), IDENTITIES)
    assert not dict(scored.checks)["referents"]


def test_a_keyless_turn_that_serves_the_other_thread_fails_k3() -> None:
    packet = dict(_packets()["K3"])
    packet.update(abstained=False, anchors=[_anchor("c5_resource_profile")], units=[{"ref": "u"}])
    scored = continuity.score(packet, _case("K3"), IDENTITIES)
    checks = dict(scored.checks)
    assert not checks["status"] and not checks["must_not_resolve"] and not checks["serves_nothing"]


def test_a_page_served_without_the_recency_carry_fails_k4() -> None:
    packet = dict(_packets()["K4"])
    packet["generation"] = {"carried_by": "retrieval"}
    scored = continuity.score(packet, _case("K4"), IDENTITIES)
    assert dict(scored.checks)["carried_by"] is False
    assert "expected carried_by 'recency', observed 'retrieval'" in scored.failure_reasons


def test_v2_a_keyless_turn_naming_the_page_partially_or_as_ambiguity_fails_k3() -> None:
    """Review F2: v1 passed this packet."""
    packet = dict(_packets()["K3"])
    packet["anchors"] = [_anchor("c5_resource_profile", status="partial", evidence=("recency",))]
    packet["ambiguity"] = [{"ref": IDENTITIES.refs["c5_resource_profile"]}]
    scored = continuity.score(packet, _case("K3"), IDENTITIES)
    checks = dict(scored.checks)
    assert not checks["must_not_resolve"]
    assert not checks["served_subset"]
    assert not checks["serves_nothing"]
    assert not scored.passed


@pytest.mark.parametrize("status", ["resolved", "partial", "retrieval_carried"])
def test_v2_the_right_page_plus_a_wrong_one_fails(status: str) -> None:
    """Review F3: an extra served page of any status fails K2."""
    packet = dict(_packets()["K2"])
    packet["anchors"] = [
        _anchor("c4_entity_profile"),
        {**_anchor("t5_available_resource", status=status), "path": "Knowledge Base/x.md"},
    ]
    scored = continuity.score(packet, _case("K2"), IDENTITIES)
    assert dict(scored.checks)["served_subset"] is False
    assert "served page(s) outside the referents: ['t5_available_resource']" in scored.failure_reasons


def test_v2_an_extra_ambiguity_candidate_fails_k4() -> None:
    packet = dict(_packets()["K4"])
    packet["ambiguity"] = [{"ref": "exomem://memory/00000000-0000-4000-8000-00000000000b"}]
    scored = continuity.score(packet, _case("K4"), IDENTITIES)
    assert dict(scored.checks)["served_subset"] is False
    assert "served page(s) outside the referents: ['<memory ref>']" in scored.failure_reasons


def _unit(page: str, fragment: str = "#unit-1", **extra) -> dict:
    return {"ref": IDENTITIES.refs[page] + fragment, **extra}


def test_v3_a_unit_of_the_must_not_resolve_page_fails_k1() -> None:
    """Integrity recheck F3: v2 passed K1 serving a unit of the other
    workspace's page."""
    packet = dict(_packets()["K1"])
    packet["units"] = [_unit("c5_resource_profile"), _unit("t5_available_resource")]
    checks = dict(continuity.score(packet, _case("K1"), IDENTITIES).checks)
    assert not checks["must_not_resolve"] and not checks["served_subset"]


@pytest.mark.parametrize("channel", ["units", "pointers", "current_state"])
def test_v3_material_of_a_wrong_page_fails_each_case(channel: str) -> None:
    wrong = "exomem://memory/00000000-0000-4000-8000-00000000000a"  # the colleague
    item = {"anchor": IDENTITIES.refs["t5_available_resource"]}
    if channel != "current_state":
        item = {"ref": IDENTITIES.refs["t5_available_resource"] + "#unit-9"}
    for case_id in ("K1", "K2", "K4"):
        packet = dict(_packets()[case_id])
        packet[channel] = [item]
        scored = continuity.score(packet, _case(case_id), IDENTITIES)
        assert dict(scored.checks)["served_subset"] is False, (case_id, channel)
    packet = dict(_packets()["K4"])
    packet[channel] = [{"anchor": wrong} if channel == "current_state" else {"ref": wrong + "#u"}]
    scored = continuity.score(packet, _case("K4"), IDENTITIES)
    assert "served page(s) outside the referents: ['c4_entity_profile']" in scored.failure_reasons


def test_v3_material_of_the_referent_page_is_fine() -> None:
    """A unit, pointer or state entry of the referent page, however spelled."""
    packet = dict(_packets()["K2"])
    entity = IDENTITIES.refs["c4_entity_profile"]
    packet["units"] = [{"ref": entity + "#unit-1"}, {"ref": "Entities/People/colleague.md#unit-2"}]
    packet["pointers"] = [{"ref": "Knowledge Base/Entities/People/colleague.md#unit-3"}]
    packet["current_state"] = [{"anchor": entity}]
    scored = continuity.score(packet, _case("K2"), IDENTITIES)
    assert scored.passed, scored.failure_reasons


def test_v3_a_unit_is_placed_by_its_provenance_when_its_ref_names_no_page() -> None:
    packet = dict(_packets()["K1"])
    packet["units"] = [
        {"ref": "fragment-only#unit-1", "provenance": {"path": "Knowledge Base/Records/Workshop Bench/_collection.md"}}
    ]
    scored = continuity.score(packet, _case("K1"), IDENTITIES)
    assert "served page(s) outside the referents: ['Knowledge Base/Records/Workshop Bench/_collection.md']" in (
        scored.failure_reasons
    )


def test_v3_matches_on_canonical_identity_not_one_spelling() -> None:
    """A hot-page ambiguity names a non-row page by path; the resolved
    anchor carries its memory ref. Both are the same page."""
    assert IDENTITIES.page("exomem://memory/00000000-0000-4000-8000-00000000000a#unit-4") == (
        "Knowledge Base/Entities/People/colleague.md"
    )
    assert IDENTITIES.page("Entities/People/colleague.md") == "Knowledge Base/Entities/People/colleague.md"
    packet = dict(_packets()["K2"])
    packet["anchors"] = [
        {"ref": "Knowledge Base/Entities/People/colleague.md", "status": "resolved", "evidence": ["recency"]}
    ]
    packet["ambiguity"] = [{"ref": "Knowledge Base/Entities/People/colleague.md", "kind": "page"}]
    checks = dict(continuity.score(packet, _case("K2"), IDENTITIES).checks)
    assert checks["referents"] and checks["served_subset"]


def test_the_continuity_modules_pass_the_privacy_gate() -> None:
    assert_public_artifacts_clean([Path(continuity.__file__), Path(__file__), V1_GROUP, V2_GROUP])
