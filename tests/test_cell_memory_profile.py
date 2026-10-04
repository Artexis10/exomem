"""The cell memory profiler (bound-cell-memory D1): its derived figures, its
content-free guarantee, and a smoke run of the real cloud cell on a tiny vault.

Every later memory change is judged against this harness, so the arithmetic that
turns raw readings into "model" and "unreturned allocator" figures is pinned
here, and so is the rule that no vault string ever reaches the report.
"""

from __future__ import annotations

import argparse
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
#: The harness reads Linux's per-process memory accounting; without it every
#: figure it exists to produce is unknown.
needs_proc_memory = pytest.mark.skipif(
    not Path("/proc/self/smaps_rollup").exists(),
    reason="needs /proc/self/smaps_rollup (Linux 4.14+)",
)


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


def test_the_warm_encode_reading_is_the_model_when_there_is_one() -> None:
    points = _cell_points()
    # One encode grows the session's arena: untraced 848 against import's 180.
    points.insert(
        2, _point("model_warm", rss=1060, anon=1000, traced=132, overhead=20, model=True)
    )

    estimate = profile.derive(points)

    assert {k: v // MIB for k, v in estimate.items() if isinstance(v, int)} == {
        "load_delta_bytes": 598,
        "warm_delta_bytes": 668,
        "attributed_bytes": 668,
    }
    assert estimate["basis"] == "model_warm"
    assert _derived(points, "first_hybrid_find")["unreturned_allocator_bytes"] == 180


def test_a_negative_model_delta_is_recorded_raw_but_never_attributed() -> None:
    points = _cell_points()
    # Untraced anon FELL across the load (170 against 180): record that, but a
    # model cannot hold negative memory, so nothing is subtracted for it.
    points[1] = _point("model_load", rss=350, anon=290, traced=100, overhead=20, model=True)

    estimate = profile.derive(points)

    assert estimate["load_delta_bytes"] == -10 * MIB
    assert estimate["attributed_bytes"] == 0
    assert _derived(points, "model_load")["model_native_bytes"] == 0
    assert _derived(points, "model_load")["unreturned_allocator_bytes"] == 170


def test_a_stub_run_records_its_raw_deltas_but_attributes_nothing() -> None:
    points = _cell_points()

    estimate = profile.derive(points, native_model=False)

    assert estimate["load_delta_bytes"] == 598 * MIB
    assert estimate["attributed_bytes"] == 0
    assert estimate["basis"] == "stub"


def test_a_point_reads_memory_before_it_snapshots_tracemalloc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The snapshot frees what it allocates; read first, or that lands in the residual."""
    calls: list[str] = []

    def recorder(name: str, value: object = None):
        def record(*_args, **_kwargs):
            calls.append(name)
            return value

        return record

    monkeypatch.setattr(profile, "read_smaps_rollup", recorder("smaps", {}))
    monkeypatch.setattr(profile, "read_peak_rss", recorder("vm_hwm", 0))
    monkeypatch.setattr(profile, "glibc_mallinfo", recorder("mallinfo", {}))
    monkeypatch.setattr(profile, "product_counters", recorder("counters", {}))
    monkeypatch.setattr(profile, "tracemalloc_sample", recorder("snapshot", {}))
    monkeypatch.setattr(profile.tracemalloc, "reset_peak", recorder("reset_tracemalloc_peak"))
    monkeypatch.setattr(profile, "reset_peak_rss", recorder("reset_rss_peak", True))

    profile.sample("import", top=1, started=0.0)

    assert calls == [
        "smaps",
        "vm_hwm",
        "mallinfo",
        "counters",
        "snapshot",
        "reset_tracemalloc_peak",
        "reset_rss_peak",
    ]


def test_stage_env_bounds_glibc_arenas_like_the_image(tmp_path: Path) -> None:
    base = {"PATH": "/bin", "MALLOC_ARENA_MAX": "8", "EXOMEM_MODE": "performance"}

    bounded = profile.cell_environment(tmp_path, encoder="stub", base=base, malloc_arena_max=2)
    unset = profile.cell_environment(tmp_path, encoder="stub", base=base, malloc_arena_max=0)

    assert bounded["MALLOC_ARENA_MAX"] == "2"
    # 0 is glibc's own default: nothing set, not even what the caller had.
    assert "MALLOC_ARENA_MAX" not in unset
    assert "EXOMEM_MODE" not in bounded


def _stage_args(tmp_path: Path) -> argparse.Namespace:
    args = profile._parser().parse_args(["--encoder", "stub"])
    args.scratch = str(tmp_path)
    return args


def test_a_failed_stage_prints_its_captured_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def failing(*_args, **kwargs):
        assert kwargs.get("capture_output") is True
        return subprocess.CompletedProcess([], 1, stdout="child said this", stderr="child failed")

    monkeypatch.setattr(profile.subprocess, "run", failing)

    with pytest.raises(SystemExit, match="stage build failed"):
        profile._run_stage("build", _stage_args(tmp_path), {})
    printed = capsys.readouterr()
    assert "child said this" in printed.err
    assert "child failed" in printed.err


def test_a_stage_that_hangs_is_stopped_and_its_output_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _stage_args(tmp_path)
    args.timeout = 5.0

    def hanging(command, **kwargs):
        assert kwargs.get("timeout") == 10.0
        raise subprocess.TimeoutExpired(
            command, kwargs["timeout"], output="child got this far", stderr="then stalled"
        )

    monkeypatch.setattr(profile.subprocess, "run", hanging)

    with pytest.raises(SystemExit, match="stage cell timed out"):
        profile._run_stage("cell", args, {})
    printed = capsys.readouterr()
    assert "child got this far" in printed.err
    assert "then stalled" in printed.err


def test_a_negative_arena_bound_is_refused() -> None:
    with pytest.raises(SystemExit):
        profile._parser().parse_args(["--malloc-arena-max", "-1"])
    assert profile._parser().parse_args(["--malloc-arena-max", "0"]).malloc_arena_max == 0


def test_a_passing_stage_prints_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def passing(*_args, **_kwargs):
        (tmp_path / "build.json").write_text('{"points": []}', encoding="utf-8")
        return subprocess.CompletedProcess([], 0, stdout="noisy log", stderr="noisy warning")

    monkeypatch.setattr(profile.subprocess, "run", passing)

    assert profile._run_stage("build", _stage_args(tmp_path), {})["points"] == []
    printed = capsys.readouterr()
    assert printed.out == printed.err == ""


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


@needs_proc_memory
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
    assert report["malloc_arena_max"] == 2
    assert "keepcost_bytes" in cell["reaper_tick"]["glibc"]
    assert cell["reaper_tick"]["facts"]["trimmed"] in {True, False, None}
    assert "lazily" in report["note"]
    assert report["cell"]["model_estimate"]["basis"] == "stub"
    assert report["cell"]["model_estimate"]["warm_delta_bytes"] is not None

    _assert_content_free(report, tmp_path, notes=12, links=3)


@pytest.mark.embeddings
@needs_proc_memory
@pytest.mark.timeout(900)
def test_real_model_run_attributes_native_memory_to_the_model(tmp_path: Path) -> None:
    pytest.importorskip("onnxruntime")
    pytest.importorskip("tokenizers")
    pytest.importorskip("huggingface_hub")

    from exomem import embedding_backend

    # The child intentionally runs offline like the image. Prepare its pinned
    # files here, before it starts, including on a cold CI model cache.
    embedding_backend.ensure_served_artifact("BAAI/bge-m3")

    _proc, report = _run_harness(
        tmp_path, "--notes", "8", "--links-per-note", "3", "--encoder", "onnx", "--top", "5"
    )

    cell = {p["name"]: p for p in report["cell"]["points"]}
    # bge-m3 int8 is hundreds of MiB of native weights; a stub would read ~0.
    assert cell["model_load"]["derived"]["model_native_bytes"] > 100 * MIB
    assert cell["reaper_tick"]["derived"]["model_native_bytes"] == 0
    _assert_content_free(report, tmp_path, notes=8, links=3)


def test_real_model_harness_prepares_its_cache_before_starting_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import embedding_backend

    cached = tmp_path / "model-ready"

    def prepare(model: str) -> None:
        assert model == "BAAI/bge-m3"
        cached.touch()

    def offline_harness(*_args, **_kwargs):
        assert cached.exists(), "the offline child needs a populated model cache"
        return None, {
            "cell": {"points": [
                {"name": "model_load", "derived": {"model_native_bytes": 101 * MIB}},
                {"name": "reaper_tick", "derived": {"model_native_bytes": 0}},
            ]},
        }

    monkeypatch.setattr(embedding_backend, "ensure_served_artifact", prepare)
    monkeypatch.setattr(pytest, "importorskip", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sys.modules[__name__], "_run_harness", offline_harness)
    monkeypatch.setattr(sys.modules[__name__], "_assert_content_free", lambda *_args, **_kwargs: None)

    test_real_model_run_attributes_native_memory_to_the_model(tmp_path)
