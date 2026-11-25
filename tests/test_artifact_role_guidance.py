from exomem import commands, workflow_skills


def test_bootstrap_teaches_role_state_authority_and_coverage(vault):
    payload = commands.op_bootstrap(vault, profile="compact", section="all")
    import json

    serialized = json.dumps(payload)
    assert "artifact_role_state_handling" in serialized
    assert "supporting units" in serialized
    assert "tool-free" in serialized
    assert "compact omission" in serialized


def test_shipped_review_and_capture_teach_role_first_homes_and_local_authority():
    reference = workflow_skills.WORKFLOW_SKILLS_DIR.parent / "references" / "audit-checks.md"
    rules = " ".join(reference.read_text().split("## Artifact role and transient state", 1)[1].split("\n## ", 1)[0].split())
    for phrase in ("artifact_role_promotion", "transient_state_review", "restructure_execution",
                   "already-authorized local", "tool-free", "Records", "exact source-unit"):
        assert phrase in rules, phrase
    for name in ("evermatter-review", "evermatter-capture"):
        text = (workflow_skills.WORKFLOW_SKILLS_DIR / name / "SKILL.md").read_text()
        assert "references/audit-checks.md#artifact-role-and-transient-state" in text
