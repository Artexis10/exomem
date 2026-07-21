from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import (
    activation_manifest,
    file_watcher,
    index_sync,
    memory_schema,
    relation_registry,
    semantic_contract,
    semantic_language_registry,
    semantic_writes,
)
from exomem import (
    audit as audit_module,
)


def _page(
    page_id: str,
    *,
    title: str,
    status: str = "active",
    project: str = "alpha",
    body: str = "- [config] Session duration is fixed.",
) -> str:
    return (
        "---\n"
        f"title: {title}\n"
        "type: insight\n"
        f"status: {status}\n"
        f"project: {project}\n"
        f"exomem_id: {page_id}\n"
        "---\n\n"
        f"# {title}\n\n{body}\n\n"
        "## Relations\n"
    )


def _write(root: Path, rel: str, source: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def test_posthoc_batch_loads_shared_state_once_and_resolves_each_scope_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = [
        _write(
            tmp_path,
            "Knowledge Base/Notes/Insights/alpha-one.md",
            _page("00000000-0000-4000-8000-000000000101", title="Alpha one"),
        ),
        _write(
            tmp_path,
            "Knowledge Base/Notes/Insights/alpha-two.md",
            _page("00000000-0000-4000-8000-000000000102", title="Alpha two"),
        ),
        _write(
            tmp_path,
            "Knowledge Base/Notes/Insights/beta.md",
            _page(
                "00000000-0000-4000-8000-000000000103",
                title="Beta",
                project="beta",
            ),
        ),
    ]
    counts = {"corpus": 0, "relations": 0, "language": 0, "contracts": 0, "resolve": 0}

    originals = {
        "corpus": semantic_contract.build_corpus_context,
        "relations": relation_registry.load_registry,
        "language": semantic_language_registry.load_registry,
        "contracts": memory_schema.load_saved_contracts,
        "resolve": memory_schema.resolve_contracts,
    }

    def counted(name, function):
        def wrapper(*args, **kwargs):
            counts[name] += 1
            return function(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(
        semantic_contract,
        "build_corpus_context",
        counted("corpus", originals["corpus"]),
    )
    monkeypatch.setattr(
        relation_registry,
        "load_registry",
        counted("relations", originals["relations"]),
    )
    monkeypatch.setattr(
        semantic_language_registry,
        "load_registry",
        counted("language", originals["language"]),
    )
    monkeypatch.setattr(
        memory_schema,
        "load_saved_contracts",
        counted("contracts", originals["contracts"]),
    )
    monkeypatch.setattr(
        memory_schema,
        "resolve_contracts",
        counted("resolve", originals["resolve"]),
    )

    batch = semantic_writes.evaluate_posthoc_batch(
        tmp_path,
        paths=paths,
        operation="watcher",
    )

    assert counts == {
        "corpus": 1,
        "relations": 1,
        "language": 1,
        "contracts": 1,
        "resolve": 2,
    }
    assert [item.path for item in batch.evaluations] == [
        "Knowledge Base/Notes/Insights/alpha-one.md",
        "Knowledge Base/Notes/Insights/alpha-two.md",
        "Knowledge Base/Notes/Insights/beta.md",
    ]


def test_posthoc_projection_keeps_shared_keys_and_omits_content_and_review_reason(
    tmp_path: Path,
) -> None:
    sentinel_content = "SENTINEL-RAW-CONTENT"
    page = _write(
        tmp_path,
        "Knowledge Base/Notes/Insights/direct-new.md",
        _page(
            "00000000-0000-4000-8000-000000000104",
            title="Direct new",
            body=f"- [config] {sentinel_content}\n- [bad category!] malformed",
        ),
    )

    batch = semantic_writes.evaluate_posthoc_batch(
        tmp_path,
        paths=[page],
        operation="watcher",
    )
    payload = batch.as_dict()
    projection = payload["semantic_contract_findings"]
    shared = batch.evaluations[0].contract_result.findings

    assert [
        (
            item["code"],
            tuple(item["governed_element_identity"]),
            tuple(item["resolved_rule"]),
        )
        for item in projection
    ] == [finding.key for finding in shared]
    encoded = json.dumps(payload, sort_keys=True)
    assert sentinel_content not in encoded
    assert "review_reason" not in encoded
    assert payload["activation"] == "prospective"
    assert not activation_manifest.manifest_path(tmp_path).exists()


def test_posthoc_projection_bounds_clean_large_batches_without_losing_internal_results() -> None:
    empty = semantic_contract.SemanticContractResult(
        mode="posthoc",
        operation="reconcile",
        findings=(),
        errors=(),
        warnings=(),
        blocking_findings=(),
        should_block=False,
        semantic_unit_count=0,
        kind_counts=(),
        category_counts=(),
        relation_disposition=None,
        actions=(),
    )
    evaluations = tuple(
        semantic_writes.PosthocPageEvaluation(
            f"Knowledge Base/Notes/Insights/{index:04d}-{'x' * 1000}.md",
            empty,
            False,
            "current",
        )
        for index in range(1000)
    )
    batch = semantic_writes.PosthocBatch("reconcile", "current", evaluations)

    payload = batch.as_dict()

    assert len(batch.evaluations) == 1000
    assert len(json.dumps(payload, sort_keys=True).encode("utf-8")) < 120 * 1024
    assert payload["omitted_counts"]["evaluated_paths"] > 0
    assert payload["truncation"]["strings_truncated"] > 0


def _posthoc_result(
    code: str,
    *,
    severity: str = "error",
    resolved_rule: tuple[str, str, str] = ("fields", "owner", "required"),
) -> semantic_contract.SemanticContractResult:
    finding = semantic_contract.ContractFinding(
        code=code,
        severity=severity,
        path="Knowledge Base/Notes/Insights/example.md",
        span=None,
        detail="Requires review.",
        remediation="Review the semantic contract.",
        governed_element_identity=("semantic", code),
        resolved_rule=resolved_rule,
    )
    return semantic_contract.SemanticContractResult(
        mode="posthoc",
        operation="audit",
        findings=(finding,),
        errors=(finding,) if severity == "error" else (),
        warnings=(finding,) if severity != "error" else (),
        blocking_findings=(finding,) if severity == "error" else (),
        should_block=severity == "error",
        semantic_unit_count=1,
        kind_counts=(),
        category_counts=(),
        relation_disposition=None,
        actions=("review_semantic_contract",),
    )


def test_posthoc_prioritizes_current_findings_before_bounds_and_full_is_unbounded() -> None:
    legacy_count = semantic_writes._POSTHOC_FINDING_LIMIT + 8
    evaluations = [
        semantic_writes.PosthocPageEvaluation(
            f"Knowledge Base/Notes/Insights/legacy-{index:04d}.md",
            _posthoc_result(
                "RELATION_DISPOSITION_MISSING",
                resolved_rule=("relations", "*", "disposition"),
            ),
            True,
            "current",
        )
        for index in range(legacy_count)
    ]
    evaluations.extend(
        [
            semantic_writes.PosthocPageEvaluation(
                "Knowledge Base/Notes/Insights/current-b.md",
                _posthoc_result("CONTRACT_CURRENT_B"),
                False,
                "current",
            ),
            semantic_writes.PosthocPageEvaluation(
                "Knowledge Base/Notes/Insights/current-a.md",
                _posthoc_result("CONTRACT_CURRENT_A"),
                False,
                "current",
            ),
            semantic_writes.PosthocPageEvaluation(
                "Knowledge Base/Notes/Insights/current-malformed.md",
                _posthoc_result(
                    "invalid_compact_category",
                    severity="warn",
                    resolved_rule=("semantic_units", "*", "syntax"),
                ),
                False,
                "current",
            ),
        ]
    )
    batch = semantic_writes.PosthocBatch(
        "audit", "current", tuple(evaluations)
    )

    bounded = batch.as_dict()
    retained_codes = [
        item["code"] for item in bounded["semantic_contract_findings"]
    ]

    assert retained_codes[:3] == [
        "CONTRACT_CURRENT_A",
        "CONTRACT_CURRENT_B",
        "invalid_compact_category",
    ]
    assert bounded["semantic_contract_summary"] == {
        "CONTRACT_CURRENT_A": 1,
        "CONTRACT_CURRENT_B": 1,
        "RELATION_DISPOSITION_MISSING": legacy_count,
        "invalid_compact_category": 1,
    }
    assert bounded["omitted_counts"]["semantic_contract_findings"] == (
        legacy_count + 3 - len(bounded["semantic_contract_findings"])
    )
    assert bounded["truncation"]["observation_complete"] is True
    assert bounded["truncation"]["findings_complete"] is False

    full = batch.as_dict(detail="full")

    assert len(full["semantic_contract_findings"]) == legacy_count + 3
    assert full["omitted_counts"]["semantic_contract_findings"] == 0
    assert full["truncation"]["finding_limit"] is None
    assert full["truncation"]["byte_budget"] is None
    assert full["truncation"]["observation_complete"] is True
    assert full["truncation"]["findings_complete"] is True


def test_non_audit_posthoc_keeps_legacy_bounded_projection_contract() -> None:
    legacy_count = semantic_writes._POSTHOC_FINDING_LIMIT + 8
    evaluations = [
        semantic_writes.PosthocPageEvaluation(
            f"Knowledge Base/Notes/Insights/legacy-{index:04d}.md",
            _posthoc_result(f"LEGACY_{index:04d}"),
            True,
            "current",
        )
        for index in range(legacy_count)
    ]
    evaluations.append(
        semantic_writes.PosthocPageEvaluation(
            "Knowledge Base/Notes/Insights/current.md",
            _posthoc_result("CONTRACT_CURRENT"),
            False,
            "current",
        )
    )

    payload = semantic_writes.PosthocBatch(
        "watcher", "current", tuple(evaluations)
    ).as_dict()

    assert payload["semantic_contract_findings"][0]["code"] == "LEGACY_0000"
    assert len(payload["semantic_contract_summary"]) == (
        semantic_writes._POSTHOC_SUMMARY_LIMIT
    )
    assert payload["omitted_counts"]["semantic_contract_summary"] == (
        legacy_count + 1 - semantic_writes._POSTHOC_SUMMARY_LIMIT
    )
    assert payload["truncation"]["summary_limit"] == (
        semantic_writes._POSTHOC_SUMMARY_LIMIT
    )
    assert "observation_complete" not in payload["truncation"]
    assert "findings_complete" not in payload["truncation"]


def test_watcher_reports_posthoc_before_one_index_fanout_and_preserves_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    page = _write(
        tmp_path,
        "Knowledge Base/Notes/Insights/direct-invalid.md",
        _page(
            "00000000-0000-4000-8000-000000000105",
            title="Direct invalid",
            body="- [config] Valid unit.\n- [bad category!] malformed",
        ),
    )
    original = page.read_bytes()
    calls: list[list[Path]] = []
    order: list[str] = []
    real_batch = semantic_writes.evaluate_posthoc_batch

    def evaluate(*args, **kwargs):
        order.append("posthoc")
        return real_batch(*args, **kwargs)

    def upsert(_root, paths, **_kwargs):
        order.append("index")
        calls.append(list(paths))

    monkeypatch.setattr(semantic_writes, "evaluate_posthoc_batch", evaluate)
    monkeypatch.setattr(index_sync, "upsert_after_write", upsert)
    watcher = file_watcher.FileWatcher(tmp_path)

    with caplog.at_level("WARNING", logger="exomem.file_watcher"):
        watcher._record(page, deleted=False)
        watcher._flush()

    assert order == ["posthoc", "index"]
    assert calls == [[page]]
    assert page.read_bytes() == original
    assert "invalid_compact_category" in caplog.text
    assert "RELATION_DISPOSITION_MISSING" in caplog.text


def test_watcher_delete_skips_posthoc_parsing_and_keeps_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted = "Knowledge Base/Notes/Insights/gone.md"
    calls: list[list[str]] = []

    def fail_if_evaluated(*_args, **_kwargs):
        raise AssertionError("delete events must not parse absent content")

    monkeypatch.setattr(
        semantic_writes,
        "evaluate_posthoc_batch",
        fail_if_evaluated,
        raising=False,
    )
    monkeypatch.setattr(
        index_sync,
        "delete_after_remove",
        lambda _root, paths: calls.append(list(paths)),
    )
    watcher = file_watcher.FileWatcher(tmp_path)

    watcher._record(tmp_path / deleted, deleted=True)
    watcher._flush()

    assert calls == [[deleted]]


def test_opt_in_audit_returns_shared_finding_keys_without_raw_content(
    tmp_path: Path,
) -> None:
    kb = tmp_path / "Knowledge Base"
    kb.mkdir(parents=True)
    activation_manifest.ensure_manifest(tmp_path)
    sentinel = "AUDIT-SENTINEL-RAW-CONTENT"
    page = _write(
        tmp_path,
        "Knowledge Base/Notes/Insights/audit-new.md",
        _page(
            "00000000-0000-4000-8000-000000000106",
            title="Audit new",
            body=f"- [config] {sentinel}",
        ),
    )
    shared = semantic_writes.evaluate_posthoc_batch(
        tmp_path,
        paths=[page],
        operation="audit",
    ).as_dict()["semantic_contract_findings"]

    report = audit_module.audit(
        tmp_path,
        categories=["semantic_contract_drift"],
    )

    assert [finding.meta["finding_key"] for finding in report.findings] == [
        {
            "code": item["code"],
            "governed_element_identity": item["governed_element_identity"],
            "resolved_rule": item["resolved_rule"],
        }
        for item in shared
    ]
    payload = json.dumps(report.as_dict(), sort_keys=True)
    assert sentinel not in payload
    assert "review_reason" not in payload


def test_default_audit_does_not_run_semantic_contract_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "Knowledge Base").mkdir(parents=True)

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("default audit must not pay semantic drift cost")

    monkeypatch.setattr(
        semantic_writes,
        "evaluate_posthoc_batch",
        fail_if_called,
    )

    report = audit_module.audit(tmp_path)

    assert all(
        finding.category != "semantic_contract_drift"
        for finding in report.findings
    )


def test_semantic_audit_reports_shared_omission_and_truncation_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projection = {
        "semantic_contract_findings": [],
        "omitted_counts": {
            "evaluated_paths": 0,
            "semantic_contract_findings": 44,
            "semantic_contract_summary": 0,
        },
        "truncation": {
            "byte_budget": 120 * 1024,
            "finding_limit": 256,
            "budget_items_omitted": 44,
        },
    }
    monkeypatch.setattr(
        semantic_writes,
        "evaluate_posthoc_batch",
        lambda *args, **kwargs: SimpleNamespace(as_dict=lambda: projection),
    )

    report = audit_module.audit(
        tmp_path, categories=["semantic_contract_drift"]
    ).as_dict()

    assert report["metadata"]["semantic_contract_drift"] == {
        "omitted_counts": projection["omitted_counts"],
        "truncation": projection["truncation"],
    }
