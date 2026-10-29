"""The retrieval carry's bounded work answers exactly what its unbounded work did.

Three bounds keep the carry's cost off the corpus size: a word's rarity count
stops one row past the count that can still change a decision, a corroborated
ranking is checked in rank order and stops once it has its rows, and an owner
whose configuration can withhold nothing ranks on the maintained catalogue's
statistics instead of rebuilding them from every page after each write. Each
case below proves one of them changes no answer, and that any admission that
can withhold a page ranks only on the pages it admits, whether one is
withheld or none. A fourth bound, the request deadline, ends a query that is already
running. Invented, generic vocabulary throughout.
"""

from __future__ import annotations

import sqlite3
import time
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
# Configuration, never the pages, decides which statistics a ranking uses.
# --------------------------------------------------------------------------- #

PHRASE_TURN = "what did the quorvel tansid survey decide"
TITLE_TURN = "where is the lantern tide schedule kept"
PHRASE_PAGE = f"{RESEARCH}/quorvel-tansid-survey.md"
TITLE_PAGE = f"{RESEARCH}/lantern-tide-schedule.md"
#: The folder that holds the withheld page: an ordinary one, or raw material,
#: which the carry never ranks but the catalogue still counts.
PRIVATE, SOURCES = "Notes/Private", "Sources"
#: Every word both turns rank on, repeated, so counting the page would move
#: every statistic a score is made of.
TURN_WORDS = "quorvel tansid survey lantern tide schedule decide kept. " * 6


def _withheld(folder: str) -> str:
    return f"Knowledge Base/{_kb_withheld(folder)}"


def _kb_withheld(folder: str) -> str:
    return f"{folder}/quorvel-tide-notes.md"


def _seed_turn_pages(vault: Path) -> None:
    seed_ordinary_notes(vault)
    _research_page(vault, "quorvel-tansid-survey", "The quorvel tansid survey closed early.")
    _research_page(vault, "lantern-tide-schedule", "The lantern tide schedule moved to the pier.")


def _seed_withheld_twin(vault: Path, folder: str = PRIVATE, *, audience: str = "external") -> None:
    """The turn pages, and one page in `folder` that a rule for `audience`
    withholds. The scope names that page alone, so it is the vault's only
    withheld page: the fixture's own raw material stays visible."""
    from test_governance_egress import _reset_caches, write_rule, write_scope

    _seed_turn_pages(vault)
    _write(
        vault / _withheld(folder),
        f"---\ntype: note\nstatus: active\n---\n\n# Quorvel tide notes\n\n{TURN_WORDS}\n",
    )
    write_scope(vault, paths=_kb_withheld(folder), name="Private")
    write_rule(vault, ceiling=0, audience=audience)
    _settle(vault)
    _reset_caches()


def _carry(vault: Path, principal) -> tuple:
    """Both rungs' answers, scores included, for `principal`: the reader's
    own view when one exists, else whatever the owner's walk admits."""
    from exomem.governance import egress
    from exomem.governance.principal import request_scope

    with request_scope(principal), state_paths.resolution_scope():
        basis = lifecycle_statuses.Basis(vault)
        visible = egress.reader_view(vault)
        named = working_set_runtime.carry_named_groups(
            vault, PHRASE_TURN, visible=visible, status_basis=basis
        )
        listed = working_set_runtime.title_named_pages(
            vault, TITLE_TURN, visible=visible, status_basis=basis
        )
    return named, listed


def _withholds(vault: Path, principal, path: str) -> bool:
    """Whether `principal`'s admission withholds `path`, asked while it exists:
    a path naming no file withholds nothing."""
    from exomem.governance import egress
    from exomem.governance.principal import request_scope

    assert (vault / path).exists()
    with request_scope(principal), state_paths.resolution_scope():
        visible = egress.reader_view(vault)
        keep = visible if visible is not None else egress.release_walk_filter(vault)
        return keep is not None and not keep(path)


def _delete(vault: Path, path: str) -> None:
    from test_governance_egress import _reset_caches

    (vault / path).unlink()
    _settle(vault)
    _reset_caches()


def _assert_turn_pages_carried(carry: tuple) -> None:
    (groups, groups_state), (listed, listed_state) = carry
    assert groups_state == listed_state == "available"
    assert [path for group in groups for path, _score in group] == [PHRASE_PAGE]
    assert [path for path, _score in listed] == [TITLE_PAGE]


@pytest.mark.parametrize("folder", [PRIVATE, SOURCES])
def test_a_restricted_readers_carry_ranks_as_if_the_withheld_page_were_absent(
    vault: Path, folder: str
) -> None:
    """One reader, one path, asked twice: with the vault's only withheld page
    present, and deleted, so its admission withholds one page and then none.
    A reader always ranks on the pages it admits, so both answers are
    identical to the last digit. Ranked on the catalogue's statistics once
    nothing is withheld, the second would differ. Raw material is never a
    carry row, but the catalogue's statistics count it."""
    from exomem.governance.principal import RequestPrincipal

    reader = RequestPrincipal(audience_id="external", surface="mcp")
    _seed_withheld_twin(vault, folder)
    assert _withholds(vault, reader, _withheld(folder))
    present = _carry(vault, reader)
    _delete(vault, _withheld(folder))
    absent = _carry(vault, reader)

    _assert_turn_pages_carried(present)
    assert present == absent


#: A page the owner's walk withholds under a governance policy that holds no
#: rule for the owner: frontmatter shaped like a bridge, with no approval.
UNAPPROVED_BRIDGE = "Knowledge Base/Notes/quorvel-tide-bridge.md"


def _seed_unapproved_bridge(vault: Path) -> None:
    from test_governance_egress import _reset_caches, write_rule, write_scope

    _seed_turn_pages(vault)
    _write(
        vault / UNAPPROVED_BRIDGE,
        "---\ntype: note\nstatus: active\nbridge_scope: tide\n---\n\n"
        f"# Quorvel tide bridge\n\n{TURN_WORDS}\n",
    )
    # A policy for sharing with another audience only.
    write_scope(vault, paths="Notes/Shared/**", name="Shared")
    write_rule(vault, ceiling=0)
    _settle(vault)
    _reset_caches()


@pytest.mark.parametrize("source", ["owner-rule", "unapproved-bridge"])
def test_the_owners_carry_ranks_as_if_a_page_its_walk_withholds_were_absent(
    vault: Path, source: str
) -> None:
    """The owner's walk withholds a page by an owner-audience rule, or, under
    a policy with no owner rule, by the page's own content. Either way a
    governance policy is configured, so the owner ranks on the pages it
    admits whether that page is present or deleted, and both answers are
    identical to the last digit."""
    from exomem.governance.principal import owner_principal

    owner = owner_principal()
    if source == "owner-rule":
        _seed_withheld_twin(vault, audience="owner")
        withheld = _withheld(PRIVATE)
    else:
        _seed_unapproved_bridge(vault)
        withheld = UNAPPROVED_BRIDGE
    assert _withholds(vault, owner, withheld)
    present = _carry(vault, owner)
    _delete(vault, withheld)
    absent = _carry(vault, owner)

    _assert_turn_pages_carried(present)
    assert present == absent


#: The operator's access policy shape: one folder kept out of the index, one
#: kept from writes. Neither is a governance policy.
EXCLUDED, READONLY = "Notes/Private", "Notes/Curated"


def _seed_access_policy(vault: Path) -> None:
    from test_governance_egress import _reset_caches

    _seed_turn_pages(vault)
    _write(
        vault / _withheld(EXCLUDED),
        f"---\ntype: note\nstatus: active\n---\n\n# Quorvel tide notes\n\n{TURN_WORDS}\n",
    )
    _write(
        vault / "Knowledge Base/_access.yaml",
        f"excluded:\n  - {EXCLUDED}\nreadonly:\n  - {READONLY}\n",
    )
    _settle(vault)
    _reset_caches()


def test_an_excluded_page_leaves_the_owners_carry_unmoved(vault: Path) -> None:
    """An excluded page never enters the catalogue, so the owner's walk has
    nothing to withhold and the owner ranks on the catalogue's statistics
    with the page present or deleted."""
    from exomem.governance.principal import owner_principal

    owner = owner_principal()
    _seed_access_policy(vault)
    assert not _withholds(vault, owner, _withheld(EXCLUDED))
    present = _carry(vault, owner)
    _delete(vault, _withheld(EXCLUDED))
    absent = _carry(vault, owner)

    _assert_turn_pages_carried(present)
    assert present == absent


def _statistics_rebuilt(vault: Path, principal) -> int:
    from exomem.governance import egress
    from exomem.governance.principal import request_scope

    with (
        call_spans.meter(call_spans.STATS_REBUILT) as measured,
        request_scope(principal),
        state_paths.resolution_scope(),
    ):
        groups, state = working_set_runtime.carry_named_groups(
            vault, PHRASE_TURN, visible=egress.reader_view(vault)
        )
    assert state == "available" and groups, (state, groups)
    return int(measured[call_spans.STATS_REBUILT])


def test_after_a_write_an_owner_whose_configuration_cannot_withhold_rebuilds_nothing(
    vault: Path,
) -> None:
    """Each catalogue write gives the catalogue a new revision. With no
    governance policy, only an access policy, the owner ranks on the
    catalogue's own statistics and rebuilds none."""
    from exomem.governance.principal import owner_principal

    owner = owner_principal()
    _seed_access_policy(vault)
    _statistics_rebuilt(vault, owner)
    _research_page(vault, "harbour-note", "A harbour note written after the carry warmed.")
    _settle(vault)

    assert _statistics_rebuilt(vault, owner) == 0


def test_a_reader_rebuilds_statistics_only_when_its_admitted_inputs_change(vault: Path) -> None:
    from exomem.governance.principal import RequestPrincipal

    reader = RequestPrincipal(audience_id="external", surface="mcp")
    _seed_withheld_twin(vault)
    assert _statistics_rebuilt(vault, reader) == 1
    before = _carry(vault, reader)
    _write(
        vault / _withheld(PRIVATE),
        "---\ntype: note\nstatus: active\n---\n\n# Withheld revision\n\n" + TURN_WORDS * 2,
    )
    _settle(vault)

    assert _statistics_rebuilt(vault, reader) == 0
    assert _carry(vault, reader) == before

    _research_page(vault, "harbour-note", "A harbour note written after the carry warmed.")
    _settle(vault)

    assert _statistics_rebuilt(vault, reader) == 1
    assert _statistics_rebuilt(vault, reader) == 0, "once per changed input, not per turn"


def test_incremental_statistics_equal_a_full_rebuild_after_an_add_an_edit_and_a_delete() -> None:
    """The figures built from the previous corpus's pages equal `CorpusStatistics.of` over the new corpus: the same
    counts, the same first-seen order of words (the idf floor sums in it) and the same page lengths."""
    from exomem import bm25

    texts = {
        "a.md": "alpha beta gamma beta",
        "b.md": "beta delta delta epsilon",
        "c.md": "gamma zeta eta zeta",
        "d.md": "theta alpha iota",
    }

    def digests(rows: dict[str, str]) -> dict[str, bytes]:
        return {path: lexstore._text_digest(text) for path, text in rows.items()}

    def snapshot(rows: dict[str, str], base):
        return lexstore._admitted_corpus(rows, digests(rows), base)

    _statistics, _postings, base = snapshot(texts, None)
    later = {
        **texts,
        "a.md": "kappa alpha beta",  # an edit earlier in path order: `kappa` is now the first word seen
        "e.md": "beta lambda",  # an add
    }
    del later["c.md"]  # a delete: `zeta` and `eta` leave the vocabulary
    statistics, postings, retained = snapshot(later, base)
    oracle = bm25.CorpusStatistics.of({path: text.split() for path, text in later.items()})

    assert statistics.pages == oracle.pages and statistics.average_length == oracle.average_length
    assert list(statistics.document_frequency.items()) == list(oracle.document_frequency.items())
    assert "zeta" not in statistics.document_frequency
    assert statistics.idf_floor == oracle.idf_floor
    assert dict(postings.lengths) == {path: len(text.split()) for path, text in later.items()}
    assert retained is not None and set(retained.pages) == set(later)
    # A page the later corpus shares with the base is the base's entry, not read again.
    assert retained.pages["b.md"] is base.pages["b.md"]
    assert retained.pages["a.md"] is not base.pages["a.md"]


def test_an_admitted_ranking_after_a_write_equals_the_ranking_of_a_fresh_process(
    vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A page added, one edited and one deleted between two rankings of the same admitted view: the second
    tokenizes the two pages whose text is new, and ranks exactly as a process that counts every page."""
    held = _seed_ranked(vault)
    hidden = next(path for path in held if path.endswith("ranked-pair-repeated.md"))

    def ranked() -> tuple[list[tuple[str, float]], int]:
        context = lexstore.RequestQueryContext(
            vault, lambda path: path != hidden, lifecycle_statuses.Basis(vault)
        )
        with call_spans.meter(call_spans.STATS_REBUILT) as measured:
            result = _ranked(vault, 1000, query_context=context)
        return result, int(measured[call_spans.STATS_REBUILT])

    assert ranked()[1] == 1
    _research_page(vault, "ranked-added", "plimsett zorvane arrives late")
    _research_page(vault, "ranked-plimsett-3", "plimsett quaddle revised at length")
    (vault / f"{RESEARCH}/ranked-quaddle-5.md").unlink()
    _settle(vault)
    tokenized: list[str] = []
    page_terms = lexstore._page_terms
    monkeypatch.setattr(lexstore, "_page_terms", lambda text: tokenized.append(text) or page_terms(text))

    warm, rebuilt = ranked()
    assert rebuilt == 1 and len(tokenized) == 2
    lexstore.clear_stores()
    assert ranked()[0] == warm


def test_snapshot_aliases_do_not_evict_another_readers_unchanged_statistics(vault: Path) -> None:
    held = _seed_ranked(vault)
    common = f"{RESEARCH}/ranked-plimsett-0.md"
    others = [f"{RESEARCH}/ranked-plimsett-{index}.md" for index in range(1, 4)]
    views = [{common, *others[:index]} for index in range(4)]

    def ranking(hidden: set[str]) -> tuple[list[tuple[str, float]], int]:
        context = lexstore.RequestQueryContext(
            vault, lambda path: path not in hidden, lifecycle_statuses.Basis(vault)
        )
        with call_spans.meter(call_spans.STATS_REBUILT) as measured:
            result = _ranked(vault, 1000, query_context=context)
        assert result and all(path in held and path not in hidden for path, _score in result)
        return result, int(measured[call_spans.STATS_REBUILT])

    first, rebuilt = ranking(views[0])
    assert rebuilt == 1
    _research_page(vault, "ranked-plimsett-0", "A revision hidden from all four readers.")
    _settle(vault)
    assert ranking(views[0]) == (first, 0)
    for view in views[1:]:
        _result, rebuilt = ranking(view)
        assert rebuilt == 1

    _research_page(vault, "ranked-plimsett-0", "Another revision hidden from all readers.")
    _settle(vault)
    _result, rebuilt = ranking(views[1])
    assert rebuilt == 0
    assert ranking(views[0]) == (first, 0), "snapshot aliases must not crowd out a reader"


def test_an_fts_only_correction_changes_warm_admitted_ranking(vault: Path) -> None:
    """A SQL repair can change scoring without a canonical checkpoint change."""
    held = _seed_ranked(vault)
    withheld = next(path for path in held if path.endswith("ranked-pair-repeated.md"))

    def ranked() -> list[tuple[str, float]]:
        context = lexstore.RequestQueryContext(
            vault, lambda path: path != withheld, lifecycle_statuses.Basis(vault)
        )
        return _ranked(vault, 1000, query_context=context)

    before = ranked()
    store = lexstore.get_store(vault)
    connection = store._connect()
    try:
        checkpoint = connection.execute(
            "SELECT key, value FROM meta WHERE key LIKE 'recall_checkpoint:%' ORDER BY key"
        ).fetchall()
        with connection:
            rowid = connection.execute(
                "SELECT rowid FROM pages WHERE path = ?",
                ("Knowledge Base/Notes/Journal/ordinary-note-00000.md",),
            ).fetchone()
            assert rowid is not None
            connection.execute(
                "UPDATE fts SET stemmed = ? WHERE rowid = ?", ("harbour " * 200, rowid[0])
            )
        assert connection.execute(
            "SELECT key, value FROM meta WHERE key LIKE 'recall_checkpoint:%' ORDER BY key"
        ).fetchall() == checkpoint
    finally:
        connection.close()

    after = ranked()
    assert after != before, "a noncandidate's tokens change the admitted score denominator"
    lexstore.clear_stores()
    assert ranked() == after, "warm repair scores must equal a fresh process cache"


@pytest.mark.parametrize("partition", ["candidate", "outside"])
def test_a_withheld_row_under_catalogue_ranking_ranks_on_admission(
    vault: Path, caplog: pytest.LogCaptureFixture, partition: str,
) -> None:
    """A context that claims catalogue ranking while its predicate withholds
    a row breaks the contract. The snapshot's check catches it: the request
    ranks on what it admits and the log names the closed reason."""
    import logging

    held = _seed_ranked(vault)
    withheld = next(path for path in held if path.endswith("ranked-pair-repeated.md"))
    if partition == "outside":
        withheld = "Knowledge Base/Sources/ranking-denominator.md"
        _write(vault / withheld, "# Ranking denominator\n\nplimsett quaddle " * 20)
        _settle(vault)
    basis = lifecycle_statuses.Basis(vault)
    denied: set[str] = set()

    def visible(path: str) -> bool:
        return path not in denied

    def ranked(catalogue_ranking: bool) -> list[tuple[str, float]]:
        context = lexstore.RequestQueryContext(
            vault, visible, basis, catalogue_ranking=catalogue_ranking
        )
        return _ranked(vault, 1000, query_context=context)

    # Warm the structural rows, then change admission without changing those rows.
    ranked(True)
    denied.add(withheld)
    with caplog.at_level(logging.WARNING, logger="exomem.lexstore"):
        claimed = ranked(True)
    assert lexstore.CATALOGUE_RANKING_CONTRACT_VIOLATION in caplog.text
    assert withheld not in [path for path, _score in claimed]
    assert claimed == ranked(False)


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


def test_a_catalogue_query_in_a_carry_scope_stops_when_the_request_budget_is_spent() -> None:
    """A running query ends within milliseconds of the deadline, with no value and a `deadline` status, instead
    of scanning on after the client has gone. Outside the scope a spent budget changes nothing, and a budget
    with time left stops nothing inside it."""
    from exomem import request_budget

    scan = (
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 30000000) SELECT count(*) FROM n"
    )
    readiness = lexstore.CatalogReadiness("available", True, "python")
    conn = sqlite3.connect(":memory:")

    def run(statement: str):
        return lexstore.LexicalStore._query_within_budget(conn, lambda c: c.execute(statement).fetchone(), readiness)

    token = request_budget.set_current(request_budget.RequestBudget(seconds=0.3))
    try:
        started = time.monotonic()
        with lexstore.stopping_at_budget() as stop:
            stopped = run(scan)
        elapsed = time.monotonic() - started
        assert stopped.value is None and stopped.readiness.status == lexstore.DEADLINE_STATUS and stop.hit
        assert elapsed < 2.0, "a scan of seconds must be cut at the 0.3 s budget"
        assert run("SELECT 1").value == (1,), "outside the scope a spent budget stops nothing"
    finally:
        request_budget.reset_current(token)
    token = request_budget.set_current(request_budget.RequestBudget(seconds=60))
    try:
        with lexstore.stopping_at_budget() as stop:
            assert run(scan.replace("30000000", "300000")).value == (300000,)
        assert not stop.hit
    finally:
        request_budget.reset_current(token)
