#!/usr/bin/env python3
"""Rebuild Knowledge Base/_Schema.zip (the claude.ai `.skill`) from the canonical.

This is the **personal** derivation of the canonical skill (the maintainer's real
content), for upload to claude.ai — the sibling of scripts/genericize-schema.py
(which derives the *generic* repo scaffold from the same canonical). Both strip the
GENERIC markers; this one keeps the REAL side, the genericize script keeps the
generic side.

So the canonical `_Schema/` stays the single editable source — its inline
`<!-- GENERIC-START ... GENERIC-REPLACE --> <real> <!-- GENERIC-END -->` regions
let one file serve both the personal zip (real content, markers removed) and the
generic scaffold (generic content) without hand-maintaining either.

Cross-platform (pure stdlib; no `zip` CLI / Compress-Archive). SKILL.md lands at the
archive root, mirroring the on-disk skill layout claude.ai expects.

Usage: python scripts/rebuild-schema-zip.py [--vault <root>]
  --vault   vault root containing "Knowledge Base/" (default: $KB_MCP_VAULT_PATH)
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import zipfile
from pathlib import Path


def strip_markers_keep_real(text: str) -> str:
    """Drop GENERIC marker scaffolding + the generic replacement, KEEP the real
    content (the inverse of genericize-schema.strip_markers, which keeps generic)."""
    out: list[str] = []
    state = "normal"
    for line in text.splitlines(keepends=True):
        s = line.strip()
        if state == "normal":
            if s.startswith("<!-- GENERIC-START"):
                state = "repl"
            elif s.startswith("<!-- GENERIC-END"):
                pass  # stray end — drop
            else:
                out.append(line)
        elif state == "repl":
            if s.endswith("GENERIC-REPLACE -->"):
                state = "real"
            # else: drop the generic replacement line
        elif state == "real":
            if s.startswith("<!-- GENERIC-END"):
                state = "normal"
            else:
                out.append(line)  # keep the real personal content
    if state != "normal":
        raise SystemExit(f"rebuild-schema-zip: unbalanced GENERIC markers (ended in state '{state}')")
    return "".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(prog="rebuild-schema-zip")
    ap.add_argument("--vault", help="vault root containing 'Knowledge Base/' (default: $KB_MCP_VAULT_PATH)")
    args = ap.parse_args()

    vault = args.vault or os.environ.get("KB_MCP_VAULT_PATH")
    if not vault:
        print("rebuild-schema-zip: set --vault or KB_MCP_VAULT_PATH (vault root containing 'Knowledge Base/').", file=sys.stderr)
        return 2
    kb = Path(vault).expanduser() / "Knowledge Base"
    canon = kb / "_Schema"
    zip_path = kb / "_Schema.zip"
    if not (canon / "SKILL.md").exists():
        print(f"rebuild-schema-zip: {canon / 'SKILL.md'} not found.", file=sys.stderr)
        return 2

    # Build the personal rendering (markers stripped, real content kept).
    files: dict[str, str] = {"SKILL.md": strip_markers_keep_real((canon / "SKILL.md").read_text(encoding="utf-8"))}
    for ref in sorted((canon / "references").glob("*.md")):
        files[f"references/{ref.name}"] = strip_markers_keep_real(ref.read_text(encoding="utf-8"))
    keys = canon / "project-keys.yaml"
    if keys.exists():
        files["project-keys.yaml"] = keys.read_text(encoding="utf-8")  # real keys, verbatim

    version = ""
    m = re.search(r"(?m)^\s*version:\s*([0-9]+\.[0-9]+\.[0-9]+)", files["SKILL.md"])
    if m:
        version = m.group(1)

    print(f"vault:      {vault}")
    print(f"schema dir: {canon}")
    print(f"zip target: {zip_path}")
    print(f"version:    {version}" if version else "warning: could not parse version from SKILL.md frontmatter.")

    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for arcname, content in files.items():
            z.writestr(arcname, content)

    size_kb = zip_path.stat().st_size // 1024
    print(f"wrote {zip_path} ({size_kb} KB, {len(files)} files; GENERIC markers stripped).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
