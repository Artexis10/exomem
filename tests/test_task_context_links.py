"""Task discovery lists each lead's outbound links, so the agent chooses what to follow.

A lead that links elsewhere used to pull those neighbours in as leads of their own, which agents then read
far less often than the leads text recall ranked. The checks below name what only a real vault and graph
catch: a neighbour that becomes a lead again, a plain link listed ahead of a typed relation, a page the
caller may not see named in a link, and a graph failure that silently serves unlinked leads as complete.
"""

from pathlib import Path

import pytest
from test_activate_context_maps import _activate
from test_context_catalogue import _built, _page

from exomem import epistemic_graph, lexstore
from exomem.governance import egress

TURN = "Inspect the optical alignment before the workshop"
LEAD_BODY = "Optical alignment workshop: the calibration appointment remains undecided.\n"
UNRELATED = "Notes on a different subject entirely.\n"
REPORT_ID = "6a0f9c3e-4b1d-4d7e-8f21-3c5e7a9b1d02"


def _linked(vault: Path, name: str, *, frontmatter: str = "") -> str:
    return _page(vault, name, frontmatter, body=UNRELATED)


def _discover_packet(vault: Path, monkeypatch: pytest.MonkeyPatch, *, max_chars: int = 8000, turn_slice: str = "off") -> dict:
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    # The turn slice drops a lead it already carries; with it off, the task context shows discovery itself.
    monkeypatch.setenv("EXOMEM_ACTIVATION_TURN_SLICE", turn_slice)
    _built(vault).close()
    lexstore.ensure_fresh(vault)
    return _activate(vault, turn=TURN, max_chars=max_chars)


def _discover(vault: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    return _discover_packet(vault, monkeypatch)["task_context"]


def _lead(task: dict, path: str) -> dict:
    return next(item for item in task["candidates"] if item["source_ref"] == path)


def test_a_lead_lists_typed_links_first_and_adds_no_neighbour_as_a_lead(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The plain link is authored first, yet the typed relation leads the list once; the neighbours stay links."""
    report = _linked(vault, "calibration-report", frontmatter=f"exomem_id: {REPORT_ID}\n")
    plain = _linked(vault, "site-notes")
    lead = _page(vault, "alignment-visit", body=(
        f"{LEAD_BODY}\nThe report is [[{report.removesuffix('.md')}]].\n\n"
        f"## Relations\n\n- links_to [[{plain.removesuffix('.md')}]]\n- evidenced_by [[{report.removesuffix('.md')}]]\n"
    ))
    # An inbound edge is the other page's link, not the lead's; listing it would reverse the relation.
    _page(vault, "inbound-note", body=f"{UNRELATED}\nSee [[{lead.removesuffix('.md')}]].\n")
    task = _discover(vault, monkeypatch)
    links = _lead(task, lead)["links"]
    assert links == [
        {"relation": "evidenced_by", "target_ref": f"exomem://memory/{REPORT_ID}", "title": "calibration-report"},
        {"relation": "links_to", "target_ref": plain, "title": "site-notes"},
    ]
    assert "links_omitted" not in _lead(task, lead)
    # The old fixed expansion made each neighbour a lead tagged `graph`.
    assert {report, plain}.isdisjoint(item["source_ref"] for item in task["candidates"])
    assert all("graph" not in item["origins"] for item in task["candidates"])


def test_a_lead_with_more_links_than_five_counts_the_rest(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A heavily linked page lists five links and says how many it left out, never a silent cut."""
    targets = [_linked(vault, f"reference-{index}") for index in range(7)]
    lead = _page(vault, "alignment-visit", body=LEAD_BODY + "\n" + "\n".join(f"- [[{t.removesuffix('.md')}]]" for t in targets) + "\n")
    task = _discover(vault, monkeypatch)
    item = _lead(task, lead)
    assert len(item["links"]) == 5
    assert item["links_omitted"] == 2
    assert {link["target_ref"] for link in item["links"]} <= set(targets)


def test_links_spend_only_the_allowance_that_every_lead_leaves(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A packet that cannot hold every link still serves both leads; the first lead counts the links it lost."""
    targets = [_linked(vault, f"reference-{index}") for index in range(5)]
    linked = _page(vault, "alignment-visit", body=LEAD_BODY + "\n" + "\n".join(f"- [[{t.removesuffix('.md')}]]" for t in targets) + "\n")
    plain = _page(vault, "alignment-workshop", body="The optical alignment workshop is on Friday, before the visit.\n")
    # Too small for the five links of the first lead beside the second lead, big enough for both leads.
    packet = _discover_packet(vault, monkeypatch, max_chars=1800)
    task = packet["task_context"]
    assert {linked, plain} <= {item["source_ref"] for item in task["candidates"]}
    item = _lead(task, linked)
    assert len(item.get("links", ())) + item["links_omitted"] == 5 and item["links_omitted"] >= 1
    assert packet["budget"]["used_chars"] <= packet["budget"]["limit_chars"]


def test_a_link_to_a_page_the_caller_may_not_see_is_not_listed(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A release decision that refuses a linked page keeps its title and ref out of the lead's links."""
    hidden = _linked(vault, "restricted-reference")
    shown = _linked(vault, "visible-reference")
    lead = _page(vault, "alignment-visit", body=(
        f"{LEAD_BODY}\n- [[{hidden.removesuffix('.md')}]]\n- [[{shown.removesuffix('.md')}]]\n"
    ))
    real = egress.release_walk_filter

    def refuse_hidden(*args, **kwargs):
        inner = real(*args, **kwargs)
        return lambda path: path != hidden and (inner is None or inner(path))

    monkeypatch.setattr(egress, "release_walk_filter", refuse_hidden)
    task = _discover(vault, monkeypatch)
    assert [link["target_ref"] for link in _lead(task, lead)["links"]] == [shown]
    assert "restricted-reference" not in str(task)


def test_unavailable_graph_keeps_the_leads_and_says_links_are_missing(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Leads served without links must not read as a lead set that has none."""
    target = _linked(vault, "calibration-report")
    lead = _page(vault, "alignment-visit", body=f"{LEAD_BODY}\n- [[{target.removesuffix('.md')}]]\n")
    monkeypatch.setattr(epistemic_graph.EpistemicGraphIndex, "available", lambda self: False)
    task = _discover(vault, monkeypatch)
    assert "links" not in _lead(task, lead)
    assert task["state"] == "partial"
    assert {"role": "task_context", "reason": "lane_failed"} in task["missing"]


def test_a_page_the_turn_names_still_lists_its_links(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The turn slice carries the named page without links, so discovery must still list where it leads."""
    targets = [_linked(vault, f"reference-{index}") for index in range(3)]
    lead = _page(vault, "optical-alignment-workshop", body=LEAD_BODY + "\n" + "\n".join(f"- [[{t.removesuffix('.md')}]]" for t in targets) + "\n")
    packet = _discover_packet(vault, monkeypatch, turn_slice="on")
    assert lead in str(packet["turn_slice"])
    assert {link["target_ref"] for link in _lead(packet["task_context"], lead)["links"]} == set(targets)
