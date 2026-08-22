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
    """The product-level statement of the same property."""
    overdue_prediction(vault)
    scratch_page(vault)
    commands.op_bootstrap(vault)
    due_state_module.reset_emission_state()
    before = _projection(vault)["emission"]

    legacy = vault / "legacy"
    legacy.mkdir(exist_ok=True)
    for index in range(12):
        (legacy / f"note-{index}.md").write_text(
            f"---\ntitle: legacy {index}\n---\n\n"
            "## Prediction\n\n"
            f"- id: q{index}\n- check_by: 2020-01-01\n\nA legacy claim.\n",
            encoding="utf-8",
        )

    result = commands.op_adopt_vault(
        vault,
        mode="copy-as-sources",
        selected_paths=[f"legacy/note-{index}.md" for index in range(12)],
    )
    assert len(result["copy"]["copied_sources"]) == 12

    ledger = _projection(vault)["emission"]
    assert ledger["writes"] - before["writes"] == 12
    assert ledger["emissions"] - before["emissions"] <= 1


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
