from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from epistemic.assertions import AssertionContext, AssertionResult
from epistemic.snapshot import EpistemicStateSnapshot, FieldDeclaration, ProjectorMeta, StateItem


def _snapshot(
    *,
    current: str = "yes",
    text: str = "current",
    provider: str = "fixture",
    variant: str = "native",
) -> EpistemicStateSnapshot:
    return EpistemicStateSnapshot(
        provider=provider, variant=variant, phase="p1", taken_at="2026-08-11T00:00:00Z",
        items=(StateItem(
            id="claim", kind="claim", title="claim", text=text, current=current,
            retired_reason="superseded" if current == "yes" else None,
        ),),
        declarations=(
            FieldDeclaration(field="current", status="declared", evidence="https://example.invalid/current"),
        ),
        projector=ProjectorMeta(
            name="fixture", version="1", author="test", endpoints_used=("broker:state.read",), loc=1,
        ),
    )


def _persist(
    run_root: Path,
    *,
    outcome: str = "fail",
    provider: str = "fixture",
    variant: str = "native",
):
    from epistemic.assertions import no_retired_state_served_as_current
    from epistemic.evidence import persist_assertion_evidence

    context = AssertionContext(
        snapshot=_snapshot(current="yes", provider=provider, variant=variant),
        subject="claim",
    )
    result = no_retired_state_served_as_current(context)
    if outcome != result.outcome:
        result = result.model_copy(update={"outcome": outcome})
    return persist_assertion_evidence(
        run_root=run_root,
        scenario_id="scenario|unsafe",
        scenario_sha256="a" * 64,
        family_id="f01",
        phase_id="p1",
        expectation_ordinal=1,
        assertion="no_retired_state_served_as_current",
        context=context,
        result=result,
    )


def _ref_for(root: Path, relative: str):
    from epistemic.evidence import AssertionEvidenceRef

    return AssertionEvidenceRef(
        path=relative, sha256=hashlib.sha256((root / relative).read_bytes()).hexdigest()
    )


def test_bound_evidence_payload_and_separate_canonical_reference_round_trip(tmp_path: Path) -> None:
    from epistemic.evidence import AssertionEvidencePayload, replay_assertion_evidence

    ref = _persist(tmp_path)
    assert not Path(ref.path).is_absolute()
    assert ".." not in Path(ref.path).parts
    payload = AssertionEvidencePayload.model_validate_json((tmp_path / ref.path).read_text())
    assert payload.scenario_id == "scenario|unsafe"
    assert payload.family_id == "f01"
    assert payload.phase_id == "p1"
    assert payload.expectation_ordinal == 1
    assert payload.assertion == "no_retired_state_served_as_current"
    assert payload.provider == "fixture"
    assert payload.variant == "native"
    assert payload.current_snapshot.path
    assert payload.prior_snapshot is None
    assert payload.parameters.subject == "claim"
    assert payload.probe_inputs.served_items is None
    assert replay_assertion_evidence(tmp_path, ref) == payload.result


def test_same_expectation_for_two_providers_gets_distinct_bound_evidence_paths(
    tmp_path: Path,
) -> None:
    from epistemic.evidence import AssertionEvidencePayload, replay_assertion_evidence

    first = _persist(tmp_path, provider="provider-a", variant="native")
    second = _persist(tmp_path, provider="provider-b", variant="hosted")

    assert first.path != second.path
    assert (tmp_path / first.path).is_file()
    assert (tmp_path / second.path).is_file()
    first_payload = AssertionEvidencePayload.model_validate_json(
        (tmp_path / first.path).read_bytes()
    )
    second_payload = AssertionEvidencePayload.model_validate_json(
        (tmp_path / second.path).read_bytes()
    )
    assert (first_payload.provider, first_payload.variant) == ("provider-a", "native")
    assert (second_payload.provider, second_payload.variant) == ("provider-b", "hosted")
    assert replay_assertion_evidence(tmp_path, first).outcome == "fail"
    assert replay_assertion_evidence(tmp_path, second).outcome == "fail"


def test_every_bound_assertion_requires_an_evidence_reference() -> None:
    from epistemic.evidence import EvidenceBoundAssertion

    failed = AssertionResult(name="x", outcome="fail", evidence="failed")
    with pytest.raises(ValidationError, match="failed assertion.*reference"):
        EvidenceBoundAssertion(result=failed, evidence_ref=None)
    passed = AssertionResult(name="x", outcome="pass", evidence="passed")
    with pytest.raises(ValidationError, match="every bound assertion.*reference"):
        EvidenceBoundAssertion(result=passed, evidence_ref=None)


def test_replay_requires_exact_frozen_assertion_result(tmp_path: Path) -> None:
    from epistemic.evidence import replay_assertion_evidence

    ref = _persist(tmp_path)
    assert replay_assertion_evidence(tmp_path, ref).outcome == "fail"


@pytest.mark.parametrize("missing", ["payload", "snapshot"])
def test_replay_refuses_missing_payload_or_snapshot(tmp_path: Path, missing: str) -> None:
    from epistemic.evidence import EvidenceReplayError, AssertionEvidencePayload, replay_assertion_evidence

    ref = _persist(tmp_path)
    if missing == "payload":
        (tmp_path / ref.path).unlink()
    else:
        payload = AssertionEvidencePayload.model_validate_json((tmp_path / ref.path).read_text())
        (tmp_path / payload.current_snapshot.path).unlink()
    with pytest.raises(EvidenceReplayError, match="missing"):
        replay_assertion_evidence(tmp_path, ref)


@pytest.mark.parametrize("unsafe", ["../escape.json", "/tmp/escape.json", "a\\b.json", "./x.json"])
def test_evidence_reference_rejects_escape_and_noncanonical_paths(unsafe: str) -> None:
    from epistemic.evidence import AssertionEvidenceRef

    with pytest.raises(ValidationError):
        AssertionEvidenceRef(path=unsafe, sha256="a" * 64)


def test_replay_opens_every_component_no_follow(tmp_path: Path) -> None:
    from epistemic.evidence import EvidenceReplayError, replay_assertion_evidence

    target = tmp_path / "real"
    target.mkdir()
    ref = _persist(target)
    os.symlink(target / Path(ref.path).parts[0], tmp_path / "linked")
    attacked = ref.model_copy(update={"path": str(Path("linked") / Path(ref.path).relative_to(Path(ref.path).parts[0]))})
    with pytest.raises(EvidenceReplayError, match="symlink|no-follow"):
        replay_assertion_evidence(tmp_path, attacked)


def test_replay_refuses_nonregular_payload(tmp_path: Path) -> None:
    from epistemic.evidence import AssertionEvidenceRef, EvidenceReplayError, replay_assertion_evidence

    (tmp_path / "directory.json").mkdir()
    ref = AssertionEvidenceRef(path="directory.json", sha256="a" * 64)
    with pytest.raises(EvidenceReplayError, match="regular file"):
        replay_assertion_evidence(tmp_path, ref)


def test_replay_refuses_payload_digest_mismatch(tmp_path: Path) -> None:
    from epistemic.evidence import EvidenceReplayError, replay_assertion_evidence

    ref = _persist(tmp_path)
    (tmp_path / ref.path).write_bytes((tmp_path / ref.path).read_bytes() + b" ")
    with pytest.raises(EvidenceReplayError, match="digest"):
        replay_assertion_evidence(tmp_path, ref)


def test_replay_refuses_schema_invalid_payload_even_with_matching_digest(tmp_path: Path) -> None:
    from epistemic.evidence import EvidenceReplayError, replay_assertion_evidence

    ref = _persist(tmp_path)
    payload = json.loads((tmp_path / ref.path).read_text())
    payload["unexpected"] = True
    (tmp_path / ref.path).write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(EvidenceReplayError, match="schema"):
        replay_assertion_evidence(tmp_path, _ref_for(tmp_path, ref.path))


def test_replay_refuses_snapshot_digest_or_schema_mismatch(tmp_path: Path) -> None:
    from epistemic.evidence import AssertionEvidencePayload, EvidenceReplayError, replay_assertion_evidence

    ref = _persist(tmp_path)
    payload_path = tmp_path / ref.path
    payload = json.loads(payload_path.read_text())
    snapshot_path = tmp_path / payload["current_snapshot"]["path"]
    snapshot = json.loads(snapshot_path.read_text())
    snapshot["unexpected"] = True
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    payload["current_snapshot"]["sha256"] = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(EvidenceReplayError, match="snapshot.*schema"):
        replay_assertion_evidence(tmp_path, _ref_for(tmp_path, ref.path))


def test_replay_refuses_snapshot_context_mismatch(tmp_path: Path) -> None:
    from epistemic.evidence import EvidenceReplayError, replay_assertion_evidence

    ref = _persist(tmp_path)
    payload_path = tmp_path / ref.path
    payload = json.loads(payload_path.read_text())
    snapshot_path = tmp_path / payload["current_snapshot"]["path"]
    snapshot = json.loads(snapshot_path.read_text())
    snapshot["provider"] = "different-provider"
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    payload["current_snapshot"]["sha256"] = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(EvidenceReplayError, match="snapshot.*identity|context"):
        replay_assertion_evidence(tmp_path, _ref_for(tmp_path, ref.path))


def test_replay_refuses_persisted_result_mismatch(tmp_path: Path) -> None:
    from epistemic.evidence import EvidenceReplayError, replay_assertion_evidence

    ref = _persist(tmp_path)
    path = tmp_path / ref.path
    payload = json.loads(path.read_text())
    payload["result"]["outcome"] = "pass"
    payload["result"]["evidence"] = "fabricated pass"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(EvidenceReplayError, match="result mismatch"):
        replay_assertion_evidence(tmp_path, _ref_for(tmp_path, ref.path))


def test_pure_assertion_module_remains_filesystem_free() -> None:
    source = Path("benchmarks/epistemic/assertions.py").read_text(encoding="utf-8")
    assert "from pathlib" not in source
    assert ".open(" not in source
    assert "read_text(" not in source
    assert "write_text(" not in source
