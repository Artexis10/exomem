"""Characterize the hosted commit/acknowledgement cut using an enrolled cell.

Run as an unprivileged Linux user. The same module can run inside the immutable
runtime image with its tests/dependencies mounted, without Kubernetes or a server.
The legacy failure assertions remain evidence, not acceptance of a repaired path.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest
from test_hosted_governance_job import _canonical, _replace_membership
from test_hosted_governance_job import cell as cell
from test_hosted_governance_migration import _offline_state as _offline_state
from test_hosted_governance_recovery import _enrolled

from exomem import capabilities, commands, graph_sync, hosted_gateway, writer_lease
from exomem.cli_ops import OpError
from exomem.governance import (
    authorization_custody,
    catalog_publication,
    principal,
    schema_v4,
    store,
)

pytestmark = pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="Custody permission characterization requires unprivileged Linux",
)


@pytest.fixture
def serving_capture(cell, monkeypatch):
    binding, now, _ = cell
    import exomem

    scaffold = Path(exomem.__file__).parent / "_scaffold"
    for path in scaffold.rglob("*"):
        if path.is_file():
            target = binding.vault_root / "Knowledge Base" / path.relative_to(scaffold)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
    binding, now, job, request, _ = _enrolled(cell, monkeypatch)
    assert job.execute(_canonical(request), now=now)["actualSchema"] == 4
    _replace_membership(
        binding,
        now,
        state="SERVING",
        schema_version=4,
        issuance_stopped=False,
        no_in_flight=False,
    )
    profile = commands.HOSTED_ALPHA_AGENT_V4_PROFILE
    command = next(
        c for c in commands.product_commands_for_profile(profile, "rest") if c.name == "remember"
    )
    args = {
        "title": "Synthetic acknowledgement probe",
        "content": "## Observations\n- [acceptance] The key is in a violet ceramic owl. #hosted ^ack-probe",
        "note_type": "insight",
        "sources": [],
        "response_detail": "full",
    }
    with (
        capabilities.active_surface(hosted_gateway.hosted_agent_surface_descriptor(profile)),
        principal.request_scope(principal.resolve_hosted_principal("principal:" + "a" * 64)),
    ):
        validation = writer_lease.invoke_command(
            command, binding.vault_root, validate_only=True, **args
        )
        validation = validation.get("leaf_result", validation)
        commit = {**args, **{k: validation[k] for k in ("draft_id", "draft_hash", "draft_token")}}
        if validation.get("reviewed_none_required"):
            commit.update(
                relation_disposition="reviewed_none",
                relation_review_hash=validation["draft_hash"],
                relation_review_reason="Isolated synthetic fixture with no related note.",
            )
        yield binding.vault_root, now, command, commit, validation["destination"]


@contextmanager
def readonly_control():
    directory = Path(os.environ[authorization_custody.CONTROL_FILE_ENV]).parent
    original = {path: path.stat().st_mode & 0o777 for path in directory.iterdir()}
    directory_mode = directory.stat().st_mode & 0o777
    try:
        for path in original:
            path.chmod(0o400)
        directory.chmod(0o500)
        yield
    finally:
        directory.chmod(directory_mode)
        for path, mode in original.items():
            if path.exists():
                path.chmod(mode)


def _epochs(root, now):
    custody = authorization_custody.load_authorization_custody(root, now=now)
    with sqlite3.connect(store.sidecar_path(root)) as connection:
        epoch = connection.execute(
            "SELECT activation_epoch FROM governance_activation_store"
        ).fetchone()[0]
    return epoch, custody.control.activation_epoch


def test_writable_custody_control_completes_capture(serving_capture):
    root, now, command, commit, destination = serving_capture
    result = writer_lease.invoke_command(command, root, idempotency_key="ack-control", **commit)
    assert result["state"] == "committed"
    assert (root / destination).is_file()
    assert _epochs(root, now) == (2, 2)


def _lose_custody_at_publication(monkeypatch):
    publish = catalog_publication.publish_markdown_batch

    def interrupted(prepared):
        with readonly_control():
            return publish(prepared)

    monkeypatch.setattr(catalog_publication, "publish_markdown_batch", interrupted)


def test_custody_loss_after_preparation_retains_canonical_uncertainty(serving_capture, monkeypatch):
    root, now, command, commit, destination = serving_capture
    _lose_custody_at_publication(monkeypatch)
    with pytest.raises(OpError) as caught:
        writer_lease.invoke_command(command, root, idempotency_key="ack-readonly", **commit)
    assert caught.value.code == "MUTATION_COMMITTED_ACKNOWLEDGEMENT_UNCERTAIN"
    assert (root / destination).is_file()
    assert _epochs(root, now) == (2, 1)
    causes = []
    error = caught.value
    while error is not None:
        causes.append(type(error))
        error = error.__cause__
    assert authorization_custody.AuthorizationCustodyUnavailable in causes


def test_legacy_same_key_remains_unknown_after_exact_activation_recovery(
    serving_capture, monkeypatch
):
    root, now, command, commit, destination = serving_capture
    _lose_custody_at_publication(monkeypatch)
    with pytest.raises(OpError) as caught:
        writer_lease.invoke_command(command, root, idempotency_key="ack-recovery", **commit)
    assert caught.value.code == "MUTATION_COMMITTED_ACKNOWLEDGEMENT_UNCERTAIN"
    manager = writer_lease.get_manager()
    with manager.idempotency._connect() as connection:
        row = connection.execute("SELECT state, commit_token FROM mutations").fetchone()
    assert row[0] == "executing"
    assert graph_sync.read_graph_commit_receipt(root, row[1]) is None
    original_note = (root / destination).read_bytes()
    original_paths = {path.relative_to(root) for path in root.rglob("*.md")}
    custody = authorization_custody.load_authorization_custody(root, now=now)
    with store.open_authorization_session_connection(root) as connection:
        recovered = catalog_publication._recover_catalog_acknowledgement(
            root, connection, custody=custody, now=now
        )
        active = schema_v4.load_active_tuple_pointer(connection)
    assert recovered.control.activation_epoch == active.activation_epoch == 2
    assert recovered.control.activation_state_digest == active.activation_state_digest
    with readonly_control():
        with pytest.raises(OpError) as replay:
            writer_lease.invoke_command(command, root, idempotency_key="ack-recovery", **commit)
        assert replay.value.code == "MUTATION_OUTCOME_UNKNOWN"
    assert (root / destination).read_bytes() == original_note
    assert {path.relative_to(root) for path in root.rglob("*.md")} == original_paths


def test_readonly_custody_is_refused_before_new_canonical_bytes(serving_capture):
    root, now, command, commit, destination = serving_capture
    with readonly_control():
        with pytest.raises(ValueError, match="^GOVERNANCE_CATALOG_PUBLICATION_BLOCKED:"):
            writer_lease.invoke_command(command, root, idempotency_key="ack-preflight", **commit)
        assert not (root / destination).exists()
        assert _epochs(root, now) == (1, 1)


def test_refused_capture_retries_same_key_after_custody_becomes_writable(serving_capture):
    root, now, command, commit, destination = serving_capture
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*.md")}
    with readonly_control():
        with pytest.raises(ValueError, match="^GOVERNANCE_CATALOG_PUBLICATION_BLOCKED:"):
            writer_lease.invoke_command(
                command, root, idempotency_key="ack-capability-retry", **commit
            )
    assert {path.relative_to(root): path.read_bytes() for path in root.rglob("*.md")} == before
    result = writer_lease.invoke_command(
        command, root, idempotency_key="ack-capability-retry", **commit
    )
    assert result["state"] == "committed"
    assert (root / destination).is_file()
    assert _epochs(root, now) == (2, 2)


def test_readonly_filesystem_is_refused_even_when_directory_access_is_allowed(
    serving_capture, monkeypatch
):
    from types import SimpleNamespace

    root, now, command, commit, destination = serving_capture
    real_statvfs = os.statvfs
    custody_parent = Path(os.environ[authorization_custody.CONTROL_FILE_ENV]).parent

    def readonly_mount(path):
        if Path(path) == custody_parent:
            return SimpleNamespace(f_flag=os.ST_RDONLY)
        return real_statvfs(path)

    monkeypatch.setattr(os, "statvfs", readonly_mount)
    with pytest.raises(ValueError, match="^GOVERNANCE_CATALOG_PUBLICATION_BLOCKED:"):
        writer_lease.invoke_command(command, root, idempotency_key="ack-readonly-mount", **commit)
    assert not (root / destination).exists()
    assert _epochs(root, now) == (1, 1)
