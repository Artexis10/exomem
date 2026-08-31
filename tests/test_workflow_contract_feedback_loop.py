"""Workflow contracts project a bounded Planning/Records agent protocol.

The service resolves authored policy.  It does not classify conversations,
choose a collection, or transition a plan; those remain guarded agent actions.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import initialize_vault_state_offline


def _proposal(**overrides: object) -> dict[str, object]:
    proposal: dict[str, object] = {
        "type": "workflow-contract",
        "contract_id": "d2df7e34-b1c0-4dd8-8a5b-3b3db9b9f79f",
        "schema_version": 1,
        "key": "delivery-feedback",
        "title": "Delivery Feedback",
        "lifecycle": "active",
        "scope": {"projects": ["delivery"], "domains": [], "activities": []},
        "planning": {"mode": "companion"},
        "companions": [
            {
                "key": "external-tracker",
                "name": "External Tracker",
                "owns": ["software.acceptance-tasks", "software.requirements"],
            }
        ],
        "capture": {"durable_intent": "proactive", "observed_outcomes": "explicit"},
        "planning_transition": "propose-after-outcome",
    }
    proposal.update(overrides)
    return proposal


def test_agent_protocol_is_code_owned_and_shared_by_builtin_saved_ephemeral_and_bootstrap(
    tmp_path: Path,
) -> None:
    from exomem import commands, workflow_contracts
    from exomem.init import init_vault

    init_vault(tmp_path)
    initialize_vault_state_offline(tmp_path, source="workflow feedback protocol")
    contract = workflow_contracts.parse_proposal(_proposal())
    workflow_contracts.save_contract(tmp_path, contract, why="reviewed workflow policy")

    portable = workflow_contracts.portable_projection()
    protocol = portable["agent_protocol"]
    builtin = workflow_contracts.resolve_contracts(tmp_path, {}, name="@standalone")
    saved = workflow_contracts.resolve_contracts(tmp_path, {"project": "delivery"})
    ephemeral = workflow_contracts.resolve_contracts(
        tmp_path, {}, proposal=_proposal(key="session-feedback")
    )
    bootstrap = commands.op_bootstrap(tmp_path, profile="compact")["workflow_contracts"]

    assert protocol == {
        "version": 1,
        "intent": {
            "explicit": "route",
            "proactive": {
                "requires": ["active-prominence", "durable-intent"],
                "excludes": ["tentative"],
            },
            "planning": ["inspect", "update-one-unambiguous", "create-if-none", "ask-if-ambiguous"],
            "context": {"missing": "unknown", "null": "known-absent"},
            "standalone": "complete-durable-hierarchy",
            "companion": "opaque-execution-references-only",
        },
        "outcomes": {
            "explicit": "route",
            "proactive": {
                "requires": ["active-prominence", "identified-outcome"],
            },
            "records": ["inspect", "append-one-compatible", "propose-if-none", "ask-if-ambiguous"],
            "references": "opaque-bounded",
            "planning_reference": {
                "unambiguous": "link-opaque",
                "absent": "record-without-plan",
                "ambiguous": "no-link-surface-review",
            },
            "transition": {
                "explicit-only": "explicit-user-transition-only",
                "propose-after-outcome": "propose-review-only",
                "automatic": "forbidden",
            },
        },
        "review": {
            "surfaces": ["plan-progress", "unreflected-outcomes"],
            "mode": "deterministic-read-only",
            "completion-inference": "forbidden",
        },
        "service": {
            "conversation-classification": "agent-supplied-facts-only",
            "companion-calls": "forbidden",
            "external-state-inference": "forbidden",
        },
    }
    assert builtin["agent_protocol"] == protocol
    assert saved["agent_protocol"] == protocol
    assert ephemeral["agent_protocol"] == protocol
    assert bootstrap["agent_protocol"] == {
        "version": 1,
        "active_prominence": "balanced",
        "effective_capture": {
            "explicit": True,
            "proactive": {
                "durable_intent": "durable-intent",
                "observed_outcomes": "sufficiently-identified-outcome",
            },
        },
        "outcomes": {
            "planning_reference": protocol["outcomes"]["planning_reference"],
            "transition": protocol["outcomes"]["transition"],
        },
    }
    assert bootstrap["portable"] == {
        key: portable[key] for key in ("family", "schema_version", "digest")
    }

    assert saved["decision"]["capture"] == {
        "durable_intent": "proactive",
        "observed_outcomes": "explicit",
    }
    assert saved["decision"]["planning_transition"] == "propose-after-outcome"
    assert saved["decision"]["planning"]["mode"] == "companion"
    assert saved["decision"]["companions"][0]["owns"] == [
        "software.acceptance-tasks",
        "software.requirements",
    ]
    assert ephemeral["source"] == "ephemeral"

    saved["agent_protocol"]["intent"]["planning"].append("mutate-call-result")
    assert workflow_contracts.portable_projection()["agent_protocol"] == protocol
    assert (
        workflow_contracts.resolve_contracts(tmp_path, {}, name="@standalone")["agent_protocol"]
        == protocol
    )


def test_workflow_resolution_and_review_surfaces_are_read_only(tmp_path: Path) -> None:
    from lifecycle_fixtures import queue_item, report_event, seed_vault

    from exomem import audit, commands, workflow_contracts

    seed_vault(tmp_path)
    queue_item(tmp_path, "Deliverable")
    report_event(tmp_path, "Deliverable")
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in sorted(tmp_path.rglob("*"))
        if path.is_file()
    }

    workflow_contracts.resolve_contracts(tmp_path, {}, name="@standalone")
    commands.op_review_memory(tmp_path, mode="plan-progress")
    audit.audit(tmp_path, categories=["unreflected_outcomes"])

    after = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in sorted(tmp_path.rglob("*"))
        if path.is_file()
    }
    assert after == before


@pytest.mark.parametrize(
    ("planning_match", "expected"),
    (
        ("unambiguous", "link-opaque"),
        ("absent", "record-without-plan"),
        ("ambiguous", "no-link-surface-review"),
    ),
)
def test_outcome_protocol_handles_present_absent_and_ambiguous_planning_without_guessing(
    planning_match: str, expected: str
) -> None:
    from exomem import workflow_contracts

    references = workflow_contracts.portable_projection()["agent_protocol"]["outcomes"][
        "planning_reference"
    ]
    assert references[planning_match] == expected


@pytest.mark.parametrize("level", ("off", "light", "balanced", "maximal"))
@pytest.mark.parametrize(
    "capture",
    (
        {"durable_intent": "explicit", "observed_outcomes": "explicit"},
        {"durable_intent": "proactive", "observed_outcomes": "proactive"},
    ),
)
def test_every_resolution_projects_the_same_prominence_cap_for_authored_capture_postures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    level: str,
    capture: dict[str, str],
) -> None:
    from exomem import prominence, workflow_contracts
    from exomem.init import init_vault

    monkeypatch.setenv("EXOMEM_PROMINENCE", level)
    init_vault(tmp_path)
    initialize_vault_state_offline(tmp_path, source="workflow effective capture")
    contract = workflow_contracts.parse_proposal(_proposal(capture=capture))
    workflow_contracts.save_contract(tmp_path, contract, why="reviewed workflow policy")

    results = (
        workflow_contracts.resolve_contracts(tmp_path, {}, name="@standalone"),
        workflow_contracts.resolve_contracts(tmp_path, {"project": "delivery"}),
        workflow_contracts.resolve_contracts(
            tmp_path, {}, proposal=_proposal(key="session-effective-capture", capture=capture)
        ),
    )
    for result in results:
        assert "effective_capture_by_prominence" not in result["agent_protocol"]
    assert results[1]["decision"]["capture"] == capture
    assert results[2]["decision"]["capture"] == capture
    effective = prominence.effective_capture(capture, level)
    assert effective["durable_intent"]["proactive_permitted"] is (
        capture["durable_intent"] == "proactive" and level in {"balanced", "maximal"}
    )


@pytest.mark.parametrize(
    ("level", "proactive_permitted"),
    (("off", False), ("light", False), ("balanced", True), ("maximal", True)),
)
def test_bootstrap_projects_the_active_prominence_capture_cap_without_transition_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    level: str,
    proactive_permitted: bool,
) -> None:
    from exomem import commands, prominence

    monkeypatch.setenv("EXOMEM_PROMINENCE", level)
    payload = commands.op_bootstrap(tmp_path, profile="compact")
    effective = payload["engagement"]["contract"]["effective_capture"]

    assert effective == prominence.capture_gate(level)
    assert effective["durable_intent"]["proactive_permitted"] is proactive_permitted
    assert effective["observed_outcomes"]["proactive_permitted"] is proactive_permitted
    assert "record then transition" not in json.dumps(payload).lower()
    capture = payload["engagement"]["contract"]["capture"].lower()
    assert payload["workflow_contracts"]["agent_protocol"]["active_prominence"] == level
    compact_effective = payload["workflow_contracts"]["agent_protocol"]["effective_capture"]
    expected_proactive = {
        "durable_intent": "durable-intent" if proactive_permitted else False,
        "observed_outcomes": (
            "sufficiently-identified-outcome" if proactive_permitted else False
        ),
    }
    assert compact_effective["explicit"] is True
    assert compact_effective["proactive"] == expected_proactive
    if proactive_permitted:
        assert "transition only on explicit user intent" in capture
    else:
        assert "ask" in capture


@pytest.mark.parametrize("level", ("off", "light", "balanced", "maximal"))
@pytest.mark.parametrize(
    "capture",
    (
        {"durable_intent": "explicit", "observed_outcomes": "explicit"},
        {"durable_intent": "proactive", "observed_outcomes": "proactive"},
    ),
)
def test_public_resolve_and_compact_bootstrap_project_active_effective_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    level: str,
    capture: dict[str, str],
) -> None:
    from exomem import commands, prominence, workflow_contracts
    from exomem.init import init_vault

    init_vault(tmp_path)
    initialize_vault_state_offline(tmp_path, source="workflow public effective capture")
    contract = workflow_contracts.parse_proposal(_proposal(capture=capture))
    workflow_contracts.save_contract(tmp_path, contract, why="reviewed workflow policy")
    core_before = workflow_contracts.resolve_contracts(tmp_path, {"project": "delivery"})
    monkeypatch.setenv("EXOMEM_PROMINENCE", level)

    results = (
        commands.op_schema_memory(
            tmp_path,
            subject="workflow-contracts",
            operation="resolve",
            name="@standalone",
            context={},
        ),
        commands.op_schema_memory(
            tmp_path,
            subject="workflow-contracts",
            operation="resolve",
            context={"project": "delivery"},
        ),
        commands.op_schema_memory(
            tmp_path,
            subject="workflow-contracts",
            operation="resolve",
            context={},
            proposal=_proposal(key="session-public-effective", capture=capture),
        ),
    )
    for result in results:
        assert result["active_prominence"] == level
        assert result["effective_capture"] == prominence.effective_capture(
            result["decision"]["capture"], level
        )
        for kind, value in result["effective_capture"].items():
            assert value["explicit_user_request_permitted"] is True
            assert value["proactive_permitted"] is (
                result["decision"]["capture"][kind] == "proactive"
                and level in {"balanced", "maximal"}
            )

    assert workflow_contracts.resolve_contracts(tmp_path, {"project": "delivery"}) == core_before
    compact = commands.op_bootstrap(tmp_path, profile="compact")["workflow_contracts"]
    effective = prominence.effective_capture(compact["builtin_fallback"]["capture"], level)
    assert compact["agent_protocol"] == {
        "version": 1,
        "active_prominence": level,
        "effective_capture": {
            "explicit": True,
            "proactive": {
                kind: (value["proactive_requires"][-1] if value["proactive_permitted"] else False)
                for kind, value in effective.items()
            },
        },
        "outcomes": {
            "planning_reference": {
                "unambiguous": "link-opaque",
                "absent": "record-without-plan",
                "ambiguous": "no-link-surface-review",
            },
            "transition": {
                "explicit-only": "explicit-user-transition-only",
                "propose-after-outcome": "propose-review-only",
                "automatic": "forbidden",
            },
        },
    }
    assert "record then transition" not in json.dumps(compact).lower()
