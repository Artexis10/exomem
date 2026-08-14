from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def test_manifest_starts_before_call_and_trace_refuses_unfinished_runs(tmp_path: Path) -> None:
    from protocol.contracts import RATIFICATION_REPOSITORY_REVISION
    from protocol.manifest import ManifestError, finalize_manifest, load_manifest, start_manifest
    from protocol.trace import CaseTraceReader, CaseTraceWriter

    start_manifest(tmp_path, run_id="run-1", dataset={"id": "fixture", "variant": "mini", "source": "local", "revision": "1", "sha256": "a" * 64, "case_count": 1}, started_at="2026-01-01T00:00:00Z", contract_revision=RATIFICATION_REPOSITORY_REVISION)
    assert (tmp_path / "manifest.json").is_file()
    assert start_manifest(
        tmp_path / "second", run_id="run-2", dataset={"id": "fixture", "variant": "mini", "source": "local", "revision": "1", "sha256": "a" * 64, "case_count": 1}, started_at="2026-01-01T00:00:00Z", contract_revision=RATIFICATION_REPOSITORY_REVISION
    ).schema_version == 2
    with pytest.raises(ManifestError, match="non-terminal"):
        load_manifest(tmp_path)
    writer = CaseTraceWriter(tmp_path, "case-1")
    writer.append({"record": "ingest", "session_ordinal": 1, "payload_sha256": "b" * 64, "provider_ids": ["source-1"]})
    assert list(CaseTraceReader(tmp_path, "case-1"))[0].record == "ingest"
    finalize_manifest(tmp_path, status="VALID", finalized_at="2026-01-01T00:00:01Z")
    assert load_manifest(tmp_path).status == "VALID"


def _feedback6_direct_run(run_dir: Path, *, finalize: bool = True) -> Path:
    from protocol.contracts import RATIFICATION_REPOSITORY_REVISION
    from protocol.manifest import finalize_manifest, start_manifest
    from protocol.models import ProviderCleanupObservation
    from protocol.trace import CaseTraceWriter

    run_id = "feedback6-run"
    start_manifest(
        run_dir,
        run_id=run_id,
        dataset={
            "id": "fixture", "variant": "mini", "source": "local", "revision": "1",
            "sha256": "a" * 64, "case_count": 2,
        },
        started_at="2026-01-01T00:00:00Z",
        provider_variant="observed",
        contract_revision=RATIFICATION_REPOSITORY_REVISION,
    )
    expected = []
    attempts = []
    for ordinal, session_id in enumerate(("session-0001", "session-0002"), 1):
        observation = ProviderCleanupObservation(
            run_id=run_id, session_id=session_id, requested_provider="fixture",
            provider_variant="observed", namespace=f"namespace-{ordinal}", cleanup_called=True,
            required_surface_ids=["provider-state"],
            observations=[
                {"kind": "provider-state", "remaining_record_ids": [], "backend_active": False}
            ],
        )
        observation_path = Path("evidence") / session_id / "provider-cleanup-observation.json"
        full_observation_path = run_dir / observation_path
        full_observation_path.parent.mkdir(parents=True)
        payload = observation.model_dump_json(indent=2).encode() + b"\n"
        full_observation_path.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        writer = CaseTraceWriter(run_dir, session_id, schema_version=2)
        writer.append({"record": "timing", "phase": "fixture", "ms": 1})
        writer.append({
            "record": "cleanup", "run_id": run_id, "session_id": session_id,
            "namespace": f"namespace-{ordinal}", "requested_provider": "fixture",
            "observation_path": observation_path.as_posix(), "observation_sha256": digest,
        })
        expected.append({
            "run_id": run_id, "requested_provider": "fixture", "session_id": session_id,
            "namespace": f"namespace-{ordinal}", "provider_variant": "observed",
            "required_surface_ids": ["provider-state"],
            "observation_path": observation_path.as_posix(), "observation_sha256": digest,
        })
        attempts.append({
            "logical_question_id": f"logical-{ordinal}", "internal_session_id": session_id,
            "factory_returned": True, "setup_completed": True,
            "provider_variant": "observed", "failure_code": None,
        })
    (run_dir / "environment.json").write_text(
        json.dumps({
            "lme": {
                "requested_provider": "fixture", "provider_variant": "observed",
                "lifecycle_attempts": attempts, "lifecycle_expected_instances": expected,
            }
        }),
        encoding="utf-8",
    )
    if finalize:
        finalize_manifest(run_dir, status="VALID", finalized_at="2026-01-01T00:00:01Z")
    return run_dir


def _feedback6_load_worker(run_dir: str, connection) -> None:
    from protocol.manifest import load_manifest

    try:
        load_manifest(Path(run_dir))
    except BaseException:  # noqa: BLE001 - subprocess must report any failed load
        connection.send("rejected")
    else:
        connection.send("accepted")
    finally:
        connection.close()


def _feedback6_load_with_deadline(run_dir: Path) -> str:
    code = (
        "from pathlib import Path\n"
        "from protocol.manifest import load_manifest\n"
        "import sys\n"
        "try:\n"
        "    load_manifest(Path(sys.argv[1]))\n"
        "except BaseException:\n"
        "    raise SystemExit(1)\n"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", code, str(run_dir)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5.0,
        )
    except subprocess.TimeoutExpired:
        return "hung"
    return "accepted" if result.returncode == 0 else "rejected"


def test_feedback6_finalize_and_load_refuse_complete_trace_topology_matrix(
    tmp_path: Path,
) -> None:
    from protocol.manifest import finalize_manifest
    from protocol.trace import CaseTraceWriter

    base = _feedback6_direct_run(tmp_path / "base")
    labels = (
        "rename", "row-swap", "cleanup-free", "empty", "duplicate-cleanup",
        "orphan-trace", "orphan-attempt", "missing-attempt", "orphan-observation",
        "symlink", "fifo", "nonregular", "oversized", "mixed-version", "v1-downgrade",
        "invalid-row", "unknown-suffix", "unsafe-name",
    )
    outcomes: dict[str, str] = {}
    for label in labels:
        run = tmp_path / f"mutation-{label}"
        shutil.copytree(base, run)
        traces = sorted((run / "traces").glob("*.jsonl"))
        first, second = traces
        if label == "rename":
            first.rename(first.with_name("renamed.jsonl"))
        elif label == "row-swap":
            first_bytes, second_bytes = first.read_bytes(), second.read_bytes()
            first.write_bytes(second_bytes)
            second.write_bytes(first_bytes)
        elif label == "cleanup-free":
            rows = [row for row in first.read_text(encoding="utf-8").splitlines() if json.loads(row)["record"] != "cleanup"]
            first.write_text("\n".join(rows) + "\n", encoding="utf-8")
        elif label == "empty":
            first.write_bytes(b"")
        elif label == "duplicate-cleanup":
            cleanup = next(row for row in first.read_text(encoding="utf-8").splitlines() if json.loads(row)["record"] == "cleanup")
            with first.open("a", encoding="utf-8") as handle:
                handle.write(cleanup + "\n")
        elif label == "orphan-trace":
            CaseTraceWriter(run, "orphan", schema_version=2).append(
                {"record": "timing", "phase": "orphan", "ms": 1}
            )
        elif label in {"orphan-attempt", "missing-attempt"}:
            environment_path = run / "environment.json"
            environment = json.loads(environment_path.read_text(encoding="utf-8"))
            attempts = environment["lme"]["lifecycle_attempts"]
            if label == "orphan-attempt":
                attempts.append({
                    "logical_question_id": "orphan", "internal_session_id": "orphan",
                    "factory_returned": True, "setup_completed": True,
                    "provider_variant": "observed", "failure_code": None,
                })
            else:
                attempts.pop()
            environment_path.write_text(json.dumps(environment), encoding="utf-8")
        elif label == "orphan-observation":
            source = next((run / "evidence").rglob("provider-cleanup-observation.json"))
            (run / "evidence" / "orphan.json").write_bytes(source.read_bytes())
        elif label == "symlink":
            outside = tmp_path / f"{label}-outside.jsonl"
            outside.write_text(
                json.dumps({"protocol_version": "1.0.0", "schema_version": 2, "record": "timing", "phase": "x", "ms": 1}) + "\n",
                encoding="utf-8",
            )
            (run / "traces" / "linked.jsonl").symlink_to(outside)
        elif label == "fifo":
            os.mkfifo(run / "traces" / "pipe.jsonl")
        elif label == "nonregular":
            (run / "traces" / "directory.jsonl").mkdir()
        elif label == "oversized":
            with (run / "traces" / "large.jsonl").open("wb") as handle:
                handle.truncate(1_048_577)
        elif label == "mixed-version":
            with first.open("a", encoding="utf-8") as handle:
                handle.write('{"record":"timing","phase":"legacy","ms":1.0}\n')
        elif label == "v1-downgrade":
            first.write_text('{"record":"timing","phase":"legacy","ms":1.0}\n', encoding="utf-8")
        elif label == "invalid-row":
            with first.open("a", encoding="utf-8") as handle:
                handle.write('{"protocol_version":"1.0.0","schema_version":2,"record":"timing"}\n')
        elif label == "unknown-suffix":
            (run / "traces" / "ignored.txt").write_bytes(b"ignored")
        else:
            (run / "traces" / "bad\\name.jsonl").write_text(
                json.dumps({"protocol_version": "1.0.0", "schema_version": 2, "record": "timing", "phase": "x", "ms": 1}) + "\n",
                encoding="utf-8",
            )
        outcome = _feedback6_load_with_deadline(run)
        outcomes[label] = outcome

    started = _feedback6_direct_run(tmp_path / "finalize-orphan", finalize=False)
    CaseTraceWriter(started, "orphan", schema_version=2).append(
        {"record": "timing", "phase": "orphan", "ms": 1}
    )
    try:
        finalize_manifest(started, status="VALID", finalized_at="2026-01-01T00:00:01Z")
    except ValueError:
        pass
    else:
        outcomes["finalize-orphan"] = "accepted"
    outcomes.setdefault("finalize-orphan", "rejected")
    assert {label: outcome for label, outcome in outcomes.items() if outcome != "rejected"} == {}


def test_feedback6_manifest_expected_observation_variant_disagreement_matrix(
    tmp_path: Path,
) -> None:
    from protocol.manifest import ManifestError, load_manifest

    accepted: list[str] = []
    for label in (
        "manifest-vs-expected", "environment-vs-expected", "expected-vs-observation",
    ):
        run = _feedback6_direct_run(tmp_path / label)
        manifest_path = run / "manifest.json"
        environment_path = run / "environment.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        environment = json.loads(environment_path.read_text(encoding="utf-8"))
        if label == "manifest-vs-expected":
            manifest["provider_variant"] = "other"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        elif label == "environment-vs-expected":
            environment["lme"]["provider_variant"] = "other"
            environment_path.write_text(json.dumps(environment), encoding="utf-8")
        else:
            expected = environment["lme"]["lifecycle_expected_instances"][0]
            observation_path = run / expected["observation_path"]
            observation = json.loads(observation_path.read_text(encoding="utf-8"))
            observation["provider_variant"] = "other"
            payload = json.dumps(observation, separators=(",", ":")).encode() + b"\n"
            observation_path.write_bytes(payload)
            digest = hashlib.sha256(payload).hexdigest()
            old_digest = expected["observation_sha256"]
            expected["observation_sha256"] = digest
            environment_path.write_text(json.dumps(environment), encoding="utf-8")
            trace = run / "traces" / f'{expected["session_id"]}.jsonl'
            trace.write_text(trace.read_text(encoding="utf-8").replace(old_digest, digest), encoding="utf-8")
        try:
            load_manifest(run)
        except ManifestError:
            pass
        else:
            accepted.append(label)
        for trace in (run / "traces").glob("*.jsonl"):
            for row in map(json.loads, trace.read_text(encoding="utf-8").splitlines()):
                if row["record"] == "cleanup":
                    assert "provider_variant" not in row
    assert accepted == []


def test_feedback6_v1_trace_exact_bytes_and_reader_behavior_are_unchanged(tmp_path: Path) -> None:
    from protocol.trace import CaseTraceReader, CaseTraceWriter

    writer = CaseTraceWriter(tmp_path, "legacy")
    writer.append({"record": "timing", "phase": "legacy", "ms": 1})
    writer.append({"record": "cleanup", "verified": True})
    assert writer.path.read_bytes() == (
        b'{"record":"timing","phase":"legacy","ms":1.0}\n'
        b'{"record":"cleanup","verified":true}\n'
    )
    records = list(CaseTraceReader(tmp_path, "legacy"))
    assert [(row.record, getattr(row, "phase", None), getattr(row, "verified", None)) for row in records] == [
        ("timing", "legacy", None), ("cleanup", None, True),
    ]


def test_feedback6_review_v2_trace_append_refuses_a_replaced_leaf(tmp_path: Path) -> None:
    from protocol.trace import CaseTraceWriter, TraceError

    writer = CaseTraceWriter(tmp_path, "session", schema_version=2)
    writer.append({"record": "timing", "phase": "first", "ms": 1})
    displaced = tmp_path / "displaced.jsonl"
    writer.path.rename(displaced)
    replacement = writer.path
    replacement.write_bytes(
        b'{"protocol_version":"1.0.0","schema_version":2,"record":"timing","phase":"replacement","ms":1.0}\n'
    )
    before = replacement.read_bytes()

    with pytest.raises(TraceError):
        writer.append({"record": "timing", "phase": "second", "ms": 1})

    assert replacement.read_bytes() == before
    assert displaced.read_bytes().count(b"\n") == 1


@pytest.mark.parametrize("label", ("empty", "missing", "extra", "changed"))
def test_feedback7_reopened_cleanup_observation_must_exactly_match_declared_surfaces(
    tmp_path: Path, label: str,
) -> None:
    from protocol.manifest import ManifestError, load_manifest

    run = _feedback6_direct_run(tmp_path / label)
    environment_path = run / "environment.json"
    environment = json.loads(environment_path.read_text(encoding="utf-8"))
    expected = environment["lme"]["lifecycle_expected_instances"][0]
    observation_path = run / expected["observation_path"]
    observation = json.loads(observation_path.read_text(encoding="utf-8"))
    if label == "empty":
        observation["required_surface_ids"] = []
        observation["observations"] = []
        expected["required_surface_ids"] = []
    elif label == "missing":
        observation["required_surface_ids"] = ["provider-state", "session-root"]
        expected["required_surface_ids"] = ["provider-state", "session-root"]
    elif label == "extra":
        observation["observations"].append(
            {"kind": "path-lstat", "path": "work", "raw_kind": "missing", "entries": []}
        )
    else:
        observation["observations"] = [
            {"kind": "path-lstat", "path": "session-root", "raw_kind": "missing", "entries": []}
        ]
    payload = json.dumps(observation, separators=(",", ":")).encode() + b"\n"
    observation_path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    old_digest = expected["observation_sha256"]
    expected["observation_sha256"] = digest
    environment_path.write_text(json.dumps(environment), encoding="utf-8")
    trace = run / "traces" / f'{expected["session_id"]}.jsonl'
    trace.write_text(trace.read_text(encoding="utf-8").replace(old_digest, digest), encoding="utf-8")

    with pytest.raises(ManifestError, match="lifecycle evidence"):
        load_manifest(run)


def test_feedback7_terminal_direct_manifest_cannot_skip_lifecycle_with_null_variant(
    tmp_path: Path,
) -> None:
    from protocol.manifest import ManifestError, load_manifest

    run = _feedback6_direct_run(tmp_path / "null-variant")
    manifest_path = run / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["provider_variant"] = None
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ManifestError, match="lifecycle"):
        load_manifest(run)


def test_feedback8_v2_lifecycle_topology_cannot_be_disguised_as_legacy(
    tmp_path: Path,
) -> None:
    from protocol.manifest import ManifestError, load_manifest

    run = _feedback6_direct_run(tmp_path / "legacy-looking-v2")
    manifest_path = run / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["provider_variant"] = None
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    environment_path = run / "environment.json"
    environment = json.loads(environment_path.read_text(encoding="utf-8"))
    environment["lme"]["requested_provider"] = "legacy-adapter"
    environment["lme"]["provider_variant"] = None
    environment_path.write_text(json.dumps(environment), encoding="utf-8")

    with pytest.raises(ManifestError, match="lifecycle"):
        load_manifest(run)


def test_feedback9_v2_topology_cannot_bypass_lifecycle_when_mutable_labels_are_cleared(
    tmp_path: Path,
) -> None:
    from protocol.manifest import ManifestError, load_manifest

    run = _feedback6_direct_run(tmp_path / "topology-only")
    manifest_path = run / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["provider_variant"] = None
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    environment_path = run / "environment.json"
    environment = json.loads(environment_path.read_text(encoding="utf-8"))
    environment["lme"]["provider_variant"] = None
    environment["lme"]["lifecycle_attempts"] = []
    environment["lme"]["lifecycle_expected_instances"] = []
    environment_path.write_text(json.dumps(environment), encoding="utf-8")

    with pytest.raises(ManifestError, match="lifecycle"):
        load_manifest(run)


def test_feedback9_manifest_lifecycle_wrap_never_renders_captured_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import lme.providers.lifecycle as lifecycle
    from protocol.manifest import ManifestError, load_manifest

    class Hostile(lifecycle.LifecycleCompletenessError):
        def __str__(self) -> str:
            raise AssertionError("captured exception rendered")

    run = _feedback6_direct_run(tmp_path / "closed-wrapper")
    monkeypatch.setattr(
        lifecycle,
        "validate_lifecycle_completeness",
        lambda **_kwargs: (_ for _ in ()).throw(Hostile()),
    )

    with pytest.raises(ManifestError, match="lifecycle evidence is incomplete") as error:
        load_manifest(run)
    assert str(error.value) == "lifecycle evidence is incomplete"


def test_feedback9_load_holds_evidence_custody_across_verification_and_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import lme.providers.lifecycle as lifecycle
    from protocol.manifest import ManifestError, load_manifest

    run = _feedback6_direct_run(tmp_path / "transactional-evidence")
    evidence = run / "evidence"
    displaced = run / "displaced-evidence"
    original_verify = lifecycle._verify_cleanup_observation_under_root
    swapped = False

    def verify_then_replace(*args, **kwargs):
        nonlocal swapped
        payload = original_verify(*args, **kwargs)
        if not swapped:
            swapped = True
            evidence.rename(displaced)
            shutil.copytree(displaced, evidence)
        return payload

    monkeypatch.setattr(lifecycle, "_verify_cleanup_observation_under_root", verify_then_replace)
    with pytest.raises(ManifestError, match="lifecycle evidence"):
        load_manifest(run)
    assert swapped


def test_feedback10_orphan_cleanup_evidence_without_traces_still_requires_lifecycle_validation(
    tmp_path: Path,
) -> None:
    from protocol.manifest import ManifestError, load_manifest

    run = _feedback6_direct_run(tmp_path / "orphan-evidence-without-traces")
    manifest_path = run / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["provider_variant"] = None
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    environment_path = run / "environment.json"
    environment = json.loads(environment_path.read_text(encoding="utf-8"))
    environment["lme"]["provider_variant"] = None
    environment["lme"]["lifecycle_attempts"] = []
    environment["lme"]["lifecycle_expected_instances"] = []
    environment_path.write_text(json.dumps(environment), encoding="utf-8")
    shutil.rmtree(run / "traces")

    with pytest.raises(ManifestError, match="lifecycle"):
        load_manifest(run)


def test_feedback10_cleanup_observation_leaf_replacement_after_verification_invalidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import lme.providers.lifecycle as lifecycle
    from protocol.manifest import ManifestError, load_manifest

    run = _feedback6_direct_run(tmp_path / "replaced-observation-leaf")
    original_verify = lifecycle._verify_cleanup_observation_under_root
    swapped = False

    def verify_then_replace(*args, **kwargs):
        nonlocal swapped
        payload = original_verify(*args, **kwargs)
        if not swapped:
            swapped = True
            relative = args[1]
            path = run / "evidence" / relative
            replacement = path.with_suffix(".replacement")
            replacement.write_bytes(path.read_bytes())
            os.replace(replacement, path)
        return payload

    monkeypatch.setattr(lifecycle, "_verify_cleanup_observation_under_root", verify_then_replace)

    with pytest.raises(ManifestError, match="lifecycle evidence"):
        load_manifest(run)
    assert swapped


def test_feedback11_evidence_inventory_binds_the_descriptor_identity_it_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from protocol.custody import HeldDirectory
    from protocol.manifest import ManifestError, load_manifest

    run = _feedback6_direct_run(tmp_path / "inventory-leaf-replacement")
    original_read = HeldDirectory.read_regular_bounded
    swapped = False

    def replace_before_inventory_read(held, name, *, max_bytes, **kwargs):
        nonlocal swapped
        if not swapped and name == "provider-cleanup-observation.json":
            swapped = True
            path = run / "evidence" / held.logical_ref.relative_to("evidence") / name
            replacement = path.with_suffix(".replacement")
            replacement.write_bytes(path.read_bytes())
            os.replace(replacement, path)
        return original_read(held, name, max_bytes=max_bytes, **kwargs)

    monkeypatch.setattr(HeldDirectory, "read_regular_bounded", replace_before_inventory_read)

    with pytest.raises(ManifestError, match="lifecycle evidence"):
        load_manifest(run)
    assert swapped


def test_feedback12_trace_inventory_refuses_a_leaf_replaced_after_its_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import protocol.trace as trace_module
    from protocol.manifest import ManifestError, load_manifest

    run = _feedback6_direct_run(tmp_path / "trace-leaf-replacement")
    original_load = trace_module._load_trace_file
    swapped = False

    def load_then_replace(traces, name, **kwargs):
        nonlocal swapped
        loaded = original_load(traces, name, **kwargs)
        if not swapped:
            swapped = True
            path = run / "traces" / name
            replacement = path.with_suffix(".replacement")
            replacement.write_bytes(b"not a trace\n")
            os.replace(replacement, path)
        return loaded

    monkeypatch.setattr(trace_module, "_load_trace_file", load_then_replace)

    with pytest.raises(ManifestError, match="lifecycle evidence"):
        load_manifest(run)
    assert swapped


def test_feedback12_trace_inventory_entry_limit_is_closed_before_sorting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import protocol.trace as trace_module
    from protocol.trace import CaseTraceWriter, TraceError, load_trace_inventory

    monkeypatch.setattr(trace_module, "MAX_TRACE_INVENTORY_ENTRIES", 2, raising=False)
    for session_id in ("one", "two"):
        writer = CaseTraceWriter(tmp_path, session_id, schema_version=2)
        writer.append({"record": "cleanup", "run_id": "run", "session_id": session_id,
                       "namespace": session_id, "requested_provider": "fixture",
                       "observation_path": f"evidence/{session_id}.json",
                       "observation_sha256": "a" * 64})
        writer.close()

    assert len(load_trace_inventory(tmp_path)) == 2

    writer = CaseTraceWriter(tmp_path, "three", schema_version=2)
    writer.append({"record": "cleanup", "run_id": "run", "session_id": "three",
                   "namespace": "three", "requested_provider": "fixture",
                   "observation_path": "evidence/three.json", "observation_sha256": "a" * 64})
    writer.close()

    with pytest.raises(TraceError, match="entry limit"):
        load_trace_inventory(tmp_path)


def test_feedback7_v2_append_rejects_leaf_replacement_after_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import protocol.trace as trace_module
    from protocol.trace import CaseTraceWriter, TraceError

    writer = CaseTraceWriter(tmp_path, "session", schema_version=2)
    writer.append({"record": "timing", "phase": "first", "ms": 1})
    original_write = trace_module.os.write
    displaced = tmp_path / "displaced.jsonl"
    replacement = writer.path
    swapped = False

    def write_then_replace(descriptor: int, payload: bytes) -> int:
        nonlocal swapped
        written = original_write(descriptor, payload)
        if not swapped:
            swapped = True
            writer.path.rename(displaced)
            replacement.write_bytes(b'{"protocol_version":"1.0.0","schema_version":2,"record":"timing","phase":"replacement","ms":1.0}\n')
        return written

    monkeypatch.setattr(trace_module.os, "write", write_then_replace)
    with pytest.raises(TraceError, match="binding"):
        writer.append({"record": "timing", "phase": "second", "ms": 1})

    assert replacement.read_bytes().count(b"\n") == 1
    assert displaced.read_bytes().count(b"\n") == 2
