"""The host-speed calibration: what the report records, and how it scales the large-import timings.

The workload runs for real, shortened; the scenario's report is a plain dict, so no cluster is needed.
"""

from __future__ import annotations

from types import SimpleNamespace

from cloud_rehearsal import host_speed, large_import

OUT = {
    "phases": {"notes.generate": 27.2, "media.generate": 8.0},
    "folders": {"notes": {"pages": [{"page": 1, "apply_seconds": 40.0}, {"page": 2, "apply_seconds": 10.0}]},
                "media": {}},
}


def test_the_calibration_is_recorded_and_the_scaled_timings_follow_from_it(monkeypatch):
    monkeypatch.setattr(host_speed, "PASSES", 2)
    monkeypatch.setattr(host_speed, "SHA256_ROUNDS", 2)
    monkeypatch.setattr(host_speed, "ZLIB_ROUNDS", 1)

    recorded = host_speed.calibrate()

    assert recorded["host_calibration_seconds"] == min(recorded["host_calibration_pass_seconds"]) > 0
    assert len(recorded["host_calibration_pass_seconds"]) == 2
    assert recorded["host_calibration"]["workload_id"] == host_speed.WORKLOAD_ID
    assert recorded["host_calibration"]["sha256"]["rounds"] == 2

    # A host that takes twice the reference seconds is half as fast: the same work scales to half the seconds.
    slow = large_import.calibrated_timings(OUT, 2 * host_speed.REFERENCE_SECONDS)
    assert slow["host_calibration"]["calibration_ratio"] == 2.0
    assert slow["phase_seconds_per_calibration"] == {"notes.generate": 13.6, "media.generate": 4.0}
    assert slow["page_1_apply"]["notes"] == {"apply_seconds": 40.0, "apply_seconds_per_calibration": 20.0}
    # Measured once for the run: the same calibration the report holds scales every timing.
    measured = recorded["host_calibration_seconds"]
    scaled = large_import.calibrated_timings(OUT, measured)["phase_seconds_per_calibration"]["notes.generate"]
    assert scaled == round(27.2 * host_speed.REFERENCE_SECONDS / measured, 2)


def test_a_run_without_a_calibration_keeps_the_raw_seconds_and_scales_nothing_to_zero():
    for missing in (None, 0.0):
        timings = large_import.calibrated_timings(OUT, missing)

        assert timings["host_calibration"]["calibration_ratio"] is None
        assert timings["phase_seconds_per_calibration"] == {"notes.generate": None, "media.generate": None}
        assert timings["page_1_apply"]["notes"] == {"apply_seconds": 40.0, "apply_seconds_per_calibration": None}

    # A folder that never reached its first page has no apply seconds to scale, with or without a calibration.
    assert large_import.calibrated_timings(OUT, 1.0)["page_1_apply"]["media"] == {
        "apply_seconds": None, "apply_seconds_per_calibration": None}


def test_the_measurements_keep_the_raw_phase_seconds_next_to_the_scaled_ones():
    sampler = SimpleNamespace(summary=lambda: {"all": large_import.peaks([]), "by_phase": {}})

    measured = large_import.measurements(OUT, sampler, 1.0)

    assert measured["phase_seconds"] == OUT["phases"]
    assert measured["phase_seconds_per_calibration"]["notes.generate"] == 13.6


# --- the host's facts -----------------------------------------------------------------------


def test_an_lscpu_without_a_clock_speed_leaves_it_unknown_and_a_host_without_lscpu_leaves_all_unknown(monkeypatch):
    reported = "CPU(s):   20\nModel name:   Example CPU @ 3.0GHz\nNUMA node0 CPU(s):   0-19\nCPU(s):   99\n"
    monkeypatch.setattr(host_speed, "run", lambda *_a, **_k: SimpleNamespace(stdout=reported))

    assert host_speed.cpu_facts() == {"cpu_model": "Example CPU @ 3.0GHz", "cpu_count_lscpu": 20, "cpu_mhz": None}

    def absent(*_a, **_k):
        raise FileNotFoundError("lscpu")

    monkeypatch.setattr(host_speed, "run", absent)

    assert host_speed.cpu_facts() == {"cpu_model": None, "cpu_count_lscpu": None, "cpu_mhz": None}
