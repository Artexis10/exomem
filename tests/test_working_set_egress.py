"""Task 5.1 — the packet crosses the same release plane a hit list does.

The compiler walks typed neighbourhoods and Records collections, which is exactly
how a permitted page becomes an existence oracle for a withheld one. So the
packet goes through its own guard, receiving the SAME release object that hit
projection gets, and the guard drops any field that names a withheld page in any
reference form — including inside wikilink syntax — plus any evidence that
depended on a withheld neighbour.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

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

from exomem import context_refs
from exomem.governance import egress
from exomem.governance.principal import request_scope


def _release(*, withheld=(RESTRICTED_PATH,), blocked: bool = False) -> egress.AnnotatedHits:
    return egress.AnnotatedHits(
        hits=[_hit(OPEN_PATH)],
        withheld_paths=frozenset(withheld),
        active=True,
        blocked=blocked,
    )


def _unit_ref(path: str, label: str) -> str:
    """A realistic semantic-unit `ref`: `<vault_ref(path)>#unit-<label>`.

    The SAME shape `semantic_units._bind_unit_identities` builds for an
    unanchored unit (`f"{parent_ref}#unit-{fingerprint}"`, `parent_ref =
    context_refs.vault_ref(path)`) — only `label` stands in for the real
    fingerprint hash, since these fixtures only need distinct, stable refs,
    not real content fingerprints. A hand-typed opaque `"unit-foo"` string is
    not a shape the compiler ever emits: under R1's strict-by-default
    candidate handling (correction round 2) it is a candidate with no page on
    disk answering to it, so it lands in `unresolvable` and withholds its
    item the moment any policy is active — which silently broke every
    fixture using that shorthand once R1 stopped skipping bare non-page-shape
    strings by default.
    """
    return f"{context_refs.vault_ref(path)}#unit-{label}"


UNIT_REF_OPEN = _unit_ref(OPEN_PATH, "open")
UNIT_REF_HIDDEN = _unit_ref(RESTRICTED_PATH, "hidden")
UNIT_REF_WIKILINK = _unit_ref(OPEN_PATH, "wikilink")
UNIT_REF_PROSE_LEAK = _unit_ref(OPEN_PATH, "prose-leak")


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
                "ref": UNIT_REF_OPEN,
                "role": "resources",
                "text": "An open unit.",
                "lifecycle": "active",
                "updated": "2026-09-01",
                "provenance": {"path": OPEN_PATH, "level": "unit", "anchor": OPEN_PATH},
            },
            {
                "ref": UNIT_REF_HIDDEN,
                "role": "resources",
                "text": "A hidden unit.",
                "lifecycle": "active",
                "updated": "2026-09-01",
                "provenance": {"path": RESTRICTED_PATH, "level": "unit", "anchor": RESTRICTED_PATH},
            },
            {
                "ref": UNIT_REF_WIKILINK,
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
    assert [unit["ref"] for unit in guarded["units"]] == [UNIT_REF_OPEN, UNIT_REF_WIKILINK]
    assert [pointer["ref"] for pointer in guarded["pointers"]] == [OPEN_PATH]
    assert [entry["anchor"] for entry in guarded["current_state"]] == [OPEN_PATH]
    assert [entry["ref"] for entry in guarded["ambiguity"]] == [OPEN_PATH]


def test_a_wikilink_to_a_withheld_page_is_stripped_from_provenance(vault: Path) -> None:
    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, _packet(), _release())

    assert guarded is not None
    wikilink_unit = next(unit for unit in guarded["units"] if unit["ref"] == UNIT_REF_WIKILINK)
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


def test_evidence_that_depended_on_a_withheld_neighbour_is_dropped_when_not_already_withheld(
    vault: Path,
) -> None:
    """Correction round 3's BLOCKER, reproduced: the sibling test above
    masks it because `_release()` defaults `withheld_paths=(RESTRICTED_PATH,)`
    -- the neighbour is ALREADY independently withheld before this guard
    ever runs. Here it is named ONLY through `neighbourhood`
    (`release.withheld_paths` empty), exactly the "walked past hit
    projection" case this guard exists for
    (`probe_v3_neighbourhood.py`). `neighbourhood` is a LIST, not a
    Mapping, and was not in `_WORKING_SET_PATH_FIELDS`/
    `_WORKING_SET_PROSE_FIELDS`/`_PATH_LIST_FIELDS`, so nothing in
    `_collect` ever reached it: the withheld neighbour was never decided,
    `_guarded_anchor`'s own `_names_withheld(neighbourhood, ...)` check
    never fired, and the anchor kept `"graph_corroboration"` in its
    evidence although the corroborating page is withheld. The
    `neighbourhood` list itself was still popped either way, so no path,
    title or text leaked -- what leaked is the CLAIM.

    Needs an ACTIVE policy (`write_scope`/`write_rule`): the sibling test
    above relies entirely on `_release()`'s own `withheld_paths` to mark
    the page withheld under an otherwise-empty policy, so removing that
    default here would just hit the ungoverned-vault fast path and prove
    nothing.

    Uses a MINIMAL packet -- one anchor, every other section empty --
    rather than `_packet()`: `_packet()`'s own `pointers`/`units`/
    `current_state`/`ambiguity` sections independently name
    `RESTRICTED_PATH` through their own `ref`/`path`/`anchor` fields, which
    decides it regardless of whether `neighbourhood` is ever scanned at
    all and would silently mask exactly the bug this test exists to catch
    -- the same isolation the reviewer's own probe uses.
    """
    write_scope(vault)  # default paths="Notes/Patterns/**" -> matches RESTRICTED_PATH
    write_rule(vault, ceiling=0, audience="external")

    packet = {
        "anchors": [
            {
                "ref": OPEN_PATH,
                "path": OPEN_PATH,
                "title": "Open hub",
                "kind": "hub",
                "status": "resolved",
                "evidence": ["exact_alias", "graph_corroboration"],
                "neighbourhood": [RESTRICTED_PATH],
            },
        ],
        "roles": [],
        "units": [],
        "pointers": [],
        "current_state": [],
        "missing": [],
        "ambiguity": [],
        "budget": {"limit_chars": 4000, "used_chars": 0},
        "generation": {
            "freshness_key": "k",
            "index_generation": 1,
            "roles_hash": "abc",
            "roles_source": "shipped",
        },
        "abstained": False,
    }

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, _release(withheld=()))

    assert guarded is not None
    anchor = guarded["anchors"][0]
    assert "graph_corroboration" not in anchor["evidence"], (
        "LEAK: corroboration evidence survived although the corroborating "
        "neighbour is a withheld page"
    )
    assert "exact_alias" in anchor["evidence"]
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
            "ref": UNIT_REF_OPEN,
            "role": "resources",
            "text": "An open unit with no references.",
            "lifecycle": "active",
            "updated": "2026-09-01",
            "provenance": {"path": OPEN_PATH, "level": "unit", "anchor": OPEN_PATH},
        },
        {
            "ref": UNIT_REF_PROSE_LEAK,
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
    assert [unit["ref"] for unit in guarded["units"]] == [UNIT_REF_OPEN]
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


def _prose_unit(stem: str, ref: str | None = None) -> dict:
    return {
        "ref": ref if ref is not None else _unit_ref(OPEN_PATH, "only-prose"),
        "role": "resources",
        "text": f"See [[{stem}]] for the rest.",
        "lifecycle": "active",
        "updated": "2026-09-01",
        "provenance": {"path": OPEN_PATH, "level": "unit", "anchor": OPEN_PATH},
    }


def _stem_of(path: str) -> str:
    return path.rsplit("/", 1)[-1].removesuffix(".md")


def _prose_release() -> egress.AnnotatedHits:
    """A release that withheld nothing, so only a real decision can withhold."""
    return egress.AnnotatedHits(
        hits=[_hit(OPEN_PATH)], withheld_paths=frozenset(), active=True
    )


def _indexed(vault: Path) -> None:
    """Build the activation index so its page-name map can resolve prose stems."""
    from exomem import working_set_index, working_set_runtime

    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).reset()
    working_set_index.WorkingSetIndex(vault).rebuild()


def test_a_prose_wikilink_to_a_withheld_page_drops_the_unit(vault: Path) -> None:
    """A page named only in prose still gets a release decision.

    `release.withheld_paths` carries what hit projection happened to touch. A
    wikilink in a unit's text can name a page recall never surfaced, so the guard
    resolves the stem to its real path and decides that path.
    """
    write_scope(vault)
    write_rule(vault, ceiling=0)
    _indexed(vault)

    packet = _packet()
    packet["units"] = [_prose_unit(_stem_of(RESTRICTED_PATH))]

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, _prose_release())

    assert guarded is not None
    assert guarded["units"] == []


def test_a_prose_wikilink_to_a_permitted_page_keeps_the_unit_unchanged(
    vault: Path,
) -> None:
    """The defect this closes: a permitted link withheld its own unit.

    A bare stem is not a vault-relative path, so handing it straight to the
    release decision returned `None` — indistinguishable from "withheld" — and
    every unit that linked anything lost itself.
    """
    write_scope(vault)
    write_rule(vault, ceiling=0)
    _indexed(vault)

    permitted = "Knowledge Base/Notes/Insights/autovacuum-thresholds-prevent-table-bloat.md"
    assert (vault / permitted).is_file()
    unit = _prose_unit(_stem_of(permitted), ref=_unit_ref(OPEN_PATH, "permitted-link"))
    packet = _packet()
    packet["units"] = [unit]

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, _prose_release())

    assert guarded is not None
    assert guarded["units"] == [unit]


def test_a_prose_wikilink_to_a_nonexistent_page_keeps_the_unit_unchanged(
    vault: Path,
) -> None:
    """A stem that resolves to no page names nothing, so it decides nothing."""
    write_scope(vault)
    write_rule(vault, ceiling=0)
    _indexed(vault)

    unit = _prose_unit("no-such-page-anywhere", ref=_unit_ref(OPEN_PATH, "dangling-link"))
    packet = _packet()
    packet["units"] = [unit]

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, _prose_release())

    assert guarded is not None
    assert guarded["units"] == [unit]


_PLAIN_APOSTROPHE_TITLE = "Kill switch's risky releases"
_TYPOGRAPHIC_APOSTROPHE_TITLE = "Kill switch’s risky releases"

#: Hyphen cells (correction round 3): a DIFFERENT name pair than the
#: apostrophe one, because the fold in question is different in kind. The
#: ASCII/non-breaking pair is a hyphenated compound word, folded to the
#: same VISIBLE hyphen either way. The soft-hyphen pair is deliberately
#: NOT hyphenated at all: U+00AD is an invisible optional break point
#: INSIDE one word, dropped by `normalize()` rather than folded to a
#: visible hyphen, so "Killswitch" and "Kill­switch" must read as the
#: same one-word name, never as "kill-switch" or two words.
_ASCII_HYPHEN_TITLE = "Kill-switch for risky releases"
_NON_BREAKING_HYPHEN_TITLE = "Kill‑switch for risky releases"
_NO_HYPHEN_TITLE = "Killswitch for risky releases"
_SOFT_HYPHEN_TITLE = "Kill­switch for risky releases"


def _retitled(vault: Path, title: str) -> None:
    """Give the restricted fixture page its own frontmatter `title:` —
    exactly the spelling the activation index records as a page name — so
    the TITLE's own apostrophe or hyphen style, not the fixture's unrelated
    H1, is what a prose wikilink has to resolve against."""
    page = vault / RESTRICTED_PATH
    text = page.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    head, rest = text[4:].split("\n---\n", 1)
    page.write_text(f'---\n{head}\ntitle: "{title}"\n---\n{rest}', encoding="utf-8")


@pytest.mark.parametrize(
    ("title", "prose"),
    [
        (_PLAIN_APOSTROPHE_TITLE, _PLAIN_APOSTROPHE_TITLE),
        (_PLAIN_APOSTROPHE_TITLE, _TYPOGRAPHIC_APOSTROPHE_TITLE),
        (_TYPOGRAPHIC_APOSTROPHE_TITLE, _PLAIN_APOSTROPHE_TITLE),
        (_TYPOGRAPHIC_APOSTROPHE_TITLE, _TYPOGRAPHIC_APOSTROPHE_TITLE),
        (_ASCII_HYPHEN_TITLE, _NON_BREAKING_HYPHEN_TITLE),
        (_NON_BREAKING_HYPHEN_TITLE, _ASCII_HYPHEN_TITLE),
        (_NO_HYPHEN_TITLE, _SOFT_HYPHEN_TITLE),
        (_SOFT_HYPHEN_TITLE, _NO_HYPHEN_TITLE),
    ],
    ids=[
        "plain-apostrophe-title/plain-apostrophe-prose",
        "plain-apostrophe-title/typographic-apostrophe-prose",
        "typographic-apostrophe-title/plain-apostrophe-prose",
        "typographic-apostrophe-title/typographic-apostrophe-prose",
        "ascii-hyphen-title/non-breaking-hyphen-prose",
        "non-breaking-hyphen-title/ascii-hyphen-prose",
        "no-hyphen-title/soft-hyphen-prose",
        "soft-hyphen-title/no-hyphen-prose",
    ],
)
def test_a_withheld_page_stays_withheld_however_its_apostrophe_or_hyphen_was_typed(
    vault: Path, title: str, prose: str
) -> None:
    """Independent review, task 4a.2b: `_resolved_prose_names` resolves a
    prose wikilink NAME to a path through the activation index's
    `page_names` map, keyed by `working_set_index.normalize()` on both the
    index-build side (the title) and the query side (the prose spelling).
    Before the apostrophe and hyphen folds moved into `normalize()` itself,
    a title authored with one apostrophe or hyphen style and prose written
    with the other normalised to two DIFFERENT keys, so resolution found
    nothing at all — not a matching failure downstream, a resolution
    failure — and the unit was served in full. Every spelling combination
    here must resolve to the SAME restricted page and withhold the unit
    that names it."""
    write_scope(vault)
    write_rule(vault, ceiling=0)
    _retitled(vault, title)
    _indexed(vault)

    packet = _packet()
    packet["units"] = [
        _prose_unit(prose, ref=_unit_ref(OPEN_PATH, "apostrophe-or-hyphen-link"))
    ]

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, _prose_release())

    assert guarded is not None
    assert guarded["units"] == [], (
        f"LEAK: title={title!r} named in prose as {prose!r} was served: "
        f"{guarded['units']!r}"
    )


def test_an_anchor_title_naming_a_withheld_page_drops_the_anchor(vault: Path) -> None:
    """`title` is authored prose and can carry a wikilink like any other field."""
    write_scope(vault)
    write_rule(vault, ceiling=0)
    _indexed(vault)

    packet = _packet()
    packet["anchors"] = [
        {
            "ref": OPEN_PATH,
            "path": OPEN_PATH,
            "title": f"Open hub (supersedes [[{_stem_of(RESTRICTED_PATH)}]])",
            "kind": "hub",
            "status": "resolved",
            "evidence": ["lexical_overlap", "retrieval"],
        }
    ]
    packet["units"] = []

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, _prose_release())

    assert guarded is not None
    assert guarded["anchors"] == []
    assert _stem_of(RESTRICTED_PATH) not in str(guarded)


def test_missing_entries_are_scanned_too(vault: Path) -> None:
    packet = _packet()
    packet["missing"] = [
        {"role": "resources", "reason": "no_material", "path": RESTRICTED_PATH},
        {"role": "methods", "reason": "lane_truncated"},
    ]

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, _release())

    assert guarded is not None
    roles = [entry["role"] for entry in guarded["missing"]]
    # The entry naming a withheld path is gone; the clean one stays. The guard's
    # own per-section markers ride alongside it.
    assert "resources" not in roles
    assert "methods" in roles
    assert RESTRICTED_PATH not in str(guarded["missing"])


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


def _project_member_packet() -> dict:
    """A resolved PROJECT anchor whose material -- exactly like `_packet()`'s
    hub -- comes from its member pages' own paths: one withheld
    (`RESTRICTED_PATH`), one open (`OPEN_PATH`). The project anchor's own
    `path` is empty by construction (a project key has no page of its own);
    what identifies each unit and pointer to the guard is its member page's
    `provenance.path`/`ref`, precisely as any other neighbourhood-sourced
    unit already works.
    """
    return {
        "anchors": [
            {
                "ref": "project:harbor-survey",
                "path": "",
                "title": "Harbor Survey",
                "kind": "project",
                "status": "resolved",
                "evidence": ["exact_alias"],
            },
        ],
        "roles": [{"id": "constraints", "source": "anchor_default", "lane": "units"}],
        "units": [
            {
                "ref": OPEN_PATH,
                "role": "constraints",
                "text": "Draft may not exceed 4 metres at low tide.",
                "lifecycle": "active",
                "updated": "2026-09-09",
                "provenance": {
                    "path": OPEN_PATH,
                    "level": "unit",
                    "anchor": "project:harbor-survey",
                },
            },
            {
                "ref": RESTRICTED_PATH,
                "role": "constraints",
                "text": "Draft may not exceed 2 metres at neap tide, classified survey.",
                "lifecycle": "active",
                "updated": "2026-09-10",
                "provenance": {
                    "path": RESTRICTED_PATH,
                    "level": "unit",
                    "anchor": "project:harbor-survey",
                },
            },
        ],
        "pointers": [
            {"ref": OPEN_PATH, "role": "constraints", "title": "Visible member", "reason": "budget"},
            {
                "ref": RESTRICTED_PATH,
                "role": "constraints",
                "title": "Withheld member",
                "reason": "budget",
            },
        ],
        "current_state": [],
        "missing": [],
        "ambiguity": [],
        "budget": {"limit_chars": 4000, "used_chars": 80},
        "generation": {
            "freshness_key": "k",
            "index_generation": 3,
            "roles_hash": "abc",
            "roles_source": "shipped",
        },
        "abstained": False,
    }


def test_a_withheld_project_member_page_yields_no_unit_and_no_pointer(
    vault: Path,
) -> None:
    """`close-memory-loop` requirement A: no new disclosure path. A project
    anchor's member pages are now recorded as its index-time `links`
    (previously a project anchor built with no path and no links carried no
    material at all), so they reach the units lane's `allowed_parent_paths`
    exactly like any other neighbourhood page -- and MUST cross the SAME
    unconditional egress guard as any other neighbourhood page, never a new
    disclosure path of their own, exactly as `test_no_field_of_the_packet_
    names_a_withheld_page` already proves for an ordinary hub. A project
    with one withheld member and one visible member must keep only the
    visible member's unit and pointer.
    """
    write_scope(vault)
    write_rule(vault, ceiling=0)

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, _project_member_packet(), _release())

    assert guarded is not None
    assert [unit["ref"] for unit in guarded["units"]] == [OPEN_PATH]
    assert [pointer["ref"] for pointer in guarded["pointers"]] == [OPEN_PATH]
    assert RESTRICTED_PATH not in str(guarded)


# --------------------------------------------------------------------------- #
# Micro-round: a broken resolver is a release-plane failure, not a silent pass
# --------------------------------------------------------------------------- #


def _wikilink_unit_packet(stem: str) -> dict:
    packet = _packet()
    packet["units"] = [_prose_unit(stem)]
    return packet


def test_a_broken_name_resolver_raises_rather_than_dropping_the_filter(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"Name unknown" and "resolver broken" are different answers.

    Swallowing a query error into "no names resolved" removes the filter exactly
    when it is least safe to remove it, which is the fail-open class the guard was
    rebuilt to close.
    """
    from exomem import working_set_index

    write_scope(vault)
    write_rule(vault, ceiling=0)
    _indexed(vault)

    def boom(self, names):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(working_set_index.WorkingSetIndex, "resolve_names", boom)

    with pytest.raises(Exception, match="prose|resolve|locked"):  # noqa: B017
        with request_scope(_external()):
            egress.guard_working_set(
                vault, _wikilink_unit_packet(_stem_of(RESTRICTED_PATH)), _prose_release()
            )


def test_an_unavailable_index_at_guard_time_is_a_release_plane_failure(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The packet was compiled FROM the index, so an absent one is a contradiction."""
    from exomem import working_set_index

    write_scope(vault)
    write_rule(vault, ceiling=0)
    _indexed(vault)

    monkeypatch.setattr(
        working_set_index.WorkingSetIndex, "available", lambda self: False
    )

    with pytest.raises(egress.WorkingSetResolutionUnavailable):
        with request_scope(_external()):
            egress.guard_working_set(
                vault, _wikilink_unit_packet(_stem_of(RESTRICTED_PATH)), _prose_release()
            )


def test_a_resolver_failure_abstains_through_the_operation(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: the operation's guard handler turns it into an abstention."""
    from test_latency_gate import _seed_freshness_live
    from test_working_set_index import _seed_planning, _seed_structure

    from exomem import commands, lexstore, working_set_index, working_set_runtime

    _seed_structure(vault)
    _seed_planning(vault)
    # A constraint unit whose prose links another page, so the guard must resolve.
    (vault / "Knowledge Base" / "Products" / "Tow Bar.md").write_text(
        """---
type: note
status: active
updated: 2026-09-07
---

# Tow Bar

## Summary

The bar that couples the sled.

## Constraints

Never tow without checking [[Cargo Sled]] first.
""",
        encoding="utf-8",
    )
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).reset()
    working_set_index.WorkingSetIndex(vault).rebuild()

    write_scope(vault, paths="Products/**")
    write_rule(vault, ceiling=0, audience="external")

    # Publication owns catalogue warming. Activation must not rebuild it just
    # to reach this test's downstream prose-release failure.
    _seed_freshness_live(vault)
    lexstore.ensure_fresh(vault)
    turn = "what are the constraints on the tow bar"
    with request_scope(_external()):
        baseline = commands.op_activate_context(vault, turn=turn)
    assert baseline is not None

    def boom(self, names):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(working_set_index.WorkingSetIndex, "resolve_names", boom)
    working_set_runtime.reset_caches_for_tests()

    with request_scope(_external()):
        packet = commands.op_activate_context(vault, turn=turn)

    assert packet["abstained"] is True
    assert packet["abstention"] == {"reason": "unavailable"}
    assert packet["units"] == []
    assert packet["anchors"] == []


def test_an_unknown_name_still_decides_nothing(vault: Path) -> None:
    """The unknown-name path is unchanged: it names no page, so it withholds none."""
    write_scope(vault)
    write_rule(vault, ceiling=0)
    _indexed(vault)

    unit = _prose_unit("still-no-such-page", ref=_unit_ref(OPEN_PATH, "unknown"))
    packet = _packet()
    packet["units"] = [unit]

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, _prose_release())

    assert guarded is not None
    assert guarded["units"] == [unit]


def test_a_colliding_name_decides_every_page_that_bears_it(vault: Path) -> None:
    """One name, two pages: the withheld one must still be decided.

    `page_names` normalises (NFKC + casefold), so `Widget.md` and `widget.md` share
    a key. Keeping one row per name let the loser go undecided, and a wikilink to
    the shared stem would then be served against a withheld page.
    """
    from exomem import working_set_index

    withheld = vault / "Knowledge Base" / "Notes" / "Patterns" / "Widget.md"
    permitted = vault / "Knowledge Base" / "Notes" / "Insights" / "widget.md"
    for path, title in ((withheld, "Widget"), (permitted, "widget")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\ntype: pattern\nstatus: active\nupdated: 2026-09-01\n---\n\n# {title}\n\nBody.\n",
            encoding="utf-8",
        )

    write_scope(vault)  # Notes/Patterns/** -> Widget.md is withheld
    write_rule(vault, ceiling=0)
    _indexed(vault)

    index = working_set_index.WorkingSetIndex(vault)
    resolved = index.resolve_names(["widget"])
    assert set(resolved.get("widget") or ()) == {
        "Knowledge Base/Notes/Patterns/Widget.md",
        "Knowledge Base/Notes/Insights/widget.md",
    }, resolved

    packet = _packet()
    packet["units"] = [_prose_unit("widget", ref=_unit_ref(OPEN_PATH, "colliding-link"))]

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, _prose_release())

    assert guarded is not None
    assert guarded["units"] == []


# --------------------------------------------------------------------------- #
# Round three: every spelling of a reference, and no sidecar on an open vault
# --------------------------------------------------------------------------- #

RESTRICTED_TITLE = "Kill switch for risky releases"


def _titled_release() -> egress.AnnotatedHits:
    """The withheld page is already in the release's withheld set, no policy."""
    return egress.AnnotatedHits(
        hits=[_hit(OPEN_PATH)],
        withheld_paths=frozenset({RESTRICTED_PATH}),
        active=True,
    )


def test_a_title_spelled_wikilink_to_a_withheld_page_drops_the_unit(
    vault: Path,
) -> None:
    """The reviewer's leak: comparison stems come from filenames, titles do not.

    `[[Kill switch for risky releases]]` resolves to the withheld path, the path is
    decided and withheld — and then the prose match canonicalises the target to the
    spaced title, which is no filename stem, so the unit was served. Every earlier
    test linked by stem, which is why this stayed invisible.
    """
    write_scope(vault)
    write_rule(vault, ceiling=0)
    _indexed(vault)

    packet = _packet()
    packet["units"] = [_prose_unit(RESTRICTED_TITLE, ref=_unit_ref(OPEN_PATH, "title-link"))]

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, _prose_release())

    assert guarded is not None
    assert guarded["units"] == []
    assert RESTRICTED_TITLE.casefold() not in str(guarded).casefold()


def test_a_title_spelled_wikilink_is_caught_with_no_policy_at_all(vault: Path) -> None:
    """Already-withheld is enough: the leak did not need a policy to fire."""
    _indexed(vault)

    packet = _packet()
    packet["units"] = [_prose_unit(RESTRICTED_TITLE, ref=_unit_ref(OPEN_PATH, "title-link"))]

    guarded = egress.guard_working_set(vault, packet, _titled_release())

    assert guarded is not None
    assert guarded["units"] == []


def test_a_title_spelled_wikilink_to_a_permitted_page_keeps_the_unit(
    vault: Path,
) -> None:
    write_scope(vault)
    write_rule(vault, ceiling=0)
    _indexed(vault)

    unit = _prose_unit(
        "Autovacuum thresholds prevent table bloat", ref=_unit_ref(OPEN_PATH, "open-title")
    )
    packet = _packet()
    packet["units"] = [unit]

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, _prose_release())

    assert guarded is not None
    assert guarded["units"] == [unit]


def _prose_only_packet(spelling: str) -> dict:
    """A packet naming the withheld page NOWHERE except one unit's prose.

    `_packet()` lists it in anchors, pointers, current_state and ambiguity, so the
    decision loop withholds the path from those fields and the prose match then
    succeeds however the link was spelled — which hides whether the resolver ever
    saw the target. Isolating it here is what makes the resolver's own behaviour
    observable.
    """
    return {
        "anchors": [
            {
                "ref": OPEN_PATH,
                "path": OPEN_PATH,
                "title": "Open anchor",
                "kind": "resource",
                "status": "resolved",
                "evidence": ["lexical_overlap"],
            }
        ],
        "roles": [{"id": "resources", "source": "anchor_default", "lane": "units"}],
        "units": [_prose_unit(spelling, ref=_unit_ref(OPEN_PATH, "decorated-link"))],
        "pointers": [],
        "current_state": [],
        "missing": [],
        "ambiguity": [],
        "budget": {"limit_chars": 4000, "used_chars": 40},
        "generation": {
            "freshness_key": "k",
            "index_generation": 3,
            "roles_hash": "abc",
            "roles_source": "shipped",
        },
        "abstained": False,
    }


@pytest.mark.parametrize(
    "spelling",
    [
        _stem_of(RESTRICTED_PATH),
        f"{_stem_of(RESTRICTED_PATH)}|the kill switch",
        f"{_stem_of(RESTRICTED_PATH)}#Summary",
        RESTRICTED_TITLE,
        f"{RESTRICTED_TITLE}|that pattern",
        f"{RESTRICTED_TITLE}#Summary",
    ],
)
def test_every_spelling_of_a_prose_only_withheld_link_drops_the_unit(
    vault: Path, spelling: str
) -> None:
    """Four spellings, one identity: stem, title, `[[x|label]]`, `[[x#Section]]`.

    The alias and heading forms are presentation. Handing the raw capture to the
    resolver made them resolve to nothing, so a page named only in prose was never
    decided and the unit was served.
    """
    write_scope(vault)
    write_rule(vault, ceiling=0)
    _indexed(vault)

    with request_scope(_external()):
        guarded = egress.guard_working_set(
            vault, _prose_only_packet(spelling), _prose_release()
        )

    assert guarded is not None
    assert guarded["units"] == []


@pytest.mark.parametrize(
    "spelling",
    [
        "autovacuum-thresholds-prevent-table-bloat",
        "autovacuum-thresholds-prevent-table-bloat|the autovacuum note",
        "autovacuum-thresholds-prevent-table-bloat#Summary",
        "Autovacuum thresholds prevent table bloat",
        "Autovacuum thresholds prevent table bloat|that note",
    ],
)
def test_every_spelling_of_a_permitted_link_keeps_the_unit(
    vault: Path, spelling: str
) -> None:
    write_scope(vault)
    write_rule(vault, ceiling=0)
    _indexed(vault)

    packet = _prose_only_packet(spelling)
    expected = list(packet["units"])

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, _prose_release())

    assert guarded is not None
    assert guarded["units"] == expected


def test_an_ungoverned_vault_never_touches_the_activation_sidecar(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No policy, no grants, nothing withheld: release needs no sidecar at all.

    Resolving above the fast path made a vault that has opted into no governance
    depend on a derived index for its reads, so a sidecar hiccup abstained a
    request that had nothing to decide.
    """
    from exomem import working_set_index

    calls: list[int] = []

    def boom(self, names):
        calls.append(1)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(working_set_index.WorkingSetIndex, "resolve_names", boom)

    packet = _packet()
    packet["units"] = [_prose_unit(_stem_of(RESTRICTED_PATH))]
    open_release = egress.AnnotatedHits(
        hits=[_hit(OPEN_PATH)], withheld_paths=frozenset(), active=False
    )

    guarded = egress.guard_working_set(vault, packet, open_release)

    assert guarded == packet
    assert calls == [], "an ungoverned read must not consult the activation index"


def test_a_governed_vault_still_fails_closed_on_a_sidecar_error(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The NEW-7 move must not weaken the governed path."""
    from exomem import working_set_index

    write_scope(vault)
    write_rule(vault, ceiling=0)
    _indexed(vault)

    def boom(self, names):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(working_set_index.WorkingSetIndex, "resolve_names", boom)

    packet = _packet()
    packet["units"] = [_prose_unit(_stem_of(RESTRICTED_PATH))]

    with pytest.raises(egress.WorkingSetResolutionUnavailable):
        with request_scope(_external()):
            egress.guard_working_set(vault, packet, _prose_release())


# --------------------------------------------------------------------------- #
# Round four: match the spelling the prose contains, and say when we removed
# --------------------------------------------------------------------------- #

NBSP = " "
FULLWIDTH_P = "Ｐ"


def _titled_page(vault: Path, rel: str, title: str) -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntype: pattern\ntitle: {title}\nstatus: active\nupdated: 2026-09-01\n---\n\n"
        f"# {title}\n\nBody.\n",
        encoding="utf-8",
    )


#: `(title as authored, the spelling prose uses)`. Each pair differs from its own
#: NFKC normalisation, which is the whole point: the match set was rebuilt from a
#: canonical form instead of from the text the prose actually contains.
_DIVERGENT_SPELLINGS = [
    (f"Nbsp{NBSP}Titled Page", f"Nbsp{NBSP}Titled Page"),
    (f"Fullwidth {FULLWIDTH_P}age", f"Fullwidth {FULLWIDTH_P}age"),
]


@pytest.mark.parametrize(("title", "spelling"), _DIVERGENT_SPELLINGS)
def test_a_unicode_divergent_spelling_of_a_withheld_title_drops_the_unit(
    vault: Path, title: str, spelling: str
) -> None:
    """The match must be made on the spelling the prose contains.

    The resolver's key is NFKC + casefold; `_canonical_reference` casefolds only.
    A title carrying a non-breaking space therefore resolved and was decided
    withheld, and the prose spelled with the NBSP still matched nothing.
    """
    _titled_page(vault, "Knowledge Base/Notes/Patterns/divergent.md", title)
    write_scope(vault)  # Notes/Patterns/** -> withheld from this audience
    write_rule(vault, ceiling=0)
    _indexed(vault)

    with request_scope(_external()):
        guarded = egress.guard_working_set(
            vault, _prose_only_packet(spelling), _prose_release()
        )

    assert guarded is not None
    assert guarded["units"] == []


@pytest.mark.parametrize(("title", "spelling"), _DIVERGENT_SPELLINGS)
def test_a_unicode_divergent_spelling_of_a_permitted_title_keeps_the_unit(
    vault: Path, title: str, spelling: str
) -> None:
    _titled_page(vault, "Knowledge Base/Notes/Insights/divergent.md", title)
    write_scope(vault)  # only Notes/Patterns/** is governed
    write_rule(vault, ceiling=0)
    _indexed(vault)

    packet = _prose_only_packet(spelling)
    expected = list(packet["units"])

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, _prose_release())

    assert guarded is not None
    assert guarded["units"] == expected


@pytest.mark.parametrize(("title", "spelling"), _DIVERGENT_SPELLINGS)
def test_the_plain_normalised_spelling_still_matches(
    vault: Path, title: str, spelling: str
) -> None:
    """Adding the raw key must not cost the normalised one."""
    import unicodedata

    _titled_page(vault, "Knowledge Base/Notes/Patterns/divergent.md", title)
    write_scope(vault)
    write_rule(vault, ceiling=0)
    _indexed(vault)

    normalised = unicodedata.normalize("NFKC", spelling)
    assert normalised != spelling, "this row must diverge under NFKC to be a test"

    with request_scope(_external()):
        guarded = egress.guard_working_set(
            vault, _prose_only_packet(normalised), _prose_release()
        )

    assert guarded is not None
    assert guarded["units"] == []


def test_a_guard_removal_is_reported_per_section(vault: Path) -> None:
    """The reviewer's shared-title case: the permitted twin loses its own anchor.

    Fail-closed is right for v0 — a title that a withheld page also bears cannot
    be told apart here — but a silent removal reads exactly like a vault with
    nothing to say, which is what `lane_truncated` and `budget` already refuse to
    do.
    """
    shared = "Shared Title"
    _titled_page(vault, "Knowledge Base/Notes/Patterns/withheld-twin.md", shared)
    _titled_page(vault, "Knowledge Base/Notes/Insights/permitted-twin.md", shared)
    write_scope(vault)
    write_rule(vault, ceiling=0)
    _indexed(vault)

    permitted = "Knowledge Base/Notes/Insights/permitted-twin.md"
    # The prose links the SHARED title, so it resolves to both twins, one of them
    # withheld — which is what promotes the title to a match key and then takes
    # the permitted twin's own anchor row with it.
    packet = _prose_only_packet(shared)
    packet["anchors"] = [
        {
            "ref": permitted,
            "path": permitted,
            "title": shared,
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
            "evidence": ["lexical_overlap"],
        },
    ]

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, _prose_release())

    assert guarded is not None
    assert [anchor["ref"] for anchor in guarded["anchors"]] == [OPEN_PATH]
    assert {"role": "anchors", "reason": "withheld"} in guarded["missing"]
    # The marker names nothing.
    for marker in guarded["missing"]:
        assert set(marker) == {"role", "reason"}


def test_a_packet_with_nothing_withheld_carries_no_withheld_marker(vault: Path) -> None:
    write_scope(vault)
    write_rule(vault, ceiling=6)
    _indexed(vault)

    packet = _prose_only_packet("no-such-page")

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, _prose_release())

    assert guarded is not None
    assert guarded["units"]
    assert [m for m in guarded["missing"] if m.get("reason") == "withheld"] == []


def test_withheld_markers_survive_the_guards_own_scan(vault: Path) -> None:
    """A marker must not be filtered by the scan that produced it.

    `missing[]` entries are compared as reference fields, so a bare word matches a
    withheld page's stem. A vault holding `anchors.md` or `withheld.md` would
    otherwise delete the very marker explaining the removal.
    """
    _titled_page(vault, "Knowledge Base/Notes/Patterns/anchors.md", "Anchors")
    _titled_page(vault, "Knowledge Base/Notes/Patterns/withheld.md", "Withheld")
    write_scope(vault)
    write_rule(vault, ceiling=0)
    _indexed(vault)

    packet = _prose_only_packet(_stem_of(RESTRICTED_PATH))

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, _prose_release())

    assert guarded is not None
    assert guarded["units"] == []
    assert {"role": "units", "reason": "withheld"} in guarded["missing"]


def test_the_fully_withheld_flip_keeps_its_markers(vault: Path) -> None:
    write_scope(vault, paths="**")
    write_rule(vault, ceiling=0)
    _indexed(vault)

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, _packet(), _prose_release())

    assert guarded is not None
    assert guarded["abstained"] is True
    assert guarded["abstention"] == {"reason": "withheld"}
    assert {"role": "anchors", "reason": "withheld"} in guarded["missing"]


# --------------------------------------------------------------------------- #
# Round five: a frontmatter alias is identity too
# --------------------------------------------------------------------------- #

ALIAS_ONE = "Dossier"
ALIAS_TWO = "The Partner File"


def _aliased_page(vault: Path, rel: str, title: str, aliases: list[str]) -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = ", ".join(aliases)
    path.write_text(
        f"---\ntype: pattern\ntitle: {title}\naliases: [{rendered}]\n"
        f"status: active\nupdated: 2026-09-01\n---\n\n# {title}\n\nBody.\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize("spelling", [ALIAS_ONE, ALIAS_TWO])
def test_an_alias_spelled_link_to_a_withheld_page_drops_the_unit(
    vault: Path, spelling: str
) -> None:
    """`[[Dossier]]` is a working Obsidian link, so it is identity, not decoration.

    The page-name map indexed four spellings and read `aliases` two lines later for
    the anchor row without ever adding them, so the stem and title spellings
    dropped the unit while every alias was served. `exact_alias` is the resolver's
    strongest evidence kind; the guard cannot treat it as weaker.
    """
    _aliased_page(
        vault,
        "Knowledge Base/Notes/Patterns/partner-dossier.md",
        "Partner dossier",
        [ALIAS_ONE, ALIAS_TWO],
    )
    write_scope(vault)  # Notes/Patterns/** -> withheld from this audience
    write_rule(vault, ceiling=0)
    _indexed(vault)

    with request_scope(_external()):
        guarded = egress.guard_working_set(
            vault, _prose_only_packet(spelling), _prose_release()
        )

    assert guarded is not None
    assert guarded["units"] == []


@pytest.mark.parametrize("spelling", [ALIAS_ONE, ALIAS_TWO])
def test_an_alias_spelled_link_to_a_permitted_page_keeps_the_unit(
    vault: Path, spelling: str
) -> None:
    _aliased_page(
        vault,
        "Knowledge Base/Notes/Insights/partner-dossier.md",
        "Partner dossier",
        [ALIAS_ONE, ALIAS_TWO],
    )
    write_scope(vault)  # only Notes/Patterns/** is governed
    write_rule(vault, ceiling=0)
    _indexed(vault)

    packet = _prose_only_packet(spelling)
    expected = list(packet["units"])

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, _prose_release())

    assert guarded is not None
    assert guarded["units"] == expected


def test_an_alias_shared_by_a_withheld_and_a_permitted_page_drops_the_unit(
    vault: Path,
) -> None:
    """The collision ruling applies to aliases exactly as it does to titles."""
    _aliased_page(
        vault,
        "Knowledge Base/Notes/Patterns/withheld-dossier.md",
        "Withheld dossier",
        [ALIAS_ONE],
    )
    _aliased_page(
        vault,
        "Knowledge Base/Notes/Insights/permitted-dossier.md",
        "Permitted dossier",
        [ALIAS_ONE],
    )
    write_scope(vault)
    write_rule(vault, ceiling=0)
    _indexed(vault)

    with request_scope(_external()):
        guarded = egress.guard_working_set(
            vault, _prose_only_packet(ALIAS_ONE), _prose_release()
        )

    assert guarded is not None
    assert guarded["units"] == []
    assert {"role": "units", "reason": "withheld"} in guarded["missing"]


def test_an_alias_spelling_resolves_to_every_page_bearing_it(vault: Path) -> None:
    from exomem import working_set_index

    _aliased_page(
        vault,
        "Knowledge Base/Notes/Patterns/withheld-dossier.md",
        "Withheld dossier",
        [ALIAS_ONE],
    )
    _aliased_page(
        vault,
        "Knowledge Base/Notes/Insights/permitted-dossier.md",
        "Permitted dossier",
        [ALIAS_ONE],
    )
    _indexed(vault)

    resolved = working_set_index.WorkingSetIndex(vault).resolve_names([ALIAS_ONE])

    assert set(resolved.get(working_set_index.normalize(ALIAS_ONE)) or ()) == {
        "Knowledge Base/Notes/Patterns/withheld-dossier.md",
        "Knowledge Base/Notes/Insights/permitted-dossier.md",
    }


# --------------------------------------------------------------------------- #
# Round five: a unit's REF is an opaque URI, not a vault-relative path — the
# activation-egress unit-ref defect. `_units_lane` hands `_working_set_paths`
# the `exomem://vault/<percent-encoded path>#unit-<hash>` URI
# `context_refs.vault_ref` builds, never a plain path. Handing that literal
# URI text to `_decide_path` as though it already were a vault-relative path
# always fails to stat, and the fail-closed `None` collapses onto the REAL
# page's canonical key through `_canonical_reference` — so under ANY active
# governance policy, even one scoped to an unrelated folder, every unit reads
# as withheld and the whole packet abstains.
# --------------------------------------------------------------------------- #


def _write_page(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


_CARGO_SLED_PAGE = """---
type: note
status: active
updated: 2026-09-02
---

# Cargo Sled

## Summary

A towed cargo sled rated for 400 kg. Attaches via the [[Tow Cable]].

## Constraints

Never exceed 400 kg.
"""

_TOW_CABLE_PAGE = """---
type: note
status: active
updated: 2026-09-04
---

# Tow Cable

## Summary

A hand-cranked winch cable used to tow the cargo sled.

## Constraints

Rated for loads under 150 kg.
"""


def _prepare_end_to_end_vault(vault: Path) -> None:
    """Build the working-set index and freshness state a real activation needs."""
    from test_latency_gate import _seed_freshness_live

    from exomem import lexstore, working_set_index, working_set_runtime

    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).reset()
    working_set_index.WorkingSetIndex(vault).rebuild()
    _seed_freshness_live(vault)
    lexstore.ensure_fresh(vault)


def _reset_governance_state(vault: Path) -> None:
    from exomem import working_set_runtime
    from exomem.governance import membership
    from exomem.governance import policy as policy_module

    policy_module._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()
    working_set_runtime.reset_caches_for_tests()


def test_a_policy_scoped_elsewhere_serves_units_end_to_end(tmp_path: Path) -> None:
    """The defect this closes: ANY active policy abstained every packet.

    A policy scoped to a folder that has nothing to do with the anchored page
    must leave that page's units served, exactly like the ungoverned baseline.
    """
    from test_working_set_index import _seed_structure

    from exomem import commands

    vault = tmp_path / "vault"
    vault.mkdir()
    _seed_structure(vault)
    _prepare_end_to_end_vault(vault)

    turn = "what are the constraints on the cargo sled"
    with request_scope(_external()):
        baseline = commands.op_activate_context(vault, turn=turn)
    assert baseline["abstained"] is False
    assert len(baseline["units"]) == 3

    write_scope(vault)  # default paths="Notes/Patterns/**" -- unrelated to Products/
    write_rule(vault, ceiling=0, audience="external")
    _reset_governance_state(vault)

    with request_scope(_external()):
        governed = commands.op_activate_context(vault, turn=turn)

    assert governed["abstained"] is False, governed.get("abstention")
    assert [a["path"] for a in governed["anchors"]] == [
        a["path"] for a in baseline["anchors"]
    ]
    assert len(governed["units"]) == 3
    assert {u["ref"] for u in governed["units"]} == {u["ref"] for u in baseline["units"]}


def test_a_policy_scoped_to_one_page_withholds_only_that_page_end_to_end(
    tmp_path: Path,
) -> None:
    """Both directions, with the real ref shape, in one packet.

    Two linked pages resolve as two anchors from one turn. A policy scoped to
    ONE of them must withhold that page's anchor and units while leaving the
    other page's anchor and units served — never the whole packet, and never
    the wrong page.
    """
    from exomem import commands

    vault = tmp_path / "vault"
    kb = vault / "Knowledge Base"
    _write_page(kb / "Products" / "Cargo Sled.md", _CARGO_SLED_PAGE)
    _write_page(kb / "Products" / "Tow Cable.md", _TOW_CABLE_PAGE)
    _prepare_end_to_end_vault(vault)

    turn = "what are the constraints on the cargo sled and the tow cable"
    write_scope(vault, paths="Products/Cargo Sled.md")
    write_rule(vault, ceiling=0, audience="external")
    _reset_governance_state(vault)

    with request_scope(_external()):
        governed = commands.op_activate_context(vault, turn=turn)

    assert governed["abstained"] is False, governed.get("abstention")
    served_paths = {a["path"] for a in governed["anchors"]}
    assert served_paths == {"Knowledge Base/Products/Tow Cable.md"}
    assert "Knowledge Base/Products/Cargo Sled.md" not in str(governed)
    # `provenance.path` is legitimately empty on the current-state unit (its
    # only anchor is `provenance.anchor`), so anchor is the field that always
    # names the real page.
    unit_anchors = {u["provenance"]["anchor"] for u in governed["units"]}
    assert unit_anchors == {"Knowledge Base/Products/Tow Cable.md"}


def _unit_with_ref(ref: str) -> dict:
    return {
        "ref": ref,
        "role": "resources",
        "text": "A unit.",
        "lifecycle": "active",
        "updated": "2026-09-01",
        "provenance": {"category": "constraint", "kind": "constraint"},
    }


def test_working_set_paths_decides_a_units_real_page_not_its_opaque_uri() -> None:
    """Guard-level reproduction, isolated from recall/anchor resolution.

    `_units_lane` never hands `_working_set_paths` a plain path for `ref` — it
    hands the opaque `exomem://vault/<percent-encoded path>#unit-<hash>` URI
    `context_refs.vault_ref` builds. `_working_set_paths` must resolve that URI
    to the real path it names before anything downstream decides it.
    """
    real_path = "Knowledge Base/Products/Cargo Sled.md"
    ref = f"{context_refs.vault_ref(real_path)}#unit-{'a' * 16}"
    packet = {"units": [_unit_with_ref(ref)]}

    paths, names, interpretations, unresolvable = egress._working_set_paths(packet)

    assert paths == {real_path}
    assert names == set()
    assert interpretations == {ref: frozenset({real_path})}
    assert unresolvable == set()


@pytest.mark.parametrize(
    ("case", "expect_unresolvable"),
    [
        ("percent_encoded_parent_traversal", True),
        ("absolute_drive_letter_path", True),
        ("unknown_exomem_authority", True),
        ("empty_path", True),
        ("memory_id_ref", False),
    ],
)
def test_working_set_paths_classifies_an_unresolvable_reference(
    case: str, expect_unresolvable: bool
) -> None:
    """A reference that cannot be unwrapped to an in-vault path is never
    silently decided as a path — but it is not always the same kind of
    "not a path" (`_interpretations_for`/`_matches_explicit_non_page_shape`,
    correction round 2's R1/R3 rewrite of the classifier this test used to
    name).

    A candidate whose own shape marks it as a page reference (an `exomem://`
    scheme) but that fails to validate has ZERO safety-valid interpretations
    and lands in `unresolvable`: the caller must withhold the item that
    carries it, never silently drop it (the exact hole a reviewer found in
    round 1 — dropping it served a page named only through that reference).
    A reference that is legitimately not a page at all (the memory-id form)
    is an explicit non-page shape (R1) and never appears in `paths`,
    `interpretations`, or `unresolvable`.

    Each ref is built with the same real URI shape the pipeline emits (the
    `context_refs`/`memory_refs` helpers, never a hand-typed plain path).
    """
    import uuid

    from exomem import memory_refs

    fragment = f"#unit-{'a' * 16}"
    refs = {
        "percent_encoded_parent_traversal": (
            context_refs.vault_ref("../../etc/passwd.md") + fragment
        ),
        "absolute_drive_letter_path": (
            context_refs.vault_ref("C:/example/local/config.md") + fragment
        ),
        "unknown_exomem_authority": f"{context_refs.SCHEME}://config/Foo.md{fragment}",
        "empty_path": context_refs.vault_ref("") + fragment,
        "memory_id_ref": memory_refs.memory_ref(str(uuid.uuid4())),
    }
    ref = refs[case]
    packet = {"units": [_unit_with_ref(ref)]}

    paths, names, interpretations, unresolvable = egress._working_set_paths(packet)

    assert paths == set()
    assert names == set()
    assert interpretations == {}
    assert unresolvable == ({ref} if expect_unresolvable else set())


# --------------------------------------------------------------------------- #
# Round six: correction round 1. A reviewer found two defects in round five's
# fix. (1) `_unwrap_reference` percent-decoded a scheme'd ref's remainder
# BEFORE splitting on `#`/`|`; a real filename containing an ENCODED `#`
# (`%23`) or `|` (`%7C`) reappeared after decoding and the split truncated
# the path, so `_unwrapped_vault_path` returned `None` for a real page. (2)
# round five's fix then DROPPED that candidate instead of withholding the
# item that carried it — the old, pre-round-five code failed closed on this
# shape (the bogus literal URI text always failed to decide, and fail-closed
# withheld it); round five's fix failed OPEN (dropped -> never decided ->
# served if nothing else independently withheld the page). This section
# proves both fixes: the decode order (a URI ref and its plain path produce
# the SAME canonical key and the SAME release decision even for a filename
# containing `#`, `|`, `%`, a space or a non-ASCII character) and the
# never-drop rule (an un-unwrappable but page-shaped reference withholds its
# item, and never reaches `_decide_path`/the filesystem).
# --------------------------------------------------------------------------- #

#: `(id, filename)`. Each name is chosen so a naive decode-then-split
#: recovers the WRONG (truncated) path: `%23`/`%7C` decode to characters the
#: old code split on again, `%25` decodes to a bare `%`, `%20` to a space,
#: and the non-ASCII name exercises decoding generally.
_TRICKY_FILENAMES = [
    ("hash", "secret#page.md"),
    ("pipe", "secret|page.md"),
    ("percent", "100%.md"),
    ("space", "secret page.md"),
    ("non_ascii", "café.md"),
]


def _write_page(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _ref_only_packet(ref: str) -> dict:
    """A packet whose page is named ONLY through one unit's opaque `ref` —
    no `path`/`anchor` field on the item, matching the reviewer's probe: the
    neighbourhood/Records/supersession case `guard_working_set`'s own
    docstring says the compiler walks past hit projection, so
    `release.withheld_paths` does not already carry the page either."""
    return {
        "anchors": [],
        "roles": [],
        "units": [
            {
                "ref": ref,
                "role": "resources",
                "text": "the payload text",
                "lifecycle": "active",
                "updated": "2026-09-01",
                "provenance": {"level": "unit"},
            }
        ],
        "pointers": [{"ref": ref, "role": "resources", "title": "t", "reason": "budget"}],
        "current_state": [],
        "missing": [],
        "ambiguity": [],
        "budget": {"limit_chars": 4000, "used_chars": 40},
        "generation": {
            "freshness_key": "k",
            "index_generation": 1,
            "roles_hash": "abc",
            "roles_source": "shipped",
        },
        "abstained": False,
    }


def _empty_release() -> egress.AnnotatedHits:
    """Hit projection never touched the page: it is named ONLY via the ref."""
    return egress.AnnotatedHits(hits=[], withheld_paths=frozenset(), active=True)


@pytest.mark.parametrize(("case", "filename"), _TRICKY_FILENAMES, ids=[c for c, _ in _TRICKY_FILENAMES])
def test_a_page_withheld_only_through_its_units_ref_stays_withheld(
    tmp_path: Path, case: str, filename: str
) -> None:
    """The reviewer's blocker, reproduced and closed.

    Round five's fix DROPPED a unit ref it could not resolve to a path, which
    served this exact page: it is withheld by an active policy, named only
    through the unit's own opaque ref (no separate `path`/`anchor`, empty
    `release.withheld_paths`), and its filename contains a character
    `context_refs._encode` percent-encodes.
    """
    vault = tmp_path / "vault"
    rel_path = f"Knowledge Base/Notes/Patterns/{filename}"
    _write_page(
        vault / rel_path,
        "---\ntype: note\nstatus: active\n---\n\n# Secret\n\nthe payload text\n",
    )
    write_scope(vault)  # default paths="Notes/Patterns/**" -> matches rel_path
    write_rule(vault, ceiling=0, audience="external")

    ref = f"{context_refs.vault_ref(rel_path)}#unit-{'a' * 16}"
    packet = _ref_only_packet(ref)

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, _empty_release())

    assert guarded is not None
    assert guarded["units"] == []
    assert guarded["pointers"] == []
    assert "the payload text" not in str(guarded)


@pytest.mark.parametrize(("case", "filename"), _TRICKY_FILENAMES, ids=[c for c, _ in _TRICKY_FILENAMES])
def test_a_page_admitted_only_through_its_units_ref_is_served(
    tmp_path: Path, case: str, filename: str
) -> None:
    """No return of the over-restriction: the same five filenames, admitted."""
    vault = tmp_path / "vault"
    rel_path = f"Knowledge Base/Notes/Insights/{filename}"
    _write_page(
        vault / rel_path,
        "---\ntype: note\nstatus: active\n---\n\n# Open\n\nthe payload text\n",
    )
    write_scope(vault)  # Notes/Patterns/** -- unrelated to Notes/Insights
    write_rule(vault, ceiling=0, audience="external")

    ref = f"{context_refs.vault_ref(rel_path)}#unit-{'a' * 16}"
    packet = _ref_only_packet(ref)

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, _empty_release())

    assert guarded is not None
    assert [unit["ref"] for unit in guarded["units"]] == [ref]
    assert [pointer["ref"] for pointer in guarded["pointers"]] == [ref]


@pytest.mark.parametrize(
    "case",
    ["percent_encoded_parent_traversal", "empty_path", "unknown_exomem_authority"],
)
def test_an_unresolvable_reference_withholds_its_item_without_a_filesystem_call(
    tmp_path: Path, case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The never-drop rule, proven at the boundary that touches disk.

    `_decide_path` is the ONLY thing in this module that calls `.stat()`.
    Wrapping it records every path it is asked to decide; an un-unwrappable
    reference must never appear there — a `../` string must not reach the
    filesystem — and the item carrying it must still be withheld.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    write_scope(vault)
    write_rule(vault, ceiling=0, audience="external")

    fragment = f"#unit-{'a' * 16}"
    refs = {
        "percent_encoded_parent_traversal": (
            context_refs.vault_ref("../../etc/passwd.md") + fragment
        ),
        "empty_path": context_refs.vault_ref("") + fragment,
        "unknown_exomem_authority": f"{context_refs.SCHEME}://config/Foo.md{fragment}",
    }
    ref = refs[case]
    packet = _ref_only_packet(ref)

    decided_paths: list[str] = []
    real_decide_path = egress._decide_path

    def _recording_decide_path(vault_root, rel_path, **kwargs):
        decided_paths.append(rel_path)
        return real_decide_path(vault_root, rel_path, **kwargs)

    monkeypatch.setattr(egress, "_decide_path", _recording_decide_path)

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, _empty_release())

    assert guarded is not None
    assert guarded["units"] == []
    assert guarded["pointers"] == []
    assert decided_paths == []


def test_a_superseded_by_traversal_string_fails_closed_without_a_filesystem_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's follow-up 2: a bare `.md`-shaped list entry, unvalidated.

    `provenance.superseded_by` is a list of references reached through
    `_collect`'s generic branch, not through `_add`'s named-field path. A
    traversal string there must be treated exactly like an invalid `ref`:
    stripped from the field (the existing asymmetry for a withheld
    `superseded_by` target keeps the unit and drops the pointer, same as a
    withheld wikilink target there), and never stat'ed.
    """
    vault = tmp_path / "vault"
    open_path = "Knowledge Base/Notes/Insights/open.md"
    _write_page(
        vault / open_path, "---\ntype: note\nstatus: active\n---\n\n# Open\n\nfine\n"
    )
    write_scope(vault)
    write_rule(vault, ceiling=0, audience="external")

    packet = {
        "anchors": [],
        "roles": [],
        "units": [
            {
                "ref": f"{context_refs.vault_ref(open_path)}#unit-{'a' * 16}",
                "role": "resources",
                "text": "An open unit.",
                "lifecycle": "superseded",
                "updated": "2026-09-01",
                "provenance": {
                    "path": open_path,
                    "level": "unit",
                    "anchor": open_path,
                    "superseded_by": ["../../x.md"],
                },
            }
        ],
        "pointers": [],
        "current_state": [],
        "missing": [],
        "ambiguity": [],
        "budget": {"limit_chars": 4000, "used_chars": 40},
        "generation": {
            "freshness_key": "k",
            "index_generation": 1,
            "roles_hash": "abc",
            "roles_source": "shipped",
        },
        "abstained": False,
    }

    decided_paths: list[str] = []
    real_decide_path = egress._decide_path

    def _recording_decide_path(vault_root, rel_path, **kwargs):
        decided_paths.append(rel_path)
        return real_decide_path(vault_root, rel_path, **kwargs)

    monkeypatch.setattr(egress, "_decide_path", _recording_decide_path)

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, _empty_release())

    assert guarded is not None
    assert len(guarded["units"]) == 1
    assert "superseded_by" not in guarded["units"][0]["provenance"]
    assert "../../x.md" not in decided_paths
    assert not any(".." in decided for decided in decided_paths)


@pytest.mark.parametrize(("case", "filename"), _TRICKY_FILENAMES, ids=[c for c, _ in _TRICKY_FILENAMES])
def test_canonical_reference_agrees_for_a_uri_ref_and_its_plain_path(
    case: str, filename: str
) -> None:
    """Key parity for `_unwrap_reference`'s other callers.

    `_canonical_reference`/`_withheld_keys` are what `_names_withheld` uses to
    compare a withheld PATH against an item's own reference field. If the
    decode-order fix only worked inside `_unwrapped_vault_path`, those
    callers would still compute a different (truncated) key for the URI form
    than for the plain path of the very same page, and the withheld-set
    comparison the whole guard depends on would silently miss it.
    """
    real_path = f"Knowledge Base/Notes/Patterns/{filename}"
    uri_ref = f"{context_refs.vault_ref(real_path)}#unit-{'a' * 16}"

    assert egress._canonical_reference(uri_ref) == egress._canonical_reference(real_path)


# --------------------------------------------------------------------------- #
# Correction round 2: three more leaks, all in PLAIN-PATH fields
# (`provenance.path`/`.anchor`), plus a wrongpath collision and an
# over-restriction risk that round 1 did not have to worry about (a
# scheme'd `exomem://` ref is never ambiguous; a plain string is). The rule:
#
# R1: default is never skip -- skip a path-bearing-field string only for an
#     explicit enumerated non-page shape (`_matches_explicit_non_page_shape`
#     -- a memory-id ref, `project:<key>`, `plan:<rel>#<title>`). Every
#     other non-empty string is a candidate: decided, or `invalid`
#     (withholds its item).
# R2: one markdown predicate, `_is_markdown_path`, case-insensitive, the
#     SAME one `_decide_path` itself uses -- never restated.
# R3: interpretation SETS. A scheme'd ref has exactly one interpretation
#     (raw-split-then-decode). A plain string containing `#`/`|` is
#     ambiguous: its interpretations are the literal string AND every
#     prefix ending where a `#`/`|` immediately follows a markdown suffix.
#     Decide every interpretation that exists; the item is served only if
#     at least one exists and every existing one is admitted.
# R4: the item side (the withheld-key comparison) expands to canonical
#     keys of all of a candidate's interpretations -- a consequence of R3's
#     architecture rather than a separate mechanism.
# --------------------------------------------------------------------------- #

#: `(id, filename)`. Round 1's `_TRICKY_FILENAMES` (hash, pipe, percent,
#: space, non_ascii) repeated for the PLAIN-PATH fields defects A/B leaked
#: through, plus the shapes the reviewer's round-2 probes demonstrated
#: leaking specifically there: a case-varied `.md` suffix (`Secret.MD`,
#: defect A's naive `text.endswith(".md")`), a case-varied suffix ahead of
#: a fragment (`x.MD#y`), a `.md`-suffixed alias target (`a.md|b.md`), the
#: fully degenerate `.md#.md`, and `notes.md#draft.md` -- defect B's
#: wrongpath-collision shape, repeated here with no sibling present (the
#: dedicated collision test below adds one).
_ROUND2_LEAK_FILENAMES = [
    ("hash", "secret#page.md"),
    ("pipe", "secret|note.md"),
    ("case_md", "Secret.MD"),
    ("case_md_fragment", "x.MD#y"),
    ("percent", "100%.md"),
    ("space", "secret page.md"),
    ("non_ascii", "café.md"),
    ("pipe_md_suffix", "a.md|b.md"),
    ("degenerate", ".md#.md"),
    ("md_suffix_before_hash", "notes.md#draft.md"),
]

#: An R1 explicit-non-page shape (`_matches_project_anchor_shape`), never a
#: real page path. `_plain_field_only_packet` is used against both the
#: fixture-backed `vault` and ad-hoc `tmp_path` vaults the round-2 tests
#: build themselves; a `ref` pointing at a REAL page (`OPEN_PATH`) would
#: need that page to exist in every one of those ad-hoc vaults too, and its
#: absence there made the unit's own `ref` unresolvable -- masking the
#: field actually under test behind an unrelated drop. A skip-shape needs
#: no page to exist anywhere, so it isolates `field` cleanly in both.
_PLAIN_FIELD_UNIT_REF = "project:plain-field-fixture"


def _plain_field_only_packet(rel_path: str, field: str) -> dict:
    """A packet whose tested page is named ONLY through one unit's
    `provenance.<field>` -- `path` or `anchor`, the exact plain-path fields
    correction round 2's defects A/B leaked through. The unit's own top-
    level `ref` is an R1 skip-shape (never decided, never withheld, never
    invalid), so this isolates the field under test: only `field` carries
    the shape under test, matching the reviewer's probes
    (`release.withheld_paths` empty, page named nowhere else in the
    packet).
    """
    provenance: dict[str, Any] = {"level": "unit", field: rel_path}
    return {
        "anchors": [],
        "roles": [],
        "units": [
            {
                "ref": _PLAIN_FIELD_UNIT_REF,
                "role": "resources",
                "text": "the payload text",
                "lifecycle": "active",
                "updated": "2026-09-01",
                "provenance": provenance,
            }
        ],
        "pointers": [],
        "current_state": [],
        "missing": [],
        "ambiguity": [],
        "budget": {"limit_chars": 4000, "used_chars": 40},
        "generation": {
            "freshness_key": "k",
            "index_generation": 1,
            "roles_hash": "abc",
            "roles_source": "shipped",
        },
        "abstained": False,
    }


def _uri_ref_only_packet(rel_path: str) -> dict:
    """The URI-ref-only equivalent of `_plain_field_only_packet`, for R4's
    key-parity checks: the SAME withheld/admitted page, named only through
    the unit's own top-level `ref` in the real URI shape (round 1), never a
    plain-path field."""
    ref = f"{context_refs.vault_ref(rel_path)}#unit-{'a' * 16}"
    return _ref_only_packet(ref)


@pytest.mark.parametrize("field", ["path", "anchor", "uri_ref"])
@pytest.mark.parametrize(
    ("case", "filename"), _ROUND2_LEAK_FILENAMES, ids=[c for c, _ in _ROUND2_LEAK_FILENAMES]
)
def test_a_round_two_leak_shape_stays_withheld(
    tmp_path: Path, field: str, case: str, filename: str
) -> None:
    """Every reported leak shape, in every field it can appear in.

    A page withheld by an active policy, named ONLY through the field under
    test (`provenance.path`, `provenance.anchor`, or the unit's own URI
    `ref`), with `release.withheld_paths` empty, must stay withheld under
    every filename the reviewer's probes demonstrated leaking.
    """
    vault = tmp_path / "vault"
    rel_path = f"Knowledge Base/Notes/Patterns/{filename}"
    _write_page(
        vault / rel_path,
        "---\ntype: note\nstatus: active\n---\n\n# Secret\n\nthe payload text\n",
    )
    write_scope(vault)  # default paths="Notes/Patterns/**" -> matches rel_path
    write_rule(vault, ceiling=0, audience="external")

    packet = (
        _uri_ref_only_packet(rel_path)
        if field == "uri_ref"
        else _plain_field_only_packet(rel_path, field)
    )

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, _empty_release())

    assert guarded is not None
    assert guarded["units"] == []
    assert guarded["pointers"] == []
    assert "the payload text" not in str(guarded)


@pytest.mark.parametrize("field", ["path", "anchor", "uri_ref"])
@pytest.mark.parametrize(
    ("case", "filename"), _ROUND2_LEAK_FILENAMES, ids=[c for c, _ in _ROUND2_LEAK_FILENAMES]
)
def test_a_round_two_leak_shape_is_served_when_admitted(
    tmp_path: Path, field: str, case: str, filename: str
) -> None:
    """The over-restriction guard: the SAME shapes, on a page a policy
    scoped elsewhere admits, must still be served -- not merely not-leaked.
    """
    vault = tmp_path / "vault"
    rel_path = f"Knowledge Base/Notes/Insights/{filename}"
    _write_page(
        vault / rel_path,
        "---\ntype: note\nstatus: active\n---\n\n# Open\n\nthe payload text\n",
    )
    write_scope(vault)  # Notes/Patterns/** -- unrelated to Notes/Insights
    write_rule(vault, ceiling=0, audience="external")

    packet = (
        _uri_ref_only_packet(rel_path)
        if field == "uri_ref"
        else _plain_field_only_packet(rel_path, field)
    )

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, _empty_release())

    assert guarded is not None
    assert len(guarded["units"]) == 1
    assert "the payload text" in str(guarded)


def test_a_page_literally_named_with_a_trailing_md_marker_is_not_confused_with_its_permitted_sibling(
    tmp_path: Path,
) -> None:
    """Defect B, reproduced and closed: `notes.md#draft.md` is a REAL,
    DIFFERENT file from its sibling `notes.md` in the same folder -- and
    the withheld one. It must be decided (and withheld) as ITSELF, never
    silently resolved to the differently-permitted sibling by a
    first-`.md`-cut guess (`_strip_trailing_marker`, deleted by this
    round's R3 rewrite) -- the reviewer's `probe_v2_wrongpath_collision.py`.
    The policy is scoped to the EXACT withheld file, never the folder, so
    the sibling staying permitted (and NOT withheld as collateral) is
    directly observable.
    """
    vault = tmp_path / "vault"
    withheld_rel = "Knowledge Base/Notes/Patterns/notes.md#draft.md"
    sibling_rel = "Knowledge Base/Notes/Patterns/notes.md"
    _write_page(
        vault / withheld_rel,
        "---\ntype: note\nstatus: active\n---\n\n# Draft\n\nthe withheld draft text\n",
    )
    _write_page(
        vault / sibling_rel,
        "---\ntype: note\nstatus: active\n---\n\n# Notes\n\nthe permitted sibling text\n",
    )
    write_scope(vault, paths="Notes/Patterns/notes.md#draft.md")
    write_rule(vault, ceiling=0, audience="external")

    withheld_packet = _plain_field_only_packet(withheld_rel, "path")
    sibling_packet = _plain_field_only_packet(sibling_rel, "path")

    with request_scope(_external()):
        withheld_guarded = egress.guard_working_set(vault, withheld_packet, _empty_release())
    with request_scope(_external()):
        sibling_guarded = egress.guard_working_set(vault, sibling_packet, _empty_release())

    assert withheld_guarded is not None
    assert withheld_guarded["units"] == []
    # `guard_working_set` never reads a page's own markdown body -- it
    # decides paths and filters packet fields -- so the leak signal is the
    # packet's own `text` field, not the file's on-disk content.
    assert "the payload text" not in str(withheld_guarded)

    assert sibling_guarded is not None
    assert len(sibling_guarded["units"]) == 1
    assert "the payload text" in str(sibling_guarded)


def test_a_page_literally_named_with_a_trailing_md_marker_stays_withheld_with_no_sibling(
    tmp_path: Path,
) -> None:
    """The same withheld page, with NO `notes.md` sibling in the folder at
    all: withholding it must not depend on a same-named sibling existing to
    collide with -- the literal file itself is what must be decided, and
    its only OTHER interpretation (`notes.md`) is a phantom reading that
    exists nowhere on disk.
    """
    vault = tmp_path / "vault"
    withheld_rel = "Knowledge Base/Notes/Patterns/notes.md#draft.md"
    _write_page(
        vault / withheld_rel,
        "---\ntype: note\nstatus: active\n---\n\n# Draft\n\nthe withheld draft text\n",
    )
    write_scope(vault, paths="Notes/Patterns/notes.md#draft.md")
    write_rule(vault, ceiling=0, audience="external")

    packet = _plain_field_only_packet(withheld_rel, "path")

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, _empty_release())

    assert guarded is not None
    assert guarded["units"] == []
    assert "the payload text" not in str(guarded)


@pytest.mark.parametrize("fragment", ["#Heading", "#current"])
def test_a_legitimate_fragment_suffixed_plain_path_still_resolves(
    vault: Path, fragment: str
) -> None:
    """`path.md#Heading`/`path.md#current` are real production shapes (a
    heading-anchored path, a current-state unit's own real `ref` -- see
    `test_a_policy_scoped_to_one_page_withholds_only_that_page_end_to_end`
    and the end-to-end `#current` unit it serves). Their ONLY existing
    interpretation is the real page before the fragment. R3's ambiguity
    handling must not cost the ordinary case its own service: a policy
    scoped to an unrelated folder must still serve the unit."""
    write_scope(vault)  # Notes/Patterns/** -- unrelated to OPEN_PATH's folder
    write_rule(vault, ceiling=0, audience="external")

    packet = _plain_field_only_packet(f"{OPEN_PATH}{fragment}", "path")

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, _prose_release())

    assert guarded is not None
    assert len(guarded["units"]) == 1


def test_a_plain_path_with_zero_valid_interpretations_withholds_its_item_without_a_filesystem_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PLAIN (non-URI) traversal string is exactly as fail-closed as
    round 1's `exomem://`-wrapped shapes: `_is_safe_relative_path` rejects
    every `..` segment, so a plain `../../etc/passwd.md` has ZERO
    safety-valid interpretations and is `unresolvable` before anything is
    ever `stat()`'d -- proven at the boundary that touches disk, the same
    monkeypatch pattern round 1/2 established.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    write_scope(vault)
    write_rule(vault, ceiling=0, audience="external")

    packet = _plain_field_only_packet("../../etc/passwd.md", "path")

    decided_paths: list[str] = []
    real_decide_path = egress._decide_path

    def _recording_decide_path(vault_root, rel_path, **kwargs):
        decided_paths.append(rel_path)
        return real_decide_path(vault_root, rel_path, **kwargs)

    monkeypatch.setattr(egress, "_decide_path", _recording_decide_path)

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, _empty_release())

    assert guarded is not None
    assert guarded["units"] == []
    assert not any(".." in decided for decided in decided_paths)


@pytest.mark.parametrize(
    ("case", "text", "expect_skipped"),
    [
        ("memory_id", None, True),
        ("project_anchor", "project:harbor-survey", True),
        ("plan_anchor", "plan:Products/plan.md#Launch checklist", True),
        ("malformed_memory_id", "exomem://memory/not-a-uuid", False),
        ("project_near_miss_path", "project:Products/plan.md", False),
        ("plan_near_miss_no_fragment", "plan:Products/plan.md", False),
    ],
)
def test_r1_skip_shapes_and_their_near_misses(
    case: str, text: str | None, expect_skipped: bool
) -> None:
    """R1's enumerated non-page shapes (`_matches_explicit_non_page_shape`)
    are skipped outright -- never a candidate, so never in ANY of
    `_working_set_paths`'s last three return values. A near miss of each
    shape (a malformed memory id, `project:`/`plan:` followed by something
    that looks like a real path or lacks the required fragment) is NOT
    skipped: it falls through to ordinary candidate handling and lands in
    `interpretations` (it has a safety-valid reading) or `unresolvable` (it
    does not) -- `_working_set_paths` itself never touches the filesystem,
    so which of the two depends only on shape, not on what exists on disk.
    """
    import uuid

    from exomem import memory_refs

    ref = memory_refs.memory_ref(str(uuid.uuid4())) if text is None else text
    packet = {"units": [_unit_with_ref(ref)]}

    paths, names, interpretations, unresolvable = egress._working_set_paths(packet)

    if expect_skipped:
        assert paths == set()
        assert names == set()
        assert interpretations == {}
        assert unresolvable == set()
    else:
        assert ref in interpretations or ref in unresolvable, (
            f"{ref!r} should have fallen through to ordinary candidate "
            "handling, not been skipped"
        )


@pytest.mark.parametrize(
    "suffix",
    [
        "#",
        "|",
        "%",
        " ",
        "##",
        "#|",
        "|#",
        "%.",
        ". ",
        " #",
        "#Draft.MD",
        "|Alias.Md",
        "#a#b",
    ],
)
def test_generated_odd_filenames_decide_the_same_set_plain_and_uri(
    tmp_path: Path, suffix: str
) -> None:
    """Property check, generalising past the specific named shapes above:
    for a filename built from the reviewer's alphabet (`# | % . space`,
    mixed-case extensions), the plain-path form and the URI `ref` form of
    the SAME reference must agree on whether the real page is served --
    R4's key-parity requirement -- and it must in fact be served, since
    every one of these pages is admitted.
    """
    vault = tmp_path / "vault"
    filename = f"secret{suffix}.md"
    rel_path = f"Knowledge Base/Notes/Insights/{filename}"
    _write_page(
        vault / rel_path,
        "---\ntype: note\nstatus: active\n---\n\n# Secret\n\nthe payload text\n",
    )
    write_scope(vault)  # Notes/Patterns/** -- unrelated to Notes/Insights
    write_rule(vault, ceiling=0, audience="external")

    plain_packet = _plain_field_only_packet(rel_path, "path")
    uri_packet = _uri_ref_only_packet(rel_path)

    with request_scope(_external()):
        plain_guarded = egress.guard_working_set(vault, plain_packet, _empty_release())
    with request_scope(_external()):
        uri_guarded = egress.guard_working_set(vault, uri_packet, _empty_release())

    assert plain_guarded is not None
    assert uri_guarded is not None
    plain_served = len(plain_guarded["units"]) == 1
    uri_served = len(uri_guarded["units"]) == 1
    assert plain_served == uri_served, (plain_guarded, uri_guarded)
    assert plain_served, "the real, admitted page itself must be served"


# --------------------------------------------------------------------------- #
# Reviewer follow-up (i): can an un-canonicalisable wikilink target reach
# `unit.text`/`anchor.title`'s withheld-reference checks?
# --------------------------------------------------------------------------- #


def test_a_degenerate_wikilink_target_in_prose_neither_leaks_nor_over_restricts(
    vault: Path,
) -> None:
    """No: a wikilink target that unwraps to the empty string (`[[#Heading]]`
    alone, naming no page at all) is inert.

    `_canonical_reference` returns `None` for an empty-unwrap candidate
    (`_unwrap_reference`'s own `if not text: return "", False`), so `_hit`
    cannot match anything through it -- there is no key to compare.
    `_working_set_paths`'s own wikilink collection independently skips it
    (`if not target: continue`), so it never becomes a path candidate
    either. It is simply invisible to this guard in both directions: never
    a match for a withheld page, never a candidate to decide on its own.
    The unit it appears in is guarded normally by every one of its OTHER
    fields, and is served here because its real page (`OPEN_PATH`) is
    permitted.
    """
    write_scope(vault)
    write_rule(vault, ceiling=0)
    _indexed(vault)

    packet = _packet()
    packet["units"] = [
        {
            "ref": _unit_ref(OPEN_PATH, "degenerate-wikilink"),
            "role": "resources",
            "text": "See [[#Heading]] above for context.",
            "lifecycle": "active",
            "updated": "2026-09-01",
            "provenance": {"path": OPEN_PATH, "level": "unit", "anchor": OPEN_PATH},
        },
    ]

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, _prose_release())

    assert guarded is not None
    assert len(guarded["units"]) == 1
    assert guarded["units"][0]["text"] == "See [[#Heading]] above for context."


# --------------------------------------------------------------------------- #
# Found while investigating follow-up (i), not originally reported: an
# aliased or heading-anchored wikilink extracted from prose stopped naming
# its target once THIS round deleted `_strip_trailing_marker`.
# `_WIKILINK_ANYWHERE.findall` always returns the bracket-stripped capture,
# so it has ALWAYS fallen to `_unwrap_reference`'s `else` branch, never the
# `text.startswith("[[")`-gated one round 1 moved the alias/heading split
# into. Round 1's own tests stayed green because the `else` branch's OWN
# `_strip_trailing_marker` split on the first `|`/`#` for any text with no
# `.md` before it, which happened to recover a bare stem's alias/heading
# correctly too -- a round-1-era side effect, not its design. This round's
# R3 rewrite deleted that function with no direct replacement for a
# wikilink target, which is what actually broke it -- a round-3 regression.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "spelling_template",
    ["{stem}|Read more", "{stem}#Some Heading"],
)
def test_an_aliased_or_heading_anchored_wikilink_still_names_its_withheld_target(
    vault: Path, spelling_template: str
) -> None:
    """A withheld page named ONLY via an aliased or heading-anchored
    wikilink in an otherwise permitted unit's prose must still drop that
    unit -- covered generally by `test_every_spelling_of_a_prose_only_
    withheld_link_drops_the_unit`; isolated here under its own name because
    it is the specific regression this round's investigation found and
    fixed (`_unwrap_reference(..., is_wikilink_target=True)`), not one of
    the reviewer's originally reported defects.
    """
    write_scope(vault)
    write_rule(vault, ceiling=0)
    _indexed(vault)

    spelling = spelling_template.format(stem=_stem_of(RESTRICTED_PATH))
    packet = _prose_only_packet(spelling)

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, _prose_release())

    assert guarded is not None
    assert guarded["units"] == [], f"LEAK: {spelling!r} named a withheld page and was served"


# --------------------------------------------------------------------------- #
# Correction round 3: `anchor["neighbourhood"]` (a LIST, unreached by round 2's
# field-name-scoped `_collect`) and the item invariant that replaces per-field
# shape reasoning: an item is served only if (a) no reference it carries is
# withheld/invalid, and (b) at least one PAGE-shaped reference it carries was
# decided and admitted. `_is_page_shaped` classifies a candidate as a page
# reference at all before it can count for either half — an opaque,
# non-page-shaped `ref` (a hand-authored id like `unit-open`) is neither
# decided nor invalid, so it can never make an item's other, genuine page
# references insufficient. `_WORKING_SET_PATH_LIST_FIELDS` closes the
# `neighbourhood`/`anchor_neighbourhood` gap.
# --------------------------------------------------------------------------- #


def test_an_opaque_unit_ref_does_not_starve_a_unit_admitted_through_path_and_anchor(
    vault: Path,
) -> None:
    """The reviewer's LANDMINE, reproduced exactly (`probe_v3_opaque_ref.py`).

    `"ref": "unit-open"` is an opaque, non-URI, non-path id — the OLD/legacy
    fixture style. Before this round, `_working_set_paths` treated ANY
    non-empty path-bearing-field string as a decidable candidate
    (`_matches_explicit_non_page_shape` was the only skip); `unit-open` names
    no page, could never resolve to a real filesystem reading, and so joined
    `unresolvable` — which `guard_working_set` withholds the whole item for,
    even though the unit's own `path`/`anchor` name a perfectly permitted page
    that has nothing to do with the active policy. Item invariant (b) fixes
    this at its root: an opaque `ref` is no longer a candidate at all (neither
    decided nor invalid), so the unit is carried by `path`/`anchor` instead,
    exactly as `working_set.py::_provenance` guarantees for every real unit.
    """
    write_scope(vault)  # default paths="Notes/Patterns/**" -- unrelated to OPEN_PATH
    write_rule(vault, ceiling=0, audience="external")

    packet = {
        "anchors": [],
        "roles": [],
        "units": [
            {
                "ref": "unit-open",
                "role": "resources",
                "text": "An entirely ordinary, permitted unit with no relation to any withheld page.",
                "lifecycle": "active",
                "updated": "2026-09-01",
                "provenance": {"path": OPEN_PATH, "level": "unit", "anchor": OPEN_PATH},
            }
        ],
        "pointers": [],
        "current_state": [],
        "missing": [],
        "ambiguity": [],
        "budget": {"limit_chars": 4000, "used_chars": 40},
        "generation": {
            "freshness_key": "k",
            "index_generation": 1,
            "roles_hash": "abc",
            "roles_source": "shipped",
        },
        "abstained": False,
    }
    release = egress.AnnotatedHits(hits=[_hit(OPEN_PATH)], withheld_paths=frozenset(), active=True, blocked=False)

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, release)

    assert guarded is not None
    assert [unit["ref"] for unit in guarded["units"]] == [
        "unit-open"
    ], "OVER-RESTRICTION: an opaque ref wrongly starved a unit admitted through its own path/anchor"


def _all_strings(value: Any) -> list[str]:
    """Every string reachable anywhere inside `value` — blind to field names,
    the opposite of `_working_set_paths`'s own field-scoped `_collect`, so
    reusing it here would just restate the thing under test rather than
    independently checking it."""
    found: list[str] = []
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, Mapping):
        for item in value.values():
            found.extend(_all_strings(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_all_strings(item))
    return found


def _real_page_readings(packet: Mapping[str, Any], real_paths: set[str]) -> set[str]:
    """Every EXISTING vault page any string anywhere in `packet`'s decided
    sections names under any reading, computed independently of
    `_working_set_paths`'s own field-name scoping: it walks every string with
    no regard for which key held it, and reuses only `_interpretations_for`
    (the reading-GENERATOR, not the field-SCOPING this pin exists to check)
    to turn each one into candidate paths, keeping only readings that are
    real files on disk -- ground truth `_working_set_paths` cannot supply
    for itself."""
    sections = (
        packet.get(section)
        for section in ("anchors", "units", "pointers", "current_state", "ambiguity", "missing")
    )
    named: set[str] = set()
    for section in sections:
        for text in _all_strings(section):
            named.update(egress._interpretations_for(text) & real_paths)
    return named


def test_every_existing_page_any_field_names_is_reached_by_the_decide_loop(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Correction round 3's PIN. A REAL packet, compiled by the REAL
    compiler on a fixture vault rich enough to carry anchors of several
    kinds (hub, resource, project, collection), units with identity and
    constraint provenance and a prose wikilink, pointers, and current_state
    -- then every string anywhere in it is walked with NO field-name
    enumeration in this test's own expectation logic
    (`_all_strings`/`_real_page_readings`), and every reading that names a
    page that genuinely exists on disk must be in `_working_set_paths`'s
    flat `paths` set. This is the general property the BLOCKER violated for
    one specific field (`neighbourhood`) and the LANDMINE's `_is_page_shaped`
    filter must not violate for a different reason (a page-shaped candidate
    silently dropped rather than decided or marked unresolvable) -- a test
    that would catch ANY future field carrying an undecided page reference,
    not just this round's two.

    Empirical finding, checked directly (see `_WORKING_SET_PATH_LIST_FIELDS`'s
    own docstring): `neighbourhood`/`anchor_neighbourhood` are NOT currently
    serialized by `ResolvedAnchor.as_dict()` into any real compiled packet's
    anchor dict -- confirmed again here, by asserting the key is simply
    absent from every anchor this real compile produces. A real-compiler
    packet therefore cannot organically exercise that one gap; the dedicated,
    isolated, red-then-green test
    (`test_evidence_that_depended_on_a_withheld_neighbour_is_dropped_when_not_already_withheld`)
    and `test_neighbourhood_survives_when_grafted_onto_a_real_anchor` below
    (a real packet with that one field added back, the shape a future anchor
    COULD carry) cover it instead. This test covers every field the real
    compiler DOES emit today, and stands as the general safety net for
    whatever it emits tomorrow.
    """
    from test_latency_gate import _seed_freshness_live
    from test_working_set_index import _seed_planning, _seed_structure

    from exomem import commands, lexstore, working_set_index, working_set_runtime

    _seed_structure(vault)
    _seed_planning(vault)
    kb = vault / "Knowledge Base"
    (kb / "Products" / "Tow Bar.md").write_text(
        "---\ntype: note\nstatus: active\nupdated: 2026-09-07\n---\n\n"
        "# Tow Bar\n\n## Summary\n\nThe bar that couples the sled.\n\n"
        "## Constraints\n\nNever tow without checking [[Cargo Sled]] first.\n",
        encoding="utf-8",
    )

    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).reset()
    working_set_index.WorkingSetIndex(vault).rebuild()
    _seed_freshness_live(vault)
    lexstore.ensure_fresh(vault)

    captured: dict[str, Any] = {}
    real_guard = egress.guard_working_set

    def capturing_guard(vault_root, packet, release, **kwargs):
        captured["packet"] = packet
        return real_guard(vault_root, packet, release, **kwargs)

    monkeypatch.setattr(egress, "guard_working_set", capturing_guard)

    with request_scope(_external()):
        commands.op_activate_context(
            vault,
            turn="what are the constraints on the tow bar near the northern corridor depot",
        )

    packet = captured["packet"]
    assert packet["anchors"], "fixture produced no anchors -- pin cannot exercise the walk"
    assert packet["units"], "fixture produced no units -- pin cannot exercise the walk"
    assert not any("neighbourhood" in anchor for anchor in packet["anchors"]), (
        "neighbourhood IS now serialized into a real anchor dict -- the empirical "
        "finding this test and _WORKING_SET_PATH_LIST_FIELDS's docstring both rely "
        "on no longer holds and both need updating, not just this assertion"
    )

    real_paths = {
        str(path.relative_to(vault)).replace("\\", "/")
        for path in vault.rglob("*.md")
    }
    expected = _real_page_readings(packet, real_paths)
    decided_paths, _names, _interpretations, _unresolvable = egress._working_set_paths(packet)

    missing = expected - decided_paths
    assert not missing, (
        f"LEAK RISK: {sorted(missing)} name real vault pages somewhere in the packet "
        "but the decide loop never saw them -- an ungoverned field"
    )


def test_neighbourhood_survives_when_grafted_onto_a_real_anchor(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The BLOCKER's shape, on a REAL compiled packet rather than a
    hand-built one: since `neighbourhood` is not currently serialized (see
    the pin test above), this grafts it onto a real anchor from a real
    compile -- the shape a FUTURE anchor could legitimately carry -- and
    proves the general walker (not a field-name list) still reaches it. RED
    against `2a7835b2` for the same reason the dedicated BLOCKER test is:
    `neighbourhood` is a LIST under a key `_collect` had no branch for.
    """
    from test_latency_gate import _seed_freshness_live
    from test_working_set_index import _seed_planning, _seed_structure

    from exomem import commands, lexstore, working_set_index, working_set_runtime

    _seed_structure(vault)
    _seed_planning(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).reset()
    working_set_index.WorkingSetIndex(vault).rebuild()
    _seed_freshness_live(vault)
    lexstore.ensure_fresh(vault)

    captured: dict[str, Any] = {}
    real_guard = egress.guard_working_set

    def capturing_guard(vault_root, packet, release, **kwargs):
        captured["packet"] = packet
        return real_guard(vault_root, packet, release, **kwargs)

    monkeypatch.setattr(egress, "guard_working_set", capturing_guard)

    with request_scope(_external()):
        commands.op_activate_context(vault, turn="the cargo sled near the northern corridor")

    packet = captured["packet"]
    assert packet["anchors"], "fixture produced no anchors -- pin cannot exercise the graft"
    packet = dict(packet)
    packet["anchors"] = [dict(anchor) for anchor in packet["anchors"]]
    packet["anchors"][0]["neighbourhood"] = [RESTRICTED_PATH]

    real_paths = {
        str(path.relative_to(vault)).replace("\\", "/")
        for path in vault.rglob("*.md")
    }
    assert RESTRICTED_PATH in real_paths, "test setup error: RESTRICTED_PATH is not in the base fixture vault"
    expected = _real_page_readings(packet, real_paths)
    decided_paths, _names, _interpretations, _unresolvable = egress._working_set_paths(packet)

    assert RESTRICTED_PATH in expected, "test setup error: the graft did not name a real-shaped page"
    assert RESTRICTED_PATH in decided_paths, (
        "LEAK: a grafted `neighbourhood` entry naming a real page was never handed "
        "to the decide loop"
    )


# --------------------------------------------------------------------------- #
# Correction round 4: `_is_page_shaped` gated EVERY field the same way,
# leaving a bare or oddly-suffixed TYPED value (`secret`, `secret.markdown`)
# invisible -- not decided, not invalid -- in `provenance.path`/`.anchor`, a
# pointer's `ref`, a `current_state` entry's `anchor`. T1: `path`/`anchor`
# (wherever they appear) and list-shaped reference fields are TYPED page
# fields, never gated by shape. T2: `_is_page_shaped` gates only a `ref` on
# an item that ALSO carries a `path`/`anchor` of its own (kept for
# `unit-open`-style opaque legacy ids). T3: the item invariant's (b) half is
# satisfied only by a typed page field, never by `ref` alone on an item that
# has one. T4: an empty string is still skipped.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("case", ["bare_stem", "wrong_extension"])
def test_a_bare_or_wrong_extension_source_is_withheld_even_with_an_admitted_ref(
    vault: Path, case: str
) -> None:
    """The reviewer's items 1+2, combined and reproduced exactly: `ref` is
    admitted and page-shaped (satisfies (b) under round 3's rules), but the
    unit's REAL source -- named only through `provenance.path`/`.anchor` --
    is a bare stem (no slash, no `.md`) or a wrong-extension value (`.markdown`,
    not `.md`). Before this round `_is_page_shaped` gated `path`/`anchor`
    the same as `ref`, so neither shape ever became a candidate: not
    decided, not invalid, invisible to the guard entirely, and the unit was
    served with prose actually sourced from the withheld page.
    """
    write_scope(vault)  # default paths="Notes/Patterns/**" -> matches RESTRICTED_PATH
    write_rule(vault, ceiling=0, audience="external")

    stem = _stem_of(RESTRICTED_PATH)
    source_value = stem if case == "bare_stem" else f"{stem}.markdown"
    packet = {
        "anchors": [],
        "roles": [],
        "units": [
            {
                "ref": OPEN_PATH,
                "role": "resources",
                "text": "the payload text actually sourced from the withheld page",
                "lifecycle": "active",
                "updated": "2026-09-01",
                "provenance": {"path": source_value, "level": "unit", "anchor": source_value},
            }
        ],
        "pointers": [],
        "current_state": [],
        "missing": [],
        "ambiguity": [],
        "budget": {"limit_chars": 4000, "used_chars": 40},
        "generation": {
            "freshness_key": "k",
            "index_generation": 1,
            "roles_hash": "abc",
            "roles_source": "shipped",
        },
        "abstained": False,
    }
    release = egress.AnnotatedHits(hits=[_hit(OPEN_PATH)], withheld_paths=frozenset(), active=True, blocked=False)

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, release)

    assert guarded is not None
    assert guarded["units"] == [], (
        f"LEAK ({case}): a {source_value!r} path/anchor naming a withheld page was "
        "invisible to the guard although `ref` alone satisfied the old (b) check"
    )


@pytest.mark.parametrize("section", ["pointers", "current_state", "ambiguity", "missing"])
def test_a_bare_word_naming_a_withheld_pages_stem_withholds_its_item(
    vault: Path, section: str
) -> None:
    """The reviewer's item 4, precise: a pointer/current_state/ambiguity/
    missing entry whose ONLY reference is a bare word matching a withheld
    page's stem must be dropped -- `release.withheld_paths` is EMPTY, so
    only `_working_set_paths` treating the bare word as a typed candidate
    (T1) and deciding it against the real, policy-withheld page catches
    this. Before this round these sections were filtered against
    `withheld`/`invalid_refs`, but a bare word never populated either set
    in the first place, so the filter had nothing to match against.

    `missing` never carries a reference in any packet the real compiler
    emits (`working_set.py`'s only shape there is `{"role", "reason"}`) --
    included anyway because `guard_working_set`'s per-section loop treats
    all four identically, and this property should hold for whichever
    section a future field lands in, not just the three the compiler
    populates today.
    """
    secret_rel = "Knowledge Base/Notes/Patterns/secret.md"
    _write_page(
        vault / secret_rel,
        "---\ntype: note\nstatus: active\n---\n\nTOP SECRET\n",
    )
    write_scope(vault)  # default paths="Notes/Patterns/**" -> covers secret_rel
    write_rule(vault, ceiling=0, audience="external")

    entries = {
        "pointers": {"ref": "secret", "role": "resources", "title": "t", "why": "w", "reason": "budget"},
        "current_state": {
            "anchor": "secret",
            "source": "records",
            "as_of": "2026-09-10",
            "statement": "state: hidden",
        },
        "ambiguity": {"ref": "secret", "title": "t", "kind": "resource", "neighbourhood_size": 0},
        "missing": {"ref": "secret", "role": "resources", "reason": "budget"},
    }
    packet = {
        "anchors": [],
        "roles": [],
        "units": [],
        "pointers": [],
        "current_state": [],
        "missing": [],
        "ambiguity": [],
        "budget": {"limit_chars": 4000, "used_chars": 40},
        "generation": {
            "freshness_key": "k",
            "index_generation": 1,
            "roles_hash": "abc",
            "roles_source": "shipped",
        },
        "abstained": False,
    }
    packet[section] = [entries[section]]
    release = egress.AnnotatedHits(hits=[_hit(OPEN_PATH)], withheld_paths=frozenset(), active=True, blocked=False)

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, release)

    assert guarded is not None
    assert guarded[section] == [], (
        f"LEAK: a bare word naming a withheld page's stem survived in {section!r}"
    )


def test_the_records_lane_current_state_units_ref_never_substitutes_for_its_withheld_anchor(
    vault: Path,
) -> None:
    """The records-lane `current_state` unit shape
    (`working_set_state.py`'s own construction: `provenance.path=""`,
    `ref="<collection path>#current"`, `provenance.anchor=<collection
    path>`) must still be served when scoped elsewhere, and withheld when
    ITS OWN collection is withheld. `ref` is merely TOLERATED here (T2) --
    `anchor` is this unit's typed field (T3) -- so `ref` naming an entirely
    different, admitted page could never paper over a withheld anchor, and
    an admitted `ref`/`anchor` pair with an empty `path` must not be
    over-restricted either, now that empty-`path` items are handled
    specially for anchors too (T1's `path or anchor` fallback).
    """
    write_scope(vault)  # default paths="Notes/Patterns/**" -> matches RESTRICTED_PATH, not OPEN_PATH
    write_rule(vault, ceiling=0, audience="external")

    def _records_unit(anchor_path: str) -> dict:
        return {
            "ref": f"{anchor_path}#current",
            "role": "current_state",
            "text": "status: active",
            "lifecycle": "active",
            "updated": "2026-09-01",
            "provenance": {"path": "", "level": "page", "anchor": anchor_path, "source": "profile"},
        }

    def _packet(anchor_path: str) -> dict:
        return {
            "anchors": [],
            "roles": [],
            "units": [_records_unit(anchor_path)],
            "pointers": [],
            "current_state": [],
            "missing": [],
            "ambiguity": [],
            "budget": {"limit_chars": 4000, "used_chars": 40},
            "generation": {
                "freshness_key": "k",
                "index_generation": 1,
                "roles_hash": "abc",
                "roles_source": "shipped",
            },
            "abstained": False,
        }

    release = egress.AnnotatedHits(hits=[_hit(OPEN_PATH)], withheld_paths=frozenset(), active=True, blocked=False)

    with request_scope(_external()):
        served = egress.guard_working_set(vault, _packet(OPEN_PATH), release)
    assert served is not None
    assert len(served["units"]) == 1, "OVER-RESTRICTION: an unrelated records-lane unit was withheld"

    with request_scope(_external()):
        withheld = egress.guard_working_set(vault, _packet(RESTRICTED_PATH), release)
    assert withheld is not None
    assert withheld["units"] == [], "LEAK: the records-lane unit's own withheld anchor was not honoured"


def test_a_units_wikilink_in_any_prose_field_not_just_text_withholds_it(vault: Path) -> None:
    """Follow-up closed in the same round, same walker: `_guarded_unit`
    checked only `unit["text"]` for a withheld wikilink, while the
    collector (`_working_set_paths`) scans EVERY field in
    `_WORKING_SET_PROSE_FIELDS` (`text`, `statement`, `why`, `title`) on any
    item type. A unit carries only `text` in every packet the real compiler
    emits today, so this is a synthetic shape -- proving the checked-field
    list is now driven by the same shared constant the collector uses,
    rather than one hardcoded field name silently falling behind it the way
    `neighbourhood` did for a different field in the BLOCKER.
    """
    write_scope(vault)
    write_rule(vault, ceiling=0)
    _indexed(vault)

    spelling = f"[[{_stem_of(RESTRICTED_PATH)}]]"
    packet = {
        "anchors": [],
        "roles": [],
        "units": [
            {
                "ref": OPEN_PATH,
                "role": "resources",
                "text": "an ordinary sentence",
                "title": f"see {spelling} for background",
                "lifecycle": "active",
                "updated": "2026-09-01",
                "provenance": {"path": OPEN_PATH, "level": "unit", "anchor": OPEN_PATH},
            }
        ],
        "pointers": [],
        "current_state": [],
        "missing": [],
        "ambiguity": [],
        "budget": {"limit_chars": 4000, "used_chars": 40},
        "generation": {
            "freshness_key": "k",
            "index_generation": 1,
            "roles_hash": "abc",
            "roles_source": "shipped",
        },
        "abstained": False,
    }

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, _prose_release())

    assert guarded is not None
    assert guarded["units"] == [], "LEAK: a withheld wikilink in a unit's `title` field survived"
