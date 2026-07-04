#!/usr/bin/env python3
"""Rebuild the claude.ai `.skill` zip from the public scaffold.

The public scaffold `src/exomem/_scaffold/_Schema/` is the single source of the skill.
This assembles the maintainer's claude.ai `.skill` archive from it — SKILL.md +
references verbatim — overlaying the maintainer's real `project-keys.yaml` when a vault
is given (so the personal upload advertises real project scopes), otherwise shipping the
scaffold's generic starter keys. SKILL.md lands at the archive root, mirroring the
on-disk skill layout claude.ai expects.

There is no longer a private marker-canonical: personal specifics live in the
maintainer's own `project-keys.yaml` / `_access.yaml`, not a forked SKILL.md.

Cross-platform (pure stdlib; no `zip` CLI / Compress-Archive).

Usage: python scripts/rebuild-schema-zip.py [--vault <root>] [--out <path>]
  --vault  vault root containing "Knowledge Base/" — used only to overlay your real
           project-keys.yaml and to default --out (default: $EXOMEM_VAULT_PATH)
  --out    output zip path (default: <vault>/Knowledge Base/_Schema.zip when --vault is
           given, else <repo>/dist/_Schema.zip)
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCAFFOLD = REPO / "src" / "exomem" / "_scaffold" / "_Schema"


def main() -> int:
    ap = argparse.ArgumentParser(prog="rebuild-schema-zip")
    ap.add_argument("--vault", help="vault root containing 'Knowledge Base/' (default: $EXOMEM_VAULT_PATH)")
    ap.add_argument("--out", help="output zip path (default: <vault>/Knowledge Base/_Schema.zip, else <repo>/dist/_Schema.zip)")
    args = ap.parse_args()

    if not (SCAFFOLD / "SKILL.md").exists():
        print(f"rebuild-schema-zip: {SCAFFOLD / 'SKILL.md'} not found.", file=sys.stderr)
        return 2

    vault = args.vault or os.environ.get("EXOMEM_VAULT_PATH")

    # SKILL.md + references come verbatim from the public scaffold.
    files: dict[str, str] = {"SKILL.md": (SCAFFOLD / "SKILL.md").read_text(encoding="utf-8")}
    for ref in sorted((SCAFFOLD / "references").glob("*.md")):
        files[f"references/{ref.name}"] = ref.read_text(encoding="utf-8")

    # project-keys.yaml: overlay the maintainer's real keys when a vault is given,
    # else ship the scaffold's generic starter.
    real_keys = (
        Path(vault).expanduser() / "Knowledge Base" / "_Schema" / "project-keys.yaml"
        if vault
        else None
    )
    if real_keys is not None and real_keys.exists():
        files["project-keys.yaml"] = real_keys.read_text(encoding="utf-8")
        keys_source = str(real_keys)
    else:
        files["project-keys.yaml"] = (SCAFFOLD / "project-keys.yaml").read_text(encoding="utf-8")
        keys_source = f"{SCAFFOLD / 'project-keys.yaml'} (generic)"

    version = ""
    m = re.search(r"(?m)^\s*version:\s*([0-9]+\.[0-9]+\.[0-9]+)", files["SKILL.md"])
    if m:
        version = m.group(1)

    if args.out:
        zip_path = Path(args.out).expanduser()
    elif vault:
        zip_path = Path(vault).expanduser() / "Knowledge Base" / "_Schema.zip"
    else:
        zip_path = REPO / "dist" / "_Schema.zip"

    print(f"scaffold:   {SCAFFOLD}")
    print(f"keys:       {keys_source}")
    print(f"zip target: {zip_path}")
    print(f"version:    {version}" if version else "warning: could not parse version from SKILL.md frontmatter.")

    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for arcname, content in files.items():
            z.writestr(arcname, content)

    size_kb = zip_path.stat().st_size // 1024
    print(f"wrote {zip_path} ({size_kb} KB, {len(files)} files) from the public scaffold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
