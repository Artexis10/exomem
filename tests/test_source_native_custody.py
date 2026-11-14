"""Private cleanup keeps custody across restart without adopting another file."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import replace

import pytest

from exomem import held_fs, writer_lease
from exomem import source_native_custody as native
from exomem.cli_ops import OpError
from exomem.governance.principal import owner_principal, request_scope


@contextmanager
def owner(root):
    manager = writer_lease.active_manager()
    with request_scope(owner_principal(surface="mcp")), manager.mutation_guard(root):
        yield native.NativeCustody(root)


def reserve(custody, data=b"complete payload", **kwargs):
    return custody.reserve(relative_path="private.tmp", expected_sha256=hashlib.sha256(data).hexdigest(),
                           expected_size=len(data), **kwargs)


def test_partial_cleanup_after_restart_preserves_bytes_without_live_allocation_proof(vault):
    unrelated = vault / "unrelated.txt"
    unrelated.write_bytes(b"keep")
    with owner(vault) as custody:
        reservation = reserve(custody, source_ref="opaque-source", attempt_ticket="opaque-attempt")
        with held_fs.acquire(vault).require() as filesystem, filesystem.parent(".").require() as parent:
            with filesystem.file(parent, "private.tmp", access="write", create=True, exclusive=True).require() as file:
                assert custody.observe(reservation.binding_id).state == "reserved"
                custody.bind(reservation, file)

                def interrupted():
                    assert custody.observe(reservation.binding_id).state == "initializing"
                    yield b"partial"
                    raise RuntimeError("process lost during chunks")

                with pytest.raises(RuntimeError, match="process lost"):
                    filesystem.write(file, held_fs.Chunks(interrupted))
        binding_id, ledger_id, scope = reservation.binding_id, custody.ledger_id, custody.physical_scope
    writer_lease.reset_managers_for_tests()
    with owner(vault) as recovered:
        assert recovered.ledger_id == ledger_id and recovered.physical_scope == scope
        result = recovered.cleanup(binding_id)
        assert result.state == "unknown" and result.outcome is None
        assert recovered.cleanup(binding_id) == result
    assert (vault / "private.tmp").read_bytes() == b"partial"
    assert unrelated.read_bytes() == b"keep"


def test_partial_cleanup_uses_the_original_live_allocation(vault):
    with owner(vault) as custody:
        reservation = reserve(custody)
        with held_fs.acquire(vault).require() as filesystem, filesystem.parent(".").require() as parent:
            with filesystem.file(parent, "private.tmp", access="write", create=True, exclusive=True).require() as file:
                custody.bind(reservation, file)
                filesystem.write(file, b"partial").require()
                assert custody.cleanup(reservation.binding_id).outcome == "removed"
    assert not (vault / "private.tmp").exists()


def test_reused_name_and_complete_in_place_drift_are_preserved(vault):
    with owner(vault) as custody:
        reservation = reserve(custody)
        with held_fs.acquire(vault).require() as filesystem, filesystem.parent(".").require() as parent:
            with filesystem.file(parent, "private.tmp", access="write", create=True, exclusive=True).require() as file:
                custody.bind(reservation, file)
                filesystem.write(file, b"complete payload").require()
                custody.verified(reservation, file, sha256=hashlib.sha256(b"complete payload").hexdigest())
            (vault / "replacement.tmp").write_bytes(b"complete payload")
            os.replace(vault / "replacement.tmp", vault / "private.tmp")
            assert custody.cleanup(reservation.binding_id).state == "unknown"
            assert (vault / "private.tmp").read_bytes() == b"complete payload"
        changed = custody.reserve(relative_path="drift.tmp", expected_sha256=hashlib.sha256(b"original").hexdigest(), expected_size=8)
        with held_fs.acquire(vault).require() as filesystem, filesystem.parent(".").require() as parent:
            with filesystem.file(parent, "drift.tmp", access="write", create=True, exclusive=True).require() as file:
                custody.bind(changed, file)
                filesystem.write(file, b"original").require()
                custody.verified(changed, file, sha256=hashlib.sha256(b"original").hexdigest())
            (vault / "drift.tmp").write_bytes(b"modified")
            result = custody.cleanup(changed.binding_id)
            assert result.state == "unknown" and result.matching is False
    assert (vault / "drift.tmp").read_bytes() == b"modified"


def test_destination_without_installation_witness_stays_unknown(vault):
    with owner(vault) as custody:
        reservation = reserve(custody, destination_relative="installed.txt")
        with held_fs.acquire(vault).require() as filesystem, filesystem.parent(".", access="mutate").require() as parent:
            with filesystem.file(parent, "private.tmp", access="write", create=True, exclusive=True).require() as file:
                custody.bind(reservation, file)
                filesystem.write(file, b"complete payload").require()
                custody.verified(reservation, file, sha256=hashlib.sha256(b"complete payload").hexdigest())
            with filesystem.file(parent, "private.tmp", access="mutate").require() as file:
                filesystem.rename(file, parent, "installed.txt").require()
            observed = custody.observe(reservation.binding_id, destination=True)
            assert observed.state == "unknown" and observed.matching is False
            assert custody.cleanup(reservation.binding_id).state == "unknown"
            custody.acknowledge(reservation, observed.identity, "installed")
            assert custody.cleanup(reservation.binding_id).state == "installed"
            (vault / "replacement.tmp").write_bytes(b"complete payload")
            os.replace(vault / "replacement.tmp", vault / "installed.txt")
            foreign = custody.observe(reservation.binding_id, destination=True)
            assert foreign.state == "unknown" and foreign.matching is False
    assert (vault / "installed.txt").read_bytes() == b"complete payload"


def test_installed_version_survives_restart_without_hashing_and_rejects_equal_byte_drift(vault, monkeypatch):
    with owner(vault) as custody:
        reservation = reserve(custody, destination_relative="installed.txt", removable=False)
        with held_fs.acquire(vault).require() as filesystem, filesystem.parent(".", access="mutate").require() as parent:
            with filesystem.file(parent, "private.tmp", access="write", create=True, exclusive=True).require() as file:
                custody.bind(reservation, file)
                filesystem.write(file, b"complete payload").require()
                custody.verified(reservation, file, sha256=hashlib.sha256(b"complete payload").hexdigest())
            verified = custody.get(reservation.binding_id).version
            held_fs.install_temporary(
                filesystem, parent, "private.tmp", "installed.txt",
                expected_identity=verified.identity, expected_version=verified,
                after_install=lambda witness: custody.record_installation(reservation, witness),
            ).require()
        installed = custody.get(reservation.binding_id).version
    writer_lease.reset_managers_for_tests()

    def unexpected_hash(*args, **kwargs):
        pytest.fail("exact installed version needs no content scan")

    monkeypatch.setattr(native.reserved_paths, "_digest_descriptor", unexpected_hash)
    with owner(vault) as recovered:
        for _ in range(3):
            observed = recovered.observe(reservation.binding_id, destination=True)
            assert observed.state == "installed" and observed.matching is True
            assert observed.version == installed
        (vault / "installed.txt").write_bytes(b"complete payload")
        changed = installed.mtime_ns + 1_000_000
        os.utime(vault / "installed.txt", ns=(changed, changed))
        observed = recovered.observe(reservation.binding_id, destination=True)
        assert observed.state == "unknown" and observed.matching is False
    assert (vault / "installed.txt").read_bytes() == b"complete payload"


@pytest.mark.parametrize("field", ["before", "root_identity", "parent_identity", "leaf", "_seal"])
def test_installation_witness_must_match_the_verified_owner_allocation(vault, field):
    with owner(vault) as custody:
        reservation = reserve(custody, destination_relative="installed.txt", removable=False)
        witnesses = []
        with held_fs.acquire(vault).require() as filesystem, filesystem.parent(".", access="mutate").require() as parent:
            with filesystem.file(parent, "private.tmp", access="write", create=True, exclusive=True).require() as file:
                custody.bind(reservation, file)
                filesystem.write(file, b"complete payload").require()
                custody.verified(reservation, file, sha256=hashlib.sha256(b"complete payload").hexdigest())
            verified = custody.get(reservation.binding_id).version
            held_fs.install_temporary(
                filesystem, parent, "private.tmp", "installed.txt",
                expected_identity=verified.identity, expected_version=verified, after_install=witnesses.append,
            ).require()
        witness = witnesses[0]
        if field == "before":
            changed = replace(witness.before, mtime_ns=witness.before.mtime_ns + 1)
        elif field in ("root_identity", "parent_identity"):
            identity = getattr(witness, field)
            changed = replace(identity, inode=identity.inode + 1)
        elif field == "leaf":
            changed = "another.txt"
        else:
            changed = object()
        with pytest.raises(ValueError):
            custody.record_installation(reservation, replace(witness, **{field: changed}))
        assert custody.observe(reservation.binding_id, destination=True).state == "unknown"
        custody.record_installation(reservation, witness)
        assert custody.observe(reservation.binding_id, destination=True).matching is True


def test_unbound_cleanup_never_adopts_file_and_missing_started_write_stays_unknown(vault):
    with owner(vault) as custody:
        reservation = reserve(custody)
        (vault / "private.tmp").write_bytes(b"unbound")
        assert custody.cleanup(reservation.binding_id).outcome == "not_written"
        assert (vault / "private.tmp").read_bytes() == b"unbound"
        started = custody.reserve(relative_path="started.tmp", expected_sha256="a" * 64, expected_size=5)
        with held_fs.acquire(vault).require() as filesystem, filesystem.parent(".").require() as parent:
            with filesystem.file(parent, "started.tmp", access="write", create=True, exclusive=True).require() as file:
                custody.bind(started, file)
        (vault / "started.tmp").unlink()
        assert custody.cleanup(started.binding_id).state == "unknown"
        custody.acknowledge(started, custody.get(started.binding_id).identity, "removed")
        assert custody.cleanup(started.binding_id).outcome == "removed"


def test_nonremovable_binding_cannot_delete_a_file(vault):
    with owner(vault) as custody:
        reservation = reserve(custody, removable=False)
        with held_fs.acquire(vault).require() as filesystem, filesystem.parent(".").require() as parent:
            with filesystem.file(parent, "private.tmp", access="write", create=True, exclusive=True).require() as file:
                custody.bind(reservation, file)
                filesystem.write(file, b"complete payload").require()
                custody.verified(reservation, file, sha256=hashlib.sha256(b"complete payload").hexdigest())
        assert custody.cleanup(reservation.binding_id).state == "unknown"
    assert (vault / "private.tmp").read_bytes() == b"complete payload"


def test_parent_replacement_and_another_physical_root_cannot_claim_allocation(vault, tmp_path):
    original_parent = vault / "private"
    original_parent.mkdir()
    with owner(vault) as custody:
        reservation = custody.reserve(relative_path="private/part.tmp", expected_sha256="a" * 64, expected_size=5)
        with held_fs.acquire(vault).require() as filesystem, filesystem.parent("private").require() as parent:
            with filesystem.file(parent, "part.tmp", access="write", create=True, exclusive=True).require() as file:
                custody.bind(reservation, file)
                filesystem.write(file, b"partial").require()
        original_parent.rename(vault / "preserved-parent")
        original_parent.mkdir()
        (original_parent / "part.tmp").write_bytes(b"replacement")
        assert custody.cleanup(reservation.binding_id).state == "unknown"
    other_root = tmp_path / "another-root"
    other_root.mkdir()
    with owner(other_root) as other:
        assert other.observe(reservation.binding_id).state == "unknown"
    assert (original_parent / "part.tmp").read_bytes() == b"replacement"
    assert (vault / "preserved-parent/part.tmp").read_bytes() == b"partial"


def test_current_private_state_anchor_supports_exact_cleanup(vault):
    with owner(vault):
        state_dir = writer_lease.active_manager().idempotency.state_dir
        custody = native.NativeCustody(vault, anchor=state_dir)
        reservation = reserve(custody)
        with held_fs.acquire(state_dir).require() as filesystem, filesystem.parent(".").require() as parent:
            with filesystem.file(parent, "private.tmp", access="write", create=True, exclusive=True).require() as file:
                custody.bind(reservation, file)
                filesystem.write(file, b"complete payload").require()
                custody.verified(reservation, file, sha256=hashlib.sha256(b"complete payload").hexdigest())
        assert custody.cleanup(reservation.binding_id).outcome == "removed"
    assert not (state_dir / "private.tmp").exists()


def test_binding_requires_empty_exact_named_allocation_and_reservation_absence(vault):
    with owner(vault) as custody:
        reservation = reserve(custody)
        with held_fs.acquire(vault).require() as filesystem, filesystem.parent(".").require() as parent:
            with filesystem.file(parent, "other.tmp", access="write", create=True, exclusive=True).require() as other:
                with pytest.raises(ValueError):
                    custody.bind(reservation, other)
            with filesystem.file(parent, "private.tmp", access="write", create=True, exclusive=True).require() as file:
                filesystem.write(file, b"already written").require()
                with pytest.raises(ValueError):
                    custody.bind(reservation, file)
        with pytest.raises(held_fs.HeldFsError):
            reserve(custody)


def test_boundary_root_ledger_and_sealed_reservation_cannot_be_borrowed(vault, tmp_path):
    with pytest.raises(OpError, match="SOURCE_CUSTODY_BOUNDARY_REQUIRED"):
        native.NativeCustody(vault)
    with owner(vault) as custody:
        reservation = reserve(custody)
        reopened = native.NativeCustody(vault)
        with pytest.raises(ValueError, match="reservation"):
            reopened.acknowledge(reservation, None, "not_written")
        reopened.acknowledge(reopened.reopen(reservation.binding_id), None, "not_written")
        outside = tmp_path / "outside"
        outside.mkdir()
        with pytest.raises(ValueError, match="anchor"):
            native.NativeCustody(vault, anchor=outside)
        (vault / "added-directory").mkdir()
        assert native.NativeCustody(vault).physical_scope == custody.physical_scope
        with writer_lease.active_manager().idempotency._connect() as conn:
            conn.execute("UPDATE capture_attempt_ledger SET ledger_id=?", ("b" * 64,))
        assert custody.lookup().state == "unknown"
        assert custody.observe(reservation.binding_id).state == "unknown"


def test_lookup_uses_bounded_indexed_pages_among_unrelated_bindings(vault):
    with owner(vault) as custody:
        chosen = [reserve(custody, source_ref="selected", attempt_ticket="target") for _ in range(3)]
        store = writer_lease.active_manager().idempotency
        with store._connect() as conn:
            row = conn.execute("SELECT * FROM source_native_bindings WHERE binding_id=?", (chosen[0].binding_id,)).fetchone()
            names = [column[1] for column in conn.execute("PRAGMA table_info(source_native_bindings)")]
            template = dict(zip(names, row, strict=True))
            template.update(source_ref="unrelated", attempt_ticket="elsewhere")
            conn.executemany(f"INSERT INTO source_native_bindings ({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
                             [tuple(uuid.uuid4().hex if name == "binding_id" else template[name] for name in names) for _ in range(20_000)])
        original = store._connect
        work = []

        def measured():
            connection = original()
            connection.set_progress_handler(lambda: work.append(1) or 0, 100)
            return connection

        store._connect = measured
        try:
            first = custody.lookup(source_ref="selected", attempt_ticket="target", limit=2)
            second = custody.lookup(source_ref="selected", attempt_ticket="target", limit=2, cursor=first.cursor)
        finally:
            store._connect = original
        assert first.state == "partial" and second.state == "ready"
        assert {item.binding_id for item in first.reservations + second.reservations} == {item.binding_id for item in chosen}
        assert len(work) < 30
        with pytest.raises(ValueError):
            custody.lookup(limit=129)


def test_unavailable_private_database_does_not_report_empty_proof(vault, monkeypatch):
    with owner(vault) as custody:
        reservation = reserve(custody)

        def unavailable():
            raise RuntimeError("private database unavailable")

        monkeypatch.setattr(writer_lease.active_manager().idempotency, "_connect", unavailable)
        assert custody.lookup().state == "unknown"
        assert custody.observe(reservation.binding_id).state == "unknown"


def test_replaced_database_cannot_reuse_cached_ledger_even_with_identical_rows(vault, tmp_path):
    with owner(vault) as custody:
        reservation = reserve(custody)
        store = writer_lease.active_manager().idempotency
        replacement_path = tmp_path / "replacement.sqlite3"
        replacement = sqlite3.connect(replacement_path)
        try:
            with store._connect() as conn:
                conn.backup(replacement)
        finally:
            replacement.close()
        os.replace(replacement_path, store.path)
        assert custody.lookup().state == "unknown"
        assert custody.observe(reservation.binding_id).state == "unknown"
