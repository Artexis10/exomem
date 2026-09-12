from exomem import commands, workflow_skills


def test_bootstrap_teaches_role_state_authority_and_coverage(vault):
    payload = commands.op_bootstrap(vault, profile="compact")
    import json

    serialized = json.dumps(payload)
    assert "artifact_role_state_handling" in serialized
    assert "supporting units" in serialized
    assert "tool-free" in serialized
    assert "compact omission" in serialized


def test_shipped_review_and_capture_teach_role_first_homes_and_local_authority():
    for name in ("exomem-review", "exomem-capture"):
        text = (workflow_skills.WORKFLOW_SKILLS_DIR / name / "SKILL.md").read_text()
        assert "artifact_role_promotion" in text
        assert "transient_state_review" in text
        assert "restructure_execution" in text
        assert "already-authorized local" in text
        assert "tool-free" in text
        assert "Records" in text
        assert "exact source-unit" in text
