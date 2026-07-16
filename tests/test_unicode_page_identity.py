from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import yaml

from exomem import add as add_module
from exomem import commands, find_corpus
from exomem import link as link_module
from exomem import note as note_module
from exomem import schema as schema_module
from exomem import vault as vault_module

TODAY = dt.date(2026, 7, 12)


def _page(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    head, body = text.removeprefix("---\n").split("\n---\n", 1)
    return yaml.safe_load(head), body.lstrip("\n")


def test_note_preserves_unicode_title_with_explicit_ascii_slug(vault: Path) -> None:
    result = note_module.note(
        vault,
        content="## 要約\n\n十分な睡眠を取る。\n",
        note_type="insight",
        title="睡眠: 基本 #1",
        slug="sleep-basics",
        status="draft",
        today=TODAY,
    )

    assert result.path.endswith("/sleep-basics.md")
    frontmatter, body = _page(vault / result.path)
    assert frontmatter["title"] == "睡眠: 基本 #1"
    assert body.startswith("# 睡眠: 基本 #1\n")


def test_note_does_not_duplicate_matching_caller_h1(vault: Path) -> None:
    result = note_module.note(
        vault,
        content="# 睡眠\n\n## 要約\n\n本文。\n",
        note_type="insight",
        title="睡眠",
        slug="sleep",
        status="draft",
        today=TODAY,
    )
    _, body = _page(vault / result.path)
    assert body.count("# 睡眠\n") == 1
    assert body.startswith("# 睡眠\n")


@pytest.mark.parametrize(
    "slug",
    ["Sleep", "sleep notes", "睡眠", "../sleep", "sleep/notes", "-sleep", "sleep-", "a" * 101],
)
def test_explicit_slug_rejects_non_ascii_kebab_case(vault: Path, slug: str) -> None:
    with pytest.raises(note_module.NoteError) as exc:
        note_module.note(
            vault,
            content="本文。",
            note_type="insight",
            title="睡眠",
            slug=slug,
            today=TODAY,
        )
    assert exc.value.code == "INVALID_SLUG"


def test_automatic_non_ascii_slug_warns_but_preserves_title(vault: Path) -> None:
    result = note_module.note(
        vault,
        content="本文。",
        note_type="insight",
        title="睡眠",
        status="draft",
        today=TODAY,
    )
    frontmatter, body = _page(vault / result.path)
    assert frontmatter["title"] == "睡眠"
    assert body.startswith("# 睡眠\n")
    assert any("lossy" in warning.lower() and "slug" in warning.lower() for warning in result.warnings)


def test_source_and_entity_store_unicode_title(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    source = add_module.add(
        vault,
        source_schema,
        content="本文。",
        source_type="session",
        title="会話: 睡眠",
        slug="sleep-conversation",
        today=TODAY,
    )
    source_fm, source_body = _page(vault / source.path)
    assert source_fm["title"] == "会話: 睡眠"
    assert source_body.startswith("# 会話: 睡眠\n")

    entity = link_module.link(
        vault,
        entity_type="concept",
        name="自閉症",
        slug="autism",
        summary="神経発達上の概念。",
        today=TODAY,
    )
    entity_fm, entity_body = _page(vault / entity.path)
    assert entity.path.endswith("/autism.md")
    assert entity_fm["title"] == "自閉症"
    assert entity_body.startswith("# 自閉症\n")


def test_title_resolution_is_frontmatter_then_h1_then_humanized_stem(tmp_path: Path) -> None:
    titled = tmp_path / "shui-mian.md"
    titled.write_text("---\ntitle: 睡眠\n---\n\n# Legacy\n", encoding="utf-8")
    h1_only = tmp_path / "legacy-slug.md"
    h1_only.write_text("# 読める見出し\n", encoding="utf-8")
    page = find_corpus.parse_page(titled, titled.stat().st_mtime, tmp_path)
    assert page is not None and page.title == "睡眠"
    page = find_corpus.parse_page(h1_only, h1_only.stat().st_mtime, tmp_path)
    assert page is not None and page.title == "読める見出し"
    assert vault_module.resolve_display_title({}, "", Path("legacy-slug.md")) == "legacy slug"


@pytest.mark.parametrize("value", ["睡眠: 基本 #1", "true", "null", "2026-01-01", "line1\nline2"])
def test_yaml_scalar_round_trips_string_identity(value: str) -> None:
    assert yaml.safe_load(f"title: {vault_module.yaml_scalar(value)}")["title"] == value


def test_search_and_fetch_return_same_structured_title(vault: Path) -> None:
    result = note_module.note(
        vault,
        content="# Legacy heading\n\nUnique multilingual marker 7799.\n",
        note_type="insight",
        title="睡眠",
        slug="sleep",
        status="draft",
        today=TODAY,
    )
    search = commands.op_search(vault, query="multilingual marker 7799", scope="kb")
    hit = next(item for item in search["results"] if item["id"] == result.path)
    fetched = commands.op_fetch(vault, id=result.path)
    assert hit["title"] == "睡眠"
    assert fetched["title"] == "睡眠"


def test_renamed_product_commands_forward_explicit_slugs(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    remembered = commands.op_remember(
        vault,
        content=(
            "本文。\n\n## Relations\n"
            "- relates_to [[Knowledge Base/Notes/Insights/"
            "progressive-disclosure-without-mode-fragmentation]]\n"
        ),
        title="睡眠",
        slug="sleep-memory",
    )
    assert remembered["path"].endswith("/sleep-memory.md")

    captured = commands.op_capture_source(
        vault,
        source_schema,
        content="会話。",
        title="睡眠の会話",
        slug="sleep-source",
    )
    assert captured["source"]["path"].endswith("-sleep-source.md")

    entity = commands.op_connect_memory(
        vault,
        operation="create-entity",
        entity_type="concept",
        name="自閉症",
        slug="autism-concept",
        summary="神経発達上の概念。",
    )
    assert entity["path"].endswith("/autism-concept.md")


def test_slug_is_registered_on_legacy_and_product_surfaces() -> None:
    product = {command.name: command for command in commands.PRODUCT_COMMANDS}
    legacy = {command.name: command for command in commands.COMMANDS}
    for name in ("remember", "replace_memory", "capture_source", "connect_memory"):
        assert "slug" in {param.name for param in product[name].params}
    for name in ("note", "replace", "add", "link"):
        assert "slug" in {param.name for param in legacy[name].params}
