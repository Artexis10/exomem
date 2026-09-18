"""Task 5.1 — the packet crosses the same release plane a hit list does.

The compiler walks typed neighbourhoods and Records collections, which is exactly
how a permitted page becomes an existence oracle for a withheld one. So the
packet goes through its own guard, receiving the SAME release object that hit
projection gets, and the guard drops any field that names a withheld page in any
reference form — including inside wikilink syntax — plus any evidence that
depended on a withheld neighbour.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_governance_egress import (
    OPEN_PATH,
    RESTRICTED_PATH,
    _external,
    _hit,
    write_broken_policy,
    write_rule,
    write_scope,
)

from exomem.governance import egress
from exomem.governance.principal import request_scope


def _release(*, withheld=(RESTRICTED_PATH,), blocked: bool = False) -> egress.AnnotatedHits:
    return egress.AnnotatedHits(
        hits=[_hit(OPEN_PATH)],
        withheld_paths=frozenset(withheld),
        active=True,
        blocked=blocked,
    )


def _packet() -> dict:
    return {
        "anchors": [
            {
                "ref": RESTRICTED_PATH,
                "path": RESTRICTED_PATH,
                "title": "Hidden anchor",
                "kind": "hub",
                "status": "resolved",
                "evidence": ["exact_alias"],
            },
            {
                "ref": OPEN_PATH,
                "path": OPEN_PATH,
                "title": "Open anchor",
                "kind": "resource",
                "status": "resolved",
                "evidence": ["lexical_overlap", "graph_corroboration"],
            },
        ],
        "roles": [{"id": "resources", "source": "anchor_default", "lane": "units"}],
        "units": [
            {
                "ref": "unit-open",
                "role": "resources",
                "text": "An open unit.",
                "lifecycle": "active",
                "updated": "2026-09-01",
                "provenance": {"path": OPEN_PATH, "level": "unit", "anchor": OPEN_PATH},
            },
            {
                "ref": "unit-hidden",
                "role": "resources",
                "text": "A hidden unit.",
                "lifecycle": "active",
                "updated": "2026-09-01",
                "provenance": {"path": RESTRICTED_PATH, "level": "unit", "anchor": RESTRICTED_PATH},
            },
            {
                "ref": "unit-wikilink",
                "role": "resources",
                "text": "Names it only through a link.",
                "lifecycle": "superseded",
                "updated": "2026-09-01",
                "provenance": {
                    "path": OPEN_PATH,
                    "level": "unit",
                    "anchor": OPEN_PATH,
                    "superseded_by": ["[[kill-switch-for-risky-releases]]"],
                },
            },
        ],
        "pointers": [
            {"ref": RESTRICTED_PATH, "role": "resources", "title": "Hidden", "reason": "budget"},
            {"ref": OPEN_PATH, "role": "resources", "title": "Open", "reason": "budget"},
        ],
        "current_state": [
            {
                "anchor": RESTRICTED_PATH,
                "source": "records",
                "as_of": "2026-09-10",
                "statement": "state: hidden",
            },
            {
                "anchor": OPEN_PATH,
                "source": "records",
                "as_of": "2026-09-10",
                "statement": "state: open",
            },
        ],
        "missing": [],
        "ambiguity": [
            {"ref": RESTRICTED_PATH, "title": "Hidden", "kind": "hub", "neighbourhood_size": 2},
            {"ref": OPEN_PATH, "title": "Open", "kind": "hub", "neighbourhood_size": 1},
        ],
        "budget": {"limit_chars": 4000, "used_chars": 40},
        "generation": {
            "freshness_key": "k",
            "index_generation": 3,
            "roles_hash": "abc",
            "roles_source": "shipped",
        },
        "abstained": False,
    }


def test_no_field_of_the_packet_names_a_withheld_page(vault: Path) -> None:
    write_scope(vault)
    write_rule(vault, ceiling=0)

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, _packet(), _release())

    assert guarded is not None
    assert RESTRICTED_PATH not in str(guarded)
    assert "kill-switch-for-risky-releases" not in str(guarded)
    assert [anchor["ref"] for anchor in guarded["anchors"]] == [OPEN_PATH]
    assert [unit["ref"] for unit in guarded["units"]] == ["unit-open", "unit-wikilink"]
    assert [pointer["ref"] for pointer in guarded["pointers"]] == [OPEN_PATH]
    assert [entry["anchor"] for entry in guarded["current_state"]] == [OPEN_PATH]
    assert [entry["ref"] for entry in guarded["ambiguity"]] == [OPEN_PATH]


def test_a_wikilink_to_a_withheld_page_is_stripped_from_provenance(vault: Path) -> None:
    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, _packet(), _release())

    assert guarded is not None
    wikilink_unit = next(unit for unit in guarded["units"] if unit["ref"] == "unit-wikilink")
    assert "superseded_by" not in wikilink_unit["provenance"]
    # The unit itself stays, still honestly marked superseded — the successor's
    # NAME is what the audience may not have, not the fact of supersession.
    assert wikilink_unit["lifecycle"] == "superseded"


def test_evidence_that_depended_on_a_withheld_neighbour_is_dropped(vault: Path) -> None:
    packet = _packet()
    packet["anchors"] = [packet["anchors"][1]]
    packet["anchors"][0]["neighbourhood"] = [RESTRICTED_PATH]

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, _release())

    assert guarded is not None
    anchor = guarded["anchors"][0]
    assert "graph_corroboration" not in anchor["evidence"]
    assert "lexical_overlap" in anchor["evidence"]
    assert "neighbourhood" not in anchor


def test_blocked_release_withholds_the_whole_packet(vault: Path) -> None:
    write_broken_policy(vault)

    guarded = egress.guard_working_set(
        vault, _packet(), _release(withheld=(RESTRICTED_PATH, OPEN_PATH), blocked=True)
    )

    assert guarded is None


def test_an_ungoverned_vault_is_untouched(vault: Path) -> None:
    packet = _packet()

    guarded = egress.guard_working_set(
        vault, packet, egress.AnnotatedHits(hits=[], withheld_paths=frozenset(), active=False)
    )

    assert guarded == packet
    assert guarded is not packet, "the guard deep-copies rather than mutating the caller's packet"


def test_purpose_never_enters_the_packet_cache_key() -> None:
    from exomem import working_set_runtime

    first = working_set_runtime.cache_key(
        freshness_key="k", index_generation=3, roles_hash="abc", turn="t", max_chars=4000
    )
    assert "audit" not in str(first)
    assert working_set_runtime.cache_key.__doc__
    import inspect

    signature = inspect.signature(working_set_runtime.cache_key)
    assert "purpose" not in signature.parameters


# --------------------------------------------------------------------------- #
# Review round: the guard runs on every served packet, and prose is scanned
# --------------------------------------------------------------------------- #


def test_a_recall_failure_does_not_bypass_the_guard(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A degraded evidence kind must not become a full disclosure.

    `retrieval` is one of eight evidence kinds. When the internal recall call
    raises, the compiler loses that kind — it does not lose the release plane.
    The reviewer's repro: with `find()` healthy the packet abstained withheld
    with 0 units; with `find()` raising it served 4 anchors and 5 units.
    """
    from test_working_set_index import _seed_planning, _seed_structure

    from exomem import commands, working_set_index, working_set_runtime
    from exomem import find as find_module

    _seed_structure(vault)
    _seed_planning(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()

    write_scope(vault, paths="**")
    write_rule(vault, ceiling=0, audience="external")

    def boom(*_args, **_kwargs):
        raise RuntimeError("recall lane exploded")

    monkeypatch.setattr(find_module, "find", boom)

    guard_calls: list[int] = []
    real_guard = egress.guard_working_set

    def counting_guard(*args, **kwargs):
        guard_calls.append(1)
        return real_guard(*args, **kwargs)

    monkeypatch.setattr(egress, "guard_working_set", counting_guard)

    with request_scope(_external()):
        packet = commands.op_activate_context(vault, turn="the cargo sled north")

    assert guard_calls, "the egress guard must run even when the recall lane fails"
    assert packet["abstained"] is True
    assert packet["units"] == []
    assert packet["anchors"] == []
    assert "Cargo Sled" not in str(packet)


def test_a_release_plane_failure_abstains_rather_than_serving(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import commands

    def boom(*_args, **_kwargs):
        raise RuntimeError("release plane exploded")

    monkeypatch.setattr(egress, "annotate_hits", boom)

    packet = commands.op_activate_context(vault, turn="the cargo sled north")

    assert packet["abstained"] is True
    assert packet["abstention"] == {"reason": "unavailable"}
    assert packet["units"] == []


def test_a_wikilink_in_unit_prose_to_a_withheld_page_drops_the_unit(vault: Path) -> None:
    packet = _packet()
    packet["units"] = [
        {
            "ref": "unit-open",
            "role": "resources",
            "text": "An open unit with no references.",
            "lifecycle": "active",
            "updated": "2026-09-01",
            "provenance": {"path": OPEN_PATH, "level": "unit", "anchor": OPEN_PATH},
        },
        {
            "ref": "unit-prose-leak",
            "role": "resources",
            "text": "Use the approach from [[kill-switch-for-risky-releases]] here.",
            "lifecycle": "active",
            "updated": "2026-09-01",
            "provenance": {"path": OPEN_PATH, "level": "unit", "anchor": OPEN_PATH},
        },
    ]

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, _release())

    assert guarded is not None
    assert [unit["ref"] for unit in guarded["units"]] == ["unit-open"]
    assert "kill-switch-for-risky-releases" not in str(guarded)


def test_a_wikilink_in_a_current_state_statement_is_dropped(vault: Path) -> None:
    packet = _packet()
    packet["current_state"] = [
        {
            "anchor": OPEN_PATH,
            "source": "records",
            "as_of": "2026-09-10",
            "statement": "state: parked per [[kill-switch-for-risky-releases]]",
        },
        {
            "anchor": OPEN_PATH,
            "source": "records",
            "as_of": "2026-09-11",
            "statement": "state: open",
        },
    ]

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, _release())

    assert guarded is not None
    assert [entry["as_of"] for entry in guarded["current_state"]] == ["2026-09-11"]
    assert "kill-switch-for-risky-releases" not in str(guarded)


def test_prose_wikilinks_are_decided_even_when_not_already_withheld(vault: Path) -> None:
    """A page named only in prose still gets a release decision.

    `release.withheld_paths` carries what hit projection happened to touch. A
    wikilink in a unit's text can name a page that recall never surfaced, so the
    guard has to harvest it and decide it rather than assume silence means
    permitted.
    """
    write_scope(vault)
    write_rule(vault, ceiling=0)

    packet = _packet()
    packet["units"] = [
        {
            "ref": "unit-only-prose",
            "role": "resources",
            "text": f"See [[{RESTRICTED_PATH.rsplit('/', 1)[-1].removesuffix('.md')}]].",
            "lifecycle": "active",
            "updated": "2026-09-01",
            "provenance": {"path": OPEN_PATH, "level": "unit", "anchor": OPEN_PATH},
        },
    ]
    empty_release = egress.AnnotatedHits(
        hits=[_hit(OPEN_PATH)], withheld_paths=frozenset(), active=True
    )

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, empty_release)

    assert guarded is not None
    assert guarded["units"] == []


def test_missing_entries_are_scanned_too(vault: Path) -> None:
    packet = _packet()
    packet["missing"] = [
        {"role": "resources", "reason": "no_material", "path": RESTRICTED_PATH},
        {"role": "methods", "reason": "lane_truncated"},
    ]

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, _release())

    assert guarded is not None
    assert [entry["role"] for entry in guarded["missing"]] == ["methods"]


def test_retrieval_refs_enter_the_cache_key_but_purpose_never_does() -> None:
    from exomem import working_set_runtime

    base = dict(
        freshness_key="k", index_generation=3, roles_hash="abc", turn="t", max_chars=4000
    )
    one = working_set_runtime.cache_key(**base, retrieval_paths=frozenset({"a.md"}))
    two = working_set_runtime.cache_key(**base, retrieval_paths=frozenset({"b.md"}))
    same = working_set_runtime.cache_key(**base, retrieval_paths=frozenset({"a.md"}))

    assert one != two, "one audience's retrieval evidence must not key another's packet"
    assert one == same, "the digest must be stable for the same refs"

    import inspect

    signature = inspect.signature(working_set_runtime.cache_key)
    assert "purpose" not in signature.parameters
    assert "audit" not in str(one)


def test_two_serves_differing_only_in_retrieval_paths_are_compiled_separately(
    vault: Path,
) -> None:
    from test_working_set_index import _seed_planning, _seed_structure

    from exomem import working_set_index, working_set_runtime

    _seed_structure(vault)
    _seed_planning(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()

    turn = "the northern corridor"
    hub = "Knowledge Base/Notes/Insights/northern-corridor-hub.md"

    without = working_set_runtime.serve(
        vault, turn=turn, max_chars=2000, freshness_key="k", retrieval_paths=frozenset()
    )
    with_hit = working_set_runtime.serve(
        vault,
        turn=turn,
        max_chars=2000,
        freshness_key="k",
        retrieval_paths=frozenset({hub}),
    )

    def _evidence(packet: dict) -> set[str]:
        return {kind for anchor in packet["anchors"] for kind in anchor["evidence"]}

    assert "retrieval" in _evidence(with_hit)
    assert "retrieval" not in _evidence(without)
