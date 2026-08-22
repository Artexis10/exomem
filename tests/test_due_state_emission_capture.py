"""The emission ledger, and batch-once emission.

The due-state emission governor is per-process memory, so no projector can read
it and the bench declares the counters surface unavailable. This module pins the
persisted ledger — how many governed writes the projection absorbed, how many
blocks were actually emitted — and the batch scope that makes "one command, at
most one block" a mechanism rather than an accident of where the terminal sits.

The first test is the GAP PROOF: the projection file records neither count, and
there is no batch scope to wrap a bulk command in.
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

    ledger = _projection(vault)["emission"]
    assert ledger["writes"] == before["writes"] + 12
    assert emitted == 0
    assert ledger["emissions"] - before["emissions"] <= 1


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

    What bounds this to one block is measured, not assumed, and it is NOT the
    batch scope: see `test_the_batch_scope_on_this_leaf_suppresses_nothing_today`
    immediately below.
    """
    overdue_prediction(vault)
    scratch_page(vault)
    commands.op_bootstrap(vault)
    due_state_module.reset_emission_state()
    before = _projection(vault)["emission"]

    response = _adopt_twelve(vault)

    blocks = [key for key in response if key == "due_state"]
    assert len(blocks) <= 1, response
    ledger = _projection(vault)["emission"]
    assert ledger["writes"] - before["writes"] == 12
    assert ledger["emissions"] - before["emissions"] <= 1


def test_the_batch_scope_on_this_leaf_suppresses_nothing_today(vault: Path) -> None:
    """Measured, because "the scope keeps it to one block" was never verified.

    `op_adopt_vault` reaches the write carrier ZERO times: `op_adopt` copies
    files through the vault writer and `_apply_batch_deltas` then applies the
    projection deltas with `apply_write_delta`, which produces no block. So the
    scope on this leaf suppresses nothing, and removing it changes nothing — the
    one block the caller can receive is bounded by the response terminal (D9),
    which runs once per invocation whatever the scope did.

    This is pinned rather than left implicit because the scope IS load-bearing
    at the carrier (`test_removing_the_batch_scope_emits_once_per_write`), and
    the difference between "defends nothing yet" and "defends nothing ever"
    matters: the day this leaf commits through `semantic_writes`, the carrier
    count below stops being zero and this test says so.
    """
    overdue_prediction(vault)
    scratch_page(vault)
    commands.op_bootstrap(vault)
    due_state_module.reset_emission_state()

    carried: list[str] = []
    real = due_state_module.block_for_write

    def counting(vault_root, rel_path, **kwargs):
        carried.append(rel_path)
        return real(vault_root, rel_path, **kwargs)

    original = due_state_module.block_for_write
    due_state_module.block_for_write = counting
    try:
        scoped = _adopt_twelve(vault)
        scoped_calls = list(carried)

        carried.clear()
        with contextlib.nullcontext():
            unscoped = _adopt_twelve(vault, directory="legacy2")
    finally:
        due_state_module.block_for_write = original

    assert scoped_calls == [], "the scoped leaf reached the write carrier"
    assert carried == [], "the leaf reached the write carrier after all"
    assert [key for key in scoped if key == "due_state"] == []
    assert [key for key in unscoped if key == "due_state"] == []


def test_the_ledger_records_how_much_was_due(vault: Path) -> None:
    """The denominator, without which the other two counters are unreadable.

    `writes=12, emissions=0` is equally the shape of governance working and the
    shape of a vault that owed nothing. `due_total` is what separates them, so
    it is recorded on production — whether or not anything was emitted — rather
    than on delivery.
    """
    overdue_prediction(vault)
    scratch_page(vault)
    due_state_module.reconcile(vault)
    assert _projection(vault)["emission"]["due_total"] == 1

    overdue_prediction(vault, "nag-second")
    due_state_module.reconcile(vault)
    assert _projection(vault)["emission"]["due_total"] == 2

    # And the write carrier keeps it current without a full recompute.
    due_state_module.block_for_write(
        vault, _bulk_pages(vault, 1)[0]
    )
    assert _projection(vault)["emission"]["due_total"] == 3


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
