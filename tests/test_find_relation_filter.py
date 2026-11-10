"""Relation-filtered recall wired into the find pipeline.

Covers the find()-level integration: participant intersection into the
eligibility seam (composes with structured filters and empty-query recall),
direction and anchor, unknown-key rejection, graph=false still filtering, and
the never-false-empty degrade matrix (available / warming / temporarily
unavailable).
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import epistemic_graph
from exomem import find as find_module
from exomem.cli_ops import OpError
from exomem.find import RetrievalIndexWarming

A = "Knowledge Base/Notes/Insights/a.md"
B = "Knowledge Base/Notes/Insights/b.md"
C = "Knowledge Base/Notes/Insights/c.md"


def _write(vault: Path, rel: str, body: str) -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _vault(tmp_path: Path, *, build: bool = True) -> Path:
    vault = tmp_path / "vault"
    _write(
        vault,
        A,
        "---\ntype: insight\n---\n# A\n\n## Relations\n\n"
        "- supports [[Knowledge Base/Notes/Insights/b]]\n"
        "- contradicts [[Knowledge Base/Notes/Insights/c]]\n",
    )
    _write(vault, B, "---\ntype: insight\n---\n# B\n\nBody.\n")
    _write(vault, C, "---\ntype: insight\n---\n# C\n\nBody.\n")
    if build:
        epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    return vault


def _paths(vault: Path, **kw) -> set[str]:
    return {h.path for h in find_module.find(vault, query="", limit=15, **kw)}


def test_relation_filter_selects_participants(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    assert _paths(vault, relations=["supports"]) == {A, B}


def test_relation_direction_filters(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    assert _paths(vault, relations=["supports"], relation_direction="outbound") == {A}
    assert _paths(vault, relations=["supports"], relation_direction="inbound") == {B}


def test_relation_of_anchor(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    assert _paths(vault, relations=["supports"], relation_of=A) == {B}


def test_symmetric_relation_ignores_direction(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    assert _paths(vault, relations=["contradicts"], relation_direction="outbound") == {A, C}


def test_relation_of_alone_matches_all_edges(tmp_path: Path) -> None:
    # An anchor with no relation keys must match every typed edge touching it,
    # never a silent empty (A supports B, A contradicts C).
    vault = _vault(tmp_path)
    assert _paths(vault, relation_of=A) == {B, C}


def test_mixed_result_level_applies_relation_filter_to_units(tmp_path: Path) -> None:
    # In mixed mode the unit half must be gated by the relation filter too — a unit
    # whose parent page does not participate must not leak through.
    vault = _vault(tmp_path)
    hits = find_module.find(
        vault, query="", relations=["supports"], result_level="mixed", limit=15
    )
    parents = set()
    unit_hits = []
    for h in hits:
        pp = getattr(h, "parent_path", None)
        if pp is not None:  # a SemanticUnitHit
            unit_hits.append(h)
            parents.add(pp)
        else:
            parents.add(getattr(h, "path", None))
    # Non-participant excluded...
    assert C not in parents
    # ...and the inclusion half: a participating parent's unit survives and is
    # annotated (matched via its parent page).
    if unit_hits:
        assert all(u.parent_path in {A, B} for u in unit_hits)
        annotated = [u for u in unit_hits if u.relation_match is not None]
        assert annotated, "surviving mixed units must carry the relation_match annotation"
        assert all(u.relation_match["matched"] == "parent" for u in annotated)


def test_filter_still_applies_with_graph_disabled_lane(tmp_path: Path) -> None:
    # graph=False turns off the graph ranking lane, but the relation filter is
    # eligibility, not lane fusion — it must still apply.
    vault = _vault(tmp_path)
    assert _paths(vault, relations=["supports"], graph=False) == {A, B}


def test_authoritative_empty_no_matching_edge(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    assert _paths(vault, relations=["mitigates"]) == set()


def test_unknown_relation_is_rejected(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    with pytest.raises(OpError) as excinfo:
        find_module.find(vault, query="", relations=["implments"], limit=15)
    assert excinfo.value.code == "INVALID_RELATION_FILTER"
    assert "implements" in excinfo.value.details.get("suggestions", [])


def test_invalid_direction_is_rejected(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    with pytest.raises(OpError) as excinfo:
        find_module.find(vault, query="", relations=["supports"], relation_direction="sideways")
    assert excinfo.value.code == "INVALID_RELATION_FILTER"


def test_disabled_index_raises_temporarily_unavailable(tmp_path: Path, monkeypatch) -> None:
    vault = _vault(tmp_path)
    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_INDEX", "1")
    with pytest.raises(RetrievalIndexWarming) as excinfo:
        find_module.find(vault, query="", relations=["supports"], limit=15)
    assert excinfo.value.status == "temporarily_unavailable"


def test_missing_sidecar_raises_warming(tmp_path: Path) -> None:
    vault = _vault(tmp_path, build=False)
    with pytest.raises(RetrievalIndexWarming) as excinfo:
        find_module.find(vault, query="", relations=["supports"], limit=15)
    assert excinfo.value.status == "warming"


def test_custom_manager_read_binds_relation_warming_to_its_runtime_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.writer_lease import LeaseConfig, LeaseManager

    ambient_state = tmp_path / "ambient-state"
    custom_state = tmp_path / "custom-state"
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(ambient_state))
    vault = _vault(tmp_path, build=False)
    manager = LeaseManager(LeaseConfig(state_dir=custom_state))
    observed: list[Path] = []
    rebuilt = threading.Event()

    def rebuild(self):  # noqa: ANN001
        observed.append(self._canonical_mutation_coordinator().state_root)
        rebuilt.set()
        return {}

    monkeypatch.setattr(epistemic_graph.EpistemicGraphIndex, "rebuild_all", rebuild)
    command = SimpleNamespace(
        name="relation-read",
        read_only=True,
        leaf=lambda root: find_module.find(root, query="", relations=["supports"], limit=15),
    )

    with pytest.raises(RetrievalIndexWarming):
        manager.invoke(command, (vault,), {})

    assert rebuilt.wait(1)
    assert observed == [custom_state]


def test_stale_sidecar_raises_warming(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    conn = sqlite3.connect(epistemic_graph.sidecar_path(vault))
    conn.execute("UPDATE graph_meta SET value = '6' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    with pytest.raises(RetrievalIndexWarming) as excinfo:
        find_module.find(vault, query="", relations=["supports"], limit=15)
    assert excinfo.value.status == "warming"


def test_no_relation_filter_is_unaffected(tmp_path: Path) -> None:
    # Absent filter: every page is eligible (empty-query recall returns all three).
    vault = _vault(tmp_path)
    assert _paths(vault) == {A, B, C}


def test_op_ask_memory_passes_relation_filter(tmp_path: Path) -> None:
    from exomem import commands

    vault = _vault(tmp_path)
    hits = commands.op_ask_memory(vault, query="", relations=["supports"], limit=15)["hits"]
    # compact detail projects hits as dicts; the relation_match annotation rides through.
    assert {h["path"] for h in hits} == {A, B}
    assert any(h.get("relation_match", {}).get("relation_type") == "supports" for h in hits)


def test_op_find_rejects_unknown_relation(tmp_path: Path) -> None:
    from exomem import commands

    vault = _vault(tmp_path)
    with pytest.raises(OpError) as excinfo:
        commands.op_find(vault, query="", relations=["contradcts"], limit=15)
    assert excinfo.value.code == "INVALID_RELATION_FILTER"


def test_relation_match_annotation_is_additive(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    hits = find_module.find(vault, query="", relations=["supports"], limit=15)
    by_path = {h.path: h for h in hits}
    assert by_path[A].relation_match is not None
    assert by_path[A].relation_match["relation_type"] == "supports"
    assert by_path[A].relation_match["direction"] == "outbound"
    assert by_path[A].relation_match["counterpart"] == B
    assert by_path[A].relation_match["matched"] == "self"
    assert by_path[B].relation_match["direction"] == "inbound"
    # Hits without a relation filter carry no annotation.
    plain = find_module.find(vault, query="", limit=15)
    assert all(h.relation_match is None for h in plain)


CLAIM = "Knowledge Base/Notes/quince-claim.md"


@pytest.mark.parametrize(
    ("target", "visible_target", "relation_filter"),
    [
        # Over the whole vault the hidden namesake makes the link ambiguous.
        ("Shared Name", True, {"relations": ["supports"]}),
        # Over the whole vault the link resolves to the hidden page alone.
        ("Secret Name", False, {"relations": ["supports"]}),
        # An anchor without relation keys reads every typed edge touching it.
        ("Shared Name", True, {"relation_of": CLAIM}),
    ],
    ids=["ambiguous-namesake", "only-hidden-target", "anchor-alone"],
)
def test_a_hidden_page_never_changes_a_restricted_callers_relation_filter(
    tmp_path: Path, target: str, visible_target: bool, relation_filter: dict
) -> None:
    """A restricted caller's relation filter reads the edges that a vault
    without the hidden page holds. Over the whole vault the hidden page either
    removes the visible target or becomes the edge's other end, and either one
    tells the caller that the hidden page exists."""
    present, absent = tmp_path / "present", tmp_path / "absent"
    for root in (present, absent):
        _write(
            root,
            CLAIM,
            f"---\ntype: insight\n---\n# Quince claim\n\n## Relations\n\n- supports [[{target}]]\n",
        )
        if visible_target:
            _write(root, f"Knowledge Base/Notes/{target}.md", "---\ntype: insight\n---\n# Catalogue\n")
    _write(present, f"Knowledge Base/Private/{target}.md", "---\ntype: insight\n---\n# Ledger\n")
    for root in (present, absent):
        epistemic_graph.EpistemicGraphIndex(root).rebuild_all()

    def admit(path: str) -> bool:
        return not path.startswith("Knowledge Base/Private/")

    def recall(root: Path, admit_path) -> list[dict]:
        hits = find_module.find(root, query="", limit=15, admit_path=admit_path, **relation_filter)
        return [hit.as_dict() for hit in hits]

    assert recall(present, None) != recall(absent, admit)
    assert recall(present, admit) == recall(absent, admit)


@pytest.fixture
def governed_anchors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from exomem.governance import policy
    from test_governance_egress import write_scope

    # Keep cold/warm boundaries deterministic; this test explicitly rebuilds as the owner.
    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_SCHEDULING", "1")
    vault = _vault(tmp_path, build=False)
    _write(vault, "Knowledge Base/Private/anchor.md",
           "---\ntype: insight\n---\n## Relations\n\n"
           "- supports [[Knowledge Base/Notes/Insights/b]]\n")
    write_scope(vault, paths="Notes/Insights/**")
    residual = "01ARZ3NDEKTSV4RRFFQ69G5FW2"
    _write(vault, "Knowledge Base/_Governance/scopes/residual.yaml",
           f"governance_version: 1\nid: {residual}\nresidual: true\n")
    _write(vault, "Knowledge Base/_Governance/rules/residual-external.yaml",
           "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
           f"scope_ids: [{residual}]\naudience: external\nceiling: 0\n")
    compiled = policy.load(vault)
    assert not compiled.empty and not compiled.blocked
    return vault


@pytest.mark.parametrize("full_path", [True, False])
def test_a_withheld_relation_anchor_matches_an_absent_anchor_before_and_after_graph_build(
    governed_anchors: Path, full_path: bool,
) -> None:
    """A policy-hidden anchor must not disclose itself through graph readiness or registry selection."""
    from exomem.governance.principal import library_scope, request_scope
    from test_governance_egress import _external

    vault = governed_anchors
    for built in (False, True):
        if built:
            with library_scope():
                epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
        with request_scope(_external()):
            hidden = _paths(vault, relations=["supports"], relation_of="Knowledge Base/Private/anchor.md" if full_path else "Private/anchor")
            absent = _paths(vault, relations=["supports"], relation_of="Knowledge Base/Private/absent.md" if full_path else "Private/absent")
        assert hidden == absent == set()


@pytest.mark.parametrize("audience", ["owner", "external"])
@pytest.mark.parametrize("full_path", [True, False])
def test_a_visible_relation_anchor_keeps_warming_and_recall_for_owner_and_non_owner(
    governed_anchors: Path, audience: str, full_path: bool,
) -> None:
    """Page release must preserve visible anchored recall, including shorthand paths and cold indexes."""
    from exomem.governance.principal import library_scope, request_scope
    from test_governance_egress import _external

    vault = governed_anchors
    anchor = A if full_path else "Notes/Insights/a"
    context = library_scope if audience == "owner" else lambda: request_scope(_external())
    with context(), pytest.raises(RetrievalIndexWarming) as excinfo:
        _paths(vault, relations=["supports"], relation_of=anchor)
    assert excinfo.value.status == "warming"
    with library_scope():
        epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    with context():
        assert _paths(vault, relations=["supports"], relation_of=anchor) == {B}


# ---------------------------------------------------------------------------
# A restricted caller: an edge to a page that an egress rule withholds reads as absent
# ---------------------------------------------------------------------------

NOTES = "Knowledge Base/Notes"
PATTERNS = f"{NOTES}/Patterns"
ROADMAP = f"{NOTES}/Roadmap.md"
REVIEW = f"{NOTES}/Review.md"
BUDGET = f"{NOTES}/Budget.md"
OLD_BUDGET = f"{NOTES}/Old budget.md"
MEMO = f"{NOTES}/Memo.md"
FINDINGS = f"{NOTES}/Findings.md"
OLD_ROADMAP = f"{PATTERNS}/Old roadmap.md"


# A rule that withholds the page only from a caller that declares the `marketing` purpose.
_MARKETING_ONLY = "purpose: marketing\npurpose_condition: matches\n"


def _release_twin(tmp_path: Path, *, withheld: bool, rule_extra: str = "") -> Path:
    """A vault whose egress rule withholds `Notes/Patterns/**` from an external caller, with the
    withheld page or as if it never had it. `rule_extra` narrows the rule, for one that applies
    only to a declared purpose. The roadmap supersedes the withheld page, and the
    withheld page supports the review. A visible supersession between two budget pages, and a
    visible support between two others, give both vaults the same non-empty answers."""
    from conftest import initialize_vault_state_offline
    from test_governance_egress import _reset_caches, write_rule, write_scope

    from exomem.governance import egress

    vault = tmp_path / ("with-page" if withheld else "without-page")
    vault.mkdir()
    initialize_vault_state_offline(vault, source="relation filter twin fixture")
    page = "---\ntype: insight\n---\n# {title}\n\n{body}\n"
    _write(
        vault,
        ROADMAP,
        page.format(
            title="Roadmap",
            body=f"## Relations\n\n- supersedes [[{PATTERNS}/Old roadmap]]",
        ),
    )
    _write(vault, REVIEW, page.format(title="Review", body="Two hires fit the budget."))
    _write(
        vault,
        BUDGET,
        page.format(title="Budget", body=f"## Relations\n\n- supersedes [[{OLD_BUDGET[:-3]}]]"),
    )
    _write(vault, OLD_BUDGET, page.format(title="Old budget", body="The budget is fixed."))
    _write(
        vault,
        MEMO,
        page.format(title="Memo", body=f"## Relations\n\n- supports [[{FINDINGS[:-3]}]]"),
    )
    _write(vault, FINDINGS, page.format(title="Findings", body="Hiring is on budget."))
    if withheld:
        _write(
            vault,
            OLD_ROADMAP,
            page.format(
                title="Old roadmap",
                body=f"## Relations\n\n- supports [[{REVIEW[:-3]}]]",
            ),
        )
    write_scope(vault)
    write_rule(vault, ceiling=egress.LEVEL_NONE, extra=rule_extra)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    _reset_caches()
    return vault


_PAGE_LEVEL_SEARCHES = {
    # The withheld page is the target of the roadmap's edge.
    "withheld-target": {"relations": ["supersedes"]},
    # The withheld page is the source of the edge that ends at the review.
    "withheld-source": {"relations": ["supports"]},
}
# An anchor on a visible page reads every edge that touches it, so its answer is the other end.
_ANCHORED_SEARCHES = {
    "anchored-on-the-roadmap": {"relation_of": ROADMAP},
    "anchored-on-the-review": {"relation_of": REVIEW},
}
_RELATION_SEARCHES = {**_PAGE_LEVEL_SEARCHES, **_ANCHORED_SEARCHES}
_SHARE_AN_EDGE_WITH_THE_WITHHELD_PAGE = {ROADMAP, REVIEW, OLD_ROADMAP}


def _search_as(audience: str, vault: Path, search: dict, *, ask: bool = False) -> dict | list:
    from test_governance_egress import _external

    from exomem import commands
    from exomem.governance import principal

    caller = _external() if audience == "external" else principal.owner_principal(surface="library")
    operation = commands.op_ask_memory if ask else commands.op_find
    with principal.request_scope(caller):
        return operation(vault, query="", mode="keyword", scope="kb-only", limit=15, **search)


def _hit_paths(reply: dict | list) -> set[str]:
    """The pages of a reply: `op_find` answers with its hits, `op_ask_memory` with an envelope."""
    return {hit["path"] for hit in (reply["hits"] if isinstance(reply, dict) else reply)}


@pytest.mark.parametrize("served", [False, True], ids=["scan", "served"])
@pytest.mark.parametrize("ask", [False, True], ids=["op_find", "op_ask_memory"])
@pytest.mark.parametrize("search", list(_PAGE_LEVEL_SEARCHES))
def test_a_restricted_caller_gets_the_reply_of_a_vault_that_never_had_the_withheld_page(
    tmp_path: Path, request: pytest.FixtureRequest, search: str, ask: bool, served: bool
) -> None:
    """Catches a relation filter that resolves its participants over the request's content
    admission alone: the roadmap, or the review, is returned only when the withheld page it
    shares an edge with exists, so the caller learns that the page exists. The served reader
    resolves links through the request's own checkpoint, so it is checked as well."""
    replies = []
    for withheld in (True, False):
        vault = _release_twin(tmp_path, withheld=withheld)
        if served:
            request.getfixturevalue("warm_managed_cell")(vault)
        replies.append(
            _search_as("external", vault, _RELATION_SEARCHES[search], ask=ask)
        )

    assert replies[0] == replies[1]
    assert _hit_paths(replies[0])


# The anchor is the page the rule withholds, so the answer is the pages around it.
_PURPOSE_SEARCHES = {
    **_PAGE_LEVEL_SEARCHES,
    "anchored-on-the-withheld-page": {"relation_of": OLD_ROADMAP},
}


@pytest.mark.parametrize("served", [False, True], ids=["scan", "served"])
@pytest.mark.parametrize("ask", [False, True], ids=["op_find", "op_ask_memory"])
@pytest.mark.parametrize("search", list(_PURPOSE_SEARCHES))
def test_a_declared_purpose_withholds_a_page_from_the_relation_filter_as_it_does_from_the_hits(
    tmp_path: Path, request: pytest.FixtureRequest, search: str, ask: bool, served: bool
) -> None:
    """Catches a relation filter that reads edges, or checks its anchor, without the purpose the
    search declares: a rule that withholds the page only for `marketing` leaves the roadmap, or
    the review, in the answer when the withheld page exists, so a caller that declares the
    purpose learns that the page exists. Undeclared, the rule withholds nothing, so the same
    vault answers differently, which shows the declared answer is not empty by accident."""
    declared, undeclared = [], []
    for withheld in (True, False):
        vault = _release_twin(tmp_path, withheld=withheld, rule_extra=_MARKETING_ONLY)
        if served:
            request.getfixturevalue("warm_managed_cell")(vault)
        marketing = {**_PURPOSE_SEARCHES[search], "purpose": "marketing"}
        declared.append(_search_as("external", vault, marketing, ask=ask))
        undeclared.append(_search_as("external", vault, _PURPOSE_SEARCHES[search], ask=ask))

    assert declared[0] == declared[1]
    assert _hit_paths(undeclared[0]) != _hit_paths(declared[0])
    if search != "anchored-on-the-withheld-page":
        # The visible budget pages answer every search not anchored on the withheld page, so an
        # empty declared answer would mean the purpose emptied the filter rather than released it.
        assert _hit_paths(declared[0])



def _answer_or_refusal(run) -> object:
    """A reply, or the refusal a caller receives instead, with the candidates it names."""
    from exomem import page_names

    try:
        return run()
    except page_names.AmbiguousTitle as refusal:
        return {"refused": refusal.message, "details": refusal.details}


@pytest.mark.parametrize("served", [False, True], ids=["scan", "served"])
@pytest.mark.parametrize("ask", [False, True], ids=["op_find", "op_ask_memory"])
def test_a_declared_purpose_resolves_a_relation_of_title_without_the_withheld_namesake(
    tmp_path: Path, request: pytest.FixtureRequest, ask: bool, served: bool
) -> None:
    """Catches `relation_of` resolved by title before the declared purpose applies: a visible page
    shares its title with a page the rule withholds only for `marketing`, so the title is ambiguous
    and the refusal names the withheld page's id and title, where a vault that never had that page
    answers with the visible one. Undeclared, the rule withholds nothing, so the same title is
    ambiguous: that shows the namesake decides the declared answer."""
    from test_governance_egress import _reset_caches

    from exomem import epistemic_graph

    namesake = f"{NOTES}/Old roadmap.md"
    search = {"relation_of": "Old roadmap"}
    declared, undeclared = [], []
    for withheld in (True, False):
        vault = _release_twin(tmp_path, withheld=withheld, rule_extra=_MARKETING_ONLY)
        _write(vault, namesake, f"---\ntype: insight\n---\n# Old roadmap\n\n## Relations\n\n- supports [[{FINDINGS[:-3]}]]\n")
        epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
        _reset_caches()
        if served:
            request.getfixturevalue("warm_managed_cell")(vault)
        declared.append(_answer_or_refusal(lambda vault=vault: _search_as("external", vault, {**search, "purpose": "marketing"}, ask=ask)))
        undeclared.append(_answer_or_refusal(lambda vault=vault: _search_as("external", vault, search, ask=ask)))

    assert declared[0] == declared[1]
    assert _hit_paths(declared[0]) == {FINDINGS}
    assert "refused" in undeclared[0]


@pytest.mark.parametrize("search", list(_RELATION_SEARCHES))
def test_a_restricted_callers_relation_hits_never_name_the_withheld_page(
    tmp_path: Path, search: str
) -> None:
    """Catches the participant set itself: `find` is the layer under the release gate, and over
    the content admission alone it returns the withheld page, with its path in `relation_match`,
    where a vault that never had the page returns nothing."""
    from test_governance_egress import _external

    from exomem.governance import principal

    hits = []
    for withheld in (True, False):
        vault = _release_twin(tmp_path, withheld=withheld)
        with principal.request_scope(_external()):
            found = find_module.find(vault, query="", limit=15, **_RELATION_SEARCHES[search])
        hits.append([hit.as_dict() for hit in found])

    assert hits[0] == hits[1]


@pytest.mark.parametrize("search", list(_RELATION_SEARCHES))
def test_the_owner_reads_the_edges_that_the_restricted_caller_reads_as_absent(
    tmp_path: Path, search: str
) -> None:
    """Catches a twin that passes by returning nothing: the owner of the same vault is returned
    the pages that share an edge with the withheld page, and the restricted caller none."""
    vault = _release_twin(tmp_path, withheld=True)
    expected = {
        # The anchor is not its own answer, so an anchored search returns the other end.
        "anchored-on-the-roadmap": {OLD_ROADMAP},
        "anchored-on-the-review": {OLD_ROADMAP},
        "withheld-target": {ROADMAP, OLD_ROADMAP},
        "withheld-source": {REVIEW, OLD_ROADMAP},
    }[search]

    owner = _hit_paths(_search_as("owner", vault, _RELATION_SEARCHES[search]))
    external = _hit_paths(_search_as("external", vault, _RELATION_SEARCHES[search]))

    assert expected <= owner
    assert not external & _SHARE_AN_EDGE_WITH_THE_WITHHELD_PAGE


def test_a_restricted_caller_still_filters_by_the_edges_between_visible_pages(
    tmp_path: Path,
) -> None:
    """Catches a fix that withholds every edge from a restricted caller: the budget pair still
    answers `supersedes` and the memo pair `supports`, as in a vault without the withheld page."""
    vault = _release_twin(tmp_path, withheld=True)

    supersedes = _search_as("external", vault, {"relations": ["supersedes"]})
    supports = _search_as("external", vault, {"relations": ["supports"]})

    assert _hit_paths(supersedes) == {BUDGET, OLD_BUDGET}
    assert _hit_paths(supports) == {MEMO, FINDINGS}
