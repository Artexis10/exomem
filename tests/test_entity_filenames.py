"""Entity creation names the file after the entity, through the vault's one naming path."""

from __future__ import annotations

import datetime as dt
import unicodedata
from pathlib import Path

import pytest
import yaml

from exomem import link as link_module

TODAY = dt.date(2026, 5, 25)


def _create(vault: Path, name: str, **extra: object) -> link_module.LinkResult:
    return link_module.link(
        vault,
        entity_type="organization",
        name=name,
        summary="An invented entity for naming checks.",
        today=TODAY,
        **extra,
    )


def _frontmatter(page: Path) -> dict:
    return yaml.safe_load(page.read_text(encoding="utf-8").split("\n---\n")[0].removeprefix("---\n"))


@pytest.mark.parametrize(
    ("name", "stem"),
    [
        ("Brûlerie Ötzberg", "Brûlerie Ötzberg"),
        ("ZÖRK Quibblex MAX", "ZÖRK Quibblex MAX"),
        ("some-kebab-tool", "some-kebab-tool"),
    ],
)
def test_entity_filename_keeps_the_name_as_written(vault: Path, name: str, stem: str) -> None:
    result = _create(vault, name)

    assert Path(result.path).name == f"{stem}.md"
    assert (vault / result.path).is_file()
    assert _frontmatter(vault / result.path)["title"] == name


def test_entity_filename_drops_reserved_characters_and_keeps_the_title(vault: Path) -> None:
    name = "Draft: A/B plan? <v2>"

    result = _create(vault, name)

    assert Path(result.path).name == "Draft A B plan v2.md"
    assert _frontmatter(vault / result.path)["title"] == name
    (adjusted,) = [warning for warning in result.warnings if warning.startswith("FILENAME_ADJUSTED")]
    assert "'Draft A B plan v2'" in adjusted and "cannot hold" in adjusted


def test_entity_filename_is_nfc_while_the_title_stays_as_given(vault: Path) -> None:
    decomposed = unicodedata.normalize("NFD", "Brûlerie Ötzberg")

    result = _create(vault, decomposed)

    assert Path(result.path).name == unicodedata.normalize("NFC", "Brûlerie Ötzberg") + ".md"
    assert _frontmatter(vault / result.path)["title"] == decomposed
    assert not [warning for warning in result.warnings if warning.startswith("FILENAME_ADJUSTED")]


def test_entity_filename_clash_by_case_is_entity_exists(vault: Path) -> None:
    first = _create(vault, "Brûlerie Ötzberg")
    before = (vault / first.path).read_bytes()

    with pytest.raises(link_module.LinkError) as refused:
        _create(vault, "brûlerie ötzberg")

    assert refused.value.code == "ENTITY_EXISTS"
    assert (vault / first.path).read_bytes() == before


def test_entity_filename_clash_by_case_between_different_titles_is_entity_exists(vault: Path) -> None:
    """Two titles that differ, but whose filenames differ only by case, are one file on Windows."""
    first = _create(vault, "Orbit Lab")
    before = (vault / first.path).read_bytes()

    with pytest.raises(link_module.LinkError) as refused:
        _create(vault, "orbit lab?")

    assert refused.value.code == "ENTITY_EXISTS"
    assert (vault / first.path).read_bytes() == before
    siblings = [entry.name for entry in (vault / first.path).parent.iterdir()]
    assert [name for name in siblings if name.casefold() == "orbit lab.md"] == ["Orbit Lab.md"]


def test_explicit_slug_still_names_the_file(vault: Path) -> None:
    result = _create(vault, "Brûlerie Ötzberg", slug="brulerie-otzberg")

    assert Path(result.path).name == "brulerie-otzberg.md"
    assert _frontmatter(vault / result.path)["title"] == "Brûlerie Ötzberg"
