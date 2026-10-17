"""The retrieval carry's bounded work answers exactly what its unbounded work did.

Three bounds keep the carry's cost off the corpus size: a word's rarity count
stops one row past the count that can still change a decision, a corroborated
ranking is checked in rank order and stops once it has its rows, and the
owner ranks on the maintained catalogue's statistics instead of rebuilding
them from every page after each write. Each case below proves one of them
changes no answer, and that a restricted reader still ranks only on the pages
it may see. Invented, generic vocabulary throughout.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_working_set_carry import _write, seed_ordinary_notes

from exomem import (
    call_spans,
    lexstore,
    lifecycle_statuses,
    state_paths,
    working_set,
    working_set_runtime,
)

RESEARCH = "Knowledge Base/Notes/Research"


def _research_page(vault: Path, name: str, body: str, *, successor: str | None = None) -> str:
    """One active research page; `successor` retires it by `superseded_by`
    only, which the catalogue cannot see and the carry discounts page by page."""
    successor_line = f"superseded_by: [{successor}]\n" if successor else ""
    rel = f"{RESEARCH}/{name}.md"
    _write(
        vault / rel,
        f"---\ntype: research-note\nstatus: active\nupdated: 2026-09-01\n{successor_line}---\n\n"
        f"# {name.replace('-', ' ').capitalize()}\n\n## Summary\n\n- [decision] {body}\n",
    )
    return rel


def _settle(vault: Path) -> None:
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()


# --------------------------------------------------------------------------- #
# Rarity counts stop one row past the window that can change a decision.
# --------------------------------------------------------------------------- #


def _seed_revised_names(vault: Path) -> dict[str, str]:
    """Words on known numbers of current and retired pages, at and around the
    window `cap + RETRIEVAL_CARRY_FETCH` past which a count is left as counted.

    With this corpus the rarity cap is 3 and the window 13, so:
    `zarnith` is on 13 pages, 10 of them retired: inside the window, it is
    discounted to 3 and is distinctive. `quevold` is on 14 pages, 11 of them
    retired: one past the window, it stays 14 and is ordinary, though a count
    stopped one row early would have discounted it to 3.
    """
    seed_ordinary_notes(vault)
    stems = dict(zip(("zarnith", "quevold", "plinth"), working_set_runtime.content_stems(
        "zarnith quevold plinth"
    ), strict=True))
    for word, current, retired in (("zarnith", 3, 10), ("quevold", 3, 11), ("plinth", 2, 0)):
        keeper = _research_page(vault, f"{word}-ledger", f"The {word} ledger is current.")
        for index in range(current - 1):
            _research_page(vault, f"{word}-copy-{index}", f"The {word} ledger copy {index}.")
        for index in range(retired):
            _research_page(
                vault, f"{word}-revision-{index}", f"The {word} ledger revision {index}.",
                successor=keeper,
            )
    _settle(vault)
    return stems


def _carry_statistics(vault: Path, stems: list[str], path_limit) -> tuple:
    result = lexstore.carry_term_statistics(
        vault,
        stems,
        visible=lambda _path: True,
        status_basis=lifecycle_statuses.Basis(vault),
        path_limit=path_limit,
    )
    assert result.readiness.complete, result.readiness
    return result.value


def _rare(vault: Path, stems: list[str], frequencies, paths, cap: int) -> set[str]:
    discounted = working_set.discount_superseded_pages(
        vault,
        frequencies,
        lambda near, limit: {stem: paths[stem][:limit] for stem in near},
        cap=cap,
    )
    return {stem for stem in stems if discounted[stem] <= cap}


def test_a_bounded_rarity_count_decides_exactly_as_the_unbounded_count(vault: Path) -> None:
    named = _seed_revised_names(vault)
    common = working_set_runtime.content_stems("ordinary week")[0]
    stems = [*named.values(), common]

    window = lambda pages: (  # noqa: E731 - the carry's own window
        working_set.rare_document_cap(pages) + working_set.RETRIEVAL_CARRY_FETCH
    )
    bounded, pages, bounded_paths, _admitted = _carry_statistics(vault, stems, window)
    exact, exact_pages, exact_paths, _admitted = _carry_statistics(
        vault, stems, lambda _pages: 10**9
    )
    cap = working_set.rare_document_cap(pages)
    limit = window(pages)
    assert (pages, cap, limit) == (exact_pages, 3, 13)
    assert exact[named["zarnith"]] == limit and exact[named["quevold"]] == limit + 1

    # A word on far more pages than the window, which a bounded count stops on.
    assert exact[common] > limit + 1
    # The pages listed for the discount are the same wherever it reads them.
    for stem in stems:
        if exact[stem] <= limit:
            assert bounded_paths[stem] == exact_paths[stem], stem

    rare = _rare(vault, stems, bounded, bounded_paths, cap)
    assert rare == _rare(vault, stems, exact, exact_paths, cap)
    assert named["zarnith"] in rare and named["plinth"] in rare
    assert named["quevold"] not in rare and common not in rare

    distinctive, corpus_pages, state = working_set_runtime.rare_turn_terms(vault, stems)
    assert state == "available" and corpus_pages == pages
    assert set(distinctive) == rare


# --------------------------------------------------------------------------- #
# A corroborated ranking stops once it has its rows, in rank order.
# --------------------------------------------------------------------------- #

#: `zorvane` is on one page alone, so that page ranks first on one word and
#: fails a two-word test. The other two words share many pages, alone or
#: together, so passing and failing rows interleave below it.
_RANKED_QUERY = "zorvane plimsett quaddle"
_RANKED_PAGES = {
    "rare-single": "zorvane zorvane zorvane",
    **{f"plimsett-{index}": f"plimsett note {index}" for index in range(12)},
    **{f"quaddle-{index}": f"quaddle note {index}" for index in range(12)},
    "pair-short": "plimsett quaddle",
    "pair-long": "plimsett quaddle and a longer line of ordinary words besides",
    "pair-repeated": "plimsett plimsett quaddle quaddle",
}


def _seed_ranked(vault: Path) -> dict[str, frozenset[str]]:
    seed_ordinary_notes(vault)
    held: dict[str, frozenset[str]] = {}
    for name, words in _RANKED_PAGES.items():
        held[_research_page(vault, f"ranked-{name}", words)] = frozenset(words.split())
    _settle(vault)
    return held


def _ranked(vault: Path, k: int, **kwargs) -> list[tuple[str, float]]:
    result = lexstore.search_bm25_result(
        vault, _RANKED_QUERY, k, scope="kb", allow_delta=False, **kwargs
    )
    assert result.readiness.complete, result.readiness
    return list(result.value or ())


def test_a_corroborated_ranking_returns_the_first_k_passing_rows_of_the_whole_ranking(
    vault: Path,
) -> None:
    held = _seed_ranked(vault)
    whole = _ranked(vault, 1000)
    query = set(_RANKED_QUERY.split())
    two_units = [row for row in whole if len(query & held.get(row[0], frozenset())) >= 2]
    # A one-word page ranks above every passing one, so filtering after a
    # LIMIT would lose rows: the case discriminates.
    assert len(query & held[whole[0][0]]) == 1, whole[:3]
    assert len(two_units) == 3

    for k in range(1, len(two_units) + 2):
        assert _ranked(
            vault, k, min_matched_terms=2,
            term_budget=working_set_runtime.lexical_term_budget(),
        ) == two_units[:k], k


def test_a_phrase_ranking_returns_the_first_k_rows_holding_the_phrase(vault: Path) -> None:
    held = _seed_ranked(vault)
    phrase = working_set_runtime.content_stems("plimsett quaddle")
    whole = _ranked(vault, 1000)
    holding = [row for row in whole if {"plimsett", "quaddle"} <= held.get(row[0], frozenset())]
    assert len(holding) == 3 and whole[0] not in holding

    for k in range(1, len(holding) + 2):
        assert _ranked(vault, k, corroboration_groups=[list(phrase)]) == holding[:k], k


# --------------------------------------------------------------------------- #
# The owner ranks on the catalogue; a restricted reader on what it may see.
# --------------------------------------------------------------------------- #

PHRASE_TURN = "what did the quorvel tansid survey decide"
TITLE_TURN = "where is the lantern tide schedule kept"
PHRASE_PAGE = f"{RESEARCH}/quorvel-tansid-survey.md"
TITLE_PAGE = f"{RESEARCH}/lantern-tide-schedule.md"
#: The folder a rule withholds from the external reader: an ordinary one, or
#: raw material, which the carry never ranks but the catalogue still counts.
PRIVATE, SOURCES = "Notes/Private", "Sources"


def _withheld(folder: str) -> str:
    return f"Knowledge Base/{folder}/quorvel-tide-notes.md"


def _quiet_withheld(folder: str) -> str:
    """Withheld too, and holds none of the turns' words. It stays in both
    vaults of a twin, so the reader's admission withholds a page in both and
    the twin compares one ranking with itself."""
    return f"Knowledge Base/{folder}/archive-ledger.md"


def _seed_withheld_twin(vault: Path, folder: str = PRIVATE) -> None:
    from test_governance_egress import _reset_caches, write_rule, write_scope

    seed_ordinary_notes(vault)
    _research_page(vault, "quorvel-tansid-survey", "The quorvel tansid survey closed early.")
    _research_page(vault, "lantern-tide-schedule", "The lantern tide schedule moved to the pier.")
    # Holds every word both turns rank on, repeated, so counting it would move
    # every statistic a score is made of.
    _write(
        vault / _withheld(folder),
        "---\ntype: note\nstatus: active\n---\n\n# Quorvel tide notes\n\n"
        + "quorvel tansid survey lantern tide schedule decide kept. " * 6 + "\n",
    )
    _write(
        vault / _quiet_withheld(folder),
        "---\ntype: note\nstatus: active\n---\n\n# Archive ledger\n\nOld invoices, filed.\n",
    )
    write_scope(vault, paths=f"{folder}/**", name="Private")
    write_rule(vault, ceiling=0)
    _settle(vault)
    _reset_caches()


def _restricted_carry(vault: Path, folder: str) -> tuple:
    from exomem.governance import egress
    from exomem.governance.principal import RequestPrincipal, request_scope

    reader = RequestPrincipal(audience_id="external", surface="mcp")
    with request_scope(reader), state_paths.resolution_scope():
        basis = lifecycle_statuses.Basis(vault)
        visible = egress.reader_view(vault)
        # A path naming no file withholds nothing, so the view is checked
        # only while the page exists.
        assert visible is not None
        assert not (vault / _withheld(folder)).exists() or not visible(_withheld(folder))
        named = working_set_runtime.carry_named_groups(
            vault, PHRASE_TURN, visible=visible, status_basis=basis
        )
        listed = working_set_runtime.title_named_pages(
            vault, TITLE_TURN, visible=visible, status_basis=basis
        )
    return named, listed


@pytest.mark.parametrize("folder", [PRIVATE, SOURCES])
def test_a_restricted_readers_carry_ranks_as_if_the_withheld_page_were_absent(
    vault: Path, folder: str
) -> None:
    """One reader, one path, asked twice: with the withheld page in the vault
    and with it deleted. Ranked on the catalogue's statistics, the page would
    move both rungs' scores and could take a row; ranked on the reader's
    admitted pages, the answers are identical to the last digit. A second
    withheld page stays in both vaults, so both rank on admitted pages. Raw
    material is never a carry row, but the catalogue's statistics count it."""
    from test_governance_egress import _reset_caches

    _seed_withheld_twin(vault, folder)
    present = _restricted_carry(vault, folder)
    (vault / _withheld(folder)).unlink()
    _settle(vault)
    _reset_caches()
    absent = _restricted_carry(vault, folder)

    (groups, groups_state), (listed, listed_state) = present
    assert groups_state == listed_state == "available"
    assert [path for group in groups for path, _score in group] == [PHRASE_PAGE]
    assert [path for path, _score in listed] == [TITLE_PAGE]
    assert present == absent


def test_after_a_write_only_a_reader_with_withheld_pages_rebuilds_ranking_statistics(
    vault: Path,
) -> None:
    """Each catalogue write gives the catalogue a new revision. An owner whose
    admission withholds nothing ranks on the catalogue's own statistics and
    never rebuilds any; a reader with withheld pages rebuilds its statistics
    over the pages it admits once per revision."""
    from exomem.governance import egress
    from exomem.governance.principal import RequestPrincipal, owner_principal, request_scope

    owner = owner_principal()
    reader = RequestPrincipal(audience_id="external", surface="mcp")

    def rebuilt(principal) -> int:
        with (
            call_spans.meter(call_spans.STATS_REBUILT) as measured,
            request_scope(principal),
            state_paths.resolution_scope(),
        ):
            visible = egress.reader_view(vault)
            assert (visible is None) == (principal is owner)
            groups, state = working_set_runtime.carry_named_groups(
                vault, PHRASE_TURN, visible=visible
            )
        assert state == "available" and groups, (state, groups)
        return int(measured[call_spans.STATS_REBUILT])

    _seed_withheld_twin(vault)
    rebuilt(owner), rebuilt(reader)
    _research_page(vault, "harbour-note", "A harbour note written after the carry warmed.")
    _settle(vault)

    assert rebuilt(owner) == 0
    assert rebuilt(reader) == 1
    assert rebuilt(reader) == 0, "a restricted reader rebuilds once per revision, not per turn"


# --------------------------------------------------------------------------- #
# The owner's own admission can withhold pages too.
# --------------------------------------------------------------------------- #

OWNER_WITHHELD = "Knowledge Base/Notes/Private/quorvel-tansid-private.md"


def _owner_packet(vault: Path) -> dict:
    """The owner's packet for `PHRASE_TURN`, without the fields that name a
    generation or a moment."""
    import json

    from test_governance_egress import _reset_caches

    from exomem import commands, working_set_index
    from exomem.governance.principal import owner_principal, request_scope

    _settle(vault)
    working_set_index.WorkingSetIndex(vault).rebuild()
    _reset_caches()
    working_set_runtime.reset_caches_for_tests()
    with request_scope(owner_principal()):
        packet = commands.op_activate_context(vault, turn=PHRASE_TURN)
    packet = json.loads(json.dumps(packet, default=str))
    packet.pop("continuity", None)
    for key in ("freshness_key", "index_generation"):
        packet.get("generation", {}).pop(key, None)
    return packet


@pytest.mark.parametrize("rival", [True, False], ids=["beside-a-visible-page", "alone"])
def test_an_owner_rule_withholds_a_page_from_the_owners_carry(vault: Path, rival: bool) -> None:
    """An owner-audience rule withholds a page from the owner's own reads. The
    owner's packet must be the one the same vault gives without that page:
    beside a visible page that holds the phrase, the withheld one may not make
    the turn ambiguous; alone, it may not leave a carried trace."""
    from test_governance_egress import write_rule, write_scope

    seed_ordinary_notes(vault)
    if rival:
        _research_page(vault, "quorvel-tansid-survey", "The quorvel tansid survey closed early.")
    _write(
        vault / OWNER_WITHHELD,
        "---\ntype: research-note\nstatus: active\n---\n\n# Quorvel tansid private\n\n"
        "## Summary\n\n- [decision] The quorvel tansid survey decided the private terms.\n",
    )
    write_scope(vault, paths="Notes/Private/**", name="Private")
    write_rule(vault, ceiling=0, audience="owner")
    present = _owner_packet(vault)
    (vault / OWNER_WITHHELD).unlink()
    absent = _owner_packet(vault)

    assert present == absent
