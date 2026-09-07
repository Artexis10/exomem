from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONTRIBUTION = (
    ROOT / "tests" / "fixtures" / "hosted_v5_contributions" / "artifact_adoption.json"
)


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8").lower()


def _active_capture_carriers(vault: Path) -> dict[str, str]:
    from exomem import commands, prominence
    from exomem._hooks import exomem_capture_nudge

    return {
        "bootstrap": json.dumps(commands.op_bootstrap(vault), ensure_ascii=False).lower(),
        "prominence-balanced": prominence.CONTRACTS["balanced"].capture.lower(),
        "prominence-maximal": prominence.CONTRACTS["maximal"].capture.lower(),
        # The router delegates the engagement rules to this reference, so it is
        # the carrier that must teach adoption in each distribution.
        "scaffold": _text("src/exomem/_scaffold/_Schema/references/engagement.md"),
        "capture-hook": exomem_capture_nudge.REMINDER.lower(),
        "plugin-skill": _text("plugins/claude-code/skills/exomem/references/engagement.md"),
        "plugin-hook": _text("plugins/claude-code/hooks/exomem_capture_nudge.py"),
    }


def test_every_active_non_hosted_capture_carrier_teaches_draft_to_adoption(vault: Path) -> None:
    for name, carrier in _active_capture_carriers(vault).items():
        assert "generated draft" in carrier, name
        assert "selected" in carrier or "selection" in carrier, name
        assert "exact" in carrier and "bytes" in carrier, name
        assert "source" in carrier and "evidence" in carrier, name
        assert "mime" in carrier, name
        assert "no handle" in carrier or "without a handle" in carrier, name
        assert "handoff" in carrier and "non-committing" in carrier, name
        assert "delivery" in carrier and "record" in carrier, name
        assert "remote byte" in carrier, name


def test_every_active_non_hosted_capture_carrier_maps_adoption_authority(vault: Path) -> None:
    for name, carrier in _active_capture_carriers(vault).items():
        assert "proactive_capture" in carrier, name
        assert "structural_suggestions" in carrier, name
        assert "restructure_execution" in carrier, name
        assert "link_acceptance" in carrier, name
        assert "not write consent" in carrier or "not write confirmation" in carrier, name


def test_off_and_light_remain_explicit_request_only() -> None:
    from exomem import prominence

    for level in ("off", "light"):
        capture = prominence.CONTRACTS[level].capture.lower()
        assert ("only when" in capture or "unless" in capture) and "asks" in capture
        assert "generated draft" not in capture
        assert "proactive_capture" not in capture


def test_scaffold_and_deployed_claude_code_copies_match() -> None:
    assert (ROOT / "src/exomem/_scaffold/_Schema/SKILL.md").read_bytes() == (
        ROOT / "plugins/claude-code/skills/exomem/SKILL.md"
    ).read_bytes()
    for reference in ("operations.md", "write-scope.md"):
        assert (
            ROOT / "src/exomem/_scaffold/_Schema/references" / reference
        ).read_bytes() == (
            ROOT / "plugins/claude-code/skills/exomem/references" / reference
        ).read_bytes()
    assert (ROOT / "src/exomem/_hooks/exomem_capture_nudge.py").read_bytes() == (
        ROOT / "plugins/claude-code/hooks/exomem_capture_nudge.py"
    ).read_bytes()


def test_public_guides_teach_exact_adoption_and_honest_handoff() -> None:
    for path in ("QUICKSTART.md", "docs/ai-assistant-guide.md", "docs/prominence.md"):
        text = _text(path)
        assert "generated draft" in text, path
        assert "exact" in text and "bytes" in text, path
        assert "non-committing" in text and "handoff" in text, path
        assert "proactive_capture" in text, path


def _validate_hosted_contribution(document: dict) -> None:
    assert set(document) == {
        "schema_version",
        "family",
        "required_commands",
        "withheld_commands",
        "authority_context",
        "traces",
        "compatibility",
    }
    assert document["schema_version"] == 1
    assert document["family"] == "generated-artifact-adoption"
    assert document["required_commands"] == [
        "capture_source",
        "preserve_artifacts",
        "record_memory",
    ]
    assert document["withheld_commands"] == ["transfer_artifact"]
    assert document["authority_context"] == {
        "proactive_capture": "silent",
        "link_acceptance": "confirm-shortcut",
        "structural_suggestions": "advisory",
        "restructure_execution": "confirm",
    }
    traces = {trace["id"]: trace for trace in document["traces"]}
    assert list(traces) == [
        "direct-source-adoption",
        "direct-evidence-adoption",
        "unselected-drafts-stay-ephemeral",
        "no-handle-handoff-is-honest",
        "receipt-linked-reported-delivery",
        "reported-remote-reference-is-not-byte-proof",
    ]

    expected_routes = {
        "direct-source-adoption": ("reasoning-input", "capture_source", None, "source"),
        "direct-evidence-adoption": (
            "approved-deliverable",
            "preserve_artifacts",
            None,
            "evidence",
        ),
    }
    for trace_id, (role, tool, operation, lane) in expected_routes.items():
        trace = traces[trace_id]
        assert trace["starting_context"]["direct_handles"] is True
        assert trace["starting_context"]["semantic_role"] == role
        assert trace["expected"]["authority"] == {
            "action_class": "proactive_capture",
            "disposition": "silent",
            "selection_is_write_confirmation": False,
        }
        [call] = trace["expected"]["tool_sequence"]
        assert (call["tool"], call["operation"]) == (tool, operation)
        adoption = call["arguments"]["adoption"]
        selected = trace["starting_context"]["selected_file_id"]
        assert selected == trace["expected"]["selected_file_id"]
        assert selected == adoption["selected_file_id"]
        assert selected in trace["offered_file_ids"]
        assert len(trace["expected"]["expected_writes"]) == 1
        [write] = trace["expected"]["expected_writes"]
        assert write == {
            "kind": f"{lane}-artifact-with-adoption-receipt",
            "file_id": selected,
            "exact_bytes": True,
        }
        assert set(trace["expected"]["forbidden_write_file_ids"]) == (
            set(trace["offered_file_ids"]) - {selected}
        )
        assert trace["expected"]["terminal"] == {
            "committed_receipt_required_for_success": True,
            "lane": lane,
        }

    drafts = traces["unselected-drafts-stay-ephemeral"]
    assert drafts["starting_context"]["selected_file_id"] is None
    assert drafts["expected"]["tool_sequence"] == []
    assert drafts["expected"]["expected_writes"] == []
    assert set(drafts["expected"]["forbidden_write_file_ids"]) == set(
        drafts["offered_file_ids"]
    )

    handoff = traces["no-handle-handoff-is-honest"]
    assert handoff["starting_context"]["direct_handles"] is False
    assert handoff["expected"]["tool_sequence"] == []
    assert handoff["expected"]["expected_writes"] == []
    assert handoff["expected"]["terminal"]["handoff_status"] == "handoff_required"
    assert handoff["expected"]["terminal"]["committed"] is False
    assert "transfer_artifact" in handoff["expected"]["forbidden_tools"]
    assert {"saved", "stored", "uploaded", "adopted", "delivered"} <= set(
        handoff["expected"]["terminal"]["forbidden_claims"]
    )

    for trace_id in (
        "receipt-linked-reported-delivery",
        "reported-remote-reference-is-not-byte-proof",
    ):
        trace = traces[trace_id]
        [call] = trace["expected"]["tool_sequence"]
        assert (call["tool"], call["operation"], call["arguments"]["action"]) == (
            "record_memory",
            "append",
            "append",
        )
        delivery = call["arguments"]["delivery"]
        assert delivery["evidence_page"] == trace["starting_context"]["evidence_page"]
        assert delivery["link_field"] == trace["starting_context"]["declared_link_field"]
        assert "platform_proof" not in delivery
        assert trace["expected"]["authority"] == {
            "action_class": "proactive_capture",
            "disposition": "silent",
            "selection_is_write_confirmation": False,
        }
        prerequisites = trace["expected"]["prerequisites"]
        assert prerequisites["committed_local_adoption_receipt"] is True
        assert prerequisites["receipt_lane"] == "evidence"
        assert prerequisites["existing_compatible_collection"] is True
        assert prerequisites["declared_link_field"] is True
        assert prerequisites["implicit_collection_creation"] is False
        [record] = trace["expected"]["expected_writes"]
        assert record["kind"] == "record"
        assert record["links_evidence_page"] is True
        assert record["reported_remote_ref"] == delivery["reported_remote_ref"]
        assert record["verified_remote_identity"] is False

    remote = traces["reported-remote-reference-is-not-byte-proof"]
    assert remote["starting_context"]["platform_byte_proof"] is False
    assert remote["expected"]["prerequisites"]["matching_platform_digest"] is False
    assert remote["expected"]["terminal"]["remote_byte_identity_verified"] is False
    assert document["compatibility"] == {
        "hosted_v1_v4": "immutable",
        "hosted_v5": "candidate-owner-validates-canonicalises-and-freezes-this-input",
    }


def test_hosted_v5_artifact_contribution_is_generic_and_complete() -> None:
    document = json.loads(CONTRIBUTION.read_text(encoding="utf-8"))
    _validate_hosted_contribution(document)
    lowered = json.dumps(document, ensure_ascii=False).lower()
    assert "synthetic" in lowered
    assert "facebook" not in lowered


@pytest.mark.parametrize(
    "mutant",
    [
        "draft-preserved",
        "selection-is-consent",
        "mime-chooses-lane",
        "handoff-is-committed",
        "receipt-free-delivery",
        "implicit-collection",
        "remote-equality-inferred",
        "authority-mapping-removed",
        "hosted-history-drift",
    ],
)
def test_hosted_contribution_validator_kills_boundary_mutants(mutant: str) -> None:
    document = copy.deepcopy(json.loads(CONTRIBUTION.read_text(encoding="utf-8")))
    traces = {trace["id"]: trace for trace in document["traces"]}
    if mutant == "draft-preserved":
        traces["direct-evidence-adoption"]["expected"]["expected_writes"].append(
            {
                "kind": "evidence-artifact-with-adoption-receipt",
                "file_id": "report-draft",
                "exact_bytes": True,
            }
        )
    elif mutant == "selection-is-consent":
        traces["direct-source-adoption"]["expected"]["authority"][
            "selection_is_write_confirmation"
        ] = True
    elif mutant == "mime-chooses-lane":
        traces["direct-source-adoption"]["starting_context"]["semantic_role"] = "image/png"
    elif mutant == "handoff-is-committed":
        traces["no-handle-handoff-is-honest"]["expected"]["terminal"]["committed"] = True
    elif mutant == "receipt-free-delivery":
        traces["receipt-linked-reported-delivery"]["expected"]["prerequisites"][
            "committed_local_adoption_receipt"
        ] = False
    elif mutant == "implicit-collection":
        traces["receipt-linked-reported-delivery"]["expected"]["prerequisites"][
            "implicit_collection_creation"
        ] = True
    elif mutant == "remote-equality-inferred":
        traces["reported-remote-reference-is-not-byte-proof"]["expected"][
            "expected_writes"
        ][0]["verified_remote_identity"] = True
    elif mutant == "authority-mapping-removed":
        document["authority_context"].pop("structural_suggestions")
    elif mutant == "hosted-history-drift":
        document["compatibility"]["hosted_v1_v4"] = "regenerated"

    with pytest.raises(AssertionError):
        _validate_hosted_contribution(document)
