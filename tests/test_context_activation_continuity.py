"""The keyed continuity group of the context-activation benchmark (design
amendment A6): pre-registration and scorer.

The digest below was pinned in a commit before the group's first run. A
change to any case, its gold or the scorer's checks is a new digest and a
visible edit here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from membench.utility import context_activation_continuity as continuity

from exomem.public_artifact_privacy import assert_public_artifacts_clean

CONTINUITY_SHA256 = "de5e79004e85487bd2b5c56cc4082d763c97c077f80bce7d88e087935ccbadc7"


def test_the_group_is_the_pre_registered_one() -> None:
    assert continuity.continuity_digest() == CONTINUITY_SHA256
    assert [case.case_id for case in continuity.CASES] == ["K1", "K2", "K3", "K4"]
    assert "continue" in {case.turn for case in continuity.CASES}
    assert all(not case.case_id.startswith(("C", "T")) for case in continuity.CASES)


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
            "units": [{"ref": "x#unit-1"}],
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


def test_the_continuity_modules_pass_the_privacy_gate() -> None:
    assert_public_artifacts_clean([Path(continuity.__file__), Path(__file__)])
