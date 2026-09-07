"""Project grants need existing membership that survives the whole write."""

import sqlite3

import pytest

from exomem import vocabulary_gate_evidence as evidence
from exomem.vocabulary_effects import CanonicalWriteImage


@pytest.mark.parametrize(("before_project", "after_project", "expected"), [
    ("alpha", "alpha", 1), ("alpha", "beta", 0), ("beta", "alpha", 0),
])
def test_project_edge_membership_is_proven_before_and_after(
    tmp_path, monkeypatch, before_project, after_project, expected,
):
    source = "Knowledge Base/Notes/source.md"
    target = "Knowledge Base/Notes/target.md"
    source_id = "11111111-1111-4111-8111-111111111111"
    target_id = "22222222-2222-4222-8222-222222222222"

    def body(identifier, project, text):
        return f"---\ntype: insight\nstatus: active\nexomem_id: {identifier}\nprojects: [{project}]\n---\n{text}\n"

    before = body(source_id, before_project, "Source.")
    after = body(source_id, after_project, "Source links [[Notes/target]].")
    for path, content in (
        (source, before), (target, body(target_id, "alpha", "Target.")),
        ("Knowledge Base/_Schema/project-keys.yaml", "projects:\n  alpha: Alpha\n  beta: Beta\n"),
    ):
        destination = tmp_path / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content)

    refs = sqlite3.connect(":memory:")
    refs.execute("CREATE TABLE identities (exomem_id TEXT, path TEXT, status TEXT)")
    refs.executemany("INSERT INTO identities VALUES (?, ?, 'valid')", [(source_id, source), (target_id, target)])
    refs.commit()
    monkeypatch.setattr(evidence.freshness, "live_recall_checkpoint", lambda *args: "current")
    monkeypatch.setattr(evidence.epistemic_graph.EpistemicGraphIndex, "_open_read_snapshot", lambda self: sqlite3.connect(":memory:"))
    monkeypatch.setattr(evidence.memory_refs.ReferenceIndex, "_current_readonly_connection", lambda self: refs)
    monkeypatch.setattr(evidence.memory_refs, "_pending_reference_projection", lambda root: None)
    resolver = evidence.vault.WikilinkResolver.from_entries(tmp_path, [(source, "Source"), (target, "Target")])
    monkeypatch.setattr(evidence.find, "recall_resolver_snapshot_at_checkpoint", lambda *args: resolver)
    result = evidence.classify(tmp_path, (CanonicalWriteImage(source, before.encode(), after.encode()),))
    try:
        assert result.classification.state == "reviewed"
        assert len(result.scope_proofs) == expected
    finally:
        result.close()
