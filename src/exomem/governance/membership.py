"""Query-time scope membership — evaluated against an already-parsed page.

Selectors read frontmatter the pipeline already holds (path globs, projects,
tags, types, author-declared classes, explicit refs), minus a scope's
`exclude` selectors. A page is a member of a scope when ANY selector kind
matches and no exclude selector catches it (design D4). Results are memoized
per `(policy fingerprint, path, mtime_ns, size)` in a bounded LRU — never an
index-time table, so a policy change invalidates by fingerprint mismatch
alone, adding no component to the deletion/upsert fan-out.
"""

from __future__ import annotations

import fnmatch
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .. import find_corpus
from ..find_types import ParsedPage
from ..kbdir import kb_dirname
from . import companions
from .policy import Policy, Scope

_MEMO_MAX = 4096
_MEMO: OrderedDict[tuple[str, str, int, int], frozenset[str]] = OrderedDict()
_SNAPSHOT_MEMO: OrderedDict[tuple[str, str, str], frozenset[str]] = OrderedDict()
_PATH_MEMO: OrderedDict[
    tuple[str, str, tuple[companions.BoundSnapshot, ...]], MembershipOutcome
] = OrderedDict()


class MembershipUnresolved(Exception):
    """The page's identity on disk could not be established.

    Deliberately an exception rather than an empty result. `frozenset()` is a
    real, meaningful answer — "this page is a member of no scope" — and
    `decisions.decide` resolves it to `DISCLOSURE_MAX`. So a silent fallback
    to any empty-ish value converts an unreadable file into FULL DISCLOSURE,
    which is the opposite of what an IO failure should cost. Callers must
    translate this into their own fail-closed signal.
    """


@dataclass(frozen=True)
class MembershipOutcome:
    """Non-Markdown membership that is either proven or safely unresolved."""

    state: Literal["classified", "unresolved"]
    scope_ids: frozenset[str]
    reason: Literal[
        "companion_required",
        "artifact_unsafe",
        "companion_unsafe",
        "descriptor_missing",
        "descriptor_invalid",
        "artifact_mismatch",
        "companion_ambiguous",
    ] | None = None

    def require_classified(self) -> frozenset[str]:
        if self.state == "classified":
            return self.scope_ids
        raise MembershipUnresolved(
            f"non-Markdown membership is unresolved: {self.reason or 'unknown'}"
        )


def clear_memo() -> None:
    _MEMO.clear()
    _SNAPSHOT_MEMO.clear()
    _PATH_MEMO.clear()


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


def _path_ref_excludes(scope: Scope, rel_path: str) -> bool:
    return (
        bool(scope.exclude_paths) and _matches_paths(scope.exclude_paths, rel_path)
    ) or (bool(scope.exclude_refs) and _matches_refs(scope.exclude_refs, rel_path))


def _path_ref_matches(scope: Scope, rel_path: str) -> bool:
    return (bool(scope.paths) and _matches_paths(scope.paths, rel_path)) or (
        bool(scope.refs) and _matches_refs(scope.refs, rel_path)
    )


def _needs_frontmatter(scope: Scope) -> bool:
    return bool(scope.projects or scope.tags or scope.types or scope.classes)


def _semantic_scope_matches(scope: Scope, companion: companions.BoundCompanion) -> bool:
    projects = {value.casefold() for value in companion.projects}
    tags = {value.casefold() for value in companion.tags}
    types = {value.casefold() for value in companion.types}
    classes = {value.casefold() for value in companion.classes}
    positive = (
        any(value.casefold() in projects for value in scope.projects)
        or any(value.casefold() in tags for value in scope.tags)
        or any(value.casefold() in types for value in scope.types)
        or any(value.casefold() in classes for value in scope.classes)
    )
    excluded = (
        any(value.casefold() in projects for value in scope.exclude_projects)
        or any(value.casefold() in tags for value in scope.exclude_tags)
        or any(value.casefold() in types for value in scope.exclude_types)
        or any(value.casefold() in classes for value in scope.exclude_classes)
    )
    return positive and not excluded


def evaluate_path_only(
    vault_root: Path, rel_path: str, policy: Policy
) -> MembershipOutcome:
    """Classify a non-Markdown item's path/ref membership without reading it.

    `find_corpus.parse_page` cannot decode a binary, and the caller used to
    read that failure as `None` — a value overloaded to mean both "unreadable"
    and "not permitted". That single conflation broke in BOTH directions at
    once: the walk filter treated every binary as permitted (so withheld media
    stayed visible in counts and samples, and kept a folder from collapsing),
    while `release_allows_download`/`release_allows_frames` treated the same
    value as deny (so creating `_Governance/` broke media for everyone,
    including the owner).

    A path/ref exclusion proves exclusion and a path/ref positive proves
    membership. A still-undecided semantic selector is evaluated only from the
    closed, byte-bound companion registry. Missing, stale, ambiguous, or unsafe
    companion state remains unresolved instead of becoming an empty scope set.
    """
    if policy.empty or not policy.scopes:
        return MembershipOutcome("classified", frozenset())

    matched: set[str] = set()
    undecided: list[tuple[str, Scope]] = []
    for scope_id, scope in policy.scopes.items():
        if _path_ref_excludes(scope, rel_path):
            continue
        if _path_ref_matches(scope, rel_path):
            matched.add(scope_id)
            continue
        if _needs_frontmatter(scope):
            undecided.append((scope_id, scope))
    if undecided:
        try:
            companion = companions.classify(vault_root, rel_path)
        except companions.CompanionClassificationError as error:
            return MembershipOutcome("unresolved", frozenset(matched), error.reason)
        memo_key = (policy.fingerprint, rel_path, companion.identities)
        cached = _PATH_MEMO.get(memo_key)
        if cached is not None:
            _PATH_MEMO.move_to_end(memo_key)
            return cached
        for scope_id, scope in undecided:
            if _semantic_scope_matches(scope, companion):
                matched.add(scope_id)
        result = MembershipOutcome("classified", frozenset(matched))
        _PATH_MEMO[memo_key] = result
        _PATH_MEMO.move_to_end(memo_key)
        while len(_PATH_MEMO) > _MEMO_MAX:
            _PATH_MEMO.popitem(last=False)
        return result
    return MembershipOutcome("classified", frozenset(matched))


def evaluate(page: ParsedPage, policy: Policy) -> frozenset[str]:
    """Return the set of scope ids `page` belongs to under `policy`, memoized."""
    if policy.empty or not policy.scopes:
        return frozenset()
    # `st_size` alongside `st_mtime_ns`, not `st_mtime_ns` alone. Two writes
    # in quick succession can share an identical `st_mtime_ns` — the platform
    # timestamp is not always as fine-grained as the field width suggests
    # (verified on this project's own WSL2/overlay checkout: two consecutive
    # `write_text` calls produced byte-identical nanosecond stamps). With
    # mtime as the sole validity probe, retagging a page into a restricted
    # scope kept serving the STALE membership, so revocation silently never
    # took effect. Size is not a content hash, but it moves for exactly the
    # authoring edit — adding a tag — that mtime alone was missing.
    try:
        stat = page.path.stat()
        mtime_ns, size = stat.st_mtime_ns, stat.st_size
    except OSError as exc:
        # FAIL CLOSED. The old fallback here was `page.mtime` with `size=-1`,
        # which is fail-OPEN twice over: it keys the memo on a stale, parse-time
        # timestamp (so a permissive answer computed before the page moved into
        # a restricted scope is replayed), and if nothing is memoized it goes on
        # to compute a membership for a page it can no longer prove exists in
        # the form it was parsed from. The stat is not an optimization — it is
        # the only evidence tying `page` to the bytes on disk. Without it there
        # is no honest answer, and the honest non-answer must not be
        # `frozenset()`; see `MembershipUnresolved`.
        raise MembershipUnresolved(
            f"cannot stat {page.rel_path!r} to resolve scope membership: {exc}"
        ) from exc
    key = (policy.fingerprint, page.rel_path, mtime_ns, size)
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


def evaluate_snapshot(
    page: ParsedPage, policy: Policy, *, content_hash: str
) -> frozenset[str]:
    """Evaluate membership for immutable bytes already held by the caller.

    Direct reads cannot use :func:`evaluate`: its live ``stat`` intentionally
    proves a search candidate still names the on-disk page, but a direct-read
    response must instead bind to the exact representation captured by its
    open file descriptor.  The content hash is that representation identity;
    no later path lookup participates in this answer.
    """
    if policy.empty or not policy.scopes:
        return frozenset()
    key = (policy.fingerprint, page.rel_path, content_hash)
    cached = _SNAPSHOT_MEMO.get(key)
    if cached is not None:
        _SNAPSHOT_MEMO.move_to_end(key)
        return cached
    result = frozenset(
        scope_id for scope_id, scope in policy.scopes.items() if _scope_matches(scope, page)
    )
    _SNAPSHOT_MEMO[key] = result
    _SNAPSHOT_MEMO.move_to_end(key)
    while len(_SNAPSHOT_MEMO) > _MEMO_MAX:
        _SNAPSHOT_MEMO.popitem(last=False)
    return result
