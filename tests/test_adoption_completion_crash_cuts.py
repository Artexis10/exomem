"""Adoption Studio completion cuts, proved by killing a real process.

One completion is three durable effects in three different places: the catalog
transaction, the proposal file under the run store, and the component row that
binds the two to this attempt. Nothing orders them into one commit, so a caller
can lose the process between any pair, and what the next process finds is the
whole question.

Each test here builds the state, hands it to a child interpreter that kills
itself at a named barrier inside `recover_hosted_completion_receipt`, then runs
the real recovery in this process and asserts it finishes the completion
without repeating any part of it.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from test_hosted_mutation_journal import _connection, _descriptor, _publication

from exomem import adoption_proposals, adoption_run
from exomem.governance import hosted_mutation_journal

CRASH_EXIT = 97
SECRET = b"s" * 32
RUN_ID = "run-1"
PROPOSAL_ID = "proposal-1"
SIDECAR = "adoption-completion-0"
PRIOR = {"proposal_id": PROPOSAL_ID, "status": "proposed", "applied": None}
TRANSITION = {
    "proposal_id": PROPOSAL_ID,
    "status": "applied",
    "applied": {"result_path": "Notes/applied.md"},
}

_CHILD = """
import os, sqlite3, sys

from exomem import adoption_proposals
from exomem.governance import hosted_mutation_journal, store as governance_store

point = {point!r}
governance_path = {governance!r}


def crash(reached):
    if reached == point:
        # No unwinding and no flush of anything not already durable.
        os._exit({code})


adoption_proposals._adoption_crash_point = crash
governance_store.open_authorization_session_connection = (
    lambda _root: sqlite3.connect(governance_path)
)

recovery = hosted_mutation_journal.prepare_canonical_mutation_recovery(
    descriptor={descriptor},
    prepared_results={results},
    attempt_secret={secret!r},
)
adoption_proposals.recover_hosted_completion_receipt(
    {vault!r}, recovery, attempt_secret={secret!r}, now=12.0
)
print("child completed without reaching the cut", file=sys.stderr)
sys.exit(1)
"""


def _payloads() -> dict[str, object]:
    return {
        "child-0": {"path": "Notes/0.md"},
        SIDECAR: {
            "path": "Notes/applied.md",
            "run_id": RUN_ID,
            "proposal_id": PROPOSAL_ID,
            "prior": PRIOR,
            "transition": TRANSITION,
        },
    }


def _recovery():
    return hosted_mutation_journal.prepare_canonical_mutation_recovery(
        descriptor=_descriptor(final_sidecar=True, sidecar_id=SIDECAR),
        prepared_results=_payloads(),
        attempt_secret=SECRET,
    )


def _prepare(tmp_path: Path) -> tuple[Path, Path]:
    """A committed catalog child and a proposal still marked `applying`."""

    vault = tmp_path / "vault"
    (vault / "Knowledge Base" / "_Adoption" / "runs" / RUN_ID).mkdir(parents=True)
    governance = tmp_path / "governance.sqlite"
    recovery = _recovery()

    connection = _connection(governance)
    hosted_mutation_journal.create_allocating_journal(
        connection,
        recovery=recovery,
        attempt_secret=SECRET,
        dependency_manifest={},
        now=10.0,
    )
    connection.execute("BEGIN IMMEDIATE")
    _publication(connection, event_id="publication-1")
    hosted_mutation_journal.record_child_in_transaction(
        connection,
        recovery=recovery,
        child_id="child-0",
        publication_event_id="publication-1",
        attempt_secret=SECRET,
        now=11.0,
    )
    connection.commit()
    connection.close()

    adoption_run.AdoptionRunStore(vault).save_proposals(
        RUN_ID,
        {
            "schema_version": 1,
            "run_id": RUN_ID,
            "proposals": [{**PRIOR, "status": "applying"}],
        },
    )
    return vault, governance


def _run_child(tmp_path: Path, vault: Path, governance: Path, point: str):
    driver = tmp_path / f"adoption-cut-{point}.py"
    driver.write_text(
        _CHILD.format(
            point=point,
            code=CRASH_EXIT,
            governance=str(governance),
            vault=str(vault),
            secret=SECRET,
            descriptor=repr(_descriptor(final_sidecar=True, sidecar_id=SIDECAR)),
            results=repr(_payloads()),
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


def _components(governance: Path) -> list[str]:
    connection = sqlite3.connect(governance)
    try:
        return [
            str(row[0])
            for row in connection.execute(
                "SELECT component_kind FROM governance_operation_components ORDER BY ordinal"
            )
        ]
    finally:
        connection.close()


def _saved(vault: Path) -> dict:
    return adoption_run.AdoptionRunStore(vault).load_proposals(RUN_ID)


def _recover_here(vault: Path, governance: Path, monkeypatch: pytest.MonkeyPatch) -> bool:
    from exomem.governance import store as governance_store

    monkeypatch.setattr(
        governance_store,
        "open_authorization_session_connection",
        lambda _root: sqlite3.connect(governance),
    )
    return adoption_proposals.recover_hosted_completion_receipt(
        vault, _recovery(), attempt_secret=SECRET, now=13.0
    )


@pytest.mark.parametrize(
    ("point", "proposal_completed", "evidence_written"),
    [
        ("after-catalog-before-proposal-completion", False, False),
        ("after-proposal-before-evidence", True, False),
        ("after-aggregate-before-terminal", True, True),
    ],
)
def test_each_completion_cut_is_finished_by_recovery_without_repeating_itself(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    point: str,
    proposal_completed: bool,
    evidence_written: bool,
) -> None:
    """Whatever the cut took, one recovery pass lands the same single outcome.

    The three barriers walk the completion forward one durable effect at a
    time, and the parameters record what each one leaves behind -- so a change
    that reorders these effects fails here rather than in a rehearsal.
    """

    vault, governance = _prepare(tmp_path)

    result = _run_child(tmp_path, vault, governance, point)
    assert result.returncode == CRASH_EXIT, result.stderr

    saved = _saved(vault)
    assert (saved["proposals"] == [TRANSITION]) is proposal_completed
    assert (_components(governance).count("hosted-mutation-child/v1") == 2) is evidence_written

    assert _recover_here(vault, governance, monkeypatch) is True

    # One completion, whichever cut preceded it: the proposal is applied once,
    # it carries exactly one receipt, and the command minted one commit over
    # exactly its two children.
    final = _saved(vault)
    assert final["proposals"] == [TRANSITION]
    assert set(final["_hosted_completion_receipts"]) == {PROPOSAL_ID}
    assert _components(governance) == [
        "hosted-mutation-plan/v1",
        "hosted-mutation-child/v1",
        "hosted-mutation-child/v1",
        "hosted-mutation-commit/v1",
    ]


def test_recovery_after_a_cut_is_idempotent_across_repeated_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A supervisor that retries recovery must not mint a second completion."""

    vault, governance = _prepare(tmp_path)
    assert (
        _run_child(tmp_path, vault, governance, "after-proposal-before-evidence").returncode
        == CRASH_EXIT
    )

    assert _recover_here(vault, governance, monkeypatch) is True
    after_first = (_saved(vault), _components(governance))

    assert _recover_here(vault, governance, monkeypatch) is True
    assert (_saved(vault), _components(governance)) == after_first


def test_the_child_really_dies_at_the_barrier_and_not_somewhere_convenient(
    tmp_path: Path,
) -> None:
    """Guard the instrument: a driver that never reaches its point would make
    every case above pass by doing nothing."""

    vault, governance = _prepare(tmp_path)

    result = _run_child(tmp_path, vault, governance, "a-barrier-that-does-not-exist")

    assert result.returncode != CRASH_EXIT
    assert "child completed without reaching the cut" in result.stderr
