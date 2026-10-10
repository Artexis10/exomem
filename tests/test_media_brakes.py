"""media_brakes — the Cloud memory brakes read the cell's own cgroup v2 files."""

from __future__ import annotations

from pathlib import Path

from exomem import media_brakes

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
