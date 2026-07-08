from __future__ import annotations

from exomem import workflow_skills

EXPECTED_WORKFLOW_SKILLS = [
    "exomem-continue",
    "exomem-capture",
    "exomem-ingest",
    "exomem-research",
    "exomem-reflect",
    "exomem-curate",
    "exomem-defrag",
    "exomem-review",
    "exomem-media",
]

REQUIRED_SECTIONS = [
    "## Purpose",
    "## When to use",
    "## Workflow",
    "## Output contract",
    "## Save rules",
    "## Mistakes to avoid",
]


def test_workflow_skill_index_lists_first_pass_skills() -> None:
    skills = workflow_skills.list_skills()

    assert [s["name"] for s in skills] == EXPECTED_WORKFLOW_SKILLS
    for skill in skills:
        assert skill["purpose"]
        assert skill["triggers"]


def test_workflow_skill_docs_have_required_contract_sections() -> None:
    for name in EXPECTED_WORKFLOW_SKILLS:
        skill_md = workflow_skills.source_dir(name) / "SKILL.md"
        assert skill_md.is_file()
        text = skill_md.read_text(encoding="utf-8")
        assert f"name: {name}" in text
        for section in REQUIRED_SECTIONS:
            assert section in text, f"{name} missing {section}"
