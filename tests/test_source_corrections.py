"""A correction changes current custody without changing historical originals."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import replace

import pytest

from exomem import (
    add,
    artifact_recovery,
    compile_proposal,
    corpus_aware,
    memory_refs,
    reserved_paths,
    source_closure,
    source_corrections,
    vault_portability,
)
from exomem import vault as vault_module
from exomem.governance.principal import owner_principal, request_scope


def test_correction_preserves_history_and_selects_current_consumers(vault, source_schema, monkeypatch):
    old = add.add(vault, source_schema, source_type="session", title="First transcript", content="A timestamp is 09:01.")
    historical = (vault / old.path).read_bytes()
    corrected = add.add(
        vault, source_schema, source_type="session", title="Correct transcript", content="The timestamp is 09:02.",
        correction={"supersedes": old.path, "expected_hash": vault_module.content_hash(historical.decode()), "reason": "Original timestamp restored"},
    )
    assert (vault / old.path).read_bytes() == historical
    metadata, _, _ = vault_module.parse_frontmatter((vault / corrected.path).read_text())
    assert metadata["supersedes"] == old.ref
    current = source_corrections.resolve_current(vault, old.path)
    assert current.selected_path == corrected.path
    assert current.selected_ref == corrected.ref
    assert "09:02" in current.source
    assert current.as_dict()["requested_ref"] == old.ref
    receipt = source_corrections.receipt_root(vault) / f"{memory_refs.parse_memory_ref(old.ref)}.json"
    entry = json.loads(receipt.read_text())
    assert entry["new_ref"] == corrected.ref
    assert reserved_paths.classify_logical(receipt.relative_to(vault).as_posix()).descriptor_id == "source-corrections"
    assert vault_portability.classify_artifact(receipt.relative_to(vault).as_posix()).artifact_class == vault_portability.ArtifactClass.CANONICAL

    compiled = f'---\ntype: insight\nsources: ["{old.ref}"]\n---\n\n# A conclusion\n'
    closure = source_closure.prepare_source_closure(vault, compiled, destination="Knowledge Base/Notes/Insights/conclusion.md")
    assert closure.inspection.resolved_paths == (corrected.path,)
    assert {write.path for write in closure.backref_writes} == {vault / corrected.path}
    assert receipt.relative_to(vault).as_posix() in {guard.target for guard in closure.required_guards}
    monkeypatch.setattr(corpus_aware, "suggest_related", lambda *args, **kwargs: [])
    proposal = compile_proposal.propose_compilation(vault, sources=[old.path.removesuffix(".md")])
    assert proposal["suggested_sources"] == [corrected.path.removesuffix(".md")]
    assert proposal["suggested_title"] == "Correct transcript"
    assert "09:02" in proposal["source_excerpts"][0]["excerpt"]
    assert "09:01" not in proposal["source_excerpts"][0]["excerpt"]

    # Extraction and back-reference workers can still update supported historical metadata.
    historical_text, historical_guard = vault_module.read_guarded_text(vault, vault / old.path)
    updated = vault_module.append_frontmatter_list_item(historical_text, "ingested_into", "[[Knowledge Base/Notes/Insights/prior-note]]")
    vault_module.batch_atomic_write([vault_module.PlannedWrite(vault / old.path, updated, guard=historical_guard)], vault_root=vault)
    assert source_corrections.resolve_current(vault, old.path).selected_ref == corrected.ref


def test_custody_page_follows_correction_but_artifact_path_stays_exact(vault, source_schema, tmp_path):
    historical_bytes = b"---\ninvalid: [\n---\nhistorical transcript\r\n"
    current_bytes = b"---\ninvalid: [\n---\ncorrect transcript\r\n"
    staged = tmp_path / "transcript.md"
    staged.write_bytes(historical_bytes)
    old = add.add(vault, source_schema, source_type="session", title="Historical recording", content="", artifact=add.SourceArtifact(staged, "transcript.md"))
    old_page = (vault / old.path).read_bytes()
    staged.write_bytes(current_bytes)
    current = add.add(
        vault, source_schema, source_type="session", title="Current recording", content="", artifact=add.SourceArtifact(staged, "transcript.md"),
        correction={"supersedes": old.path, "expected_hash": vault_module.content_hash(old_page.decode()), "reason": "Restore exact original"},
    )
    who = owner_principal(surface="mcp")
    with request_scope(who):
        with artifact_recovery.stage(vault, old.path, who) as (copied, descriptor):
            assert copied.private_path.read_bytes() == current_bytes
            assert descriptor["path"] == current.artifact_path
        with artifact_recovery.stage(vault, old.artifact_path, who) as (copied, descriptor):
            assert copied.private_path.read_bytes() == historical_bytes
            assert descriptor["path"] == old.artifact_path
    assert (vault / old.path).read_bytes() == old_page
    assert (vault / old.artifact_path).read_bytes() == historical_bytes

    context = vault_portability.PortabilityContext(
        cell_id="cell-test", vault_id="vault-test", operation_id="correction-export",
        created_at="2026-10-26T12:00:00+00:00", operator_authorized=True,
        lifecycle_state="quiesced", routing_stopped=True, active_mutations=0,
        background_writers_stopped=True, reads_allowed=True,
    )
    exported = vault_portability.export_quiesced_vault(vault, tmp_path / "exports", context=context)
    restored = tmp_path / "restored"
    vault_portability.prepare_restore(exported.archive_path, restored, context=replace(context, lifecycle_state="restore-staging"))
    assert source_corrections.resolve_current(restored, old.path).selected_ref == current.ref
    assert (restored / old.path).read_bytes() == old_page
    assert (restored / old.artifact_path).read_bytes() == historical_bytes
    assert (restored / current.artifact_path).read_bytes() == current_bytes


def test_stale_hash_and_parallel_corrections_leave_no_orphan(vault, source_schema):
    old = add.add(vault, source_schema, source_type="session", title="Original", content="Original body")
    before = {path for path in (vault / "Knowledge Base/Sources").rglob("*.md")}
    with pytest.raises(ValueError, match="SOURCE_CORRECTION_STALE"):
        add.add(vault, source_schema, source_type="session", title="Stale replacement", content="Correct body", correction={"supersedes": old.path, "expected_hash": "0" * 64, "reason": "Restore original"})
    assert {path for path in (vault / "Knowledge Base/Sources").rglob("*.md")} == before
    correction = {"supersedes": old.path, "expected_hash": vault_module.content_hash((vault / old.path).read_text()), "reason": "Restore original"}

    def capture(title):
        try:
            return add.add(vault, source_schema, source_type="session", title=title, content="Correct body", correction=correction)
        except (ValueError, vault_module.CreateOnlyConflict):
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [pool.submit(copy_context().run, capture, title) for title in ("Correction one", "Correction two")]
        results = [call.result() for call in calls]
    assert sum(result is not None for result in results) == 1
    assert len({path for path in (vault / "Knowledge Base/Sources").rglob("*.md")} - before) == 1
    assert len(list(source_corrections.receipt_root(vault).glob("*.json"))) == 1


@pytest.mark.parametrize("failure", ["withheld", "missing", "malformed", "cycle"])
def test_invalid_or_withheld_successor_is_content_free(vault, source_schema, failure):
    old = add.add(vault, source_schema, source_type="session", title="Public original", content="Historical body")
    new = add.add(vault, source_schema, source_type="session", title="Sensitive replacement", content="Sensitive current body", correction={"supersedes": old.path, "expected_hash": vault_module.content_hash((vault / old.path).read_text()), "reason": "Sensitive correction reason"})
    receipt = source_corrections.receipt_root(vault) / f"{memory_refs.parse_memory_ref(old.ref)}.json"
    if failure == "missing":
        (vault / new.path).unlink()
    elif failure == "malformed":
        receipt.write_text("{invalid json")
    elif failure == "cycle":
        entry = json.loads(receipt.read_text())
        entry.update(new_ref=old.ref, new_path=old.path)
        receipt.write_text(json.dumps(entry))

    def allowed(path):
        return failure != "withheld" or path != new.path

    with pytest.raises(source_corrections.SourceCorrectionUnavailable) as caught:
        source_corrections.resolve_current(vault, old.path, authorize_path=allowed)
    assert str(caught.value) == "SOURCE_UNAVAILABLE: source is unavailable"
    compiled = f'---\ntype: insight\nsources: ["{old.path}"]\n---\n# Conclusion\n'
    closure = source_closure.inspect_source_closure(vault, compiled, authorize_path=allowed)
    assert not closure.closed
    assert closure.resolved == ()


def test_legacy_identity_requires_explicit_repair(vault, source_schema):
    path = vault / "Knowledge Base/Sources/Sessions/legacy.md"
    path.write_text("---\ntype: source\n---\n# Historical original\n")
    with pytest.raises(ValueError, match="SOURCE_CORRECTION_ID_REQUIRED"):
        add.add(vault, source_schema, source_type="session", title="Correction", content="Correct body", correction={"supersedes": path.relative_to(vault).as_posix(), "expected_hash": vault_module.content_hash(path.read_text()), "reason": "Restore original"})
    assert "exomem_id" not in path.read_text()
