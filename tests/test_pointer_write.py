"""An append-only page changes only in the pointer spans a call declares.

OpenSpec add-curation-relocation-protocol, group 2. The vault is the canonical fixture in file
mode. Each test names the failure it catches.
"""

from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path

import pytest

from exomem import commands, page_fields, pointer_write, set_frontmatter_field, writer_lease
from exomem.governance.principal import library_scope
from exomem.vocabulary import registry

TODAY = dt.date(2026, 11, 16)
SOURCE = "Knowledge Base/Sources/Articles/2026-05-04-best-egcg-supplements.md"
NOTE = "Knowledge Base/Notes/Insights/autovacuum-thresholds-prevent-table-bloat.md"
MOVED_NOTE = "Knowledge Base/Notes/Insights/Postgres/autovacuum-thresholds-prevent-table-bloat.md"
OTHER_NOTE = "Knowledge Base/Notes/Insights/vacuum-freeze-ages.md"
SCHEMA = next(c for c in commands.PRODUCT_COMMANDS if c.name == "schema_memory")


def _with_identity(vault: Path, rel: str, *, title: str | None = None) -> None:
    path = vault / rel
    if not path.exists():
        path.write_text(f"---\ntitle: {title}\ntype: note\n---\n# {title}\n", encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("---\n", f"---\nexomem_id: {uuid.uuid4()}\n", 1), encoding="utf-8")


def _source_linking(vault: Path, target: str) -> str:
    """The fixture Source with a body line that links `target` by path; returns its bytes."""
    path = vault / SOURCE
    text = path.read_text(encoding="utf-8") + f"\nSee [[{target.removesuffix('.md')}]] for tuning.\n"
    path.write_text(text, encoding="utf-8")
    return text


def _schema(vault: Path, operation: str, **arguments: object) -> dict:
    """The owner's `schema_memory` call on the page-fields registry."""
    with library_scope():
        return writer_lease.invoke_command(
            SCHEMA, vault, operation=operation, subject="page-fields", **arguments
        )


def test_a_vault_overlay_that_retires_a_shipped_pointer_takes_it_out_of_the_pointer_write(
    vault: Path,
) -> None:
    """A narrowing rule that also refuses a retirement, so an owner cannot stop pointer
    corrections of a field the shipped pack declares."""
    page_fields.registry_path(vault).write_text(
        "schema_version: 1\nentries:\n  project:\n    status: deprecated\n", encoding="utf-8"
    )
    registry.invalidate(vault)

    snapshot = registry.load(page_fields.SPEC, vault)

    assert list(snapshot.findings) == []
    assert "project" not in page_fields.pointer_fields(vault)
    assert "projects" in page_fields.pointer_fields(vault)


@pytest.mark.parametrize("field", ["captured", "exomem_id"], ids=["provenance", "identity"])
def test_a_schema_save_that_makes_a_captured_field_a_pointer_refuses_and_the_patch_still_refuses(
    vault: Path, field: str
) -> None:
    """The three-call path by which an agent declares a provenance or identity field a pointer
    through `schema_memory` and then rewrites it on a Source."""
    inspected = _schema(vault, "inspect")
    with pytest.raises(registry.RegistryError) as widened:
        _schema(
            vault, "save", expected_hash=inspected["content_hash"], why="correct it on captures",
            proposal={"upsert": {field.replace("_", "-"): {
                "label": "Field", "attributes": {"field": field, "role": "pointer"},
            }}},
        )
    with pytest.raises(set_frontmatter_field.SetFrontmatterError) as refused:
        set_frontmatter_field.set_frontmatter_field(
            vault, path=SOURCE, field=field, value="2026-05-05", why="correct it", today=TODAY,
        )

    assert str(widened.value).startswith(page_fields.WIDENING_CODE)
    assert field not in page_fields.pointer_fields(vault)
    assert refused.value.code in {"APPEND_ONLY", "INVALID_SET"}


def test_an_overlay_file_that_gives_a_lineage_field_a_role_is_reported_and_ignored(
    vault: Path,
) -> None:
    """An overlay written straight to disk that makes `moved_from` a pointer, so a caller
    could forge where a Source moved from."""
    page_fields.registry_path(vault).write_text(
        "schema_version: 1\nentries:\n  moved-from:\n    label: Moved from\n"
        "    attributes: {field: moved_from, role: pointer}\n",
        encoding="utf-8",
    )
    registry.invalidate(vault)

    snapshot = registry.load(page_fields.SPEC, vault)

    assert [finding["code"] for finding in snapshot.findings] == ["invalid_registry_overlay"]
    assert page_fields.WIDENING_CODE in snapshot.findings[0]["detail"]
    assert "moved_from" not in page_fields.pointer_fields(vault)
    assert "project" in page_fields.pointer_fields(vault)


def test_a_repointed_source_link_is_the_only_span_that_differs(vault: Path) -> None:
    """A proof that admits a link change without pairing every other byte of the page."""
    _with_identity(vault, NOTE)
    before = _source_linking(vault, NOTE)
    after = before.replace(f"[[{NOTE[:-3]}]]", f"[[{MOVED_NOTE[:-3]}]]")

    write = pointer_write.plan(
        vault, SOURCE, before, after, pointer_write.Declared(moves={NOTE: MOVED_NOTE}),
        reason="the note moved",
    )

    assert [(span.kind, span.before, span.after) for span in write.spans] == [
        ("link", NOTE[:-3], MOVED_NOTE[:-3])
    ]
    span = write.spans[0]
    assert before[: span.before_at] == after[: span.after_at]
    assert before[span.before_at + len(span.before) :] == after[span.after_at + len(span.after) :]


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda text: text.replace(f"[[{NOTE[:-3]}]]", f"[[{OTHER_NOTE[:-3]}]]"),
                     id="link-to-another-page"),
        pytest.param(lambda text: text.replace("source_type: article", "source_type: report"),
                     id="undeclared-field"),
        pytest.param(lambda text: text.replace("captured: 2026-05-04", "captured: 2026-05-05"),
                     id="declared-field-that-is-not-a-pointer"),
        pytest.param(lambda text: text.replace("300–400 mg/day", "300–500 mg/day"),
                     id="body-text"),
    ],
)
def test_a_crafted_change_outside_a_declared_span_refuses(vault: Path, change) -> None:
    """A pointer write that lets a caller repoint a link to another page, edit a field it did not
    declare, edit a declared field that the registry does not hold as a pointer, or change
    captured body text."""
    _with_identity(vault, NOTE)
    _with_identity(vault, OTHER_NOTE, title="Vacuum freeze ages")
    before = _source_linking(vault, NOTE)

    with pytest.raises(pointer_write.PointerWriteError) as refused:
        pointer_write.plan(
            vault, SOURCE, before, change(before),
            pointer_write.Declared(fields={"project", "captured"}, moves={NOTE: MOVED_NOTE}),
            reason="crafted",
        )

    assert refused.value.code == "APPEND_ONLY"
    assert (vault / SOURCE).read_text(encoding="utf-8") == before


def _correct(vault: Path, rel: str, value: str, **kwargs: object):
    return set_frontmatter_field.set_pointer_field(
        vault, path=rel, field="project", value=value, why="the survey merged",
        expected_hash=None, today=TODAY, **kwargs,
    )


def test_a_project_correction_changes_only_its_span_and_a_direct_patch_names_the_curation_route(
    vault: Path,
) -> None:
    """A correction that bumps `updated` or touches other bytes of a Source, or a direct patch
    that changes a captured page without the plan its owner confirms."""
    path = vault / SOURCE
    before = path.read_text(encoding="utf-8").replace(
        "ingested_into: []\n", "ingested_into: []\nproject: harbour-survey\n"
    )
    path.write_text(before, encoding="utf-8")

    with pytest.raises(set_frontmatter_field.SetFrontmatterError) as direct:
        set_frontmatter_field.set_frontmatter_field(
            vault, path=SOURCE, field="project", value="coastal-survey", why="merged", today=TODAY,
        )
    preview = _correct(vault, SOURCE, "coastal-survey", validate_only=True)
    result = _correct(vault, SOURCE, "coastal-survey")

    assert direct.value.code == "APPEND_ONLY"
    assert "curation" in direct.value.reason and "edit" in direct.value.reason
    assert (preview.old_value, preview.new_value) == ("harbour-survey", "coastal-survey")
    assert path.read_text(encoding="utf-8") == before.replace(
        "project: harbour-survey", "project: coastal-survey"
    )
    assert result.pointer_write is not None


@pytest.mark.parametrize(
    ("field", "route"),
    [("source_type", "relocates"), ("domain", "relocates"), ("evidence_file", "raw file")],
)
def test_a_correction_of_a_field_another_operation_owns_refuses_and_names_it(
    vault: Path, field: str, route: str
) -> None:
    """A plain correction of a Source's classification or raw-file pointer, which skips the
    classification history, the relocation or the file-identity check its own operation makes."""
    before = (vault / SOURCE).read_bytes()

    with pytest.raises(set_frontmatter_field.SetFrontmatterError) as refused:
        set_frontmatter_field.set_pointer_field(
            vault, path=SOURCE, field=field, value="report", why="reclass it",
            expected_hash=None, today=TODAY,
        )

    assert refused.value.code == "POINTER_FIELD_OWNED"
    assert route in refused.value.reason
    assert (vault / SOURCE).read_bytes() == before


def test_a_revert_restores_the_exact_before_bytes_and_refuses_after_later_work(
    vault: Path,
) -> None:
    """A revert that rebuilds bytes other than those the write found, or that overwrites work
    done on the page after the write."""
    path = vault / SOURCE
    before = path.read_text(encoding="utf-8")
    first = _correct(vault, SOURCE, "coastal-survey")

    record = pointer_write.load_record(vault, first.pointer_write)
    assert pointer_write.reverted_text(record, path.read_text(encoding="utf-8")) == before

    _correct(vault, SOURCE, "tide-survey")
    later = path.read_text(encoding="utf-8")
    with pytest.raises(pointer_write.PointerWriteError) as refused:
        pointer_write.reverted_text(record, later)

    assert refused.value.code == "POINTER_REVERT_STALE"


def test_a_comment_and_blank_line_after_a_sources_lineage_survive_a_relocation_and_a_correction(
    vault: Path,
) -> None:
    """A splice that ends a block list where the parser stops, so a relocation or a pointer
    correction drops the owner's comment and blank line after `moved_from`, or a relocation
    whose own lineage write leaves no record to read it back by."""
    from exomem import page_lineage
    from exomem import reclassify_source as rc

    earlier = "Knowledge Base/Sources/Articles/2026-05-01-egcg-notes.md"
    kept = "# filed by hand from the old capture folder\n\nproject: harbour-survey\n"
    path = vault / SOURCE
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "ingested_into: []\n",
            f"ingested_into: []\nexomem_id: {uuid.uuid4()}\nmoved_from:\n  - path: {earlier}\n{kept}",
        ),
        encoding="utf-8",
    )

    moved = rc.reclassify(
        vault, path=SOURCE, source_kind="research-report", reason="a written study", today=TODAY,
    ).new_path
    relocated = (vault / moved).read_text(encoding="utf-8")
    _correct(vault, moved, "coastal-survey")
    corrected = (vault / moved).read_text(encoding="utf-8")

    assert moved != SOURCE
    assert kept in relocated
    assert [entry["path"] for entry in _front(relocated)["moved_from"]] == [earlier, SOURCE]
    assert kept.replace("harbour-survey", "coastal-survey") in corrected
    operation = page_lineage.latest_operation(relocated)
    assert moved in pointer_write.operation_records(vault, operation)


def test_a_comment_after_a_list_item_and_a_blank_line_after_a_block_scalar_survive_a_correction(
    vault: Path,
) -> None:
    """A splice that ends a block list after its last item's inline comment, or a literal block
    scalar after the blank line that follows it, so a correction drops the owner's bytes."""
    path = vault / SOURCE
    before = path.read_text(encoding="utf-8").replace(
        "tags: [",
        "project: |\n  harbour-survey\n\nprojects:\n  - harbour-survey  # merged later\ntags: [",
    )
    path.write_text(before, encoding="utf-8")

    _correct(vault, SOURCE, "coastal-survey")
    set_frontmatter_field.set_pointer_field(
        vault, path=SOURCE, field="projects", value=["coastal-survey"], why="the survey merged",
        expected_hash=None, today=TODAY,
    )

    assert path.read_text(encoding="utf-8") == before.replace(
        "project: |\n  harbour-survey\n", "project: coastal-survey\n"
    ).replace("projects:\n  - harbour-survey", "projects: [coastal-survey]")


def test_a_pointer_write_that_drops_a_comment_inside_its_declared_field_refuses(
    vault: Path,
) -> None:
    """A proof that compares only the bytes outside a declared field, so a rewrite of that
    field silently drops a comment the owner wrote inside it."""
    path = vault / SOURCE
    before = path.read_text(encoding="utf-8").replace(
        "ingested_into: []\n",
        "ingested_into: []\nprojects:\n  # filed by hand\n  - harbour-survey\n",
    )
    path.write_text(before, encoding="utf-8")
    after = before.replace(
        "projects:\n  # filed by hand\n  - harbour-survey\n", "projects: [coastal-survey]\n"
    )

    with pytest.raises(pointer_write.PointerWriteError) as refused:
        pointer_write.plan(
            vault, SOURCE, before, after, pointer_write.Declared(fields={"projects"}),
            reason="crafted",
        )

    assert refused.value.code == "APPEND_ONLY"
    assert "comment" in refused.value.reason


def _front(text: str) -> dict:
    from exomem.vault import parse_frontmatter

    return parse_frontmatter(text)[0]


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda text: text.replace(f"  - path: {NOTE}\n", ""), id="removal"),
        pytest.param(
            lambda text: text.replace("reclassified_from: Sources/Articles", "reclassified_from: Sources/Reports"),
            id="reclassified-from-rewrite",
        ),
        pytest.param(
            lambda text: text.replace(f"  - path: {NOTE}\n", f"  - path: {OTHER_NOTE}\n"),
            id="earlier-entry-rewrite",
        ),
    ],
)
def test_a_lineage_proof_admits_only_one_appended_moved_from_entry(vault: Path, change) -> None:
    """A lineage declaration that lets a caller drop or rewrite an earlier entry, or rewrite
    `reclassified_from`, so a page takes or loses the links of a path it never left."""
    path = vault / SOURCE
    before = path.read_text(encoding="utf-8").replace(
        "ingested_into: []\n",
        f"ingested_into: []\nreclassified_from: Sources/Articles\nmoved_from:\n  - path: {NOTE}\n",
    )
    path.write_text(before, encoding="utf-8")

    with pytest.raises(pointer_write.PointerWriteError) as refused:
        pointer_write.plan(
            vault, SOURCE, before, change(before), pointer_write.Declared(lineage=True),
            reason="crafted",
        )

    assert refused.value.code == "APPEND_ONLY"


def test_a_pointer_write_on_a_frontmatter_with_a_yaml_alias_refuses(vault: Path) -> None:
    """An admitted pointer span that an alias copies into a field the write does not declare."""
    path = vault / SOURCE
    before = path.read_text(encoding="utf-8").replace(
        "ingested_into: []\n", "ingested_into: []\nproject: &home harbour-survey\nlabel: *home\n"
    )
    path.write_text(before, encoding="utf-8")

    with pytest.raises(pointer_write.PointerWriteError) as refused:
        pointer_write.plan(
            vault, SOURCE, before, before.replace("&home harbour-survey", "&home coastal-survey"),
            pointer_write.Declared(fields={"project"}), reason="crafted",
        )

    assert refused.value.code == "APPEND_ONLY"
    assert "alias" in refused.value.reason


def test_a_repointed_link_inside_an_origin_carrier_refuses(vault: Path) -> None:
    """A link rewrite that changes recorded origin data, which the move's own rewrite keeps."""
    _with_identity(vault, NOTE)
    path = vault / SOURCE
    before = path.read_text(encoding="utf-8") + (
        f'\n<!-- exomem-origin:v1 {{"reason":"Filed beside [[{NOTE[:-3]}]]."}} -->\n'
    )
    path.write_text(before, encoding="utf-8")

    with pytest.raises(pointer_write.PointerWriteError) as refused:
        pointer_write.plan(
            vault, SOURCE, before, before.replace(f"[[{NOTE[:-3]}]]", f"[[{MOVED_NOTE[:-3]}]]"),
            pointer_write.Declared(moves={NOTE: MOVED_NOTE}), reason="the note moved",
        )

    assert refused.value.code == "APPEND_ONLY"
    assert "origin carrier" in refused.value.reason


def test_a_stop_between_the_commit_and_the_record_keeps_the_record(vault: Path) -> None:
    """A stop after the page changed that loses the record its revert needs, or a stop before
    it that leaves a record of a write the page never received."""
    path = vault / SOURCE
    before = path.read_text(encoding="utf-8")
    after = before.replace("---\n", "---\nproject: coastal-survey\n", 1)
    write = pointer_write.PointerWrite(SOURCE, "file it", before, after, ())
    committed, never = pointer_write.new_record_id(), pointer_write.new_record_id()

    # The block does not exit while the checks run, as when the process stops inside it. The
    # generator is closed at the end, so its cleanup runs here and not at a later collection.
    stopped = pointer_write.recording(vault, [(write, committed), (write, never)])
    stopped.__enter__()
    try:
        path.write_text(after, encoding="utf-8")

        assert pointer_write.load_record(vault, committed)["after_hash"] == write.after_hash
        path.write_text(before, encoding="utf-8")
        with pytest.raises(pointer_write.PointerWriteError) as unknown:
            pointer_write.load_record(vault, never)
        assert unknown.value.code == "POINTER_RECORD_UNKNOWN"
    finally:
        stopped.gen.close()
