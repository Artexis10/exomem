from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from exomem.governance import authorization_hosted_mount

_FILES = {
    "keyring.json": b'{"keyring":"sentinel"}',
    "control.json": b'{"control":"sentinel"}',
    "serving-membership.json": b'{"membership":"sentinel"}',
}


def _projected_secret(root: Path) -> None:
    root.mkdir(mode=0o700)
    generation = root / "..2026_08_28_00_00_00.000000001"
    generation.mkdir(mode=0o700)
    for name, payload in _FILES.items():
        target = generation / name
        target.write_bytes(payload)
        target.chmod(0o440)
    (root / "..data").symlink_to(generation.name, target_is_directory=True)
    for name in _FILES:
        (root / name).symlink_to(f"..data/{name}")


def test_copy_projected_hosted_custody_publishes_owner_only_files(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    _projected_secret(source)
    destination.mkdir(mode=0o700)

    authorization_hosted_mount.copy_projected_custody(source, destination)

    assert {path.name for path in destination.iterdir()} == set(_FILES)
    for name, payload in _FILES.items():
        copied = destination / name
        assert copied.read_bytes() == payload
        assert stat.S_IMODE(copied.stat().st_mode) == 0o600
        assert copied.stat().st_uid == os.geteuid()


@pytest.mark.parametrize("mutation", ["extra", "leaf", "generation"])
def test_copy_projected_hosted_custody_rejects_ambiguous_sources(
    tmp_path: Path,
    mutation: str,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    _projected_secret(source)
    destination.mkdir(mode=0o700)
    if mutation == "extra":
        (source / "unexpected").write_text("no", encoding="utf-8")
    elif mutation == "leaf":
        (source / "keyring.json").unlink()
        (source / "keyring.json").write_text("no", encoding="utf-8")
    else:
        (source / "..data").unlink()
        (source / "..data").symlink_to("../outside", target_is_directory=True)

    with pytest.raises(authorization_hosted_mount.HostedCustodyMountUnavailable):
        authorization_hosted_mount.copy_projected_custody(source, destination)

    assert list(destination.iterdir()) == []


def test_copy_projected_hosted_custody_cleans_a_partial_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    _projected_secret(source)
    destination.mkdir(mode=0o700)
    replace = authorization_hosted_mount.os.replace
    calls = 0

    def fail_second(source_path: Path, target_path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected publication failure")
        replace(source_path, target_path)

    monkeypatch.setattr(authorization_hosted_mount.os, "replace", fail_second)

    with pytest.raises(authorization_hosted_mount.HostedCustodyMountUnavailable):
        authorization_hosted_mount.copy_projected_custody(source, destination)

    assert list(destination.iterdir()) == []
