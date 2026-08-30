from __future__ import annotations

import hashlib
import multiprocessing
import os
import sys
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import pytest

VAULT_BINDING = hashlib.sha256(b"admission-vault").hexdigest()
OTHER_VAULT_BINDING = hashlib.sha256(b"other-admission-vault").hexdigest()
RUN_ID = "00000000-0000-4000-8000-000000000061"
OPERATION_ID = "00000000-0000-4000-8000-000000000062"
JOURNAL_DIGEST = hashlib.sha256(b"admission-journal").hexdigest()
REQUEST_DIGEST = hashlib.sha256(b"admission-request").hexdigest()
OTHER_REQUEST_DIGEST = hashlib.sha256(b"other-admission-request").hexdigest()
T0 = "2026-08-28T16:00:00.000Z"
T1 = "2026-08-28T16:00:01.000Z"
T2 = "2026-08-28T16:00:02.000Z"


def _open_admission(vault: Path, *, binding: str = VAULT_BINDING):
    from exomem.governance import consolidation_admission, consolidation_seal

    store = consolidation_seal.ConsolidationSealStore(vault)
    store.initialize_open(vault_binding_digest=binding, recorded_at=T0)
    return consolidation_admission.ConsolidationAdmission(
        vault,
        vault_binding_digest=binding,
    )


def _apply_authority():
    from exomem.governance import consolidation_authority

    return consolidation_authority.issue_authority(
        vault_binding_digest=VAULT_BINDING,
        run_id=RUN_ID,
        operation_id=OPERATION_ID,
        journal_digest=JOURNAL_DIGEST,
        phase="sealing",
        action="apply",
    )


def _convert_apply(admission):
    ordinary = admission.admit_mutation()
    mutation = ordinary.__enter__()
    try:
        control = admission.convert_control_mutation(
            mutation,
            authority=_apply_authority(),
            run_id=RUN_ID,
            operation_id=OPERATION_ID,
            journal_digest=JOURNAL_DIGEST,
            request_digest=REQUEST_DIGEST,
            phase="sealing",
            action="apply",
        )
    except BaseException:
        ordinary.__exit__(*sys.exc_info())
        raise
    return ordinary, control


def _seal_with_new_control(
    admission,
    *,
    authority=None,
    timeout: float = 2.0,
    stoppers=(),
):
    exact_authority = _apply_authority() if authority is None else authority
    with admission.admit_mutation() as mutation:
        control = admission.convert_control_mutation(
            mutation,
            authority=exact_authority,
            run_id=RUN_ID,
            operation_id=OPERATION_ID,
            journal_digest=JOURNAL_DIGEST,
            request_digest=REQUEST_DIGEST,
            phase="sealing",
            action="apply",
        )
        return admission.seal_and_drain(
            control=control,
            authority=exact_authority,
            run_id=RUN_ID,
            operation_id=OPERATION_ID,
            journal_digest=JOURNAL_DIGEST,
            sealed_at=T1,
            completed_at=T2,
            expected_revision=0,
            timeout=timeout,
            stoppers=stoppers,
        )


def _seal_with_recovered_control(admission, *, timeout: float = 2.0):
    with admission.resume_control_mutation(
        authority=_apply_authority(),
        run_id=RUN_ID,
        operation_id=OPERATION_ID,
        journal_digest=JOURNAL_DIGEST,
        request_digest=REQUEST_DIGEST,
        phase="sealing",
        action="apply",
    ) as control:
        return admission.seal_and_drain(
            control=control,
            authority=_apply_authority(),
            run_id=RUN_ID,
            operation_id=OPERATION_ID,
            journal_digest=JOURNAL_DIGEST,
            sealed_at=T1,
            completed_at=T2,
            expected_revision=0,
            timeout=timeout,
        )


def _assert_admission_error(code: str):
    from exomem.governance import consolidation_admission

    return pytest.raises(
        consolidation_admission.ConsolidationAdmissionUnavailable,
        match=f"^{code}$",
    )


def _hold_process_read(
    vault: str,
    entered: Any,
    release: Any,
) -> None:
    from exomem.governance import consolidation_admission

    admission = consolidation_admission.ConsolidationAdmission(
        Path(vault),
        vault_binding_digest=VAULT_BINDING,
    )
    with admission.admit_read():
        entered.set()
        release.wait(5.0)


def _crash_process_read(vault: str, entered: Any) -> None:
    from exomem.governance import consolidation_admission

    admission = consolidation_admission.ConsolidationAdmission(
        Path(vault),
        vault_binding_digest=VAULT_BINDING,
    )
    with admission.admit_read():
        entered.set()
        os._exit(0)


def test_seal_intent_precedes_stop_and_new_work_while_admitted_read_drains(
    tmp_path: Path,
) -> None:
    from exomem.governance import consolidation_admission, consolidation_seal

    controller = _open_admission(tmp_path)
    admission = consolidation_admission.ConsolidationAdmission(
        tmp_path,
        vault_binding_digest=VAULT_BINDING,
    )
    admitted = admission.admit_read()
    admitted.__enter__()
    stopper_observed = threading.Event()
    finished = threading.Event()
    failure: list[Exception] = []

    def stop_background() -> None:
        state = consolidation_seal.ConsolidationSealStore(tmp_path).load(
            vault_binding_digest=VAULT_BINDING
        )
        assert state.phase == "sealing"
        for enter in (
            admission.admit_read,
            admission.admit_mutation,
            admission.admit_transfer,
            admission.admit_background,
        ):
            with _assert_admission_error("CONSOLIDATION_SEALED"):
                with enter():
                    pass
        stopper_observed.set()

    def seal() -> None:
        try:
            _seal_with_new_control(
                controller,
                timeout=2.0,
                stoppers=(stop_background,),
            )
        except Exception as error:  # noqa: BLE001  # pragma: no cover - asserted below
            failure.append(error)
        finally:
            finished.set()

    worker = threading.Thread(target=seal)
    worker.start()
    assert stopper_observed.wait(1.0)
    assert not finished.wait(0.05)

    admitted.__exit__(None, None, None)
    worker.join(2.0)
    assert not worker.is_alive()
    assert failure == []
    assert controller.snapshot().state.phase == "sealed"
    assert admission.snapshot().state.phase == "sealed"
    assert controller.snapshot().active_total == 0
    with _assert_admission_error("CONSOLIDATION_SEALED"):
        with admission.admit_read():
            pass


def test_seal_drains_participant_admitted_by_another_process(tmp_path: Path) -> None:
    from exomem.governance import consolidation_seal

    admission = _open_admission(tmp_path)
    context = multiprocessing.get_context("spawn")
    entered = context.Event()
    release = context.Event()
    participant = context.Process(
        target=_hold_process_read,
        args=(str(tmp_path), entered, release),
    )
    participant.start()
    assert entered.wait(3.0)
    finished = threading.Event()
    failures: list[Exception] = []

    def seal() -> None:
        try:
            _seal_with_new_control(
                admission,
                timeout=4.0,
            )
        except Exception as error:  # noqa: BLE001  # pragma: no cover - asserted below
            failures.append(error)
        finally:
            finished.set()

    controller = threading.Thread(target=seal)
    controller.start()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        state = consolidation_seal.ConsolidationSealStore(tmp_path).load(
            vault_binding_digest=VAULT_BINDING
        )
        if state.phase == "sealing":
            break
        time.sleep(0.005)
    assert state.phase == "sealing"
    assert not finished.wait(0.05)
    with _assert_admission_error("CONSOLIDATION_SEALED"):
        with admission.admit_mutation():
            pass

    release.set()
    participant.join(3.0)
    controller.join(3.0)
    assert participant.exitcode == 0
    assert not controller.is_alive()
    assert failures == []
    assert admission.snapshot().state.phase == "sealed"


def test_seal_reclaims_same_domain_record_after_participant_process_crash(
    tmp_path: Path,
) -> None:
    admission = _open_admission(tmp_path)
    context = multiprocessing.get_context("spawn")
    entered = context.Event()
    participant = context.Process(
        target=_crash_process_read,
        args=(str(tmp_path), entered),
    )
    participant.start()
    assert entered.wait(3.0)
    participant.join(3.0)
    assert participant.exitcode == 0
    assert admission.snapshot().active_reads == 1

    sealed = _seal_with_new_control(admission, timeout=2.0)

    assert sealed.state.phase == "sealed"
    assert sealed.active_total == 0
    assert admission.snapshot().active_total == 0


def test_foreign_state_domain_fails_immediately_without_deleting_evidence(
    tmp_path: Path,
) -> None:
    from exomem.governance import consolidation_admission, consolidation_seal

    admission = _open_admission(tmp_path)
    participant_id = "a" * 32
    admission._publish_participant(  # noqa: SLF001 - adversarial stored-state fixture
        consolidation_admission._Participant(  # noqa: SLF001
            participant_id,
            "read",
            hashlib.sha256(b"foreign-state-domain").hexdigest(),
        )
    )

    started = time.monotonic()
    with _assert_admission_error("CONSOLIDATION_ADMISSION_DOMAIN_CONFLICT"):
        _seal_with_new_control(admission, timeout=2.0)
    assert time.monotonic() - started < 0.5

    durable = consolidation_seal.ConsolidationSealStore(tmp_path).load(
        vault_binding_digest=VAULT_BINDING
    )
    assert durable.phase == "sealing"
    assert admission.snapshot().active_reads == 1
    assert admission._load_participant(participant_id) is not None  # noqa: SLF001


def test_drain_timeout_leaves_recoverable_durable_sealing_state(tmp_path: Path) -> None:
    from exomem.governance import consolidation_seal

    admission = _open_admission(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def hold_transfer() -> None:
        with admission.admit_transfer():
            entered.set()
            release.wait(2.0)

    participant = threading.Thread(target=hold_transfer)
    participant.start()
    assert entered.wait(1.0)
    with _assert_admission_error("CONSOLIDATION_DRAIN_TIMEOUT"):
        _seal_with_new_control(admission, timeout=0.05)
    release.set()
    participant.join(2.0)
    assert not participant.is_alive()

    durable = consolidation_seal.ConsolidationSealStore(tmp_path).load(
        vault_binding_digest=VAULT_BINDING
    )
    assert durable.kind == "consolidation-sealed"
    assert durable.phase == "sealing"
    assert durable.revision == 1
    with _assert_admission_error("CONSOLIDATION_SEALED"):
        with admission.admit_mutation():
            pass

    recovered = _seal_with_recovered_control(admission, timeout=1.0)
    assert recovered.state.phase == "sealed"
    assert recovered.draining is False


def test_restart_loads_nonterminal_seal_before_any_ordinary_admission(
    tmp_path: Path,
) -> None:
    from exomem.governance import consolidation_admission, consolidation_seal

    store = consolidation_seal.ConsolidationSealStore(tmp_path)
    store.initialize_open(vault_binding_digest=VAULT_BINDING, recorded_at=T0)
    store.begin_consolidation(
        vault_binding_digest=VAULT_BINDING,
        run_id=RUN_ID,
        operation_id=OPERATION_ID,
        journal_digest=JOURNAL_DIGEST,
        sealed_at=T1,
        expected_revision=0,
    )

    restarted = consolidation_admission.ConsolidationAdmission(
        tmp_path,
        vault_binding_digest=VAULT_BINDING,
    )
    assert restarted.snapshot().state.phase == "sealing"
    for enter in (
        restarted.admit_read,
        restarted.admit_mutation,
        restarted.admit_transfer,
        restarted.admit_background,
    ):
        with _assert_admission_error("CONSOLIDATION_SEALED"):
            with enter():
                pass


def test_enrolled_admission_never_interprets_missing_seal_as_open(tmp_path: Path) -> None:
    from exomem.governance import consolidation_admission

    with _assert_admission_error("CONSOLIDATION_SEAL_UNAVAILABLE"):
        consolidation_admission.ConsolidationAdmission(
            tmp_path,
            vault_binding_digest=VAULT_BINDING,
        )


def test_all_ordinary_participant_kinds_are_counted_until_exit(tmp_path: Path) -> None:
    admission = _open_admission(tmp_path)
    with ExitStack() as stack:
        stack.enter_context(admission.admit_read())
        stack.enter_context(admission.admit_mutation())
        stack.enter_context(admission.admit_transfer())
        stack.enter_context(admission.admit_background())
        snapshot = admission.snapshot()
        assert snapshot.active_reads == 1
        assert snapshot.active_mutations == 1
        assert snapshot.active_transfers == 1
        assert snapshot.active_background == 1
        assert snapshot.active_total == 4
    assert admission.snapshot().active_total == 0


def test_other_canonical_vault_remains_independently_open(tmp_path: Path) -> None:
    first = _open_admission(tmp_path / "first")
    second = _open_admission(tmp_path / "second", binding=OTHER_VAULT_BINDING)
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def hold_first() -> None:
        with first.admit_read():
            entered.set()
            release.wait(2.0)

    def seal_first() -> None:
        _seal_with_new_control(first, timeout=2.0)
        finished.set()

    participant = threading.Thread(target=hold_first)
    participant.start()
    assert entered.wait(1.0)
    controller = threading.Thread(target=seal_first)
    controller.start()
    deadline = time.monotonic() + 1.0
    while first.reload().state.phase != "sealing" and time.monotonic() < deadline:
        time.sleep(0.005)
    assert first.reload().state.phase == "sealing"
    assert not finished.is_set()
    try:
        with second.admit_mutation():
            assert second.snapshot().active_mutations == 1
    finally:
        release.set()
    participant.join(2.0)
    controller.join(2.0)
    assert not participant.is_alive()
    assert not controller.is_alive()
    assert finished.is_set()


@pytest.mark.parametrize("timeout", [float("inf"), float("-inf"), float("nan")])
def test_non_finite_drain_timeout_is_rejected_before_seal(
    tmp_path: Path,
    timeout: float,
) -> None:
    admission = _open_admission(tmp_path)

    with pytest.raises(ValueError, match="finite and non-negative"):
        admission.seal_and_drain(
            control=object(),
            authority=_apply_authority(),
            run_id=RUN_ID,
            operation_id=OPERATION_ID,
            journal_digest=JOURNAL_DIGEST,
            sealed_at=T1,
            completed_at=T2,
            expected_revision=0,
            timeout=timeout,
        )

    assert admission.snapshot().state.kind == "open"


def test_deletion_seal_uses_same_content_free_ordinary_refusal(tmp_path: Path) -> None:
    from exomem.governance import consolidation_admission, consolidation_seal

    store = consolidation_seal.ConsolidationSealStore(tmp_path)
    opened = store.initialize_open(vault_binding_digest=VAULT_BINDING, recorded_at=T0)
    store.seal_for_deletion(
        vault_binding_digest=VAULT_BINDING,
        checkpoint_digest=hashlib.sha256(b"checkpoint").hexdigest(),
        sealed_at=T1,
        expected_revision=opened.revision,
    )
    restarted = consolidation_admission.ConsolidationAdmission(
        tmp_path,
        vault_binding_digest=VAULT_BINDING,
    )
    assert restarted.snapshot().state.kind == "deletion-sealed"
    with _assert_admission_error("CONSOLIDATION_SEALED"):
        with restarted.admit_read():
            pass


def test_background_stop_failure_keeps_durable_seal_intent(tmp_path: Path) -> None:
    from exomem.governance import consolidation_seal

    admission = _open_admission(tmp_path)

    def fail() -> None:
        raise RuntimeError("private worker detail")

    with _assert_admission_error("CONSOLIDATION_DRAIN_FAILED"):
        _seal_with_new_control(
            admission,
            timeout=1.0,
            stoppers=(fail,),
        )
    durable = consolidation_seal.ConsolidationSealStore(tmp_path).load(
        vault_binding_digest=VAULT_BINDING
    )
    assert durable.phase == "sealing"


def test_untrusted_authority_cannot_create_denial_of_service_seal(tmp_path: Path) -> None:
    admission = _open_admission(tmp_path)
    with _assert_admission_error("CONSOLIDATION_AUTHORITY_UNAVAILABLE"):
        _seal_with_new_control(admission, authority=object(), timeout=1.0)
    assert admission.snapshot().state.kind == "open"
    with admission.admit_read():
        pass


def test_hung_background_stopper_is_bounded_and_keeps_seal_intent(
    tmp_path: Path,
) -> None:
    from exomem.governance import consolidation_seal

    admission = _open_admission(tmp_path)
    release = threading.Event()

    def hang() -> None:
        release.wait(5.0)

    started = time.monotonic()
    with _assert_admission_error("CONSOLIDATION_DRAIN_TIMEOUT"):
        _seal_with_new_control(
            admission,
            timeout=0.05,
            stoppers=(hang,),
        )
    elapsed = time.monotonic() - started
    release.set()
    assert elapsed < 0.5
    durable = consolidation_seal.ConsolidationSealStore(tmp_path).load(
        vault_binding_digest=VAULT_BINDING
    )
    assert durable.phase == "sealing"


def test_control_conversion_excludes_only_itself_before_seal_drain(
    tmp_path: Path,
) -> None:
    admission = _open_admission(tmp_path)
    other = admission.admit_mutation()
    other.__enter__()
    ordinary, control = _convert_apply(admission)
    finished = threading.Event()
    failures: list[Exception] = []

    assert admission.snapshot().active_mutations == 1

    def seal() -> None:
        try:
            admission.seal_and_drain(
                control=control,
                authority=_apply_authority(),
                run_id=RUN_ID,
                operation_id=OPERATION_ID,
                journal_digest=JOURNAL_DIGEST,
                sealed_at=T1,
                completed_at=T2,
                expected_revision=0,
                timeout=2.0,
            )
        except Exception as error:  # noqa: BLE001  # pragma: no cover - asserted below
            failures.append(error)
        finally:
            finished.set()

    controller = threading.Thread(target=seal)
    controller.start()
    deadline = time.monotonic() + 1.0
    while admission.reload().state.phase != "sealing" and time.monotonic() < deadline:
        time.sleep(0.005)
    assert admission.reload().state.phase == "sealing"
    assert not finished.wait(0.05)

    other.__exit__(None, None, None)
    controller.join(2.0)
    ordinary.__exit__(None, None, None)
    assert not controller.is_alive()
    assert failures == []
    assert admission.snapshot().state.phase == "sealed"
    assert admission.snapshot().active_total == 0


def test_converted_control_blocks_late_ordinary_admission_before_seal_intent(
    tmp_path: Path,
) -> None:
    admission = _open_admission(tmp_path)
    ordinary, _control = _convert_apply(admission)
    try:
        assert admission.snapshot().state.kind == "open"
        assert admission.snapshot().active_mutations == 0
        for enter in (
            admission.admit_read,
            admission.admit_mutation,
            admission.admit_transfer,
            admission.admit_background,
        ):
            with _assert_admission_error("CONSOLIDATION_CONTROL_PENDING"):
                with enter():
                    pass
    finally:
        ordinary.__exit__(None, None, None)


def test_restart_recovers_only_exact_converted_control_operation(
    tmp_path: Path,
) -> None:
    from exomem.governance import consolidation_admission

    admission = _open_admission(tmp_path)
    ordinary, _control = _convert_apply(admission)
    ordinary.__exit__(None, None, None)

    restarted = consolidation_admission.ConsolidationAdmission(
        tmp_path,
        vault_binding_digest=VAULT_BINDING,
    )
    with _assert_admission_error("CONSOLIDATION_CONTROL_CONFLICT"):
        with restarted.resume_control_mutation(
            authority=_apply_authority(),
            run_id=RUN_ID,
            operation_id=OPERATION_ID,
            journal_digest=JOURNAL_DIGEST,
            request_digest=OTHER_REQUEST_DIGEST,
            phase="sealing",
            action="apply",
        ):
            pass

    with restarted.resume_control_mutation(
        authority=_apply_authority(),
        run_id=RUN_ID,
        operation_id=OPERATION_ID,
        journal_digest=JOURNAL_DIGEST,
        request_digest=REQUEST_DIGEST,
        phase="sealing",
        action="apply",
    ) as recovered:
        sealed = restarted.seal_and_drain(
            control=recovered,
            authority=_apply_authority(),
            run_id=RUN_ID,
            operation_id=OPERATION_ID,
            journal_digest=JOURNAL_DIGEST,
            sealed_at=T1,
            completed_at=T2,
            expected_revision=0,
            timeout=1.0,
        )
    assert sealed.state.phase == "sealed"


def test_forged_control_handle_cannot_exclude_itself_from_drain(tmp_path: Path) -> None:
    admission = _open_admission(tmp_path)

    with _assert_admission_error("CONSOLIDATION_CONTROL_UNAVAILABLE"):
        admission.seal_and_drain(
            control=object(),
            authority=_apply_authority(),
            run_id=RUN_ID,
            operation_id=OPERATION_ID,
            journal_digest=JOURNAL_DIGEST,
            sealed_at=T1,
            completed_at=T2,
            expected_revision=0,
            timeout=1.0,
        )
    assert admission.snapshot().state.kind == "open"


def test_control_handle_expires_when_its_admission_scope_exits(tmp_path: Path) -> None:
    admission = _open_admission(tmp_path)
    ordinary, control = _convert_apply(admission)
    ordinary.__exit__(None, None, None)

    with _assert_admission_error("CONSOLIDATION_CONTROL_UNAVAILABLE"):
        admission.seal_and_drain(
            control=control,
            authority=_apply_authority(),
            run_id=RUN_ID,
            operation_id=OPERATION_ID,
            journal_digest=JOURNAL_DIGEST,
            sealed_at=T1,
            completed_at=T2,
            expected_revision=0,
            timeout=1.0,
        )
    assert admission.snapshot().state.kind == "open"


def test_foreign_domain_control_is_preserved_and_never_recovered_locally(
    tmp_path: Path,
) -> None:
    from exomem.governance import consolidation_admission

    admission = _open_admission(tmp_path)
    participant_id = "b" * 32
    admission._publish_participant(  # noqa: SLF001 - adversarial stored-state fixture
        consolidation_admission._Participant(  # noqa: SLF001
            participant_id,
            "control",
            hashlib.sha256(b"foreign-control-domain").hexdigest(),
            RUN_ID,
            OPERATION_ID,
            JOURNAL_DIGEST,
            REQUEST_DIGEST,
            "sealing",
            "apply",
        )
    )

    with _assert_admission_error("CONSOLIDATION_ADMISSION_DOMAIN_CONFLICT"):
        with admission.resume_control_mutation(
            authority=_apply_authority(),
            run_id=RUN_ID,
            operation_id=OPERATION_ID,
            journal_digest=JOURNAL_DIGEST,
            request_digest=REQUEST_DIGEST,
            phase="sealing",
            action="apply",
        ):
            pass
    assert admission._load_participant(participant_id) is not None  # noqa: SLF001


def test_control_record_is_revalidated_inside_seal_intent_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admission = _open_admission(tmp_path)
    ordinary, control = _convert_apply(admission)
    original = admission._require_control  # noqa: SLF001

    def remove_after_validation(*args, **kwargs):
        participant = original(*args, **kwargs)
        admission._remove_participant(participant.participant_id)  # noqa: SLF001
        return participant

    monkeypatch.setattr(admission, "_require_control", remove_after_validation)
    try:
        with _assert_admission_error("CONSOLIDATION_CONTROL_UNAVAILABLE"):
            admission.seal_and_drain(
                control=control,
                authority=_apply_authority(),
                run_id=RUN_ID,
                operation_id=OPERATION_ID,
                journal_digest=JOURNAL_DIGEST,
                sealed_at=T1,
                completed_at=T2,
                expected_revision=0,
                timeout=1.0,
            )
    finally:
        ordinary.__exit__(None, None, None)
    assert admission.snapshot().state.kind == "open"


def test_two_pre_admitted_mutations_cannot_both_convert_to_control(
    tmp_path: Path,
) -> None:
    admission = _open_admission(tmp_path)
    first_scope = admission.admit_mutation()
    second_scope = admission.admit_mutation()
    first = first_scope.__enter__()
    second = second_scope.__enter__()
    try:
        admission.convert_control_mutation(
            first,
            authority=_apply_authority(),
            run_id=RUN_ID,
            operation_id=OPERATION_ID,
            journal_digest=JOURNAL_DIGEST,
            request_digest=REQUEST_DIGEST,
            phase="sealing",
            action="apply",
        )
        with _assert_admission_error("CONSOLIDATION_CONTROL_CONFLICT"):
            admission.convert_control_mutation(
                second,
                authority=_apply_authority(),
                run_id=RUN_ID,
                operation_id=OPERATION_ID,
                journal_digest=JOURNAL_DIGEST,
                request_digest=OTHER_REQUEST_DIGEST,
                phase="sealing",
                action="apply",
            )
        assert admission.snapshot().active_mutations == 1
    finally:
        second_scope.__exit__(None, None, None)
        first_scope.__exit__(None, None, None)
    assert admission.snapshot().active_mutations == 0


def test_lost_control_conversion_ack_adopts_the_durable_control(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import consolidation_admission

    admission = _open_admission(tmp_path)
    publish = admission._publish_participant  # noqa: SLF001

    def publish_then_lose_ack(participant, **kwargs) -> None:
        publish(participant, **kwargs)
        if participant.kind == "control":
            raise consolidation_admission.ConsolidationAdmissionUnavailable(
                "CONSOLIDATION_ADMISSION_UNAVAILABLE"
            )

    monkeypatch.setattr(admission, "_publish_participant", publish_then_lose_ack)
    with admission.admit_mutation() as mutation:
        control = admission.convert_control_mutation(
            mutation,
            authority=_apply_authority(),
            run_id=RUN_ID,
            operation_id=OPERATION_ID,
            journal_digest=JOURNAL_DIGEST,
            request_digest=REQUEST_DIGEST,
            phase="sealing",
            action="apply",
        )
        assert control is not None

    persisted = admission._matching_control(  # noqa: SLF001
        run_id=RUN_ID,
        operation_id=OPERATION_ID,
        journal_digest=JOURNAL_DIGEST,
        request_digest=REQUEST_DIGEST,
        phase="sealing",
        action="apply",
    )
    assert persisted.kind == "control"
    with _assert_admission_error("CONSOLIDATION_CONTROL_PENDING"):
        with admission.admit_read():
            pass


def test_lost_seal_terminal_ack_replays_the_exact_durable_terminal(
    tmp_path: Path,
) -> None:
    from exomem.governance import consolidation_admission

    admission = _open_admission(tmp_path)
    sealed = _seal_with_new_control(admission)
    restarted = consolidation_admission.ConsolidationAdmission(
        tmp_path,
        vault_binding_digest=VAULT_BINDING,
    )

    replayed = _seal_with_recovered_control(restarted)

    assert replayed == sealed


@pytest.mark.parametrize(
    ("sealed_at", "completed_at", "expected_revision"),
    ((T0, T2, 0), (T1, T0, 0), (T1, T2, 1)),
)
def test_seal_terminal_replay_rejects_changed_request_facts(
    tmp_path: Path,
    sealed_at: str,
    completed_at: str,
    expected_revision: int,
) -> None:
    from exomem.governance import consolidation_admission

    admission = _open_admission(tmp_path)
    _seal_with_new_control(admission)
    restarted = consolidation_admission.ConsolidationAdmission(
        tmp_path,
        vault_binding_digest=VAULT_BINDING,
    )

    with restarted.resume_control_mutation(
        authority=_apply_authority(),
        run_id=RUN_ID,
        operation_id=OPERATION_ID,
        journal_digest=JOURNAL_DIGEST,
        request_digest=REQUEST_DIGEST,
        phase="sealing",
        action="apply",
    ) as control:
        with _assert_admission_error("CONSOLIDATION_SEAL_UNAVAILABLE"):
            restarted.seal_and_drain(
                control=control,
                authority=_apply_authority(),
                run_id=RUN_ID,
                operation_id=OPERATION_ID,
                journal_digest=JOURNAL_DIGEST,
                sealed_at=sealed_at,
                completed_at=completed_at,
                expected_revision=expected_revision,
                timeout=1.0,
            )


@pytest.mark.parametrize("expected_revision", (False, 0.0))
def test_seal_terminal_replay_rejects_non_integer_revision(
    tmp_path: Path,
    expected_revision: object,
) -> None:
    from exomem.governance import consolidation_admission

    admission = _open_admission(tmp_path)
    _seal_with_new_control(admission)
    restarted = consolidation_admission.ConsolidationAdmission(
        tmp_path,
        vault_binding_digest=VAULT_BINDING,
    )

    with restarted.resume_control_mutation(
        authority=_apply_authority(),
        run_id=RUN_ID,
        operation_id=OPERATION_ID,
        journal_digest=JOURNAL_DIGEST,
        request_digest=REQUEST_DIGEST,
        phase="sealing",
        action="apply",
    ) as control:
        with _assert_admission_error("CONSOLIDATION_SEAL_UNAVAILABLE"):
            restarted.seal_and_drain(
                control=control,
                authority=_apply_authority(),
                run_id=RUN_ID,
                operation_id=OPERATION_ID,
                journal_digest=JOURNAL_DIGEST,
                sealed_at=T1,
                completed_at=T2,
                expected_revision=expected_revision,  # type: ignore[arg-type]
                timeout=1.0,
            )


def test_control_record_must_survive_until_seal_terminal(tmp_path: Path) -> None:
    from exomem.governance import consolidation_seal

    admission = _open_admission(tmp_path)
    ordinary, control = _convert_apply(admission)

    def remove_control() -> None:
        participant = next(
            item
            for item in admission._load_participants()
            if item.kind == "control"  # noqa: SLF001
        )
        admission._remove_participant(participant.participant_id)  # noqa: SLF001

    try:
        with _assert_admission_error("CONSOLIDATION_CONTROL_UNAVAILABLE"):
            admission.seal_and_drain(
                control=control,
                authority=_apply_authority(),
                run_id=RUN_ID,
                operation_id=OPERATION_ID,
                journal_digest=JOURNAL_DIGEST,
                sealed_at=T1,
                completed_at=T2,
                expected_revision=0,
                timeout=1.0,
                stoppers=(remove_control,),
            )
    finally:
        ordinary.__exit__(None, None, None)
    durable = consolidation_seal.ConsolidationSealStore(tmp_path).load(
        vault_binding_digest=VAULT_BINDING
    )
    assert durable.phase == "sealing"


def test_receipt_first_seal_can_persist_intent_then_drain_as_two_exact_effects(
    tmp_path: Path,
) -> None:
    admission = _open_admission(tmp_path)
    ordinary, control = _convert_apply(admission)
    authority = _apply_authority()
    try:
        sealing = admission.begin_seal(
            control=control,
            authority=authority,
            run_id=RUN_ID,
            operation_id=OPERATION_ID,
            journal_digest=JOURNAL_DIGEST,
            sealed_at=T1,
            expected_revision=0,
        )
        assert sealing.state.phase == "sealing"
        assert sealing.state.revision == 1
        assert admission.begin_seal(
            control=control,
            authority=authority,
            run_id=RUN_ID,
            operation_id=OPERATION_ID,
            journal_digest=JOURNAL_DIGEST,
            sealed_at=T1,
            expected_revision=0,
        ) == sealing

        with _assert_admission_error("CONSOLIDATION_SEALED"):
            with admission.admit_read():
                pass

        sealed = admission.drain_and_seal(
            control=control,
            authority=authority,
            run_id=RUN_ID,
            operation_id=OPERATION_ID,
            journal_digest=JOURNAL_DIGEST,
            sealed_at=T1,
            completed_at=T2,
            expected_revision=1,
            timeout=1.0,
        )
        assert sealed.state.phase == "sealed"
        assert sealed.state.revision == 2
        assert admission.drain_and_seal(
            control=control,
            authority=authority,
            run_id=RUN_ID,
            operation_id=OPERATION_ID,
            journal_digest=JOURNAL_DIGEST,
            sealed_at=T1,
            completed_at=T2,
            expected_revision=1,
            timeout=1.0,
        ) == sealed
    finally:
        ordinary.__exit__(None, None, None)


def test_split_seal_rejects_changed_identity_without_advancing(
    tmp_path: Path,
) -> None:
    admission = _open_admission(tmp_path)
    ordinary, control = _convert_apply(admission)
    try:
        sealing = admission.begin_seal(
            control=control,
            authority=_apply_authority(),
            run_id=RUN_ID,
            operation_id=OPERATION_ID,
            journal_digest=JOURNAL_DIGEST,
            sealed_at=T1,
            expected_revision=0,
        )
        with _assert_admission_error("CONSOLIDATION_SEAL_UNAVAILABLE"):
            admission.drain_and_seal(
                control=control,
                authority=_apply_authority(),
                run_id=RUN_ID,
                operation_id=OPERATION_ID,
                journal_digest=JOURNAL_DIGEST,
                sealed_at=T1,
                completed_at=T2,
                expected_revision=0,
                timeout=1.0,
            )
        assert admission.reload().state == sealing.state
    finally:
        ordinary.__exit__(None, None, None)
