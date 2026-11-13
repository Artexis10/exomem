"""Refuse a release PR that carries changes release-please did not make.

The release PR runs no CI test job (`.github/workflows/ci.yml`). That is sound
only while its diff is release-please's own work: its manifest, the changelog,
the version lines of the configured extra files, and the Cloud packages that
the fixed-source controller regenerates. Any other change must land through
its own pull request, which runs the test jobs.

JSON values must match the pinned Release Please updaters. An allowed path
never permits arbitrary content.

Usage: check_release_pr_files.py <release-please-config.json> <pr-files.jsonl>
Each line of the second file is one entry of GitHub's "list pull request files"
response.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
import tomllib
from pathlib import Path

#: release-please bumps its manifest in every release PR.
_MANIFEST = ".release-please-manifest.json"
#: Files release-please itself writes besides the configured extra files.
_RELEASE_PLEASE_FILES = frozenset({_MANIFEST, "CHANGELOG.md"})
#: The directories that the fixed-source controller regenerates.
_REGENERATED_PACKAGES = ("plugins/cloud/generated/", "plugins/claude-code/")
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
            if len(statement.targets) != 1 or not isinstance(
                target := statement.targets[0], ast.Name
            ):
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


def version_updates(config: dict, files: dict[str, str], version: str) -> dict[str, str]:
    result = subprocess.run(
        ("node", str(Path(__file__).with_name("release_metadata.cjs"))),
        input=json.dumps({"config": config, "files": files, "version": version}),
        text=True,
        capture_output=True,
        check=True,
        timeout=60,
    )
    return json.loads(result.stdout)


def unreleased_changes(config: dict, files: list[dict]) -> list[str]:
    """Each change in `files` that release-please did not make, as a reason."""
    extra = _extra_files(config)
    managed = _RELEASE_PLEASE_FILES | {entry["path"] for entry in extra}
    generic = {entry["path"] for entry in extra if entry.get("type") == "generic"}
    json_files = {_MANIFEST} | {entry["path"] for entry in extra if entry.get("type") == "json"}
    problems = []
    manifest = next((file for file in files if file["filename"] == _MANIFEST), {})
    expected = {}
    if "after" in manifest:
        try:
            expected = version_updates(
                config,
                {
                    file["filename"]: file["before"]
                    for file in files
                    if file["filename"] in managed and "before" in file
                },
                json.loads(manifest["after"])["."],
            )
        except (KeyError, ValueError, subprocess.CalledProcessError) as error:
            problems.append(f"configured version updates cannot be verified: {error}")
    if not any(file["filename"] == _MANIFEST for file in files):
        # An empty or partial listing is an unexpected state, not a clean PR.
        problems.append(f"{_MANIFEST}: the release PR does not change it")
    for file in files:
        for path in filter(None, (file["filename"], file.get("previous_filename"))):
            if not (path in managed or path.startswith(_REGENERATED_PACKAGES)):
                problems.append(f"{path}: release-please does not manage this file")
        if file["filename"] in json_files:
            try:
                if json.loads(expected[file["filename"]]) != json.loads(file["after"]):
                    raise ValueError("undeclared value changed")
            except (KeyError, ValueError, StopIteration, subprocess.CalledProcessError) as error:
                problems.append(
                    f"{file['filename']}: cannot verify only configured versions: {error}"
                )
        if file["filename"] not in generic:
            continue
        if "after" in file and file.get("after") != expected.get(file["filename"]):
            problems.append(f"{file['filename']}: differs from the selected version update")
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("listing", type=Path)
    parser.add_argument("--base")
    parser.add_argument("--head")
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    files = [
        json.loads(line)
        for line in args.listing.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if args.base and args.head:
        managed = {_MANIFEST} | {entry["path"] for entry in _extra_files(config)}
        for file in files:
            if file["filename"] in managed:
                for key, ref in (("before", args.base), ("after", args.head)):
                    file[key] = subprocess.check_output(
                        ("git", "show", f"{ref}:{file['filename']}"),
                        text=True,
                        timeout=30,
                    )
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
