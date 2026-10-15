"""The authority projection separates authored promotion from queued acceptance."""

from pathlib import Path

import pytest

from exomem import commands, vocabulary_authority
from exomem.governance.principal import RequestPrincipal, library_scope, request_scope


@pytest.mark.parametrize(
    ("audience", "resolved", "mode", "registry_class"),
    [
        ("owner", True, "v1", "proactive_capture"),
        ("external", True, "v1", "structural_suggestions"),
        ("external", False, "v1", None),
        ("owner", True, "v2", None),
        ("owner", True, "unavailable", None),
    ],
)
def test_bootstrap_serves_only_the_resolved_v1_callers_promotion_classes(
    vault: Path, monkeypatch: pytest.MonkeyPatch,
    audience: str, resolved: bool, mode: str, registry_class: str | None,
) -> None:
    class Authority:
        def __init__(self, _root):
            pass

        def runtime_status(self):
            return vocabulary_authority.AuthorityStatus(mode, 0, 0)

    monkeypatch.setattr(vocabulary_authority, "VocabularyAuthority", Authority)
    principal = RequestPrincipal(audience_id=audience, resolved=resolved, surface="mcp")
    with request_scope(principal):
        reference = commands.op_bootstrap(vault, profile="compact", section="all")
        core = commands.op_bootstrap(vault, profile="compact")
    envelope = reference["engagement"]["envelope"]
    lifecycle = reference["entity_registry"]["lifecycle"]["authority"]
    if registry_class is None:
        assert "promotion_classes" not in envelope
        assert "promotion_classes" not in core["engagement"]["envelope"]
        assert "relation_outside_curation" not in lifecycle
    else:
        expected = {
            "entity_creation": "proactive_capture", "typed_edge": "proactive_capture",
            "registry_save": registry_class, "queue_acceptance": "link_acceptance",
            "edge_removal": "restructure_execution", "promotion_revert": "restructure_execution",
        }
        assert envelope["promotion_classes"] == core["engagement"]["envelope"]["promotion_classes"] == expected
        assert lifecycle["relation_outside_curation"] == expected["typed_edge"]
    authority = reference["vocabulary_workflow"]["authority"]
    assert authority["contract_version"] == mode
    assert authority["scoped_delegation"] is (mode == "v2")


def test_promotion_map_does_not_lift_queue_or_restructure_ceilings(vault: Path) -> None:
    with library_scope():
        envelope = commands.op_bootstrap(vault, profile="compact")["engagement"]["envelope"]
    assert envelope["classes"]["link_acceptance"]["ceiling"] == "confirm"
    assert envelope["classes"]["restructure_execution"]["ceiling"] == "confirm-required"
    assert "read-only recall follows governance" in envelope["protocol"]
    assert "ordinary writes keep mutation receipts" in envelope["protocol"]
