"""Refuse a release PR that carries changes release-please did not make.

The release PR runs no CI test job (`.github/workflows/ci.yml`). That is sound
only while its diff is release-please's own work: its manifest, the changelog,
the version lines of the configured extra files, and the Cloud packages that
`release-please.yml`'s sync job regenerates. Any other change must land through
its own pull request, which runs the test jobs.

A JSON extra file is checked per file, because release-please rewrites it
through its JSON updater; this repository's one (`server.json`) is registry
metadata that no runtime code reads.

Usage: check_release_pr_files.py <release-please-config.json> <pr-files.jsonl>
Each line of the second file is one entry of GitHub's "list pull request files"
response.
"""

from __future__ import annotations

import ast
import json
import sys
import tomllib
from pathlib import Path

#: release-please bumps its manifest in every release PR.
_MANIFEST = ".release-please-manifest.json"
#: Files release-please itself writes besides the configured extra files.
_RELEASE_PLEASE_FILES = frozenset({_MANIFEST, "CHANGELOG.md"})
#: The directory that release-please.yml's sync-cloud-artifacts job commits.
_REGENERATED_CLOUD_PACKAGES = "plugins/cloud/generated/"
#: release-please's generic updater rewrites only lines that carry this marker.
_VERSION_MARKER = "x-release-please-version"


def _extra_files(config: dict) -> list[dict]:
    extra = []
    for package in config.get("packages", {}).values():
        for entry in package.get("extra-files", []):
            extra.append({"path": entry, "type": "generic"} if isinstance(entry, str) else entry)
    return extra


def _version_value(path: str, line: str) -> str | None:
    """Parse the version assignment, never code beside a version marker."""
    try:
        if Path(path).suffix == ".py":
            statements = ast.parse(line).body
            if len(statements) != 1 or not isinstance(statement := statements[0], ast.Assign):
                return None
            if len(statement.targets) != 1 or not isinstance(target := statement.targets[0], ast.Name):
                return None
            # Python release metadata assigns only this public package attribute.
            if target.id != "__version__" or not isinstance(statement.value, ast.Constant):
                return None
            value = statement.value.value
        else:
            # The configured non-Python generic files use TOML version assignments.
            values = tomllib.loads(line)
            if set(values) != {"version"}:
                return None
            value = values["version"]
    except (SyntaxError, ValueError):
        return None
    return value if isinstance(value, str) else None


def unreleased_changes(config: dict, files: list[dict]) -> list[str]:
    """Each change in `files` that release-please did not make, as a reason."""
    extra = _extra_files(config)
    managed = _RELEASE_PLEASE_FILES | {entry["path"] for entry in extra}
    generic = {entry["path"] for entry in extra if entry.get("type") == "generic"}
    problems = []
    if not any(file["filename"] == _MANIFEST for file in files):
        # An empty or partial listing is an unexpected state, not a clean PR.
        problems.append(f"{_MANIFEST}: the release PR does not change it")
    for file in files:
        for path in filter(None, (file["filename"], file.get("previous_filename"))):
            if not (path in managed or path.startswith(_REGENERATED_CLOUD_PACKAGES)):
                problems.append(f"{path}: release-please does not manage this file")
        if file["filename"] not in generic:
            continue
        patch = file.get("patch")
        if patch is None:
            problems.append(f"{file['filename']}: GitHub returned no diff to verify")
            continue
        lines = patch.splitlines()
        changed = [line for line in lines if line[:1] in ("+", "-")]
        if any(_VERSION_MARKER not in line for line in changed):
            problems.append(f"{file['filename']}: changes a line other than its version")
            continue
        removed = [line[1:] for line in changed if line.startswith("-")]
        added = [line[1:] for line in changed if line.startswith("+")]
        if (
            len(removed) != 1
            or len(added) != 1
            # Adjacent diff lines replace one assignment in place; a moved key changes its scope.
            or lines.index("+" + added[0]) != lines.index("-" + removed[0]) + 1
            or _version_value(file["filename"], removed[0]) is None
            or _version_value(file["filename"], added[0]) is None
        ):
            problems.append(f"{file['filename']}: changes more than its version value")
    return problems


def main(argv: list[str]) -> int:
    config = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    files = [
        json.loads(line)
        for line in Path(argv[1]).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    problems = unreleased_changes(config, files)
    if not problems:
        print(f"the release PR changes only what release-please writes ({len(files)} files)")
        return 0
    print("the release PR carries changes that release-please did not make:", file=sys.stderr)
    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    print(
        "land them through their own pull request, which runs the test jobs",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
