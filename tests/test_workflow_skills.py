from __future__ import annotations

import re

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

PRODUCT_COMMAND_HINTS = [
    "ask_memory",
    "read_memory",
    "remember",
    "edit_memory",
    "replace_memory",
    "capture_source",
    "compile_source",
    "preserve_evidence",
    "transfer_artifact",
    "review_memory",
    "connect_memory",
    "maintain_memory",
    "read_media",
]

LEAF_COMMANDS_THAT_SHOULD_NOT_DRIVE_WORKFLOW_SKILLS = [
    "find",
    "get",
    "add",
    "note",
    "preserve",
    "edit",
    "replace",
    "suggest_links",
    "graph_context",
    "attention",
    "audit",
    "evolution",
    "propose_compilation",
    "get_video_frames",
    "query_data",
    "overview",
    "adopt",
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


def test_workflow_skill_docs_route_through_product_commands() -> None:
    for name in EXPECTED_WORKFLOW_SKILLS:
        skill_md = workflow_skills.source_dir(name) / "SKILL.md"
        text = skill_md.read_text(encoding="utf-8")

        assert any(f"`{command}" in text for command in PRODUCT_COMMAND_HINTS), (
            f"{name} should mention at least one product command"
        )

        for command in LEAF_COMMANDS_THAT_SHOULD_NOT_DRIVE_WORKFLOW_SKILLS:
            pattern = rf"`{re.escape(command)}(?:`|\()"
            assert not re.search(pattern, text), (
                f"{name} should not route agents through leaf command `{command}`"
            )


def test_core_skill_tool_loading_mentions_current_product_surface() -> None:
    skill_md = workflow_skills.WORKFLOW_SKILLS_DIR.parent / "SKILL.md"
    text = skill_md.read_text(encoding="utf-8")
    loading_section = text.split("## Loading the tools", maxsplit=1)[1].split(
        "## Workflow skills", maxsplit=1
    )[0]

    for command in [
        "bootstrap",
        "ask_memory",
        "read_memory",
        "browse_memory",
        "remember",
        "edit_memory",
        "replace_memory",
        "capture_source",
        "compile_source",
        "preserve_evidence",
        "transfer_artifact",
        "review_memory",
        "connect_memory",
        "adopt_vault",
        "maintain_memory",
        "query_dataset",
        "read_media",
    ]:
        assert command in loading_section
