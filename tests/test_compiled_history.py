"""Prior interpretations remain recoverable without entering ordinary recall."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_observe_memory import PAGE, _page_hash, _page_source, _write_page

from exomem import commands, compiled_history, semantic_index, semantic_writes, vault
from exomem.governance.principal import owner_principal, request_scope


@pytest.fixture
def owner():
    with request_scope(owner_principal(surface="mcp")):
        yield


def _correct(root: Path, value: str, reason: str) -> dict:
    current = semantic_index.current_parent_index_state(root, PAGE)
    unit = current.document.units[0]
    return commands.op_observe_memory(
        root,
        path=PAGE,
        operation="update",
        category="rule",
        content=value,
        kind="claim",
        unit_ref=unit.unit_ref,
        expected_fingerprint=unit.fingerprint,
        expected_hash=_page_hash(root / PAGE),
        reason=reason,
    )


def test_repeated_corrections_keep_prior_words_and_unit_identity(tmp_path: Path, owner) -> None:
    _write_page(
        tmp_path, source=_page_source("## Claim\n- category: rule\n- id: stable\n\nFirst wording.")
    )
    original = commands.op_read_memory(tmp_path, path=PAGE)
    unit = semantic_index.current_parent_index_state(tmp_path, PAGE).document.units[0]
    first = _correct(tmp_path, "Second wording.", "New source clarified the rule.")
    second = _correct(tmp_path, "Third wording.", "A later source narrowed its scope.")
    result = commands.op_read_memory(
        tmp_path, path=PAGE, unit_ref=unit.unit_ref, include_history=True
    )
    revisions = result["revision_history"]["revisions"]
    assert first["unit_ref"] == second["unit_ref"] == unit.unit_ref
    assert [r["prior"]["unit"]["content"] for r in revisions] == [
        "Second wording.",
        "First wording.",
    ]
    assert revisions[1]["prior"]["source"] == _page_source(
        "## Claim\n- category: rule\n- id: stable\n\nFirst wording."
    )
    assert revisions[1]["before_hash"] == original["content_hash"]
    assert revisions[0]["reason"] == "A later source narrowed its scope."
    assert result["unit"]["content"] == "Third wording."
    assert "history" not in result  # The page log keeps its existing page-only meaning.


def test_timestamp_only_write_and_replay_add_no_revision(tmp_path: Path, owner) -> None:
    source = _page_source("- [rule] First wording. ^stable")
    _write_page(tmp_path, source=source)
    prepared = semantic_writes.preflight_existing(
        tmp_path,
        path=PAGE,
        after_source=source.replace("2026-07-15", "2026-07-16"),
        operation="edit",
    )
    semantic_writes.commit_existing(tmp_path, preflight=prepared)
    assert compiled_history.read(tmp_path, path=PAGE)["returned"] == 0
    _correct(tmp_path, "Second wording.", "A correction.")
    assert compiled_history.read(tmp_path, path=PAGE)["returned"] == 1


def test_history_write_fails_atomically_with_primary(tmp_path: Path, owner, monkeypatch) -> None:
    _write_page(tmp_path, source=_page_source("- [rule] First wording. ^stable"))
    before = (tmp_path / PAGE).read_bytes()
    original = vault.batch_atomic_write

    def fail(writes, **kwargs):
        if any(
            str(write.path).endswith(".json") and "_Compiled" in str(write.path) for write in writes
        ):
            raise vault.PathGuardError("PATH_GUARD_STALE", "simulated concurrent correction")
        return original(writes, **kwargs)

    monkeypatch.setattr(vault, "batch_atomic_write", fail)
    with pytest.raises(ValueError, match="STALE_SEMANTIC_WRITE"):
        _correct(tmp_path, "Second wording.", "A correction.")
    assert (tmp_path / PAGE).read_bytes() == before
    assert compiled_history.read(tmp_path, path=PAGE)["returned"] == 0


def test_each_prior_snapshot_needs_admission_before_counts_or_reason(
    tmp_path: Path, owner, monkeypatch
) -> None:
    _write_page(tmp_path, source=_page_source("- [rule] First wording. ^stable"))
    _correct(tmp_path, "Second wording.", "A private rationale.")
    from exomem import registry_history

    original = registry_history._require_snapshot_admission

    def refuse_prior(root, path, raw):
        if b"First wording." in raw:
            raise registry_history.HistoryAdmissionRefused
        return original(root, path, raw)

    monkeypatch.setattr(registry_history, "_require_snapshot_admission", refuse_prior)
    hidden = compiled_history.read(tmp_path, path=PAGE)
    assert hidden["returned"] == hidden["retained"] == 0
    assert hidden["revisions"] == []
    assert hidden["truncated"] is False
    assert "private" not in json.dumps(hidden)


def test_exact_transition_replay_reuses_the_same_retained_revision(tmp_path: Path, owner) -> None:
    source = _page_source("- [rule] First wording. ^stable")
    _write_page(tmp_path, source=source)
    after = source.replace("First wording.", "Second wording.")
    prepared = semantic_writes.preflight_existing(
        tmp_path, path=PAGE, after_source=after, operation="edit"
    )
    semantic_writes.commit_existing(tmp_path, preflight=prepared, history_reason="A correction.")
    replay = semantic_writes.preflight_existing(
        tmp_path,
        path=PAGE,
        after_source=after,
        operation="edit",
        transition_token=prepared.transition_token,
    )
    result = semantic_writes.commit_existing(
        tmp_path, preflight=replay, history_reason="A correction."
    )
    assert result.mutated is False and result.lifecycle_state == "committed_replay"
    assert compiled_history.read(tmp_path, path=PAGE)["returned"] == 1


def test_revisions_do_not_rotate_at_registry_history_limit_and_paginate(
    tmp_path: Path, owner
) -> None:
    _write_page(tmp_path, source=_page_source("- [rule] Wording 0. ^stable"))
    for index in range(1, 23):
        _correct(tmp_path, f"Wording {index}.", "A source clarified this interpretation.")
    first = commands.op_read_memory(tmp_path, path=PAGE, include_history=True, history_limit=2)[
        "revision_history"
    ]
    assert first["retained"] == 22 and first["returned"] == 2 and first["truncated"]
    second = compiled_history.read(tmp_path, path=PAGE, limit=2, continuation=first["continuation"])
    assert "Wording 21." in first["revisions"][0]["prior"]["source"]
    assert "Wording 19." in second["revisions"][0]["prior"]["source"]
    retained = list((tmp_path / compiled_history.history_root()).rglob("*.json"))
    assert len(retained) == 22
    with pytest.raises(ValueError):
        commands.op_read_memory(tmp_path, path=retained[0].relative_to(tmp_path).as_posix())


def test_export_and_restore_keep_prior_compiled_interpretations(tmp_path: Path, owner) -> None:
    from dataclasses import replace

    from exomem import graph_sync
    from exomem import init as init_module
    from exomem import vault_portability as portability

    source = tmp_path / "source"
    init_module.init_vault(source)
    _write_page(source, source=_page_source("- [rule] First wording. ^stable"))
    state = semantic_index.current_parent_index_state(source, PAGE)
    unit = state.document.units[0]
    args = dict(
        path=PAGE,
        category="rule",
        kind="claim",
        content="Second wording.",
        unit_ref=unit.unit_ref,
        expected_fingerprint=unit.fingerprint,
        expected_hash=_page_hash(source / PAGE),
    )
    validated = commands.op_observe_memory(source, operation="validate", **args)
    semantic = validated["semantic"]
    commands.op_observe_memory(
        source,
        operation="update",
        **args,
        transition_token=semantic["transition_token"],
        relation_disposition="reviewed_none",
        relation_review_hash=semantic["relation_review_hash"],
        relation_review_reason="No supported relation in this isolated fixture.",
        reason="A corrected interpretation.",
    )
    before = compiled_history.read(source, path=PAGE)
    assert graph_sync.drain_active_rebuilds()
    context = portability.PortabilityContext(
        cell_id="source-cell",
        vault_id="logical-vault",
        operation_id="export-operation",
        created_at="2026-10-15T10:00:00+00:00",
        operator_authorized=True,
        lifecycle_state="quiesced",
        routing_stopped=True,
        active_mutations=0,
        background_writers_stopped=True,
        reads_allowed=True,
    )
    exported = portability.export_quiesced_vault(source, tmp_path / "artifacts", context=context)
    prepared = portability.prepare_restore(
        exported.archive_path,
        tmp_path / "staging",
        context=replace(
            context, lifecycle_state="restore-staging", operation_id="restore-operation"
        ),
    )
    restored = portability.publish_prepared_restore(prepared, tmp_path / "restored")
    assert restored.state == "published" and restored.lexical_ready
    assert compiled_history.read(tmp_path / "restored", path=PAGE) == before


def test_retained_source_bindings_need_current_authority_before_history_disclosure(
    tmp_path: Path, owner, monkeypatch
) -> None:
    from test_observe_memory import _origin_block

    from exomem import origin_bindings, provenance

    _write_page(tmp_path)
    clean = commands.op_observe_memory(
        tmp_path,
        path=PAGE,
        operation="validate",
        category="rule",
        content="The first rule.",
    )
    block = _origin_block(tmp_path, {"kind": "unit", "unit_ref": "#" + clean["unit"]["anchor"]})
    commands.op_observe_memory(
        tmp_path,
        path=PAGE,
        category="rule",
        content="The first rule.\n\n" + block,
    )
    _correct(tmp_path, "The corrected rule.", "The retained evidence clarified this rule.")
    visible = compiled_history.read(tmp_path, path=PAGE)
    assert visible["revisions"][0]["binding"]["inputs"]["original"]["reference"]

    def denied(*args, **kwargs):
        raise provenance.OriginError("ORIGIN_INPUT_UNAVAILABLE", "input is unavailable")

    monkeypatch.setattr(origin_bindings, "resolve_origin_input", denied)
    hidden = compiled_history.read(tmp_path, path=PAGE)
    monkeypatch.undo()
    (tmp_path / "Knowledge Base/Sources/observe-original.md").unlink()
    absent = compiled_history.read(tmp_path, path=PAGE)
    assert hidden == absent
    assert "Sources/observe-original" not in json.dumps(hidden)
    assert "retained evidence clarified" not in json.dumps(hidden)


def test_a_note_rename_preserves_unit_identity_and_retained_interpretations(
    tmp_path: Path, owner
) -> None:
    _write_page(tmp_path, source=_page_source("- [rule] First wording. ^stable"))
    changed = _correct(tmp_path, "Second wording.", "A corrected interpretation.")
    before = compiled_history.read(tmp_path, path=PAGE, unit_ref=changed["unit_ref"])
    moved = "Knowledge Base/Notes/Insights/renamed-rule.md"
    commands.op_manage_memory_file(
        tmp_path, operation="move", old_path=PAGE, new_path=moved, update_wikilinks=False
    )
    current = commands.op_read_memory(
        tmp_path, path=moved, unit_ref=changed["unit_ref"], include_history=True
    )
    assert current["status"] == "found" and current["unit"]["content"] == "Second wording."
    assert current["parent"]["path"] == moved
    assert current["revision_history"]["revisions"] == before["revisions"]
