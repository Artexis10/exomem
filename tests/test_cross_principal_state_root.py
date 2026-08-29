"""Cross-principal machine-local state root (issue #933).

The service runs as LocalSystem and the operator's CLI runs as the user, but
the private-DACL model is relative to the CALLING token: a root created by one
principal is `unsafe` to the other. Every operator flow against a service-owned
root therefore fails, and it fails as a traceback rather than as a finding.

These tests pin the three behaviours the issue requires that do not depend on a
deployment decision: a posture inspector that names the cross-principal case, a
doctor that reports it (and unreadable state) as findings rather than crashing,
and CLI maintenance ops that refuse up front instead of part-way through.

Every fixture writes a tmpdir; `conftest._isolate_state_root` injects
`EXOMEM_STATE_ROOT` so nothing here can reach the real user state root.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from exomem.doctor import _REBUILD_TEMP_ORPHAN_FAIL_COUNT
from exomem.kbdir import kb_dirname

windows_only = pytest.mark.skipif(
    os.name != "nt", reason="the private-DACL model is Windows-only"
)

#: The DACL a LocalSystem service writes and validates: SYSTEM + Administrators
#: and nobody else. Applied to a directory the test user owns, this reproduces
#: the incident's root exactly -- the user keeps ownership (so it can be undone)
#: but loses every access the operator flows need.
_SERVICE_PRIVATE_SDDL = "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / kb_dirname()).mkdir(parents=True)
    return root


def _posture(directory: Path, **overrides):
    """A synthetic posture with healthy defaults; override exactly one axis.

    Synthetic on purpose. Every branch of `state.dacl` has to be reachable from
    a test that does not depend on the developer's box being elevated, having a
    service registered, or being Windows at all -- two of these branches
    survived mutation to `"pass"` with zero failures because every test drove
    the same one.
    """
    from exomem import state_paths

    fields = {
        "directory": directory,
        "present": True,
        "accessible": True,
        "unopenable_reason": None,
        "verdict": "private",
        "runtime_verdict": "private",
        "child_verdict": "private",
        "child_sampled": 3,
        "observed": "O:SYD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)",
        "expected": ("S-1-5-21-1-2-3-1001", "SY", "BA"),
        "runtime_expected": ("SY", "BA"),
        "runtime_principal_source": "service:exomem",
        "remediation": "icacls.exe 'X' /reset",
    }
    fields.update(overrides)
    return state_paths.StateRootPosture(**fields)


def _make_service_owned(directory: Path) -> None:
    """Re-ACL *directory* to the SY/BA-only posture a LocalSystem service writes.

    Skips when the calling token can still open the result. That is not a
    workaround: an ELEVATED token is a member of `BUILTIN\\Administrators`, so
    the `BA` ACE genuinely admits it and the cross-principal scenario does not
    exist for that caller. Asserting inaccessibility anyway would make these
    tests pass or fail on whether the runner happened to be elevated. The
    synthetic-posture tests cover the same branches unconditionally.
    """
    from exomem import mutation_lock

    mutation_lock._windows_apply_dacl_sddl(directory, _SERVICE_PRIVATE_SDDL)
    try:
        handle = mutation_lock._windows_open_path(directory, directory=True)
    except OSError:
        return
    mutation_lock._windows_close_handle(handle)
    _restore(directory)
    pytest.skip(
        "running elevated: the BA ACE admits this token, so a SY/BA-only root is "
        "not cross-principal for it"
    )


def _restore(directory: Path) -> None:
    """Give the test user its access back so tmpdir teardown can remove the tree."""
    from exomem import mutation_lock

    mutation_lock._windows_apply_private_dacl(
        directory, mutation_lock._windows_current_user_sid()
    )


# --------------------------------------------------------------------------- #
# 1. The posture inspector names the cross-principal case
# --------------------------------------------------------------------------- #


def test_state_root_posture_accepts_a_root_this_token_created(vault: Path) -> None:
    from exomem import state_paths

    directory = state_paths.ensure_vault_state_dir(vault)

    posture = state_paths.inspect_state_root(vault)

    assert posture.directory == directory
    assert posture.present is True
    assert posture.accessible is True
    assert posture.cross_principal is False


def test_state_root_posture_reports_an_absent_root_as_not_cross_principal(
    vault: Path,
) -> None:
    from exomem import state_paths

    posture = state_paths.inspect_state_root(vault)

    assert posture.present is False
    assert posture.cross_principal is False


@windows_only
def test_windows_state_root_posture_reports_a_service_owned_root_as_cross_principal(
    vault: Path,
) -> None:
    from exomem import state_paths

    directory = state_paths.ensure_vault_state_dir(vault)
    _make_service_owned(directory)
    try:
        posture = state_paths.inspect_state_root(vault)

        assert posture.present is True
        assert posture.accessible is False
        assert posture.cross_principal is True
        # The observed descriptor and the trustees THIS token requires, so a
        # report from another host is actionable without logging into it.
        assert posture.observed is not None
        assert "(A;OICI;FA;;;SY)" in posture.observed
        assert posture.expected
        assert posture.remediation is not None
        assert "icacls" in posture.remediation
        assert str(directory) in posture.remediation
    finally:
        _restore(directory)


@windows_only
def test_windows_a_junction_state_root_is_not_reported_as_cross_principal(
    tmp_path: Path,
) -> None:
    """A reparse point is unopenable for a reason no `icacls` can repair.

    Collapsing every open failure into one state diagnosed a junction as
    "owned by another principal" and handed over an ACL repair that cannot work.
    """
    import subprocess

    from exomem import mutation_lock

    target = tmp_path / "real-target"
    target.mkdir()
    link = tmp_path / "junction"
    # `mklink` is a cmd builtin, so it needs cmd; argv list, never a shell string.
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, check=False,
    )
    if created.returncode != 0 or not os.path.lexists(link):
        pytest.skip("could not create a junction on this filesystem")

    posture = mutation_lock.inspect_windows_private_directory(link)

    assert posture is not None
    assert posture.accessible is False
    assert posture.unopenable_reason == "reparse-point"


# --------------------------------------------------------------------------- #
# 2a. Doctor FAILs on a root DACL the running principal will reject
# --------------------------------------------------------------------------- #


def test_doctor_state_dacl_passes_on_a_root_this_token_owns(vault: Path) -> None:
    from exomem import doctor, state_paths

    state_paths.ensure_vault_state_dir(vault)

    check = doctor._check_state_root_dacl(vault)

    assert check.id == "state.dacl"
    assert check.status == "pass"


def _pin_posture(monkeypatch: pytest.MonkeyPatch, directory: Path, **overrides):
    from exomem import state_paths

    monkeypatch.setattr(
        state_paths, "inspect_state_root", lambda _root: _posture(directory, **overrides)
    )


def test_doctor_state_dacl_fails_when_the_runtime_principal_rejects_the_dacl(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Manifestation 1, seen from the side that matters: the SERVICE's.

    Pins the branch that a mutation to `"pass"` survived with zero failures,
    because every other `state.dacl` test drove the cross-principal branch.
    """
    from exomem import doctor, state_paths

    directory = state_paths.ensure_vault_state_dir(vault)
    _pin_posture(monkeypatch, directory, verdict="private", runtime_verdict="unsafe")

    check = doctor._check_state_root_dacl(vault)

    assert check.id == "state.dacl"
    assert check.status == "fail"
    assert check.remediation is not None and "icacls" in check.remediation
    assert check.details is not None
    assert check.details["state_root"] == str(directory)
    assert check.details["runtime_dacl_verdict"] == "unsafe"


def test_doctor_state_dacl_warns_when_the_descriptor_could_not_be_read(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An un-evaluated trustee set is its own state, never a pass.

    The second branch that survived mutation to `"pass"`.
    """
    from exomem import doctor, state_paths

    directory = state_paths.ensure_vault_state_dir(vault)
    _pin_posture(
        monkeypatch, directory, verdict=None, runtime_verdict=None, observed=None
    )

    check = doctor._check_state_root_dacl(vault)

    assert check.status == "warn"
    assert "unverified" in check.message
    assert check.details is not None
    assert check.details["runtime_dacl_verdict"] is None


def test_doctor_state_dacl_passes_on_a_healthy_service_root_the_caller_cannot_open(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ship blocker: a correct LocalSystem root must not be red to operators.

    Measured, not assumed — a real `D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)` root
    reads `verdict=unsafe, accessible=False` from any operator token. Judging
    cell health by the caller would FAIL every healthy Windows service install,
    including inside `upgrade.ps1`'s own doctor gate, which runs as the operator.
    """
    from exomem import doctor, state_paths

    directory = state_paths.ensure_vault_state_dir(vault)
    _pin_posture(
        monkeypatch,
        directory,
        accessible=False,
        unopenable_reason="access-denied",
        verdict="unsafe",          # the operator's view of a healthy root
        runtime_verdict="private",  # the service's view of the same root
    )

    check = doctor._check_state_root_dacl(vault)

    assert check.status == "pass"
    assert "cannot open it" in check.message
    assert check.details is not None
    assert check.details["accessible"] is False


def test_doctor_state_dacl_fails_on_an_alias_root_without_offering_an_acl_repair(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A junction is a real refusal with no ACL that fixes it."""
    from exomem import doctor, state_paths

    directory = state_paths.ensure_vault_state_dir(vault)
    _pin_posture(
        monkeypatch, directory, accessible=False, unopenable_reason="reparse-point"
    )

    check = doctor._check_state_root_dacl(vault)

    assert check.status == "fail"
    assert "reparse point" in check.message
    assert check.remediation is not None
    assert "icacls" not in check.remediation
    assert "not repairable with an ACL change" in check.remediation


def test_doctor_state_dacl_names_the_path_even_when_the_posture_is_undeterminable(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import doctor, state_paths

    directory = state_paths.vault_state_dir(vault)
    monkeypatch.setattr(
        state_paths,
        "inspect_state_root",
        lambda _root: (_ for _ in ()).throw(OSError("token identity unavailable")),
    )

    check = doctor._check_state_root_dacl(vault)

    assert check.status == "fail"
    assert check.details is not None
    # A finding that cannot name its path is not actionable from a remote report.
    assert check.details["state_root"] == str(directory)


@windows_only
def test_windows_doctor_state_dacl_fails_on_a_service_owned_root_without_raising(
    vault: Path,
) -> None:
    from exomem import doctor, state_paths

    directory = state_paths.ensure_vault_state_dir(vault)
    _make_service_owned(directory)
    try:
        check = doctor._check_state_root_dacl(vault)

        assert check.id == "state.dacl"
        assert check.status == "fail"
        assert check.details is not None
        assert check.details["accessible"] is False
        assert check.remediation is not None and "icacls" in check.remediation
    finally:
        _restore(directory)


def test_doctor_reports_state_dacl_in_its_check_set(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import doctor

    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    report = doctor.doctor(profile="lean")

    assert "state.dacl" in {check.id for check in report.checks}


# --------------------------------------------------------------------------- #
# 2b. Doctor degrades to a finding on state it cannot read
# --------------------------------------------------------------------------- #


class _DeniedPath(type(Path())):  # type: ignore[misc]
    """A path whose stat-backed probes fail the way a foreign DACL makes them."""

    def exists(self, *args: object, **kwargs: object) -> bool:
        raise PermissionError(13, "Access is denied", str(self))

    def stat(self, *args: object, **kwargs: object) -> os.stat_result:
        raise PermissionError(13, "Access is denied", str(self))


def test_doctor_lexical_check_reports_an_unreadable_sidecar_instead_of_raising(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import doctor, lexstore

    denied = _DeniedPath(lexstore.lexical_path(vault))
    monkeypatch.setattr(lexstore, "backend", lambda: "fts5")
    monkeypatch.setattr(lexstore, "fts5_available", lambda: True)
    monkeypatch.setattr(lexstore, "lexical_path", lambda _root: denied)

    check = doctor._check_lexical(vault)

    # Not a crash, and not a confident "the sidecar will be built on first
    # search" either: an inaccessible sidecar is not an absent one, and
    # `os.path.exists` would have reported exactly that false absence.
    assert check.status in {"warn", "fail"}
    assert "could not be inspected" in check.message
    assert "will be built" not in check.message
    assert check.remediation is not None and "state.dacl" in check.remediation


def test_doctor_deferred_backlog_check_survives_an_unreadable_sidecar(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import doctor, lexstore

    denied = _DeniedPath(lexstore.lexical_path(vault))
    monkeypatch.setattr(lexstore, "lexical_path", lambda _root: denied)

    check = doctor._check_deferred_index_backlog(vault)

    assert check.id == "deferred_index_backlog"
    assert check.status in {"pass", "warn", "fail"}


@windows_only
def test_windows_doctor_completes_end_to_end_against_a_service_owned_root(
    vault: Path,
) -> None:
    """No check may crash the run on a root another principal governs.

    Found by driving the real CLI rather than the units: `state.dacl` reported
    the cause correctly and then `rebuild_temp.orphans` killed the process one
    check later on `iterdir`. Hardening only the sites the issue named leaves
    doctor just as unusable during the incident it is meant to diagnose.
    """
    from exomem import doctor, state_paths

    directory = state_paths.ensure_vault_state_dir(vault)
    _make_service_owned(directory)
    try:
        report = doctor.doctor(vault=str(vault), profile="lean")
    finally:
        _restore(directory)

    ids = {check.id for check in report.checks}
    assert "state.dacl" in ids
    assert "rebuild_temp.orphans" in ids
    dacl = next(c for c in report.checks if c.id == "state.dacl")
    assert dacl.status == "fail"
    orphans = next(c for c in report.checks if c.id == "rebuild_temp.orphans")
    # Not a confident "no orphans found" over a directory it could not list.
    assert orphans.status == "warn"
    assert "could not be inspected" in orphans.message


def test_doctor_rebuild_temp_orphans_reports_an_unlistable_state_root(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import doctor, state_paths

    directory = state_paths.ensure_vault_state_dir(vault)

    class _UnlistablePath(type(Path())):  # type: ignore[misc]
        def iterdir(self):
            raise PermissionError(13, "Access is denied", str(self))

    monkeypatch.setattr(
        state_paths, "vault_state_dir", lambda _root: _UnlistablePath(directory)
    )

    check = doctor._check_rebuild_temp_orphans(vault)

    assert check.status == "warn"
    assert "could not be inspected" in check.message
    assert check.details is not None
    assert check.details["unreadable_roots"] == [str(directory)]


def test_doctor_rebuild_temp_orphans_never_downgrades_a_real_leak_to_warn(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An un-scanned root makes a finding less CERTAIN, not less severe.

    Short-circuiting on the unreadable root turned a genuine leak into a warn,
    and `DoctorReport.success` gates on `fail` — so the leak stopped failing the
    run at exactly the moment state was hardest to inspect.
    """
    import time

    from exomem import doctor, state_paths
    from exomem import vault as vault_module
    from exomem.kbdir import kb_dirname

    state_paths.ensure_vault_state_dir(vault)
    stale = time.time() - vault_module.REBUILD_TEMP_STALE_AGE_SECONDS - 60
    kb = vault / kb_dirname()
    for index in range(_REBUILD_TEMP_ORPHAN_FAIL_COUNT + 1):
        name = f".graph-rebuild-{index:064x}-{index:024x}.sqlite"
        # Asserted against the canonical matcher rather than trusting the shape:
        # a name this check does not recognise would make the test vacuous.
        assert vault_module.is_graph_rebuild_runtime_file_name(name)
        orphan = kb / name
        orphan.write_bytes(b"x" * (12 * 1024 * 1024))
        os.utime(orphan, (stale, stale))

    class _UnlistablePath(type(Path())):  # type: ignore[misc]
        def iterdir(self):
            raise PermissionError(13, "Access is denied", str(self))

    unlistable = _UnlistablePath(state_paths.vault_state_dir(vault))
    monkeypatch.setattr(state_paths, "vault_state_dir", lambda _root: unlistable)

    check = doctor._check_rebuild_temp_orphans(vault)

    assert check.status == "fail", "an unreadable root must not mask a real leak"
    assert "could not be inspected" in check.message
    assert "this count is a floor" in check.message


# --------------------------------------------------------------------------- #
# 3. CLI maintenance refuses up front instead of crashing part-way through
# --------------------------------------------------------------------------- #


def _cross_principal_posture(directory: Path):
    return _posture(
        directory,
        accessible=False,
        unopenable_reason="access-denied",
        verdict="unsafe",
        runtime_verdict="private",
    )


def _openable_but_unsafe_posture(directory: Path):
    """The half the first pre-flight missed — and the half the service was on.

    LocalSystem holds an `SY` full-access ACE on a `user+SY+BA` root, so it OPENS
    the root fine and then rejects the trustee set at every private-state
    boundary behind it.
    """
    return _posture(directory, accessible=True, verdict="unsafe", runtime_verdict="unsafe")


def test_maintain_refuses_a_cross_principal_state_root_before_any_work(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import commands, state_paths
    from exomem.cli_ops import OpError

    directory = state_paths.ensure_vault_state_dir(vault)
    monkeypatch.setattr(
        state_paths, "inspect_state_root", lambda _root: _cross_principal_posture(directory)
    )
    reached = []
    monkeypatch.setattr(
        commands,
        "op_reconcile",
        lambda *a, **k: reached.append(True),
    )

    with pytest.raises(OpError) as raised:
        commands.op_maintain_memory(vault, mode="reconcile", rebuild_graph=True)

    assert raised.value.code == "STATE_ROOT_CROSS_PRINCIPAL"
    assert raised.value.remediation is not None
    assert "icacls" in raised.value.remediation
    assert reached == [], "the refusal must precede every unit of maintenance work"


def test_maintain_refuses_an_openable_but_unsafe_state_root_before_any_work(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H1: gating only on "cannot open" let the SERVICE's failure straight through.

    A LocalSystem service opens a `user+SY+BA` root through its own `SY` ACE, so
    `cross_principal` is False — and `maintain` then proceeded into
    `ensure_vault_state_dir` and died there with a raw `WindowsRuntimeDaclError`,
    which is the exact failure this pre-flight exists to remove.
    """
    from exomem import commands, state_paths
    from exomem.cli_ops import OpError

    directory = state_paths.ensure_vault_state_dir(vault)
    monkeypatch.setattr(
        state_paths,
        "inspect_state_root",
        lambda _root: _openable_but_unsafe_posture(directory),
    )
    reached = []
    monkeypatch.setattr(commands, "op_reconcile", lambda *a, **k: reached.append(True))

    with pytest.raises(OpError) as raised:
        commands.op_maintain_memory(vault, mode="reconcile", rebuild_graph=True)

    assert raised.value.code == "STATE_ROOT_CROSS_PRINCIPAL"
    assert "private-state validator rejects" in str(raised.value)
    assert reached == [], "the refusal must precede every unit of maintenance work"


def test_maintain_does_not_call_an_alias_root_a_cross_principal_one(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A junction is unopenable, but not by a principal — and no `icacls` fixes it.

    Pins `cross_principal` to the access-denied reason specifically. Widening it
    back to "any open failure" hands the operator an ACL repair for a path
    problem, which is the shape medium finding 2 describes.
    """
    from exomem import commands, state_paths

    directory = state_paths.ensure_vault_state_dir(vault)
    monkeypatch.setattr(
        state_paths,
        "inspect_state_root",
        lambda _root: _posture(
            directory, accessible=False, unopenable_reason="reparse-point"
        ),
    )
    monkeypatch.setattr(commands, "op_audit", lambda *a, **k: {"ran": True})

    posture = state_paths.inspect_state_root(vault)
    assert posture.cross_principal is False
    assert posture.blocks_current_token is False
    # And so the DACL pre-flight stays silent: the alias is refused at the
    # private-state boundary that can actually describe it, and `state.dacl`
    # reports it without offering an ACL repair.
    assert commands.op_maintain_memory(vault, mode="audit") == {"ran": True}


def test_maintain_reports_a_stable_code_when_its_own_preflight_cannot_inspect(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard must not emit the kind of raw error it exists to replace."""
    from exomem import commands, state_paths
    from exomem.cli_ops import OpError

    monkeypatch.setattr(
        state_paths,
        "inspect_state_root",
        lambda _root: (_ for _ in ()).throw(
            ValueError("EXOMEM_STATE_ROOT must be an absolute path")
        ),
    )

    with pytest.raises(OpError) as raised:
        commands.op_maintain_memory(vault, mode="audit")

    assert raised.value.code == "STATE_ROOT_UNREADABLE"


def test_maintain_audit_also_refuses_a_cross_principal_state_root(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import commands, state_paths
    from exomem.cli_ops import OpError

    directory = state_paths.ensure_vault_state_dir(vault)
    monkeypatch.setattr(
        state_paths, "inspect_state_root", lambda _root: _cross_principal_posture(directory)
    )

    with pytest.raises(OpError) as raised:
        commands.op_maintain_memory(vault, mode="audit")

    assert raised.value.code == "STATE_ROOT_CROSS_PRINCIPAL"


def test_maintain_runs_normally_when_the_state_root_is_this_token_s(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import commands, state_paths

    state_paths.ensure_vault_state_dir(vault)
    monkeypatch.setattr(commands, "op_reconcile", lambda *a, **k: {"ran": True})

    assert commands.op_maintain_memory(vault, mode="reconcile") == {"ran": True}


def test_offline_state_migration_refuses_a_cross_principal_state_root(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import state_migration, state_paths

    directory = state_paths.ensure_vault_state_dir(vault)
    monkeypatch.setattr(
        state_paths, "inspect_state_root", lambda _root: _cross_principal_posture(directory)
    )
    monkeypatch.setattr(
        state_migration,
        "_resolve_locked",
        lambda *a, **k: pytest.fail("migration must refuse before taking the lock"),
    )

    authority = state_migration.assert_offline_migration_authority(source="test")
    with pytest.raises(state_paths.StateRootAccessDenied) as raised:
        state_migration.migrate_vault_state_offline(vault, authority=authority)

    # The stable code, not a substring of the prose: `upgrade.ps1` consumes the
    # JSON envelope this reaches, and a message-only envelope cannot be branched on.
    assert raised.value.code == "STATE_ROOT_CROSS_PRINCIPAL"
    assert "icacls" in (raised.value.remediation or "")


def test_offline_migration_json_envelope_carries_the_stable_code(
    vault: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    import json

    from exomem import __main__ as main_module
    from exomem import state_paths

    directory = state_paths.ensure_vault_state_dir(vault)
    monkeypatch.setattr(
        main_module,
        "_run_offline_state_migration",
        lambda **_k: (_ for _ in ()).throw(
            state_paths.StateRootAccessDenied(_cross_principal_posture(directory))
        ),
    )
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))

    exit_code = main_module._simple_maintain_main(["--migrate-state", "--offline", "--json"])

    assert exit_code == 1
    envelope = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert envelope["success"] is False
    assert envelope["error"]["code"] == "STATE_ROOT_CROSS_PRINCIPAL"


# --------------------------------------------------------------------------- #
# 4. Runtime-principal resolution (requirement 1)
# --------------------------------------------------------------------------- #


@windows_only
def test_runtime_principal_is_the_current_token_without_a_service_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No service install leaves single-principal behaviour byte-identical."""
    from exomem import mutation_lock

    monkeypatch.delenv("EXOMEM_RUNTIME_PRINCIPAL", raising=False)
    monkeypatch.setattr(mutation_lock, "_windows_service_object_name", lambda _n: None)

    principal = mutation_lock.resolve_windows_runtime_principal()

    assert principal.sid == mutation_lock._windows_current_user_sid()
    assert principal.source == "current-token"
    assert principal.authoritative is False


@windows_only
def test_runtime_principal_reads_the_service_account_from_the_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import mutation_lock

    monkeypatch.delenv("EXOMEM_RUNTIME_PRINCIPAL", raising=False)
    monkeypatch.setattr(
        mutation_lock,
        "_windows_service_object_name",
        lambda name: "LocalSystem" if name == "exomem" else None,
    )

    principal = mutation_lock.resolve_windows_runtime_principal()

    assert principal.sid == "S-1-5-18"
    assert principal.source == "service:exomem"
    assert principal.authoritative is True


@windows_only
def test_runtime_principal_degrades_with_a_named_reason_when_unresolvable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unresolvable account must never become a guessed principal.

    Sealing to the wrong principal locks out the operator AND the service at
    once, which is strictly worse than not sealing at all.
    """
    from exomem import mutation_lock

    monkeypatch.delenv("EXOMEM_RUNTIME_PRINCIPAL", raising=False)
    monkeypatch.setattr(
        mutation_lock, "_windows_service_object_name", lambda _n: "NoSuchAccount"
    )
    monkeypatch.setattr(mutation_lock, "_windows_sid_for_account", lambda _a: None)

    principal = mutation_lock.resolve_windows_runtime_principal()

    assert principal.sid == mutation_lock._windows_current_user_sid()
    assert principal.source.startswith("current-token (")
    assert "unresolvable" in principal.source
    # Load-bearing: a degraded principal must not authorise re-ACLing anything.
    assert principal.authoritative is False


@windows_only
def test_seal_is_a_noop_when_the_runtime_principal_is_the_current_token(
    vault: Path,
) -> None:
    from exomem import mutation_lock, state_paths

    directory = state_paths.ensure_vault_state_dir(vault)
    before = mutation_lock._windows_dacl_sddl(directory)

    assert state_paths.seal_state_root_for_runtime_principal(vault) is None
    assert mutation_lock._windows_dacl_sddl(directory) == before


@windows_only
def test_an_unresolvable_principal_leaves_the_dacl_completely_untouched(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Degrade, never guess. A garbage override falls back to the current token,
    which makes the seal a no-op rather than a rewrite to the wrong principal."""
    from exomem import mutation_lock, state_paths

    directory = state_paths.ensure_vault_state_dir(vault)
    before = mutation_lock._windows_dacl_sddl(directory)
    monkeypatch.setenv("EXOMEM_RUNTIME_PRINCIPAL", "not-a-sid")

    assert state_paths.seal_state_root_for_runtime_principal(vault) is None
    assert mutation_lock._windows_dacl_sddl(directory) == before
    # And the degradation is visible rather than indistinguishable from clean.
    principal = mutation_lock.resolve_windows_runtime_principal()
    assert "is not a SID" in principal.source
    assert principal.authoritative is False


@windows_only
def test_seal_refuses_a_degraded_principal_rather_than_re_acling_on_a_guess(
    vault: Path,
) -> None:
    """The guard for a caller that hands in a principal it could not establish."""
    from exomem import mutation_lock, state_paths

    directory = state_paths.ensure_vault_state_dir(vault)
    before = mutation_lock._windows_dacl_sddl(directory)
    degraded = mutation_lock.WindowsRuntimePrincipal(
        sid="S-1-5-18", source="current-token (registry unreadable)"
    )

    with pytest.raises(mutation_lock.WindowsRuntimePrincipalUnresolved):
        mutation_lock.seal_windows_state_root_for_runtime_principal(
            directory, runtime_principal=degraded
        )

    assert mutation_lock._windows_dacl_sddl(directory) == before


@windows_only
def test_seal_refuses_to_re_acl_a_root_this_process_does_not_own(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never re-ACL a root you do not own — an explicit invariant of #933."""
    from exomem import mutation_lock, state_paths

    directory = state_paths.ensure_vault_state_dir(vault)
    monkeypatch.setattr(
        mutation_lock, "_windows_owner_admits_current_user", lambda _o, _s: False
    )
    before = mutation_lock._windows_dacl_sddl(directory)

    with pytest.raises(mutation_lock.WindowsRuntimePrincipalUnresolved) as raised:
        mutation_lock.seal_windows_state_root_for_runtime_principal(
            directory,
            runtime_principal=mutation_lock.WindowsRuntimePrincipal(
                sid="S-1-5-18", source="service:exomem"
            ),
        )

    assert "does not own" in str(raised.value)
    assert mutation_lock._windows_dacl_sddl(directory) == before


@windows_only
def test_seal_leaves_a_root_the_runtime_principals_own_validator_accepts(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requirement 1, asserted with the validator itself rather than an SDDL literal.

    The seal is caught rather than allowed to propagate. `conftest` converts a
    `WindowsRuntimeDaclError` into a SKIP (it is in
    `_HOSTED_POSIX_OWNERSHIP_REFUSAL`), so letting the fail-closed path raise
    out of here would turn a real regression in this Windows-only code into a
    silent skip on the only platform that runs it.
    """
    from exomem import mutation_lock, state_paths

    directory = state_paths.ensure_vault_state_dir(vault)
    monkeypatch.setenv("EXOMEM_RUNTIME_PRINCIPAL", "S-1-5-18")
    try:
        sealed = None
        raised: str | None = None
        try:
            sealed = state_paths.seal_state_root_for_runtime_principal(vault)
        except Exception as error:  # noqa: BLE001 - see the docstring
            raised = repr(error)
        # Outside the handler on purpose: raising inside it would leave the
        # refusal as this failure's `__context__`, and conftest's skip walker
        # follows that chain -- which is how the mutant first came back "skipped".
        if raised is not None:
            pytest.fail(f"seal did not apply the runtime principal's DACL: {raised}")

        assert sealed is not None and sealed.sid == "S-1-5-18"
        observed = mutation_lock._windows_dacl_sddl(directory)
        assert mutation_lock._windows_private_dacl_is_valid(
            observed, "S-1-5-18", directory=True
        ), observed
        # And it is genuinely no longer this token's: the two views disagree,
        # which is the whole point of resolving a runtime principal.
        assert not mutation_lock._windows_private_dacl_is_valid(
            observed, mutation_lock._windows_current_user_sid(), directory=True
        )
    finally:
        _restore(directory)


@windows_only
def test_offline_migration_seals_the_root_for_the_service_principal(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End of requirement 1: the migration is what leaves the root correct."""
    from exomem import mutation_lock, state_migration, state_paths

    directory = state_paths.vault_state_dir(vault)
    monkeypatch.setenv("EXOMEM_RUNTIME_PRINCIPAL", "S-1-5-18")
    try:
        authority = state_migration.assert_offline_migration_authority(source="test")
        state_migration.migrate_vault_state_offline(vault, authority=authority)

        observed = mutation_lock._windows_dacl_sddl(directory)
        assert mutation_lock._windows_private_dacl_is_valid(
            observed, "S-1-5-18", directory=True
        ), observed
    finally:
        _restore(directory)


@windows_only
def test_windows_maintain_refuses_a_real_service_owned_state_root(vault: Path) -> None:
    from exomem import commands, state_paths
    from exomem.cli_ops import OpError

    directory = state_paths.ensure_vault_state_dir(vault)
    _make_service_owned(directory)
    try:
        with pytest.raises(OpError) as raised:
            commands.op_maintain_memory(vault, mode="reconcile", rebuild_graph=True)

        assert raised.value.code == "STATE_ROOT_CROSS_PRINCIPAL"
        assert str(directory) in (raised.value.remediation or "")
    finally:
        _restore(directory)


# --------------------------------------------------------------------------- #
# Round 3: the seal's scope, its reach, and its proof
# --------------------------------------------------------------------------- #


@windows_only
def test_seal_refuses_a_vault_the_service_is_not_bound_to(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H1. A machine having a service says nothing about an unrelated vault.

    Without this, `exomem init` of a brand-new vault on a box that happens to
    have a service registered handed its state root to LocalSystem and locked
    the operator out of the directory it had just made for them.
    """
    from exomem import mutation_lock, state_paths

    directory = state_paths.ensure_vault_state_dir(vault)
    before = mutation_lock._windows_dacl_sddl(directory)
    monkeypatch.delenv("EXOMEM_RUNTIME_PRINCIPAL", raising=False)
    monkeypatch.setattr(
        mutation_lock, "_windows_service_object_name",
        lambda name: "LocalSystem" if name == "exomem" else None,
    )
    monkeypatch.setattr(
        mutation_lock, "_windows_service_bound_vault",
        lambda _name: str(vault.parent / "some-other-vault"),
    )

    assert state_paths.seal_state_root_for_runtime_principal(vault) is None
    assert mutation_lock._windows_dacl_sddl(directory) == before
    # The operator keeps the access they had before, which is the whole point.
    assert os.listdir(directory) is not None


@windows_only
def test_seal_accepts_the_vault_the_service_is_bound_to(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other side of H1: the upgrade path must still seal."""
    from exomem import mutation_lock, state_paths

    directory = state_paths.ensure_vault_state_dir(vault)
    monkeypatch.delenv("EXOMEM_RUNTIME_PRINCIPAL", raising=False)
    monkeypatch.setattr(
        mutation_lock, "_windows_service_object_name",
        lambda name: "LocalSystem" if name == "exomem" else None,
    )
    # A different spelling of the same vault, to prove the comparison normalises
    # rather than string-matches.
    monkeypatch.setattr(
        mutation_lock, "_windows_service_bound_vault",
        lambda _name: str(vault).upper(),
    )
    try:
        sealed = state_paths.seal_state_root_for_runtime_principal(vault)
        assert sealed is not None and sealed.sid == "S-1-5-18"
    finally:
        _restore(directory)


@windows_only
def test_seal_refuses_a_binding_it_could_not_read(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable binding is "do not act", never "no binding exists"."""
    from exomem import mutation_lock, state_paths

    directory = state_paths.ensure_vault_state_dir(vault)
    before = mutation_lock._windows_dacl_sddl(directory)
    monkeypatch.delenv("EXOMEM_RUNTIME_PRINCIPAL", raising=False)
    monkeypatch.setattr(
        mutation_lock, "_windows_service_object_name",
        lambda name: "LocalSystem" if name == "exomem" else None,
    )
    monkeypatch.setattr(mutation_lock, "_windows_service_bound_vault", lambda _n: None)

    assert state_paths.seal_state_root_for_runtime_principal(vault) is None
    assert mutation_lock._windows_dacl_sddl(directory) == before


@windows_only
def test_seal_protects_the_state_inside_the_root_not_just_the_entry(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H2. A root that merely LOOKS sealed is worse than an honestly unsealed one.

    Protecting the directory alone makes Windows convert each child's inherited
    ACEs into explicit ones, so every file the migration just moved in stayed
    readable and writable by the operator behind a directory that refused a
    listing -- and `state.dacl` reported pass.
    """
    from exomem import mutation_lock, state_paths

    directory = state_paths.ensure_vault_state_dir(vault)
    child = directory / "state.sqlite"
    child.write_bytes(b"PRE-EXISTING SECRET STATE")
    monkeypatch.setenv("EXOMEM_RUNTIME_PRINCIPAL", "S-1-5-18")
    seal_error = None
    reachable = []
    try:
        try:
            sealed = state_paths.seal_state_root_for_runtime_principal(vault)
        except Exception as error:  # noqa: BLE001 - conftest skips on this type
            seal_error, sealed = type(error).__name__, None
        for name, probe in (
            ("read", lambda: child.read_bytes()),
            ("write", lambda: child.open("ab").close()),
            ("stat", lambda: child.stat()),
        ):
            try:
                probe()
                reachable.append(name)
            except PermissionError:
                pass
        observed = mutation_lock._windows_dacl_sddl(child)
    finally:
        mutation_lock._windows_apply_private_dacl(
            directory, mutation_lock._windows_current_user_sid(), propagate=True
        )
    # Asserted outside every handler: conftest converts `WindowsRuntimeDaclError`
    # into a SKIP and follows `__context__` to do it (issue #952), so a raise in
    # here would hide a regression instead of failing on it.
    assert seal_error is None, f"seal raised {seal_error}"
    assert sealed is not None
    assert reachable == [], f"operator can still {reachable} inside a sealed root"
    # The child's own DACL no longer grants this token. Compared against the DACL
    # section only: the owner field still names the creating account, and
    # ownership is not access.
    assert mutation_lock._windows_current_user_sid() not in observed.split("D:", 1)[1]


def test_doctor_state_dacl_fails_when_children_are_not_private(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reporting half of H2: a private root over non-private state is a FAIL."""
    from exomem import doctor, state_paths

    directory = state_paths.ensure_vault_state_dir(vault)
    _pin_posture(monkeypatch, directory, runtime_verdict="private",
                 child_verdict="unsafe")

    check = doctor._check_state_root_dacl(vault)

    assert check.status == "fail"
    assert "state INSIDE it is not" in check.message
    assert check.details is not None
    assert check.details["child_dacl_verdict"] == "unsafe"


@windows_only
def test_seal_refuses_an_aliased_state_root(tmp_path: Path) -> None:
    """Item 4. The seal is the only DACL write that never opened a handle."""
    import subprocess

    from exomem import mutation_lock

    target = tmp_path / "real-target"
    target.mkdir()
    link = tmp_path / "junction"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, check=False,
    )
    if created.returncode != 0 or not os.path.lexists(link):
        pytest.skip("could not create a junction on this filesystem")
    before = mutation_lock._windows_dacl_sddl(target)

    with pytest.raises(mutation_lock.WindowsReparsePointError):
        mutation_lock.seal_windows_state_root_for_runtime_principal(
            link,
            runtime_principal=mutation_lock.WindowsRuntimePrincipal(
                sid="S-1-5-18", source="pinned:S-1-5-18"
            ),
        )

    # The real root behind the junction was never touched.
    assert mutation_lock._windows_dacl_sddl(target) == before


@windows_only
def test_seal_proves_the_write_took(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """M1. The seal's own fail-closed proof, which survived round 2's mutation.

    A seal that reports success without checking is how a root gets recorded as
    sealed while carrying whatever the write actually left behind.
    """
    from exomem import mutation_lock, state_paths

    directory = state_paths.ensure_vault_state_dir(vault)
    monkeypatch.setenv("EXOMEM_RUNTIME_PRINCIPAL", "S-1-5-18")
    # The write silently does nothing; only the post-write validation can catch it.
    monkeypatch.setattr(
        mutation_lock, "_windows_apply_private_dacl", lambda *a, **k: None
    )
    raised = None
    try:
        state_paths.seal_state_root_for_runtime_principal(vault)
    except Exception as error:  # noqa: BLE001 - conftest skips on this type
        raised = type(error).__name__
    finally:
        _restore(directory)
    # Asserted outside the handler: conftest's skip walker follows __context__
    # and would turn this failure into a silent skip (issue #952).
    assert raised == "WindowsRuntimeDaclError", raised


def test_maintain_refuses_an_unopenable_root_of_unknown_cause(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 3. Narrowing `cross_principal` must not leave a passable residual.

    A sharing violation or a not-ready device is un-evaluated, and un-evaluated
    is not safe: round 1 refused on any `accessible=False` and that floor holds.
    """
    from exomem import commands, state_paths
    from exomem.cli_ops import OpError

    directory = state_paths.ensure_vault_state_dir(vault)
    monkeypatch.setattr(
        state_paths, "inspect_state_root",
        lambda _root: _posture(
            directory, accessible=False, unopenable_reason="unopenable"
        ),
    )

    posture = state_paths.inspect_state_root(vault)
    assert posture.cross_principal is False
    assert posture.blocks_current_token is True

    with pytest.raises(OpError) as raised:
        commands.op_maintain_memory(vault, mode="audit")
    assert raised.value.code == "STATE_ROOT_CROSS_PRINCIPAL"
    assert "unverified" in str(raised.value)


def test_doctor_withholds_a_token_relative_repair_when_the_principal_is_unresolved(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 2. A repair for the wrong principal restarts the ACL ping-pong."""
    from exomem import doctor, state_paths

    directory = state_paths.ensure_vault_state_dir(vault)
    _pin_posture(
        monkeypatch, directory,
        runtime_verdict="unsafe",
        runtime_principal_source="current-token (service 'exomem' account is unresolvable)",
    )

    check = doctor._check_state_root_dacl(vault)

    assert check.status == "fail"
    assert check.remediation is not None
    assert "icacls" not in check.remediation
    assert "could not be established" in check.remediation


def test_a_pinned_current_token_is_not_read_as_a_degraded_principal(
    vault: Path,
) -> None:
    """`pinned:` and `current-token (...)` must stay distinguishable.

    The conftest pins `current-token` for isolation; if that read as a
    degradation, every test would exercise the withheld-repair path instead of
    the real one.
    """
    from exomem import state_paths

    state_paths.ensure_vault_state_dir(vault)
    posture = state_paths.inspect_state_root(vault)

    assert posture.runtime_principal_unresolved is False


@windows_only
def test_the_real_registry_readers_answer_in_the_expected_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the real SCM readers, asserting only on shape.

    Box-independent by construction: this machine may or may not have a service
    registered, and either answer is valid. What must hold is that the readers
    run against the real registry without raising and produce a `source` this
    module's own consumers can branch on.
    """
    from exomem import mutation_lock

    # The conftest pins this for isolation; drop it so the REAL readers run.
    monkeypatch.delenv("EXOMEM_RUNTIME_PRINCIPAL", raising=False)
    principal = mutation_lock.resolve_windows_runtime_principal()

    assert principal.sid.startswith("S-1-")
    # The degraded form is admitted too: on a box whose service account is
    # unresolvable, `current-token (...)` is the CORRECT answer, and a test
    # that rejects it is exactly the box-dependency it was written to avoid.
    assert (
        principal.source.startswith("service:")
        or principal.source == "current-token"
        or principal.source.startswith("current-token (")
    ), principal.source
    if principal.source.startswith("service:"):
        assert principal.bound_vault is None or isinstance(principal.bound_vault, str)
    else:
        assert principal.bound_vault is None


@windows_only
def test_the_child_sampler_reports_a_foreign_ace_on_a_private_root(
    tmp_path: Path,
) -> None:
    """H2's detector, exercised for real rather than through a synthetic posture.

    A root can be `private` while a single child inside it admits a foreign
    trustee, which is exactly the shape a non-propagating seal leaves behind.
    """
    from exomem import mutation_lock

    sid = mutation_lock._windows_current_user_sid()
    root = tmp_path / "root"
    root.mkdir()
    mutation_lock._windows_apply_private_dacl(root, sid)
    (root / "good.sqlite").write_bytes(b"x")

    assert mutation_lock._windows_child_dacl_verdict(root, sid) == ("private", 1)

    # `WD` is Everyone: a trustee outside the private set, on one child only.
    # Named to sort AFTER the clean one deliberately -- an aggregation that only
    # escalates on the first child it sees would miss exactly this.
    bad = root / "zzz-bad.sqlite"
    bad.write_bytes(b"x")
    mutation_lock._windows_apply_dacl_sddl(
        bad, f"D:P(A;;FA;;;{sid})(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;WD)"
    )

    assert mutation_lock._windows_child_dacl_verdict(root, sid)[0] == "unsafe"
    # The root itself still reads clean -- which is why the root descriptor
    # alone cannot answer whether the state inside it is private.
    assert mutation_lock._windows_private_dacl_verdict(
        mutation_lock._windows_dacl_sddl(root), sid, directory=True
    ) == "private"


# --------------------------------------------------------------------------- #
# Round 4: say what was not checked, prove what was claimed, degrade honestly
# --------------------------------------------------------------------------- #


def test_doctor_pass_says_so_when_the_contents_were_not_evaluated(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MEDIUM 1. A sealed root denies the operator's listing, so nothing inside
    it is examined -- and a PASS that asserts privacy unqualified is exactly the
    confidently-green report that cost a day in the original incident."""
    from exomem import doctor, state_paths

    directory = state_paths.ensure_vault_state_dir(vault)
    _pin_posture(
        monkeypatch, directory,
        accessible=False, unopenable_reason="access-denied",
        verdict="unsafe", runtime_verdict="private",
        child_verdict=None, child_sampled=0,
    )

    check = doctor._check_state_root_dacl(vault)

    assert check.status == "pass"
    assert "Contents were NOT evaluated" in check.message
    assert check.details is not None
    assert check.details["child_sampled"] == 0


def test_doctor_pass_names_the_sample_size_when_contents_were_evaluated(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import doctor, state_paths

    directory = state_paths.ensure_vault_state_dir(vault)
    _pin_posture(monkeypatch, directory, child_verdict="private", child_sampled=4)

    check = doctor._check_state_root_dacl(vault)

    assert check.status == "pass"
    assert "Contents verified across 4 sampled child(ren)" in check.message
    assert "NOT evaluated" not in check.message


@windows_only
def test_seal_fails_when_a_child_resists_the_propagation(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MEDIUM 2. The seal's proof must cover the contents it claims to have fixed.

    A child directory carrying its own PROTECTED DACL does not inherit, so the
    propagation genuinely does not reach it. Checking only the root reported
    success over exactly that.
    """
    from exomem import mutation_lock, state_paths

    directory = state_paths.ensure_vault_state_dir(vault)
    stubborn = directory / "stubborn"
    stubborn.mkdir()
    # Protected against inheritance and naming a trustee SYSTEM's validator
    # rejects, so propagation cannot reach it and it stays `unsafe`.
    mutation_lock._windows_apply_dacl_sddl(
        stubborn,
        f"D:P(A;OICI;FA;;;{mutation_lock._windows_current_user_sid()})"
        "(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)",
    )
    monkeypatch.setenv("EXOMEM_RUNTIME_PRINCIPAL", "S-1-5-18")

    raised = None
    try:
        state_paths.seal_state_root_for_runtime_principal(vault)
    except Exception as error:  # noqa: BLE001 - conftest skips on this type
        raised = (type(error).__name__, str(error))
    finally:
        mutation_lock._windows_apply_private_dacl(
            directory, mutation_lock._windows_current_user_sid(), propagate=True
        )
    # Asserted outside the handler: conftest's skip walker follows __context__
    # and would turn this failure into a silent skip (issue #952).
    assert raised is not None, "the seal reported success over unsealed contents"
    assert raised[0] == "WindowsRuntimeDaclError"
    assert "contents remain" in raised[1]


@windows_only
def test_a_failed_registry_read_degrades_rather_than_reading_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MEDIUM 3. "Read failed" and "no service installed" are different facts.

    Collapsed, a correctly-sealed root gets a `current-token` principal, a FAIL,
    and an `icacls` that grants the operator and breaks the service.
    """
    from exomem import mutation_lock

    monkeypatch.delenv("EXOMEM_RUNTIME_PRINCIPAL", raising=False)

    def _denied(_name):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(mutation_lock, "_windows_service_object_name", _denied)

    principal = mutation_lock.resolve_windows_runtime_principal()

    assert principal.source.startswith("current-token (")
    assert "registry read failed" in principal.source
    assert principal.authoritative is False


@windows_only
def test_an_absent_service_is_not_reported_as_a_failed_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other side of MEDIUM 3: a genuinely absent service stays clean."""
    import winreg

    from exomem import mutation_lock

    monkeypatch.delenv("EXOMEM_RUNTIME_PRINCIPAL", raising=False)

    def _absent(*_args, **_kwargs):
        error = OSError(2, "The system cannot find the file specified")
        error.winerror = 2  # ERROR_FILE_NOT_FOUND
        raise error

    monkeypatch.setattr(winreg, "OpenKey", _absent)

    principal = mutation_lock.resolve_windows_runtime_principal()

    # Clean `current-token`, with no parenthesised degradation reason: nothing
    # failed, there is simply no service here.
    assert principal.source == "current-token"
    assert principal.authoritative is False


@windows_only
def test_sealing_an_already_sealed_root_is_a_no_op(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LOW. A seal has to be idempotent; the alias guard made the second call
    raise a raw `[Errno 5]`."""
    from exomem import mutation_lock, state_paths

    directory = state_paths.ensure_vault_state_dir(vault)
    monkeypatch.setenv("EXOMEM_RUNTIME_PRINCIPAL", "S-1-5-18")
    try:
        assert state_paths.seal_state_root_for_runtime_principal(vault) is not None
        sealed = mutation_lock._windows_dacl_sddl(directory)

        # Second call: no exception, no change.
        assert state_paths.seal_state_root_for_runtime_principal(vault) is None
        assert mutation_lock._windows_dacl_sddl(directory) == sealed
    finally:
        mutation_lock._windows_apply_private_dacl(
            directory, mutation_lock._windows_current_user_sid(), propagate=True
        )


@windows_only
def test_a_skipped_seal_distinguishes_wrong_vault_from_unreadable_binding(
    vault: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """LOW. "Bound elsewhere" is correct and expected; "binding unreadable" is a
    silent miss on the upgrade path. They must not look alike in a log."""
    import logging

    from exomem import mutation_lock, state_paths

    state_paths.ensure_vault_state_dir(vault)
    monkeypatch.delenv("EXOMEM_RUNTIME_PRINCIPAL", raising=False)
    monkeypatch.setattr(
        mutation_lock, "_windows_service_object_name",
        lambda name: "LocalSystem" if name == "exomem" else None,
    )

    monkeypatch.setattr(
        mutation_lock, "_windows_service_bound_vault",
        lambda _n: str(vault.parent / "elsewhere"),
    )
    with caplog.at_level(logging.INFO, logger="exomem.mutation_lock"):
        assert state_paths.seal_state_root_for_runtime_principal(vault) is None
    assert any("bound to a different vault" in r.message for r in caplog.records)
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)

    caplog.clear()
    monkeypatch.setattr(mutation_lock, "_windows_service_bound_vault", lambda _n: None)
    with caplog.at_level(logging.INFO, logger="exomem.mutation_lock"):
        assert state_paths.seal_state_root_for_runtime_principal(vault) is None
    assert any("could not be read" in r.message for r in caplog.records)
    # A silent miss on the upgrade path warrants a WARNING, not an INFO.
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


@windows_only
def test_the_real_object_name_reader_distinguishes_absent_from_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MEDIUM 3 at the reader itself, not through a stubbed caller.

    The degrade test monkeypatches `_windows_service_object_name`, so it never
    executes the branch that tells the two apart. This one does.
    """
    import winreg

    from exomem import mutation_lock

    def _raising(winerror: int):
        def _open(*_args, **_kwargs):
            error = OSError(winerror, "synthetic")
            error.winerror = winerror
            raise error

        return _open

    # ERROR_FILE_NOT_FOUND: the service is genuinely absent -> None, no raise.
    monkeypatch.setattr(winreg, "OpenKey", _raising(2))
    assert mutation_lock._windows_service_object_name("exomem") is None

    # ERROR_ACCESS_DENIED: the read FAILED -> raises, so the caller can degrade.
    monkeypatch.setattr(winreg, "OpenKey", _raising(5))
    with pytest.raises(OSError):
        mutation_lock._windows_service_object_name("exomem")


@windows_only
def test_the_child_sampler_judges_a_subdirectory_with_the_directory_flag_set(
    tmp_path: Path,
) -> None:
    """LOW. `Path.is_dir()` swallows `OSError`, so it belongs inside the guard —
    and a child DIRECTORY must be judged as one.

    An INHERITING subdirectory is the distinguishing shape, and the ordinary one:
    its `OICIID` flags read `inherited` under the directory flag-set and `unsafe`
    under the file one. (A subdirectory with its own protected `OICI` DACL is
    `private` either way, so it cannot tell the two apart.)
    """
    from exomem import mutation_lock

    sid = mutation_lock._windows_current_user_sid()
    root = tmp_path / "root"
    root.mkdir()
    mutation_lock._windows_apply_private_dacl(root, sid)
    child_dir = root / "subdir"
    child_dir.mkdir()  # inherits the root's ACEs, as every real one does

    verdict, sampled = mutation_lock._windows_child_dacl_verdict(root, sid)

    assert sampled == 1
    assert verdict == "inherited", (
        "a child directory judged with the file flag-set reads unsafe"
    )


def test_doctor_pass_message_names_the_unevaluated_contents_verbatim(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MEDIUM 1, pinned on the sentence rather than only on the detail field.

    The details carried the count before this round; what was missing was the
    message SAYING so, which is what an operator actually reads.
    """
    from exomem import doctor, state_paths

    directory = state_paths.ensure_vault_state_dir(vault)
    _pin_posture(monkeypatch, directory, child_verdict=None, child_sampled=0)

    check = doctor._check_state_root_dacl(vault)

    assert check.status == "pass"
    assert "verdict covers the directory entry only" in check.message
