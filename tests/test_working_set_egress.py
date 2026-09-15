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
        freshness_key="k", index_generation=3, roles_hash="abc", turn="t", budget_chars=4000
    )
    assert "audit" not in str(first)
    assert working_set_runtime.cache_key.__doc__
    import inspect

    signature = inspect.signature(working_set_runtime.cache_key)
    assert "purpose" not in signature.parameters
