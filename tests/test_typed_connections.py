"""The typed-edge recall change: connections rank by registry behaviour, carry a `why`,
and never name a page the caller may not see.

The earlier link list served a relation and a title with no reason and read only
outbound edges; these checks name what only the shared `read_connections` path
proves: an inbound `replaces` edge outranks the seed's own links, an invented
vault relation ranks by its behaviours rather than its key, a line that also
links a withheld page is never quoted, and a served record keeps only
relation, ref and tier.
"""

from pathlib import Path

import pytest
import yaml
from test_activate_context_maps import _activate
from test_context_catalogue import INSIGHTS, _built, _page
from test_task_context_links import _discover, _discover_packet, _lead, _linked

from exomem import (
    context_roles,
    context_views,
    epistemic_graph,
    find,
    lexstore,
    served_context,
    working_set,
)
from exomem import vault as vault_module
from exomem.governance import egress


def _warm_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the why path the borrowed resolver a warm deployment already has."""
    monkeypatch.setattr(
        find, "borrow_query_resolver",
        lambda root, visible=None, **kwargs: find._get_query_resolver(root),
    )

TURN = "Inspect the optical alignment before the workshop"
LEAD_BODY = "Optical alignment workshop: the calibration appointment remains undecided.\n"
UNRELATED = "Notes on a different subject entirely.\n"


def _write(vault: Path, rel: str, text: str) -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _registry_overlay(vault: Path, extensions: dict) -> None:
    _write(
        vault,
        "Knowledge Base/_Schema/relation-registry.yaml",
        yaml.safe_dump({"schema_version": 1, "extensions": extensions}, sort_keys=False),
    )


def test_an_inbound_replaces_edge_ranks_before_the_pages_own_links(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A page superseded by a newer one names that replacement first; its `why` is the newer page's prose."""
    for index in range(3):
        _linked(vault, f"reference-{index}")
    lead = _page(
        vault, "alignment-visit",
        body=LEAD_BODY + "\n" + "\n".join(
            f"- [[Knowledge Base/Notes/Insights/reference-{index}]]" for index in range(3)
        ) + "\n",
    )
    _page(
        vault, "alignment-visit-revised",
        body=f"The revised visit report.\n\n- supersedes [[{lead.removesuffix('.md')}]] after the review board\n",
    )
    _warm_resolver(monkeypatch)
    task = _discover(vault, monkeypatch)
    first = _lead(task, lead)["links"][0]
    assert first["relation"] == "supersedes"
    assert first["direction"] == "inbound"
    assert first["tier"] == -1
    # The authoring line carries prose beyond the relation and link, so it is quoted with the
    # link shown as the lead's title.
    assert first["why"] == "supersedes (inbound): supersedes alignment-visit after the review board"


def test_invented_vault_relations_rank_by_behaviour_not_key(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A vault extension's behaviours set its tier: `replaces` leads, `associative` trails, whatever the key."""
    _registry_overlay(vault, {
        # Neither key names a behaviour; only the declared parents do.
        "lab.revision": {"parent": "supersedes", "description": "A later revision"},
        "lab.thread": {"parent": "links_to", "description": "A loose thread"},
    })
    replacement = _linked(vault, "replacement-note")
    loose = _linked(vault, "loose-note")
    lead = _page(vault, "alignment-visit", body=(
        f"{LEAD_BODY}\n- lab.thread [[{loose.removesuffix('.md')}]]\n- lab.revision [[{replacement.removesuffix('.md')}]]\n"
    ))
    task = _discover(vault, monkeypatch)
    links = _lead(task, lead)["links"]
    assert [(link["relation"], link["tier"]) for link in links] == [("lab.revision", 0), ("lab.thread", 3)]


def test_an_open_planning_target_ranks_first_inside_its_tier(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Between two plain links the one whose target still plans openly comes first."""
    settled = _page(vault, "settled-note", "status: completed\n", body=UNRELATED)
    _page(vault, "open-plan", body=UNRELATED)
    open_plan = f"{INSIGHTS}/open-plan.md"
    lead = _page(vault, "alignment-visit", body=(
        f"{LEAD_BODY}\n- [[{settled.removesuffix('.md')}]]\n- [[{open_plan.removesuffix('.md')}]]\n"
    ))
    task = _discover(vault, monkeypatch)
    links = _lead(task, lead)["links"]
    assert links[0]["target_ref"] == open_plan and links[0]["tier"] == 3
    assert links[1]["target_ref"] == settled


def test_a_line_that_also_links_a_withheld_page_is_never_quoted(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The connection to a visible page falls back to its lede rather than quote a line naming a withheld one."""
    hidden = _linked(vault, "restricted-reference")
    shown = _linked(vault, "visible-reference")
    lead = _page(vault, "alignment-visit", body=(
        f"{LEAD_BODY}\nBoth [[{hidden.removesuffix('.md')}]] and [[{shown.removesuffix('.md')}]] apply here.\n"
    ))
    real = egress.release_walk_filter

    def refuse_hidden(*args, **kwargs):
        inner = real(*args, **kwargs)
        return lambda path: path != hidden and (inner is None or inner(path))

    monkeypatch.setattr(egress, "release_walk_filter", refuse_hidden)
    task = _discover(vault, monkeypatch)
    links = _lead(task, lead)["links"]
    assert [link["target_ref"] for link in links] == [shown]
    assert links[0]["why"] == f"links_to (outbound): {UNRELATED.strip()}"
    assert "restricted" not in str(task)


def test_a_lede_reason_shows_link_names_and_never_a_withheld_one(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare relation bullet takes the target's lede; its links read as names, and a withheld one drops the lede."""
    _warm_resolver(monkeypatch)
    hidden = _linked(vault, "restricted-reference")
    shown = _linked(vault, "field-log")
    summary = _page(vault, "survey-summary", body=f"Compiled from [[{shown.removesuffix('.md')}]] readings.\n")
    sealed = _page(vault, "sealed-summary", body=f"Compiled from [[{hidden.removesuffix('.md')}]] readings.\n")
    lead = _page(vault, "alignment-visit", body=(
        f"{LEAD_BODY}\n## Relations\n\n- derived_from [[{summary.removesuffix('.md')}]]\n"
        f"- derived_from [[{sealed.removesuffix('.md')}]]\n"
    ))
    real = egress.release_walk_filter

    def refuse_hidden(*args, **kwargs):
        inner = real(*args, **kwargs)
        return lambda path: path != hidden and (inner is None or inner(path))

    monkeypatch.setattr(egress, "release_walk_filter", refuse_hidden)
    whys = {link["target_ref"]: link["why"] for link in _lead(_discover(vault, monkeypatch), lead)["links"]}
    assert whys[summary] == "derived_from (outbound): Compiled from field-log readings."
    assert whys[sealed] == "derived_from (outbound)"


@pytest.mark.parametrize(
    ("authored", "lede", "why"),
    [
        pytest.param(
            "## Relations\n\n- derived_from [[{source}]]", "Run `[[No Such Page]]` before the visit.",
            "derived_from (outbound)", id="a link in a code span of the lede",
        ),
        pytest.param(
            "## Relations\n\n- derived_from [[{source}]]",
            "See [[" + "Restricted reference with a very long working title " * 5 + "]] for the schedule.",
            "derived_from (outbound)", id="a link the lede cut leaves open",
        ),
        pytest.param(
            "Run `[[Hidden Page]]` first, then read [[{source}]].", UNRELATED.strip(),
            f"links_to (outbound): {UNRELATED.strip()}", id="a link in a code span of the quoted line",
        ),
    ],
)
def test_a_reason_never_quotes_a_link_the_display_pass_cannot_read(
    vault: Path, monkeypatch: pytest.MonkeyPatch, authored: str, lede: str, why: str
) -> None:
    """A code-span link, or a long link that the lede's 240-character cut leaves open, keeps its raw name.

    A quoted line that holds one gives its place to the lede; a lede that holds one leaves the bare relation.
    """
    _warm_resolver(monkeypatch)
    source = _page(vault, "survey-summary", body=f"{lede}\n")
    lead = _page(vault, "alignment-visit", body=f"{LEAD_BODY}\n{authored.format(source=source.removesuffix('.md'))}\n")
    links = _lead(_discover(vault, monkeypatch), lead)["links"]
    assert [link["why"] for link in links if link["target_ref"] == source] == [why]


def test_a_lead_admitted_alone_counts_the_connections_it_lost(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When only the lead fits, it serves without connections and counts every one it omitted."""
    targets = [_linked(vault, f"reference-{index}") for index in range(3)]
    lead = _page(vault, "alignment-visit", body=(
        LEAD_BODY + "\n" + "\n".join(f"- [[{t.removesuffix('.md')}]]" for t in targets) + "\n"
    ))
    # A second lead takes just enough room that the first's link cannot join it.
    _page(vault, "alignment-workshop", body="The optical alignment workshop is on Friday, before the visit.\n")
    packet = _discover_packet(vault, monkeypatch, max_chars=700)
    task = packet["task_context"]
    item = _lead(task, lead)
    assert "links" not in item and item["links_omitted"] == 3
    assert packet["budget"]["used_chars"] <= packet["budget"]["limit_chars"]


def test_the_served_record_keeps_relation_ref_and_tier_only(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The record credits a later read to an offered link without retaining its title or why."""
    report = _linked(vault, "calibration-report")
    plain = _linked(vault, "site-notes")
    lead = _page(vault, "alignment-visit", body=(
        f"{LEAD_BODY}\n- links_to [[{plain.removesuffix('.md')}]]\n- evidenced_by [[{report.removesuffix('.md')}]]\n"
    ))
    packet = _discover_packet(vault, monkeypatch)
    record = served_context.metadata(packet)
    item = next(route for route in record["task_context"]["candidates"] if route["source_ref"] == lead)
    assert item["links"] == [
        {"relation": "evidenced_by", "target_ref": report, "tier": 1},
        {"relation": "links_to", "target_ref": plain, "tier": 3},
    ]


def test_every_tier_behaviour_is_one_the_registry_declares() -> None:
    """A misspelt behaviour would silently never match and drop its relations to the last tier."""
    from exomem import relation_registry

    tiers = (
        working_set._CONNECTION_TIER_ZERO_BEHAVIOURS | working_set._CONNECTION_LAST_TIER_BEHAVIOURS
        | {working_set._CONNECTION_TIER_ONE_BEHAVIOUR, working_set._CONNECTION_INBOUND_REPLACES_BEHAVIOUR}
    )
    assert tiers <= relation_registry.RELATION_BEHAVIOURS


def test_the_connections_role_is_the_graph_lane_default_for_every_anchor_kind() -> None:
    """The shipped registry maps `connections` to the graph lane for every anchor kind it documents."""
    role = context_roles.load_roles().roles["connections"]
    assert role.lane == "graph"
    assert role.anchor_defaults == frozenset(context_roles.ANCHOR_KINDS)


def test_graph_lane_surfaces_ranked_connections_as_pointers(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`_graph_lane` emits one pointer per ranked connection and never lets a withheld target take a slot."""
    anchor = _page(vault, "Harbour Crane", body="The harbour crane handles the heavy lifts.\n")
    note = _page(vault, "crane-note", body=f"{UNRELATED}\n- refines [[{anchor.removesuffix('.md')}]]\n")
    hidden = _page(vault, "crane-secret", body=f"{UNRELATED}\n- links_to [[{anchor.removesuffix('.md')}]]\n")
    _built(vault).close()
    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()
    role = context_roles.load_roles().roles["connections"]
    resolved = type("Anchor", (), {"status": "resolved", "path": anchor, "ref": anchor})()
    items = working_set._graph_lane(
        vault, role, anchors=[resolved],
        visible=lambda path: path != hidden,
    )
    assert [item.path for item in items] == [note]
    assert items[0].ref == note and items[0].why.startswith("refines (inbound)")
    assert items[0].provenance == {"source": "graph"}


def test_recall_profile_neighbourhood_includes_plain_links(vault: Path) -> None:
    """Under `recall` an anchor's `links_to` neighbour joins the graph neighbourhood the unit lanes read."""
    anchor = _page(vault, "Harbour Crane", body=(
        "The harbour crane handles the heavy lifts.\n\n- links_to [[Knowledge Base/Notes/Insights/crane-manual]]\n"
    ))
    _page(vault, "crane-manual", body=UNRELATED)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    neighbourhood = working_set._graph_neighbours(vault, anchor, depth=2)
    assert f"{INSIGHTS}/crane-manual.md" in neighbourhood


def test_recall_traverse_link_and_source_edges_but_not_implements(vault: Path) -> None:
    """`graph_context` under `recall` follows `derived_from` and `links_to`; the `implements` edge stays out."""
    note = _page(vault, "field-note", body=(
        "A note.\n\n- derived_from [[Knowledge Base/Notes/Insights/field-source]]\n"
        "- links_to [[Knowledge Base/Notes/Insights/field-other]]\n"
    ))
    _page(vault, "field-source", body=UNRELATED)
    _page(vault, "field-other", body=UNRELATED)
    _page(vault, "implemented-thing", body=f"{UNRELATED}\n- implements [[{note.removesuffix('.md')}]]\n")
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    context = epistemic_graph.graph_context(vault, path=note, depth=1, traversal_profile="recall")
    assert context["available"] and context["profile"]["name"] == "recall"
    # Every wikilink is also a plain-link edge, so the implementing page reaches the note through
    # `links_to`; the traversal must not carry the operational `implements` edge itself.
    assert {edge["relation_type"] for edge in context["edges"]} >= {"derived_from", "links_to"}
    assert "implements" not in {edge["relation_type"] for edge in context["edges"]}
    assert "implementation" not in context["included_relation_families"]


def test_a_quoted_line_names_every_link_it_carries(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A line holding two visible links shows both titles in the `why`, not an empty gap."""
    first = _linked(vault, "calibration-report")
    second = _linked(vault, "site-notes")
    lead = _page(vault, "alignment-visit", body=(
        f"{LEAD_BODY}\nBoth [[{first.removesuffix('.md')}]] and [[{second.removesuffix('.md')}]] apply here.\n"
    ))
    _warm_resolver(monkeypatch)
    task = _discover(vault, monkeypatch)
    links = _lead(task, lead)["links"]
    assert {link["target_ref"] for link in links} == {first, second}
    for link in links:
        assert link["why"] == "links_to (outbound): Both calibration-report and site-notes apply here."
        assert "calibration-report" in link["why"] and "site-notes" in link["why"]


def test_a_missing_resolver_never_builds_one_for_a_why(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """On a resolver miss the why falls back to the lede or bare relation; no resolver is built."""
    target = _linked(vault, "calibration-report")
    lead = _page(vault, "alignment-visit", body=(
        f"{LEAD_BODY}\nSee [[{target.removesuffix('.md')}]] for details.\n"
    ))
    monkeypatch.setattr(find, "borrow_query_resolver", lambda *args, **kwargs: None)

    def _refuse(self, *args, **kwargs):
        raise AssertionError("activation built a WikilinkResolver")

    monkeypatch.setattr(vault_module.WikilinkResolver, "__init__", _refuse)
    task = _discover(vault, monkeypatch)
    # The quoted line is unreachable without a resolver, so the target's lede stands.
    assert _lead(task, lead)["links"][0]["why"] == f"links_to (outbound): {UNRELATED.strip()}"


def test_a_spent_connection_budget_still_names_the_relation(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With the why budget exhausted the connection still serves, its reason reduced to relation and direction."""
    target = _linked(vault, "calibration-report")
    lead = _page(vault, "alignment-visit", body=f"{LEAD_BODY}\n- [[{target.removesuffix('.md')}]]\n")
    real = working_set.budget_exhausted
    monkeypatch.setattr(
        working_set, "budget_exhausted",
        lambda stage, **kwargs: stage == "working_set.connection_why" or real(stage, **kwargs),
    )
    task = _discover(vault, monkeypatch)
    assert _lead(task, lead)["links"][0]["why"] == "links_to (outbound)"


def test_each_served_lead_keeps_its_first_connection_until_room_runs_out(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Leads are admitted with their first connection; the lead no room holds is cut, and methods are never displaced."""
    targets = [_linked(vault, f"reference-{index}") for index in range(3)]
    for index, target in enumerate(targets):
        _page(vault, f"alignment-{index}", body=(
            f"{LEAD_BODY}\n- [technique] Inspect the optical alignment. ^step{index}\n"
            f"- [[{target.removesuffix('.md')}]]\n"
        ))
    packet = _discover_packet(vault, monkeypatch, max_chars=1000)
    task = packet["task_context"]
    served = task["candidates"]
    assert 0 < len(served) < 3
    assert all(len(item["links"]) == 1 and "links_omitted" not in item for item in served)
    assert len(task["methods"]["items"]) == len(served)
    assert {"role": "task_context", "reason": "budget"} in task["missing"]
    assert packet["budget"]["used_chars"] <= packet["budget"]["limit_chars"]


def test_a_reason_is_built_only_for_a_connection_the_allocation_tries(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reason-building reads pages, so a tight packet builds about one reason per lead, not five."""
    leads = []
    for lead_index in range(3):
        targets = [_linked(vault, f"reference-{lead_index}-{index}") for index in range(5)]
        leads.append(_page(vault, f"alignment-visit-{lead_index}", body=(
            LEAD_BODY + "\n" + "\n".join(f"- [[{t.removesuffix('.md')}]]" for t in targets) + "\n"
        )))
    builds: list[str] = []
    real = working_set._connection_why

    def counted(*args, **kwargs):
        builds.append(kwargs["target_rel"])
        return real(*args, **kwargs)

    monkeypatch.setattr(working_set, "_connection_why", counted)
    task = _discover_packet(vault, monkeypatch, max_chars=1500)["task_context"]
    served = sum(len(lead.get("links", ())) for lead in task["candidates"])
    assert len(task["candidates"]) == len(leads) and 0 < served < 5 * len(leads)
    assert len(builds) <= served + len(leads)


def test_an_unreadable_reader_view_keeps_the_leads_and_reports_the_links_lane(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A reason that cannot be read for the caller lists no link; the lead still serves and the gap is named."""
    target = _linked(vault, "calibration-report")
    lead = _page(vault, "alignment-visit", body=f"{LEAD_BODY}\n- [[{target.removesuffix('.md')}]]\n")

    def unreadable(*args, **kwargs):
        raise egress.ReaderViewUnavailable("the release plane could not decide a unit")

    monkeypatch.setattr(working_set, "_connection_why", unreadable)
    task = _discover(vault, monkeypatch)
    assert "links" not in _lead(task, lead)
    assert {"role": "task_context", "reason": "lane_failed"} in task["missing"]


def test_a_leads_own_method_is_never_cut_while_its_link_is_served(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Across tight allowances the lead's method outranks the lead's first link."""
    target = _linked(vault, "reference-card")
    _page(vault, "alignment-visit", body=(
        f"{LEAD_BODY}\n- [technique] Inspect the optical alignment. ^step\n- [[{target.removesuffix('.md')}]]\n"
    ))
    for max_chars in range(700, 1101, 50):
        task = _discover_packet(vault, monkeypatch, max_chars=max_chars)["task_context"]
        for lead in task["candidates"]:
            assert not (lead.get("links") and not task["methods"]["items"]), max_chars


def test_the_default_recall_budget_is_six_thousand_with_a_half_turn_slice(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A default request reports a 6,000-char limit; with map and task context populated the slice holds back half."""
    _page(vault, "alignment-visit", body=LEAD_BODY)
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    monkeypatch.setenv("EXOMEM_ACTIVATION_TURN_SLICE", "on")
    _built(vault).close()
    lexstore.ensure_fresh(vault)
    packet = _activate(vault, turn=TURN)
    assert packet["budget"]["limit_chars"] == 6000
    assert packet["map"]["items"] and packet["task_context"]["candidates"]
    assert context_views._slice_reserve(6000) == int(6000 * context_views.TURN_SLICE_SHARE) == 3000


def test_an_inbound_reason_withholds_the_authoring_pages_origin_carrier(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The lede that stands in for a bare inbound relation is the other page's, so its carrier is withheld.

    A source page keeps its own carriers, so judging the other page's lede as the seed's
    served a restricted reader that page's origin carrier inside the reason.
    """
    seed = _page(vault, "visit-capture", body=LEAD_BODY, folder="Knowledge Base/Sources/Articles")
    _page(vault, "visit-report", body=(
        "<!-- exomem-origin withheld-input-reference -->\n\nThe revised visit report.\n\n"
        f"- supersedes [[{seed.removesuffix('.md')}]]\n"
    ))
    catalogue = _built(vault)
    by_path = {entry.path: entry for entry in catalogue.task_entries() if entry.path}
    catalogue.close()
    _warm_resolver(monkeypatch)
    # A vault with a configured audience: every caller but the owner gets withheld prose.
    monkeypatch.setattr(egress, "projects_origin_for_caller", lambda *args, **kwargs: True)

    served = working_set.read_connections(vault, [seed], by_path=by_path, visible=None, per_seed=4)

    listed, _omitted = served[seed]
    inbound = next(connection for connection in listed if connection.direction == "inbound")
    assert "The revised visit report" in inbound.why
    assert "exomem-origin" not in inbound.why


def test_a_line_inside_an_origin_carrier_is_never_quoted_as_a_reason(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A carrier that spans lines can hold a line linking the seed; that line is withheld, not quoted."""
    seed = _page(vault, "visit-capture", body=LEAD_BODY)
    _page(vault, "visit-report", body=(
        "The revised visit report.\n\n<!-- exomem-origin\n"
        f"confidential interview transcript about [[{seed.removesuffix('.md')}]]\n-->\n\n"
        f"- supersedes [[{seed.removesuffix('.md')}]]\n"
    ))
    catalogue = _built(vault)
    by_path = {entry.path: entry for entry in catalogue.task_entries() if entry.path}
    catalogue.close()
    _warm_resolver(monkeypatch)
    monkeypatch.setattr(egress, "projects_origin_for_caller", lambda *args, **kwargs: True)

    served = working_set.read_connections(vault, [seed], by_path=by_path, visible=None, per_seed=4)

    listed, _omitted = served[seed]
    reasons = " ".join(connection.why for connection in listed)
    assert "confidential interview transcript" not in reasons
