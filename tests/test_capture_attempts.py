"""Capture withdrawal closes admission without guessing the earlier capture outcome."""

from __future__ import annotations

import contextvars
import json
import subprocess
import sys
import threading
from contextlib import contextmanager
from dataclasses import replace

import pytest
from pydantic import ValidationError

from exomem import capture_attempts, commands, source_capture, writer_lease
from exomem.governance.principal import owner_principal, request_scope


def dispatch(manager, root, name, arguments, source_schema=None):
    command = next(c for c in commands.PRODUCT_COMMANDS if c.name == name)
    injected = (root, source_schema) if name == "capture_source" else (root,)
    return manager.invoke(command, injected, arguments)


@pytest.fixture
def attempts(vault, source_schema, tmp_path):
    manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=tmp_path / "attempt-state"), mutation_timeout_seconds=0.03)

    def call(name, **kwargs):
        command = next(c for c in commands.PRODUCT_COMMANDS if c.name == name)
        injected = (vault, source_schema) if name == "capture_source" else (vault,)
        with request_scope(owner_principal(surface="mcp")):
            return manager.invoke(command, injected, kwargs, idempotency_key="same-key", implicit_idempotency_scope="same-scope")

    yield manager, call
    writer_lease.reset_managers_for_tests()


def authored(**changes):
    return dict(title="Original survey", content="The café opens at dawn.", source_kind="session",
                source_identity={"system": "archive", "record_id": "survey", "revision": "one"}, **changes)


def reserve(call, args):
    return call("reserve_source_capture", source_identity=args["source_identity"],
                request_sha256=capture_attempts.CaptureWireV1.model_validate(args).sha256())["reservation"]


def test_delayed_body_cannot_write_after_closure_and_reservation_never_replays(vault, attempts):
    manager, call = attempts
    args = authored()
    ticket = reserve(call, args)
    fresh = reserve(call, args)
    assert fresh["ticket"] != ticket["ticket"]
    result = call("settle_source_capture", reservation=ticket)
    assert result["state"] == "closed" and result["admission_closed_here"] is True
    assert result["past_outcome"] == "not_started" and result["capture_receipt"] is None
    assert result["custody_state"] == "complete" and result["owned_targets"] == []
    assert result["attempt_residue"]["state"] == "clear" and result["attempt_residue"]["owners"] == []
    before = set(vault.rglob("*.md"))
    with pytest.raises(ValueError, match="CAPTURE_ATTEMPT_CLOSED"):
        call("capture_source", **args, capture_reservation=ticket)
    assert set(vault.rglob("*.md")) == before
    with manager.idempotency._connect() as conn:
        assert conn.execute("SELECT count(*) FROM mutations").fetchone()[0] == 0


def test_success_has_a_retained_receipt_and_exact_retry_does_not_execute(attempts, monkeypatch):
    _, call = attempts
    args = authored()
    ticket = reserve(call, args)
    first = call("capture_source", **args, capture_reservation=ticket)
    assert first["capture_outcome"] == "created"
    monkeypatch.setattr(commands, "_add_source", lambda *a, **k: pytest.fail("replayed attempt executed"))
    again = call("capture_source", **args, capture_reservation=ticket)
    assert again["replayed"] is True
    settled = call("settle_source_capture", reservation=ticket)
    assert settled["past_outcome"] == "created"
    assert settled["capture_receipt"]["source"]["path"] == first["source"]["path"]


def test_exact_unicode_and_order_bind_before_normalization(attempts):
    _, call = attempts
    args = authored()
    ticket = reserve(call, args)
    changed = {**args, "content": args["content"].replace("é", "e\u0301")}
    with pytest.raises(ValueError, match="CAPTURE_ATTEMPT_INPUT_MISMATCH"):
        call("capture_source", **changed, capture_reservation=ticket)
    assert capture_attempts.CaptureWireV1.model_validate({**args, "tags": ["a", "b"]}).sha256() != capture_attempts.CaptureWireV1.model_validate({**args, "tags": ["b", "a"]}).sha256()
    assert call("settle_source_capture", reservation=ticket)["past_outcome"] == "not_started"
    ordered = {**args, "tags": ["a", "b"]}
    ticket = reserve(call, ordered)
    with pytest.raises(ValueError, match="CAPTURE_ATTEMPT_INPUT_MISMATCH"):
        call("capture_source", **{**ordered, "tags": ["b", "a"]}, capture_reservation=ticket)


def test_expired_unused_admission_never_starts_execution(attempts, monkeypatch):
    manager, call = attempts
    args = authored()
    ticket = reserve(call, args)
    monkeypatch.setattr(manager.idempotency, "clock", lambda: ticket["admission_expires_at"] + 1)
    monkeypatch.setattr(commands, "_add_source", lambda *a, **k: pytest.fail("expired attempt executed"))
    with pytest.raises(ValueError, match="CAPTURE_ATTEMPT_CLOSED"):
        call("capture_source", **args, capture_reservation=ticket)
    assert call("settle_source_capture", reservation=ticket)["past_outcome"] == "not_started"


def test_accepted_writer_outlives_expiry_and_settlement_observes_current_state(attempts, monkeypatch):
    manager, call = attempts
    args = authored()
    ticket = reserve(call, args)
    started, release = threading.Event(), threading.Event()
    result, errors = [], []
    real = commands._add_source

    def paused(*a, **k):
        started.set()
        assert release.wait(10)
        return real(*a, **k)

    monkeypatch.setattr(commands, "_add_source", paused)

    def capture():
        try:
            result.append(call("capture_source", **args, capture_reservation=ticket))
        except Exception as error:  # noqa: BLE001 - report the worker failure to the test thread
            errors.append(error)

    thread = threading.Thread(target=capture)
    thread.start()
    try:
        assert started.wait(10)
        monkeypatch.setattr(manager.idempotency, "clock", lambda: ticket["admission_expires_at"] + 1)
        pending = call("settle_source_capture", reservation=ticket)
        assert pending["state"] == "pending" and pending["admission_closed_here"] is None
    finally:
        release.set()
        thread.join(10)
    assert not errors and result
    settled = call("settle_source_capture", reservation=ticket)
    assert settled["state"] == "closed" and settled["past_outcome"] == "created"


def test_another_process_cannot_close_while_the_canonical_boundary_is_held(vault, attempts):
    manager, call = attempts
    ticket = reserve(call, authored())
    script = """
import json, sys
from pathlib import Path
from exomem import commands, writer_lease
from exomem.governance.principal import owner_principal, request_scope
root, state = Path(sys.argv[1]), Path(sys.argv[2])
manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=state), mutation_timeout_seconds=0.03)
writer_lease._ACTIVE_LEASE_MANAGER.set(manager)
with request_scope(owner_principal(surface="mcp")):
    command = next(c for c in commands.PRODUCT_COMMANDS if c.name == "settle_source_capture")
    print(json.dumps(manager.invoke(command, (root,), {"reservation": json.loads(sys.argv[3])})))
"""
    with manager.mutation_guard(vault):
        peer = subprocess.run([sys.executable, "-c", script, str(vault), str(manager.config.state_dir), json.dumps(ticket)], capture_output=True, text=True, timeout=20, check=True)
    result = json.loads(peer.stdout)
    assert result["state"] == "pending" and result["admission_closed_here"] is None
    assert result["observed_scope"] == ticket["scope"]
    assert call("settle_source_capture", reservation=ticket)["past_outcome"] == "not_started"


def test_missing_receipt_and_collected_row_close_locally_with_unknown_past(attempts, monkeypatch):
    manager, call = attempts
    args = authored()
    ticket = reserve(call, args)
    ledger = manager.idempotency.capture_attempts
    real = ledger.complete
    monkeypatch.setattr(ledger, "complete", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("receipt unavailable")))
    with pytest.raises(writer_lease._PostCommitOutcomeUncertain):
        call("capture_source", **args, capture_reservation=ticket)
    monkeypatch.setattr(ledger, "complete", real)
    result = call("settle_source_capture", reservation=ticket)
    assert result["state"] == "closed" and result["past_outcome"] == "unknown"
    with manager.idempotency._connect() as conn:
        conn.execute("DELETE FROM capture_attempts WHERE ticket=?", (ticket["ticket"],))
    assert call("settle_source_capture", reservation=ticket)["past_outcome"] == "unknown"
    with pytest.raises(ValueError, match="CAPTURE_ATTEMPT_CLOSED"):
        call("capture_source", **args, capture_reservation=ticket)


def test_another_process_preserves_unused_and_never_reexecutes_accepted(vault, attempts):
    manager, call = attempts
    args = authored()
    unused = reserve(call, args)
    interrupted = reserve(call, args)
    with manager.idempotency._connect() as conn:
        conn.execute("UPDATE capture_attempts SET state='accepted' WHERE ticket=?", (interrupted["ticket"],))
    script = """
import json, sys
from pathlib import Path
from exomem import commands, schema, writer_lease
from exomem.governance.principal import owner_principal, request_scope
root, state = Path(sys.argv[1]), Path(sys.argv[2])
args, unused, interrupted = json.loads(sys.argv[3])
manager = writer_lease.LeaseManager(writer_lease.LeaseConfig(state_dir=state))
writer_lease._ACTIVE_LEASE_MANAGER.set(manager)
with request_scope(owner_principal(surface="mcp")):
    capture = next(c for c in commands.PRODUCT_COMMANDS if c.name == "capture_source")
    settle = next(c for c in commands.PRODUCT_COMMANDS if c.name == "settle_source_capture")
    captured = manager.invoke(capture, (root, schema.load_source_schema(root)), {**args, "capture_reservation": unused})
    closed = manager.invoke(settle, (root,), {"reservation": interrupted})
print(json.dumps({"captured": captured, "closed": closed}))
"""
    peer = subprocess.run([sys.executable, "-c", script, str(vault), str(manager.config.state_dir), json.dumps([args, unused, interrupted])], capture_output=True, text=True, timeout=20, check=True)
    result = json.loads(peer.stdout)
    assert result["captured"]["capture_outcome"] == "created"
    assert result["closed"]["admission_closed_here"] is True and result["closed"]["past_outcome"] == "unknown"


def test_changed_ledger_or_actual_boundary_reports_observed_unknown(vault, attempts):
    manager, call = attempts
    args = authored()
    ticket = reserve(call, args)
    for field in ("ledger_id", "boundary_id", "replica_id", "cell_id"):
        changed = {**ticket, "scope": {**ticket["scope"], field: "other"}}
        result = call("settle_source_capture", reservation=changed)
        assert result["state"] == "unknown" and result["admission_closed_here"] is None
        assert result["observed_scope"] == ticket["scope"]


def test_a_fresh_replacement_database_has_new_custody(vault, tmp_path, attempts):
    manager, call = attempts
    ticket = reserve(call, authored())
    replacement = writer_lease.LeaseManager(manager.config)
    replacement.idempotency = writer_lease.IdempotencyStore(manager.idempotency.path.with_name("replacement.sqlite"))
    token = writer_lease._ACTIVE_LEASE_MANAGER.set(replacement)
    try:
        with request_scope(owner_principal(surface="mcp")):
            result = dispatch(replacement, vault, "settle_source_capture", {"reservation": ticket})
    finally:
        writer_lease._ACTIVE_LEASE_MANAGER.reset(token)
    assert result["state"] == "unknown" and result["admission_closed_here"] is None
    assert result["observed_scope"]["ledger_id"] != ticket["scope"]["ledger_id"]
    assert result["observed_scope"]["boundary_id"] == ticket["scope"]["boundary_id"]


def test_actual_boundary_change_does_not_reuse_the_ledger_proof(tmp_path, attempts):
    manager, call = attempts
    ticket = reserve(call, authored())
    other = tmp_path / "other-vault"
    (other / "Knowledge Base").mkdir(parents=True)
    token = writer_lease._ACTIVE_LEASE_MANAGER.set(manager)
    try:
        with request_scope(owner_principal(surface="mcp")):
            result = dispatch(manager, other, "settle_source_capture", {"reservation": ticket})
    finally:
        writer_lease._ACTIVE_LEASE_MANAGER.reset(token)
    assert result["state"] == "unknown" and result["admission_closed_here"] is None
    assert result["observed_scope"]["ledger_id"] == ticket["scope"]["ledger_id"]
    assert result["observed_scope"]["boundary_id"] != ticket["scope"]["boundary_id"]


def test_nested_capture_cannot_settle_or_collect_its_active_ancestor(vault, attempts, monkeypatch):
    manager, call = attempts
    args = authored()
    outer = reserve(call, args)
    inner_args = {**args, "source_identity": {**args["source_identity"], "record_id": "inner"}}
    inner = reserve(call, inner_args)
    real = commands._add_source
    observed = []

    def nested(*a, **k):
        if k["source_identity"]["record_id"] == "survey":
            call("capture_source", **inner_args, capture_reservation=inner)
        else:
            observed.append(call("settle_source_capture", reservation=outer))
            manager.idempotency.capture_attempts.collect(manager._mutation_coordinator_for(vault))
        return real(*a, **k)

    monkeypatch.setattr(commands, "_add_source", nested)
    call("capture_source", **args, capture_reservation=outer)
    assert observed[0]["state"] == "pending"
    assert call("settle_source_capture", reservation=outer)["past_outcome"] == "created"


def test_authority_precedes_ledger_and_is_rechecked_on_terminal(vault, attempts, monkeypatch):
    _, call = attempts
    args = authored()
    ticket = reserve(call, args)
    def forbidden(*a, **k):
        raise source_capture.SourceCaptureError("SOURCE_IDENTITY_FORBIDDEN")
    monkeypatch.setattr(source_capture, "require_authority", forbidden)
    for name, arguments in (("reserve_source_capture", {"source_identity": args["source_identity"], "request_sha256": "a" * 64}), ("settle_source_capture", {"reservation": ticket}), ("capture_source", {**args, "capture_reservation": ticket})):
        with pytest.raises(ValueError, match="SOURCE_IDENTITY_FORBIDDEN"):
            call(name, **arguments)


def test_wire_defaults_are_one_exact_json_contract():
    wire = capture_attempts.CaptureWireV1.model_validate(authored())
    value = json.loads(wire.model_dump_json())
    assert value["raw_protection"] is False
    assert value["source_type"] is None and value["passages"] is None
    assert wire.sha256() == capture_attempts.CaptureWireV1.model_validate(value).sha256()


def test_copied_context_on_another_thread_cannot_impersonate_a_boundary_holder(vault, attempts, monkeypatch):
    manager, call = attempts
    args = authored()
    ticket = reserve(call, args)
    real = commands._add_source
    observed = []

    def copied(*a, **k):
        context = contextvars.copy_context()

        def settle():
            boundary = manager._mutation_coordinator_for(vault)
            observed.append(boundary.current_thread_holds_boundary())
            observed.append(call("settle_source_capture", reservation=ticket))

        thread = threading.Thread(target=lambda: context.run(settle))
        thread.start()
        thread.join(10)
        assert not thread.is_alive()
        return real(*a, **k)

    monkeypatch.setattr(commands, "_add_source", copied)
    call("capture_source", **args, capture_reservation=ticket)
    assert observed[0] is False and observed[1]["state"] == "pending"
    assert call("settle_source_capture", reservation=ticket)["past_outcome"] == "created"


def test_gc_excludes_every_active_ancestor_even_during_terminal_persistence(vault, attempts, monkeypatch):
    manager, call = attempts
    args = authored()
    ticket = reserve(call, args)
    ledger = manager.idempotency.capture_attempts
    complete = ledger.complete

    def collect_during_terminal(handle, receipt, **policy):
        complete(handle, receipt, **policy)
        with manager.idempotency._connect() as conn:
            conn.execute("UPDATE capture_attempts SET gc_after=0 WHERE ticket=?", (handle,))
        with manager.mutation_guard(vault) as boundary:
            ledger.collect(boundary)
        with manager.idempotency._connect() as conn:
            assert conn.execute("SELECT state FROM capture_attempts WHERE ticket=?", (handle,)).fetchone()[0] == "completed"

    monkeypatch.setattr(ledger, "complete", collect_during_terminal)
    call("capture_source", **args, capture_reservation=ticket)
    with manager.mutation_guard(vault) as boundary:
        assert ledger.collect(boundary) == 1
    assert call("settle_source_capture", reservation=ticket)["past_outcome"] == "unknown"


def test_gc_is_bounded_and_never_expires_accepted_execution(vault, attempts, monkeypatch):
    manager, call = attempts
    args = authored()
    ticket = reserve(call, args)
    with manager.idempotency._connect() as conn:
        conn.executemany("INSERT INTO capture_attempts(ticket,boundary_id,reservation,principal,state,expires_at,retention_seconds) VALUES (?,?,?,?,'reserved',0,10)", [(f"expired-{n}", ticket["scope"]["boundary_id"], "{}", "principal") for n in range(capture_attempts.GC_BATCH_SIZE + 2)])
        conn.execute("INSERT INTO capture_attempts(ticket,boundary_id,reservation,principal,state,expires_at,retention_seconds) VALUES ('other-boundary','other','{}','principal','reserved',0,10)")
        conn.execute("UPDATE capture_attempts SET state='accepted',expires_at=0 WHERE ticket=?", (ticket["ticket"],))
    with manager.mutation_guard(vault) as boundary:
        assert manager.idempotency.capture_attempts.collect(boundary) == capture_attempts.GC_BATCH_SIZE
    with manager.idempotency._connect() as conn:
        assert conn.execute("SELECT state FROM capture_attempts WHERE ticket=?", (ticket["ticket"],)).fetchone()[0] == "accepted"
        assert conn.execute("SELECT state FROM capture_attempts WHERE ticket='other-boundary'").fetchone()[0] == "reserved"


def test_direct_cloud_owner_without_origin_or_oauth_client_survives_bearer_rotation(vault, source_schema, attempts):
    from exomem.cloud_cell import issuer_family

    manager, _ = attempts
    args = authored()
    who = replace(owner_principal(surface="mcp"), issuer_family=issuer_family("test-cell"), administrative_ingress=False)
    token = writer_lease._ACTIVE_LEASE_MANAGER.set(manager)
    try:
        with request_scope(replace(who, session_id="old-bearer")):
            ticket = dispatch(manager, vault, "reserve_source_capture", {"source_identity": args["source_identity"], "request_sha256": capture_attempts.CaptureWireV1.model_validate(args).sha256()})["reservation"]
        with request_scope(replace(who, session_id="rotated-bearer")):
            assert dispatch(manager, vault, "capture_source", {**args, "capture_reservation": ticket}, source_schema)["capture_outcome"] == "created"
            assert dispatch(manager, vault, "settle_source_capture", {"reservation": ticket})["past_outcome"] == "created"
    finally:
        writer_lease._ACTIVE_LEASE_MANAGER.reset(token)


def test_verified_client_binding_refuses_another_client(vault, attempts):
    from exomem.governance.principal import ClientBinding

    manager, _ = attempts
    args = authored()
    who = owner_principal(surface="mcp")
    token = writer_lease._ACTIVE_LEASE_MANAGER.set(manager)
    try:
        with request_scope(replace(who, client_binding=ClientBinding("issuer", "first"))):
            ticket = dispatch(manager, vault, "reserve_source_capture", {"source_identity": args["source_identity"], "request_sha256": capture_attempts.CaptureWireV1.model_validate(args).sha256()})["reservation"]
        with request_scope(replace(who, client_binding=ClientBinding("issuer", "second"))):
            with pytest.raises(ValueError, match="CAPTURE_ATTEMPT_INVALID"):
                dispatch(manager, vault, "settle_source_capture", {"reservation": ticket})
    finally:
        writer_lease._ACTIVE_LEASE_MANAGER.reset(token)


def test_terminal_authority_revocation_refuses_a_completed_receipt(vault, attempts, monkeypatch):
    manager, call = attempts
    args = authored()
    ticket = reserve(call, args)
    ledger = manager.idempotency.capture_attempts
    real = ledger.complete

    def revoke(handle, receipt, **policy):
        real(handle, receipt, **policy)

        def denied(root):
            raise source_capture.SourceCaptureError("SOURCE_IDENTITY_FORBIDDEN")

        monkeypatch.setattr(source_capture, "require_authority", denied)

    monkeypatch.setattr(ledger, "complete", revoke)
    with pytest.raises(writer_lease._PostCommitOutcomeUncertain):
        call("capture_source", **args, capture_reservation=ticket)
    with pytest.raises(ValueError, match="SOURCE_IDENTITY_FORBIDDEN"):
        call("settle_source_capture", reservation=ticket)


def test_all_attempt_operations_skip_generic_keys_and_saved_terminal_lookup(attempts, monkeypatch):
    manager, call = attempts
    monkeypatch.setattr(writer_lease, "_effective_idempotency_key", lambda *a, **k: pytest.fail("generic key derived"))
    monkeypatch.setattr(manager.idempotency, "completed_terminal", lambda *a, **k: pytest.fail("generic terminal read"))
    args = authored()
    ticket = reserve(call, args)
    call("capture_source", **args, capture_reservation=ticket)
    assert call("settle_source_capture", reservation=ticket)["past_outcome"] == "created"


def test_settlement_does_no_source_scan_or_recovery_capture(attempts, monkeypatch):
    from exomem import reserved_paths

    _, call = attempts
    ticket = reserve(call, authored())
    monkeypatch.setattr(reserved_paths, "warm_identity_catalogue_before_boundary", lambda *a: pytest.fail("settlement scanned Sources"))
    monkeypatch.setattr(commands, "op_capture_source", lambda *a, **k: pytest.fail("settlement recovered capture"))
    assert call("settle_source_capture", reservation=ticket)["past_outcome"] == "not_started"


def test_passage_wire_hash_precedes_joining_and_excludes_guidance(attempts):
    _, call = attempts
    args = authored()
    args.pop("content")
    args["passages"] = [{"key": "one", "text": "The café opens."}, {"key": "two", "text": "The café closes."}]
    ticket = reserve(call, args)
    captured = call("capture_source", **args, capture_reservation=ticket, compile_guidance=False, suggested_title="Presentation only")
    assert captured["capture_outcome"] == "created"
    assert call("settle_source_capture", reservation=ticket)["past_outcome"] == "created"


@pytest.mark.parametrize("extra", [{"files": [{"file_id": "one"}]}, {"adoption": {}}, {"correction": {}}, {"source_identity": None}])
def test_attempt_protocol_refuses_non_text_capture_combinations(attempts, extra):
    _, call = attempts
    args = authored()
    ticket = reserve(call, args)
    with pytest.raises(ValueError, match="CAPTURE_ATTEMPT_INVALID"):
        call("capture_source", **{**args, **extra}, capture_reservation=ticket)


def test_ticketed_capture_adds_no_authority_acquisition_and_persists_the_terminal_under_guard(vault, source_schema, attempts, monkeypatch):
    manager, call = attempts
    args = authored()
    ticket = reserve(call, args)
    authorities = []
    authority = manager.writer_authority_guard

    @contextmanager
    def counted(**kwargs):
        authorities.append(kwargs)
        with authority(**kwargs):
            yield

    monkeypatch.setattr(manager, "writer_authority_guard", counted)
    with request_scope(owner_principal(surface="mcp")):
        dispatch(manager, vault, "capture_source", {**args, "content": "Another original.", "source_identity": {**args["source_identity"], "record_id": "ordinary"}}, source_schema)
    ordinary = len(authorities)
    authorities.clear()
    ledger = manager.idempotency.capture_attempts
    complete = ledger.complete

    def persist(handle, terminal, **policy):
        boundary = manager._mutation_coordinator_for(vault)
        assert boundary.current_thread_holds_boundary() and not boundary.current_thread_borrows_boundary()
        assert terminal["_terminal"]
        assert len(authorities) == ordinary
        complete(handle, terminal, **policy)

    monkeypatch.setattr(ledger, "complete", persist)
    call("capture_source", **args, capture_reservation=ticket)
    assert len(authorities) == ordinary
    authorities.clear()
    reserve(call, args)
    assert len(authorities) == 1
    authorities.clear()
    call("settle_source_capture", reservation=ticket)
    assert len(authorities) == 1


def test_internal_ticketed_leaf_requires_its_dispatch_lifecycle(vault, source_schema, attempts):
    _, call = attempts
    args = authored()
    ticket = reserve(call, args)
    with request_scope(owner_principal(surface="mcp")), pytest.raises(ValueError, match="CAPTURE_ATTEMPT_LIFECYCLE_REQUIRED"):
        commands.op_capture_source(vault, source_schema, **args, capture_reservation=ticket)


def test_operator_policy_applies_to_new_deadlines_and_keeps_issued_values(attempts, monkeypatch):
    manager, call = attempts
    monkeypatch.delenv("EXOMEM_CAPTURE_ATTEMPT_ADMISSION_SECONDS", raising=False)
    monkeypatch.delenv("EXOMEM_CAPTURE_ATTEMPT_RETENTION_SECONDS", raising=False)
    config = capture_attempts.CaptureAttemptConfig.from_env()
    assert config.admission_seconds == 600.0 and config.retention_seconds == 604800.0
    monkeypatch.setenv("EXOMEM_CAPTURE_ATTEMPT_ADMISSION_SECONDS", "30")
    monkeypatch.setenv("EXOMEM_CAPTURE_ATTEMPT_RETENTION_SECONDS", "80")
    monkeypatch.setattr(manager.idempotency, "clock", lambda: 1000.0)
    args = authored()
    ticket = reserve(call, args)
    assert ticket["admission_expires_at"] == 1030.0
    monkeypatch.setenv("EXOMEM_CAPTURE_ATTEMPT_ADMISSION_SECONDS", "1")
    monkeypatch.setenv("EXOMEM_CAPTURE_ATTEMPT_RETENTION_SECONDS", "20")
    monkeypatch.setattr(manager.idempotency, "clock", lambda: 1010.0)
    call("capture_source", **args, capture_reservation=ticket)
    with manager.idempotency._connect() as conn:
        assert conn.execute("SELECT expires_at,retention_seconds,gc_after FROM capture_attempts WHERE ticket=?", (ticket["ticket"],)).fetchone() == (1030.0, 80.0, 1090.0)
    monkeypatch.setenv("EXOMEM_CAPTURE_ATTEMPT_RETENTION_SECONDS", "1000")
    monkeypatch.setattr(manager.idempotency, "clock", lambda: 1011.0)
    assert call("settle_source_capture", reservation=ticket)["past_outcome"] == "created"
    with manager.idempotency._connect() as conn:
        assert conn.execute("SELECT gc_after FROM capture_attempts WHERE ticket=?", (ticket["ticket"],)).fetchone()[0] == 1090.0


def test_repeated_closure_does_not_extend_issued_retention(attempts, monkeypatch):
    manager, call = attempts
    monkeypatch.setenv("EXOMEM_CAPTURE_ATTEMPT_RETENTION_SECONDS", "10")
    monkeypatch.setattr(manager.idempotency, "clock", lambda: 1000.0)
    ticket = reserve(call, authored())
    assert call("settle_source_capture", reservation=ticket)["past_outcome"] == "not_started"
    monkeypatch.setenv("EXOMEM_CAPTURE_ATTEMPT_RETENTION_SECONDS", "1000")
    monkeypatch.setattr(manager.idempotency, "clock", lambda: 1001.0)
    call("settle_source_capture", reservation=ticket)
    with manager.idempotency._connect() as conn:
        assert conn.execute("SELECT gc_after FROM capture_attempts WHERE ticket=?", (ticket["ticket"],)).fetchone()[0] == 1010.0


@pytest.mark.parametrize("field", ["ADMISSION", "RETENTION"])
@pytest.mark.parametrize("value", ["not-seconds", "0", "nan", "inf"])
def test_malformed_operator_policy_is_typed_unavailable(monkeypatch, field, value):
    from exomem.cli_ops import OpError

    monkeypatch.setenv(f"EXOMEM_CAPTURE_ATTEMPT_{field}_SECONDS", value)
    with pytest.raises(OpError) as caught:
        capture_attempts.CaptureAttemptConfig.from_env()
    assert caught.value.code == "CAPTURE_ATTEMPT_CONFIG_UNAVAILABLE"


def test_invalid_current_policy_blocks_only_new_reservations(vault, source_schema, attempts, monkeypatch):
    from exomem.cli_ops import OpError

    manager, call = attempts
    args = authored()
    ticket = reserve(call, args)
    unused = reserve(call, args)
    closed = reserve(call, args)
    call("capture_source", **args, capture_reservation=ticket)
    call("settle_source_capture", reservation=closed)
    monkeypatch.setenv("EXOMEM_CAPTURE_ATTEMPT_ADMISSION_SECONDS", "nan")
    monkeypatch.setenv("EXOMEM_CAPTURE_ATTEMPT_RETENTION_SECONDS", "nan")
    with pytest.raises(OpError) as caught:
        reserve(call, args)
    assert caught.value.code == "CAPTURE_ATTEMPT_CONFIG_UNAVAILABLE"
    assert call("capture_source", **args, capture_reservation=ticket)["replayed"] is True
    assert call("settle_source_capture", reservation=ticket)["past_outcome"] == "created"
    assert call("settle_source_capture", reservation=unused)["past_outcome"] == "not_started"
    assert call("settle_source_capture", reservation=closed)["past_outcome"] == "not_started"
    changed = {**ticket, "scope": {**ticket["scope"], "ledger_id": "other"}}
    assert call("settle_source_capture", reservation=changed)["state"] == "unknown"
    with manager.idempotency._connect() as conn:
        conn.execute("DELETE FROM capture_attempts WHERE ticket=?", (unused["ticket"],))
    missing = call("settle_source_capture", reservation=unused)
    assert missing["admission_closed_here"] is True and missing["past_outcome"] == "unknown"
    with request_scope(owner_principal(surface="mcp")):
        assert dispatch(manager, vault, "capture_source", {**args, "source_identity": {**args["source_identity"], "record_id": "ordinary"}}, source_schema)["capture_outcome"] == "created"


@pytest.mark.parametrize("expires", [float("nan"), float("inf"), -float("inf")])
def test_reservation_rejects_nonfinite_expiry_during_typed_parsing(attempts, expires):
    _, call = attempts
    ticket = reserve(call, authored())
    with pytest.raises(ValidationError):
        capture_attempts.CaptureReservation.model_validate({**ticket, "admission_expires_at": expires})


def test_accepted_execution_keeps_its_retention_policy_snapshot(attempts, monkeypatch):
    manager, call = attempts
    monkeypatch.setenv("EXOMEM_CAPTURE_ATTEMPT_RETENTION_SECONDS", "20")
    monkeypatch.setattr(manager.idempotency, "clock", lambda: 1000.0)
    args = authored()
    ticket = reserve(call, args)
    add_source = commands._add_source

    def policy_changes(*args, **kwargs):
        monkeypatch.setenv("EXOMEM_CAPTURE_ATTEMPT_RETENTION_SECONDS", "nan")
        return add_source(*args, **kwargs)

    monkeypatch.setattr(commands, "_add_source", policy_changes)
    assert call("capture_source", **args, capture_reservation=ticket)["capture_outcome"] == "created"
    with manager.idempotency._connect() as conn:
        assert conn.execute("SELECT gc_after FROM capture_attempts WHERE ticket=?", (ticket["ticket"],)).fetchone()[0] == 1020.0


def test_restarted_manager_uses_the_issued_retention_policy(vault, source_schema, attempts, monkeypatch):
    manager, call = attempts
    monkeypatch.setenv("EXOMEM_CAPTURE_ATTEMPT_RETENTION_SECONDS", "80")
    args = authored()
    ticket = reserve(call, args)
    monkeypatch.setenv("EXOMEM_CAPTURE_ATTEMPT_ADMISSION_SECONDS", "invalid")
    monkeypatch.setenv("EXOMEM_CAPTURE_ATTEMPT_RETENTION_SECONDS", "invalid")
    restarted = writer_lease.LeaseManager(manager.config)
    with request_scope(owner_principal(surface="mcp")):
        assert dispatch(restarted, vault, "capture_source", {**args, "capture_reservation": ticket}, source_schema)["capture_outcome"] == "created"
        assert dispatch(restarted, vault, "settle_source_capture", {"reservation": ticket})["past_outcome"] == "created"
    with restarted.idempotency._connect() as conn:
        assert conn.execute("SELECT retention_seconds FROM capture_attempts WHERE ticket=?", (ticket["ticket"],)).fetchone()[0] == 80.0


def test_older_candidate_table_cannot_invent_an_issued_retention_policy(attempts):
    from exomem.cli_ops import OpError

    manager, _ = attempts
    with manager.idempotency._connect() as conn:
        conn.execute("CREATE TABLE capture_attempts (ticket TEXT PRIMARY KEY, boundary_id TEXT NOT NULL, reservation TEXT NOT NULL, principal TEXT NOT NULL, state TEXT NOT NULL, expires_at REAL NOT NULL, gc_after REAL, past_outcome TEXT, receipt TEXT)")
        conn.execute("INSERT INTO capture_attempts(ticket,boundary_id,reservation,principal,state,expires_at) VALUES ('old-candidate','boundary','{}','principal','reserved',0)")
    with pytest.raises(OpError) as caught:
        _ = manager.idempotency.capture_attempts
    assert caught.value.code == "CAPTURE_ATTEMPT_LEDGER_UNAVAILABLE"
    with manager.idempotency._connect() as conn:
        assert "retention_seconds" not in {row[1] for row in conn.execute("PRAGMA table_info(capture_attempts)")}
        assert conn.execute("SELECT state FROM capture_attempts WHERE ticket='old-candidate'").fetchone()[0] == "reserved"
