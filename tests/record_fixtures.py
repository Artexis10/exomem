"""Reusable structured-collection acceptance fixtures."""

from __future__ import annotations

import shutil
from pathlib import Path

FIXTURES = Path(__file__).with_name("fixtures") / "records"


def copy_x3_fixture(destination: Path) -> Path:
    target = _copy_fixture("x3", destination)
    crlf_variant = target / "Training Log CRLF no final newline.md"
    crlf_variant.write_bytes(
        (target / "Training Log.md").read_bytes().replace(b"\n", b"\r\n").rstrip(b"\r\n")
    )
    return target


def copy_vehicle_maintenance_fixture(destination: Path) -> Path:
    return _copy_fixture("vehicle-maintenance", destination)


def copy_dataset_fixture(destination: Path) -> Path:
    return _copy_fixture("dataset", destination)


def _copy_fixture(name: str, destination: Path) -> Path:
    target = destination / "Knowledge Base" / "Records" / name
    shutil.copytree(FIXTURES / name, target)
    return target
