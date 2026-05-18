"""Read-only search across the Knowledge Base.

Scans every `.md` under `Knowledge Base/`, parses YAML frontmatter, filters by
structured fields, then does case-insensitive substring matching on
title + body. Hugo's vault is hundreds of pages — full-scan is fast enough.

Cached in-process between calls: keyed by file path, invalidated by mtime.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


log = logging.getLogger(__name__)

EXCLUDED_DIR_NAMES = frozenset({"_Schema", "_attachments", "_archive"})
FRONTMATTER_PATTERN = re.compile(r"^---\n(.*?)\n---\n(.*)", re.DOTALL)
H1_PATTERN = re.compile(r"^# (.+)$", re.MULTILINE)

EXCERPT_RADIUS = 100  # chars on each side of the match
EXCERPT_MAX_LEN = 220


@dataclass
class ParsedPage:
    path: Path  # absolute
    rel_path: str  # vault-relative, e.g. "Knowledge Base/Notes/Insights/foo.md"
    frontmatter: dict[str, Any]
    body: str
    title: str
    mtime: float

    @property
    def page_type(self) -> str | None:
        t = self.frontmatter.get("type")
        return str(t) if t else None

    @property
    def scope(self) -> str | None:
        """Per-type "scope" field as defined in the plan.

        - research-note → project
        - pattern / insight / failure → project (singular) or projects (joined)
        - experiment → domain
        - production-log → medium
        - entity → entity_type
        - source → source_type
        Fallback: project / projects / domain / medium / entity_type in that order.
        """
        fm = self.frontmatter
        t = self.page_type

        def _project_or_projects() -> str | None:
            if (proj := fm.get("project")):
                return str(proj)
            if (projects := fm.get("projects")):
                if isinstance(projects, list) and projects:
                    return ",".join(str(p) for p in projects)
                return str(projects)
            return None

        if t == "production-log":
            return str(fm["medium"]) if fm.get("medium") else None
        if t == "experiment":
            return str(fm["domain"]) if fm.get("domain") else None
        if t == "entity":
            return str(fm["entity_type"]) if fm.get("entity_type") else None
        if t == "source":
            return str(fm["source_type"]) if fm.get("source_type") else None
        if t in ("research-note", "pattern", "insight", "failure"):
            return _project_or_projects()

        # Unknown type: fall back across all candidates
        return (
            _project_or_projects()
            or (str(fm["domain"]) if fm.get("domain") else None)
            or (str(fm["medium"]) if fm.get("medium") else None)
            or (str(fm["entity_type"]) if fm.get("entity_type") else None)
        )

    @property
    def updated(self) -> str:
        u = self.frontmatter.get("updated") or self.frontmatter.get("captured") or ""
        return str(u)

    @property
    def tags(self) -> list[str]:
        t = self.frontmatter.get("tags") or []
        return [str(x).lower() for x in t] if isinstance(t, list) else []


@dataclass
class Hit:
    path: str
    type: str | None
    scope: str | None
    title: str
    updated: str
    excerpt: str

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "type": self.type,
            "scope": self.scope,
            "title": self.title,
            "updated": self.updated,
            "excerpt": self.excerpt,
        }


@dataclass
class FrontmatterCache:
    """Per-process cache of parsed pages, invalidated by mtime."""

    entries: dict[Path, ParsedPage] = field(default_factory=dict)

    def get(self, path: Path) -> ParsedPage | None:
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            self.entries.pop(path, None)
            return None
        cached = self.entries.get(path)
        if cached and cached.mtime == mtime:
            return cached
        parsed = _parse_page(path, mtime)
        if parsed is not None:
            self.entries[path] = parsed
        return parsed


_CACHE = FrontmatterCache()


def find(
    vault_root: Path,
    *,
    query: str,
    types: list[str] | None = None,
    projects: list[str] | None = None,
    tags: list[str] | None = None,
    limit: int = 15,
) -> list[Hit]:
    """Search the KB. Returns up to `limit` hits, most recently updated first."""
    if limit < 1:
        limit = 1
    limit = min(limit, 100)
    query_norm = (query or "").lower().strip()

    kb = vault_root / "Knowledge Base"
    if not kb.is_dir():
        log.error("KB directory missing: %s", kb)
        return []

    hits: list[tuple[str, Hit]] = []  # (sort_key, hit)
    for path in _walk_md(kb):
        page = _CACHE.get(path)
        if page is None:
            continue
        if not _passes_filters(page, types=types, projects=projects, tags=tags):
            continue
        excerpt = _make_excerpt(page, query_norm)
        if query_norm and excerpt is None:
            continue
        rel = path.relative_to(vault_root).as_posix()
        hits.append(
            (
                page.updated or "0000-00-00",
                Hit(
                    path=rel,
                    type=page.page_type,
                    scope=page.scope,
                    title=page.title,
                    updated=page.updated,
                    excerpt=excerpt or "",
                ),
            )
        )

    # Sort: most recently updated first; ties broken by path for determinism.
    hits.sort(key=lambda t: (t[0], t[1].path), reverse=True)
    return [h for _, h in hits[:limit]]


def _walk_md(root: Path):
    """Yield every .md path under root, skipping excluded subtrees."""
    for child in root.iterdir():
        if child.is_dir():
            if child.name in EXCLUDED_DIR_NAMES:
                continue
            yield from _walk_md(child)
        elif child.is_file() and child.suffix.lower() == ".md":
            yield child


def _parse_page(path: Path, mtime: float) -> ParsedPage | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        log.warning("could not read %s: %s", path, e)
        return None

    fm_match = FRONTMATTER_PATTERN.match(text)
    if fm_match:
        try:
            frontmatter = yaml.safe_load(fm_match.group(1)) or {}
            if not isinstance(frontmatter, dict):
                frontmatter = {}
        except yaml.YAMLError as e:
            log.warning("YAML parse error in %s: %s", path, e)
            frontmatter = {}
        body = fm_match.group(2)
    else:
        frontmatter = {}
        body = text

    h1_match = H1_PATTERN.search(body)
    title = h1_match.group(1).strip() if h1_match else path.stem

    # Vault-relative path: we want "Knowledge Base/..." not absolute.
    # Caller resolves; we just store the path for find() to compute relpath.
    return ParsedPage(
        path=path,
        rel_path="",  # filled in find()
        frontmatter=frontmatter,
        body=body,
        title=title,
        mtime=mtime,
    )


def _passes_filters(
    page: ParsedPage,
    *,
    types: list[str] | None,
    projects: list[str] | None,
    tags: list[str] | None,
) -> bool:
    if types and page.page_type not in types:
        return False
    if projects:
        page_projects = _all_projects(page.frontmatter)
        if not any(p in page_projects for p in projects):
            return False
    if tags:
        page_tags = set(page.tags)
        if not any(t.lower() in page_tags for t in tags):
            return False
    return True


def _all_projects(fm: dict) -> set[str]:
    out: set[str] = set()
    if (p := fm.get("project")):
        out.add(str(p))
    if (ps := fm.get("projects")):
        if isinstance(ps, list):
            out.update(str(x) for x in ps)
        else:
            out.add(str(ps))
    return out


def _make_excerpt(page: ParsedPage, query_norm: str) -> str | None:
    """Return ~200-char snippet around the first body match; None if no query match.

    If query is empty, returns the first ~200 chars of body (no match required).
    """
    body = page.body.strip()
    if not query_norm:
        snippet = body[:EXCERPT_MAX_LEN]
        return _collapse(snippet)
    title_norm = page.title.lower()
    body_norm = body.lower()
    if query_norm in title_norm:
        # Title match still gets a body excerpt for context.
        snippet = body[:EXCERPT_MAX_LEN]
        return _collapse(snippet)
    idx = body_norm.find(query_norm)
    if idx == -1:
        return None
    start = max(0, idx - EXCERPT_RADIUS)
    end = min(len(body), idx + len(query_norm) + EXCERPT_RADIUS)
    snippet = body[start:end]
    if start > 0:
        snippet = "…" + snippet.lstrip()
    if end < len(body):
        snippet = snippet.rstrip() + "…"
    return _collapse(snippet)


def _collapse(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def clear_cache() -> None:
    """Test hook: flush the in-process cache between tests."""
    _CACHE.entries.clear()
