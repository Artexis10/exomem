"""Bounded, read-only semantic-language census for vault adoption."""

from __future__ import annotations

import os
import stat
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from . import (
    memory_schema,
    overview,
    relation_registry,
    semantic_contract,
    semantic_language_registry,
    semantic_writes,
)
from .kbdir import kb_dirname, kb_prefix

DEFAULT_MAX_FILES = 512
DEFAULT_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_EXAMPLE_LIMIT = 12
HARD_MAX_FILES = 4096
HARD_MAX_BYTES = 32 * 1024 * 1024
HARD_EXAMPLE_LIMIT = 50
FREQUENCY_LIMIT = 128

_SKIP_ALWAYS = frozenset({".git", "node_modules"})
_SKIP_DEFAULT = frozenset({"_trash", "_attachments", ".trash"})


def _bounded_limit(value: int, *, default: int, hard_max: int, name: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return min(value, hard_max)


def _bounded_frequencies(counter: Counter[str]) -> tuple[dict[str, int], int]:
    ordered = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    retained = ordered[:FREQUENCY_LIMIT]
    return dict(retained), len(ordered) - len(retained)


def _safe_span(diagnostic: Any) -> dict[str, int] | None:
    span = diagnostic.span
    if span is None:
        return None
    return {
        "start_line": span.start_line,
        "start_column": span.start_column,
        "end_line": span.end_line,
        "end_column": span.end_column,
    }


def _category_collisions(
    labels: dict[str, Counter[str]],
    *,
    identity_key: str,
    labels_key: str,
    example_limit: int,
) -> tuple[list[dict[str, Any]], int]:
    collisions = [
        {
            identity_key: identity,
            labels_key: sorted(authored),
            "count": sum(authored.values()),
        }
        for identity, authored in sorted(labels.items())
        if len(authored) > 1
    ]
    return collisions[:example_limit], max(0, len(collisions) - example_limit)


def _governance_report(
    root: Path,
    *,
    scanned_paths: list[str],
    complete_root_scan: bool,
) -> dict[str, Any]:
    kb_present = (root / kb_dirname()).is_dir()
    unavailable = {
        "kb_present": kb_present,
        "saved_contracts": {
            "status": "unavailable" if not kb_present else "partial",
            "count": 0,
            "debt": {},
        },
        "relation_dispositions": {
            "status": "unavailable" if not kb_present else "partial",
            "counts": {},
        },
    }
    if not kb_present:
        return unavailable
    if not complete_root_scan:
        return unavailable

    try:
        saved = memory_schema.load_saved_contracts(root)
        batch = semantic_writes.evaluate_posthoc_batch(
            root,
            paths=[path for path in scanned_paths if path.startswith(kb_prefix())],
            operation="audit",
        )
    except (OSError, UnicodeDecodeError, ValueError) as error:
        unavailable["saved_contracts"].update(
            {
                "status": "error",
                "error": type(error).__name__,
            }
        )
        unavailable["relation_dispositions"].update(
            {
                "status": "error",
                "error": type(error).__name__,
            }
        )
        return unavailable

    debt: Counter[str] = Counter()
    dispositions: Counter[str] = Counter()
    for evaluation in batch.evaluations:
        for finding in evaluation.contract_result.findings:
            if finding.code.startswith("CONTRACT_"):
                debt[finding.code] += 1
        disposition = evaluation.contract_result.relation_disposition
        if disposition is not None:
            dispositions[disposition.kind] += 1
    return {
        "kb_present": True,
        "saved_contracts": {
            "status": "current",
            "count": len(saved),
            "debt": dict(sorted(debt.items())),
        },
        "relation_dispositions": {
            "status": "current",
            "counts": dict(sorted(dispositions.items())),
        },
    }


def _safe_next_actions(
    *,
    kb_present: bool,
    malformed: int,
    category_collisions: int,
    alias_collisions: int,
    registry_findings: int,
    contract_debt: int,
    relation_debt: int,
    truncated: bool,
) -> list[dict[str, str]]:
    actions: list[dict[str, str]] = []
    if malformed:
        actions.append(
            {
                "action": "review-malformed-candidates",
                "description": "Review the bounded path/span examples; edit originals only after human confirmation.",
            }
        )
    if category_collisions or alias_collisions or registry_findings:
        actions.append(
            {
                "action": "review-category-governance",
                "description": "Review raw/canonical/resolved frequencies before proposing aliases; open categories remain valid.",
            }
        )
    if contract_debt:
        actions.append(
            {
                "action": "review-saved-contract-debt",
                "description": "Inspect saved-contract findings through the normal read-only audit/review surface.",
            }
        )
    if relation_debt:
        actions.append(
            {
                "action": "review-relation-dispositions",
                "description": "Review missing or stale dispositions without fabricating relations or review decisions.",
            }
        )
    if truncated:
        actions.append(
            {
                "action": "rescan-narrower-subtree",
                "description": "Scan a narrower subtree or deliberately raise the bounded census limits.",
            }
        )
    if not kb_present:
        actions.append(
            {
                "action": "initialize-kb",
                "description": "Initialize the governed Knowledge Base only when ready; the census does not require it.",
            }
        )
    if not actions:
        actions.append(
            {
                "action": "review-census",
                "description": "Use the census as a read-only adoption baseline; no migration is implied.",
            }
        )
    return actions


def scan(
    root: Path | str,
    *,
    path: str = "",
    include_hidden: bool = False,
    max_files: int = DEFAULT_MAX_FILES,
    max_bytes: int = DEFAULT_MAX_BYTES,
    example_limit: int = DEFAULT_EXAMPLE_LIMIT,
) -> dict[str, Any]:
    """Return a deterministic semantic census without writing or loading a model."""
    root_path = Path(root)
    scan_root, scope = overview._resolve_subtree(root_path, path)
    effective_max_files = _bounded_limit(
        max_files,
        default=DEFAULT_MAX_FILES,
        hard_max=HARD_MAX_FILES,
        name="max_files",
    )
    effective_max_bytes = _bounded_limit(
        max_bytes,
        default=DEFAULT_MAX_BYTES,
        hard_max=HARD_MAX_BYTES,
        name="max_bytes",
    )
    effective_example_limit = _bounded_limit(
        example_limit,
        default=DEFAULT_EXAMPLE_LIMIT,
        hard_max=HARD_EXAMPLE_LIMIT,
        name="example_limit",
    )

    language = semantic_language_registry.load_registry(root_path)
    relations = relation_registry.load_registry(root_path)
    raw_frequencies: Counter[str] = Counter()
    canonical_frequencies: Counter[str] = Counter()
    resolved_frequencies: Counter[str] = Counter()
    canonical_raw: dict[str, Counter[str]] = defaultdict(Counter)
    resolved_canonical: dict[str, Counter[str]] = defaultdict(Counter)
    units = Counter(total=0, compact=0, rich=0)
    diagnostic_counts: Counter[str] = Counter()
    diagnostic_examples: list[dict[str, Any]] = []
    scanned_paths: list[str] = []
    markdown_seen = 0
    markdown_scanned = 0
    markdown_omitted = 0
    hidden_omitted = 0
    unreadable_files = 0
    bytes_scanned = 0
    parseable_pages = 0
    pages_with_diagnostics = 0

    for dirpath, dirnames, filenames in os.walk(scan_root):
        kept_dirs: list[str] = []
        for dirname in sorted(dirnames):
            if dirname in _SKIP_ALWAYS or (
                not include_hidden
                and (dirname.startswith(".") or dirname in _SKIP_DEFAULT)
            ):
                hidden_omitted += 1
                continue
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs
        for filename in sorted(filenames):
            if not include_hidden and filename.startswith("."):
                hidden_omitted += 1
                continue
            if not filename.casefold().endswith(".md"):
                continue
            markdown_seen += 1
            if markdown_scanned >= effective_max_files:
                markdown_omitted += 1
                continue
            disk_path = Path(dirpath) / filename
            try:
                info = disk_path.lstat()
            except OSError:
                unreadable_files += 1
                markdown_omitted += 1
                continue
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                unreadable_files += 1
                markdown_omitted += 1
                continue
            if info.st_size > effective_max_bytes - bytes_scanned:
                markdown_omitted += 1
                continue
            try:
                raw = disk_path.read_bytes()
                text = raw.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                unreadable_files += 1
                markdown_omitted += 1
                continue
            bytes_scanned += len(raw)
            markdown_scanned += 1
            rel_path = disk_path.relative_to(root_path).as_posix()
            scanned_paths.append(rel_path)
            state = semantic_contract.build_page_state(
                root_path,
                rel_path,
                text,
                relation_registry=relations,
                language_registry=language,
            )
            document = state.document
            if document.is_valid:
                parseable_pages += 1
            else:
                pages_with_diagnostics += 1
            for unit in document.units:
                units["total"] += 1
                units[unit.form] += 1
                raw_frequencies[unit.category_raw] += 1
                canonical_frequencies[unit.category_key] += 1
                resolved_frequencies[unit.category] += 1
                canonical_raw[unit.category_key][unit.category_raw] += 1
                resolved_canonical[unit.category][unit.category_key] += 1
            for diagnostic in document.errors:
                if diagnostic.registry_namespace is not None:
                    continue
                diagnostic_counts[diagnostic.code] += 1
                if len(diagnostic_examples) < effective_example_limit:
                    diagnostic_examples.append(
                        {
                            "path": rel_path,
                            "code": diagnostic.code,
                            "severity": diagnostic.severity,
                            "line": diagnostic.line,
                            "span": _safe_span(diagnostic),
                            "remediation": diagnostic.remediation,
                        }
                    )

    canonical_collisions, canonical_collision_omitted = _category_collisions(
        canonical_raw,
        identity_key="canonical",
        labels_key="raw_labels",
        example_limit=effective_example_limit,
    )
    resolved_alias_collisions, alias_collision_omitted = _category_collisions(
        resolved_canonical,
        identity_key="resolved",
        labels_key="canonical_labels",
        example_limit=effective_example_limit,
    )
    raw_values, raw_omitted = _bounded_frequencies(raw_frequencies)
    canonical_values, canonical_omitted = _bounded_frequencies(canonical_frequencies)
    resolved_values, resolved_omitted = _bounded_frequencies(resolved_frequencies)
    registry_findings = [
        finding.as_dict() for finding in language.findings[:effective_example_limit]
    ]
    registry_findings_omitted = max(
        0, len(language.findings) - effective_example_limit
    )
    complete_root_scan = not scope and markdown_omitted == 0 and unreadable_files == 0
    governance = _governance_report(
        root_path,
        scanned_paths=scanned_paths,
        complete_root_scan=complete_root_scan,
    )
    contract_debt = sum(governance["saved_contracts"]["debt"].values())
    relation_counts = governance["relation_dispositions"]["counts"]
    relation_debt = sum(
        count for kind, count in relation_counts.items() if kind in {"missing", "stale"}
    )
    truncated = markdown_omitted > 0
    malformed_count = sum(diagnostic_counts.values())
    return {
        "read_only": True,
        "scope": scope,
        "limits": {
            "max_files": effective_max_files,
            "max_bytes": effective_max_bytes,
            "example_limit": effective_example_limit,
            "include_hidden": include_hidden,
        },
        "coverage": {
            "markdown_files_seen": markdown_seen,
            "markdown_files_scanned": markdown_scanned,
            "markdown_files_omitted": markdown_omitted,
            "bytes_scanned": bytes_scanned,
            "parseable_pages": parseable_pages,
            "pages_with_diagnostics": pages_with_diagnostics,
            "unreadable_files": unreadable_files,
            "hidden_entries_omitted": hidden_omitted,
            "truncated": truncated,
        },
        "units": dict(units),
        "categories": {
            "open_categories_valid": True,
            "raw_frequencies": raw_values,
            "canonical_frequencies": canonical_values,
            "resolved_frequencies": resolved_values,
            "frequency_labels_omitted": {
                "raw": raw_omitted,
                "canonical": canonical_omitted,
                "resolved": resolved_omitted,
            },
            "canonical_collisions": canonical_collisions,
            "canonical_collisions_omitted": canonical_collision_omitted,
            "resolved_alias_collisions": resolved_alias_collisions,
            "resolved_alias_collisions_omitted": alias_collision_omitted,
            "alias_conflicts": registry_findings,
            "alias_conflicts_omitted": registry_findings_omitted,
        },
        "diagnostics": {
            "malformed_candidates": malformed_count,
            "counts": dict(sorted(diagnostic_counts.items())),
            "examples": diagnostic_examples,
            "examples_omitted": max(0, malformed_count - len(diagnostic_examples)),
        },
        "governance": governance,
        "safe_next_actions": _safe_next_actions(
            kb_present=governance["kb_present"],
            malformed=malformed_count,
            category_collisions=len(canonical_collisions) + canonical_collision_omitted,
            alias_collisions=len(resolved_alias_collisions) + alias_collision_omitted,
            registry_findings=len(language.findings),
            contract_debt=contract_debt,
            relation_debt=relation_debt,
            truncated=truncated,
        ),
    }
