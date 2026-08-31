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
    from exomem.governance import consolidation_verification

    return consolidation_verification.build_verification_plan(
        positive_probes=(
            _probe("owner-note-full"),
            _probe("delegated-approved-projection"),
        ),
        negative_probes=(
            _probe("delegated-private-body-absent-pair"),
            _probe("wrong-purpose-wire-equivalence"),
        ),
    )


def _rebuilt_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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

    plan = _verification_plan()
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
    verify_arguments.update(
        verification_plan=plan,
        verified_at=VERIFIED_AT,
    )
    return vault, verify_arguments, plan


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
        "3270b6c5047a088e38bdbf5e7ebbc4b4c483869a8aa669ea501b4b785bc307df"
    )
    assert plan.negative_probe_digest == (
        "cc9a67cc0748b61a11a6c9073e040dbb817727fbce00cbdf4860d957d8a21efc"
    )
    assert tuple(probe.probe_digest for probe in plan.probes) == (
        "425c0416e0a1efd536e97c0395d6d7b4105b57fcb62d8db2aa4f399eac8ea269",
        "14bf94b53db7a43ca1f4b35616068ab50bca5a4d7392c77faa19f00b9e990697",
        "cb7f779b936cba0d1daefdbcc556d04a5bff777b8bc81a834ea766400fc290c8",
        "5968f29ee48f0e48bf8ec49bf30499a309854ff71b000dc0a98dd5ccef3036a7",
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
    from exomem.governance import (
        consolidation_verification,
        consolidation_verification_coordinator,
    )

    _vault, arguments, _plan = _rebuilt_run(tmp_path, monkeypatch)
    arguments["verification_plan"] = consolidation_verification.build_verification_plan(
        positive_probes=(_probe("changed-owner-probe"),),
        negative_probes=(_probe("wrong-purpose-wire-equivalence"),),
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
