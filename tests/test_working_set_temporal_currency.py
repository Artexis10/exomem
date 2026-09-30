"""Activation temporal currency, from AUTHORED signals only.

A served unit carries the date its author wrote in its context slot, never a
date its prose mentions; a unit is history only when an authored supersession
says so; a resolved anchor serves a current-state page only when its own page
declares one; and nothing internal (a relation target, a page time) is
published. Every fixture is invented, and the adversarial shapes the review
found (deadline dates, undated units, unrelated look-alikes, lower-case
spellings) are pinned here.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import context_roles, lexstore, working_set, working_set_currency, working_set_state
from exomem._hooks import exomem_retrieve_nudge as hook

PAGE = "Knowledge Base/Notes/relay-trial.md"


def _item(ref, text, *, role="recent_change", category="observation", kind="observation",
          updated="", lifecycle="active", path=PAGE, provenance=None, level="unit"):
    return working_set.LaneItem(
        role=role, level=level, ref=ref, path=path, title="Relay trial", text=text,
        lifecycle=lifecycle, updated=updated, anchor=path,
        provenance={"category": category, "kind": kind, **(provenance or {})},
    )


def _packet(items, *, max_chars=4000, current_state=()):
    return working_set.build_packet(
        items=tuple(items), anchors=(), roles=(), current_state=tuple(current_state),
        ambiguity=(), missing=(), max_chars=max_chars,
        generation={"freshness_key": "k", "index_generation": 1, "roles_hash": "a",
                    "roles_source": "shipped"},
        status="resolved",
    )


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _lane_items(root: Path, rel: str, *roles: str) -> list:
    registry = context_roles.load_roles().roles
    out = []
    for role in roles:
        out.extend(
            working_set._units_lane(root, registry[role], neighbourhood=frozenset({rel})).items
        )
    return out


# Rule 1: a unit's own time comes from its context slot only -------------------


@pytest.mark.parametrize(
    "context",
    [
        None,
        "",
        "due 2027-01-15",
        "Due 2027-01-15",
        "expires on 2027-01-15",
        "renew before 2027-01-15",
        "deadline: 2027-01-15",
        "by 2027-01-15",
        "valid until 2027-01-15",
        "check by 2027-01-15",
        "2027-13-45",
    ],
)
def test_own_time_never_reads_a_deadline_or_a_non_date(context):
    assert working_set_currency.own_time(context) == ""


@pytest.mark.parametrize(
    ("context", "expected"),
    [
        ("2026-01-10", "2026-01-10"),
        ("decision on 2026-03-04", "2026-03-04"),
        ("measured 2026-02-01, retest due 2026-05-01", "2026-02-01"),
        ("due 2026-05-01; measured 2026-02-01", "2026-02-01"),
    ],
)
def test_own_time_reads_the_authored_context_date(context, expected):
    assert working_set_currency.own_time(context) == expected


def test_a_date_mentioned_in_the_prose_is_not_the_units_time(tmp_path):
    """Reviewer probe P1: a renewal deadline in the text became the unit's time."""
    _write(tmp_path, PAGE, (
        "---\ntype: insight\ntitle: Relay trial\nupdated: 2026-09-20\n---\n# Relay trial\n\n"
        "- [decision] Renew the gateway TLS certificate before it expires on 2027-01-15 ^renew\n"
        "- [decision] renewed the gateway tls certificate on 2026-10-02 ^renewed\n"
        "- [decision] Chose transport B for the relay (decision on 2026-03-04) ^chose-b\n"
    ))
    lexstore.ensure_fresh(tmp_path)
    by_anchor = {i.ref.rsplit("#", 1)[1]: i for i in _lane_items(tmp_path, PAGE, "recent_change")}
    assert by_anchor["renew"].updated == ""
    assert by_anchor["renewed"].updated == ""
    assert by_anchor["chose-b"].updated == "2026-03-04"


def test_undated_units_order_by_their_page_time_not_their_ref():
    newer_page = _item("a-older", "Undated unit on a recently edited page",
                       path="Knowledge Base/Notes/b.md", provenance={"page_updated": "2026-09-20"})
    older_page = _item("b-newer", "Undated unit on an old page",
                       path="Knowledge Base/Notes/a.md", provenance={"page_updated": "2026-01-02"})
    order = [u["ref"] for u in _packet([older_page, newer_page])["units"]]
    assert order == ["a-older", "b-newer"]
    reversed_refs = [
        _item("z", "Undated unit on a recently edited page", path="Knowledge Base/Notes/b.md",
              provenance={"page_updated": "2026-09-20"}),
        _item("a", "Undated unit on an old page", path="Knowledge Base/Notes/a.md",
              provenance={"page_updated": "2026-01-02"}),
    ]
    assert [u["ref"] for u in _packet(reversed_refs)["units"]] == ["z", "a"]


# Rule 2: history only from an authored supersession --------------------------


def test_a_dated_decision_does_not_make_an_unrelated_older_fact_history():
    """Reviewer probe P2: the date-only rule had no subject check."""
    fact = _item("fact", "Production database password was rotated", category="fact",
                 kind="fact", updated="2026-08-01")
    dec = _item("dec", "decided to adopt the new logo colours", category="decision",
                kind="decision", updated="2026-09-01")
    units = {u["ref"]: u for u in _packet([fact, dec])["units"]}
    assert "history" not in units["fact"]
    assert units["fact"]["lifecycle"] == "active"


@pytest.mark.parametrize("lifecycle", ["draft", "planned", "dropped"])
def test_a_draft_or_planned_page_is_not_history(lifecycle):
    """Reviewer probe P6 (LOW-8): only superseded or archived material is history."""
    page = _item(f"Knowledge Base/Projects/{lifecycle}.md", "Plan body", level="page",
                 lifecycle=lifecycle, path=f"Knowledge Base/Projects/{lifecycle}.md")
    assert "history" not in _packet([page])["units"][0]


@pytest.mark.parametrize("lifecycle", ["superseded", "archived"])
def test_superseded_or_archived_material_is_history(lifecycle):
    unit = _item("old", "Transport A carries the relay", lifecycle=lifecycle)
    assert _packet([unit])["units"][0]["history"] is True


_RELAY_PAGE = (
    "---\ntype: insight\ntitle: Relay trial\nupdated: 2026-09-20\n---\n# Relay trial\n\n"
    "- [decision] Considered transport A for the relay ^cand-a\n"
    "- [decision] Kept the relay budget fixed ^budget\n\n"
    "## Decision\n- id: pick-b\n- relations: supersedes: {target}\n\n"
    "Transport B replaces transport A.\n"
)


@pytest.mark.parametrize(
    "target",
    [
        "[[relay-trial#cand-a]]",
        "[[#cand-a]]",
        "[[Knowledge Base/Notes/relay-trial.md#cand-a]]",
        "[[Knowledge Base/Notes/relay-trial#cand-a]]",
        "[[relay-trial#cand-a|the old candidate]]",
    ],
)
def test_an_authored_supersedes_relation_makes_its_sibling_history(tmp_path, target):
    """Reviewer probe P8: the real ref is `exomem://vault/...#anchor` and the
    authored target a stem, so the old matcher never fired on a real vault."""
    _write(tmp_path, PAGE, _RELAY_PAGE.format(target=target))
    lexstore.ensure_fresh(tmp_path)
    packet = _packet(_lane_items(tmp_path, PAGE, "recent_change"))
    by_anchor = {u["ref"].rsplit("#", 1)[1]: u for u in packet["units"]}
    assert by_anchor["cand-a"]["history"] is True
    assert by_anchor["cand-a"]["lifecycle"] == "superseded"
    assert "history" not in by_anchor["pick-b"]
    assert "history" not in by_anchor["budget"]
    order = [u["ref"].rsplit("#", 1)[1] for u in packet["units"]]
    assert order.index("pick-b") < order.index("cand-a")


def test_a_supersedes_relation_naming_another_page_marks_nothing_here(tmp_path):
    """A relation authored on one page never marks a unit on another: the
    marking would otherwise tell a reader of the released page that some other
    page, perhaps withheld, replaced it."""
    _write(tmp_path, PAGE, (
        "---\ntype: insight\ntitle: Relay trial\nupdated: 2026-09-20\n---\n# Relay trial\n\n"
        "- [decision] Considered transport A for the relay ^cand-a\n"
    ))
    other = "Knowledge Base/Notes/relay-review.md"
    _write(tmp_path, other, (
        "---\ntype: insight\ntitle: Relay review\nupdated: 2026-09-21\n---\n# Relay review\n\n"
        "## Decision\n- id: pick-b\n- relations: supersedes: [[relay-trial#cand-a]]\n\n"
        "Transport B replaces transport A.\n"
    ))
    lexstore.ensure_fresh(tmp_path)
    registry = context_roles.load_roles().roles
    items = working_set._units_lane(
        tmp_path, registry["recent_change"], neighbourhood=frozenset({PAGE, other})
    ).items
    units = {u["ref"].rsplit("#", 1)[1]: u for u in _packet(items)["units"]}
    assert "history" not in units["cand-a"]


# Rule 4: an outcome supersedes a recommendation only through an authored link --


def test_shared_words_do_not_make_a_different_subject_outcome_supersede():
    """Reviewer probe P3: three shared words were treated as the same action."""
    rec = _item("rec", "Migrate the billing service database to Postgres",
                category="action", kind="recommendation", updated="2026-08-01")
    out = _item("out", "migrated the analytics service database to postgres",
                category="outcome", kind="outcome", updated="2026-08-15")
    units = {u["ref"]: u for u in _packet([rec, out])["units"]}
    assert "history" not in units["rec"]
    assert not {"newer_than", "superseded_by_outcome"} & (set(units["out"]) | set(units["rec"]))


def test_a_failed_attempt_sharing_words_does_not_supersede():
    """Reviewer probe P3b: a failed outcome retired the still-open action."""
    rec = _item("rec", "Try restarting the ingest worker queue daemon",
                category="action", kind="recommendation", updated="2026-08-01")
    out = _item("out", "Restarting the ingest worker queue daemon did not help; still failing",
                category="result", updated="2026-08-15")
    units = {u["ref"]: u for u in _packet([rec, out])["units"]}
    assert "history" not in units["rec"]


def test_an_outcome_that_authors_supersedes_retires_the_recommendation(tmp_path):
    _write(tmp_path, PAGE, (
        "---\ntype: insight\ntitle: Relay trial\nupdated: 2026-09-20\n---\n# Relay trial\n\n"
        "- [decision] call the admissions office about the deposit deadline ^call\n\n"
        "## Decision\n- id: called\n- relations: supersedes: [[#call]]\n\n"
        "Called the admissions office; the deposit deadline moved and we accepted.\n"
    ))
    lexstore.ensure_fresh(tmp_path)
    packet = _packet(_lane_items(tmp_path, PAGE, "recent_change"))
    units = {u["ref"].rsplit("#", 1)[1]: u for u in packet["units"]}
    assert units["call"]["history"] is True
    assert [u["ref"].rsplit("#", 1)[1] for u in packet["units"]] == ["called", "call"]


# Rule 3: weakly resolved state is labelled, never silently dropped ------------


def _anchor(evidence):
    return SimpleNamespace(ref="a", path="p.md", kind="resource", evidence=frozenset(evidence))


def _entry(as_of, **extra):
    return {"anchor": "a", "source": "profile", "as_of": as_of, "statement": "state: open", **extra}


TODAY = date(2026, 9, 28)


def test_weak_anchor_old_state_is_held_back():
    out = working_set_state.temper_weak_entries(
        [_entry("2026-01-01")], [_anchor({"lexical_overlap", "rare_term"})], today=TODAY)
    assert out == ()


def test_weak_anchor_recent_state_is_labelled_with_age():
    out = working_set_state.temper_weak_entries(
        [_entry("2026-09-18")], [_anchor({"lexical_overlap"})], today=TODAY)
    assert out[0]["age_days"] == 10 and out[0]["resolved_by"] == "lexical"


def test_strongly_resolved_state_is_untouched():
    entries = [_entry("2026-01-01")]
    out = working_set_state.temper_weak_entries(
        entries, [_anchor({"lexical_overlap", "exact_alias"})], today=TODAY)
    assert out == tuple(entries)


def test_weak_anchor_undated_state_is_served_labelled_with_its_page_time():
    """MED-6: undated state was dropped, which on a real vault is most of it."""
    out = working_set_state.temper_weak_entries(
        [_entry("", page_updated="2026-09-01")], [_anchor({"rare_term"})], today=TODAY)
    assert len(out) == 1
    assert out[0]["resolved_by"] == "lexical"
    assert out[0]["page_updated"] == "2026-09-01"
    assert "age_days" not in out[0]


def test_retrieval_alone_is_weak_evidence():
    """MED-6: an anchor reached only by recall shared words, not a name."""
    out = working_set_state.temper_weak_entries(
        [_entry("2026-01-01")], [_anchor({"retrieval"})], today=TODAY)
    assert out == ()


# Rule 5: the current-state page is only the one the anchor's page declares ----


def _canonical_hit(ref, path, content, *, updated, line, category="fact", context=None):
    return SimpleNamespace(
        unit_ref=ref, parent_path=path, parent_title=path, content=content, excerpt=content,
        context=context, category=category, kind=category, parent_updated=updated,
        parent_superseded_by=[], relations=[], source_span={"start_line": line, "end_line": line},
    )


HUB = "Knowledge Base/Entities/Organizations/harbour-portal.md"


def _hub_anchor(neighbourhood, *, kind="entity"):
    return SimpleNamespace(
        ref=HUB, path=HUB, kind=kind, title="Harbour portal",
        neighbourhood=frozenset(neighbourhood), evidence=frozenset({"exact_alias"}),
    )


def _patch_units(monkeypatch, hits):
    from exomem import find as find_module

    seen = []

    def fake(*args, **kwargs):
        allowed = kwargs.get("allowed_parent_paths") or set()
        seen.append(set(allowed))
        return [hit for hit in hits if hit.parent_path in allowed]

    monkeypatch.setattr(find_module, "FreshnessSnapshot", lambda root: SimpleNamespace(
        recall_checkpoint=lambda scope: None))
    monkeypatch.setattr(find_module, "_find_semantic_units", fake)
    return seen


def _hub_page(root, named=None):
    field = f'current_state_page: "[[{named}]]"\n' if named else ""
    _write(root, HUB, f"---\ntype: entity\ntitle: Harbour portal\n{field}---\n\nHub.\n")


def test_no_declared_page_means_no_canonical_state(monkeypatch, tmp_path):
    """LOW-10: "the newest page with any fact" served a look-alike page."""
    lookalike = "Knowledge Base/Notes/harbour-portal-scratch.md"
    seen = _patch_units(monkeypatch, [
        _canonical_hit("s1", lookalike, "maybe renegotiate the retainer", updated="2026-09-20",
                       line=1),
    ])
    _hub_page(tmp_path)
    out = working_set_state.current_state_for(
        tmp_path, anchors=(_hub_anchor({lookalike}),), state_fields=("state",),
        date_fields=("as_of",))
    assert out == ()
    assert seen == []  # no unit read at all without a declaration


def test_the_declared_page_serves_its_leading_unit_with_its_path(monkeypatch, tmp_path):
    named = "Knowledge Base/Notes/harbour-portal-operating-state.md"
    newer = "Knowledge Base/Notes/harbour-portal-scratch.md"
    seen = _patch_units(monkeypatch, [
        _canonical_hit("n2", named, "Invoices go out monthly", updated="2026-08-01", line=9),
        _canonical_hit("n1", named, "Retainer agreement is signed", updated="2026-08-01",
                       line=2, context="signed 2026-07-30"),
        _canonical_hit("s1", newer, "maybe renegotiate", updated="2026-09-20", line=1),
    ])
    _hub_page(tmp_path, "harbour-portal-operating-state")
    out = working_set_state.current_state_for(
        tmp_path, anchors=(_hub_anchor({named, newer}),), state_fields=("state",),
        date_fields=("as_of",))
    assert [e["statement"] for e in out] == ["Retainer agreement is signed"]
    assert out[0]["path"] == named
    assert out[0]["as_of"] == "2026-07-30"
    assert out[0]["source"] == "canonical_page"
    assert seen == [{named}]


def test_an_undated_declared_unit_is_labelled_with_its_page_time(monkeypatch, tmp_path):
    named = "Knowledge Base/Notes/harbour-portal-operating-state.md"
    _patch_units(monkeypatch, [
        _canonical_hit("n1", named, "Retainer agreement is signed", updated="2026-08-01",
                       line=2, context="renew by 2027-01-01"),
    ])
    _hub_page(tmp_path, "Harbour-Portal-Operating-State")
    out = working_set_state.current_state_for(
        tmp_path, anchors=(_hub_anchor({named}),), state_fields=("state",),
        date_fields=("as_of",))
    assert out[0]["as_of"] == ""
    assert out[0]["page_updated"] == "2026-08-01"


def test_a_project_anchor_has_no_page_to_declare_one(monkeypatch, tmp_path):
    member = "Knowledge Base/Notes/harbour-portal-operating-state.md"
    seen = _patch_units(monkeypatch, [
        _canonical_hit("n1", member, "Retainer agreement is signed", updated="2026-08-01", line=2),
    ])
    anchor = SimpleNamespace(ref="project:harbour", path="", kind="project", title="Harbour",
                             neighbourhood=frozenset({member}), evidence=frozenset({"exact_alias"}))
    out = working_set_state.current_state_for(
        tmp_path, anchors=(anchor,), state_fields=("state",), date_fields=("as_of",))
    assert out == () and seen == []


def test_canonical_state_is_charged_to_the_budget_and_precedes_history():
    entry = {"anchor": "a", "source": "canonical_page", "path": "p.md", "as_of": "2026-09-01",
             "statement": "Executed agreement between Northwind Ltd and Tidewater Co"}
    history = _item("h", "Considered a different agreement", lifecycle="superseded",
                    updated="2026-01-01")
    packet = _packet([history], current_state=[entry])
    assert packet["current_state"][0]["statement"] == entry["statement"]
    assert packet["budget"]["used_chars"] >= len(entry["statement"]) + len(history.text)


# Packet surface: nothing internal is published (HIGH-2) ----------------------


def test_internal_currency_fields_are_not_published(tmp_path):
    _write(tmp_path, PAGE, _RELAY_PAGE.format(target="[[relay-trial#cand-a]]"))
    lexstore.ensure_fresh(tmp_path)
    packet = _packet(_lane_items(tmp_path, PAGE, "recent_change"))
    for unit in packet["units"]:
        assert not {"supersession", "supersedes_targets", "page_updated"} & set(
            unit["provenance"]
        ), unit["provenance"]
        assert not {"newer_than", "superseded_by_outcome", "superseded_by_unit"} & set(unit)


# Caps: no class is exempt (LOW-9) -------------------------------------------


def test_supersession_units_are_capped_like_any_other():
    """Reviewer probe P7: 200 supersession units went out uncapped."""
    items = [
        _item(f"{PAGE}#s{i:03d}", f"Transport {i} replaces the earlier relay plan.",
              role="precedents", updated="2026-09-01",
              provenance={"supersedes_targets": [f"[[relay-trial#old{i:03d}]]"],
                          "supersession": True})
        for i in range(200)
    ]
    packet = _packet(items, max_chars=working_set.clamp_budget(10**9))
    assert len(packet["units"]) == working_set.MAX_ITEMS_PER_ROLE
    assert {p["reason"] for p in packet["pointers"]} == {"role_cap"}


def test_standing_units_are_capped_like_any_other():
    items = [
        _item(f"s{i}", f"Standing precedent {i} for the relay", role="precedents",
              provenance={"standing": True})
        for i in range(working_set.MAX_ITEMS_PER_ROLE + 2)
    ]
    packet = _packet(items)
    assert len(packet["units"]) == working_set.MAX_ITEMS_PER_ROLE
    assert [p["reason"] for p in packet["pointers"]] == ["role_cap", "role_cap"]


def test_history_ranks_below_current_within_a_role_only():
    """#1449's order holds: role first; lifecycle orders within a role."""
    same_role = _packet([
        _item("h", "Considered transport A", role="constraints", lifecycle="superseded",
              updated="2026-09-01"),
        _item("c", "Chose transport B", role="constraints", updated="2026-01-01"),
    ])
    assert [u["ref"] for u in same_role["units"]] == ["c", "h"]


# Hook: history is visible to the agent (MED-7) ------------------------------


def test_the_hook_marks_a_history_unit_compactly():
    unit = {"ref": "r1", "text": "Considered transport A", "history": True,
            "provenance": {"path": PAGE}}
    current = {"ref": "r2", "text": "Considered transport A", "provenance": {"path": PAGE}}
    carried = {"ref": "r3", "text": "Considered transport A", "history": True,
               "provenance": {"path": PAGE, "carried": True}}
    carried_current = {**carried, "history": False}
    lines = hook._packet_lines({"units": [current, unit, carried_current, carried]})
    assert lines[0].startswith("- unit: ")
    assert lines[1].startswith("- history: ")
    assert lines[2].startswith("- carried: ")
    assert lines[3].startswith("- carried history: ")
    # The marker is a label, not a sentence: at most ~10 bytes over the plain line.
    assert len(lines[1].encode()) - len(lines[0].encode()) <= 10
    assert len(lines[3].encode()) - len(lines[2].encode()) <= 10


# Egress: a restricted current-state page and a withheld successor (HIGH-1/2) --


def _governed_entity_vault(tmp_path: Path, *, declared: bool) -> Path:
    from test_governance_egress import write_rule, write_scope
    from test_working_set_egress import _prepare_end_to_end_vault, _reset_governance_state

    vault = tmp_path / "vault"
    kb = "Knowledge Base"
    field = 'current_state_page: "[[harbourline-operating-state]]"\n' if declared else ""
    _write(vault, f"{kb}/Entities/Organizations/Harbourline Trust.md", (
        f"---\ntype: entity\ntitle: Harbourline Trust\nstatus: active\nupdated: 2026-09-01\n"
        f"{field}---\n\n# Harbourline Trust\n\nA shipping trust. Operating state lives in "
        "[[harbourline-operating-state]]; the old plan is [[harbourline-kickoff]].\n"
    ))
    _write(vault, f"{kb}/Notes/Patterns/harbourline-operating-state.md", (
        "---\ntype: note\nstatus: active\nupdated: 2026-09-20\n---\n\n"
        "# Harbourline operating state\n\n## Summary\n\n"
        "- [fact] SECRETFACT the harbourline retainer is ninety thousand ^retainer\n"
    ))
    _write(vault, f"{kb}/Notes/harbourline-kickoff.md", (
        "---\ntype: note\nstatus: active\nupdated: 2026-02-01\n---\n\n"
        "# Harbourline kickoff\n\n## Summary\n\n"
        "- [fact] Harbourline scope was still under negotiation ^scope\n"
    ))
    _prepare_end_to_end_vault(vault)
    write_scope(vault)
    write_rule(vault, ceiling=0, audience="external")
    _reset_governance_state(vault)
    return vault


@pytest.mark.parametrize("declared", [True, False])
@pytest.mark.parametrize(
    "turn", ["what is the current state of Harbourline Trust", "what's up with harbourline trust"]
)
def test_a_restricted_current_state_page_never_reaches_an_external_packet(
    tmp_path, declared, turn
):
    """HIGH-1: the reviewer's SECRETFACT repro. A restricted page in a released
    anchor's neighbourhood must not leak through `current_state[]`."""
    from test_governance_egress import _external

    from exomem import commands
    from exomem.governance.principal import request_scope

    vault = _governed_entity_vault(tmp_path, declared=declared)
    with request_scope(_external()):
        packet = commands.op_activate_context(vault, turn=turn)
    # Not vacuous: the released entity still resolves and serves.
    assert packet["abstained"] is False, packet.get("abstention")
    assert [a["title"] for a in packet["anchors"]] == ["Harbourline Trust"]
    assert "SECRETFACT" not in json.dumps(packet)
    assert "harbourline-operating-state" not in json.dumps(packet).casefold()


def test_the_declared_page_is_served_to_its_owner(tmp_path):
    from exomem import commands

    from exomem.governance.principal import RequestPrincipal, request_scope

    vault = _governed_entity_vault(tmp_path, declared=True)
    with request_scope(RequestPrincipal(audience_id="owner", surface="cli")):
        packet = commands.op_activate_context(
            vault, turn="what is the current state of harbourline trust"
        )
    canonical = [e for e in packet["current_state"] if e.get("source") == "canonical_page"]
    assert [e["path"] for e in canonical] == [
        "Knowledge Base/Notes/Patterns/harbourline-operating-state.md"
    ]
    assert canonical[0]["statement"].startswith("SECRETFACT")


_OPEN = "Knowledge Base/Notes/Insights/relay-open.md"
_HIDDEN = "Knowledge Base/Notes/Patterns/relay-successor.md"
_SPELLINGS = (
    "relay-successor",
    "[[relay-successor]]",
    "[[relay-successor#pick-b]]",
    "Knowledge Base/Notes/Patterns/relay-successor.md",
    "[[Knowledge Base/Notes/Patterns/relay-successor.md#pick-b]]",
    "[[Notes/Patterns/relay-successor]]",
    "[[Relay-Successor|the new plan]]",
    # The forms the lane stored after stripping the brackets off a relation.
    "relay-successor#pick-b",
    "Relay-Successor|the new plan",
    "Knowledge Base/Notes/Patterns/relay-successor.md#pick-b",
    "Knowledge Base/Notes/Patterns/relay-successor#pick-b",
    "exomem://vault/Knowledge%20Base/Notes/Patterns/relay-successor.md#pick-b",
)


@pytest.mark.parametrize("carrier", ["relation", "page", "both"])
@pytest.mark.parametrize("successor_served", [False, True])
@pytest.mark.parametrize("spelling", _SPELLINGS)
def test_a_withheld_successors_name_never_reaches_an_external_packet(
    tmp_path, spelling, successor_served, carrier
):
    """HIGH-2: a successor on a withheld page, named in any spelling, from a
    released unit's relation, a released page's supersession, or the
    successor's own relation back to the released unit."""
    from test_governance_egress import _external, write_rule, write_scope

    from exomem.governance import egress
    from exomem.governance.principal import request_scope

    vault = tmp_path / "vault"
    _write(vault, _OPEN, "---\ntype: insight\ntitle: Relay open\n---\n\n# Relay open\n")
    _write(vault, _HIDDEN, "---\ntype: insight\ntitle: Relay successor\n---\n\n# Relay successor\n")
    write_scope(vault)
    write_rule(vault, ceiling=0, audience="external")
    released = _item(f"{_OPEN}#cand-a", "Considered transport A for the relay", path=_OPEN,
                     provenance={"supersedes_targets": [spelling], "supersession": True,
                                 "page_updated": "2026-09-01"})
    released_page = _item(f"{_OPEN}#budget", "Kept the relay budget fixed", path=_OPEN,
                          lifecycle="superseded", provenance={"superseded_by": [spelling]})
    successor = _item(f"{_HIDDEN}#pick-b", "Transport B replaces transport A", path=_HIDDEN,
                      provenance={"supersedes_targets": ["[[relay-open#cand-a]]"],
                                  "supersession": True})
    carried = {"relation": (released,), "page": (released_page,),
               "both": (released, released_page)}[carrier]
    packet = _packet([*carried, *((successor,) if successor_served else ())])
    # Nothing upstream decided the hidden page: the guard learns of it only from
    # the fields it reads, so a name in a field it does not read goes out.
    release = egress.AnnotatedHits(hits=[], withheld_paths=frozenset(), active=True,
                                   blocked=False)
    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, release)
    assert guarded is not None
    text = json.dumps(guarded).casefold()
    assert "relay-successor" not in text
    assert "relay successor" not in text
    assert "pick-b" not in text
    assert {u["ref"] for u in guarded["units"]} <= {f"{_OPEN}#cand-a", f"{_OPEN}#budget"}


@pytest.mark.parametrize("field", ["supersession", "supersedes_targets", "page_updated"])
def test_internal_currency_provenance_is_stripped_even_from_supplied_items(field):
    unit = _item("internal", "Kept the relay budget fixed", provenance={field: "private"})
    assert field not in _packet([unit])["units"][0]["provenance"]


def test_undated_current_state_publishes_page_time_as_a_label_not_internal_metadata():
    entry = _entry("", page_updated="2026-09-01")
    packet = _packet([], current_state=[entry])
    state = packet["current_state"][0]
    assert "page_updated" not in state
    assert state["as_of"] == ""
    assert "page updated 2026-09-01" in state["statement"]
    assert packet["budget"]["used_chars"] == len(state["statement"])
    assert entry["statement"] == "state: open"


def test_resolved_units_precede_standing_units_carried_beside_them():
    resolved = _item("resolved", "The named anchor's current constraint", role="constraints")
    carried = _item("carried", "A standing constraint on a carried page", role="constraints",
                    provenance={"carried": True, "standing": True})
    packet = _packet([carried, resolved])
    assert [u["ref"] for u in packet["units"]] == ["resolved", "carried"]


def test_role_priority_precedes_lifecycle_and_carriage():
    roles = [{"id": "constraints"}, {"id": "recent_change"}]
    items = [
        _item("low", "Current material in the lower-priority role"),
        _item("high", "History carried under the higher-priority role", role="constraints",
              lifecycle="superseded", provenance={"carried": True}),
    ]
    packet = working_set.build_packet(
        items=items, anchors=(), roles=roles, current_state=(), ambiguity=(), missing=(),
        max_chars=4000, generation={}, status="resolved")
    assert [u["ref"] for u in packet["units"]] == ["high", "low"]


def test_an_ambiguous_current_state_page_name_never_picks_a_lookalike(monkeypatch, tmp_path):
    named = "Knowledge Base/Notes/harbour-portal-operating-state.md"
    lookalike = "Knowledge Base/Notes/Other/harbour-portal-operating-state.md"
    seen = _patch_units(monkeypatch, [
        _canonical_hit("real", named, "Retainer agreement is signed", updated="2026-08-01", line=2),
        _canonical_hit("other", lookalike, "A different retainer", updated="2026-09-01", line=2),
    ])
    _hub_page(tmp_path, "harbour-portal-operating-state")
    anchor = _hub_anchor({named, lookalike})
    assert working_set_state.current_state_for(
        tmp_path, anchors=(anchor,), state_fields=("state",), date_fields=("as_of",)) == ()
    assert seen == []


def test_a_full_current_state_page_path_beats_a_same_stem_lookalike(monkeypatch, tmp_path):
    named = "Knowledge Base/Notes/harbour-portal-operating-state.md"
    lookalike = "Knowledge Base/Notes/Other/harbour-portal-operating-state.md"
    _patch_units(monkeypatch, [
        _canonical_hit("real", named, "Retainer agreement is signed", updated="2026-08-01", line=2),
        _canonical_hit("other", lookalike, "A different retainer", updated="2026-09-01", line=2),
    ])
    _hub_page(tmp_path, named)
    out = working_set_state.current_state_for(
        tmp_path, anchors=(_hub_anchor({named, lookalike}),), state_fields=("state",),
        date_fields=("as_of",))
    assert [entry["path"] for entry in out] == [named]
