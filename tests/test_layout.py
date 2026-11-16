"""The layout model: record resolution, selector translation, the per-page mapping and names."""

from __future__ import annotations

import random
import time
import unicodedata
from pathlib import Path

import pytest

from exomem import layout, note_types
from exomem.layout import FixedBecause, Layout, NameClass, Placement
from exomem.vault_store import read

KB = "Knowledge Base"
NOTE_TYPE_OVERLAY = (
    "schema_version: 1\nentries:\n  playbook:\n"
    "    attributes: {role: compiled, folder: Notes/Playbooks}\n"
)
HOMES_OVERLAY = (
    "schema_version: 1\nentries:\n  equipment:\n    label: Equipment\n    description: Workshop notes.\n"
    "    attributes: {folder: Notes/Equipment, topics: [equipment]}\n"
)


def _write(vault: Path, rel: str, text: str = "") -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    _write(tmp_path, f"{KB}/_Schema/SKILL.md")
    return tmp_path


@pytest.fixture
def registry(vault: Path) -> layout.Registry:
    _write(vault, f"{KB}/_Schema/note-types.yaml", NOTE_TYPE_OVERLAY)
    _write(vault, f"{KB}/_Schema/homes.yaml", HOMES_OVERLAY)
    return layout.load_registry(vault)


# --------------------------------------------------------------------------- #
# layout.resolve
# --------------------------------------------------------------------------- #


def test_a_record_that_says_root_gives_root_and_reports_a_leftover_wrapper_schema_folder(tmp_path):
    """The record outranks folders: a root vault that still holds a leftover wrapper must read as root."""
    _write(tmp_path, f"_Schema/{layout.RECORD_NAME}", "layout: root\nunmanaged: [Reference]\n")
    _write(tmp_path, f"{KB}/_Schema/SKILL.md")

    resolved = layout.inspect(tmp_path)

    assert resolved.layout is Layout.ROOT
    assert resolved.record.unmanaged == ("Reference",)
    assert resolved.leftover_schema_folders == (f"{KB}/_Schema",)


def test_a_record_that_says_nested_gives_nested_from_either_place_and_reports_the_other_folder(
    tmp_path,
):
    """The record lives in the managed root's schema folder, so a nested vault keeps it inside the wrapper."""
    inside = tmp_path / "inside"
    _write(inside, f"{KB}/_Schema/{layout.RECORD_NAME}", "layout: nested\n")
    _write(inside, "_Schema/SKILL.md")
    at_root = tmp_path / "at-root"
    _write(at_root, f"_Schema/{layout.RECORD_NAME}", "layout: nested\n")

    assert layout.inspect(inside).layout is Layout.NESTED
    assert layout.inspect(inside).leftover_schema_folders == ("_Schema",)
    assert layout.inspect(at_root) == layout.Resolved(Layout.NESTED, layout.read_record(at_root))
    assert layout.read_record(inside).path == f"{KB}/_Schema/{layout.RECORD_NAME}"


def test_the_vault_root_record_is_read_before_the_wrapper_record(tmp_path):
    """Two records can disagree after a half-finished move; the order must not depend on the walk."""
    _write(tmp_path, f"_Schema/{layout.RECORD_NAME}", "layout: root\n")
    _write(tmp_path, f"{KB}/_Schema/{layout.RECORD_NAME}", "layout: nested\n")

    resolved = layout.inspect(tmp_path)

    assert resolved.layout is Layout.ROOT
    assert resolved.leftover_schema_folders == (f"{KB}/_Schema",)


def test_without_a_record_the_wrapper_schema_or_no_schema_folder_is_nested(tmp_path):
    """Every vault that exists today has no record and must stay nested."""
    cases = {
        "wrapper": [f"{KB}/_Schema/SKILL.md"],
        "dot-folder": [".exomem/schema/SKILL.md"],
        "empty": [],
    }
    for name, files in cases.items():
        vault_root = tmp_path / name
        vault_root.mkdir()
        for rel in files:
            _write(vault_root, rel)

        assert layout.resolve(vault_root) is Layout.NESTED, name


def test_schema_folder_at_the_root_without_a_record_is_refused_naming_the_record(tmp_path):
    """Reading such a vault as nested would keep wrapper paths that no folder holds."""
    _write(tmp_path, "_Schema/SKILL.md")

    with pytest.raises(layout.LayoutError) as refused:
        layout.resolve(tmp_path)

    assert refused.value.code == "LAYOUT_RECORD_MISSING"
    assert f"_Schema/{layout.RECORD_NAME}" in refused.value.remediation
    assert "Write a layout record" in refused.value.remediation


def test_schema_folders_in_both_places_without_a_record_are_refused_naming_both_and_the_remedy(
    tmp_path,
):
    _write(tmp_path, "_Schema/SKILL.md")
    _write(tmp_path, f"{KB}/_Schema/SKILL.md")

    with pytest.raises(layout.LayoutError) as refused:
        layout.resolve(tmp_path)

    assert refused.value.code == "LAYOUT_AMBIGUOUS"
    assert "_Schema/" in refused.value.message and f"{KB}/_Schema/" in refused.value.message
    assert f"_Schema/{layout.RECORD_NAME}" in refused.value.remediation
    assert f"{KB}/_Schema/{layout.RECORD_NAME}" in refused.value.remediation


def test_record_served_by_the_store_is_read_when_its_file_is_absent(tmp_path, monkeypatch):
    """A store-mode vault keeps the record as a stored page; reading only the disk would refuse the vault."""
    _write(tmp_path, "_Schema/SKILL.md")
    stored = {f"_Schema/{layout.RECORD_NAME}": b"layout: root\n"}
    monkeypatch.setattr(read, "owned_bytes", lambda _root, rel: stored.get(rel))

    assert layout.resolve(tmp_path) is Layout.ROOT

    stored = {f"{KB}/_Schema/{layout.RECORD_NAME}": b"layout: nested\n"}

    assert layout.resolve(tmp_path) is Layout.NESTED


def test_a_malformed_record_is_refused_not_read_as_nested(tmp_path):
    """A typo in the record must not fall back to a layout that the owner did not choose."""
    texts = ["layout: flat\n", "- root\n", "layout: root\nunmanaged: Reference\n", ": ["]
    for index, text in enumerate(texts):
        vault_root = tmp_path / str(index)
        _write(vault_root, f"_Schema/{layout.RECORD_NAME}", text)

        with pytest.raises(layout.LayoutError) as refused:
            layout.resolve(vault_root)

        assert refused.value.code == "LAYOUT_RECORD_INVALID", text


# --------------------------------------------------------------------------- #
# area folders and translate_path
# --------------------------------------------------------------------------- #


def test_a_compiled_area_loses_its_notes_parent_only_in_root():
    """The writer needs a single answer for where a compiled type files its pages in each layout."""
    insight = note_types.shipped_registry().types["insight"]
    bare = note_types.NoteType(key="runbook", role="compiled", folder="Runbooks")
    entity = note_types.NoteType(key="asset", role="entity", folder="Assets")

    assert layout.area_folder(insight, "nested") == "Notes/Insights"
    assert layout.area_folder(insight, "root") == "Insights"
    assert layout.area_folder(bare, Layout.NESTED) == "Notes/Runbooks"
    assert layout.area_folder(bare, Layout.ROOT) == "Runbooks"
    assert (
        layout.area_folder(entity, Layout.NESTED)
        == layout.area_folder(entity, Layout.ROOT)
        == "Assets"
    )
    assert layout.area_folder(note_types.shipped_registry().types["source"], Layout.ROOT) is None


def test_registry_reads_overlay_types_and_homes_as_areas(registry):
    assert {"Insights", "Playbooks", "Equipment"} <= registry.areas
    assert registry.findings == ()


def test_root_translation_drops_the_wrapper_and_the_notes_level_for_registered_areas(registry):
    expected = {
        f"{KB}/Notes/Insights/**": "Insights/**",
        "Notes/Insights/**": "Insights/**",
        f"{KB}/Notes/Playbooks/2026/*.md": "Playbooks/2026/*.md",
        "Notes/Equipment/pump.md": "Equipment/pump.md",
        f"{KB}/Sources/**": "Sources/**",
        f"{KB}/Evidence/alpha/Scan.pdf": "Evidence/alpha/Scan.pdf",
        "exomem://memory/0f6c1d2e": "exomem://memory/0f6c1d2e",
    }
    for value, translated in expected.items():
        assert layout.translate_path(value, "root", registry) == translated, value


def test_root_translation_leaves_a_glob_slot_an_unregistered_area_and_other_spellings_alone(
    registry,
):
    """A guess would widen or narrow a selector; the member-set diff, not the text, vouches for a translation."""
    for value in (
        "Notes/*/draft.md",
        f"{KB}/Notes/**",
        "Notes/Insight*/**",
        "Notes/Scratch/**",
        "Notes/insights/**",
        "**/Notes/Insights/**",
    ):
        assert layout.translate_path(value, "root", registry) == value.removeprefix(f"{KB}/"), value


def test_nested_translation_returns_the_value_unchanged(registry):
    assert (
        layout.translate_path(f"{KB}/Notes/Insights/**", "nested", registry)
        == f"{KB}/Notes/Insights/**"
    )


# --------------------------------------------------------------------------- #
# place: folder and name class
# --------------------------------------------------------------------------- #


def _place(registry, path, type_value="insight", title="A title"):
    return layout.place(path, type_value=type_value, title=title, registry=registry)


def test_compiled_page_in_a_registered_area_derives_its_name_and_drops_wrapper_and_notes(registry):
    placed = _place(
        registry, f"{KB}/Notes/Insights/why-churn-rose.md", title="Why churn rose in Q3"
    )

    assert (placed.new_dir, placed.name_class, placed.stem) == (
        "Insights",
        NameClass.DERIVED,
        "Why churn rose in Q3",
    )


def test_page_in_a_home_or_overlay_area_drops_notes(registry):
    assert _place(registry, f"{KB}/Notes/Equipment/pump.md").new_dir == "Equipment"
    assert (
        _place(registry, f"{KB}/Notes/Playbooks/Q4/restart.md", type_value="playbook").new_dir
        == "Playbooks/Q4"
    )


def test_unregistered_notes_folder_keeps_its_level_is_named_and_its_pages_keep_their_names(
    registry,
):
    """Renaming into an unregistered folder would hide pages the owner never asked to move."""
    placed = _place(registry, f"{KB}/Notes/Scratch/idea.md")

    assert placed.new_dir == "Notes/Scratch"
    assert placed.unregistered_folder == "Scratch"
    assert (placed.name_class, placed.fixed_because) == (NameClass.FIXED, FixedBecause.OUTSIDE_AREA)


def test_other_folders_keep_their_managed_relative_path(registry):
    placed = _place(
        registry, f"{KB}/Entities/People/ada.md", type_value="entity", title="Ada Lovelace"
    )

    assert (placed.new_dir, placed.stem) == ("Entities/People", "Ada Lovelace")


def test_path_outside_the_managed_root_is_not_placed(registry):
    assert _place(registry, "Reference/handbook.md") is None
    assert _place(registry, f"{KB}") is None


def test_pages_whose_names_must_survive_keep_their_basename(registry):
    """Renaming any of these would break an identity the relayout does not own."""
    cases = [
        (f"{KB}/Evidence/alpha/receipts/Scan.md", "evidence", "Scan", FixedBecause.ROLE_KEEPS_NAME),
        (f"{KB}/Notes/Insights/index.md", "insight", "Insights", FixedBecause.NAVIGATION),
        (f"{KB}/Notes/Insights/log.md", "insight", "Log", FixedBecause.NAVIGATION),
        (f"{KB}/Records/leads/lead-7.md", "record", "Lead 7", FixedBecause.COLLECTION),
        (f"{KB}/Planning/roadmap/item-3.md", "plan", "Item 3", FixedBecause.COLLECTION),
        (f"{KB}/Notes/Insights/untitled.md", "insight", None, FixedBecause.NO_TITLE),
        (f"{KB}/Notes/Insights/blank.md", "insight", "   ", FixedBecause.NO_TITLE),
        (f"{KB}/Notes/Insights/notype.md", None, "Has a title", FixedBecause.UNTYPED),
        (f"{KB}/Notes/Insights/strange.md", "no-such-type", "Has a title", FixedBecause.UNTYPED),
        (f"{KB}/Notes/Insights/only-symbols.md", "insight", "#^[]", FixedBecause.NO_USABLE_NAME),
        (f"{KB}/Notes/Insights/chart.png", "insight", "Chart", FixedBecause.NOT_MARKDOWN),
        (f"{KB}/_trash/Notes/Insights/gone.md", "insight", "Gone", FixedBecause.MACHINERY),
        (f"{KB}/_archive/Sources/2026-01-01-old.md", "source", "Old", FixedBecause.MACHINERY),
    ]
    for path, type_value, title, because in cases:
        placed = _place(registry, path, type_value=type_value, title=title)

        assert placed.name_class is NameClass.FIXED, path
        assert placed.fixed_because is because, path
        assert placed.stem + placed.extension == Path(path).name, path


def test_a_part_page_keeps_its_name_and_holds_no_name_of_its_own(registry):
    """A part page is found by its folder and its file name, so it never renames and never takes a source page's name."""
    placed = _place(
        registry,
        f"{KB}/Sources/Articles/.2026-05-02-long-article.parts/part-02.md",
        type_value="source",
        title="Part 2 of 2: Long article",
    )

    assert (placed.name_class, placed.fixed_because) == (
        NameClass.FIXED,
        FixedBecause.EXTRACTION_PART,
    )
    assert placed.holds_name is False
    assert placed.stem + placed.extension == "part-02.md"


def test_trashed_and_non_markdown_files_hold_no_name_but_evidence_markdown_does(registry):
    """Name uniqueness counts owned Markdown outside the machinery folders, so a trashed page never holds a name."""
    assert _place(registry, f"{KB}/_trash/Notes/Insights/gone.md").holds_name is False
    assert _place(registry, f"{KB}/Evidence/a/Scan.pdf").holds_name is False
    assert _place(registry, f"{KB}/Evidence/a/Scan.md", type_value="evidence").holds_name is True


def test_source_keeps_the_date_prefix_that_its_current_name_parses_to(registry):
    """The relayout never moves a date: a dated name keeps its prefix, an undated name gains none."""
    expected = {
        "2026-05-02-pricing-basics": "2026-05-02-",
        "2026-05-pricing-basics": "2026-05-",
        "2026-13-02-pricing-basics": "",
        "2026-02-31-pricing-basics": "2026-02-",
        "pricing-basics": "",
    }
    for current, prefix in expected.items():
        placed = _place(
            registry,
            f"{KB}/Sources/Articles/{current}.md",
            type_value="source",
            title="Pricing basics",
        )

        assert placed.date_prefix == prefix, current


def test_title_stem_type_gets_no_date_prefix_and_a_started_stem_keeps_one(registry):
    undated = _place(registry, f"{KB}/Notes/Insights/2026-05-02-retro.md", title="Retro")
    started = _place(
        registry,
        f"{KB}/Notes/Experiments/2026-05-onboarding.md",
        type_value="experiment",
        title="Onboarding",
    )

    assert undated.date_prefix == ""
    assert started.date_prefix == "2026-05-"


def test_placement_reports_which_name_corrections_changed_the_title(registry):
    placed = _place(registry, f"{KB}/Notes/Insights/c-sharp.md", title=".C# basics")

    assert placed.stem == "C basics"
    assert placed.corrections == (layout.CORRECTION_LINK_CHARACTERS, layout.CORRECTION_LEADING_DOTS)


# --------------------------------------------------------------------------- #
# corrected names
# --------------------------------------------------------------------------- #


def test_each_link_grammar_character_is_removed_from_a_title():
    """A name that holds any of these would write a link that Obsidian reads as an anchor, a block or a code span."""
    for character in ("#", "^", "[", "]", "`"):
        corrected = layout.corrected_title_name(f"Before{character} after")

        assert corrected.name == "Before after", character
        assert corrected.corrections == (layout.CORRECTION_LINK_CHARACTERS,), character


def test_title_without_grammar_characters_is_untouched_apart_from_the_sanitiser():
    assert (
        layout.corrected_title_name("Why churn rose in Q3: a look").name
        == "Why churn rose in Q3 a look"
    )
    assert layout.corrected_title_name("Why churn rose in Q3").corrections == ()


def test_leading_dots_are_stripped_so_the_file_is_not_hidden():
    """A dot-named file is outside the store and hidden in Obsidian."""
    corrected = layout.corrected_title_name("..Roadmap v1.2")

    assert corrected.name == "Roadmap v1.2"
    assert corrected.corrections == (layout.CORRECTION_LEADING_DOTS,)


def test_a_name_that_becomes_a_reserved_device_name_after_the_dots_go_is_empty():
    """`.con` is a legal name on Windows, `con` is not, so the stripped form must be re-checked."""
    assert layout.corrected_title_name(".con").name == ""


# --------------------------------------------------------------------------- #
# the byte cap
# --------------------------------------------------------------------------- #


def _bytes(stem: str) -> int:
    return len((stem + ".md").encode())


def test_multibyte_title_is_cut_at_a_code_point_under_the_cap_with_the_suffix_counted():
    title = "あ" * 90  # 270 bytes
    plain = layout.fit_filename(title)
    second = layout.fit_filename(title, number=2)

    assert plain is not None and second is not None
    for (stem, truncated), number in ((plain, 1), (second, 2)):
        assert truncated is True
        assert _bytes(stem) <= layout.MAX_FILENAME_BYTES
        assert stem.removesuffix(" (2)").strip("あ") == ""  # only whole characters remain
        assert stem.endswith(" (2)") == (number == 2)
    # The suffix takes bytes from the title, not from the cap.
    assert len(second[0].removesuffix(" (2)")) < len(plain[0])


def test_date_prefix_and_extension_are_never_shortened():
    stem, truncated = layout.fit_filename("x" * 300, date_prefix="2026-05-02-", number=3)

    assert stem.startswith("2026-05-02-x") and stem.endswith("x (3)")
    assert truncated is True
    assert _bytes(stem) == layout.MAX_FILENAME_BYTES


def test_a_title_that_fits_is_not_marked_truncated_and_a_cut_does_not_end_on_a_dot_or_space():
    assert layout.fit_filename("Short title") == ("Short title", False)
    cut = layout.fit_filename("a" * 195 + " ...tail")[0]

    assert not cut.endswith((".", " "))


def test_no_name_fits_when_the_prefix_and_suffix_use_the_whole_cap():
    assert layout.fit_filename("Title", date_prefix="p" * 200) is None


# --------------------------------------------------------------------------- #
# vault-wide uniqueness
# --------------------------------------------------------------------------- #


def _derived(path, stem, *, new_dir="Insights", prefix=""):
    return Placement(path, new_dir, NameClass.DERIVED, stem, ".md", date_prefix=prefix)


def _fixed(path, stem, *, new_dir="Evidence", holds_name=True, extension=".md"):
    return Placement(path, new_dir, NameClass.FIXED, stem, extension, holds_name=holds_name)


def _names(result):
    return {item.current_path: item.new_path for item in result.placed}


def test_first_homonym_by_current_path_keeps_the_plain_name_and_later_ones_take_the_lowest_free_number():
    """Which page keeps `Budget` must not depend on read order, or a second plan renames different pages."""
    result = layout.allocate(
        [
            _derived("kb/Notes/Patterns/b.md", "Budget", new_dir="Patterns"),
            _derived("kb/Notes/Insights/a.md", "Budget"),
            _derived("kb/Notes/Failures/c.md", "Budget", new_dir="Failures"),
        ]
    )

    assert _names(result) == {
        "kb/Notes/Failures/c.md": "Failures/Budget.md",
        "kb/Notes/Insights/a.md": "Insights/Budget (2).md",
        "kb/Notes/Patterns/b.md": "Patterns/Budget (3).md",
    }


def test_homonyms_compare_under_case_and_unicode_form():
    composed, decomposed = "Café", unicodedata.normalize("NFD", "café")
    result = layout.allocate(
        [_derived("a.md", composed), _derived("b.md", decomposed, new_dir="Failures")]
    )

    assert _names(result)["b.md"] == f"Failures/{decomposed} (2).md"


def test_derived_name_yields_to_a_fixed_name_whatever_the_path_order():
    """A fixed name cannot change, so the derived page must take the suffix even if it sorts first."""
    result = layout.allocate(
        [
            _derived("a/first.md", "Scan"),
            _fixed("z/last.md", "Scan", new_dir="Evidence/alpha"),
        ]
    )

    assert _names(result) == {
        "a/first.md": "Insights/Scan (2).md",
        "z/last.md": "Evidence/alpha/Scan.md",
    }


def test_fixed_names_may_repeat_in_different_folders():
    """Evidence names collide by design, so only a shared path makes a fixed page unmappable."""
    result = layout.allocate(
        [_fixed("a.md", "Scan", new_dir="Evidence/a"), _fixed("b.md", "Scan", new_dir="Evidence/b")]
    )

    assert result.unmappable == ()
    assert len(result.placed) == 2


def test_a_page_keeps_its_plain_name_when_another_pages_suffix_would_equal_it():
    """Sequential numbering would turn the page titled `Budget (2)` into `Budget (2) (2)`."""
    result = layout.allocate(
        [
            _derived("a.md", "Budget"),
            _derived("b.md", "Budget"),
            _derived("c.md", "Budget (2)"),
        ]
    )

    assert _names(result) == {
        "a.md": "Insights/Budget.md",
        "b.md": "Insights/Budget (3).md",
        "c.md": "Insights/Budget (2).md",
    }


def test_names_in_trash_do_not_hold_a_name():
    result = layout.allocate(
        [
            _fixed("_trash/Budget.md", "Budget", new_dir="_trash", holds_name=False),
            _derived("a.md", "Budget"),
        ]
    )

    assert _names(result)["a.md"] == "Insights/Budget.md"


def test_two_fixed_pages_on_one_new_path_are_both_unmappable():
    result = layout.allocate(
        [
            _fixed("kb/Evidence/x/Scan.md", "Scan", new_dir="Evidence/x"),
            _fixed("kb/Notes/Evidence/x/Scan.md", "Scan", new_dir="Evidence/x"),
            _fixed("kb/Evidence/y/Scan.md", "Scan", new_dir="Evidence/y"),
        ]
    )

    assert [(item.current_path, item.reason, item.also) for item in result.unmappable] == [
        ("kb/Evidence/x/Scan.md", layout.UNMAPPABLE_SAME_PATH, ("kb/Notes/Evidence/x/Scan.md",)),
        ("kb/Notes/Evidence/x/Scan.md", layout.UNMAPPABLE_SAME_PATH, ("kb/Evidence/x/Scan.md",)),
    ]
    assert list(_names(result)) == ["kb/Evidence/y/Scan.md"]


def test_a_derived_page_with_no_room_for_its_name_is_unmappable():
    result = layout.allocate([_derived("a.md", "Title", prefix="p" * 200)])

    assert [item.reason for item in result.unmappable] == [layout.UNMAPPABLE_NO_NAME_FITS]
    assert result.placed == ()


def test_multibyte_homonyms_stay_under_the_byte_cap_with_their_suffix():
    title = "あ" * 90
    result = layout.allocate([_derived("a.md", title), _derived("b.md", title)])

    by_path = {item.current_path: item for item in result.placed}
    assert by_path["a.md"].number == 1 and by_path["b.md"].number == 2
    assert all(
        len(Path(item.new_path).name.encode()) <= layout.MAX_FILENAME_BYTES
        for item in result.placed
    )
    assert by_path["b.md"].new_path.endswith(" (2).md") and by_path["b.md"].truncated is True


def test_allocation_is_repeatable_whatever_order_the_pages_arrive_in():
    """A second plan run must give the same names and suffixes."""
    pages = [
        _derived(f"p{index:02d}.md", ("Budget", "Plan", "Budget (2)", "Notes")[index % 4])
        for index in range(24)
    ]
    pages += [_fixed("zz.md", "Plan", new_dir="Evidence")]
    first = layout.allocate(pages)
    shuffled = pages[:]
    random.Random(7).shuffle(shuffled)

    assert layout.allocate(shuffled) == first
    assert len({item.new_path.casefold() for item in first.placed}) == len(first.placed)


def test_twenty_thousand_pages_with_a_homonym_group_of_ten_thousand_allocate_in_seconds():
    """A suffix search that restarts at ` (2)` for each page is quadratic in the group and takes minutes here."""
    pages = [
        _derived(
            f"kb/Notes/Insights/p{number:05d}.md", "Untitled" if number % 2 else f"Page {number}"
        )
        for number in range(20_000)
    ]

    started = time.perf_counter()
    result = layout.allocate(pages)
    elapsed = time.perf_counter() - started

    assert result.unmappable == ()
    assert len({item.new_path.casefold() for item in result.placed}) == 20_000
    assert max(item.number for item in result.placed) == 10_000
    assert elapsed < 10  # measured near 0.1 s; the margin absorbs a loaded host
