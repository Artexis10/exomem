"""Write-path advisory review-state contract."""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest

from exomem import audit as audit_module
from exomem import commands, corpus_aware, mutation_terminal, review_state
from exomem import edit as edit_module
from exomem import note as note_module
from exomem import replace as replace_module

_IDENTITY_RE = re.compile(
    r"\[review: (?P<ref>exomem://review/write-advisory/[0-9a-f]{24}); "
    r"fingerprint: (?P<fingerprint>[0-9a-f]{24})\]$"
)


def _seed_page(vault: Path, name: str, body: str) -> str:
    path = f"Knowledge Base/Notes/Insights/{name}.md"
    target = vault / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "---\ntype: insight\nstatus: active\ncreated: 2026-08-16\n"
        "updated: 2026-08-16\ntags: []\n---\n"
        f"## Observations\n\n- [test] {body} ^{name}\n",
        encoding="utf-8",
    )
    return path


def _candidate(vault: Path, name: str = "counterpart") -> corpus_aware.DupCandidate:
    path = _seed_page(vault, name, f"Counterpart signal for {name}.")
    return corpus_aware.DupCandidate(path=path, title=f"Existing {name}", cosine=0.86)


def _retired_band_identity(
    vault: Path, self_path: str, candidate: corpus_aware.DupCandidate
) -> corpus_aware.WriteAdvisoryIdentity:
    """The identity the RETIRED `contradiction-band` kind would have minted.

    Reconstructed through the live formula with the kind briefly re-admitted,
    rather than restated here, so it is exactly what a pre-retirement dismissal
    record is keyed by.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            corpus_aware,
            "_WRITE_ADVISORY_KINDS",
            frozenset({*corpus_aware._WRITE_ADVISORY_KINDS, "contradiction-band"}),
        )
        return corpus_aware.write_advisory_identity(
            vault, kind="contradiction-band", self_path=self_path, candidate=candidate
        )


def _target(vault: Path) -> str:
    return _seed_page(vault, "editable", "Repeated body.")


def _identity(warning: str) -> tuple[str, str]:
    match = _IDENTITY_RE.search(warning)
    assert match is not None, warning
    return match.group("ref"), match.group("fingerprint")


def _wire_edit_candidate(
    monkeypatch: pytest.MonkeyPatch,
    candidate: corpus_aware.DupCandidate,
) -> None:
    # The suite disables embeddings globally; an empty value opens the real edit
    # advisory gate while the detector itself remains deterministic below.
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "")
    monkeypatch.setattr(
        corpus_aware,
        "detect_contradictions",
        lambda *args, **kwargs: [candidate],
    )


def _edit(vault: Path, path: str, marker: str):
    return edit_module.edit(
        vault,
        path=path,
        why=f"exercise write advisory {marker}",
        new_body=(
            f"## Observations\n\n- [test] Repeated body {marker}. ^repeated\n\n"
            "[[Knowledge Base/Notes/Insights/counterpart]]\n"
        ),
    )


def test_dismissed_overlap_warning_stays_quiet_on_next_write_end_to_end(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario: A dismissed write advisory stays quiet on the next write."""
    path = _target(vault)
    candidate = _candidate(vault)
    _wire_edit_candidate(monkeypatch, candidate)

    first = _edit(vault, path, "first")
    ref, fingerprint = _identity(first.warnings[0])
    commands.op_triage_memory(
        vault,
        ref=ref,
        action="dismiss",
        expected_fingerprint=fingerprint,
        why="Already reviewed this overlap.",
    )
    second = _edit(vault, path, "second")

    assert second.path == path
    assert not any("overlaps active note" in warning for warning in second.warnings)


def test_dismissed_near_duplicate_signal_is_suppressed(vault: Path) -> None:
    path = _target(vault)
    candidate = _candidate(vault)
    first = corpus_aware.emit_write_advisories(
        vault, self_path=path, kind="near-duplicate", candidates=[candidate]
    )
    ref, fingerprint = _identity(first[0])
    commands.op_triage_memory(
        vault,
        ref=ref,
        action="dismiss",
        expected_fingerprint=fingerprint,
        why="Reviewed near duplicate.",
    )

    assert corpus_aware.emit_write_advisories(
        vault, self_path=path, kind="near-duplicate", candidates=[candidate]
    ) == []


def test_counterpart_material_change_resurfaces_advisory_end_to_end(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario: A material change resurfaces the advisory."""
    path = _target(vault)
    candidate = _candidate(vault)
    _wire_edit_candidate(monkeypatch, candidate)

    first = _edit(vault, path, "first")
    ref, first_fingerprint = _identity(first.warnings[0])
    commands.op_triage_memory(
        vault,
        ref=ref,
        action="dismiss",
        expected_fingerprint=first_fingerprint,
        why="Reviewed the original counterpart.",
    )
    counterpart = vault / candidate.path
    counterpart.write_text(
        counterpart.read_text(encoding="utf-8").replace(
            "Counterpart signal", "Materially revised counterpart signal"
        ),
        encoding="utf-8",
    )

    resurfaced = _edit(vault, path, "second")
    next_ref, next_fingerprint = _identity(resurfaced.warnings[0])

    assert next_ref == ref
    assert next_fingerprint != first_fingerprint


def test_reopen_clears_suppression_end_to_end(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _target(vault)
    candidate = _candidate(vault)
    _wire_edit_candidate(monkeypatch, candidate)

    first = _edit(vault, path, "first")
    ref, fingerprint = _identity(first.warnings[0])
    commands.op_triage_memory(
        vault,
        ref=ref,
        action="dismiss",
        expected_fingerprint=fingerprint,
        why="Reviewed.",
    )
    commands.op_triage_memory(
        vault,
        ref=ref,
        action="reopen",
        expected_fingerprint=fingerprint,
    )

    reopened = _edit(vault, path, "second")

    assert any("overlaps active note" in warning for warning in reopened.warnings)


def test_unreadable_review_state_fails_open_to_advisory_emission(vault: Path) -> None:
    """Scenario: Unreadable review state fails open to emission."""
    path = _target(vault)
    candidate = _candidate(vault)
    review_state.state_path(vault).write_text("{not-json", encoding="utf-8")

    warnings = corpus_aware.emit_write_advisories(
        vault, self_path=path, kind="overlap", candidates=[candidate]
    )

    assert len(warnings) == 1
    assert "overlaps active note" in warnings[0]


def test_declared_rival_pair_produces_no_duplicate_advisory(vault: Path) -> None:
    """Scenario: A declared rival pair produces no duplicate advisory."""
    from exomem import contradiction_stance

    path = _target(vault)
    candidate = _candidate(vault)
    identity = contradiction_stance.pair_identity(vault, path, candidate.path)
    assert identity is not None
    review_state.ReviewStateStore(vault).apply(
        identity[0], identity[1], action="competing", why="Intentional alternatives."
    )

    assert corpus_aware.emit_write_advisories(
        vault,
        self_path=path,
        kind="near-duplicate",
        candidates=[candidate],
        apply_declared_pair_filter=True,
    ) == []


def test_triage_write_advisory_isolated_from_other_namespaces_and_note(
    vault: Path,
) -> None:
    """Scenarios: Triage accepts a write-advisory reference; namespaces stay isolated."""
    path = _target(vault)
    candidate = _candidate(vault)
    advisory = corpus_aware.write_advisory_identity(
        vault, kind="near-duplicate", self_path=path, candidate=candidate
    )
    target_before = (vault / path).read_bytes()
    files_before = {
        item.relative_to(vault).as_posix(): item.read_bytes()
        for item in vault.rglob("*")
        if item.is_file()
    }

    result = commands.op_triage_memory(
        vault,
        ref=advisory.ref,
        action="dismiss",
        expected_fingerprint=advisory.fingerprint,
        why="Reviewed duplicate candidate.",
    )

    refs = review_state.refs_for_paths(vault, [path])
    target_ref = refs[path]
    attention_id = review_state.item_id(target_ref)
    activation_id = review_state.item_id(f"activation:{target_ref}")
    payload = review_state.ReviewStateStore(vault).load()
    changed = {
        item.relative_to(vault).as_posix()
        for item in vault.rglob("*")
        if item.is_file()
        and files_before.get(item.relative_to(vault).as_posix()) != item.read_bytes()
    }

    assert result["ref"] == advisory.ref
    assert advisory.review_id not in {attention_id, activation_id}
    assert all(key.startswith(f"{advisory.review_id}:") for key in payload["records"])
    assert changed == set()
    assert review_state.state_path(vault).is_file()
    assert (vault / path).read_bytes() == target_before


def test_snoozed_advisory_resurfaces_after_expiry(vault: Path) -> None:
    path = _target(vault)
    candidate = _candidate(vault)
    advisory = corpus_aware.write_advisory_identity(
        vault, kind="overlap", self_path=path, candidate=candidate
    )
    store = review_state.ReviewStateStore(vault)
    store.apply(
        advisory.review_id,
        advisory.fingerprint,
        action="snooze",
        until="2099-01-01",
    )
    assert corpus_aware.emit_write_advisories(
        vault, self_path=path, kind="overlap", candidates=[candidate]
    ) == []

    store.apply(
        advisory.review_id,
        advisory.fingerprint,
        action="snooze",
        until="2026-01-01",
    )
    assert corpus_aware.emit_write_advisories(
        vault, self_path=path, kind="overlap", candidates=[candidate]
    )


def test_emit_batches_review_state_and_endpoint_ref_reads(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _target(vault)
    candidates = [_candidate(vault, "counterpart-a"), _candidate(vault, "counterpart-b")]
    calls = {"load": 0, "refs": 0}
    real_load = review_state.ReviewStateStore.load
    real_refs = review_state.refs_for_paths

    def counted_load(self):
        calls["load"] += 1
        return real_load(self)

    def counted_refs(root, paths):
        calls["refs"] += 1
        return real_refs(root, paths)

    monkeypatch.setattr(review_state.ReviewStateStore, "load", counted_load)
    monkeypatch.setattr(review_state, "refs_for_paths", counted_refs)

    warnings = corpus_aware.emit_write_advisories(
        vault, self_path=path, kind="overlap", candidates=candidates
    )

    assert len(warnings) == 2
    # One read for suppression, and one for the first-surfaced ledger's own
    # write, which re-reads under the lock rather than writing back a snapshot
    # taken before the advisories were composed. Both are per CALL, not per
    # advisory, which is what this test exists to pin: a second emission of the
    # same advisories reads once, because the ledger already holds them.
    assert calls == {"load": 2, "refs": 1}

    calls.update(load=0, refs=0)
    assert (
        len(
            corpus_aware.emit_write_advisories(
                vault, self_path=path, kind="overlap", candidates=candidates
            )
        )
        == 2
    )
    assert calls == {"load": 1, "refs": 1}


def test_legacy_replace_suppresses_dismissed_advisory_and_identifies_remaining_one(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_path = _target(vault)
    dismissed = _candidate(vault, "dismissed-counterpart")
    remaining = _candidate(vault, "remaining-counterpart")
    candidates = [dismissed, remaining]
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "")
    monkeypatch.setattr(corpus_aware, "_best_cosine_per_file", lambda *args, **kwargs: {})
    monkeypatch.setattr(corpus_aware, "suggest_related", lambda *args, **kwargs: [])
    monkeypatch.setattr(corpus_aware, "detect_duplicates", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        corpus_aware,
        "detect_contradictions",
        lambda *args, **kwargs: candidates,
    )
    note_kwargs = {
        "content": "## Observations\n\n- [test] Replacement body. ^replacement\n",
        "note_type": "insight",
        "title": "Replacement Advisory Target",
        "suggestions": False,
        "today": dt.date(2026, 8, 16),
    }

    first = note_module._legacy_note(vault, _planned_writes=[], **note_kwargs)
    first_warnings = [
        warning for warning in first.warnings if "overlaps active note" in warning
    ]
    ref, fingerprint = _identity(first_warnings[0])
    commands.op_triage_memory(
        vault,
        ref=ref,
        action="dismiss",
        expected_fingerprint=fingerprint,
        why="Reviewed replacement overlap.",
    )

    replaced = replace_module._legacy_replace(vault, old_path=old_path, **note_kwargs)
    replacement_warnings = [
        warning for warning in replaced.warnings if "overlaps active note" in warning
    ]

    assert len(replacement_warnings) == 1
    assert "remaining-counterpart" in replacement_warnings[0]
    assert "dismissed-counterpart" not in replacement_warnings[0]
    _identity(replacement_warnings[0])


def test_emit_batch_failure_renders_each_advisory_once_in_plain_form(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _target(vault)
    candidates = [
        _candidate(vault, "first-counterpart"),
        _candidate(vault, "second-counterpart"),
    ]

    success = corpus_aware.emit_write_advisories(
        vault, self_path=path, kind="overlap", candidates=candidates
    )
    assert len(success) == 2
    assert all(_IDENTITY_RE.search(warning) for warning in success)

    real_identity = corpus_aware.write_advisory_identity

    def fail_on_second(*args, candidate, **kwargs):
        if candidate.title == "Existing second-counterpart":
            raise OSError("counterpart disappeared")
        return real_identity(*args, candidate=candidate, **kwargs)

    monkeypatch.setattr(corpus_aware, "write_advisory_identity", fail_on_second)

    failed_open = corpus_aware.emit_write_advisories(
        vault, self_path=path, kind="overlap", candidates=candidates
    )

    assert len(failed_open) == 2
    assert not any(_IDENTITY_RE.search(warning) for warning in failed_open)
    assert sum("first-counterpart" in warning for warning in failed_open) == 1
    assert sum("second-counterpart" in warning for warning in failed_open) == 1


def test_compact_projection_retains_complete_write_advisory_identity(vault: Path) -> None:
    path = _target(vault)
    candidate = _candidate(vault, "counterpart-with-a-very-long-name-" + "x" * 120)
    warning = corpus_aware.emit_write_advisories(
        vault, self_path=path, kind="overlap", candidates=[candidate]
    )[0]
    compact = mutation_terminal.project_terminal(
        mutation_terminal.committed_terminal(
            {"path": path, "warnings": [warning]},
            request_id="11111111-1111-4111-8111-111111111111",
            receipt_id=None,
            idempotency_key=None,
        ),
        "compact",
    )

    assert len(compact["warnings"][0]) <= 300
    _identity(compact["warnings"][0])


@pytest.mark.parametrize("fingerprint", ["abc", "A" * 24, "a" * 23, "a" * 25])
def test_triage_rejects_malformed_write_advisory_fingerprint(
    vault: Path, fingerprint: str
) -> None:
    path = _target(vault)
    advisory = corpus_aware.write_advisory_identity(
        vault, kind="overlap", self_path=path, candidate=_candidate(vault)
    )

    with pytest.raises(ValueError, match="exactly 24 lowercase hex"):
        commands.op_triage_memory(
            vault,
            ref=advisory.ref,
            action="dismiss",
            expected_fingerprint=fingerprint,
            why="Reviewed.",
        )

    assert not review_state.state_path(vault).exists()


def test_triage_reason_and_until_rules_match_write_advisory_contract(vault: Path) -> None:
    path = _target(vault)
    advisory = corpus_aware.write_advisory_identity(
        vault, kind="overlap", self_path=path, candidate=_candidate(vault)
    )

    with pytest.raises(ValueError, match="dismiss requires `why`"):
        commands.op_triage_memory(
            vault,
            ref=advisory.ref,
            action="dismiss",
            expected_fingerprint=advisory.fingerprint,
        )
    commands.op_triage_memory(
        vault,
        ref=advisory.ref,
        action="snooze",
        until="2099-01-01",
        expected_fingerprint=advisory.fingerprint,
    )
    with pytest.raises(ValueError, match="until.*valid only for snooze"):
        commands.op_triage_memory(
            vault,
            ref=advisory.ref,
            action="reopen",
            until="2099-01-01",
            expected_fingerprint=advisory.fingerprint,
        )
    reopened = commands.op_triage_memory(
        vault,
        ref=advisory.ref,
        action="reopen",
        expected_fingerprint="",
    )
    assert reopened["state"] == "open"


def test_contradiction_band_kind_is_retired(vault: Path) -> None:
    """Its only producer was the write-path polarity call, which is gone."""
    candidate = _candidate(vault)
    assert corpus_aware._WRITE_ADVISORY_KINDS == frozenset({"near-duplicate", "overlap"})
    assert "contradiction-band" not in review_state.registered_families()
    with pytest.raises(ValueError, match="unknown write advisory kind"):
        corpus_aware.write_advisory_identity(
            vault,
            kind="contradiction-band",
            self_path=_target(vault),
            candidate=candidate,
        )
    assert corpus_aware.detected_overlap_advisory_groups([candidate]) == [
        ("overlap", [candidate])
    ]


def test_dismissed_contradiction_band_identity_does_not_suppress_the_overlap_advisory(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stated one-time cost of retiring the kind.

    A dismissal recorded against the retired identity does not transfer, so the
    pair resurfaces exactly once under the overlap identity — and is then
    suppressible there like any other advisory.
    """
    path = _target(vault)
    candidate = _candidate(vault)
    retired = _retired_band_identity(vault, path, candidate)
    commands.op_triage_memory(
        vault,
        ref=retired.ref,
        action="dismiss",
        expected_fingerprint=retired.fingerprint,
        why="Reviewed the contradiction band before the kind was retired.",
    )

    _wire_edit_candidate(monkeypatch, candidate)
    resurfaced = _edit(vault, path, "after-retirement")

    overlap = corpus_aware.write_advisory_identity(
        vault, kind="overlap", self_path=path, candidate=candidate
    )
    assert retired.ref != overlap.ref
    ref, fingerprint = _identity(resurfaced.warnings[0])
    assert ref == overlap.ref
    assert "claim-level check" not in resurfaced.warnings[0]

    # And the resurfaced advisory is suppressible under its own identity.
    commands.op_triage_memory(
        vault,
        ref=ref,
        action="dismiss",
        expected_fingerprint=fingerprint,
        why="Reviewed the overlap.",
    )
    quiet = _edit(vault, path, "after-dismissal")
    assert not any(overlap.ref in warning for warning in quiet.warnings)


def test_cited_but_unconnected_write_feedback_matches_relation_debt_audit(
    vault: Path,
) -> None:
    """Scenario: A cited but unconnected page reports debt consistently."""
    source = "Knowledge Base/Sources/Articles/2026-05-04-best-egcg-supplements"
    kwargs = {
        "content": "## Observations\n\n- [test] Cited but unconnected. ^cited-unconnected\n",
        "note_type": "insight",
        "title": "Cited but unconnected",
        "sources": [source],
        "today": dt.date(2026, 8, 16),
    }
    validation = note_module.note(vault, validate_only=True, **kwargs)
    result = note_module.note(
        vault,
        draft_id=validation.draft_id,
        draft_hash=validation.draft_hash,
        draft_token=validation.draft_token,
        relation_disposition="reviewed_none",
        relation_review_hash=validation.draft_hash,
        relation_review_reason="The page deliberately records an unresolved connection.",
        **kwargs,
    )

    findings = audit_module.audit(vault, categories=["relation_debt"]).findings

    assert result.write_feedback["relations"]["relation_debt"] is True
    assert result.write_feedback["sources"]["present"] is True
    assert [finding.path for finding in findings if finding.path == result.path] == [result.path]
