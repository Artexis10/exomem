"""The storage spike's parsers: the numbers it records come through these."""

from __future__ import annotations

import json

import pytest

from cloud_rehearsal import storage

LVS = {
    "report": [{"lv": [
        {"lv_name": "pool0", "lv_size": "25769803776", "pool_lv": "", "origin": "", "data_percent": "3.50",
         "metadata_percent": "0.81", "lv_attr": "twi-aot---", "lv_metadata_size": "134217728"},
        {"lv_name": "pool0_tdata", "lv_size": "25769803776", "pool_lv": "", "origin": "", "data_percent": "",
         "metadata_percent": "", "lv_attr": "Twi-ao----", "lv_metadata_size": ""},
        {"lv_name": "cell", "lv_size": "4294967296", "pool_lv": "pool0", "origin": "", "data_percent": "20.0",
         "metadata_percent": "", "lv_attr": "Vwi-aotz--", "lv_metadata_size": ""},
        {"lv_name": "snap", "lv_size": "4294967296", "pool_lv": "pool0", "origin": "cell", "data_percent": "20.0",
         "metadata_percent": "", "lv_attr": "Vri---tz-k", "lv_metadata_size": ""},
    ]}]
}


def test_pool_usage_counts_every_thin_volume_and_snapshot_at_full_virtual_size() -> None:
    usage = storage.parse_lvs(json.dumps(LVS))
    assert (usage.pool_bytes, usage.metadata_bytes) == (25769803776, 134217728)
    assert (usage.data_percent, usage.metadata_percent) == (3.5, 0.81)
    # The pool's own hidden data volume is not a thin volume; the snapshot is one.
    assert (usage.thin_volumes, usage.virtual_bytes) == (2, 2 * 4294967296)


def test_a_report_without_the_pool_is_an_error_not_an_empty_pool() -> None:
    with pytest.raises(ValueError, match="no thin pool"):
        storage.parse_lvs(json.dumps(LVS), pool="other")


def test_byte_quantities_as_csi_storage_capacity_publishes_them() -> None:
    assert storage.parse_quantity("15Gi") == 15 * 1024**3
    assert storage.parse_quantity("20000000000") == 20_000_000_000
    assert storage.parse_quantity("1500M") == 1_500_000_000
    with pytest.raises(ValueError):
        storage.parse_quantity("-3Gi")


def test_pool_size_is_read_from_the_node_plugin_metrics_by_device_class() -> None:
    text = "\n".join([
        "# HELP topolvm_thinpool_size_bytes LVM VG Thin Pool raw size bytes",
        'topolvm_thinpool_size_bytes{device_class="thin",node="spike-1"} 2.5769803776e+10',
        'topolvm_thinpool_metadata_percent{device_class="thin",node="spike-1"} 0.81',
        'topolvm_volumegroup_size_bytes{device_class="thin",node="spike-1"} 3.4355e+10',
        'go_goroutines 12',
    ])
    assert storage.parse_thinpool_metrics(text) == {
        "thin": {
            "topolvm_thinpool_size_bytes": 25769803776.0,
            "topolvm_thinpool_metadata_percent": 0.81,
            "topolvm_volumegroup_size_bytes": 34355000000.0,
        }
    }
