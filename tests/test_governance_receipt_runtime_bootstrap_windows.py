"""Native Windows red coverage for receipt-first writer-runtime bootstrap."""

from __future__ import annotations

import multiprocessing
import os
from pathlib import Path

import pytest

from exomem import mutation_lock
from exomem.governance import receipts
from exomem.mutation_lock import VaultMutationCoordinator
from exomem.writer_lease import IdempotencyStore


pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows receipt runtime bootstrap")


def _assert_private_directory_dacl(path: Path) -> None:
    sid = mutation_lock._windows_current_user_sid()
    assert mutation_lock._windows_private_dacl_is_valid(
        mutation_lock._windows_dacl_sddl(path), sid, directory=True
    )


def _record_root_dacl_before_lock_artifact(
    monkeypatch: pytest.MonkeyPatch, state_root: Path
) -> list[Path]:
    applied: list[Path] = []
    original_apply = mutation_lock._windows_apply_private_dacl

    def apply(path: Path, sid: str) -> None:
        if path == state_root:
            assert list(state_root.iterdir()) == []
        original_apply(path, sid)
        applied.append(path)

    monkeypatch.setattr(mutation_lock, "_windows_apply_private_dacl", apply)
    return applied


def _first_use_process(
    vault_root: str, state_root: str, mode: str, start, results  # noqa: ANN001
) -> None:
    os.environ["EXOMEM_WRITER_LEASE_STATE_DIR"] = state_root
    from exomem.governance import receipts as child_receipts

    root = Path(state_root)
    lock_root = root / "mutation-locks"
    original_apply = mutation_lock._windows_apply_private_dacl
    original_validate = mutation_lock._validate_windows_runtime_entry
    original_mkdir = Path.mkdir
    original_open = Path.open
    root_dacl_applies = 0
    root_validations = 0
    root_validated = False

    def is_own_lock_artifact(path: Path) -> bool:
        try:
            path.relative_to(lock_root)
        except ValueError:
            return False
        return True

    def apply(path: Path, sid: str) -> None:
        nonlocal root_dacl_applies
        if path == root and list(root.iterdir()):
            raise AssertionError("runtime root gained a lock artifact before DACL validation")
        if path == root:
            root_dacl_applies += 1
        original_apply(path, sid)

    def validate(
        path: Path, *, directory: bool, sid: str, handle: int | None = None
    ) -> None:
        nonlocal root_validated, root_validations
        if path == root and directory:
            original_validate(path, directory=directory, sid=sid, handle=handle)
            root_validations += 1
            root_validated = True
            return
        original_validate(path, directory=directory, sid=sid, handle=handle)

    def mkdir(path: Path, *args, **kwargs):  # noqa: ANN001
        if is_own_lock_artifact(path):
            assert root_validated, "own mutation-lock directory opened before root validation"
        return original_mkdir(path, *args, **kwargs)

    def open(path: Path, *args, **kwargs):  # noqa: ANN001
        if is_own_lock_artifact(path):
            assert root_validated, "own mutation-lock file opened before root validation"
        return original_open(path, *args, **kwargs)

    mutation_lock._windows_apply_private_dacl = apply
    mutation_lock._validate_windows_runtime_entry = validate
    Path.mkdir = mkdir
    Path.open = open
    try:
        start.wait(timeout=15.0)
        if mode == "receipt":
            record = child_receipts.append_event(
                Path(vault_root), event_type="disclosure", payload={"outcomes": []}
            )
            detail: object = record["seq"]
        else:
            with VaultMutationCoordinator(root, Path(vault_root)).hold():
                pass
            detail = mode
    except Exception as exc:  # noqa: BLE001 - report exact child outcome to the parent
        results.put(
            {
                "status": "error",
                "detail": f"{type(exc).__name__}: {exc}",
                "root_dacl_applies": root_dacl_applies,
                "root_validations": root_validations,
            }
        )
    else:
        results.put(
            {
                "status": "ok",
                "detail": detail,
                "root_dacl_applies": root_dacl_applies,
                "root_validations": root_validations,
            }
        )


def _join_processes(processes: list[multiprocessing.Process]) -> None:
    for process in processes:
        process.join(timeout=20.0)
    for process in processes:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5.0)


def test_windows_receipt_first_secures_absent_runtime_before_lock_and_idempotency(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Receipt-first startup must install the exact root DACL before its lock child."""
    state_root = tmp_path / "receipt-first-state"
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(state_root))
    applied = _record_root_dacl_before_lock_artifact(monkeypatch, state_root)

    receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})

    assert applied
    assert applied[0] == state_root
    _assert_private_directory_dacl(state_root)
    IdempotencyStore(state_root / "idempotency.sqlite")


def test_windows_coordinator_first_secures_absent_runtime_before_idempotency(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The coordinator-first path owns the same root-security bootstrap."""
    state_root = tmp_path / "coordinator-first-state"
    applied = _record_root_dacl_before_lock_artifact(monkeypatch, state_root)
    coordinator = VaultMutationCoordinator(state_root, vault)

    assert coordinator.snapshot()["state"] == "free"
    with coordinator.hold():
        pass

    assert applied
    assert applied[0] == state_root
    _assert_private_directory_dacl(state_root)
    IdempotencyStore(state_root / "idempotency.sqlite")


def test_windows_receipt_first_refuses_existing_unsafe_runtime_without_lock_child(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An inherited legacy root remains untouched and actionable at receipt entry."""
    state_root = tmp_path / "unsafe-existing-state"
    state_root.mkdir()
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(state_root))
    before_dacl = mutation_lock._windows_dacl_sddl(state_root)
    assert not mutation_lock._windows_private_dacl_is_valid(
        before_dacl, mutation_lock._windows_current_user_sid(), directory=True
    )

    sid = mutation_lock._windows_current_user_sid()
    expected_remediation = mutation_lock._windows_private_dacl_repair_command(
        state_root, sid, directory=True
    )
    expected_error = (
        f"unsafe Windows DACL at {state_root}; run in elevated PowerShell: "
        f"{expected_remediation}"
    )
    with pytest.raises(receipts.ReceiptError) as exc_info:
        receipts.append_event(vault, event_type="disclosure", payload={"outcomes": []})

    assert str(exc_info.value) == expected_error
    assert mutation_lock._windows_dacl_sddl(state_root) == before_dacl
    assert list(state_root.iterdir()) == []


def _run_concurrent_first_use(
    vault: Path, state_root: Path, modes: tuple[str, ...]
) -> list[dict[str, object]]:
    context = multiprocessing.get_context("spawn")
    start = context.Barrier(len(modes))
    results = context.Queue()
    processes = [
        context.Process(
            target=_first_use_process,
            args=(str(vault), str(state_root), mode, start, results),
        )
        for mode in modes
    ]
    for process in processes:
        process.start()
    try:
        outcomes = [results.get(timeout=30.0) for _ in processes]
    finally:
        _join_processes(processes)
    assert [process.exitcode for process in processes] == [0] * len(processes)
    assert all(outcome["status"] == "ok" for outcome in outcomes), outcomes
    assert sum(int(outcome["root_dacl_applies"]) for outcome in outcomes) == 1
    assert all(int(outcome["root_validations"]) >= 1 for outcome in outcomes)
    return outcomes


def test_windows_concurrent_receipt_first_bootstrap_converges_on_one_private_root(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Eight simultaneous first receipts must all serialize through one valid root."""
    state_root = tmp_path / "concurrent-receipt-first-state"
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(state_root))
    outcomes = _run_concurrent_first_use(vault, state_root, ("receipt",) * 8)

    _assert_private_directory_dacl(state_root)


def test_windows_mixed_concurrent_first_use_converges_on_one_private_root(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Receipt and coordinator first users share one same-principal bootstrap race."""
    state_root = tmp_path / "mixed-concurrent-first-state"
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(state_root))
    outcomes = _run_concurrent_first_use(
        vault, state_root, ("receipt", "coordinator") * 4
    )

    _assert_private_directory_dacl(state_root)
