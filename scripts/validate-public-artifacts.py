#!/usr/bin/env python
"""Validate public repository inputs and unpacked/generated artifact content."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = REPO_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from exomem.public_artifact_privacy import (  # noqa: E402
    PublicArtifactPrivacyError,
    assert_public_artifacts_clean,
    scan_repository_inputs,
)


def _artifact_files(paths: list[Path]) -> list[Path]:
    files: set[Path] = set()
    for path in paths:
        if path.is_symlink():
            files.add(path)
        elif path.is_dir():
            files.update(
                candidate
                for candidate in path.rglob("*")
                if candidate.is_symlink() or not candidate.is_dir()
            )
        else:
            try:
                path.lstat()
            except OSError as error:
                raise FileNotFoundError(
                    f"public artifact path does not exist: {path}"
                ) from error
            files.add(path)
    return sorted(files)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repository",
        action="store_true",
        help="scan the complete declared public repository-input inventory",
    )
    parser.add_argument("artifacts", nargs="*", type=Path)
    args = parser.parse_args()
    if not args.repository and not args.artifacts:
        parser.error("select --repository and/or one or more artifact paths")

    try:
        if args.repository:
            report = scan_repository_inputs(REPO_ROOT)
            if report.findings:
                diagnostics = "\n".join(str(item) for item in report.findings)
                raise PublicArtifactPrivacyError(
                    "public repository privacy validation failed "
                    f"({len(report.findings)} findings):\n{diagnostics}"
                )
            print(
                "public repository inputs clean: "
                f"{report.scanned_files} files, {report.scanned_text_files} text payloads"
            )

        if args.artifacts:
            files = _artifact_files(args.artifacts)
            labels = {
                path: (
                    path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
                    if REPO_ROOT.resolve() in path.resolve().parents
                    else path.name
                )
                for path in files
            }
            assert_public_artifacts_clean(files, labels=labels)
            print(f"generated public artifacts clean: {len(files)} files")
    except (FileNotFoundError, PublicArtifactPrivacyError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
