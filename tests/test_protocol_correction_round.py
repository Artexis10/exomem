"""Regression coverage added for the Lane-0 correction round."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError


def _identity(path: Path, count: int = 1):
    from protocol.models import DatasetIdentity

    return DatasetIdentity(
        id="fixture", variant="mini", source="local", revision="pin-1",
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(), case_count=count,
    )


def _gold():
    from protocol.models import CaseGold

    return CaseGold(
        case_id="case-1", answer="3", answer_session_ids=["answer_x1"],
        question_type="knowledge-update", question="Where did the violet cedar lantern open at dawn?",
    )


def test_ingest_field_detectors_are_scoped_and_token_bounded() -> None:
    from protocol.leakage import scan_ingest

    gold = _gold()
    content = ["Answer me this: Where did the violet cedar lantern open at dawn?"]
    harness = {"title": "case 1", "tags": ["longmemeval"]}
    detectors = {finding.detector for finding in scan_ingest(content, harness, harness, gold)}
    assert "question-text" in detectors
    assert "raw-upstream-id" not in detectors
    assert not scan_ingest(["The number is 3."], {"title": "case {case}"}, {"title": "case 34", "tags": []}, gold)
    assert "gold-text" in {finding.detector for finding in scan_ingest([], {"title": "3"}, {"title": "3"}, gold)}
    assert "raw-upstream-id" in {
        finding.detector for finding in scan_ingest(["answer_x1"], {"title": "case"}, {"title": "case"}, gold)
    }


def test_all_question_types_are_harness_labels() -> None:
    from lme.dataset import QUESTION_TYPES
    from protocol.leakage import scan_ingest

    for question_type in QUESTION_TYPES:
        findings = scan_ingest([], {"tags": [question_type]}, {"tags": [question_type]}, _gold())
        assert any(finding.detector == "label-token" for finding in findings), question_type


def test_identity_checks_never_scan_message_content() -> None:
    from protocol.events import LeakageError, assert_no_evidence_marked_ids
    from protocol.models import EventProvenance, ProtocolEvent

    event = ProtocolEvent(
        dataset=_identity(Path("benchmarks/lme/fixtures/leaky.json"), 2), case_id="case-1",
        session_ordinal=1, sequence=0, role="user", turn_ordinal=1,
        content="Answer me this: ordinary message content", content_sha256="a" * 64,
        original_timestamp=None, timestamp_semantics="ingestion_order_only", ingestion_ordinal=0,
        provenance=EventProvenance(dataset_row_index=0, upstream_session_id_sha256="b" * 64, converter="test", converter_version="1"),
    )
    assert_no_evidence_marked_ids([event], raw_upstream_session_ids=["answer_x1"])
    with pytest.raises(LeakageError):
        assert_no_evidence_marked_ids([event.model_copy(update={"case_id": "answer_x1"})], raw_upstream_session_ids=["answer_x1"])


def test_readiness_method_is_a_closed_allowlist() -> None:
    from protocol.readiness import LaneReadiness, validate

    with pytest.raises(ValidationError):
        LaneReadiness(lane="semantic", requested=True, verified=True, method="exit_code", evidence="0")
    assert validate([LaneReadiness(lane="unused", requested=False, verified=False, method="index-count", evidence="")]).status == "VALID"
    assert validate([LaneReadiness(lane="fallback", requested=True, verified=True, method="index-count", evidence="x", fallback_detected=True)]).status == "INVALID"


def test_models_close_probe_and_trace_payload_vocabulary() -> None:
    from protocol.models import CaseTrace, IngestRecord, ProbeResult

    with pytest.raises(ValidationError):
        ProbeResult(case_id="case", probe_kind="unknown", outcome="perhaps")
    trace = CaseTrace(case_id="case", entries=[IngestRecord(session_ordinal=1, payload_sha256="a" * 64, provider_ids=["p-1"])])
    assert trace.entries[0].record == "ingest"


def test_namespace_provider_prefixes_are_distinct_and_safe() -> None:
    from protocol.namespace import derive_namespace

    names = {kind: derive_namespace("Run with symbols!" * 10, "case/1", kind) for kind in ("exomem", "basic-memory", "supermemory", "hybrid-rag")}
    assert len(set(names.values())) == 4
    assert all(len(name) <= 100 and name.replace("-", "").isalnum() for name in names.values())


def test_namespace_is_deterministic_for_identical_inputs() -> None:
    from protocol.namespace import derive_namespace

    assert derive_namespace("run-1", "case-1", "exomem") == derive_namespace("run-1", "case-1", "exomem")


def test_budget_refusal_does_not_consume_budget_and_stale_lock_recovers(tmp_path: Path) -> None:
    from protocol.budget import BudgetExceeded, BudgetLedger

    ledger = BudgetLedger(tmp_path, caps={"usd": 5})
    ledger.reserve(ts="2026-01-01T00:00:00Z", seq=1, actor="t", op="i", units=4)
    with pytest.raises(BudgetExceeded):
        ledger.reserve(ts="2026-01-01T00:00:01Z", seq=2, actor="t", op="i", units=2)
    assert any(json.loads(line)["decision"] == "refused-cap" for line in (tmp_path / "ledger.jsonl").read_text().splitlines())
    (tmp_path / "STOP").unlink()
    ledger.release(ts="2026-01-01T00:00:02Z", seq=3, actor="t", op="i", units=4)
    assert ledger.reserve(ts="2026-01-01T00:00:03Z", seq=4, actor="t", op="i", units=5).decision == "approved"
    (tmp_path / ".budget.lock").write_text("999999 0\n", encoding="utf-8")
    ledger.approve(ts="2026-01-01T00:00:04Z", seq=5, actor="t", op="i")


def test_manifest_refuses_unknown_versions_duplicates_and_nonterminal_finalization(tmp_path: Path) -> None:
    from protocol.manifest import ManifestError, finalize_manifest, load_manifest, start_manifest

    identity = _identity(Path("benchmarks/lme/fixtures/leaky.json"), 2)
    start_manifest(tmp_path, run_id="run", dataset=identity, started_at="2026-01-01T00:00:00Z")
    with pytest.raises(ManifestError):
        start_manifest(tmp_path, run_id="run", dataset=identity, started_at="2026-01-01T00:00:00Z")
    with pytest.raises(ManifestError):
        finalize_manifest(tmp_path, status="started", finalized_at="2026-01-01T00:00:01Z")
    raw = json.loads((tmp_path / "manifest.json").read_text())
    raw["schema_version"] = 2
    (tmp_path / "manifest.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ManifestError):
        load_manifest(tmp_path)


def test_canary_never_ingested_contaminates_and_kinds_are_distinct() -> None:
    from protocol.canary import canary_for, evaluate_probes

    values = {canary_for("first", "case", kind) for kind in ("presence", "cross_case", "never_ingested")}
    assert len(values) == 3
    assert canary_for("first", "case", "presence") != canary_for("second", "case", "presence")
    assert evaluate_probes({"presence": True, "cross_case": False, "never_ingested": True}) == "contaminated"
