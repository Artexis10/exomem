"""Excluded-tier enforcement on direct-read surfaces.

`_access.yaml`'s `excluded` tier is invisible to find/embedding AND
unwritable (access.py:1-9). This file proves the remaining direct-read
surfaces — `get_page`, `overview`, `query_data`, `video_frames`, and the
epistemic-graph lane — honor that tier the same way `review_context.py`
already does, and that refusal is indistinguishable from a genuinely
missing path (same code, same shape, same message text, no path echo
beyond what a missing-path error already carries).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exomem import (
    access,
    epistemic_graph,
    get_page,
    list_directory,
    memory_refs,
    query_data,
    semantic_units,
    video_frames,
)
from exomem import overview as overview_module


def _write_cfg(vault: Path, text: str) -> Path:
    p = vault / "Knowledge Base" / "_access.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _write(vault: Path, rel: str, body: str) -> Path:
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


# ---------------- 1.1: shared tier-resolution helper ----------------


@pytest.mark.parametrize(
    "probe",
    ["Knowledge Base/Private/secret.md", "Private/secret.md"],
)
def test_refuse_if_excluded_resolves_across_surface_path_forms(
    vault: Path, probe: str
) -> None:
    """Both the KB-prefixed form (get_page/query_data/video_frames/graph) and
    the KB-stripped form resolve to the same excluded verdict."""
    _write_cfg(vault, "excluded:\n  - Private\n")
    assert access.refuse_if_excluded(vault, probe) is True
    assert access.refuse_if_excluded(vault, "Knowledge Base/Notes/x.md") is False


# ---------------- 2: get_page / read_memory ----------------


def test_get_page_refuses_excluded_path(vault: Path) -> None:
    excluded_rel = "Knowledge Base/Private/secret.md"
    _write(
        vault,
        excluded_rel,
        "---\ntype: note\n---\nsecret body\n",
    )
    _write_cfg(vault, "excluded:\n  - Private\n")

    with pytest.raises(get_page.GetError) as excluded_exc:
        get_page.get_page(vault, path=excluded_rel)

    missing_rel = "Knowledge Base/Private/does-not-exist.md"
    with pytest.raises(get_page.GetError) as missing_exc:
        get_page.get_page(vault, path=missing_rel)

    # byte-identical shape: same code, same message template, and the
    # excluded reason never mentions exclusion/privacy/permission — it is
    # the exact "file does not exist" text a genuine miss produces.
    assert excluded_exc.value.code == missing_exc.value.code == "NOT_FOUND"
    assert excluded_exc.value.reason == f"file does not exist: {excluded_rel}"
    assert missing_exc.value.reason == f"file does not exist: {missing_rel}"
    assert excluded_exc.value.as_dict() == {
        "code": "NOT_FOUND",
        "reason": f"file does not exist: {excluded_rel}",
    }


# ---------------- 2b: list_directory / browse_memory(mode="list") ----------------


def test_list_directory_refuses_excluded_dir_byte_identical_to_missing(vault: Path) -> None:
    excluded_rel = "Knowledge Base/Private"
    _write(vault, f"{excluded_rel}/secret.md", "---\ntype: note\n---\nsecret body\n")
    _write_cfg(vault, "excluded:\n  - Private\n")

    with pytest.raises(list_directory.ListDirectoryError) as excluded_exc:
        list_directory.list_directory(vault, path=excluded_rel)

    missing_rel = "Knowledge Base/DoesNotExist"
    with pytest.raises(list_directory.ListDirectoryError) as missing_exc:
        list_directory.list_directory(vault, path=missing_rel)

    assert excluded_exc.value.code == missing_exc.value.code == "NOT_FOUND"
    assert excluded_exc.value.reason == f"path does not exist: {excluded_rel}"
    assert missing_exc.value.reason == f"path does not exist: {missing_rel}"
    assert excluded_exc.value.as_dict() == {
        "code": "NOT_FOUND",
        "reason": f"path does not exist: {excluded_rel}",
    }


def test_list_directory_refuses_excluded_file_never_not_a_dir(vault: Path) -> None:
    """An excluded FILE probed as `path` must read as missing (NOT_FOUND), never
    as NOT_A_DIR — the latter would leak that a non-directory exists there."""
    excluded_rel = "Knowledge Base/Private/secret.md"
    _write(vault, excluded_rel, "---\ntype: note\n---\nsecret body\n")
    _write_cfg(vault, "excluded:\n  - Private\n")

    with pytest.raises(list_directory.ListDirectoryError) as exc:
        list_directory.list_directory(vault, path=excluded_rel)

    assert exc.value.code == "NOT_FOUND"
    assert exc.value.reason == f"path does not exist: {excluded_rel}"


def test_list_directory_recursive_prunes_excluded_subtree(vault: Path) -> None:
    excluded_rel = "Knowledge Base/Private"
    _write(vault, f"{excluded_rel}/secret.md", "---\ntype: note\n---\nsecret body\n")
    _write(vault, f"{excluded_rel}/Nested/deep-secret.md", "---\ntype: note\n---\ndeep\n")
    _write(vault, "Knowledge Base/Notes/visible.md", "---\ntype: note\n---\nvisible\n")
    _write_cfg(vault, "excluded:\n  - Private\n")

    result = list_directory.list_directory(vault, path="", recursive=True)

    assert not any(e.path.startswith(excluded_rel) for e in result.entries)
    assert any(e.path == "Knowledge Base/Notes/visible.md" for e in result.entries)


# ---------------- 3: overview / browse_memory ----------------


def test_overview_hides_excluded_tree(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _write(root, "Knowledge Base/Private/secret.md", "secret\n")
    _write(root, "Knowledge Base/Notes/visible.md", "visible\n")
    cfg = _write_cfg(root, "readonly: []\nexcluded: []\n")

    baseline = overview_module.overview(root)
    # secret.md + visible.md + _access.yaml itself
    assert baseline["totals"]["files"] == 3
    assert any(e["path"] == "Knowledge Base/Private" for e in baseline["tree"])

    cfg.write_text("readonly: []\nexcluded:\n  - Private\n", encoding="utf-8")
    report = overview_module.overview(root)

    # secret.md is gone from every count/coverage figure; _access.yaml + visible.md remain
    assert report["totals"]["files"] == 2
    assert not any(e["path"].startswith("Knowledge Base/Private") for e in report["tree"])
    assert not any("secret" in n for e in report["tree"] for n in e["sample_names"])
    # no "hidden N" marker of any kind anywhere in the report
    assert "hidden" not in json.dumps(report).lower()


def test_overview_scoped_excluded_dir_byte_identical_to_missing(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _write(root, "Knowledge Base/Private/secret.md", "secret\n")
    _write_cfg(root, "excluded:\n  - Private\n")

    with pytest.raises(overview_module.OverviewError) as excluded_exc:
        overview_module.overview(root, path="Knowledge Base/Private")

    with pytest.raises(overview_module.OverviewError) as missing_exc:
        overview_module.overview(root, path="Knowledge Base/NoSuchFolder")

    assert excluded_exc.value.code == missing_exc.value.code == "NOT_FOUND"
    assert excluded_exc.value.reason == "no such vault path: Knowledge Base/Private"
    assert missing_exc.value.reason == "no such vault path: Knowledge Base/NoSuchFolder"


def test_overview_scoped_excluded_file_never_not_a_dir(tmp_path: Path) -> None:
    """An excluded existing FILE as `path` must read as missing, never NOT_A_DIR
    — the latter would leak that a non-directory exists there."""
    root = tmp_path / "vault"
    excluded_rel = "Knowledge Base/Private/secret.md"
    _write(root, excluded_rel, "secret\n")
    _write_cfg(root, "excluded:\n  - Private\n")

    with pytest.raises(overview_module.OverviewError) as exc:
        overview_module.overview(root, path=excluded_rel)

    assert exc.value.code == "NOT_FOUND"
    assert exc.value.reason == f"no such vault path: {excluded_rel}"


def test_overview_scoped_excluded_via_browse_memory_command(vault: Path) -> None:
    from exomem import commands

    excluded_rel = "Knowledge Base/Private/secret.md"
    _write(vault, excluded_rel, "---\ntype: note\n---\nsecret\n")
    _write_cfg(vault, "excluded:\n  - Private\n")

    with pytest.raises(ValueError, match="NOT_FOUND"):
        commands.op_browse_memory(vault, mode="overview", path="Knowledge Base/Private")
    with pytest.raises(ValueError, match="NOT_FOUND"):
        commands.op_browse_memory(vault, mode="overview", path=excluded_rel)


# ---------------- 4: query_dataset / read_media ----------------


def test_query_dataset_refuses_excluded(vault: Path) -> None:
    rel = "Knowledge Base/Evidence/Private/labs.csv"
    _write(vault, rel, "a,b\n1,2\n")
    _write_cfg(vault, "excluded:\n  - Evidence/Private\n")

    with pytest.raises(query_data.QueryDataError) as exc:
        query_data.query_data(vault, path=rel)
    assert exc.value.code == "NOT_FOUND"
    assert exc.value.reason == f"path does not exist: {rel}"

    missing_rel = "Knowledge Base/Evidence/Private/does-not-exist.csv"
    with pytest.raises(query_data.QueryDataError) as missing_exc:
        query_data.query_data(vault, path=missing_rel)
    assert missing_exc.value.code == "NOT_FOUND"
    assert missing_exc.value.reason == f"path does not exist: {missing_rel}"


def test_read_media_refuses_excluded(vault: Path) -> None:
    rel = "Knowledge Base/Sources/Private/clip.mp4"
    (vault / rel).parent.mkdir(parents=True, exist_ok=True)
    (vault / rel).write_bytes(b"\x00fake-mp4")
    _write_cfg(vault, "excluded:\n  - Sources/Private\n")

    with pytest.raises(video_frames.VideoFramesError) as exc:
        video_frames.get_frames(vault, rel)
    assert exc.value.code == "NOT_FOUND"
    assert exc.value.reason == f"path does not exist: {rel}"


# ---------------- 5: graph lane ----------------


def test_graph_context_never_seeds_or_returns_excluded(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    excluded_rel = "Knowledge Base/Private/secret.md"
    linker_rel = "Knowledge Base/Notes/Insights/linker.md"
    _write(
        vault,
        excluded_rel,
        "---\ntype: insight\nstatus: active\n---\n# Secret\n\n"
        "zzqqxx-graph-marker unique body.\n",
    )
    _write(
        vault,
        linker_rel,
        "---\ntype: insight\nstatus: active\n---\n# Linker\n\n"
        "See [[Knowledge Base/Private/secret]].\n",
    )
    _write_cfg(vault, "excluded:\n  - Private\n")
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()

    # seed-by-path: an excluded path never seeds a graph context.
    by_path = epistemic_graph.graph_context(vault, path=excluded_rel, depth=1)
    assert by_path["seeds"] == []
    assert by_path["nodes"] == []

    # seed-by-query: text unique to the excluded page never seeds one either.
    by_query = epistemic_graph.graph_context(
        vault, query="zzqqxx-graph-marker", depth=1
    )
    assert by_query["seeds"] == []

    # neighbour: the linker's own context never surfaces the excluded page as
    # a node, nor as either endpoint of a returned edge.
    neighbour = epistemic_graph.graph_context(vault, path=linker_rel, depth=1)
    assert neighbour["available"] is True
    assert not any(n["path"] == excluded_rel for n in neighbour["nodes"])
    excluded_key = epistemic_graph._file_key(excluded_rel)
    assert not any(
        e["src_key"] == excluded_key or e["dst_key"] == excluded_key
        for e in neighbour["edges"]
    )


def test_graph_context_unit_ref_excluded_matches_dead_unit_status(tmp_path: Path) -> None:
    """An excluded-but-current unit must be indistinguishable from a
    genuinely-gone-but-indexed unit: both resolve to `unit_status: "stale"`
    with empty seeds/nodes/edges. Before the fix, the excluded unit resolved
    `unit_status: "found"` with empty seeds — "found" alone leaked that the
    page still exists, since a truly dead unit reports "stale"."""
    vault = tmp_path / "vault"
    excluded_rel = "Knowledge Base/Notes/Excluded/unit-a.md"
    deleted_rel = "Knowledge Base/Notes/Insights/unit-b.md"
    id_excluded = "00000000-0000-4000-8000-0000000000b1"
    id_deleted = "00000000-0000-4000-8000-0000000000b2"

    def _unit_body(exomem_id: str) -> str:
        return (
            "---\n"
            "type: insight\n"
            "status: active\n"
            f"exomem_id: {exomem_id}\n"
            "---\n"
            "# Title\n\n"
            "## Claim\n"
            "- id: claim-1\n\n"
            "The rich claim body.\n"
        )

    _write(vault, excluded_rel, _unit_body(id_excluded))
    _write(vault, deleted_rel, _unit_body(id_deleted))
    idx = epistemic_graph.EpistemicGraphIndex(vault)
    idx.rebuild_all()

    def _unit_ref_for(rel: str, exomem_id: str) -> str:
        path = vault / rel
        page = epistemic_graph.find_module._parse_page(path, path.stat().st_mtime, vault)
        document = semantic_units.parse_semantic_units(
            page.body,
            path=page.rel_path,
            parent_ref=memory_refs.memory_ref(exomem_id),
            validate=False,
            language_registry=idx.language_registry,
            relation_registry=idx.registry,
            include_legacy_relations=True,
            retain_unknown_relations=True,
            page_type=page.page_type,
        )
        return document.units[0].unit_ref

    unit_ref_excluded = _unit_ref_for(excluded_rel, id_excluded)
    unit_ref_deleted = _unit_ref_for(deleted_rel, id_deleted)

    # Probe 1: the page stays on disk and current, but its folder is now
    # excluded (_access.yaml) — index NOT rebuilt.
    _write_cfg(vault, "excluded:\n  - Notes/Excluded\n")
    # Probe 2: a genuinely dead unit — the page file itself is deleted,
    # index NOT rebuilt (a truly-gone-but-indexed unit).
    (vault / deleted_rel).unlink()

    excluded_ctx = epistemic_graph.graph_context(vault, unit_ref=unit_ref_excluded, depth=1)
    deleted_ctx = epistemic_graph.graph_context(vault, unit_ref=unit_ref_deleted, depth=1)

    # unit_status and seeds/nodes/edges are the caller-visible existence
    # signal and must match exactly.
    assert excluded_ctx["unit_status"] == deleted_ctx["unit_status"] == "stale"
    assert excluded_ctx["seeds"] == deleted_ctx["seeds"] == []
    assert excluded_ctx["nodes"] == deleted_ctx["nodes"] == []
    assert excluded_ctx["edges"] == deleted_ctx["edges"] == []
    # Residual (reported per task instructions, not restructured): the two
    # cases reach "stale" via different internal drift reasons — excluded via
    # "missing_graph_row" (seed dropped pre-filter, so `indexed` looks empty),
    # deleted via "missing_parent" (file read fails). Both surface under the
    # same warning code, just a different `reasons` key.
    assert excluded_ctx["warnings"][0]["code"] == deleted_ctx["warnings"][0]["code"]


# ---------------- 6: command-layer sweep ----------------


def test_command_layer_sweep_refuses_excluded_path(vault: Path) -> None:
    """read_memory, query_dataset, and read_media refuse an excluded path
    identically to a missing one; browse_memory (overview) hides the excluded
    subtree; the graph_context lane behind connect_memory(graph-context)
    never seeds from it — each matching that surface's own contract for
    absence (raise vs. hide vs. empty-seed), never leaking existence."""
    from exomem import commands

    note_rel = "Knowledge Base/Private/secret.md"
    csv_rel = "Knowledge Base/Private/data.csv"
    video_rel = "Knowledge Base/Private/clip.mp4"
    _write(
        vault,
        note_rel,
        "---\ntype: insight\nstatus: active\n---\n# Secret\n\nbody.\n",
    )
    _write(vault, csv_rel, "a,b\n1,2\n")
    (vault / video_rel).write_bytes(b"\x00fake-mp4")
    _write_cfg(vault, "excluded:\n  - Private\n")

    with pytest.raises(ValueError, match="NOT_FOUND"):
        commands.op_read_memory(vault, path=note_rel)
    with pytest.raises(ValueError, match="NOT_FOUND"):
        commands.op_query_dataset(vault, path=csv_rel)
    with pytest.raises(ValueError, match="NOT_FOUND"):
        commands.op_read_media(vault, path=video_rel)

    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    context = commands.op_graph_context(vault, path=note_rel, depth=1)
    assert context["seeds"] == []
    assert context["nodes"] == []

    overview_report = commands.op_browse_memory(vault, mode="overview")
    assert not any(
        e["path"].startswith("Knowledge Base/Private")
        for e in overview_report["tree"]
    )

    with pytest.raises(ValueError, match="NOT_FOUND"):
        commands.op_browse_memory(vault, mode="list", path="Knowledge Base/Private")

    list_report = commands.op_browse_memory(vault, mode="list", recursive=True)
    assert not any(
        e["path"].startswith("Knowledge Base/Private") for e in list_report["entries"]
    )
