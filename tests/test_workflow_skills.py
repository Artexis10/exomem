from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
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
    "preserve_artifacts",
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
    # The concise projection carries the v4 identity marker and the complete
    # portable-category teaching; every embedding must therefore be exact.
    assert "exomem-semantic-authoring:v4 " in concise
    assert identity.split(" ", 1)[1] in concise  # content digest
    for expected_fragment in (
        "Core keys are `action`",
        "`techniques` → `technique`",
        "- [decision] Relocate to a coastal city next spring #life ^relocation",
        "- [nutrition] Evening protein improves adherence #experiment ^evening-protein",
        "- [constraint] Keep retry windows bounded #code ^retry-windows",
        "[[Knowledge Base/Notes/Health/Morning training]]",
    ):
        assert expected_fragment in concise

    core = workflow_skills.WORKFLOW_SKILLS_DIR.parent / "SKILL.md"
    core_text = core.read_text(encoding="utf-8")
    assert core_text.count(concise) == 1
    workflow_skills.validate_contract_projection("exomem", core.parent, core=True)
    core_text = core.read_text(encoding="utf-8")
    operating_rules = core_text.split("## Portable operating rules\n", 1)[1].split(
        "\n## ", 1
    )[0].strip()

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
        assert text.count(operating_rules) == 1, (
            f"{name} must carry the core's portable startup/retry rules"
        )
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


def test_skill_contract_stamps_cover_the_canonical_skill_sources(tmp_path: Path) -> None:
    schema = tmp_path / "_Schema"
    shutil.copytree(workflow_skills.WORKFLOW_SKILLS_DIR.parent, schema)
    expected = workflow_skills.skill_contract(schema)

    assert len(expected) == 64
    for _, source in workflow_skills.contract_sources(schema):
        if source.name == "SKILL.md":
            frontmatter = source.read_text(encoding="utf-8").split("\n---\n", 1)[0]
            assert yaml.safe_load(frontmatter.removeprefix("---\n"))["metadata"][
                "skill_contract"
            ] == expected

    for index, (source, old, new) in enumerate((
        (schema / "SKILL.md", "Never recommend", "Always recommend"),
        (schema / "references" / "engagement.md", "bootstrap()", "bootstrap-now()"),
        (
            schema / "workflow-skills" / "exomem-capture" / "SKILL.md",
            "Save durable conclusions",
            "Preserve durable conclusions",
        ),
    )):
        copied = tmp_path / f"changed-{index}"
        shutil.copytree(schema, copied)
        changed = copied / source.relative_to(schema)
        changed.write_text(changed.read_text(encoding="utf-8").replace(old, new, 1), encoding="utf-8")
        assert workflow_skills.skill_contract(copied) != expected
        with pytest.raises(ValueError, match="skill contract stamp"):
            workflow_skills.validate_skill_contract(copied)


def test_skill_contract_normalizes_crlf_and_refreshes_stamps(tmp_path: Path) -> None:
    schema = tmp_path / "_Schema"
    shutil.copytree(workflow_skills.WORKFLOW_SKILLS_DIR.parent, schema)
    expected = workflow_skills.skill_contract(schema)
    core = schema / "SKILL.md"
    core.write_bytes(core.read_bytes().replace(b"\n", b"\r\n"))

    assert workflow_skills.skill_contract(schema) == expected
    workflow_skills.refresh_skill_contract_stamps(schema)
    workflow_skills.validate_skill_contract(schema)


def test_skill_contract_hashes_body_examples_without_rewriting_them(tmp_path: Path) -> None:
    schema = tmp_path / "_Schema"
    shutil.copytree(workflow_skills.WORKFLOW_SKILLS_DIR.parent, schema)
    expected = workflow_skills.skill_contract(schema)
    body_example = "\nContract example:\n  skill_contract: first-version\n"
    core = schema / "SKILL.md"
    reference = schema / "references" / "engagement.md"

    core.write_text(core.read_text(encoding="utf-8") + body_example, encoding="utf-8")
    first_body_digest = workflow_skills.skill_contract(schema)
    assert first_body_digest != expected
    core.write_text(
        core.read_text(encoding="utf-8").replace("first-version", "second-version"),
        encoding="utf-8",
    )
    assert workflow_skills.skill_contract(schema) != first_body_digest
    core.write_text(
        core.read_text(encoding="utf-8").replace(
            f"  skill_contract: {expected}\n", "", 1
        ),
        encoding="utf-8",
    )
    workflow_skills.refresh_skill_contract_stamps(schema)
    assert "  skill_contract: second-version" in core.read_text(encoding="utf-8")

    stamped = workflow_skills.skill_contract(schema)
    reference.write_text(
        reference.read_text(encoding="utf-8") + body_example,
        encoding="utf-8",
    )
    first_reference_digest = workflow_skills.skill_contract(schema)
    assert first_reference_digest != stamped
    reference.write_text(
        reference.read_text(encoding="utf-8").replace("first-version", "second-version"),
        encoding="utf-8",
    )
    assert workflow_skills.skill_contract(schema) != first_reference_digest


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


def test_core_skill_routes_tool_loading_by_current_intent() -> None:
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
        "preserve_artifacts",
        "transfer_artifact",
        "record_memory",
        "review_memory",
        "connect_memory",
        "adopt_vault",
        "maintain_memory",
        "schema_memory",
        "govern_memory",
        "process_media",
        "query_dataset",
        "read_media",
    ]:
        assert command in loading_section
    # Discovery must not turn a lookup into a load of every mutation/media schema.
    assert 'ToolSearch("select:ask_memory")' in loading_section
    assert "Load only" in loading_section
    assert "available_product_tools" in loading_section
    assert "Load the product surface up front" not in loading_section
    assert "select:bootstrap,ask_memory,read_memory,browse_memory,remember" not in text


def test_core_skill_reference_routes_resolve_inside_every_distribution() -> None:
    """Native installs and uploads must carry the router's actual destinations."""
    from exomem import package_skills

    root = workflow_skills.WORKFLOW_SKILLS_DIR.parent
    text = (root / "SKILL.md").read_text(encoding="utf-8")
    targets = set(re.findall(r"\]\((references/[^)#]+\.md)(?:#[^)]*)?\)", text))
    assert targets, "conditional workflows need explicit local routes"
    payload = package_skills._core_payload(None)
    for target in targets:
        assert (root / target).is_file(), target
        assert payload[target] == (root / target).read_text(encoding="utf-8")


def test_shipped_records_guidance_routes_observed_state_without_magic_verbs() -> None:
    """Both generic skill copies teach the same safe Records routing contract."""
    repo_root = Path(__file__).resolve().parents[1]
    skill_copies = (
        workflow_skills.WORKFLOW_SKILLS_DIR.parent / "references" / "planning-records.md",
        repo_root / "plugins" / "claude-code" / "skills" / "exomem" / "references" / "planning-records.md",
    )
    operation_copies = (
        workflow_skills.WORKFLOW_SKILLS_DIR.parent / "references" / "operations.md",
        repo_root
        / "plugins"
        / "claude-code"
        / "skills"
        / "exomem"
        / "references"
        / "operations.md",
    )

    for path in skill_copies + operation_copies:
        text = " ".join(path.read_text(encoding="utf-8").split())
        assert "without waiting for a magic verb" in text
        assert "exactly one compatible existing collection" in text
        assert "propose a concise collection" in text
        assert "must not silently create" in text

    for path in operation_copies:
        text = " ".join(path.read_text(encoding="utf-8").split())
        assert (
            "`describe`, `validate`, `inspect`, and `query`; `create`, `append`, "
            "`update`, `revise`, and `rebaseline`"
        ) in text
