"""Activation temporal currency: what a served unit says about WHEN it is true.

Four rules, each with an invented fixture: a unit carries its own time, history
is labelled and ranked below current material, weakly-resolved state is held
back or aged, and a later outcome supersedes an earlier recommendation.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace


from exomem import context_roles, working_set, working_set_state


def _item(ref, text, *, role="recent_change", category="observation", kind="observation",
          updated="", lifecycle="active", path="Knowledge Base/Notes/relay-trial.md",
          provenance=None):
    return working_set.LaneItem(
        role=role, level="unit", ref=ref, path=path, title="Relay trial", text=text,
        lifecycle=lifecycle, updated=updated, anchor=path,
        provenance={"category": category, "kind": kind, **(provenance or {})},
    )


def _packet(items, *, max_chars=4000):
    return working_set.build_packet(
        items=tuple(items), anchors=(), roles=(), current_state=(), ambiguity=(),
        missing=(), max_chars=max_chars,
        generation={"freshness_key": "k", "index_generation": 1, "roles_hash": "a",
                    "roles_source": "shipped"},
        status="resolved",
    )


def _hit(ref, content, *, context=None, category="observation", relations=()):
    return SimpleNamespace(
        unit_ref=ref, parent_path="Knowledge Base/Notes/relay-trial.md",
        parent_title="Relay trial", content=content, excerpt=content, context=context,
        category=category, kind=category, parent_updated="2026-09-20",
        parent_superseded_by=[], relations=list(relations),
    )


def _lane(monkeypatch, tmp_path, hits):
    from exomem import find as find_module

    monkeypatch.setattr(find_module, "FreshnessSnapshot", lambda root: SimpleNamespace(
        recall_checkpoint=lambda scope: None))
    monkeypatch.setattr(find_module, "_find_semantic_units", lambda *a, **k: list(hits))
    role = context_roles.load_roles().roles["recent_change"]
    return working_set._units_lane(
        tmp_path, role, neighbourhood=frozenset({"Knowledge Base/Notes/relay-trial.md"})
    ).items


# Rule 1 -------------------------------------------------------------------


def test_unit_carries_its_own_time_not_the_parent_page(monkeypatch, tmp_path):
    items = _lane(monkeypatch, tmp_path, [
        _hit("u1", "Considered transport A for the relay", context="2026-01-10"),
        _hit("u2", "Chose transport B; noted 2026-03-04 in the log"),
        _hit("u3", "Relay budget is fixed"),
    ])
    by_ref = {item.ref: item for item in items}
    assert by_ref["u1"].updated == "2026-01-10"
    assert by_ref["u2"].updated == "2026-03-04"
    # No authored time of its own: never the parent's, which is labelled apart.
    assert by_ref["u3"].updated == ""
    assert by_ref["u3"].provenance["page_updated"] == "2026-09-20"


# Rule 2 -------------------------------------------------------------------


def test_older_observation_is_history_ranked_below_current(monkeypatch, tmp_path):
    items = _lane(monkeypatch, tmp_path, [
        _hit("old", "Considered transport A for the relay", context="2026-01-10"),
        _hit("dec", "Chose transport B for the relay", context="2026-03-04",
             category="decision"),
    ])
    packet = _packet(items)
    units = {u["ref"]: u for u in packet["units"]}
    assert units["old"]["history"] is True
    assert "history" not in units["dec"]
    order = [u["ref"] for u in packet["units"]]
    assert order.index("dec") < order.index("old")


def test_history_ranks_below_current_across_roles():
    for history_role, current_role in (("constraints", "resources"), ("resources", "constraints")):
        packet = _packet([
            _item("h", "Considered transport A", role=history_role, lifecycle="historical"),
            _item("c", "Chose transport B", role=current_role),
        ])
        assert [u["ref"] for u in packet["units"]] == ["c", "h"]


def test_supersession_units_are_served_in_full_not_capped():
    items = [
        _item(f"s{i}", f"Transport {i} replaces the earlier relay plan {i}",
              provenance={"supersession": True}, updated=f"2026-03-0{i + 1}")
        for i in range(working_set.MAX_ITEMS_PER_ROLE + 2)
    ]
    packet = _packet(items)
    assert len(packet["units"]) == len(items)
    assert not [p for p in packet["pointers"] if p["reason"] == "role_cap"]


def test_unit_relation_marks_its_target_historical(monkeypatch, tmp_path):
    rel = SimpleNamespace(kind="supersedes", target="old")
    hits = [
        _hit("old", "Transport A carries the relay"),
        _hit("new", "Transport B replaces transport A", relations=[rel.__dict__ | {}]),
    ]
    items = _lane(monkeypatch, tmp_path, hits)
    assert {i.ref: i for i in items}["new"].provenance["supersession"] is True
    units = {u["ref"]: u for u in _packet(items)["units"]}
    assert units["old"]["history"] is True
    assert units["old"]["lifecycle"] == "historical"
    assert "history" not in units["new"]


# Rule 3 -------------------------------------------------------------------


def _anchor(evidence):
    return SimpleNamespace(ref="a", path="p.md", kind="resource", evidence=frozenset(evidence))


def _entry(as_of):
    return {"anchor": "a", "source": "profile", "as_of": as_of, "statement": "state: open"}


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


def test_weak_anchor_undated_state_is_held_back():
    assert working_set_state.temper_weak_entries(
        [_entry("")], [_anchor({"rare_term"})], today=TODAY) == ()


# Rule 4 -------------------------------------------------------------------


def test_later_outcome_supersedes_recommendation():
    rec = _item("rec", "Call the admissions office about the deposit deadline",
                category="action", kind="recommendation", updated="2026-08-01")
    out = _item("out", "Called the admissions office about the deposit deadline; accepted",
                category="finding", kind="outcome", updated="2026-08-15")
    packet = _packet([rec, out])
    units = {u["ref"]: u for u in packet["units"]}
    assert units["out"]["newer_than"] == ["rec"]
    assert units["rec"]["history"] is True
    assert units["rec"]["superseded_by_outcome"] == "out"
    assert [u["ref"] for u in packet["units"]][0] == "out"


def test_unrelated_or_older_outcome_leaves_recommendation_current():
    rec = _item("rec", "Call the admissions office about the deposit deadline",
                category="action", kind="recommendation", updated="2026-08-20")
    older = _item("o1", "Called the admissions office about the deposit deadline",
                  category="finding", kind="outcome", updated="2026-08-01")
    other = _item("o2", "Repainted the garden fence", category="finding",
                  kind="outcome", updated="2026-09-01")
    units = {u["ref"]: u for u in _packet([rec, older, other])["units"]}
    assert "history" not in units["rec"]


def test_outcome_recognised_by_category_alone():
    rec = _item("rec", "Call the admissions office about the deposit deadline",
                category="action", kind="observation", updated="2026-08-01")
    out = _item("out", "Called the admissions office about the deposit deadline; accepted",
                category="outcome", kind="observation", updated="2026-08-15")
    units = {u["ref"]: u for u in _packet([rec, out])["units"]}
    assert units["rec"]["superseded_by_outcome"] == "out"
