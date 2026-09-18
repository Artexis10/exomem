"""make-anchor-resolution-sound, task 1.2/2/3 — the real-vault false-activation
defect, reproduced on a seeded dense cluster and fixed.

`design.md`'s "Why" section: run against a real, densely linked vault with a
recall index available, every turn resolved ~5 irrelevant anchors with
evidence `[graph_corroboration, retrieval]` — `retrieval` granted when the
anchor OR any page in its link neighbourhood was a recall hit (hybrid recall
returns hits for every turn, and a hub links dozens of pages), and
`graph_corroboration` granted to any linked pair regardless of how either got
there, so candidates admitted through (1) corroborate each other by
construction and two kinds resolve.

These tests build a small, fully generic, synthetic analogue of that
structure (`build_soundness_probe_corpus`: a six-hub, fourteen-support ring
cluster) and run the REAL resolver end to end — `WorkingSetIndex` +
`working_set_resolve.candidates_for`/`add_graph_corroboration`/`resolve` —
never a hand-authored packet. Confirmed red-first against the pre-fix
resolver (git history, `working_set_resolve.py`/`working_set_index.py` before
this change): T10's turn resolved four unrelated hub anchors with evidence
`(graph_corroboration, retrieval)` (the real vault's exact defect shape);
T11's turn produced a false `partial` candidate whose only "contact" was a
recall hit on the anchor's own neighbour; C10's turn produced no candidate at
all. All three are asserted fixed below.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BENCHMARKS_DIR = Path(__file__).resolve().parents[1] / "benchmarks"
if str(BENCHMARKS_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARKS_DIR))

from epistemic.corpora.context_activation import (  # noqa: E402
    C10_TURN,
    T10_TURN,
    T11_TURN,
    build_soundness_probe_corpus,
)

from exomem import working_set_index, working_set_resolve  # noqa: E402


def _resolve_turn(vault: Path, turn: str, retrieval_paths: frozenset[str]):
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    rows = working_set_resolve.facts_from_rows(index.anchors())
    analysis = working_set_resolve.analyze_turn(turn)
    candidates = working_set_resolve.candidates_for(
        analysis,
        rows,
        retrieval_paths=retrieval_paths,
        term_anchor_counts=index.term_anchor_counts(),
    )
    candidates = working_set_resolve.add_graph_corroboration(
        candidates, retrieval_paths=retrieval_paths
    )
    return working_set_resolve.resolve(candidates)


@pytest.fixture
def probe(tmp_path: Path):
    manifest = build_soundness_probe_corpus(tmp_path)
    return tmp_path, manifest


def test_dense_cluster_is_at_least_twenty_pages_with_at_least_six_anchors(probe) -> None:
    """design.md decision 7's corpus shape, checked structurally."""
    _vault, manifest = probe
    assert len(manifest.hub_paths) + len(manifest.support_paths) >= 20
    assert len(manifest.hub_paths) >= 6


def test_t10_negative_twin_does_not_falsely_activate_the_cluster(probe) -> None:
    """T10: recall hits fall on support pages inside the cluster, never on any
    hub's own page. Fixed resolver: no hub is even a candidate, so the turn
    abstains as `unresolved` (canonical spec: "A dense vault does not
    activate on an unrelated turn").
    """
    vault, manifest = probe
    hits = frozenset(manifest.support_paths[0:4])

    resolution = _resolve_turn(vault, T10_TURN, hits)

    assert resolution.status == "unresolved"
    assert resolution.anchors == ()


def test_t11_a_neighbours_hit_is_not_contact_for_the_hub(probe) -> None:
    """T11: the only retrieval hit is one hub's OWN neighbour, never the hub's
    own page. Fixed resolver: the hub carries no contact kind at all and is
    not a candidate (canonical spec: "A recall hit near an anchor is not
    contact" -- "the hub is not a candidate on that account").
    """
    vault, manifest = probe
    hits = frozenset({manifest.first_hub_own_neighbour})

    resolution = _resolve_turn(vault, T11_TURN, hits)

    assert resolution.status == "unresolved"
    assert resolution.anchors == ()
    assert manifest.hub_paths[0] not in {anchor.path for anchor in resolution.anchors}


def test_c10_a_one_word_reference_resolves_via_the_derived_short_name(probe) -> None:
    """C10: a uniquely, qualifier-titled resource ("Bike (spare, blue
    frame)") reached by its everyday one-word name alone. Fixed index: "bike"
    is derived as an alias while unique in the catalogue, so the turn resolves
    on `exact_alias` -- never merely `partial` on a weak or retrieved kind.
    """
    vault, manifest = probe

    resolution = _resolve_turn(vault, C10_TURN, frozenset())

    assert resolution.status == "resolved"
    bike = next(a for a in resolution.anchors if a.path == manifest.bike_path)
    assert bike.status == "resolved"
    assert "exact_alias" in bike.evidence


def test_c10_derived_alias_is_retired_by_a_second_same_leading_name_page(probe) -> None:
    """The uniqueness half of the same mechanism, on the same probe vault:
    once a second page shares the derived leading name, neither gets the
    alias, and the one-word turn no longer resolves either of them via
    `exact_alias` from that account.
    """
    vault, manifest = probe
    from test_working_set_index import _write

    _write(
        vault / "Knowledge Base" / "Products" / "second-bike.md",
        "---\ntitle: Bike (second, red frame)\nstatus: active\nupdated: 2026-09-01\n---\n\n"
        "# Bike (second, red frame)\n\nA second bicycle, unrelated to the first.\n",
    )

    resolution = _resolve_turn(vault, C10_TURN, frozenset())

    resolved_paths = {a.path for a in resolution.anchors if a.status == "resolved"}
    assert manifest.bike_path not in resolved_paths
