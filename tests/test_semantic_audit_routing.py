"""Acceptance coverage for typed posthoc semantic audit routing."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import (
    attention as attention_module,
)
from exomem import (
    audit as audit_module,
)
from exomem import (
    commands,
    semantic_writes,
)
from exomem import (
    server as server_module,
)

TYPED_SEMANTIC_CATEGORIES = (
    "semantic_malformed_unit",
    "semantic_category_governance",
    "semantic_strict_schema_drift",
    "semantic_relation_disposition",
)


def _finding(
    *,
    code: str,
    governed_element_identity: list[str],
    resolved_rule: list[str],
    relation_kind: str = "missing",
) -> dict:
    return {
        "path": "Knowledge Base/Notes/Insights/direct-edit.md",
        "code": code,
        "severity": "error",
        "governed_element_identity": governed_element_identity,
        "resolved_rule": resolved_rule,
        "relation_disposition": {
            "kind": relation_kind,
            "satisfied": False,
            "current": False,
        },
        "actions": ["review_semantic_contract"],
        "activation": "current",
        "grandfathered": True,
    }


def _projection(findings: list[dict]) -> dict:
    return {
        "operation": "audit",
        "activation": "current",
        "evaluated_paths": ["Knowledge Base/Notes/Insights/direct-edit.md"],
        "semantic_contract_findings": findings,
        "semantic_contract_summary": {
            item["code"]: 1 for item in findings
        },
        "omitted_counts": {
            "evaluated_paths": 0,
            "semantic_contract_findings": 0,
            "semantic_contract_summary": 0,
        },
        "truncation": {
            "byte_budget": 120 * 1024,
            "finding_limit": 256,
            "path_limit": 512,
            "summary_limit": 256,
            "strings_truncated": 0,
            "string_bytes_omitted": 0,
            "nested_items_omitted": 0,
            "budget_items_omitted": 0,
        },
    }


def _audit_finding(
    path: str,
    *,
    category: str,
    code: str,
    severity: str = "error",
    grandfathered: bool = False,
) -> audit_module.AuditFinding:
    return audit_module.AuditFinding(
        category=category,
        severity=severity,
        path=path,
        detail=f"{code} requires review.",
        proposed_fix="Review it.",
        meta={
            "code": code,
            "activation": "current",
            "grandfathered": grandfathered,
            "finding_key": {
                "code": code,
                "governed_element_identity": ["semantic", path],
                "resolved_rule": ["relations", "*", "disposition"],
            },
        },
    )


def test_action_first_projection_groups_legacy_backlog_without_mutating_raw_report() -> None:
    legacy = [
        _audit_finding(
            f"Knowledge Base/Notes/Insights/legacy-{suffix}.md",
            category="semantic_relation_disposition",
            code="RELATION_DISPOSITION_MISSING",
            grandfathered=True,
        )
        for suffix in ("d", "b", "a", "c")
    ]
    blocker = _audit_finding(
        "Knowledge Base/Notes/Insights/blocker.md",
        category="semantic_strict_schema_drift",
        code="CONTRACT_REQUIRED_FIELD",
    )
    malformed = _audit_finding(
        "Knowledge Base/Notes/Insights/malformed.md",
        category="semantic_malformed_unit",
        code="invalid_compact_category",
        severity="warn",
    )
    unregistered_relation = audit_module.AuditFinding(
        category="relation_registry",
        severity="warning",
        path="Knowledge Base/Notes/Insights/unregistered-relation.md",
        detail="Relation type is not registered.",
        meta={"code": "CONTRACT_UNKNOWN_RELATION"},
    )
    ordinary = audit_module.AuditFinding(
        category="broken_wikilink",
        severity="warn",
        path="Knowledge Base/Notes/Insights/ordinary.md",
        detail="Broken link.",
    )
    report = audit_module.AuditReport(
        findings=[ordinary, *legacy, unregistered_relation, malformed, blocker],
        summary={
            "broken_wikilink": 1,
            "relation_registry": 1,
            "semantic_malformed_unit": 1,
            "semantic_relation_disposition": 7,
            "semantic_strict_schema_drift": 1,
        },
        metadata={
            "semantic_contract_drift": {
                "semantic_contract_summary": {
                    "CONTRACT_REQUIRED_FIELD": 1,
                    "RELATION_DISPOSITION_MISSING": 7,
                    "invalid_compact_category": 1,
                },
                "omitted_counts": {
                    "evaluated_paths": 0,
                    "semantic_contract_findings": 3,
                    "semantic_contract_summary": 0,
                },
                "truncation": {
                    "observation_complete": True,
                    "findings_complete": False,
                    "budget_items_omitted": 0,
                },
            }
        },
    )
    raw_before = report.as_dict()

    public = report.as_public_dict(detail="actionable", legacy_sample_limit=2)

    assert [item["path"] for item in public["findings"]] == [
        blocker.path,
        malformed.path,
        unregistered_relation.path,
        ordinary.path,
    ]
    assert public["summary"] == report.summary
    assert public["legacy_backlog"] == {
        "code": "RELATION_DISPOSITION_MISSING",
        "severity": "info",
        "kind": "legacy_backlog",
        "observed_count": 7,
        "observed_complete": True,
        "available_sample_count": 4,
        "sample_limit": 2,
        "sample_omitted_count": 5,
        "upstream_findings_truncated": True,
        "upstream_omitted_count": 3,
        "samples": public["legacy_backlog"]["samples"],
    }
    assert [item["path"] for item in public["legacy_backlog"]["samples"]] == [
        "Knowledge Base/Notes/Insights/legacy-a.md",
        "Knowledge Base/Notes/Insights/legacy-b.md",
    ]
    assert all(
        item["severity"] == "info"
        and item["raw_severity"] == "error"
        and item["presentation"] == "legacy_backlog"
        for item in public["legacy_backlog"]["samples"]
    )
    assert report.as_dict() == raw_before
    assert all(item.severity == "error" for item in legacy)

    full = report.as_public_dict(detail="full", legacy_sample_limit=1)
    assert "legacy_backlog" not in full
    assert full["findings"] == raw_before["findings"]
    assert full["presentation"] == {
        "grouped_legacy_backlog": False,
        "upstream_findings_complete": False,
        "upstream_omitted_count": 3,
    }


def _posthoc_evaluation(
    path: str,
    *,
    code: str,
    resolved_rule: tuple[str, str, str],
    grandfathered: bool,
) -> semantic_writes.PosthocPageEvaluation:
    finding = SimpleNamespace(
        code=code,
        severity="error",
        governed_element_identity=("semantic", path),
        resolved_rule=resolved_rule,
    )
    result = SimpleNamespace(
        relation_disposition=None,
        findings=(finding,),
        actions=("review_semantic_contract",),
    )
    return semantic_writes.PosthocPageEvaluation(
        path,
        result,
        grandfathered,
        "current",
    )


def test_public_summary_uses_complete_semantic_category_counts_without_changing_raw(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_count = semantic_writes._POSTHOC_FINDING_LIMIT + 8
    evaluations = [
        _posthoc_evaluation(
            f"Knowledge Base/Notes/Insights/legacy-{index:04d}.md",
            code="RELATION_DISPOSITION_MISSING",
            resolved_rule=("relations", "*", "disposition"),
            grandfathered=True,
        )
        for index in range(legacy_count)
    ]
    evaluations.append(
        _posthoc_evaluation(
            "Knowledge Base/Notes/Insights/current-blocker.md",
            code="CONTRACT_REQUIRED_FIELD",
            resolved_rule=("fields", "owner", "required"),
            grandfathered=False,
        )
    )
    posthoc = semantic_writes.PosthocBatch(
        "audit", "current", tuple(evaluations)
    )
    monkeypatch.setattr(
        semantic_writes,
        "evaluate_posthoc_batch",
        lambda *_args, **_kwargs: posthoc,
    )
    categories = [
        "semantic_contract_drift",
        "semantic_relation_disposition",
        "semantic_strict_schema_drift",
    ]

    report = audit_module.audit(tmp_path, categories=categories)
    raw_before = report.as_dict()
    public = report.as_public_dict()

    assert raw_before["summary"]["semantic_contract_drift"] < legacy_count + 1
    assert public["summary"] == {
        "semantic_contract_drift": legacy_count + 1,
        "semantic_relation_disposition": legacy_count,
        "semantic_strict_schema_drift": 1,
    }
    assert public["legacy_backlog"]["observed_count"] == legacy_count
    assert report.as_dict() == raw_before

    full_report = audit_module.audit(
        tmp_path,
        categories=categories,
        semantic_detail="full",
    )
    full = full_report.as_public_dict(detail="full")

    assert full["summary"] == public["summary"]
    assert full["metadata"]["semantic_contract_drift"]["omitted_counts"][
        "semantic_contract_findings"
    ] == 0


@pytest.mark.parametrize("route", ["audit", "review", "maintain"])
def test_public_audit_routes_forward_detail_and_sample_control(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    calls: list[str] = []
    findings = [
        _audit_finding(
            f"Knowledge Base/Notes/Insights/legacy-{index}.md",
            category="semantic_relation_disposition",
            code="RELATION_DISPOSITION_MISSING",
            grandfathered=True,
        )
        for index in range(3)
    ]

    def fake_audit(*_args, semantic_detail: str = "actionable", **_kwargs):
        calls.append(semantic_detail)
        return audit_module.AuditReport(
            findings=findings,
            summary={"semantic_relation_disposition": 3},
            metadata={
                "semantic_contract_drift": {
                    "semantic_contract_summary": {
                        "RELATION_DISPOSITION_MISSING": 3
                    },
                    "omitted_counts": {"semantic_contract_findings": 0},
                    "truncation": {
                        "observation_complete": True,
                        "findings_complete": True,
                    },
                }
            },
        )

    monkeypatch.setattr(audit_module, "audit", fake_audit)
    kwargs = {"detail": "actionable", "legacy_sample_limit": 1}
    if route == "audit":
        actionable = commands.op_audit(tmp_path, **kwargs)
        full = commands.op_audit(tmp_path, detail="full", legacy_sample_limit=1)
    elif route == "review":
        actionable = commands.op_review_memory(tmp_path, mode="audit", **kwargs)
        full = commands.op_review_memory(
            tmp_path, mode="audit", detail="full", legacy_sample_limit=1
        )
    else:
        actionable = commands.op_maintain_memory(tmp_path, mode="audit", **kwargs)
        full = commands.op_maintain_memory(
            tmp_path, mode="audit", detail="full", legacy_sample_limit=1
        )

    assert len(actionable["legacy_backlog"]["samples"]) == 1
    assert "legacy_backlog" not in full
    assert len(full["findings"]) == 3
    assert calls == ["actionable", "full"]


@pytest.mark.parametrize("route", ["audit", "review", "maintain"])
def test_public_audit_routes_reject_invalid_presentation_controls(
    tmp_path: Path,
    route: str,
) -> None:
    def call(**kwargs):
        if route == "audit":
            return commands.op_audit(tmp_path, **kwargs)
        if route == "review":
            return commands.op_review_memory(tmp_path, mode="audit", **kwargs)
        return commands.op_maintain_memory(tmp_path, mode="audit", **kwargs)

    with pytest.raises(
        ValueError,
        match="^INVALID_AUDIT_DETAIL: detail must be 'actionable' or 'full'$",
    ):
        call(detail="verbose")
    for invalid_sample in (-1, 51, True, "2"):
        with pytest.raises(
            ValueError,
            match=(
                "^INVALID_AUDIT_SAMPLE_LIMIT: legacy_sample_limit must be an "
                "integer from 0 to 50$"
            ),
        ):
            call(legacy_sample_limit=invalid_sample)


def test_audit_command_surfaces_advertise_presentation_controls() -> None:
    for surface in ("mcp", "rest", "cli"):
        audit = next(
            item for item in commands.commands_for(surface) if item.name == "audit"
        )
        products = {
            item.name: item for item in commands.product_commands_for(surface)
        }

        for command in (
            audit,
            products["review_memory"],
            products["maintain_memory"],
        ):
            params = {item.name: item for item in command.params}
            assert params["detail"].choices == ("actionable", "full")
            assert params["legacy_sample_limit"].type == "int"
            assert params["legacy_sample_limit"].required is False


def test_mcp_audit_routes_reject_boolean_sample_limits_before_leaf_execution(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    def fake_audit(*_args, **_kwargs):
        calls.append(1)
        return audit_module.AuditReport(findings=[], summary={})

    monkeypatch.setattr(audit_module, "audit", fake_audit)
    monkeypatch.setenv("EXOMEM_MCP_LEGACY_COMPAT", "1")
    mcp = server_module.build_server(require_auth=False)
    schemas = {
        tool.name: tool.to_mcp_tool().model_dump(mode="json")["inputSchema"]
        for tool in asyncio.run(mcp.list_tools(run_middleware=False))
    }
    routes = (
        ("audit", {"categories": ["broken_wikilink"]}),
        (
            "review_memory",
            {"mode": "audit", "categories": ["broken_wikilink"]},
        ),
        (
            "maintain_memory",
            {"mode": "audit", "categories": ["broken_wikilink"]},
        ),
    )

    for name, arguments in routes:
        sample_schema = schemas[name]["properties"]["legacy_sample_limit"]
        assert sample_schema["type"] == "integer"
        assert sample_schema["minimum"] == 0
        assert sample_schema["maximum"] == audit_module.MAX_LEGACY_SAMPLE_LIMIT
        for invalid in (False, True):
            with pytest.raises(Exception, match="legacy_sample_limit"):
                asyncio.run(
                    mcp.call_tool(
                        name,
                        {**arguments, "legacy_sample_limit": invalid},
                        run_middleware=False,
                    )
                )

    assert calls == []


def test_posthoc_semantic_findings_have_typed_audit_routing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Posthoc contract drift must be exposed as typed, repairable review work."""
    page = tmp_path / "Knowledge Base/Notes/Insights/direct-edit.md"
    page.parent.mkdir(parents=True)
    page.write_text("# Direct edit\n", encoding="utf-8")
    current_findings = [
        _finding(
            code="invalid_compact_category",
            governed_element_identity=["syntax", "invalid:1"],
            resolved_rule=["semantic_units", "*", "syntax"],
        ),
        _finding(
            code="alias_conflict",
            governed_element_identity=["categories", "operations"],
            resolved_rule=["categories", "operations", "registry"],
        ),
        _finding(
            code="CONTRACT_REQUIRED_FIELD",
            governed_element_identity=["fields", "owner"],
            resolved_rule=["fields", "owner", "required"],
        ),
        _finding(
            code="RELATION_DISPOSITION_STALE",
            governed_element_identity=["relations", "disposition"],
            resolved_rule=["relations", "*", "disposition"],
            relation_kind="stale",
        ),
    ]
    evaluations: list[tuple[str, ...]] = []

    def evaluate(*_args, **_kwargs):
        evaluations.append(tuple(item["code"] for item in current_findings))
        return SimpleNamespace(as_dict=lambda: _projection(current_findings))

    monkeypatch.setattr(semantic_writes, "evaluate_posthoc_batch", evaluate)

    report = audit_module.audit(
        tmp_path,
        categories=list(TYPED_SEMANTIC_CATEGORIES),
    )

    assert [finding.category for finding in report.findings] == list(
        TYPED_SEMANTIC_CATEGORIES
    )
    assert len(evaluations) == 1
    assert [finding.meta["finding_key"] for finding in report.findings] == [
        {
            "code": item["code"],
            "governed_element_identity": item["governed_element_identity"],
            "resolved_rule": item["resolved_rule"],
        }
        for item in current_findings
    ]
    assert all(finding.meta["activation"] == "current" for finding in report.findings)
    assert all(finding.meta["grandfathered"] is True for finding in report.findings)

    filtered = audit_module.audit(
        tmp_path,
        categories=["semantic_strict_schema_drift"],
    )
    assert [finding.meta["code"] for finding in filtered.findings] == [
        "CONTRACT_REQUIRED_FIELD"
    ]

    legacy = audit_module.audit(
        tmp_path,
        categories=["semantic_contract_drift"],
    )
    assert {finding.category for finding in legacy.findings} == {
        "semantic_contract_drift"
    }
    assert [finding.meta["finding_key"] for finding in legacy.findings] == [
        finding.meta["finding_key"] for finding in report.findings
    ]

    first = attention_module.attention(
        tmp_path,
        categories=list(TYPED_SEMANTIC_CATEGORIES),
        limit=0,
        state="all",
    )
    second = attention_module.attention(
        tmp_path,
        categories=list(TYPED_SEMANTIC_CATEGORIES),
        limit=0,
        state="all",
    )
    assert len(first.items) == 1
    assert first.items[0].categories == list(TYPED_SEMANTIC_CATEGORIES)
    assert first.items[0].ref == second.items[0].ref
    assert first.items[0].fingerprint == second.items[0].fingerprint
    assert [reason["meta"]["finding_key"] for reason in first.items[0].reasons] == [
        finding.meta["finding_key"] for finding in report.findings
    ]

    current_findings.clear()
    repaired = audit_module.audit(
        tmp_path,
        categories=list(TYPED_SEMANTIC_CATEGORIES),
    )
    repaired_attention = attention_module.attention(
        tmp_path,
        categories=list(TYPED_SEMANTIC_CATEGORIES),
        limit=0,
        state="all",
    )
    assert repaired.findings == []
    assert repaired_attention.items == []
