"""The `episode` Source kind: where a conversation recap lives, and what it carries.

A recap is agent-authored raw material about a conversation. It is a Source,
so it is append-only like every other Source: a later revision never rewrites
an earlier one's body. What changes is frontmatter only -- the earlier revision
is marked superseded in the same atomic batch that writes the new one, which is
the frontmatter mutation the Sources contract already permits.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import yaml

from exomem import add as add_module
from exomem import schema as schema_module
from exomem import source_taxonomy as st
from exomem.vault import ContentHashMismatchError, content_hash

TODAY = dt.datetime(2026, 5, 18, 9, 12, 33, tzinfo=dt.UTC)
KEY = "ep-" + "a1" * 16
DIGEST = "d" * 64


def _fields(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "summary": "Chose the brass Harbor Lamp; delivery still open.",
        "episode": KEY,
        "episode_digest": DIGEST,
    }
    fields.update(overrides)
    return fields


def _record(
    vault: Path,
    source_schema: schema_module.SourceSchema,
    *,
    slug: str = "harbor-lamp-purchase-epa1a1a1a1a1a1-20260518t091233000000-dddddddd",
    supersede: tuple[tuple[str, str], ...] = (),
    **fields: object,
) -> add_module.AddResult:
    return add_module.add(
        vault,
        source_schema,
        content="## Worked on\n\n- Compared two lamps for Project Alpha.",
        title="Harbor Lamp purchase",
        source_type="episode",
        slug=slug,
        today=TODAY,
        extra_frontmatter=_fields(**fields),
        supersede=supersede,
    )


def _frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    head, _, _ = text.removeprefix("---\n").partition("\n---\n")
    return yaml.safe_load(head)


def test_the_episode_kind_routes_to_its_own_sources_folder(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    result = _record(vault, source_schema)

    assert result.path == (
        "Knowledge Base/Sources/Episodes/2026-05-18-"
        "harbor-lamp-purchase-epa1a1a1a1a1a1-20260518t091233000000-dddddddd.md"
    )
    assert st.core_taxonomy().resolve_kind("episode").path_label == "Episodes"


def test_the_episode_fields_render_in_the_source_frontmatter(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    result = _record(vault, source_schema, client="claude-code")

    parsed = _frontmatter(vault / result.path)
    assert parsed["type"] == "source"
    assert parsed["source_type"] == "episode"
    assert parsed["summary"] == "Chose the brass Harbor Lamp; delivery still open."
    assert parsed["episode"] == KEY
    assert parsed["episode_digest"] == DIGEST
    assert parsed["client"] == "claude-code"
    assert parsed["ingested_into"] == []


def test_the_optional_episode_fields_are_omitted_when_absent(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    parsed = _frontmatter(vault / _record(vault, source_schema).path)

    assert "client" not in parsed
    assert "about" not in parsed


def test_the_episode_kind_is_refused_without_its_fields(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    """A plain capture filed as an episode would be a recap nobody bound."""
    with pytest.raises(add_module.AddError) as error:
        add_module.add(
            vault,
            source_schema,
            content="Pasted text.",
            title="Not a recap",
            source_type="episode",
            today=TODAY,
        )
    assert error.value.code == "EPISODE_KIND_RESERVED"
    assert not (vault / "Knowledge Base" / "Sources" / "Episodes").exists()


def test_episode_fields_are_refused_on_any_other_kind(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    with pytest.raises(add_module.AddError) as error:
        add_module.add(
            vault,
            source_schema,
            content="Pasted conversation.",
            title="A session",
            source_type="session",
            today=TODAY,
            extra_frontmatter=_fields(),
        )
    assert error.value.code == "INVALID_SOURCE"


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "active"},
        {"type": "note"},
        {"summary": "two\nlines"},
        {"episode": ["not", "a", "string"]},
        {"about": "exomem://memory/69b2b4d3-d4c3-4361-8714-91b8f1b1c0b1"},
        # The refs a recap concerns live in the recorder's own ledger, never on
        # the shared page every audience may read.
        {"about": ["exomem://memory/69b2b4d3-d4c3-4361-8714-91b8f1b1c0b1"]},
    ],
)
def test_the_episode_frontmatter_set_is_closed(
    vault: Path, source_schema: schema_module.SourceSchema, fields: dict
) -> None:
    with pytest.raises(add_module.AddError) as error:
        _record(vault, source_schema, **fields)
    assert error.value.code == "INVALID_SOURCE"


def test_a_required_episode_field_cannot_be_dropped(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    fields = _fields()
    del fields["episode_digest"]
    with pytest.raises(add_module.AddError) as error:
        add_module.add(
            vault,
            source_schema,
            content="## Worked on\n\n- Something.",
            title="Harbor Lamp purchase",
            source_type="episode",
            today=TODAY,
            extra_frontmatter=fields,
        )
    assert error.value.code == "INVALID_SOURCE"


def test_a_vault_cannot_move_the_episode_folder() -> None:
    """Recent-context detection is a pure path test, so the label is reserved."""
    taxonomy = st.taxonomy_from_data(
        {
            "source_kinds": {
                "episode": {"path_label": "Chats"},
                "chat-log": {"path_label": "Episodes"},
            }
        }
    )

    assert taxonomy.resolve_kind("episode").path_label == "Episodes"
    assert taxonomy.resolve_kind("chat-log").path_label != "Episodes"
    assert any("episode" in finding and "reserved" in finding for finding in taxonomy.findings)
    assert any("chat-log" in finding and "reserved" in finding for finding in taxonomy.findings)


def test_a_new_revision_marks_the_previous_one_superseded_in_the_same_batch(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    first = _record(vault, source_schema)
    before = (vault / first.path).read_text(encoding="utf-8")
    second = _record(
        vault,
        source_schema,
        slug="harbor-lamp-purchase-epa1a1a1a1a1a1-20260518t101233000000-eeeeeeee",
        supersede=((first.path, content_hash(before)),),
        episode_digest="e" * 64,
    )

    after = (vault / first.path).read_text(encoding="utf-8")
    old = _frontmatter(vault / first.path)
    assert old["status"] == "superseded"
    assert second.path.removesuffix(".md") in str(old["superseded_by"])
    # Body immutability is the property the Sources contract protects.
    assert after.partition("\n---\n")[2] == before.partition("\n---\n")[2]
    assert "status" not in _frontmatter(vault / second.path)


def test_supersede_guards_the_text_the_caller_read(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    """A revision that changed after the caller read it loses the whole batch:
    no new page, and the changed revision is left exactly as it now is."""
    first = _record(vault, source_schema)
    read = (vault / first.path).read_text(encoding="utf-8")
    changed = read.replace("summary:", "summary: (changed)", 1)
    (vault / first.path).write_text(changed, encoding="utf-8")

    with pytest.raises(ContentHashMismatchError):
        _record(
            vault,
            source_schema,
            slug="harbor-lamp-purchase-epa1a1a1a1a1a1-20260518t101233000000-eeeeeeee",
            supersede=((first.path, content_hash(read)),),
            episode_digest="e" * 64,
        )

    assert (vault / first.path).read_text(encoding="utf-8") == changed
    assert len(list((vault / first.path).parent.glob("*.md"))) == 1


def test_an_already_superseded_revision_is_not_marked_again(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    first = _record(vault, source_schema)
    second = _record(
        vault,
        source_schema,
        slug="harbor-lamp-purchase-epa1a1a1a1a1a1-20260518t101233000000-eeeeeeee",
        supersede=((first.path, content_hash((vault / first.path).read_text("utf-8"))),),
        episode_digest="e" * 64,
    )
    retired = (vault / first.path).read_text(encoding="utf-8")

    _record(
        vault,
        source_schema,
        slug="harbor-lamp-purchase-epa1a1a1a1a1a1-20260518t111233000000-ffffffff",
        supersede=(
            (first.path, content_hash(retired)),
            (second.path, content_hash((vault / second.path).read_text("utf-8"))),
        ),
        episode_digest="f" * 64,
    )

    assert (vault / first.path).read_text(encoding="utf-8") == retired
    assert _frontmatter(vault / second.path)["status"] == "superseded"


def test_supersede_is_refused_outside_the_episode_folder(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    session = add_module.add(
        vault,
        source_schema,
        content="Pasted conversation.",
        title="A session",
        source_type="session",
        today=TODAY,
    )
    before = (vault / session.path).read_text(encoding="utf-8")

    with pytest.raises(add_module.AddError) as error:
        _record(vault, source_schema, supersede=((session.path, content_hash(before)),))

    assert error.value.code == "INVALID_SOURCE"
    assert (vault / session.path).read_text(encoding="utf-8") == before
    assert not (vault / "Knowledge Base" / "Sources" / "Episodes").exists()


def test_existing_kinds_render_byte_identically() -> None:
    """The episode fields are a closed extension; every other kind is untouched."""
    article = add_module._render_source(
        title="Harbor Lamp purchase",
        source_type="article",
        date_iso="2026-05-18T09:12:33Z",
        url="https://example.com/lamp",
        tags=["lamp", "harbor"],
        why_captured="Useful for Project Alpha.",
        content="Body text.",
        exomem_id="0123456789abcdef0123456789abcdef",
        domain="equipment",
        projects=["project-alpha"],
    )
    session = add_module._render_source(
        title="Scope session",
        source_type="session",
        date_iso="2026-05-18",
        url=None,
        tags=[],
        why_captured=None,
        content="Pasted conversation.",
        exomem_id="fedcba9876543210fedcba9876543210",
    )

    assert article == (
        "---\ntype: source\nexomem_id: 0123456789abcdef0123456789abcdef\n"
        "title: Harbor Lamp purchase\nsource_type: article\ndomain: equipment\n"
        "projects: [project-alpha]\ncaptured: 2026-05-18T09:12:33Z\n"
        'url: "https://example.com/lamp"\ntags: [lamp, harbor]\ningested_into: []\n'
        "---\n\n# Harbor Lamp purchase\n\n> Useful for Project Alpha.\n\n"
        "## Capture\n\nBody text.\n"
    )
    assert session == (
        "---\ntype: source\nexomem_id: fedcba9876543210fedcba9876543210\n"
        "title: Scope session\nsource_type: session\ncaptured: 2026-05-18\n"
        "tags: []\ningested_into: []\n---\n\n# Scope session\n\n## Capture\n\n"
        "Pasted conversation.\n"
    )
