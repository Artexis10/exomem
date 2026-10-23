"""Activation unit roles read only their selected neighbourhood."""

from __future__ import annotations

from pathlib import Path

from exomem import context_roles, lexstore, lifecycle_statuses, working_set


def _write_unit(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\ntype: insight\ntitle: Unit\nupdated: 2026-01-01\n---\n"
        f"# Unit\n\n- [constraint] {text} ^unit\n",
        encoding="utf-8",
    )


def test_parent_filter_precedes_unit_limit(tmp_path: Path) -> None:
    local = "Knowledge Base/Notes/local.md"
    for number in range(working_set.UNIT_LANE_LIMIT + 1):
        _write_unit(tmp_path, f"Knowledge Base/Notes/outside-{number}.md", "outside")
    _write_unit(tmp_path, local, "local")
    assert lexstore.search_bm25(tmp_path, "outside", k=20, scope="kb") is not None

    result = lexstore.search_semantic_units_result(
        tmp_path,
        "",
        1,
        categories=["constraint"],
        allowed_parent_paths={local},
        _validate_current=False,
        allow_delta=False,
        repair=False,
    )

    assert result.readiness.complete
    assert [hit.parent_path for hit in result.value or ()] == [local]

    lane = working_set._units_lane(
        tmp_path,
        context_roles.load_roles().roles["constraints"],
        neighbourhood=frozenset({local}),
    )
    assert [item.path for item in lane.items] == [local]
    assert lane.truncated is False


def test_rejected_units_cannot_expand_past_the_activation_cap(tmp_path, monkeypatch):
    from exomem import find as find_module

    local = "Knowledge Base/Notes/local.md"
    path = tmp_path / local
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\ntype: insight\ntitle: Local\n---\n# Local\n\n"
        + "\n".join(
            f"- [constraint] Constraint {number}. ^u{number}"
            for number in range(working_set.UNIT_LANE_LIMIT + 2)
        ),
        encoding="utf-8",
    )
    lexstore.ensure_fresh(tmp_path)
    original = lexstore.search_semantic_units_result
    reads = []
    parent_reads = []
    original_get = find_module._CACHE.get

    def query(*args, **kwargs):
        reads.append(kwargs["k"])
        return original(*args, **kwargs)

    def get(parent, root):
        parent_reads.append(parent.relative_to(root).as_posix())
        return original_get(parent, root)

    monkeypatch.setattr(lexstore, "search_semantic_units_result", query)
    monkeypatch.setattr(find_module._CACHE, "get", get)
    role = context_roles.load_roles().roles["constraints"]
    lane = working_set._units_lane(
        tmp_path, role,
        neighbourhood=frozenset({local}),
    )
    assert reads == [working_set.UNIT_LANE_LIMIT + 1]
    assert len(lane.items) == working_set.UNIT_LANE_LIMIT
    assert parent_reads == []
    assert lane.truncated is True
    path.unlink()

    packet = working_set.build_packet(
        items=lane.items, anchors=(), roles=({"id": role.id},), current_state=(), ambiguity=(),
        missing=({"role": role.id, "reason": "lane_truncated"},),
        max_chars=working_set.MAX_BUDGET_CHARS, generation={}, status="resolved",
        status_basis=lifecycle_statuses.Basis(tmp_path),
    )

    assert packet["units"] == []
    assert {"role": role.id, "reason": "lane_failed"} in packet["missing"]
    assert {"role": role.id, "reason": "lane_truncated"} in packet["missing"]
    assert reads == [working_set.UNIT_LANE_LIMIT + 1]
    assert parent_reads == [local]


def test_bounded_hydration_preserves_supersession_and_parent_metadata(tmp_path):
    local = "Knowledge Base/Notes/previous.md"
    path = tmp_path / local
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\ntype: insight\ntitle: Previous rule\nupdated: 2026-08-15\n"
        "status: superseded\nsuperseded_by: [Knowledge Base/Notes/current.md]\n---\n"
        "# Previous rule\n\n- [constraint] Old limit. ^old-limit\n",
        encoding="utf-8",
    )
    lexstore.ensure_fresh(tmp_path)
    lane = working_set._units_lane(
        tmp_path, context_roles.load_roles().roles["constraints"],
        neighbourhood=frozenset({local}),
    )
    assert len(lane.items) == 1
    item = lane.items[0]
    assert item.lifecycle == "superseded"
    assert item.title == "Previous rule"
    # A unit authors no time here: it is served undated, and the page's time is
    # labelled apart (temporal currency, rule 1).
    assert item.updated == ""
    assert item.provenance["page_updated"] == "2026-08-15"
    assert item.provenance["superseded_by"] == ["Knowledge Base/Notes/current.md"]
