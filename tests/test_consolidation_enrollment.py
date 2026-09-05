from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import __main__ as cli_main
from exomem import product_invoke, server_runtime, writer_lease
from exomem.governance import principal

VAULT_BINDING = "a" * 64


@pytest.fixture(autouse=True)
def _isolated_writer_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_root = tmp_path / "writer-state"
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(state_root))
    writer_lease.reset_managers_for_tests()
    yield state_root
    writer_lease.reset_managers_for_tests()


def test_local_presence_allows_concurrent_runtime_registrations(
    tmp_path: Path,
    _isolated_writer_state: Path,
) -> None:
    from exomem.governance import consolidation_enrollment

    registry = consolidation_enrollment.LocalRuntimePresenceRegistry(
        tmp_path / "vault",
        state_root=_isolated_writer_state,
        slots=2,
    )
    first_entered = threading.Event()
    release_first = threading.Event()

    def hold_first() -> None:
        with registry.runtime_presence():
            first_entered.set()
            assert release_first.wait(timeout=2)

    thread = threading.Thread(target=hold_first)
    thread.start()
    assert first_entered.wait(timeout=2)
    try:
        with registry.runtime_presence(timeout_seconds=0):
            pass
    finally:
        release_first.set()
        thread.join(timeout=2)
    assert not thread.is_alive()


def test_local_offline_enrollment_refuses_live_presence_then_succeeds(
    tmp_path: Path,
    _isolated_writer_state: Path,
) -> None:
    from exomem.governance import consolidation_enrollment

    registry = consolidation_enrollment.LocalRuntimePresenceRegistry(
        tmp_path / "vault",
        state_root=_isolated_writer_state,
        slots=2,
    )
    with registry.runtime_presence():
        with pytest.raises(
            consolidation_enrollment.ConsolidationEnrollmentUnavailable,
            match="^CONSOLIDATION_ENROLLMENT_BUSY$",
        ):
            with registry.offline_enrollment(timeout_seconds=0):
                pytest.fail("live runtime must exclude enrollment")

    with registry.offline_enrollment(timeout_seconds=0):
        pass


def test_runtime_presence_does_not_report_a_vault_mutation(
    tmp_path: Path,
    _isolated_writer_state: Path,
) -> None:
    from exomem.governance import consolidation_enrollment
    from exomem.mutation_lock import (
        VaultMutationCoordinator,
        active_mutation_snapshot,
        process_local_mutation_boundary,
    )

    vault = tmp_path / "vault"
    registry = consolidation_enrollment.LocalRuntimePresenceRegistry(
        vault, state_root=_isolated_writer_state, slots=2,
    )
    mutation = VaultMutationCoordinator(_isolated_writer_state, vault)
    with registry.runtime_presence():
        assert active_mutation_snapshot() == {"state": "free"}
        assert process_local_mutation_boundary() == {
            "state": "unknown", "reason": "process_local_only",
        }
        assert mutation.snapshot()["state"] == "free"
        with mutation.hold(
            request_id="actual-mutation", operation="edit_memory", holder_kind="command",
        ):
            assert active_mutation_snapshot()["request_id"] == "actual-mutation"
        assert active_mutation_snapshot() == {"state": "free"}
        with pytest.raises(
            consolidation_enrollment.ConsolidationEnrollmentUnavailable,
            match="^CONSOLIDATION_ENROLLMENT_BUSY$",
        ):
            with registry.offline_enrollment(timeout_seconds=0):
                pytest.fail("diagnostic classification must not weaken exclusion")


def test_local_offline_gate_blocks_new_runtime_registration(
    tmp_path: Path,
    _isolated_writer_state: Path,
) -> None:
    from exomem.governance import consolidation_enrollment

    registry = consolidation_enrollment.LocalRuntimePresenceRegistry(
        tmp_path / "vault",
        state_root=_isolated_writer_state,
        slots=2,
    )
    with registry.offline_enrollment(timeout_seconds=0):
        with pytest.raises(
            consolidation_enrollment.ConsolidationEnrollmentUnavailable,
            match="^CONSOLIDATION_ENROLLMENT_BUSY$",
        ):
            with registry.runtime_presence(timeout_seconds=0):
                pytest.fail("offline gate must block new runtime presence")


def test_local_presence_is_process_safe_and_crash_released(
    tmp_path: Path,
    _isolated_writer_state: Path,
) -> None:
    from exomem.governance import consolidation_enrollment

    vault_root = tmp_path / "vault"
    script = """
import sys
from pathlib import Path
from exomem.governance.consolidation_enrollment import LocalRuntimePresenceRegistry

registry = LocalRuntimePresenceRegistry(Path(sys.argv[1]), state_root=Path(sys.argv[2]))
with registry.runtime_presence():
    print("ready", flush=True)
    sys.stdin.read()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(vault_root), str(_isolated_writer_state)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"
        registry = consolidation_enrollment.LocalRuntimePresenceRegistry(
            vault_root,
            state_root=_isolated_writer_state,
        )
        with pytest.raises(
            consolidation_enrollment.ConsolidationEnrollmentUnavailable,
            match="^CONSOLIDATION_ENROLLMENT_BUSY$",
        ):
            with registry.offline_enrollment(timeout_seconds=0):
                pass
        process.kill()
        process.wait(timeout=5)
        with registry.offline_enrollment(timeout_seconds=0):
            pass
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_local_enrollment_requires_owner_and_existing_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolated_writer_state: Path,
) -> None:
    from exomem.governance import consolidation_enrollment, consolidation_identity

    calls = 0

    def load_identity(_root: Path, *, now: int):
        nonlocal calls
        calls += 1
        raise consolidation_identity.ConsolidationIdentityUnavailable

    monkeypatch.setattr(consolidation_identity, "load_local_identity", load_identity)
    outsider = principal.RequestPrincipal(
        audience_id="someone-else",
        surface="cli",
        resolved=True,
        issuer_family="cli-local-owner",
    )

    with pytest.raises(consolidation_enrollment.ConsolidationEnrollmentUnavailable):
        consolidation_enrollment.enroll_local(
            tmp_path / "vault",
            principal=outsider,
            now=1_788_188_400,
            state_root=_isolated_writer_state,
        )
    assert calls == 0

    with pytest.raises(consolidation_enrollment.ConsolidationEnrollmentUnavailable):
        consolidation_enrollment.enroll_local(
            tmp_path / "vault",
            principal=principal.owner_principal(surface="cli"),
            now=1_788_188_400,
            state_root=_isolated_writer_state,
        )
    assert calls == 1


def test_local_enrollment_publishes_and_reloads_exact_identity_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolated_writer_state: Path,
) -> None:
    from exomem.governance import consolidation_enrollment, consolidation_identity

    identity = SimpleNamespace(record_digest=VAULT_BINDING)
    loads: list[int] = []

    def load_identity(_root: Path, *, now: int):
        loads.append(now)
        return identity

    monkeypatch.setattr(consolidation_identity, "load_local_identity", load_identity)
    state = consolidation_enrollment.enroll_local(
        tmp_path / "vault",
        principal=principal.owner_principal(surface="cli"),
        now=1_788_188_400,
        state_root=_isolated_writer_state,
    )

    assert state.kind == "open"
    assert state.revision == 0
    assert state.vault_binding_digest == VAULT_BINDING
    assert loads == [1_788_188_400, 1_788_188_400]


def test_local_enrollment_retry_returns_existing_open_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolated_writer_state: Path,
) -> None:
    from exomem.governance import consolidation_enrollment, consolidation_identity

    identity = SimpleNamespace(record_digest=VAULT_BINDING)
    monkeypatch.setattr(
        consolidation_identity,
        "load_local_identity",
        lambda _root, *, now: identity,
    )
    owner = principal.owner_principal(surface="cli")
    first = consolidation_enrollment.enroll_local(
        tmp_path / "vault",
        principal=owner,
        now=1_788_188_400,
        state_root=_isolated_writer_state,
    )
    retry = consolidation_enrollment.enroll_local(
        tmp_path / "vault",
        principal=owner,
        now=1_788_188_500,
        state_root=_isolated_writer_state,
    )

    assert retry == first


def test_hosted_enrollment_uses_existing_lifetime_exclusion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import consolidation_enrollment, consolidation_identity
    from exomem.hosted_restore import acquire_hosted_lifetime_lock

    vault_root = tmp_path / "vault"
    state_root = tmp_path / "state"
    binding = SimpleNamespace(
        vault_root=vault_root,
        state_root=state_root,
        runtime_uid=os.getuid(),
        runtime_gid=os.getgid(),
    )
    monkeypatch.setattr(
        consolidation_identity,
        "load_hosted_identity",
        lambda _binding, *, custody, now: SimpleNamespace(record_digest=VAULT_BINDING),
    )

    with acquire_hosted_lifetime_lock(state_root, binding=binding):
        state = consolidation_enrollment.enroll_hosted_locked(
            binding,
            custody=object(),
            now=1_788_188_400,
        )
        assert state.vault_binding_digest == VAULT_BINDING
        with pytest.raises(
            consolidation_enrollment.ConsolidationEnrollmentUnavailable,
            match="^CONSOLIDATION_ENROLLMENT_BUSY$",
        ):
            consolidation_enrollment.enroll_hosted(
                binding,
                custody=object(),
                now=1_788_188_400,
            )

    state = consolidation_enrollment.enroll_hosted(
        binding,
        custody=object(),
        now=1_788_188_400,
    )
    assert state.vault_binding_digest == VAULT_BINDING


def test_hosted_init_enrolls_while_lifetime_lock_is_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import hosted_runtime
    from exomem.hosted_restore import acquire_hosted_lifetime_lock

    request = {
        "request_id": "123e4567-e89b-42d3-a456-426614174000",
        "operation_id": "provision-alpha-1",
        "cell_id": "cell-alpha",
        "vault_id": "vault-alpha",
        "vault_root": str(tmp_path / "vault"),
        "state_root": str(tmp_path / "state"),
        "log_root": str(tmp_path / "logs"),
        "runtime_uid": os.getuid(),
        "runtime_gid": os.getgid(),
        "expected_release": hosted_runtime.__version__,
        "expected_protocol": hosted_runtime.HOSTED_PROTOCOL_VERSION,
        "active_credential_version": "credential-v1",
    }
    initialized = SimpleNamespace(
        as_operator_data=lambda: {
            "status": "provisioned",
            "cell_id": "cell-alpha",
            "vault_id": "vault-alpha",
            "binding_version": 2,
            "lifecycle_status": "stopped",
            "exomem_release": hosted_runtime.__version__,
            "hosted_protocol": hosted_runtime.HOSTED_PROTOCOL_VERSION,
            "runtime_uid": os.getuid(),
            "runtime_gid": os.getgid(),
            "credential_version": "credential-v1",
            "credential_revision": 1,
            "capabilities": ("hosted-operator-v1",),
        }
    )
    monkeypatch.setattr(
        hosted_runtime,
        "initialize_hosted_cell_v2",
        lambda *_args, **_kwargs: initialized,
    )
    monkeypatch.setattr(
        hosted_runtime,
        "_prepare_hosted_enrollment_custody",
        lambda: None,
        raising=False,
    )
    observed = False

    def enroll(binding, *, now: int):
        nonlocal observed
        assert now > 0
        with pytest.raises(Exception) as busy:
            with acquire_hosted_lifetime_lock(binding.state_root, binding=binding):
                pass
        assert getattr(busy.value, "code", None) == "HOSTED_RESTORE_BUSY"
        observed = True

    monkeypatch.setattr(
        hosted_runtime,
        "_enroll_initialized_hosted_cell",
        enroll,
        raising=False,
    )

    code, _data = hosted_runtime.execute_hosted_init_v2(request)

    assert code == "HOSTED_CELL_INITIALIZED"
    assert observed


@pytest.mark.parametrize("through_operator", [False, True])
def test_hosted_init_helper_publishes_identity_and_revision_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    through_operator: bool,
) -> None:
    from exomem import hosted_runtime
    from exomem.governance import (
        authorization_custody,
        consolidation_identity,
        consolidation_seal,
    )

    now = 1_788_188_400
    binding = hosted_runtime.HostedBindingV2(
        cell_id="hosted-cell-alpha",
        vault_id="logical-vault-alpha",
        vault_root=tmp_path / "vault",
        state_root=tmp_path / "state",
        log_root=tmp_path / "logs",
        runtime_uid=os.getuid(),
        runtime_gid=os.getgid(),
    )
    for _kind, root in binding.roots():
        root.mkdir(mode=0o700)
    (binding.vault_root / "Knowledge Base").mkdir()
    key = authorization_custody.AuthorizationVerifierKey(
        key_id="hosted-cell-key-v1",
        key=b"h" * 32,
        not_before=now - 10,
        not_after=now + 10_000,
    )
    keyring = authorization_custody.AuthorizationKeyring(
        version=1,
        keyring_id="hosted-keyring-v1",
        cell_id=binding.cell_id,
        logical_vault_id=binding.vault_id,
        active_key_id=key.key_id,
        accepted_keys=(key,),
    )
    custody = authorization_custody.AuthorizationCustody(
        keyring_path=tmp_path / "external-keyring.json",
        control_path=tmp_path / "external-control.json",
        keyring=keyring,
        control=authorization_custody.AuthorizationControlRecord(
            version=1,
            keyring_id=keyring.keyring_id,
            cell_id=binding.cell_id,
            logical_vault_id=binding.vault_id,
            registry_attachment_id="hosted-control-attachment-v1",
            attachment_epoch=1,
            governance_enrolled=False,
            activation_store_id=None,
            activation_epoch=None,
            activation_state_digest=None,
            serving_membership_epoch=1,
            serving_membership_digest="a" * 64,
            issued_at=now - 1,
            expires_at=now + 1_000,
            signing_key_id=key.key_id,
        ),
    )
    monkeypatch.setattr(
        authorization_custody,
        "load_authorization_custody",
        lambda _root, *, now: custody,
    )

    if through_operator:
        monkeypatch.delenv("EXOMEM_WRITER_LEASE_STATE_DIR", raising=False)
        writer_lease.reset_managers_for_tests()
        monkeypatch.setattr(
            Path, "home",
            lambda: pytest.fail("Hosted initialization must not use ambient home state"),
        )
        monkeypatch.setattr(
            hosted_runtime, "initialize_hosted_cell_v2",
            lambda *_args, **_kwargs: SimpleNamespace(as_operator_data=lambda: {}),
        )
        monkeypatch.setattr(hosted_runtime, "_prepare_hosted_enrollment_custody", lambda: None)
        monkeypatch.setattr(hosted_runtime.time, "time", lambda: now)
        code, _data = hosted_runtime.execute_hosted_init_v2({
            "request_id": "123e4567-e89b-42d3-a456-426614174000",
            "operation_id": "provision-enrollment-state",
            "cell_id": binding.cell_id,
            "vault_id": binding.vault_id,
            "vault_root": str(binding.vault_root),
            "state_root": str(binding.state_root),
            "log_root": str(binding.log_root),
            "runtime_uid": binding.runtime_uid,
            "runtime_gid": binding.runtime_gid,
            "expected_release": hosted_runtime.__version__,
            "expected_protocol": hosted_runtime.HOSTED_PROTOCOL_VERSION,
            "active_credential_version": "credential-v1",
        })
        assert code == "HOSTED_CELL_INITIALIZED"
        assert "EXOMEM_WRITER_LEASE_STATE_DIR" not in os.environ
        monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(binding.state_root))
    else:
        hosted_runtime._enroll_initialized_hosted_cell(binding, now=now)

    identity = consolidation_identity.load_hosted_identity(
        binding,
        custody=custody,
        now=now + 1,
    )
    seal = consolidation_seal.ConsolidationSealStore(binding.vault_root).load(
        vault_binding_digest=identity.record_digest,
    )
    assert seal.kind == "open"
    assert seal.revision == 0


def test_local_enrollment_cli_is_explicit_and_content_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from exomem.governance import consolidation_enrollment

    monkeypatch.setattr(
        consolidation_enrollment,
        "enroll_local",
        lambda *_args, **_kwargs: SimpleNamespace(kind="open", revision=0),
    )

    assert cli_main._governance_enroll_main(
        ["--vault", str(tmp_path / "vault"), "--json"]
    ) == 0
    assert capsys.readouterr().out == (
        '{"ok": true, "status": "enrolled", "kind": "open", "revision": 0}\n'
    )


def test_local_enrollment_cli_returns_stable_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from exomem.governance import consolidation_enrollment

    def refuse(*_args, **_kwargs):
        raise consolidation_enrollment.ConsolidationEnrollmentUnavailable(
            "CONSOLIDATION_ENROLLMENT_BUSY"
        )

    monkeypatch.setattr(consolidation_enrollment, "enroll_local", refuse)
    assert cli_main._governance_enroll_main(
        ["--vault", str(tmp_path / "vault"), "--json"]
    ) == 1
    assert capsys.readouterr().out == (
        '{"ok": false, "error": {"code": "CONSOLIDATION_ENROLLMENT_BUSY", '
        '"message": "governance enrollment is unavailable"}}\n'
    )


def test_enrollment_operator_is_absent_from_product_registries() -> None:
    from exomem import commands

    for surface in ("mcp", "rest", "hosted"):
        assert "governance-enroll" not in {
            command.name for command in commands.product_commands_for(surface)
        }


def test_product_invocation_holds_local_runtime_presence(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolated_writer_state: Path,
) -> None:
    from exomem.governance import consolidation_enrollment

    observed = False

    def invoke_command(*_args, **_kwargs):
        nonlocal observed
        registry = consolidation_enrollment.LocalRuntimePresenceRegistry(
            vault,
            state_root=_isolated_writer_state,
        )
        with pytest.raises(consolidation_enrollment.ConsolidationEnrollmentUnavailable):
            with registry.offline_enrollment(timeout_seconds=0):
                pass
        observed = True
        return {"ok": True}

    monkeypatch.setattr(writer_lease, "invoke_command", invoke_command)
    result = product_invoke.invoke_product("browse_memory", {"mode": "list"})
    assert result == {"ok": True}
    assert observed


def test_direct_cli_presence_covers_final_graph_drain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolated_writer_state: Path,
) -> None:
    from exomem import graph_sync
    from exomem.governance import consolidation_enrollment

    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    observed = False

    def run(_argv) -> int:
        consolidation_enrollment.ensure_cli_runtime_presence(vault_root)
        return 0

    def drain() -> bool:
        nonlocal observed
        registry = consolidation_enrollment.LocalRuntimePresenceRegistry(
            vault_root,
            state_root=_isolated_writer_state,
        )
        with pytest.raises(
            consolidation_enrollment.ConsolidationEnrollmentUnavailable,
            match="^CONSOLIDATION_ENROLLMENT_BUSY$",
        ):
            with registry.offline_enrollment(timeout_seconds=0):
                pass
        observed = True
        return True

    monkeypatch.setattr(cli_main, "_run_cli", run)
    monkeypatch.setattr(graph_sync, "drain_active_rebuilds", drain)

    assert cli_main.main([]) == 0
    assert observed

    registry = consolidation_enrollment.LocalRuntimePresenceRegistry(
        vault_root,
        state_root=_isolated_writer_state,
    )
    with registry.offline_enrollment(timeout_seconds=0):
        pass


def test_legacy_direct_cli_registers_before_vault_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolated_writer_state: Path,
) -> None:
    from exomem import backfill, graph_sync
    from exomem.governance import consolidation_enrollment

    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    observed = False

    def backfill_media(root: Path, **_kwargs) -> None:
        nonlocal observed
        assert root == vault_root
        registry = consolidation_enrollment.LocalRuntimePresenceRegistry(
            vault_root,
            state_root=_isolated_writer_state,
        )
        with pytest.raises(
            consolidation_enrollment.ConsolidationEnrollmentUnavailable,
            match="^CONSOLIDATION_ENROLLMENT_BUSY$",
        ):
            with registry.offline_enrollment(timeout_seconds=0):
                pass
        observed = True

    monkeypatch.setattr(backfill, "backfill_media", backfill_media)
    monkeypatch.setattr(graph_sync, "drain_active_rebuilds", lambda: True)

    assert cli_main.main(
        ["backfill-media", "--vault", str(vault_root), "--dry-run"]
    ) == 0
    assert observed


@pytest.mark.parametrize("inherited_vault", [False, True])
def test_doctor_retains_the_vault_selected_after_loading_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolated_writer_state: Path,
    inherited_vault: bool,
) -> None:
    from exomem import doctor, graph_sync
    from exomem.governance import consolidation_enrollment

    vault_root = tmp_path / "selected-vault"
    vault_root.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "EXOMEM_VAULT_PATH", str(tmp_path / "old-vault") if inherited_vault else "",
    )
    (tmp_path / ".env").write_text(
        f'EXOMEM_VAULT_PATH="{vault_root.as_posix()}"\n', encoding="utf-8",
    )
    observed = False

    def run_doctor(**_kwargs):
        nonlocal observed
        assert os.environ["EXOMEM_VAULT_PATH"] == str(vault_root)
        registry = consolidation_enrollment.LocalRuntimePresenceRegistry(
            vault_root, state_root=_isolated_writer_state,
        )
        with pytest.raises(
            consolidation_enrollment.ConsolidationEnrollmentUnavailable,
            match="^CONSOLIDATION_ENROLLMENT_BUSY$",
        ):
            with registry.offline_enrollment(timeout_seconds=0):
                pass
        observed = True
        return SimpleNamespace(success=True, as_dict=lambda: {})

    monkeypatch.setattr(doctor, "doctor", run_doctor)
    monkeypatch.setattr(graph_sync, "drain_active_rebuilds", lambda: True)
    assert cli_main.main(["doctor", "--json"]) == 0
    assert observed


def test_cli_runtime_registration_excludes_only_enrollment(
    tmp_path: Path,
    _isolated_writer_state: Path,
) -> None:
    from exomem.governance import consolidation_enrollment

    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    registry = consolidation_enrollment.LocalRuntimePresenceRegistry(
        vault_root,
        state_root=_isolated_writer_state,
    )

    with consolidation_enrollment.cli_runtime_scope():
        cli_main._retain_direct_cli_runtime(
            ["governance-enroll", "--vault", str(vault_root)]
        )
        with registry.offline_enrollment(timeout_seconds=0):
            pass

        cli_main._retain_direct_cli_runtime(
            ["governance-schema", "status", "--vault", str(vault_root)]
        )
        with pytest.raises(
            consolidation_enrollment.ConsolidationEnrollmentUnavailable,
            match="^CONSOLIDATION_ENROLLMENT_BUSY$",
        ):
            with registry.offline_enrollment(timeout_seconds=0):
                pass


def test_tui_process_presence_outlives_threaded_vault_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolated_writer_state: Path,
) -> None:
    from exomem import graph_sync, tui
    from exomem.governance import consolidation_enrollment

    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    observed = False

    def run(*, vault: str | None, mouse: bool) -> int:
        assert vault == str(vault_root)
        assert mouse is True

        def worker() -> None:
            nonlocal observed
            registry = consolidation_enrollment.LocalRuntimePresenceRegistry(
                vault_root,
                state_root=_isolated_writer_state,
            )
            try:
                with registry.offline_enrollment(timeout_seconds=0):
                    return
            except consolidation_enrollment.ConsolidationEnrollmentUnavailable as error:
                observed = error.code == "CONSOLIDATION_ENROLLMENT_BUSY"

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
        return 0

    monkeypatch.setattr(cli_main, "_tui_stdio_is_tty", lambda: True)
    monkeypatch.setattr(cli_main, "_module_available", lambda _name: True)
    monkeypatch.setattr(tui, "run", run)
    monkeypatch.setattr(graph_sync, "drain_active_rebuilds", lambda: True)

    assert cli_main.main(["tui", "--vault", str(vault_root)]) == 0
    assert observed


def test_tui_adopted_vault_is_retained_on_app_thread(
    tmp_path: Path,
    _isolated_writer_state: Path,
) -> None:
    from exomem.governance import consolidation_enrollment
    from exomem.tui.backend import retain_runtime_presence

    vault_root = tmp_path / "vault"
    vault_root.mkdir()

    with consolidation_enrollment.cli_runtime_scope():
        assert retain_runtime_presence(vault_root)
        registry = consolidation_enrollment.LocalRuntimePresenceRegistry(
            vault_root,
            state_root=_isolated_writer_state,
        )
        with pytest.raises(
            consolidation_enrollment.ConsolidationEnrollmentUnavailable,
            match="^CONSOLIDATION_ENROLLMENT_BUSY$",
        ):
            with registry.offline_enrollment(timeout_seconds=0):
                pass

    with registry.offline_enrollment(timeout_seconds=0):
        pass


def test_direct_cli_presence_precedes_authorization_and_coercion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolated_writer_state: Path,
) -> None:
    from exomem.governance import (
        authorization_request,
        authorization_transport,
        consolidation_enrollment,
    )

    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    cmd = SimpleNamespace(name="browse_memory")
    admission = object()

    def verify(*_args, **_kwargs):
        registry = consolidation_enrollment.LocalRuntimePresenceRegistry(
            vault_root,
            state_root=_isolated_writer_state,
        )
        with pytest.raises(consolidation_enrollment.ConsolidationEnrollmentUnavailable):
            with registry.offline_enrollment(timeout_seconds=0):
                pass
        return admission

    monkeypatch.setattr(product_invoke, "resolve_vault_for", lambda *_args: vault_root)
    monkeypatch.setattr(authorization_request, "verify_authorization_context", verify)

    with consolidation_enrollment.cli_runtime_scope():
        root, observed = product_invoke.verify_local_authorization_transport(
            cmd,
            raw_for_vault={},
            surface="cli",
            authorization_carrier=authorization_transport.CredentialCarrier.absent(),
        )

    assert root == vault_root
    assert observed is admission


def test_local_server_holds_presence_for_process_after_lifespan_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolated_writer_state: Path,
) -> None:
    from exomem.governance import consolidation_enrollment

    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    monkeypatch.setattr(server_runtime, "hosted_mode_enabled", lambda: False)
    monkeypatch.setattr(server_runtime, "resolve_vault", lambda: vault_root)
    monkeypatch.setattr(
        server_runtime.schema,
        "load_source_schema",
        lambda _root: SimpleNamespace(source_types=()),
    )
    monkeypatch.setattr(server_runtime.project_keys, "keys_hint", lambda _root: "")
    monkeypatch.setattr(
        server_runtime.projection_runtime,
        "preactivate_projection_runtime",
        lambda _root: None,
    )
    monkeypatch.setattr(server_runtime, "_start_metrics_persistence", lambda: None)

    runtime = server_runtime.initialize_runtime(load_dotenv_func=lambda **_kwargs: None)
    registry = consolidation_enrollment.LocalRuntimePresenceRegistry(
        vault_root,
        state_root=_isolated_writer_state,
    )
    with pytest.raises(consolidation_enrollment.ConsolidationEnrollmentUnavailable):
        with registry.offline_enrollment(timeout_seconds=0):
            pass

    activation = server_runtime.LocalRuntimeActivation(
        vault_root,
        fallback_seconds=60,
        runtime_presence=runtime.local_runtime_presence,
    )

    async def exercise() -> None:
        async with activation.lifespan()(SimpleNamespace()):
            with pytest.raises(consolidation_enrollment.ConsolidationEnrollmentUnavailable):
                with registry.offline_enrollment(timeout_seconds=0):
                    pass

    asyncio.run(exercise())
    with pytest.raises(consolidation_enrollment.ConsolidationEnrollmentUnavailable):
        with registry.offline_enrollment(timeout_seconds=0):
            pass

    # The production boundary is process exit. Explicitly unwind the retained
    # context here so this test process can continue using its isolated state.
    assert activation._runtime_presence is runtime.local_runtime_presence  # noqa: SLF001
    activation._runtime_presence = None  # noqa: SLF001
    assert runtime.local_runtime_presence is not None
    runtime.local_runtime_presence.__exit__(None, None, None)
    with registry.offline_enrollment(timeout_seconds=0):
        pass
