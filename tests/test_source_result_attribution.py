"""Source retirement ownership survives capture result presentation."""

from __future__ import annotations

import json

import pytest

from exomem import source_result_attribution as attribution

SOURCE_A = "exomem://memory/11111111-1111-4111-8111-111111111111"
SOURCE_B = "exomem://memory/22222222-2222-4222-8222-222222222222"


def owned(result, source):
    return {item.path for item in attribution.recipe(result).contributions if source in item.source_refs}


def test_result_field_move_keeps_advisory_ownership_separate_from_primary() -> None:
    from exomem import add, duplicates, mutation_terminal, source_result_custody

    with attribution.scope():
        earlier = duplicates.Duplicate("source-a.md", SOURCE_A, duplicates.PagePayload(
            "a" * 64, "source", False, None, "https://example.invalid/article", SOURCE_A,
            {"title": "Earlier material", "captured": "2026-01-01"}, "Earlier material",
        ))
        advisory = add._same_url_advisory(duplicates.SameUrl((earlier,), 1))
        source = add.AddResult("source-b.md", SOURCE_B, [], same_url=advisory, capture_outcome="created").as_dict()
        result = {"source": source, "capture_outcome": "created", "duplicate_check_unavailable": True}
        attribution.carry_fields(source, result, {"same_url": "same_url"})
        result["same_url"] = source.pop("same_url")

        assert ("source", "path") in owned(result, SOURCE_B)
        assert ("same_url", "sources", 0) in owned(result, SOURCE_A)
        assert ("same_url", "count") in owned(result, SOURCE_A)
        assert not any(path[:1] == ("same_url",) for path in owned(result, SOURCE_B))
        assert attribution.recipe(result).unknown is False
        terminal = mutation_terminal.committed_terminal(result, request_id="capture", receipt_id="receipt", idempotency_key=None)
        compact = mutation_terminal.project_terminal(terminal)
        assert ("same_url", "sources", 0) in owned(compact, SOURCE_A)
        assert ("same_url", "count") in owned(compact, SOURCE_A)
        assert ("source", "path") in owned(compact, SOURCE_B)
        assert not any(path[:1] == ("same_url",) for path in owned(compact, SOURCE_B))
        assert ("capture_outcome",) not in owned(compact, SOURCE_B)
        assert ("duplicate_check_unavailable",) not in owned(compact, SOURCE_B)
        assert attribution.recipe(compact).unknown is False
        retired, _remaining = source_result_custody.redact(compact, attribution.recipe(compact), frozenset({SOURCE_A}))
        assert retired["source"]["path"] == "source-b.md"
        assert retired["capture_outcome"] == "created"
        assert retired["duplicate_check_unavailable"] is True
        assert retired["same_url"]["count"] is None
        assert retired["source_projection"]["state"] == "partial"
        assert SOURCE_A not in json.dumps(retired)
        retired_terminal, _remaining = source_result_custody.redact(terminal, attribution.recipe(terminal), frozenset({SOURCE_A}))
        assert mutation_terminal.project_terminal(retired_terminal)["source_projection"]["state"] == "partial"


@pytest.mark.parametrize(("count", "unknown"), [(5, False), (6, True)])
def test_same_url_total_records_uncertainty_when_typed_members_are_truncated(count, unknown) -> None:
    import uuid

    from exomem import add, duplicates, mutation_terminal

    shown = tuple(
        duplicates.Duplicate(
            f"source-{index}.md", ref := f"exomem://memory/{uuid.UUID(int=index + 1)}",
            duplicates.PagePayload(
                "a" * 64, "source", False, None, "https://example.invalid/article", ref,
                {"title": f"Earlier material {index}", "captured": "2026-01-01"}, "Earlier material",
            ),
        )
        for index in range(5)
    )
    found = duplicates.SameUrl(shown, count)
    refs = tuple(source.ref for source in shown)
    with attribution.scope():
        advisory = add._same_url_advisory(found)
        assert advisory["count"] == count
        assert len(advisory["sources"]) == 5
        assert attribution.recipe(advisory).sources == refs
        assert attribution.recipe(advisory).unknown is unknown
        source = add.AddResult("primary.md", SOURCE_B, [], same_url=advisory, capture_outcome="created").as_dict()
        result = {"source": source, "capture_outcome": "created"}
        attribution.carry_fields(source, result, {"same_url": "same_url"})
        result["same_url"] = source.pop("same_url")
        terminal = mutation_terminal.committed_terminal(result, request_id="capture", receipt_id="receipt", idempotency_key=None)
        compact = mutation_terminal.project_terminal(terminal)
        ownership = attribution.Recipe.model_validate_json(attribution.recipe(compact).model_dump_json())

        assert ownership.unknown is unknown
        assert set(ownership.sources) == {*refs, SOURCE_B}
        assert all(("same_url", "sources", index) in owned(compact, ref) for index, ref in enumerate(refs))
        assert any(item.path == ("same_url", "count") and item.source_refs == refs and item.action == "count"
                   for item in ownership.contributions)
    assert add._same_url_advisory(found) == advisory
    with attribution.scope():
        assert attribution.recipe({}).unknown is False
        decoded = json.loads(json.dumps(compact))
        attribution.restore(decoded, ownership)
        assert attribution.recipe(decoded) == ownership


def test_frame_does_not_leak_and_restore_needs_only_persisted_recipe() -> None:
    value = {"title": "Captured title", "outcome": "stored"}
    with attribution.scope():
        attribution.bind(value, (SOURCE_A,), paths=(("title",),))
        stored = attribution.recipe(value)
        assert attribution.Recipe.model_validate_json(stored.model_dump_json()) == stored
        with attribution.scope():
            assert attribution.recipe(value).sources == ()
        assert attribution.recipe(value) == stored
    assert attribution.recipe(value).sources == ()
    decoded = json.loads(json.dumps(value))
    with attribution.scope():
        assert attribution.restore(decoded, stored) is decoded
        assert owned(decoded, SOURCE_A) == {("title",)}


def test_shared_scalar_values_do_not_acquire_another_fields_ownership() -> None:
    with attribution.scope():
        source = {"title": "same", "state": "same"}
        attribution.bind(source, (SOURCE_A,), paths=(("title",),))
        copied = attribution.carry(source, dict(source))
        assert owned({"copy": copied, "control": "same"}, SOURCE_A) == {("copy", "title")}


def test_unusable_source_and_lost_selector_are_visible_without_blocking_capture() -> None:
    with attribution.scope():
        value = {"title": "Captured title"}
        assert attribution.bind(value, ("invalid",)) is value
        assert attribution.recipe(value).unknown is True
    with attribution.scope():
        value = {"title": "Captured title"}
        attribution.bind(value, (SOURCE_A,), paths=(("title",),))
        del value["title"]
        assert attribution.recipe(value).unknown is True


def test_recipe_bound_keeps_unknown_instead_of_an_oversized_owner_record() -> None:
    with attribution.scope():
        values = []
        for index in range(2000):
            row = {"title": str(index)}
            attribution.bind(row, (SOURCE_A,), paths=(("title",),))
            values.append(row)
        recipe = attribution.recipe(values)
        assert recipe.unknown is True
        assert len(recipe.model_dump_json().encode()) <= 64 * 1024
        assert "title" in recipe.model_dump_json()
        assert "Captured title" not in recipe.model_dump_json()


def test_recipe_rejects_noncanonical_reference_and_negative_ordinal() -> None:
    with pytest.raises(ValueError):
        attribution.Contribution(path=("files", -1), source_refs=(SOURCE_A,), action="omit")
    with pytest.raises(ValueError):
        attribution.Contribution(path=(), source_refs=(SOURCE_A.upper(),), action="omit")


def test_corrected_source_owns_proposal_title_excerpt_and_guidance(vault, source_schema, monkeypatch) -> None:
    from exomem import add, compile_proposal, corpus_aware
    from exomem import vault as vault_module

    original = add.add(vault, source_schema, source_type="session", title="Earlier transcript", content="Earlier text")
    corrected = add.add(
        vault, source_schema, source_type="session", title="Current transcript", content="Corrected original text",
        correction={"supersedes": original.path, "expected_hash": vault_module.content_hash((vault / original.path).read_text()), "reason": "Restore the original"},
    )
    monkeypatch.setattr(corpus_aware, "suggest_related", lambda *args, **kwargs: [])
    with attribution.scope():
        result = compile_proposal.propose_compilation(vault, sources=[original.path])

        assert result["suggested_title"] == "Current transcript"
        assert result["source_excerpts"][0]["excerpt"] == "Corrected original text"
        assert original.ref not in attribution.recipe(result).sources
        assert ("suggested_title",) in owned(result, corrected.ref)
        assert ("source_excerpts", 0) in owned(result, corrected.ref)
        assert ("semantic_compilation", "full_source_reads", 0) in owned(result, corrected.ref)
        assert attribution.recipe(result).unknown is False


def test_mixed_artifacts_keep_source_ordinals_and_partial_summary_through_compaction(vault, source_schema, tmp_path, monkeypatch) -> None:
    import hashlib

    from exomem import client_artifacts, commands, mutation_terminal, source_result_custody

    staged = []
    for ordinal, body in enumerate((b"first original", b"second original")):
        path = tmp_path / f"original-{ordinal}.bin"
        path.write_bytes(body)
        staged.append(client_artifacts.StagedArtifact(
            f"file-{ordinal}", path, len(body), hashlib.sha256(body).hexdigest(),
            "application/octet-stream", path.name,
        ))
    monkeypatch.setattr(client_artifacts, "stage_artifact", lambda *args, **kwargs: staged.pop(0))
    with attribution.scope():
        leaf = commands.op_capture_source(
            vault, source_schema, title="Original files", source_kind="session",
            files=[{"download_url": f"https://example.invalid/file-{ordinal}", "file_id": f"file-{ordinal}"} for ordinal in range(2)],
        )
        refs = [row["ref"] for row in leaf["files"]]
        terminal = mutation_terminal.committed_terminal(leaf, request_id="capture", receipt_id="receipt", idempotency_key=None)
        reply = mutation_terminal.project_terminal(terminal)
        projected = attribution.carry(reply, {
            **reply, "files": [{key: value for key, value in row.items() if key not in ("page", "ref")} for row in reply["files"]],
        })

        assert ("files", 0) in owned(projected, refs[0])
        assert ("files", 1) not in owned(projected, refs[0])
        assert ("files", 1) in owned(projected, refs[1])
        assert ("paths", 0) in owned(projected, refs[0])
        assert ("paths", 1) in owned(projected, refs[1])
        assert ("semantic_compilation", "full_source_reads", 0) in owned(projected, refs[0])
        assert ("semantic_compilation", "full_source_reads", 1) not in owned(projected, refs[0])
        assert ("semantic_compilation", "full_source_reads", 1) in owned(projected, refs[1])
        assert ("semantic_compilation", "preparation_route", "args", "sources", 0) in owned(projected, refs[0])
        assert ("semantic_compilation", "preparation_route", "args", "sources", 1) in owned(projected, refs[1])
        assert all(
            any(item.path == ("summary", "stored") and item.action == "count" and ref in item.source_refs
                for item in attribution.recipe(projected).contributions)
            for ref in refs
        )
        assert attribution.recipe(projected).unknown is False
        retired, _remaining = source_result_custody.redact(projected, attribution.recipe(projected), frozenset({refs[0]}))
        assert retired["files"][1]["stored_path"] == projected["files"][1]["stored_path"]
        assert retired["semantic_compilation"]["full_source_reads"][1] == projected["semantic_compilation"]["full_source_reads"][1]
        assert retired["summary"]["stored"] is None
        assert retired["source_projection"]["state"] == "partial"
        assert refs[0] not in json.dumps(retired)


def test_artifact_adoption_replay_keeps_ownership_when_public_row_has_no_ref(vault, source_schema, tmp_path, monkeypatch) -> None:
    import hashlib

    from exomem import client_artifacts, commands, source_result_custody

    body = b"original file bytes"

    def stage(*args, **kwargs):
        path = tmp_path / "download.bin"
        path.write_bytes(body)
        return client_artifacts.StagedArtifact(
            "file-original", path, len(body), hashlib.sha256(body).hexdigest(),
            "application/octet-stream", "original.bin",
        )

    monkeypatch.setattr(client_artifacts, "stage_artifact", stage)
    arguments = {
        "title": "Original handover", "source_kind": "session",
        "files": [{"file_id": "file-original", "download_url": "https://example.invalid/original"}],
        "adoption": {"key": "original-handover", "trigger": "requested", "selected_file_id": "file-original"},
    }
    with attribution.scope():
        first = commands.op_capture_source(vault, source_schema, **arguments)
        source_ref = first["files"][0]["ref"]
    with attribution.scope():
        replay = commands.op_capture_source(vault, source_schema, **arguments)

        assert replay["files"][0]["outcome"] == "replayed"
        assert "ref" not in replay["files"][0]
        assert ("files", 0) in owned(replay, source_ref)
        assert ("semantic_compilation", "full_source_reads", 0) in owned(replay, source_ref)
        assert attribution.recipe(replay).unknown is False
        retired, _remaining = source_result_custody.redact(replay, attribution.recipe(replay), frozenset({source_ref}))
        assert "original.bin" not in json.dumps(retired)
        assert retired["summary"]["replayed"] is None


def test_whole_source_retirement_marker_survives_replay_and_both_terminal_views() -> None:
    from exomem import add, mutation_terminal, source_result_custody

    with attribution.scope():
        source = add.AddResult("source-a.md", SOURCE_A, [], capture_outcome="created").as_dict()
        terminal = mutation_terminal.committed_terminal(
            {"source": source, "capture_outcome": "created"}, request_id="capture", receipt_id="receipt", idempotency_key=None,
        )
        retired, _remaining = source_result_custody.redact(terminal, attribution.recipe(terminal), frozenset({SOURCE_A}))
        replay = mutation_terminal.as_replay(retired)
        expected = {"state": "unavailable", "reason": "source_retired"}
        assert mutation_terminal.project_terminal(replay, "compact")["source_projection"] == expected
        assert mutation_terminal.project_terminal(replay, "full")["source_projection"] == expected


@pytest.mark.parametrize("marker", [
    {"state": "partial", "reason": "source_retired", "body": "untrusted content"},
    {"state": "partial", "reason": "another_reason"},
    {"state": "another_state", "reason": "source_retired"},
])
def test_terminal_does_not_forward_an_arbitrary_source_projection_field(marker) -> None:
    from exomem import mutation_terminal

    terminal = mutation_terminal.committed_terminal({}, request_id="capture", receipt_id="receipt", idempotency_key=None)
    terminal["source_projection"] = marker
    assert "source_projection" not in mutation_terminal.project_terminal(terminal)
