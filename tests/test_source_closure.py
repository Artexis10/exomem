from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from exomem import add as add_module
from exomem import audit as audit_module
from exomem import cli_ops, source_closure
from exomem import create_file as create_file_module
from exomem import edit as edit_module
from exomem import note as note_module
from exomem import schema as schema_module
from exomem import set_frontmatter_field as set_frontmatter_module


def _compiled(*sources: str, include_field: bool = True) -> str:
    lines = ["---", "type: insight", "status: active"]
    if include_field:
        if sources:
            lines.append("sources:")
            lines.extend(f'  - "[[{value}]]"' for value in sources)
        else:
            lines.append("sources: []")
    lines.extend(("---", "", "# Derived note", ""))
    return "\n".join(lines)


def _captured(
    root: Path,
    rel: str,
    *,
    page_type: str = "source",
    identity: str | None = None,
) -> str:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    identity = identity or str(uuid.uuid4())
    target.write_text(
        "\n".join(
            (
                "---",
                f"type: {page_type}",
                f"exomem_id: {identity}",
                "ingested_into: []",
                "---",
                "",
                "# Captured original",
                "",
            )
        ),
        encoding="utf-8",
    )
    return identity


def test_absent_and_empty_sources_are_closed(vault: Path) -> None:
    absent = source_closure.inspect_source_closure(vault, _compiled(include_field=False))
    empty = source_closure.inspect_source_closure(vault, _compiled())

    assert absent.closed is True
    assert empty.closed is True
    assert absent.asserted_values == ()
    assert empty.asserted_values == ()


def test_current_path_and_stable_reference_close(vault: Path) -> None:
    rel = "Knowledge Base/Sources/Articles/captured-original.md"
    identity = _captured(vault, rel)

    current = source_closure.inspect_source_closure(vault, _compiled(rel.removesuffix(".md")))
    stable = source_closure.inspect_source_closure(vault, _compiled(f"exomem://memory/{identity}"))

    assert current.closed is True
    assert stable.closed is True
    assert current.resolved_paths == (rel,)
    assert stable.resolved_paths == (rel,)


def test_external_locators_and_ineligible_pages_do_not_close(vault: Path) -> None:
    ineligible = "Knowledge Base/Notes/Insights/not-an-original.md"
    _captured(vault, ineligible, page_type="insight")
    supplied = (
        "https://example.invalid/original",
        "drive-file:1A2B3C",
        "connector-message:987654",
        ineligible.removesuffix(".md"),
    )

    result = source_closure.inspect_source_closure(vault, _compiled(*supplied))

    assert result.closed is False
    assert result.unresolved_values == supplied


def test_mixed_refusal_is_deterministic_and_bounded(vault: Path) -> None:
    captured = "Knowledge Base/Sources/Other/present.md"
    _captured(vault, captured)
    missing = tuple(
        f"Knowledge Base/Sources/Other/missing-{index}"
        for index in range(source_closure.PUBLIC_UNRESOLVED_LIMIT + 3)
    )

    first = source_closure.inspect_source_closure(
        vault, _compiled(captured.removesuffix(".md"), *missing)
    )
    second = source_closure.inspect_source_closure(
        vault, _compiled(captured.removesuffix(".md"), *missing)
    )

    assert first.public_details() == second.public_details()
    assert first.public_details() == {
        "unresolved_sources": list(missing[: source_closure.PUBLIC_UNRESOLVED_LIMIT]),
        "unresolved_source_count": len(missing),
        "unresolved_sources_truncated": True,
    }
    assert first.resolved_paths == (captured,)


def test_missing_withheld_and_ineligible_have_the_same_public_shape(
    vault: Path,
) -> None:
    supplied = "Knowledge Base/Sources/Private/claimed-original"
    _captured(vault, supplied + ".md")

    withheld = source_closure.inspect_source_closure(
        vault,
        _compiled(supplied),
        authorize_path=lambda _path: False,
    )

    missing_root = vault.parent / "missing-vault"
    missing_root.mkdir()
    missing = source_closure.inspect_source_closure(
        missing_root,
        _compiled(supplied),
        authorize_path=lambda _path: True,
    )

    ineligible_root = vault.parent / "ineligible-vault"
    _captured(ineligible_root, supplied + ".md", page_type="insight")
    ineligible = source_closure.inspect_source_closure(
        ineligible_root,
        _compiled(supplied),
        authorize_path=lambda _path: True,
    )

    assert withheld.public_details() == missing.public_details()
    assert ineligible.public_details() == missing.public_details()
    assert set(missing.public_details()) == {
        "unresolved_sources",
        "unresolved_source_count",
        "unresolved_sources_truncated",
    }


def _remember(vault: Path, *, title: str, sources: list[str]):
    return note_module.note(
        vault,
        content="## Claim\n\nA compiled conclusion.\n",
        note_type="insight",
        title=title,
        status="draft",
        sources=sources,
    )


def test_cite_before_capture_refuses_without_partial_state(vault: Path) -> None:
    missing = "Knowledge Base/Sources/Articles/not-captured"

    with pytest.raises(note_module.NoteError) as caught:
        _remember(vault, title="Cite before capture", sources=[missing])

    assert caught.value.code == "UNRESOLVED_SOURCE_CITATION"
    assert caught.value.details == {
        "unresolved_sources": [missing],
        "unresolved_source_count": 1,
        "unresolved_sources_truncated": False,
    }
    assert not (vault / "Knowledge Base/Notes/Insights/cite-before-capture.md").exists()


def test_mixed_create_refuses_without_touching_resolved_source(vault: Path) -> None:
    source_rel = "Knowledge Base/Sources/Articles/present-original.md"
    _captured(vault, source_rel)
    source_path = vault / source_rel
    before = source_path.read_bytes()

    with pytest.raises(note_module.NoteError) as caught:
        _remember(
            vault,
            title="Mixed provenance",
            sources=[
                source_rel.removesuffix(".md"),
                "Knowledge Base/Sources/Articles/absent-original",
            ],
        )

    assert caught.value.code == "UNRESOLVED_SOURCE_CITATION"
    assert source_path.read_bytes() == before
    assert not (vault / "Knowledge Base/Notes/Insights/mixed-provenance.md").exists()


def test_capture_then_cite_updates_both_sides(vault: Path) -> None:
    source_rel = "Knowledge Base/Sources/Articles/captured-first.md"
    _captured(vault, source_rel)

    result = _remember(
        vault,
        title="Capture then cite",
        sources=[source_rel.removesuffix(".md")],
    )

    assert (vault / result.path).is_file()
    source_text = (vault / source_rel).read_text(encoding="utf-8")
    assert "[[Knowledge Base/Notes/Insights/capture-then-cite]]" in source_text


def test_writer_preserves_external_value_in_shared_public_refusal(vault: Path) -> None:
    supplied = "https://example.invalid/external-only"

    with pytest.raises(note_module.NoteError) as caught:
        _remember(vault, title="External only", sources=[supplied])

    error = cli_ops.error_dict(caught.value)
    assert error == {
        "code": "UNRESOLVED_SOURCE_CITATION",
        "message": "one or more explicit sources do not resolve to captured material",
        "remediation": source_closure.UNRESOLVED_REMEDIATION,
        "unresolved_sources": [supplied],
        "unresolved_source_count": 1,
        "unresolved_sources_truncated": False,
        "mutated": False,
    }


def test_writer_accepts_stable_reference_and_renders_current_path(vault: Path) -> None:
    source_rel = "Knowledge Base/Sources/Articles/stable-original.md"
    identity = _captured(vault, source_rel)

    result = _remember(
        vault,
        title="Stable citation",
        sources=[f"exomem://memory/{identity}"],
    )

    derived = (vault / result.path).read_text(encoding="utf-8")
    assert f'"[[{source_rel.removesuffix(".md")}]]"' in derived


def _legacy_derived(vault: Path, *sources: str) -> tuple[str, Path]:
    rel = "Knowledge Base/Notes/Insights/legacy-source-debt.md"
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_compiled(*sources), encoding="utf-8")
    return rel, path


def test_unrelated_edit_preserves_legacy_unresolved_source(vault: Path) -> None:
    missing = "Knowledge Base/Sources/Other/legacy-missing"
    rel, path = _legacy_derived(vault, missing)

    edit_module.edit(
        vault,
        path=rel,
        why="correct the reader-facing heading",
        old_string="# Derived note",
        new_string="# Clearer derived note",
    )

    final = path.read_text(encoding="utf-8")
    assert "# Clearer derived note" in final
    assert missing in final


def test_source_changing_edit_validates_the_complete_final_list(vault: Path) -> None:
    old = "Knowledge Base/Sources/Other/old-missing"
    still_missing = "Knowledge Base/Sources/Other/still-missing"
    captured = "Knowledge Base/Sources/Other/newly-captured"
    _captured(vault, captured + ".md")
    rel, path = _legacy_derived(vault, old, still_missing)
    before = path.read_bytes()

    with pytest.raises(set_frontmatter_module.SetFrontmatterError) as caught:
        set_frontmatter_module.set_frontmatter_field(
            vault,
            path=rel,
            why="repair one source claim",
            field="sources",
            value=[captured, still_missing],
        )

    assert caught.value.code == "UNRESOLVED_SOURCE_CITATION"
    assert path.read_bytes() == before


def test_complete_tier2_replacement_reasserts_unchanged_source_claim(
    vault: Path,
) -> None:
    missing = "Knowledge Base/Sources/Other/legacy-missing"
    rel, path = _legacy_derived(vault, missing)
    before = path.read_bytes()

    with pytest.raises(create_file_module.CreateFileError) as caught:
        create_file_module.create_file(
            vault,
            path=rel,
            content=before.decode("utf-8"),
            overwrite=True,
        )

    assert caught.value.code == "UNRESOLVED_SOURCE_CITATION"
    assert path.read_bytes() == before


def test_retry_after_original_capture_commits_the_unchanged_derived_write(
    vault: Path,
) -> None:
    source_rel = "Knowledge Base/Sources/Articles/recovered-original.md"
    supplied = source_rel.removesuffix(".md")

    with pytest.raises(note_module.NoteError) as first:
        _remember(vault, title="Retry after capture", sources=[supplied])
    assert first.value.code == "UNRESOLVED_SOURCE_CITATION"

    _captured(vault, source_rel)
    committed = _remember(vault, title="Retry after capture", sources=[supplied])

    assert (vault / committed.path).is_file()
    assert "retry-after-capture" in (vault / source_rel).read_text(encoding="utf-8")


def test_concurrent_source_change_refuses_without_half_publishing(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_rel = "Knowledge Base/Sources/Articles/concurrent-original.md"
    _captured(vault, source_rel)
    source_path = vault / source_rel
    original_enforce = source_closure.enforce_source_closure
    raced = False

    def race_then_enforce(*args, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            source_path.write_text(
                source_path.read_text(encoding="utf-8") + "\nConcurrent capture metadata.\n",
                encoding="utf-8",
            )
        return original_enforce(*args, **kwargs)

    monkeypatch.setattr(source_closure, "enforce_source_closure", race_then_enforce)

    with pytest.raises(note_module.NoteError) as caught:
        _remember(
            vault,
            title="Concurrent source",
            sources=[source_rel.removesuffix(".md")],
        )

    assert caught.value.code == "STALE_SEMANTIC_WRITE"
    assert "Concurrent capture metadata" in source_path.read_text(encoding="utf-8")
    assert "concurrent-source" not in source_path.read_text(encoding="utf-8")
    assert not (vault / "Knowledge Base/Notes/Insights/concurrent-source.md").exists()


def test_capture_preserves_external_origin_then_governed_citation_closes(
    vault: Path,
    source_schema: schema_module.SourceSchema,
) -> None:
    origin = "https://example.invalid/messages/connector-123"
    captured = add_module.add(
        vault,
        source_schema,
        content=(
            "Original connector payload.\n"
            "Connector message ID: connector-123\n"
            "Remote file ID: remote-file-456\n"
        ),
        source_type="article",
        title="Captured connector original",
        url=origin,
    )

    source_text = (vault / captured.path).read_text(encoding="utf-8")
    assert origin in source_text
    assert "connector-123" in source_text
    assert "remote-file-456" in source_text

    derived = _remember(
        vault,
        title="Derived from captured connector material",
        sources=[captured.path.removesuffix(".md")],
    )
    assert (vault / derived.path).is_file()


def test_compiled_writer_never_invokes_capture_for_external_locator(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invoked = False

    def forbidden_capture(*_args, **_kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("compiled source closure must remain network/capture free")

    monkeypatch.setattr(add_module, "add", forbidden_capture)

    with pytest.raises(note_module.NoteError) as caught:
        _remember(
            vault,
            title="No implicit capture",
            sources=["connector-message:uncaptured-123"],
        )

    assert caught.value.code == "UNRESOLVED_SOURCE_CITATION"
    assert invoked is False


def test_authorization_change_is_rechecked_before_publication(vault: Path) -> None:
    source_rel = "Knowledge Base/Sources/Private/authorization-original.md"
    _captured(vault, source_rel)
    markdown = _compiled(source_rel.removesuffix(".md"))
    destination = "Knowledge Base/Notes/Insights/authorization-derived.md"
    prepared = source_closure.prepare_source_closure(
        vault,
        markdown,
        destination=destination,
        authorize_path=lambda _path: True,
    )

    with pytest.raises(source_closure.SourceClosureViolation) as caught:
        source_closure.enforce_source_closure(
            vault,
            markdown,
            destination=destination,
            prepared=prepared,
            authorize_path=lambda _path: False,
        )

    assert caught.value.code == "UNRESOLVED_SOURCE_CITATION"
    assert caught.value.details == {
        "unresolved_sources": [source_rel.removesuffix(".md")],
        "unresolved_source_count": 1,
        "unresolved_sources_truncated": False,
    }


def test_stable_reference_retry_tracks_source_relocation(vault: Path) -> None:
    old_rel = "Knowledge Base/Sources/Other/original-location.md"
    new_rel = "Knowledge Base/Sources/Articles/current-location.md"
    identity = _captured(vault, old_rel)
    markdown = _compiled(f"exomem://memory/{identity}")
    destination = "Knowledge Base/Notes/Insights/relocated-source.md"
    prepared = source_closure.prepare_source_closure(
        vault,
        markdown,
        destination=destination,
    )

    new_path = vault / new_rel
    new_path.parent.mkdir(parents=True, exist_ok=True)
    (vault / old_rel).rename(new_path)

    with pytest.raises(source_closure.SourceClosureViolation) as stale:
        source_closure.enforce_source_closure(
            vault,
            markdown,
            destination=destination,
            prepared=prepared,
        )
    assert stale.value.code == "STALE_SEMANTIC_WRITE"

    retried = source_closure.prepare_source_closure(
        vault,
        markdown,
        destination=destination,
    )
    source_closure.enforce_source_closure(
        vault,
        markdown,
        destination=destination,
        prepared=retried,
    )
    assert retried.inspection.resolved_paths == (new_rel,)


def test_source_change_moves_supported_backref_in_the_same_commit(vault: Path) -> None:
    first = "Knowledge Base/Sources/Other/first-original.md"
    second = "Knowledge Base/Sources/Other/second-original.md"
    _captured(vault, first)
    _captured(vault, second)
    created = _remember(
        vault,
        title="Move the source edge",
        sources=[first.removesuffix(".md")],
    )

    set_frontmatter_module.set_frontmatter_field(
        vault,
        path=created.path,
        why="replace the explicit source claim",
        field="sources",
        value=[second.removesuffix(".md")],
    )

    derived_link = f"[[{created.path.removesuffix('.md')}]]"
    assert derived_link not in (vault / first).read_text(encoding="utf-8")
    assert derived_link in (vault / second).read_text(encoding="utf-8")


def test_end_to_end_provenance_fixture_never_promotes_a_partial_derivative(
    vault: Path,
    source_schema: schema_module.SourceSchema,
) -> None:
    missing_original = "Knowledge Base/Sources/Other/client-script-original"
    legacy_rel, legacy_path = _legacy_derived(vault, missing_original)
    legacy_path.write_text(
        legacy_path.read_text(encoding="utf-8").replace("status: active", "status: draft", 1),
        encoding="utf-8",
    )
    partial_rel = "Knowledge Base/Notes/Insights/partial-script-summary.md"
    partial = vault / partial_rel
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_text(
        _compiled(include_field=False)
        .replace("status: active", "status: draft", 1)
        .replace("# Derived note", "# Partial script summary", 1),
        encoding="utf-8",
    )

    with pytest.raises(note_module.NoteError) as before_capture:
        _remember(
            vault,
            title="Cite the absent original",
            sources=[missing_original],
        )
    assert before_capture.value.code == "UNRESOLVED_SOURCE_CITATION"
    assert not (vault / "Knowledge Base/Notes/Insights/cite-the-absent-original.md").exists()

    with pytest.raises(note_module.NoteError) as partial_attempt:
        _remember(
            vault,
            title="Wrongly promote a partial derivative",
            sources=[partial_rel.removesuffix(".md")],
        )
    assert partial_attempt.value.code == "UNRESOLVED_SOURCE_CITATION"
    assert not (
        vault / "Knowledge Base/Notes/Insights/wrongly-promote-a-partial-derivative.md"
    ).exists()

    debt = audit_module.audit(vault, categories=["unresolved_source_citation"])
    assert [finding.path for finding in debt.findings] == [legacy_rel]
    edit_module.edit(
        vault,
        path=legacy_rel,
        why="clarify the legacy derivative without changing provenance",
        old_string="# Derived note",
        new_string="# Clarified legacy derivative",
    )

    captured = add_module.add(
        vault,
        source_schema,
        content="Original script supplied by its author.\n",
        source_type="other",
        title="Captured client script original",
        url="https://example.invalid/original-script",
    )
    set_frontmatter_module.set_frontmatter_field(
        vault,
        path=legacy_rel,
        why="replace the unsupported citation with the captured original",
        field="sources",
        value=[captured.path.removesuffix(".md")],
    )
    valid = _remember(
        vault,
        title="Valid derivative from captured original",
        sources=[captured.path.removesuffix(".md")],
    )

    assert (vault / valid.path).is_file()
    assert audit_module.audit(vault, categories=["unresolved_source_citation"]).findings == []
    captured_text = (vault / captured.path).read_text(encoding="utf-8")
    assert f"[[{legacy_rel.removesuffix('.md')}]]" in captured_text
    assert f"[[{valid.path.removesuffix('.md')}]]" in captured_text
    assert "Partial script summary" in partial.read_text(encoding="utf-8")
    assert "Partial script summary" not in captured_text
