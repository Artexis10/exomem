"""W8: direct providers own observable, one-shot lifecycle cleanup."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest import mock

import pytest
from benchmark_capabilities import require_procfs_descriptor_paths


def _attempts(run_dir: Path) -> list[dict[str, object]]:
    environment = json.loads((run_dir / "environment.json").read_text(encoding="utf-8"))
    return environment["lme"]["lifecycle_attempts"]


def _attempt_session(run_dir: Path, logical_question_id: str | None) -> str:
    matches = [
        str(item["internal_session_id"])
        for item in _attempts(run_dir)
        if item["logical_question_id"] == logical_question_id
    ]
    assert len(matches) == 1
    return matches[0]


def _context(tmp_path: Path):
    from lme.providers.base import ProviderSessionContext

    work_root = tmp_path / "work"
    evidence_root = tmp_path / "evidence"
    work_root.mkdir()
    evidence_root.mkdir()
    return ProviderSessionContext(
        run_id="run-1", session_id="session-1", namespace="namespace-1",
        work_root=work_root, evidence_root=evidence_root,
    )


class _Provider:
    def __init__(self, *, failure: BaseException | None = None, variant: str = "observed") -> None:
        self.failure = failure
        self.variant = variant
        self.cleanups = 0
        self.context = None

    def setup(self, _profile, context) -> None:
        self.context = context
        if self.failure is not None:
            raise self.failure

    def ingest_case(self, _events, _handle):
        return ()

    def retrieve(self, _question, _top_k, _purpose):
        return []

    def export_state(self):
        return ()

    def cleanup(self) -> None:
        self.cleanups += 1

    def variant_id(self) -> str:
        return self.variant

    def readiness(self):
        return []


def _binding():
    from lme.providers.base import ProviderRuntimeBinding

    return ProviderRuntimeBinding(
        required_surface_ids=("provider-state", "session-root"),
        observe=lambda _context, provider: (
            {"kind": "provider-state", "remaining_record_ids": list(provider.export_state()), "backend_active": False},
            {"kind": "path-lstat", "path": "session-root", "raw_kind": "missing", "entries": []},
        ),
    )


def test_provider_spec_is_inert_and_session_context_is_immutable(tmp_path: Path) -> None:
    from lme.providers.base import ProviderDescriptor, ProviderSessionContext
    from lme.providers.registry import provider_spec

    spec = provider_spec("no-memory")
    assert spec.descriptor == ProviderDescriptor(
        provider_id="no-memory",
        execution_model="in-process-no-post-return-background",
    )
    assert spec.factory.__name__ == "NullDirectProvider"
    context = _context(tmp_path)
    with pytest.raises(FrozenInstanceError):
        context.namespace = "changed"  # type: ignore[misc]
    assert isinstance(context, ProviderSessionContext)


def test_setup_gets_the_exact_context_and_retrieval_purpose_is_closed(tmp_path: Path) -> None:
    from lme.providers.base import RetrievalPurpose
    from lme.providers.lifecycle import run_provider_lifecycle

    provider = _Provider()
    context = _context(tmp_path)
    run_provider_lifecycle(
        provider=provider, profile=None, context=context, binding=_binding(),
        requested_provider="fixture", operation=lambda provider: provider.retrieve("q", 1, RetrievalPurpose.SCORED_RETRIEVAL),
    )
    assert provider.context is context
    assert {purpose.value for purpose in RetrievalPurpose} == {
        "scored-retrieval", "positive-probe", "absence-probe-expected-empty",
    }


@pytest.mark.parametrize("failure", [RuntimeError("stage failure"), KeyboardInterrupt(), SystemExit(3), GeneratorExit()])
def test_lifecycle_cleans_once_and_reraises_the_original_base_exception(tmp_path: Path, failure: BaseException) -> None:
    from lme.providers.lifecycle import LifecycleRunError, run_provider_lifecycle

    provider = _Provider(failure=failure)
    if isinstance(failure, Exception):
        with pytest.raises(LifecycleRunError) as caught:
            run_provider_lifecycle(
                provider=provider, profile=None, context=_context(tmp_path), binding=_binding(),
                requested_provider="fixture", operation=lambda _provider: None,
            )
        assert caught.value.primary is failure
    else:
        with pytest.raises(type(failure)) as caught:
            run_provider_lifecycle(
                provider=provider, profile=None, context=_context(tmp_path), binding=_binding(),
                requested_provider="fixture", operation=lambda _provider: None,
            )
        assert caught.value is failure
    assert provider.cleanups == 1


def test_constructor_failure_has_no_provider_state_claim_and_terminalizes(tmp_path: Path) -> None:
    from lme.providers import registry
    from lme.reader import StubReader
    from lme.runner import LmeRunInvalid, execute_run

    def constructor():
        raise RuntimeError("constructor")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(registry, "provider_spec", lambda _name: _runner_spec(constructor))
    try:
        config = _mini_config(tmp_path, 1, "constructor", provider="fixture")
        with pytest.raises(LmeRunInvalid, match="constructor") as rejected:
            execute_run(config, reader=StubReader())
    finally:
        monkeypatch.undo()
    run_dir = rejected.value.run_dir
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "INVALID"
    environment = json.loads((run_dir / "environment.json").read_text(encoding="utf-8"))
    assert environment["lme"]["lifecycle_expected_instances"] == []
    assert not list((run_dir / "traces").glob("*.jsonl"))
    assert not list((run_dir / "evidence").rglob("provider-cleanup-observation.json"))
    assert (run_dir / "work").is_dir()
    assert list((run_dir / "work").iterdir()) == []


def test_provider_claims_cannot_create_cleanup_truth(tmp_path: Path) -> None:
    from lme.providers.lifecycle import CleanupUnproved, run_provider_lifecycle

    provider = _Provider()
    provider.cleanup = lambda: True  # type: ignore[method-assign]
    bad_binding = _binding()
    object.__setattr__(bad_binding, "observe", lambda _context, _provider: (
        {"kind": "provider-state", "remaining_record_ids": ["still-here"], "backend_active": True},
        {"kind": "path-lstat", "path": "session-root", "raw_kind": "missing", "entries": []},
    ))
    with pytest.raises(CleanupUnproved, match="absence"):
        run_provider_lifecycle(
            provider=provider, profile=None, context=_context(tmp_path), binding=bad_binding,
            requested_provider="fixture", operation=lambda _provider: None,
        )


def test_observation_is_raw_self_identifying_and_derived_from_reobservation(tmp_path: Path) -> None:
    from lme.providers.lifecycle import observe_cleanup

    context = _context(tmp_path)
    provider = _Provider()
    observation = observe_cleanup(
        context=context, requested_provider="fixture", observed_variant="observed",
        binding=_binding(), provider=provider, cleanup_called=True,
    )
    assert observation.artifact_type == "provider-cleanup-observation.v1"
    assert observation.run_id == context.run_id
    assert observation.namespace == context.namespace
    assert not hasattr(observation, "verified")
    assert not hasattr(observation, "absent")
    assert [item.kind for item in observation.observations] == ["path-lstat", "provider-state"]


@pytest.mark.parametrize("path", ["/absolute", "../escape", "work\\bad", "./work", ""])
def test_observation_refuses_noncanonical_evidence_paths(path: str) -> None:
    from protocol.models import ProviderCleanupPathLstat

    with pytest.raises(ValueError):
        ProviderCleanupPathLstat(kind="path-lstat", path=path, raw_kind="missing", entries=[])


def test_trace_v2_is_self_versioned_and_cannot_mix_v1(tmp_path: Path) -> None:
    from protocol.trace import CaseTraceReader, CaseTraceWriter, TraceError

    writer = CaseTraceWriter(tmp_path, "case", schema_version=2)
    writer.append({"record": "cleanup", "run_id": "run-1", "session_id": "session-1", "namespace": "ns", "observation_path": "evidence/cleanup.json", "observation_sha256": "a" * 64})
    records = list(CaseTraceReader(tmp_path, "case"))
    assert records[0].schema_version == 2
    writer_v1 = CaseTraceWriter(tmp_path, "case")
    with pytest.raises(TraceError, match="mixed"):
        writer_v1.append({"record": "timing", "phase": "x", "ms": 1})


def test_variant_binds_once_after_setup_and_refuses_drift(tmp_path: Path) -> None:
    from lme.providers.lifecycle import VariantDriftError, bind_observed_variant

    provider = _Provider()
    context = _context(tmp_path)
    assert bind_observed_variant(context, provider) == "observed"
    provider.variant = "later"
    with pytest.raises(VariantDriftError, match="drift"):
        bind_observed_variant(context, provider)


def test_manifest_pins_stay_selection_only_and_requested_provider_is_diagnostic(tmp_path: Path) -> None:
    from protocol.manifest import start_manifest

    manifest = start_manifest(
        tmp_path, run_id="run-1", started_at="2026-01-01T00:00:00Z",
        dataset={"id": "fixture", "variant": "mini", "source": "local", "revision": "1", "sha256": "a" * 64, "case_count": 1},
        pins={"selection_artifact_path": "selection.json"},
    )
    assert manifest.pins == {"selection_artifact_path": "selection.json"}
    assert "requested_provider" not in manifest.model_dump()


def test_cleanup_observation_digest_is_verified_after_atomic_write(tmp_path: Path) -> None:
    from lme.providers.lifecycle import verify_cleanup_observation

    path = tmp_path / "cleanup.json"
    payload = json.dumps({
        "protocol_version": "1.0.0", "schema_version": 1,
        "artifact_type": "provider-cleanup-observation.v1", "run_id": "run-1",
        "session_id": "session-1", "requested_provider": "fixture", "provider_variant": "observed",
        "namespace": "namespace-1", "cleanup_called": True,
        "required_surface_ids": ["provider-state"],
        "observations": [{"kind": "provider-state", "remaining_record_ids": [], "backend_active": False}],
    }, separators=(",", ":")).encode() + b"\n"
    path.write_bytes(payload)
    assert verify_cleanup_observation(path, hashlib.sha256(payload).hexdigest()) == payload
    with pytest.raises(ValueError, match="digest"):
        verify_cleanup_observation(path, "0" * 64)


@pytest.mark.parametrize(
    "stage",
    (
        "setup", "semantic-probe", "update-probe", "ingest", "scored-retrieve",
        "positive-probe", "absence-probe", "readiness", "reader", "metadata", "persistence",
    ),
)
def test_every_owned_stage_failure_cleans_once_and_terminalizes(tmp_path: Path, stage: str) -> None:
    """The outer owner covers every provider-adjacent stage, not just ingest."""
    from lme.providers.lifecycle import LifecycleRunError, run_provider_lifecycle

    provider = _Provider()
    observed: list[str] = []

    def operation(candidate) -> None:
        for name in (
            "semantic-probe", "update-probe", "ingest", "scored-retrieve", "positive-probe",
            "absence-probe", "readiness", "reader", "metadata", "persistence",
        ):
            observed.append(name)
            if stage == name:
                raise RuntimeError(stage)
        candidate.ingest_case([], None)

    if stage == "setup":
        provider.failure = RuntimeError(stage)
    with pytest.raises(LifecycleRunError) as failure:
        run_provider_lifecycle(
            provider=provider, profile=None, context=_context(tmp_path), binding=_binding(),
            requested_provider="fixture", operation=operation,
        )
    assert provider.cleanups == 1
    assert str(failure.value) == "direct provider lifecycle failed"
    assert failure.value.primary.args == (stage,)
    assert failure.value.fact == (
        "provider_setup_failed" if stage == "setup" else "provider_operation_failed"
    )
    assert failure.value.terminal_status == "INVALID"
    if stage != "setup":
        assert stage in observed


def test_cleanup_and_observation_failures_are_secondary_to_primary_failure(tmp_path: Path) -> None:
    from lme.providers.lifecycle import LifecycleRunError, run_provider_lifecycle

    provider = _Provider()

    def cleanup() -> None:
        provider.cleanups += 1
        raise RuntimeError("cleanup failure")

    provider.cleanup = cleanup  # type: ignore[method-assign]
    with pytest.raises(LifecycleRunError) as failure:
        run_provider_lifecycle(
            provider=provider, profile=None, context=_context(tmp_path), binding=_binding(),
            requested_provider="fixture", operation=lambda _provider: (_ for _ in ()).throw(RuntimeError("primary")),
        )
    assert provider.cleanups == 1
    assert failure.value.primary.args == ("primary",)
    assert failure.value.secondary_facts == ("provider_cleanup_failed",)
    assert failure.value.terminal_status == "INVALID"


def test_cleanup_observation_rejects_missing_duplicate_unobservable_and_still_present_surfaces(tmp_path: Path) -> None:
    from lme.providers.lifecycle import CleanupUnproved, observe_cleanup

    context = _context(tmp_path)
    provider = _Provider()
    bad_surfaces = (
        (),
        ({"kind": "provider-state", "remaining_record_ids": [], "backend_active": False},) * 2,
        ({"kind": "unknown", "fact": "unobservable"},),
        ({"kind": "provider-state", "remaining_record_ids": ["live"], "backend_active": True},),
    )
    for surfaces in bad_surfaces:
        binding = _binding()
        object.__setattr__(binding, "observe", lambda _context, _provider, surfaces=surfaces: surfaces)
        with pytest.raises(CleanupUnproved):
            observe_cleanup(
                context=context, requested_provider="fixture", observed_variant="observed",
                binding=binding, provider=provider, cleanup_called=True,
            )


def test_cleanup_observation_reobserves_snapshot_and_rejects_mutation(tmp_path: Path) -> None:
    from lme.providers.lifecycle import CleanupUnproved, observe_cleanup

    provider = _Provider()
    calls = 0

    def changing(_context, _provider):
        nonlocal calls
        calls += 1
        remaining = [] if calls == 1 else ["mutated"]
        return (
            {"kind": "provider-state", "remaining_record_ids": remaining, "backend_active": False},
            {"kind": "path-lstat", "path": "session-root", "raw_kind": "missing", "entries": []},
        )

    binding = _binding()
    object.__setattr__(binding, "observe", changing)
    with pytest.raises(CleanupUnproved, match="disagree"):
        observe_cleanup(
            context=_context(tmp_path), requested_provider="fixture", observed_variant="observed",
            binding=binding, provider=provider, cleanup_called=True,
        )


def test_evidence_reference_refuses_symlink_components_and_nonregular_final_files(tmp_path: Path) -> None:
    from lme.providers.lifecycle import CleanupUnproved, verify_cleanup_observation

    evidence = tmp_path / "evidence"
    evidence.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    (evidence / "link").symlink_to(outside)
    with pytest.raises(CleanupUnproved, match="symlink"):
        verify_cleanup_observation(evidence / "link", "0" * 64, evidence_root=evidence)
    directory = evidence / "directory"
    directory.mkdir()
    with pytest.raises(CleanupUnproved, match="regular"):
        verify_cleanup_observation(directory, "0" * 64, evidence_root=evidence)


def test_completeness_refuses_missing_duplicate_or_orphan_lifecycle_artifacts(tmp_path: Path) -> None:
    from lme.providers.lifecycle import LifecycleCompletenessError, validate_lifecycle_completeness

    expected = (("session-1", "namespace-1", "observed"),)
    missing = []
    duplicate = [
        {"session_id": "session-1", "namespace": "namespace-1", "provider_variant": "observed", "observation_path": "evidence/a.json", "observation_sha256": "a" * 64},
        {"session_id": "session-1", "namespace": "namespace-1", "provider_variant": "observed", "observation_path": "evidence/b.json", "observation_sha256": "b" * 64},
    ]
    orphan = [{"session_id": "other", "namespace": "namespace-1", "provider_variant": "observed", "observation_path": "evidence/a.json", "observation_sha256": "a" * 64}]
    for records in (missing, duplicate, orphan):
        with pytest.raises(LifecycleCompletenessError):
            validate_lifecycle_completeness(expected_instances=expected, cleanup_records=records, evidence_root=tmp_path)


@pytest.mark.parametrize(
    "field,value",
    (
        ("session_id", "wrong-session"), ("namespace", "wrong-namespace"),
        ("provider_variant", "wrong-variant"), ("observation_path", "../escape.json"),
        ("observation_sha256", "0" * 64),
    ),
)
def test_completeness_binds_trace_identity_and_digest_exactly(tmp_path: Path, field: str, value: str) -> None:
    from lme.providers.lifecycle import LifecycleCompletenessError, validate_lifecycle_completeness

    record = {
        "session_id": "session-1", "namespace": "namespace-1", "provider_variant": "observed",
        "observation_path": "evidence/cleanup.json", "observation_sha256": "a" * 64,
    }
    record[field] = value
    with pytest.raises(LifecycleCompletenessError):
        validate_lifecycle_completeness(
            expected_instances=(("session-1", "namespace-1", "observed"),), cleanup_records=[record], evidence_root=tmp_path,
        )


@pytest.mark.parametrize("mutation", ("missing-trace", "missing-observation", "duplicate", "orphan", "v1-downgrade"))
def test_terminal_manifest_and_report_revalidate_valid_lifecycle_artifacts(
    tmp_path: Path, mutation: str
) -> None:
    """Start valid, then prove both terminal consumers reject lifecycle artifact drift."""
    from lme.reader import StubReader
    from lme.runner import execute_run
    from lme.report import render_run_report
    from protocol.manifest import ManifestError, load_manifest
    from protocol.trace import CaseTraceReader

    with mock.patch.dict(os.environ, {"PROTOCOL_FIXTURE_EMBEDDER": "1", "EXOMEM_DISABLE_EMBEDDINGS": "1"}):
        result = execute_run(
            _mini_config(tmp_path, 2, f"valid-{mutation}", provider="hybrid-rag-control"),
            reader=StubReader(),
        )
    traces = sorted((result.run_dir / "traces").glob("*.jsonl"))
    assert traces
    cleanup = next(record for record in CaseTraceReader(result.run_dir, traces[0].stem) if record.record == "cleanup")
    if mutation == "missing-trace":
        traces[0].unlink()
    elif mutation == "missing-observation":
        (result.run_dir / cleanup.observation_path).unlink()
    elif mutation == "duplicate":
        with traces[0].open("a", encoding="utf-8") as handle:
            handle.write(cleanup.model_dump_json() + "\n")
    elif mutation == "orphan":
        (result.run_dir / "traces" / "orphan.jsonl").write_text(cleanup.model_dump_json() + "\n", encoding="utf-8")
    else:
        rows = (traces[0]).read_text(encoding="utf-8").splitlines()
        rows[0] = json.dumps({"record": "ingest", "session_ordinal": 1, "payload_sha256": "a" * 64, "provider_ids": []})
        traces[0].write_text("\n".join(rows) + "\n", encoding="utf-8")
    with pytest.raises(ManifestError, match="lifecycle|trace"):
        load_manifest(result.run_dir)
    with pytest.raises(ValueError, match="lifecycle|trace|cleanup"):
        render_run_report(result.run_dir, offline=True)


def test_feedback1_registry_binding_never_uses_provider_export_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cleanup facts must come from concrete backing state, not provider evidence."""
    from lme.providers.hybrid_rag_direct import HybridRagDirectProvider
    from lme.providers.registry import provider_spec

    monkeypatch.setenv("PROTOCOL_FIXTURE_EMBEDDER", "1")
    provider = HybridRagDirectProvider()
    provider.export_state = lambda: (_ for _ in ()).throw(AssertionError("provider export hook used"))  # type: ignore[method-assign]
    observations = provider_spec("hybrid-rag-control").runtime_binding.observe(_context(tmp_path), provider)
    assert {item["kind"] for item in observations} == {"namespace-membership", "provider-state", "path-lstat"}


def test_feedback1_completeness_rejects_forged_live_cleanup_state(tmp_path: Path) -> None:
    from lme.providers.lifecycle import LifecycleCompletenessError, validate_lifecycle_completeness

    evidence = tmp_path / "evidence"
    evidence.mkdir()
    payload = {
        "protocol_version": "1.0.0", "schema_version": 1,
        "artifact_type": "provider-cleanup-observation.v1", "run_id": "run-1",
        "session_id": "session-1", "requested_provider": "fixture", "provider_variant": "observed",
        "namespace": "namespace-1", "cleanup_called": True,
        "required_surface_ids": ["provider-state"],
        "observations": [{"kind": "provider-state", "remaining_record_ids": ["live"], "backend_active": False}],
    }
    path = evidence / "cleanup.json"
    bytes_ = json.dumps(payload, separators=(",", ":")).encode() + b"\n"
    path.write_bytes(bytes_)
    record = {
        "session_id": "session-1", "namespace": "namespace-1", "provider_variant": "observed",
        "observation_path": "evidence/cleanup.json", "observation_sha256": hashlib.sha256(bytes_).hexdigest(),
    }
    with pytest.raises(LifecycleCompletenessError, match="absence|live|state"):
        validate_lifecycle_completeness(
            expected_instances=(("session-1", "namespace-1", "observed"),), cleanup_records=[record],
            evidence_root=evidence, run_dir=tmp_path,
        )


def test_feedback1_terminal_direct_manifest_requires_immutable_lifecycle_evidence(tmp_path: Path) -> None:
    from protocol.manifest import ManifestError, finalize_manifest, start_manifest

    start_manifest(
        tmp_path, run_id="run-1", started_at="2026-01-01T00:00:00Z", provider_variant="observed",
        dataset={"id": "fixture", "variant": "mini", "source": "local", "revision": "1", "sha256": "a" * 64, "case_count": 1},
    )
    with pytest.raises(ManifestError, match="lifecycle"):
        finalize_manifest(tmp_path, status="VALID", finalized_at="2026-01-01T00:01:00Z")


def test_feedback1_pre_owned_neutralization_failure_still_cleans_returned_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lme.providers import registry
    from lme.reader import StubReader
    from lme.runner import LmeRunInvalid, execute_run
    import lme.runner as runner

    providers: list[_Provider] = []

    def factory() -> _Provider:
        provider = _Provider()
        providers.append(provider)
        return provider

    monkeypatch.setattr(registry, "provider_spec", lambda _name: _runner_spec(factory))
    monkeypatch.setattr(runner, "neutralize", lambda *_args: (_ for _ in ()).throw(RuntimeError("neutralization failed")))
    with pytest.raises(LmeRunInvalid, match="provider_operation_failed"):
        execute_run(_mini_config(tmp_path, 1, "feedback1-pre-owner", provider="fixture"), reader=StubReader())
    assert len(providers) == 2
    run_dir = tmp_path / "feedback1-pre-owner"
    assert [provider.context.session_id for provider in providers] == [
        _attempt_session(run_dir, None),
        _attempt_session(run_dir, "mini-single-user"),
    ]
    assert all(provider.cleanups == 1 for provider in providers)
    assert json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))["status"] == "INVALID"
    assert not list((run_dir / "evidence").rglob("orphan*.json"))


def test_feedback1_runner_terminalizes_before_reraising_base_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lme.providers import registry
    from lme.reader import StubReader
    from lme.runner import execute_run

    monkeypatch.setattr(registry, "provider_spec", lambda _name: _runner_spec(lambda: _Provider(failure=KeyboardInterrupt())))
    with pytest.raises(KeyboardInterrupt) as interrupted:
        execute_run(_mini_config(tmp_path, 1, "feedback1-base", provider="fixture"), reader=StubReader())
    assert interrupted.value.args == ()
    manifest = json.loads((tmp_path / "feedback1-base" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "INVALID"


def test_feedback1_diagnostics_use_a_separate_lifecycle_session_before_scoring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lme.providers import registry
    from lme.providers.hybrid_rag_direct import HybridRagDirectProvider
    from lme.reader import StubReader
    from lme.runner import execute_run

    contexts = []

    class Recording(HybridRagDirectProvider):
        def setup(self, profile, context):
            contexts.append(context.session_id)
            return super().setup(profile, context)

    monkeypatch.setattr(registry, "provider_spec", lambda _name: _runner_spec(Recording))
    with mock.patch.dict(os.environ, {"PROTOCOL_FIXTURE_EMBEDDER": "1", "EXOMEM_DISABLE_EMBEDDINGS": "1"}):
        execute_run(_mini_config(tmp_path, 2, "feedback1-diagnostic", provider="fixture"), reader=StubReader())
    run_dir = tmp_path / "feedback1-diagnostic"
    assert contexts == [
        _attempt_session(run_dir, None),
        _attempt_session(run_dir, "mini-single-user"),
        _attempt_session(run_dir, "mini-single-assistant_abs"),
    ]


def test_feedback1_variant_drift_after_retrieval_refuses_terminal_validity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lme.providers import registry
    from lme.providers.hybrid_rag_direct import HybridRagDirectProvider
    from lme.reader import StubReader
    from lme.runner import LmeRunInvalid, execute_run

    class Drifting(HybridRagDirectProvider):
        def retrieve(self, question, top_k, purpose):
            self.variant = "drifted"
            return super().retrieve(question, top_k, purpose)

        def variant_id(self):
            return getattr(self, "variant", "observed")

    monkeypatch.setattr(registry, "provider_spec", lambda _name: _runner_spec(Drifting))
    with mock.patch.dict(os.environ, {"PROTOCOL_FIXTURE_EMBEDDER": "1", "EXOMEM_DISABLE_EMBEDDINGS": "1"}):
        with pytest.raises(LmeRunInvalid, match="drift"):
            execute_run(_mini_config(tmp_path, 2, "feedback1-drift", provider="fixture"), reader=StubReader())


def test_feedback1_symlinked_evidence_root_never_receives_cleanup_payload(tmp_path: Path) -> None:
    from lme.providers.lifecycle import CleanupUnproved, run_provider_lifecycle

    context = _context(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    context.evidence_root.rmdir()
    context.evidence_root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(CleanupUnproved, match="symlink"):
        run_provider_lifecycle(
            provider=_Provider(), profile=None, context=context, binding=_binding(),
            requested_provider="fixture", operation=lambda _provider: None,
        )
    assert not (outside / "provider-cleanup-observation.json").exists()


@pytest.mark.parametrize("path", ["/absolute", "../escape", "work\\bad", "./work", "", "work//child"])
def test_feedback1_schema_and_model_agree_on_canonical_cleanup_paths(tmp_path: Path, path: str) -> None:
    from jsonschema import Draft202012Validator
    from pydantic import ValidationError
    from protocol.models import CleanupRecordV2, ProviderCleanupPathLstat, export_json_schemas

    with pytest.raises(ValidationError):
        CleanupRecordV2(
            protocol_version="1.0.0", schema_version=2, run_id="run-1", session_id="session-1",
            namespace="ns", observation_path=path, observation_sha256="a" * 64,
        )
    with pytest.raises(ValidationError):
        ProviderCleanupPathLstat(kind="path-lstat", path=path, raw_kind="missing", entries=[])
    paths = {item.name: item for item in export_json_schemas(tmp_path)}
    schema = json.loads(paths["case-trace.v2.schema.json"].read_text(encoding="utf-8"))
    payload = {"protocol_version": "1.0.0", "schema_version": 2, "case_id": "case", "entries": [{
        "protocol_version": "1.0.0", "schema_version": 2, "record": "cleanup", "run_id": "run-1",
        "session_id": "session-1", "namespace": "ns", "observation_path": path, "observation_sha256": "a" * 64,
    }]}
    assert list(Draft202012Validator(schema).iter_errors(payload))


@pytest.mark.parametrize("name", ["exomem-source-only", "hybrid-rag-control", "no-memory"])
def test_feedback1_provider_specific_observers_do_not_call_export_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    from lme.providers.registry import provider_spec

    monkeypatch.setenv("PROTOCOL_FIXTURE_EMBEDDER", "1")
    provider = provider_spec(name).factory()
    provider.export_state = lambda: (_ for _ in ()).throw(AssertionError("generic export evidence"))  # type: ignore[method-assign]
    observations = provider_spec(name).runtime_binding.observe(_context(tmp_path), provider)
    assert observations


@pytest.mark.parametrize(
    "mutation",
    ("wrong-run", "wrong-requested", "cleanup-false", "required-missing", "namespace-live", "backend-live", "orphan-file"),
)
def test_feedback1_completeness_refuses_every_forged_cleanup_binding(tmp_path: Path, mutation: str) -> None:
    from lme.providers.lifecycle import LifecycleCompletenessError, validate_lifecycle_completeness

    evidence = tmp_path / "evidence"
    evidence.mkdir()
    payload = {
        "protocol_version": "1.0.0", "schema_version": 1, "artifact_type": "provider-cleanup-observation.v1",
        "run_id": "run-1", "session_id": "session-1", "requested_provider": "fixture", "provider_variant": "observed",
        "namespace": "namespace-1", "cleanup_called": True, "required_surface_ids": ["provider-state"],
        "observations": [{"kind": "provider-state", "remaining_record_ids": [], "backend_active": False}],
    }
    if mutation == "wrong-run":
        payload["run_id"] = "wrong"
    elif mutation == "wrong-requested":
        payload["requested_provider"] = "wrong"
    elif mutation == "cleanup-false":
        payload["cleanup_called"] = False
    elif mutation == "required-missing":
        payload["required_surface_ids"] = []
    elif mutation == "namespace-live":
        payload["required_surface_ids"] = ["namespace-membership"]
        payload["observations"] = [{"kind": "namespace-membership", "expected_namespace": "namespace-1", "live_namespaces": ["namespace-1"]}]
    elif mutation == "backend-live":
        payload["observations"] = [{"kind": "provider-state", "remaining_record_ids": [], "backend_active": True}]
    path = evidence / "cleanup.json"
    bytes_ = json.dumps(payload, separators=(",", ":")).encode() + b"\n"
    path.write_bytes(bytes_)
    if mutation == "orphan-file":
        (evidence / "orphan.json").write_bytes(bytes_)
    record = {"session_id": "session-1", "namespace": "namespace-1", "provider_variant": "observed", "observation_path": "evidence/cleanup.json", "observation_sha256": hashlib.sha256(bytes_).hexdigest()}
    with pytest.raises(LifecycleCompletenessError):
        validate_lifecycle_completeness(expected_instances=(("session-1", "namespace-1", "observed"),), cleanup_records=[record], evidence_root=evidence, run_dir=tmp_path)


def test_feedback1_semantic_probe_fact_and_query_are_disjoint_but_related() -> None:
    from protocol.probes import known_answer_probe_specs

    semantic = next(spec for spec in known_answer_probe_specs() if spec.kind == "semantic-zero-overlap")
    assert set(semantic.fact.lower().split()).isdisjoint(semantic.query.lower().split())


def test_feedback1_started_manifest_has_null_variant_until_factory_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lme.providers import registry
    from lme.reader import StubReader
    from lme.runner import LmeRunInvalid, execute_run

    observed = []

    def factory():
        observed.append(json.loads((tmp_path / "feedback1-started-null" / "manifest.json").read_text(encoding="utf-8"))["provider_variant"])
        return _Provider()

    monkeypatch.setattr(registry, "provider_spec", lambda _name: _runner_spec(factory))
    with pytest.raises(LmeRunInvalid):
        execute_run(_mini_config(tmp_path, 1, "feedback1-started-null", provider="fixture"), reader=StubReader())
    assert observed == [None, "observed"]
    environment = json.loads((tmp_path / "feedback1-started-null" / "environment.json").read_text(encoding="utf-8"))
    assert environment["lme"]["requested_provider"] == "fixture"


def test_feedback1_cleanup_record_schema_forbids_claim_fields_and_duplicate_surface_ids(tmp_path: Path) -> None:
    from jsonschema import Draft202012Validator
    from pydantic import ValidationError
    from protocol.models import ProviderCleanupObservation, export_json_schemas

    payload = {"protocol_version": "1.0.0", "schema_version": 1, "artifact_type": "provider-cleanup-observation.v1", "run_id": "run", "session_id": "session", "requested_provider": "fixture", "provider_variant": None, "namespace": "ns", "cleanup_called": True, "required_surface_ids": ["provider-state", "provider-state"], "observations": [{"kind": "provider-state", "remaining_record_ids": [], "backend_active": False}], "verified": True}
    with pytest.raises(ValidationError):
        ProviderCleanupObservation.model_validate(payload)
    schema = json.loads(next(item for item in export_json_schemas(tmp_path) if item.name == "provider-cleanup-observation.v1.schema.json").read_text(encoding="utf-8"))
    assert list(Draft202012Validator(schema).iter_errors(payload))


@pytest.mark.parametrize("provider_name", ["exomem-source-only", "hybrid-rag-control", "no-memory"])
def test_feedback1_provider_observers_recompute_concrete_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider_name: str
) -> None:
    """The observer must inspect concrete state and identify a root symlink by lstat."""
    from lme.providers.registry import provider_spec

    monkeypatch.setenv("PROTOCOL_FIXTURE_EMBEDDER", "1")
    provider = provider_spec(provider_name).factory()
    provider.export_state = lambda: (_ for _ in ()).throw(AssertionError("historical provider state"))  # type: ignore[method-assign]
    context = _context(tmp_path)
    context.work_root.rmdir()
    context.work_root.symlink_to(tmp_path / "outside", target_is_directory=True)
    rows = provider_spec(provider_name).runtime_binding.observe(context, provider)
    root = next(row for row in rows if row["kind"] == "path-lstat")
    assert root["raw_kind"] == "symlink"


@pytest.mark.parametrize("mutation", ["missing", "malformed", "empty", "trace", "observation"])
def test_feedback1_terminal_loader_requires_complete_direct_environment(tmp_path: Path, mutation: str) -> None:
    from lme.reader import StubReader
    from lme.runner import execute_run
    from protocol.manifest import ManifestError, load_manifest
    from protocol.trace import CaseTraceReader

    with mock.patch.dict(os.environ, {"PROTOCOL_FIXTURE_EMBEDDER": "1", "EXOMEM_DISABLE_EMBEDDINGS": "1"}):
        run = execute_run(_mini_config(tmp_path, 2, f"feedback1-env-{mutation}", provider="hybrid-rag-control"), reader=StubReader()).run_dir
    environment = run / "environment.json"
    if mutation == "missing":
        environment.unlink()
    elif mutation == "malformed":
        environment.write_text("not-json", encoding="utf-8")
    elif mutation == "empty":
        payload = json.loads(environment.read_text(encoding="utf-8"))
        payload["lme"]["lifecycle_expected_instances"] = []
        environment.write_text(json.dumps(payload), encoding="utf-8")
    else:
        trace = next((run / "traces").glob("*.jsonl"))
        if mutation == "trace":
            trace.unlink()
        else:
            cleanup = next(row for row in CaseTraceReader(run, trace.stem) if row.record == "cleanup")
            (run / cleanup.observation_path).unlink()
    with pytest.raises(ManifestError, match="lifecycle|environment|cleanup|trace"):
        load_manifest(run)


@pytest.mark.parametrize("boundary", ["neutralize", "leakage", "retrieve"])
def test_feedback1_runner_failure_ledger_covers_all_post_factory_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    from lme.providers import registry
    from lme.reader import StubReader
    from lme.runner import LmeRunInvalid, execute_run
    import lme.runner as runner

    providers: list[_Provider] = []
    class FailingProvider(_Provider):
        def retrieve(self, *_args):
            if boundary == "retrieve":
                raise RuntimeError("retrieve")
            return []

    monkeypatch.setattr(registry, "provider_spec", lambda _name: _runner_spec(lambda: providers.append(FailingProvider()) or providers[-1]))
    if boundary == "neutralize":
        monkeypatch.setattr(runner, "neutralize", lambda *_args: (_ for _ in ()).throw(RuntimeError("neutralize")))
    elif boundary == "leakage":
        monkeypatch.setattr(runner, "scan_ingest", lambda *_args, **_kwargs: [type("Finding", (), {"detector": "forbidden"})()])
    with pytest.raises(LmeRunInvalid):
        execute_run(_mini_config(tmp_path, 1, f"feedback1-ledger-{boundary}", provider="fixture"), reader=StubReader())
    assert providers and providers[0].cleanups == 1


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), SystemExit(7), GeneratorExit(), BaseException("control")])
def test_feedback1_runner_finalizes_then_reraises_all_control_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    from lme.providers import registry
    from lme.reader import StubReader
    from lme.runner import execute_run

    class Interrupting(_Provider):
        def retrieve(self, *_args):
            raise failure

    monkeypatch.setattr(registry, "provider_spec", lambda _name: _runner_spec(Interrupting))
    with pytest.raises(type(failure)) as raised:
        execute_run(_mini_config(tmp_path, 1, f"feedback1-control-{type(failure).__name__}", provider="fixture"), reader=StubReader())
    assert raised.value is failure
    manifest = json.loads((tmp_path / f"feedback1-control-{type(failure).__name__}" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "INVALID"


def test_feedback1_diagnostics_execute_the_declared_contract_in_an_isolated_session() -> None:
    from protocol.probes import diagnostic_probe_events, known_answer_probe_specs

    specs = {spec.kind: spec for spec in known_answer_probe_specs()}
    update = specs["update-current-state"]
    records = {event.case_id: event.content for event in diagnostic_probe_events()}
    assert update.old_marker in records["__probe__-update-current-state"]
    assert update.current_marker in records["__probe__-update-current-state"]
    semantic = specs["semantic-zero-overlap"]
    assert set(semantic.fact.lower().split()).isdisjoint(semantic.query.lower().split())


@pytest.mark.parametrize("phase", ["ingest", "readiness", "before-cleanup", "after-cleanup"])
def test_feedback1_variant_identity_and_drift_matrix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
    from lme.providers import registry
    from lme.providers.hybrid_rag_direct import HybridRagDirectProvider
    from lme.reader import StubReader
    from lme.runner import LmeRunInvalid, execute_run

    class Drifting(HybridRagDirectProvider):
        def ingest_case(self, *args):
            result = super().ingest_case(*args)
            if phase == "ingest": self._drift = True
            return result
        def readiness(self):
            if phase == "readiness": self._drift = True
            return super().readiness()
        def cleanup(self):
            if phase == "before-cleanup": self._drift = True
            super().cleanup()
            if phase == "after-cleanup": self._drift = True
        def variant_id(self): return "drift" if getattr(self, "_drift", False) else super().variant_id()

    monkeypatch.setattr(registry, "provider_spec", lambda _name: _runner_spec(Drifting))
    with mock.patch.dict(os.environ, {"PROTOCOL_FIXTURE_EMBEDDER": "1", "EXOMEM_DISABLE_EMBEDDINGS": "1"}):
        with pytest.raises(LmeRunInvalid, match="drift"):
            execute_run(_mini_config(tmp_path, 2, f"feedback1-drift-{phase}", provider="fixture"), reader=StubReader())


@pytest.mark.parametrize("attack", ["root", "intermediate", "existing", "nonregular"])
def test_feedback1_dirfd_evidence_io_refuses_or_contains_races(tmp_path: Path, attack: str) -> None:
    from lme.providers.lifecycle import CleanupUnproved, run_provider_lifecycle

    context = _context(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    if attack == "root":
        context.evidence_root.rmdir()
        context.evidence_root.symlink_to(outside, target_is_directory=True)
    elif attack == "intermediate":
        (context.evidence_root / "nested").symlink_to(outside, target_is_directory=True)
    elif attack == "existing":
        (context.evidence_root / "provider-cleanup-observation.json").write_text("occupied", encoding="utf-8")
    else:
        (context.evidence_root / "provider-cleanup-observation.json").mkdir()
    if attack == "intermediate":
        run_provider_lifecycle(
            provider=_Provider(), profile=None, context=context, binding=_binding(),
            requested_provider="fixture", operation=lambda _provider: None,
        )
    else:
        with pytest.raises(CleanupUnproved):
            run_provider_lifecycle(provider=_Provider(), profile=None, context=context, binding=_binding(), requested_provider="fixture", operation=lambda _provider: None)
    assert not (outside / "provider-cleanup-observation.json").exists()


def _mini_dataset(tmp_path: Path, count: int) -> Path:
    rows = json.loads(Path("benchmarks/lme/fixtures/mini.json").read_text(encoding="utf-8"))[:count]
    path = tmp_path / f"{count}-case.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    return path


def _mini_config(tmp_path: Path, count: int, run_id: str, *, provider: str) -> object:
    from lme.runner import RunConfig

    dataset = _mini_dataset(tmp_path, count)
    return RunConfig(
        dataset=dataset, dataset_sha256=hashlib.sha256(dataset.read_bytes()).hexdigest(),
        dataset_revision="test-fixture", pilot=count, out=tmp_path, run_id=run_id, provider=provider,
    )


def _runner_spec(factory):
    from lme.providers.base import ProviderDescriptor, ProviderSpec

    return ProviderSpec(
        factory=factory,
        descriptor=ProviderDescriptor(
            provider_id="fixture",
            execution_model="in-process-no-post-return-background",
        ),
        namespace_kind="fixture",
        derive_namespace=lambda run_id, session_id: f"fixture-{run_id}-{session_id}",
        runtime_binding=_binding(),
    )


def test_one_case_isolation_records_typed_na_without_a_foreign_query(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lme.providers import registry
    from lme.reader import StubReader
    from lme.runner import LmeRunInvalid, execute_run

    queries: list[tuple[str, str]] = []

    class Recording(_Provider):
        def ingest_case(self, events, handle):
            self.token = next((event.content.rsplit(": ", 1)[-1].rstrip(".") for event in events if "canary-presence-" in event.content), None)
            return ()

        def retrieve(self, question, top_k, purpose):
            queries.append((purpose.value, question))
            return []

    monkeypatch.setattr(registry, "provider_spec", lambda _name: _runner_spec(Recording))
    with mock.patch.dict(os.environ, {"EXOMEM_DISABLE_EMBEDDINGS": "1"}):
        with pytest.raises(LmeRunInvalid, match="unverifiable") as rejected:
            execute_run(
            _mini_config(tmp_path, 1, "one-case", provider="fixture"),
                reader=StubReader(),
            )
    run_dir = rejected.value.run_dir
    isolation = [json.loads(line) for line in (run_dir / "isolation.jsonl").read_text(encoding="utf-8").splitlines()]
    assert isolation == [{"case_ordinal": 1, "prior_case": "not-applicable-no-prior-case"}]
    absence_queries = [query for purpose, query in queries if purpose == "absence-probe-expected-empty"]
    from protocol.canary import canary_for

    assert absence_queries == [canary_for("one-case", "mini-single-user", "never_ingested")]
    assert all("canary-presence-" not in query for query in absence_queries)
    assert json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))["status"] != "VALID"


def test_two_cases_probe_the_actual_prior_token_and_shared_state_invalidates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lme.providers import registry
    from lme.reader import StubReader
    from lme.runner import LmeRunInvalid, execute_run

    ingested: list[str] = []
    absence_queries: list[str] = []

    class Leaky(_Provider):
        shared: list[str] = []

        def ingest_case(self, events, handle):
            token = next((event.content.rsplit(": ", 1)[-1].rstrip(".") for event in events if "canary-presence-" in event.content), None)
            if token is not None:
                ingested.append(token)
                self.shared.append(token)
            return ()

        def retrieve(self, question, top_k, purpose):
            if purpose.value == "absence-probe-expected-empty":
                absence_queries.append(question)
            return [type("Hit", (), {"hit_id": "leak", "text": token, "score": 1.0})() for token in self.shared if token in question]

        def cleanup(self):
            self.cleanups += 1
            # Deliberately does not clear class-owned shared state.

    monkeypatch.setattr(registry, "provider_spec", lambda _name: _runner_spec(Leaky))
    with mock.patch.dict(os.environ, {"EXOMEM_DISABLE_EMBEDDINGS": "1"}):
        with pytest.raises(LmeRunInvalid, match="contamination") as rejected:
            execute_run(
            _mini_config(tmp_path, 2, "two-case", provider="fixture"),
                reader=StubReader(),
            )
    assert len(ingested) == 2
    from protocol.canary import canary_for

    expected = {
        canary_for("two-case", "mini-single-user", "never_ingested"),
        ingested[0],
        canary_for("two-case", "mini-single-assistant_abs", "never_ingested"),
    }
    assert set(absence_queries) == expected and len(absence_queries) == len(expected)
    assert [query for query in absence_queries if "canary-presence-" in query] == [ingested[0]]
    assert json.loads((rejected.value.run_dir / "manifest.json").read_text(encoding="utf-8"))["status"] == "INVALID"


@pytest.mark.parametrize("stage", ("setup", "ingest", "scored-retrieve", "readiness", "reader", "trace-persistence"))
def test_runner_delegates_representative_failures_to_one_lifecycle_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    """This covers runner wiring; the parameterized unit test covers every inner stage."""
    import lme.runner as runner
    from lme.providers import registry
    from lme.runner import LmeRunInvalid, execute_run

    instances: list[object] = []

    class Failing(_Provider):
        def __init__(self):
            super().__init__()
            instances.append(self)

        def setup(self, profile, context):
            super().setup(profile, context)
            if stage == "setup":
                raise RuntimeError("setup")

        def ingest_case(self, events, handle):
            if stage == "ingest":
                raise RuntimeError("ingest")
            return ()

        def retrieve(self, question, top_k, purpose):
            if stage == "scored-retrieve" and purpose.value == "scored-retrieval":
                raise RuntimeError("scored-retrieve")
            return []

        def readiness(self):
            if stage == "readiness":
                raise RuntimeError("readiness")
            return []

    class FailingReader:
        name = "failing"

        def answer(self, _question, _hits):
            if stage == "reader":
                raise RuntimeError("reader")
            return "answer"

    monkeypatch.setattr(registry, "provider_spec", lambda _name: _runner_spec(Failing))
    if stage == "trace-persistence":
        real_writer = runner.CaseTraceWriter

        class FailingWriter(real_writer):
            def append(self, entry):
                if entry.get("record") == "search":
                    raise RuntimeError("trace-persistence")
                return super().append(entry)

        monkeypatch.setattr(runner, "CaseTraceWriter", FailingWriter)
    with mock.patch.dict(os.environ, {"EXOMEM_DISABLE_EMBEDDINGS": "1"}):
        expected_code = (
            "provider_setup_failed" if stage == "setup" else "provider_operation_failed"
        )
        with pytest.raises(LmeRunInvalid, match=expected_code) as rejected:
            execute_run(
                _mini_config(tmp_path, 2, f"runner-{stage}", provider="fixture"),
                reader=FailingReader(),
            )
    assert instances and all(instance.cleanups == 1 for instance in instances)
    assert json.loads((rejected.value.run_dir / "manifest.json").read_text(encoding="utf-8"))["status"] == "INVALID"


def test_probe_contract_uses_semantic_fact_opaque_markers_and_user_role() -> None:
    from protocol.probes import diagnostic_probe_events, known_answer_probe_specs

    probes = {probe.kind: probe for probe in known_answer_probe_specs()}
    semantic = probes["semantic-zero-overlap"]
    assert semantic.fact and semantic.query and semantic.fact != semantic.query
    update = probes["update-current-state"]
    assert update.old_marker.startswith("revision-") and update.current_marker.startswith("revision-")
    assert update.old_marker != update.current_marker
    events = diagnostic_probe_events()
    assert events and {event.role for event in events} == {"user"}


def test_update_classification_uses_exact_opaque_membership_not_keywords() -> None:
    from protocol.probes import classify_update_outcome

    assert classify_update_outcome([{"record_id": "revision-current-opaque"}], old_marker="revision-old-opaque", current_marker="revision-current-opaque") == "superseded"
    assert classify_update_outcome([{"record_id": "current sounds old"}], old_marker="revision-old-opaque", current_marker="revision-current-opaque") == "unresolvable"


def test_new_lifecycle_models_export_and_schema_drift_is_checked(tmp_path: Path) -> None:
    from jsonschema import Draft202012Validator
    from pydantic import TypeAdapter, ValidationError
    from protocol.models import ProviderCleanupObservation, TraceRecordV2
    from protocol.models import export_json_schemas

    paths = {path.name: path for path in export_json_schemas(tmp_path)}
    assert "provider-cleanup-observation.v1.schema.json" in paths
    assert ProviderCleanupObservation.model_json_schema()["properties"]["artifact_type"]["const"] == "provider-cleanup-observation.v1"
    observation = {
        "protocol_version": "1.0.0", "schema_version": 1,
        "artifact_type": "provider-cleanup-observation.v1", "run_id": "run-1",
        "session_id": "session-1", "requested_provider": "fixture", "provider_variant": "observed",
        "namespace": "namespace-1", "cleanup_called": True,
        "required_surface_ids": ["provider-state", "session-root"],
        "observations": [
            {"kind": "provider-state", "remaining_record_ids": [], "backend_active": False},
            {"kind": "path-lstat", "path": "session-root", "raw_kind": "missing", "entries": []},
        ],
    }
    ProviderCleanupObservation.model_validate(observation)
    schema = json.loads(paths["provider-cleanup-observation.v1.schema.json"].read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    assert list(validator.iter_errors(observation)) == []
    for field, value in (("verified", True), ("absent", True), ("required_surface_ids", ["provider-state", "provider-state"])):
        adversarial = dict(observation)
        adversarial[field] = value
        with pytest.raises(ValidationError):
            ProviderCleanupObservation.model_validate(adversarial)
        assert list(validator.iter_errors(adversarial))
    record = {
        "protocol_version": "1.0.0", "schema_version": 2, "record": "cleanup",
        "run_id": "run-1", "session_id": "session-1", "namespace": "namespace-1",
        "observation_path": "evidence/cleanup.json", "observation_sha256": "a" * 64,
    }
    TypeAdapter(TraceRecordV2).validate_python(record)
    trace_schema = json.loads(paths["case-trace.v2.schema.json"].read_text(encoding="utf-8"))
    trace_validator = Draft202012Validator(trace_schema)
    assert list(trace_validator.iter_errors({"case_id": "case-1", "entries": [record]})) == []
    malformed = dict(record)
    malformed.pop("session_id")
    with pytest.raises(ValidationError):
        TypeAdapter(TraceRecordV2).validate_python(malformed)
    assert list(trace_validator.iter_errors({"case_id": "case-1", "entries": [malformed]}))


def test_extra_manifest_pins_still_refuse_after_lifecycle_metadata(tmp_path: Path) -> None:
    from protocol.manifest import ManifestError, start_manifest

    with pytest.raises(ManifestError, match="pins"):
        start_manifest(
            tmp_path, run_id="run-1", started_at="2026-01-01T00:00:00Z",
            dataset={"id": "fixture", "variant": "mini", "source": "local", "revision": "1", "sha256": "a" * 64, "case_count": 1},
            pins={"requested_provider": "fixture"},
        )


def test_dataset_roles_are_preserved_while_harness_diagnostic_events_are_user_only() -> None:
    from lme.dataset import load_dataset
    from lme.normalize import neutralize
    from protocol.models import DatasetIdentity
    from protocol.probes import diagnostic_probe_events

    question = load_dataset(Path("benchmarks/lme/fixtures/mini.json")).questions[0]
    identity = DatasetIdentity(
        id="fixture", variant="mini", source="local", revision="1", sha256="a" * 64, case_count=1,
    )
    dataset_roles = [message.role for session in question.sessions for message in session.messages]
    normalized_roles = [event.role for event in neutralize(question, identity)]
    assert normalized_roles == dataset_roles
    assert {event.role for event in diagnostic_probe_events()} == {"user"}


def _feedback2_offline_profile():
    from membench.adapters.base import Profile

    return Profile(
        name="feedback2-offline",
        settings={
            "EXOMEM_DISABLE_EMBEDDINGS": "1",
            "EXOMEM_DISABLE_WARMUP": "1",
            "EXOMEM_DISABLE_FILE_WATCHER": "1",
            "EXOMEM_DISABLE_MODE_WATCH": "1",
            "EXOMEM_DISABLE_CORPUS_CACHE": "1",
            "EXOMEM_DISABLE_CLIP": "1",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
        },
    )


def _feedback2_context(tmp_path: Path, label: str):
    from lme.providers.base import ProviderSessionContext

    work_root = tmp_path / "work"
    evidence_root = tmp_path / "evidence"
    work_root.mkdir(parents=True)
    evidence_root.mkdir(parents=True)
    return ProviderSessionContext(
        run_id=f"feedback2-{label}",
        session_id=f"{label}-session",
        namespace=f"{label}-namespace",
        work_root=work_root,
        evidence_root=evidence_root,
    )


class _Feedback2RememberingProvider(_Provider):
    """Small real-boundary provider used to keep runner attacks deterministic."""

    def __init__(self, *, variant: str = "observed") -> None:
        super().__init__(variant=variant)
        self.records: list[str] = []

    def ingest_case(self, events, _handle):
        self.records.extend(event.content for event in events)
        return tuple(f"record-{index}" for index, _event in enumerate(events))

    def retrieve(self, question, _top_k, purpose):
        from lme.providers.base import ProviderHit, RetrievalPurpose

        if purpose is RetrievalPurpose.ABSENCE_PROBE_EXPECTED_EMPTY:
            return []
        matches = [record for record in self.records if question in record]
        selected = matches[:1] or (self.records[:1] if purpose is RetrievalPurpose.POSITIVE_PROBE else [])
        return [ProviderHit(f"hit-{index}", text, 1.0) for index, text in enumerate(selected)]


def _feedback2_cleanup_fixture(tmp_path: Path) -> tuple[dict[str, object], dict[str, object], Path]:
    from protocol.models import ProviderCleanupObservation

    run_dir = tmp_path / "run"
    path = run_dir / "evidence" / "session-1" / "cleanup.attestation"
    path.parent.mkdir(parents=True)
    observation = ProviderCleanupObservation(
        run_id="run-1",
        session_id="session-1",
        requested_provider="fixture",
        provider_variant="observed",
        namespace="namespace-1",
        cleanup_called=True,
        required_surface_ids=["provider-state"],
        observations=[
            {"kind": "provider-state", "remaining_record_ids": [], "backend_active": False}
        ],
    )
    payload = observation.model_dump_json(indent=2).encode() + b"\n"
    path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    observation_path = path.relative_to(run_dir).as_posix()
    expected = {
        "run_id": "run-1",
        "requested_provider": "fixture",
        "session_id": "session-1",
        "namespace": "namespace-1",
        "provider_variant": "observed",
        "required_surface_ids": ["provider-state"],
        "observation_path": observation_path,
        "observation_sha256": digest,
    }
    record = {
        "run_id": "run-1",
        "requested_provider": "fixture",
        "session_id": "session-1",
        "namespace": "namespace-1",
        "observation_path": observation_path,
        "observation_sha256": digest,
    }
    return expected, record, run_dir


def test_feedback2_exomem_observer_uses_real_vault_bytes_not_allocated_wrapper(
    tmp_path: Path,
) -> None:
    """A removed real vault is absent even while the adapter still holds its Path."""
    from lme.dataset import load_dataset
    from lme.normalize import neutralize
    from lme.providers.lifecycle import CleanupUnproved, run_provider_lifecycle
    from lme.providers.registry import provider_spec
    from protocol.models import CaseHandle, DatasetIdentity

    question = load_dataset(Path("benchmarks/lme/fixtures/mini.json")).questions[0]
    identity = DatasetIdentity(
        id="fixture",
        variant="mini",
        source="local",
        revision="feedback2",
        sha256=hashlib.sha256(Path("benchmarks/lme/fixtures/mini.json").read_bytes()).hexdigest(),
        case_count=6,
    )
    events = neutralize(question, identity)
    handle = CaseHandle(
        case_id=question.question_id,
        case_ordinal=1,
        question_date=question.question_date_text,
    )
    spec = provider_spec("exomem-source-only")
    clean_context = _feedback2_context(tmp_path / "clean", "exomem-clean")
    _result, _path, _digest, variant = run_provider_lifecycle(
        provider=spec.factory(),
        profile=_feedback2_offline_profile(),
        context=clean_context,
        binding=spec.runtime_binding,
        requested_provider="exomem-source-only",
        operation=lambda provider: provider.ingest_case(events, handle),
    )
    assert variant == "exomem-source-only"
    assert clean_context.work_root.is_dir()
    assert list(clean_context.work_root.iterdir()) == []

    retained_context = _feedback2_context(tmp_path / "retained", "exomem-retained")
    retained_provider = spec.factory()
    real_cleanup = retained_provider.cleanup

    def retain_source_bytes() -> None:
        real_cleanup()
        source = retained_context.work_root / "vault" / "Knowledge Base" / "Sources" / "Other" / "retained.md"
        source.parent.mkdir(parents=True)
        source.write_text("retained provider bytes", encoding="utf-8")

    retained_provider.cleanup = retain_source_bytes  # type: ignore[method-assign]
    with pytest.raises(CleanupUnproved, match="absence|state|remain"):
        run_provider_lifecycle(
            provider=retained_provider,
            profile=_feedback2_offline_profile(),
            context=retained_context,
            binding=spec.runtime_binding,
            requested_provider="exomem-source-only",
            operation=lambda provider: provider.ingest_case(events, handle),
        )


def test_feedback2_hybrid_observer_reads_chunk_and_index_containers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lme.dataset import load_dataset
    from lme.normalize import neutralize
    from lme.providers.registry import provider_spec
    from protocol.models import CaseHandle, DatasetIdentity

    monkeypatch.setenv("PROTOCOL_FIXTURE_EMBEDDER", "1")
    spec = provider_spec("hybrid-rag-control")
    provider = spec.factory()
    context = _feedback2_context(tmp_path, "hybrid")
    question = load_dataset(Path("benchmarks/lme/fixtures/mini.json")).questions[0]
    identity = DatasetIdentity(
        id="fixture", variant="mini", source="local", revision="feedback2",
        sha256="a" * 64, case_count=6,
    )
    handle = CaseHandle(case_id=question.question_id, case_ordinal=1, question_date=question.question_date_text)
    provider.setup(None, context)
    inserted = provider.ingest_case(neutralize(question, identity), handle)
    before = spec.runtime_binding.observe(context, provider)
    state = next(item for item in before if item["kind"] == "provider-state")
    assert state == {
        "kind": "provider-state",
        "remaining_record_ids": sorted(inserted),
        "backend_active": True,
    }
    provider.cleanup()
    after = spec.runtime_binding.observe(context, provider)
    state = next(item for item in after if item["kind"] == "provider-state")
    assert state["remaining_record_ids"] == [] and state["backend_active"] is False


def test_feedback2_null_observer_fails_closed_on_unexpected_retained_attributes(
    tmp_path: Path,
) -> None:
    from lme.providers.lifecycle import CleanupUnproved, run_provider_lifecycle
    from lme.providers.registry import provider_spec

    spec = provider_spec("no-memory")
    provider = spec.factory()
    provider.retained_state = {"record": "still-live"}
    with pytest.raises(CleanupUnproved, match="state|shape|attribute|unobservable"):
        run_provider_lifecycle(
            provider=provider,
            profile=None,
            context=_feedback2_context(tmp_path, "null"),
            binding=spec.runtime_binding,
            requested_provider="no-memory",
            operation=lambda _provider: None,
        )


@pytest.mark.parametrize(
    "field,value",
    (
        ("run_id", "wrong-run"),
        ("requested_provider", "wrong-provider"),
        ("session_id", "wrong-session"),
        ("namespace", "wrong-namespace"),
        ("provider_variant", "wrong-variant"),
        ("required_surface_ids", ["namespace-membership"]),
        ("observation_path", "evidence/session-1/wrong.attestation"),
        ("observation_sha256", "0" * 64),
    ),
)
def test_feedback2_expected_instance_binds_the_full_cleanup_identity(
    tmp_path: Path, field: str, value: object
) -> None:
    from lme.providers.lifecycle import LifecycleCompletenessError, validate_lifecycle_completeness

    expected, record, run_dir = _feedback2_cleanup_fixture(tmp_path)
    validate_lifecycle_completeness(
        expected_instances=(expected,),
        cleanup_records=[record],
        evidence_root=run_dir / "evidence",
        run_dir=run_dir,
    )
    forged = dict(expected)
    forged[field] = value
    with pytest.raises(LifecycleCompletenessError):
        validate_lifecycle_completeness(
            expected_instances=(forged,),
            cleanup_records=[record],
            evidence_root=run_dir / "evidence",
            run_dir=run_dir,
        )


def test_feedback2_non_json_cleanup_observation_is_still_an_orphan(
    tmp_path: Path,
) -> None:
    from lme.reader import StubReader
    from lme.runner import execute_run
    from protocol.manifest import ManifestError, load_manifest
    from protocol.trace import CaseTraceReader

    with mock.patch.dict(
        os.environ,
        {"PROTOCOL_FIXTURE_EMBEDDER": "1", "EXOMEM_DISABLE_EMBEDDINGS": "1"},
    ):
        run = execute_run(
            _mini_config(tmp_path, 2, "feedback2-non-json-orphan", provider="hybrid-rag-control"),
            reader=StubReader(),
        ).run_dir
    trace = next((run / "traces").glob("*.jsonl"))
    cleanup = next(row for row in CaseTraceReader(run, trace.stem) if row.record == "cleanup")
    payload = (run / cleanup.observation_path).read_bytes()
    (run / "evidence" / "unreferenced-cleanup.attestation").write_bytes(payload)
    with pytest.raises(ManifestError, match="orphan|lifecycle|cleanup"):
        load_manifest(run)


def test_feedback2_blocking_leakage_after_factory_return_is_owned_and_ledgered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import lme.runner as runner
    from lme.providers import registry
    from lme.reader import StubReader
    from lme.runner import LmeRunInvalid, execute_run
    from protocol.trace import CaseTraceReader

    providers: list[_Provider] = []

    def factory() -> _Provider:
        providers.append(_Provider())
        return providers[-1]

    monkeypatch.setattr(registry, "provider_spec", lambda _name: _runner_spec(factory))
    monkeypatch.setattr(
        runner,
        "scan_ingest",
        lambda *_args, **_kwargs: [type("Finding", (), {"detector": "gold-answer"})()],
    )
    with pytest.raises(LmeRunInvalid, match="provider_operation_failed") as rejected:
        execute_run(
            _mini_config(tmp_path, 1, "feedback2-owned-leakage", provider="fixture"),
            reader=StubReader(),
        )
    assert len(providers) == 2
    assert [provider.cleanups for provider in providers] == [1, 1]
    environment = json.loads((rejected.value.run_dir / "environment.json").read_text(encoding="utf-8"))
    expected = environment["lme"]["lifecycle_expected_instances"]
    returned = {
        item["internal_session_id"]
        for item in environment["lme"]["lifecycle_attempts"]
        if item["factory_returned"]
    }
    assert {item["session_id"] for item in expected} == returned
    trace = list(CaseTraceReader(
        rejected.value.run_dir,
        _attempt_session(rejected.value.run_dir, "mini-single-user"),
    ))
    assert len([row for row in trace if row.record == "cleanup"]) == 1


@pytest.mark.parametrize("failure_mode", ("retrieve", "cleanup"))
def test_feedback2_failure_ledger_is_written_for_operation_and_cleanup_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_mode: str
) -> None:
    from lme.providers import registry
    from lme.providers.base import RetrievalPurpose
    from lme.reader import StubReader
    from lme.runner import LmeRunInvalid, execute_run
    from protocol.trace import CaseTraceReader

    calls = 0

    class Failing(_Feedback2RememberingProvider):
        def retrieve(self, question, top_k, purpose):
            if failure_mode == "retrieve" and purpose is RetrievalPurpose.SCORED_RETRIEVAL:
                raise RuntimeError("feedback2 retrieve failure")
            return super().retrieve(question, top_k, purpose)

        def cleanup(self) -> None:
            super().cleanup()
            if failure_mode == "cleanup":
                raise RuntimeError("feedback2 cleanup failure")

    def factory():
        nonlocal calls
        calls += 1
        return _Feedback2RememberingProvider() if calls == 1 else Failing()

    monkeypatch.setattr(registry, "provider_spec", lambda _name: _runner_spec(factory))
    failure_code = (
        "provider_operation_failed" if failure_mode == "retrieve" else "provider_cleanup_failed"
    )
    with pytest.raises(LmeRunInvalid, match=failure_code) as rejected:
        execute_run(
            _mini_config(tmp_path, 1, f"feedback2-ledger-{failure_mode}", provider="fixture"),
            reader=StubReader(),
        )
    environment = json.loads((rejected.value.run_dir / "environment.json").read_text(encoding="utf-8"))
    expected = environment["lme"]["lifecycle_expected_instances"]
    returned = {
        item["internal_session_id"]
        for item in environment["lme"]["lifecycle_attempts"]
        if item["factory_returned"]
    }
    assert {item["session_id"] for item in expected} == returned
    assert all(
        set(item) == {
            "run_id", "requested_provider", "session_id", "namespace", "provider_variant",
            "required_surface_ids", "observation_path", "observation_sha256",
        }
        for item in expected
    )
    scored_session = _attempt_session(rejected.value.run_dir, "mini-single-user")
    cleanup_rows = [
        row for row in CaseTraceReader(rejected.value.run_dir, scored_session)
        if row.record == "cleanup"
    ]
    assert len(cleanup_rows) == 1
    expected_case = next(item for item in expected if item["session_id"] == scored_session)
    assert cleanup_rows[0].observation_path == expected_case["observation_path"]
    assert cleanup_rows[0].observation_sha256 == expected_case["observation_sha256"]


@pytest.mark.parametrize("failed_factory", ("diagnostic", "scored"))
def test_feedback2_constructor_control_flow_keeps_exact_identity_after_root_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_factory: str
) -> None:
    from lme.providers import registry
    from lme.reader import StubReader
    from lme.runner import execute_run

    class ConstructorControl(BaseException):
        pass

    failure = ConstructorControl(failed_factory)
    run_id = f"feedback2-constructor-{failed_factory}"
    calls = 0
    constructed_sessions: list[str] = []

    def factory():
        nonlocal calls
        calls += 1
        work_children = list((tmp_path / run_id / "work").iterdir())
        assert len(work_children) == 1
        session = work_children[0].name
        constructed_sessions.append(session)
        work = tmp_path / run_id / "work" / session
        evidence = tmp_path / run_id / "evidence" / session
        assert work.is_dir() and evidence.is_dir()
        if failed_factory == "diagnostic" or calls == 2:
            raise failure
        return _Feedback2RememberingProvider()

    monkeypatch.setattr(registry, "provider_spec", lambda _name: _runner_spec(factory))
    with pytest.raises(ConstructorControl) as raised:
        execute_run(_mini_config(tmp_path, 1, run_id, provider="fixture"), reader=StubReader())
    assert raised.value is failure
    failed_session = constructed_sessions[-1]
    assert not (tmp_path / run_id / "work" / failed_session).exists()
    assert not (tmp_path / run_id / "evidence" / failed_session).exists()
    manifest = json.loads((tmp_path / run_id / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "INVALID"


def test_feedback2_ordinary_diagnostic_constructor_failure_is_typed_and_cleans_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lme.providers import registry
    from lme.reader import StubReader
    from lme.runner import LmeRunInvalid, execute_run

    run_id = "feedback2-constructor-ordinary"
    calls = 0
    failed_session: str | None = None

    def factory():
        nonlocal calls, failed_session
        calls += 1
        if calls == 1:
            work_children = list((tmp_path / run_id / "work").iterdir())
            assert len(work_children) == 1
            failed_session = work_children[0].name
            assert (tmp_path / run_id / "evidence" / failed_session).is_dir()
            raise RuntimeError("ordinary constructor")
        return _Feedback2RememberingProvider()

    monkeypatch.setattr(registry, "provider_spec", lambda _name: _runner_spec(factory))
    with pytest.raises(LmeRunInvalid, match="provider_constructor_failed") as rejected:
        execute_run(_mini_config(tmp_path, 1, run_id, provider="fixture"), reader=StubReader())
    assert calls == 1
    assert failed_session is not None
    assert not (rejected.value.run_dir / "work" / failed_session).exists()
    assert not (rejected.value.run_dir / "evidence" / failed_session).exists()


def test_feedback2_cleanup_only_control_flow_is_primary_after_finalized_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lme.providers import registry
    from lme.reader import StubReader
    from lme.runner import execute_run
    from protocol.trace import CaseTraceReader

    class CleanupControl(BaseException):
        pass

    failure = CleanupControl("cleanup control")
    calls = 0

    class InterruptingCleanup(_Feedback2RememberingProvider):
        def cleanup(self) -> None:
            super().cleanup()
            raise failure

    def factory():
        nonlocal calls
        calls += 1
        return _Feedback2RememberingProvider() if calls == 1 else InterruptingCleanup()

    monkeypatch.setattr(registry, "provider_spec", lambda _name: _runner_spec(factory))
    run_id = "feedback2-cleanup-control"
    with pytest.raises(CleanupControl) as raised:
        execute_run(_mini_config(tmp_path, 1, run_id, provider="fixture"), reader=StubReader())
    assert raised.value is failure
    run = tmp_path / run_id
    assert json.loads((run / "manifest.json").read_text(encoding="utf-8"))["status"] == "INVALID"
    expected = json.loads((run / "environment.json").read_text(encoding="utf-8"))["lme"]["lifecycle_expected_instances"]
    returned = {
        item["internal_session_id"]
        for item in _attempts(run)
        if item["factory_returned"]
    }
    assert {item["session_id"] for item in expected} == returned
    scored_session = _attempt_session(run, "mini-single-user")
    assert len([row for row in CaseTraceReader(run, scored_session) if row.record == "cleanup"]) == 1


def test_feedback2_semantic_probe_rejects_an_unrelated_hit() -> None:
    from lme.providers.base import ProviderHit
    from lme.runner import _run_probes
    from protocol.models import DatasetIdentity
    from protocol.probes import known_answer_probe_specs

    semantic_query = next(
        spec.query for spec in known_answer_probe_specs() if spec.kind == "semantic-zero-overlap"
    )

    class Unrelated(_Provider):
        def retrieve(self, question, _top_k, _purpose):
            if question == semantic_query:
                return [ProviderHit("unrelated", "an unrelated memory", 1.0)]
            return []

    results, _readiness = _run_probes(
        Unrelated(),
        DatasetIdentity(
            id="diagnostic", variant="fixture", source="local", revision="1",
            sha256="0" * 64, case_count=0,
        ),
        "feedback2-semantic",
    )
    semantic = next(result for result in results if result.probe_kind == "semantic-zero-overlap")
    assert semantic.outcome == "fail"
    assert semantic.hits == ["unrelated"]


def test_feedback2_update_probe_uses_declared_sequence_neutral_query_and_exact_markers() -> None:
    from lme.providers.base import ProviderHit
    from lme.runner import _run_probes
    from protocol.models import DatasetIdentity
    from protocol.probes import known_answer_probe_specs

    update = next(spec for spec in known_answer_probe_specs() if spec.kind == "update-current-state")
    ingested: list[tuple[str, list[str]]] = []
    queries: list[str] = []

    class Echo(_Provider):
        def ingest_case(self, events, handle):
            ingested.append((handle.case_id, [event.content for event in events]))
            return ()

        def retrieve(self, question, _top_k, _purpose):
            queries.append(question)
            return [ProviderHit("echo", question, 1.0)]

    results, _readiness = _run_probes(
        Echo(),
        DatasetIdentity(
            id="diagnostic", variant="fixture", source="local", revision="1",
            sha256="0" * 64, case_count=0,
        ),
        "feedback2-update",
    )
    update_writes = [contents for case_id, contents in ingested if case_id == "__probe__-update-current-state"]
    assert update.query not in {update.old_marker, update.current_marker}
    assert update_writes == [[update.old_marker], [update.current_marker]]
    assert queries[-1] == update.query
    outcome = next(result for result in results if result.probe_kind == "update-current-state")
    assert outcome.outcome == "unresolvable"


def test_feedback2_observed_variant_is_immutable_across_all_run_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lme.providers import registry
    from lme.reader import StubReader
    from lme.runner import LmeRunInvalid, execute_run

    calls = 0

    def factory():
        nonlocal calls
        calls += 1
        return _Feedback2RememberingProvider(variant="diagnostic-variant" if calls == 1 else "scored-variant")

    monkeypatch.setattr(registry, "provider_spec", lambda _name: _runner_spec(factory))
    with pytest.raises(LmeRunInvalid, match="variant|drift"):
        execute_run(
            _mini_config(tmp_path, 2, "feedback2-global-variant", provider="fixture"),
            reader=StubReader(),
        )


def test_feedback2_existing_final_race_cannot_be_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import protocol.custody as custody_module
    from lme.providers.lifecycle import CleanupUnproved, run_provider_lifecycle

    real_link = custody_module.os.link
    raced = False

    def racing_link(src, dst, *, src_dir_fd=None, dst_dir_fd=None, follow_symlinks=True):
        nonlocal raced
        raced = True
        final = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dst_dir_fd)
        try:
            os.write(final, b"racer-owned")
        finally:
            os.close(final)
        return real_link(
            src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(custody_module.os, "link", racing_link)
    context = _feedback2_context(tmp_path, "existing-final-race")
    with pytest.raises(CleanupUnproved, match="publication"):
        run_provider_lifecycle(
            provider=_Provider(), profile=None, context=context, binding=_binding(),
            requested_provider="fixture", operation=lambda _provider: None,
        )
    assert raced
    assert (context.evidence_root / "provider-cleanup-observation.json").read_bytes() == b"racer-owned"


def test_feedback2_root_swap_cannot_substitute_the_reopened_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import protocol.custody as custody_module
    from lme.providers.lifecycle import CleanupUnproved, run_provider_lifecycle

    real_link = custody_module.os.link
    displaced = tmp_path / "held-evidence-root"
    outside = tmp_path / "substituted-evidence-root"
    context = _feedback2_context(tmp_path, "root-swap")

    def swap_after_publish(src, dst, *, src_dir_fd=None, dst_dir_fd=None, follow_symlinks=True):
        result = real_link(
            src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )
        context.evidence_root.rename(displaced)
        outside.mkdir()
        context.evidence_root.symlink_to(outside, target_is_directory=True)
        source = displaced / str(dst)
        (outside / str(dst)).write_bytes(source.read_bytes())
        return result

    monkeypatch.setattr(custody_module.os, "link", swap_after_publish)
    with pytest.raises(CleanupUnproved, match="publication"):
        run_provider_lifecycle(
            provider=_Provider(), profile=None,
            context=context, binding=_binding(),
            requested_provider="fixture", operation=lambda _provider: None,
        )
    assert (displaced / "provider-cleanup-observation.json").is_file()


@pytest.mark.parametrize("path", ("work/", "a/b/"))
def test_feedback2_trailing_separator_is_rejected_by_models_and_draft_schemas(
    tmp_path: Path, path: str
) -> None:
    from jsonschema import Draft202012Validator
    from pydantic import ValidationError
    from protocol.models import CleanupRecordV2, ProviderCleanupObservation, ProviderCleanupPathLstat, export_json_schemas

    with pytest.raises(ValidationError):
        CleanupRecordV2(
            protocol_version="1.0.0", schema_version=2, run_id="run-1", session_id="session-1",
            namespace="namespace-1", observation_path=path, observation_sha256="a" * 64,
        )
    with pytest.raises(ValidationError):
        ProviderCleanupPathLstat(kind="path-lstat", path=path, raw_kind="missing", entries=[])

    schemas = {item.name: json.loads(item.read_text(encoding="utf-8")) for item in export_json_schemas(tmp_path)}
    trace_payload = {
        "protocol_version": "1.0.0", "schema_version": 2, "case_id": "case-1",
        "entries": [{
            "protocol_version": "1.0.0", "schema_version": 2, "record": "cleanup",
            "run_id": "run-1", "session_id": "session-1", "namespace": "namespace-1",
            "observation_path": path, "observation_sha256": "a" * 64,
        }],
    }
    observation_payload = ProviderCleanupObservation(
        run_id="run-1", session_id="session-1", requested_provider="fixture",
        provider_variant="observed", namespace="namespace-1", cleanup_called=True,
        required_surface_ids=["provider-state"],
        observations=[{"kind": "provider-state", "remaining_record_ids": [], "backend_active": False}],
    ).model_dump(mode="json")
    observation_payload["observations"] = [
        {"kind": "path-lstat", "path": path, "raw_kind": "missing", "entries": []}
    ]
    assert list(Draft202012Validator(schemas["case-trace.v2.schema.json"]).iter_errors(trace_payload))
    assert list(
        Draft202012Validator(schemas["provider-cleanup-observation.v1.schema.json"]).iter_errors(
            observation_payload
        )
    )


def test_feedback3_cleanup_verification_holds_the_owned_dirfd_chain_during_intermediate_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-named artifact substituted by path must not become verification input."""
    import lme.providers.lifecycle as lifecycle
    from lme.providers.lifecycle import verify_cleanup_observation
    from protocol.models import ProviderCleanupObservation

    evidence_root = tmp_path / "evidence"
    original_dir = evidence_root / "owned" / "session"
    original_dir.mkdir(parents=True)
    path = original_dir / "provider-cleanup-observation.json"

    def payload(session_id: str) -> bytes:
        observation = ProviderCleanupObservation(
            run_id="run-1", session_id=session_id, requested_provider="fixture",
            provider_variant="observed", namespace="namespace-1", cleanup_called=True,
            required_surface_ids=["provider-state"],
            observations=[{"kind": "provider-state", "remaining_record_ids": [], "backend_active": False}],
        )
        return observation.model_dump_json().encode()

    original = payload("original")
    replacement = payload("replacement")
    path.write_bytes(original)
    expected_digest = hashlib.sha256(replacement).hexdigest()
    real_open = lifecycle.os.open
    swapped = False

    def swap_before_leaf_open(name, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if name == "provider-cleanup-observation.json" and dir_fd is not None and not swapped:
            swapped = True
            displaced = evidence_root / "displaced"
            (evidence_root / "owned").rename(displaced)
            replacement_path = evidence_root / "owned" / "session" / "provider-cleanup-observation.json"
            replacement_path.parent.mkdir(parents=True)
            replacement_path.write_bytes(replacement)
        return real_open(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(lifecycle.os, "open", swap_before_leaf_open)
    with pytest.raises(ValueError, match="digest"):
        verify_cleanup_observation(path, expected_digest, evidence_root=evidence_root)
    assert swapped


@pytest.mark.parametrize("terminalization_fails", (False, True))
def test_feedback3_runner_preserves_control_flow_after_local_write_and_terminalization_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, terminalization_fails: bool
) -> None:
    from lme.providers import registry
    import lme.runner as runner
    from lme.reader import StubReader

    class ProviderControl(BaseException):
        pass

    original = ProviderControl("provider control flow")

    class InterruptingProvider(_Provider):
        def setup(self, profile, context) -> None:
            super().setup(profile, context)
            raise original

    monkeypatch.setattr(registry, "provider_spec", lambda _name: _runner_spec(InterruptingProvider))
    real_write_jsonl = runner._write_jsonl

    def fail_late_writer(path: Path, rows: list[dict[str, object]]) -> None:
        if path.name == "gold-evidence-ceiling.jsonl":
            raise RuntimeError("late local artifact write")
        real_write_jsonl(path, rows)

    monkeypatch.setattr(runner, "_write_jsonl", fail_late_writer)
    terminalizations: list[str] = []
    real_finalize = runner.finalize_manifest

    def finalize_or_fail(*args, **kwargs):
        terminalizations.append(str(kwargs["status"]))
        if terminalization_fails:
            raise RuntimeError("terminalization write")
        return real_finalize(*args, **kwargs)

    monkeypatch.setattr(runner, "finalize_manifest", finalize_or_fail)
    run_id = f"feedback3-control-{terminalization_fails}"
    with pytest.raises(ProviderControl) as raised:
        runner.execute_run(_mini_config(tmp_path, 1, run_id, provider="fixture"), reader=StubReader())
    assert raised.value is original
    assert terminalizations == ["INVALID"]
    if not terminalization_fails:
        manifest = json.loads((tmp_path / run_id / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["status"] == "INVALID"
        environment = json.loads((tmp_path / run_id / "environment.json").read_text(encoding="utf-8"))
        assert environment["lme"]["lifecycle_expected_instances"] == []
        assert environment["lme"]["lifecycle_attempts"] == [
            {
                "internal_session_id": _attempt_session(tmp_path / run_id, None),
                "logical_question_id": None,
                "factory_returned": True,
                "setup_completed": False,
                "provider_variant": None,
                "failure_code": "provider_control_flow",
            }
        ]
        assert not list((tmp_path / run_id / "traces").glob("*.jsonl"))
        assert not list((tmp_path / run_id / "evidence").rglob("provider-cleanup-observation.json"))


@pytest.mark.parametrize("secondary", (KeyboardInterrupt(), SystemExit(7)))
def test_feedback4_runner_preserves_hostile_primary_when_persistence_raises_base_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, secondary: BaseException
) -> None:
    from lme.providers import registry
    import lme.runner as runner
    from lme.reader import StubReader

    class PrimaryControl(BaseException):
        def add_note(self, _note: str) -> None:
            raise RuntimeError("hostile note attachment")

    primary = PrimaryControl("primary")

    class InterruptingProvider(_Provider):
        def setup(self, profile, context) -> None:
            super().setup(profile, context)
            raise primary

    monkeypatch.setattr(registry, "provider_spec", lambda _name: _runner_spec(InterruptingProvider))
    real_write_jsonl = runner._write_jsonl

    def interrupt_persistence(path: Path, rows: list[dict[str, object]]) -> None:
        if path.name == "gold-evidence-ceiling.jsonl":
            raise secondary
        real_write_jsonl(path, rows)

    monkeypatch.setattr(runner, "_write_jsonl", interrupt_persistence)
    run_id = f"feedback4-persistence-{type(secondary).__name__}"
    caught: BaseException | None = None
    try:
        runner.execute_run(_mini_config(tmp_path, 1, run_id, provider="fixture"), reader=StubReader())
    except BaseException as exc:
        caught = exc
    assert caught is primary
    assert json.loads((tmp_path / run_id / "manifest.json").read_text(encoding="utf-8"))["status"] == "INVALID"


def test_feedback4_runner_preserves_primary_when_terminalization_raises_custom_base_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lme.providers import registry
    import lme.runner as runner
    from lme.reader import StubReader

    class PrimaryControl(BaseException):
        pass

    class TerminalControl(BaseException):
        pass

    primary = PrimaryControl("primary")
    terminal = TerminalControl("terminal")

    class InterruptingProvider(_Provider):
        def setup(self, profile, context) -> None:
            super().setup(profile, context)
            raise primary

    monkeypatch.setattr(registry, "provider_spec", lambda _name: _runner_spec(InterruptingProvider))
    real_finalize = runner.finalize_manifest

    def finalize_then_interrupt(*args, **kwargs):
        real_finalize(*args, **kwargs)
        raise terminal

    monkeypatch.setattr(runner, "finalize_manifest", finalize_then_interrupt)
    run_id = "feedback4-terminal"
    with pytest.raises(PrimaryControl) as raised:
        runner.execute_run(_mini_config(tmp_path, 1, run_id, provider="fixture"), reader=StubReader())
    assert raised.value is primary
    assert json.loads((tmp_path / run_id / "manifest.json").read_text(encoding="utf-8"))["status"] == "INVALID"


def test_feedback4_persistence_never_reopens_a_swapped_intermediate_evidence_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import protocol.custody as custody_module
    from lme.providers.lifecycle import CleanupUnproved, run_provider_lifecycle

    intermediate = tmp_path / "intermediate"
    intermediate.mkdir()
    context = _context(intermediate)
    displaced = tmp_path / "displaced"
    real_open = custody_module.os.open
    swapped = False

    def swap_after_held_root(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if path == context.evidence_root.name and dir_fd is not None and not swapped:
            swapped = True
            intermediate.rename(displaced)
            (intermediate / "evidence").mkdir(parents=True)
        return descriptor

    monkeypatch.setattr(custody_module.os, "open", swap_after_held_root)
    with pytest.raises(CleanupUnproved, match="root|binding|changed"):
        run_provider_lifecycle(
            provider=_Provider(), profile=None, context=context, binding=_binding(),
            requested_provider="fixture", operation=lambda _provider: None,
        )
    assert swapped
    assert not (intermediate / "evidence" / "provider-cleanup-observation.json").exists()


def test_feedback5_hostile_primary_note_cannot_replace_setup_control_flow(
    tmp_path: Path,
) -> None:
    from lme.providers.lifecycle import run_provider_lifecycle

    class PrimaryControl(BaseException):
        def add_note(self, _note: str) -> None:
            raise RuntimeError("hostile note attachment")

    primary = PrimaryControl("setup")

    class CleanupFailure(_Provider):
        def cleanup(self) -> None:
            super().cleanup()
            raise RuntimeError("cleanup secondary")

    provider = CleanupFailure(failure=primary)
    caught: BaseException | None = None
    try:
        run_provider_lifecycle(
            provider=provider, profile=None, context=_context(tmp_path), binding=_binding(),
            requested_provider="fixture", operation=lambda _provider: None,
        )
    except BaseException as exc:
        caught = exc
    assert caught is primary
    assert provider.cleanups == 1


@pytest.mark.parametrize("primary", (KeyboardInterrupt(), SystemExit(7), BaseException("custom")))
def test_feedback5_constructor_cleanup_failure_never_replaces_base_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, primary: BaseException
) -> None:
    import shutil
    from lme.providers.lifecycle import terminalize_constructor_failure

    context = _context(tmp_path)

    def fail_root_cleanup(_path: Path) -> None:
        raise RuntimeError("constructor cleanup secondary")

    monkeypatch.setattr(shutil, "rmtree", fail_root_cleanup)
    caught: BaseException | None = None
    try:
        terminalize_constructor_failure(context, requested_provider="fixture", error=primary)
    except BaseException as exc:
        caught = exc
    assert caught is primary


def _feedback6_custodied_context(tmp_path: Path, label: str):
    from lme.providers.base import ProviderSessionContext
    from lme.providers.lifecycle import LifecycleCustody
    from protocol.custody import hold_directory

    run = hold_directory(tmp_path / label, create=True, logical_ref=Path("."))
    session = run.mkdir("session", logical_ref=Path("sessions/session"))
    work = session.mkdir("work", logical_ref=Path("work/session"))
    evidence = session.mkdir("evidence", logical_ref=Path("evidence/session"))
    custody = LifecycleCustody(session=session, work=work, evidence=evidence)
    context = ProviderSessionContext(
        run_id=f"feedback6-{label}",
        session_id="session",
        namespace="namespace",
        work_root=work.capability_path,
        evidence_root=evidence.capability_path,
        work_ref=work.logical_ref,
        evidence_ref=evidence.logical_ref,
    )
    return run, custody, context


def test_feedback6_cleanup_orders_publish_reobserve_bind_register_then_retire(
    tmp_path: Path,
) -> None:
    from lme.providers.base import ProviderRuntimeBinding
    from lme.providers.lifecycle import run_provider_lifecycle

    run, custody, context = _feedback6_custodied_context(tmp_path, "ordering")
    events: list[str] = []

    class Ordered(_Provider):
        def cleanup(self) -> None:
            super().cleanup()
            events.append("cleanup")

    def observe(_context, _provider):
        final_exists = (custody.evidence.capability_path / "provider-cleanup-observation.json").exists()
        events.append("observe:second" if final_exists else "observe:first")
        return (
            {"kind": "provider-state", "remaining_record_ids": [], "backend_active": False},
            {"kind": "path-lstat", "path": "work", "raw_kind": "directory", "entries": []},
        )

    def finalized(evidence) -> None:
        events.append("finalized")
        payload = evidence.observation_path.read_bytes()
        assert hashlib.sha256(payload).hexdigest() == evidence.observation_sha256
        custody.assert_bound()
        assert custody.work.capability_path.is_dir()

    try:
        run_provider_lifecycle(
            provider=Ordered(), profile=None, context=context,
            binding=ProviderRuntimeBinding(("provider-state", "work-root"), observe),
            requested_provider="fixture", operation=lambda _provider: None,
            finalized=finalized, custody=custody,
        )
        assert events == ["cleanup", "observe:first", "observe:second", "finalized"]
        assert custody.work.retire(max_entries=32, max_depth=8) is True
    finally:
        custody.close()
        run.close()


def test_feedback6_post_publish_reappearance_or_snapshot_drift_invalidates(
    tmp_path: Path,
) -> None:
    # The custodied context below addresses its evidence through a proc-fd
    # capability path. Off procfs there is no such path to observe, so the live
    # post-publication observation asserted here cannot be staged at all.
    require_procfs_descriptor_paths()

    from lme.providers.base import ProviderRuntimeBinding
    from lme.providers.lifecycle import CleanupUnproved, run_provider_lifecycle

    missed: list[str] = []
    for surface in ("provider-state", "namespace-membership", "path-lstat", "absent-but-different"):
        run, custody, context = _feedback6_custodied_context(tmp_path, f"reappear-{surface}")
        calls = 0

        def observe(_context, _provider):
            nonlocal calls
            calls += 1
            final_exists = (custody.evidence.capability_path / "provider-cleanup-observation.json").exists()
            if calls == 2:
                assert final_exists, "the second live observation must follow publication"
            return (
                {
                    "kind": "provider-state",
                    "remaining_record_ids": ["returned"] if calls == 2 and surface == "provider-state" else [],
                    "backend_active": calls == 2 and surface == "provider-state",
                },
                {
                    "kind": "namespace-membership",
                    "expected_namespace": "namespace",
                    "live_namespaces": ["namespace"] if calls == 2 and surface == "namespace-membership" else [],
                },
                {
                    "kind": "path-lstat", "path": "work", "raw_kind": "directory",
                    "entries": ["returned"] if calls == 2 and surface == "path-lstat" else [],
                },
            )

        if surface == "absent-but-different":
            def observe(_context, _provider):
                nonlocal calls
                calls += 1
                final_exists = (custody.evidence.capability_path / "provider-cleanup-observation.json").exists()
                if calls == 2:
                    assert final_exists
                return (
                    {"kind": "provider-state", "remaining_record_ids": [], "backend_active": False},
                    {"kind": "namespace-membership", "expected_namespace": "namespace", "live_namespaces": []},
                    {
                        "kind": "path-lstat", "path": "work",
                        "raw_kind": "missing" if calls == 1 else "directory", "entries": [],
                    },
                )

        try:
            try:
                run_provider_lifecycle(
                    provider=_Provider(), profile=None, context=context,
                    binding=ProviderRuntimeBinding(("provider-state", "namespace-membership", "work-root"), observe),
                    requested_provider="fixture", operation=lambda _provider: None, custody=custody,
                )
            except CleanupUnproved:
                pass
            else:
                missed.append(surface)
            raw = json.loads(
                (custody.evidence.capability_path / "provider-cleanup-observation.json").read_text(encoding="utf-8")
            )
            assert all(
                not item.get("remaining_record_ids") and not item.get("live_namespaces") and not item.get("entries")
                for item in raw["observations"]
            )
        finally:
            custody.close()
            run.close()
    assert missed == []


@pytest.mark.parametrize("substitution", ("bytes", "schema", "oversized"))
def test_feedback6_post_publish_substitution_is_refused_before_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, substitution: str,
) -> None:
    import protocol.custody as custody_module
    from lme.providers.base import ProviderRuntimeBinding
    from lme.providers.lifecycle import CleanupUnproved, run_provider_lifecycle

    run, custody, context = _feedback6_custodied_context(tmp_path, f"substitute-{substitution}")
    real_link = custody_module.os.link
    callback_called = False

    def substitute_after_link(src, dst, *, src_dir_fd=None, dst_dir_fd=None, follow_symlinks=True):
        real_link(
            src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )
        os.unlink(dst, dir_fd=dst_dir_fd)
        descriptor = os.open(
            dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dst_dir_fd,
        )
        try:
            if substitution == "bytes":
                os.write(descriptor, b"substituted")
            elif substitution == "schema":
                os.write(descriptor, b'{}\n')
            else:
                os.ftruncate(descriptor, 1_048_577)
        finally:
            os.close(descriptor)

    def finalized(_evidence) -> None:
        nonlocal callback_called
        callback_called = True

    monkeypatch.setattr(custody_module.os, "link", substitute_after_link)
    try:
        with pytest.raises(CleanupUnproved):
            run_provider_lifecycle(
                provider=_Provider(), profile=None, context=context,
                binding=ProviderRuntimeBinding(
                    ("provider-state", "work-root"),
                    lambda *_args: (
                        {"kind": "provider-state", "remaining_record_ids": [], "backend_active": False},
                        {"kind": "path-lstat", "path": "work", "raw_kind": "directory", "entries": []},
                    ),
                ),
                requested_provider="fixture", operation=lambda _provider: None,
                finalized=finalized, custody=custody,
            )
        assert callback_called is False
    finally:
        custody.close()
        run.close()


def test_feedback6_provider_residue_is_raw_evidence_before_runner_retirement(
    tmp_path: Path,
) -> None:
    from lme.providers.base import ProviderRuntimeBinding
    from lme.providers.lifecycle import CleanupUnproved, run_provider_lifecycle

    run, custody, context = _feedback6_custodied_context(tmp_path, "residue")

    class Residue(_Provider):
        def cleanup(self) -> None:
            super().cleanup()
            (context.work_root / "provider-residue").write_bytes(b"retained")

    def observe(_context, _provider):
        return (
            {"kind": "provider-state", "remaining_record_ids": ["provider-residue"], "backend_active": True},
            {"kind": "path-lstat", "path": "work", "raw_kind": "directory", "entries": ["provider-residue"]},
        )

    provider = Residue()
    try:
        with pytest.raises(CleanupUnproved):
            run_provider_lifecycle(
                provider=provider, profile=None, context=context,
                binding=ProviderRuntimeBinding(("provider-state", "work-root"), observe),
                requested_provider="fixture", operation=lambda _provider: None, custody=custody,
            )
        observation = json.loads(
            (custody.evidence.capability_path / "provider-cleanup-observation.json").read_text(encoding="utf-8")
        )
        assert any(item.get("entries") == ["provider-residue"] for item in observation["observations"])
        assert (custody.work.capability_path / "provider-residue").is_file()
        custody.work.empty_recursive(max_entries=32, max_depth=8)
        assert list(custody.work.capability_path.iterdir()) == []
        assert provider.cleanups == 1
    finally:
        custody.close()
        run.close()


def test_feedback6_moved_work_root_keeps_provider_on_held_inode_and_replacement_safe(
    tmp_path: Path,
) -> None:
    from lme.providers.base import ProviderRuntimeBinding
    from lme.providers.lifecycle import CleanupUnproved, run_provider_lifecycle

    run, custody, context = _feedback6_custodied_context(tmp_path, "moved-work")
    logical_work = tmp_path / "moved-work" / "session" / "work"
    displaced = tmp_path / "displaced-work"
    replacement_sentinel = logical_work / "replacement-sentinel"

    class Moving(_Provider):
        def setup(self, profile, supplied) -> None:
            super().setup(profile, supplied)
            (supplied.work_root / "setup").write_bytes(b"held")

        def cleanup(self) -> None:
            super().cleanup()
            (context.work_root / "setup").unlink()
            (context.work_root / "operation").unlink()

    def operation(_provider):
        logical_work.rename(displaced)
        logical_work.mkdir()
        replacement_sentinel.write_bytes(b"replacement")
        (context.work_root / "operation").write_bytes(b"held")

    try:
        with pytest.raises(CleanupUnproved):
            run_provider_lifecycle(
                provider=Moving(), profile=None, context=context,
                binding=ProviderRuntimeBinding(
                    ("provider-state", "work-root"),
                    lambda *_args: (
                        {"kind": "provider-state", "remaining_record_ids": [], "backend_active": False},
                        {"kind": "path-lstat", "path": "work", "raw_kind": "directory", "entries": []},
                    ),
                ),
                requested_provider="fixture", operation=operation, custody=custody,
            )
        assert replacement_sentinel.read_bytes() == b"replacement"
        assert sorted(item.name for item in displaced.iterdir()) == []
        assert custody.work.retire(max_entries=32, max_depth=8) is False
        assert replacement_sentinel.read_bytes() == b"replacement"
    finally:
        custody.close()
        run.close()


class _Feedback6HostileMeta(type):
    def __getattribute__(cls, name: str):
        if name in {"__name__", "__qualname__"}:
            _FEEDBACK6_HOOKS.append(f"class:{name}")
            raise RuntimeError("dynamic-name-hook-ran")
        return super().__getattribute__(name)


_FEEDBACK6_HOOKS: list[str] = []


class _Feedback6HostileError(RuntimeError, metaclass=_Feedback6HostileMeta):
    def __str__(self) -> str:
        _FEEDBACK6_HOOKS.append("str")
        raise RuntimeError("str-hook-ran")

    def __repr__(self) -> str:
        _FEEDBACK6_HOOKS.append("repr")
        raise RuntimeError("repr-hook-ran")

    def add_note(self, _note: str) -> None:
        _FEEDBACK6_HOOKS.append("add_note")
        raise RuntimeError("add-note-hook-ran")


class _Feedback6HostileControl(BaseException, metaclass=_Feedback6HostileMeta):
    __str__ = _Feedback6HostileError.__str__
    __repr__ = _Feedback6HostileError.__repr__
    add_note = _Feedback6HostileError.add_note


def test_feedback6_hostile_lifecycle_boundaries_are_closed_and_never_rendered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lme.providers.base import ProviderRuntimeBinding
    from lme.providers.lifecycle import LifecycleRunError, run_provider_lifecycle
    from protocol.custody import HeldDirectory

    expected_facts = {
        "setup": "provider_setup_failed", "operation": "provider_operation_failed",
        "cleanup": "provider_cleanup_failed", "observation": "cleanup_observation_failed",
        "publish": "cleanup_evidence_publish_failed", "callback": "lifecycle_callback_failed",
    }
    failures: list[str] = []
    for boundary, expected_fact in expected_facts.items():
        _FEEDBACK6_HOOKS.clear()
        hostile = _Feedback6HostileError()
        run, custody, context = _feedback6_custodied_context(tmp_path, f"hostile-{boundary}")

        class HostileProvider(_Provider):
            def setup(self, profile, supplied) -> None:
                super().setup(profile, supplied)
                if boundary == "setup":
                    raise hostile

            def cleanup(self) -> None:
                super().cleanup()
                if boundary == "cleanup":
                    raise hostile

        def operation(_provider):
            if boundary == "operation":
                raise hostile

        def observe(_context, _provider):
            if boundary == "observation":
                raise hostile
            return (
                {"kind": "provider-state", "remaining_record_ids": [], "backend_active": False},
                {"kind": "path-lstat", "path": "work", "raw_kind": "directory", "entries": []},
            )

        real_publish = HeldDirectory.publish_exclusive

        def publish(held, *args, **kwargs):
            if held is custody.evidence and boundary == "publish":
                raise hostile
            return real_publish(held, *args, **kwargs)

        monkeypatch.setattr(HeldDirectory, "publish_exclusive", publish)
        try:
            caught: BaseException | None = None
            try:
                run_provider_lifecycle(
                    provider=HostileProvider(), profile=None, context=context,
                    binding=ProviderRuntimeBinding(("provider-state", "work-root"), observe),
                    requested_provider="fixture", operation=operation,
                    finalized=(lambda _evidence: (_ for _ in ()).throw(hostile)) if boundary == "callback" else None,
                    custody=custody,
                )
            except BaseException as exc:
                caught = exc
            if not isinstance(caught, LifecycleRunError):
                failures.append(f"{boundary}:wrapper")
            else:
                if caught.primary is not hostile:
                    failures.append(f"{boundary}:identity")
                if str(caught) != "direct provider lifecycle failed":
                    failures.append(f"{boundary}:message")
                if caught.fact != expected_fact:
                    failures.append(f"{boundary}:fact")
                if not isinstance(caught.secondary_facts, tuple):
                    failures.append(f"{boundary}:secondary-facts")
            if _FEEDBACK6_HOOKS:
                failures.append(f"{boundary}:hooks={','.join(_FEEDBACK6_HOOKS)}")
        finally:
            monkeypatch.setattr(HeldDirectory, "publish_exclusive", real_publish)
            custody.close()
            run.close()
    assert failures == []


def test_feedback6_lifecycle_primary_precedence_matrix(tmp_path: Path) -> None:
    from lme.providers.base import ProviderRuntimeBinding
    from lme.providers.lifecycle import LifecycleRunError, run_provider_lifecycle

    rows = (
        ("ordinary", None, None, "operation", False),
        ("ordinary", "ordinary", None, "operation", False),
        ("ordinary", "control", None, "cleanup", True),
        ("control", "ordinary", None, "operation", True),
        ("control", "control", None, "operation", True),
        (None, "ordinary", None, "cleanup", False),
        (None, "control", None, "cleanup", True),
        ("ordinary", None, "control", "observation", True),
        (None, "ordinary", "control", "observation", True),
        (None, "control", "control", "cleanup", True),
        ("control", "control", "control", "operation", True),
    )
    mismatches: list[str] = []
    for index, (operation_kind, cleanup_kind, observation_kind, selected, is_control) in enumerate(rows):
        run, custody, context = _feedback6_custodied_context(tmp_path, f"precedence-{index}")
        errors = {
            "operation": _Feedback6HostileControl() if operation_kind == "control" else _Feedback6HostileError(),
            "cleanup": _Feedback6HostileControl() if cleanup_kind == "control" else _Feedback6HostileError(),
            "observation": _Feedback6HostileControl() if observation_kind == "control" else _Feedback6HostileError(),
        }

        class MatrixProvider(_Provider):
            def cleanup(self) -> None:
                super().cleanup()
                if cleanup_kind is not None:
                    raise errors["cleanup"]

        def operation(_provider):
            if operation_kind is not None:
                raise errors["operation"]

        def observe(_context, _provider):
            if observation_kind is not None:
                raise errors["observation"]
            return (
                {"kind": "provider-state", "remaining_record_ids": [], "backend_active": False},
                {"kind": "path-lstat", "path": "work", "raw_kind": "directory", "entries": []},
            )

        try:
            caught: BaseException | None = None
            try:
                run_provider_lifecycle(
                    provider=MatrixProvider(), profile=None, context=context,
                    binding=ProviderRuntimeBinding(("provider-state", "work-root"), observe),
                    requested_provider="fixture", operation=operation, custody=custody,
                )
            except BaseException as exc:
                caught = exc
            expected = errors[selected]
            if is_control:
                if caught is not expected:
                    mismatches.append(f"row-{index}:control")
            elif not isinstance(caught, LifecycleRunError) or caught.primary is not expected:
                mismatches.append(f"row-{index}:ordinary")
        finally:
            custody.close()
            run.close()
    assert mismatches == []


def test_feedback6_execute_run_retires_work_only_after_lifecycle_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lme.providers import registry
    from lme.providers.base import ProviderRuntimeBinding
    from lme.reader import StubReader
    from lme.runner import execute_run

    events: dict[str, list[str]] = {}
    contexts = []

    class Emptying(_Feedback2RememberingProvider):
        def setup(self, profile, context):
            super().setup(profile, context)
            contexts.append(context)

        def retrieve(self, question, top_k, purpose):
            from lme.providers.base import ProviderHit, RetrievalPurpose
            from protocol.probes import known_answer_probe_specs

            if purpose is RetrievalPurpose.ABSENCE_PROBE_EXPECTED_EMPTY:
                return []
            if purpose is RetrievalPurpose.POSITIVE_PROBE:
                semantic = next(item for item in known_answer_probe_specs() if item.kind == "semantic-zero-overlap")
                if question == semantic.query:
                    text = next(record for record in self.records if semantic.fact in record)
                    return [ProviderHit("probe", text, 1.0)]
                return [ProviderHit("probe", question, 1.0)]
            return super().retrieve(question, top_k, purpose)

    def observe(context, provider):
        session_events = events.setdefault(context.session_id, [])
        final = context.evidence_root / "provider-cleanup-observation.json"
        session_events.append("observe:post-publish" if final.exists() else "observe:first")
        assert context.work_root.is_dir(), "runner retirement must follow both observations"
        return (
            {"kind": "provider-state", "remaining_record_ids": [], "backend_active": False},
            {"kind": "path-lstat", "path": "work", "raw_kind": "directory", "entries": []},
        )

    spec = _runner_spec(Emptying)
    object.__setattr__(spec, "runtime_binding", ProviderRuntimeBinding(("provider-state", "work-root"), observe))
    monkeypatch.setattr(registry, "provider_spec", lambda _name: spec)
    result = execute_run(
        _mini_config(tmp_path, 2, "feedback6-runner-retirement", provider="fixture"),
        reader=StubReader(),
    )
    assert contexts
    assert all(value == ["observe:first", "observe:post-publish"] for value in events.values())
    assert all(not (result.run_dir / context.work_ref).exists() for context in contexts)


def test_feedback6_review_cleanup_observation_surfaces_match_the_declaration(tmp_path: Path) -> None:
    from lme.providers.base import ProviderRuntimeBinding
    from lme.providers.lifecycle import CleanupUnproved, run_provider_lifecycle

    accepted: list[str] = []
    cases = {
        "empty": (),
        "missing": ({"kind": "provider-state", "remaining_record_ids": [], "backend_active": False},),
        "wrong": (
            {"kind": "provider-state", "remaining_record_ids": [], "backend_active": False},
            {"kind": "path-lstat", "path": "session-root", "raw_kind": "directory", "entries": []},
        ),
        "extra": (
            {"kind": "provider-state", "remaining_record_ids": [], "backend_active": False},
            {"kind": "path-lstat", "path": "work", "raw_kind": "directory", "entries": []},
            {"kind": "namespace-membership", "expected_namespace": "namespace", "live_namespaces": []},
        ),
    }
    for label, rows in cases.items():
        run, custody, context = _feedback6_custodied_context(tmp_path, f"review-surfaces-{label}")
        try:
            try:
                run_provider_lifecycle(
                    provider=_Provider(), profile=None, context=context,
                    binding=ProviderRuntimeBinding(("provider-state", "work-root"), lambda *_args: rows),
                    requested_provider="fixture", operation=lambda _provider: None, custody=custody,
                )
            except CleanupUnproved:
                pass
            else:
                accepted.append(label)
        finally:
            custody.close()
            run.close()
    assert accepted == []


def test_feedback6_review_direct_adapter_failures_are_closed_and_unrendered(tmp_path: Path) -> None:
    from membench.adapters.base import AdapterEnvironmentError
    from lme.dataset import load_dataset
    from lme.normalize import neutralize
    from lme.providers.exomem_direct import ExomemDirectProvider
    from protocol.models import CaseHandle, DatasetIdentity

    hooks: list[str] = []

    class Hostile(RuntimeError):
        def __str__(self):
            hooks.append("str")
            raise RuntimeError("rendered")

        def __repr__(self):
            hooks.append("repr")
            raise RuntimeError("rendered")

    hostile = Hostile()
    context = _context(tmp_path)

    class BrokenSetup:
        def setup(self, *_args):
            raise hostile

    provider = ExomemDirectProvider()
    provider._adapter = BrokenSetup()  # type: ignore[assignment]
    with pytest.raises(AdapterEnvironmentError, match="^direct provider setup failed$"):
        provider.setup(None, context)

    class BrokenIngest:
        def ingest_case(self, *_args):
            raise hostile

    provider._adapter = BrokenIngest()  # type: ignore[assignment]
    question = load_dataset(Path("benchmarks/lme/fixtures/mini.json")).questions[0]
    identity = DatasetIdentity(
        id="fixture", variant="mini", source="local", revision="review",
        sha256=hashlib.sha256(Path("benchmarks/lme/fixtures/mini.json").read_bytes()).hexdigest(), case_count=6,
    )
    events = neutralize(question, identity)
    handle = CaseHandle(case_id=question.question_id, case_ordinal=1, question_date=question.question_date_text)
    with pytest.raises(RuntimeError, match="^direct provider ingestion failed$"):
        provider.ingest_case(events, handle)
    assert hooks == []


def test_feedback7_real_adapter_setup_and_ingest_catches_are_closed_and_unrendered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from membench.adapters.base import AdapterEnvironmentError, Profile
    from membench.adapters.exomem_local import ExomemLocalAdapter
    from exomem import commands
    from lme.adapter import LmeExomemAdapter
    from lme.dataset import load_dataset
    from lme.normalize import neutralize
    from protocol.models import CaseHandle, DatasetIdentity

    hooks: list[str] = []

    class Hostile(RuntimeError):
        def __str__(self) -> str:
            hooks.append("str")
            return "rendered-secret"

        def __repr__(self) -> str:
            hooks.append("repr")
            return "rendered-secret"

        @property
        def args(self):
            hooks.append("args")
            return ()

    hostile = Hostile()
    adapter = LmeExomemAdapter()
    monkeypatch.setattr(adapter, "cleanup", lambda: None)
    monkeypatch.setattr(
        ExomemLocalAdapter, "setup", lambda *_args: (_ for _ in ()).throw(hostile),
    )
    with pytest.raises(AdapterEnvironmentError, match="^direct provider setup failed$"):
        adapter.setup(tmp_path / "work", Profile(name="fixture", settings={"EXOMEM_DISABLE_EMBEDDINGS": "1"}))

    adapter._vault = object()
    adapter._schema = object()
    question = load_dataset(Path("benchmarks/lme/fixtures/mini.json")).questions[0]
    identity = DatasetIdentity(
        id="fixture", variant="mini", source="local", revision="review",
        sha256=hashlib.sha256(Path("benchmarks/lme/fixtures/mini.json").read_bytes()).hexdigest(), case_count=6,
    )
    events = neutralize(question, identity)
    handle = CaseHandle(case_id=question.question_id, case_ordinal=1, question_date=question.question_date_text)
    monkeypatch.setattr(commands, "op_capture_source", lambda *_args, **_kwargs: (_ for _ in ()).throw(hostile))
    result = adapter.ingest_case(events, handle)

    assert result and all(not item.ok for item in result)
    assert all(item.detail == "direct provider ingestion failed" for item in result)
    assert hooks == []


def test_feedback7_completeness_wrapping_never_renders_hostile_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import lme.providers.lifecycle as lifecycle
    from lme.providers.lifecycle import LifecycleCompletenessError, validate_lifecycle_completeness

    hooks: list[str] = []

    class Hostile(RuntimeError):
        def __str__(self) -> str:
            hooks.append("str")
            return "rendered-secret"

    hostile = Hostile()
    monkeypatch.setattr(
        lifecycle, "verify_cleanup_observation", lambda *_args, **_kwargs: (_ for _ in ()).throw(hostile),
    )
    with pytest.raises(LifecycleCompletenessError, match="^lifecycle cleanup observation is invalid$") as caught:
        validate_lifecycle_completeness(
            expected_instances=(),
            cleanup_records=[{
                "run_id": "run", "requested_provider": "fixture", "session_id": "session",
                "namespace": "namespace", "observation_path": "evidence/session/cleanup.json",
                "observation_sha256": "a" * 64,
            }],
            evidence_root=tmp_path,
        )
    assert caught.value.__cause__ is hostile
    assert hooks == []


def test_feedback6_review_constructor_retirement_control_flow_keeps_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lme.providers.lifecycle import terminalize_constructor_failure
    from protocol.custody import HeldDirectory

    class RetirementControl(BaseException):
        pass

    control = RetirementControl()
    run, custody, context = _feedback6_custodied_context(tmp_path, "review-retirement-control")
    real_retire = HeldDirectory.retire

    def interrupt_work(held, **kwargs):
        if held is custody.work:
            raise control
        return real_retire(held, **kwargs)

    monkeypatch.setattr(HeldDirectory, "retire", interrupt_work)
    try:
        caught: BaseException | None = None
        try:
            terminalize_constructor_failure(
                context, requested_provider="fixture", error=RuntimeError("constructor"), custody=custody,
            )
        except BaseException as exc:
            caught = exc
        assert caught is control
    finally:
        monkeypatch.setattr(HeldDirectory, "retire", real_retire)
        custody.close()
        run.close()


def test_feedback8_finalized_callback_binding_loss_refuses_authorization(
    tmp_path: Path,
) -> None:
    from lme.providers.base import ProviderRuntimeBinding
    from lme.providers.lifecycle import CleanupUnproved, run_provider_lifecycle

    run, custody, context = _feedback6_custodied_context(tmp_path, "callback-binding")
    replacement = custody.session.capability_path / custody.work.name

    def observe(_context, _provider):
        return ({"kind": "provider-state", "remaining_record_ids": [], "backend_active": False},)

    def finalized(_evidence) -> None:
        os.rename(
            custody.work.name,
            "displaced-work",
            src_dir_fd=custody.work.parent_fd,
            dst_dir_fd=custody.work.parent_fd,
        )
        os.mkdir(custody.work.name, dir_fd=custody.work.parent_fd)
        (replacement / "sentinel").write_bytes(b"replacement")

    try:
        with pytest.raises(CleanupUnproved, match="custody binding"):
            run_provider_lifecycle(
                provider=_Provider(), profile=None, context=context,
                binding=ProviderRuntimeBinding(("provider-state",), observe),
                requested_provider="fixture", operation=lambda _provider: None,
                finalized=finalized, custody=custody,
            )
        assert (replacement / "sentinel").read_bytes() == b"replacement"
    finally:
        custody.close()
        run.close()


def test_feedback12_finalized_registration_refuses_an_identical_cleanup_leaf_replacement(
    tmp_path: Path,
) -> None:
    from lme.providers.base import ProviderRuntimeBinding
    from lme.providers.lifecycle import CleanupUnproved, run_provider_lifecycle

    run, custody, context = _feedback6_custodied_context(tmp_path, "cleanup-leaf-replacement")
    registered = False

    def observe(_context, _provider):
        return ("provider-state",)

    def finalized(evidence) -> None:
        nonlocal registered
        registered = True
        replacement = evidence.observation_path.with_suffix(".replacement")
        replacement.write_bytes(evidence.observation_path.read_bytes())
        os.replace(replacement, evidence.observation_path)

    try:
        with pytest.raises(CleanupUnproved, match="cleanup evidence"):
            run_provider_lifecycle(
                provider=_Provider(), profile=None, context=context,
                binding=ProviderRuntimeBinding(
                    ("provider-state",),
                    lambda *_args: ({"kind": "provider-state", "remaining_record_ids": [], "backend_active": False},),
                ),
                requested_provider="fixture", operation=lambda _provider: None,
                finalized=finalized, custody=custody,
            )
        assert registered
    finally:
        custody.close()
        run.close()
