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


def _environment(zlib_seconds):
    """What `report.environment` holds after the calibration, with a chosen zlib time."""
    return {host_speed.ZLIB_SECONDS_KEY: zlib_seconds}


def _shortened(monkeypatch):
    monkeypatch.setattr(host_speed, "PASSES", 2)
    monkeypatch.setattr(host_speed, "SHA256_ROUNDS", 20)
    monkeypatch.setattr(host_speed, "ZLIB_ROUNDS", 1)


def test_the_calibration_is_recorded_and_the_scaled_timings_follow_from_it(monkeypatch):
    _shortened(monkeypatch)

    recorded = host_speed.calibrate()

    zlib_passes = [each["zlib"] for each in recorded[host_speed.PASS_SECONDS_KEY]]
    sha256_passes = [each["sha256"] for each in recorded[host_speed.PASS_SECONDS_KEY]]
    assert len(zlib_passes) == 2
    assert recorded[host_speed.ZLIB_SECONDS_KEY] == min(zlib_passes) > 0
    assert recorded[host_speed.SHA256_SECONDS_KEY] == min(sha256_passes) > 0
    assert recorded[host_speed.WORKLOAD_KEY]["workload_id"] == host_speed.WORKLOAD_ID
    assert recorded[host_speed.WORKLOAD_KEY]["sha256"]["rounds"] == 20

    # A host that takes twice the reference's zlib seconds is half as fast: the same work scales to half the seconds.
    slow = large_import.calibrated_timings(OUT, _environment(2 * host_speed.REFERENCE_ZLIB_SECONDS))
    assert slow["calibration_scale"]["calibration_ratio"] == 2.0
    assert slow["calibration_scale"]["basis"] == "zlib"
    assert slow["phase_seconds_per_calibration"] == {"notes.generate": 13.6, "media.generate": 4.0}
    assert slow["page_1_apply"]["notes"] == {"apply_seconds": 40.0, "apply_seconds_per_calibration": 20.0}


def test_the_calibration_that_calibrate_writes_is_the_one_the_large_import_report_reads(monkeypatch):
    _shortened(monkeypatch)
    environment = host_speed.calibrate()

    timings = large_import.calibrated_timings(OUT, environment)

    measured = environment[host_speed.ZLIB_SECONDS_KEY]
    assert timings["calibration_scale"][host_speed.ZLIB_SECONDS_KEY] == measured
    assert timings["phase_seconds_per_calibration"]["notes.generate"] == round(
        27.2 * host_speed.REFERENCE_ZLIB_SECONDS / measured, 2)


def test_the_sha256_seconds_are_a_recorded_fact_and_do_not_move_the_scaled_timings():
    fast_sha = {**_environment(0.4), host_speed.SHA256_SECONDS_KEY: 0.04}
    slow_sha = {**_environment(0.4), host_speed.SHA256_SECONDS_KEY: 0.26}

    assert large_import.calibrated_timings(OUT, fast_sha) == large_import.calibrated_timings(OUT, slow_sha)


def test_a_run_without_a_calibration_keeps_the_raw_seconds_and_scales_nothing_to_zero():
    for missing in ({}, _environment(None), _environment(0.0)):
        timings = large_import.calibrated_timings(OUT, missing)

        assert timings["calibration_scale"]["calibration_ratio"] is None
        assert timings["phase_seconds_per_calibration"] == {"notes.generate": None, "media.generate": None}
        assert timings["page_1_apply"]["notes"] == {"apply_seconds": 40.0, "apply_seconds_per_calibration": None}

    # A folder that never reached its first page has no apply seconds to scale, with or without a calibration.
    assert large_import.calibrated_timings(OUT, _environment(1.0))["page_1_apply"]["media"] == {
        "apply_seconds": None, "apply_seconds_per_calibration": None}


def test_the_measurements_keep_the_raw_phase_seconds_next_to_the_scaled_ones():
    sampler = SimpleNamespace(summary=lambda: {"all": large_import.peaks([]), "by_phase": {}})

    measured = large_import.measurements(OUT, sampler, _environment(2 * host_speed.REFERENCE_ZLIB_SECONDS))

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
