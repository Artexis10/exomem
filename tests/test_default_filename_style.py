"""A new compiled note or entity is named by its title unless the vault opts out with `slug`.

Each case drives the real writers over a temporary vault that sets no filename style, with invented
names. A captured Source keeps its dated slug name. The doctor reports the style and its source.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import yaml

from exomem import add as add_module
from exomem import doctor as doctor_module
from exomem import find as find_module
from exomem import freshness
from exomem import note as note_module
from exomem import schema as schema_module
from exomem import vault as vault_module

TODAY = dt.date(2026, 5, 18)


@pytest.fixture(autouse=True)
def _no_style_in_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped default is under test, so no style may reach the vault from outside it."""
    monkeypatch.delenv("EXOMEM_FILENAME_STYLE", raising=False)


def _insight(vault_root: Path, title: str) -> note_module.NoteResult:
    return note_module.note(
        vault_root,
        content="## Claim\n\nAn invented claim for naming checks.\n",
        note_type="insight",
        title=title,
        status="draft",
        today=TODAY,
    )


def _stored_title(vault_root: Path, result_path: str) -> str:
    text = (vault_root / result_path).read_text(encoding="utf-8")
    return yaml.safe_load(text.split("\n---\n")[0].removeprefix("---\n"))["title"]


def _hold_the_title_index(vault_root: Path) -> None:
    """Hold the shared resolver in memory and current, as the running server's watcher keeps it."""
    freshness.seed(
        vault_root, "vault",
        [(str(page), freshness.stat_signature(page)) for page in vault_module.walk_vault_md(vault_root)],
    )
    find_module.shared_resolver(vault_root)


def _choose_slug_in_the_vault(vault_root: Path) -> None:
    keys = vault_module.kb_root(vault_root) / "_Schema" / "project-keys.yaml"
    keys.parent.mkdir(parents=True, exist_ok=True)
    keys.write_text("filename_style: slug\n", encoding="utf-8")


def test_a_new_note_is_named_by_its_title_and_the_title_is_unchanged(vault: Path) -> None:
    result = _insight(vault, "Brûlerie Ötzberg")

    assert Path(result.path).name == "Brûlerie Ötzberg.md"
    assert _stored_title(vault, result.path) == "Brûlerie Ötzberg"
    assert not [warning for warning in result.warnings if warning.startswith("FILENAME_ADJUSTED")]


def _adjusted(result: note_module.NoteResult) -> list[str]:
    return [warning for warning in result.warnings if warning.startswith("FILENAME_ADJUSTED")]


@pytest.mark.parametrize(
    ("title", "stem"),
    [
        ("Plan #3 [draft]", "Plan 3 draft"),
        ("..hidden plan", "hidden plan"),
        # The file-name sanitiser, not the link rule, removes these two.
        ("Plan | B", "Plan B"),
        ("Plan / B", "Plan B"),
    ],
)
def test_a_title_that_would_break_a_link_gets_a_link_safe_name_and_a_warning(
    vault: Path, title: str, stem: str
) -> None:
    result = _insight(vault, title)

    assert Path(result.path).name == f"{stem}.md"
    assert _stored_title(vault, result.path) == title
    adjusted = _adjusted(result)
    assert len(adjusted) == 1 and "cannot hold" in adjusted[0] and f"'{stem}'" in adjusted[0]
    # The wikilink an agent writes from the warning resolves to the page.
    resolved, unresolved = vault_module.normalize_wikilink(stem, vault, strict=True)
    assert unresolved is None and resolved.endswith(f"/{stem}")


def test_a_very_long_title_is_cut_under_the_byte_cap_with_a_warning(vault: Path) -> None:
    # Three bytes per character: the 100-character cap alone would leave a name over the byte cap.
    title = " ".join(["あいうえお"] * 40)

    result = _insight(vault, title)

    name = Path(result.path).name
    assert len(name.encode()) <= vault_module.MAX_FILENAME_BYTES
    assert _stored_title(vault, result.path) == title
    adjusted = _adjusted(result)
    assert len(adjusted) == 1 and "shortened" in adjusted[0] and "cannot hold" not in adjusted[0]


def test_a_second_page_with_the_same_corrected_name_is_warned_about_by_its_own_name(vault: Path) -> None:
    first = _insight(vault, "Plan #3 [draft]")

    second = _insight(vault, "Plan #3 [draft]")

    assert Path(first.path).name == "Plan 3 draft.md" and Path(second.path).name == "Plan 3 draft-2.md"
    (first_warning,) = _adjusted(first)
    (second_warning,) = _adjusted(second)
    assert "'Plan 3 draft'" in first_warning and "a number was added" not in first_warning
    assert "'Plan 3 draft-2'" in second_warning and "a number was added" in second_warning
    # The name in the warning is the one a link resolves: not the first page's.
    resolved, unresolved = vault_module.normalize_wikilink("Plan 3 draft-2", vault, strict=True)
    assert unresolved is None and resolved.endswith("/Plan 3 draft-2")


def test_a_number_added_to_a_plain_title_is_reported(vault: Path) -> None:
    _insight(vault, "Budget Review")

    second = _insight(vault, "budget review")

    (warning,) = _adjusted(second)
    assert "'budget review-2'" in warning and "a number was added" in warning


def test_a_number_added_to_a_long_name_keeps_the_file_under_the_byte_cap(vault: Path) -> None:
    # 99 two-byte characters: the name fits, but the name and `-2` together would not.
    title = "é" * 99

    first = _insight(vault, title)
    second = _insight(vault, title)
    third = _insight(vault, title)

    names = [Path(result.path).name for result in (first, second, third)]
    assert all(len(name.encode()) <= vault_module.MAX_FILENAME_BYTES for name in names), names
    assert names[1].endswith("-2.md") and names[2].endswith("-3.md")
    assert len(set(names)) == 3
    assert "shortened" in _adjusted(second)[0] and "a number was added" in _adjusted(second)[0]


def test_a_number_added_after_a_date_prefix_keeps_the_file_under_the_byte_cap(vault: Path) -> None:
    title = "é" * 99

    first = _experiment(vault, title)
    second = _experiment(vault, title)

    names = [Path(result.path).name for result in (first, second)]
    assert all(name.startswith("2026-05-") for name in names)
    assert all(len(name.encode()) <= vault_module.MAX_FILENAME_BYTES for name in names), names
    assert names[1].endswith("-2.md") and names[0] != names[1]


def _experiment(vault_root: Path, title: str) -> note_module.NoteResult:
    return note_module.note(
        vault_root,
        content="## Finding\n\nAn invented finding.\n",
        note_type="experiment",
        title=title,
        domain="health",
        started="2026-05-01",
        duration="30 days",
        status="draft",
        today=TODAY,
    )


def test_an_experiment_is_named_by_its_title_after_its_date_prefix(vault: Path) -> None:
    result = _experiment(vault, "Brûlée Soak Trial")

    assert Path(result.path).name == "2026-05-Brûlée Soak Trial.md"
    assert _stored_title(vault, result.path) == "Brûlée Soak Trial"


def test_a_production_log_is_named_by_its_title_after_the_month_it_is_written(vault: Path) -> None:
    draft = note_module.note(
        vault,
        content="## Observations\n\n- [finding] An invented outcome.\n",
        note_type="production-log",
        title="Launch Recap Ünit",
        medium="posts",
        validate_only=True,
        today=TODAY,
    )

    assert Path(draft.destination).name == "2026-05-Launch Recap Ünit.md"


def test_a_long_dated_title_is_cut_so_the_date_prefix_fits_under_the_byte_cap(vault: Path) -> None:
    # The name alone fits the cap; only the prefix beside it pushes the file over.
    title = "あ" * 63 + "xx"

    result = _experiment(vault, title)

    name = Path(result.path).name
    assert name.startswith("2026-05-") and len(name.encode()) <= vault_module.MAX_FILENAME_BYTES
    assert _stored_title(vault, result.path) == title
    adjusted = _adjusted(result)
    assert len(adjusted) == 1 and "shortened" in adjusted[0] and "'2026-05-" in adjusted[0]


def test_an_experiment_in_a_vault_that_chooses_slug_keeps_its_dated_slug_name(vault: Path) -> None:
    _choose_slug_in_the_vault(vault)

    result = _experiment(vault, "Brûlée Soak Trial")

    assert Path(result.path).name == "2026-05-brulee-soak-trial.md"


def test_titles_that_differ_only_by_case_get_separate_files(vault: Path) -> None:
    first = _insight(vault, "Budget Review")
    first_bytes = (vault / first.path).read_bytes()

    second = _insight(vault, "budget review")

    assert Path(second.path).name == "budget review-2.md"
    assert (vault / first.path).read_bytes() == first_bytes


def test_a_vault_that_chooses_slug_is_named_the_old_way(vault: Path) -> None:
    _choose_slug_in_the_vault(vault)

    result = _insight(vault, "Brûlerie Ötzberg")

    assert Path(result.path).name == "brulerie-otzberg.md"
    assert _stored_title(vault, result.path) == "Brûlerie Ötzberg"


def test_a_source_keeps_its_dated_slug_name_by_default(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    result = add_module.add(
        vault,
        source_schema,
        content="An invented article body.",
        source_type="article",
        title="Brûlerie Ötzberg notes",
        url="https://example.com/roastery",
        today=TODAY,
    )

    assert Path(result.path).name == "2026-05-18-brulerie-otzberg-notes.md"
    assert _stored_title(vault, result.path) == "Brûlerie Ötzberg notes"


def test_a_source_follows_a_style_the_vault_chose(
    vault: Path, source_schema: schema_module.SourceSchema, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_FILENAME_STYLE", "title")

    result = add_module.add(
        vault,
        source_schema,
        content="An invented article body.",
        source_type="article",
        title="Brûlerie Ötzberg notes",
        url="https://example.com/roastery",
        today=TODAY,
    )

    assert Path(result.path).name == "2026-05-18-Brûlerie Ötzberg notes.md"


def test_a_source_named_by_its_title_reports_a_name_that_differs_from_it(
    vault: Path, source_schema: schema_module.SourceSchema, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_FILENAME_STYLE", "title")

    result = add_module.add(
        vault,
        source_schema,
        content="An invented article body.",
        source_type="article",
        title="Brûlerie | Ötzberg notes",
        url="https://example.com/roastery",
        today=TODAY,
    )

    assert Path(result.path).name == "2026-05-18-Brûlerie Ötzberg notes.md"
    (adjusted,) = [warning for warning in result.warnings if warning.startswith("FILENAME_ADJUSTED")]
    assert "'2026-05-18-Brûlerie Ötzberg notes'" in adjusted


def _style_check(vault_root: Path) -> doctor_module.DoctorCheck:
    report = doctor_module.doctor(vault=str(vault_root))
    return next(check for check in report.checks if check.id == "vault.filename_style")


def test_doctor_reports_the_style_and_where_it_came_from(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default = _style_check(vault)
    assert (default.status, default.details) == ("pass", {"style": "title", "source": "default"})

    _choose_slug_in_the_vault(vault)
    from_vault = _style_check(vault)
    assert from_vault.details == {"style": "slug", "source": "project-keys.yaml"}

    monkeypatch.setenv("EXOMEM_FILENAME_STYLE", "title")
    from_environment = _style_check(vault)
    assert from_environment.details == {"style": "title", "source": "environment"}


def test_doctor_fails_an_unrecognised_style_instead_of_raising(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_FILENAME_STYLE", "kebab")

    check = _style_check(vault)

    assert check.status == "fail"
    assert "environment" in check.message and "slug, title" in (check.remediation or "")


def test_a_new_page_does_not_take_over_a_bare_link_to_an_older_page_with_its_title(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An older page: slug-named, so a bare link to its title finds it only by the title.
    monkeypatch.setenv("EXOMEM_FILENAME_STYLE", "slug")
    older = _insight(vault, "BRÛLERIE ÖTZBERG")
    monkeypatch.delenv("EXOMEM_FILENAME_STYLE")
    assert Path(older.path).name == "brulerie-otzberg.md"
    before, warning = vault_module.normalize_wikilink("BRÛLERIE ÖTZBERG", vault)
    assert warning is None and before.endswith("/brulerie-otzberg")
    _hold_the_title_index(vault)

    new = _insight(vault, "Brûlerie Ötzberg")

    # A stem match beats a title match, so the plain name would have moved the link to this page.
    assert Path(new.path).name == "Brûlerie Ötzberg-2.md"
    (adjusted,) = _adjusted(new)
    assert "'Brûlerie Ötzberg-2'" in adjusted and "a number was added" in adjusted
    _after, ambiguity = vault_module.normalize_wikilink("BRÛLERIE ÖTZBERG", vault)
    assert ambiguity is not None and "matches 2 files by title" in ambiguity


def test_a_write_without_a_resident_title_index_builds_none_and_names_the_page_by_its_title(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_FILENAME_STYLE", "slug")
    _insight(vault, "Brûlerie Ötzberg")
    monkeypatch.delenv("EXOMEM_FILENAME_STYLE")

    def scanned(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a link-free write scanned the vault for titles")

    monkeypatch.setattr(vault_module.WikilinkResolver, "_build", scanned)

    new = _insight(vault, "Brûlerie Ötzberg")

    assert Path(new.path).name == "Brûlerie Ötzberg.md"


def _write_project_keys(vault_root: Path, text: str) -> Path:
    keys = vault_module.kb_root(vault_root) / "_Schema" / "project-keys.yaml"
    keys.parent.mkdir(parents=True, exist_ok=True)
    keys.write_text(text, encoding="utf-8")
    return keys


@pytest.mark.parametrize(
    "text",
    [
        "projects: {}\n",
        "# nothing chosen yet\n",
        "",
    ],
    ids=["no-key", "comments-only", "empty-file"],
)
def test_a_file_that_sets_no_style_takes_the_default(vault: Path, text: str) -> None:
    _write_project_keys(vault, text)

    assert vault_module.filename_style_source(vault) == ("title", "default")


def test_a_missing_project_keys_file_takes_the_default(vault: Path) -> None:
    keys = vault_module.kb_root(vault) / "_Schema" / "project-keys.yaml"
    keys.unlink(missing_ok=True)

    assert vault_module.filename_style_source(vault) == ("title", "default")


@pytest.mark.parametrize(
    ("text", "named"),
    [
        ("filename_style: [slug]\n", "['slug']"),
        ("filename_style: 7\n", "7"),
        ("filename_style:\n", "None"),
        ("filename_style: ''\n", "''"),
        ("filename_style: slug: [\n", "cannot be read"),
        ("- filename_style\n- slug\n", "not a mapping"),
    ],
    ids=["list", "number", "empty-value", "empty-string", "unparseable", "not-a-mapping"],
)
def test_a_style_key_that_cannot_be_read_refuses_instead_of_taking_the_default(
    vault: Path, text: str, named: str
) -> None:
    _write_project_keys(vault, text)

    with pytest.raises(vault_module.InvalidSlugError) as refused:
        vault_module.filename_style_source(vault)
    assert "project-keys.yaml" in str(refused.value) and named in str(refused.value)
    with pytest.raises(note_module.NoteError) as write:
        _insight(vault, "Brûlerie Ötzberg")
    assert write.value.code == "INVALID_SLUG"
    check = _style_check(vault)
    assert check.status == "fail" and "project-keys.yaml" in check.message


def test_a_project_keys_file_that_cannot_be_opened_refuses_instead_of_taking_the_default(vault: Path) -> None:
    keys = vault_module.kb_root(vault) / "_Schema" / "project-keys.yaml"
    keys.unlink(missing_ok=True)
    keys.mkdir(parents=True)

    with pytest.raises(vault_module.InvalidSlugError, match="cannot be read"):
        vault_module.filename_style_source(vault)


def test_the_environment_style_applies_without_reading_a_malformed_project_keys_file(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_project_keys(vault, "filename_style: [slug]\n")
    monkeypatch.setenv("EXOMEM_FILENAME_STYLE", "slug")

    assert vault_module.filename_style_source(vault) == ("slug", "environment")
