"""Query-time scope membership — evaluated against an already-parsed page.

Selectors read frontmatter the pipeline already holds (path globs, projects,
tags, types, author-declared classes, explicit refs), minus a scope's
`exclude` selectors. A page is a member of a scope when ANY selector kind
matches and no exclude selector catches it (design D4). Results are memoized
per `(policy fingerprint, path, mtime_ns)` in a bounded LRU — never an
index-time table, so a policy change invalidates by fingerprint mismatch
alone, adding no component to the deletion/upsert fan-out.
"""

from __future__ import annotations

import fnmatch
from collections import OrderedDict

from .. import find_corpus
from ..find_types import ParsedPage
from ..kbdir import kb_dirname
from .policy import Policy, Scope

_MEMO_MAX = 4096
_MEMO: OrderedDict[tuple[str, str, int], frozenset[str]] = OrderedDict()


def clear_memo() -> None:
    _MEMO.clear()


def _kb_relative(rel_path: str) -> str:
    """Strip a leading KB dirname so both authored path forms compare equal."""
    rel = rel_path.replace("\\", "/").strip("/")
    parts = rel.split("/")
    if parts and parts[0].casefold() == kb_dirname().casefold():
        return "/".join(parts[1:])
    return rel


def _matches_paths(patterns: tuple[str, ...], rel_path: str) -> bool:
    kb_rel = _kb_relative(rel_path)
    return any(fnmatch.fnmatchcase(rel_path, p) or fnmatch.fnmatchcase(kb_rel, p) for p in patterns)


def _matches_projects(values: tuple[str, ...], page: ParsedPage) -> bool:
    page_projects = {p.lower() for p in find_corpus.all_projects(page.frontmatter)}
    return any(v.lower() in page_projects for v in values)


def _matches_tags(values: tuple[str, ...], page: ParsedPage) -> bool:
    page_tags = set(page.tags)  # ParsedPage.tags is already lower-cased
    return any(v.lower() in page_tags for v in values)


def _matches_types(values: tuple[str, ...], page: ParsedPage) -> bool:
    page_type = page.page_type
    return page_type is not None and any(v.lower() == page_type.lower() for v in values)


def _matches_classes(values: tuple[str, ...], page: ParsedPage) -> bool:
    """Author-declared `classes:` frontmatter — no detector exists in this
    kernel-only change; a later change can populate the same field."""
    raw = page.frontmatter.get("classes") or []
    if not isinstance(raw, list):
        return False
    page_classes = {str(c).lower() for c in raw}
    return any(v.lower() in page_classes for v in values)


def _matches_refs(values: tuple[str, ...], rel_path: str) -> bool:
    kb_rel = _kb_relative(rel_path)
    normalized: set[str] = set()
    for v in values:
        normalized.add(v)
        normalized.add(_kb_relative(v))
    return rel_path in normalized or kb_rel in normalized


def _scope_matches(scope: Scope, page: ParsedPage) -> bool:
    positive = (
        (bool(scope.paths) and _matches_paths(scope.paths, page.rel_path))
        or (bool(scope.projects) and _matches_projects(scope.projects, page))
        or (bool(scope.tags) and _matches_tags(scope.tags, page))
        or (bool(scope.types) and _matches_types(scope.types, page))
        or (bool(scope.classes) and _matches_classes(scope.classes, page))
        or (bool(scope.refs) and _matches_refs(scope.refs, page.rel_path))
    )
    if not positive:
        return False
    excluded = (
        (bool(scope.exclude_paths) and _matches_paths(scope.exclude_paths, page.rel_path))
        or (bool(scope.exclude_projects) and _matches_projects(scope.exclude_projects, page))
        or (bool(scope.exclude_tags) and _matches_tags(scope.exclude_tags, page))
        or (bool(scope.exclude_types) and _matches_types(scope.exclude_types, page))
        or (bool(scope.exclude_classes) and _matches_classes(scope.exclude_classes, page))
        or (bool(scope.exclude_refs) and _matches_refs(scope.exclude_refs, page.rel_path))
    )
    return not excluded


def evaluate(page: ParsedPage, policy: Policy) -> frozenset[str]:
    """Return the set of scope ids `page` belongs to under `policy`, memoized."""
    if policy.empty or not policy.scopes:
        return frozenset()
    try:
        mtime_ns = page.path.stat().st_mtime_ns
    except OSError:
        mtime_ns = int(page.mtime * 1_000_000_000)
    key = (policy.fingerprint, page.rel_path, mtime_ns)
    cached = _MEMO.get(key)
    if cached is not None:
        _MEMO.move_to_end(key)
        return cached
    result = frozenset(
        scope_id for scope_id, scope in policy.scopes.items() if _scope_matches(scope, page)
    )
    _MEMO[key] = result
    _MEMO.move_to_end(key)
    while len(_MEMO) > _MEMO_MAX:
        _MEMO.popitem(last=False)
    return result
