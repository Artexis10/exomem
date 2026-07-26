from __future__ import annotations

import json
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_script() -> dict:
    return runpy.run_path(str(ROOT / "scripts" / "verify-resource-envelope.py"))


def test_physical_limit_is_skipped_when_native_sampling_is_incomplete(
    monkeypatch, capsys
) -> None:
    script = _load_script()
    row = script["ProcessRow"]
    script["main"].__globals__["process_rows"] = lambda: [
        row(1, 200.0, 600.0, "physical_footprint", 600.0, 0.0, "exomem --transport http"),
        row(2, 300.0, 300.0, "rss", None, 0.0, "exomem --transport http"),
    ]

    code = script["main"]([
        "--samples", "1", "--expected-servers", "2", "--max-rss-mb", "1000",
        "--max-memory-mb", "512",
    ])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["server_memory"]["memory_metric"] == "mixed"
    assert not [item for item in payload["failures"] if "physical footprint" in item]


def test_physical_limit_applies_to_the_complete_server_total(monkeypatch, capsys) -> None:
    script = _load_script()
    row = script["ProcessRow"]
    script["main"].__globals__["process_rows"] = lambda: [
        row(1, 200.0, 300.0, "physical_footprint", 300.0, 0.0, "exomem --transport http"),
        row(2, 200.0, 300.0, "physical_footprint", 300.0, 0.0, "exomem --transport http"),
    ]

    code = script["main"]([
        "--samples", "1", "--expected-servers", "2", "--max-rss-mb", "1000",
        "--max-memory-mb", "512",
    ])

    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["server_memory"]["memory_metric"] == "physical_footprint"
    assert any("physical footprint total 600.0 MiB" in item for item in payload["failures"])
