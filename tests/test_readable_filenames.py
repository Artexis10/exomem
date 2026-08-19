"""A vault may name its files the way a human reads them (#598).

Obsidian's quick switcher, file explorer, graph labels and hand-typed wikilinks
all show and target the FILENAME, not the frontmatter title. Deriving every name
by kebab-slugging the whole title produced 96-character strings on a real vault
where a second tool writing the same titles produced readable ones.

The constraint this is built around: exomem has no permalink -- `grep -rn
permalink src/` returns nothing -- so the filename IS the identity, which
`slugify_with_truncation_check` says outright when it truncates. That is why the
default does not move and why nothing on disk is ever renamed.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

import pytest

from exomem import project_keys, vault


# ------------------------------------------------------- the pure sanitizer


@pytest.mark.parametrize("character", list('<>:"/' + chr(92) + "|?*"))
def test_every_reserved_character_is_removed_on_every_platform(character: str) -> None:
    """The union of platform rules, not the running host's.

    A vault is synced between machines. A name containing ':' written on Linux
    cannot be checked out on Windows at all, so the failure lands on whoever
    opens the vault next rather than on whoever wrote it.
    """
    result = vault.sanitize_title_filename(f"before{character}after")

    assert character not in result
    assert "before" in result and "after" in result


def test_characters_are_removed_rather_than_substituted() -> None:
    """A substitution invents a character the author did not write.

    The frontmatter `title` still carries the exact original either way, so an
    omission loses nothing a reader cannot recover -- while `Q3- revenue - margin`
    is a name nobody typed and nobody can explain.
    """
    assert vault.sanitize_title_filename('Q3: revenue / margin — what "held"?') == (
        "Q3 revenue margin — what held"
    )


def test_a_title_keeps_the_shape_that_made_it_readable() -> None:
    """The whole point: capitals, spaces and inner punctuation survive."""
    title = "Exomem first-run defect inventory — issues 477 to 485"

    assert vault.sanitize_title_filename(title) == title


@pytest.mark.parametrize("name", ["CON", "nul", "Com4", "LPT9", "aux.txt"])
def test_a_reserved_device_name_never_becomes_a_file(name: str) -> None:
    """Windows refuses these regardless of extension, so the stem is what counts."""
    assert vault.sanitize_title_filename(name) == ""


def test_a_trailing_dot_or_space_is_dropped_before_the_filesystem_drops_it() -> None:
    """Windows silently strips them, so the name written is not the name stored.

    Dropping them here keeps the name exomem believes it wrote equal to the name
    on disk, which is what every later link resolution depends on.
    """
    assert vault.sanitize_title_filename("Quarterly review. ") == "Quarterly review"
    assert vault.sanitize_title_filename("Quarterly review ") == "Quarterly review"


def test_control_and_zero_width_characters_cannot_reach_a_filename() -> None:
    """Unicode category C covers both, and both are wrong in a name a human reads.

    A tab is whitespace and collapses to a space. A zero-width space leaves no
    visible trace at all, so two files whose names look identical in every
    listing would be different files, and a hand-typed wikilink could only
    ever match one of them by accident.
    """
    assert vault.sanitize_title_filename("before\tafter") == "before after"
    assert vault.sanitize_title_filename("before\x07after") == "beforeafter"
    assert vault.sanitize_title_filename("before\u200bafter") == "beforeafter"


def test_a_title_that_sanitises_away_reports_it_rather_than_guessing() -> None:
    """The caller falls back to slug style for that one note.

    Returning "" rather than a placeholder keeps the decision with the caller,
    which is the only place that knows the title it could slug instead.
    """
    assert vault.sanitize_title_filename('<>:"|?*') == ""
    assert vault.sanitize_title_filename("   ") == ""


def test_the_name_is_nfc_wherever_it_was_authored() -> None:
    """HFS+ stores NFD; Obsidian and git both expect NFC.

    Normalising at the boundary keeps one form on disk, so the same title
    authored on macOS and on Linux is the same file rather than two.
    """
    decomposed = unicodedata.normalize("NFD", "Tallinnasse sõbra juurde")

    result = vault.sanitize_title_filename(decomposed)

    assert result == unicodedata.normalize("NFC", result)
    assert result == "Tallinnasse sõbra juurde"


def test_truncation_falls_on_a_word_boundary() -> None:
    """Matching `slugify_title`, so a cut name still ends on something readable."""
    title = "Yadm dotfiles repo clone it rather than worktree it and diff test failures"

    result = vault.sanitize_title_filename(title, max_length=40)

    assert len(result) <= 40
    assert result == "Yadm dotfiles repo clone it rather than"


def test_the_sanitizer_is_platform_independent() -> None:
    """The property the union exists for, asserted rather than assumed.

    A per-host sanitizer would pass its own CI lane and produce a vault the other
    platforms cannot open -- a failure with no local symptom.
    """
    samples = [
        'Q3: revenue / margin — what "held"?',
        "NUL",
        "Quarterly review. ",
        "Tallinnasse sõbra juurde",
    ]
    # No platform branch may exist in the implementation for these to differ.
    import inspect

    source = inspect.getsource(vault.sanitize_title_filename)
    assert "os.name" not in source
    assert "sys.platform" not in source
    assert all(vault.sanitize_title_filename(s) == vault.sanitize_title_filename(s) for s in samples)


# ------------------------------------------------------------ style resolution


def _write_keys(root: Path, body: str) -> None:
    schema = vault.kb_root(root) / "_Schema"
    schema.mkdir(parents=True, exist_ok=True)
    (schema / "project-keys.yaml").write_text(body, encoding="utf-8")


def test_a_vault_that_says_nothing_gets_todays_behaviour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default does not move.

    The filename is the identity, so flipping the default would change the
    address of every note written after an upgrade, in vaults whose owners never
    asked. Half a vault named each way, caused by a version bump, is worse than a
    vault consistently named the less pretty way.
    """
    monkeypatch.delenv("EXOMEM_FILENAME_STYLE", raising=False)

    assert vault.resolve_filename_style(tmp_path) == "slug"
    assert vault.resolve_filename_style(None) == "slug"


def test_the_vault_key_selects_the_style(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_FILENAME_STYLE", raising=False)
    _write_keys(tmp_path, "filename_style: title\nprojects:\n  demo: Demo\n")

    assert vault.resolve_filename_style(tmp_path) == "title"


def test_the_environment_outranks_the_vault_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """So a single run can be pinned without editing the vault."""
    _write_keys(tmp_path, "filename_style: title\n")
    monkeypatch.setenv("EXOMEM_FILENAME_STYLE", "slug")

    assert vault.resolve_filename_style(tmp_path) == "slug"


def test_an_unrecognised_style_is_refused_not_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo that silently means "carry on" is how a user concludes it is broken.

    The error names where the value came from, because the two sources are edited
    in different places and the wrong guess costs a second round of confusion.
    """
    monkeypatch.setenv("EXOMEM_FILENAME_STYLE", "Title Case")

    with pytest.raises(vault.InvalidSlugError) as caught:
        vault.resolve_filename_style(tmp_path)

    assert "environment" in str(caught.value)

    monkeypatch.delenv("EXOMEM_FILENAME_STYLE", raising=False)
    _write_keys(tmp_path, "filename_style: kebab\n")

    with pytest.raises(vault.InvalidSlugError) as caught:
        vault.resolve_filename_style(tmp_path)

    assert "project-keys.yaml" in str(caught.value)


def test_an_unreadable_key_file_means_no_preference_not_a_choice(
    tmp_path: Path,
) -> None:
    """Distinct from `load_project_registry`, which falls back to a real registry.

    A vault with no project keys still has to route writes somewhere, so that
    reader invents a registry. Here inventing anything would be claiming the
    vault chose a style it never mentioned.
    """
    assert project_keys.filename_style(tmp_path) is None

    _write_keys(tmp_path, "projects: [this is not a mapping\n")
    assert project_keys.filename_style(tmp_path) is None

    _write_keys(tmp_path, "filename_style: 4\n")
    assert project_keys.filename_style(tmp_path) is None


# ------------------------------------------------------------- end to end


_ISSUE_TITLES = [
    "Exomem first-run defect inventory — issues 477 to 485",
    "Zellij session serialization: what cold recovery can and cannot know",
    "Hub88 Postgres MCP consolidated banner discriminator",
]


def test_the_default_derivation_is_byte_identical_to_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The property that makes this safe to land before anyone picks a default.

    A vault that sets nothing must derive the names it always derived, or the
    change is a silent rename of every note written after an upgrade.
    """
    monkeypatch.delenv("EXOMEM_FILENAME_STYLE", raising=False)

    for title in _ISSUE_TITLES:
        derived, _warnings = vault.resolve_filename_slug(title, vault_root=tmp_path)
        assert derived == vault.slugify_title(title)


def test_title_style_produces_the_name_the_issue_asked_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#598 states the problem as a side-by-side `ls`; this is that comparison."""
    monkeypatch.setenv("EXOMEM_FILENAME_STYLE", "title")
    title = "Exomem first-run defect inventory — issues 477 to 485"

    derived, warnings = vault.resolve_filename_slug(title, vault_root=tmp_path)

    assert derived == title
    assert warnings == []
    # And the old behaviour is what it replaces, for the same title.
    monkeypatch.setenv("EXOMEM_FILENAME_STYLE", "slug")
    slugged, _ = vault.resolve_filename_slug(title, vault_root=tmp_path)
    assert slugged == "exomem-first-run-defect-inventory-issues-477-to-485"


def test_an_explicit_slug_still_wins_under_title_style(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It is how a caller pins a name it already intends to link to."""
    monkeypatch.setenv("EXOMEM_FILENAME_STYLE", "title")

    derived, warnings = vault.resolve_filename_slug(
        "Some Human Title", "quarterly-review", vault_root=tmp_path
    )

    assert derived == "quarterly-review"
    assert warnings == []
    with pytest.raises(vault.InvalidSlugError):
        vault.resolve_filename_slug("t", "Not Kebab", vault_root=tmp_path)


def test_title_style_does_not_warn_about_transliteration_it_did_not_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """That warning exists because the slug path is lossy for non-ASCII text.

    Preserving the title is precisely what a vault sets this style to do, so
    emitting the warning anyway would train the user to ignore it.
    """
    monkeypatch.setenv("EXOMEM_FILENAME_STYLE", "title")

    derived, warnings = vault.resolve_filename_slug(
        "Tallinnasse sõbra juurde", vault_root=tmp_path
    )

    assert derived == "Tallinnasse sõbra juurde"
    assert warnings == []

    monkeypatch.setenv("EXOMEM_FILENAME_STYLE", "slug")
    _slugged, slug_warnings = vault.resolve_filename_slug(
        "Tallinnasse sõbra juurde", vault_root=tmp_path
    )
    assert any("transliteration" in warning for warning in slug_warnings)


def test_a_title_of_only_reserved_characters_falls_back_rather_than_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One note named the old way beats a refused write."""
    monkeypatch.setenv("EXOMEM_FILENAME_STYLE", "title")

    derived, _warnings = vault.resolve_filename_slug('<>:"|?*', vault_root=tmp_path)

    assert derived
    assert derived == vault.slugify_title('<>:"|?*')


def test_two_titles_differing_only_by_case_do_not_collide(tmp_path: Path) -> None:
    """The new failure mode that title style introduces.

    Under slug style everything was lowercased, so these already landed on one
    name and were disambiguated. Preserving capitals makes them two names that
    are the same file on Windows and default macOS -- and two different files on
    Linux, which is the same vault losing a note when it syncs.
    """
    (tmp_path / "Budget Review.md").write_text("first", encoding="utf-8")

    resolved = vault.unique_path(tmp_path, "budget review")

    assert resolved.name != "budget review.md"
    assert resolved.name == "budget review-2.md"
    assert (tmp_path / "Budget Review.md").read_text(encoding="utf-8") == "first"


def test_collision_detection_does_not_depend_on_the_platform() -> None:
    """`Path.exists()` is case-sensitive on Linux and not elsewhere.

    Using it made the contents of a vault depend on which machine wrote the
    note, with the damage only visible after a sync. Asserted against the
    implementation because CI cannot run the same test on a case-sensitive and a
    case-insensitive filesystem in one job.
    """
    import inspect

    source = inspect.getsource(vault.unique_path)
    assert "casefold()" in source
    assert "iterdir()" in source


def test_changing_the_style_renames_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The promise that makes this landable without a migration.

    Because the filename is the identity here, a rename is a link break. So the
    style governs derivation only, and a vault reconfigured mid-life keeps every
    name it already had -- at the cost of being mixed, which is the trade taken
    deliberately.
    """
    notes = vault.kb_root(tmp_path) / "Notes"
    notes.mkdir(parents=True)
    existing = notes / "exomem-first-run-defect-inventory-issues-477-to-485.md"
    existing.write_text("---\ntitle: Exomem first-run defect inventory\n---\nbody\n", encoding="utf-8")
    before = {path.name: path.read_bytes() for path in notes.iterdir()}

    monkeypatch.setenv("EXOMEM_FILENAME_STYLE", "title")
    assert vault.resolve_filename_style(tmp_path) == "title"
    new_name, _ = vault.resolve_filename_slug("A newly written note", vault_root=tmp_path)

    assert {path.name: path.read_bytes() for path in notes.iterdir()} == before
    assert new_name == "A newly written note"


def test_a_title_wikilink_resolves_across_both_styles(tmp_path: Path) -> None:
    """Why a mixed vault is survivable rather than merely tolerated.

    `WikilinkResolver` already keys by frontmatter title as well as by path and
    stem -- the docstring's own example is a link resolving to a file whose stem
    does not match its title. So a link written the way a human writes it does
    not care which style named the target.
    """
    resolver = vault.WikilinkResolver.from_entries(
        tmp_path,
        [
            ("Knowledge Base/Notes/old-style-slugged-name", "Old Style Note"),
            ("Knowledge Base/Notes/New Style Readable Name", "New Style Note"),
        ],
    )

    assert resolver.titles["old style note"] == ["Knowledge Base/Notes/old-style-slugged-name"]
    assert resolver.titles["new style note"] == ["Knowledge Base/Notes/New Style Readable Name"]
    # And each is still reachable by its own stem, so path-keyed links survive too.
    assert "old-style-slugged-name" in resolver.stems
    assert "New Style Readable Name" in resolver.stems
