from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
import yaml

from exomem import (
    cli_ops,
    deferred_index,
    epistemic_graph,
    graph_sync,
    index_sync,
    relation_registry,
    traversal_profiles,
    vault,
    writer_lease,
)
from exomem import find as find_module

KB = "Knowledge Base/Notes/Insights"


def _write(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _registry_document(*, alias: bool) -> dict[str, object]:
    current: dict[str, object] = {
        "parent": "relates_to",
        "description": "A reviewed applicability relation.",
        "direction": "directed",
        "inverse": "vault.applied_from",
        "scope": {"projects": ["Project Alpha"], "page_types": ["insight"]},
    }
    if alias:
        current["aliases"] = ["applies_to"]
    return {
        "schema_version": 1,
        "extensions": {
            "vault.applies_to": current,
            "vault.applied_from": {
                "parent": "relates_to",
                "description": "The reviewed inverse registry metadata.",
                "direction": "directed",
                "inverse": "vault.applies_to",
            },
            "vault.old_policy_link": {
                "parent": "relates_to",
                "description": "Historical policy applicability wording.",
                "direction": "directed",
                "status": "deprecated",
                "replaced_by": "vault.applies_to",
            },
        },
    }


def _registry_yaml(*, alias: bool) -> str:
    return yaml.safe_dump(_registry_document(alias=alias), sort_keys=False)


def _seed_rebind_vault(root: Path) -> None:
    registry_path = relation_registry.extension_registry_path(root)
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(_registry_yaml(alias=False), encoding="utf-8")
    _write(
        root,
        f"{KB}/policy.md",
        """---
type: insight
status: active
project: Project Alpha
created: 2026-01-02
updated: 2026-01-03
tags: [policy]
---
# Policy

## Claim
- id: claim-1
- relations: applies_to: [[Knowledge Base/Notes/Insights/case]]

The policy applies.

## Relations
- old_policy_link [[Knowledge Base/Notes/Insights/history]]
- applies_to [[Knowledge Base/Notes/Insights/missing-case]]
""",
    )
    _write(
        root,
        f"{KB}/case.md",
        "---\ntype: insight\nstatus: active\n---\n# Case\n",
    )
    _write(
        root,
        f"{KB}/history.md",
        "---\ntype: insight\nstatus: active\n---\n# History\n",
    )


def _owe_full_rebuild(root: Path) -> tuple[graph_sync.GraphSyncCheckpoint, int]:
    """A committed Markdown batch whose whole-vault rebuild is owed."""
    vault.batch_atomic_write(
        [vault.PlannedWrite(root / f"{KB}/case.md", "---\ntype: insight\n---\n# Case\n\nRevised.\n")],
        vault_root=root,
        post_commit_fanout=False,
    )
    checkpoint = graph_sync.read_checkpoint(root)
    assert checkpoint is not None
    deferred_index.mark_graph_full_rebuild(root, generation=checkpoint.generation)
    marker = deferred_index.graph_full_rebuild_pending(root)
    assert marker is not None
    return checkpoint, marker


def _stable_nodes(index: epistemic_graph.EpistemicGraphIndex) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for source in index.nodes():
        row = dict(source)
        metadata = dict(row.get("metadata") or {})
        # This freshness token intentionally binds the owning vault instance;
        # clean-rebuild parity is over graph meaning, not a root-local token.
        metadata.pop("parent_generation", None)
        row["metadata"] = metadata
        rows.append(row)
    return sorted(rows, key=lambda row: str(row["node_key"]))


def _stable_edges(index: epistemic_graph.EpistemicGraphIndex) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for source in index.edges():
        row = dict(source)
        metadata = dict(row.get("metadata") or {})
        metadata.pop("parent_generation", None)
        row["metadata"] = metadata
        rows.append(row)
    return sorted(rows, key=lambda row: str(row["edge_key"]))


def test_a_registry_save_leaves_the_graph_current_and_resolves_at_read_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "vault"
    _seed_rebind_vault(root)
    epistemic_graph.EpistemicGraphIndex(root).rebuild_all()
    before = epistemic_graph.EpistemicGraphIndex(root).edges()
    assert not any(row["relation_type"] == "vault.applies_to" for row in before)
    registry_path = relation_registry.extension_registry_path(root)

    # Stored rows keep raw observations, so the save owes the graph no work.
    with monkeypatch.context() as guarded:
        guarded.setattr(
            find_module,
            "_parse_page",
            lambda *_args, **_kwargs: pytest.fail("a registry save parsed Markdown"),
        )
        vault.batch_atomic_write(
            [vault.PlannedWrite(registry_path, _registry_yaml(alias=True))],
            vault_root=root,
            post_commit_fanout=False,
        )
        report = index_sync.upsert_after_write(root, [registry_path])
        current = epistemic_graph.EpistemicGraphIndex(root)
        assert current.available()
        edges = _stable_edges(current)

    assert report.components == ()
    assert deferred_index.graph_full_rebuild_pending(root) is None
    assert graph_sync.status(root)["state"] == "current"
    applies = [row for row in edges if row["raw_relation"] == "applies_to"]
    assert {row["relation_type"] for row in applies} == {"vault.applies_to"}
    assert {row["registry_status"] for row in applies} == {"alias"}

    clean = tmp_path / "clean"
    shutil.copytree(root / "Knowledge Base", clean / "Knowledge Base")
    clean_index = epistemic_graph.EpistemicGraphIndex(clean)
    clean_index.rebuild_all()
    assert edges == _stable_edges(clean_index)


def test_interrupted_full_marker_convergence_keeps_checkpoint_and_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "vault"
    _seed_rebind_vault(root)
    epistemic_graph.EpistemicGraphIndex(root).rebuild_all()
    checkpoint, marker = _owe_full_rebuild(root)
    monkeypatch.setattr(
        graph_sync,
        "replace_sidecar",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("publish cut")),
    )

    result = epistemic_graph.converge_full_graph_marker(root)

    assert result.outcome == "deferred"
    assert graph_sync.acknowledged_checkpoint(root) != graph_sync.GraphBuildOutcome.covering(
        checkpoint
    )
    assert deferred_index.graph_full_rebuild_pending(root) == marker


def test_dispatcher_leaves_marker_untouched_when_canonical_boundary_is_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "vault"
    _seed_rebind_vault(root)
    _checkpoint, marker = _owe_full_rebuild(root)

    class BusyCoordinator:
        def hold(self, **_kwargs):
            raise cli_ops.OpError("MUTATION_BUSY", "synthetic held boundary")

    class BusyManager:
        def _mutation_coordinator_for(self, _root: Path) -> BusyCoordinator:
            return BusyCoordinator()

    active_manager = writer_lease.active_manager
    monkeypatch.setattr(writer_lease, "active_manager", lambda: BusyManager())
    monkeypatch.setattr(
        epistemic_graph.EpistemicGraphIndex,
        "rebuild_all",
        lambda *_args, **_kwargs: pytest.fail("busy dispatch started graph work"),
    )

    result = epistemic_graph.converge_full_graph_marker(root)

    assert result == epistemic_graph.GraphDispatchResult("failed", "graph_boundary_busy")
    monkeypatch.setattr(writer_lease, "active_manager", active_manager)
    assert deferred_index.graph_full_rebuild_pending(root) == marker


def test_relation_query_plan_is_alias_family_and_survivor_directed() -> None:
    registry = relation_registry.load_registry(
        proposal={
            "schema_version": 1,
            "extensions": {
                "vault.current": {
                    "parent": "relates_to",
                    "description": "Current narrow meaning.",
                    "direction": "directed",
                    "aliases": ["clean_current"],
                },
                "vault.middle": {
                    "parent": "relates_to",
                    "description": "Middle historical meaning.",
                    "direction": "directed",
                    "status": "deprecated",
                    "replaced_by": "relates_to",
                },
                "vault.old": {
                    "parent": "relates_to",
                    "description": "Old historical meaning.",
                    "direction": "directed",
                    "status": "deprecated",
                    "replaced_by": "vault.middle",
                },
            },
        }
    )

    broad = traversal_profiles.relation_query_plan(registry, ["relates_to"])
    alias = traversal_profiles.relation_query_plan(registry, ["clean_current"])
    historical = traversal_profiles.relation_query_plan(registry, ["vault.old"])

    assert broad.exact_keys == frozenset({"relates_to"})
    assert broad.replacement_keys == frozenset({"vault.old", "vault.middle"})
    assert broad.parent_keys == frozenset({"relates_to"})
    assert alias.requested == ("clean_current",)
    assert alias.resolved == ("vault.current",)
    assert alias.exact_keys == frozenset({"vault.current"})
    assert alias.replacement_keys == frozenset()
    assert historical.exact_keys == frozenset({"vault.old"})
    assert historical.replacement_keys == frozenset()
    assert historical.replacements["vault.old"] == (
        "vault.middle",
        "relates_to",
    )


def test_relation_participants_use_exact_replacement_parent_precedence_without_successor_overreach(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    registry_path = relation_registry.extension_registry_path(root)
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    proposal = {
        "schema_version": 1,
        "extensions": {
            "vault.current": {
                "parent": "relates_to",
                "description": "Current narrow meaning.",
                "direction": "directed",
                "aliases": ["clean_current"],
            },
            "vault.middle": {
                "parent": "relates_to",
                "description": "Middle historical meaning.",
                "direction": "directed",
                "status": "deprecated",
                "replaced_by": "relates_to",
            },
            "vault.old": {
                "parent": "relates_to",
                "description": "Old historical meaning.",
                "direction": "directed",
                "status": "deprecated",
                "replaced_by": "vault.middle",
            },
        },
    }
    registry_path.write_text(yaml.safe_dump(proposal, sort_keys=False), encoding="utf-8")
    for name in ("exact", "replacement", "parent", "broad"):
        _write(root, f"{KB}/{name}.md", f"---\ntype: insight\n---\n# {name}\n")
    _write(
        root,
        f"{KB}/source.md",
        f"""---
type: insight
---
# Source

## Relations
- clean_current [[{KB}/exact]]
- vault.old [[{KB}/replacement]]
- vault.current [[{KB}/parent]]
- relates_to [[{KB}/broad]]
""",
    )
    index = epistemic_graph.EpistemicGraphIndex(root)
    index.rebuild_all()

    broad = index.relation_participants(["relates_to"], anchor=f"{KB}/source.md")
    alias = index.relation_participants(["clean_current"], anchor=f"{KB}/source.md")
    historical = index.relation_participants(["vault.old"], anchor=f"{KB}/source.md")

    assert broad.paths == frozenset(
        {
            f"{KB}/exact.md",
            f"{KB}/replacement.md",
            f"{KB}/parent.md",
            f"{KB}/broad.md",
        }
    )
    assert broad.provenance[f"{KB}/broad.md"].matched_via == "relation_type"
    assert broad.provenance[f"{KB}/replacement.md"].matched_via == "replacement"
    assert broad.provenance[f"{KB}/parent.md"].matched_via == "parent_relation"
    assert alias.provenance[f"{KB}/exact.md"].requested_relation == "clean_current"
    assert alias.provenance[f"{KB}/exact.md"].resolved_relation == "vault.current"
    assert historical.paths == frozenset({f"{KB}/replacement.md"})
    assert historical.provenance[f"{KB}/replacement.md"].matched_via == "relation_type"

    context = epistemic_graph.graph_context(
        root,
        path=f"{KB}/source.md",
        relation_types=["clean_current"],
        depth=1,
    )
    matching_edge = next(
        edge for edge in context["edges"] if edge["relation_type"] == "vault.current"
    )
    assert matching_edge["matched_via"] == "relation_type"
    assert matching_edge["requested_relation"] == "clean_current"
    assert matching_edge["resolved_relation"] == "vault.current"

    hits = find_module.find(root, query="", relations=["clean_current"], limit=10)
    exact_hit = next(hit for hit in hits if hit.path == f"{KB}/exact.md")
    assert exact_hit.relation_match["requested_relation"] == "clean_current"
    assert exact_hit.relation_match["resolved_relation"] == "vault.current"


def test_graph_schema_persists_resolution_context_and_review_inputs(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _seed_rebind_vault(root)
    index = epistemic_graph.EpistemicGraphIndex(root)
    index.rebuild_all()
    with sqlite3.connect(epistemic_graph.sidecar_path(root)) as conn:
        edge_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(graph_edges)")}
        node_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(graph_nodes)")}
        context = conn.execute(
            "SELECT resolver_project, resolver_page_type, resolver_source_kind, "
            "resolver_target_kind, resolver_origin FROM graph_edges "
            "WHERE raw_relation = 'applies_to' ORDER BY source_anchor LIMIT 1"
        ).fetchone()
        file_row = conn.execute(
            "SELECT page_type, lifecycle_status, project, origin_date, updated_date, "
            "review_eligible, activation_signal_version FROM graph_nodes "
            "WHERE path = ? AND kind = 'file'",
            (f"{KB}/policy.md",),
        ).fetchone()
    assert {
        "resolver_project",
        "resolver_page_type",
        "resolver_source_kind",
        "resolver_target_kind",
        "resolver_origin",
        "review_evidence",
    } <= edge_columns
    assert {
        "page_type",
        "lifecycle_status",
        "tags_json",
        "project",
        "origin_date",
        "updated_date",
        "access_tier",
        "review_eligible",
        "activation_signal_version",
    } <= node_columns
    # The unit's kind is a selected meaning: shared rows leave it to each reader.
    assert context == ("Project Alpha", "insight", None, "file", "semantic_relation")
    served = next(
        edge for edge in index.edges() if edge["raw_relation"] == "applies_to"
        and edge["origin"] == "semantic_relation"
    )
    assert served["resolver_source_kind"] == "claim"
    assert file_row[:-1] == (
        "insight",
        "active",
        "Project Alpha",
        "2026-01-02",
        "2026-01-03",
        1,
    )
    assert isinstance(file_row[-1], str) and len(file_row[-1]) == 16
