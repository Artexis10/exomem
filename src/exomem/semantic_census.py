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
    vault,
)
from .kbdir import kb_dirname, kb_prefix

DEFAULT_MAX_FILES = 512
DEFAULT_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_EXAMPLE_LIMIT = 12
HARD_MAX_FILES = 4096
HARD_MAX_BYTES = 32 * 1024 * 1024
HARD_EXAMPLE_LIMIT = 50
FREQUENCY_LIMIT = 128
MIN_DIRECTORY_ENTRY_BUDGET = 32
DIRECTORY_ENTRIES_PER_FILE = 16

_SKIP_ALWAYS = frozenset({".git", "node_modules"})
_SKIP_DEFAULT = frozenset({"_trash", "_attachments", ".trash"})


def _directory_entry_budget(max_files: int) -> int:
    return max(MIN_DIRECTORY_ENTRY_BUDGET, max_files * DIRECTORY_ENTRIES_PER_FILE)


def _is_reparse(info: os.stat_result) -> bool:
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(getattr(info, "st_file_attributes", 0) & marker)


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        getattr(left, "st_dev", None) == getattr(right, "st_dev", None)
        and getattr(left, "st_ino", None) == getattr(right, "st_ino", None)
        and left.st_mode == right.st_mode
    )


def _same_file_state(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        _same_identity(left, right)
        and left.st_size == right.st_size
        and getattr(left, "st_mtime_ns", None)
        == getattr(right, "st_mtime_ns", None)
        and getattr(left, "st_ctime_ns", None)
        == getattr(right, "st_ctime_ns", None)
    )


def _open_regular_file_descriptor(root: Path, path: Path) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        from . import vault

        return vault._open_windows_path_descriptor(
            path,
            desired_access=0x80000000,
            attributes=0x00200000,
            crt_flags=os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory or os.open not in os.supports_dir_fd:
        raise RuntimeError("safe descriptor-relative open unavailable")
    relative = path.relative_to(root)
    parts = relative.parts
    if not parts:
        raise RuntimeError("safe descriptor-relative open requires a file")

    directory_fd: int | None = None
    file_fd: int | None = None
    try:
        directory_flags = os.O_RDONLY | directory | nofollow | getattr(os, "O_CLOEXEC", 0)
        directory_fd = os.open(root, directory_flags)
        root_info = os.fstat(directory_fd)
        if not stat.S_ISDIR(root_info.st_mode) or _is_reparse(root_info):
            raise RuntimeError("unsafe census root")
        for component in parts[:-1]:
            next_fd = os.open(
                component,
                directory_flags,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = next_fd
            directory_info = os.fstat(directory_fd)
            if not stat.S_ISDIR(directory_info.st_mode) or _is_reparse(directory_info):
                raise RuntimeError("unsafe census directory")
        file_fd = os.open(parts[-1], flags, dir_fd=directory_fd)
        descriptor = file_fd
        file_fd = None
        return descriptor
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def _read_regular_file_bounded(
    root: Path,
    path: Path,
    remaining: int,
) -> tuple[str, bytes | None]:
    """Read one stable regular file without following links or exceeding remaining+1."""
    try:
        expected = path.lstat()
    except OSError:
        return "unsafe", None
    if (
        not stat.S_ISREG(expected.st_mode)
        or stat.S_ISLNK(expected.st_mode)
        or _is_reparse(expected)
    ):
        return "unsafe", None

    try:
        descriptor = _open_regular_file_descriptor(root, path)
    except (OSError, RuntimeError, ValueError):
        return "unsafe", None
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _is_reparse(opened)
            or not _same_identity(expected, opened)
        ):
            return "unsafe", None
        if opened.st_size > remaining:
            return "oversized", None

        chunks: list[bytes] = []
        total = 0
        while total <= remaining:
            chunk = os.read(descriptor, min(65536, remaining + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > remaining:
            return "oversized", None
        after = os.fstat(descriptor)
        if not _same_file_state(opened, after):
            return "unsafe", None
    except OSError:
        return "unsafe", None
    finally:
        os.close(descriptor)

    try:
        current = path.lstat()
    except OSError:
        return "unsafe", None
    if _is_reparse(current) or not _same_file_state(opened, current):
        return "unsafe", None
    return "ok", b"".join(chunks)


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


def _registry_finding_payload(
    finding: semantic_language_registry.RegistryFinding,
) -> dict[str, str]:
    payload = finding.as_dict()
    payload["namespace"] = finding.path.split(".", 1)[0]
    return payload


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
    states: tuple[semantic_contract.SemanticPageState, ...],
    relations: relation_registry.RelationRegistry,
    language: semantic_language_registry.SemanticLanguageRegistry,
    complete_root_scan: bool,
) -> tuple[dict[str, Any], set[str]]:
    metadata_work: set[str] = set()
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
        return unavailable, metadata_work
    if not complete_root_scan:
        unavailable["resource_status"] = "partial_corpus"
        return unavailable, metadata_work

    kb_states = tuple(
        state for state in states if state.path.startswith(kb_prefix())
    )
    try:
        identity_census = semantic_contract.StableIdentityCensus.from_states(kb_states)
    except ValueError as error:
        unavailable["saved_contracts"].update(
            {"status": "error", "error": type(error).__name__}
        )
        unavailable["relation_dispositions"].update(
            {"status": "error", "error": type(error).__name__}
        )
        unavailable["resource_status"] = "partial_identity_error"
        return unavailable, metadata_work

    corpus_states = tuple(
        state
        for state in states
        if not vault.in_excluded_scan_dir(state.path)
        and ".sync-conflict-" not in Path(state.path).name
    )
    metadata_work.update({"activation_manifest", "saved_contracts"})
    try:
        saved = memory_schema.load_saved_contracts(root)
        corpus = semantic_contract.SemanticCorpusContext.from_states(
            root,
            corpus_states,
            registry=relations,
            identity_census=identity_census,
        )
        if any(state.eligible_compiled for state in kb_states):
            metadata_work.add("relation_review_state")
        batch = semantic_writes.evaluate_posthoc_batch(
            root,
            paths=[state.path for state in kb_states],
            operation="audit",
            corpus=corpus,
            language_registry=language,
            saved_contracts=saved,
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
        unavailable["resource_status"] = "partial_metadata_error"
        return unavailable, metadata_work

    debt: Counter[str] = Counter()
    dispositions: Counter[str] = Counter()
    for evaluation in batch.evaluations:
        for finding in evaluation.contract_result.findings:
            if finding.code.startswith("CONTRACT_"):
                debt[finding.code] += 1
        disposition = evaluation.contract_result.relation_disposition
        if disposition is not None:
            dispositions[disposition.kind] += 1
    return (
        {
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
            "resource_status": "unbounded_metadata",
        },
        metadata_work,
    )


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
    effective_max_directory_entries = _directory_entry_budget(effective_max_files)

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
    page_states: list[semantic_contract.SemanticPageState] = []
    markdown_seen = 0
    markdown_scanned = 0
    markdown_omitted = 0
    hidden_omitted = 0
    unreadable_files = 0
    bytes_scanned = 0
    parseable_pages = 0
    pages_with_diagnostics = 0
    directory_entries_enumerated = 0
    unreadable_directories = 0
    enumeration_incomplete = False
    directory_entry_budget_exhausted = False
    pending_directories = [scan_root]

    while pending_directories:
        dirpath = pending_directories.pop()
        entries: list[os.DirEntry[str]] = []
        try:
            with os.scandir(dirpath) as iterator:
                while directory_entries_enumerated < effective_max_directory_entries:
                    try:
                        entry = next(iterator)
                    except StopIteration:
                        break
                    directory_entries_enumerated += 1
                    entries.append(entry)
        except OSError:
            unreadable_directories += 1
            enumeration_incomplete = True
            continue

        reached_entry_budget = (
            directory_entries_enumerated >= effective_max_directory_entries
        )
        child_directories: list[Path] = []
        for entry in sorted(entries, key=lambda item: item.name):
            filename = entry.name
            try:
                entry_info = entry.stat(follow_symlinks=False)
            except OSError:
                if filename.casefold().endswith(".md"):
                    markdown_seen += 1
                    markdown_omitted += 1
                    unreadable_files += 1
                continue
            is_directory = (
                stat.S_ISDIR(entry_info.st_mode)
                and not stat.S_ISLNK(entry_info.st_mode)
                and not _is_reparse(entry_info)
            )
            if is_directory:
                if filename in _SKIP_ALWAYS or (
                    not include_hidden
                    and (filename.startswith(".") or filename in _SKIP_DEFAULT)
                ):
                    hidden_omitted += 1
                    continue
                child_directories.append(Path(entry.path))
                continue
            if not include_hidden and filename.startswith("."):
                hidden_omitted += 1
                continue
            if not filename.casefold().endswith(".md"):
                continue
            markdown_seen += 1
            if markdown_scanned >= effective_max_files:
                markdown_omitted += 1
                continue
            disk_path = Path(entry.path)
            read_status, raw = _read_regular_file_bounded(
                root_path,
                disk_path,
                effective_max_bytes - bytes_scanned,
            )
            if read_status == "oversized":
                markdown_omitted += 1
                continue
            if read_status != "ok" or raw is None:
                unreadable_files += 1
                markdown_omitted += 1
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                unreadable_files += 1
                markdown_omitted += 1
                continue
            bytes_scanned += len(raw)
            markdown_scanned += 1
            rel_path = disk_path.relative_to(root_path).as_posix()
            state = semantic_contract.build_page_state(
                root_path,
                rel_path,
                text,
                relation_registry=relations,
                language_registry=language,
            )
            page_states.append(state)
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
        if reached_entry_budget:
            enumeration_incomplete = True
            directory_entry_budget_exhausted = True
            break
        pending_directories.extend(reversed(child_directories))

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
        _registry_finding_payload(finding)
        for finding in language.findings[:effective_example_limit]
    ]
    registry_findings_omitted = max(
        0, len(language.findings) - effective_example_limit
    )
    category_alias_findings = tuple(
        finding
        for finding in language.findings
        if finding.code == "alias_conflict"
        and finding.path.startswith("categories.")
    )
    alias_conflicts = [
        _registry_finding_payload(finding)
        for finding in category_alias_findings[:effective_example_limit]
    ]
    alias_conflicts_omitted = max(
        0, len(category_alias_findings) - effective_example_limit
    )
    complete_root_scan = (
        not scope
        and not enumeration_incomplete
        and markdown_omitted == 0
        and unreadable_files == 0
        and unreadable_directories == 0
    )
    governance_complete = complete_root_scan and hidden_omitted == 0
    governance, governance_metadata_work = _governance_report(
        root_path,
        states=tuple(page_states),
        relations=relations,
        language=language,
        complete_root_scan=governance_complete,
    )
    metadata_work = {
        "relation_registry",
        "semantic_language_registry",
        *governance_metadata_work,
    }
    contract_debt = sum(governance["saved_contracts"]["debt"].values())
    relation_counts = governance["relation_dispositions"]["counts"]
    relation_debt = sum(
        count for kind, count in relation_counts.items() if kind in {"missing", "stale"}
    )
    truncated = markdown_omitted > 0 or enumeration_incomplete
    coverage_complete = (
        not truncated and unreadable_files == 0 and unreadable_directories == 0
    )
    malformed_count = sum(diagnostic_counts.values())
    return {
        "read_only": True,
        "scope": scope,
        "limits": {
            "max_files": effective_max_files,
            "max_bytes": effective_max_bytes,
            "max_directory_entries": effective_max_directory_entries,
            "example_limit": effective_example_limit,
            "include_hidden": include_hidden,
        },
        "coverage": {
            "complete": coverage_complete,
            "governance_complete": governance_complete,
            "omitted_is_lower_bound": enumeration_incomplete,
            "markdown_files_seen": markdown_seen,
            "markdown_files_seen_is_lower_bound": enumeration_incomplete,
            "markdown_files_scanned": markdown_scanned,
            "markdown_files_omitted": markdown_omitted,
            "bytes_scanned": bytes_scanned,
            "parseable_pages": parseable_pages,
            "pages_with_diagnostics": pages_with_diagnostics,
            "unreadable_files": unreadable_files,
            "unreadable_directories": unreadable_directories,
            "hidden_entries_omitted": hidden_omitted,
            "directory_entries_enumerated": directory_entries_enumerated,
            "directory_entry_budget_exhausted": directory_entry_budget_exhausted,
            "metadata_work": {
                "status": "unbounded",
                "bounded": False,
                "counted_as_markdown_bytes": False,
                "sources": sorted(metadata_work),
            },
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
            "registry_findings": registry_findings,
            "registry_findings_omitted": registry_findings_omitted,
            "alias_conflicts": alias_conflicts,
            "alias_conflicts_omitted": alias_conflicts_omitted,
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
