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


def _choose_slug_in_the_vault(vault_root: Path) -> None:
    keys = vault_module.kb_root(vault_root) / "_Schema" / "project-keys.yaml"
    keys.parent.mkdir(parents=True, exist_ok=True)
    keys.write_text("filename_style: slug\n", encoding="utf-8")


def test_a_new_note_is_named_by_its_title_and_the_title_is_unchanged(vault: Path) -> None:
    result = _insight(vault, "Jäne Exämple")

    assert Path(result.path).name == "Jäne Exämple.md"
    assert _stored_title(vault, result.path) == "Jäne Exämple"
    assert not [warning for warning in result.warnings if warning.startswith("FILENAME_ADJUSTED")]


@pytest.mark.parametrize(
    ("title", "stem", "correction"),
    [
        ("Plan #3 [draft]", "Plan 3 draft", "link characters"),
        ("..hidden plan", "hidden plan", "leading dots"),
    ],
)
def test_a_title_that_would_break_a_link_gets_a_link_safe_name_and_a_warning(
    vault: Path, title: str, stem: str, correction: str
) -> None:
    result = _insight(vault, title)

    assert Path(result.path).name == f"{stem}.md"
    assert _stored_title(vault, result.path) == title
    adjusted = [warning for warning in result.warnings if warning.startswith("FILENAME_ADJUSTED")]
    assert len(adjusted) == 1 and correction in adjusted[0] and f"'{stem}'" in adjusted[0]
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
    adjusted = [warning for warning in result.warnings if warning.startswith("FILENAME_ADJUSTED")]
    assert len(adjusted) == 1 and "name shortened" in adjusted[0]


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


def test_an_experiment_keeps_its_dated_slug_name_unless_the_vault_chose_a_style(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    by_default = _experiment(vault, "Retry Budget Trial")
    monkeypatch.setenv("EXOMEM_FILENAME_STYLE", "title")
    by_choice = _experiment(vault, "Retry Budget Trial Two")

    assert Path(by_default.path).name == "2026-05-retry-budget-trial.md"
    assert Path(by_choice.path).name == "2026-05-Retry Budget Trial Two.md"


def test_titles_that_differ_only_by_case_get_separate_files(vault: Path) -> None:
    first = _insight(vault, "Budget Review")
    first_bytes = (vault / first.path).read_bytes()

    second = _insight(vault, "budget review")

    assert Path(second.path).name == "budget review-2.md"
    assert (vault / first.path).read_bytes() == first_bytes


def test_a_vault_that_chooses_slug_is_named_the_old_way(vault: Path) -> None:
    _choose_slug_in_the_vault(vault)

    result = _insight(vault, "Jäne Exämple")

    assert Path(result.path).name == "jane-example.md"
    assert _stored_title(vault, result.path) == "Jäne Exämple"


def test_a_source_keeps_its_dated_slug_name_by_default(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    result = add_module.add(
        vault,
        source_schema,
        content="An invented article body.",
        source_type="article",
        title="Jäne Exämple notes",
        url="https://example.com/jane",
        today=TODAY,
    )

    assert Path(result.path).name == "2026-05-18-jane-example-notes.md"
    assert _stored_title(vault, result.path) == "Jäne Exämple notes"


def test_a_source_follows_a_style_the_vault_chose(
    vault: Path, source_schema: schema_module.SourceSchema, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_FILENAME_STYLE", "title")

    result = add_module.add(
        vault,
        source_schema,
        content="An invented article body.",
        source_type="article",
        title="Jäne Exämple notes",
        url="https://example.com/jane",
        today=TODAY,
    )

    assert Path(result.path).name == "2026-05-18-Jäne Exämple notes.md"


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
