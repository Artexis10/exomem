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
from exomem.vault import content_hash


def test_draft_retry_after_log_publication_keeps_one_activity(vault, monkeypatch) -> None:
    """A draft has no review record to retain a clock value across a pending retry."""
    arguments = {
        "content": "## Claim\n\nA retained conclusion.\n",
        "note_type": "insight", "title": "Structural retry", "status": "draft",
        "today": dt.date(2026, 10, 26),
    }
    draft = note.note(vault, validate_only=True, **arguments)
    retry = {"draft_id": draft.draft_id, "draft_token": draft.draft_token,
             "draft_hash": draft.draft_hash}
    before = len(vault_module.read_operation_log(vault)["entries"])
    publish = vault_module._after_batch_destination_published

    class SimulatedProcessDeath(BaseException):
        pass

    def die_after_log(path):
        publish(path)
        if path == vault / "Knowledge Base/log.md":
            raise SimulatedProcessDeath

    with monkeypatch.context() as crash:
        crash.setattr(vault_module, "_after_batch_destination_published", die_after_log)
        with pytest.raises(SimulatedProcessDeath):
            note.note(vault, **arguments, **retry)
    assert not (vault / draft.destination).exists()

    saved = note.note(vault, **arguments, **retry)

    assert (vault / saved.path).is_file()
    assert len(vault_module.read_operation_log(vault)["entries"]) == before + 1


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
        "expected_hash": content_hash((vault / created.path).read_text(encoding="utf-8")),
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
        "content": f"## Claim\n\nA retained conclusion.\n\n## Relations\n\n- extends [[{target.removesuffix('.md')}]]\n",
        "note_type": "insight",
        "title": "Authored additions",
        "status": "active",
        "today": dt.date(2020, 1, 1),
    }
    draft = note.note(vault, validate_only=True, **draft_arguments)
    created = note.note(
        vault, draft_token=draft.draft_token, draft_id=draft.draft_id,
        draft_hash=draft.draft_hash, relation_review_hash=draft.draft_hash, **draft_arguments
    )
    path = created.path
    _, body, _ = vault_module.parse_frontmatter((vault / path).read_text())
    extra = "Knowledge Base/Notes/Insights/rrf-fusion-beats-score-normalization"
    changed_body = body + f"\n- links_to [[{extra}]]\n"
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
        "operation": {
            "kind": "replace_body",
            "new_body": changed_body,
            "expected_hash": content_hash((vault / path).read_text(encoding="utf-8")),
        },
    }
    with request_scope(owner_principal(surface="mcp")):
        written = bound(**arguments)
        replay = bound(**arguments)
    assert replay == written
    # Respelling an edge during a real edit resolves to the same stable endpoint.
    commands.op_edit_memory(
        vault,
        path,
        "Use the target's short address.",
        operation={
            "kind": "replace_body",
            "new_body": (changed_body + "\nClarify the conclusion without another relation.\n").replace(
                target.removesuffix(".md"), Path(target).stem
            ),
            "expected_hash": content_hash((vault / path).read_text(encoding="utf-8")),
        },
    )
    graph_sync.await_active_rebuild(vault, timeout=30)
    graph_sync.await_active_rebuild(vault, state_root=manager.config.state_dir, timeout=30)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    before = relation_census.census(vault)
    cohort = before["new_edges"]
    assert cohort["authored_edges"]["numerator"] == 2
    # No real write adds a generic edge, so the log classes both edges and none is generic.
    assert cohort["endpoint_classes"]["note_to_entity"]["raw_generic_share"]["numerator"] == 0
    assert cohort["endpoint_classes"]["note_to_note"]["raw_generic_share"]["denominator"] == 1
    assert (cohort["raw_generic_share"]["numerator"], cohort["raw_generic_share"]["denominator"]) == (0, 2)
    # One edge names a specific relation and the other is a plain link.
    assert (cohort["specific_share"]["numerator"], cohort["specific_share"]["denominator"]) == (1, 2)
    assert (cohort["plain_link_share"]["numerator"], cohort["plain_link_share"]["denominator"]) == (1, 2)
    assert cohort["referents"]["acted"]["numerator"] is None
    assert cohort["placement_by_basis"]["numerator"] == 1
    assert cohort["coverage"]["status"] == "partial"
    assert cohort["review"]["gate"] is False
    assert cohort["review"]["needed"] is False

    monkeypatch.setenv("EXOMEM_LOG_ROTATE_BYTES", "1")
    monkeypatch.setattr(vault_module, "LOG_ROTATE_KEEP_ENTRIES", 1)
    vault_module.rotate_log_if_needed(vault)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    after = relation_census.census(vault)
    assert after["metrics"] == before["metrics"]
    assert after["new_edges"]["authored_edges"]["numerator"] == 2
    assert after["new_edges"]["placement_by_basis"]["numerator"] == 1


_NOTE = "Knowledge Base/Notes/Insights/rrf-fusion-beats-score-normalization.md"
_OTHER = "Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation.md"


def _log_added_edges(vault: Path, edges: list[dict]) -> None:
    """Append one governed edit to the operation log, as an earlier release recorded it."""
    event = {
        "server_at": dt.datetime.now(dt.UTC).isoformat(),
        "path": _NOTE,
        "before_hash": content_hash((vault / _NOTE).read_text()),
        "after_hash": content_hash((vault / _NOTE).read_text()),
        "mutation_kind": "edit",
        "placement": None,
        "added_edges": [
            {
                "identity": str(index),
                "source": _NOTE,
                "target": _OTHER,
                "source_path": _NOTE,
                "target_path": _OTHER,
                "endpoint_class": "note_to_note",
                "reason": None,
                "reason_recorded": False,
                **edge,
            }
            for index, edge in enumerate(edges)
        ],
    }
    plan = vault_module.plan_log_writes(
        vault,
        date_iso="2020-01-01",
        op="edit",
        rel_path_no_ext=_NOTE.removesuffix(".md"),
        body="A recorded edit.",
        operation_token="logged-edges",
        provenance=event,
    )
    vault_module.batch_atomic_write(plan.writes, vault_root=vault)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()


def test_specific_and_plain_link_shares_count_authored_relations_only(vault: Path) -> None:
    _log_added_edges(
        vault,
        [
            {"generic": False, "origin": "markdown_relation", "relation": "supports"},
            {"generic": False, "origin": "markdown_relation", "relation": "refines"},
            {"generic": False, "origin": "markdown_relation", "relation": "links_to"},
            # A body wikilink is not an authored relation.
            {"generic": False, "origin": "wikilink", "relation": "links_to"},
        ],
    )

    cohort = relation_census.census(vault)["new_edges"]

    assert (cohort["specific_share"]["numerator"], cohort["specific_share"]["denominator"]) == (2, 3)
    assert (cohort["plain_link_share"]["numerator"], cohort["plain_link_share"]["denominator"]) == (1, 3)


def test_an_extension_of_the_plain_link_counts_as_a_plain_link(vault: Path) -> None:
    overlay = vault / "Knowledge Base/_Schema/relation-registry.yaml"
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(
        "schema_version: 1\nextensions:\n"
        "  x.see_also: {parent: links_to, description: Points to related reading}\n"
    )
    _log_added_edges(
        vault,
        [
            {"generic": False, "origin": "markdown_relation", "relation": "x.see_also"},
            {"generic": False, "origin": "markdown_relation", "relation": "supports"},
        ],
    )

    cohort = relation_census.census(vault)["new_edges"]

    assert (cohort["specific_share"]["numerator"], cohort["specific_share"]["denominator"]) == (1, 2)
    assert (cohort["plain_link_share"]["numerator"], cohort["plain_link_share"]["denominator"]) == (1, 2)


def test_specific_and_plain_link_shares_are_unavailable_when_the_log_names_no_relation(
    vault: Path,
) -> None:
    _log_added_edges(vault, [{"generic": False, "origin": "markdown_relation"}])

    cohort = relation_census.census(vault)["new_edges"]

    assert cohort["raw_generic_share"]["numerator"] == 0
    assert cohort["specific_share"]["numerator"] is None
    assert cohort["plain_link_share"]["numerator"] is None


def test_the_generic_share_review_follows_the_configured_target(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An earlier release could keep a generic edge to an entity; the log still carries them.
    _log_added_edges(
        vault,
        [{"generic": True, "origin": "markdown_relation", "relation": "relates_to", "endpoint_class": "note_to_entity"}],
    )

    reviewed = commands.op_review_memory(
        vault, mode="audit", categories=[audit.RELATION_GENERIC_SHARE_CATEGORY], detail="full"
    )
    assert len(reviewed["findings"]) == 1
    assert reviewed["findings"][0]["severity"] == "info"
    assert reviewed["findings"][0]["meta"]["measurement"]["numerator"] == 1
    config = vault.parent / "review-target.json"
    config.write_text(json.dumps({"relation_census": {"entity_generic_review_share": 1}}))
    monkeypatch.setenv("EXOMEM_CONFIG_PATH", str(config))
    assert commands.op_review_memory(
        vault, mode="audit", categories=[audit.RELATION_GENERIC_SHARE_CATEGORY], detail="full"
    )["findings"] == []


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
        content=f"## Claim\n\nA retained conclusion.\n\n## Relations\n\n- refines [[{carried}]]\n",
    )
    arguments = {
        "old_path": old.path,
        "note_type": "insight",
        "title": "Revised accounting conclusion",
        "content": f"## Claim\n\nAn updated conclusion.\n\n## Relations\n\n- refines [[{carried}]]\n- supports [[{extra}]]\n",
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
    assert (cohort["specific_share"]["numerator"], cohort["specific_share"]["denominator"]) == (2, 2)
    assert cohort["plain_link_share"]["numerator"] == 0


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
        operation={
            "kind": "replace_body",
            "new_body": "## Claim\n\nA revised conclusion.\n",
            "expected_hash": content_hash((vault / created.path).read_text(encoding="utf-8")),
        },
    )
    graph_sync.await_active_rebuild(vault, timeout=30)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    cohort = relation_census.census(vault)["new_edges"]
    assert cohort["authored_edges"]["numerator"] is None
    assert cohort["coverage"]["unknown_server_time_entries"] >= 2
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
        status="active",
        content=f"## Claim\n{identity}- relations: supports: [[{target}]]\n\nA retained conclusion.\n",
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
