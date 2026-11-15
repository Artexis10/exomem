"""What a vault page must be to count as an Agent Skill, and how its line is cut for the catalogue."""

from __future__ import annotations

import pytest

from exomem import vault_skills

SUMMARY_LIMIT = 200

KB = "Knowledge Base"


@pytest.mark.parametrize(
    "path",
    [
        f"{KB}/Skills/release/SKILL.md",
        f"{KB}/Projects/Harbour Ops/runbooks/mooring/SKILL.md",
        "Personal/Methods/SKILL.md",
    ],
)
def test_a_skill_md_page_with_a_name_and_description_is_a_skill_wherever_it_lives(path: str) -> None:
    skill = vault_skills.recognise(path, {"name": " mooring ", "description": "Use when tying up. "})

    assert skill == vault_skills.Skill("mooring", "Use when tying up.")


@pytest.mark.parametrize(
    ("path", "frontmatter"),
    [
        (f"{KB}/Skills/a/SKILL.md", {"name": "a"}),
        (f"{KB}/Skills/a/SKILL.md", {"name": "a", "description": "   "}),
        (f"{KB}/Skills/a/SKILL.md", {"name": "a", "description": ["Use when tying up."]}),
        (f"{KB}/Skills/a/SKILL.md", {"name": "", "description": "Use when tying up."}),
        (f"{KB}/Skills/a/SKILL.md", {"name": 7, "description": "Use when tying up."}),
        (f"{KB}/Skills/a/SKILL.md", None),
        (f"{KB}/Skills/a/README.md", {"name": "a", "description": "Use when tying up."}),
        (f"{KB}/Skills/a/skill.md", {"name": "a", "description": "Use when tying up."}),
    ],
)
def test_a_page_without_both_text_fields_or_the_exact_file_name_is_not_a_skill(path: str, frontmatter) -> None:
    assert vault_skills.recognise(path, frontmatter) is None


@pytest.mark.parametrize("tree", ["Sources", "Evidence"])
def test_a_skill_shaped_page_in_captured_material_is_not_a_skill(tree: str) -> None:
    """Sources and Evidence hold captured text, so an instruction page there is never offered as a skill."""
    path = f"{KB}/{tree}/imported/SKILL.md"

    assert vault_skills.recognise(path, {"name": "a", "description": "Use when tying up."}) is None


def test_a_name_with_a_wikilink_is_served_without_it() -> None:
    skill = vault_skills.recognise(f"{KB}/Skills/a/SKILL.md", {"name": "docs [[Hidden Page]] writer", "description": "Use when writing."})

    assert skill == vault_skills.Skill("docs writer", "Use when writing.")


def test_the_summary_is_the_first_sentence_of_the_description() -> None:
    description = "Use when tying up a boat.\nNever use it for\nanchoring. Ask first."

    assert vault_skills.summary(description, SUMMARY_LIMIT) == "Use when tying up a boat."


def test_a_long_first_sentence_is_cut_at_a_whole_word_and_marked() -> None:
    description = "Use when " + "mooring " * 60 + "at the quay."

    summary = vault_skills.summary(description, SUMMARY_LIMIT)

    assert len(summary) <= SUMMARY_LIMIT
    assert summary.endswith("mooring…")
    assert description.startswith(summary.removesuffix("…"))


def test_native_names_keep_strings_within_the_request_bounds_and_ignore_the_rest() -> None:
    long_name = "n" * (vault_skills.NATIVE_SKILL_NAME_MAX_CHARS + 1)
    many = [f"skill-{index}" for index in range(vault_skills.NATIVE_SKILLS_MAX + 50)]

    assert vault_skills.bounded_native_names(["review", " deploy ", "", 3, None, long_name]) == {"review", "deploy"}
    assert len(vault_skills.bounded_native_names(many)) == vault_skills.NATIVE_SKILLS_MAX
    assert vault_skills.bounded_native_names("review") == frozenset()
