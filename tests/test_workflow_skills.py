from __future__ import annotations

import re

import yaml

from exomem import semantic_authoring, workflow_skills

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
    "observe_memory",
    "replace_memory",
    "capture_source",
    "compile_source",
    "preserve_evidence",
    "transfer_artifact",
    "review_memory",
    "connect_memory",
    "maintain_memory",
    "process_media",
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


def test_core_and_standalone_authoring_skills_embed_the_canonical_contract() -> None:
    concise = semantic_authoring.render_concise()
    identity = semantic_authoring.contract_identity()
    # The concise projection carries the v3 identity marker and the complete
    # portable-category teaching; every embedding must therefore be exact.
    assert "exomem-semantic-authoring:v3 " in concise
    assert identity.split(" ", 1)[1] in concise  # content digest
    for expected_fragment in (
        "Core keys are `action`",
        "`techniques` → `technique`",
        "- [constraint] Keep retry windows bounded #code ^retry-windows",
        "- [design] Keep the public adapter stateless #api ^public-adapter",
        "[[Knowledge Base/Notes/Design/Public adapter]]",
    ):
        assert expected_fragment in concise

    core = workflow_skills.WORKFLOW_SKILLS_DIR.parent / "SKILL.md"
    core_text = core.read_text(encoding="utf-8")
    assert core_text.count(concise) == 1
    workflow_skills.validate_contract_projection("exomem", core.parent, core=True)

    authoring = [
        str(skill["name"])
        for skill in workflow_skills.list_skills()
        if skill.get("standalone_authoring") is True
    ]
    assert authoring == EXPECTED_WORKFLOW_SKILLS
    # Core scaffold SKILL + every standalone authoring workflow = the 10 required
    # generic SKILL files that must embed the canonical contract verbatim once.
    assert len(authoring) + 1 == 10
    for name in authoring:
        skill_dir = workflow_skills.source_dir(name)
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        assert text.count(concise) == 1, f"{name} must carry the standalone contract"
        assert "repository checkout" not in concise.lower()
        # A reference to the core skill is not a substitute for the embedding.
        workflow_skills.validate_contract_projection(name, skill_dir)


def test_compiled_note_templates_teach_parse_inert_compact_authoring() -> None:
    page_types = (
        workflow_skills.WORKFLOW_SKILLS_DIR.parent / "references" / "page-types.md"
    ).read_text(encoding="utf-8")
    compiled_types = (
        "research-note",
        "insight",
        "failure",
        "pattern",
        "experiment",
        "production-log",
    )
    compact_example = "- [operating constraint] Keep retries bounded #reliability"
    for index, page_type in enumerate(compiled_types):
        start = page_types.index(f"## {page_type}\n")
        end = (
            page_types.index(f"## {compiled_types[index + 1]}\n")
            if index + 1 < len(compiled_types)
            else page_types.index("## entity\n")
        )
        template = page_types[start:end]
        assert "```markdown" in template
        assert "## Observations" in template
        assert compact_example in template

    guidance = page_types.split("## research-note\n", 1)[0]
    assert "open vocabulary" in guidance
    assert "governed rich kind" in guidance


def test_workflow_skill_docs_have_required_contract_sections() -> None:
    for name in EXPECTED_WORKFLOW_SKILLS:
        skill_md = workflow_skills.source_dir(name) / "SKILL.md"
        assert skill_md.is_file()
        text = skill_md.read_text(encoding="utf-8")
        assert f"name: {name}" in text
        for section in REQUIRED_SECTIONS:
            assert section in text, f"{name} missing {section}"


def test_workflow_skill_frontmatter_is_valid_yaml() -> None:
    for name in EXPECTED_WORKFLOW_SKILLS:
        skill_md = workflow_skills.source_dir(name) / "SKILL.md"
        text = skill_md.read_text(encoding="utf-8")
        frontmatter = text.removeprefix("---\n").split("\n---\n", 1)[0]
        parsed = yaml.safe_load(frontmatter)
        assert parsed["name"] == name
        assert isinstance(parsed["description"], str)


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
        "observe_memory",
        "replace_memory",
        "capture_source",
        "compile_source",
        "preserve_evidence",
        "transfer_artifact",
        "review_memory",
        "connect_memory",
        "adopt_vault",
        "maintain_memory",
        "process_media",
        "query_dataset",
        "read_media",
    ]:
        assert command in loading_section
