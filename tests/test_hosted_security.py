from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import hosted_security as security
from exomem.hosted_runtime import HostedCellConfig
from exomem.server_auth import HostedCellTokenVerifier


def test_hosted_security_module_is_available() -> None:
    assert importlib.util.find_spec("exomem.hosted_security") is not None


def _credential(label: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(label.encode()).digest()).rstrip(b"=").decode()


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _bundle(**credentials: str) -> security.CredentialBundle:
    return security.CredentialBundle(credentials)


def _authority(
    tmp_path: Path,
    bundle: security.CredentialBundle,
    **kwargs: object,
) -> security.HostedSecurityAuthority:
    return security.HostedSecurityAuthority(
        tmp_path / "state",
        cell_id="cell-alpha",
        vault_id="vault-alpha",
        bundle_loader=lambda: bundle,
        **kwargs,
    )


def _projected_bundle(
    mount: Path,
    raw: bytes,
    *,
    generation: str = "..2026_07_14_00_00_00",
    mode: int = 0o444,
) -> Path:
    mount.mkdir(mode=0o755)
    generation_dir = mount / generation
    generation_dir.mkdir(mode=0o755)
    projected = generation_dir / "credentials.json"
    projected.write_bytes(raw)
    projected.chmod(mode)
    (mount / "..data").symlink_to(generation)
    (mount / "credentials.json").symlink_to("..data/credentials.json")
    return mount / "credentials.json"


def test_projected_bundle_reads_one_confined_atomicwriter_generation(tmp_path: Path) -> None:
    old = _credential("old")
    new = _credential("new")
    mount = tmp_path / "credentials"
    leaf = _projected_bundle(
        mount,
        json.dumps({"schema_version": 1, "credentials": {"old": old}}).encode(),
    )
    next_generation = mount / "..2026_07_14_00_00_01"
    next_generation.mkdir()
    next_file = next_generation / "credentials.json"
    next_file.write_text(json.dumps({"schema_version": 1, "credentials": {"new": new}}))
    next_file.chmod(0o444)

    def swap_generation() -> None:
        replacement = mount / "..data-next"
        replacement.symlink_to(next_generation.name)
        replacement.replace(mount / "..data")

    loaded = security._load_projected_credential_bundle_at(
        leaf,
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        after_open=swap_generation,
    )

    assert loaded.credentials == {"old": old}


def test_projected_bundle_can_require_a_read_only_secret_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = _credential("active")
    leaf = _projected_bundle(
        tmp_path / "credentials",
        json.dumps({"schema_version": 1, "credentials": {"active": active}}).encode(),
    )
    monkeypatch.setattr(os, "statvfs", lambda _path: SimpleNamespace(f_flag=0))
    with pytest.raises(security.HostedCredentialBundleInvalid):
        security._load_projected_credential_bundle_at(
            leaf,
            expected_uid=os.getuid(),
            expected_gid=os.getgid(),
            require_read_only_mount=True,
        )

    monkeypatch.setattr(
        os,
        "statvfs",
        lambda _path: SimpleNamespace(f_flag=getattr(os, "ST_RDONLY", 1)),
    )
    loaded = security._load_projected_credential_bundle_at(
        leaf,
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        require_read_only_mount=True,
    )
    assert loaded.credentials == {"active": active}


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b'{"schema_version":1,"schema_version":1,"credentials":{}}', "HOSTED_CREDENTIAL_BUNDLE_INVALID"),
        (b'{"schema_version":1,"credentials":{"v":"x"},"extra":1}', "HOSTED_CREDENTIAL_BUNDLE_INVALID"),
        (b'{"schema_version":2,"credentials":{"v":"x"}}', "HOSTED_CREDENTIAL_BUNDLE_INVALID"),
        (b'{"schema_version":1,"credentials":{"v":"plain-text-secret"}}', "HOSTED_CREDENTIAL_WEAK"),
        (
            json.dumps(
                {
                    "schema_version": 1,
                    "credentials": {
                        "one": _credential("one"),
                        "two": _credential("two"),
                        "three": _credential("three"),
                    },
                }
            ).encode(),
            "HOSTED_CREDENTIAL_BUNDLE_INVALID",
        ),
    ],
)
def test_projected_bundle_rejects_noncanonical_or_weak_content(
    tmp_path: Path, raw: bytes, code: str
) -> None:
    leaf = _projected_bundle(tmp_path / "mount", raw)

    with pytest.raises(security.HostedSecurityError) as error:
        security._load_projected_credential_bundle_at(
            leaf,
            expected_uid=os.getuid(),
            expected_gid=os.getgid(),
        )

    assert error.value.code == code
    assert raw.decode(errors="ignore") not in str(error.value)


@pytest.mark.parametrize("unsafe", ["mode", "owner", "leaf", "generation", "escape"])
def test_projected_bundle_rejects_unsafe_atomicwriter_topology(
    tmp_path: Path, unsafe: str
) -> None:
    mount = tmp_path / "mount"
    raw = json.dumps(
        {"schema_version": 1, "credentials": {"active": _credential("active")}}
    ).encode()
    leaf = _projected_bundle(mount, raw)
    kwargs = {"expected_uid": os.getuid(), "expected_gid": os.getgid()}
    if unsafe == "mode":
        (mount / os.readlink(mount / "..data") / "credentials.json").chmod(0o640)
    elif unsafe == "owner":
        kwargs["expected_uid"] = os.getuid() + 1
    elif unsafe == "leaf":
        leaf.unlink()
        leaf.write_bytes(raw)
        leaf.chmod(0o444)
    elif unsafe == "generation":
        generation = mount / os.readlink(mount / "..data")
        real = mount / "real-generation"
        generation.rename(real)
        generation.symlink_to(real.name)
    elif unsafe == "escape":
        outside = tmp_path / "outside"
        outside.mkdir()
        outside_file = outside / "credentials.json"
        outside_file.write_bytes(raw)
        outside_file.chmod(0o444)
        (mount / "..data").unlink()
        (mount / "..data").symlink_to("../../outside")

    with pytest.raises(security.HostedSecurityError) as error:
        security._load_projected_credential_bundle_at(leaf, **kwargs)

    assert error.value.code == "HOSTED_CREDENTIAL_BUNDLE_INVALID"


def test_projected_bundle_opens_fifo_nonblocking_before_type_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mount = tmp_path / "mount"
    leaf = _projected_bundle(
        mount,
        json.dumps(
            {
                "schema_version": 1,
                "credentials": {"active": _credential("active")},
            }
        ).encode(),
    )
    projected = mount / os.readlink(mount / "..data") / "credentials.json"
    projected.unlink()
    os.mkfifo(projected, mode=0o444)
    real_open = os.open

    def guarded_open(path: object, flags: int) -> int:
        assert flags & os.O_NONBLOCK
        return real_open(path, flags)

    monkeypatch.setattr(security.os, "open", guarded_open)

    with pytest.raises(security.HostedCredentialBundleInvalid):
        security._load_projected_credential_bundle_at(
            leaf,
            expected_uid=os.getuid(),
            expected_gid=os.getgid(),
        )


def test_security_authority_bootstraps_private_durable_digest_only_state(
    tmp_path: Path,
) -> None:
    active = _credential("active")
    authority = _authority(tmp_path, _bundle(active=active))

    snapshot = authority.bootstrap(
        active_version="active",
        operation_id="bootstrap-1",
        request_digest=_digest("bootstrap-request"),
    )

    assert snapshot == security.SecuritySnapshot(
        phase="stable",
        revision=1,
        active_version="active",
        pending_version=None,
        preferred_version="active",
        rotation_id=None,
        proof_valid_until=None,
    )
    database = tmp_path / "state" / "hosted-security.sqlite"
    assert database.stat().st_mode & 0o777 == 0o600
    assert database.stat().st_uid == os.geteuid()
    assert database.stat().st_gid == os.getegid()
    assert active.encode() not in database.read_bytes()
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        assert connection.execute("PRAGMA synchronous").fetchone() == (2,)

    replay = authority.bootstrap(
        active_version="active",
        operation_id="bootstrap-1",
        request_digest=_digest("bootstrap-request"),
    )
    assert replay == snapshot
    with pytest.raises(security.HostedOperationConflict):
        authority.bootstrap(
            active_version="active",
            operation_id="bootstrap-1",
            request_digest=_digest("changed-bootstrap-request"),
        )

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE operations SET result_json=? WHERE operation_id='bootstrap-1'",
            ('{"snapshot":{"phase":"corrupt"}}',),
        )
    with pytest.raises(security.HostedSecurityStateInvalid):
        authority.bootstrap(
            active_version="active",
            operation_id="bootstrap-1",
            request_digest=_digest("bootstrap-request"),
        )


def test_security_descriptor_owner_converges_and_is_verified_as_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = SimpleNamespace(st_uid=0, st_gid=0)
    after = SimpleNamespace(st_uid=10001, st_gid=10002)
    stats = iter([before, after])
    ownership_changes: list[tuple[int, int, int]] = []
    monkeypatch.setattr(security.os, "geteuid", lambda: 0)
    monkeypatch.setattr(security.os, "fstat", lambda descriptor: next(stats))
    monkeypatch.setattr(
        security.os,
        "fchown",
        lambda descriptor, uid, gid: ownership_changes.append((descriptor, uid, gid)),
    )

    security._converge_descriptor_owner(7, expected_uid=10001, expected_gid=10002)

    assert ownership_changes == [(7, 10001, 10002)]


def test_security_authority_rejects_foreign_runtime_owner(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)

    with pytest.raises(security.HostedSecurityStateInvalid):
        security.HostedSecurityAuthority(
            state_root,
            cell_id="cell-alpha",
            vault_id="vault-alpha",
            bundle_loader=lambda: _bundle(active=_credential("active")),
            expected_uid=os.geteuid() + 1,
            expected_gid=os.getegid(),
        )


def test_security_authority_rejects_foreign_cell_or_schema(tmp_path: Path) -> None:
    active = _credential("active")
    authority = _authority(tmp_path, _bundle(active=active))
    authority.bootstrap(
        active_version="active",
        operation_id="bootstrap-1",
        request_digest=_digest("bootstrap"),
    )

    with pytest.raises(security.HostedSecurityStateInvalid):
        security.HostedSecurityAuthority(
            tmp_path / "state",
            cell_id="cell-foreign",
            vault_id="vault-alpha",
            bundle_loader=lambda: _bundle(active=active),
        )
    with sqlite3.connect(tmp_path / "state" / "hosted-security.sqlite") as connection:
        connection.execute("PRAGMA user_version = 99")
    with pytest.raises(security.HostedSecurityStateInvalid):
        _authority(tmp_path, _bundle(active=active))


def test_security_authority_rejects_semantically_corrupt_state_row(tmp_path: Path) -> None:
    active = _credential("active")
    authority = _authority(tmp_path, _bundle(active=active))
    authority.bootstrap(
        active_version="active",
        operation_id="bootstrap",
        request_digest=_digest("bootstrap"),
    )
    with sqlite3.connect(tmp_path / "state" / "hosted-security.sqlite") as connection:
        connection.execute(
            "UPDATE credential_state SET preferred_version='foreign' WHERE singleton=1"
        )

    with pytest.raises(security.HostedSecurityStateInvalid):
        authority.snapshot()


def test_credential_material_maps_corrupt_sqlite_schema_content_free(tmp_path: Path) -> None:
    active = _credential("active")
    authority = _authority(tmp_path, _bundle(active=active))
    authority.bootstrap(
        active_version="active",
        operation_id="bootstrap",
        request_digest=_digest("bootstrap"),
    )
    with sqlite3.connect(tmp_path / "state" / "hosted-security.sqlite") as connection:
        connection.execute("DROP TABLE credential_state")

    with pytest.raises(security.HostedSecurityStateInvalid) as error:
        authority.credential_material("active")

    assert str(error.value) == (
        "HOSTED_CREDENTIAL_STATE_INVALID: hosted credential state is invalid"
    )


def _bootstrapped_rotation(
    tmp_path: Path,
) -> tuple[security.HostedSecurityAuthority, str, str]:
    active = _credential("active")
    pending = _credential("pending")
    current_bundle = [_bundle(active=active)]
    authority = security.HostedSecurityAuthority(
        tmp_path / "state",
        cell_id="cell-alpha",
        vault_id="vault-alpha",
        bundle_loader=lambda: current_bundle[0],
    )
    authority.bootstrap(
        active_version="active",
        operation_id="bootstrap-1",
        request_digest=_digest("bootstrap"),
    )
    current_bundle[0] = _bundle(active=active, pending=pending)
    authority.stage(
        pending_version="pending",
        expected_revision=1,
        operation_id="stage-1",
        request_digest=_digest("stage"),
    )
    return authority, active, pending


def _record_pending_proof(
    authority: security.HostedSecurityAuthority,
    *,
    selected_version: str = "pending",
    revision: int = 2,
    now: int = 1_700_000_000,
    operation_id: str = "probe-1",
) -> security.ProofPersistence:
    return authority.record_probe_proof(
        selected_version=selected_version,
        expected_revision=revision,
        operation_id=operation_id,
        request_digest=_digest(operation_id),
        request_id="11111111-1111-4111-8111-111111111111",
        release="0.20.0",
        protocol="1",
        worker_policy_digest=_digest("workers"),
        readiness_digest=_digest("ready"),
        now=now,
    )


def test_matching_probe_retry_reuses_current_equivalent_durable_proof(tmp_path: Path) -> None:
    authority, _active, _pending = _bootstrapped_rotation(tmp_path)
    first = _record_pending_proof(authority, operation_id="probe-a", now=1_700_000_000)
    second = authority.record_probe_proof(
        selected_version="pending",
        expected_revision=2,
        operation_id="probe-b",
        request_digest=_digest("probe-b"),
        request_id="22222222-2222-4222-8222-222222222222",
        release="0.20.0",
        protocol="1",
        worker_policy_digest=_digest("workers"),
        readiness_digest=_digest("ready"),
        now=1_700_000_001,
    )

    replay = _record_pending_proof(authority, operation_id="probe-a", now=1_700_000_002)

    assert first.valid_until == 1_700_000_300
    assert second.valid_until == 1_700_000_301
    assert replay.valid_until == second.valid_until


def test_rotation_state_machine_survives_restart_and_finalizes_immediately(
    tmp_path: Path,
) -> None:
    authority, active, pending = _bootstrapped_rotation(tmp_path)

    assert authority.authenticate(active).credential_version == "active"
    assert authority.authenticate(pending).credential_version == "pending"
    proof = _record_pending_proof(authority)
    assert proof.recorded is True
    assert proof.snapshot.revision == 2
    assert proof.valid_until == 1_700_000_300

    promoted = authority.promote(
        expected_revision=2,
        operation_id="promote-1",
        request_digest=_digest("promote"),
        now=1_700_000_001,
    )
    assert promoted.phase == "promoted"
    assert promoted.revision == 3
    assert promoted.preferred_version == "pending"

    restarted = _authority(tmp_path, _bundle(active=active, pending=pending))
    assert restarted.snapshot() == promoted
    assert restarted.authenticate(active) is not None
    assert restarted.authenticate(pending) is not None

    finalized = restarted.finalize(
        expected_revision=3,
        operation_id="finalize-1",
        request_digest=_digest("finalize"),
        now=1_700_000_002,
    )
    assert finalized.phase == "stable"
    assert finalized.active_version == "pending"
    assert finalized.revision == 4
    assert restarted.authenticate(active) is None
    assert restarted.authenticate(pending).credential_version == "pending"


@pytest.mark.parametrize("from_phase", ["staged", "promoted"])
def test_rotation_can_abort_from_overlap_phases(tmp_path: Path, from_phase: str) -> None:
    authority, active, pending = _bootstrapped_rotation(tmp_path)
    expected_revision = 2
    if from_phase == "promoted":
        _record_pending_proof(authority)
        authority.promote(
            expected_revision=2,
            operation_id="promote-1",
            request_digest=_digest("promote"),
            now=1_700_000_001,
        )
        expected_revision = 3

    aborted = authority.abort(
        expected_revision=expected_revision,
        operation_id="abort-1",
        request_digest=_digest("abort"),
    )

    assert aborted.phase == "stable"
    assert aborted.active_version == "active"
    assert authority.authenticate(active) is not None
    assert authority.authenticate(pending) is None


def test_rotation_replay_conflict_cas_and_stale_proof_fail_closed(tmp_path: Path) -> None:
    active = _credential("active")
    pending_a = _credential("pending-a")
    pending_b = _credential("pending-b")
    current_bundle = [_bundle(active=active)]
    authority = security.HostedSecurityAuthority(
        tmp_path / "state",
        cell_id="cell-alpha",
        vault_id="vault-alpha",
        bundle_loader=lambda: current_bundle[0],
    )
    authority.bootstrap(
        active_version="active",
        operation_id="bootstrap-1",
        request_digest=_digest("bootstrap"),
    )
    current_bundle[0] = _bundle(active=active, pending_a=pending_a)
    staged = authority.stage(
        pending_version="pending_a",
        expected_revision=1,
        operation_id="stage-1",
        request_digest=_digest("stage-a"),
    )
    assert authority.stage(
        pending_version="pending_a",
        expected_revision=1,
        operation_id="stage-1",
        request_digest=_digest("stage-a"),
    ) == staged
    with pytest.raises(security.HostedOperationConflict):
        authority.stage(
            pending_version="pending_a",
            expected_revision=1,
            operation_id="stage-1",
            request_digest=_digest("stage-changed"),
        )
    with pytest.raises(security.HostedCredentialRevisionConflict):
        authority.abort(
            expected_revision=1,
            operation_id="abort-stale",
            request_digest=_digest("abort-stale"),
        )

    _record_pending_proof(
        authority,
        selected_version="pending_a",
        now=1_700_000_000,
        operation_id="probe-a",
    )
    with pytest.raises(security.HostedCredentialProofStale):
        authority.promote(
            expected_revision=2,
            operation_id="promote-before-proof",
            request_digest=_digest("promote-before-proof"),
            now=1_699_999_999,
        )
    with pytest.raises(security.HostedCredentialProofStale):
        authority.promote(
            expected_revision=2,
            operation_id="promote-stale",
            request_digest=_digest("promote-stale"),
            now=1_700_000_301,
        )

    authority.abort(
        expected_revision=2,
        operation_id="abort-a",
        request_digest=_digest("abort-a"),
    )
    current_bundle[0] = _bundle(active=active, pending_b=pending_b)
    restaged = authority.stage(
        pending_version="pending_b",
        expected_revision=3,
        operation_id="stage-b",
        request_digest=_digest("stage-b"),
    )
    assert restaged.proof_valid_until is None


def test_concurrent_stage_cas_allows_one_complete_transition(tmp_path: Path) -> None:
    active = _credential("active")
    pending = _credential("pending")
    current_bundle = [_bundle(active=active)]
    authority = security.HostedSecurityAuthority(
        tmp_path / "state",
        cell_id="cell-alpha",
        vault_id="vault-alpha",
        bundle_loader=lambda: current_bundle[0],
    )
    authority.bootstrap(
        active_version="active",
        operation_id="bootstrap",
        request_digest=_digest("bootstrap"),
    )
    current_bundle[0] = _bundle(active=active, pending=pending)
    barrier = threading.Barrier(2)
    results: list[str] = []

    def stage(index: int) -> None:
        barrier.wait()
        try:
            authority.stage(
                pending_version="pending",
                expected_revision=1,
                operation_id=f"stage-{index}",
                request_digest=_digest(f"stage-{index}"),
            )
            results.append("ok")
        except security.HostedSecurityError as error:
            results.append(error.code)

    threads = [threading.Thread(target=stage, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count("ok") == 1
    assert len(results) == 2
    assert authority.snapshot().phase == "staged"


def test_transaction_hook_crash_preserves_previous_accepted_set(tmp_path: Path) -> None:
    active = _credential("active")
    pending = _credential("pending")

    def crash(label: str) -> None:
        if label == "stage:before_commit":
            raise RuntimeError("simulated crash")

    current_bundle = [_bundle(active=active)]
    authority = security.HostedSecurityAuthority(
        tmp_path / "state",
        cell_id="cell-alpha",
        vault_id="vault-alpha",
        bundle_loader=lambda: current_bundle[0],
        transaction_hook=crash,
    )
    authority.bootstrap(
        active_version="active",
        operation_id="bootstrap",
        request_digest=_digest("bootstrap"),
    )
    current_bundle[0] = _bundle(active=active, pending=pending)
    with pytest.raises(RuntimeError, match="simulated crash"):
        authority.stage(
            pending_version="pending",
            expected_revision=1,
            operation_id="stage",
            request_digest=_digest("stage"),
        )

    restarted = _authority(tmp_path, _bundle(active=active, pending=pending))
    assert restarted.snapshot().phase == "stable"
    assert restarted.authenticate(active) is not None
    assert restarted.authenticate(pending) is None


def test_dynamic_fastmcp_verifier_reads_authority_on_every_decision(tmp_path: Path) -> None:
    authority, active, pending = _bootstrapped_rotation(tmp_path)
    config = HostedCellConfig(
        cell_id="cell-alpha",
        vault_root=tmp_path / "vault",
        state_root=tmp_path / "state",
        log_root=tmp_path / "logs",
        service_credential="legacy-compatibility-credential-value",
    )
    verifier = HostedCellTokenVerifier(config, authenticator=authority)

    accepted = asyncio.run(verifier.verify_token(pending))
    assert accepted is not None
    assert accepted.claims["credential_version"] == "pending"
    _record_pending_proof(authority)
    authority.promote(
        expected_revision=2,
        operation_id="promote",
        request_digest=_digest("promote"),
        now=1_700_000_001,
    )
    authority.finalize(
        expected_revision=3,
        operation_id="finalize",
        request_digest=_digest("finalize"),
        now=1_700_000_002,
    )
    assert asyncio.run(verifier.verify_token(active)) is None
    assert asyncio.run(verifier.verify_token(pending)) is not None


def test_transfer_signature_uses_only_currently_accepted_version(tmp_path: Path) -> None:
    authority, active, pending = _bootstrapped_rotation(tmp_path)
    payload = b"canonical-ascii-payload"
    import hmac

    signature = hmac.digest(pending.encode(), payload, "sha256")
    assert authority.verify_transfer_signature("pending", payload, signature)
    assert not authority.verify_transfer_signature("active", payload, signature)
    assert not authority.verify_transfer_signature("missing", payload, signature)
    assert active not in repr(authority.snapshot())


def test_jti_consume_is_unique_process_safe_and_restart_safe(tmp_path: Path) -> None:
    active = _credential("active")
    authority = _authority(tmp_path, _bundle(active=active))
    authority.bootstrap(
        active_version="active",
        operation_id="bootstrap",
        request_digest=_digest("bootstrap"),
    )
    jti = str(uuid.uuid4())
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def consume() -> None:
        barrier.wait()
        try:
            authority.consume_transfer_jti(
                cell_id="cell-alpha",
                schema_version=2,
                kid="active",
                jti=jti,
                expires_at=1_700_000_100,
                consumed_at=1_700_000_000,
            )
            outcomes.append("ok")
        except security.HostedSecurityError as error:
            outcomes.append(error.code)

    threads = [threading.Thread(target=consume) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["HOSTED_JTI_REPLAY", "ok"]
    restarted = _authority(tmp_path, _bundle(active=active))
    with pytest.raises(security.HostedJTIReplay):
        restarted.consume_transfer_jti(
            cell_id="cell-alpha",
            schema_version=2,
            kid="active",
            jti=jti,
            expires_at=1_700_000_100,
            consumed_at=1_700_000_001,
        )


def test_jti_capacity_cleanup_and_expiry_fail_closed(tmp_path: Path) -> None:
    active = _credential("active")
    authority = _authority(tmp_path, _bundle(active=active), jti_capacity=2)
    authority.bootstrap(
        active_version="active",
        operation_id="bootstrap",
        request_digest=_digest("bootstrap"),
    )
    consumed: list[str] = []
    for _ in range(2):
        jti = str(uuid.uuid4())
        consumed.append(jti)
        authority.consume_transfer_jti(
            cell_id="cell-alpha",
            schema_version=2,
            kid="active",
            jti=jti,
            expires_at=200,
            consumed_at=100,
        )
    with pytest.raises(security.HostedJTIReplay):
        authority.consume_transfer_jti(
            cell_id="cell-alpha",
            schema_version=2,
            kid="active",
            jti=consumed[0],
            expires_at=200,
            consumed_at=101,
        )
    with pytest.raises(security.HostedJTICapacity):
        authority.consume_transfer_jti(
            cell_id="cell-alpha",
            schema_version=2,
            kid="active",
            jti=str(uuid.uuid4()),
            expires_at=201,
            consumed_at=101,
        )
    assert authority.cleanup_jtis(now=200 + 86_400) == 2
    with pytest.raises(security.HostedJTIExpired):
        authority.consume_transfer_jti(
            cell_id="cell-alpha",
            schema_version=2,
            kid="active",
            jti=str(uuid.uuid4()),
            expires_at=200,
            consumed_at=200,
        )
    with pytest.raises(security.HostedSecurityStateInvalid):
        authority.consume_transfer_jti(
            cell_id="foreign-cell",
            schema_version=2,
            kid="active",
            jti=str(uuid.uuid4()),
            expires_at=300,
            consumed_at=200,
        )


def test_security_store_busy_fails_closed_with_bounded_wait(tmp_path: Path) -> None:
    active = _credential("active")
    authority = _authority(tmp_path, _bundle(active=active), busy_timeout_ms=10)
    authority.bootstrap(
        active_version="active",
        operation_id="bootstrap",
        request_digest=_digest("bootstrap"),
    )
    blocker = sqlite3.connect(tmp_path / "state" / "hosted-security.sqlite")
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(security.HostedSecurityUnavailable):
            authority.credential_material("active")
        with pytest.raises(security.HostedSecurityUnavailable):
            authority.consume_transfer_jti(
                cell_id="cell-alpha",
                schema_version=2,
                kid="active",
                jti=str(uuid.uuid4()),
                expires_at=300,
                consumed_at=200,
            )
    finally:
        blocker.rollback()
        blocker.close()


def test_bootstrap_operator_adapter_constructs_bound_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = _credential("active")
    binding = SimpleNamespace(
        state_root=tmp_path / "state",
        cell_id="cell-alpha",
        vault_id="vault-alpha",
        runtime_uid=os.geteuid(),
        runtime_gid=os.getegid(),
    )
    monkeypatch.setattr(security, "load_credential_bundle", lambda: _bundle(active=active))

    snapshot = security.bootstrap_hosted_security(
        binding=binding,
        active_credential_version="active",
        operation_id="bootstrap",
        request_digest=_digest("bootstrap"),
    )

    assert snapshot.revision == 1
    assert snapshot.active_version == "active"


def test_ready_snapshot_requires_the_projected_bundle_to_match_durable_state(
    tmp_path: Path,
) -> None:
    active = _credential("active")
    current_bundle = [_bundle(active=active)]
    authority = security.HostedSecurityAuthority(
        tmp_path / "state",
        cell_id="cell-alpha",
        vault_id="vault-alpha",
        bundle_loader=lambda: current_bundle[0],
    )
    bootstrapped = authority.bootstrap(
        active_version="active",
        operation_id="bootstrap",
        request_digest=_digest("bootstrap"),
    )

    assert authority.validate_ready() == bootstrapped

    current_bundle[0] = _bundle(foreign=_credential("foreign"))
    with pytest.raises(security.HostedSecurityStateInvalid):
        authority.validate_ready()


def test_server_composition_injects_one_authority_into_all_hosted_auth_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import server
    from exomem.hosted_runtime import HostedCellLifecycle
    from exomem.server_runtime import ServerRuntime

    for name in ("vault", "state", "logs"):
        (tmp_path / name).mkdir()
    config = HostedCellConfig(
        cell_id="cell-alpha",
        vault_id="vault-alpha",
        vault_root=tmp_path / "vault",
        state_root=tmp_path / "state",
        log_root=tmp_path / "logs",
        service_credential=None,
        runtime_uid=os.geteuid(),
        runtime_gid=os.getegid(),
    )
    lifecycle = HostedCellLifecycle(config)
    authority = object()
    runtime = ServerRuntime(
        vault_root=config.vault_root,
        source_schema=object(),
        project_keys_hint="",
        base_url="",
        hosted_config=config,
        hosted_lifecycle=lifecycle,
        hosted_security_authority=authority,
    )
    captured: dict[str, object] = {}
    original_verifier = server.HostedCellTokenVerifier

    def verifier(config_arg: HostedCellConfig, *, authenticator: object | None = None):
        captured["mcp"] = authenticator
        return original_verifier(config_arg, authenticator=authenticator)

    def register(_app: object, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(server, "initialize_runtime", lambda **_kwargs: runtime)
    monkeypatch.setattr(server, "HostedCellTokenVerifier", verifier)
    monkeypatch.setattr(server, "register_hosted_routes", register)
    monkeypatch.setattr("exomem.writer_lease.start_server_lifecycle", lambda: None)

    app = server.build_server(require_auth=True)

    assert captured["mcp"] is authority
    assert captured["private_authenticator"] is authority
    assert captured["transfer_security_authority"] is authority
    assert app._exomem_server_runtime is runtime


def test_server_runtime_validates_dynamic_security_before_reporting_auth_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import server_runtime

    config = HostedCellConfig(
        cell_id="cell-alpha",
        vault_id="vault-alpha",
        vault_root=tmp_path / "vault",
        state_root=tmp_path / "state",
        log_root=tmp_path / "logs",
        service_credential=None,
        runtime_uid=os.geteuid(),
        runtime_gid=os.getegid(),
        worker_policy_digest="a" * 64,
    )
    captured: dict[str, object] = {}

    class FakeAuthority:
        def __init__(self, state_root: Path, **kwargs: object) -> None:
            captured["state_root"] = state_root
            captured.update(kwargs)

        def validate_ready(self) -> None:
            captured["validated"] = True

    monkeypatch.setattr(security, "HostedSecurityAuthority", FakeAuthority)

    authority = server_runtime._initialize_hosted_security(config)

    assert isinstance(authority, FakeAuthority)
    assert captured == {
        "state_root": config.state_root,
        "cell_id": "cell-alpha",
        "vault_id": "vault-alpha",
        "expected_uid": os.geteuid(),
        "expected_gid": os.getegid(),
        "validated": True,
    }


def test_credential_operator_adapter_dispatches_and_returns_exact_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = _credential("active")
    pending = _credential("pending")
    current_bundle = [_bundle(active=active)]
    monkeypatch.setattr(security, "load_credential_bundle", lambda: current_bundle[0])
    digest = _digest("request")

    class OperatorFailure(RuntimeError):
        def __init__(self, code: str) -> None:
            self.code = code

    monkeypatch.setitem(
        sys.modules,
        "exomem.hosted_operator",
        SimpleNamespace(
            OperatorFailure=OperatorFailure,
            canonical_request_digest=lambda request: digest,
        ),
    )
    binding = SimpleNamespace(
        state_root=tmp_path / "state",
        cell_id="cell-alpha",
        vault_id="vault-alpha",
        runtime_uid=os.geteuid(),
        runtime_gid=os.getegid(),
    )
    security.bootstrap_hosted_security(
        binding=binding,
        active_credential_version="active",
        operation_id="bootstrap",
        request_digest=_digest("bootstrap"),
    )
    current_bundle[0] = _bundle(active=active, pending=pending)

    code, data = security.execute_credential_operator(
        {
            "request_id": str(uuid.uuid4()),
            "operation_id": "stage",
            "cell_id": "cell-alpha",
            "vault_id": "vault-alpha",
            "state_root": str(tmp_path / "state"),
            "action": "stage",
            "expected_revision": 1,
            "pending_version": "pending",
        }
    )

    assert code == "HOSTED_CREDENTIAL_STAGED"
    assert data == {
        "phase": "staged",
        "revision": 2,
        "active_version": "active",
        "pending_version": "pending",
        "preferred_version": "active",
        "rotation_id": data["rotation_id"],
        "proof_valid_until": None,
    }
    assert uuid.UUID(data["rotation_id"]).version == 4


def test_credential_operator_adapter_maps_modeled_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class OperatorFailure(RuntimeError):
        def __init__(self, code: str) -> None:
            self.code = code

    monkeypatch.setitem(
        sys.modules,
        "exomem.hosted_operator",
        SimpleNamespace(
            OperatorFailure=OperatorFailure,
            canonical_request_digest=lambda request: _digest("request"),
        ),
    )
    monkeypatch.setattr(
        security,
        "load_credential_bundle",
        lambda: _bundle(active=_credential("active")),
    )

    with pytest.raises(OperatorFailure) as error:
        security.execute_credential_operator(
            {
                "operation_id": "promote",
                "cell_id": "cell-alpha",
                "vault_id": "vault-alpha",
                "state_root": str(tmp_path / "state"),
                "action": "promote",
                "expected_revision": 1,
            }
        )

    assert error.value.code == "HOSTED_CREDENTIAL_STATE_INVALID"
