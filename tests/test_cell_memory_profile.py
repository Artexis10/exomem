"""The cell memory profiler (bound-cell-memory D1): its derived figures, its
content-free guarantee, and a smoke run of the real cloud cell on a tiny vault.

Every later memory change is judged against this harness, so the arithmetic that
turns raw readings into "model" and "unreturned allocator" figures is pinned
here, and so is the rule that no vault string ever reaches the report.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "cell_memory_profile.py"
MIB = 1024 * 1024


def _load():
    spec = importlib.util.spec_from_file_location("cell_memory_profile_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


profile = _load()


def _point(name: str, *, rss: int, anon: int, traced: int, overhead: int, model: bool) -> dict:
    return {
        "name": name,
        "tracemalloc": {"current_bytes": traced * MIB, "overhead_bytes": overhead * MIB},
        "smaps_rollup": {"rss_bytes": rss * MIB, "anonymous_bytes": anon * MIB},
        "counters": {"models": {"embeddings": model}},
    }


def _cell_points() -> list[dict]:
    return [
        _point("import", rss=360, anon=300, traced=100, overhead=20, model=False),
        _point("model_load", rss=960, anon=900, traced=102, overhead=20, model=True),
        _point("first_hybrid_find", rss=1060, anon=1000, traced=130, overhead=22, model=True),
        _point("reaper_tick", rss=1060, anon=1000, traced=128, overhead=22, model=False),
    ]


def _derived(points: list[dict], name: str) -> dict:
    point = next(p for p in points if p["name"] == name)
    return {k: (v // MIB if isinstance(v, int) else v) for k, v in point["derived"].items()}


def test_smaps_rollup_yields_the_four_d1_fields_in_bytes() -> None:
    text = (
        "00400000-7ffd15dfd000 ---p 00000000 00:00 0   [rollup]\n"
        "Rss:               11640 kB\nPss:                5769 kB\n"
        "Pss_Dirty:          4672 kB\nPrivate_Dirty:      4672 kB\n"
        "Anonymous:          4000 kB\nSwap:                  0 kB\n"
    )
    assert profile.parse_smaps_rollup(text) == {
        "rss_bytes": 11640 * 1024,
        "pss_bytes": 5769 * 1024,
        "private_dirty_bytes": 4672 * 1024,
        "anonymous_bytes": 4000 * 1024,
    }


def test_the_model_load_growth_is_the_model_while_it_is_resident() -> None:
    points = _cell_points()
    profile.derive(points)

    # import: 300 anon - 100 traced - 20 tracemalloc overhead.
    assert _derived(points, "import") == {
        "untraced_anon_bytes": 180,
        "model_native_bytes": 0,
        "unreturned_allocator_bytes": 180,
        "file_backed_bytes": 60,
    }
    # model load: untraced 778, so the model is the 598 it added.
    assert _derived(points, "model_load")["model_native_bytes"] == 598
    assert _derived(points, "model_load")["unreturned_allocator_bytes"] == 180
    # a find: untraced 848, of which 598 is still the model.
    assert _derived(points, "first_hybrid_find")["unreturned_allocator_bytes"] == 250
    # after a reap the model is gone, so what it left behind is unreturned.
    reaped = _derived(points, "reaper_tick")
    assert reaped["model_native_bytes"] == 0
    assert reaped["unreturned_allocator_bytes"] == 850


def test_a_stub_encoder_is_never_charged_a_native_model() -> None:
    points = _cell_points()
    profile.derive(points, native_model=False)

    assert _derived(points, "model_load")["model_native_bytes"] == 0
    assert _derived(points, "model_load")["unreturned_allocator_bytes"] == 778


def test_derived_figures_are_unknown_without_smaps() -> None:
    points = _cell_points()
    for point in points:
        point["smaps_rollup"] = None
    profile.derive(points)

    assert set(_derived(points, "model_load").values()) == {None}


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("/venv/lib/python3.13/site-packages/numpy/linalg.py", "numpy/linalg.py:7"),
        ("/usr/lib/python3.13/json/decoder.py", "json/decoder.py:7"),
        ("/checkout/src/exomem/find.py", "exomem/find.py:7"),
        ("/checkout/scripts/cell_memory_profile.py", "cell_memory_profile.py:7"),
        ("<frozen importlib._bootstrap>", "<frozen importlib._bootstrap>:7"),
        (r"venv\Lib\site-packages\numpy\linalg.py", "numpy/linalg.py:7"),
    ],
)
def test_allocation_sites_are_package_relative(filename: str, expected: str) -> None:
    assert profile.code_site(filename, 7) == expected


def test_cache_counters_drop_the_per_vault_breakdown() -> None:
    caches = {
        "vector_matrices": {
            "embedding": {"rows": 3, "by_vault": {"/data/vault": {"rows": 3}}},
        },
        "find": {"pages": {"entries": 2}},
    }

    assert profile._without_vault_keys(caches) == {
        "vector_matrices": {"embedding": {"rows": 3}},
        "find": {"pages": {"entries": 2}},
    }


def test_a_vault_string_in_a_key_or_a_value_is_a_leak() -> None:
    report = {
        "cell": {"points": [{"top": [{"site": "exomem/find.py:10"}]}]},
        "by_path": {"/scratch/vault/Knowledge Base": 1},
        "facts": ["Note 3 about topic note-00003-topic-1"],
    }
    forbidden = {"/scratch/vault", "note-00003-topic-1", "note-00004-topic-9", "abc"}

    assert profile.content_leaks(report, forbidden) == ["/scratch/vault", "note-00003-topic-1"]


def test_the_scan_covers_note_stems_titles_roots_and_probe_text(tmp_path: Path) -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        from synth_vault import gen_dense_vault
    finally:
        sys.path.remove(str(ROOT / "scripts"))
    vault = tmp_path / "vault"
    rels = gen_dense_vault(vault, 3, links_per_note=2)

    strings = profile.vault_strings(vault, tmp_path, rels)

    for rel in rels:
        assert Path(rel).stem in strings
    assert any(s.startswith("Note 0 about topic ") for s in strings)
    assert {str(vault), str(tmp_path), profile.QUERY, profile.WRITE_TITLE} <= strings


def _run_harness(tmp_path: Path, *args: str) -> tuple[subprocess.CompletedProcess, dict]:
    out = tmp_path / "report.json"
    scratch_parent = tmp_path / "scratch"
    scratch_parent.mkdir()
    env = {**os.environ, "TMPDIR": str(scratch_parent)}
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *args, "--out", str(out), "--settle-seconds", "30",
         "--timeout", "300"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-4000:]
    # The scratch root is the harness's own and is gone after the run.
    assert list(scratch_parent.iterdir()) == []
    return proc, json.loads(out.read_text(encoding="utf-8"))


def _assert_content_free(report: dict, tmp_path: Path, notes: int, links: int) -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        from synth_vault import gen_dense_vault
    finally:
        sys.path.remove(str(ROOT / "scripts"))
    # The generator is deterministic: the same call names the same notes.
    twin = tmp_path / "twin"
    rels = gen_dense_vault(twin, notes, links_per_note=links)
    forbidden = profile.vault_strings(twin, tmp_path, rels) - {str(twin)}
    assert profile.content_leaks(report, forbidden) == []
    assert str(tmp_path) not in json.dumps(report)


@pytest.mark.timeout(600)
def test_smoke_run_profiles_a_real_cloud_cell_and_stays_content_free(tmp_path: Path) -> None:
    proc, report = _run_harness(
        tmp_path, "--notes", "12", "--links-per-note", "3", "--encoder", "stub", "--top", "5"
    )

    assert report["ok"] is True
    assert report["vault"]["notes"] == 12
    assert [p["name"] for p in report["build"]["points"]] == list(profile.BUILD_POINTS)
    assert [p["name"] for p in report["cell"]["points"]] == list(profile.CELL_POINTS)
    for point in report["build"]["points"] + report["cell"]["points"]:
        assert point["smaps_rollup"]["rss_bytes"] > 0
        assert point["tracemalloc"]["current_bytes"] > 0
        assert 0 < len(point["tracemalloc"]["top"]) <= 5
        assert point["derived"]["unreturned_allocator_bytes"] is not None
    cell = {p["name"]: p for p in report["cell"]["points"]}
    assert cell["first_hybrid_find"]["facts"]["served"] is True
    assert cell["first_governed_write"]["facts"]["committed"] is True
    assert "embeddings" in cell["reaper_tick"]["facts"]["reaped"]
    assert cell["cell_ready"]["counters"]["caches"]["vector_matrices"]["embedding"]["rows"] > 0
    assert "first_governed_write" in proc.stdout

    _assert_content_free(report, tmp_path, notes=12, links=3)


@pytest.mark.embeddings
@pytest.mark.timeout(900)
def test_real_model_run_attributes_native_memory_to_the_model(tmp_path: Path) -> None:
    pytest.importorskip("onnxruntime")
    pytest.importorskip("tokenizers")
    pytest.importorskip("huggingface_hub")

    _proc, report = _run_harness(
        tmp_path, "--notes", "8", "--links-per-note", "3", "--encoder", "onnx", "--top", "5"
    )

    cell = {p["name"]: p for p in report["cell"]["points"]}
    # bge-m3 int8 is hundreds of MiB of native weights; a stub would read ~0.
    assert cell["model_load"]["derived"]["model_native_bytes"] > 100 * MIB
    assert cell["reaper_tick"]["derived"]["model_native_bytes"] == 0
    _assert_content_free(report, tmp_path, notes=8, links=3)
