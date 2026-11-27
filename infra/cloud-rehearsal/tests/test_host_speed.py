"""The host-speed calibration: what the report records, and how it scales the large-import timings.

The workload runs for real, shortened; the scenario's report is a plain dict, so no cluster is needed.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from cloud_rehearsal import host_speed, large_import

OUT = {
    "phases": {"notes.generate": 27.2, "media.generate": 8.0},
    "folders": {"notes": {"pages": [{"page": 1, "file_seconds": 40.0}, {"page": 2, "file_seconds": 10.0}]},
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
    assert slow["page_1_file"]["notes"] == {"file_seconds": 40.0, "file_seconds_per_calibration": 20.0}


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
        assert timings["page_1_file"]["notes"] == {"file_seconds": 40.0, "file_seconds_per_calibration": None}

    # A folder that never reached its first page has no file seconds to scale, with or without a calibration.
    assert large_import.calibrated_timings(OUT, _environment(1.0))["page_1_file"]["media"] == {
        "file_seconds": None, "file_seconds_per_calibration": None}


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


def _disk(sys_root, name, *, model, rotational, partition=None, number="259:1"):
    """A disk in a fake sysfs, with an optional partition and the `major:minor` link that mountinfo names."""
    disk = sys_root / "devices" / "pci0" / "block" / name
    (disk / "queue").mkdir(parents=True)
    (disk / "device").mkdir()
    (disk / "queue" / "rotational").write_text(f"{rotational}\n")
    (disk / "device" / "model").write_text(f"{model}\n")
    target = disk
    if partition:
        target = disk / partition
        target.mkdir()
        (target / "partition").write_text("1\n")
    (sys_root / "dev" / "block").mkdir(parents=True, exist_ok=True)
    (sys_root / "dev" / "block" / number).symlink_to(target)


MOUNTINFO = (
    "25 1 259:9 / / rw,relatime shared:1 - ext4 /dev/old rw\n"
    "31 25 0:6 / /dev rw,nosuid - devtmpfs udev rw\n"
    "40 25 259:1 / / rw,relatime shared:2 - ext4 /dev/nvme0n1p1 rw,discard\n"
)


def test_the_report_names_the_machine_and_the_root_disk_behind_the_runners_label(tmp_path):
    """Catches a report that cannot tell a runner on a local NVMe snapshot from one on a network disk."""
    dmi = tmp_path / "dmi"
    dmi.mkdir()
    (dmi / "sys_vendor").write_text("Example Cloud\n")
    (dmi / "product_name").write_text("Virtual Machine\n")
    _disk(tmp_path / "sys", "nvme0n1", model="Example NVMe Direct Disk", rotational=0, partition="nvme0n1p1")
    (tmp_path / "mountinfo").write_text(MOUNTINFO)

    provider = host_speed.provider_facts(dmi, {"RUNNER_ENVIRONMENT": "github-hosted", "ImageOS": "ubuntu24", "HOME": "/x"})
    disk = host_speed.root_disk_facts(tmp_path / "mountinfo", tmp_path / "sys")

    assert provider["host_provider"]["dmi"] == {"sys_vendor": "Example Cloud", "product_name": "Virtual Machine",
                                                "board_vendor": None, "bios_vendor": None, "chassis_asset_tag": None}
    assert provider["host_provider"]["runner"] == {"RUNNER_ENVIRONMENT": "github-hosted", "RUNNER_OS": None,
                                                   "RUNNER_ARCH": None, "ImageOS": "ubuntu24", "ImageVersion": None}
    # The later mount of "/" covers the earlier one, and a partition reports its whole disk's model and queue.
    assert disk["root_disk"]["mount"] == {"device_number": "259:1", "mount_options": "rw,relatime",
                                          "filesystem": "ext4", "source": "/dev/nvme0n1p1"}
    assert disk["root_disk"]["block_device"] == {"name": "nvme0n1p1", "disk": "nvme0n1", "rotational": False,
                                                 "model": "Example NVMe Direct Disk", "mapper_name": None,
                                                 "devices_under": []}
    json.dumps([provider, disk])  # the report is JSON


def test_a_root_that_is_not_a_block_device_or_a_host_that_hides_its_facts_leaves_them_unknown(tmp_path):
    (tmp_path / "mountinfo").write_text("22 1 0:50 / / rw - overlay overlay rw,lowerdir=/a\n")

    overlay = host_speed.root_disk_facts(tmp_path / "mountinfo", tmp_path / "sys")
    hidden = host_speed.root_disk_facts(tmp_path / "absent", tmp_path / "sys")

    assert overlay["root_disk"]["mount"]["filesystem"] == "overlay" and overlay["root_disk"]["block_device"] is None
    assert hidden == {"root_disk": {"mount": None, "block_device": None}}
    assert set(host_speed.provider_facts(tmp_path / "absent", {})["host_provider"]["dmi"].values()) == {None}


def test_a_device_mapper_root_names_the_devices_under_it(tmp_path):
    """Catches a copy-on-write snapshot root that reads as a plain disk."""
    sys_root = tmp_path / "sys"
    _disk(sys_root, "dm-0", model="Mapped", rotational=0, number="253:0")
    (sys_root / "devices" / "pci0" / "block" / "dm-0" / "dm").mkdir()
    (sys_root / "devices" / "pci0" / "block" / "dm-0" / "dm" / "name").write_text("snap-root\n")
    (sys_root / "devices" / "pci0" / "block" / "dm-0" / "slaves").mkdir()
    (sys_root / "devices" / "pci0" / "block" / "dm-0" / "slaves" / "nvme1n1").mkdir()

    found = host_speed.block_device_facts("253:0", sys_root)

    assert found["mapper_name"] == "snap-root" and found["devices_under"] == ["nvme1n1"]


# --- the start call's calibration ------------------------------------------------------------


def _with_start(cpu_seconds, mode="full"):
    """The report as it stands after the notes folder's `start` call took `cpu_seconds` of `command.body` thread CPU."""
    return {**OUT, "mode": mode,
            "folders": {"notes": {"start_call": {"command_body_thread_cpu_seconds": cpu_seconds},
                                  "pages": [{"page": 1, "file_seconds": 40.0}, {"page": 2, "file_seconds": 10.0}]},
                        "media": {"pages": [{"page": 1, "file_seconds": 30.0}]}}}


def test_the_raw_file_seconds_stay_beside_the_ones_divided_by_the_start_calls_cpu_ratio():
    """Catches a runner that is slower for this work than its zlib calibration says: the start call is the same work."""
    slower = large_import.REFERENCE_START_CPU_SECONDS * 2  # a host that took twice the reference's CPU for the call

    scaled = large_import.calibrated_timings(_with_start(slower), _environment(host_speed.REFERENCE_ZLIB_SECONDS))

    assert scaled["start_call_scale"]["calibration_ratio"] == 2.0 and scaled["start_call_scale"]["basis"] == "start_call_thread_cpu"
    assert scaled["page_file_seconds_per_start_calibration"] == {
        "notes": [{"page": 1, "file_seconds": 40.0, "file_seconds_per_start_calibration": 20.0},
                  {"page": 2, "file_seconds": 10.0, "file_seconds_per_start_calibration": 5.0}],
        "media": [{"page": 1, "file_seconds": 30.0, "file_seconds_per_start_calibration": 15.0}]}
    assert scaled["page_1_file"]["notes"]["file_seconds"] == 40.0  # the raw value stays the record


def test_a_smoke_run_or_a_start_call_that_was_not_read_scales_nothing_to_zero():
    for report in (_with_start(5.0, mode="smoke"), _with_start(None), {**_with_start(5.0), "folders": {"notes": {}, "media": {}}}):
        scaled = large_import.calibrated_timings(report, _environment(1.0))

        assert scaled["start_call_scale"]["calibration_ratio"] is None
        assert all(page["file_seconds_per_start_calibration"] is None
                   for pages in scaled["page_file_seconds_per_start_calibration"].values() for page in pages)
