"""Preview the actual diff that normalize_vault_wikilinks.py would apply
to a few sample files. No writes."""

from __future__ import annotations

import difflib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from exomem.vault import WikilinkResolver, resolve_vault  # noqa: E402
from normalize_vault_wikilinks import normalize_file, walk_kb_md  # noqa: E402


def main() -> int:
    vault_root = resolve_vault()
    kb = vault_root / "Knowledge Base"
    resolver = WikilinkResolver(vault_root)
    sample_paths_arg = sys.argv[1:] if len(sys.argv) > 1 else []
    if sample_paths_arg:
        targets = [vault_root / p for p in sample_paths_arg]
    else:
        # Take the first 3 changed files we encounter.
        targets = []
        for md in walk_kb_md(kb):
            new_text, _warns = normalize_file(md, vault_root, resolver)
            if new_text is not None:
                targets.append(md)
                if len(targets) >= 3:
                    break

    for md in targets:
        original = md.read_text(encoding="utf-8")
        new_text, warns = normalize_file(md, vault_root, resolver)
        if new_text is None:
            print(f"=== {md.relative_to(vault_root)}: NO CHANGE ===\n")
            continue
        print(f"=== {md.relative_to(vault_root)} ===")
        diff = list(difflib.unified_diff(
            original.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile="before",
            tofile="after",
            n=1,
        ))
        # Print only the +/- lines + headers, skip context noise.
        for line in diff:
            if line.startswith(("---", "+++", "@@", "+", "-")):
                print(line, end="")
        if warns:
            print("  warnings:")
            for w in warns[:5]:
                print(f"    - {w}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
