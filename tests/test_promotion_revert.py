"""Reverting a recent promotion: one call, bound to what the caller read.

These tests drive the real writers, the real review tools and the real curation
executor; they change only the caller identity and, for the withheld case, the policy.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import command_surface, commands, promotions, review_state
from exomem import due_state as due_state_module

ENTITIES = "Knowledge Base/Entities/Concepts"
NOTE = "Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation.md"
OTHER_NOTE = "Knowledge Base/Notes/Insights/rrf-fusion-beats-score-normalization.md"


@pytest.fixture(autouse=True)
def _clean_emission_state():
    from exomem.governance.principal import library_scope

    due_state_module.reset_emission_state()
    with library_scope():
        yield
    due_state_module.reset_emission_state()


def _as(monkeypatch: pytest.MonkeyPatch, session: str | None) -> None:
    monkeypatch.setattr(
        command_surface,
        "mcp_caller_identity",
        lambda: {
            "client_name": None, "client_version": None, "app_id": None,
            "transport": "stdio" if session else None, "session_id": session,
        },
    )


def _create_entity(vault: Path, name: str, *, ref: str | None = None, **extra) -> str:
    return commands.op_connect_memory(
        vault,
        operation="create-entity",
        entity_type="concept",
        name=name,
        summary="A synthetic accounting concept used to exercise reverts.",
        ref=ref,
        **extra,
    )["path"]


def _append_relation(vault: Path, path: str, bullet: str) -> None:
    from exomem.vault import parse_frontmatter

    _meta, body, _text = parse_frontmatter((vault / path).read_text(encoding="utf-8"))
    section = "" if "## Relations" in body else "\n## Relations\n"
    commands.op_edit_memory(
        vault, path, "Author a synthetic relation.",
        operation={"kind": "replace_body", "new_body": f"{body.rstrip()}\n{section}{bullet}\n"},
    )


def _without_stamp(text: str) -> str:
    return "".join(line for line in text.splitlines(keepends=True) if not line.startswith("updated:"))


def _entry_ref(vault: Path, kind: str) -> str:
    ledger = review_state.ReviewStateStore(vault).load()["promotions"]
    ids = [key for key, row in ledger.items() if row["kind"] == kind]
    assert ids, f"no {kind} entry in the ledger"
    return promotions.promotion_ref(ids[-1])


def _revert(vault: Path, ref: str, fingerprint: str | None, why: str = "The user asked to undo it.") -> dict:
    return commands.op_maintain_memory(
        vault, mode="curation", curation_action="revert", review_ref=ref,
        expected_plan_fingerprint=fingerprint, why=why,
    )


def _context(vault: Path, ref: str) -> dict:
    return commands.op_review_item_context(vault, ref)


def _edge_vault(vault: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str, str]:
    """Two entities and one promoted edge between them; returns (subject, target, edge ref)."""
    _as(monkeypatch, "session-a")
    target = _create_entity(vault, "Tidal Ledger")
    subject = _create_entity(vault, "Harbour Reconciliation")
    _append_relation(vault, subject, f"- supports [[{target.removesuffix('.md')}]]")
    return subject, target, _entry_ref(vault, "edge")


def test_an_edge_reverts_in_one_call_and_only_that_bullet_goes(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject, target, ref = _edge_vault(vault, monkeypatch)
    _append_relation(vault, subject, "- relates_to [[Knowledge Base/Notes/Insights/rrf-fusion-beats-score-normalization]]")
    page = vault / subject
    before = page.read_text(encoding="utf-8")
    edge_line = f"- supports [[{target.removesuffix('.md')}]]\n"
    assert edge_line in before

    _as(monkeypatch, "session-b")
    fingerprint = _context(vault, ref)["revert_fingerprint"]
    result = _revert(vault, ref, fingerprint)

    assert result["outcome"] == "reverted"
    after = page.read_text(encoding="utf-8")
    # The editor restamps `updated`; everything else is as the page was, minus the bullet.
    assert _without_stamp(after) == _without_stamp(before).replace(edge_line, ""), (
        "exactly that bullet left, the rest is byte for byte"
    )
    assert _context(vault, ref)["item"]["subjects"] == [], "the entry no longer holds"
    assert _revert(vault, ref, fingerprint)["outcome"] == "absent", "a repeat reports absent"


def test_one_notice_with_two_edges_reverts_both_in_one_call(
    vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _as(monkeypatch, "creator")
    first = _create_entity(vault, "Tidal Ledger")
    second = _create_entity(vault, "Harbour Ledger")
    subject = _create_entity(vault, "Tidal Accounts", relations=[
        {"kind": "supports", "target": first},
        {"kind": "supports", "target": second},
    ])
    ref = _entry_ref(vault, "edge")
    _as(monkeypatch, "reader")
    preview = _context(vault, ref)
    assert len(preview["item"]["subjects"]) == 2

    result = _revert(vault, ref, preview["revert_fingerprint"])

    assert result["outcome"] == "reverted"
    assert len(result["removed"]) == 2
    assert not promotions.edge_present(vault, subject, "supports", first)
    assert not promotions.edge_present(vault, subject, "supports", second)


def test_an_endpoint_edit_invalidates_the_callers_full_revert_preview(
    vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject, target, ref = _edge_vault(vault, monkeypatch)
    preview = _context(vault, ref)
    _link_from(vault, target, subject)
    before = (vault / subject).read_bytes()

    with pytest.raises(ValueError, match="CURATION_PLAN_IDENTITY_MISMATCH"):
        _revert(vault, ref, preview["revert_fingerprint"])

    assert (vault / subject).read_bytes() == before


def test_a_revert_needs_the_current_fingerprint_and_a_stale_one_changes_nothing(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject, _target, ref = _edge_vault(vault, monkeypatch)
    page = vault / subject
    _as(monkeypatch, "session-b")
    fingerprint = _context(vault, ref)["revert_fingerprint"]

    _append_relation(vault, subject, "- relates_to [[Knowledge Base/Notes/Insights/rrf-fusion-beats-score-normalization]]")
    edited = page.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="CURATION_PLAN_IDENTITY_MISMATCH"):
        _revert(vault, ref, fingerprint)
    with pytest.raises(ValueError, match="CURATION_PLAN_IDENTITY_MISMATCH"):
        _revert(vault, ref, "0" * 64)
    assert page.read_text(encoding="utf-8") == edited, "a refused revert changes nothing"

    assert _revert(vault, ref, _context(vault, ref)["revert_fingerprint"])["outcome"] == "reverted"


def test_the_leaf_keeps_an_unrelated_edit_made_after_the_plan_was_read(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import promotion_revert

    subject, target, _ref = _edge_vault(vault, monkeypatch)
    page = vault / subject
    _append_relation(vault, subject, "- relates_to [[Knowledge Base/Notes/Insights/rrf-fusion-beats-score-normalization]]")

    promotion_revert.remove_relation(
        vault, subject=subject, predicate="supports", target=target, why="Revert it."
    )

    text = page.read_text(encoding="utf-8")
    assert "rrf-fusion-beats-score-normalization" in text, "the concurrent edit survived"
    assert f"[[{target.removesuffix('.md')}]]" not in text


def _link_from(vault: Path, note: str, entity: str) -> None:
    """Make an ordinary note link to an entity, as a later write would."""
    from exomem.vault import parse_frontmatter

    _meta, body, _text = parse_frontmatter((vault / note).read_text(encoding="utf-8"))
    commands.op_edit_memory(
        vault, note, "Mention the entity.",
        operation={
            "kind": "replace_body",
            "new_body": f"{body.rstrip()}\n\nSee also [[{entity.removesuffix('.md')}]].\n",
        },
    )


def test_an_entity_reverts_to_the_trash_and_names_every_link_but_the_one_that_made_it(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as(monkeypatch, "session-a")
    entity = _create_entity(vault, "Tidal Ledger", ref=NOTE)
    _link_from(vault, NOTE, entity)
    ref = _entry_ref(vault, "entity")

    _as(monkeypatch, "session-b")
    first = _context(vault, ref)
    assert first["dependants"] == [], "the originating note's link is not a dependant"

    _link_from(vault, OTHER_NOTE, entity)
    with pytest.raises(ValueError, match="CURATION_PLAN_IDENTITY_MISMATCH"):
        _revert(vault, ref, first["revert_fingerprint"])
    assert (vault / entity).is_file(), "a stale revert changes nothing"

    second = _context(vault, ref)
    assert [link["path"] for link in second["dependants"]] == [OTHER_NOTE]
    assert second["revert_fingerprint"] != first["revert_fingerprint"]

    result = _revert(vault, ref, second["revert_fingerprint"])

    assert result["outcome"] == "reverted"
    assert not (vault / entity).exists(), "the page left the vault"
    trashed = list((vault / "Knowledge Base" / "_trash").rglob("*Tidal Ledger.md".replace(" ", "*")))
    assert trashed, "and went to the trash, where it can be recovered"
    assert entity.removesuffix(".md") in (vault / OTHER_NOTE).read_text(encoding="utf-8"), (
        "a dependant the caller named is left as it was"
    )


def test_an_old_entity_promotion_cannot_revert_a_replacement_at_the_same_path(
    vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import memory_refs

    _as(monkeypatch, "creator")
    path = _create_entity(vault, "Tidal Replacement")
    ref = _entry_ref(vault, "entity")
    first = _context(vault, ref)
    old_identity = memory_refs.ref_from_markdown((vault / path).read_text())
    commands.op_manage_memory_file(vault, operation="delete", path=path, confirm=True)
    replacement = _create_entity(vault, "Tidal Replacement")
    before = (vault / replacement).read_bytes()
    assert replacement == path
    assert memory_refs.ref_from_markdown(before.decode()) != old_identity

    _as(monkeypatch, "reader")
    assert _context(vault, ref)["revert"] is None
    assert _revert(vault, ref, first["revert_fingerprint"])["outcome"] == "absent"
    assert (vault / replacement).read_bytes() == before


def test_entity_revert_rechecks_the_reviewed_bytes_at_the_trash_boundary(
    vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import graph_sync

    _as(monkeypatch, "creator")
    path = _create_entity(vault, "Tidal Preview")
    ref = _entry_ref(vault, "entity")
    _as(monkeypatch, "reader")
    preview = _context(vault, ref)
    page = vault / path
    before = page.read_text()
    replacement = before.replace("A synthetic accounting concept", "A changed accounting concept")
    begin = graph_sync.begin_deletion_transition

    def replace_before_capture(*args, **kwargs):
        page.write_text(replacement)
        return begin(*args, **kwargs)

    monkeypatch.setattr(graph_sync, "begin_deletion_transition", replace_before_capture)
    with pytest.raises(ValueError, match="STALE_DELETE"):
        _revert(vault, ref, preview["revert_fingerprint"])

    assert page.read_text() == replacement


def test_an_entity_revert_removes_the_edges_of_its_own_write_and_no_other_edge(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as(monkeypatch, "session-a")
    entity = _create_entity(vault, "Tidal Ledger")
    kept = _create_entity(vault, "Harbour Reconciliation")
    other = _create_entity(vault, "Ledger Audit")
    stem = entity.removesuffix(".md")
    _append_relation(vault, kept, f"- supports [[{stem}]]")
    _append_relation(vault, other, f"- supports [[{stem}]]")
    entity_id, entity_entry = next(
        (key, row) for key, row in review_state.ReviewStateStore(vault).load()["promotions"].items()
        if row["kind"] == "entity" and any(subject["path"] == entity for subject in row["subjects"])
    )
    # `kept`'s bullet was authored by the write that made the entity; `other`'s was not.
    own_subject = next(
        subject for row in review_state.ReviewStateStore(vault).load()["promotions"].values()
        if row["kind"] == "edge" for subject in row["subjects"] if subject["subject"] == kept
    )
    promotions.record(vault, "edge", [own_subject], write=entity_entry["write"])
    entity_ref = promotions.promotion_ref(entity_id)

    _as(monkeypatch, "session-b")
    context = _context(vault, entity_ref)
    assert [link["path"] for link in context["dependants"]] == [other], "only the other write's edge is named"
    _link_from(vault, kept, entity)
    with pytest.raises(ValueError, match="CURATION_PLAN_IDENTITY_MISMATCH"):
        _revert(vault, entity_ref, context["revert_fingerprint"])
    context = _context(vault, entity_ref)
    assert [link["path"] for link in context["dependants"]] == [kept, other], (
        "the prose link survives removing the own-write edge and must be reviewed"
    )
    _revert(vault, entity_ref, context["revert_fingerprint"])

    assert not (vault / entity).exists()
    kept_text = (vault / kept).read_text(encoding="utf-8")
    assert f"- supports [[{stem}]]" not in kept_text, "the write's own edge went"
    assert f"See also [[{stem}]]" in kept_text, "the reviewed prose link stayed"
    assert f"[[{stem}]]" in (vault / other).read_text(encoding="utf-8"), "another write's edge stayed"


def test_an_old_edge_notice_cannot_remove_an_edge_added_after_removal(
    vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import datetime as dt

    from exomem import edit, promotion_revert, temporal
    from exomem.vault import parse_frontmatter

    subject, target, ref = _edge_vault(vault, monkeypatch)
    preview = _context(vault, ref)
    updated = temporal.parse(parse_frontmatter((vault / subject).read_text())[0]["updated"]).instant
    # Distinct write stamps keep this ended-incarnation case separate from cycle refusal.
    stamps = iter([temporal.stamp(updated + dt.timedelta(seconds=offset)) for offset in (1, 2)])
    monkeypatch.setattr(edit, "_resolve_date_iso", lambda *_args: next(stamps))
    promotion_revert.remove_relation(
        vault, subject=subject, predicate="supports", target=target, why="Remove this edge."
    )
    _append_relation(vault, subject, f"- supports [[{target.removesuffix('.md')}]]")
    before = (vault / subject).read_bytes()
    assert _context(vault, ref)["revert"] is None
    assert _revert(vault, ref, preview["revert_fingerprint"])["outcome"] == "absent"
    assert (vault / subject).read_bytes() == before


def test_an_old_edge_notice_cannot_remove_a_replacement_endpoint(
    vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject, target, ref = _edge_vault(vault, monkeypatch)
    preview = _context(vault, ref)
    commands.op_manage_memory_file(vault, operation="delete", path=target, confirm=True,
                                   expected_dead_inbound=[subject])
    assert _create_entity(vault, "Tidal Ledger") == target
    before = (vault / subject).read_bytes()
    assert _context(vault, ref)["revert"] is None
    assert _revert(vault, ref, preview["revert_fingerprint"])["outcome"] == "absent"
    assert (vault / subject).read_bytes() == before


def test_an_old_registry_notice_cannot_remove_a_reintroduced_key(
    vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_recent_promotions import _save_relation

    from exomem.governance.principal import library_scope

    _as(monkeypatch, "creator")
    with library_scope():
        _save_relation(vault, "vault.accounts_for", parent="supports")
        ref = _entry_ref(vault, "registry")
        preview = _context(vault, ref)
        arguments = dict(preview["revert"]["arguments"])
        arguments["why"] = "Remove the earlier definition."
        commands.op_schema_memory(vault, **arguments)
        _save_relation(vault, "vault.accounts_for", parent="supports")
        current = commands.op_schema_memory(vault, subject="relations", operation="inspect")
        arguments["expected_hash"] = current["content_hash"]
        assert _context(vault, ref)["revert"] is None
        with pytest.raises(ValueError, match="PROMOTION_SUBJECT_(CHANGED|UNVERIFIED)"):
            commands.op_schema_memory(vault, **arguments)
        assert commands.op_schema_memory(vault, subject="relations", operation="inspect")["content_hash"] == current["content_hash"]


def test_registry_undo_survives_an_unrelated_save_and_an_exact_self_loop(
    vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_recent_promotions import _save_relation

    _as(monkeypatch, "creator")
    saved = _save_relation(vault, "vault.accounts_for", parent="supports")
    ref = _entry_ref(vault, "registry")
    _save_relation(vault, "vault.accounts_for", parent="supports")
    _save_relation(vault, "vault.audits", parent="supports")
    context = _context(vault, ref)
    arguments = dict(context["revert"]["arguments"])
    arguments["why"] = "Undo only the original addition."
    result = commands.op_schema_memory(vault, **arguments)
    assert result["saved"] is not None
    from exomem import relation_registry

    entries = relation_registry.load_registry(vault).extensions
    assert "vault.accounts_for" not in entries
    assert "vault.audits" in entries
    assert saved["saved"]["history"]["version"]


def test_legacy_registry_history_disables_only_the_promotion_undo(
    vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from test_recent_promotions import _save_relation

    from exomem import registry_history
    from exomem.vocabulary import instances, registry_spec

    _as(monkeypatch, "creator")
    _save_relation(vault, "vault.accounts_for", parent="supports")
    ref = _entry_ref(vault, "registry")
    _save_relation(vault, "vault.audits", parent="supports")
    spec = instances.select(vault, registry_spec("relations"))
    history = registry_history.history_dir(vault, spec.stem)
    latest = sorted(history.glob("*.yaml"))[-1]
    header, body = registry_history._split(latest.read_text())
    header.pop("before_source_hash")
    header.pop("after_source_hash")
    latest.write_text("# exomem-history: " + json.dumps(header) + "\n" + body)
    context = _context(vault, ref)
    assert context["revert"] is None
    assert context["findings"][0]["code"] == "PROMOTION_SUBJECT_UNVERIFIED"
    assert _save_relation(vault, "vault.reconciles", parent="supports")["saved"] is not None


def test_pruned_registry_introduction_is_explicitly_unavailable(
    vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_recent_promotions import _save_relation

    from exomem import registry_history

    _as(monkeypatch, "creator")
    _save_relation(vault, "vault.accounts_for", parent="supports")
    ref = _entry_ref(vault, "registry")
    monkeypatch.setattr(registry_history, "HISTORY_KEEP", 1)
    _save_relation(vault, "vault.audits", parent="supports")
    context = _context(vault, ref)
    assert context["revert"] is None
    assert context["findings"][0]["code"] == "PROMOTION_SUBJECT_UNVERIFIED"


@pytest.mark.parametrize("gap", ["missing", "capped", "legacy"])
def test_incomplete_edge_proof_stays_unavailable_while_ordinary_edits_work(
    vault: Path, monkeypatch: pytest.MonkeyPatch, gap: str,
) -> None:
    from exomem import compiled_history, memory_refs

    subject, _target, ref = _edge_vault(vault, monkeypatch)
    _append_relation(vault, subject, "- relates_to [[Knowledge Base/Notes/Insights/rrf-fusion-beats-score-normalization]]")
    source_ref = memory_refs.ref_from_markdown((vault / subject).read_text())
    history = vault / compiled_history.history_dir(source_ref)
    if gap == "missing":
        sorted(history.glob("*.json"))[0].unlink()
    elif gap == "capped":
        monkeypatch.setattr(compiled_history, "_scan_cap", lambda _size: 1)
    else:
        promotion_id = promotions.parse_promotion_ref(ref)
        review_state.ReviewStateStore(vault).edit_promotions(
            lambda ledger: ledger[promotion_id]["subjects"][0].pop("introduction")
        )
    context = _context(vault, ref)
    assert context["revert"] is None
    assert context["findings"][0]["code"] == "PROMOTION_SUBJECT_UNVERIFIED"
    with pytest.raises(ValueError, match="PROMOTION_SUBJECT_UNVERIFIED"):
        _revert(vault, ref, "0" * 64)
    page = vault / subject
    commands.op_edit_memory(
        vault, subject, "An ordinary edit does not require an undo proof.",
        operation={"kind": "replace_string", "old_string": "synthetic accounting", "new_string": "revised accounting"},
    )
    assert "revised accounting" in page.read_text()
    assert _context(vault, ref)["item"]["subjects"], "unknown is not settled as absent"


def test_an_exact_compiled_self_loop_keeps_a_continuous_edge_undo_available(
    vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import datetime as dt
    import json
    import uuid

    from exomem import compiled_history, memory_refs, registry_history
    from exomem.vault import content_hash

    subject, _target, ref = _edge_vault(vault, monkeypatch)
    source = (vault / subject).read_text()
    parent_ref = memory_refs.ref_from_markdown(source)
    history = vault / compiled_history.history_dir(parent_ref)
    entry = json.loads(next(history.glob("*.json")).read_text())
    moment = dt.datetime.now(dt.UTC)
    entry.update({
        "transition_id": str(uuid.uuid4()), "at": moment.isoformat(),
        "before_hash": content_hash(source), "after_hash": content_hash(source),
        "prior": {"source": source}, "affected_units": [],
    })
    name = registry_history.version_id(moment, entry["transition_id"]) + ".json"
    (history / name).write_text(json.dumps(entry))
    context = _context(vault, ref)
    assert context["revert"] is not None
    assert _revert(vault, ref, context["revert_fingerprint"])["outcome"] == "reverted"


def test_a_move_gap_with_an_earlier_remove_readd_cycle_cannot_authorize_undo(
    vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import edit, promotion_revert, temporal
    from exomem.vault import parse_frontmatter

    subject, target, ref = _edge_vault(vault, monkeypatch)
    original = (vault / subject).read_bytes()
    updated = temporal.parse(parse_frontmatter(original.decode())[0]["updated"]).instant
    monkeypatch.setattr(edit, "_resolve_date_iso", lambda *_args: temporal.stamp(updated))
    moved = target.replace("Tidal Ledger", "Moved Ledger")
    commands.op_manage_memory_file(vault, operation="move", old_path=target, new_path=moved)
    promotion_revert.remove_relation(
        vault, subject=subject, predicate="supports", target=moved, why="Remove the moved edge."
    )
    _append_relation(vault, subject, f"- supports [[{moved.removesuffix('.md')}]]")
    commands.op_manage_memory_file(vault, operation="move", old_path=moved, new_path=target)
    # Keep the supported move's exact endpoint restoration independent of the writer clock.
    assert f"[[{target.removesuffix('.md')}]]" in (vault / subject).read_text()
    context = _context(vault, ref)
    assert context["revert"] is None
    assert context["findings"][0]["code"] == "PROMOTION_SUBJECT_UNVERIFIED"
    with pytest.raises(ValueError, match="PROMOTION_SUBJECT_UNVERIFIED"):
        _revert(vault, ref, "0" * 64)
    assert (vault / subject).read_bytes() == original


def test_edge_undo_guards_the_endpoint_until_the_atomic_commit(
    vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uuid

    from exomem import memory_refs
    from exomem import vault as vault_module

    subject, target, ref = _edge_vault(vault, monkeypatch)
    context = _context(vault, ref)
    before = (vault / subject).read_bytes()
    batch = vault_module.batch_atomic_write
    replaced = False

    def replace_endpoint(writes, **kwargs):
        nonlocal replaced
        if not replaced and kwargs.get("required_guards") and any(write.path == vault / subject for write in writes):
            source = (vault / target).read_text()
            original = memory_refs.parse_memory_ref(memory_refs.ref_from_markdown(source))
            (vault / target).write_text(source.replace(original, str(uuid.uuid4())))
            replaced = True
        return batch(writes, **kwargs)

    monkeypatch.setattr(vault_module, "batch_atomic_write", replace_endpoint)
    with pytest.raises(ValueError, match="(PATH_GUARD|PROMOTION_SUBJECT_CHANGED)"):
        _revert(vault, ref, context["revert_fingerprint"])
    assert replaced
    assert (vault / subject).read_bytes() == before


def test_registry_undo_guards_retained_history_until_the_atomic_commit(
    vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_recent_promotions import _save_relation

    from exomem import registry_history
    from exomem import vault as vault_module
    from exomem.vocabulary import instances, registry_spec

    _save_relation(vault, "vault.accounts_for", parent="supports")
    ref = _entry_ref(vault, "registry")
    context = _context(vault, ref)
    spec = instances.select(vault, registry_spec("relations"))
    before = spec.overlay(vault).read_bytes()
    history = next(registry_history.history_dir(vault, spec.stem).glob("*.yaml"))
    batch = vault_module.batch_atomic_write
    changed = False

    def change_history(writes, **kwargs):
        nonlocal changed
        if not changed and kwargs.get("required_guards"):
            history.write_text(history.read_text().replace("synthetic", "different"))
            changed = True
        return batch(writes, **kwargs)

    monkeypatch.setattr(vault_module, "batch_atomic_write", change_history)
    arguments = dict(context["revert"]["arguments"])
    arguments["why"] = "Undo with retained evidence."
    with pytest.raises(ValueError, match="PATH_GUARD"):
        commands.op_schema_memory(vault, **arguments)
    assert changed
    assert spec.overlay(vault).read_bytes() == before


@pytest.mark.parametrize("change", ["addition", "removal"])
def test_only_the_opted_in_history_census_binds_internal_files(
    vault: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    from exomem import compiled_history, memory_refs
    from exomem import vault as vault_module

    subject, _target, _ref = _edge_vault(vault, monkeypatch)
    parent_ref = memory_refs.ref_from_markdown((vault / subject).read_text())
    directory = compiled_history.history_dir(parent_ref)
    ordinary = vault_module.DirectoryCensusGuard.capture(vault, directory, max_entries=10)
    proof = vault_module.DirectoryCensusGuard.capture(
        vault, directory, max_entries=10, include_internal_state=True,
    )
    assert ordinary.entries == ()
    assert len(proof.entries) == 1
    if change == "addition":
        (vault / directory / "another.json").write_text("{}")
    else:
        next((vault / directory).glob("*.json")).unlink()
    ordinary.recheck(vault)
    with pytest.raises(vault_module.PathGuardError, match="PATH_GUARD_CHANGED"):
        proof.recheck(vault)


def test_a_public_same_path_replacement_does_not_disclose_the_old_private_subject(
    vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json
    import uuid

    from exomem import memory_refs
    from exomem.governance import egress, membership, policy
    from exomem.governance.principal import RequestPrincipal, request_scope

    _as(monkeypatch, "creator")
    path = _create_entity(vault, "Tidal Replacement")
    ref = _entry_ref(vault, "entity")
    original = memory_refs.ref_from_markdown((vault / path).read_text())
    governance = vault / "Knowledge Base" / "_Governance"
    (governance / "scopes").mkdir(parents=True)
    (governance / "rules").mkdir(parents=True)
    scope_id, rule_id = "01ARZ3NDEKTSV4RRFFQ69G5FAV", "01ARZ3NDEKTSV4RRFFQ69G5FB0"
    (governance / "scopes" / "confidential.yaml").write_text(
        f'governance_version: 1\nid: {scope_id}\nname: Confidential\npaths: ["Entities/Concepts/Tidal Replacement.md", "_trash/**"]\n'
    )
    (governance / "rules" / "external.yaml").write_text(
        f'governance_version: 1\nid: {rule_id}\nscope_ids: ["{scope_id}"]\n'
        "audience: external\nceiling: 0\n"
    )
    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()
    who = RequestPrincipal(audience_id="external", surface="mcp")
    with request_scope(who), pytest.raises(ValueError, match="REVIEW_ITEM_NOT_FOUND"):
        _context(vault, ref)
    # A filesystem replacement isolates identity admission from deletion tombstones.
    page = vault / path
    old_id = memory_refs.parse_memory_ref(original)
    page.write_text(page.read_text().replace(old_id, str(uuid.uuid4())))
    (governance / "scopes" / "confidential.yaml").write_text(
        f'governance_version: 1\nid: {scope_id}\nname: Confidential\npaths: ["_trash/**"]\n'
    )
    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()
    with request_scope(who):
        assert promotions.page_visible(vault, path), "the replacement itself is public"
        with pytest.raises(ValueError, match="REVIEW_ITEM_NOT_FOUND"):
            _context(vault, ref)
        assert ref not in json.dumps(promotions.served_rows(vault))
    assert original != memory_refs.ref_from_markdown((vault / path).read_text())


def test_a_creation_edge_keeps_its_canonical_binding_when_authored_with_an_alias(
    vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _as(monkeypatch, "creator")
    inspected = commands.op_schema_memory(vault, subject="relations", operation="inspect")
    commands.op_schema_memory(vault, subject="relations", operation="save", proposal={
        "upsert": {"vault.accounts_for": {
            "parent": "supports", "description": "Accounts for a synthetic ledger.",
            "aliases": ["accounts"],
        }},
    }, expected_hash=inspected["content_hash"], why="Register the relation and its authoring alias.")
    target = _create_entity(vault, "Tidal Ledger")
    subject = _create_entity(vault, "Harbour Reconciliation", relations=[{
        "kind": "accounts", "target": target,
    }])
    ref = _entry_ref(vault, "edge")
    context = _context(vault, ref)
    assert context["item"]["subjects"][0]["predicate"] == "vault.accounts_for"
    assert _revert(vault, ref, context["revert_fingerprint"])["outcome"] == "reverted"
    assert not promotions.edge_present(vault, subject, "vault.accounts_for", target)


@pytest.mark.parametrize("kind", ["edge", "registry"])
def test_current_absence_settles_a_notice_even_when_its_introduction_is_no_longer_retained(
    vault: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    from exomem import compiled_history, memory_refs, promotion_revert, registry_history
    from exomem.vocabulary import instances, registry_spec

    if kind == "edge":
        subject, target, ref = _edge_vault(vault, monkeypatch)
        promotion_revert.remove_relation(
            vault, subject=subject, predicate="supports", target=target, why="Remove the edge."
        )
        source_ref = memory_refs.ref_from_markdown((vault / subject).read_text())
        introduction = sorted((vault / compiled_history.history_dir(source_ref)).glob("*.json"))[0]
    else:
        from test_recent_promotions import _save_relation

        _save_relation(vault, "vault.accounts_for", parent="supports")
        ref = _entry_ref(vault, "registry")
        arguments = dict(_context(vault, ref)["revert"]["arguments"])
        arguments["why"] = "Remove the key."
        commands.op_schema_memory(vault, **arguments)
        spec = instances.select(vault, registry_spec("relations"))
        introduction = sorted(registry_history.history_dir(vault, spec.stem).glob("*.yaml"))[0]
    introduction.unlink()
    context = _context(vault, ref)
    assert context["item"]["subjects"] == []
    assert context["revert"] is None
    assert context.get("findings") is None, "proved current absence is not an unknown incarnation"
