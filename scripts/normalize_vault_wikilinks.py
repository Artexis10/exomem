"""One-shot cleanup: rewrite every wikilink under `Knowledge Base/` to the
preferred form for the detected Obsidian vault root, plus refresh indexes.

This is the "drain the lake" Phase 5 step from the plan. The exomem writer
now enforces canonical form on every write going forward (Phase 1), but
existing files have drift accumulated over months. Run this once to bring
the vault to a clean baseline.

Usage:
    python scripts/normalize_vault_wikilinks.py [--dry-run] [--vault PATH]

By default runs against the vault resolved via `exomem.vault.resolve_vault()`
(the `EXOMEM_VAULT_PATH` env var). Use --vault to override.

Canonical path resolution remains vault-rooted internally. The script:
1. Builds a WikilinkResolver against the vault (one walk; includes
   frontmatter `title:` index for date-prefixed bare-name resolution).
2. Walks every `.md` under `Knowledge Base/`, normalizes wikilinks in both
   body and frontmatter (sources, connections, related, supersedes,
   superseded_by, ingested_into).
3. Skips `_trash/`, `_archive/`, and curated trees.
4. Refreshes every auto-managed sub-folder index via
   `indexes.compute_subindex_writes`.
5. Refreshes the top-level `Knowledge Base/index.md` Counts.
6. Surfaces a final audit summary.

Dry-run mode shows which files would change and the per-file warnings,
without writing anything. Re-run without --dry-run to apply.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Local-import the exomem package without requiring install.
HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from exomem import access  # noqa: E402
from exomem import audit as audit_module  # noqa: E402
from exomem import indexes  # noqa: E402
from exomem.vault import (  # noqa: E402
    PlannedWrite,
    WikilinkResolver,
    batch_atomic_write,
    kb_root,
    normalize_body_wikilinks,
    normalize_wikilink,
    parse_frontmatter,
    render_wikilink_target,
    resolve_vault,
)


# Skip append-only and infrastructure subtrees entirely. Per-vault readonly and
# excluded policy is checked separately through ``access_tier`` below.
SKIP_KB_SUBDIRS = frozenset({
    "Sources", "Evidence", "_Schema", "_trash", "_archive", "_attachments",
})


def _is_writable_tree(vault_root: Path, path: Path) -> bool:
    rel = path.relative_to(vault_root).as_posix()
    return access.access_tier(vault_root, rel) == access.TIER_READ_WRITE


def walk_kb_md(kb_dir: Path):
    """Yield writable compiled Markdown, preserving protected provenance trees."""
    vault_root = kb_dir.parent
    for child in sorted(kb_dir.iterdir()):
        if child.is_dir():
            if child.name in SKIP_KB_SUBDIRS or not _is_writable_tree(vault_root, child):
                continue
            yield from _walk(child, vault_root)
        elif (
            child.is_file()
            and child.suffix.lower() == ".md"
            and _is_writable_tree(vault_root, child)
        ):
            yield child


def _walk(dir_: Path, vault_root: Path):
    for child in sorted(dir_.iterdir()):
        if child.is_dir():
            if child.name in SKIP_KB_SUBDIRS or not _is_writable_tree(vault_root, child):
                continue
            yield from _walk(child, vault_root)
        elif (
            child.is_file()
            and child.suffix.lower() == ".md"
            and _is_writable_tree(vault_root, child)
        ):
            yield child


# Wikilink value inside YAML: `"[[<target>]]"` or `'[[<target>]]'` or bare.
_YAML_WIKILINK = re.compile(r'\[\[([^\]\|\n]+?)(\|[^\]\n]*)?\]\]')


def normalize_frontmatter_wikilinks(
    fm_text: str, vault_root: Path, resolver: WikilinkResolver
) -> tuple[str, list[str]]:
    """Rewrite every wikilink inside a YAML frontmatter block.

    Frontmatter wikilinks live in fields like `sources:`, `related:`,
    `supersedes:`, `superseded_by:`, `ingested_into:`. We use a simple text
    rewrite — any `[[...]]` is canonicalized in place. The YAML alias
    syntax doesn't conflict because `[[...]]` is not standard YAML.
    """
    warnings: list[str] = []
    new_text = fm_text
    matches = list(_YAML_WIKILINK.finditer(fm_text))
    for m in reversed(matches):
        target = m.group(1).strip()
        alias = (m.group(2) or "").strip()
        canonical, warning = normalize_wikilink(
            target, vault_root, resolver=resolver, strict=False
        )
        if warning:
            warnings.append(warning)
            continue
        rendered = render_wikilink_target(canonical, vault_root)
        if rendered == target and not alias:
            continue
        if alias:
            replacement = f"[[{rendered}{alias}]]"
        else:
            replacement = f"[[{rendered}]]"
        if replacement != m.group(0):
            new_text = new_text[: m.start()] + replacement + new_text[m.end():]
    return new_text, warnings


def normalize_file(
    path: Path, vault_root: Path, resolver: WikilinkResolver
) -> tuple[str | None, list[str]]:
    """Return (new_text, warnings) for a single file. None = no change."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return None, [f"read error on {path}: {e}"]

    fm, body, fm_text = parse_frontmatter(text)
    new_fm_text = fm_text
    new_body = body
    fm_warnings: list[str] = []
    body_warnings: list[str] = []
    if fm_text is not None:
        new_fm_text, fm_warnings = normalize_frontmatter_wikilinks(
            fm_text, vault_root, resolver
        )
    new_body, body_warnings = normalize_body_wikilinks(
        body, vault_root, resolver=resolver
    )

    if fm_text is not None:
        # Preserve the blank-line convention between `---` and body. The
        # FM regex strips one leading \n from body, so reconstruct with the
        # same convention the original file used: if the original had a
        # blank line after `---`, keep it.
        had_blank_after_fm = bool(re.match(r"^---\n.*?\n---\n\n", text, re.DOTALL))
        body_prefix = "\n" if had_blank_after_fm else ""
        new_text = f"---\n{new_fm_text}\n---\n{body_prefix}{new_body}"
    else:
        new_text = new_body

    if new_text == text:
        return None, []
    return new_text, fm_warnings + body_warnings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="show what would change without writing"
    )
    parser.add_argument(
        "--vault", type=Path, default=None,
        help="override vault root (otherwise resolved via resolve_vault())"
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="process at most N files (0 = no limit)"
    )
    args = parser.parse_args()

    vault_root = args.vault if args.vault else resolve_vault()
    if not vault_root.is_dir():
        print(f"ERROR: vault root not a directory: {vault_root}", file=sys.stderr)
        return 1
    kb = kb_root(vault_root)
    if not kb.is_dir():
        print(f"ERROR: Knowledge Base/ not under vault root: {kb}", file=sys.stderr)
        return 1

    print(f"Vault: {vault_root}")
    print(f"KB:    {kb}")
    print(f"Mode:  {'DRY RUN (no writes)' if args.dry_run else 'APPLY'}")
    print()

    print("Building WikilinkResolver (full vault walk + frontmatter title index)...")
    resolver = WikilinkResolver(vault_root)
    print(f"  full_paths: {len(resolver.full_paths)}")
    print(f"  kb_stripped: {len(resolver.kb_stripped)}")
    print(f"  stems: {len(resolver.stems)}")
    print(f"  titles: {len(resolver.titles)}")
    print()

    print("Walking Knowledge Base/...")
    changed_files: list[tuple[Path, str]] = []
    warnings_by_file: dict[Path, list[str]] = {}
    total_seen = 0
    for md in walk_kb_md(kb):
        total_seen += 1
        if args.limit and total_seen > args.limit:
            break
        new_text, warns = normalize_file(md, vault_root, resolver)
        if new_text is not None:
            changed_files.append((md, new_text))
        if warns:
            warnings_by_file[md] = warns

    print(f"Files scanned: {total_seen}")
    print(f"Files needing rewrite: {len(changed_files)}")
    print(f"Files with unresolvable warnings: {len(warnings_by_file)}")
    print()

    if changed_files:
        print("Files that would be rewritten:")
        for path, _ in changed_files[:30]:
            print(f"  {path.relative_to(vault_root)}")
        if len(changed_files) > 30:
            print(f"  ... and {len(changed_files) - 30} more")
        print()

    if warnings_by_file:
        print("Sample unresolvable-link warnings (first 10 files):")
        for path, warns in list(warnings_by_file.items())[:10]:
            print(f"  {path.relative_to(vault_root)}:")
            for w in warns[:3]:
                print(f"    - {w}")
            if len(warns) > 3:
                print(f"    ... and {len(warns) - 3} more on this file")
        print()

    if not args.dry_run and changed_files:
        print("Writing changes...")
        writes = [PlannedWrite(path=p, content=text) for p, text in changed_files]
        # Split into batches to keep the atomic-write tempfile count reasonable.
        BATCH = 100
        for i in range(0, len(writes), BATCH):
            batch_atomic_write(writes[i : i + BATCH], vault_root=vault_root)
        print(f"  wrote {len(writes)} files.")
        print()

    # Refresh sub-folder indexes + top-index counts.
    print("Refreshing sub-folder indexes + top-index counts...")
    top_index_path = kb / "index.md"
    top_text = (
        top_index_path.read_text(encoding="utf-8")
        if top_index_path.exists() else None
    )
    sub_writes, new_top = indexes.compute_subindex_writes(
        vault_root, top_index_text=top_text
    )
    refresh_writes: list[PlannedWrite] = list(sub_writes)
    if new_top is not None and top_text is not None and new_top != top_text:
        refresh_writes.append(PlannedWrite(path=top_index_path, content=new_top))
    print(f"  refresh writes: {len(refresh_writes)}")
    for w in refresh_writes:
        print(f"    {w.path.relative_to(vault_root)}")
    if not args.dry_run and refresh_writes:
        batch_atomic_write(refresh_writes)
        print("  applied.")
    print()

    # Final audit summary.
    print("Final audit summary:")
    report = audit_module.audit(vault_root)
    for cat, n in sorted(report.summary.items()):
        print(f"  {cat}: {n}")
    print()
    print(f"Total findings: {len(report.findings)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
