"""A write that lands between two role lanes of one request is served, not dropped.

The role lanes of one request share one read of each parent page. A write can
land after an earlier lane read a page, so that a later lane's catalogue rows
carry a newer generation than the shared read. The later lane must read the
page again and serve its units, as it did when every lane read each parent
itself, rather than drop them as stale or fail. Invented, generic vocabulary
throughout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import context_roles, freshness, lexstore, working_set
from exomem import find as find_module
from exomem.vault import walk_vault_md
from exomem.working_set_resolve import analyze_turn

PAGES = 6
ROLES = ("preferences", "constraints", "recent_change", "material")
TURN = "what about the harbor ledger amber gauges"


def _page(index: int, edit: str = "") -> str:
    return (
        "---\ntype: note\nstatus: active\nupdated: 2026-03-01\n---\n\n"
        f"# Harbor ledger {index}\n\n"
        f"- [preference] Ledger {index} prefers amber gauges. ^pref\n"
        f"- [constraint] Ledger {index} stays under the tide mark. ^limit\n"
        f"- [decision] Ledger {index} moved the crane to the east quay. ^moved\n"
        f"- [observation] Ledger {index} harbor amber gauges drift in winter. ^seen\n" + edit
    )


def _lanes(vault: Path, paths: list[str]) -> tuple[dict[str, list[str]], list[dict]]:
    """Every role's item refs from one run of the lanes, and what they missed."""
    items, missing = working_set.run_lanes(
        vault,
        anchors=(),
        roles=[{"id": role} for role in ROLES],
        registry=context_roles.load_roles(vault),
        neighbourhood=frozenset(paths),
        analysis=analyze_turn(TURN),
        # One snapshot for the whole request, taken before any lane runs.
        freshness_snapshot=find_module.FreshnessSnapshot(vault),
    )
    return {role: sorted(item.ref for item in items if item.role == role) for role in ROLES}, list(
        missing
    )


@pytest.mark.timeout(300)
def test_a_write_between_lanes_is_served_by_the_later_lanes(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = [f"Knowledge Base/Notes/Harbor/ledger-{index}.md" for index in range(PAGES)]
    for index, rel in enumerate(paths):
        (vault / rel).parent.mkdir(parents=True, exist_ok=True)
        (vault / rel).write_text(_page(index), encoding="utf-8")
    # The registry a running service keeps: the request's snapshot reads it.
    freshness.seed(
        vault, "vault", ((str(p), freshness.stat_signature(p)) for p in walk_vault_md(vault))
    )
    freshness.seed(
        vault,
        "kb",
        (
            (str(p), freshness.stat_signature(p))
            for p in find_module._walk_md(vault / "Knowledge Base")
        ),
    )
    lexstore.ensure_fresh(vault)
    calm, calm_missing = _lanes(vault, paths)
    assert all(calm[role] for role in ROLES), calm

    real_lane = working_set._lane

    def lane_then_write(root, role, **kwargs):
        result = real_lane(root, role, **kwargs)
        if role.id == ROLES[0]:
            # Every page changes, file and catalogue, after the first lane read it.
            for index, rel in enumerate(paths):
                (vault / rel).write_text(
                    _page(index, "\nEdited between lanes.\n"), encoding="utf-8"
                )
            assert lexstore.get_store(vault).upsert_paths([vault / rel for rel in paths])
        return result

    with monkeypatch.context() as patch:
        patch.setattr(working_set, "_lane", lane_then_write)
        raced, raced_missing = _lanes(vault, paths)

    assert raced == calm
    assert raced_missing == calm_missing


def test_a_definition_change_between_lanes_refreshes_meaning_without_rereading_the_parent(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unchanged row generation does not preserve a removed selected alias."""
    import uuid

    from exomem import ranking_config, semantic_language_registry, structured_filters
    from exomem.vocabulary import registry

    rel = "Knowledge Base/Notes/Harbor/definitions.md"
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\ntype: note\n"
        f"exomem_id: {uuid.uuid5(uuid.NAMESPACE_URL, 'lane-definitions')}\n---\n"
        "# Definitions\n\n- [harbor-choice] Keep amber gauges. ^choice\n",
        encoding="utf-8",
    )
    overlay = semantic_language_registry.registry_path(vault)
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(
        "schema_version: 1\ncategories:\n  quay_choice:\n"
        "    description: A local choice\n    aliases: [harbor-choice]\nkinds: {}\n",
        encoding="utf-8",
    )
    find_module.clear_cache()
    lexstore.ensure_fresh(vault)
    reads = find_module.UnitParentReads()

    def lane(category):
        return find_module._find_semantic_units(
            vault,
            query="",
            limit=3,
            mode="keyword",
            scope="kb",
            plan=structured_filters.compile_filter({"unit.category": {"$eq": category}}),
            snapshot=find_module.FreshnessSnapshot(vault),
            prefer_active=False,
            config=ranking_config.DEFAULT_RANKING,
            degraded_out=[],
            failed_out=[],
            allowed_parent_paths={rel},
            parent_reads=reads,
        )

    first = lane("quay_choice")
    assert len(first) == 1 and first[0].category == "quay_choice"
    generation = reads.parents[rel].state.parent_generation
    overlay.write_text("schema_version: 1\ncategories: {}\nkinds: {}\n", encoding="utf-8")
    registry.invalidate(vault, path=overlay)
    original_read_text = Path.read_text

    def no_second_body_read(self, *args, **kwargs):
        if self == path:
            raise AssertionError("definition refresh reread the parent body")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", no_second_body_read)
    assert lane("quay_choice") == []
    second = lane("harbor_choice")
    assert len(second) == 1 and second[0].category == "harbor_choice"
    assert second[0].unit_ref == first[0].unit_ref
    assert reads.parents[rel].state.parent_generation == generation


def test_role_units_read_only_parents_that_survive_the_packet_budget(
    vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A categorical role pool must not read bodies for units the packet discards."""
    from exomem import lifecycle_statuses

    paths = [f"Knowledge Base/Notes/Harbor/budget-{index}.md" for index in range(PAGES)]
    for index, rel in enumerate(paths):
        (vault / rel).parent.mkdir(parents=True, exist_ok=True)
        (vault / rel).write_text(_page(index), encoding="utf-8")
    lexstore.ensure_fresh(vault)
    roles = [{"id": "preferences", "lane": "units"}]
    registry = context_roles.load_roles(vault)
    basis = lifecycle_statuses.Basis(vault)
    original = find_module._CACHE.get
    read_paths = []

    def counted(path, root):
        rel = path.relative_to(root).as_posix()
        if rel in paths:
            read_paths.append(rel)
        return original(path, root)

    monkeypatch.setattr(find_module._CACHE, "get", counted)
    items, missing = working_set.run_lanes(
        vault, anchors=(), roles=roles, registry=registry, neighbourhood=frozenset(paths),
        freshness_snapshot=find_module.FreshnessSnapshot(vault), status_basis=basis,
    )
    assert items and read_paths == []

    packet = working_set.build_packet(
        items=items, anchors=(), roles=roles, current_state=(), ambiguity=(), missing=missing,
        max_chars=500, generation={}, status="resolved", status_basis=basis,
    )

    selected = {unit["provenance"]["path"] for unit in packet["units"]}
    assert selected and len(selected) < len(paths)
    assert set(read_paths) == selected
    assert len(read_paths) == len(selected)
