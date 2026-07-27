#!/usr/bin/env python
"""Point the hosted plugin definition's `source_release` at the package version.

Release Please bumps `pyproject.toml`, but the hosted plugin definition pins
`source_release` separately and `hosted_plugins.compatibility_manifest` hard
-fails when the two diverge. That alone would be a job for a Release Please
`extra-files` updater; it is not, because the generated descriptors under
`plugins/hosted/generated/` carry sha256 locks over the definition and the
compatibility manifest. No config-file updater can recompute those, so the
release branch has to run the real generator.

This script does the version half. `hosted-plugin.py regenerate` does the rest,
and refuses to run until this has happened -- the release guard also blocks its
own remedy, so the order matters.

Idempotent: writes nothing and reports no change when already in sync. The
replacement is textual and anchored to the key so the file's formatting (and
therefore its sha256, once regenerated) stays under the generator's control
rather than json.dumps'.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path
from typing import NamedTuple

DEFINITION_PATH = Path("plugins/hosted/definition.json")
PYPROJECT_PATH = Path("pyproject.toml")
_SOURCE_RELEASE = re.compile(r'("source_release"\s*:\s*")([^"]*)(")')


class SyncResult(NamedTuple):
    version: str
    previous: str
    changed: bool


def package_version(repo_root: Path) -> str:
    with (repo_root / PYPROJECT_PATH).open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def sync(repo_root: Path) -> SyncResult:
    """Rewrite `source_release` to the package version. Returns what happened."""

    version = package_version(repo_root)
    definition = repo_root / DEFINITION_PATH
    text = definition.read_text(encoding="utf-8")

    match = _SOURCE_RELEASE.search(text)
    if match is None:
        raise SystemExit(f"no source_release key in {DEFINITION_PATH}")
    previous = match.group(2)
    if previous == version:
        return SyncResult(version, previous, False)

    # count=1: the key appears once, and a stray match elsewhere should not be
    # silently rewritten too.
    updated = _SOURCE_RELEASE.sub(rf"\g<1>{version}\g<3>", text, count=1)
    definition.write_text(updated, encoding="utf-8")
    return SyncResult(version, previous, True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root (defaults to this script's repository)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report drift without writing; exit 1 when out of sync",
    )
    args = parser.parse_args(argv)

    if args.check:
        version = package_version(args.repo_root)
        text = (args.repo_root / DEFINITION_PATH).read_text(encoding="utf-8")
        match = _SOURCE_RELEASE.search(text)
        if match is None:
            raise SystemExit(f"no source_release key in {DEFINITION_PATH}")
        if match.group(2) != version:
            print(
                f"source_release is {match.group(2)}, package version is {version}; "
                "run scripts/sync_hosted_release.py then "
                "scripts/hosted-plugin.py regenerate --platform claude",
                file=sys.stderr,
            )
            return 1
        print(f"source_release is in sync at {version}")
        return 0

    result = sync(args.repo_root)
    if result.changed:
        print(f"source_release {result.previous} -> {result.version}")
    else:
        print(f"source_release already at {result.version}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
