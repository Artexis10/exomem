"""W8: direct providers own observable, one-shot lifecycle cleanup."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest import mock

import pytest


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
            {"kind": "path-lstat", "path": "work", "raw_kind": "missing", "entries": []},
        ),
    )


def test_provider_spec_is_inert_and_session_context_is_immutable(tmp_path: Path) -> None:
    from lme.providers.base import ProviderSessionContext
    from lme.providers.registry import provider_spec

    spec = provider_spec("no-memory")
    assert spec.descriptor == "no-memory"
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
    from lme.runner import LmeRunInvalid, RunConfig, execute_run

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
    assert not (run_dir / "work").exists()


def test_provider_claims_cannot_create_cleanup_truth(tmp_path: Path) -> None:
    from lme.providers.lifecycle import CleanupUnproved, run_provider_lifecycle

    provider = _Provider()
    provider.cleanup = lambda: True  # type: ignore[method-assign]
    bad_binding = _binding()
    object.__setattr__(bad_binding, "observe", lambda _context, _provider: ({"kind": "provider-state", "remaining_record_ids": ["still-here"], "backend_active": True},))
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
    with pytest.raises(LifecycleRunError, match=stage) as failure:
        run_provider_lifecycle(
            provider=provider, profile=None, context=_context(tmp_path), binding=_binding(),
            requested_provider="fixture", operation=operation,
        )
    assert provider.cleanups == 1
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
    with pytest.raises(LifecycleRunError, match="primary") as failure:
        run_provider_lifecycle(
            provider=provider, profile=None, context=_context(tmp_path), binding=_binding(),
            requested_provider="fixture", operation=lambda _provider: (_ for _ in ()).throw(RuntimeError("primary")),
        )
    assert provider.cleanups == 1
    assert "cleanup failure" in failure.value.secondary_failures
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
            {"kind": "path-lstat", "path": "work", "raw_kind": "missing", "entries": []},
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
    from lme.runner import RunConfig, execute_run
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

    monkeypatch.setattr(registry, "provider_spec", lambda _name: _runner_spec(lambda _spec: factory()))
    monkeypatch.setattr(runner, "neutralize", lambda *_args: (_ for _ in ()).throw(RuntimeError("neutralization failed")))
    with pytest.raises(LmeRunInvalid, match="neutralization"):
        execute_run(_mini_config(tmp_path, 1, "feedback1-pre-owner", provider="fixture"), reader=StubReader())
    assert len(providers) == 2
    assert [provider.context.session_id for provider in providers] == ["__diagnostic__", "mini-single-user"]
    assert all(provider.cleanups == 1 for provider in providers)
    run_dir = tmp_path / "feedback1-pre-owner"
    assert json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))["status"] == "INVALID"
    assert not list((run_dir / "evidence").rglob("orphan*.json"))


def test_feedback1_runner_terminalizes_before_reraising_base_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lme.providers import registry
    from lme.reader import StubReader
    from lme.runner import execute_run

    monkeypatch.setattr(registry, "provider_spec", lambda _name: _runner_spec(lambda _spec: _Provider(failure=KeyboardInterrupt())))
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
    assert contexts[0].startswith("__diagnostic__")
    assert contexts[1:] == ["mini-single-user", "mini-single-assistant_abs"]


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

    def factory(_spec):
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

    monkeypatch.setattr(registry, "provider_spec", lambda _name: _runner_spec(lambda _spec: providers.append(FailingProvider()) or providers[-1]))
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
    return type("Spec", (), {
        "factory": factory, "descriptor": "fixture", "namespace_kind": "fixture",
        "derive_namespace": staticmethod(lambda run_id, session_id: f"fixture-{run_id}-{session_id}"),
        "runtime_binding": _binding(),
    })()


def test_one_case_isolation_records_typed_na_without_a_foreign_query(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lme.providers import registry
    from lme.reader import StubReader
    from lme.runner import LmeRunInvalid, RunConfig, execute_run

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
    from lme.runner import LmeRunInvalid, RunConfig, execute_run

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
    from lme.runner import LmeRunInvalid, RunConfig, execute_run

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
        with pytest.raises(LmeRunInvalid, match=stage) as rejected:
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
