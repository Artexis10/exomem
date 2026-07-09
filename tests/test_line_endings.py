from __future__ import annotations

import subprocess

import pytest


def test_lf_governed_files_are_normalized_in_git_index() -> None:
    result = subprocess.run(
        ["git", "ls-files", "--eol"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip("git metadata is unavailable")

    offenders: list[str] = []
    for line in result.stdout.splitlines():
        metadata, _, path = line.partition("\t")
        fields = metadata.split()
        if "eol=lf" not in fields:
            continue

        index_eol = next((field for field in fields if field.startswith("i/")), "")
        if index_eol in {"i/crlf", "i/mixed"}:
            offenders.append(f"{path} ({index_eol})")

    assert offenders == []
