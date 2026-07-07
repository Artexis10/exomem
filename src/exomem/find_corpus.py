"""Corpus parsing, walking, freshness, and filters for find()."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import freshness
from .find_types import ParsedPage

log = logging.getLogger(__name__)

EXCLUDED_DIR_NAMES = frozenset({"_Schema", "_attachments", "_archive", "_trash"})
NAVIGATION_BASENAMES = frozenset({"index.md", "log.md"})
FRONTMATTER_PATTERN = re.compile(r"^---\n(.*?)\n---\n(.*)", re.DOTALL)
H1_PATTERN = re.compile(r"^# (.+)$", re.MULTILINE)


@dataclass
class FrontmatterCache:
    """Per-process cache of parsed pages, invalidated by mtime."""

    entries: dict[Path, ParsedPage] = field(default_factory=dict)

    def get(self, path: Path, vault_root: Path) -> ParsedPage | None:
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            self.entries.pop(path, None)
            return None
        cached = self.entries.get(path)
        if cached and cached.mtime == mtime:
            return cached
        parsed = parse_page(path, mtime, vault_root)
        if parsed is not None:
            self.entries[path] = parsed
        return parsed


CACHE = FrontmatterCache()


def walk_freshness_key(paths) -> tuple[int, int, str]:
    """(file count, max st_mtime_ns, digest of sorted path+mtime pairs).

    Digest-strength on purpose: count/max-mtime alone miss a delete paired
    with a create, a rename (mtime preserved), and a replacement carrying an
    older mtime. Every consumer that caches corpus-derived state (the hot
    find cache, BM25, the wikilink resolver, the inbound-link index) compares
    the whole triple, so those histories now invalidate correctly.
    """
    entries: list[tuple[str, int]] = []
    for p in paths:
        try:
            entries.append((str(p), p.stat().st_mtime_ns))
        except OSError:
            continue
    return freshness.triple_from_entries(entries)


def walk_md(root: Path):
    """Yield every .md path under root, skipping excluded subtrees.

    Skips Obsidian `*.sync-conflict-*.md` files — transient conflict
    duplicates that would otherwise pollute the index and search results.
    """
    for child in root.iterdir():
        if child.is_dir():
            if child.name in EXCLUDED_DIR_NAMES:
                continue
            yield from walk_md(child)
        elif (
            child.is_file()
            and child.suffix.lower() == ".md"
            and ".sync-conflict-" not in child.name
        ):
            yield child


def parse_page(path: Path, mtime: float, vault_root: Path) -> ParsedPage | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        log.warning("could not read %s: %s", path, e)
        return None

    fm_match = FRONTMATTER_PATTERN.match(text)
    if fm_match:
        try:
            # Hot path: every page-cache miss parses here (warm-up walks the
            # whole vault through it). libyaml loader via the vault seam.
            from .vault import yaml_safe_load
            frontmatter = yaml_safe_load(fm_match.group(1)) or {}
            if not isinstance(frontmatter, dict):
                frontmatter = {}
        except yaml.YAMLError as e:
            log.warning("YAML parse error in %s: %s", path, e)
            frontmatter = {}
        body = fm_match.group(2)
        # The FRONTMATTER_PATTERN consumes the closing `\n---\n` but not the
        # blank line that conventionally follows. Strip a single leading `\n`
        # so callers (notably `get`) can feed `body` back into `edit` without
        # accumulating blanks across round-trips.
        if body.startswith("\n"):
            body = body[1:]
    else:
        frontmatter = {}
        body = text

    h1_match = H1_PATTERN.search(body)
    title = h1_match.group(1).strip() if h1_match else path.stem

    try:
        rel_path = path.resolve().relative_to(vault_root.resolve()).as_posix()
    except ValueError:
        rel_path = path.as_posix()

    return ParsedPage(
        path=path,
        rel_path=rel_path,
        frontmatter=frontmatter,
        body=body,
        title=title,
        mtime=mtime,
    )


def passes_filters(
    page: ParsedPage,
    *,
    vault_root: Path | None = None,
    types: list[str] | None,
    projects: list[str] | None,
    tags: list[str] | None,
    speakers: list[str] | None = None,
    file_types: list[str] | None = None,
    exclude_file_types: list[str] | None = None,
) -> bool:
    # `excluded` tier (_access.yaml): never surfaced. Checked first — an excluded
    # page is invisible regardless of how well it matches. (vault_root omitted in
    # unit tests -> skip; real find paths always pass it.)
    if vault_root is not None:
        from . import access
        if not access.is_indexable(vault_root, page.rel_path):
            return False
    if types and page.page_type not in types:
        return False
    if projects:
        page_projects = all_projects(page.frontmatter)
        if not any(p in page_projects for p in projects):
            return False
    if tags:
        page_tags = set(page.tags)
        if not any(t.lower() in page_tags for t in tags):
            return False
    if speakers:
        page_speakers = {s.lower() for s in page.speakers}
        if not any(s.lower() in page_speakers for s in speakers):
            return False
    # File-type scoping (opt-in; default None/None lets every kind through — a
    # search must never hide an artifact type by default).
    if file_types or exclude_file_types:
        kind = page.file_kind
        if file_types and kind not in {ft.lower() for ft in file_types}:
            return False
        if exclude_file_types and kind in {ft.lower() for ft in exclude_file_types}:
            return False
    return True


def all_projects(fm: dict) -> set[str]:
    out: set[str] = set()
    if (p := fm.get("project")):
        out.add(str(p))
    if (ps := fm.get("projects")):
        if isinstance(ps, list):
            out.update(str(x) for x in ps)
        else:
            out.add(str(ps))
    return out
