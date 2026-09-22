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


# --------------------------------------------------------------------------- #
# Independent review, round 3
# --------------------------------------------------------------------------- #


def _hub_page(vault: Path, rel: str, title: str) -> str:
    from test_working_set_index import _write

    _write(
        vault / rel,
        f"---\ntitle: {title}\nstatus: active\ntags: [hub]\nupdated: 2026-09-01\n---\n\n"
        f"# {title}\n\nA coordination hub.\n",
    )
    return rel


def test_a_tag_every_hub_carries_is_not_a_name_for_the_others(tmp_path: Path) -> None:
    """BLOCKER 1 (the reviewer's four-page hub vault): every hub carries
    `tags: [hub]`; exactly ONE hub's title literally contains the word "hub".
    A turn saying only "hub" must not let a SECOND hub — reached only by a
    recall hit on its own page, sharing nothing else with the turn — resolve
    on `[rare_term, retrieval]`. That evidence pair is the original defect in
    a new hat: `rare_term` must be granted only from the anchor's OWN
    authored title/alias terms, never a term it carries only through a tag.
    """
    from exomem import working_set, working_set_index

    vault = tmp_path / "vault"
    _hub_page(vault, "Knowledge Base/Notes/Insights/h1.md", "Hub overview")
    _hub_page(vault, "Knowledge Base/Notes/Insights/h2.md", "Northern Circuit")
    _hub_page(vault, "Knowledge Base/Notes/Insights/h3.md", "Southern Circuit")
    _hub_page(vault, "Knowledge Base/Notes/Insights/h4.md", "Eastern Circuit")

    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    packet = working_set.compile_packet(
        vault,
        turn="hub",
        retrieval_paths=frozenset({"Knowledge Base/Notes/Insights/h2.md"}),
        index=index,
    )

    by_path = {a["path"]: a for a in packet["anchors"]}
    h2 = by_path.get("Knowledge Base/Notes/Insights/h2.md")
    assert h2 is None or (
        "rare_term" not in h2["evidence"] and h2["status"] != "resolved"
    ), h2
    assert packet["abstained"] is True


def test_a_partial_anchor_besides_a_resolved_one_contributes_nothing_to_units(
    tmp_path: Path,
) -> None:
    """BLOCKER 2 (the reviewer's shape): one resolved anchor (exact_alias) and
    one retrieval-only `partial` anchor. The partial anchor's own lede must
    not appear anywhere in the packet -- not as a unit, not as a pointer, not
    as current state (canonical spec's restated "Bounded role lanes"
    requirement, "A partial anchor beside a resolved one is listed, not
    served").
    """
    from test_working_set_index import _write

    from exomem import working_set, working_set_index

    vault = tmp_path / "vault"
    _write(
        vault / "Knowledge Base/Products/widget.md",
        "---\ntitle: Widget\nstatus: active\nupdated: 2026-09-01\n---\n\n"
        "# Widget\n\n## Summary\n\nA small generic device kept on the shelf.\n",
    )
    secret_lede = "This exact sentence must never leak into a served packet."
    _write(
        vault / "Knowledge Base/Products/other-thing.md",
        "---\ntitle: Other thing\nstatus: active\nupdated: 2026-09-01\n---\n\n"
        f"# Other thing\n\n{secret_lede}\n",
    )

    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    packet = working_set.compile_packet(
        vault,
        turn="what about the widget",
        retrieval_paths=frozenset({"Knowledge Base/Products/other-thing.md"}),
        index=index,
    )

    by_path = {a["path"]: a for a in packet["anchors"]}
    widget = by_path["Knowledge Base/Products/widget.md"]
    other = by_path["Knowledge Base/Products/other-thing.md"]
    assert widget["status"] == "resolved"
    assert other["status"] == "partial"
    packet_text = str(packet)
    assert secret_lede not in packet_text
    assert all(unit.get("provenance", {}).get("path") != other["path"] for unit in packet["units"])
    assert all(pointer.get("ref") != other["path"] for pointer in packet["pointers"])


def test_a_graph_hop_only_hit_does_not_grant_the_linked_hub_retrieval(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MAJOR 4 (the reviewer's p6 shape): a hub page shares no word with the
    turn at all, but is wikilinked from a page that DOES answer the turn's
    words. Real `find(graph=True)` surfaces the hub only via a
    `graph_hop=True` hit -- never because the turn's own words reached it --
    and `op_activate_context` must not let that graph-hop-only hit grant the
    hub `retrieval`. This runs the REAL product surface end to end (never a
    hand-built `retrieval_paths` set, which the lower-level `_resolve_turn`
    tests above use and so never exercise this path at all).
    """
    from test_working_set_index import _write

    from exomem import bm25, commands, working_set_index, working_set_runtime
    from exomem import find as find_module

    # Raw writes, not `create_file`: the tier-2 authoring contract (a
    # qualifying relation, a non-empty `## Observations`/`## Decision` unit)
    # is orthogonal to what this test exercises -- the resolver's own
    # evidence rule -- and every other direct-write test in this module
    # bypasses it the same way.
    _write(
        vault / "Knowledge Base/Notes/Insights/major4-hub.md",
        "---\ntitle: Major4 hub\nstatus: active\ntags: [hub]\nupdated: 2026-09-01\n---\n\n"
        "# Major4 hub\n\nA coordination hub with no lexical overlap with the query at all.\n",
    )
    _write(
        vault / "Knowledge Base/Notes/Insights/major4-linker.md",
        "---\ntitle: Major4 linker\nstatus: active\nupdated: 2026-09-01\n---\n\n"
        "# Major4 linker\n\nDistinctive probe phrase used only here for the query. "
        "See also [[Knowledge Base/Notes/Insights/major4-hub]].\n",
    )
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    bm25.clear_cache()
    find_module.clear_cache()
    find_module._RESOLVER_CACHE.clear()

    # Confirm the graph-hop shape is actually real before trusting the packet:
    # the hub must come back ONLY as a graph_hop=True hit, never lexically.
    hits = find_module.find(
        vault, query="distinctive probe phrase", mode="hybrid", graph=True, limit=10
    )
    hub_hit = next((h for h in hits if "major4-hub" in h.path), None)
    assert hub_hit is not None and hub_hit.graph_hop, (
        f"expected a graph_hop=True hub hit, got {[(h.path, h.graph_hop) for h in hits]}"
    )

    packet = commands.op_activate_context(vault, turn="distinctive probe phrase")

    by_path = {a.get("path"): a for a in packet.get("anchors", ()) or ()}
    hub = by_path.get("Knowledge Base/Notes/Insights/major4-hub.md")
    assert hub is None or "retrieval" not in (hub.get("evidence") or ()), hub


# --------------------------------------------------------------------------- #
# fix/activation-competing-senses, R3, end to end
# --------------------------------------------------------------------------- #


def test_r3_a_named_project_carries_the_packet_past_two_weak_unrelated_entities(
    tmp_path: Path,
) -> None:
    """Problem 3, end to end through the real resolver + index +
    `working_set.compile_packet` -- the same deterministic pattern
    `test_a_tag_every_hub_carries_is_not_a_name_for_the_others` above uses
    (a hand-fed `retrieval_paths` set rather than the real bm25/find
    pipeline, so the two "unrelated weak entities" shape stays deterministic
    instead of depending on live ranking).

    Turn "fix the flaky gate test in alpha" names project `alpha` by
    `exact_alias`. Two unrelated entity pages, "North Gate" and "South
    Gate", each resolve only on `rare_term` + `retrieval` (the single shared
    word "gate") -- same kind, no structural link between them. Before R3
    this same-kind weak competition made the whole turn `ambiguous` and
    withheld the named project's own material; after R3 the two weak
    entities are demoted to `partial` and the turn resolves on the project.

    Correction round 1, C2: the units LANE needs the maintained semantic-
    recall catalog, which a bare `tmp_path` vault is not warm for by
    default -- but `tests/test_activate_context_request_path.py`'s own
    `activation_vault` fixture already shows the fix, one extra call,
    `lexstore.ensure_fresh(vault)`, before `WorkingSetIndex(vault).rebuild()`.
    With it, the resolved project's own member page ("Alpha notes") serves
    a real unit, which this test now asserts directly (by provenance path),
    alongside the existing non-leakage assertions for the two demoted weak
    entities.
    """
    from test_working_set_index import _write

    from exomem import lexstore, working_set, working_set_index, working_set_runtime

    vault = tmp_path / "vault"
    _write(
        vault / "Knowledge Base" / "_Schema" / "project-keys.yaml",
        """projects:
  alpha:
    folder: Alpha
    category: engineering
""",
    )
    _write(
        vault / "Knowledge Base" / "Notes" / "Engineering" / "alpha-notes.md",
        "---\ntitle: Alpha notes\nproject: alpha\nstatus: active\nupdated: 2026-09-01\n---\n\n"
        "# Alpha notes\n\n## Decision\n\nThe alpha gate test suite runs nightly.\n",
    )
    north_gate = "Knowledge Base/Entities/People/North Gate.md"
    south_gate = "Knowledge Base/Entities/People/South Gate.md"
    _write(
        vault / north_gate,
        "---\ntitle: North Gate\ntype: entity\nstatus: active\nupdated: 2026-09-01\n---\n\n"
        "# North Gate\n\n## Summary\n\nRuns a review gate for a different, unrelated team.\n",
    )
    _write(
        vault / south_gate,
        "---\ntitle: South Gate\ntype: entity\nstatus: active\nupdated: 2026-09-01\n---\n\n"
        "# South Gate\n\n## Summary\n\nRuns a review gate for yet another unrelated team.\n",
    )

    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    packet = working_set.compile_packet(
        vault,
        turn="fix the flaky gate test in alpha",
        retrieval_paths=frozenset({north_gate, south_gate}),
        index=index,
    )

    assert packet["abstained"] is False, packet
    by_path = {a["path"]: a for a in packet["anchors"]}
    project_anchor = next(a for a in packet["anchors"] if a["kind"] == "project")
    assert project_anchor["status"] == "resolved", project_anchor
    north = by_path.get(north_gate)
    south = by_path.get(south_gate)
    assert north is None or north["status"] == "partial", north
    assert south is None or south["status"] == "partial", south
    assert packet["ambiguity"] == []
    alpha_notes_path = "Knowledge Base/Notes/Engineering/alpha-notes.md"
    assert any(
        unit.get("provenance", {}).get("path") == alpha_notes_path for unit in packet["units"]
    ), packet["units"]
    # The two weak, demoted entities supply no material of their own.
    assert all(unit.get("provenance", {}).get("path") != north_gate for unit in packet["units"])
    assert all(unit.get("provenance", {}).get("path") != south_gate for unit in packet["units"])
    assert all(pointer.get("ref") != north_gate for pointer in packet["pointers"])
    assert all(pointer.get("ref") != south_gate for pointer in packet["pointers"])


# --------------------------------------------------------------------------- #
# The same turns with a LIVE hot profile (close-memory-loop D2). Every test
# above resolves without one, which is exactly why they could not see a prior
# answering a novel turn. Here the freshness registry is seeded the way the
# running service keeps it, with one page freshly edited, and each negative
# is paired with a "continue" that proves the profile is actually on.
# --------------------------------------------------------------------------- #


def _live_hot_profile(vault: Path, *, freshest: str) -> None:
    """Every page old, `freshest` just edited, the registry seeded."""
    import os
    import time

    from exomem import file_watcher

    now = time.time()
    for index, page in enumerate(sorted((vault / "Knowledge Base").rglob("*.md"))):
        os.utime(page, (now - 10_000 - index, now - 10_000 - index))
    target = vault / freshest
    os.utime(target, (now, now))
    file_watcher.FileWatcher(vault)._reconcile_once(seed=True)


def _statuses(packet: dict) -> dict[str, str]:
    return {item["path"]: item["status"] for item in packet["anchors"]}


@pytest.fixture
def hot_probe(probe):
    vault, manifest = probe
    working_set_index.WorkingSetIndex(vault).rebuild()
    _live_hot_profile(vault, freshest=manifest.hub_paths[0])
    return vault, manifest


def test_the_negative_twins_stay_unresolved_with_a_live_hot_profile(hot_probe) -> None:
    """T10 and T11 name nothing and say nothing about pointing back. The
    hottest hub is right there, and neither turn is answered with it."""
    from exomem import working_set

    vault, manifest = hot_probe
    control = working_set.compile_packet(vault, turn="continue", max_chars=4000)
    assert _statuses(control).get(manifest.hub_paths[0]) == "resolved", (
        "the profile must be live, or the negatives below prove nothing"
    )

    for turn in (T10_TURN, T11_TURN):
        packet = working_set.compile_packet(vault, turn=turn, max_chars=4000)

        assert packet["abstained"] is True, turn
        assert packet["abstention"] == {"reason": "unresolved"}, turn
        assert "resolved" not in _statuses(packet).values(), turn
        assert packet["units"] == [], turn
        assert all("recency" not in item["evidence"] for item in packet["anchors"]), turn
        # The block, not an answer: what was recently worked on is still said.
        assert packet["recent_context"], turn


def test_the_one_word_reference_is_unchanged_by_a_live_hot_profile(hot_probe) -> None:
    """C10 reaches its page on its own words; the prior adds nothing to it."""
    from exomem import working_set

    vault, manifest = hot_probe

    packet = working_set.compile_packet(vault, turn=C10_TURN, max_chars=4000)

    assert _statuses(packet).get(manifest.bike_path) == "resolved"
    assert all("recency" not in item["evidence"] for item in packet["anchors"])


@pytest.mark.parametrize(
    "retire",
    [
        pytest.param(("status: active", "status: archived"), id="archived"),
        pytest.param(
            ("status: active", 'status: active\nsuperseded_by: "[[Cluster node 02]]"'),
            id="superseded",
        ),
    ],
)
def test_a_retired_hot_hub_is_never_offered(probe, retire: tuple[str, str]) -> None:
    """The freshest page in the vault is a retired hub. A prior never
    resurrects superseded state: "continue" is answered from the next
    current page instead, and the retired hub appears nowhere in the packet."""
    from exomem import working_set

    vault, manifest = probe
    hub = vault / manifest.hub_paths[0]
    before, after = retire
    text = hub.read_text(encoding="utf-8")
    assert before in text
    hub.write_text(text.replace(before, after, 1), encoding="utf-8")
    working_set_index.WorkingSetIndex(vault).rebuild()
    _live_hot_profile(vault, freshest=manifest.hub_paths[0])

    packet = working_set.compile_packet(vault, turn="continue", max_chars=4000)

    assert manifest.hub_paths[0] not in _statuses(packet)
    assert packet["abstained"] is False, packet.get("abstention")
    assert all(
        manifest.hub_paths[0] not in str(unit.get("ref") or "") for unit in packet["units"]
    )


# --------------------------------------------------------------------------- #
# Every turn of the context-activation corpus, with a live hot profile.
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def hot_product_corpus(tmp_path_factory: pytest.TempPathFactory):
    from epistemic.corpora.context_activation import build_corpus

    root = tmp_path_factory.mktemp("hot-corpus") / "vault"
    manifest = build_corpus(root, distractor_count=0)
    return root, manifest


@pytest.mark.timeout(240)
def test_no_corpus_turn_consults_the_prior_under_a_live_hot_profile(
    hot_product_corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """None of the corpus's turns or reminder turns points back, so none of
    them may be answered, or even ranked, by recency: the hot profile is not
    computed for any of them, and no anchor they reach carries `recency`.
    The freshest page is C1's gold collection, so a prior that leaked into
    T1 — C1's negative twin — would show."""
    from epistemic.corpora.context_activation import FIXTURES

    from exomem import lexstore, working_set, working_set_runtime

    vault, manifest = hot_product_corpus
    freshest = manifest.key_to_path["c1_subscriptions_collection"]
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    assert freshest in {row.path for row in index.anchors()}, "must be an anchor to be hot"
    _live_hot_profile(vault, freshest=freshest)

    control = working_set.compile_packet(vault, turn="continue", max_chars=4000)
    assert _statuses(control).get(freshest) == "resolved", (
        "the profile must be live, or the corpus run below proves nothing"
    )

    real = working_set.hot_profile
    consulted: list[str] = []

    def _spy(*args, **kwargs):
        consulted.append("hot_profile")
        return real(*args, **kwargs)

    monkeypatch.setattr(working_set, "hot_profile", _spy)
    for fixture in FIXTURES:
        for turn in (fixture.turn, fixture.reminder_turn):
            packet = working_set.compile_packet(vault, turn=turn, max_chars=4000)
            assert all(
                "recency" not in item["evidence"] for item in packet["anchors"]
            ), (fixture.case_id, turn)

    assert consulted == []
