"""Prior interpretations remain recoverable without entering ordinary recall."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_connector_boundary import PUBLIC
from test_connector_boundary import configured_boundary as configured_boundary
from test_latency_gate import _seed_freshness_live
from test_observe_memory import (
    PAGE,
    UNTYPED_PAGE,
    _page_hash,
    _page_source,
    _untyped_page_source,
    _write_page,
)

from exomem import (
    append_to_file,
    commands,
    compiled_history,
    readiness,
    registry_history,
    semantic_index,
    semantic_language_registry,
    semantic_writes,
    vault,
    vocabulary_auxiliaries,
    working_set_runtime,
)
from exomem.governance.principal import library_scope, owner_principal, request_scope


@pytest.fixture
def owner():
    with request_scope(owner_principal(surface="mcp")):
        yield


def _correct(root: Path, value: str, reason: str, *, index: int = 0) -> dict:
    current = semantic_index.current_parent_index_state(root, PAGE)
    unit = current.document.units[index]
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


def test_repeated_corrections_keep_prior_words_and_identity_after_a_category_alias(
    tmp_path: Path, owner
) -> None:
    _write_page(
        tmp_path, source=_page_source("## Claim\n- category: rule\n- id: stable\n\nFirst wording.")
    )
    original = commands.op_read_memory(tmp_path, path=PAGE)
    unit = semantic_index.current_parent_index_state(tmp_path, PAGE).document.units[0]
    first = _correct(tmp_path, "Second wording.", "New source clarified the rule.")
    after_first = (tmp_path / PAGE).read_text(encoding="utf-8")
    second = _correct(tmp_path, "Third wording.", "A later source narrowed its scope.")
    with library_scope():
        categories = commands.op_schema_memory(tmp_path, subject="categories", operation="inspect")
        commands.op_schema_memory(
            tmp_path,
            subject="categories",
            operation="save",
            proposal={"upsert": {"operating_rule": {
                "description": "An operating constraint.", "aliases": ["rule"],
            }}},
            expected_hash=categories["content_hash"],
            why="Register the existing category as an alias.",
        )
    result = commands.op_read_memory(
        tmp_path, path=PAGE, unit_ref=unit.unit_ref, include_history=True
    )
    revisions = result["revision_history"]["revisions"]
    assert first["unit_ref"] == second["unit_ref"] == unit.unit_ref
    assert [r["prior"]["unit"]["content"] for r in revisions] == [
        "Second wording.",
        "First wording.",
    ]
    assert [r["prior"]["unit"]["category"] for r in revisions] == [
        "operating_rule", "operating_rule",
    ]
    assert revisions[0]["prior"]["source"] == after_first
    assert revisions[1]["prior"]["source"] == _page_source(
        "## Claim\n- category: rule\n- id: stable\n\nFirst wording."
    )
    assert revisions[1]["before_hash"] == original["content_hash"]
    assert [r["reason"] for r in revisions] == [
        "A later source narrowed its scope.", "New source clarified the rule.",
    ]
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


def test_unusable_history_matches_absence_without_claiming_retention(tmp_path: Path, owner) -> None:
    _write_page(tmp_path, source=_page_source("- [rule] First wording. ^stable"))
    absent = commands.op_read_memory(tmp_path, path=PAGE, include_history=True)["revision_history"]
    _correct(tmp_path, "Second wording.", "A correction.")
    entry, = (tmp_path / compiled_history.history_root()).rglob("*.json")
    damaged = json.loads(entry.read_text(encoding="utf-8"))
    damaged["prior"]["source"] += "Unverified bytes."
    entry.write_text(json.dumps(damaged), encoding="utf-8")

    result = commands.op_read_memory(tmp_path, path=PAGE, include_history=True)

    assert result["revision_history"]["status"] == "unavailable"
    assert result["revision_history"]["coverage"] == "unverified"
    assert result["revision_history"] == absent
    assert "Second wording." in result["body"]


def test_a_revision_names_the_exomem_release_that_wrote_it(tmp_path: Path, owner) -> None:
    # A reader tracing a bad interpretation needs the writer release, which the
    # entry did not record before.
    import exomem

    _write_page(tmp_path, source=_page_source("- [rule] First wording. ^stable"))
    _correct(tmp_path, "Second wording.", "The first correction.")

    history = commands.op_read_memory(tmp_path, path=PAGE, include_history=True)["revision_history"]

    revision, = history["revisions"]
    assert "First wording." in revision["prior"]["source"]
    assert revision["exomem_version"] == exomem.__version__


def test_admitted_history_does_not_claim_complete_coverage(tmp_path: Path, owner) -> None:
    _write_page(tmp_path, source=_page_source("- [rule] First wording. ^stable"))
    _correct(tmp_path, "Second wording.", "The first correction.")
    _correct(tmp_path, "Third wording.", "The second correction.")
    for path in (tmp_path / compiled_history.history_root()).rglob("*.json"):
        entry = json.loads(path.read_text(encoding="utf-8"))
        if "First wording." in entry["prior"]["source"]:
            entry["prior"]["source"] += "Unverified bytes."
            path.write_text(json.dumps(entry), encoding="utf-8")

    history = commands.op_read_memory(tmp_path, path=PAGE, include_history=True)["revision_history"]

    assert history["status"] == "retained"
    assert history["coverage"] == "unverified"
    assert history["returned"] == 1
    revision, = history["revisions"]
    assert "Second wording." in revision["prior"]["source"]
    assert "Unverified bytes." not in json.dumps(history)


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

    def refuse_prior(root, path, raw, **kwargs):
        if b"First wording." in raw:
            raise registry_history.HistoryAdmissionRefused
        return original(root, path, raw, **kwargs)

    monkeypatch.setattr(registry_history, "_require_snapshot_admission", refuse_prior)
    hidden = compiled_history.read(tmp_path, path=PAGE)
    assert hidden["returned"] == 0
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


def test_successive_appends_to_a_crlf_page_chain_their_history(tmp_path: Path, owner) -> None:
    page = _write_page(
        tmp_path, source=_page_source("- [rule] First wording. ^stable"), newline="\r\n"
    )
    for appendix in ("First appendix.", "Second appendix."):
        append_to_file.append_to_file(tmp_path, path=PAGE, content=f"\n{appendix}\n")
    newer, older = compiled_history.read(tmp_path, path=PAGE)["revisions"]
    assert newer["before_hash"] == older["after_hash"]
    assert newer["after_hash"] == _page_hash(page)


def test_revisions_do_not_rotate_at_registry_history_limit_and_paginate(
    tmp_path: Path, owner
) -> None:
    _write_page(tmp_path, source=_page_source("- [rule] Wording 0. ^stable"))
    for index in range(1, 23):
        _correct(tmp_path, f"Wording {index}.", "A source clarified this interpretation.")
    first = commands.op_read_memory(tmp_path, path=PAGE, include_history=True, history_limit=2)[
        "revision_history"
    ]
    assert first["returned"] == 2 and first["truncated"] and first["status"] == "retained"
    second = compiled_history.read(tmp_path, path=PAGE, limit=2, continuation=first["continuation"])
    assert "Wording 21." in first["revisions"][0]["prior"]["source"]
    assert "Wording 19." in second["revisions"][0]["prior"]["source"]
    walked = _walk_history(tmp_path, limit=5)
    assert len(walked) == 22
    assert all(f"Wording {21 - i}." in r["prior"]["source"] for i, r in enumerate(walked))
    retained = list((tmp_path / compiled_history.history_root()).rglob("*.json"))
    assert len(retained) == 22
    with pytest.raises(ValueError):
        commands.op_read_memory(tmp_path, path=retained[0].relative_to(tmp_path).as_posix())


def _walk_history(root: Path, *, limit: int) -> list[dict]:
    seen, continuation = [], None
    while True:
        page = compiled_history.read(root, path=PAGE, limit=limit, continuation=continuation)
        seen += page["revisions"]
        if not page["truncated"]:
            return seen
        continuation = page["continuation"]


def _entry_files_oldest_first(root: Path) -> list[Path]:
    return sorted((root / compiled_history.history_root()).rglob("*.json"), key=lambda p: p.name)


def test_a_history_page_does_not_depend_on_entries_beyond_its_lookahead(
    tmp_path: Path, owner, monkeypatch
) -> None:
    """A deep history must page without opening old entries: a damaged ancient snapshot
    must not change the newest page, or make every read pay for all retained depth."""
    _write_page(tmp_path, source=_page_source("- [rule] Wording 0. ^stable"))
    for index in range(1, 6):
        _correct(tmp_path, f"Wording {index}.", "A source clarified this interpretation.")
    intact = compiled_history.read(tmp_path, path=PAGE, limit=1)
    for path in _entry_files_oldest_first(tmp_path)[:3]:
        path.write_text("not json", encoding="utf-8")
    opened = []
    read_kept = registry_history.read_kept

    def counting(root, directory, name, **kwargs):
        opened.append(name)
        return read_kept(root, directory, name, **kwargs)

    monkeypatch.setattr(registry_history, "read_kept", counting)
    assert compiled_history.read(tmp_path, path=PAGE, limit=1) == intact
    assert intact["returned"] == 1 and intact["truncated"] and intact["status"] == "retained"
    assert len(opened) == 2  # The page and one further admitted entry; nothing older.


def test_pagination_skips_unavailable_entries_without_revealing_them(
    tmp_path: Path, owner
) -> None:
    _write_page(tmp_path, source=_page_source("- [rule] Wording 0. ^stable"))
    for index in range(1, 10):
        _correct(tmp_path, f"Wording {index}.", "A source clarified this interpretation.")
    files = _entry_files_oldest_first(tmp_path)  # Wording 0..8 snapshots, oldest first.
    for position in (1, 5, 6):
        files[position].write_text("not json", encoding="utf-8")
    walked = _walk_history(tmp_path, limit=2)
    versions = [r["version"] for r in walked]
    assert len(walked) == 6 and versions == sorted(versions, reverse=True)
    assert len(set(versions)) == 6
    assert not {files[p].stem for p in (1, 5, 6)} & set(versions)
    first = compiled_history.read(tmp_path, path=PAGE, limit=2)
    assert first["continuation"] == first["revisions"][-1]["version"]


def _unit_pages(root: Path, unit_ref: str, *, limit: int) -> list[dict]:
    pages, continuation = [], None
    while True:
        page = compiled_history.read(
            root, path=PAGE, unit_ref=unit_ref, limit=limit, continuation=continuation
        )
        pages.append(page)
        if not page["truncated"]:
            return pages
        continuation = page["continuation"]


def test_a_sparse_unit_read_is_capped_and_pages_on_to_its_old_revision(
    tmp_path: Path, owner
) -> None:
    """A unit corrected once, long ago, among many corrections of another unit: one read
    must not open every retained entry to find it. It stops at a page-derived cap and
    reports `incomplete`, not `unavailable`; continuations reach the old revision exactly
    once without ever resting on an entry this reader cannot see."""
    _write_page(
        tmp_path,
        source=_page_source("- [rule] Never 0. ^never\n- [rule] Busy 0. ^busy\n- [rule] Rare 0. ^rare"),
    )
    units = semantic_index.current_parent_index_state(tmp_path, PAGE).document.units
    never, rare = units[0].unit_ref, units[2].unit_ref
    _correct(tmp_path, "Rare 1.", "The rare unit changed once.", index=2)
    for index in range(1, 13):
        _correct(tmp_path, f"Busy {index}.", "The busy unit keeps changing.", index=1)
    files = _entry_files_oldest_first(tmp_path)
    assert len(files) == 13
    broken = {files[position].stem for position in range(4, 8)}  # None touches the rare unit.
    for position in range(4, 8):
        files[position].write_text("not json", encoding="utf-8")
    visible = {r["version"] for r in _walk_history(tmp_path, limit=50)}
    assert len(visible) == 9 and not visible & broken

    pages = _unit_pages(tmp_path, rare, limit=1)
    assert [page["status"] for page in pages] == ["incomplete", "incomplete", "retained"]
    assert pages[0]["truncated"] and pages[0]["returned"] == 0
    assert all(page["continuation"] in visible for page in pages[:-1])  # Page-level history shows them.
    found = [r for page in pages for r in page["revisions"]]
    assert len(found) == 1 and "Rare 0." in found[0]["prior"]["source"]
    assert found[0]["version"] == files[0].stem

    # A unit with no revision at all stays incomplete until the walk ends, then unavailable.
    statuses = [page["status"] for page in _unit_pages(tmp_path, never, limit=1)]
    assert statuses == ["incomplete", "incomplete", "unavailable"]


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


def test_governed_legacy_correction_uses_existing_edit_and_keeps_prior_account(
    tmp_path: Path, owner
) -> None:
    source = _untyped_page_source("# Existing account\n\nThe window is 30 minutes.\n")
    _write_page(tmp_path, rel=UNTYPED_PAGE, source=source)
    before = commands.op_read_memory(tmp_path, path=UNTYPED_PAGE)
    operation = {
        "kind": "replace_string",
        "old_string": "The window is 30 minutes.",
        "new_string": "The window is 45 minutes.",
        "expected_hash": before["content_hash"],
    }
    preview = commands.op_edit_memory(
        tmp_path, path=UNTYPED_PAGE, why="A source corrected the window.",
        operation=operation, validate_only=True,
    )
    commands.op_edit_memory(
        tmp_path, path=UNTYPED_PAGE, why="A source corrected the window.",
        operation={**operation, "transition_token": preview["semantic"]["transition_token"]},
    )
    current = commands.op_read_memory(tmp_path, path=UNTYPED_PAGE, include_history=True)
    assert "The window is 45 minutes." in current["body"]
    revision, = current["revision_history"]["revisions"]
    assert revision["prior"]["source"] == source
    assert revision["reason"] == "A source corrected the window."
    assert list((tmp_path / "Knowledge Base/Notes").rglob("*.md")) == [tmp_path / UNTYPED_PAGE]


@pytest.mark.parametrize("claim", ["parent", "directory", "path", "prior", "before_hash", "after_hash", "version", "filename", "create_only", "guard", "late_create_only", "late_guard"])
def test_resealed_history_claim_cannot_borrow_an_unrelated_primary(
    tmp_path: Path, owner, claim: str,
) -> None:
    """A valid seal cannot authorize history that the primary did not replace."""
    source = _page_source("An earlier interpretation.")
    _write_page(tmp_path, source=source)
    after = source.replace("earlier", "corrected")
    before_state = semantic_index.build_parent_index_state(tmp_path, PAGE, source=source)
    after_state = semantic_index.build_parent_index_state(tmp_path, PAGE, source=after)
    history = compiled_history.plan_entry(
        tmp_path,
        preflight=SimpleNamespace(
            path=PAGE, before=SimpleNamespace(document=before_state.document),
            after=SimpleNamespace(document=after_state.document, source_hash=vault.content_hash(after)),
            before_source=source, exact_before_source=source, before_hash=vault.content_hash(source),
            after_source=after, operation="edit", committed_replay=False,
        ),
        token={"history_at": "2025-01-01T00:00:00Z", "transition_id": "a" * 64},
        reason="The source clarified the interpretation.",
    )
    assert history is not None
    entry = json.loads(history.content)
    target = history.path
    if claim == "parent":
        entry["parent_ref"] = "exomem://memory/33333333-3333-4333-8333-333333333333"
    elif claim == "directory":
        target = tmp_path / compiled_history.history_dir("foreign-parent") / target.name
    elif claim == "path":
        entry["path"] = "Knowledge Base/Notes/another.md"
    elif claim == "prior":
        entry["prior"]["source"] = source.replace("earlier", "unrelated")
        entry["before_hash"] = vault.content_hash(entry["prior"]["source"])
    elif claim == "before_hash":
        entry["before_hash"] = "b" * 64
    elif claim == "after_hash":
        entry["after_hash"] = "b" * 64
    elif claim == "version":
        entry["version"] = 2
    elif claim == "filename":
        target = target.with_name("20250101T000000000000Z-bbbbbbbb.json")
    forged = replace(
        history, path=target, content=json.dumps(entry) + "\n",
        create_only=claim != "create_only",
        guard=replace(
            vault.PathGuard.capture(tmp_path, target.relative_to(tmp_path).as_posix(), leaf_policy="absent"),
            leaf_policy="stable" if claim == "guard" else "absent",
        ),
    )
    primary = vault.PlannedWrite(
        tmp_path / PAGE, after,
        guard=vault.PathGuard.capture(tmp_path, PAGE, leaf_policy="content", expected_content_hash=vault.content_hash(source)),
    )
    manifest = vocabulary_auxiliaries.seal(tmp_path, primary=primary, derived=(("compiled-history", history),))
    if claim == "late_create_only":
        forged = replace(history, create_only=False)
    elif claim == "late_guard":
        forged = replace(history, guard=replace(history.guard, leaf_policy="stable"))
    with pytest.raises(ValueError, match="VOCABULARY_AUXILIARY"):
        if claim.startswith("late_"):
            vault._validate_batch_write_access(tmp_path, (forged, primary), auxiliary_manifest=manifest)
        else:
            manifest = vocabulary_auxiliaries.seal(tmp_path, primary=primary, derived=(("compiled-history", forged),))
            vocabulary_auxiliaries.validate(tmp_path, vocabulary_auxiliaries.planned_images(tmp_path, (forged, primary)), manifest)


def test_limited_legacy_path_ref_edit_keeps_public_prior_under_project_scope(
    configured_boundary, vault: Path,
) -> None:
    """Legacy history uses the producer's path identity without granting UUID authority."""
    from exomem import context_refs

    _, authenticate = configured_boundary
    source = "---\nproject: public-project\n---\nThe window is 30 minutes.\n"
    _write_page(vault, rel=UNTYPED_PAGE, source=source)
    with request_scope(authenticate("limited")):
        commands.op_edit_memory(
            vault, path=UNTYPED_PAGE, why="A source corrected the window.",
            operation={"kind": "replace_string", "old_string": "30 minutes", "new_string": "45 minutes"},
        )
        result = commands.op_read_memory(vault, path=UNTYPED_PAGE, include_history=True)
    revision, = result["revision_history"]["revisions"]
    assert revision["parent_ref"] == context_refs.vault_ref(UNTYPED_PAGE)
    assert revision["prior"]["source"] == source
    assert revision["reason"] == "A source corrected the window."


def test_denied_project_prior_matches_absent_history_in_the_complete_read(
    configured_boundary, vault: Path,
) -> None:
    """A denied prior must contribute neither text nor metadata to an admitted page."""
    _, authenticate = configured_boundary
    source = _untyped_page_source("A retained private interpretation.").replace(
        "status: active", "status: active\nproject: private-project",
    )
    _write_page(vault, rel=UNTYPED_PAGE, source=source)
    after = source.replace("private-project", "public-project").replace(
        "A retained private interpretation.", "The current public interpretation.",
    )
    with request_scope(authenticate("full")):
        prepared = semantic_writes.preflight_existing(vault, path=UNTYPED_PAGE, after_source=after, operation="edit")
        semantic_writes.commit_existing(vault, preflight=prepared, history_reason="A private rationale.")
        assert compiled_history.read(vault, path=UNTYPED_PAGE)["returned"] == 1
    with request_scope(authenticate("limited")):
        hidden = commands.op_read_memory(vault, path=UNTYPED_PAGE, include_history=True, history_limit=1)
        assert hidden["revision_history"]["returned"] == 0
        entry, = (vault / compiled_history.history_root()).rglob("*.json")
        entry.unlink()
        absent = commands.op_read_memory(vault, path=UNTYPED_PAGE, include_history=True, history_limit=1)
    assert hidden == absent
    assert "private interpretation" not in json.dumps(hidden, default=str)
    assert "private rationale" not in json.dumps(hidden, default=str)


LINKED_PAGE = "Knowledge Base/Notes/Legacy/window.md"
LINKED_SOURCE = (
    "---\ntitle: Window\nstatus: active\nproject: public-project\n"
    "exomem_id: 00000000-0000-4000-8000-0000000000a1\nupdated: 2026-07-15\n---\n\n"
    "The window is 30 minutes, as [[Ada Lovelace]] measured.\n"
)


def _linked_page_with_current_lookup(root: Path) -> None:
    """A name-linked page under a live registry, with the index a recall has just stamped."""
    _write_page(root, rel=LINKED_PAGE, source=LINKED_SOURCE)
    _seed_freshness_live(root)
    with request_scope(owner_principal(surface="mcp")):
        commands.op_activate_context(root, turn="What is the window?")
        current = commands.op_read_memory(root, path=PUBLIC, include_history=True)
    assert "lookup" not in current["revision_history"]


def _edit_window(root: Path) -> None:
    commands.op_edit_memory(
        root, path=LINKED_PAGE, why="A source corrected the window.",
        operation={"kind": "replace_string", "old_string": "30 minutes", "new_string": "45 minutes"},
    )


def test_a_name_linked_prior_refused_right_after_a_write_reads_once_the_stamped_build_lands(
    configured_boundary, vault: Path, monkeypatch,
) -> None:
    """Any write leaves name lookup stale; history says so and asks for the build that heals it."""
    _, authenticate = configured_boundary
    _linked_page_with_current_lookup(vault)
    with request_scope(authenticate("full")):
        _edit_window(vault)
        stale = commands.op_read_memory(vault, path=LINKED_PAGE, include_history=True)
        no_history = commands.op_read_memory(vault, path=PUBLIC, include_history=True)
        monkeypatch.setattr(readiness, "runtime_managed", lambda: True)
        commands.op_read_memory(vault, path=LINKED_PAGE, include_history=True)
        working_set_runtime.join_builds(vault)
        healed = commands.op_read_memory(vault, path=LINKED_PAGE, include_history=True)

    stale, no_history, healed = (r["revision_history"] for r in (stale, no_history, healed))
    assert (stale["status"], stale["returned"], stale["lookup"]) == ("unavailable", 0, "stale")
    # Vault-wide: a page with nothing retained reports the same state.
    assert (no_history["status"], no_history["lookup"]) == ("unavailable", "stale")
    assert (healed["status"], healed["returned"]) == ("retained", 1)
    assert "lookup" not in healed
    assert healed["revisions"][0]["prior"]["source"] == LINKED_SOURCE


def test_a_restricted_caller_gets_no_write_revealing_lookup_marker(
    configured_boundary, vault: Path,
) -> None:
    """Lookup state flips on every write, withheld pages included, like recall's vault generation."""
    _, authenticate = configured_boundary
    _linked_page_with_current_lookup(vault)
    with request_scope(authenticate("full")):
        _edit_window(vault)
    with request_scope(authenticate("limited")):
        limited = commands.op_read_memory(vault, path=LINKED_PAGE, include_history=True)

    history = limited["revision_history"]
    assert (history["status"], history["returned"]) == ("unavailable", 0)
    assert "lookup" not in history


def test_retained_bridge_cannot_borrow_the_current_bridge_approval(vault: Path) -> None:
    """An approved current bridge must not authorize different retained bridge bytes."""
    from test_governance_bridges import BRIDGE_PATH, _external, _write_bridge_fixture

    from exomem import registry_history

    bridge, _ = _write_bridge_fixture(vault, approval=False)
    snapshot = bridge.read_bytes()
    with request_scope(_external()):
        with pytest.raises(registry_history.HistoryAdmissionRefused):
            registry_history._require_snapshot_admission(vault, BRIDGE_PATH, snapshot, markdown=True)
    _write_bridge_fixture(vault, approval=True)
    with request_scope(_external()):
        registry_history._require_snapshot_admission(vault, BRIDGE_PATH, snapshot, markdown=True)
        prior = snapshot.replace(b"unlimited evening capacity", b"unlimited daily capacity")
        assert prior != snapshot
        with pytest.raises(registry_history.HistoryAdmissionRefused):
            registry_history._require_snapshot_admission(vault, BRIDGE_PATH, prior, markdown=True)


@pytest.mark.parametrize("change", ["hash", "identity", "unrelated_ref"])
def test_terminal_history_binding_cannot_outlive_its_exact_current_parent(
    configured_boundary, vault: Path, change: str,
) -> None:
    """Terminal shaping must not bind stale parent evidence or another memory URI."""
    from exomem.governance import egress

    _, authenticate = configured_boundary
    source = _untyped_page_source("An earlier interpretation.").replace(
        "status: active", "status: active\nproject: public-project",
    )
    _write_page(vault, rel=UNTYPED_PAGE, source=source)
    with request_scope(authenticate("limited")):
        commands.op_edit_memory(
            vault, path=UNTYPED_PAGE, why="A clarified interpretation.",
            operation={"kind": "replace_string", "old_string": "earlier", "new_string": "corrected"},
        )
        current = commands.op_read_memory(vault, path=UNTYPED_PAGE, include_history=True)
        assert egress.postfilter("read_memory", current, vault)["revision_history"]["returned"] == 1
        if change == "hash":
            current["content_hash"] = "b" * 64
        elif change == "identity":
            page = vault / UNTYPED_PAGE
            page.write_text(page.read_text().replace(
                "00000000-0000-4000-8000-000000000082", "33333333-3333-4333-8333-333333333333",
            ))
        else:
            current["revision_history"]["parent_ref"] = "exomem://memory/33333333-3333-4333-8333-333333333333"
        filtered = egress.postfilter("read_memory", current, vault)
    assert "revision_history" not in filtered


def test_a_registered_dynamic_entity_keeps_both_prior_meanings_on_one_node(
    vault: Path, owner
) -> None:
    from exomem import entity_types, memory_refs
    from exomem.governance.principal import library_scope

    proposal = {
        "schema_version": 1,
        "entity_types": {
            "site": {
                "folder": "Sites",
                "label": "Site",
                "aliases": [],
                "capture_guidance": "A stable physical site identity.",
            }
        },
    }
    with library_scope():
        commands.op_schema_memory(
            vault,
            operation="save-entity-types",
            proposal=proposal,
            expected_hash=entity_types.load_entity_types(vault).extension_hash,
            why="Register a synthetic site type.",
        )
        created = commands.op_connect_memory(
            vault,
            operation="create-entity",
            entity_type="site",
            name="Kestrel Field",
            summary="The window is 30 minutes.",
        )
    path = created["path"]
    original = commands.op_read_memory(vault, path=path)
    for old, new, why in (
        ("30 minutes", "45 minutes", "A source corrected the window."),
        ("45 minutes", "60 minutes", "A later source corrected it again."),
    ):
        current = commands.op_read_memory(vault, path=path)
        operation = {
            "kind": "replace_string",
            "old_string": f"The window is {old}.",
            "new_string": f"The window is {new}.",
            "expected_hash": current["content_hash"],
        }
        preview = commands.op_edit_memory(
            vault, path=path, why=why, operation=operation, validate_only=True
        )
        commands.op_edit_memory(
            vault,
            path=path,
            why=why,
            operation={**operation, "transition_token": preview["semantic"]["transition_token"]},
        )
    result = commands.op_read_memory(vault, path=path, include_history=True)
    history = result["revision_history"]
    assert "The window is 60 minutes." in result["body"]
    assert [r["reason"] for r in history["revisions"]] == [
        "A later source corrected it again.",
        "A source corrected the window.",
    ]
    assert "The window is 45 minutes." in history["revisions"][0]["prior"]["source"]
    assert "The window is 30 minutes." in history["revisions"][1]["prior"]["source"]
    assert history["revisions"][1]["before_hash"] == original["content_hash"]
    identity = original["frontmatter"][memory_refs.ID_FIELD]
    assert result["frontmatter"][memory_refs.ID_FIELD] == identity
    assert history["parent_ref"] == memory_refs.memory_ref(identity)
    assert result["path"] == path
    assert [p.name for p in (vault / "Knowledge Base/Entities/Sites").glob("*.md")] == [
        "Kestrel Field.md"
    ]


def test_prior_revision_stays_readable_when_page_definitions_become_unreadable(
    tmp_path: Path, owner, monkeypatch
) -> None:
    from exomem.vocabulary import instances

    _write_page(tmp_path, source=_page_source("- [rule] First wording. ^stable"))
    unit = semantic_index.current_parent_index_state(tmp_path, PAGE).document.units[0]
    _correct(tmp_path, "Second wording.", "A correction.")

    def unreadable(*args, **kwargs):
        raise ValueError("REGISTRY_UNAVAILABLE: definitions cannot be read")

    monkeypatch.setattr(instances, "page_definitions", unreadable)
    found = compiled_history.read(tmp_path, path=PAGE, unit_ref=unit.unit_ref)
    revision, = found["revisions"]
    assert "First wording." in revision["prior"]["source"]
    assert revision["prior"]["unit_status"] == "found"
    other = compiled_history.read(tmp_path, path=PAGE)["revisions"][0]
    assert other["prior"]["source"] == revision["prior"]["source"]


def test_a_prior_unit_reports_unavailable_after_registry_bytes_become_unreadable(
    tmp_path: Path, owner
) -> None:
    source = _page_source("## Custom section\n- [rule] First wording. ^stable")
    _write_page(tmp_path, source=source)
    overlay = semantic_language_registry.registry_path(tmp_path)
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(
        json.dumps(semantic_language_registry.empty_proposal()), encoding="utf-8"
    )
    unit = semantic_index.current_parent_index_state(tmp_path, PAGE).document.units[0]
    _correct(tmp_path, "Second wording.", "A correction.")
    original = commands.op_read_memory(
        tmp_path, path=PAGE, unit_ref=unit.unit_ref, include_history=True
    )
    assert original["revision_history"]["revisions"][0]["prior"]["unit_status"] == "found"

    overlay.write_bytes(b"\xff")

    result = commands.op_read_memory(
        tmp_path, path=PAGE, unit_ref=unit.unit_ref, include_history=True
    )
    revision, = result["revision_history"]["revisions"]
    assert revision["prior"]["source"] == source
    assert revision["prior"]["unit_status"] == "unavailable"
    assert revision["prior"]["unit"] is None


def test_history_is_unreadable_once_the_page_is_deleted(tmp_path: Path, owner) -> None:
    _write_page(tmp_path, source=_page_source("- [rule] First wording. ^stable"))
    _correct(tmp_path, "Second wording.", "A correction.")
    commands.op_delete(tmp_path, path=PAGE, confirm=True)
    with pytest.raises(ValueError) as direct:
        compiled_history.read(tmp_path, path=PAGE)
    with pytest.raises(ValueError) as through_read:
        commands.op_read_memory(tmp_path, path=PAGE, include_history=True)
    assert str(direct.value).startswith("NOT_FOUND:")
    assert str(through_read.value).startswith("NOT_FOUND:")
