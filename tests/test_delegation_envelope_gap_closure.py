"""The routed envelope control and post-reset write-advisory quiet offer."""

from __future__ import annotations

import datetime as dt

import pytest

from exomem import commands, corpus_aware, envelope, review_state


@pytest.fixture
def config(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setenv("EXOMEM_CONFIG_PATH", str(path))
    return path


def test_triage_routes_the_envelope_before_item_references(config, vault) -> None:
    result = commands.op_triage_memory(
        vault,
        ref="exomem://envelope/proactive_capture",
        action="advisory",
    )

    assert result == {
        "class": "proactive_capture",
        "ceiling": "silent-capable",
        "disposition": "advisory",
        "provenance": "override",
        "ref": "exomem://envelope/proactive_capture",
    }


def test_triage_envelope_reset_inspects_the_derived_class_and_refuses_irrelevant_fields(
    config, vault
) -> None:
    ref = "exomem://envelope/proactive_capture"
    commands.op_triage_memory(vault, ref=ref, action="advisory")
    reset = commands.op_triage_memory(vault, ref=ref, action="reset")

    assert reset["disposition"] == "silent"
    assert reset["provenance"] == "derived"
    before = config.read_bytes()
    with pytest.raises(ValueError, match="does not accept `until`"):
        commands.op_triage_memory(vault, ref=ref, action="advisory", until="2099-01-01")
    with pytest.raises(ValueError, match="does not accept `expected_fingerprint`"):
        commands.op_triage_memory(vault, ref=ref, action="advisory", expected_fingerprint="a" * 24)
    assert config.read_bytes() == before


@pytest.mark.parametrize(
    "action_class,action",
    [("disclosure", "advisory"), ("restructure_execution", "silent"), ("missing", "off")],
)
def test_triage_preserves_the_envelope_refusal_and_config_bytes(
    config, vault, action_class, action
) -> None:
    before = config.read_bytes() if config.exists() else b""
    with pytest.raises(ValueError) as via_triage:
        commands.op_triage_memory(vault, ref=f"exomem://envelope/{action_class}", action=action)
    with pytest.raises(ValueError) as direct:
        envelope.set_disposition(action_class, action)
    assert str(via_triage.value) == str(direct.value)
    assert (config.read_bytes() if config.exists() else b"") == before


def test_a_normal_family_reset_starts_a_new_dismissal_epoch(vault) -> None:
    store = review_state.ReviewStateStore(vault)
    family = "near-duplicate"
    before = dt.datetime(2026, 8, 30, tzinfo=dt.UTC)
    after = before + dt.timedelta(seconds=1)
    for number in range(3):
        store.apply(
            f"{'a' * 23}{number}",
            f"{'b' * 23}{number}",
            action="dismiss",
            family=family,
            now=before,
        )

    store.set_disposition(family, "normal", now=after)

    payload = store.load()
    assert review_state.manual_dismissal_events(payload, family) == 0
    assert payload["adaptation_resets"][family] == "2026-08-30T00:00:01Z"


@pytest.mark.parametrize("kind", sorted(corpus_aware._WRITE_ADVISORY_KINDS))
def test_each_surfaced_write_advisory_records_its_family_for_triage(vault, kind) -> None:
    target = vault / "Knowledge Base/Notes/target.md"
    candidate = vault / "Knowledge Base/Notes/candidate.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Target\n", encoding="utf-8")
    candidate.write_text("# Candidate\n", encoding="utf-8")
    pair = corpus_aware.DupCandidate(
        path="Knowledge Base/Notes/candidate.md", title="Candidate", cosine=0.9
    )
    warning = corpus_aware.emit_write_advisories(
        vault, self_path="Knowledge Base/Notes/target.md", kind=kind, candidates=[pair]
    )[0]
    identity = corpus_aware.write_advisory_identity(
        vault, kind=kind, self_path="Knowledge Base/Notes/target.md", candidate=pair
    )
    payload = review_state.ReviewStateStore(vault).load()
    assert review_state.surfaced_family(payload, identity.review_id, identity.fingerprint) == kind

    commands.op_triage_memory(
        vault,
        ref=identity.ref,
        action="dismiss",
        expected_fingerprint=identity.fingerprint,
        why="handled: reviewed it",
    )
    record = review_state.ReviewStateStore(vault).load()["records"][
        f"{identity.review_id}:{identity.fingerprint}"
    ]
    assert record["family"] == kind
    assert identity.ref in warning


def test_the_first_eligible_write_advisory_carries_one_quiet_offer(vault) -> None:
    target = vault / "Knowledge Base/Notes/target.md"
    candidate = vault / "Knowledge Base/Notes/candidate.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Target\n", encoding="utf-8")
    candidate.write_text("# Candidate\n", encoding="utf-8")
    pair = corpus_aware.DupCandidate(
        path="Knowledge Base/Notes/candidate.md", title="Candidate", cosine=0.9
    )
    identity = corpus_aware.write_advisory_identity(
        vault,
        kind="near-duplicate",
        self_path="Knowledge Base/Notes/target.md",
        candidate=pair,
    )
    store = review_state.ReviewStateStore(vault)
    for number in range(3):
        store.apply(
            f"{'c' * 23}{number}",
            f"{'d' * 23}{number}",
            action="dismiss",
            family="near-duplicate",
        )

    warning = corpus_aware.emit_write_advisories(
        vault,
        self_path="Knowledge Base/Notes/target.md",
        kind="near-duplicate",
        candidates=[pair],
    )[0]

    assert identity.ref in warning
    assert "quiet" in warning.lower()
    assert (
        "[quiet offer: ref=exomem://review/family/near-duplicate; action=quiet; "
        "reason required]" in warning
    )
    assert warning.endswith(
        f"[review: {identity.ref}; fingerprint: {identity.fingerprint}]"
    )
    assert len(warning) <= 300
    assert review_state.quiet_offered_at(store.load(), "near-duplicate")
    again = corpus_aware.emit_write_advisories(
        vault,
        self_path="Knowledge Base/Notes/target.md",
        kind="near-duplicate",
        candidates=[pair],
    )[0]
    assert "quiet" not in again.lower()
    assert identity.ref in again
    assert len(again) <= 300


def test_an_offer_failure_does_not_resurrect_a_dismissed_write_advisory(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = vault / "Knowledge Base/Notes/target.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Target\n", encoding="utf-8")
    dismissed = vault / "Knowledge Base/Notes/dismissed.md"
    open_candidate = vault / "Knowledge Base/Notes/open.md"
    dismissed.write_text("# Dismissed\n", encoding="utf-8")
    open_candidate.write_text("# Open\n", encoding="utf-8")
    candidates = [
        corpus_aware.DupCandidate(
            path="Knowledge Base/Notes/dismissed.md", title="Dismissed", cosine=0.9
        ),
        corpus_aware.DupCandidate(path="Knowledge Base/Notes/open.md", title="Open", cosine=0.9),
    ]
    identities = [
        corpus_aware.write_advisory_identity(
            vault,
            kind="near-duplicate",
            self_path="Knowledge Base/Notes/target.md",
            candidate=candidate,
        )
        for candidate in candidates
    ]
    corpus_aware.emit_write_advisories(
        vault,
        self_path="Knowledge Base/Notes/target.md",
        kind="near-duplicate",
        candidates=candidates,
    )
    commands.op_triage_memory(
        vault,
        ref=identities[0].ref,
        action="dismiss",
        expected_fingerprint=identities[0].fingerprint,
        why="handled: already reviewed",
    )
    store = review_state.ReviewStateStore(vault)
    for number in range(3):
        store.apply(
            f"{'e' * 23}{number}",
            f"{'f' * 23}{number}",
            action="dismiss",
            family="near-duplicate",
        )

    def fail_offer(*args, **kwargs):
        raise OSError("offer store unavailable")

    monkeypatch.setattr(review_state.ReviewStateStore, "arm_quiet_offer", fail_offer)
    warnings = corpus_aware.emit_write_advisories(
        vault,
        self_path="Knowledge Base/Notes/target.md",
        kind="near-duplicate",
        candidates=candidates,
    )

    assert len(warnings) == 1
    warning = warnings[0]
    assert identities[1].ref in warning
    assert warning.endswith(
        f"[review: {identities[1].ref}; fingerprint: {identities[1].fingerprint}]"
    )
    assert identities[0].ref not in warning
    assert "quiet offer" not in warning
    assert len(warning) <= 300
    assert review_state.quiet_offered_at(store.load(), "near-duplicate") is None
