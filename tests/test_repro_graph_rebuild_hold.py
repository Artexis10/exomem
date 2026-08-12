"""Unit coverage for the graph rebuild publication-hold reproduction harness."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "repro_graph_rebuild_hold.py"


def load_module():
    spec = importlib.util.spec_from_file_location("repro_graph_rebuild_hold_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_summary_reports_request_and_canonical_publication_hold_percentiles() -> None:
    summary = load_module().summarize([10.0, 30.0, 20.0])

    assert summary == {"median_ms": 20.0, "p95_ms": 30.0}


def test_format_result_identifies_the_publication_operation_and_scale_ratio() -> None:
    output = load_module().format_result(
        {
            "pages": 2_000,
            "trials": 5,
            "request": {"median_ms": 32_000.0, "p95_ms": 33_000.0},
            "publication_hold": {"median_ms": 12.0, "p95_ms": 15.0},
        },
        hold_ratio=1.25,
    )

    assert "FINAL canonical publication hold" in output
    assert "operation=epistemic_graph_publish_rebuild" in output
    assert "2000/500 hold median ratio=1.25x" in output


def test_publication_hold_rejects_timing_from_any_other_operation() -> None:
    module = load_module()

    assert module.publication_hold_ms(
        {"operation": "epistemic_graph_publish_rebuild", "hold_ms": 12.5}
    ) == 12.5
    with pytest.raises(RuntimeError, match="epistemic_graph_publish_rebuild"):
        module.publication_hold_ms({"operation": "epistemic_graph_refresh_paths", "hold_ms": 12.5})


def test_advance_checkpoint_uses_a_real_write_then_reseeds_live_freshness(
    tmp_path: Path,
) -> None:
    module = load_module()
    target = tmp_path / "Knowledge Base" / "Notes" / "target.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Target\n", encoding="utf-8")
    batches = []
    seeded = []

    class FakeVault:
        class PlannedWrite:
            def __init__(self, path, source):
                self.path = path
                self.source = source

        @staticmethod
        def batch_atomic_write(writes, **kwargs):
            batches.append((list(writes), kwargs))

    class FakeFreshness:
        @staticmethod
        def stat_signature(path):
            return str(path)

        @staticmethod
        def seed(vault_root, scope, entries):
            seeded.append((vault_root, scope, list(entries)))

    imports = {
        "find": SimpleNamespace(_walk_md=lambda _path: []),
        "freshness": FakeFreshness,
        "kb_dirname": lambda: "Knowledge Base",
        "vault": FakeVault,
        "walk_vault_md": lambda _root: [target],
    }

    module.advance_checkpoint(tmp_path, target, imports)

    writes, kwargs = batches[0]
    assert [(write.path, write.source) for write in writes] == [(target, "# Target\n\n")]
    assert kwargs == {"vault_root": tmp_path, "post_commit_fanout": False}
    assert [scope for _root, scope, _entries in seeded] == ["vault", "kb"]
