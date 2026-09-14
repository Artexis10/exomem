"""Standby warm-up, cutover readiness and promotion (D7-D9)."""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import readiness, service_standby


@pytest.fixture(autouse=True)
def _clean_standby_state():
    service_standby.reset_for_tests()
    readiness.reset()
    yield
    service_standby.reset_for_tests()
    readiness.reset()


def test_a_fresh_process_is_not_a_standby() -> None:
    assert service_standby.in_standby() is False
    assert service_standby.promoted() is False


def test_standby_reports_the_cutover_component_it_waits_on(monkeypatch) -> None:
    monkeypatch.setattr(service_standby.warmup, "model_preload_allowed", lambda *a: False)
    service_standby.enter_standby()
    readiness.mark_ready("lexical")
    payload = service_standby.readiness_payload()
    assert payload["standby"] is True
    assert payload["components"] == {
        "lexical": "ready",
        "graph_snapshot": "waiting",
        "semantic_corpus": "waiting",
    }
    assert payload["cutover_ready"] is False
    assert service_standby.waiting_component() == "graph_snapshot"


def test_embeddings_join_the_cutover_set_only_when_preload_is_allowed(monkeypatch) -> None:
    monkeypatch.setattr(service_standby.warmup, "model_preload_allowed", lambda *a: True)
    service_standby.enter_standby()
    payload = service_standby.readiness_payload()
    assert payload["components"] == {
        "lexical": "waiting",
        "embeddings": "waiting",
        "graph_snapshot": "waiting",
        "semantic_corpus": "waiting",
    }
    assert service_standby.waiting_component() == "lexical"


def test_cutover_becomes_ready_once_every_component_lands(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(service_standby.warmup, "model_preload_allowed", lambda *a: False)
    monkeypatch.setattr(service_standby, "snapshot_token", lambda root: "checkpoint-1")
    _stub_adoption(monkeypatch)
    _stub_corpus(monkeypatch)
    service_standby.enter_standby()
    readiness.mark_ready("lexical")
    assert service_standby.prove_graph_snapshot(tmp_path) is True
    # The write admission gate waits on the corpus (task 1.14), so a standby
    # that has not built one is a standby whose promotion cannot admit a write.
    assert service_standby.readiness_payload()["cutover_ready"] is False
    assert service_standby.waiting_component() == "semantic_corpus"
    assert service_standby.build_semantic_corpus(tmp_path) is True
    payload = service_standby.readiness_payload()
    assert payload["components"]["graph_snapshot"] == "ready"
    assert payload["components"]["semantic_corpus"] == "ready"
    assert payload["cutover_ready"] is True
    assert service_standby.waiting_component() is None


def test_an_unprovable_snapshot_leaves_the_component_waiting(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(service_standby.warmup, "model_preload_allowed", lambda *a: False)
    monkeypatch.setattr(service_standby, "snapshot_token", lambda root: None)
    _stub_adoption(monkeypatch, adopted=False, reason="snapshot_proof_declined")
    service_standby.enter_standby()
    readiness.mark_ready("lexical")
    assert service_standby.prove_graph_snapshot(tmp_path) is False
    assert service_standby.readiness_payload()["cutover_ready"] is False


def _stub_corpus(monkeypatch, *, ok=True):
    """Stand in for the semantic corpus build; its read-only-ness is pinned separately."""
    from exomem import semantic_contract

    def _build(_root, *_a, **_kw):
        if not ok:
            raise RuntimeError("corpus build failed")
        return None

    monkeypatch.setattr(semantic_contract, "build_corpus_context", _build, raising=True)


def _stub_adoption(monkeypatch, *, adopted=True, residue=(), reason="adopted"):
    """Stand in for the graph adoption; its own proof is covered by the graph suite."""
    from exomem import epistemic_graph

    monkeypatch.setattr(
        epistemic_graph.EpistemicGraphIndex,
        "adopt_published_snapshot",
        lambda self, **_kwargs: epistemic_graph.SnapshotAdoption(
            adopted, residue=tuple(residue), reason=reason
        ),
        raising=True,
    )


class _Activation:
    def __init__(self) -> None:
        self.released = False

    def release(self) -> None:
        self.released = True


def test_a_standby_takes_no_lease_and_starts_no_scheduler_until_promotion(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(service_standby.warmup, "model_preload_allowed", lambda *a: False)
    monkeypatch.setattr(service_standby, "snapshot_token", lambda root: "checkpoint-1")
    leases: list[str] = []
    monkeypatch.setattr(service_standby, "_acquire_ownership", lambda: leases.append("lease"))
    _stub_adoption(monkeypatch)
    activation = _Activation()
    service_standby.enter_standby()
    service_standby.register_activation(activation)
    service_standby.prove_graph_snapshot(tmp_path)
    assert leases == []
    assert activation.released is False

    record = service_standby.promote(tmp_path, migrated=False)
    assert leases == ["lease"]
    assert activation.released is True
    assert service_standby.promoted() is True
    assert record["snapshot"] == "current"
    assert record["reproved"] is True


def test_promotion_records_a_checkpoint_that_moved_under_the_standby(
    monkeypatch, tmp_path: Path
) -> None:
    """With no migration, promotion re-compares the checkpoint pair."""
    monkeypatch.setattr(service_standby.warmup, "model_preload_allowed", lambda *a: False)
    monkeypatch.setattr(service_standby, "_acquire_ownership", lambda: None)
    tokens = iter(["checkpoint-1", "checkpoint-2"])
    monkeypatch.setattr(service_standby, "snapshot_token", lambda root: next(tokens))
    _stub_adoption(monkeypatch)
    service_standby.enter_standby()
    service_standby.register_activation(_Activation())
    service_standby.prove_graph_snapshot(tmp_path)
    record = service_standby.promote(tmp_path, migrated=False)
    assert record["snapshot"] == "advanced"
    assert record["reproved"] is True


def test_a_declared_migration_re_runs_the_whole_source_proof(
    monkeypatch, tmp_path: Path
) -> None:
    """The migrator is the only writer between workers, so its run earns a proof.

    A checkpoint pair describes state the migrator may have rewritten, so
    comparing it would not be a re-validation at all.
    """
    monkeypatch.setattr(service_standby.warmup, "model_preload_allowed", lambda *a: False)
    monkeypatch.setattr(service_standby, "_acquire_ownership", lambda: None)
    monkeypatch.setattr(service_standby, "snapshot_token", lambda root: "checkpoint-1")
    _stub_adoption(monkeypatch)
    reproofs: list[str] = []
    real_reprove = service_standby._reprove

    def traced(vault_root):
        reproofs.append("reprove")
        return real_reprove(vault_root)

    monkeypatch.setattr(service_standby, "_reprove", traced)
    service_standby.enter_standby()
    service_standby.register_activation(_Activation())
    service_standby.prove_graph_snapshot(tmp_path)
    record = service_standby.promote(tmp_path, migrated=True)
    assert reproofs == ["reprove"]
    assert record["snapshot"] == "current"
    assert record["migrated"] is True


def test_a_failed_reproof_promotes_anyway_and_records_the_rebuild(
    monkeypatch, tmp_path: Path
) -> None:
    """A re-proof that fails must not strand the service unpromoted.

    The coalesced rebuild path owns that repair; refusing to promote would leave
    nobody serving at all.
    """
    from exomem import epistemic_graph

    monkeypatch.setattr(service_standby.warmup, "model_preload_allowed", lambda *a: False)
    monkeypatch.setattr(service_standby, "_acquire_ownership", lambda: None)
    monkeypatch.setattr(service_standby, "snapshot_token", lambda root: "checkpoint-1")
    _stub_adoption(monkeypatch)
    monkeypatch.setattr(
        service_standby,
        "_reprove",
        lambda root: epistemic_graph.SnapshotAdoption(False, reason="snapshot_proof_declined"),
    )
    activation = _Activation()
    service_standby.enter_standby()
    service_standby.register_activation(activation)
    service_standby.prove_graph_snapshot(tmp_path)
    record = service_standby.promote(tmp_path, migrated=True)
    assert record["snapshot"] == "rebuild-after-promotion"
    assert record["residue_applied"] == 0
    assert activation.released is True
    assert service_standby.promoted() is True


def test_promotion_is_idempotent(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(service_standby.warmup, "model_preload_allowed", lambda *a: False)
    calls: list[str] = []
    monkeypatch.setattr(service_standby, "_acquire_ownership", lambda: calls.append("lease"))
    _stub_adoption(monkeypatch)
    monkeypatch.setattr(service_standby, "snapshot_token", lambda root: "checkpoint-1")
    service_standby.enter_standby()
    service_standby.register_activation(_Activation())
    service_standby.prove_graph_snapshot(tmp_path)
    first = service_standby.promote(tmp_path, migrated=False)
    second = service_standby.promote(tmp_path, migrated=False)
    assert calls == ["lease"]
    assert second["already_promoted"] is True
    assert first.get("already_promoted") is None


def test_a_process_that_never_entered_standby_refuses_promotion(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="standby"):
        service_standby.promote(tmp_path, migrated=False)


def test_readiness_payload_is_absent_outside_standby_and_present_after_promotion(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(service_standby.warmup, "model_preload_allowed", lambda *a: False)
    monkeypatch.setattr(service_standby, "_acquire_ownership", lambda: None)
    monkeypatch.setattr(service_standby, "snapshot_token", lambda root: "checkpoint-1")
    _stub_adoption(monkeypatch)
    _stub_corpus(monkeypatch)
    assert service_standby.readiness_payload()["standby"] is False
    service_standby.enter_standby()
    readiness.mark_ready("lexical")
    service_standby.register_activation(_Activation())
    service_standby.prove_graph_snapshot(tmp_path)
    service_standby.build_semantic_corpus(tmp_path)
    service_standby.promote(tmp_path, migrated=False)
    payload = service_standby.readiness_payload()
    assert payload["standby"] is False
    assert payload["cutover_ready"] is True
    # The operator's first look when a cutover is fast but the writes after it
    # are not: what the promoted worker's warm is entitled to skip.
    assert payload["carried_from_standby"] == ["graph_handoff", "lexical", "semantic_corpus"]


def test_the_real_standby_warm_never_publishes_index_state(monkeypatch, tmp_path: Path) -> None:
    """A standby proves the catalog; it never reconciles or repairs it.

    `ensure_fresh` discards verified state and mutates the live sidecar under
    the per-vault publication barrier, and `request_repair` schedules a
    publication. Both belong to the worker still serving. This runs the real
    `warm()` and pins that neither is reached and no sidecar byte changes.
    """
    from exomem import lexstore, state_paths

    vault = tmp_path / "vault"
    (vault / "Knowledge Base" / "Notes").mkdir(parents=True)
    (vault / "Knowledge Base" / "Notes" / "one.md").write_text(
        "---\ntype: note\n---\n\n# One\n\nBody.\n", encoding="utf-8"
    )
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))

    forbidden: list[str] = []
    monkeypatch.setattr(
        lexstore, "ensure_fresh", lambda root: forbidden.append("ensure_fresh")
    )
    monkeypatch.setattr(
        lexstore, "request_repair", lambda root: forbidden.append("request_repair")
    )
    monkeypatch.setattr(service_standby, "snapshot_token", lambda root: None)

    def _fingerprint() -> dict[str, bytes]:
        import hashlib

        state = state_paths.vault_state_dir(vault)
        prints: dict[str, bytes] = {}
        if not state.exists():
            return prints
        for path in sorted(state.rglob("*")):
            if path.is_file():
                prints[str(path.relative_to(state))] = hashlib.sha256(
                    path.read_bytes()
                ).digest()
        return prints

    before = _fingerprint()
    service_standby.enter_standby()
    service_standby.warm(vault)

    assert forbidden == [], forbidden
    assert _fingerprint() == before, "a standby must not publish index state"


def test_the_standby_primes_the_resolver_even_when_caches_are_not_preloaded(
    monkeypatch, tmp_path: Path
) -> None:
    """The adoption's ordering must not depend on a policy that can skip it.

    `adopt_recall_origin` needs a seeded scope and the bounded repair needs a
    resolver at this exact checkpoint. The resolver used to come from
    `warm_caches`, which returns at its first gate in every resource mode that
    does not preload CPU caches -- the personal service's `mode=normal` among
    them -- so on that path the standby proved its snapshot with no resolver.
    """
    from exomem import find as find_module
    from exomem import mode

    vault = tmp_path / "vault"
    (vault / "Knowledge Base" / "Notes").mkdir(parents=True)
    (vault / "Knowledge Base" / "Notes" / "one.md").write_text(
        "---\ntype: note\n---\n\n# One\n\nBody.\n", encoding="utf-8"
    )
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    monkeypatch.setattr(mode, "preload_cpu_caches", lambda: False, raising=True)

    primed_before_proof: list[bool] = []
    real_prove = service_standby.prove_graph_snapshot

    def observed_prove(root):
        primed_before_proof.append(Path(root) in find_module._RECALL_RESOLVER_CACHE)
        return real_prove(root)

    monkeypatch.setattr(
        service_standby, "prove_graph_snapshot", observed_prove, raising=True
    )

    find_module.evict_resolver_caches(vault)
    service_standby.enter_standby()
    service_standby.warm(vault)

    assert primed_before_proof == [True], (
        "the standby proved its snapshot with no resident resolver, which is "
        "what leaves the promoted worker's first write without one"
    )


def test_an_uncurrent_catalog_leaves_lexical_as_the_waiting_component(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(service_standby.warmup, "model_preload_allowed", lambda *a: False)
    monkeypatch.setattr(service_standby, "prove_retrieval_catalog", lambda root: False)
    monkeypatch.setattr(service_standby, "snapshot_token", lambda root: "checkpoint-1")
    service_standby.enter_standby()
    service_standby.warm(tmp_path)
    assert service_standby.waiting_component() == "lexical"
    assert service_standby.readiness_payload()["cutover_ready"] is False


def test_the_catalog_proof_never_schedules_a_repair(monkeypatch, tmp_path: Path) -> None:
    from exomem import lexstore

    seen: list[dict] = []

    def _current(root, *, require_live_projection=True, schedule_repair=True):
        seen.append({"schedule_repair": schedule_repair})
        return True

    monkeypatch.setattr(lexstore, "maintained_content_index_enabled", lambda: True)
    monkeypatch.setattr(lexstore, "runtime_retrieval_catalog_current", _current)
    assert service_standby.prove_retrieval_catalog(tmp_path) is True
    assert seen == [{"schedule_repair": False}]


def test_the_cutover_block_reports_an_adoption_that_owes_a_drain(
    monkeypatch, tmp_path: Path
) -> None:
    """An adoption with a residue is not the same event as a clean one."""
    monkeypatch.setattr(service_standby.warmup, "model_preload_allowed", lambda *a: False)
    monkeypatch.setattr(service_standby, "snapshot_token", lambda root: "checkpoint-1")
    _stub_adoption(monkeypatch, residue=("Knowledge Base/Notes/a.md",), reason="adopted")
    service_standby.enter_standby()
    readiness.mark_ready("lexical")
    assert service_standby.prove_graph_snapshot(tmp_path) is True
    payload = service_standby.readiness_payload()
    assert payload["components"]["graph_snapshot"] == "ready"
    assert payload["adoption"] == {"residue": 1, "reason": "adopted"}


def test_a_refused_adoption_is_named_in_the_cutover_block(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(service_standby.warmup, "model_preload_allowed", lambda *a: False)
    _stub_adoption(monkeypatch, adopted=False, reason="residue_exceeds_drain_limit")
    service_standby.enter_standby()
    readiness.mark_ready("lexical")
    assert service_standby.prove_graph_snapshot(tmp_path) is False
    payload = service_standby.readiness_payload()
    assert payload["components"]["graph_snapshot"] == "waiting"
    assert payload["adoption"] == {"residue": 0, "reason": "residue_exceeds_drain_limit"}
    assert service_standby.waiting_component() == "graph_snapshot"


def test_the_standby_owns_its_adoption_so_the_warm_does_not_pay_it_twice(
    monkeypatch, tmp_path: Path
) -> None:
    """`warm_caches` adopts for an ordinary worker; a standby adopts itself."""
    from exomem import warmup

    durations: dict[str, float] = {}
    calls: list[str] = []
    monkeypatch.setattr(
        service_standby, "snapshot_token", lambda root: calls.append("token") or "c1"
    )
    _stub_adoption(monkeypatch)

    # An ordinary worker adopts through the warm and records what it owed.
    assert warmup._adopt_graph_snapshot(tmp_path, durations) is True
    assert durations == {"graph_snapshot_residue": 0.0}

    service_standby.enter_standby()
    standby_durations: dict[str, float] = {}
    assert warmup._adopt_graph_snapshot(tmp_path, standby_durations) is False
    assert standby_durations == {}
    # The standby's own step is the one that runs, and it reports the residue.
    assert service_standby.prove_graph_snapshot(tmp_path) is True
    assert service_standby.adoption_record() == {"residue": 0, "reason": "adopted"}


def test_the_lease_is_acquired_before_the_residue_is_enqueued(
    monkeypatch, tmp_path: Path
) -> None:
    """Enqueueing the repair is a write, so the lease has to come first.

    Until the lease says this vault is this process's own, it has no standing to
    schedule work on it -- the previous worker may still be relinquishing.
    """
    order: list[str] = []
    monkeypatch.setattr(service_standby.warmup, "model_preload_allowed", lambda *a: False)
    monkeypatch.setattr(service_standby, "snapshot_token", lambda root: "checkpoint-1")
    monkeypatch.setattr(
        service_standby, "_acquire_ownership", lambda: order.append("lease")
    )
    monkeypatch.setattr(
        service_standby,
        "_apply_residue",
        lambda root, adoption: (order.append("residue") or 1, ""),
    )
    _stub_adoption(monkeypatch, residue=("Knowledge Base/Notes/a.md",))
    service_standby.enter_standby()
    service_standby.register_activation(_Activation())
    service_standby.prove_graph_snapshot(tmp_path)

    record = service_standby.promote(tmp_path, migrated=False)

    assert order == ["lease", "residue"]
    assert record["residue_applied"] == 1
    # Still exactly once, and a second call short-circuits.
    assert service_standby.promote(tmp_path, migrated=False)["already_promoted"] is True
    assert order == ["lease", "residue"]


def test_a_migrated_promotion_reports_the_residue_the_reproof_found(
    monkeypatch, tmp_path: Path
) -> None:
    """`adoption_record()` must describe what this process owes, not the warm."""
    from exomem import epistemic_graph

    monkeypatch.setattr(service_standby.warmup, "model_preload_allowed", lambda *a: False)
    monkeypatch.setattr(service_standby, "_acquire_ownership", lambda: None)
    monkeypatch.setattr(service_standby, "snapshot_token", lambda root: "checkpoint-1")
    monkeypatch.setattr(service_standby, "_apply_residue", lambda root, adoption: (2, ""))
    _stub_adoption(monkeypatch)
    service_standby.enter_standby()
    service_standby.register_activation(_Activation())
    service_standby.prove_graph_snapshot(tmp_path)
    assert service_standby.adoption_record()["residue"] == 0

    monkeypatch.setattr(
        service_standby,
        "_reprove",
        lambda root: epistemic_graph.SnapshotAdoption(
            True, residue=("a.md", "b.md"), reason="adopted"
        ),
    )
    service_standby.promote(tmp_path, migrated=True)
    assert service_standby.adoption_record() == {"residue": 2, "reason": "adopted"}


def test_a_residue_that_could_not_be_enqueued_is_named_not_silently_dropped(
    monkeypatch, tmp_path: Path
) -> None:
    """Three outcomes, three records.

    "Nothing owed", "repair queued" and "repair we failed to queue" produced the
    same `residue_applied: 0` / `snapshot: current`, so an upgrade that silently
    lost its repair looked exactly like a clean one. The cold path keeps that
    distinction as `residue_enqueue_failed`; this path has to as well.
    """
    monkeypatch.setattr(service_standby.warmup, "model_preload_allowed", lambda *a: False)
    monkeypatch.setattr(service_standby, "_acquire_ownership", lambda: None)
    monkeypatch.setattr(service_standby, "snapshot_token", lambda root: "checkpoint-1")

    for failure in ("refused", "raised"):
        service_standby.reset_for_tests()
        readiness.reset()
        _stub_adoption(monkeypatch, residue=("Knowledge Base/Notes/a.md",))

        def _enqueue(self, residue, mode=failure):
            if mode == "raised":
                raise RuntimeError("deferred index unavailable")
            return False

        from exomem import epistemic_graph

        monkeypatch.setattr(
            epistemic_graph.EpistemicGraphIndex,
            "apply_adopted_residue",
            _enqueue,
            raising=True,
        )
        activation = _Activation()
        service_standby.enter_standby()
        service_standby.register_activation(activation)
        service_standby.prove_graph_snapshot(tmp_path)

        record = service_standby.promote(tmp_path, migrated=False)

        assert record["residue_applied"] == 0, failure
        assert record["snapshot"] == "rebuild-after-promotion", failure
        assert record["reason"] == "residue_enqueue_failed", failure
        # The worker still serves: the coalesced rebuild owns the repair.
        assert service_standby.promoted() is True, failure
        assert activation.released is True, failure


def test_a_clean_promotion_names_no_residue_failure(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(service_standby.warmup, "model_preload_allowed", lambda *a: False)
    monkeypatch.setattr(service_standby, "_acquire_ownership", lambda: None)
    monkeypatch.setattr(service_standby, "snapshot_token", lambda root: "checkpoint-1")
    _stub_adoption(monkeypatch)
    service_standby.enter_standby()
    service_standby.register_activation(_Activation())
    service_standby.prove_graph_snapshot(tmp_path)

    record = service_standby.promote(tmp_path, migrated=False)

    assert record["residue_applied"] == 0
    assert record["snapshot"] == "current"
    assert "reason" not in record


# --- Carrying the standby's warm forward (D7/D9, task 2.8) --------------------
#
# The 0.85.0 cutover took 1672.9 ms by the supervisor's clock and one 1.635 s
# request by the client's, and then the promoted worker refused every governed
# write for about thirty seconds with `MUTATION_WARMING warming_component=
# semantic_corpus` while it repeated, in the same process, the warm its own
# standby had already finished: a second `graph snapshot adoption` at 10:23:03
# and a 9935.2 ms corpus build whose predecessor cost 8516.1 ms at 10:23:16.
# A short unavailable window that hands back a process which refuses writes has
# moved the outage, not removed it. These tests pin the subtraction.


def _promote_from_a_complete_standby_warm(monkeypatch, tmp_path: Path):
    """Reach promotion the way a real standby does: warm, prove, build, promote."""
    monkeypatch.setattr(service_standby.warmup, "model_preload_allowed", lambda *a: False)
    monkeypatch.setattr(service_standby, "_acquire_ownership", lambda: None)
    monkeypatch.setattr(service_standby, "snapshot_token", lambda root: "checkpoint-1")
    _stub_adoption(monkeypatch)
    _stub_corpus(monkeypatch)
    service_standby.enter_standby()
    readiness.mark_ready("lexical")
    readiness.mark_ready("embeddings")
    service_standby.register_activation(_Activation())
    service_standby.prove_graph_snapshot(tmp_path)
    service_standby.build_semantic_corpus(tmp_path)
    return service_standby.promote(tmp_path, migrated=False)


def test_a_promotion_carries_the_standbys_finished_warm_forward(monkeypatch, tmp_path) -> None:
    record = _promote_from_a_complete_standby_warm(monkeypatch, tmp_path)
    assert record["snapshot"] == "current"
    carried = service_standby.carried_warm_components()
    assert carried == frozenset({"lexical", "embeddings", "semantic_corpus", "graph_handoff"})
    # Readable after `promote()` returns, which is the whole point: the promoted
    # worker's own `warm_all` is what reads it, and it starts inside `release()`.
    assert record["carried_from_standby"] == sorted(carried)


def test_a_component_the_standby_never_finished_is_not_carried(monkeypatch, tmp_path) -> None:
    """Carrying is a claim about work this process did, never about work it skipped."""
    monkeypatch.setattr(service_standby.warmup, "model_preload_allowed", lambda *a: False)
    monkeypatch.setattr(service_standby, "_acquire_ownership", lambda: None)
    monkeypatch.setattr(service_standby, "snapshot_token", lambda root: "checkpoint-1")
    _stub_adoption(monkeypatch)
    _stub_corpus(monkeypatch, ok=False)
    service_standby.enter_standby()
    readiness.mark_ready("lexical")
    service_standby.register_activation(_Activation())
    service_standby.prove_graph_snapshot(tmp_path)
    assert service_standby.build_semantic_corpus(tmp_path) is False
    service_standby.promote(tmp_path, migrated=False)
    carried = service_standby.carried_warm_components()
    assert "semantic_corpus" not in carried
    assert "embeddings" not in carried
    assert carried == frozenset({"lexical", "graph_handoff"})


@pytest.mark.parametrize(
    ("tokens", "verdict"),
    [
        (["checkpoint-1", "checkpoint-2"], "advanced"),
        (["checkpoint-1", None], "rebuild-after-promotion"),
    ],
)
def test_a_snapshot_that_moved_under_the_standby_carries_no_graph_handoff(
    monkeypatch, tmp_path: Path, tokens, verdict
) -> None:
    """Only a `current` verdict means re-adopting would buy nothing."""
    monkeypatch.setattr(service_standby.warmup, "model_preload_allowed", lambda *a: False)
    monkeypatch.setattr(service_standby, "_acquire_ownership", lambda: None)
    issued = iter(tokens)
    monkeypatch.setattr(service_standby, "snapshot_token", lambda root: next(issued))
    _stub_adoption(monkeypatch)
    _stub_corpus(monkeypatch)
    service_standby.enter_standby()
    readiness.mark_ready("lexical")
    service_standby.register_activation(_Activation())
    service_standby.prove_graph_snapshot(tmp_path)
    service_standby.build_semantic_corpus(tmp_path)
    record = service_standby.promote(tmp_path, migrated=False)
    assert record["snapshot"] == verdict
    carried = service_standby.carried_warm_components()
    assert "graph_handoff" not in carried
    # The corpus is still carried: a moved checkpoint says nothing about a
    # process-local, stat-captioned corpus context.
    assert "semantic_corpus" in carried


def test_a_failed_corpus_build_still_lets_the_upgrade_happen(monkeypatch, tmp_path) -> None:
    """A control that cannot fix anything must not block everything.

    A corpus build that RAN and failed settles the component. The promoted
    worker pays the same failing build whether it cut over or cold started, so
    holding the standby -- and with it every release -- for a defect the
    standby cannot fix would cost more than it prevents. What it must NOT do is
    claim the work: a failed build is not carried, so the promoted worker's own
    warm runs it.
    """
    monkeypatch.setattr(service_standby.warmup, "model_preload_allowed", lambda *a: False)
    monkeypatch.setattr(service_standby, "_acquire_ownership", lambda: None)
    monkeypatch.setattr(service_standby, "snapshot_token", lambda root: "checkpoint-1")
    _stub_adoption(monkeypatch)
    _stub_corpus(monkeypatch, ok=False)
    service_standby.enter_standby()
    readiness.mark_ready("lexical")
    service_standby.register_activation(_Activation())
    service_standby.prove_graph_snapshot(tmp_path)
    assert service_standby.build_semantic_corpus(tmp_path) is False

    assert service_standby.readiness_payload()["cutover_ready"] is True
    service_standby.promote(tmp_path, migrated=False)
    assert "semantic_corpus" not in service_standby.carried_warm_components()


def test_a_standby_that_never_reached_the_corpus_is_still_discarded(
    monkeypatch, tmp_path: Path
) -> None:
    """The other half: unattempted is not settled, so the budget still bites."""
    monkeypatch.setattr(service_standby.warmup, "model_preload_allowed", lambda *a: False)
    monkeypatch.setattr(service_standby, "snapshot_token", lambda root: "checkpoint-1")
    _stub_adoption(monkeypatch)
    service_standby.enter_standby()
    readiness.mark_ready("lexical")
    service_standby.prove_graph_snapshot(tmp_path)
    assert service_standby.readiness_payload()["cutover_ready"] is False
    assert service_standby.waiting_component() == "semantic_corpus"


def test_an_unpromoted_process_carries_nothing(monkeypatch, tmp_path: Path) -> None:
    """A cold start reads an empty set, so `warm_all` runs in full as it always has."""
    assert service_standby.carried_warm_components() == frozenset()
    _stub_adoption(monkeypatch)
    _stub_corpus(monkeypatch)
    monkeypatch.setattr(service_standby.warmup, "model_preload_allowed", lambda *a: False)
    monkeypatch.setattr(service_standby, "snapshot_token", lambda root: "checkpoint-1")
    service_standby.enter_standby()
    readiness.mark_ready("lexical")
    service_standby.prove_graph_snapshot(tmp_path)
    service_standby.build_semantic_corpus(tmp_path)
    # Warmed to cutover readiness, but never promoted.
    assert service_standby.carried_warm_components() == frozenset()


def test_the_standbys_corpus_build_writes_nothing_under_the_vault_or_the_state_root(
    tmp_path: Path, monkeypatch
) -> None:
    """A standby owns no state, so every step of its warm must be a read.

    The corpus build is the one this task added, and it is the one most likely
    to grow a publication later: `build_corpus_context` caches, and a cache is
    a short walk from a sidecar. This census fails here, on a fixture, rather
    than on a live cutover where the standby would be writing under a worker
    that still owns the vault.
    """
    import os

    vault = tmp_path / "vault"
    (vault / "Notes").mkdir(parents=True)
    (vault / "Notes" / "one.md").write_text("# One\n\nA note.\n", encoding="utf-8")
    (vault / "Notes" / "two.md").write_text("# Two\n\nAnother, see [[One]].\n", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(state))

    def census(root: Path) -> dict[str, tuple[int, int]]:
        out: dict[str, tuple[int, int]] = {}
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                path = Path(dirpath) / name
                try:
                    st = path.stat()
                except OSError:
                    continue
                out[str(path.relative_to(root))] = (st.st_size, st.st_mtime_ns)
        return out

    before_vault, before_state = census(vault), census(state)
    assert service_standby.build_semantic_corpus(vault) is True
    assert census(vault) == before_vault
    assert census(state) == before_state
