"""Due-state carriage on the five operation leaves.

The page-write and structured-collection carriers already exist. These pin the
OPERATION leaves — vault adoption, Adoption Studio applies, maintenance repair,
artifact preservation, media processing — as carriers of the same bounded
advisory block, under the same contract: served from the committed terminal
projection, one block per invocation, change-only, and silent when the
invocation committed nothing.

The whole file is written against the response the CALLER receives, driven
through `writer_lease.invoke_command` (the one dispatcher MCP, REST, hosted and
CLI share), because the mutation terminal that admits the block only runs there
— calling a leaf directly skips the only code that can put one in.
"""

from __future__ import annotations

import datetime as dt
import json
from contextlib import nullcontext
from pathlib import Path

import pytest
from _nag_governance_helpers import overdue_prediction, scratch_page, write

from exomem import client_artifacts, commands, writer_lease
from exomem import due_state as due_state_module

INSIGHTS = "Knowledge Base/Notes/Insights"


@pytest.fixture(autouse=True)
def _clean_emission_state():
    due_state_module.reset_emission_state()
    yield
    due_state_module.reset_emission_state()


def _command(name: str):
    return next(c for c in commands.PRODUCT_COMMANDS if c.name == name)


def _projection(vault: Path) -> dict:
    return json.loads(due_state_module.state_path(vault).read_text(encoding="utf-8"))


def _ledger(vault: Path) -> dict:
    return _projection(vault)["emission"]


def _seed(vault: Path) -> None:
    """One overdue prediction the projection owes, and a warm projection file."""
    overdue_prediction(vault)
    scratch_page(vault)
    commands.op_bootstrap(vault)
    due_state_module.reset_emission_state()


def _overdue_legacy(vault: Path, directory: str, count: int) -> list[str]:
    """`count` un-governed legacy files that each add one overdue prediction."""
    source = vault / directory
    source.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (source / f"note-{index}.md").write_text(
            f"---\ntitle: legacy {index}\n---\n\n"
            "## Prediction\n\n"
            f"- id: q{index}\n"
            f"- check_by: {(dt.date.today() - dt.timedelta(days=index + 1)).isoformat()}\n\n"
            "A legacy claim.\n",
            encoding="utf-8",
        )
    return [f"{directory}/note-{index}.md" for index in range(count)]


def _adopt(vault: Path, *, directory: str = "legacy", count: int = 12, **kwargs):
    selected = _overdue_legacy(vault, directory, count)
    return writer_lease.invoke_command(
        _command("adopt_vault"),
        vault,
        mode="copy-as-sources",
        selected_paths=selected,
        **kwargs,
    )


def _studio_apply(vault: Path, *, directory: str = "Old Notes", **kwargs):
    from exomem import find

    old = vault / directory
    old.mkdir(parents=True, exist_ok=True)
    for index in range(3):
        (old / f"studio-{index}.md").write_text(
            f"---\ntitle: studio {index}\n---\n\n"
            "## Prediction\n\n"
            f"- id: s{index}\n"
            f"- check_by: {(dt.date.today() - dt.timedelta(days=index + 1)).isoformat()}\n\n"
            "A studio claim.\n",
            encoding="utf-8",
        )
    find.clear_cache()
    started = commands.op_adoption_studio(vault, action="start", path=directory)
    run_id = started["run_id"]
    commands.op_adoption_studio(vault, action="select", run_id=run_id, include=[directory])
    planned = commands.op_adoption_studio(vault, action="plan", run_id=run_id)
    return writer_lease.invoke_command(
        _command("adoption_studio"),
        vault,
        action="apply",
        run_id=run_id,
        plan_id=planned["plan"]["plan_id"],
        **kwargs,
    )


def _maintain(vault: Path, **kwargs):
    return writer_lease.invoke_command(_command("maintain_memory"), vault, **kwargs)


def _repairable_overdue(vault: Path, slug: str) -> str:
    """A page `fix` rewrites (no `status`) that also owes an overdue prediction."""
    return write(
        vault,
        f"{INSIGHTS}/{slug}.md",
        "---\n"
        f"title: {slug}\n"
        "type: insight\n"
        "created: 2026-01-01\n"
        "updated: 2026-01-01\n"
        "---\n\n"
        "## Prediction\n\n"
        "- id: r1\n"
        f"- check_by: {(dt.date.today() - dt.timedelta(days=3)).isoformat()}\n\n"
        "A repairable claim.\n",
    )


def _drop_media(vault: Path, name: str = "carrier.m4a") -> str:
    binary = vault / "Knowledge Base" / "Evidence" / "Audio" / name
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"\x00\x00\x00\x18ftypM4A fake audio")
    return binary.relative_to(vault).as_posix()


def _process_media(vault: Path, relative: str, *, operation: str = "process", **kwargs):
    return writer_lease.invoke_command(
        _command("process_media"), vault, path=relative, operation=operation, **kwargs
    )


def _stub_artifact_fetch(vault: Path, monkeypatch) -> None:
    staged_file = vault / "staged-artifact.png"
    staged_file.write_bytes(b"ok")
    monkeypatch.setattr(
        client_artifacts,
        "stage_artifact",
        lambda *_a, **_k: client_artifacts.StagedArtifact(
            file_id="file-one",
            path=staged_file,
            size=2,
            sha256="b" * 64,
            content_type="image/png",
            filename="artifact.png",
        ),
    )
    monkeypatch.setattr(
        client_artifacts,
        "preserve_stream",
        lambda _vault, **kwargs: type(
            "Result",
            (),
            {
                "as_dict": lambda self: {
                    "path": f"Knowledge Base/Evidence/case/raw/{kwargs['filename']}",
                    "size": 2,
                    "hash": "a" * 64,
                    "hash_algorithm": "sha256",
                    "content_type": "image/png",
                    "warnings": [],
                }
            },
        )(),
    )
    monkeypatch.setattr(
        client_artifacts,
        "active_manager",
        lambda: type("Manager", (), {"mutation_guard": lambda self, *_a, **_k: nullcontext()})(),
    )


def _preserve(vault: Path, monkeypatch, **kwargs):
    _stub_artifact_fetch(vault, monkeypatch)
    return writer_lease.invoke_command(
        _command("preserve_artifacts"),
        vault,
        scope="case",
        category="raw",
        files=[{"download_url": "https://files.example/one", "file_id": "file-one"}],
        **kwargs,
    )


class _CommitCount:
    def __init__(self, result, calls: int) -> None:
        self.result = result
        self.calls = calls


def _count_commits(monkeypatch, run) -> _CommitCount:
    """Run `run` and report how many times the canonical writer marked a commit.

    A no-write pin has to prove the invocation committed nothing rather than
    assume it: "no block" is equally the shape of the contract holding and of a
    fixture that quietly stopped exercising the leaf.
    """
    calls = {"n": 0}
    real = writer_lease.mark_active_mutation_committed

    def spy() -> None:
        calls["n"] += 1
        real()

    monkeypatch.setattr(writer_lease, "mark_active_mutation_committed", spy)
    try:
        return _CommitCount(run(), calls["n"])
    finally:
        monkeypatch.setattr(writer_lease, "mark_active_mutation_committed", real)


# ==========================================================================
# 1.1 — each leaf carries exactly one block reflecting the post-batch projection
# ==========================================================================


def test_adopt_vault_carries_one_block(vault: Path) -> None:
    _seed(vault)

    response = _adopt(vault)

    assert "due_state" in response, response
    assert response["due_state"] == due_state_module.served(vault)


def test_adoption_studio_apply_carries_one_block(vault: Path) -> None:
    _seed(vault)

    response = _studio_apply(vault)

    assert "due_state" in response, response
    assert response["due_state"] == due_state_module.served(vault)


def test_adoption_studio_apply_proposal_carries_one_terminal_block(tmp_path: Path) -> None:
    from test_adoption_proposals import (
        _applied_run,
        _imported_paths,
        _legacy_vault,
        _submit_compilation,
    )

    vault = _legacy_vault(tmp_path)
    applied = _applied_run(vault)
    proposal = _submit_compilation(
        vault,
        applied["run_id"],
        applied["inventory_fingerprint"],
        _imported_paths(applied),
        title="Carrier proposal",
    )
    overdue_prediction(vault, "apply-proposal-carrier")
    due_state_module.reconcile(vault)
    due_state_module.reset_emission_state()
    before = _ledger(vault)

    response = writer_lease.invoke_command(
        _command("adoption_studio"),
        vault,
        action="apply-proposal",
        ref=proposal["ref"],
        expected_fingerprint=proposal["fingerprint"],
        why="Approved after reviewing the compiled synthesis.",
    )

    assert list(response).count("due_state") == 1
    assert response["due_state"] == due_state_module.served(vault)
    assert "_vault" not in response
    assert "_vault" not in response["due_state"]
    ledger = _ledger(vault)
    assert ledger["writes"] - before["writes"] == 1
    assert ledger["emissions"] - before["emissions"] == 1


def test_maintain_fix_carries_one_block(vault: Path) -> None:
    _seed(vault)
    _repairable_overdue(vault, "carrier-fix")

    response = _maintain(vault, mode="fix", dry_run=False)

    assert "due_state" in response, response
    assert response["due_state"] == due_state_module.served(vault)


def test_maintain_reconcile_carries_one_block(vault: Path) -> None:
    _seed(vault)

    response = _maintain(vault, mode="reconcile")

    assert "due_state" in response, response
    assert response["due_state"] == due_state_module.served(vault)


def test_maintain_backfill_ids_carries_one_block(vault: Path) -> None:
    _seed(vault)

    response = _maintain(vault, mode="backfill-ids", dry_run=False)

    assert "due_state" in response, response
    assert response["due_state"] == due_state_module.served(vault)


def test_preserve_artifacts_carries_one_block(vault: Path, monkeypatch) -> None:
    _seed(vault)
    before = _ledger(vault)

    response = _preserve(vault, monkeypatch)

    assert "due_state" in response, response
    assert response["due_state"] == due_state_module.served(vault)
    ledger = _ledger(vault)
    assert ledger["writes"] == before["writes"]
    assert ledger["emissions"] == before["emissions"] + 1


def test_process_media_carries_one_block(vault: Path) -> None:
    _seed(vault)
    relative = _drop_media(vault)
    before = _ledger(vault)

    response = _process_media(vault, relative)

    assert "due_state" in response, response
    assert response["due_state"] == due_state_module.served(vault)
    ledger = _ledger(vault)
    assert ledger["writes"] == before["writes"]
    assert ledger["emissions"] == before["emissions"] + 1


def test_structured_files_apply_carries_one_block(tmp_path: Path) -> None:
    from test_structured_file_migration import _seed as seed_collection

    manifest_path, _first, _second = seed_collection(tmp_path)
    overdue_prediction(tmp_path, "structured-carrier")
    due_state_module.reconcile(tmp_path)
    due_state_module.reset_emission_state()
    before = _ledger(tmp_path)
    plan = commands.op_maintain_memory(
        tmp_path, mode="structured-files", collection=manifest_path
    )

    response = _maintain(
        tmp_path,
        mode="structured-files",
        collection=manifest_path,
        apply=True,
        plan_id=plan["plan_id"],
        source_snapshot=plan["source_snapshot"],
        why="apply the reviewed readable representation",
    )

    assert "due_state" in response, response
    ledger = _ledger(tmp_path)
    assert ledger["writes"] == before["writes"]
    assert ledger["emissions"] == before["emissions"] + 1


def test_the_block_leaves_the_operation_outcome_keys_untouched(
    vault: Path, monkeypatch
) -> None:
    """The advisory is additive: same invocation, same outcome keys, plus one."""
    _seed(vault)
    carrying = _adopt(vault, directory="legacy-carrying")
    assert "due_state" in carrying, carrying

    monkeypatch.setattr(due_state_module, "served", lambda *_a, **_k: None)
    due_state_module.reset_emission_state()
    control = _adopt(vault, directory="legacy-control")

    assert "due_state" not in control, control
    assert set(carrying) - {"due_state"} == set(control)
    for key in ("ok", "state", "status", "terminal", "mutated"):
        assert carrying[key] == control[key]


# ==========================================================================
# 1.2 — batch-once, change-only, and the emission ledger
# ==========================================================================


def test_a_twelve_write_adopt_records_one_emission_for_twelve_writes(
    vault: Path,
) -> None:
    _seed(vault)
    before = _ledger(vault)

    response = _adopt(vault, count=12)

    assert "due_state" in response, response
    after = _ledger(vault)
    assert after["writes"] - before["writes"] == 12
    assert after["emissions"] - before["emissions"] == 1


def test_a_committing_invocation_with_unchanged_totals_carries_nothing(
    vault: Path, monkeypatch
) -> None:
    """Change-only, on a carrying leaf: the second batch owes the same, so it stays quiet."""
    _seed(vault)
    first = _preserve(vault, monkeypatch)
    assert "due_state" in first, first
    after_first = _ledger(vault)["emissions"]

    second = _preserve(vault, monkeypatch)

    assert "due_state" not in second, second
    assert _ledger(vault)["emissions"] == after_first


# ==========================================================================
# 1.3 — negative pins: no commit, no carriage
# ==========================================================================


def test_a_clean_vault_fix_carries_nothing(tmp_path: Path, monkeypatch) -> None:
    """A repair pass that commits nothing has no terminal to carry a block on.

    Deliberately NOT the canonical fixture vault. Measured: `audit_fix` composes
    a sub-index refresh into the same batch as its content repairs and commits
    it without counting it in `files_rewritten`, and on the fixture vault that
    refresh recurs on every pass — so a repeat `fix` there really does commit a
    governed write and really should carry. The scenario is about the pass that
    commits NOTHING, so it needs a vault whose indexes are already current, and
    the commit count is asserted rather than assumed.
    """
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(tmp_path))
    commands.op_bootstrap(tmp_path)
    overdue_prediction(tmp_path, "clean-vault-overdue")
    due_state_module.reconcile(tmp_path)
    due_state_module.reset_emission_state()

    commits = _count_commits(
        monkeypatch,
        lambda: writer_lease.invoke_command(
            _command("maintain_memory"), tmp_path, mode="fix", dry_run=False
        ),
    )

    assert due_state_module.served(tmp_path) is not None, "the projection owes nothing to report"
    assert commits.calls == 0, "the fixture committed, so this is not the no-write case"
    assert "due_state" not in commits.result, commits.result


def test_already_valid_media_carries_nothing(vault: Path, monkeypatch) -> None:
    """Re-processing media whose sidecar is already valid commits nothing."""
    _seed(vault)
    relative = _drop_media(vault, "already-valid.m4a")
    _process_media(vault, relative)
    due_state_module.reset_emission_state()

    commits = _count_commits(monkeypatch, lambda: _process_media(vault, relative))

    assert commits.calls == 0, "the fixture committed, so this is not the no-write case"
    assert "due_state" not in commits.result, commits.result


def test_process_media_retry_carries_nothing(vault: Path) -> None:
    """A re-enqueue commits nothing to the vault."""
    _seed(vault)
    relative = _drop_media(vault, "retry-carrier.m4a")
    _process_media(vault, relative)
    due_state_module.reset_emission_state()

    response = _process_media(vault, relative, operation="retry")

    assert "due_state" not in response, response


def test_scan_only_adopt_carries_nothing(vault: Path) -> None:
    _seed(vault)
    _overdue_legacy(vault, "legacy-scan", 3)

    response = writer_lease.invoke_command(
        _command("adopt_vault"), vault, mode="scan-only", path="legacy-scan"
    )

    assert "due_state" not in response, response


def test_the_default_dry_run_fix_carries_nothing(vault: Path) -> None:
    _seed(vault)
    _repairable_overdue(vault, "carrier-preview")

    response = _maintain(vault, mode="fix")

    assert response["dry_run"] is True
    assert "due_state" not in response, response


def test_adoption_studio_previews_carry_nothing(vault: Path) -> None:
    """`plan` must not burn the session's one change-only emission before `apply`."""
    from exomem import find

    _seed(vault)
    old = vault / "Old Notes"
    old.mkdir(parents=True, exist_ok=True)
    (old / "preview.md").write_text("# Preview\n\nSome legacy prose.\n", encoding="utf-8")
    find.clear_cache()
    started = commands.op_adoption_studio(vault, action="start", path="Old Notes")
    run_id = started["run_id"]
    commands.op_adoption_studio(vault, action="select", run_id=run_id, include=["Old Notes"])

    planned = writer_lease.invoke_command(
        _command("adoption_studio"), vault, action="plan", run_id=run_id
    )
    status = writer_lease.invoke_command(
        _command("adoption_studio"), vault, action="status", run_id=run_id
    )

    assert "due_state" not in planned, planned
    assert "due_state" not in status, status


def test_the_legacy_response_detail_carries_no_block(vault: Path) -> None:
    _seed(vault)

    response = _adopt(vault, response_detail="legacy")

    assert "due_state" not in response, response
    assert "summary" in response, "the legacy detail must still return the raw leaf"


# ==========================================================================
# 1.4 — failure isolation
# ==========================================================================


def test_an_unreadable_review_state_yields_no_block_and_completes(
    vault: Path, monkeypatch
) -> None:
    _seed(vault)
    relative = _drop_media(vault, "isolated.m4a")

    def unreadable(*_args, **_kwargs):
        raise OSError("review state is unreadable")

    monkeypatch.setattr(due_state_module, "served_entries", unreadable)

    response = _process_media(vault, relative)

    assert "due_state" not in response, response
    assert response["ok"] is True
    assert response["state"] == "committed"


def test_a_partially_failed_invocation_that_committed_still_carries(
    vault: Path,
) -> None:
    """One batch, some items excluded: the committed half still reports."""
    _seed(vault)
    selected = _overdue_legacy(vault, "legacy-partial", 4)
    selected.append("legacy-partial/does-not-exist.md")

    response = writer_lease.invoke_command(
        _command("adopt_vault"),
        vault,
        mode="copy-as-sources",
        selected_paths=selected,
    )

    assert "due_state" in response, response


# ==========================================================================
# 1.5 — the block reflects the POST-batch projection, never a stale read
# ==========================================================================


def test_the_adopt_block_reports_the_projection_after_the_batch(vault: Path) -> None:
    """Twelve writes, and the block is the vault's post-batch truth — measured.

    MEASURED, and load-bearing for the f23 driver's docstring: twelve
    `copy-as-sources` writes add ZERO due items, because they land as Source
    pages and the projected categories are authored on compiled pages. The
    ledger's write count moves by twelve and the due total does not move at all.
    So this batch delivers its one block as the FIRST QUALIFYING RESPONSE of the
    session, not because the counts changed — anyone reading a delivered block
    here as evidence that the bulk pages moved the counts would be reading it
    wrong.
    """
    _seed(vault)
    before_total = due_state_module.served(vault)["total"]
    before_writes = _ledger(vault)["writes"]

    response = _adopt(vault, count=12)

    assert _ledger(vault)["writes"] - before_writes == 12
    assert due_state_module.served(vault)["total"] == before_total
    assert response["due_state"] == due_state_module.served(vault)


def test_the_fix_block_counts_the_pages_the_pass_just_rewrote(vault: Path) -> None:
    """`maintain fix` has no full recompute, so its batch deltas must be wired."""
    _seed(vault)
    before = due_state_module.served(vault)["total"]
    _repairable_overdue(vault, "carrier-stale")

    response = _maintain(vault, mode="fix", dry_run=False)

    assert response["due_state"]["total"] == before + 1


def test_the_reconcile_block_counts_a_page_written_out_of_band(vault: Path) -> None:
    """Reconcile's own full recompute is the delta path for this mode (design D3)."""
    _seed(vault)
    before = due_state_module.served(vault)["total"]
    overdue_prediction(vault, "carrier-out-of-band")

    response = _maintain(vault, mode="reconcile")

    assert response["due_state"]["total"] == before + 1
