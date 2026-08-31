"""Receipt-first in-process verification after consolidation rebuilds."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from exomem.governance import consolidation_receipts

_tests_package = ModuleType("tests")
_tests_package.__path__ = [str(Path(__file__).parent)]
sys.modules.setdefault("tests", _tests_package)

from tests.test_consolidation_content_publication import (  # noqa: E402
    JOURNAL_DIGEST,
    OPERATION_ID,
    PLAN_DIGEST,
    RUN_ID,
    VAULT_BINDING,
)
from tests.test_consolidation_rebuild_coordinator import (  # noqa: E402
    POST_PUBLICATION_CENSUS,
    _published_run,
    _terminal,
)

VERIFIED_AT = "2026-08-30T12:00:07.000Z"
DRIFTED_CENSUS = hashlib.sha256(b"verification-drifted-census").hexdigest()


class SimulatedVerificationCrash(BaseException):
    pass


@pytest.fixture(autouse=True)
def _private_writer_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from exomem import writer_lease

    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR",
        str(tmp_path / "writer-state"),
    )
    writer_lease.reset_managers_for_tests()
    yield
    writer_lease.reset_managers_for_tests()


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _probe(probe_id: str) -> dict[str, str]:
    return {
        "probe_id": probe_id,
        "executor_id": "canonical-governance-surface-v1",
        "contract_digest": _digest(f"{probe_id}:contract"),
        "expected_result_digest": _digest(f"{probe_id}:pass"),
    }


def _verification_plan():
    return _verification_manifest().verification_plan


def _contract(probe_id: str) -> dict[str, object]:
    return {
        "probe_id": probe_id,
        "executor_id": "canonical-governance-surface-v1",
        "surface": "rest",
        "principal_kind": "owner",
        "principal_id": "owner",
        "purpose": "consolidation-verification",
        "command_name": "get",
        "arguments": {"path": f"Knowledge Base/Notes/{probe_id}.md"},
        "expected_result_digest": _digest(f"{probe_id}:pass"),
    }


def _verification_manifest():
    from exomem.governance import consolidation_verification_manifest

    return consolidation_verification_manifest.build_verification_manifest(
        positive_contracts=(
            _contract("owner-note-full"),
            _contract("delegated-approved-projection"),
        ),
        negative_contracts=(
            _contract("delegated-private-body-absent-pair"),
            _contract("wrong-purpose-wire-equivalence"),
        ),
    )


def _rebuilt_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    manifest=None,
):
    from exomem.governance import (
        consolidation_rebuild_coordinator,
        consolidation_verification_coordinator,
    )

    vault, arguments, _after = _published_run(tmp_path, monkeypatch)
    monkeypatch.setattr(
        consolidation_rebuild_coordinator,
        "_snapshot_census",
        lambda _root: POST_PUBLICATION_CENSUS,
    )
    monkeypatch.setattr(
        consolidation_rebuild_coordinator,
        "_component_rebuilder",
        lambda: _terminal,
    )
    rebuilt = consolidation_rebuild_coordinator.rebuild_published_destination(**arguments)
    assert rebuilt.seal_state.phase == "verifying"

    manifest = manifest or _verification_manifest()
    plan = manifest.verification_plan
    stored = SimpleNamespace(
        digest=PLAN_DIGEST,
        preimage={
            "run_id": RUN_ID,
            "plan_kind": "cutover",
            "verification_plan": {
                "schema": "exomem.consolidation-verification-plan/v1",
                "positive_probe_digest": plan.positive_probe_digest,
                "negative_probe_digest": plan.negative_probe_digest,
            },
        },
    )
    monkeypatch.setattr(
        consolidation_verification_coordinator.consolidation_plan_store.ConsolidationPlanStore,
        "load",
        lambda _store, _run_id, *, plan_kind, plan_digest: stored,
    )
    monkeypatch.setattr(
        consolidation_verification_coordinator.consolidation_verification_manifest.ConsolidationVerificationManifestStore,
        "load",
        lambda _store, _run_id, _plan_digest: manifest,
    )
    monkeypatch.setattr(
        consolidation_verification_coordinator,
        "_snapshot_census",
        lambda _root: POST_PUBLICATION_CENSUS,
    )
    verify_arguments = {
        key: arguments[key]
        for key in (
            "vault_root",
            "admission",
            "vault_binding_digest",
            "run_id",
            "operation_id",
            "journal_digest",
            "request_digest",
            "plan_digest",
        )
    }
    verify_arguments.update(verified_at=VERIFIED_AT)
    return vault, verify_arguments, plan


def _real_registry_manifest():
    from exomem import cli_ops
    from exomem.governance import (
        consolidation_verification_manifest,
        consolidation_verification_registry,
    )

    present_data = {
        "path": "Knowledge Base/Notes/destination.md",
        "frontmatter": {},
        "has_frontmatter": False,
    }
    present_wire = consolidation_verification_registry.render_rest_verification_wire(
        success=True,
        data=present_data,
    )
    missing_error = cli_ops.error_dict(
        ValueError("NOT_FOUND: file does not exist: Knowledge Base/Notes/absent.md")
    )
    missing_wire = consolidation_verification_registry.render_rest_verification_wire(
        success=False,
        error=missing_error,
    )

    def contract(probe_id: str, path: str, expected: str) -> dict[str, object]:
        return {
            "probe_id": probe_id,
            "executor_id": "canonical-governance-surface-v1",
            "surface": "rest",
            "principal_kind": "owner",
            "principal_id": "owner",
            "purpose": "consolidation-verification",
            "command_name": "read_memory",
            "arguments": {"path": path, "frontmatter_only": True},
            "expected_result_digest": expected,
        }

    return consolidation_verification_manifest.build_verification_manifest(
        positive_contracts=(
            contract(
                "owner-present",
                "Knowledge Base/Notes/destination.md",
                consolidation_verification_registry.verification_wire_result_digest(
                    "rest",
                    present_wire,
                ),
            ),
        ),
        negative_contracts=(
            contract(
                "owner-absent",
                "Knowledge Base/Notes/absent.md",
                consolidation_verification_registry.verification_wire_result_digest(
                    "rest",
                    missing_wire,
                ),
            ),
        ),
    )


def _install_passing_runner(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[str],
) -> None:
    from exomem.governance import (
        consolidation_authority,
        consolidation_verification,
        consolidation_verification_coordinator,
    )

    def run(probe, context):
        consolidation_authority.require_authority(
            context.authority,
            vault_binding_digest=VAULT_BINDING,
            run_id=RUN_ID,
            operation_id=OPERATION_ID,
            journal_digest=JOURNAL_DIGEST,
            phase="verifying",
            action="verify",
        )
        calls.append(probe.probe_id)
        return consolidation_verification.VerificationProbeTerminal(
            schema=consolidation_verification.VERIFICATION_PROBE_TERMINAL_SCHEMA,
            probe_id=probe.probe_id,
            probe_digest=probe.probe_digest,
            result_digest=probe.expected_result_digest,
            outcome="passed",
        )

    monkeypatch.setattr(
        consolidation_verification_coordinator,
        "_canonical_surface_probe_runner",
        run,
    )


def _verification_records(vault: Path) -> list[dict[str, object]]:
    return [
        record
        for record in consolidation_receipts._active_records(vault)  # noqa: SLF001
        if record.get("event_type") == "consolidation"
        and isinstance(record.get("consolidation_event"), dict)
        and record["consolidation_event"].get("kind") in {"in-process-probe", "in-process-verified"}
    ]


def test_probe_and_matrix_digests_are_fixed_framed_jcs_vectors() -> None:
    plan = _verification_plan()

    assert plan.positive_probe_digest == (
        "9c72df10e936be2d2727487907b97a7644fadfb53171477f2126744a98788612"
    )
    assert plan.negative_probe_digest == (
        "d25ca94e49a9355f3e32809424f66a4f999ad683532426bc966c8dc8f1e3a689"
    )
    assert tuple(probe.probe_digest for probe in plan.probes) == (
        "6cbf2cab57b9f55d2e84305a232fc52a087b6830388ddc366ffcc5c950be950a",
        "7f210b108f73c767a81e42c10f8a8d859ac5448779aaf142774a5b33fbcad9b0",
        "0cb339d0f20473ca576511bca06b500c8edb58fcd841bf9b07a8e05079cbfe70",
        "010273dcec2db69ba2de795f2d25842c7a56c13644cc8beb20b07010879939ca",
    )


def test_probe_plan_is_bounded_and_requires_the_closed_executor() -> None:
    from exomem.governance import consolidation_verification

    with pytest.raises(consolidation_verification.ConsolidationVerificationUnavailable):
        consolidation_verification.build_verification_plan(
            positive_probes=tuple(_probe(f"positive-{ordinal}") for ordinal in range(1024)),
            negative_probes=(_probe("one-negative"),),
        )
    changed = _probe("changed-executor")
    changed["executor_id"] = "caller-selected-runner-v1"
    with pytest.raises(consolidation_verification.ConsolidationVerificationUnavailable):
        consolidation_verification.build_verification_plan(
            positive_probes=(changed,),
            negative_probes=(_probe("one-negative"),),
        )


def test_verification_runs_bound_positive_and_negative_probes_then_advances_seal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import (
        consolidation_verification_coordinator,
        consolidation_verification_journal,
    )

    vault, arguments, plan = _rebuilt_run(tmp_path, monkeypatch)
    calls: list[str] = []
    _install_passing_runner(monkeypatch, calls)

    result = consolidation_verification_coordinator.verify_rebuilt_destination(
        **arguments,
    )

    expected_ids = tuple(probe.probe_id for probe in plan.probes)
    assert tuple(calls) == expected_ids
    assert result.completed_probe_ids == expected_ids
    assert result.verification_basis_digest == result.verification_journal.binding_digest
    assert result.seal_state.phase == "verified"
    assert all(entry.status == "final" for entry in result.verification_journal.probes)
    assert result.verification_journal.terminal.status == "final"
    assert (
        consolidation_verification_journal.ConsolidationVerificationJournalStore(
            vault,
            run_id=RUN_ID,
        ).load()
        == result.verification_journal
    )

    records = _verification_records(vault)
    intents = [
        consolidation_receipts.validate_nested(record["consolidation_event"], outer_phase="intent")
        for record in records
        if record["phase"] == "intent"
    ]
    assert [intent["kind"] for intent in intents] == [
        "in-process-probe",
        "in-process-probe",
        "in-process-probe",
        "in-process-probe",
        "in-process-verified",
    ]
    assert [intent["probe_ordinal"] for intent in intents[:-1]] == [0, 1, 2, 3]
    assert intents[-1].get("probe_ordinal") is None
    assert all(
        later["semantic_parent_event_id"] == previous["event_id"]
        for previous, later in zip(
            [record for record in records if record["phase"] == "committed"][:-1],
            intents[1:],
            strict=True,
        )
    )


def test_uninstalled_canonical_probe_registry_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import (
        consolidation_seal,
        consolidation_verification_coordinator,
    )

    vault, arguments, _plan = _rebuilt_run(tmp_path, monkeypatch)

    with pytest.raises(
        consolidation_verification_coordinator.ConsolidationVerificationCoordinatorUnavailable,
        match="^CONSOLIDATION_VERIFICATION_COORDINATOR_UNAVAILABLE$",
    ):
        consolidation_verification_coordinator.verify_rebuilt_destination(**arguments)

    assert (
        consolidation_seal.ConsolidationSealStore(vault)
        .load(vault_binding_digest=VAULT_BINDING)
        .phase
        == "verifying"
    )
    assert not any(record["phase"] == "committed" for record in _verification_records(vault))


def test_installed_registry_verifies_real_present_and_absent_rest_responses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import consolidation_verification_coordinator

    manifest = _real_registry_manifest()
    _vault, arguments, plan = _rebuilt_run(
        tmp_path,
        monkeypatch,
        manifest=manifest,
    )

    result = consolidation_verification_coordinator.verify_rebuilt_destination(
        **arguments,
    )

    assert result.seal_state.phase == "verified"
    assert result.completed_probe_ids == tuple(probe.probe_id for probe in plan.probes)


def test_tampered_verification_journal_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import (
        consolidation_plan,
        consolidation_verification_coordinator,
        consolidation_verification_journal,
    )

    vault, arguments, _plan = _rebuilt_run(tmp_path, monkeypatch)
    _install_passing_runner(monkeypatch, [])
    consolidation_verification_coordinator.verify_rebuilt_destination(
        **arguments,
    )
    store = consolidation_verification_journal.ConsolidationVerificationJournalStore(
        vault,
        run_id=RUN_ID,
    )
    value = json.loads(store.path.read_bytes())
    value["probes"][0]["probe_id"] = "tampered-probe"
    store.path.write_bytes(consolidation_plan.canonical_closed_jcs(value))

    with pytest.raises(
        consolidation_verification_journal.ConsolidationVerificationJournalUnavailable,
        match="^CONSOLIDATION_VERIFICATION_JOURNAL_UNAVAILABLE$",
    ):
        store.load()


def test_mismatched_probe_result_fails_closed_in_verifying(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import (
        consolidation_seal,
        consolidation_verification,
        consolidation_verification_coordinator,
    )

    vault, arguments, _plan = _rebuilt_run(tmp_path, monkeypatch)

    def mismatched(probe, _context):
        return consolidation_verification.VerificationProbeTerminal(
            schema=consolidation_verification.VERIFICATION_PROBE_TERMINAL_SCHEMA,
            probe_id=probe.probe_id,
            probe_digest=probe.probe_digest,
            result_digest=_digest("unexpected-wire-result"),
            outcome="passed",
        )

    monkeypatch.setattr(
        consolidation_verification_coordinator,
        "_canonical_surface_probe_runner",
        mismatched,
    )

    with pytest.raises(
        consolidation_verification_coordinator.ConsolidationVerificationCoordinatorUnavailable,
        match="^CONSOLIDATION_VERIFICATION_COORDINATOR_UNAVAILABLE$",
    ):
        consolidation_verification_coordinator.verify_rebuilt_destination(
            **arguments,
        )

    assert (
        consolidation_seal.ConsolidationSealStore(vault)
        .load(vault_binding_digest=VAULT_BINDING)
        .phase
        == "verifying"
    )
    assert not any(record["phase"] == "committed" for record in _verification_records(vault))


def test_retry_after_probe_result_does_not_rerun_the_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import (
        consolidation_effect_coordinator,
        consolidation_verification_coordinator,
    )

    _vault, arguments, plan = _rebuilt_run(tmp_path, monkeypatch)
    calls: list[str] = []
    crashed = False
    _install_passing_runner(monkeypatch, calls)

    def crash_after_result(point: str) -> None:
        nonlocal crashed
        if point == "after-effect" and not crashed:
            crashed = True
            raise SimulatedVerificationCrash

    monkeypatch.setattr(
        consolidation_effect_coordinator,
        "_crash_point",
        crash_after_result,
    )
    with pytest.raises(SimulatedVerificationCrash):
        consolidation_verification_coordinator.verify_rebuilt_destination(
            **arguments,
        )
    assert calls == [plan.probes[0].probe_id]

    monkeypatch.setattr(
        consolidation_effect_coordinator,
        "_crash_point",
        lambda _point: None,
    )
    result = consolidation_verification_coordinator.verify_rebuilt_destination(
        **arguments,
    )

    assert result.seal_state.phase == "verified"
    assert calls.count(plan.probes[0].probe_id) == 1
    assert tuple(calls) == tuple(probe.probe_id for probe in plan.probes)


def test_retry_after_probe_prepared_runs_the_probe_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import (
        consolidation_effect_coordinator,
        consolidation_verification_coordinator,
    )

    _vault, arguments, plan = _rebuilt_run(tmp_path, monkeypatch)
    calls: list[str] = []
    crashed = False
    _install_passing_runner(monkeypatch, calls)

    def crash_after_prepared(point: str) -> None:
        nonlocal crashed
        if point == "after-prepared" and not crashed:
            crashed = True
            raise SimulatedVerificationCrash

    monkeypatch.setattr(
        consolidation_effect_coordinator,
        "_crash_point",
        crash_after_prepared,
    )
    with pytest.raises(SimulatedVerificationCrash):
        consolidation_verification_coordinator.verify_rebuilt_destination(
            **arguments,
        )
    assert calls == []

    monkeypatch.setattr(
        consolidation_effect_coordinator,
        "_crash_point",
        lambda _point: None,
    )
    result = consolidation_verification_coordinator.verify_rebuilt_destination(
        **arguments,
    )

    assert result.seal_state.phase == "verified"
    assert tuple(calls) == tuple(probe.probe_id for probe in plan.probes)


def test_retry_after_verified_terminal_only_advances_the_seal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import consolidation_verification_coordinator

    _vault, arguments, plan = _rebuilt_run(tmp_path, monkeypatch)
    calls: list[str] = []
    _install_passing_runner(monkeypatch, calls)

    def crash(point: str) -> None:
        if point == "before-verified":
            raise SimulatedVerificationCrash

    monkeypatch.setattr(consolidation_verification_coordinator, "_crash_point", crash)
    with pytest.raises(SimulatedVerificationCrash):
        consolidation_verification_coordinator.verify_rebuilt_destination(
            **arguments,
        )
    assert tuple(calls) == tuple(probe.probe_id for probe in plan.probes)

    monkeypatch.setattr(
        consolidation_verification_coordinator,
        "_crash_point",
        lambda _point: None,
    )
    monkeypatch.setattr(
        consolidation_verification_coordinator,
        "_canonical_surface_probe_runner",
        lambda *_args: pytest.fail("final probes must not rerun"),
    )
    result = consolidation_verification_coordinator.verify_rebuilt_destination(
        **arguments,
    )

    assert result.seal_state.phase == "verified"


def test_canonical_census_drift_during_probe_keeps_destination_sealed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import (
        consolidation_seal,
        consolidation_verification_coordinator,
    )

    vault, arguments, _plan = _rebuilt_run(tmp_path, monkeypatch)
    _install_passing_runner(monkeypatch, [])
    snapshots = iter((POST_PUBLICATION_CENSUS, DRIFTED_CENSUS))
    monkeypatch.setattr(
        consolidation_verification_coordinator,
        "_snapshot_census",
        lambda _root: next(snapshots),
    )

    with pytest.raises(
        consolidation_verification_coordinator.ConsolidationVerificationCoordinatorUnavailable
    ):
        consolidation_verification_coordinator.verify_rebuilt_destination(
            **arguments,
        )

    assert (
        consolidation_seal.ConsolidationSealStore(vault)
        .load(vault_binding_digest=VAULT_BINDING)
        .phase
        == "verifying"
    )


def test_changed_probe_matrix_is_rejected_before_any_probe_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import consolidation_verification_coordinator

    _vault, arguments, _plan = _rebuilt_run(tmp_path, monkeypatch)
    changed_manifest = consolidation_verification_coordinator.consolidation_verification_manifest.build_verification_manifest(
        positive_contracts=(_contract("changed-owner-probe"),),
        negative_contracts=(_contract("wrong-purpose-wire-equivalence"),),
    )
    monkeypatch.setattr(
        consolidation_verification_coordinator.consolidation_verification_manifest.ConsolidationVerificationManifestStore,
        "load",
        lambda _store, _run_id, _plan_digest: changed_manifest,
    )
    calls: list[str] = []
    _install_passing_runner(monkeypatch, calls)

    with pytest.raises(
        consolidation_verification_coordinator.ConsolidationVerificationCoordinatorUnavailable
    ):
        consolidation_verification_coordinator.verify_rebuilt_destination(
            **arguments,
        )

    assert calls == []
