"""Deterministic measurements for progressively activating an existing corpus.

This module measures explicit Markdown structure only. It does not infer semantic
relationships, score knowledge quality, or write to the vault.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import (
    access,
    relation_registry,
    semantic_language_registry,
    semantic_units,
)
from . import find as find_module
from .audit import AuditFinding
from .vault import content_hash, find_body_wikilinks, kb_root

ACTIVATION_CATEGORIES: tuple[str, ...] = (
    "unregistered_relation",
    "provenance_debt",
    "typed_relation_debt",
    "relation_debt",
)

_ELIGIBLE_TYPES = frozenset(
    {
        "research-note",
        "insight",
        "pattern",
        "failure",
        "experiment",
        "production-log",
        "entity",
    }
)
_COMPILED_PAGE_TYPES = frozenset(
    {
        "research-note",
        "insight",
        "pattern",
        "failure",
        "experiment",
        "production-log",
    }
)
_INACTIVE_STATUSES = frozenset({"superseded", "archived", "draft", "dropped"})
_SKIP_SLUG_SUFFIXES = ("-architecture", "-snapshot", "-catalog-snapshot")
_SKIP_TAGS = frozenset({"hub", "snapshot"})
_ASSERTION_BLOCK_TYPES = frozenset({"claim", "finding", "inference", "hypothesis", "result"})
_PROVENANCE_RELATIONS = frozenset({"derived_from", "evidenced_by", "cites"})
_FRONTMATTER_TYPED_FIELDS: dict[str, str] = {
    "sources": "derived_from",
    "evidence": "evidenced_by",
    "evidences": "evidenced_by",
    "evidence_paths": "evidenced_by",
    "supersedes": "supersedes",
    "superseded_by": "supersedes",
}
_WIKILINK_RE = re.compile(r"\[\[([^\]|\n]+)(?:\|[^\]\n]+)?\]\]")


@dataclass(frozen=True)
class ActivationScan:
    findings: list[AuditFinding]
    coverage: dict[str, int]


def scan(vault_root: Path) -> ActivationScan:
    """Measure activation coverage and review deficits in one tolerant vault walk."""
    vault_root = Path(vault_root)
    registry = relation_registry.load_registry(vault_root)
    language_registry = semantic_language_registry.load_registry(vault_root)
    findings: list[AuditFinding] = []
    coverage = {
        "eligible_pages": 0,
        "connected_pages": 0,
        "typed_relation_pages": 0,
        "generic_only_pages": 0,
        "disconnected_pages": 0,
        "provenance_candidate_pages": 0,
        "provenance_linked_pages": 0,
        "unregistered_relation_observations": 0,
    }

    kb = kb_root(vault_root)
    if not kb.is_dir():
        return ActivationScan(findings=findings, coverage=coverage)

    for path in find_module._walk_md(kb):
        try:
            page = find_module._parse_page(path, path.stat().st_mtime, vault_root)
        except OSError:
            continue
        if page is None or not _eligible(vault_root, page):
            continue

        coverage["eligible_pages"] += 1
        measurement = _measure_page(page, registry, language_registry=language_registry)
        meta = {
            "signal_version": _signal_version(page),
            "typed_relations": measurement["typed_relations"],
            "body_wikilinks": measurement["body_wikilinks"],
            "frontmatter_links": measurement["frontmatter_links"],
            "assertion_blocks": measurement["assertion_blocks"],
            "provenance_relations": measurement["provenance_relations"],
        }

        if measurement["connected"]:
            coverage["connected_pages"] += 1
        else:
            coverage["disconnected_pages"] += 1
            findings.append(_relation_debt(page.rel_path, meta))

        if measurement["typed_relations"]:
            coverage["typed_relation_pages"] += 1
        elif measurement["connected"]:
            coverage["generic_only_pages"] += 1
            findings.append(_typed_relation_debt(page.rel_path, meta))

        if measurement["assertion_blocks"]:
            coverage["provenance_candidate_pages"] += 1
            if measurement["provenance_relations"]:
                coverage["provenance_linked_pages"] += 1
            else:
                findings.append(_provenance_debt(page.rel_path, meta))

        unknown = measurement["unregistered"]
        if unknown:
            coverage["unregistered_relation_observations"] += len(unknown)
            findings.append(_unregistered_relation(page.rel_path, meta, unknown))

    return ActivationScan(
        findings=sorted(
            findings,
            key=lambda item: (ACTIVATION_CATEGORIES.index(item.category), item.path),
        ),
        coverage=coverage,
    )


def _eligible(vault_root: Path, page: Any) -> bool:
    return is_eligible_governed_page(vault_root, page)


def is_eligible_governed_page(vault_root: Path, page: Any) -> bool:
    """Return whether ``page`` is an active governed graph endpoint."""
    return _eligible_for_types(vault_root, page, page_types=_ELIGIBLE_TYPES)


def is_eligible_compiled_page(vault_root: Path, page: Any) -> bool:
    """Return whether ``page`` belongs to the writable compiled-page domain.

    Activation coverage historically includes entities.  The semantic contract
    applies to the six compiled conclusion types only, while sharing every
    other activation eligibility rule.
    """
    return _eligible_for_types(vault_root, page, page_types=_COMPILED_PAGE_TYPES)


def _eligible_for_types(
    vault_root: Path, page: Any, *, page_types: frozenset[str]
) -> bool:
    if page.page_type not in page_types:
        return False
    if page.path.name in {"index.md", "log.md"}:
        return False
    if page.status in _INACTIVE_STATUSES:
        return False
    if access.access_tier(vault_root, page.rel_path) != access.TIER_READ_WRITE:
        return False
    stem = page.path.stem.lower()
    if any(stem.endswith(suffix) for suffix in _SKIP_SLUG_SUFFIXES):
        return False
    return not bool(_SKIP_TAGS & set(page.tags))


def _measure_page(
    page: Any,
    registry: relation_registry.RelationRegistry,
    *,
    language_registry: semantic_language_registry.SemanticLanguageRegistry | None = None,
) -> dict[str, Any]:
    project = _page_project(page.frontmatter)
    document = semantic_units.parse_semantic_units(
        page.body,
        validate=False,
        language_registry=language_registry,
        relation_registry=registry,
        include_legacy_relations=True,
        retain_unknown_relations=True,
        project=project,
        page_type=page.page_type,
    )

    registered: list[str] = []
    unregistered: list[dict[str, str | int]] = []
    for relation in document.note_relations:
        resolution = registry.resolve(
            relation.kind,
            project=project,
            page_type=page.page_type,
            source_kind="file",
            origin="semantic_relation",
        )
        if resolution.canonical is None:
            unregistered.append(
                {"label": relation.kind, "anchor": f"line-{relation.line}"}
            )
        else:
            registered.append(resolution.canonical)

    for unit in document.rich_units:
        for relation in unit.relations:
            raw = relation.raw.split(":", 1)[0].strip()
            resolution = registry.resolve(
                raw,
                project=project,
                page_type=page.page_type,
                source_kind=unit.kind,
                origin="semantic_relation",
            )
            if resolution.canonical is None:
                unregistered.append(
                    {
                        "label": relation_registry.normalize_relation(raw),
                        "anchor": unit.anchor or f"line-{relation.line}",
                    }
                )
            else:
                registered.append(resolution.canonical)

    frontmatter_links = 0
    for field, relation_kind in _FRONTMATTER_TYPED_FIELDS.items():
        count = len(_frontmatter_links(page.frontmatter.get(field)))
        frontmatter_links += count
        registered.extend([relation_kind] * count)
    related_count = len(_frontmatter_links(page.frontmatter.get("related")))
    frontmatter_links += related_count

    body_wikilinks = sum(1 for _ in find_body_wikilinks(page.body))
    assertion_blocks = sum(
        1 for unit in document.rich_units if unit.kind in _ASSERTION_BLOCK_TYPES
    )
    provenance_relations = sum(1 for kind in registered if kind in _PROVENANCE_RELATIONS)
    unique_unknown = {
        (str(item["label"]), str(item["anchor"])): item for item in unregistered
    }
    authored_relations = len(document.note_relations) + sum(
        len(unit.relations) for unit in document.rich_units
    )
    return {
        "connected": bool(body_wikilinks or frontmatter_links or authored_relations),
        "typed_relations": len(registered),
        "body_wikilinks": body_wikilinks,
        "frontmatter_links": frontmatter_links,
        "assertion_blocks": assertion_blocks,
        "provenance_relations": provenance_relations,
        "unregistered": list(unique_unknown.values()),
    }


def _frontmatter_links(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        matches = _WIKILINK_RE.findall(value)
        return (
            [item.strip() for item in matches]
            if matches
            else ([value.strip()] if value.strip() else [])
        )
    if isinstance(value, list):
        return [link for item in value for link in _frontmatter_links(item)]
    if isinstance(value, dict):
        return [link for item in value.values() for link in _frontmatter_links(item)]
    return []


def _page_project(frontmatter: dict[str, Any]) -> str | None:
    if frontmatter.get("project") not in (None, ""):
        return str(frontmatter["project"])
    projects = frontmatter.get("projects")
    if isinstance(projects, list) and len(projects) == 1:
        return str(projects[0])
    return None


def _signal_version(page: Any) -> str:
    frontmatter = yaml.safe_dump(page.frontmatter, sort_keys=True, allow_unicode=True)
    return content_hash(frontmatter + "\n" + page.body)[:16]


def _relation_debt(path: str, meta: dict[str, Any]) -> AuditFinding:
    return AuditFinding(
        category="relation_debt",
        severity="info",
        path=path,
        detail="Active compiled page has no explicit outbound graph connection.",
        proposed_fix="Review candidate neighbours; accept only meaningful durable edges.",
        meta={
            **meta,
            "next_actions": [
                {"tool": "connect_memory", "args": {"operation": "suggest-links", "path": path}},
                {
                    "tool": "connect_memory",
                    "args": {"operation": "suggest-relations", "path": path},
                },
            ],
        },
    )


def _typed_relation_debt(path: str, meta: dict[str, Any]) -> AuditFinding:
    return AuditFinding(
        category="typed_relation_debt",
        severity="info",
        path=path,
        detail="Page has generic connections but no registered typed semantic relation.",
        proposed_fix=(
            "Review relation proposals; generic links may remain generic when that "
            "is accurate."
        ),
        meta={
            **meta,
            "next_actions": [
                {
                    "tool": "connect_memory",
                    "args": {"operation": "suggest-relations", "path": path},
                },
                {"tool": "read_memory", "args": {"identifier": path}},
            ],
        },
    )


def _provenance_debt(path: str, meta: dict[str, Any]) -> AuditFinding:
    return AuditFinding(
        category="provenance_debt",
        severity="info",
        path=path,
        detail=(
            "Page has assertion-bearing semantic blocks but no explicit page-level provenance; "
            "page-level provenance would not by itself establish support for every block."
        ),
        proposed_fix=(
            "Review the assertions and add only provenance that the stored sources "
            "or evidence support."
        ),
        meta={
            **meta,
            "next_actions": [
                {"tool": "read_memory", "args": {"identifier": path}},
                {
                    "tool": "connect_memory",
                    "args": {"operation": "suggest-relations", "path": path},
                },
            ],
        },
    )


def _unregistered_relation(
    path: str, meta: dict[str, Any], observations: list[dict[str, str | int]]
) -> AuditFinding:
    labels = sorted({str(item["label"]) for item in observations})
    return AuditFinding(
        category="unregistered_relation",
        severity="warn",
        path=path,
        detail=f"Page uses relation vocabulary not governed by the loaded registry: {labels}.",
        proposed_fix=(
            "Review corpus-wide usage before registering, aliasing, replacing, or "
            "removing the vocabulary."
        ),
        meta={
            **meta,
            "unregistered": observations,
            "next_actions": [
                {"tool": "schema_memory", "args": {"operation": "infer", "subject": "relations"}},
                {"tool": "read_memory", "args": {"identifier": path}},
            ],
        },
    )
