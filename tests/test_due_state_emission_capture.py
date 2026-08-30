"""The emission ledger, and batch-once emission.

The due-state emission governor is per-process memory, so no projector can read
it and the bench declares the counters surface unavailable. This module pins the
persisted ledger — how many governed writes the projection absorbed, how many
blocks were actually emitted — and the batch scope that makes "one command, at
most one block" a mechanism rather than an accident of where the terminal sits.

The first test is the GAP PROOF: the projection file records neither count, and
there is no batch scope to wrap a bulk command in.

Two tests here were built as tripwires on the missing bulk carrier and have
been INVERTED by `extend-due-state-to-bulk-carriers`, which made the operation
leaves carriers: `test_a_multi_write_command_carries_one_block` and
`test_the_batch_scope_on_this_leaf_suppresses_nothing_today`. Their old
expectation — the measured zero — is what their red run proved had changed.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
from pathlib import Path

import pytest
from _nag_governance_helpers import overdue_prediction, scratch_page, write

from exomem import commands, writer_lease
from exomem import due_state as due_state_module

SCRATCH = "Knowledge Base/Notes/Research/Infrastructure/nag-scratch.md"


@pytest.fixture(autouse=True)
def _clean_emission_state():
    due_state_module.reset_emission_state()
    yield
    due_state_module.reset_emission_state()


def _projection(vault: Path) -> dict:
    return json.loads(
        due_state_module.state_path(vault).read_text(encoding="utf-8")
    )


def _command(name: str):
    return next(c for c in commands.PRODUCT_COMMANDS if c.name == name)


def _observe(vault: Path, content: str) -> dict:
    return writer_lease.invoke_command(
        _command("observe_memory"),
        vault,
        path=SCRATCH,
        operation="add",
        category="finding",
        content=content,
        tags=["infrastructure"],
    )


def _adopt_twelve(vault: Path, *, directory: str = "legacy") -> dict:
    """Twelve legacy files absorbed by ONE `adopt_vault` through the real terminal."""
    source = vault / directory
    source.mkdir(exist_ok=True)
    for index in range(12):
        (source / f"note-{index}.md").write_text(
            f"---\ntitle: legacy {index}\n---\n\n"
            "## Prediction\n\n"
            f"- id: q{index}\n"
            f"- check_by: {(dt.date.today() - dt.timedelta(days=index + 1)).isoformat()}\n\n"
            "A legacy claim.\n",
            encoding="utf-8",
        )
    response = writer_lease.invoke_command(
        _command("adopt_vault"),
        vault,
        mode="copy-as-sources",
        selected_paths=[f"{directory}/note-{index}.md" for index in range(12)],
    )
    assert isinstance(response, dict), response
    return response


def _bulk_pages(vault: Path, count: int) -> list[str]:
    """`count` pages that each add one overdue prediction, so each write moves the counts."""
    paths = []
    for index in range(count):
        paths.append(
            write(
                vault,
                f"Knowledge Base/Notes/Insights/nag-bulk-{index}.md",
                "---\n"
                f"title: nag-bulk-{index}\n"
                "type: insight\nstatus: active\n"
                "created: 2026-01-01\nupdated: 2026-01-01\n---\n\n"
                "## Prediction\n\n"
                f"- id: p{index}\n"
                f"- check_by: {(dt.date.today() - dt.timedelta(days=index + 1)).isoformat()}\n\n"
                f"Claim number {index}.\n",
            )
        )
    return paths


# ==========================================================================
# THE GAP PROOF
# ==========================================================================


def test_the_projection_records_no_emission_counts_and_has_no_batch_scope(
    vault: Path,
) -> None:
    """RED-FIRST. Nothing a projector could read, and nothing to wrap a batch in."""
    overdue_prediction(vault)
    scratch_page(vault)
    commands.op_bootstrap(vault)
    due_state_module.reset_emission_state()
    _observe(vault, "Reader saturation reproduces on the replica too.")

    silent: list[str] = []
    payload = _projection(vault)
    if "emission" not in payload:
        silent.append("the projection file carries no `emission` section")
    if not hasattr(due_state_module, "batch_scope"):
        silent.append("there is no batch scope to wrap a multi-write command in")

    assert silent == [], "; ".join(silent)


# ==========================================================================
# the ledger
# ==========================================================================


def test_a_governed_write_increments_the_write_count(vault: Path) -> None:
    overdue_prediction(vault)
    scratch_page(vault)
    commands.op_bootstrap(vault)
    before = _projection(vault)["emission"]["writes"]

    _observe(vault, "Reader saturation reproduces on the replica too.")

    assert _projection(vault)["emission"]["writes"] == before + 1


def test_an_emitted_block_increments_the_emission_count(vault: Path) -> None:
    overdue_prediction(vault)
    scratch_page(vault)
    commands.op_bootstrap(vault)
    due_state_module.reset_emission_state()
    before = _projection(vault)["emission"]["emissions"]

    result = _observe(vault, "Reader saturation reproduces on the replica too.")

    assert "due_state" in result
    ledger = _projection(vault)["emission"]
    assert ledger["emissions"] == before + 1
    assert ledger["last_digest"]


def test_a_block_that_is_not_delivered_does_not_count(vault: Path) -> None:
    """Production is not delivery — the ledger records what the caller received."""
    overdue_prediction(vault)
    scratch_page(vault)
    commands.op_bootstrap(vault)
    due_state_module.reset_emission_state()
    _observe(vault, "First observation.")
    after_first = _projection(vault)["emission"]["emissions"]

    # Identical totals: the governor goes quiet, so nothing is delivered.
    _observe(vault, "Second observation with the same totals.")

    assert _projection(vault)["emission"]["emissions"] == after_first


# ==========================================================================
# batch scope
# ==========================================================================


def test_a_twelve_write_batch_emits_at_most_once(vault: Path) -> None:
    """Twelve carrier runs in one scope, then one delivery at the terminal.

    The whole shape matters, not just the suppressed half. Inside the scope the
    governor declines every block; the delivery happens once after it exits,
    which is what D9's response terminal does for a real command. Stopping at
    scope exit would assert "zero emissions" and call that governance — but a
    batch that ends up telling the caller NOTHING is a different (and worse)
    outcome than one that tells them once.
    """
    overdue_prediction(vault)
    scratch_page(vault)
    commands.op_bootstrap(vault)
    due_state_module.reset_emission_state()
    before = _projection(vault)["emission"]

    pages = _bulk_pages(vault, 12)
    emitted = 0
    with due_state_module.batch_scope(vault):
        for page in pages:
            block = due_state_module.block_for_write(vault, page)
            if due_state_module.should_emit(block, vault_root=vault):
                emitted += 1
    assert emitted == 0, "a block was delivered from inside the batch"

    # The terminal, once, after the scope.
    assert due_state_module.should_emit(due_state_module.served(vault), vault_root=vault)

    ledger = _projection(vault)["emission"]
    assert ledger["writes"] == before["writes"] + 12
    assert ledger["emissions"] == before["emissions"] + 1
    assert ledger["due_total"] >= 12, ledger


def test_removing_the_batch_scope_emits_once_per_write(vault: Path) -> None:
    """The mechanism-removal pair: without the scope, twelve writes, twelve blocks."""
    overdue_prediction(vault)
    scratch_page(vault)
    commands.op_bootstrap(vault)
    due_state_module.reset_emission_state()

    pages = _bulk_pages(vault, 12)
    emitted = 0
    for page in pages:
        block = due_state_module.block_for_write(vault, page)
        if due_state_module.should_emit(block, vault_root=vault):
            emitted += 1

    assert emitted == 12
    assert _projection(vault)["emission"]["emissions"] >= 12


def test_separate_calls_stay_separate_batches(vault: Path) -> None:
    overdue_prediction(vault)
    scratch_page(vault)
    commands.op_bootstrap(vault)
    due_state_module.reset_emission_state()

    pages = _bulk_pages(vault, 2)
    emitted = 0
    for page in pages:
        with due_state_module.batch_scope(vault):
            due_state_module.block_for_write(vault, page)
        block = due_state_module.served(vault)
        if due_state_module.should_emit(block, vault_root=vault):
            emitted += 1

    assert emitted == 2


def test_a_batch_scope_is_per_vault(vault: Path, tmp_path: Path) -> None:
    """Silencing vault A must not silence vault B."""
    other = tmp_path / "other-vault"
    (other / "Knowledge Base").mkdir(parents=True)
    with due_state_module.batch_scope(vault):
        assert due_state_module.batch_active(vault)
        assert not due_state_module.batch_active(other)


def test_a_multi_write_command_carries_one_block(vault: Path) -> None:
    """The product-level statement, read off the RESPONSE rather than the ledger.

    The scenario is about what the caller receives, so this inspects the
    command's own response for `due_state` blocks instead of inferring delivery
    from a counter. Driven through `writer_lease.invoke_command` — the normal
    command path — so the mutation terminal that admits the block actually runs;
    calling the leaf directly skips the only code that can put one there.

    INVERTED on purpose, and this was the tripwire that said so. Until the
    operation leaves became carriers, this asserted the measured zero — the
    response carried no block at all — and its docstring named the day it would
    have to change. That day is `extend-due-state-to-bulk-carriers`: twelve
    governed writes in one invocation, one block at its terminal, one emission
    in the ledger.

    Still not the weaker `<= 1`. A dict cannot hold a key twice, so counting
    `due_state` keys in a response could only ever return 0 or 1 and `<= 1` was
    a tautology dressed as a governance check. What bounds this leaf to one is
    the response terminal, which runs once per invocation — and that is still
    NOT the batch scope: see
    `test_the_batch_scope_on_this_leaf_suppresses_nothing_today` below.
    """
    overdue_prediction(vault)
    scratch_page(vault)
    commands.op_bootstrap(vault)
    due_state_module.reset_emission_state()
    before = _projection(vault)["emission"]

    response = _adopt_twelve(vault)

    assert "due_state" in response, response
    ledger = _projection(vault)["emission"]
    assert ledger["writes"] - before["writes"] == 12
    assert ledger["emissions"] - before["emissions"] == 1


def test_the_batch_scope_on_this_leaf_suppresses_nothing_today(vault: Path) -> None:
    """Compare independent sessions, because the leaf's scope suppresses nothing.

    `op_adopt_vault` still reaches the PER-WRITE carrier ZERO times, and that
    half of the measurement is unchanged by
    `extend-due-state-to-bulk-carriers`: `op_adopt` copies files through the
    vault writer and `_apply_batch_deltas` applies the projection deltas with
    `apply_write_delta`, which produces no block. The block the leaf now carries
    is a BATCH block — `due_state.block_for_batch`, served once at the end of the
    invocation — so the scope on this leaf still suppresses nothing, and removing
    it still changes nothing. What bounds the caller to one block is the response
    terminal (D9), which runs once per invocation whatever the scope did.

    Both independent-session runs now carry: reset the process-local governor
    between them so change-only quieting cannot mask whether removing the scope
    altered the terminal delivery.

    This is pinned rather than left implicit because the scope IS load-bearing
    at the per-write carrier
    (`test_removing_the_batch_scope_emits_once_per_write`), and the difference
    between "defends nothing yet" and "defends nothing ever" matters: the day
    this leaf commits through `semantic_writes`, the carrier count below stops
    being zero and this test says so.

    The unscoped leg is a real removal — `due_state.batch_scope` monkeypatched
    to a no-op, so the leaf genuinely runs without it. An earlier version wrapped
    the second call in `contextlib.nullcontext()`, which changed nothing and made
    the A and the B the same run twice.
    """
    overdue_prediction(vault)
    scratch_page(vault)
    commands.op_bootstrap(vault)
    due_state_module.reset_emission_state()

    carried: list[str] = []
    real_block = due_state_module.block_for_write
    real_scope = due_state_module.batch_scope

    def counting(vault_root, rel_path, **kwargs):
        carried.append(rel_path)
        return real_block(vault_root, rel_path, **kwargs)

    due_state_module.block_for_write = counting
    try:
        scoped = _adopt_twelve(vault)
        scoped_calls = list(carried)

        carried.clear()
        due_state_module.reset_emission_state()
        due_state_module.batch_scope = lambda *a, **k: contextlib.nullcontext()
        try:
            unscoped = _adopt_twelve(vault, directory="legacy2")
        finally:
            due_state_module.batch_scope = real_scope
        unscoped_calls = list(carried)
    finally:
        due_state_module.block_for_write = real_block
        due_state_module.batch_scope = real_scope

    assert scoped_calls == [], "the scoped leaf reached the write carrier"
    assert unscoped_calls == [], "the leaf reached the write carrier without the scope"
    assert "due_state" in scoped, scoped
    assert "due_state" in unscoped, unscoped


def test_the_ledger_records_the_size_of_the_block_it_delivered(vault: Path) -> None:
    """The denominator, with ONE definition: what the caller was handed.

    `writes=12, emissions=0` is equally the shape of governance working and the
    shape of a batch that delivered nothing. `due_total` separates them only if
    it means one thing, so it is written at delivery and nowhere else. An
    earlier version also wrote reconcile's own unfiltered count under the same
    name, which let the field report a pre-dismissal number as the denominator
    for a later batch that owed nothing at all.
    """
    overdue_prediction(vault)
    overdue_prediction(vault, "nag-second")
    scratch_page(vault)
    due_state_module.reconcile(vault)

    block = due_state_module.served(vault)
    assert block is not None and block["total"] == 2
    due_state_module.reset_emission_state()
    assert due_state_module.should_emit(block, vault_root=vault)

    ledger = _projection(vault)["emission"]
    assert ledger["due_total"] == 2
    assert ledger["emissions"] == 1


def test_a_heal_never_writes_the_denominator(vault: Path) -> None:
    """Reconcile hands nobody anything, so it records no denominator.

    This is the two-definitions bug pinned as a test. The vault genuinely owes
    two items and a full recompute knows it — but a heal is not a delivery, and
    a number that means "what a caller received" must not be written by a code
    path where nobody received anything.
    """
    overdue_prediction(vault)
    overdue_prediction(vault, "nag-second")
    scratch_page(vault)
    due_state_module.reconcile(vault)

    assert _projection(vault)["emission"]["due_total"] == 0
    # ...and the recompute really did find them, so the zero is about the
    # writer and not about an empty vault.
    assert due_state_module.served(vault)["total"] == 2


def test_a_production_that_is_never_delivered_records_no_denominator(
    vault: Path,
) -> None:
    """Producing a block inside a batch tells the ledger nothing about delivery."""
    overdue_prediction(vault)
    scratch_page(vault)
    due_state_module.reconcile(vault)
    due_state_module.reset_emission_state()

    with due_state_module.batch_scope(vault):
        produced = due_state_module.block_for_write(vault, _bulk_pages(vault, 1)[0])
        assert produced is not None and produced["total"] >= 1
        due_state_module.should_emit(produced, vault_root=vault)

    ledger = _projection(vault)["emission"]
    assert ledger["emissions"] == 0
    assert ledger["due_total"] == 0


def test_a_vault_that_owes_nothing_records_a_zero_denominator(vault: Path) -> None:
    scratch_page(vault)
    due_state_module.reconcile(vault)
    ledger = _projection(vault)["emission"]
    assert ledger["due_total"] == 0
    assert ledger["emissions"] == 0


# ==========================================================================
# the projector reads it
# ==========================================================================


def test_the_projector_declares_the_counters_available_and_reads_them(
    vault: Path,
) -> None:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))
    from epistemic.projectors.exomem_vault import VaultProjector

    overdue_prediction(vault)
    scratch_page(vault)
    commands.op_bootstrap(vault)
    due_state_module.reset_emission_state()
    _observe(vault, "Reader saturation reproduces on the replica too.")

    projector = VaultProjector(vault)
    declarations = {d.field: d for d in projector.declarations()}
    assert declarations["due_state_counters"].status == "available_via:due_state_file"

    snapshot = projector.project(phase="s6", taken_at="2026-08-20T00:00:00Z")
    block = next(
        item for item in snapshot.items if item.id == "surface-due_state_counters"
    )
    assert block.raw["projection"] == "complete"
    assert int(block.raw["writes"]) >= 1
    assert int(block.raw["emissions"]) >= 1
