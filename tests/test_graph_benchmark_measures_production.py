"""Guard the graph benchmarks against measuring a path production never takes.

Three separate defects in the `converge-graph-incrementally` work were this one
mistake wearing different clothes, and each produced *confident wrong numbers*
rather than an obvious failure:

1. Calling `epistemic_graph.upsert_after_write` directly. A standalone library
   caller joins its rebuild to completion by design, so the benchmark reported
   ~28 s writes. Through `writer_lease` the same write is ~210 ms.
2. Re-seeding freshness wholesale with `freshness.seed` between trials. Production
   rides incremental freshness events, which let `on_resolver_files_changed` patch
   the recall resolver in place; a wholesale re-seed makes `recall_delta_since`
   incomplete, so that patch *evicts* the resolver and every drain re-walks the
   vault. The drain looked linear in vault size. It is not: 2473 ms became 187 ms.
3. Giving the writer a private lease state dir, so it shared no mutation lock
   with the drainer and the two collided on `.graph.sqlite` with
   `database is locked` — indistinguishable from a product concurrency defect.

A wrong benchmark is worse than no benchmark: it is evidence, and it gets acted
on. This pins the three call-site rules structurally, the way
`test_every_graph_join_site_is_bounded_or_declared` pins the join sites.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
BENCHMARKS = (
    "scripts/graph_concurrent_convergence.py",
    "scripts/repro_graph_rebuild_hold.py",
)


def _module(rel: str) -> ast.Module:
    return ast.parse((ROOT / rel).read_text(encoding="utf-8"), filename=rel)


def _called_attributes(tree: ast.Module) -> set[str]:
    """Every `a.b(...)` attribute name called anywhere in the module."""
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def _keyword_values(tree: ast.Module, keyword: str) -> list[ast.expr]:
    return [
        word.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for word in node.keywords
        if word.arg == keyword
    ]


@pytest.mark.parametrize("rel", BENCHMARKS)
def test_a_graph_benchmark_commits_through_the_production_fanout(rel: str) -> None:
    """A measured write must run the post-commit fanout a real write runs.

    `post_commit_fanout=False` skips the resolver patch, leaving the recall
    resolver cold so every later drain re-walks the vault. A benchmark that
    writes only that way measures a cache state production never has.
    """
    tree = _module(rel)
    fanout_values = _keyword_values(tree, "post_commit_fanout")
    assert fanout_values, f"{rel} writes nothing through vault.batch_atomic_write"
    assert any(
        isinstance(value, ast.Constant) and value.value is True for value in fanout_values
    ), (
        f"{rel} never commits with post_commit_fanout=True, so its measured drains "
        "run against a resolver cache that a real write would have patched"
    )


@pytest.mark.parametrize("rel", BENCHMARKS)
def test_a_graph_benchmark_does_not_reseed_freshness_per_trial(rel: str) -> None:
    """Wholesale re-seeding evicts the recall resolver; events patch it.

    One `freshness.seed` pair at setup is how a vault starts. Calling it again
    inside the measured loop makes `recall_delta_since` incomplete, and the
    incremental resolver patch then evicts instead of patching.
    """
    tree = _module(rel)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.For, ast.While)):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "seed"
                and isinstance(inner.func.value, ast.Name)
                and inner.func.value.id in {"freshness", "freshness_module"}
            ):
                pytest.fail(
                    f"{rel} re-seeds freshness inside a loop; production rides "
                    "incremental events and this evicts the recall resolver, so "
                    "every measured drain re-walks the vault"
                )


def test_the_convergence_benchmark_writes_through_a_mutation_request() -> None:
    """Only a caller inside a mutation request takes the non-blocking policy.

    A direct `upsert_after_write` joins its rebuild by design, so a benchmark
    calling it measures the standalone contract rather than the write path.
    """
    rel = "scripts/graph_concurrent_convergence.py"
    tree = _module(rel)
    called = _called_attributes(tree)
    assert "invoke" in called, (
        f"{rel} must drive writes through a lease manager's invoke(), or it "
        "measures the standalone join rather than an interactive write"
    )
    assert "upsert_after_write" not in called, (
        f"{rel} calls the graph dispatch directly; a standalone caller joins its "
        "rebuild to completion, which is not what an interactive write does"
    )


def test_the_convergence_benchmark_shares_one_lease_state_dir() -> None:
    """The drainer resolves the default manager; the writer must be the same one.

    The drain runs outside any mutation request, so its graph index falls back to
    the default manager's coordinator. A writer built on a private state dir
    shares no mutation lock with it, and the two collide on `.graph.sqlite`.
    """
    rel = "scripts/graph_concurrent_convergence.py"
    tree = _module(rel)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "LeaseConfig"
            and any(word.arg == "state_dir" for word in node.keywords)
        ):
            pytest.fail(
                f"{rel} builds a lease manager on a private state_dir; the drainer "
                "resolves the default manager, so the two would share no mutation "
                "lock and collide on the graph sidecar"
            )
