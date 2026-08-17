"""Issue #571: a mid-rebuild external epoch is a supersession, not instability.

`_rebuild_all_locked`'s `_mark_available` returns False purely because
`freshness.external_pending` was set *during* the pass. The projection identity
is byte-identical across attempts, membership agrees and the source versions are
current -- the rebuild is fine. A newer external epoch simply superseded its
publication.

Nothing inside the stabilization loop can clear that flag: `_mark_unavailable`
does not, and `find.unload_ram_caches` is only on the `resolver_versions is
None` branch. So attempt 2 pays a full second rebuild pass and fails at the
identical guard, deterministically, then raises *out of*
`_rebuild_all_off_boundary` past the one seam that can clear the condition --
that loop's `prepare_recall_publication` / `_reconcile_recall_publication` pair.

The reported cell logged 43 of these in ten hours on the interactive write path,
alongside `VAULT_LOCK_TIMEOUT` deferrals, with two user-facing writes exceeding
a 120 s client timeout against a ~750 ms budgeted median.

Two halves are tested here:

* **F1** -- refuse early as Class B (`GraphPublicationSuperseded`) instead of
  paying the second doomed pass.
* **F2** -- let `_rebuild_all_off_boundary` catch *that* refusal specifically,
  reconcile, and converge inside the same call rather than deferring to the next
  reconcile tick ~5 minutes later.

The retry carries its own budget, `REBUILD_SUPERSESSION_RETRIES`, rather than
the publication one. A superseded publication attempt is not always one pass:
the marker can fail on stabilization attempt 1 for a reason that has nothing to
do with an epoch -- the recall projection identity moving between writing the
availability marker and re-reading it -- which closes the supersession gate for
that attempt, so the refusal cannot fire until attempt 2. Charging the whole
`REBUILD_PUBLICATION_ATTEMPTS` budget to this condition would therefore cost
eight full rebuild passes where the unfixed code costs two, with
`claim_rebuild_owner` held across all of them, serializing graph rebuilds
against each other for the duration.

The catch must also be *narrow*. `GraphPublicationUnavailable` is raised for at
least two other, genuinely doomed conditions -- a lost rebuild owner and a
marker that will not publish for any other reason -- and those must keep today's
behaviour. The subtype is what makes them distinguishable; the type alone would
not.

Scope note. Convergence is gated on `projection_moved` being False, so the
*mixed* run -- a Class C attempt followed by a supersession -- deliberately
still raises `GraphProjectionMoved` and still pays the ~5-minute deferral. That
is the conservative choice: Class C owes the registry exactly one mark, and
refusing early there would both misname the class and still allocate that epoch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import epistemic_graph, freshness, graph_sync
from exomem import find as find_module
from exomem import vault as vault_module
from exomem.epistemic_graph import EpistemicGraphIndex

PAGE_A = "Knowledge Base/Notes/Insights/midflight-a.md"
PAGE_B = "Knowledge Base/Notes/Insights/midflight-b.md"


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
    (root / PAGE_A).write_text(_page("A", "A claims against [[midflight-b]]."), encoding="utf-8")
    (root / PAGE_B).write_text(_page("B", "B is a plain claim."), encoding="utf-8")
    _seed_live_freshness(root)
    EpistemicGraphIndex(root).rebuild_all()
    epistemic_graph.clear_publication_memos()
    yield root
    epistemic_graph.clear_publication_memos()


def _count_rebuild_passes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every full `_rebuild_all_pass`, the unit of wasted work at issue."""
    real = EpistemicGraphIndex._rebuild_all_pass
    passes: list[str] = []

    def counting_pass(
        self: EpistemicGraphIndex, resolver: vault_module.WikilinkResolver
    ) -> dict[str, int]:
        report = real(self, resolver)
        passes.append("pass")
        return report

    monkeypatch.setattr(EpistemicGraphIndex, "_rebuild_all_pass", counting_pass)
    return passes


def _supersede_after_each_pass(
    monkeypatch: pytest.MonkeyPatch, *, limit: int | None = None
) -> list[str]:
    """Land a newer external epoch mid-rebuild, exactly as production does.

    The mark lands after the pass and before `_mark_available`, which is the
    only window the reported failure can occupy: `_mark_available`'s first
    false-term is the flag, and the instrumented production attempts showed the
    projection identity matching on both sides of the pass.
    """
    real = EpistemicGraphIndex._rebuild_all_pass
    passes: list[str] = []

    def superseding_pass(
        self: EpistemicGraphIndex, resolver: vault_module.WikilinkResolver
    ) -> dict[str, int]:
        report = real(self, resolver)
        passes.append("pass")
        if limit is None or len(passes) <= limit:
            freshness.mark_external_pending(self.vault_root)
        return report

    monkeypatch.setattr(EpistemicGraphIndex, "_rebuild_all_pass", superseding_pass)
    return passes


def _refuse_the_marker_once_per_pair_then_supersede(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cost the worst case: a non-superseded marker failure, then a supersession.

    Every odd `_mark_available` fails the way a projection identity that moved
    across the marker write fails -- False, no external epoch -- and every even
    one lands the epoch. So each `_rebuild_all_locked` burns both stabilization
    passes before it can refuse.
    """
    calls = 0

    def marker(
        self: EpistemicGraphIndex,
        identity: tuple[tuple[int, int, str], str, str],
        *,
        checkpoint: object | None = None,
    ) -> bool:
        nonlocal calls
        calls += 1
        if calls % 2 == 1:
            return False
        freshness.mark_external_pending(self.vault_root)
        return False

    monkeypatch.setattr(EpistemicGraphIndex, "_mark_available", marker)


def _refuse_the_availability_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """A genuinely doomed publication: no external epoch, the marker just fails."""
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


# --- F1: one superseded pass, refused, not two -----------------------------


def test_a_single_midflight_epoch_costs_one_rebuild_pass_not_two(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The measured waste: one mark is enough to burn a whole second pass.

    Nothing in the stabilization loop can clear `external_pending`, so attempt 2
    rebuilds the entire graph again only to fail at the identical guard.
    """
    passes = _supersede_after_each_pass(monkeypatch, limit=1)

    with pytest.raises(epistemic_graph.GraphPublicationUnavailable):
        EpistemicGraphIndex(vault)._rebuild_all_locked()

    assert len(passes) == 1, (
        "a superseded publication paid a second, provably doomed full rebuild pass"
    )


def test_the_superseded_refusal_is_a_classified_class_b_failure(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Correct-by-construction: the subtype must keep the Class B answers."""
    _supersede_after_each_pass(monkeypatch, limit=1)
    epoch_probe = tmp_path / "epoch-probe-root"
    clock_before = freshness.mark_external_pending(epoch_probe)

    with pytest.raises(epistemic_graph.GraphPublicationUnavailable) as raised:
        EpistemicGraphIndex(vault)._rebuild_all_locked()

    error = raised.value
    assert not isinstance(error, epistemic_graph.GraphProjectionMoved)
    assert epistemic_graph.is_publication_failure(error) is True
    assert epistemic_graph.may_mark_external_pending(error) is False
    message = str(error)
    assert "Class B" in message
    assert "superseded" in message, (
        "the refusal reused the generic marker-refusal text: " + message
    )
    # `projection_moved` is False here, so the `finally` block's Class C
    # `mark_external_pending` must stay untouched: the only epoch allocated in
    # this run is the one the probe planted mid-pass.
    clock_after = freshness.mark_external_pending(epoch_probe)
    assert clock_after == clock_before + 2


def test_the_refusal_has_its_own_type_so_the_caller_can_catch_only_it(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`GraphPublicationUnavailable` alone cannot separate this from a doomed one.

    The same base type names a lost rebuild owner and a marker that will not
    publish for any other reason. F2's `continue` must not swallow either, so
    the supersession needs a distinguishable subtype.
    """
    _supersede_after_each_pass(monkeypatch, limit=1)

    with pytest.raises(epistemic_graph.GraphPublicationSuperseded):
        EpistemicGraphIndex(vault)._rebuild_all_locked()

    assert issubclass(
        epistemic_graph.GraphPublicationSuperseded, epistemic_graph.GraphPublicationUnavailable
    )


# --- F2: the same call converges instead of deferring ~5 minutes -----------


def test_the_off_boundary_call_converges_after_a_midflight_supersession(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The half that recovers write latency.

    `_rebuild_all_off_boundary` already begins each attempt with
    `prepare_recall_publication` and reconciles when it returns `None` -- which
    is exactly why an epoch marked *before* the call succeeds in one pass. The
    inner loop's raise is what destroyed that ability.
    """
    _supersede_after_each_pass(monkeypatch, limit=1)
    index = EpistemicGraphIndex(vault)

    report = index._rebuild_all_off_boundary()

    assert report["indexed_files"] == 2
    assert index.available() is True
    assert freshness.external_pending(vault) is False
    assert epistemic_graph.publication_refusal_active(vault) is False


def test_a_repeatedly_superseded_publication_terminates_within_the_budget(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retry is bounded by its own budget, and exhaustion is still Class B.

    Every attempt is superseded, so convergence never happens. The loop must
    stop after `REBUILD_SUPERSESSION_RETRIES` and re-raise the refusal it
    already holds -- never an unbounded retry, and never the whole publication
    budget.
    """
    passes = _supersede_after_each_pass(monkeypatch)
    index = EpistemicGraphIndex(vault)

    with pytest.raises(epistemic_graph.GraphPublicationSuperseded) as raised:
        index._rebuild_all_off_boundary()

    error = raised.value
    assert epistemic_graph.is_publication_failure(error) is True
    assert epistemic_graph.may_mark_external_pending(error) is False
    assert len(passes) == epistemic_graph.REBUILD_SUPERSESSION_RETRIES + 1, (
        "a supersession that repeats must not be charged the publication budget"
    )
    # R2: the doomed publication is memoized, so the next cycle does not re-pay it.
    assert epistemic_graph.publication_refusal_active(vault) is True
    # Even on the give-up path the epoch is reconciled away, so the graph is not
    # left fenced for the ~5 minutes until the next reconcile tick. Unfixed,
    # this call leaves the registry cool and the graph unavailable.
    assert freshness.external_pending(vault) is False
    assert index.available() is True


def test_the_costliest_interleaving_stays_inside_the_supersession_budget(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A superseded attempt is not always one pass -- this is the ceiling.

    `_mark_available` returns False with no external epoch when the recall
    projection identity moves between writing the availability marker and
    re-reading it. That closes the supersession gate on stabilization attempt 1,
    so the refusal cannot fire until attempt 2: two full passes for one
    publication attempt. It needs no `projection_moved` at any point, so a vault
    under concurrent writes -- the reported cell's own condition -- reaches it.

    Charged to `REBUILD_PUBLICATION_ATTEMPTS` this would be eight passes against
    the unfixed code's two. Its own budget holds it to four.
    """
    passes = _count_rebuild_passes(monkeypatch)
    _refuse_the_marker_once_per_pair_then_supersede(monkeypatch)
    index = EpistemicGraphIndex(vault)

    with pytest.raises(epistemic_graph.GraphPublicationSuperseded):
        index._rebuild_all_off_boundary()

    ceiling = (
        epistemic_graph.REBUILD_SUPERSESSION_RETRIES + 1
    ) * epistemic_graph.REBUILD_STABILIZATION_ATTEMPTS
    assert len(passes) == ceiling
    assert len(passes) < (
        epistemic_graph.REBUILD_PUBLICATION_ATTEMPTS
        * epistemic_graph.REBUILD_STABILIZATION_ATTEMPTS
    ), "the supersession retry was charged the whole publication budget"


# --- The catch must not widen -----------------------------------------------


def test_a_genuinely_doomed_publication_is_not_swallowed_by_the_retry(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A marker that fails with no external epoch keeps today's behaviour."""
    passes = _count_rebuild_passes(monkeypatch)
    _refuse_the_availability_marker(monkeypatch)

    with pytest.raises(epistemic_graph.GraphPublicationUnavailable) as raised:
        EpistemicGraphIndex(vault)._rebuild_all_off_boundary()

    error = raised.value
    assert not isinstance(error, epistemic_graph.GraphPublicationSuperseded)
    assert "availability marker" in str(error)
    assert len(passes) == epistemic_graph.REBUILD_STABILIZATION_ATTEMPTS, (
        "the doomed publication escaped after one off-boundary attempt, as before"
    )
    assert freshness.external_pending(vault) is False


def test_a_moved_projection_still_escapes_the_off_boundary_loop_as_class_c(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Class C regression: `GraphProjectionMoved` is not caught or retried."""
    _move_the_resolver_identity(monkeypatch)

    with pytest.raises(epistemic_graph.GraphProjectionMoved) as raised:
        EpistemicGraphIndex(vault)._rebuild_all_off_boundary()

    assert type(raised.value) is epistemic_graph.GraphProjectionMoved
    assert freshness.external_pending(vault) is True


def test_a_class_c_attempt_followed_by_a_supersession_stays_class_c(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mixed run, and the only way the new refusal could mis-fire.

    `projection_moved` is sticky, and the `finally` block owes the registry
    exactly one mark when it is set. Refusing early on attempt 2 would raise
    Class B *and* still mark externally pending -- a contract violation. The
    refusal is therefore gated on `projection_moved` being False.
    """
    _move_the_resolver_identity_on_the_first_attempt_only(monkeypatch)
    _supersede_after_each_pass(monkeypatch)

    with pytest.raises(epistemic_graph.GraphProjectionMoved) as raised:
        EpistemicGraphIndex(vault)._rebuild_all_locked()

    error = raised.value
    assert type(error) is epistemic_graph.GraphProjectionMoved
    message = str(error)
    assert "Class C" in message
    assert "resolver bytes" in message, "the Class C label must quote the Class C cause"
    assert epistemic_graph.may_mark_external_pending(error) is False
    assert freshness.external_pending(vault) is True


def test_a_supersession_that_coincides_with_a_moved_projection_stays_class_c(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reverse interleaving, and it is byte-identical to before the fix.

    The epoch lands during the pass, but the projection moved across that same
    pass, so the outer stability triple fails, `_mark_available` is never
    called, and the supersession gate never opens. Class C wins on both sides.
    """
    _supersede_after_each_pass(monkeypatch)
    monkeypatch.setattr(
        EpistemicGraphIndex, "_source_versions_current", lambda *_args, **_kwargs: False
    )

    with pytest.raises(epistemic_graph.GraphProjectionMoved) as raised:
        EpistemicGraphIndex(vault)._rebuild_all_locked()

    error = raised.value
    assert type(error) is epistemic_graph.GraphProjectionMoved
    assert "Class C" in str(error)
    assert "resolver source versions" in str(error)
    assert freshness.external_pending(vault) is True


def test_the_retry_does_not_mask_a_class_c_that_lands_after_it(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Publication attempt 1 is superseded; attempt 2's projection genuinely moves.

    The convergence retry must hand the second attempt's verdict through
    untouched, not absorb it into the Class B lane it came from.
    """
    locked_calls = 0
    real_locked = EpistemicGraphIndex._rebuild_all_locked

    def counting_locked(self: EpistemicGraphIndex) -> dict[str, int]:
        nonlocal locked_calls
        locked_calls += 1
        return real_locked(self)

    monkeypatch.setattr(EpistemicGraphIndex, "_rebuild_all_locked", counting_locked)

    real_pass = EpistemicGraphIndex._rebuild_all_pass

    def superseding_first_pass(
        self: EpistemicGraphIndex, resolver: vault_module.WikilinkResolver
    ) -> dict[str, int]:
        report = real_pass(self, resolver)
        if locked_calls == 1:
            freshness.mark_external_pending(self.vault_root)
        return report

    monkeypatch.setattr(EpistemicGraphIndex, "_rebuild_all_pass", superseding_first_pass)
    monkeypatch.setattr(
        EpistemicGraphIndex,
        "_source_versions_current",
        lambda *_args, **_kwargs: locked_calls < 2,
    )

    with pytest.raises(epistemic_graph.GraphProjectionMoved) as raised:
        EpistemicGraphIndex(vault)._rebuild_all_off_boundary()

    assert type(raised.value) is epistemic_graph.GraphProjectionMoved
    assert locked_calls == 2, "the supersession must have been retried exactly once"
    assert freshness.external_pending(vault) is True


# --- #479: every refusal this diff can reach names a runnable surface --------


def test_the_superseded_refusal_carries_the_runnable_reconcile_command(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A permanently-superseded publication is the terminal the caller sees."""
    _supersede_after_each_pass(monkeypatch)

    with pytest.raises(epistemic_graph.GraphPublicationSuperseded) as raised:
        EpistemicGraphIndex(vault)._rebuild_all_off_boundary()

    error = raised.value
    assert error.code == "GRAPH_SYNC_PUBLICATION_UNAVAILABLE"
    assert 'maintain_memory(mode="reconcile")' in error.remediation
    assert "exomem maintain --reconcile" in error.remediation


def test_the_publication_exhaustion_names_a_runnable_reconcile_command(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#479 regression: "run reconcile" matches neither the MCP tool nor the CLI.

    This raise stays reachable whenever the publication attempts are spent on
    the recall-preparation, epoch-coalescing or ticket-refresh paths, and its
    remediation reaches `graph_sync_remediation` in the mutation terminal
    verbatim rather than being rebuilt from `_RECONCILE_HINT` downstream.
    """
    monkeypatch.setattr(freshness, "prepare_recall_publication", lambda *_a, **_k: None)
    monkeypatch.setattr(
        EpistemicGraphIndex, "_reconcile_recall_publication", lambda *_a, **_k: None
    )

    with pytest.raises(graph_sync.GraphRebuildRegistrationError) as raised:
        EpistemicGraphIndex(vault)._rebuild_all_off_boundary()

    error = raised.value
    assert error.code == "GRAPH_SYNC_STABILIZATION_EXHAUSTED"
    assert f"after {epistemic_graph.REBUILD_PUBLICATION_ATTEMPTS} attempts" in str(error)
    assert "run reconcile to recover the derived graph" not in error.remediation
    assert 'maintain_memory(mode="reconcile")' in error.remediation
    assert "exomem maintain --reconcile" in error.remediation
