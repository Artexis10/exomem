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
        freshness_key="k",
        index_generation=3,
        roles_hash="abc",
        conventions_hash="conv0",
        turn="t",
        max_chars=4000,
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


def _prose_unit(stem: str, ref: str = "unit-only-prose") -> dict:
    return {
        "ref": ref,
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
    unit = _prose_unit(_stem_of(permitted), ref="unit-permitted-link")
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

    unit = _prose_unit("no-such-page-anywhere", ref="unit-dangling-link")
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
    packet["units"] = [_prose_unit(prose, ref="unit-apostrophe-or-hyphen-link")]

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
        freshness_key="k",
        index_generation=3,
        roles_hash="abc",
        conventions_hash="conv0",
        turn="t",
        max_chars=4000,
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
    from test_working_set_index import _seed_planning, _seed_structure

    from exomem import commands, working_set_index, working_set_runtime

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

    unit = _prose_unit("still-no-such-page", ref="unit-unknown")
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
    packet["units"] = [_prose_unit("widget", ref="unit-colliding-link")]

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
    packet["units"] = [_prose_unit(RESTRICTED_TITLE, ref="unit-title-link")]

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, _prose_release())

    assert guarded is not None
    assert guarded["units"] == []
    assert RESTRICTED_TITLE.casefold() not in str(guarded).casefold()


def test_a_title_spelled_wikilink_is_caught_with_no_policy_at_all(vault: Path) -> None:
    """Already-withheld is enough: the leak did not need a policy to fire."""
    _indexed(vault)

    packet = _packet()
    packet["units"] = [_prose_unit(RESTRICTED_TITLE, ref="unit-title-link")]

    guarded = egress.guard_working_set(vault, packet, _titled_release())

    assert guarded is not None
    assert guarded["units"] == []


def test_a_title_spelled_wikilink_to_a_permitted_page_keeps_the_unit(
    vault: Path,
) -> None:
    write_scope(vault)
    write_rule(vault, ceiling=0)
    _indexed(vault)

    unit = _prose_unit("Autovacuum thresholds prevent table bloat", ref="unit-open-title")
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
        "units": [_prose_unit(spelling, ref="unit-decorated-link")],
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
