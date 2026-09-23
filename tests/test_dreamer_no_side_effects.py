"""D1-T6: the churn lesson as tests. The dreamer's seven nevers.

The dreamer never writes the vault, never marks freshness pending, never
enqueues graph debt, never builds or repairs an index, never takes the writer
lease or the mutation guard, never writes review state, and never loads or runs
a model. Each never is a spy around a real tick over a real vault with every
registered family, and each spy is proven to bite by a deliberately leaky
family that does exactly the forbidden thing.

Off is also pinned: with the dreamer off, the ordinary surfaces never consult
it, carry no `upkeep` block and create no sidecar.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path

import dreamer_fixture as fx
import pytest

from exomem import (
    claims,
    commands,
    corpus_aware,
    deferred_index,
    dreamer,
    dreamer_families,
    dreamer_store,
    embeddings,
    epistemic_graph,
    freshness,
    graph_drain,
    index_sync,
    memory_refs,
    review_state,
    working_set_index,
    working_set_runtime,
    writer_lease,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    freshness.clear()
    dreamer.reset_for_tests()
    dreamer_store.clear_reader_memo()
    yield
    dreamer.reset_for_tests()
    freshness.clear()
    dreamer_store.clear_reader_memo()


class Spies:
    def __init__(self) -> None:
        self.calls: dict[str, list[str]] = {}

    def record(self, group: str, name: str) -> None:
        self.calls.setdefault(group, []).append(name)

    def install(
        self,
        monkeypatch: pytest.MonkeyPatch,
        group: str,
        target: object,
        name: str,
        *,
        passthrough: bool = False,
    ) -> None:
        original = getattr(target, name)
        label = f"{getattr(target, '__name__', type(target).__name__)}.{name}"

        def spy(*args, **kwargs):
            self.record(group, label)
            if passthrough:
                return original(*args, **kwargs)
            raise AssertionError(f"the dreamer called {label}")

        monkeypatch.setattr(target, name, spy)


def _install_all(monkeypatch: pytest.MonkeyPatch) -> Spies:
    spies = Spies()
    for target, name in (
        (freshness, "mark_external_pending"),
        (freshness, "on_files_changed"),
    ):
        spies.install(monkeypatch, "freshness", target, name)
    for target, name in (
        (graph_drain, "note_graph_debt"),
        (deferred_index, "mark_graph_full_rebuild"),
        (index_sync, "drain_graph_work"),
        (epistemic_graph.EpistemicGraphIndex, "rebuild_all"),
        (epistemic_graph.EpistemicGraphIndex, "_connect"),
        (working_set_index.WorkingSetIndex, "rebuild"),
        (working_set_index.WorkingSetIndex, "update"),
        (working_set_runtime, "ensure_index"),
        (memory_refs, "request_rebuild"),
    ):
        spies.install(monkeypatch, "index", target, name)
    spies.install(monkeypatch, "lease", writer_lease.LeaseManager, "mutation_guard")
    spies.install(monkeypatch, "lease", writer_lease, "invoke_command")
    spies.install(monkeypatch, "review", review_state.ReviewStateStore, "_write")
    for target, name in (
        (corpus_aware, "_best_cosine_per_file"),
        (embeddings, "get_model"),
        (embeddings, "get_reranker"),
        (embeddings, "get_clip_model"),
        (embeddings, "embed_texts"),
        (claims, "verifier_polarity"),
    ):
        spies.install(monkeypatch, "model", target, name)
    return spies


def _run_to_quiet(vault: Path) -> list[dreamer.TickResult]:
    results = []
    for _ in range(20):
        result = dreamer.run_once(vault)
        results.append(result)
        if result.stop_reason != "pages" or not result.processed:
            break
    return results


def _leaky(action: Callable[[dreamer_families.Context, str], None]) -> dreamer_families.Family:
    return dreamer_families.Family(
        name="upkeep_leaky",
        kinds=("leak",),
        on_page=action,
        on_delete=lambda ctx, rel: None,
        revalidate=lambda ctx, row: None,
    )


# The seven nevers, each with the leak that proves its spy is wired.
_LEAKS: dict[str, Callable[[dreamer_families.Context, str], None]] = {
    "freshness": lambda ctx, rel: freshness.mark_external_pending(ctx.vault_root, paths=[rel]),
    "index": lambda ctx, rel: graph_drain.note_graph_debt(),
    "lease": lambda ctx, rel: writer_lease.get_manager().mutation_guard(ctx.vault_root),
    "review": lambda ctx, rel: review_state.ReviewStateStore(ctx.vault_root).apply(
        "0" * 24, "0" * 24, action="dismiss"
    ),
    "model": lambda ctx, rel: corpus_aware._best_cosine_per_file(
        ctx.vault_root, title="t", body="b", k=1, published_path=rel
    ),
}


@pytest.mark.parametrize("group", sorted(_LEAKS))
def test_each_spy_bites_on_a_leaky_family(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, group: str
) -> None:
    vault = fx.build(tmp_path)
    spies = _install_all(monkeypatch)
    monkeypatch.setattr(dreamer_families, "REGISTRY", [_leaky(_LEAKS[group])])
    result = dreamer.run_once(vault)
    assert result.stop_reason == "error"
    assert spies.calls.get(group), f"the {group} spy did not see the leak"


def test_no_vault_write_no_freshness_mark(tmp_path: Path, monkeypatch) -> None:
    vault = fx.build(tmp_path)
    before = fx.tree_state(vault)
    spies = _install_all(monkeypatch)
    results = _run_to_quiet(vault)
    assert all(result.stop_reason != "error" for result in results), results
    assert sum(len(result.processed) for result in results) >= 7
    assert fx.tree_state(vault) == before
    assert spies.calls.get("freshness") is None
    assert not freshness.external_pending(vault)


def test_no_graph_debt_no_index_build(tmp_path: Path, monkeypatch) -> None:
    vault = fx.build(tmp_path)
    spies = _install_all(monkeypatch)
    results = _run_to_quiet(vault)
    assert all(result.stop_reason != "error" for result in results), results
    assert spies.calls.get("index") is None
    assert int(deferred_index.graph_status(vault).get("count") or 0) == 0
    assert deferred_index.graph_full_rebuild_pending(vault) is None


def test_no_writer_lease_or_mutation_guard(tmp_path: Path, monkeypatch) -> None:
    vault = fx.build(tmp_path)
    spies = _install_all(monkeypatch)
    results = _run_to_quiet(vault)
    assert all(result.stop_reason != "error" for result in results), results
    assert spies.calls.get("lease") is None


def test_no_model_is_loaded_or_run(tmp_path: Path, monkeypatch) -> None:
    vault = fx.build(tmp_path)
    spies = _install_all(monkeypatch)
    monkeypatch.setenv("EXOMEM_CLAIM_POLARITY_NLI", "1")
    results = _run_to_quiet(vault)
    assert all(result.stop_reason != "error" for result in results), results
    assert spies.calls.get("model") is None


def test_no_review_state_write(tmp_path: Path, monkeypatch) -> None:
    vault = fx.build(tmp_path)
    before = review_state.state_path(vault)
    existed = before.exists()
    snapshot = before.read_bytes() if existed else None
    spies = _install_all(monkeypatch)
    results = _run_to_quiet(vault)
    assert all(result.stop_reason != "error" for result in results), results
    assert spies.calls.get("review") is None
    assert before.exists() is existed
    if existed:
        assert before.read_bytes() == snapshot


def _command(name: str):
    return next(command for command in commands.PRODUCT_COMMANDS if command.name == name)


def _reads(vault: Path) -> dict[str, object]:
    return {
        "ask_memory": commands.op_ask_memory(vault, query="retrieval fusion", limit=5),
        "activate_context": commands.op_activate_context(
            vault, turn="continue the retrieval fusion work"
        ),
    }


def _dump(value: object) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def test_off_is_byte_identical(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Off: the ordinary surfaces never consult the dreamer and change nothing."""
    from exomem import due_state

    scratch = vault / "Knowledge Base/Notes/Research/Infrastructure/dreamer-off-scratch.md"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.write_text(
        "---\ntitle: dreamer off scratch\ntype: research-note\nstatus: active\n"
        "created: 2026-08-01\nupdated: 2026-08-01\n---\n\n# Scratch\n\n## Observations\n\n"
        "- [finding] Baseline.\n",
        encoding="utf-8",
    )
    assert dreamer.setting() == "off"
    consulted: list[str] = []
    for target, name in (
        (dreamer, "status"),
        (dreamer, "start"),
        (dreamer_store, "read_view"),
        (dreamer_store.DreamerStore, "connect"),
    ):
        original = getattr(target, name)

        def spy(*args, _name=name, _original=original, **kwargs):
            consulted.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(target, name, spy)
    due_state.reset_emission_state()
    bootstrap = commands.op_bootstrap(vault)
    first = _reads(vault)
    # A second session start in the same process: nothing the dreamer might
    # remember between calls may change what the read surfaces answer.
    due_state.reset_emission_state()
    second = _reads(vault)
    write = writer_lease.invoke_command(
        _command("observe_memory"),
        vault,
        path="Knowledge Base/Notes/Research/Infrastructure/dreamer-off-scratch.md",
        operation="add",
        category="finding",
        content="The pool saturates above 400 readers.",
        tags=["infrastructure"],
    )
    assert consulted == []
    for payload in (bootstrap, first, second, write):
        assert "upkeep" not in _dump(payload)
    assert _dump(second["ask_memory"]) == _dump(first["ask_memory"])
    # The continuity token embeds its own mint time; everything else must match.
    assert _dump({**second["activate_context"], "continuity": None}) == _dump(
        {**first["activate_context"], "continuity": None}
    )
    assert not dreamer_store.sidecar_path(vault).exists()
    assert [t for t in threading.enumerate() if t.name == dreamer.THREAD_NAME] == []
