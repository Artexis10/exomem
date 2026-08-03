"""Reusable structured-collection acceptance fixtures."""

from __future__ import annotations

import shutil
from pathlib import Path

FIXTURES = Path(__file__).with_name("fixtures") / "records"


def copy_x3_fixture(destination: Path) -> Path:
    target = _copy_fixture_at("x3", destination, "Health/X3")
    template_target = x3_template_directory(destination)
    template_target.mkdir(parents=True, exist_ok=True)
    for name in ("X3 Push.md", "X3 Pull.md"):
        shutil.move(target / name, template_target / name)
    crlf_variant = target / "Training Log CRLF no final newline.md"
    crlf_variant.write_bytes(
        (target / "Training Log.md").read_bytes().replace(b"\n", b"\r\n").rstrip(b"\r\n")
    )
    return target


def x3_template_directory(destination: Path) -> Path:
    """Return the ordinary Obsidian template location for the copied X3 fixture."""
    return destination / "Knowledge Base" / "Templates" / "Records" / "Health" / "X3"


def copy_vehicle_maintenance_fixture(destination: Path) -> Path:
    return _copy_fixture("vehicle-maintenance", destination)


def copy_dataset_fixture(destination: Path) -> Path:
    return _copy_fixture("dataset", destination)


def _copy_fixture(name: str, destination: Path) -> Path:
    return _copy_fixture_at(name, destination, name)


def _copy_fixture_at(name: str, destination: Path, target_name: str) -> Path:
    target = destination / "Knowledge Base" / "Records" / target_name
    shutil.copytree(FIXTURES / name, target)
    return target
