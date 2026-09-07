"""The real Python and TypeScript capture boundaries must agree byte for byte."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest


def _events():
    from lme.dataset import load_dataset
    from lme.normalize import neutralize
    from protocol.models import DatasetIdentity

    fixture = Path("benchmarks/lme/fixtures/mini.json")
    question = load_dataset(fixture).questions[0]
    identity = DatasetIdentity(id="longmemeval", variant="mini", source="local", revision="fixture", sha256=hashlib.sha256(fixture.read_bytes()).hexdigest(), case_count=1)
    events = neutralize(question, identity)
    return [event for event in events if event.session_ordinal == 1]


def test_python_and_guest_transmit_the_same_complete_product_payload():
    from lme.exomem_capture import capture_payload, payload_digest

    if not shutil.which("bun"):
        pytest.skip("cross-language test requires Bun")
    events = _events()
    events = [event.model_copy(update={"content": "Cafe\u0301 <tea>\nSnow: 雪", "original_timestamp": "2025-01-02T03:04:05+02:00"}) for event in events]
    expected = capture_payload(events)
    module = Path("benchmarks/memorybench/providers/exomem/index.ts").resolve()
    script = f"import {{ capturePayload }} from {json.dumps(str(module))}; const x = JSON.parse(await Bun.stdin.text()); console.log(JSON.stringify(await capturePayload(x.session, x.position)));"
    result = subprocess.run(["bun", "--eval", script], input=json.dumps({"session": {"sessionId": "answer_secret_abs", "messages": [{"role": event.role, "content": event.content} for event in events], "metadata": {"date": "2025-01-02T01:04:05.000Z"}}, "position": 0}), text=True, capture_output=True, check=True)
    actual = json.loads(result.stdout)
    assert actual == expected
    assert set(actual) == {"content", "title", "slug", "tags", "source_type", "compile_guidance"}
    assert "Café <tea>\nSnow: 雪" in actual["content"]
    assert "Session timestamp: 2025-01-02T01:04:05Z" in actual["content"]
    assert "answer_secret_abs" not in json.dumps(actual)
    assert payload_digest(actual) == hashlib.sha256(json.dumps(actual, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def test_transport_envelope_does_not_change_product_digest_but_product_changes_do():
    from lme.exomem_capture import capture_payload, product_payload

    payload = capture_payload(_events())
    assert product_payload({**payload, "request_id": "first", "idempotency_key": "first"}) == payload
    assert product_payload({**payload, "request_id": "second", "idempotency_key": "second"}) == payload
    with pytest.raises(ValueError):
        product_payload({**payload, "projects": ["undeclared"]})
    with pytest.raises(ValueError):
        product_payload({key: value for key, value in payload.items() if key != "tags"})


def test_direct_namespace_is_the_actual_guest_container_tag_digest():
    from lme.exomem_capture import NAMESPACE_PATTERN
    from protocol.namespace import derive_namespace, namespace_pattern

    run_id, case_id = "guest-run-123", "question_abs"
    expected = hashlib.sha256(f"{case_id}-{run_id}".encode()).hexdigest()[:24]
    assert derive_namespace(run_id, case_id, "exomem") == expected
    assert namespace_pattern("exomem") == NAMESPACE_PATTERN


def test_direct_readiness_uses_current_case_doctor_and_cannot_be_promoted_by_old_probe(monkeypatch, tmp_path):
    from exomem import doctor
    from lme.providers.exomem_direct import ExomemDirectProvider
    from lme.runner import _readiness_with_probe_evidence
    from protocol.models import ProbeResult
    from types import SimpleNamespace

    provider = ExomemDirectProvider()
    provider._adapter._vault = Path("owned-case")
    provider._context = SimpleNamespace(evidence_root=tmp_path)
    reports = []
    def inspect(**kwargs):
        reports.append(kwargs)
        return SimpleNamespace(as_dict=lambda: {"success": False, "profile": "hybrid", "checks": []})
    monkeypatch.setattr(doctor, "doctor", inspect)
    lanes = provider.readiness()
    promoted = _readiness_with_probe_evidence(lanes, [ProbeResult(case_id="other", probe_kind="semantic-zero-overlap", outcome="pass")])
    assert reports == [{"vault": str(provider._adapter._vault), "profile": "hybrid"}]
    assert promoted[0].verified is False
    assert promoted[0].fallback_detected is True
    assert list(tmp_path.iterdir()) == [], "lifecycle evidence inventory owns this directory"
    assert provider.last_doctor_report == {"success": False, "profile": "hybrid", "checks": []}
