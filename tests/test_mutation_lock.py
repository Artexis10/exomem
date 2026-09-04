from __future__ import annotations

import inspect
import json
import logging
import multiprocessing
import os
import stat
import threading
import time
from collections import deque
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


#: How long a holder keeps the mutation boundary, and how long a test will
#: wait to observe a contender being refused. The gap between them is the
#: whole discriminating power of the contention assertions: a contender that
#: waited for the holder instead of honouring its own timeout blows straight
#: through the observation window.
_HOLD_SECONDS = 45.0
_OBSERVE_SECONDS = 15.0


def test_contention_view_snapshots_busy_refusals_while_recording_a_refusal() -> None:
    """A full recent-refusal deque may be evicted while a view is built."""

    entered_iteration = threading.Event()
    resume_iteration = threading.Event()

    class _PausingDeque(deque[float]):
        def __iter__(self):
            iterator = super().__iter__()
            yield next(iterator)
            entered_iteration.set()
            assert resume_iteration.wait(timeout=_OBSERVE_SECONDS)
            yield from iterator

    state = mutation_lock_module._LocalLockState(
        busy_refusal_monotonic=_PausingDeque(
            [time.monotonic()] * mutation_lock_module._CONTENTION_RECENT_SAMPLES,
            maxlen=mutation_lock_module._CONTENTION_RECENT_SAMPLES,
        )
    )
    failures: list[BaseException] = []

    def view() -> None:
        try:
            mutation_lock_module._contention_view(state)
        except BaseException as error:
            failures.append(error)

    reader = threading.Thread(target=view)
    reader.start()
    assert entered_iteration.wait(timeout=_OBSERVE_SECONDS)

    writer = threading.Thread(
        target=mutation_lock_module._note_busy_refusal,
        args=(state, None),
    )
    writer.start()
    resume_iteration.set()
    reader.join(timeout=_OBSERVE_SECONDS)
    writer.join(timeout=_OBSERVE_SECONDS)

    assert not reader.is_alive()
    assert not writer.is_alive()
    assert failures == []


#: A NEGATIVE observation -- how long the test waits to prove something has NOT
#: happened yet -- is a different animal and stays short. Widening one does not
#: merely slow the test, it changes the scenario: the product's own acquire
#: timeout runs during the same window. At 15s the holder in
#: `test_probe_cleanup_cannot_delete_a_new_holders_metadata` gave up with
#: MUTATION_BUSY inside the settle period and never entered afterwards, so the
#: positive assertion that followed failed. A slow runner can only make these
#: pass vacuously, never fail, which is why they are safe left tight where a
#: positive wait is not. They are written as literals, deliberately, so nobody
#: sweeps them along with the constants above.

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
#: Deliberately tiny sub-authorities. A real Windows account SID carries
#: 32-bit values and identifies one specific machine and account, which is
#: not something a public repository should carry.
_USER_SID = "S-1-5-21-1-2-3-1001"


#: The descriptor a GitHub Windows runner reads back for its own cache
#: directory, with the runner's real SID replaced by a synthetic one. That
#: runner's user is its account domain's built-in Administrator
#: (RID 500), and SDDL renders that account as `LA` -- never as its SID. The
#: grants are exactly the three this module writes; only the spelling differs.
_LOCAL_ADMIN_DACL = "O:BAD:P(A;OICI;FA;;;LA)(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
#: RID 500 with the same deliberately tiny sub-authorities as `_USER_SID`.
_LOCAL_ADMIN_SID = "S-1-5-21-1-2-3-500"


def test_local_admin_alias_counts_as_the_users_grant_when_the_user_is_rid_500() -> None:
    """The validator must not reject a DACL it wrote itself.

    This exact descriptor cost 1725 failures -- 72% of the whole Windows lane --
    because every runner user is RID 500, so every private directory this module
    created read back with its own grant spelled `LA` and failed closed with no
    repair path. The instrumentation added earlier in this PR is what produced
    the string; it was never guessable from a developer box, where the current
    user is not the built-in Administrator and the SID is written literally.
    """
    assert mutation_lock_module._windows_private_dacl_is_valid(
        _LOCAL_ADMIN_DACL, _LOCAL_ADMIN_SID, directory=True
    )


def test_local_admin_alias_is_rejected_for_any_other_account() -> None:
    """`LA` is a spelling of *us* only when we are the account it names.

    Accepting it for an ordinary user would admit full access for a different
    principal entirely -- the built-in Administrator -- which is the class of
    hole this validator exists to catch.
    """
    assert not mutation_lock_module._windows_private_dacl_is_valid(
        _LOCAL_ADMIN_DACL, _USER_SID, directory=True
    )


def test_local_admin_alias_alongside_a_literal_grant_is_still_a_duplicate() -> None:
    """Resolving the alias before the duplicate check keeps that check honest."""
    doubled = (
        f"O:BAD:P(A;OICI;FA;;;LA)(A;OICI;FA;;;{_LOCAL_ADMIN_SID})"
        "(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
    )
    assert not mutation_lock_module._windows_private_dacl_is_valid(
        doubled, _LOCAL_ADMIN_SID, directory=True
    )


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
        if not release.wait(_HOLD_SECONDS):
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
        if not publish.wait(_HOLD_SECONDS):
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
        if not release.wait(_HOLD_SECONDS):
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
        process.join(timeout=_HOLD_SECONDS)
    for process in processes:
        if process.is_alive():
            process.terminate()
            process.join(timeout=_HOLD_SECONDS)


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
            assert release_first.wait(_HOLD_SECONDS)

    def enter_second() -> None:
        second_attempting.set()
        with second.hold(timeout_seconds=2.0):
            second_entered.set()

    first_thread = threading.Thread(target=hold_first)
    second_thread = threading.Thread(target=enter_second)
    first_thread.start()
    assert first_entered.wait(_OBSERVE_SECONDS)
    second_thread.start()
    assert second_attempting.wait(_OBSERVE_SECONDS)
    assert not second_entered.wait(0.1)
    release_first.set()
    assert second_entered.wait(_OBSERVE_SECONDS)
    first_thread.join(timeout=_HOLD_SECONDS)
    second_thread.join(timeout=_HOLD_SECONDS)
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
        assert first_attempting.wait(_OBSERVE_SECONDS)
        assert first_entered.wait(_OBSERVE_SECONDS)
        second.start()
        assert second_attempting.wait(_OBSERVE_SECONDS)
        assert not second_entered.wait(0.2)
        release_first.set()
        assert second_entered.wait(_OBSERVE_SECONDS)
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
            assert release_first.wait(_HOLD_SECONDS)

    first_thread = threading.Thread(target=hold_first)
    first_thread.start()
    assert first_entered.wait(_OBSERVE_SECONDS)
    with second.hold(timeout_seconds=0.2):
        second_entered.set()
    assert second_entered.is_set()
    release_first.set()
    first_thread.join(timeout=_HOLD_SECONDS)
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
        with holder.hold(timeout_seconds=_HOLD_SECONDS):
            entered.set()
            assert release.wait(_HOLD_SECONDS)

    thread = threading.Thread(target=hold_lock)
    thread.start()
    assert entered.wait(_OBSERVE_SECONDS)
    started = time.monotonic()
    try:
        with pytest.raises(OpError) as raised:
            with contender.hold(timeout_seconds=0.05):
                pytest.fail("contender entered a held mutation boundary")
        assert raised.value.code == "MUTATION_BUSY"
        assert raised.value.remediation
        assert "retry" in raised.value.remediation.lower()
        # The discriminating assertion: the contender gave up on its own 0.05s
        # timeout instead of waiting out the holder. That stays provable because
        # the holder holds for _HOLD_SECONDS, which is far larger -- so this
        # bound can be generous without going vacuous. At 0.5s it was also
        # claiming a contended Windows shard schedules two threads within half a
        # second, which is not a property of this code.
        assert time.monotonic() - started < _OBSERVE_SECONDS
    finally:
        release.set()
        thread.join(timeout=_HOLD_SECONDS)
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
        assert attempting.wait(_OBSERVE_SECONDS)
        assert entered.wait(_OBSERVE_SECONDS)
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
        assert attempting.wait(_OBSERVE_SECONDS)
        assert entered.wait(_OBSERVE_SECONDS)
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


def test_metadata_free_hold_is_reserved_for_internal_identity_coordination(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    coordinator = VaultMutationCoordinator(tmp_path / "state", vault)

    with pytest.raises(ValueError, match="reserved-state"):
        with coordinator.hold(
            holder_kind="command",
            publish_holder_metadata=False,
        ):
            pytest.fail("ordinary mutation holds must remain attributable")


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
        assert acquired.wait(_OBSERVE_SECONDS)
        status_thread.start()
        time.sleep(0.05)
        assert not result
        publish.set()
        assert entered.wait(_OBSERVE_SECONDS)
        status_thread.join(timeout=_HOLD_SECONDS)
        assert result
        assert result[0]["state"] == "held"
        assert result[0]["verified"] is True
        assert result[0]["request_id"] == "req-new-generation"
    finally:
        publish.set()
        release.set()
        status_thread.join(timeout=_HOLD_SECONDS)
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
            assert continue_cleanup.wait(_HOLD_SECONDS)

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
            assert release_holder.wait(_HOLD_SECONDS)

    status_thread = threading.Thread(target=read_status)
    holder_thread = threading.Thread(target=acquire_after_probe)
    status_thread.start()
    assert cleanup_started.wait(_OBSERVE_SECONDS)
    holder_thread.start()
    assert not holder_entered.wait(0.05)
    continue_cleanup.set()
    status_thread.join(timeout=_HOLD_SECONDS)
    assert [_boundary(entry) for entry in status_result] == [{"state": "free"}]
    assert holder_entered.wait(_OBSERVE_SECONDS)
    snapshot = status_coordinator.snapshot()
    assert snapshot["verified"] is True
    assert snapshot["request_id"] == "req-after-cleanup"
    release_holder.set()
    holder_thread.join(timeout=_HOLD_SECONDS)
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
        with holder.hold(timeout_seconds=_HOLD_SECONDS):
            entered.set()
            assert release.wait(_HOLD_SECONDS)

    thread = threading.Thread(target=hold_lock)
    thread.start()
    assert entered.wait(_OBSERVE_SECONDS)
    try:
        time.sleep(0.05)
        with pytest.raises(OpError) as raised:
            with contender.hold(timeout_seconds=0.03):
                pytest.fail("contender entered a held mutation boundary")
        assert raised.value.details["wait_ms"] >= 0
        assert 750 <= raised.value.details["retry_after_ms"] <= 15000
    finally:
        release.set()
        thread.join(timeout=_HOLD_SECONDS)


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

    # The guard here is "exactly ONE warning", not "exactly one record": the
    # `already_warned` flag exists so the probe path and the release path can
    # never both warn for the same hold, and that is what must stay pinned.
    # The human line and its structured twin are counted separately so the
    # twin's existence cannot mask a genuine double warning.
    human = [
        record.getMessage()
        for record in caplog.records
        if getattr(record, "event", None) is None
    ]
    assert len(human) == 1
    assert "req-unprobed" in human[0]
    events = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "mutation_lock_long_hold"
    ]
    assert len(events) == 1
    # Same level as the human line it twins: an operator who filters a noisy
    # JSONL log to WARNING and above must not lose the machine-readable half.
    assert events[0].levelno == logging.WARNING
    # The total is bounded too, and that is the half the two filtered counts
    # above cannot supply: without it, an extra WARNING under ANY other event
    # name passes unseen -- including every release row escalating to WARNING,
    # which is the flood regression this whole change exists to prevent.
    assert len(caplog.records) == 2, (
        f"expected exactly the human warning and its structured twin, got "
        f"{[(r.levelname, getattr(r, 'event', None)) for r in caplog.records]}"
    )
    assert all("unprobed-vault" not in record.getMessage() for record in caplog.records)


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
    assert entered.wait(_OBSERVE_SECONDS)
    crashing.join(timeout=_HOLD_SECONDS)
    if crashing.is_alive():
        crashing.terminate()
        crashing.join(timeout=_HOLD_SECONDS)
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


def test_dacl_error_reports_what_it_observed_not_just_that_it_refused() -> None:
    """A rejection you cannot reproduce locally is only useful if it self-describes.

    The message named the path and a repair command and stopped there. That is
    enough on a machine you can log into and inspect, and useless from anywhere
    else -- so a CI runner's rejection could only be guessed at from whatever
    shape some other machine happened to produce. Carrying the descriptor turns
    the report itself into the evidence.
    """
    error = mutation_lock_module.WindowsRuntimeDaclError(
        Path(r"C:\Users\example\.cache\exomem"),
        "icacls.exe ...",
        observed="D:AI(A;OICIID;FA;;;BA)(A;OICIID;FA;;;SY)(A;OICIID;FA;;;WD)",
        expected=("S-1-5-21-1-2-3-1001", "SY", "BA"),
    )

    assert error.observed is not None
    assert "WD" in str(error), "the offending trustee must survive into the text"
    assert "expected full-access trustees" in str(error)
    assert "S-1-5-21-1-2-3-1001" in str(error)
    assert "icacls.exe" in str(error)


def test_dacl_error_still_renders_without_a_descriptor() -> None:
    """The two new fields are optional; older call sites must not break."""
    error = mutation_lock_module.WindowsRuntimeDaclError(
        Path(r"C:\example\x"), "icacls.exe ..."
    )

    assert error.observed is None
    assert error.expected == ()
    assert "unsafe Windows DACL" in str(error)


def test_owner_rights_counts_when_the_administrators_group_owns_the_entry() -> None:
    """The second half of the same descriptor, and 38 more Windows failures.

    `LA` fixed the *trustee* spelling; this is the *owner*. Where the policy
    "System objects: Default owner for objects created by members of the
    Administrators group" names the group -- which is how GitHub's
    `windows-latest` runners are configured -- every directory an administrator
    creates is owned by `BA`, not by the account. So the descriptor this module
    writes itself came back as `O:BA` with an `OW` ACE, and `OW` was refused
    because the owner was not spelled as the current user.

    The built-in Administrator belongs to that group by construction, so the
    ACE does grant to us.
    """
    assert mutation_lock_module._windows_private_dacl_is_valid(
        f"O:BA{_OWNER_RIGHTS_DACL}", _LOCAL_ADMIN_SID, directory=True
    )


def test_administrators_owner_is_no_concession_to_an_ordinary_account() -> None:
    """Membership is what carries the grant, and an ordinary user has none.

    Were this unscoped, any account would accept an `OW` ACE on an entry owned
    by Administrators -- inferring its own access from a group it may not be in.
    """
    assert not mutation_lock_module._windows_private_dacl_is_valid(
        f"O:BA{_OWNER_RIGHTS_DACL}", _USER_SID, directory=True
    )


def test_the_administrators_owner_concession_admits_no_new_principal() -> None:
    """`BA` already holds full access here, so resolving `OW` to it widens nothing.

    That is the whole justification: the concession cannot admit anyone the
    descriptor did not already admit, because the group it names is one of the
    three trustees private runtime state permits.
    """
    trustees = mutation_lock_module._windows_private_dacl_trustees(_LOCAL_ADMIN_SID)

    assert "BA" in trustees


def test_a_foreign_owner_is_still_refused_for_the_built_in_administrator() -> None:
    """Being RID 500 does not make every owner ours."""
    assert not mutation_lock_module._windows_private_dacl_is_valid(
        f"O:S-1-5-21-9-9-9-1001{_OWNER_RIGHTS_DACL}", _LOCAL_ADMIN_SID, directory=True
    )


def test_the_administrators_owner_does_not_relax_the_trustee_set() -> None:
    """The owner decides what `OW` means, never who else may be granted."""
    foreign = (
        "O:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;OW)"
        "(A;OICI;FA;;;S-1-5-21-9-9-9-1001)"
    )
    assert not mutation_lock_module._windows_private_dacl_is_valid(
        foreign, _LOCAL_ADMIN_SID, directory=True
    )


def test_an_inherited_directory_dacl_is_still_refused() -> None:
    """Protection is a separate requirement and this change does not touch it.

    The other 12 of the 50 runner failures are this: a directory that already
    existed, so it carries its parent's ACEs and no `P`. Whatever grants it
    happens to name, an unprotected DACL can change under us when the parent's
    does -- and this module validates a pre-existing entry rather than
    repairing it.
    """
    inherited = "O:BAD:(A;OICIID;FA;;;SY)(A;OICIID;FA;;;BA)(A;OICIID;FA;;;OW)"

    assert not mutation_lock_module._windows_private_dacl_is_valid(
        inherited, _LOCAL_ADMIN_SID, directory=True
    )


def test_the_administrators_owner_is_not_a_trustee_spelling() -> None:
    """`BA` resolves an owner, never a literal ACE trustee.

    Folding it into the trustee spellings would collapse a real `BA` grant onto
    the user's slot, and the descriptor below -- which names the user, SYSTEM
    and Administrators exactly once each -- would read as a duplicate.
    """
    literal = (
        f"O:BAD:P(A;OICI;FA;;;{_LOCAL_ADMIN_SID})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
    )
    assert mutation_lock_module._windows_private_dacl_is_valid(
        literal, _LOCAL_ADMIN_SID, directory=True
    )


# --- an inherited private DACL is tightened, not refused -------------------------
#
# Windows gives a directory created inside an already-private parent its
# parent's ACEs by inheritance rather than a DACL of its own, so the entry is
# private but unprotected. The validator demanded `D:P` with explicit `OICI`
# flags and refused everything else, which failed closed on directories that
# had never admitted anybody outside the private set -- 29 of 241 failures on
# the Windows lane, all of them state roots created under a pytest temporary
# directory. `tempfile.mkdtemp` protects its own root as
# `D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;OW)`, and everything made
# inside it inherits exactly that.

_INHERITED_DACL = (
    f"O:{_USER_SID}D:"
    f"(A;OICIID;FA;;;{_USER_SID})(A;OICIID;FA;;;SY)(A;OICIID;FA;;;BA)"
)
_INHERITED_OWNER_RIGHTS_DACL = (
    f"O:{_USER_SID}D:(A;OICIID;FA;;;SY)(A;OICIID;FA;;;BA)(A;OICIID;FA;;;OW)"
)


def test_an_inherited_private_dacl_is_reported_as_inherited_not_unsafe() -> None:
    """The verdict the repair decision is made on.

    Every trustee is one this module writes itself, so no principal outside the
    private set can hold a handle to the entry. That is what separates this from
    a DACL that must be refused: there is nothing to take back.
    """
    assert (
        mutation_lock_module._windows_private_dacl_verdict(
            _INHERITED_DACL, _USER_SID, directory=True
        )
        == mutation_lock_module._WINDOWS_DACL_INHERITED
    )
    assert not mutation_lock_module._windows_private_dacl_is_valid(
        _INHERITED_DACL, _USER_SID, directory=True
    )


def test_the_runners_owner_rights_shape_is_inherited_rather_than_unsafe() -> None:
    """The exact descriptor the Windows lane fails on, spelled with `OW`."""
    assert (
        mutation_lock_module._windows_private_dacl_verdict(
            _INHERITED_OWNER_RIGHTS_DACL, _USER_SID, directory=True
        )
        == mutation_lock_module._WINDOWS_DACL_INHERITED
    )


def test_a_protected_private_dacl_stays_private() -> None:
    """The verdict split must not disturb the descriptor this module writes."""
    written = mutation_lock_module._windows_private_dacl_sddl(_USER_SID)

    assert (
        mutation_lock_module._windows_private_dacl_verdict(
            f"O:{_USER_SID}{written}", _USER_SID, directory=True
        )
        == mutation_lock_module._WINDOWS_DACL_PRIVATE
    )


@pytest.mark.parametrize(
    "trustee",
    ["WD", "AU", "BU", "S-1-5-21-1-2-3-1002"],
    ids=["everyone", "authenticated-users", "users", "another-account"],
)
def test_a_foreign_trustee_is_unsafe_however_it_arrived(trustee: str) -> None:
    """Inheritance is not a defence, and this is the line that matters.

    A trustee outside the private set means somebody else may already hold a
    handle, and tightening a DACL does not close a handle opened against the
    old one. So this stays a refusal rather than becoming a repair.
    """
    sddl = (
        f"O:{_USER_SID}D:(A;OICIID;FA;;;{trustee})"
        f"(A;OICIID;FA;;;{_USER_SID})(A;OICIID;FA;;;SY)(A;OICIID;FA;;;BA)"
    )

    assert (
        mutation_lock_module._windows_private_dacl_verdict(
            sddl, _USER_SID, directory=True
        )
        == mutation_lock_module._WINDOWS_DACL_UNSAFE
    )


def test_partial_rights_are_unsafe_even_for_a_private_trustee() -> None:
    """Full access is the grant this module writes; anything else is not it."""
    sddl = (
        f"O:{_USER_SID}D:(A;OICIID;0x1301bf;;;{_USER_SID})"
        f"(A;OICIID;FA;;;SY)(A;OICIID;FA;;;BA)"
    )

    assert (
        mutation_lock_module._windows_private_dacl_verdict(
            sddl, _USER_SID, directory=True
        )
        == mutation_lock_module._WINDOWS_DACL_UNSAFE
    )


def test_owner_rights_inherited_under_a_foreign_owner_is_unsafe() -> None:
    """`OW` names whoever owns the entry, so the owner check still decides it."""
    sddl = "O:S-1-5-21-9-9-9-1001D:(A;OICIID;FA;;;SY)(A;OICIID;FA;;;BA)(A;OICIID;FA;;;OW)"

    assert (
        mutation_lock_module._windows_private_dacl_verdict(
            sddl, _USER_SID, directory=True
        )
        == mutation_lock_module._WINDOWS_DACL_UNSAFE
    )


def test_an_inherited_file_ace_is_still_private() -> None:
    """Files already accepted inheritance; the directory split must not move them."""
    sddl = (
        f"O:{_USER_SID}D:(A;ID;FA;;;{_USER_SID})(A;ID;FA;;;SY)(A;ID;FA;;;BA)"
    )

    assert (
        mutation_lock_module._windows_private_dacl_verdict(
            sddl, _USER_SID, directory=False
        )
        == mutation_lock_module._WINDOWS_DACL_PRIVATE
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL semantics")
def test_a_directory_inheriting_a_private_dacl_is_protected_in_place(
    tmp_path: Path,
) -> None:
    """End to end on real Windows security, because the verdict is only half of it.

    The entry this observes is built the way the runner's is: a parent carrying
    the protected private DACL, and a child that inherits it. Before this, the
    child was refused with a remediation the user had to run by hand in an
    elevated shell.
    """
    parent = tmp_path / "parent"
    parent.mkdir()
    sid = mutation_lock_module._windows_current_user_sid()
    mutation_lock_module._windows_apply_private_dacl(parent, sid)
    child = parent / "hosted-state"
    child.mkdir()
    observed = mutation_lock_module._windows_dacl_sddl(child)
    assert (
        mutation_lock_module._windows_private_dacl_verdict(observed, sid, directory=True)
        == mutation_lock_module._WINDOWS_DACL_INHERITED
    )

    mutation_lock_module._prepare_windows_private_directory(child)

    after = mutation_lock_module._windows_dacl_sddl(child)
    assert mutation_lock_module._windows_private_dacl_is_valid(after, sid, directory=True)
    assert after.split("D:", 1)[1].startswith("P")


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL semantics")
def test_a_directory_admitting_another_account_is_refused_and_left_alone(
    tmp_path: Path,
) -> None:
    """The refusal path, on real security, including that it writes nothing.

    Tightening here would be worse than refusing: the foreign grant means a
    handle may already be open, and rewriting the DACL would hide that while
    changing nothing about the access already obtained.
    """
    target = tmp_path / "state"
    target.mkdir()
    sid = mutation_lock_module._windows_current_user_sid()
    mutation_lock_module._windows_apply_dacl_sddl(
        target,
        f"D:(A;OICIID;FA;;;AU)(A;OICIID;FA;;;SY)(A;OICIID;FA;;;BA)(A;OICIID;FA;;;{sid})",
    )
    before = mutation_lock_module._windows_dacl_sddl(target)

    with pytest.raises(mutation_lock_module.WindowsRuntimeDaclError):
        mutation_lock_module._prepare_windows_private_directory(target)

    assert mutation_lock_module._windows_dacl_sddl(target) == before


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL semantics")
def test_the_tightening_is_attempted_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write that does not take is an error, not a reason to spin.

    The stabilization wait exists for a creator that has not written its DACL
    yet. Retrying our own failed write inside it would spend that whole budget
    re-attempting something already known not to work, and report a timeout
    instead of the permission failure that caused it.
    """
    parent = tmp_path / "parent"
    parent.mkdir()
    sid = mutation_lock_module._windows_current_user_sid()
    mutation_lock_module._windows_apply_private_dacl(parent, sid)
    child = parent / "hosted-state"
    child.mkdir()
    attempts: list[Path] = []

    def refuse(target: Path, directory: object, sid: str) -> None:
        attempts.append(target)

    monkeypatch.setattr(
        mutation_lock_module, "_windows_tighten_private_directory", refuse
    )
    monkeypatch.setattr(mutation_lock_module, "_WINDOWS_DACL_STABILIZATION_SECONDS", 0.05)

    with pytest.raises(mutation_lock_module.WindowsRuntimeDaclError):
        mutation_lock_module._prepare_windows_private_directory(child)

    assert attempts == [child]


# --- Boundary log volume: quiet the routine pair, never the interesting case ---
#
# `_log_mutation_lock_event` logged every acquire and every release at INFO,
# unconditionally.  The dominant caller is reserved-state identity
# coordination, whose holds are sub-millisecond and whose only purpose is
# filesystem-identity serialization, so the service wrote ~87k boundary rows
# an hour and rotated a 5 MB log every eight minutes.  That destroyed the
# retention an operator needs: a traceback that diagnosed a live latency
# incident survived in `exomem.log.4` with about twenty minutes to spare.
#
# The tests below pin BOTH halves of the fix.  Quieting the boring case is
# worthless if it also quiets the case a real diagnosis used, so every
# interesting path — a contended acquire, a slow hold, an overdue hold, a
# refusal, and the boundary metrics — has its own assertion here.


def _events(caplog: pytest.LogCaptureFixture, name: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "event", None) == name]


def _reserved_state_hold(coordinator: VaultMutationCoordinator, **kwargs):
    """A metadata-free reserved-state hold — the high-frequency flood class."""
    return coordinator.hold(
        operation="reserved_identity:media-jobs-store",
        holder_kind="reserved-state",
        publish_holder_metadata=False,
        **kwargs,
    )


def test_routine_reserved_state_holds_are_not_info_rows(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A burst of short, uncontended reserved-state holds must add no INFO rows.

    This is the 87k-rows-an-hour case.  Before the fix each pass wrote two
    INFO rows; the assertion is on the *level*, not on the event existing —
    the rows stay available at DEBUG for anyone who turns them on.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    coordinator = VaultMutationCoordinator(tmp_path / "state", vault)

    with caplog.at_level(logging.DEBUG, logger="exomem.mutation_lock"):
        for _ in range(25):
            with _reserved_state_hold(coordinator):
                pass

    info_or_louder = [
        r
        for r in caplog.records
        if r.levelno >= logging.INFO
        and getattr(r, "event", None)
        in {"mutation_lock_acquired", "mutation_lock_released"}
    ]
    assert info_or_louder == [], (
        f"{len(info_or_louder)} routine reserved-state rows logged at INFO or "
        "louder; this is the log flood that rotates real diagnostics away"
    )
    # The evidence is demoted, never deleted: DEBUG still carries every pair
    # with its timing, so an operator can turn the detail back on.
    assert len(_events(caplog, "mutation_lock_acquired")) == 25
    assert len(_events(caplog, "mutation_lock_released")) == 25
    assert all(
        r.levelno == logging.DEBUG for r in _events(caplog, "mutation_lock_acquired")
    )


def test_attributable_command_holds_stay_at_info(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Only the metadata-free reserved-state class is demoted.

    A command or lifecycle boundary publishes a holder sidecar and runs at
    human frequency (tens of rows an hour, not tens of thousands).  Those rows
    are the mutation audit trail and must survive the quieting.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    coordinator = VaultMutationCoordinator(tmp_path / "state", vault)

    with caplog.at_level(logging.DEBUG, logger="exomem.mutation_lock"):
        with coordinator.hold(
            request_id="req-audit", operation="remember", holder_kind="command"
        ):
            pass

    [acquired] = _events(caplog, "mutation_lock_acquired")
    [released] = _events(caplog, "mutation_lock_released")
    assert acquired.levelno == logging.INFO
    assert released.levelno == logging.INFO
    assert released.fields["operation"] == "remember"


def test_a_slow_reserved_state_hold_stays_visible_at_info(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A reserved-state hold that is NOT sub-millisecond is still interesting.

    The demotion is measurement-driven, not a blanket per-kind mute: the same
    holder class that floods when it is trivial must report itself when it
    starts costing real time, because that is the shape of a boundary problem.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    coordinator = VaultMutationCoordinator(tmp_path / "state", vault)

    with caplog.at_level(logging.DEBUG, logger="exomem.mutation_lock"):
        with _reserved_state_hold(coordinator):
            time.sleep(0.05)

    [released] = _events(caplog, "mutation_lock_released")
    assert released.levelno == logging.INFO, (
        "a slow reserved-state hold was demoted to DEBUG — the quieting swallowed "
        "the very signal a boundary diagnosis reads"
    )
    assert released.fields["hold_ms"] >= mutation_lock_module._QUIET_HOLD_MS
    # The INFO release row must be self-sufficient: it carries the wait as well
    # as the hold, because its paired acquire row may legitimately be DEBUG.
    assert isinstance(released.fields["wait_ms"], float)


def test_a_contended_acquire_stays_visible_at_info(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An acquire that actually waited is reported, whatever its holder kind.

    Nothing is monkeypatched here.  The acquire threshold is derived from the
    coordinator's own `poll_interval_seconds`, so this exercises the shipped
    value: the holder below keeps the boundary for ~60ms, which is more than
    twice the 25ms default poll and therefore an acquire that went round the
    retry loop rather than merely missing a free boundary.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    coordinator = VaultMutationCoordinator(tmp_path / "state", vault)
    threshold_ms = coordinator.poll_interval_seconds * 1000.0

    holder_in = threading.Event()
    release = threading.Event()

    def _holder() -> None:
        with _reserved_state_hold(coordinator):
            holder_in.set()
            release.wait(timeout=_HOLD_SECONDS)

    thread = threading.Thread(target=_holder, daemon=True)
    thread.start()
    try:
        assert holder_in.wait(timeout=_OBSERVE_SECONDS)
        with caplog.at_level(logging.DEBUG, logger="exomem.mutation_lock"):
            contender = threading.Thread(
                target=lambda: _enter_and_leave(coordinator), daemon=True
            )
            contender.start()
            time.sleep(0.06)
            release.set()
            contender.join(timeout=_OBSERVE_SECONDS)
    finally:
        release.set()
        thread.join(timeout=_OBSERVE_SECONDS)

    contended = [
        r
        for r in _events(caplog, "mutation_lock_acquired")
        if r.fields["wait_ms"] >= threshold_ms
    ]
    assert contended, "no contended acquire was observed — the test did not contend"
    assert all(r.levelno == logging.INFO for r in contended), (
        "a contended acquire was demoted to DEBUG"
    )
    # The band a 100ms constant used to swallow whole.  A wait of tens of
    # milliseconds is ~1,000x the uncontended median and is the signal this
    # threshold exists to keep; assert it lands inside the visible band rather
    # than merely above whatever the threshold happens to be.
    assert any(r.fields["wait_ms"] < 100.0 for r in contended), (
        "the test did not exercise the 25-100ms band, where an acquire is "
        "contended but a hand-picked 100ms threshold would have hidden it"
    )


def _enter_and_leave(coordinator: VaultMutationCoordinator) -> None:
    with _reserved_state_hold(coordinator, timeout_seconds=_HOLD_SECONDS):
        pass


def test_refusal_is_logged_at_warning_even_when_the_caller_swallows_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """MUTATION_BUSY must leave a trace in the log, not only in the raised error.

    The refusal payload is what let a live latency incident be diagnosed, but
    it reached the log only because `file_watcher` happened to log the OpError
    it caught.  A caller that retries quietly left the boundary refusal
    invisible.  The boundary records its own refusals now, at WARNING.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    coordinator = VaultMutationCoordinator(tmp_path / "state", vault)

    holder_in = threading.Event()
    release = threading.Event()

    def _holder() -> None:
        with coordinator.hold(operation="epistemic_graph_drain_paths", holder_kind="graph"):
            holder_in.set()
            release.wait(timeout=_HOLD_SECONDS)

    thread = threading.Thread(target=_holder, daemon=True)
    thread.start()
    try:
        assert holder_in.wait(timeout=_OBSERVE_SECONDS)
        with caplog.at_level(logging.DEBUG, logger="exomem.mutation_lock"):
            with pytest.raises(OpError):
                with coordinator.hold(timeout_seconds=0.03):
                    pytest.fail("contender entered a held mutation boundary")
    finally:
        release.set()
        thread.join(timeout=_HOLD_SECONDS)

    refusals = _events(caplog, "mutation_lock_refused")
    assert refusals, "a MUTATION_BUSY refusal left no log record at all"
    assert all(r.levelno >= logging.WARNING for r in refusals), (
        "a refusal was logged below WARNING"
    )
    [refused] = refusals
    assert refused.fields["holder_kind"] == "graph"
    assert refused.fields["operation"] == "epistemic_graph_drain_paths"
    assert refused.fields["wait_ms"] >= 0.0
    # Every key the raised OpError carries, the row carries too.  A refusal row
    # that dropped half the payload would still "log the refusal" while losing
    # the fields a diagnosis actually reads.
    assert refused.fields["status"] == "retryable"
    assert isinstance(refused.fields["retry_after_ms"], int)
    assert refused.fields["age_seconds"] >= 0.0
    assert refused.fields["overdue"] is False
    # The contention counters matter MORE now that routine holds are at DEBUG:
    # `_contention_view` is the only remaining way to see a bounded waiter
    # losing to a stream of short holds.
    assert refused.fields["busy_refusals"] >= 1
    assert refused.fields["busy_refusals_recent"] >= 1
    assert refused.fields["acquire_attempts"] >= 1
    assert refused.fields["contention_scope"] == "process_local"


def test_the_refusal_row_and_the_raised_error_cannot_drift_apart(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Every key the row carries is read out of the payload, value for value.

    This is a field list, not a relationship, and it cannot be anything else:
    `_refused` selects its keys explicitly so an error payload is never copied
    into the log wholesale.  What is pinned is per-key provenance for the named
    keys -- a row that invented, stale-cached or mistyped one of them goes red
    here.  A new `MUTATION_BUSY` key does need a second edit, in `_refused` and
    then in this list; `last_holder` and `committed` both showed that.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    coordinator = VaultMutationCoordinator(tmp_path / "state", vault)

    holder_in = threading.Event()
    release = threading.Event()

    def _holder() -> None:
        with coordinator.hold(operation="epistemic_graph_drain_paths", holder_kind="graph"):
            holder_in.set()
            release.wait(timeout=_HOLD_SECONDS)

    thread = threading.Thread(target=_holder, daemon=True)
    thread.start()
    try:
        assert holder_in.wait(timeout=_OBSERVE_SECONDS)
        with caplog.at_level(logging.DEBUG, logger="exomem.mutation_lock"):
            with pytest.raises(OpError) as raised:
                with coordinator.hold(timeout_seconds=0.03):
                    pytest.fail("contender entered a held mutation boundary")
    finally:
        release.set()
        thread.join(timeout=_HOLD_SECONDS)

    [refused] = _events(caplog, "mutation_lock_refused")
    details = raised.value.details
    holder = details["holder"]
    for key in ("status", "retry_after_ms", "wait_ms"):
        assert refused.fields[key] == details[key], f"refusal row disagrees on {key!r}"
    for key in ("operation", "holder_kind", "age_seconds", "overdue"):
        assert refused.fields[key] == holder[key], f"refusal row disagrees on holder {key!r}"
    for key in ("acquire_attempts", "busy_refusals", "busy_refusals_recent"):
        assert refused.fields[key] == details[key], f"refusal row disagrees on {key!r}"


def test_refusal_payload_keeps_every_field_the_incident_diagnosis_used(
    tmp_path: Path,
) -> None:
    """Pin the exact MUTATION_BUSY shape a real diagnosis read off the log.

    Reproduced from the incident record:

        {"code":"MUTATION_BUSY","holder":{"age_seconds":4.917,
         "holder_kind":"graph","operation":"epistemic_graph_drain_paths",
         "overdue":false},"retry_after_ms":2458,"status":"retryable",
         "wait_ms":5000.46}

    Every one of those keys earned its place by being read.  This is a guard,
    not a behaviour change: it passes before and after the quieting, and it is
    here so the quieting cannot silently take the payload with it.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    coordinator = VaultMutationCoordinator(tmp_path / "state", vault)

    holder_in = threading.Event()
    release = threading.Event()

    def _holder() -> None:
        with coordinator.hold(operation="epistemic_graph_drain_paths", holder_kind="graph"):
            holder_in.set()
            release.wait(timeout=_HOLD_SECONDS)

    thread = threading.Thread(target=_holder, daemon=True)
    thread.start()
    try:
        assert holder_in.wait(timeout=_OBSERVE_SECONDS)
        with pytest.raises(OpError) as raised:
            with coordinator.hold(timeout_seconds=0.03):
                pytest.fail("contender entered a held mutation boundary")
    finally:
        release.set()
        thread.join(timeout=_HOLD_SECONDS)

    error = raised.value
    assert error.code == "MUTATION_BUSY"
    details = error.details
    assert details["status"] == "retryable"
    assert isinstance(details["retry_after_ms"], int)
    assert isinstance(details["wait_ms"], float)
    holder = details["holder"]
    for key in ("age_seconds", "holder_kind", "operation", "overdue"):
        assert key in holder, f"MUTATION_BUSY holder lost the {key!r} field"
    assert holder["holder_kind"] == "graph"
    assert holder["operation"] == "epistemic_graph_drain_paths"
    assert holder["overdue"] is False


def test_an_overdue_hold_still_warns_and_logs_its_long_hold_event(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The long-holder warning is the loudest boundary signal and stays loud.

    A reserved-state holder is used deliberately: the demoted class must still
    escalate when it holds the boundary too long.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    # Both numbers are under `_QUIET_HOLD_MS`, deliberately: a 50ms hold trips
    # the slow-hold arm of `_boundary_event_level` on its own, so the release
    # row reached INFO whether or not the overdue escalation existed at all.
    coordinator = VaultMutationCoordinator(
        tmp_path / "state", vault, long_holder_seconds=0.001
    )

    with caplog.at_level(logging.DEBUG, logger="exomem.mutation_lock"):
        with _reserved_state_hold(coordinator):
            time.sleep(0.002)

    long_holds = _events(caplog, "mutation_lock_long_hold")
    assert long_holds, "an overdue hold emitted no mutation_lock_long_hold event"
    assert all(r.levelno >= logging.WARNING for r in long_holds)
    assert any(
        r.levelno == logging.WARNING and "held too long" in r.getMessage()
        for r in caplog.records
    ), "the long-holder warning line disappeared"
    # An overdue hold is interesting by construction: its release row is never
    # demoted, whatever the holder kind.
    [released] = _events(caplog, "mutation_lock_released")
    assert released.levelno == logging.INFO


def test_boundary_metrics_fire_on_every_hold_including_the_quiet_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Metrics are not logging and keep their coverage through the demotion.

    `exomem_boundary_wait_ms`, `exomem_boundary_hold_ms` and the overdue
    counter are the only thing left measuring the boundary once the routine
    rows are at DEBUG, so a hold that logs nothing at INFO must still be
    counted.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    coordinator = VaultMutationCoordinator(
        tmp_path / "state", vault, long_holder_seconds=0.01
    )

    durations: list[tuple[str, float]] = []
    counters: list[str] = []
    monkeypatch.setattr(
        mutation_lock_module,
        "_observe_boundary_ms",
        lambda name, value: durations.append((name, value)),
    )
    monkeypatch.setattr(
        mutation_lock_module,
        "_bump_boundary_metric",
        lambda name, labels=None: counters.append(name),
    )

    with _reserved_state_hold(coordinator):
        pass
    assert [name for name, _ in durations] == [
        "exomem_boundary_wait_ms",
        "exomem_boundary_hold_ms",
    ], "a quiet reserved-state hold stopped observing its boundary durations"
    assert "exomem_boundary_overdue_total" not in counters

    durations.clear()
    with _reserved_state_hold(coordinator):
        time.sleep(0.05)
    assert "exomem_boundary_overdue_total" in counters, (
        "an overdue quiet hold stopped bumping the overdue counter"
    )
    assert [name for name, _ in durations] == [
        "exomem_boundary_wait_ms",
        "exomem_boundary_hold_ms",
    ]



# --- The shipped thresholds themselves, exercised unpatched ---
#
# Every other test in this group monkeypatched `_QUIET_*` to a convenient
# value, so between them they proved the MECHANISM works and never that the
# SHIPPED numbers are sane.  Both constants could be raised to ten seconds --
# hiding every contended acquire and every slow hold on the box -- with the
# whole suite still green.  These four assert the real values.


def test_the_shipped_hold_threshold_is_small_enough_to_matter(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A hold just above the shipped constant is INFO; a routine one is DEBUG.

    Deliberately tight.  A generous margin here is what let a 100x-too-large
    threshold survive: 50ms against a 5ms bar passes just as happily against a
    10-second bar.  10ms against 5ms does not.
    """
    assert mutation_lock_module._QUIET_HOLD_MS <= 10.0, (
        "the hold threshold has drifted upward; measured uncontended holds are "
        "sub-millisecond, so anything above ~10ms starts hiding real signal"
    )
    vault = tmp_path / "vault"
    vault.mkdir()
    coordinator = VaultMutationCoordinator(tmp_path / "state", vault)

    with caplog.at_level(logging.DEBUG, logger="exomem.mutation_lock"):
        with _reserved_state_hold(coordinator):
            time.sleep(mutation_lock_module._QUIET_HOLD_MS / 1000.0 * 2.0)

    [released] = _events(caplog, "mutation_lock_released")
    assert released.levelno == logging.INFO, (
        f"a hold of {released.fields['hold_ms']}ms was demoted while the "
        f"threshold is {mutation_lock_module._QUIET_HOLD_MS}ms"
    )


def test_a_genuinely_routine_hold_is_still_demoted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The other side of the same bar: no sleep at all stays at DEBUG.

    This is the population the flood came from -- 5,000 sampled uncontended
    reserved-state holds, none of which exceeded 5ms.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    coordinator = VaultMutationCoordinator(tmp_path / "state", vault)

    with caplog.at_level(logging.DEBUG, logger="exomem.mutation_lock"):
        with _reserved_state_hold(coordinator):
            pass

    [released] = _events(caplog, "mutation_lock_released")
    assert released.levelno == logging.DEBUG


def test_the_acquire_threshold_tracks_the_coordinator_poll_interval() -> None:
    """The acquire bar IS the poll interval, not a constant that resembles it.

    Pinning the derivation rather than a number: a wait that outlasted one
    acquisition poll went round the retry loop, and that is what makes it
    contended.  Asserted at three intervals so a hard-coded value cannot
    impersonate the derived one.
    """
    for poll_seconds in (0.005, 0.025, 0.100):
        bar_ms = poll_seconds * 1000.0
        just_under = mutation_lock_module._boundary_event_level(
            publish_holder_metadata=False,
            wait_ms=bar_ms * 0.5,
            hold_ms=0.1,
            poll_interval_seconds=poll_seconds,
        )
        just_over = mutation_lock_module._boundary_event_level(
            publish_holder_metadata=False,
            wait_ms=bar_ms * 1.5,
            hold_ms=0.1,
            poll_interval_seconds=poll_seconds,
        )
        assert just_under == logging.DEBUG, f"poll={poll_seconds}s demoted nothing"
        assert just_over == logging.INFO, (
            f"a wait longer than one {poll_seconds}s poll was demoted to DEBUG"
        )


def test_the_default_acquire_threshold_keeps_the_tens_of_milliseconds_band(
    tmp_path: Path,
) -> None:
    """A default coordinator must report a 55ms wait, not swallow it.

    The exact case a hand-picked 100ms constant hid: 55.65ms is over twice the
    25ms default poll and roughly 1,100x the uncontended median, and it logged
    at DEBUG with zero INFO rows for the whole contended episode.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    coordinator = VaultMutationCoordinator(tmp_path / "state", vault)
    assert coordinator.poll_interval_seconds <= 0.030, (
        "the default poll interval grew; the visible contention band grew with it"
    )
    assert (
        mutation_lock_module._boundary_event_level(
            publish_holder_metadata=False,
            wait_ms=55.65,
            hold_ms=0.5,
            poll_interval_seconds=coordinator.poll_interval_seconds,
        )
        == logging.INFO
    )
