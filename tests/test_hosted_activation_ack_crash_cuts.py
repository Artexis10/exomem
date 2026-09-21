"""Crash cuts proved by killing a real process, not by catching an exception.

A caught exception unwinds the stack and leaves the interpreter's state intact,
so it proves the error path and nothing about durability. The cuts this repair
depends on are the ones where the process is simply gone: the canonical bytes
are committed, the acknowledgement never happened, and whatever the next
process can work out has to come from what actually reached the disk.

Each test here builds a vault, hands it to a child interpreter that kills
itself at a named barrier inside the write, and then reopens the same vault in
this process to see what survived.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_governance_active_tuple import (
    _configure_custody,
    _documents,
    _migrate_with_projection_item,
    _write_workspace,
)

from exomem import vault as vault_module
from exomem import writer_lease
from exomem.governance import authorization_custody, store

CRASH_EXIT = 97
RELATIVE = "Knowledge Base/Notes/private.md"
BEFORE = "---\ntitle: Private\nstatus: draft\n---\n\nbefore\n"
AFTER = BEFORE.replace("before", "after")

_CHILD = """
import os
import sys

from exomem import semantic_writes, vault as vault_module, writer_lease
from exomem.governance import schema_v4

writer_lease.reset_managers_for_tests()
vault = {vault!r}
point = {point!r}


def crash(reached: str) -> None:
    if reached == point:
        # No unwinding, no atexit, no flush of anything the write did not
        # already make durable. This is the whole point of the cut.
        os._exit({code})


schema_v4._crash_point = crash

preflight = semantic_writes.preflight_existing(
    vault,
    path={relative!r},
    after_source={after!r},
    operation="edit",
    expected_before_hash=vault_module.content_hash({before!r}),
)
semantic_writes.commit_existing(vault, preflight=preflight)
print("child completed without reaching the cut", file=sys.stderr)
sys.exit(1)
"""


def _run_child(tmp_path: Path, vault: Path, point: str) -> subprocess.CompletedProcess[str]:
    driver = tmp_path / f"cut-{point}.py"
    driver.write_text(
        _CHILD.format(
            vault=str(vault),
            point=point,
            code=CRASH_EXIT,
            relative=RELATIVE,
            after=AFTER,
            before=BEFORE,
        ),
        encoding="utf-8",
    )
    return subprocess.run(
        [sys.executable, str(driver)],
        capture_output=True,
        text=True,
        timeout=180,
        env=dict(os.environ),
        check=False,
    )


def _prepare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, int]:
    now = int(time.time())
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state"))
    writer_lease.reset_managers_for_tests()
    vault = tmp_path / "vault"
    target = vault / RELATIVE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(BEFORE, encoding="utf-8")
    _write_workspace(vault, _documents(ceiling=2))
    migration = _migrate_with_projection_item(vault, path=RELATIVE, source=BEFORE, now=now)
    _configure_custody(
        monkeypatch,
        tmp_path / "custody",
        activation_epoch=1,
        activation_state_digest=migration.activation_state_digest,
        now=now,
    )
    return vault, now


def _epoch(vault: Path, *, now: int) -> int | None:
    return authorization_custody.load_authorization_custody(vault, now=now).control.activation_epoch


def _publications(vault: Path) -> int:
    """Count committed tuple publications.

    This is what separates the two cuts. Both leave the canonical bytes on
    disk, because the filesystem batch is durable before the catalog
    transaction opens; only a committed publication distinguishes them.
    """

    connection = store.open_connection(vault)
    try:
        return connection.execute("SELECT count(*) FROM governance_tuple_publications").fetchone()[
            0
        ]
    finally:
        connection.close()


def _store_activation_epoch(vault: Path) -> int:
    """The epoch the governance store itself reports, not the one custody holds."""

    connection = store.open_connection(vault)
    try:
        return int(
            connection.execute(
                "SELECT activation_epoch FROM governance_activation_store"
            ).fetchone()[0]
        )
    finally:
        connection.close()


def test_a_process_killed_before_the_catalog_commit_leaves_the_bytes_but_no_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The canonical filesystem batch is durable before the catalog opens.

    This is the ordering the repair depends on and it is easy to get backwards:
    a process killed here has already changed the page on disk, and the catalog
    transaction it was about to commit is gone. Recovery has to publish the
    outstanding transition against bytes that are already correct, not rerun
    the write.
    """

    vault, now = _prepare(tmp_path, monkeypatch)
    baseline = _publications(vault)

    result = _run_child(tmp_path, vault, "catalog-publication-before-commit")

    assert result.returncode == CRASH_EXIT, result.stderr
    assert (vault / RELATIVE).read_text(encoding="utf-8") == AFTER
    assert _publications(vault) == baseline
    assert _epoch(vault, now=now + 1) == 1


def test_a_process_killed_after_commit_before_acknowledgement_keeps_the_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect this repair exists for, observed from outside the process.

    The canonical effect is durable and the acknowledgement never happened. A
    fresh process has to find the committed state, not a rolled-back one, and
    must not be able to mistake the gap for an uncommitted attempt.
    """

    vault, now = _prepare(tmp_path, monkeypatch)
    baseline = _publications(vault)

    result = _run_child(tmp_path, vault, "catalog-publication-after-commit-before-registry")

    assert result.returncode == CRASH_EXIT, result.stderr
    assert (vault / RELATIVE).read_text(encoding="utf-8") == AFTER
    # One publication more than the cut before it, and custody still behind the
    # store: the acknowledgement is precisely what was lost.
    assert _publications(vault) == baseline + 1
    assert _epoch(vault, now=now + 1) == 1


def test_repeating_the_request_after_a_cut_refuses_instead_of_publishing_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retry is not a recovery, and the write path knows the difference.

    Both cuts leave the page already changed on disk, so the caller's original
    expected-before hash no longer describes it. Repeating the request is
    refused rather than treated as a fresh edit -- which is what stops a
    crashed attempt from being published a second time by a client that simply
    tries again.
    """

    from exomem import semantic_writes

    for point in (
        "catalog-publication-before-commit",
        "catalog-publication-after-commit-before-registry",
    ):
        vault, now = _prepare(tmp_path / point, monkeypatch)
        assert _run_child(tmp_path, vault, point).returncode == CRASH_EXIT
        publications = _publications(vault)

        with pytest.raises(semantic_writes.SemanticWriteError) as refusal:
            semantic_writes.preflight_existing(
                vault,
                path=RELATIVE,
                after_source=AFTER,
                operation="edit",
                expected_before_hash=vault_module.content_hash(BEFORE),
            )

        assert refusal.value.code == "STALE_SEMANTIC_WRITE"
        # The refusal changed nothing: no second publication, no advance, and
        # the committed bytes are still the ones the crashed attempt wrote.
        assert _publications(vault) == publications
        assert _epoch(vault, now=now + 1) == 1
        assert (vault / RELATIVE).read_text(encoding="utf-8") == AFTER


def test_a_filesystem_cut_blocks_the_next_ordinary_write_until_it_is_recovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This is the stranding shape, and it is why recovery is not optional.

    A process killed between the filesystem batch and the catalog commit
    leaves the page changed on disk and the catalog still describing the old
    content. The next ordinary write -- a different edit by a different
    process, not a retry -- is refused, because the reviewed predecessor the
    catalog holds is no longer what is on disk. Until the outstanding
    publication is recovered, the page cannot be written at all.

    The refusal is the catalog's, not the writer lease's: the dead process's
    lease is reclaimed, and the write gets far enough to be judged on content.
    """

    from exomem import semantic_writes

    vault, now = _prepare(tmp_path, monkeypatch)
    assert _run_child(tmp_path, vault, "catalog-publication-before-commit").returncode == CRASH_EXIT
    baseline = _publications(vault)

    later = AFTER.replace("after", "later")
    preflight = semantic_writes.preflight_existing(
        vault,
        path=RELATIVE,
        after_source=later,
        operation="edit",
        expected_before_hash=vault_module.content_hash(AFTER),
    )

    with pytest.raises(semantic_writes.SemanticWriteError) as refusal:
        semantic_writes.commit_existing(vault, preflight=preflight)

    assert refusal.value.code == "GOVERNANCE_CATALOG_PUBLICATION_BLOCKED"
    assert _publications(vault) == baseline
    assert _epoch(vault, now=now + 1) == 1


def test_the_child_really_dies_at_the_barrier_and_not_somewhere_convenient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard the instrument: a driver that never reaches its point would make
    every test above pass by doing nothing."""

    vault, _ = _prepare(tmp_path, monkeypatch)

    result = _run_child(tmp_path, vault, "a-barrier-that-does-not-exist")

    assert result.returncode != CRASH_EXIT
    assert "child completed without reaching the cut" in result.stderr


def test_a_committed_unacknowledged_write_does_not_close_the_attestation_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fuse Decision 7 was written to defuse does not exist.

    That decision reasoned: a cell whose store leads custody cannot sign its
    readiness attestation, the provisioner's hourly renewal needs that
    signature, so the window lapses within the hour and the cell is lost. Task
    3.4 proposed widening the readiness proof and its provisioner validator --
    an authority boundary -- to rescue it.

    Two things are true instead. The renewal never reaches `_ready_custody`:
    `schema_v4.load_active_state` is the only comparison of the store's
    activation tuple against `control.json`, and none of its callers sit on the
    minting path. And the one input to that path which *is* recomputed on a pod
    restart -- `_mutation_authority_ready`, from
    `probe_hosted_mutation_authority` -- still admits on a diverged cell, which
    is what this test pins.

    The divergence here is the real one, not a hand-written control record: a
    child process commits the governed write and dies before acknowledging it,
    so the store genuinely leads custody by one epoch. Synthesising it by
    setting `activation_epoch=0` instead proves nothing -- the schema's
    `CHECK(activation_epoch>0)` rejects that as malformed rather than behind,
    and it reads like a refusal.

    What a pending acknowledgement does block is `_ready_custody`, and so
    `/ready`, session issuance and content serving. That is the defect this
    change repairs, and it is an outage rather than an unrecoverable loss.
    """

    from exomem.server_runtime import probe_hosted_mutation_authority

    vault, now = _prepare(tmp_path, monkeypatch)
    assert probe_hosted_mutation_authority(vault) == (True, "HOSTED_READY")
    baseline = _publications(vault)

    result = _run_child(tmp_path, vault, "catalog-publication-after-commit-before-registry")
    assert result.returncode == CRASH_EXIT, result.stderr

    # The store advanced and custody did not: this is the stranding shape.
    assert _publications(vault) == baseline + 1
    assert _epoch(vault, now=now + 1) == 1
    assert _store_activation_epoch(vault) == 2

    # And the startup probe -- the thing a restarting pod re-runs -- still admits.
    writer_lease.reset_managers_for_tests()
    assert probe_hosted_mutation_authority(vault) == (True, "HOSTED_READY")
