"""A correction changes current custody without changing historical originals."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import replace

import pytest
import yaml

from exomem import (
    add,
    artifact_recovery,
    client_artifacts,
    compile_proposal,
    corpus_aware,
    memory_refs,
    preserve,
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


def test_legacy_markdown_source_receipt_replays_and_original_cannot_claim_identity(vault, source_schema, tmp_path, monkeypatch):
    original = b"---\ntype: source\nsource_identity: malformed-untrusted-value\n---\nOriginal Markdown\n"

    def stage(file, _budget, **kwargs):
        staged = tmp_path / "original.md"
        staged.write_bytes(original)
        digest, size = add.artifact_identity(staged)
        return client_artifacts.StagedArtifact(
            file_id=file["file_id"], path=staged, size=size, sha256=digest,
            content_type="text/markdown", filename="original.md",
        )

    monkeypatch.setattr(client_artifacts, "stage_artifact", stage)
    inputs = {
        "source_schema": source_schema, "title": "Legacy original", "source_type": "session",
        "files": [{"file_id": "original", "file_name": "original.md", "mime_type": "text/markdown", "download_url": "https://files.example/original"}],
        "adoption": {"key": "legacy-original", "trigger": "selected", "selected_file_id": "original"},
    }
    captured = client_artifacts.capture_source_artifacts(vault, **inputs)
    receipt = captured["files"][0]["adoption"]
    artifact = vault / receipt["stored_path"]
    page = vault / receipt["page_path"]
    artifact, legacy = _legacy_markdown_pair(vault, artifact, page)

    replay = client_artifacts.capture_source_artifacts(vault, **inputs)
    assert replay["files"][0]["outcome"] == "replayed"
    assert replay["files"][0]["adoption"]["page_path"] == legacy.relative_to(vault).as_posix()
    who = owner_principal(surface="mcp")
    with request_scope(who):
        with artifact_recovery.stage(vault, artifact.relative_to(vault).as_posix(), who) as (copied, _):
            assert copied.private_path.read_bytes() == original
        captured_identity = add.add(
            vault, source_schema, title="Independent capture", source_type="session", content="Trusted authored text",
            source_identity={"system": "archive", "record_id": "independent", "revision": "one"},
        )
    assert captured_identity.capture_outcome == "created"


def _legacy_original(vault, source_schema, tmp_path, original, *, title="Legacy original"):
    staged = tmp_path / "legacy.md"
    staged.write_bytes(original)
    result = add.add(vault, source_schema, content="", title=title, source_type="session", artifact=add.SourceArtifact(staged, "legacy.md"))
    artifact = vault / result.artifact_path
    page = vault / result.path
    legacy_artifact, legacy_page = _legacy_markdown_pair(vault, artifact, page)
    return replace(result, path=legacy_page.relative_to(vault).as_posix(), artifact_path=legacy_artifact.relative_to(vault).as_posix())


def _legacy_markdown_pair(vault, artifact, page):
    legacy_artifact = artifact.with_suffix("") if artifact.name.endswith(".md.bin") else artifact
    legacy_page = preserve._sidecar_path(legacy_artifact, protected=True)
    metadata, body, _ = vault_module.parse_frontmatter(page.read_text())
    metadata["evidence_file"] = legacy_artifact.relative_to(vault).as_posix()
    if "artifact_adoption" in metadata:
        metadata["artifact_adoption"]["stored_path"] = legacy_artifact.relative_to(vault).as_posix()
        metadata["artifact_adoption"]["page_path"] = legacy_page.relative_to(vault).as_posix()
    if artifact != legacy_artifact:
        artifact.rename(legacy_artifact)
    if page != legacy_page:
        page.rename(legacy_page)
    legacy_page.write_text("---\n" + yaml.safe_dump(metadata, sort_keys=False) + "---\n" + body)
    return legacy_artifact, legacy_page


def test_legacy_original_uuid_cannot_redirect_canonical_source(vault, source_schema, tmp_path):
    victim = add.add(vault, source_schema, title="Trusted original", content="Trusted content", source_type="session")
    original = f"---\ntype: source\nexomem_id: {memory_refs.parse_memory_ref(victim.ref)}\n---\nUploaded bytes\n".encode()
    captured = _legacy_original(vault, source_schema, tmp_path, original)
    before = {path: path.read_bytes() for path in vault.rglob("*.md")}
    with pytest.raises(source_corrections.SourceCorrectionUnavailable):
        add.add(vault, source_schema, title="Attempted redirect", content="Other text", source_type="session", correction={"supersedes": captured.artifact_path, "expected_hash": hashlib.sha256(original).hexdigest(), "reason": "Correct the upload"})
    assert {path: path.read_bytes() for path in vault.rglob("*.md")} == before
    assert source_corrections.resolve_current(vault, victim.path).selected_ref == victim.ref
    with pytest.raises(source_corrections.SourceCorrectionUnavailable):
        source_corrections.resolve_current(vault, captured.artifact_path)
    corrected = add.add(vault, source_schema, title="Correct wrapper", content="Correct original", source_type="session", correction={"supersedes": captured.path, "expected_hash": vault_module.content_hash((vault / captured.path).read_text()), "reason": "Correct the captured Source"})
    assert source_corrections.resolve_current(vault, captured.path).selected_ref == corrected.ref


@pytest.mark.parametrize("deny_occupant", [False, True])
def test_renamed_successor_uses_identity_when_hint_is_reused(vault, source_schema, deny_occupant):
    old = add.add(vault, source_schema, title="Earlier original", content="Earlier content", source_type="session")
    current = add.add(vault, source_schema, title="Current original", content="Current content", source_type="session", correction={"supersedes": old.path, "expected_hash": vault_module.content_hash((vault / old.path).read_text()), "reason": "Restore original"})
    renamed = (vault / current.path).with_name("renamed-current.md")
    (vault / current.path).rename(renamed)
    unrelated = add.add(vault, source_schema, title="Current original", content="Unrelated content", source_type="session")
    assert unrelated.path == current.path

    def authorize(path):
        return not deny_occupant or path != unrelated.path

    resolved = source_corrections.resolve_current(vault, old.path, authorize_path=authorize)
    assert resolved.selected_path == renamed.relative_to(vault).as_posix()
    assert resolved.selected_ref == current.ref
    assert "Current content" in resolved.source
    assert resolved.guard.target == renamed.relative_to(vault).as_posix()


def test_duplicate_canonical_uuid_cannot_publish_a_correction(vault, source_schema):
    old = add.add(vault, source_schema, title="Canonical original", content="Original text", source_type="session")
    source = (vault / old.path).read_text()
    duplicate = (vault / old.path).with_name("duplicate-original.md")
    duplicate.write_text(source)
    before = {path: path.read_bytes() for path in vault.rglob("*.md")}
    with pytest.raises(source_corrections.SourceCorrectionUnavailable):
        add.add(vault, source_schema, title="Correction", content="Correct text", source_type="session", correction={"supersedes": old.path, "expected_hash": vault_module.content_hash(source), "reason": "Restore original"})
    assert {path: path.read_bytes() for path in vault.rglob("*.md")} == before


def test_legacy_custody_skips_an_unrelated_occupied_companion_name(vault, source_schema, tmp_path):
    original = b"# Legacy original\nExact historical bytes.\n"
    captured = _legacy_original(vault, source_schema, tmp_path, original)
    unrelated = add.add(vault, source_schema, title="Legacy original notes", content="Unrelated page", source_type="session")
    assert vault / unrelated.path == preserve._sidecar_path(vault / captured.artifact_path, protected=False)
    who = owner_principal(surface="mcp")
    with request_scope(who):
        with artifact_recovery.stage(vault, captured.artifact_path, who) as (copied, _):
            assert copied.private_path.read_bytes() == original


def test_utf16_markdown_capture_keeps_bytes_outside_page_indexes(vault, source_schema, tmp_path):
    from exomem import epistemic_graph

    original = "---\ntype: source\n---\n# Original Ω\r\n".encode("utf-16")
    staged = tmp_path / "original.md"
    staged.write_bytes(original)
    captured = add.add(vault, source_schema, title="UTF16 document", content="", source_type="session", artifact=add.SourceArtifact(staged, "original.md", "text/markdown; charset=utf-16"), expected_sha256=hashlib.sha256(original).hexdigest())
    assert captured.artifact_path.endswith(".md.bin")
    assert captured.path == captured.artifact_path + ".md"
    assert (vault / captured.artifact_path).read_bytes() == original
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    who = owner_principal(surface="mcp")
    with request_scope(who):
        with artifact_recovery.stage(vault, captured.path, who) as (copied, descriptor):
            assert copied.private_path.read_bytes() == original
            assert descriptor["file_name"] == "original.md"
            assert descriptor["mime_type"] == "text/markdown; charset=utf-16"


@pytest.mark.parametrize("content_type", ["text/plain\r\nX-Injected: 1", "x" * 256])
def test_original_content_type_keeps_recovery_header_bounds(vault, source_schema, tmp_path, content_type):
    staged = tmp_path / "original.md"
    staged.write_bytes(b"# Original\n")
    captured = add.add(vault, source_schema, title="Original headers", content="", source_type="session", artifact=add.SourceArtifact(staged, "original.md", content_type))
    who = owner_principal(surface="mcp")
    with request_scope(who), pytest.raises(artifact_recovery.RecoveryUnavailable):
        with artifact_recovery.stage(vault, captured.path, who):
            pytest.fail("invalid MIME reached original delivery")
