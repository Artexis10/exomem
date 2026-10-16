"""Measure bounded graph reads and production write fanout on synthetic inputs.

Cold means a fresh Python process. The OS file cache is not cleared. Reports
retain raw samples; they do not enforce a latency or memory release threshold.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def memory() -> dict:
    from exomem.process_memory import enrich_process_memory

    current = None
    if sys.platform.startswith("linux"):
        try:
            current = int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf(
                "SC_PAGE_SIZE"
            )
        except (OSError, ValueError, IndexError):
            pass
    selected = (
        enrich_process_memory(os.getpid(), current / (1024 * 1024)) if current is not None else {}
    )
    result = {"rss_bytes": current, "selected": selected}
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # getrusage fixes these platform units; this does not classify content.
        scale = 1 if sys.platform == "darwin" else 1024
        return {
            **result,
            "peak_rss_bytes": peak * scale,
            "source": "getrusage process high-water since worker start",
        }
    except ImportError:
        return {**result, "peak_rss_bytes": None, "source": "peak unavailable on this platform"}


def elapsed(call):
    wall, cpu = time.perf_counter(), time.process_time()
    value = call()
    return value, {
        "wall_ms": (time.perf_counter() - wall) * 1000,
        "process_cpu_ms": (time.process_time() - cpu) * 1000,
    }


def worker(args) -> dict:
    sys.path.insert(0, str(args.repo / "scripts"))
    from graph_concurrent_convergence import _converge, _imports, _seed_live_freshness

    imports = _imports(args.repo)
    from exomem import commands, memory_refs, semantic_index, semantic_language_registry
    from exomem.runtime_resources import bootstrap

    bootstrap()
    root = args.root / "vault"
    root.mkdir(exist_ok=True)
    manifest_path = args.root / "inputs.json"
    index = imports["epistemic_graph"].EpistemicGraphIndex(root)
    if args.worker == "build":
        paths = imports["gen_dense_vault"](root, args.pages, links_per_note=3, ids=True)
        registry = semantic_language_registry.registry_path(root)
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(
            "schema_version: 1\ncategories:\n  measurement:\n"
            "    description: Synthetic measurement facts\nkinds: {}\n"
        )
        for i, rel in enumerate(paths):
            page = root / rel
            page.write_text(
                page.read_text() + "\n## Claim\n- category: measurement\n"
                "- id: fact\n- relations: supports: [["
                + paths[(i + 1) % len(paths)][:-3]
                + "#fact]]\n\n"
                f"Synthetic observation {i}.\n",
                encoding="utf-8",
            )
        corpus = hashlib.sha256()
        corpus.update(registry.read_bytes())
        for rel in paths:
            corpus.update(rel.encode())
            corpus.update((root / rel).read_bytes())
        state = semantic_index.current_parent_index_state(root, paths[0])
        unit = next(item for item in state.document.units if item.anchor == "fact")
        manifest = {"paths": paths, "corpus_sha256": corpus.hexdigest(), "unit_ref": unit.unit_ref}
        manifest_path.write_text(json.dumps(manifest))
        _seed_live_freshness(root, imports)
        result, timing = elapsed(index.rebuild_all)
        build_memory = memory()
        expected = {
            (rel, memory_refs.ref_from_markdown((root / rel).read_text()) + "#fact")
            for rel in paths
        }
        with sqlite3.connect(index.path.as_uri() + "?mode=ro", uri=True) as connection:
            # Candidates store neutral occurrences. The maintained reader, not
            # raw SQL columns, binds their selected public unit identities.
            view = imports["epistemic_graph"].GraphView(root, connection)
            actual = {
                (row["path"], row["metadata"]["unit_ref"])
                for _, ref in expected
                for row in view.indexed_units(ref)
            }
            unavailable = sorted(view.unavailable)
        fact_count = len(expected & actual) if not unavailable else None
        return {
            "build": timing,
            "published": index.available(),
            "counts": result,
            "indexed_fact_units": fact_count,
            "source_count_match": result.get("indexed_files") == len(paths)
            and not unavailable
            and expected == actual,
            "identity_unavailable": unavailable,
            "input_pages": len(paths),
            "corpus_sha256": manifest["corpus_sha256"],
            "memory": build_memory,
        }

    manifest = json.loads(manifest_path.read_text())
    page = root / manifest["paths"][0]
    samples = []
    for depth in (1, 1, 2, 3):
        packet, timing = elapsed(
            lambda depth=depth: commands.op_graph_context(
                root, unit_ref=manifest["unit_ref"], depth=depth, max_nodes=40, max_edges=80
            )
        )
        samples.append(
            {
                "phase": "process_cold" if not samples else "process_warm",
                "depth": depth,
                **timing,
                "available": packet.get("available"),
                "unit_status": packet.get("unit_status"),
                "nodes": len(packet["nodes"]) if packet.get("available") else None,
                "edges": len(packet["edges"]) if packet.get("available") else None,
                "truncation": packet.get("truncation"),
                "memory": memory(),
            }
        )
    # Match the maintained concurrency benchmark: one real mutation request,
    # canonical batch fanout and the default shared coordinator, never a direct
    # graph-dispatch call or a new freshness seed for each write.
    from types import SimpleNamespace

    from exomem.writer_lease import get_manager, mark_active_mutation_committed

    def leaf(vault_root, **_kwargs):
        vault = imports["vault"]
        vault.batch_atomic_write(
            [vault.PlannedWrite(page, page.read_text() + "\nUpdated observation.\n")],
            vault_root=vault_root,
            post_commit_fanout=True,
        )
        mark_active_mutation_committed()
        return {"status": "committed", "mutated": True}

    command = SimpleNamespace(name="remember", read_only=False, leaf=leaf)
    terminal, write_timing = elapsed(lambda: get_manager().invoke(command, (root,), {}))
    converged, convergence = elapsed(lambda: _converge(root, imports, deadline_seconds=30))
    result = {
        "queries": samples,
        "write": write_timing,
        "terminal": terminal,
        "queue_quiesced": converged,
        "convergence": convergence,
        "graph_state": imports["graph_sync"].status(root)["state"],
        "graph_available_after_update": index.available(),
        "graph_drift_count": len(imports["audit"].audit(root, categories=["graph_drift"]).findings),
        "index_bytes": index.path.stat().st_size,
        "wal_bytes": (
            index.path.with_name(index.path.name + "-wal").stat().st_size
            if index.path.with_name(index.path.name + "-wal").exists()
            else 0
        ),
        "memory": memory(),
    }
    imports["graph_sync"].drain_active_rebuilds()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--sizes", type=int, nargs="+", default=[32, 128, 512])
    parser.add_argument("--repeats", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", choices=("build", "query"))
    parser.add_argument("--root", type=Path)
    parser.add_argument("--pages", type=int)
    args = parser.parse_args()
    if any(size < 2 for size in args.sizes):
        parser.error("each size must contain at least two pages")
    if args.worker:
        print(json.dumps(worker(args)))
        return 0
    report = {
        "schema_version": 1,
        "source_revision": subprocess.check_output(
            ["git", "-C", str(args.repo), "rev-parse", "HEAD"], text=True
        ).strip(),
        "interpreter": sys.version,
        "source_dirty": bool(
            subprocess.check_output(
                ["git", "-C", str(args.repo), "status", "--porcelain"], text=True
            ).strip()
        ),
        "cold_definition": "first graph call in a fresh process after module imports; OS cache retained",
        "execution_scope": "local public graph command leaf; no remote transport",
        "limits": {"max_nodes": 40, "max_edges": 80},
        "runs": [],
    }
    for pages in args.sizes:
        for repeat in range(args.repeats):
            with tempfile.TemporaryDirectory(prefix="exomem-graph-resource-") as temp:
                root = Path(temp)
                env = {
                    "PATH": os.environ.get("PATH", ""),
                    "HOME": str(root / "home"),
                    "XDG_CACHE_HOME": str(root / "cache"),
                    "EXOMEM_STATE_ROOT": str(root / "state"),
                    "EXOMEM_DISABLE_MEDIA_EXTRACTION": "1",
                }
                row = {"input_pages": pages, "repeat": repeat + 1}
                for mode in ("build", "query"):
                    command = [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--repo",
                        str(args.repo),
                        "--worker",
                        mode,
                        "--root",
                        str(root),
                        "--pages",
                        str(pages),
                    ]
                    result = subprocess.run(
                        command, env=env, text=True, capture_output=True, timeout=180
                    )
                    if result.returncode:
                        raise RuntimeError(result.stderr)
                    row[mode] = json.loads(result.stdout)
                    # A failed input hop cannot support a performance measure.
                    if mode == "build" and not (
                        row[mode]["published"] and row[mode]["source_count_match"]
                    ):
                        row["status"] = "setup_failed"
                        break
                if "query" in row:
                    query = row["query"]
                    row["status"] = (
                        "complete"
                        if (
                            all(item["available"] for item in query["queries"])
                            and query["graph_state"] == "current"
                            and query["graph_drift_count"] == 0
                        )
                        else "partial"
                    )
                report["runs"].append(row)
                if args.output:
                    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
