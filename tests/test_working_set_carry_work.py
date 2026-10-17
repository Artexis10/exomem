"""The carry's phrase query costs what its phrases match, not what its words match.

A long voice turn names one thing in a sea of everyday words. The carry then
asks for the pages whose title holds a phrase of the turn: a distinctive word
beside its neighbours. That query ranked every page holding ANY word of the
turn, which on such a turn is nearly every page, and ran its all-words-of-a-
phrase test on each of them once per phrase. A live turn spent seconds there.
The index is now asked for the pages that hold a phrase's words, and the test
runs on those alone; the pages returned and their scores are unchanged.

Work is the number of candidate rows the carry's corroboration test examines:
no clock in it, and no filler page holds a phrase, so four times the filler
must not multiply it. Invented, generic vocabulary throughout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import call_spans, lexstore, working_set_runtime

#: Everyday words: every filler page holds all of them, so none is distinctive.
EVERYDAY = tuple(
    """
    garden window market river table paper school letter summer winter
    morning evening kitchen station bridge corner ticket travel weather
    coffee bottle basket pencil blanket carpet ladder mirror pillow rocket
    circle square number record signal method machine engine vessel harbor
    island valley forest desert meadow stream canyon summit border village
    review castle temple tower palace cottage cabin shelter garage office
    """.split()
)
#: The page the turn names: one distinctive word beside two everyday ones.
NAMED_PAGE = "Knowledge Base/Notes/Research/kelvane-harbor-review.md"
#: The turn: one name inside a long run of everyday words, as dictated.
TURN = (
    "so yeah " + " ".join(EVERYDAY[:30]) + " and you know the kelvane harbor review "
    + " ".join(EVERYDAY[30:]) + " anyway"
)
SMALL_CORPUS = 120
LARGE_CORPUS = 4 * SMALL_CORPUS


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _grow_filler(vault: Path, total: int) -> None:
    """Filler pages up to `total`, each holding every everyday word."""
    folder = vault / "Knowledge Base" / "Notes" / "Journal"
    for index in range(total):
        path = folder / f"filler-{index:04d}.md"
        if not path.exists():
            _write(
                path,
                f"---\ntype: note\nstatus: active\n---\n\n# Filler {index:04d}\n\n"
                + " ".join(EVERYDAY) + "\n",
            )
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()


def _ranked_pages(vault: Path) -> tuple[int, tuple]:
    """How many candidate rows one warm carry's corroboration test examines,
    and its groups.

    Either ranking path reports that count to an open `call_spans.meter`: the
    rows the admitted-corpus scorer scores, or the rows the catalogue checks
    in rank order. The first call fills the per-snapshot statistics; the
    second is the steady state of a served turn.
    """
    working_set_runtime.carry_named_groups(vault, TURN)
    with call_spans.meter(call_spans.CANDIDATES) as measured:
        groups, state = working_set_runtime.carry_named_groups(vault, TURN)
    assert state == "available"
    return (
        int(measured[call_spans.CANDIDATES]),
        tuple(tuple(path for path, _score in group) for group in groups),
    )


@pytest.mark.timeout(300)
def test_the_carry_phrase_query_does_not_grow_with_the_pages_its_words_match(
    vault: Path,
) -> None:
    _write(
        vault / NAMED_PAGE,
        "---\ntype: research-note\nstatus: active\n---\n\n# Kelvane Harbor Review\n\n"
        "The kelvane harbor review raised the ceiling. " + " ".join(EVERYDAY) + "\n",
    )
    _grow_filler(vault, SMALL_CORPUS)
    small_tests, small_groups = _ranked_pages(vault)
    _grow_filler(vault, LARGE_CORPUS)
    large_tests, large_groups = _ranked_pages(vault)

    assert small_groups == large_groups == ((NAMED_PAGE,),)
    assert small_tests > 0, "the carry no longer ranks candidate pages; re-target this guard"
    assert large_tests <= 1.5 * small_tests, (
        f"the carry ranked {small_tests} candidate pages over {SMALL_CORPUS} filler pages and "
        f"{large_tests} over {LARGE_CORPUS}: it examines the pages the turn's words "
        "match, not only the pages that hold a phrase"
    )


@pytest.mark.parametrize("audience", ["owner", "external"])
def test_warm_governed_carry_reuses_membership_without_parsing_the_corpus(
    vault: Path, monkeypatch: pytest.MonkeyPatch, audience: str
) -> None:
    from exomem import find, lifecycle_statuses, state_paths, vault as vault_module
    from exomem.governance import egress
    from exomem.governance.principal import RequestPrincipal, owner_principal, request_scope
    from test_governance_egress import write_rule, write_scope

    _write(
        vault / NAMED_PAGE,
        "---\ntype: research-note\nstatus: active\n---\n\n# Kelvane Harbor Review\n\n"
        "The kelvane harbor review raised the ceiling. " + " ".join(EVERYDAY) + "\n",
    )
    hidden = "Knowledge Base/Notes/Private/hidden.md"
    _write(vault / hidden, "---\ntype: note\n---\n# Private\nThe kelvane harbor review is pending.\n")
    write_scope(vault, paths="Notes/Private/**", name="Private")
    write_rule(vault, ceiling=0)
    _grow_filler(vault, SMALL_CORPUS)
    checkpoint = find.FreshnessSnapshot(vault).recall_checkpoint("kb")
    who = owner_principal() if audience == "owner" else RequestPrincipal(
        audience_id="external", surface="mcp"
    )
    parses = 0
    load = vault_module.yaml_safe_load

    def counted_load(*args, **kwargs):
        nonlocal parses
        parses += 1
        return load(*args, **kwargs)

    monkeypatch.setattr(vault_module, "yaml_safe_load", counted_load)

    def carry():
        with request_scope(who), state_paths.resolution_scope():
            basis = lifecycle_statuses.Basis(vault)
            visible = egress.reader_view(vault)
            admitted = visible if visible is not None else egress.release_walk_filter(vault)
            context = lexstore.RequestQueryContext(vault, admitted, basis, restricted=visible is not None)
            return working_set_runtime.carry_named_groups(
                vault, TURN, visible=visible, status_basis=basis, query_context=context,
                freshness=checkpoint.triple, recall_checkpoint=checkpoint,
            )

    cold_groups, cold_state = carry()
    assert parses > 0, "the cold governed corpus did not exercise metadata parsing"
    parses = 0
    warm_groups, warm_state = carry()

    assert warm_state == cold_state == "available"
    assert warm_groups == cold_groups
    assert warm_groups and any(path == NAMED_PAGE for group in warm_groups for path, _ in group)
    if audience == "external":
        assert all(path != hidden for group in warm_groups for path, _ in group)
    assert parses == 0, "warm governed carry reparsed unchanged corpus metadata"


def _grow_paused(vault: Path, total: int) -> None:
    """Pages up to `total` under a status label the vault defines, each holding
    every everyday word so the carry's term query matches all of them."""
    folder = vault / "Knowledge Base" / "Notes" / "Pending"
    for index in range(total):
        path = folder / f"paused-{index:04d}.md"
        if not path.exists():
            _write(
                path,
                f"---\ntype: note\nstatus: paused\n---\n\n# Paused {index:04d}\n\n"
                + " ".join(EVERYDAY) + "\n",
            )
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()


def test_warm_governed_carry_reads_the_policy_once_however_many_vault_defined_pages_match(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page whose status label the vault defines is classified through its own
    registry, behind a release decision. That decision must come from one read of
    the governance policy per carry, not one per matched page and turn word."""
    from test_governance_egress import write_rule, write_scope

    from exomem import find, lifecycle_statuses, state_paths
    from exomem.governance import egress, policy
    from exomem.governance.principal import owner_principal, request_scope

    overlay = lifecycle_statuses.registry_path(vault)
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text("schema_version: 1\nentries:\n  paused:\n    attributes: {class: pending}\n")
    _write(
        vault / NAMED_PAGE,
        "---\ntype: research-note\nstatus: active\n---\n\n# Kelvane Harbor Review\n\n"
        "The kelvane harbor review raised the ceiling. " + " ".join(EVERYDAY) + "\n",
    )
    write_scope(vault, paths="Notes/Private/**", name="Private")
    write_rule(vault, ceiling=0)
    _grow_filler(vault, SMALL_CORPUS)
    who = owner_principal()
    signatures = 0
    signature = policy._signature

    def counted_signature(*args, **kwargs):
        nonlocal signatures
        signatures += 1
        return signature(*args, **kwargs)

    monkeypatch.setattr(policy, "_signature", counted_signature)

    def warm_carry_signatures() -> int:
        nonlocal signatures
        checkpoint = find.FreshnessSnapshot(vault).recall_checkpoint("kb")

        def carry():
            with request_scope(who), state_paths.resolution_scope():
                basis = lifecycle_statuses.Basis(vault)
                visible = egress.reader_view(vault)
                admitted = visible if visible is not None else egress.release_walk_filter(vault)
                context = lexstore.RequestQueryContext(
                    vault, admitted, basis, restricted=visible is not None
                )
                return working_set_runtime.carry_named_groups(
                    vault, TURN, visible=visible, status_basis=basis, query_context=context,
                    freshness=checkpoint.triple, recall_checkpoint=checkpoint,
                )

        carry()
        signatures = 0
        groups, state = carry()
        assert state == "available"
        assert any(path == NAMED_PAGE for group in groups for path, _ in group)
        return signatures

    _grow_paused(vault, 8)
    few = warm_carry_signatures()
    _grow_paused(vault, 32)
    many = warm_carry_signatures()

    assert few > 0, "the carry no longer loads the policy; re-target this guard"
    assert many <= few, (
        f"a warm carry read the policy {few} times with 8 matching vault-defined pages and "
        f"{many} times with 32: it decides release once per matched page"
    )
