from __future__ import annotations

import inspect
import json
import logging
import multiprocessing
import os
import stat
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import mutation_lock as mutation_lock_module
from exomem.cli_ops import OpError
from exomem.mutation_lock import (
    VaultMutationCoordinator,
    active_mutation_snapshot,
    last_mutation_timing,
)


def _boundary(snapshot: dict) -> dict:
    """Drop the additive contention block so these assertions pin the flag keys.

    Contention attribution has its own coverage in
    `tests/test_readiness_honesty.py`; the assertions below exist to pin the
    boundary state keys, which the stats block must never disturb.
    """
    return {key: value for key, value in snapshot.items() if key != "contention"}


def test_retained_regular_file_rename_moves_the_pinned_entry(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    destination = tmp_path / "quarantine" / "source.sqlite"
    source.write_bytes(b"graph")
    destination.parent.mkdir()

    retained = mutation_lock_module.retain_regular_file(source)
    try:
        if os.name != "nt":
            assert retained.identity == mutation_lock_module.nofollow_regular_file_identity(
                source
            )
        mutation_lock_module.rename_retained_regular_file(retained, destination)
    finally:
        retained.close()

    assert not source.exists()
    assert destination.read_bytes() == b"graph"


def test_windows_retained_rename_uses_file_rename_info_filename_offset() -> None:
    source = inspect.getsource(mutation_lock_module._windows_rename_handle)

    assert "filename_offset = _RenameInfo.filename.offset" in source
    assert "ctypes.sizeof(_RenameInfo) + len(encoded)" in source


def test_windows_retained_child_and_cleanup_use_exact_native_handles() -> None:
    create = inspect.getsource(mutation_lock_module._windows_create_child_directory_handle)
    publish = inspect.getsource(mutation_lock_module.retained_write_file)
    cleanup = inspect.getsource(mutation_lock_module.retained_unlink_file)

    assert "NtCreateFile" in create
    assert "parent.windows_handle" in create
    assert "_windows_rename_handle(source_handle" in publish
    assert "_windows_delete_handle(msvcrt.get_osfhandle(held.fd))" in cleanup


def test_windows_nt_child_creation_requests_synchronize_access() -> None:
    source = inspect.getsource(mutation_lock_module._windows_create_child_directory_handle)

    assert "0x00130080" in source  # SYNCHRONIZE | DELETE | READ_CONTROL | FILE_READ_ATTRIBUTES


def _synthetic_windows_path(*parts: str) -> Path:
    return Path("\\".join(("C:", "example", *parts)))


def test_windows_path_inspection_access_has_dacl_read_right_without_mutation_rights() -> None:
    signature = inspect.signature(mutation_lock_module._windows_open_path)
    access = signature.parameters["access"].default
    share = signature.parameters["share"].default

    assert isinstance(access, int)
    assert access & 0x00000080  # FILE_READ_ATTRIBUTES
    assert access & 0x00020000  # READ_CONTROL, required by GetSecurityInfo(DACL)
    assert not access & 0x40000000  # GENERIC_WRITE
    assert not access & 0x00010000  # DELETE
    assert isinstance(share, int)
    assert not share & 0x4  # FILE_SHARE_DELETE


def test_windows_retained_directory_ancestors_keep_metadata_only_access() -> None:
    """Retaining an ancestor must not request write or delete rights."""
    opened: list[tuple[Path, dict[str, object]]] = []

    def open_path(path: Path, **kwargs: object) -> int:
        opened.append((path, kwargs))
        return len(opened)

    directory = mutation_lock_module._acquire_windows_secure_directory(
        _synthetic_windows_path("vault", "Knowledge Base", "_Governance", "events"),
        create=False,
        mode=0o700,
        open_path=open_path,
        close_handle=lambda _handle: None,
    )
    try:
        metadata_access = inspect.signature(mutation_lock_module._windows_open_path).parameters["access"].default
        assert all(kwargs == {"directory": True} for _path, kwargs in opened)
        assert metadata_access == 0x00020080  # READ_CONTROL | FILE_READ_ATTRIBUTES
        assert not metadata_access & 0x40000000  # GENERIC_WRITE
        assert not metadata_access & 0x00010000  # DELETE
    finally:
        directory.close()


@pytest.mark.skipif(os.name != "nt", reason="exercises the native Windows secure-directory branch")
def test_windows_secure_directory_refuses_ordinary_replacement_after_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A created component must not be replaceable before its retained handle opens."""
    target = tmp_path / "created"
    replaced = False

    def replace_created(path: Path) -> None:
        nonlocal replaced
        if path == target:
            path.rmdir()
            path.mkdir()
            replaced = True

    monkeypatch.setattr(mutation_lock_module, "_after_windows_secure_directory_create", replace_created)

    with pytest.raises(OSError, match="changed during creation"):
        mutation_lock_module._acquire_windows_secure_directory(target, create=True, mode=0o700)

    assert replaced is True


@pytest.mark.skipif(os.name != "nt", reason="exercises the native Windows secure-directory branch")
def test_windows_private_root_refuses_replacement_before_dacl_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the retained entry created by this process may receive the DACL."""
    state_root = tmp_path / "state"
    replaced = False
    applied: list[Path] = []
    original_apply = mutation_lock_module._windows_apply_private_dacl

    def replace_created(path: Path) -> None:
        nonlocal replaced
        if path == state_root:
            path.rmdir()
            path.mkdir()
            replaced = True

    def apply(path: Path, sid: str) -> None:
        applied.append(path)
        original_apply(path, sid)

    monkeypatch.setattr(
        mutation_lock_module, "_after_windows_secure_directory_create", replace_created
    )
    monkeypatch.setattr(mutation_lock_module, "_windows_apply_private_dacl", apply)

    with pytest.raises(OSError, match="changed during creation"):
        mutation_lock_module.prepare_windows_private_state_root(state_root)

    assert replaced is True
    assert applied == []


@pytest.mark.skipif(os.name != "nt", reason="exercises the native Windows secure-child branch")
@pytest.mark.parametrize(
    ("flags", "expected_creation"),
    [
        (os.O_RDONLY, 3),  # OPEN_EXISTING
        (os.O_WRONLY, 3),  # OPEN_EXISTING
        (os.O_RDWR, 3),  # OPEN_EXISTING
        (os.O_WRONLY | os.O_CREAT, 4),  # OPEN_ALWAYS
        (os.O_WRONLY | os.O_CREAT | os.O_EXCL, 1),  # CREATE_NEW
    ],
)
def test_windows_secure_child_preserves_open_disposition_and_exclusive_create(
    monkeypatch: pytest.MonkeyPatch, flags: int, expected_creation: int
) -> None:
    """The Windows native open must retain POSIX exclusive-create semantics."""
    calls: list[dict[str, object]] = []

    def open_path(path: Path, **kwargs: object) -> int:
        calls.append({"path": path, **kwargs})
        return 73

    directory = mutation_lock_module._SecureDirectory(
        _synthetic_windows_path("vault", "state"),
        windows_handles=[71],
        close_windows_handle=lambda _handle: None,
    )
    monkeypatch.setattr(mutation_lock_module, "_windows_open_path", open_path)
    monkeypatch.setattr(mutation_lock_module, "_windows_child_is_in_directory", lambda *_args: True)
    monkeypatch.setattr(
        mutation_lock_module,
        "msvcrt",
        SimpleNamespace(open_osfhandle=lambda _handle, _flags: 79),
    )
    monkeypatch.setattr(
        mutation_lock_module.os,
        "fstat",
        lambda _fd: SimpleNamespace(st_mode=stat.S_IFREG),
    )
    monkeypatch.setattr(mutation_lock_module.os, "close", lambda _fd: None)

    assert mutation_lock_module._open_secure_file_at(directory, "receipt.jsonl", flags) == 79
    assert calls == [
        {
            "path": directory.path / "receipt.jsonl",
            "directory": False,
            "access": 0x40000000 if flags & os.O_WRONLY else (0xC0000000 if flags & os.O_RDWR else 0x80000000),
            "share": 0x3,
            "creation": expected_creation,
        }
    ]


@pytest.mark.skipif(os.name != "nt", reason="exercises the native Windows secure-child branch")
def test_windows_secure_child_closes_descriptor_when_post_open_fstat_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CRT descriptor owns the raw handle after conversion, even on inspection failure."""
    closed: list[int] = []
    directory = mutation_lock_module._SecureDirectory(
        _synthetic_windows_path("vault", "state"),
        windows_handles=[71],
        close_windows_handle=lambda _handle: None,
    )
    monkeypatch.setattr(mutation_lock_module, "_windows_open_path", lambda *_args, **_kwargs: 73)
    monkeypatch.setattr(mutation_lock_module, "_windows_child_is_in_directory", lambda *_args: True)
    monkeypatch.setattr(
        mutation_lock_module,
        "msvcrt",
        SimpleNamespace(open_osfhandle=lambda _handle, _flags: 79),
    )
    monkeypatch.setattr(
        mutation_lock_module.os,
        "fstat",
        lambda _fd: (_ for _ in ()).throw(OSError("injected fstat failure")),
    )
    monkeypatch.setattr(mutation_lock_module.os, "close", closed.append)

    with pytest.raises(OSError, match="injected fstat failure"):
        mutation_lock_module._open_secure_file_at(directory, "receipt.jsonl", os.O_RDONLY)

    assert closed == [79]


def test_windows_private_dacl_deduplicates_local_system_principal() -> None:
    sddl = mutation_lock_module._windows_private_dacl_sddl("S-1-5-18")

    assert sddl == "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
    assert mutation_lock_module._windows_private_dacl_is_valid(
        sddl, "S-1-5-18", directory=True
    )


#: The DACL Windows actually writes for an entry created beneath a user's
#: temporary directory: protected, three full-access ACEs, no broad trustee --
#: and the user's own grant spelled `OW` (OWNER RIGHTS) rather than as a literal
#: SID. Refusing it failed closed on a directory that was already private.
_OWNER_RIGHTS_DACL = "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;OW)"
_USER_SID = "S-1-5-21-896069015-2321608379-4234408933-1001"


def test_owner_rights_counts_as_the_users_grant_when_the_user_owns_the_entry() -> None:
    """`OW` is a spelling of the owner, so ownership decides what it admits."""
    assert mutation_lock_module._windows_private_dacl_is_valid(
        f"O:{_USER_SID}{_OWNER_RIGHTS_DACL}", _USER_SID, directory=True
    )


def test_owner_rights_is_rejected_when_somebody_else_owns_the_entry() -> None:
    """The concession is ownership-scoped or it is a hole.

    Accepting `OW` unconditionally would admit full access for whoever happens
    to own the entry -- the one case this validator exists to catch.
    """
    stranger = "S-1-5-21-9999-8888-7777-1001"
    assert not mutation_lock_module._windows_private_dacl_is_valid(
        f"O:{stranger}{_OWNER_RIGHTS_DACL}", _USER_SID, directory=True
    )


def test_owner_rights_is_rejected_when_the_descriptor_carries_no_owner() -> None:
    """No owner, no way to resolve `OW` -- so fail closed rather than assume."""
    assert not mutation_lock_module._windows_private_dacl_is_valid(
        _OWNER_RIGHTS_DACL, _USER_SID, directory=True
    )


def test_owner_rights_does_not_let_a_principal_be_granted_twice() -> None:
    """`OW` beside a literal grant to the same owner is one principal, named twice.

    Resolving `OW` before the duplicate check is what keeps that rule meaningful;
    checking the raw trustee would see two distinct strings and wave it through.
    """
    doubled = (
        f"O:{_USER_SID}D:P(A;OICI;FA;;;{_USER_SID})"
        "(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;OW)"
    )
    assert not mutation_lock_module._windows_private_dacl_is_valid(
        doubled, _USER_SID, directory=True
    )


def test_owner_rights_does_not_excuse_a_broad_trustee() -> None:
    """Everything else the validator rejected, it still rejects."""
    with_everyone = f"O:{_USER_SID}{_OWNER_RIGHTS_DACL}(A;OICI;FA;;;WD)"
    assert not mutation_lock_module._windows_private_dacl_is_valid(
        with_everyone, _USER_SID, directory=True
    )
    unprotected = f"O:{_USER_SID}D:(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;OW)"
    assert not mutation_lock_module._windows_private_dacl_is_valid(
        unprotected, _USER_SID, directory=True
    )


def test_an_owner_bearing_descriptor_still_accepts_the_dacl_this_module_writes() -> None:
    """The literal-SID form is the one we write; prefixing an owner cannot break it."""
    sddl = mutation_lock_module._windows_private_dacl_sddl(_USER_SID)

    assert mutation_lock_module._windows_private_dacl_is_valid(
        f"O:{_USER_SID}{sddl}", _USER_SID, directory=True
    )
    assert mutation_lock_module._windows_private_dacl_is_valid(
        sddl, _USER_SID, directory=True
    )


def test_sddl_owner_is_read_for_both_raw_sids_and_two_letter_aliases() -> None:
    """Windows picks the spelling, so both have to parse back to the same answer."""
    owner = mutation_lock_module._windows_sddl_owner
    assert owner(f"O:{_USER_SID}{_OWNER_RIGHTS_DACL}") == _USER_SID
    assert owner(f"O:BA{_OWNER_RIGHTS_DACL}") == "BA"
    assert owner(_OWNER_RIGHTS_DACL) is None


def _process_hold(
    state_root: str,
    vault_root: str,
    attempting,
    entered,
    release,
    request_id: str | None = None,
    operation: str | None = None,
    holder_kind: str = "unknown",
) -> None:
    coordinator = VaultMutationCoordinator(Path(state_root), Path(vault_root))
    attempting.set()
    with coordinator.hold(
        timeout_seconds=3.0,
        request_id=request_id,
        operation=operation,
        holder_kind=holder_kind,
    ):
        entered.set()
        if not release.wait(5.0):
            raise RuntimeError("test release signal was not received")


def _process_hold_paused_before_publish(
    state_root: str,
    vault_root: str,
    acquired,
    publish,
    entered,
    release,
) -> None:
    original = VaultMutationCoordinator._publish_holder_metadata

    def paused_publish(self, holder):  # noqa: ANN001
        acquired.set()
        if not publish.wait(5.0):
            raise RuntimeError("test publish signal was not received")
        return original(self, holder)

    VaultMutationCoordinator._publish_holder_metadata = paused_publish
    coordinator = VaultMutationCoordinator(Path(state_root), Path(vault_root))
    with coordinator.hold(
        timeout_seconds=3.0,
        request_id="req-new-generation",
        operation="replace_memory",
        holder_kind="command",
    ):
        entered.set()
        if not release.wait(5.0):
            raise RuntimeError("test release signal was not received")


def _process_crash(state_root: str, vault_root: str, entered) -> None:
    coordinator = VaultMutationCoordinator(Path(state_root), Path(vault_root))
    guard = coordinator.hold(timeout_seconds=3.0)
    guard.__enter__()
    entered.set()
    time.sleep(0.05)
    os._exit(23)


def _join_or_terminate(processes: list[multiprocessing.Process]) -> None:
    for process in processes:
        process.join(timeout=5.0)
    for process in processes:
        if process.is_alive():
            process.terminate()
            process.join(timeout=2.0)


def test_same_canonical_vault_serializes_competing_threads(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    vault = tmp_path / "vault"
    vault.mkdir()
    alias = vault / ".." / "vault"
    first = VaultMutationCoordinator(state_root, vault)
    second = VaultMutationCoordinator(state_root, alias)
    first_entered = threading.Event()
    second_attempting = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()

    def hold_first() -> None:
        with first.hold(timeout_seconds=2.0):
            first_entered.set()
            assert release_first.wait(2.0)

    def enter_second() -> None:
        second_attempting.set()
        with second.hold(timeout_seconds=2.0):
            second_entered.set()

    first_thread = threading.Thread(target=hold_first)
    second_thread = threading.Thread(target=enter_second)
    first_thread.start()
    assert first_entered.wait(1.0)
    second_thread.start()
    assert second_attempting.wait(1.0)
    assert not second_entered.wait(0.1)
    release_first.set()
    assert second_entered.wait(1.0)
    first_thread.join(timeout=2.0)
    second_thread.join(timeout=2.0)
    assert not first_thread.is_alive()
    assert not second_thread.is_alive()


def test_same_canonical_vault_serializes_competing_processes(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    state_root = tmp_path / "state"
    vault = tmp_path / "vault"
    vault.mkdir()
    first_attempting = context.Event()
    first_entered = context.Event()
    release_first = context.Event()
    second_attempting = context.Event()
    second_entered = context.Event()
    release_second = context.Event()
    first = context.Process(
        target=_process_hold,
        args=(
            str(state_root),
            str(vault),
            first_attempting,
            first_entered,
            release_first,
        ),
    )
    second = context.Process(
        target=_process_hold,
        args=(
            str(state_root),
            str(vault / ".." / "vault"),
            second_attempting,
            second_entered,
            release_second,
        ),
    )
    processes = [first, second]
    try:
        first.start()
        assert first_attempting.wait(2.0)
        assert first_entered.wait(2.0)
        second.start()
        assert second_attempting.wait(2.0)
        assert not second_entered.wait(0.2)
        release_first.set()
        assert second_entered.wait(2.0)
        release_second.set()
    finally:
        release_first.set()
        release_second.set()
        _join_or_terminate(processes)
    assert first.exitcode == 0
    assert second.exitcode == 0


def test_independent_vaults_can_mutate_concurrently(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    vault_a = tmp_path / "vault-a"
    vault_b = tmp_path / "vault-b"
    vault_a.mkdir()
    vault_b.mkdir()
    first = VaultMutationCoordinator(state_root, vault_a)
    second = VaultMutationCoordinator(state_root, vault_b)
    first_entered = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()

    def hold_first() -> None:
        with first.hold(timeout_seconds=2.0):
            first_entered.set()
            assert release_first.wait(2.0)

    first_thread = threading.Thread(target=hold_first)
    first_thread.start()
    assert first_entered.wait(1.0)
    with second.hold(timeout_seconds=0.2):
        second_entered.set()
    assert second_entered.is_set()
    release_first.set()
    first_thread.join(timeout=2.0)
    assert not first_thread.is_alive()


def test_nested_acquisition_is_reentrant_across_coordinator_instances(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    first = VaultMutationCoordinator(tmp_path / "state", vault)
    second = VaultMutationCoordinator(tmp_path / "state", vault / ".")

    with first.hold(timeout_seconds=0.2):
        with second.hold(timeout_seconds=0.0):
            assert first.lock_path == second.lock_path


def test_bounded_timeout_raises_actionable_mutation_busy(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    vault = tmp_path / "vault"
    vault.mkdir()
    holder = VaultMutationCoordinator(state_root, vault)
    contender = VaultMutationCoordinator(state_root, vault)
    entered = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with holder.hold(timeout_seconds=2.0):
            entered.set()
            assert release.wait(2.0)

    thread = threading.Thread(target=hold_lock)
    thread.start()
    assert entered.wait(1.0)
    started = time.monotonic()
    try:
        with pytest.raises(OpError) as raised:
            with contender.hold(timeout_seconds=0.05):
                pytest.fail("contender entered a held mutation boundary")
        assert raised.value.code == "MUTATION_BUSY"
        assert raised.value.remediation
        assert "retry" in raised.value.remediation.lower()
        assert time.monotonic() - started < 0.5
    finally:
        release.set()
        thread.join(timeout=2.0)
    assert not thread.is_alive()


def test_process_contention_uses_same_bounded_timeout_contract(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    state_root = tmp_path / "state"
    vault = tmp_path / "vault"
    vault.mkdir()
    attempting = context.Event()
    entered = context.Event()
    release = context.Event()
    holder = context.Process(
        target=_process_hold,
        args=(str(state_root), str(vault), attempting, entered, release),
    )
    holder.start()
    try:
        assert attempting.wait(2.0)
        assert entered.wait(2.0)
        contender = VaultMutationCoordinator(state_root, vault)
        with pytest.raises(OpError) as raised:
            with contender.hold(timeout_seconds=0.05):
                pytest.fail("contender entered a process-held mutation boundary")
        assert raised.value.code == "MUTATION_BUSY"
        assert raised.value.remediation
    finally:
        release.set()
        _join_or_terminate([holder])
    assert holder.exitcode == 0


def test_cross_process_status_and_busy_error_report_verified_current_holder(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    state_root = tmp_path / "state"
    vault = tmp_path / "vault"
    vault.mkdir()
    attempting = context.Event()
    entered = context.Event()
    release = context.Event()
    holder = context.Process(
        target=_process_hold,
        args=(
            str(state_root),
            str(vault),
            attempting,
            entered,
            release,
            "req-external",
            "edit_memory",
            "command",
        ),
    )
    holder.start()
    try:
        assert attempting.wait(2.0)
        assert entered.wait(2.0)
        contender = VaultMutationCoordinator(state_root, vault)
        snapshot = contender.snapshot()
        assert _boundary(snapshot) | {"age_seconds": 0.0} == {
            "state": "held",
            "request_id": "req-external",
            "operation": "edit_memory",
            "holder_kind": "command",
            "age_seconds": 0.0,
            "overdue": False,
            "verified": True,
        }
        with pytest.raises(OpError) as raised:
            with contender.hold(timeout_seconds=0.05):
                pytest.fail("contender entered a process-held mutation boundary")
        busy_holder = raised.value.details["holder"]
        assert busy_holder["verified"] is True
        assert busy_holder["request_id"] == "req-external"
        assert busy_holder["operation"] == "edit_memory"
        assert busy_holder["holder_kind"] == "command"
    finally:
        release.set()
        _join_or_terminate([holder])
    assert holder.exitcode == 0


@pytest.mark.parametrize("payload", ["not json", '{"schema": 1}'])
def test_stale_or_malformed_metadata_cannot_report_a_verified_holder(
    tmp_path: Path, payload: str
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    state_root = tmp_path / "state"
    mutation_lock_module.prepare_windows_private_state_root(state_root)
    coordinator = VaultMutationCoordinator(state_root, vault)
    coordinator.metadata_path.parent.mkdir(parents=True, exist_ok=True)
    coordinator.metadata_path.write_text(payload, encoding="utf-8")

    assert _boundary(coordinator.snapshot()) == {"state": "free"}
    assert not coordinator.metadata_path.exists()


@pytest.mark.parametrize("payload", [None, "not json", '{"schema": 1}'])
def test_external_holder_without_valid_metadata_is_explicitly_unverified(
    tmp_path: Path, payload: str | None
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    coordinator = VaultMutationCoordinator(tmp_path / "state", vault)
    handle = coordinator._open_lock_file(coordinator.lock_path)
    assert mutation_lock_module._try_os_lock(handle)
    try:
        if payload is not None:
            coordinator.metadata_path.write_text(payload, encoding="utf-8")
        snapshot = coordinator.snapshot()
        assert _boundary(snapshot) == {
            "state": "held",
            "request_id": "untracked",
            "operation": "unknown",
            "holder_kind": "external",
            "age_seconds": 0.0,
            "overdue": False,
            "verified": False,
        }
    finally:
        mutation_lock_module._release_os_lock(handle)
        handle.close()


def test_holder_sidecar_is_bounded_content_free_runtime_metadata(tmp_path: Path) -> None:
    vault = tmp_path / "private-vault-name"
    vault.mkdir()
    coordinator = VaultMutationCoordinator(tmp_path / "state", vault)

    with coordinator.hold(
        request_id="req-content-free",
        operation="remember",
        holder_kind="command",
    ):
        holder = json.loads(coordinator.metadata_path.read_text(encoding="utf-8"))
        assert set(holder) == {
            "schema",
            "generation",
            "request_id",
            "operation",
            "holder_kind",
            "acquired_at",
            "long_holder_seconds",
        }
        rendered = repr(holder).lower()
        assert "private-vault-name" not in rendered
        assert "credential" not in rendered
        assert "tenant" not in rendered


def test_status_waits_out_acquire_to_publish_generation_transition(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    state_root = tmp_path / "state"
    vault = tmp_path / "vault"
    vault.mkdir()
    acquired = context.Event()
    publish = context.Event()
    entered = context.Event()
    release = context.Event()
    holder = context.Process(
        target=_process_hold_paused_before_publish,
        args=(
            str(state_root),
            str(vault),
            acquired,
            publish,
            entered,
            release,
        ),
    )
    holder.start()
    result: list[dict[str, object]] = []
    status_thread = threading.Thread(
        target=lambda: result.append(
            VaultMutationCoordinator(state_root, vault).snapshot()
        )
    )
    try:
        assert acquired.wait(2.0)
        status_thread.start()
        time.sleep(0.05)
        assert not result
        publish.set()
        assert entered.wait(2.0)
        status_thread.join(timeout=2.0)
        assert result
        assert result[0]["state"] == "held"
        assert result[0]["verified"] is True
        assert result[0]["request_id"] == "req-new-generation"
    finally:
        publish.set()
        release.set()
        status_thread.join(timeout=2.0)
        _join_or_terminate([holder])
    assert holder.exitcode == 0


def test_probe_cleanup_cannot_delete_a_new_holders_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    vault = tmp_path / "vault"
    vault.mkdir()
    mutation_lock_module.prepare_windows_private_state_root(state_root)
    status_coordinator = VaultMutationCoordinator(state_root, vault)
    status_coordinator.metadata_path.parent.mkdir(parents=True, exist_ok=True)
    status_coordinator.metadata_path.write_text("stale", encoding="utf-8")
    cleanup_started = threading.Event()
    continue_cleanup = threading.Event()
    original = mutation_lock_module._clear_holder_metadata
    paused = False

    def paused_clear(path: Path) -> None:
        nonlocal paused
        original(path)
        if not paused:
            paused = True
            cleanup_started.set()
            assert continue_cleanup.wait(2.0)

    monkeypatch.setattr(mutation_lock_module, "_clear_holder_metadata", paused_clear)
    status_result: list[dict[str, object]] = []
    holder_entered = threading.Event()
    release_holder = threading.Event()

    def read_status() -> None:
        status_result.append(status_coordinator.snapshot())

    def acquire_after_probe() -> None:
        writer = VaultMutationCoordinator(state_root, vault)
        with writer.hold(
            timeout_seconds=2.0,
            request_id="req-after-cleanup",
            operation="remember",
            holder_kind="command",
        ):
            holder_entered.set()
            assert release_holder.wait(2.0)

    status_thread = threading.Thread(target=read_status)
    holder_thread = threading.Thread(target=acquire_after_probe)
    status_thread.start()
    assert cleanup_started.wait(1.0)
    holder_thread.start()
    assert not holder_entered.wait(0.05)
    continue_cleanup.set()
    status_thread.join(timeout=2.0)
    assert [_boundary(entry) for entry in status_result] == [{"state": "free"}]
    assert holder_entered.wait(1.0)
    snapshot = status_coordinator.snapshot()
    assert snapshot["verified"] is True
    assert snapshot["request_id"] == "req-after-cleanup"
    release_holder.set()
    holder_thread.join(timeout=2.0)
    assert not status_thread.is_alive()
    assert not holder_thread.is_alive()


def test_unusable_state_root_raises_actionable_lock_error(tmp_path: Path) -> None:
    state_root = tmp_path / "not-a-directory"
    state_root.write_text("occupied", encoding="utf-8")
    vault = tmp_path / "vault"
    vault.mkdir()
    coordinator = VaultMutationCoordinator(state_root, vault)

    with pytest.raises(OpError) as raised:
        with coordinator.hold(timeout_seconds=0.05):
            pytest.fail("coordinator entered with an unusable state root")
    assert raised.value.code == "MUTATION_LOCK_UNAVAILABLE"
    assert raised.value.remediation


def test_exception_releases_mutation_authority(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    coordinator = VaultMutationCoordinator(tmp_path / "state", vault)

    with pytest.raises(RuntimeError, match="boom"):
        with coordinator.hold(timeout_seconds=0.2):
            raise RuntimeError("boom")

    with coordinator.hold(timeout_seconds=0.2):
        pass


def test_holder_snapshot_is_content_free_and_clears_after_release(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    coordinator = VaultMutationCoordinator(tmp_path / "state", vault)

    with coordinator.hold(
        timeout_seconds=0.2,
        request_id="req-123",
        operation="edit_memory",
        holder_kind="command",
    ):
        snapshot = coordinator.snapshot()
        assert snapshot["state"] == "held"
        assert snapshot["request_id"] == "req-123"
        assert snapshot["operation"] == "edit_memory"
        assert snapshot["holder_kind"] == "command"
        assert snapshot["age_seconds"] >= 0
        assert str(vault) not in str(snapshot)

    assert _boundary(coordinator.snapshot()) == {"state": "free"}


def test_active_mutation_snapshot_reports_oldest_process_holder(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    coordinator = VaultMutationCoordinator(tmp_path / "state", vault)

    assert active_mutation_snapshot() == {"state": "free"}
    with coordinator.hold(
        request_id="req-status",
        operation="edit_memory",
        holder_kind="command",
    ):
        snapshot = active_mutation_snapshot()
        assert snapshot["state"] == "held"
        assert snapshot["request_id"] == "req-status"
        assert snapshot["operation"] == "edit_memory"
        assert snapshot["holder_kind"] == "command"
        assert str(vault) not in str(snapshot)
    assert active_mutation_snapshot() == {"state": "free"}


def test_long_holder_warning_is_bounded_and_content_free(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    vault = tmp_path / "private-vault-name"
    vault.mkdir()
    coordinator = VaultMutationCoordinator(
        tmp_path / "state", vault, long_holder_seconds=0.01
    )

    with caplog.at_level(logging.WARNING, logger="exomem.mutation_lock"):
        with coordinator.hold(
            request_id="req-slow",
            operation="background_media_reconcile",
            holder_kind="background",
        ):
            time.sleep(0.02)
            assert coordinator.snapshot()["overdue"] is True
            assert coordinator.snapshot()["overdue"] is True

    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 1
    assert "req-slow" in messages[0]
    assert "private-vault-name" not in messages[0]


def test_dynamic_retry_after_ms_scales_with_age_and_is_capped() -> None:
    from exomem.mutation_lock import dynamic_retry_after_ms

    assert dynamic_retry_after_ms(None) == 750
    assert dynamic_retry_after_ms({"state": "free"}) == 750
    assert dynamic_retry_after_ms({"state": "held", "age_seconds": 0.0, "overdue": False}) == 750
    assert dynamic_retry_after_ms({"state": "held", "age_seconds": 4.0, "overdue": False}) == 2000
    # Capped at 15000 regardless of how large age_seconds gets.
    assert dynamic_retry_after_ms({"state": "held", "age_seconds": 100.0, "overdue": False}) == 15000
    # Overdue floors at 5000 even for a still-small age.
    assert dynamic_retry_after_ms({"state": "held", "age_seconds": 1.0, "overdue": True}) == 5000


def test_mutation_busy_includes_wait_ms_and_dynamic_retry_hint(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    holder = VaultMutationCoordinator(tmp_path / "state", vault, long_holder_seconds=60.0)
    contender = VaultMutationCoordinator(tmp_path / "state", vault, long_holder_seconds=60.0)
    entered = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with holder.hold(timeout_seconds=2.0):
            entered.set()
            assert release.wait(2.0)

    thread = threading.Thread(target=hold_lock)
    thread.start()
    assert entered.wait(1.0)
    try:
        time.sleep(0.05)
        with pytest.raises(OpError) as raised:
            with contender.hold(timeout_seconds=0.03):
                pytest.fail("contender entered a held mutation boundary")
        assert raised.value.details["wait_ms"] >= 0
        assert 750 <= raised.value.details["retry_after_ms"] <= 15000
    finally:
        release.set()
        thread.join(timeout=2.0)


def test_hold_emits_acquired_and_released_events_with_timing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    coordinator = VaultMutationCoordinator(tmp_path / "state", vault)

    with caplog.at_level(logging.INFO, logger="exomem.mutation_lock"):
        with coordinator.hold(request_id="req-timed", operation="remember", holder_kind="command"):
            pass

    acquired = next(r for r in caplog.records if getattr(r, "event", None) == "mutation_lock_acquired")
    released = next(r for r in caplog.records if getattr(r, "event", None) == "mutation_lock_released")
    assert isinstance(acquired.fields["wait_ms"], float)
    assert acquired.fields["operation"] == "remember"
    assert isinstance(released.fields["hold_ms"], float)
    assert released.fields["operation"] == "remember"


def test_last_mutation_timing_identifies_the_completed_operation(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    coordinator = VaultMutationCoordinator(tmp_path / "state", vault)

    with coordinator.hold(operation="epistemic_graph_publish_rebuild", holder_kind="graph"):
        pass

    timing = last_mutation_timing()
    assert timing is not None
    assert timing["wait_ms"] >= 0.0
    assert timing["hold_ms"] >= 0.0
    assert timing["operation"] == "epistemic_graph_publish_rebuild"
    assert timing["holder_kind"] == "graph"


def test_long_hold_warning_is_guaranteed_on_release_without_any_probe(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Unlike a probe-triggered warning, this warning must fire even when
    nobody ever calls `snapshot()` while the boundary is held."""
    vault = tmp_path / "unprobed-vault"
    vault.mkdir()
    coordinator = VaultMutationCoordinator(
        tmp_path / "state", vault, long_holder_seconds=0.01
    )

    with caplog.at_level(logging.WARNING, logger="exomem.mutation_lock"):
        with coordinator.hold(request_id="req-unprobed", operation="edit", holder_kind="command"):
            time.sleep(0.02)
            # Deliberately never call coordinator.snapshot() here.

    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 1
    assert "req-unprobed" in messages[0]
    assert "unprobed-vault" not in messages[0]


def test_orphan_snapshot_reports_real_age_when_metadata_mutex_is_contended(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """When the metadata mutex cannot be acquired within the status timeout,
    a lock-free (tear-free, atomically-published) sidecar read must report
    the holder's real age/overdue rather than fabricating a healthy-looking
    unknown holder at age 0."""
    vault = tmp_path / "vault"
    vault.mkdir()
    state_root = tmp_path / "state"
    mutation_lock_module.prepare_windows_private_state_root(state_root)
    coordinator = VaultMutationCoordinator(state_root, vault, long_holder_seconds=60.0)

    acquired_at = time.time() - 5.0
    holder = {
        "schema": 1,
        "generation": "a" * 32,
        "request_id": "req-orphan",
        "operation": "remember",
        "holder_kind": "command",
        "acquired_at": acquired_at,
        "long_holder_seconds": 60.0,
    }
    mutation_lock_module._atomic_write_holder_metadata(coordinator.metadata_path, holder)

    metadata_handle = coordinator._open_lock_file(coordinator.metadata_lock_path)
    assert mutation_lock_module._try_os_lock(metadata_handle)
    try:
        with caplog.at_level(logging.INFO, logger="exomem.mutation_lock"):
            snapshot = coordinator.snapshot()
    finally:
        mutation_lock_module._release_os_lock(metadata_handle)
        metadata_handle.close()

    assert snapshot["state"] == "held"
    assert snapshot["verified"] is False
    assert snapshot["request_id"] == "req-orphan"
    assert snapshot["holder_kind"] == "command"
    assert snapshot["age_seconds"] >= 4.5
    assert snapshot["overdue"] is False
    assert any(
        getattr(r, "event", None) == "mutation_holder_unverified" for r in caplog.records
    )


def test_orphan_snapshot_reports_real_overdue_state(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    state_root = tmp_path / "state"
    mutation_lock_module.prepare_windows_private_state_root(state_root)
    coordinator = VaultMutationCoordinator(state_root, vault, long_holder_seconds=1.0)

    holder = {
        "schema": 1,
        "generation": "b" * 32,
        "request_id": "req-overdue-orphan",
        "operation": "edit_memory",
        "holder_kind": "background",
        "acquired_at": time.time() - 10.0,
        "long_holder_seconds": 1.0,
    }
    mutation_lock_module._atomic_write_holder_metadata(coordinator.metadata_path, holder)

    metadata_handle = coordinator._open_lock_file(coordinator.metadata_lock_path)
    assert mutation_lock_module._try_os_lock(metadata_handle)
    try:
        snapshot = coordinator.snapshot()
    finally:
        mutation_lock_module._release_os_lock(metadata_handle)
        metadata_handle.close()

    assert snapshot["overdue"] is True
    assert snapshot["verified"] is False


def test_orphan_snapshot_falls_back_to_unknown_holder_without_a_sidecar(
    tmp_path: Path,
) -> None:
    """No sidecar at all (never published, or already cleared) still
    fabricates the unknown-holder shape — only a readable sidecar earns the
    age-aware path."""
    vault = tmp_path / "vault"
    vault.mkdir()
    coordinator = VaultMutationCoordinator(tmp_path / "state", vault)
    assert not coordinator.metadata_path.exists()

    metadata_handle = coordinator._open_lock_file(coordinator.metadata_lock_path)
    assert mutation_lock_module._try_os_lock(metadata_handle)
    try:
        snapshot = coordinator.snapshot()
    finally:
        mutation_lock_module._release_os_lock(metadata_handle)
        metadata_handle.close()

    assert _boundary(snapshot) == {
        "state": "held",
        "request_id": "untracked",
        "operation": "unknown",
        "holder_kind": "external",
        "age_seconds": 0.0,
        "overdue": False,
        "verified": False,
    }


def test_process_exit_releases_os_mutation_lock(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    state_root = tmp_path / "state"
    vault = tmp_path / "vault"
    vault.mkdir()
    entered = context.Event()
    crashing = context.Process(
        target=_process_crash,
        args=(str(state_root), str(vault), entered),
    )
    crashing.start()
    assert entered.wait(2.0)
    crashing.join(timeout=3.0)
    if crashing.is_alive():
        crashing.terminate()
        crashing.join(timeout=2.0)
    assert crashing.exitcode == 23

    recovered = VaultMutationCoordinator(state_root, vault)
    with recovered.hold(timeout_seconds=1.0):
        assert recovered.lock_path.exists()

def test_sequential_holds_on_one_thread_reacquire_the_os_boundary(
    tmp_path: Path,
) -> None:
    """Regression: releasing a hold must reset the reentrancy state so the same
    thread's NEXT hold reacquires the OS lock and republishes the holder
    sidecar, instead of silently taking the reentrant fast path with no
    cross-process exclusion at all."""
    vault = tmp_path / "vault"
    vault.mkdir()
    coordinator = VaultMutationCoordinator(tmp_path / "state", vault)

    with coordinator.hold(timeout_seconds=3.0, operation="first", holder_kind="command"):
        pass

    with coordinator.hold(timeout_seconds=3.0, operation="second", holder_kind="command"):
        probe = coordinator._open_lock_file(coordinator.lock_path)
        try:
            assert not mutation_lock_module._try_os_lock(probe), (
                "a foreign handle acquired the vault lock while the second "
                "sequential hold was supposedly active"
            )
        finally:
            probe.close()
        snapshot = coordinator.snapshot()
        assert snapshot["state"] == "held"
        assert snapshot["operation"] == "second"

    probe = coordinator._open_lock_file(coordinator.lock_path)
    try:
        assert mutation_lock_module._try_os_lock(probe)
        mutation_lock_module._release_os_lock(probe)
    finally:
        probe.close()


def test_the_private_state_root_is_creatable_under_a_missing_ancestor(
    tmp_path: Path,
) -> None:
    """A fresh profile has no `~/.cache`, and the store must still come up.

    `LeaseConfig.state_dir` defaults to `~/.cache/exomem`, so on a profile that
    has never had an XDG-style cache directory the store's parent chain is two
    levels deep and neither level exists. The POSIX branch creates the chain
    (`mkdir(parents=True)`); the Windows branch creates exactly one directory so
    it can own that entry's DACL, and used to inherit `FileNotFoundError` from
    the missing ancestor -- which surfaced as the whole server failing to start
    with `WinError 3`. Nothing in CI runs on Windows, so this is the guard.
    """
    from exomem.writer_lease import IdempotencyStore

    root = tmp_path / "home" / ".cache" / "exomem"
    assert not root.parent.exists()

    store = IdempotencyStore(root / "idempotency.sqlite")

    assert root.is_dir()
    assert store._runtime_state_error is None
