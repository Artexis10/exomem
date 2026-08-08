"""The mutation state machine — correlating a guarded write's two halves.

Regression cover for the 2026-08-06 misclassification: a governed `remember` returned
a precommit refusal, then committed on the follow-up call, and the client reported the
whole write as failed. Both responses were individually correct; the *sequence* was
unreadable because the refusal had no relationship to the success envelope.
"""

from __future__ import annotations

import pytest

from exomem import mutation_terminal as mt

DRAFT = "01JAAAAAAAAAAAAAAAAAAAAAAA"


def _precommit(**overrides):
    payload = {
        "mutated": False,
        "draft_id": DRAFT,
        "draft_hash": "a" * 64,
        "draft_token": "opaque",
        "committable_after_review": True,
        "contract_result": {"blocking_findings": [{"code": "RELATION_DISPOSITION_MISSING"}]},
    }
    payload.update(overrides)
    return payload


def _committed(**overrides):
    payload = {"mutated": True, "draft_id": DRAFT, "path": "Knowledge Base/Notes/x.md"}
    payload.update(overrides)
    return mt.committed_terminal(
        payload, request_id="r-1", receipt_id="rc-1", idempotency_key=None
    )


# ------------------------------------------------------------------ the state machine


def test_states_are_closed_and_terminality_is_explicit():
    assert set(mt.STATES) == {
        "needs_review",
        "committed",
        "rejected",
        "retryable",
        "indeterminate",
    }
    assert mt.TERMINAL_STATES == {"committed", "rejected"}
    assert "needs_review" not in mt.TERMINAL_STATES


def test_needs_review_is_not_a_failure():
    """The decisive property. `ok` stays true and `terminal` is explicitly false."""
    env = mt.project_terminal(mt.needs_review_terminal(_precommit()))
    assert env["ok"] is True
    assert env["state"] == "needs_review"
    assert env["terminal"] is False
    assert env["mutated"] is False
    assert "error_code" not in env


def test_needs_review_names_the_next_action_and_denies_failure():
    env = mt.project_terminal(mt.needs_review_terminal(_precommit()))
    assert "not a failure" in env["next_action"]
    assert "draft identity" in env["next_action"]


def test_uncommittable_draft_points_at_the_blockers_instead():
    env = mt.project_terminal(
        mt.needs_review_terminal(_precommit(committable_after_review=False))
    )
    assert env["state"] == "needs_review"
    assert "blocking findings" in env["next_action"]


# ------------------------------------------------------------------- the correlation


def test_both_halves_share_one_operation_id():
    """Without this, a client has nothing tying the refusal to the later success."""
    first = mt.project_terminal(mt.needs_review_terminal(_precommit()))
    second = mt.project_terminal(_committed())
    assert first["operation_id"] == second["operation_id"] == DRAFT


def test_the_terminal_result_supersedes_the_nonterminal_one():
    first = mt.project_terminal(mt.needs_review_terminal(_precommit()))
    second = mt.project_terminal(_committed())
    assert first["terminal"] is False and second["terminal"] is True
    assert second["state"] == "committed" and second["mutated"] is True


def test_operation_id_is_found_through_a_nested_commit_record():
    terminal = mt.committed_terminal(
        {"mutated": True, "creation_commit": {"draft_id": DRAFT}, "path": "a.md"},
        request_id="r-1",
        receipt_id=None,
        idempotency_key=None,
    )
    assert mt.project_terminal(terminal)["operation_id"] == DRAFT


# ------------------------------------------------------------------------ narrowness


@pytest.mark.parametrize(
    "leaf",
    [
        {"hits": [1, 2, 3]},
        {"mutated": False},
        {"mutated": False, "note": "no draft identity"},
        {"draft_id": DRAFT},
        "not a mapping",
        None,
    ],
)
def test_non_guarded_results_pass_through_untouched(leaf):
    """Most non-committing leaves are ordinary reads and must not be re-shaped."""
    assert mt.needs_review_terminal(leaf) is leaf


def test_a_committed_result_is_never_treated_as_precommit():
    assert mt.needs_review_terminal({"mutated": True, "draft_id": DRAFT}) == {
        "mutated": True,
        "draft_id": DRAFT,
    }


# -------------------------------------------------------------------- back-compat


def test_status_still_agrees_with_state_for_existing_consumers():
    env = mt.project_terminal(_committed())
    assert env["status"] == env["state"] == "committed"


def test_diagnostics_stay_behind_full_detail_on_both_states():
    """Success burial is a presentation bug: noise must never lead the response."""
    pre = mt.needs_review_terminal(_precommit())
    assert "contract_result" not in mt.project_terminal(pre, "compact")
    assert mt.project_terminal(pre, "full")["diagnostics"]["contract_result"]

    com = _committed()
    assert "draft_id" not in mt.project_terminal(com, "compact")
    assert mt.project_terminal(com, "full")["diagnostics"]["draft_id"] == DRAFT


def test_legacy_detail_returns_the_untouched_leaf_for_both_states():
    raw = _precommit()
    assert mt.project_terminal(mt.needs_review_terminal(raw), "legacy") is raw


def test_envelope_keys_lead_the_compact_projection():
    """A client scanning the first keys must reach the decisive ones immediately."""
    env = mt.project_terminal(_committed())
    leading = list(env)[:4]
    assert leading == ["ok", "state", "terminal", "status"]
