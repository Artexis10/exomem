"""The release PR runs no test job, so it may carry only release-please's work.

A release PR once carried a hand commit to an E2E script. With the test jobs
skipped on the release PR, that change would have merged with a green gate.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
_spec = importlib.util.spec_from_file_location(
    "check_release_pr_files", ROOT / "scripts" / "check_release_pr_files.py"
)
check_release_pr_files = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_release_pr_files)

CONFIG = {
    "packages": {
        ".": {
            "extra-files": [
                {"type": "generic", "path": "pyproject.toml"},
                "src/exomem/__init__.py",
                {"type": "json", "path": "server.json", "jsonpath": "$.version"},
            ]
        }
    }
}

VERSION_BUMP = (
    "@@ -1,6 +1,6 @@\n"
    " [project]\n"
    ' name = "exomem"\n'
    '-version = "1.2.3" # x-release-please-version\n'
    '+version = "1.3.0" # x-release-please-version\n'
    ' description = "example"\n'
)


def _check(tmp_path: Path, files: list[dict], capsys: pytest.CaptureFixture) -> tuple[int, str]:
    config = tmp_path / "release-please-config.json"
    config.write_text(json.dumps(CONFIG), encoding="utf-8")
    listing = tmp_path / "pr-files.jsonl"
    listing.write_text("".join(json.dumps(file) + "\n" for file in files), encoding="utf-8")
    status = check_release_pr_files.main([str(config), str(listing)])
    return status, capsys.readouterr().err


def test_a_release_please_pr_passes(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    files = [
        {"filename": ".release-please-manifest.json", "patch": "@@\n-1\n+2\n"},
        {"filename": "CHANGELOG.md", "patch": "@@\n+## 1.3.0\n"},
        {"filename": "pyproject.toml", "patch": VERSION_BUMP},
        {
            "filename": "server.json",
            "patch": '@@\n-  "version": "1.2.3",\n+  "version": "1.3.0",\n',
        },
        {"filename": "plugins/cloud/generated/release.json", "patch": "@@\n-a\n+b\n"},
        {"filename": "plugins/cloud/generated/claude.zip"},
    ]

    status, errors = _check(tmp_path, files, capsys)

    assert status == 0, errors


def test_a_hand_commit_to_another_file_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    files = [
        {"filename": ".release-please-manifest.json", "patch": "@@\n-1\n+2\n"},
        {"filename": "scripts/e2e_product_loop.py", "patch": "@@\n-old\n+new\n"},
    ]

    status, errors = _check(tmp_path, files, capsys)

    assert status == 1
    assert "scripts/e2e_product_loop.py" in errors


def test_a_rename_out_of_an_unmanaged_path_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    files = [{"filename": "CHANGELOG.md", "previous_filename": "docs/notes.md"}]

    status, errors = _check(tmp_path, files, capsys)

    assert status == 1
    assert "docs/notes.md" in errors


def test_a_non_version_edit_inside_a_versioned_file_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    dependency_edit = VERSION_BUMP + '+dependencies = ["example>=2"]\n'
    files = [{"filename": "pyproject.toml", "patch": dependency_edit}]

    status, errors = _check(tmp_path, files, capsys)

    assert status == 1
    assert "pyproject.toml: changes a line other than its version" in errors


def test_a_versioned_file_without_a_diff_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    files = [{"filename": "src/exomem/__init__.py"}]

    status, errors = _check(tmp_path, files, capsys)

    assert status == 1
    assert "no diff to verify" in errors


def test_a_listing_without_the_manifest_bump_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    status, errors = _check(tmp_path, [], capsys)

    assert status == 1
    assert ".release-please-manifest.json: the release PR does not change it" in errors
