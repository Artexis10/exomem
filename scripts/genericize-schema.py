#!/usr/bin/env python3
"""Generate the shipped generic skill scaffold from the vault canonical.

`src/exomem/_scaffold/_Schema/` is a DERIVED artifact — the genericized, shippable
copy of the skill whose canonical source lives in the vault's `_Schema/`. This
script is its build step, the sibling of `scripts/rebuild-schema-zip.*` (which
derives the claude.ai zip from the same canonical). RETIRED as a generator: the
scaffold is now hand-authored (see main()); this module's marker/leak utilities
remain in use by rebuild-schema-zip.py and the tests.

Genericization, in order:
1. Block markers — strip personal *regions* the author wants to keep in the
   canonical but not ship. In the canonical:

      <!-- GENERIC-START
      <generic replacement lines — shipped in the generated output>
      GENERIC-REPLACE -->
      <real personal lines — kept in the canonical, dropped from output>
      <!-- GENERIC-END -->

   The replacement sits inside the opening comment, so it is invisible in Obsidian
   while the real content renders normally. An empty replacement omits the block.
2. Substitutions — swap scattered personal *tokens* (e.g. a real tenant key) for
   generic ones, from scripts/generic/substitutions.txt (gitignored), one
   `real => generic` per line. Keeps the real token in the canonical.
3. project-keys.yaml — replaced wholesale by the generic template (scripts/generic/).

Leak-guard (aborts before writing on any hit), two layers:
- Committed pattern guard: machine-path shapes etc. — catches the common leak in
  CI/tests without committing sensitive literals.
- Optional local denylist (scripts/generic/leakguard.txt, gitignored) plus the
  left-hand side of every substitution — exact personal tokens that must not
  survive into the output.

Usage: python scripts/genericize-schema.py [--vault <root>] [--check]
  --vault   vault root containing "Knowledge Base/" (default: $EXOMEM_VAULT_PATH)
  --check   dry run: run the guard, report, write nothing.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCAFFOLD = REPO / "src" / "exomem" / "_scaffold" / "_Schema"
GENERIC_KEYS = REPO / "scripts" / "generic" / "project-keys.yaml"
LOCAL_DENYLIST = REPO / "scripts" / "generic" / "leakguard.txt"
LOCAL_SUBS = REPO / "scripts" / "generic" / "substitutions.txt"

# Committed pattern guard — structural personal data that must never ship.
# Patterns, NOT sensitive literals, so this file itself stays clean. Keep in sync
# with tests/test_scaffold_no_leak.py (it imports this list).
LEAK_PATTERNS = [
    r"/mnt/[a-z]/",                                              # WSL drive mounts
    r"[A-Za-z]:\\Users\\(?!<)",                                  # Windows user paths (real, not <placeholder>)
    r"[A-Za-z]:\\[A-Za-z0-9 _-]+\\(?:Personal|Archive|Documents)",  # Windows abs paths
    r"/Users/(?!<)[^/\s<]+/",                                    # macOS home (real, not <placeholder>)
    r"/home/(?!<)[^/\s<]+/",                                     # Linux home (real, not <placeholder>)
    r"~/\.claude/hooks/",                                        # author's hook wiring
    r"\bQ_MNT_ALLOWLIST\b",                                      # author's allowlist env
]


def strip_markers(text: str) -> str:
    """Replace each GENERIC-marked personal region with its generic replacement."""
    out: list[str] = []
    repl: list[str] = []
    state = "normal"
    for line in text.splitlines(keepends=True):
        s = line.strip()
        if state == "normal":
            if s.startswith("<!-- GENERIC-START"):
                state, repl = "repl", []
            else:
                out.append(line)
        elif state == "repl":
            if s.endswith("GENERIC-REPLACE -->"):
                out.extend(repl)
                state = "real"
            else:
                repl.append(line)
        elif state == "real":
            if s.startswith("<!-- GENERIC-END"):
                state = "normal"
            # else: drop the real personal line
    if state != "normal":
        raise SystemExit(f"genericize: unbalanced GENERIC markers (ended in state '{state}')")
    return "".join(out)


def load_subs() -> list[tuple[str, str]]:
    if not LOCAL_SUBS.exists():
        return []
    subs = []
    for ln in LOCAL_SUBS.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=>" not in ln:
            continue
        real, generic = ln.split("=>", 1)
        subs.append((real.strip(), generic.strip()))
    return subs


def apply_subs(text: str, subs: list[tuple[str, str]]) -> str:
    for real, generic in subs:
        text = text.replace(real, generic)
    return text


def load_denylist() -> list[str]:
    if not LOCAL_DENYLIST.exists():
        return []
    toks = []
    for ln in LOCAL_DENYLIST.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            toks.append(ln)
    return toks


def leak_scan(files: dict[str, str], denylist: list[str]) -> list[str]:
    pats = [re.compile(p) for p in LEAK_PATTERNS]
    hits: list[str] = []
    for name, content in files.items():
        for i, line in enumerate(content.splitlines(), 1):
            for p in pats:
                if p.search(line):
                    hits.append(f"{name}:{i}: pattern /{p.pattern}/ -> {line.strip()[:80]}")
            for tok in denylist:
                if tok and tok in line:
                    hits.append(f"{name}:{i}: token '{tok}' -> {line.strip()[:80]}")
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(prog="genericize-schema")
    ap.add_argument("--vault", help="vault root containing 'Knowledge Base/' (default: $EXOMEM_VAULT_PATH)")
    ap.add_argument("--check", action="store_true", help="dry run: guard only, write nothing")
    ap.add_argument("--force", action="store_true", help="regenerate anyway (RETIRED — clobbers the hand-authored scaffold)")
    args = ap.parse_args()

    # RETIRED as a generator: the shipped scaffold at src/exomem/_scaffold/_Schema/
    # is now HAND-AUTHORED (a lean, deliberately-generic starter) and guarded by
    # tests/test_scaffold_no_leak.py — it is no longer derived from a vault
    # canonical, so regenerating would overwrite it. The marker/leak utilities in
    # this module live on (used by rebuild-schema-zip.py + the tests).
    if not args.check and not args.force:
        print(
            "genericize-schema: RETIRED as a generator. The scaffold at "
            "src/exomem/_scaffold/_Schema/ is hand-authored and leak-guarded "
            "(tests/test_scaffold_no_leak.py) — edit it directly, keep it generic. "
            "Use --check to dry-run the leak scan, or --force to regenerate anyway.",
            file=sys.stderr,
        )
        return 2

    vault = args.vault or os.environ.get("EXOMEM_VAULT_PATH")
    if not vault:
        print("genericize: set --vault or EXOMEM_VAULT_PATH (vault root containing 'Knowledge Base/').", file=sys.stderr)
        return 2
    canon = Path(vault).expanduser() / "Knowledge Base" / "_Schema"
    if not (canon / "SKILL.md").exists():
        print(f"genericize: {canon / 'SKILL.md'} not found.", file=sys.stderr)
        return 2
    if not GENERIC_KEYS.exists():
        print(f"genericize: missing generic template {GENERIC_KEYS}.", file=sys.stderr)
        return 2

    subs = load_subs()

    def generic(text: str) -> str:
        return apply_subs(strip_markers(text), subs)

    outputs: dict[str, str] = {"SKILL.md": generic((canon / "SKILL.md").read_text(encoding="utf-8"))}
    for ref in sorted((canon / "references").glob("*.md")):
        outputs[f"references/{ref.name}"] = generic(ref.read_text(encoding="utf-8"))
    outputs["project-keys.yaml"] = GENERIC_KEYS.read_text(encoding="utf-8")

    # Leak-guard BEFORE writing. Denylist = explicit tokens + every substitution's
    # real side (so an un-substituted variant can't slip through).
    denylist = load_denylist() + [real for real, _ in subs]
    hits = leak_scan(outputs, denylist)
    if hits:
        print("genericize: LEAK-GUARD FAILED — personal data would ship:", file=sys.stderr)
        for h in hits:
            print(f"  {h}", file=sys.stderr)
        print("Fix: wrap in <!-- GENERIC-START ... --> markers in the canonical, add a "
              "substitution to scripts/generic/substitutions.txt, or genericize in place.", file=sys.stderr)
        return 1

    if args.check:
        print(f"genericize --check: leak-guard passed; {len(outputs)} files ready (nothing written).")
        return 0

    # Write the scaffold as a clean mirror of the canonical structure.
    shutil.rmtree(SCAFFOLD / "references", ignore_errors=True)
    for name, content in outputs.items():
        dest = SCAFFOLD / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
    print(f"genericize: wrote {len(outputs)} files to {SCAFFOLD} (leak-guard passed).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
