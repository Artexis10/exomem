"""Targeted residual cleanups after normalize_vault_wikilinks.py.

Handles three patterns that the structured normalize can't address because
they require text-substitution semantics:

1. **Trailing backslash typos**: `[[X\]]` where the backslash is a literal
   escape error. If the de-backslashed target resolves, rewrite.
2. **Relative-up paths**: `[[../../../X]]` that should be vault-rooted `[[X]]`.
   Verified against the on-disk target.
3. **Renamed-file references**: explicit (old_target, new_target) pairs
   for files renamed outside the standard `move_file` workflow.

Use --dry-run to preview. Skips fenced code blocks and inline code spans.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from exomem.vault import (  # noqa: E402
    PlannedWrite,
    _WIKILINK_PATTERN,
    _mask_code_spans,
    batch_atomic_write,
    kb_root,
    resolve_vault,
)
from normalize_vault_wikilinks import walk_kb_md  # noqa: E402


def _target_exists(vault_root: Path, target_no_ext: str) -> bool:
    candidate_md = vault_root / (target_no_ext + ".md")
    return candidate_md.exists()


def _rewrite_in_text(text: str, replacements: list[tuple[str, str]]) -> tuple[str, int]:
    """Apply (old_inner, new_inner) wikilink replacements, code-aware.

    Each replacement targets the EXACT content inside `[[...]]`. Matches
    are only made on wikilinks outside fenced code blocks and inline code
    spans (per the same masking logic the audit uses).
    """
    if not replacements:
        return text, 0
    masked = _mask_code_spans(text)
    new_text = text
    total = 0
    matches = list(_WIKILINK_PATTERN.finditer(masked))
    # Walk back-to-front so positions don't shift.
    for m in reversed(matches):
        inner = m.group(1).strip()
        # Preserve alias if present in the actual text.
        actual_full = text[m.start(): m.end()]
        actual_inner = actual_full[2:-2]
        alias = ""
        target_only = actual_inner
        if "|" in actual_inner:
            target_only, alias_part = actual_inner.split("|", 1)
            target_only = target_only.strip()
            alias = "|" + alias_part
        for old_inner, new_inner in replacements:
            if target_only == old_inner:
                replacement = f"[[{new_inner}{alias}]]"
                new_text = new_text[: m.start()] + replacement + new_text[m.end():]
                total += 1
                break
    return new_text, total


def find_trailing_backslash_targets(vault_root: Path) -> list[tuple[str, str]]:
    """Scan vault for `[[X\\]]` matches; return (old, new) pairs that resolve."""
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for md in walk_kb_md(kb_root(vault_root)):
        try:
            text = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        masked = _mask_code_spans(text)
        for m in _WIKILINK_PATTERN.finditer(masked):
            inner = m.group(1).strip()
            if not inner.endswith("\\"):
                continue
            if inner in seen:
                continue
            seen.add(inner)
            stripped = inner.rstrip("\\").rstrip()
            # Strip alias for resolve check.
            target = stripped.split("|", 1)[0].strip()
            if _target_exists(vault_root, target):
                pairs.append((inner, stripped))
    return pairs


def find_relative_up_targets(vault_root: Path) -> list[tuple[str, str]]:
    """Scan vault for `[[../../...]]` refs that resolve when made vault-rooted."""
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for md in walk_kb_md(kb_root(vault_root)):
        try:
            text = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        masked = _mask_code_spans(text)
        for m in _WIKILINK_PATTERN.finditer(masked):
            inner = m.group(1).strip()
            if not inner.startswith("../"):
                continue
            if inner in seen:
                continue
            seen.add(inner)
            # Strip leading `../` segments (vault-rooted form).
            stripped = re.sub(r"^(\.\./)+", "", inner)
            target = stripped.split("|", 1)[0].strip()
            if _target_exists(vault_root, target):
                pairs.append((inner, stripped))
    return pairs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--vault", type=Path, default=None)
    args = parser.parse_args()

    vault_root = args.vault if args.vault else resolve_vault()
    kb = kb_root(vault_root)
    print(f"Vault: {vault_root}")
    print(f"Mode:  {'DRY RUN' if args.dry_run else 'APPLY'}")
    print()

    backslash_pairs = find_trailing_backslash_targets(vault_root)
    print(f"Trailing-backslash targets that resolve when stripped: {len(backslash_pairs)}")
    for old, new in backslash_pairs:
        print(f"  [[{old}]]  ->  [[{new}]]")
    print()

    rel_pairs = find_relative_up_targets(vault_root)
    print(f"Relative-up `../../` targets that resolve when made vault-rooted: {len(rel_pairs)}")
    for old, new in rel_pairs:
        print(f"  [[{old}]]  ->  [[{new}]]")
    print()

    all_pairs = list(backslash_pairs) + list(rel_pairs)
    if not all_pairs:
        print("Nothing to rewrite.")
        return 0

    print("Scanning files for matches and computing rewrites...")
    writes: list[PlannedWrite] = []
    rewrite_count = 0
    for md in walk_kb_md(kb):
        try:
            text = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        new_text, n = _rewrite_in_text(text, all_pairs)
        if n > 0:
            writes.append(PlannedWrite(path=md, content=new_text))
            rewrite_count += n
            print(f"  {md.relative_to(vault_root)}: {n} link(s)")
    print()
    print(f"Files to write: {len(writes)}; total link rewrites: {rewrite_count}")
    if writes and not args.dry_run:
        BATCH = 100
        for i in range(0, len(writes), BATCH):
            batch_atomic_write(writes[i : i + BATCH])
        print(f"Applied {len(writes)} file writes.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
