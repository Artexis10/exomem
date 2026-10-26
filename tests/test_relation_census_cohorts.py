"""Authored additions survive replay and log rotation without inventing history."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from exomem import (
    audit,
    command_surface,
    commands,
    epistemic_graph,
    graph_sync,
    note,
    relation_census,
    writer_lease,
)
from exomem import vault as vault_module
from exomem.governance.principal import owner_principal, request_scope


def test_edit_recovery_keeps_the_first_committing_attempts_server_time(vault, monkeypatch) -> None:
    """A crash before the primary write cannot change the log bytes bound to recovery."""
    from exomem import edit, relation_review

    arguments = {
        "content": "## Claim\n\nA retained conclusion.\n",
        "note_type": "insight", "title": "Recoverable conclusion",
    }
    draft = note.note(vault, validate_only=True, **arguments)
    created = note.note(
        vault, **arguments, draft_id=draft.draft_id, draft_hash=draft.draft_hash,
        draft_token=draft.draft_token, relation_disposition="reviewed_none",
        relation_review_hash=draft.draft_hash, relation_review_reason="No honest relation applies.",
    )
    change = {
        "path": created.path, "why": "Correct the conclusion.",
        "new_body": "## Claim\n\nA corrected conclusion.\n",
        "relation_disposition": "reviewed_none", "relation_review_reason": "No honest relation applies.",
    }
    validation = edit.edit(vault, validate_only=True, **change)
    retry = {
        "semantic_transition_token": validation.semantic["transition_token"],
        "relation_review_hash": validation.semantic["relation_review_hash"],
    }
    prepared_path = relation_review.lifecycle_prepared_path(vault, draft.draft_id)
    before = (vault / created.path).read_bytes()
    publish = vault_module._after_batch_destination_published

    class SimulatedProcessDeath(BaseException):
        pass

    def die_after_prepared(path):
        publish(path)
        if path == prepared_path:
            raise SimulatedProcessDeath

    with monkeypatch.context() as crash:
        crash.setattr(vault_module, "_after_batch_destination_published", die_after_prepared)
        with pytest.raises(SimulatedProcessDeath):
            edit.edit(vault, **change, **retry)
    prepared = prepared_path.read_bytes()
    assert (vault / created.path).read_bytes() == before

    edit.edit(vault, **change, **retry)

    assert prepared_path.read_bytes() == prepared
    assert "A corrected conclusion." in (vault / created.path).read_text()
    events = [entry["provenance"] for entry in vault_module.read_operation_log(vault)["entries"]
              if entry.get("provenance", {}).get("mutation_kind") == "edit"]
    assert len(events) == 1
    assert events[0]["server_at"] == json.loads(prepared)["provenance_server_at"]


def test_new_edges_follow_real_writes_not_page_age_or_reindex(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import link

    target = link.link(
        vault,
        entity_type="concept",
        name="Tidal accounting",
        summary="A synthetic accounting concept.",
    ).path
    draft_arguments = {
        "content": f"## Claim\n\nA retained conclusion.\n\n## Relations\n\n- relates_to [[{target.removesuffix('.md')}]]\n",
        "note_type": "insight",
        "title": "Authored additions",
        "status": "draft",
        "today": dt.date(2020, 1, 1),
    }
    draft = note.note(vault, validate_only=True, **draft_arguments)
    created = note.note(
        vault, draft_token=draft.draft_token, draft_id=draft.draft_id, **draft_arguments
    )
    path = created.path
    _, body, _ = vault_module.parse_frontmatter((vault / path).read_text())
    extra = "Knowledge Base/Notes/Insights/rrf-fusion-beats-score-normalization"
    changed_body = body + f"\n- supports [[{extra}]]\n"
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=vault.parent / "receipts")
    )
    monkeypatch.setattr(writer_lease, "get_manager", lambda: manager)
    monkeypatch.setattr(command_surface, "mcp_retry_scope", lambda: "principal:cohort-proof")
    command = next(
        item for item in commands.product_commands_for("mcp") if item.name == "edit_memory"
    )
    bound = command_surface.bind_vault(command.leaf, vault, command=command)
    arguments = {
        "path": path,
        "why": "Record another authored relation.",
        "operation": {"kind": "replace_body", "new_body": changed_body},
    }
    with request_scope(owner_principal(surface="mcp")):
        written = bound(**arguments)
        replay = bound(**arguments)
    assert replay == written
    # The spelling-only rewrite resolves to the same stable endpoint.
    commands.op_edit_memory(
        vault,
        path,
        "Use the target's short address.",
        operation={
            "kind": "replace_body",
            "new_body": (body + f"\n- supports [[{extra}]]\n").replace(
                target.removesuffix(".md"), Path(target).stem
            ),
        },
    )
    graph_sync.await_active_rebuild(vault, timeout=30)
    graph_sync.await_active_rebuild(vault, state_root=manager.config.state_dir, timeout=30)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    before = relation_census.census(vault)
    cohort = before["new_edges"]
    assert cohort["authored_edges"]["numerator"] == 2
    assert cohort["endpoint_classes"]["note_to_entity"]["raw_generic_share"]["numerator"] == 1
    assert cohort["endpoint_classes"]["note_to_note"]["raw_generic_share"]["denominator"] == 1
    assert cohort["unreasoned_generic_edges"]["numerator"] is None
    assert cohort["unreasoned_generic_edges"]["coverage"]["status"] == "unavailable"
    assert cohort["referents"]["acted"]["numerator"] is None
    assert cohort["placement_by_basis"]["numerator"] == 1
    assert cohort["coverage"]["status"] == "partial"
    assert cohort["review"]["gate"] is False
    assert cohort["review"]["needed"] is True
    reviewed = commands.op_review_memory(
        vault, mode="audit", categories=[audit.RELATION_GENERIC_SHARE_CATEGORY], detail="full"
    )
    assert len(reviewed["findings"]) == 1
    assert reviewed["findings"][0]["severity"] == "info"
    assert reviewed["findings"][0]["meta"]["measurement"]["numerator"] == 1
    config = vault.parent / "review-target.json"
    config.write_text(json.dumps({"relation_census": {"entity_generic_review_share": 1}}))
    with monkeypatch.context() as changed_target:
        changed_target.setenv("EXOMEM_CONFIG_PATH", str(config))
        assert commands.op_review_memory(
            vault, mode="audit", categories=[audit.RELATION_GENERIC_SHARE_CATEGORY], detail="full"
        )["findings"] == []

    monkeypatch.setenv("EXOMEM_LOG_ROTATE_BYTES", "1")
    monkeypatch.setattr(vault_module, "LOG_ROTATE_KEEP_ENTRIES", 1)
    vault_module.rotate_log_if_needed(vault)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    after = relation_census.census(vault)
    assert after["metrics"] == before["metrics"]
    assert after["new_edges"]["authored_edges"]["numerator"] == 2
    assert after["new_edges"]["placement_by_basis"]["numerator"] == 1


def test_missing_and_legacy_logs_never_measure_zero_new_edges(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    legacy = relation_census.census(vault)["new_edges"]
    assert legacy["coverage"]["status"] == "unavailable"
    assert legacy["authored_edges"]["numerator"] is None
    (vault / "Knowledge Base/log.md").unlink()
    missing = relation_census.census(vault)["new_edges"]
    assert missing["coverage"]["live_log_present"] is False
    assert missing["raw_generic_share"]["denominator"] is None
    config = vault.parent / "census-config.json"
    config.write_text(
        json.dumps({"relation_census": {"window_days": 7, "entity_generic_review_share": 0.8}})
    )
    monkeypatch.setenv("EXOMEM_CONFIG_PATH", str(config))
    configured = relation_census.census(vault)["new_edges"]
    assert configured["window"]["days"] == 7
    assert configured["review"]["target"] == 0.8


def test_successor_keeps_inherited_edges_out_of_the_new_authored_cohort(vault: Path) -> None:
    from exomem import replace

    carried = "Knowledge Base/Notes/Insights/rrf-fusion-beats-score-normalization"
    extra = "Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation"
    old = note.note(
        vault,
        note_type="insight",
        title="Initial accounting conclusion",
        content=f"## Claim\n\nA retained conclusion.\n\n## Relations\n\n- relates_to [[{carried}]]\n",
    )
    arguments = {
        "old_path": old.path,
        "note_type": "insight",
        "title": "Revised accounting conclusion",
        "content": f"## Claim\n\nAn updated conclusion.\n\n## Relations\n\n- relates_to [[{carried}]]\n- supports [[{extra}]]\n",
    }
    draft = replace.replace(vault, validate_only=True, **arguments)
    replace.replace(
        vault,
        draft_token=draft.draft_token,
        draft_id=draft.draft_id,
        draft_hash=draft.draft_hash,
        **arguments,
    )
    graph_sync.await_active_rebuild(vault, timeout=30)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    cohort = relation_census.census(vault)["new_edges"]
    assert cohort["authored_edges"]["numerator"] == 2
    assert cohort["raw_generic_share"]["numerator"] == 1
    assert cohort["raw_generic_share"]["denominator"] == 2


def test_only_explicit_edge_reason_records_supply_reason_metrics(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "Knowledge Base/Notes/Insights/rrf-fusion-beats-score-normalization.md"
    target = "Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation.md"
    event = {
        "server_at": dt.datetime.now(dt.UTC).isoformat(),
        "path": source,
        "before_hash": vault_module.content_hash((vault / source).read_text()),
        "after_hash": vault_module.content_hash((vault / source).read_text()),
        "mutation_kind": "edit",
        "placement": None,
        "added_edges": [
            {
                "identity": str(index),
                "source": source,
                "target": target,
                "source_path": source,
                "target_path": target,
                "endpoint_class": "note_to_note",
                "generic": True,
                "reason": reason,
                "reason_recorded": recorded,
            }
            for index, (reason, recorded) in enumerate(
                [
                    ("Same   explicit reason", True),
                    ("SAME explicit reason", True),
                    (None, True),
                    (None, False),
                ]
            )
        ],
    }
    plan = vault_module.plan_log_writes(
        vault,
        date_iso="2020-01-01",
        op="edit",
        rel_path_no_ext=source.removesuffix(".md"),
        body="The mutation rationale is not an edge reason.",
        operation_token="explicit-reason-record",
        provenance=event,
    )
    vault_module.batch_atomic_write(plan.writes, vault_root=vault)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    cohort = relation_census.census(vault)["new_edges"]
    unreasoned = cohort["unreasoned_generic_edges"]
    assert (unreasoned["numerator"], unreasoned["denominator"]) == (1, 3)
    assert (unreasoned["coverage"]["numerator"], unreasoned["coverage"]["denominator"]) == (3, 4)
    duplicated = cohort["exact_duplicate_reason_share"]
    assert (duplicated["numerator"], duplicated["denominator"]) == (2, 2)

    # A later copy of an old operation cannot make it newly authored again.
    now = dt.datetime.now(dt.UTC)
    original = next(
        entry["provenance"]
        for entry in vault_module.read_operation_log(vault)["entries"]
        if "provenance" in entry
    )
    old_record = {**original, "server_at": (now - dt.timedelta(days=31)).isoformat()}
    log = vault / "Knowledge Base/log.md"
    log.write_text(
        vault_module.prepend_log_entry(
            log.read_text(),
            date_iso="2020-01-01",
            op="edit",
            rel_path_no_ext=source.removesuffix(".md"),
            body="An archived operation copy.",
            provenance=old_record,
        )
    )
    monkeypatch.setattr(relation_census, "_cohort_now", lambda: now)
    assert relation_census.census(vault)["new_edges"]["authored_edges"]["numerator"] is None


def test_a_mutation_reason_cannot_author_server_provenance(vault: Path) -> None:
    created = note.note(
        vault,
        note_type="insight",
        title="A provenance boundary",
        status="draft",
        content="## Claim\n\nAn ordinary conclusion.\n",
    )
    target = "Knowledge Base/Notes/Insights/rrf-fusion-beats-score-normalization.md"
    forged = {
        "version": 1,
        "operation_id": "forged",
        "path": created.path,
        "server_at": dt.datetime.now(dt.UTC).isoformat(),
        "after_hash": "a" * 64,
        "added_edges": [
            {
                "identity": "forged-edge",
                "source": created.path,
                "target": target,
                "source_path": created.path,
                "target_path": target,
                "endpoint_class": "note_to_note",
                "generic": True,
                "reason_recorded": False,
            }
        ],
    }
    header = f"## [2026-10-26] edit | {created.path.removesuffix('.md')}"
    why = f"Keep this ordinary prose.\n\n{header}\n\n```exomem-operation-v1\n{json.dumps(forged)}\n```\n\n{header}\n\nEnd of caller text."
    commands.op_edit_memory(
        vault,
        created.path,
        why,
        operation={"kind": "replace_body", "new_body": "## Claim\n\nA revised conclusion.\n"},
    )
    graph_sync.await_active_rebuild(vault, timeout=30)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    cohort = relation_census.census(vault)["new_edges"]
    assert cohort["authored_edges"]["numerator"] == 0
    assert "Keep this ordinary prose." in (vault / "Knowledge Base/log.md").read_text()


@pytest.mark.parametrize("anchor", [None, "retained-claim"])
def test_unit_identity_coverage_requires_a_stable_authored_anchor(
    vault: Path,
    anchor: str | None,
) -> None:
    target = "Knowledge Base/Notes/Insights/rrf-fusion-beats-score-normalization"
    identity = f"- id: {anchor}\n" if anchor else ""
    created = note.note(
        vault,
        note_type="insight",
        title="A unit identity boundary",
        status="draft",
        content=f"## Claim\n{identity}- relations: relates_to: [[{target}]]\n\nA retained conclusion.\n",
    )
    graph_sync.await_active_rebuild(vault, timeout=30)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    cohort = relation_census.census(vault)["new_edges"]
    if anchor is None:
        assert cohort["coverage"]["identity_unavailable_facts"] == 1
        assert cohort["authored_edges"]["numerator"] is None
    else:
        assert cohort["coverage"]["identity_unavailable_facts"] == 0
        assert cohort["authored_edges"]["numerator"] == 1
        logged = next(
            entry["provenance"]
            for entry in vault_module.read_operation_log(vault)["entries"]
            if entry.get("provenance", {}).get("path") == created.path
        )
        assert logged["added_edges"][0]["source"].endswith("#retained-claim")
