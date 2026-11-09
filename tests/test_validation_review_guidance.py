"""A caller can finish an evidence-based preview without recording its assessment."""

import pytest

from exomem import (
    activation_manifest,
    commands,
    semantic_authoring,
    semantic_index,
    vault,
)


def test_preview_review_guidance_reaches_an_agent_without_requiring_a_skill(vault):
    """The bootstrap and the concise contract carry it; the 28 KB tool definitions do not."""
    guidance = semantic_authoring.EXISTING_VALIDATION_GUIDANCE
    bootstrap = commands.op_bootstrap(vault, profile="compact", section="authoring")
    assert bootstrap["semantic_authoring"]["validation_review"] == guidance
    assert guidance in semantic_authoring.render_concise()


@pytest.mark.parametrize("route", ["existing-unit", "append"])
def test_proposed_relation_review_completes_validation_without_persisting_content_or_review(tmp_path, route):
    relative = "Knowledge Base/Notes/Insights/retry-bounds.md"
    page_id = "00000000-0000-4000-8000-000000000085"
    original = (
        "---\ntitle: Retry bounds\ntype: insight\nstatus: active\n"
        f"exomem_id: {page_id}\n---\n\n"
        "# Retry bounds\n\n## Decision\n- id: retry-window\n"
        "- tags: reliability\n- context: client operation\n\n"
        "Use one bounded retry window.\n"
    )
    page = tmp_path / relative
    page.parent.mkdir(parents=True)
    # The page lies past the activation boundary with no relation decision, so a write needs one.
    activation_manifest.ensure_manifest(tmp_path)
    page.write_text(original)
    current = semantic_index.current_parent_index_state(tmp_path, relative, source=original).document.units[0]

    if route == "existing-unit":
        def validate(**review):
            result = commands.op_observe_memory(
                tmp_path, path=relative, operation="validate", kind="decision", category="decision",
                unit_ref=current.unit_ref, expected_fingerprint=current.fingerprint,
                expected_hash=vault.content_hash(original), content="Use one bounded retry window per request.",
                tags=["reliability"], context="client operation", **review,
            )
            assert result["unit"]["content"] == "Use one bounded retry window per request."
            assert result["unit"]["unit_ref"] == current.unit_ref
            assert result["unit"]["tags"] == ["reliability"]
            assert result["unit"]["context"] == "client operation"
            return result["semantic"]

    else:
        appended = "\n## Maintenance\n\nKeep the retry window visible in diagnostics.\n"
        proposed = original + appended

        def validate(**review):
            return commands.op_manage_memory_file(
                tmp_path, path=relative, operation="append", content=appended,
                allow_curated=True, validate_only=True, **review,
            )

    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    first = validate()
    assert first["contract_result"]["should_block"] is True
    reviewed = validate(
        relation_disposition="reviewed_none",
        relation_review_reason="The complete corpus has this single page and no distinct relation target.",
    )
    assert reviewed["mutated"] is False
    assert reviewed["contract_result"]["should_block"] is False
    if route == "append":
        assert reviewed["after_hash"] == vault.content_hash(proposed)
    assert {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
