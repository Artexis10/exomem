#!/usr/bin/env python3
"""Rebuild the maintainer's claude.ai `.skill` zip from the public scaffold.

Thin wrapper over `exomem package-skills` (src/exomem/package_skills.py) so there is
exactly ONE implementation of the archive payload. This script used to carry its own
copy, which is precisely the drift hazard the packaging module exists to remove.

The public scaffold `src/exomem/_scaffold/_Schema/` is the single source of the skill.
SKILL.md and references ship verbatim; the maintainer's real `project-keys.yaml` is
overlaid when a vault is given, so the personal upload advertises real project scopes.
SKILL.md lands at the archive root, mirroring the layout claude.ai expects.

Prefer `exomem package-skills` directly: it builds ALL ten skills, not just the core
one. This wrapper remains for the documented maintainer flow and single-file --out.

Usage: python scripts/rebuild-schema-zip.py [--vault <root>] [--out <path>]
  --vault  explicit vault root containing "Knowledge Base/"
  --out    output zip path (default: <vault>/Knowledge Base/_Schema.zip when --vault
           is given, else <repo>/dist/_Schema.zip)
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from uuid import uuid4

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from exomem import package_skills as package_module  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(prog="rebuild-schema-zip")
    ap.add_argument("--vault", help="vault root containing 'Knowledge Base/'")
    ap.add_argument("--out", help="output zip path")
    args = ap.parse_args()

    vault = args.vault

    if args.out:
        zip_path = Path(args.out).expanduser()
    elif vault:
        zip_path = Path(vault).expanduser() / "Knowledge Base" / "_Schema.zip"
    else:
        zip_path = REPO / "dist" / "_Schema.zip"

    if vault:
        try:
            package_module.ensure_personalized_output(zip_path, vault=Path(vault))
        except ValueError as e:
            print(f"rebuild-schema-zip: {e}", file=sys.stderr)
            return 2

    zip_path.parent.mkdir(parents=True, exist_ok=True)
    scratch = zip_path.parent / f".exomem-package-build-{uuid4().hex}"
    scratch.mkdir()
    try:
        try:
            report = package_module.package_skills(
                scratch, vault=Path(vault) if vault else None
            )
        except FileNotFoundError as e:
            print(f"rebuild-schema-zip: {e}", file=sys.stderr)
            return 2
        built = scratch / "exomem.zip"
        shutil.copyfile(built, zip_path)
    finally:
        shutil.rmtree(scratch)

    print(f"keys:       {'vault overlay' if vault else 'scaffold (generic)'}")
    print(f"zip target: {zip_path}")
    print(f"wrote {zip_path} ({zip_path.stat().st_size // 1024} KB) from the public scaffold.")
    print(f"note: `exomem package-skills` builds all {report['count']} skills, not just this one.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
