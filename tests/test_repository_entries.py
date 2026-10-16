from __future__ import annotations

import subprocess

import pytest


def test_repository_tracks_no_symlinks_or_submodules() -> None:
    # The Claude plugin directory scanner refuses to unpack a commit that holds either.
    result = subprocess.run(
        ["git", "ls-files", "--stage"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip("git metadata is unavailable")

    offenders: list[str] = []
    for line in result.stdout.splitlines():
        metadata, _, path = line.partition("\t")
        mode = metadata.split(" ", 1)[0]
        if mode in {"120000", "160000"}:
            offenders.append(f"{path} (mode {mode})")

    assert offenders == []
