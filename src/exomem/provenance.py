"""provenance: read-only scan of `<!-- key:value -->` tags in note bodies.

The taste/opinion notes carry lightweight provenance as HTML comments —
`<!-- platform:imdb -->`, `<!-- conv:2026-06-01 -->`, `<!-- add-to-imdb -->`.
They're invisible to structured query, so reconciliation ("which takes are
flagged add-to-imdb but not yet pushed?") used to mean a manual full-text scan.

This module reads those tags at query time — an on-demand walk over markdown
bodies, the same cheap pass `audit` and keyword-`find` already do (<1s for
~600 files). Crucially it adds NO new state: the tags stay in the markdown
(the single source of truth); there is no index and no sidecar to drift.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from . import find as find_module
from . import get_page as get_page_module
from .vault import _mask_code_spans, kb_root


# One HTML comment; capture its (trimmed) inner text. Non-greedy → one per match.
_COMMENT_RE = re.compile(r"<!--\s*(.*?)\s*-->")
# A key:value token inside a comment. Value runs to the next whitespace.
_TAG_RE = re.compile(r"([A-Za-z][\w-]*)\s*:\s*([^\s]+)")


@dataclass
class ProvenanceFinding:
    path: str            # vault-relative, with .md
    line_number: int     # 1-based, body-relative (frontmatter excluded)
    row_text: str        # the full source line
    tags: dict[str, str]  # merged key:value across all comments on the line

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "line_number": self.line_number,
            "row_text": self.row_text,
            "tags": self.tags,
        }


def _resolve_filter(
    tag: str | None, key: str | None, value: str | None
) -> tuple[str | None, str | None]:
    """Fold `tag` ("key" or "key:value") into (key, value); explicit args win.

    Returns lowercased (key, value) for case-insensitive comparison, or Nones.
    """
    if tag:
        if ":" in tag:
            t_key, t_val = tag.split(":", 1)
        else:
            t_key, t_val = tag, None
        key = key or t_key
        if value is None:
            value = t_val
    return (key.lower() if key else None, value.lower() if value else None)


def _scan_body(
    rel_path: str, body: str, key_f: str | None, value_f: str | None
) -> list[ProvenanceFinding]:
    masked = _mask_code_spans(body)  # comments inside code fences → spaces
    findings: list[ProvenanceFinding] = []
    for lineno, (line, mline) in enumerate(
        zip(body.split("\n"), masked.split("\n")), start=1
    ):
        tags: dict[str, str] = {}
        for comment in _COMMENT_RE.finditer(mline):  # masked → skips code
            for tm in _TAG_RE.finditer(comment.group(1)):
                tags[tm.group(1)] = tm.group(2)  # last-wins on duplicate keys
        if not tags:
            continue
        if key_f is not None:
            matched = next((k for k in tags if k.lower() == key_f), None)
            if matched is None:
                continue
            if value_f is not None and tags[matched].lower() != value_f:
                continue
        findings.append(
            ProvenanceFinding(
                path=rel_path, line_number=lineno, row_text=line, tags=tags
            )
        )
    return findings


def scan_provenance(
    vault_root: Path,
    *,
    tag: str | None = None,
    key: str | None = None,
    value: str | None = None,
    path: str | None = None,
) -> list[ProvenanceFinding]:
    """Scan note bodies for provenance tags. Read-only; no index/sidecar.

    Filter by `tag` ("key" or "key:value" shorthand), or explicit `key`/`value`.
    Restrict to one file with `path` (else the whole Knowledge Base is walked).
    Line numbers are body-relative (provenance never lives in frontmatter).
    """
    key_f, value_f = _resolve_filter(tag, key, value)

    if path is not None:
        res = get_page_module.get_page(vault_root, path=path)
        return _scan_body(res.path, res.body, key_f, value_f)

    findings: list[ProvenanceFinding] = []
    for p in find_module._walk_md(kb_root(vault_root)):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        page = find_module._parse_page(p, mtime, vault_root)
        if page is None:
            continue
        findings.extend(_scan_body(page.rel_path, page.body, key_f, value_f))
    findings.sort(key=lambda f: (f.path, f.line_number))
    return findings


def origin_keys(
    sources_by_path: Mapping[str, Iterable[str]],
    *,
    fallback: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """One independent-origin key per page. Overlapping Sources collapse.

    Pages that declare an overlapping Source belong to one derivative
    component (a union-find over shared Sources), so a single Source fanned
    out into many notes counts once and copies add nothing. A page with no
    declared Source is its own origin: `fallback[path]` when given (a session
    key, say), else `page:<path>`.
    """
    paths = list(sources_by_path)
    parent = list(range(len(paths)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    sources = [frozenset(sources_by_path[path]) for path in paths]
    first_by_source: dict[str, int] = {}
    for index, declared in enumerate(sources):
        for source in sorted(declared):
            union(index, first_by_source.setdefault(source, index))
    component: dict[int, set[str]] = {}
    for index, declared in enumerate(sources):
        if declared:
            component.setdefault(find(index), set()).update(declared)
    out: dict[str, str] = {}
    for index, path in enumerate(paths):
        if sources[index]:
            encoded = json.dumps(
                sorted(component[find(index)]),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            out[path] = "source:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        else:
            out[path] = (fallback or {}).get(path) or f"page:{path}"
    return out
