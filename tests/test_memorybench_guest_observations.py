"""4.6a-2: project what the Exomem guest already recorded, and nothing else.

The guest logs a request/response pair for every call it makes. These checks
pin that the projection reports only what those entries prove: a half-recorded
call, a response that breaks the guest's own limit contract, or an empty
evidence directory must never become a published value.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

SEARCH_LABELS = {
    "search.transmitted_query",
    "search.options.limit",
    "search.normalized_hit_ids",
}
INGEST_LABELS = {"ingest.transmitted_payloads"}
SEMANTIC_CHECKS = [
    "embeddings.enabled",
    "dep.sentence-transformers",
    "dep.torch",
    "dep.pillow",
    "models.cache",
    "embeddings.sidecar",
]


def _write(directory: Path, sequence: int, event: str, data: dict) -> None:
    entry = {
        "protocol_version": 1,
        "event": event,
        "recorded_at_utc": "2026-08-15T00:00:00.000Z",
        "data": data,
    }
    payload = json.dumps(entry, sort_keys=True) + "\n"
    digest = hashlib.sha256(payload.encode()).hexdigest()[:12]
    (directory / f"operation-{sequence:06d}-{digest}.json").write_text(payload, encoding="utf-8")


def _search_pair(directory: Path, *, query: str, limit: int, paths: list[str], start: int = 1) -> None:
    _write(directory, start, "request", {
        "path": "/api/ask_memory",
        "body": {"query": query, "limit": limit, "scope": "kb", "mode": "hybrid", "detail": "full"},
    })
    _write(directory, start + 1, "response", {
        "path": "/api/ask_memory",
        "response": [{"path": item} for item in paths],
    })


def _project(directory: Path):
    from memorybench.guest_observations import project_guest_evidence

    return project_guest_evidence(directory)


def test_a_recorded_search_yields_the_transmitted_query_limit_and_hit_order(tmp_path: Path) -> None:
    _search_pair(tmp_path, query="which lantern?", limit=10, paths=["b.md", "a.md"])
    observed = _project(tmp_path)

    assert observed.search == {
        "transmitted_query": "which lantern?",
        "options": {"limit": 10},
        # Returned order is the retrieval result; sorting it would destroy the
        # only ranking signal this path carries.
        "normalized_hit_ids": ["b.md", "a.md"],
    }
    assert SEARCH_LABELS <= observed.resolved_labels()
    assert not observed.problems


def test_the_expected_service_descriptor_is_not_misclassified_as_operation_evidence(
    tmp_path: Path,
) -> None:
    _search_pair(tmp_path, query="which lantern?", limit=10, paths=["a.md"])
    (tmp_path / "service.json").write_text("{}\n", encoding="utf-8")

    observed = _project(tmp_path)

    assert observed.search is not None
    assert not observed.problems


def test_a_request_with_no_paired_response_publishes_nothing(tmp_path: Path) -> None:
    _write(tmp_path, 1, "request", {
        "path": "/api/ask_memory",
        "body": {"query": "q", "limit": 3, "scope": "kb", "mode": "hybrid", "detail": "full"},
    })
    observed = _project(tmp_path)

    assert observed.search is None
    assert not (SEARCH_LABELS & observed.resolved_labels())
    assert "guest_evidence_incomplete" in observed.problems


def test_an_over_limit_response_is_a_problem_not_a_published_value(tmp_path: Path) -> None:
    """The guest refuses this before returning, so evidence showing it is suspect."""

    _search_pair(tmp_path, query="q", limit=1, paths=["a.md", "b.md"])
    observed = _project(tmp_path)

    assert observed.search is None
    assert "guest_evidence_invalid" in observed.problems


def test_ingest_payload_digests_follow_transmission_order(tmp_path: Path) -> None:
    bodies = [{"title": "s1", "body": "one"}, {"title": "s2", "body": "two"}]
    for index, body in enumerate(bodies):
        _write(tmp_path, index + 1, "request", {"path": "/api/capture_source", "body": body})
        _write(tmp_path, index + 10, "response", {"path": "/api/capture_source", "response": {"ok": True}})
    observed = _project(tmp_path)

    expected = [
        hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        for body in bodies
    ]
    assert observed.ingest == {"transmitted_payload_sha256": expected}
    assert INGEST_LABELS <= observed.resolved_labels()


def test_v2_evidence_keeps_wire_hash_and_compares_the_complete_product_payload(tmp_path):
    from lme.exomem_capture import CAPTURE_CONTRACT, NAMESPACE_PATTERN, payload_digest

    root = tmp_path / ("a" * 24)
    root.mkdir()
    body = {"content": "Session timestamp: 2025-01-01T00:00:00Z\nSession ordinal: 1\n\nuser: hello", "title": "neutral", "slug": "neutral", "source_type": "session", "tags": ["longmemeval"], "compile_guidance": False}
    manifest = {"session_normalization": CAPTURE_CONTRACT, "product_input_contract": CAPTURE_CONTRACT, "namespace_pattern": NAMESPACE_PATTERN, "namespace": root.name}
    _write(root, 1, "provider-manifest", manifest)
    wire = {**body, "request_id": "fresh-a", "idempotency_key": "fresh-a"}
    _write(root, 2, "request", {"path": "/api/capture_source", "body": wire})
    observed = _project(root)
    assert not observed.problems
    assert observed.ingest["transmitted_payload_sha256"] == [payload_digest(wire)]
    assert observed.ingest["product_payload_sha256"] == [payload_digest(body)]
    assert observed.namespace_pattern == NAMESPACE_PATTERN
    assert observed.session_normalization == CAPTURE_CONTRACT


@pytest.mark.parametrize("damage", ["namespace", "extra-field", "contract"])
def test_v2_capture_evidence_refuses_contract_or_namespace_drift(tmp_path, damage):
    from lme.exomem_capture import CAPTURE_CONTRACT, NAMESPACE_PATTERN

    tmp_path = tmp_path / ("a" * 24)
    tmp_path.mkdir()
    manifest = {"session_normalization": CAPTURE_CONTRACT, "product_input_contract": CAPTURE_CONTRACT, "namespace_pattern": NAMESPACE_PATTERN, "namespace": tmp_path.name}
    body = {"content": "content", "title": "neutral", "slug": "neutral", "source_type": "session", "tags": ["longmemeval"], "compile_guidance": False}
    if damage == "namespace":
        manifest["namespace"] = "wrong-namespace"
    elif damage == "contract":
        manifest["product_input_contract"] = "unknown/v3"
    else:
        body["projects"] = ["undeclared"]
    _write(tmp_path, 1, "provider-manifest", manifest)
    _write(tmp_path, 2, "request", {"path": "/api/capture_source", "body": body})
    assert "guest_evidence_invalid" in _project(tmp_path).problems


def test_doctor_evidence_becomes_verified_semantic_readiness(tmp_path: Path) -> None:
    _write(tmp_path, 1, "doctor-request", {"profile": "hybrid"})
    _write(tmp_path, 2, "doctor-response", {
        "response": {"checks": [
            *({"id": check, "status": "pass"} for check in SEMANTIC_CHECKS),
            {"id": "torch.cuda", "status": "warn"},
        ]}
    })
    observed = _project(tmp_path)

    assert observed.readiness is not None
    lane = observed.readiness[0]
    assert lane["lane"] == "semantic"
    assert lane["verified"] is True
    assert lane["method"] == "doctor-check"
    assert lane["fallback_detected"] is False


def test_a_failing_doctor_check_is_reported_unverified_not_omitted(tmp_path: Path) -> None:
    _write(tmp_path, 1, "doctor-request", {"profile": "hybrid"})
    _write(tmp_path, 2, "doctor-response", {
        "response": {"checks": [
            {"id": check, "status": "fail" if check == "embeddings.sidecar" else "pass"}
            for check in SEMANTIC_CHECKS
        ]}
    })
    observed = _project(tmp_path)

    lane = observed.readiness[0]
    assert lane["verified"] is False
    assert lane["fallback_detected"] is True
    assert "embeddings.sidecar" in lane["evidence"]


def test_an_empty_evidence_directory_is_absence_not_a_fault(tmp_path: Path) -> None:
    observed = _project(tmp_path)

    assert observed.search is None and observed.ingest is None and observed.readiness is None
    assert observed.resolved_labels() == frozenset()
    assert not observed.problems


def test_projection_never_emits_a_bearer_token_or_absolute_path(tmp_path: Path) -> None:
    _search_pair(tmp_path, query="q", limit=2, paths=["a.md"])
    _write(tmp_path, 5, "request", {
        "path": "/api/capture_source",
        "body": {"title": "s", "body": "x", "authorization": "Bearer super-secret-token"},
    })
    _write(tmp_path, 6, "response", {"path": "/api/capture_source", "response": {"ok": True}})
    observed = _project(tmp_path)

    rendered = json.dumps({
        "search": observed.search, "ingest": observed.ingest, "readiness": observed.readiness,
    })
    assert "super-secret-token" not in rendered
    assert str(tmp_path) not in rendered


def test_entries_are_read_in_sequence_order_not_directory_order(tmp_path: Path) -> None:
    """Sequence 10 must not sort before sequence 2."""

    for index, body in enumerate([{"n": i} for i in range(12)]):
        _write(tmp_path, index + 1, "request", {"path": "/api/capture_source", "body": body})
    observed = _project(tmp_path)

    expected = [
        hashlib.sha256(json.dumps({"n": i}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        for i in range(12)
    ]
    assert observed.ingest["transmitted_payload_sha256"] == expected


def test_a_malformed_entry_is_a_problem_and_stops_publication(tmp_path: Path) -> None:
    (tmp_path / "operation-000001-deadbeefcafe.json").write_text("{not json", encoding="utf-8")
    observed = _project(tmp_path)

    assert observed.search is None
    assert "guest_evidence_invalid" in observed.problems


@pytest.mark.parametrize("body", [
    {"query": "", "limit": 3},
    {"query": "q", "limit": 0},
    {"query": "q", "limit": "3"},
    {"query": "q"},
])
def test_a_search_body_that_does_not_prove_query_and_limit_publishes_nothing(
    tmp_path: Path, body: dict
) -> None:
    _write(tmp_path, 1, "request", {"path": "/api/ask_memory", "body": body})
    _write(tmp_path, 2, "response", {"path": "/api/ask_memory", "response": [{"path": "a.md"}]})
    observed = _project(tmp_path)

    assert observed.search is None
    assert not (SEARCH_LABELS & observed.resolved_labels())


def test_actual_guest_manifest_survives_evidence_writer_and_python_projection(tmp_path):
    import subprocess
    from benchmark_capabilities import require_pinned_bun
    from lme.exomem_capture import CAPTURE_CONTRACT, NAMESPACE_PATTERN

    require_pinned_bun()
    tag = 'fixture-case-run'
    root = tmp_path / hashlib.sha256(tag.encode()).hexdigest()[:24]
    module = Path('benchmarks/memorybench/providers/exomem/index.ts').resolve()
    script = f'''
import {{ ExomemProvider }} from {json.dumps(str(module))};
const input = JSON.parse(await Bun.stdin.text());
const provider = new ExomemProvider({{
  ensureService: async () => ({{protocol_version: 1, provider: "exomem", base_url: "http://127.0.0.1:1", bearer_token: "fixture-service-secret", pid: 1, process_start_identity: "fixture", checkout_pin: "fixture", work_root: input.root, evidence_root: input.root}}),
  post: async () => ({{source: {{path: "source.md"}}}}),
  clearAllServices: async () => {{}},
}});
// Substitute service/model I/O, while exercising the real provider-authored
// manifest and production evidence writer (normally disabled by injected I/O).
provider.evidenceEnabled = true;
await provider.ingest([{{sessionId: "fixture-session-0", metadata: {{date: "2025-01-01"}}, messages: [{{role: "user", content: "public text"}}]}}], {{containerTag: input.tag}});
'''
    subprocess.run(['bun', '--eval', script], input=json.dumps({'root': str(root), 'tag': tag}), text=True, capture_output=True, check=True)
    observed = _project(root)
    assert not observed.problems
    assert observed.session_normalization == CAPTURE_CONTRACT
    assert observed.namespace_pattern == NAMESPACE_PATTERN
    assert len(observed.ingest['product_payload_sha256']) == 1
