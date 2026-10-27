"""media_brakes — the Cloud memory brakes read the cell's own cgroup v2 files."""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import media_brakes, media_jobs, media_worker

_GIB = 1 << 30
_MIB = 1 << 20


def _cell(tmp_path: Path, *, anon: int, current: int, high: str, pressure: str | None) -> dict:
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    (proc / "self/cgroup").write_text("0::/cell.scope\n", encoding="utf-8")
    group = tmp_path / "sys/cell.scope"
    group.mkdir(parents=True)
    (group / "memory.stat").write_text(f"anon {anon}\nfile {current - anon}\n", encoding="utf-8")
    (group / "memory.current").write_text(f"{current}\n", encoding="utf-8")
    (group / "memory.max").write_text(f"{3 * _GIB}\n", encoding="utf-8")
    (group / "memory.high").write_text(f"{high}\n", encoding="utf-8")
    if pressure is not None:
        (group / "memory.pressure").write_text(pressure, encoding="utf-8")
    return {"proc": proc, "sys_root": tmp_path / "sys"}


def test_admission_reads_anonymous_memory_under_the_lower_ceiling(tmp_path: Path) -> None:
    config = media_brakes.settings({})
    budget = media_brakes.Budget(anon_bytes=512 * _MIB, vmdata_bytes=_GIB)
    # Page cache fills memory.current to the limit; anonymous memory leaves room.
    roomy = media_brakes.read_cell(**_cell(tmp_path / "a", anon=_GIB, current=3 * _GIB, high="max", pressure=None))
    assert media_brakes.admits(roomy, budget, config)
    # memory.high (2 GiB) below 80% of memory.max (2.4 GiB) governs admission.
    capped = media_brakes.read_cell(
        **_cell(tmp_path / "b", anon=int(1.6 * _GIB), current=2 * _GIB, high=str(2 * _GIB), pressure=None)
    )
    assert not media_brakes.admits(capped, budget, config)


def test_pressure_stop_reads_psi_and_falls_back_to_the_ceiling(tmp_path: Path) -> None:
    config = media_brakes.settings({})
    stalled = media_brakes.read_cell(
        **_cell(
            tmp_path / "a", anon=_GIB, current=_GIB, high="max",
            pressure="some avg10=31.50 avg60=4.00 avg300=1.00 total=9\nfull avg10=2.00 avg60=0 avg300=0 total=1\n",
        )
    )
    assert media_brakes.pressure_exceeded(stalled, config)
    # Without PSI, anonymous memory at 80% of memory.max stops the child.
    blind_low = media_brakes.read_cell(**_cell(tmp_path / "b", anon=2 * _GIB, current=3 * _GIB, high="max", pressure=None))
    blind_high = media_brakes.read_cell(**_cell(tmp_path / "c", anon=int(2.5 * _GIB), current=3 * _GIB, high="max", pressure=None))
    assert not media_brakes.pressure_exceeded(blind_low, config)
    assert media_brakes.pressure_exceeded(blind_high, config)


@pytest.mark.parametrize(
    "state",
    ["cgroup v1", "garbage memory.max", "no anon", "no memory limit"],
)
def test_an_unexpected_cgroup_on_a_cloud_cell_admits_nothing_and_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    # Failing open here would let media run blind and take the serving process down.
    paths = _cell(tmp_path, anon=_GIB, current=_GIB, high="max", pressure=None)
    group = tmp_path / "sys/cell.scope"
    if state == "cgroup v1":
        (paths["proc"] / "self/cgroup").write_text("4:memory:/cell.scope\n", encoding="utf-8")
    elif state == "garbage memory.max":
        (group / "memory.max").write_text("lots\n", encoding="utf-8")
    elif state == "no anon":
        (group / "memory.stat").write_text("file 1024\n", encoding="utf-8")
    else:
        (group / "memory.max").write_text("max\n", encoding="utf-8")
    config = media_brakes.settings({})
    budget = media_brakes.Budget(anon_bytes=_MIB, vmdata_bytes=_GIB)

    cell = media_brakes.read_cell(**paths)

    assert not media_brakes.admits(cell, budget, config)
    assert media_brakes.pressure_exceeded(cell, config)
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setattr(media_brakes, "read_cell", lambda: cell)
    status = media_brakes.status()
    assert status["state"] == "unavailable" and status["reason"]


def test_a_budget_outside_the_cell_falls_back_to_the_default(tmp_path: Path) -> None:
    cell = media_brakes.read_cell(**_cell(tmp_path, anon=_GIB, current=_GIB, high="max", pressure=None))
    raw = '{"ocr": {"anon_mib": 0, "vmdata_mib": 512}, "documents": {"anon_mib": 4096, "vmdata_mib": 512}}'

    config = media_brakes.settings({media_brakes.BUDGETS_ENV: raw}, cell)

    # A zero budget admits everything and one above memory.max can never be met.
    assert config.budget_for("ocr") == config.default_budget
    assert config.budget_for("documents") == config.default_budget


def test_file_backed_speech_accepts_virtual_data_above_the_physical_cell_limit(tmp_path: Path) -> None:
    cell = media_brakes.read_cell(**_cell(tmp_path, anon=_GIB, current=_GIB, high="max", pressure=None))
    raw = '{"speech": {"anon_mib": 1024, "vmdata_mib": 4096}}'

    config = media_brakes.settings({media_brakes.BUDGETS_ENV: raw}, cell)

    assert config.budget_for("speech") == media_brakes.Budget(_GIB, 4 * _GIB)
    assert media_brakes.admits(cell, config.budget_for("speech"), config)


def test_a_child_that_embeds_an_image_budgets_the_clip_tower_only_while_image_search_is_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = '{"ocr": {"anon_mib": 640, "vmdata_mib": 1024}, "clip": {"anon_mib": 256, "vmdata_mib": 1536}}'
    config = media_brakes.settings({media_brakes.BUDGETS_ENV: raw})
    job = media_jobs.MediaJob(
        binary_path=Path("photo.png"), sidecar_path=Path("photo.png.md"), media_type="image", do_clip=True
    )

    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    assert media_worker._budget_for(job, config) == media_brakes.Budget(896 * _MIB, 1536 * _MIB)

    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    assert media_worker._budget_for(job, config) == media_brakes.Budget(640 * _MIB, 1024 * _MIB)


def test_a_child_that_loads_speech_and_the_clip_tower_adds_their_heaps_but_not_their_mappings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = '{"speech": {"anon_mib": 1024, "vmdata_mib": 4096}, "clip": {"anon_mib": 256, "vmdata_mib": 1536}}'
    config = media_brakes.settings({media_brakes.BUDGETS_ENV: raw})
    job = media_jobs.MediaJob(
        binary_path=Path("talk.mp4"), sidecar_path=Path("talk.mp4.md"), media_type="video", do_clip=True
    )
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)

    assert media_worker._budget_for(job, config) == media_brakes.Budget(1280 * _MIB, 4096 * _MIB)
