"""Issue #566: a Class B stabilization exhaustion must be a classified failure.

`EpistemicGraphIndex._rebuild_all_locked` can exhaust
`REBUILD_STABILIZATION_ATTEMPTS` for two different reasons:

* **Class C** -- the vault bytes or the recall projection provably moved under
  the in-flight proof. It raises `GraphProjectionMoved`, and the proof that
  detected it has already marked the registry externally pending exactly once.
* **Class B** -- the pass proved nothing stale and still could not publish. It
  used to raise a bare `RuntimeError`, which `is_publication_failure`
  deliberately leaves *unclassified* so genuine registry-loss signals keep the
  pre-contract behaviour.

The unclassified Class B raise is the reported defect. `may_mark_external_pending`
answered True for it, so `file_watcher._recover_external_pending` took the
`mark_external_pending` branch, never armed the Class B refusal memo, and
`publication_refusal_active` never tripped -- so a doomed full rebuild was
re-paid on every recovery cycle. The reported cell logged 143 such cycles over
days, at ~62 min intervals, holding ~4.5 cores continuously on rebuilds that
were all discarded.

Class B exhaustion is exactly what `GraphPublicationUnavailable` already
documents: "A rebuild proved nothing stale but still could not publish".

Scope note. Classifying the exhaustion stops the loop from *self-sustaining*
through the registry: the recovery handler no longer allocates a fresh
external-pending epoch, so `_reconcile_once`'s `pending_epoch is not None`
gate stops re-arming this lane. It does not by itself lower the rebuild
cadence, because the sibling `_recover_suspended_graph` lane -- unblocked once
the vault is no longer externally pending -- retries the same doomed
publication once per reconcile interval, and `PUBLICATION_RETRY_BACKOFF_SECONDS`
(60 s) is far below that interval (>= 300 s), so the memo has always expired by
then. Lowering the cadence of a persistently doomed publication is a separate
decision about the backoff, not about this classification.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import epistemic_graph, freshness, graph_sync
from exomem import find as find_module
from exomem import vault as vault_module
from exomem.epistemic_graph import EpistemicGraphIndex
from exomem.file_watcher import FileWatcher

PAGE_A = "Knowledge Base/Notes/Insights/exhaustion-a.md"
PAGE_B = "Knowledge Base/Notes/Insights/exhaustion-b.md"


def _page(title: str, body: str) -> str:
    return f"---\ntype: insight\nstatus: active\n---\n# {title}\n\n## Claim\n\n{body}\n"


def _seed_live_freshness(root: Path) -> None:
    freshness.seed(
        root,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in vault_module.walk_vault_md(root)),
    )
    kb = root / "Knowledge Base"
    freshness.seed(
        root,
        "kb",
        ((str(path), freshness.stat_signature(path)) for path in find_module._walk_md(kb)),
    )


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "Knowledge Base/Notes/Insights").mkdir(parents=True)
    (root / PAGE_A).write_text(_page("A", "A claims against [[exhaustion-b]]."), encoding="utf-8")
    (root / PAGE_B).write_text(_page("B", "B is a plain claim."), encoding="utf-8")
    _seed_live_freshness(root)
    EpistemicGraphIndex(root).rebuild_all()
    epistemic_graph.clear_publication_memos()
    yield root
    epistemic_graph.clear_publication_memos()


def _refuse_the_availability_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Class B: every pass is internally stable, but the marker will not publish."""
    monkeypatch.setattr(
        EpistemicGraphIndex, "_mark_available", lambda *_args, **_kwargs: False
    )


def _move_the_resolver_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Class C: the supplied freshness identity does not name the resolver bytes."""
    monkeypatch.setattr(
        EpistemicGraphIndex, "_resolver_source_versions", lambda *_args, **_kwargs: None
    )


def _move_the_resolver_identity_on_the_first_attempt_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Class C on attempt 1; every later attempt resolves its source versions."""
    real = EpistemicGraphIndex._resolver_source_versions
    attempts = 0

    def first_attempt_moves(
        self: EpistemicGraphIndex,
        resolver: vault_module.WikilinkResolver,
        expected_membership: frozenset[str],
    ) -> dict[str, epistemic_graph.GraphSourceSignature] | None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return None
        return real(self, resolver, expected_membership)

    monkeypatch.setattr(EpistemicGraphIndex, "_resolver_source_versions", first_attempt_moves)


# --- F1: the exhaustion is a classified Class B publication failure ---------


def test_class_b_exhaustion_raises_a_classified_publication_failure(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mechanism: the raised type must be one `is_publication_failure` knows."""
    _refuse_the_availability_marker(monkeypatch)

    with pytest.raises(RuntimeError, match="did not stabilize") as raised:
        EpistemicGraphIndex(vault)._rebuild_all_locked()

    error = raised.value
    assert not isinstance(error, epistemic_graph.GraphProjectionMoved)
    assert isinstance(error, epistemic_graph.GraphPublicationUnavailable)
    assert epistemic_graph.is_publication_failure(error) is True
    assert epistemic_graph.may_mark_external_pending(error) is False
    # Class B may never cool the vault-global registry (contract section 1).
    assert freshness.external_pending(vault) is False


def test_watcher_recovery_arms_the_memo_for_a_class_b_exhaustion(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The classification has to reach the watcher's own branch, not just the type."""
    _refuse_the_availability_marker(monkeypatch)
    watcher = FileWatcher(vault)
    epoch_probe = tmp_path / "epoch-probe-root"
    clock_before = freshness.mark_external_pending(epoch_probe)

    pending_epoch = freshness.mark_external_pending(vault)
    watcher._recover_external_pending(pending_epoch)

    assert epistemic_graph.publication_refusal_active(vault) is True
    assert EpistemicGraphIndex(vault).available() is False
    # R1: a Class B retry allocates no fresh external-pending epoch. One mark
    # above (ours) plus the probe below is the whole budget.
    clock_after = freshness.mark_external_pending(epoch_probe)
    assert clock_after == clock_before + 2


def test_second_recovery_cycle_does_not_repay_the_doomed_rebuild(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported symptom: ~62 min apart, forever, at full rebuild cost."""
    rebuilds: list[str] = []
    real_rebuild = EpistemicGraphIndex._rebuild_all_locked

    def counting_rebuild(self: EpistemicGraphIndex) -> dict[str, int]:
        rebuilds.append("rebuild")
        return real_rebuild(self)

    monkeypatch.setattr(EpistemicGraphIndex, "_rebuild_all_locked", counting_rebuild)
    _refuse_the_availability_marker(monkeypatch)
    watcher = FileWatcher(vault)

    watcher._recover_external_pending(freshness.mark_external_pending(vault))
    after_first_cycle = len(rebuilds)
    assert after_first_cycle >= 1, "the first cycle must actually have attempted a rebuild"

    watcher._recover_external_pending(freshness.mark_external_pending(vault))

    assert len(rebuilds) == after_first_cycle, (
        "the same doomed publication was re-paid at full rebuild cost"
    )


# --- The memo is a bounded backoff, never a permanent fence -----------------


def test_the_memo_expires_on_its_deadline(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(epistemic_graph, "PUBLICATION_RETRY_BACKOFF_SECONDS", 0.0)
    epistemic_graph.note_publication_refusal(vault)

    assert epistemic_graph.publication_refusal_active(vault) is False


def test_the_memo_expires_when_a_different_publication_is_asked_for(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _checkpoint(generation: int) -> graph_sync.GraphSyncCheckpoint:
        return graph_sync.GraphSyncCheckpoint.create(
            generation=generation,
            mutation_id=f"{generation:024x}",
            paths=((PAGE_A, "d" * 64),),
            created_paths=(PAGE_A,),
        )

    monkeypatch.setattr(graph_sync, "read_checkpoint", lambda *_a, **_k: _checkpoint(1))
    epistemic_graph.note_publication_refusal(vault)
    assert epistemic_graph.publication_refusal_active(vault) is True

    monkeypatch.setattr(graph_sync, "read_checkpoint", lambda *_a, **_k: _checkpoint(2))

    assert epistemic_graph.publication_refusal_active(vault) is False


# --- Class C is untouched ---------------------------------------------------


def test_class_c_exhaustion_keeps_its_type_and_its_single_mark(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the fix must not widen Class C into the publication classifier."""
    _move_the_resolver_identity(monkeypatch)
    epoch_probe = tmp_path / "epoch-probe-root"
    clock_before = freshness.mark_external_pending(epoch_probe)

    with pytest.raises(epistemic_graph.GraphProjectionMoved, match="did not stabilize") as raised:
        EpistemicGraphIndex(vault)._rebuild_all_locked()

    error = raised.value
    assert type(error) is epistemic_graph.GraphProjectionMoved
    assert epistemic_graph.is_publication_failure(error) is False
    assert epistemic_graph.may_mark_external_pending(error) is False
    # The proof marks exactly once and arms no Class B memo.
    assert freshness.external_pending(vault) is True
    assert epistemic_graph.publication_refusal_active(vault) is False
    clock_after = freshness.mark_external_pending(epoch_probe)
    assert clock_after == clock_before + 2


# --- F2: the failure is diagnosable ----------------------------------------


def test_the_message_names_the_class_and_the_marker_refusal_cause(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse_the_availability_marker(monkeypatch)

    with pytest.raises(RuntimeError) as raised:
        EpistemicGraphIndex(vault)._rebuild_all_locked()

    message = str(raised.value)
    assert "Class B" in message
    assert "availability marker" in message


def test_the_message_names_the_class_and_the_resolver_identity_cause(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _move_the_resolver_identity(monkeypatch)

    with pytest.raises(epistemic_graph.GraphProjectionMoved) as raised:
        EpistemicGraphIndex(vault)._rebuild_all_locked()

    message = str(raised.value)
    assert "Class C" in message
    assert "resolver bytes" in message


def test_the_message_distinguishes_a_moved_source_version_from_the_others(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A third cause, so the message is not merely a per-class constant."""
    monkeypatch.setattr(
        EpistemicGraphIndex, "_source_versions_current", lambda *_args, **_kwargs: False
    )

    with pytest.raises(epistemic_graph.GraphProjectionMoved) as raised:
        EpistemicGraphIndex(vault)._rebuild_all_locked()

    message = str(raised.value)
    assert "Class C" in message
    assert "resolver source versions" in message


def test_a_mixed_run_reports_the_cause_of_the_class_it_actually_raises(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Attempt 1 fires Class C, attempt 2 fires Class B; only two attempts exist.

    `projection_moved` is sticky by design -- the conservative choice, and the
    raised type must stay `GraphProjectionMoved`. A single sticky `cause`
    string is not: the last writer wins, so the Class B cause overwrote the
    Class C one and the message announced "Class C, projection moved" while
    quoting the reason the *marker* would not publish. That contradiction lands
    in exactly the mixed failure this message exists to explain.
    """
    _move_the_resolver_identity_on_the_first_attempt_only(monkeypatch)
    _refuse_the_availability_marker(monkeypatch)

    with pytest.raises(epistemic_graph.GraphProjectionMoved) as raised:
        EpistemicGraphIndex(vault)._rebuild_all_locked()

    message = str(raised.value)
    assert "Class C" in message
    assert "resolver bytes" in message, "the Class C label must quote the Class C cause"
    assert "availability marker" not in message, (
        "the Class C label quoted the Class B cause: " + message
    )


# --- The classified type must not cost the write path its runnable command ---


def test_the_class_b_remediation_keeps_the_runnable_reconcile_command() -> None:
    """`graph_sync._run` no longer wraps this raise, so it carries its own hint.

    A bare `RuntimeError` fell through to `_run`'s `else` and became
    `GraphRebuildStopped`, whose remediation is built from `_RECONCILE_HINT`.
    `GraphPublicationUnavailable` subclasses `GraphRebuildRegistrationError`,
    so it now passes through unwrapped and its own remediation reaches
    `graph_sync_remediation` in the mutation terminal payload verbatim. It has
    to name the same runnable surfaces -- "run reconcile" is an internal
    registry name that matches neither the MCP tool nor the CLI (#479).
    """
    remediation = epistemic_graph.GraphPublicationUnavailable("exhausted").remediation

    assert graph_sync._RECONCILE_HINT in remediation
    assert 'maintain_memory(mode="reconcile")' in remediation
    assert "exomem maintain --reconcile" in remediation
