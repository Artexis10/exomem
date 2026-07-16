"""Acceptance coverage for typed posthoc semantic audit routing."""

from __future__ import annotations

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
    semantic_writes,
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
