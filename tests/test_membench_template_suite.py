"""Full scenario-template suite: coverage, provenance, governance, determinism."""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem.public_artifact_privacy import scan_artifact

from membench.generate import generate_corpus
from membench.schema import (
    ClaimRecord,
    CorpusManifest,
    ExpectedRecord,
    QueryRecord,
    load_jsonl,
)

REQUIRED_FAMILIES = {
    "temporal",
    "epistemics",
    "maintenance",
    "identity",
    "multimodal",
    "governance",
    "query_behavior",
}

T11 = "t11_transitive_provenance"


@pytest.fixture(scope="module")
def suite(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, CorpusManifest]:
    root = tmp_path_factory.mktemp("suite") / "s1"
    manifest = generate_corpus(1, root)
    return root, manifest


def test_full_suite_counts(suite: tuple[Path, CorpusManifest]) -> None:
    _, manifest = suite
    assert len(manifest.templates) >= 16
    assert manifest.counts["queries"] >= 200
    assert manifest.counts["expected"] == manifest.counts["queries"]


def test_family_coverage(suite: tuple[Path, CorpusManifest]) -> None:
    root, _ = suite
    queries = load_jsonl(QueryRecord, root / "queries.jsonl")
    families = {q.family for q in queries}
    missing = REQUIRED_FAMILIES - families
    assert not missing, f"families without queries: {sorted(missing)}"


def test_t11_requires_transitive_citations(suite: tuple[Path, CorpusManifest]) -> None:
    root, _ = suite
    queries = load_jsonl(QueryRecord, root / "queries.jsonl")
    expected = {e.query_id: e for e in load_jsonl(ExpectedRecord, root / "expected.jsonl")}
    claims = {c.claim_id: c for c in load_jsonl(ClaimRecord, root / "claims.jsonl")}
    satisfied = False
    for query in queries:
        if query.template_id != T11:
            continue
        record = expected[query.query_id]
        if len(record.required_citations) < 2 or not record.required_claims:
            continue
        direct_sources = {
            a.source_id
            for claim_id in record.required_claims
            for a in claims[claim_id].assertions
        }
        transitive_only = [
            s for s in record.required_citations if s not in direct_sources
        ]
        if transitive_only:
            satisfied = True
            break
    assert satisfied, "no t11 expectation cites a source reachable only via derived_from"


def test_governance_expectations_forbid_disclosure(
    suite: tuple[Path, CorpusManifest],
) -> None:
    root, _ = suite
    expected = load_jsonl(ExpectedRecord, root / "expected.jsonl")
    assert any(e.forbidden_disclosures and e.abstain for e in expected)


def test_full_suite_is_deterministic(
    suite: tuple[Path, CorpusManifest], tmp_path: Path
) -> None:
    _, manifest = suite
    again = generate_corpus(1, tmp_path / "s2")
    assert manifest == again


def test_sampled_artifacts_pass_privacy_scan(suite: tuple[Path, CorpusManifest]) -> None:
    root, _ = suite
    text_files = sorted(
        p
        for p in (root / "sources").rglob("*")
        if p.is_file() and p.suffix in {".md", ".csv", ".txt"}
    )
    assert len(text_files) >= 5
    step = max(1, len(text_files) // 5)
    sample = text_files[::step][:5]
    assert len(sample) == 5
    findings = []
    for path in sample:
        findings.extend(scan_artifact(path, label=path.name))
    assert findings == []
