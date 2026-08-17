"""Measure full rebuild request latency apart from its final publication hold.

Each trial starts with a production-shaped, fresh graph checkpoint and deletes
only the derived graph sidecar.  The durable epoch remains intact, forcing the
normal missing-sidecar full rebuild while leaving the final canonical
publication boundary as the only measured lock hold.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

DEFAULT_SIZES = (500, 2_000)
DEFAULT_TRIALS = 5
PUBLICATION_OPERATION = "epistemic_graph_publish_rebuild"


def _p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, max(0, int(len(ordered) * 0.95 + 0.999) - 1))]


def summarize(samples: list[float]) -> dict[str, float]:
    if not samples:
        raise ValueError("cannot summarize an empty benchmark sample")
    return {
        "median_ms": round(statistics.median(samples), 1),
        "p95_ms": round(_p95(samples), 1),
    }


def publication_hold_ms(timing: dict[str, float | str] | None) -> float:
    if timing is None or timing.get("operation") != PUBLICATION_OPERATION:
        operation = None if timing is None else timing.get("operation")
        raise RuntimeError(
            f"expected {PUBLICATION_OPERATION} timing, got operation={operation!r}"
        )
    hold_ms = timing.get("hold_ms")
    if not isinstance(hold_ms, float):
        raise RuntimeError(f"expected numeric {PUBLICATION_OPERATION} hold_ms")
    return hold_ms


def _imports(repo_root: Path) -> dict[str, Any]:
    for name in (
        "EXOMEM_DISABLE_EMBEDDINGS",
        "EXOMEM_DISABLE_CLIP",
        "EXOMEM_DISABLE_MEDIA_EXTRACTION",
        "EXOMEM_DISABLE_RANKING",
    ):
        os.environ[name] = "1"
    sys.path.insert(0, str(repo_root / "src"))
    sys.path.insert(0, str(repo_root / "scripts"))
    from synth_vault import gen_dense_vault

    from exomem import (
        deferred_index,
        epistemic_graph,
        find,
        freshness,
        graph_sync,
        index_sync,
        mutation_lock,
        vault,
    )
    from exomem.kbdir import kb_dirname
    from exomem.vault import walk_vault_md

    return {
        "deferred_index": deferred_index,
        "index_sync": index_sync,
        "epistemic_graph": epistemic_graph,
        "find": find,
        "freshness": freshness,
        "gen_dense_vault": gen_dense_vault,
        "graph_sync": graph_sync,
        "kb_dirname": kb_dirname,
        "mutation_lock": mutation_lock,
        "vault": vault,
        "walk_vault_md": walk_vault_md,
    }


def _seed_live_freshness(vault_root: Path, imports: dict[str, Any]) -> None:
    freshness = imports["freshness"]
    find = imports["find"]
    walk_vault_md = imports["walk_vault_md"]
    kb_dirname = imports["kb_dirname"]

    freshness.seed(
        vault_root,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in walk_vault_md(vault_root)),
    )
    freshness.seed(
        vault_root,
        "kb",
        (
            (str(path), freshness.stat_signature(path))
            for path in find._walk_md(vault_root / kb_dirname())
        ),
    )


def advance_checkpoint(vault_root: Path, target: Path, imports: dict[str, Any]) -> None:
    """Advance the real graph epoch and restore the caller's live freshness proof."""
    vault = imports["vault"]
    vault.batch_atomic_write(
        [vault.PlannedWrite(target, target.read_text(encoding="utf-8") + "\n")],
        vault_root=vault_root,
        post_commit_fanout=False,
    )

    _seed_live_freshness(vault_root, imports)


def _seed_live_checkpoint(vault_root: Path, imports: dict[str, Any]) -> Path:
    graph_sync = imports["graph_sync"]
    kb_dirname = imports["kb_dirname"]
    target = next((vault_root / kb_dirname()).rglob("*.md"))
    advance_checkpoint(vault_root, target, imports)
    checkpoint = graph_sync.read_checkpoint(vault_root)
    if checkpoint is None:
        raise RuntimeError("production-shaped setup did not create a graph checkpoint")
    return target


def measure(vault_root: Path, size: int, trials: int, imports: dict[str, Any]) -> dict[str, Any]:
    if trials < 1:
        raise ValueError("trials must be positive")
    imports["gen_dense_vault"](vault_root, size, links_per_note=3)
    target = _seed_live_checkpoint(vault_root, imports)
    epistemic_graph = imports["epistemic_graph"]
    mutation_lock = imports["mutation_lock"]
    graph = epistemic_graph.EpistemicGraphIndex(vault_root)
    graph.rebuild_all()
    if not graph.available():
        raise RuntimeError("production-shaped setup did not publish a current graph sidecar")

    request_samples: list[float] = []
    hold_samples: list[float] = []
    sidecar = epistemic_graph.sidecar_path(vault_root)
    for _ in range(trials):
        advance_checkpoint(vault_root, target, imports)
        # The previous trial's rebuild is a daemon thread, and on Windows a
        # reader still holding the sidecar refuses the unlink outright. Join it
        # before removing the file it is reading; the measurement below starts
        # after this and is unaffected by it.
        imports["graph_sync"].drain_active_rebuilds()
        sidecar.unlink(missing_ok=True)
        if sidecar.exists():
            raise RuntimeError("could not remove derived graph sidecar")
        started = time.perf_counter()
        result = epistemic_graph.upsert_after_write(vault_root, [target])
        request_samples.append((time.perf_counter() - started) * 1_000.0)
        if result.code != "graph_rebuild_completed":
            raise RuntimeError(f"missing-sidecar trial did not run a full rebuild: {result.code}")
        hold_samples.append(publication_hold_ms(mutation_lock.last_mutation_timing()))
        if not sidecar.exists() or not graph.available():
            raise RuntimeError("full rebuild did not republish a current graph sidecar")

    drain_samples = _measure_drain(vault_root, target, trials, imports)

    return {
        "pages": size,
        "trials": trials,
        "request": summarize(request_samples),
        "publication_hold": summarize(hold_samples),
        "drain": summarize(drain_samples),
    }


def _measure_drain(
    vault_root: Path, target: Path, trials: int, imports: dict[str, Any]
) -> list[float]:
    """Time repairing one changed page through the durable queue.

    The number this whole change is for. `request` above is what an ordinary
    write used to cost whenever the incremental path bailed out: a whole-vault
    rebuild. This is what the same repair costs once it is proportional to the
    change -- same vault size, same box, same run. Reporting them together is
    the point; a ratio taken across two runs on two machines would prove
    nothing.

    The sidecar is deliberately *not* deleted here. A missing sidecar is one of
    the cases that still earns a full rebuild, so deleting it would measure the
    rebuild again under a different name.
    """
    deferred_index = imports["deferred_index"]
    index_sync = imports["index_sync"]
    epistemic_graph = imports["epistemic_graph"]

    graph = epistemic_graph.EpistemicGraphIndex(vault_root)
    if not graph.available():
        graph.rebuild_all()

    samples: list[float] = []
    rel = target.resolve().relative_to(vault_root.resolve()).as_posix()
    for _ in range(trials):
        advance_checkpoint(vault_root, target, imports)
        deferred_index.add_graph(vault_root, [rel])
        started = time.perf_counter()
        index_sync.drain_deferred_work(vault_root)
        samples.append((time.perf_counter() - started) * 1_000.0)
        if deferred_index.list_graph_paths(vault_root):
            raise RuntimeError("drain left queued graph work behind")
    return samples


def format_result(result: dict[str, Any], *, hold_ratio: float | None = None) -> str:
    request = result["request"]
    hold = result["publication_hold"]
    drain = result["drain"]
    line = (
        f"pages={result['pages']:<6} trials={result['trials']} "
        f"request median/p95={request['median_ms']:.1f}/{request['p95_ms']:.1f}ms  "
        f"drain median/p95={drain['median_ms']:.1f}/{drain['p95_ms']:.1f}ms "
        f"({request['median_ms'] / max(drain['median_ms'], 0.001):.1f}x cheaper)  "
        f"FINAL canonical publication hold median/p95="
        f"{hold['median_ms']:.1f}/{hold['p95_ms']:.1f}ms "
        f"operation={PUBLICATION_OPERATION}"
    )
    if hold_ratio is not None:
        line += f"  2000/500 hold median ratio={hold_ratio:.2f}x"
    return line


@contextlib.contextmanager
def _quiesced(imports: dict[str, Any]):
    """Let no graph rebuild outlive the vault it was building against.

    A rebuild runs on a daemon thread. One still holding `.graph.sqlite` when
    the temporary root is removed fails cleanup with WinError 32 -- raised
    *after* every measurement is taken, so a working benchmark is reported as a
    crashed one and the numbers it printed scroll past unread. Nested inside the
    temporary directory so this unwinds first, which is the whole point.
    """
    try:
        yield
    finally:
        if not imports["graph_sync"].drain_active_rebuilds():
            print(
                "repro-graph-rebuild-hold: a graph rebuild did not finish before "
                "teardown; the measurements stand, cleanup may not",
                flush=True,
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo_root", type=Path)
    parser.add_argument("sizes", nargs="*", type=int, default=list(DEFAULT_SIZES))
    parser.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    args = parser.parse_args(argv)
    if args.trials < 1:
        parser.error("--trials must be positive")

    imports = _imports(args.repo_root.resolve())
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="graph-rebuild-hold-") as temp, _quiesced(imports):
        root = Path(temp)
        for size in args.sizes:
            vault_root = root / f"vault-{size}"
            vault_root.mkdir()
            results.append(measure(vault_root, size, args.trials, imports))

    ratio = None
    by_size = {result["pages"]: result for result in results}
    if 500 in by_size and 2_000 in by_size:
        ratio = (
            by_size[2_000]["publication_hold"]["median_ms"]
            / max(by_size[500]["publication_hold"]["median_ms"], 0.001)
        )
    for result in results:
        print(format_result(result, hold_ratio=ratio if result["pages"] == 2_000 else None), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
