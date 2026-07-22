#!/usr/bin/env python
"""Generate the committed, wholly synthetic public-privacy fixture corpus."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = REPO_ROOT / "tests" / "privacy_fixtures" / "public_artifact_privacy"


def render_corpus() -> dict[str, str]:
    """Return deterministic fixtures without reading environment or vault state."""

    manifest = {
        "provenance": "committed synthetic generator",
        "live_vault_input": False,
        "records": ["Notes/Research/Project Lantern/generic-cache-study.md"],
    }
    note = """---
type: research-note
status: active
created: 2026-01-15
---

# Generic cache study

## Observations

- [latency] A synthetic cache reduced the invented fixture's lookup time. #benchmark

"""
    return {
        "generic-note.md": note,
        "manifest.json": json.dumps(manifest, indent=2) + "\n",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = render_corpus()

    if args.check:
        current = {
            str(path.relative_to(OUTPUT_ROOT)).replace("\\", "/"): path.read_text(
                encoding="utf-8"
            )
            for path in sorted(OUTPUT_ROOT.rglob("*"))
            if path.is_file()
        }
        return 0 if current == rendered else 1

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    for relative_path, content in rendered.items():
        target = OUTPUT_ROOT / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
