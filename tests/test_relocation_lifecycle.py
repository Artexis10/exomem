"""A relocation keeps each page's relation-review lifecycle record in step with its bytes.

OpenSpec add-curation-relocation-protocol. Each test names the failure it catches.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from exomem import curation, relation_review, semantic_contract, semantic_writes
from exomem import edit as edit_module
from exomem import note as note_module
from exomem.cli_ops import OpError
from exomem.vault import content_hash, parse_frontmatter

FOLDER = "Knowledge Base/Notes/Insights"
HOME = "Knowledge Base/Notes/Insights/rehomed"
DAY = dt.date(2026, 7, 14)
OBSERVATION = "## Observations\n\n- [operating constraint] Keep retries bounded #reliability\n"


def _note(vault: Path, title: str, body: str, **kwargs: object) -> tuple[str, str]:
    created = note_module.note(
        vault, content=f"# {title}\n\n{body}\n\n{OBSERVATION}", note_type="insight", title=title,
        today=DAY,
        **kwargs,
    )
    fields, _body, _raw = parse_frontmatter((vault / created.path).read_text(encoding="utf-8"))
    return created.path, str(fields["exomem_id"])


def _link(rel: str) -> str:
    return f"[[{rel.removesuffix('.md')}]]"


def _plan(vault: Path, title: str, moves: list[tuple[str, str]]) -> dict[str, object]:
    steps: list[dict[str, object]] = [
        {"step_id": f"move-{index}", "kind": "move", "args": {"old_path": old, "new_path": new}}
        for index, (old, new) in enumerate(moves)
    ]
    steps.append({"step_id": "repoint", "kind": "repoint-links", "args": {}})
    return curation.propose(vault, {"version": 1, "title": title, "steps": steps})


def _run(vault: Path, proposed: dict[str, object]) -> dict[str, object]:
    result = curation.apply(
        vault,
        run_id=proposed["run_id"],
        plan_id=proposed["plan_id"],
        expected_plan_fingerprint=proposed["plan_fingerprint"],
        why="Approved relocation.",
    )
    while result["phase"] == "executing":
        result = curation.resume(vault, run_id=proposed["run_id"], plan_id=proposed["plan_id"])
    return result


def _compensate(vault: Path, run_id: str) -> dict[str, object]:
    proposed = curation.propose_compensation(vault, run_id=run_id)
    result = curation.apply_compensation(
        vault,
        run_id=run_id,
        plan_id=proposed["plan_id"],
        expected_plan_fingerprint=proposed["plan_fingerprint"],
        why="Undo the relocation.",
    )
    while result["phase"] == "compensating":
        result = curation.resume(vault, run_id=run_id, plan_id=proposed["plan_id"])
    return result


def _slot_state(vault: Path, identity: str, rel: str) -> str | None:
    prepared = relation_review.load_lifecycle_prepared(vault, identity)
    if prepared is None:
        return None
    text = (vault / rel).read_text(encoding="utf-8")
    current = relation_review.LifecyclePrimaryBinding(
        rel,
        content_hash(text),
        semantic_contract.review_content_fingerprint(identity, text),
    )
    return relation_review.lifecycle_prepared_state(prepared, current)


def _edit(vault: Path, rel: str, old: str, new: str) -> None:
    edit_module.edit(vault, path=rel, why="reword", old_string=old, new_string=new, today=DAY)


def test_a_rehome_of_a_repointed_page_resumes_takes_edits_and_undoes(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a relocation that strands a page's lifecycle record or its reviewed-none decision.

    A rename's repoint left the referrer's record behind its bytes, so the rehome that moved the
    referrer refused stale; a move left the moved page's decision behind, so the undo refused the
    page as unreviewed; a warm-up refusal made the rehome's run dead instead of resumable.
    """
    target, target_id = _note(
        vault, "Lifecycle target", "Retries stay bounded under load.",
        relation_disposition="reviewed_none", relation_review_reason="Nothing in the vault fits",
    )
    referrer, referrer_id = _note(
        vault, "Lifecycle referrer", f"Builds on {_link(target)} for its limits.",
        relation_disposition="reviewed_none", relation_review_reason="Its link is enough",
    )
    # A governed edit gives each page a lifecycle record, as pages in use have.
    for rel in (target, referrer):
        _edit(vault, rel, "Keep retries", "Hold retries")
    renamed = target.replace(FOLDER, f"{FOLDER}/renamed")
    assert _run(vault, _plan(vault, "Rename", [(target, renamed)]))["phase"] == "completed"
    assert _link(renamed) in (vault / referrer).read_text(encoding="utf-8")
    assert _slot_state(vault, referrer_id, referrer) == "committed"
    assert _slot_state(vault, target_id, renamed) == "committed"

    rehomed = referrer.replace(FOLDER, HOME)
    pre_rehome = {rel: (vault / rel).read_bytes() for rel in (referrer, renamed)}
    rehome = _plan(vault, "Rehome", [(referrer, rehomed)])
    real_preflight = semantic_writes.preflight_move
    warming = [OpError(
        "MUTATION_WARMING", "semantic corpus warm-up is still in progress",
        details={"status": "retryable", "committed": False, "retry_after_ms": 750},
    )]

    def warming_once(*args: object, **kwargs: object) -> object:
        if warming:
            raise warming.pop()
        return real_preflight(*args, **kwargs)

    monkeypatch.setattr(semantic_writes, "preflight_move", warming_once)
    with pytest.raises(curation.CurationError) as refused:
        curation.apply(
            vault, run_id=rehome["run_id"], plan_id=rehome["plan_id"],
            expected_plan_fingerprint=rehome["plan_fingerprint"], why="Approved relocation.",
        )
    assert refused.value.code == "MUTATION_WARMING"
    resealed = _plan(vault, "Rehome", [(referrer, rehomed)])
    assert resealed["run_id"] == rehome["run_id"]
    assert (resealed["phase"], resealed["retryable"], resealed["next_action"]) == (
        "failed", True, "resume",
    )
    result = curation.resume(vault, run_id=rehome["run_id"], plan_id=rehome["plan_id"])
    while result["phase"] == "executing":
        result = curation.resume(vault, run_id=rehome["run_id"], plan_id=rehome["plan_id"])
    assert result["phase"] == "completed", result
    assert _slot_state(vault, referrer_id, rehomed) == "committed"

    _edit(vault, rehomed, "its limits", "its bounds")
    assert _slot_state(vault, referrer_id, rehomed) == "committed"
    with pytest.raises(curation.CurationError) as stale:
        curation.propose_compensation(vault, run_id=rehome["run_id"])
    assert stale.value.code == "CURATION_BINDING_STALE"
    _edit(vault, rehomed, "its bounds", "its limits")

    undo = _compensate(vault, rehome["run_id"])
    assert undo["phase"] == "compensated"
    assert not (vault / rehomed).exists()
    assert {rel: (vault / rel).read_bytes() for rel in pre_rehome} == pre_rehome



@pytest.mark.parametrize("changed_page", ["moved", "referrer"])
def test_a_relocation_refuses_at_proposal_a_page_changed_outside_a_governed_write(
    vault: Path, changed_page: str
) -> None:
    """Catches a relocation that starts on a page whose lifecycle record no longer binds its bytes.

    It would carry that record forward over bytes nothing reviewed, or refuse mid-run after it
    moved other pages. The moved page and a repointed referrer reach the check by different steps.
    """
    target, _ = _note(
        vault, "Stale target", "Retries stay bounded under load.",
        relation_disposition="reviewed_none", relation_review_reason="Nothing in the vault fits",
    )
    referrer, _ = _note(
        vault, "Stale referrer", f"Builds on {_link(target)} for its limits.",
        relation_disposition="reviewed_none", relation_review_reason="Its link is enough",
    )
    for rel in (target, referrer):
        _edit(vault, rel, "Keep retries", "Hold retries")
    changed = target if changed_page == "moved" else referrer
    page = vault / changed
    page.write_text(page.read_text(encoding="utf-8").replace("Hold retries", "Cap retries"), encoding="utf-8")

    with pytest.raises(curation.CurationError) as refused:
        _plan(vault, "Rename", [(target, target.replace(FOLDER, f"{FOLDER}/renamed"))])

    assert refused.value.code == "LIFECYCLE_TRANSITION_STALE"
    assert changed in refused.value.reason
    assert (vault / target).exists()


def test_the_undo_of_a_rename_leaves_the_referrer_record_bound_for_its_next_edit(vault: Path) -> None:
    """Catches an undo that restores a repointed referrer's bytes but not its lifecycle record.

    The referrer's next governed edit or relocation would then refuse it as changed outside a
    governed write.
    """
    target, target_id = _note(
        vault, "Undo target", "Retries stay bounded under load.",
        relation_disposition="reviewed_none", relation_review_reason="Nothing in the vault fits",
    )
    referrer, referrer_id = _note(
        vault, "Undo referrer", f"Builds on {_link(target)} for its limits.",
        relation_disposition="reviewed_none", relation_review_reason="Its link is enough",
    )
    for rel in (target, referrer):
        _edit(vault, rel, "Keep retries", "Hold retries")
    run = _plan(vault, "Rename", [(target, target.replace(FOLDER, f"{FOLDER}/renamed"))])
    assert _run(vault, run)["phase"] == "completed"

    assert _compensate(vault, run["run_id"])["phase"] == "compensated"

    assert _link(target) in (vault / referrer).read_text(encoding="utf-8")
    assert _slot_state(vault, referrer_id, referrer) == "committed"
    assert _slot_state(vault, target_id, target) == "committed"
    _edit(vault, referrer, "its limits", "its bounds")
    assert _slot_state(vault, referrer_id, referrer) == "committed"


def test_a_plan_proposed_again_after_a_refused_step_reports_its_dead_run(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a re-proposal that answers `proposed` for a run whose step was refused for good.

    The agent would apply it again and meet the same refusal, or tell the person nothing ran.
    """
    target, _ = _note(
        vault, "Dead run target", "Retries stay bounded under load.",
        relation_disposition="reviewed_none", relation_review_reason="Nothing in the vault fits",
    )
    renamed = target.replace(FOLDER, f"{FOLDER}/renamed")
    proposed = _plan(vault, "Rename", [(target, renamed)])

    def refused(*args: object, **kwargs: object) -> object:
        raise OpError("SEMANTIC_CONTRACT_BLOCKED", "the moved page breaks its contract", details={})

    with monkeypatch.context() as patched:
        patched.setattr(semantic_writes, "preflight_move", refused)
        with pytest.raises(curation.CurationError) as failed:
            curation.apply(
                vault, run_id=proposed["run_id"], plan_id=proposed["plan_id"],
                expected_plan_fingerprint=proposed["plan_fingerprint"], why="Approved relocation.",
            )
    assert failed.value.code == "SEMANTIC_CONTRACT_BLOCKED"

    again = _plan(vault, "Rename", [(target, renamed)])

    assert again["run_id"] == proposed["run_id"]
    assert (again["phase"], again["retryable"], again["next_action"]) == ("failed", False, None)
    assert again["failed_step"] == "move-0"


def test_a_moved_page_keeps_a_decision_an_edit_made_in_force_for_its_next_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a move whose lifecycle record drops the reviewed-none decision that an edit recorded.

    Such a decision lives in its own record, not in the page's creation receipt, and only the
    page's lifecycle record points to it. Without that pointer the move refuses the page as
    unreviewed, and so would the page's first edit after it.
    """
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(tmp_path))
    # The first page takes the bootstrap review; the second page makes that review stale, so the
    # first page's next edit records a decision of its own.
    first, first_id = _note(tmp_path, "First page", "Retries stay bounded under load.")
    _note(
        tmp_path, "Second page", "Timeouts stay short.",
        relation_disposition="reviewed_none", relation_review_reason="Nothing in the vault fits",
    )
    edit_module.edit(
        tmp_path, path=first, why="reword", old_string="Keep retries", new_string="Hold retries",
        today=DAY, relation_disposition="reviewed_none",
        relation_review_reason="The second page is about timeouts",
    )
    _edit(tmp_path, first, "under load", "under heavy load")
    renamed = first.replace(FOLDER, f"{FOLDER}/renamed")
    assert _run(tmp_path, _plan(tmp_path, "Rename", [(first, renamed)]))["phase"] == "completed"

    _edit(tmp_path, renamed, "under heavy load", "under load")

    assert _slot_state(tmp_path, first_id, renamed) == "committed"
