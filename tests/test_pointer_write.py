"""An append-only page changes only in the pointer spans a call declares.

OpenSpec add-curation-relocation-protocol, group 2. The vault is the canonical fixture in file
mode. Each test names the failure it catches.
"""

from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path

import pytest

from exomem import page_fields, pointer_write, set_frontmatter_field
from exomem.vocabulary import registry

TODAY = dt.date(2026, 11, 16)
SOURCE = "Knowledge Base/Sources/Articles/2026-05-04-best-egcg-supplements.md"
NOTE = "Knowledge Base/Notes/Insights/autovacuum-thresholds-prevent-table-bloat.md"
MOVED_NOTE = "Knowledge Base/Notes/Insights/Postgres/autovacuum-thresholds-prevent-table-bloat.md"
OTHER_NOTE = "Knowledge Base/Notes/Insights/vacuum-freeze-ages.md"


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


def test_an_overlay_that_retires_a_shipped_pointer_reports_it_and_the_field_stays_a_pointer(
    vault: Path,
) -> None:
    """An owner overlay that silently takes `project` out of the pointer write, so a Source's
    merged project can never be corrected."""
    page_fields.registry_path(vault).write_text(
        "schema_version: 1\nentries:\n  project:\n    status: deprecated\n"
        "    attributes: {field: project, role: pointer}\n",
        encoding="utf-8",
    )
    registry.invalidate(vault)

    snapshot = registry.load(page_fields.SPEC, vault)

    assert [finding["code"] for finding in snapshot.findings] == ["invalid_registry_overlay"]
    assert "PACK_ENTRY_FIXED" in snapshot.findings[0]["detail"]
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
        pytest.param(lambda text: text.replace("300–400 mg/day", "300–500 mg/day"),
                     id="body-text"),
    ],
)
def test_a_crafted_change_outside_a_declared_span_refuses(vault: Path, change) -> None:
    """A pointer write that lets a caller repoint a link to another page, edit a field that is
    not a declared pointer, or change captured body text."""
    _with_identity(vault, NOTE)
    _with_identity(vault, OTHER_NOTE, title="Vacuum freeze ages")
    before = _source_linking(vault, NOTE)

    with pytest.raises(pointer_write.PointerWriteError) as refused:
        pointer_write.plan(
            vault, SOURCE, before, change(before),
            pointer_write.Declared(fields={"project"}, moves={NOTE: MOVED_NOTE}),
            reason="crafted",
        )

    assert refused.value.code == "APPEND_ONLY"
    assert (vault / SOURCE).read_text(encoding="utf-8") == before


def test_a_project_correction_changes_only_its_span_and_a_title_patch_still_refuses(
    vault: Path,
) -> None:
    """A correction that bumps `updated` or touches other bytes of a Source, or a narrowed
    refusal that lets any field of an append-only page change."""
    path = vault / SOURCE
    before = path.read_text(encoding="utf-8").replace(
        "ingested_into: []\n", "ingested_into: []\nproject: harbour-survey\n"
    )
    path.write_text(before, encoding="utf-8")

    preview = set_frontmatter_field.set_frontmatter_field(
        vault, path=SOURCE, field="project", value="coastal-survey",
        why="the harbour survey merged into the coastal survey", today=TODAY, validate_only=True,
    )
    result = set_frontmatter_field.set_frontmatter_field(
        vault, path=SOURCE, field="project", value="coastal-survey",
        why="the harbour survey merged into the coastal survey", today=TODAY,
    )
    with pytest.raises(set_frontmatter_field.SetFrontmatterError) as refused:
        set_frontmatter_field.set_frontmatter_field(
            vault, path=SOURCE, field="title", value="Renamed", why="tidy", today=TODAY,
        )

    assert (preview.old_value, preview.new_value) == ("harbour-survey", "coastal-survey")
    assert path.read_text(encoding="utf-8") == before.replace(
        "project: harbour-survey", "project: coastal-survey"
    )
    assert result.pointer_write is not None
    assert refused.value.code == "APPEND_ONLY"


def test_a_revert_restores_the_exact_before_bytes_and_refuses_after_later_work(
    vault: Path,
) -> None:
    """A revert that rebuilds bytes other than those the write found, or that overwrites work
    done on the page after the write."""
    path = vault / SOURCE
    before = path.read_text(encoding="utf-8")
    first = set_frontmatter_field.set_frontmatter_field(
        vault, path=SOURCE, field="project", value="coastal-survey", why="file it", today=TODAY,
    )

    pointer_write.revert(vault, first.pointer_write)
    assert path.read_text(encoding="utf-8") == before

    second = set_frontmatter_field.set_frontmatter_field(
        vault, path=SOURCE, field="project", value="coastal-survey", why="file it", today=TODAY,
    )
    set_frontmatter_field.set_frontmatter_field(
        vault, path=SOURCE, field="project", value="tide-survey", why="refile it", today=TODAY,
    )
    later = path.read_text(encoding="utf-8")
    with pytest.raises(pointer_write.PointerWriteError) as refused:
        pointer_write.revert(vault, second.pointer_write)

    assert refused.value.code == "POINTER_REVERT_STALE"
    assert path.read_text(encoding="utf-8") == later
