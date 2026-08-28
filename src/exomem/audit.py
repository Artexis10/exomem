"""Read-only audit of the Knowledge Base. Returns structured findings.

Checks (all read-only; no writes ever):
- `broken_wikilink`: `[[...]]` with a definite resolution error.
- `forward_reference`: `[[...]]` to a Markdown page that does not exist yet.
  Skips wikilinks inside fenced code blocks and inline code spans (so
  `[[:space:]]` regex literals don't false-positive). Bare names resolve
  against filename stems AND frontmatter `title:` (so date-prefixed
  sources with a title match are not flagged).
- `orphan_entity`: file under `Entities/` with no inbound wikilinks from
  anywhere in `Knowledge Base/`
- `unprocessed_source`: `type: source` page whose `ingested_into:` is empty
- `unresolved_source_citation`: compiled page whose explicit `sources:` claim
  does not resolve to authorized governed Source or Evidence material
- `index_drift`: top-level `index.md` Counts disagree with on-disk counts
- `tag_inconsistency`: case/separator variants of the same tag
- `frontmatter_compliance`: per-page-type required-field gaps,
  `tenant:` set without `project: q`, patterns using `project:` (singular)
  instead of `projects:` (plural list)
- `stale_review`: active compiled conclusion that is old AND rarely surfaced in
  `find` AND low inbound-link degree — a measurement-only review candidate.
  Surfaces it for the reader to judge (keep / supersede / archive); never
  decays, down-ranks, or moves anything (`find` ordering is unchanged).
- `unfinished_experiments`: an `experiment` whose `started` date is present, whose
  elapsed time EXCEEDS its declared `duration`, and which records no `outcome:`.
  The trigger is the missing outcome, not `status`: `status: concluded` says the
  experiment stopped, `outcome:` says what it showed, and only the second closes
  the loop. An open-ended or unparseable `duration` (`ongoing`) declares no
  window and so can never exceed one — never flagged. Measurement-only at `info`,
  ordered oldest-first; nothing is auto-concluded or archived.
- `prediction_window`: a rich semantic unit whose authored `check_by` date has
  arrived or passed with nothing recorded against it. "Nothing recorded" is
  deliberately UNIT-LOCAL: no `verdict` on the unit, and no outbound relation on
  the unit itself resolving to `supports`/`contradicts`/`resolves`/`evidenced_by`.
  Inbound edges do not count, because relation targets still lose their
  `#fragment` and so cannot address a unit. Measurement-only at `info`, ordered
  most-overdue-first, partitioned per unit by fingerprint so each due prediction
  is its own review item; never judges whether the prediction held.
- `question_aging`: a governed question unit on an active page whose authored
  date is at least `QUESTION_AGING_DAYS` old, carrying no `verdict` and no
  answering relation ON THE UNIT (same unit-local rule `prediction_window`
  uses). Reported at `info` as a review CANDIDATE, never a defect, and kept out
  of the default attention union — its threshold is the system's invention, not
  something an author declared.
- `supersession_integrity`: a `supersedes`/`superseded_by` pointer that does not
  resolve, and a supersession component with more than one current head. Both
  are `warn` DEFECTS in authored state with no threshold anywhere, which is why
  this one does join the default attention union. Parked statuses are NOT
  excluded: a `superseded` page is exactly where a rotted forward pointer lives.
- `derivation_double_counting` (optional): walks `sources:` (`derived_from`)
  chains for two failures ordinary checks cannot see. Support collapse: a
  compiled page cites two or more sources as independent support that
  themselves trace back to a shared ancestor — a source laundered into
  "multiple sources agree". Circular derivation: a `sources:` chain that
  loops back on itself. Both are informational/warn only, never error; the
  traversal is bounded by depth and total-edge caps, and a dedicated
  `truncated` finding makes a hit cap visible rather than silently reading as
  "nothing found".
- `corpus_contradictions`: corpus-wide sweep for pairs of ACTIVE read-write
  COMPILED conclusions whose embeddings sit in the contradiction band
  `[floor, dup_threshold)` — close enough to plausibly restate/refine/contradict
  each other, but not near-duplicates. A PROXIMITY measurement, not a stance
  judgment (cosine can't tell agreement from contradiction); deduped pairs are
  surfaced for the reader to reconcile or supersede. The queue is ORDERED by a
  review priority (cosine + ACT-R dormancy of the pair's notes), same-family
  `Notes/Research/<X>/` architecture noise is demoted, and the surfaced set is
  capped at `EXOMEM_CONTRADICTION_TOP_N` with an explicit omitted count.
  Ordering/capping is measure-only — never auto-acts, never touches `find`.
  No-ops cleanly when embeddings are disabled.

Audit is the diagnostic counterpart to the writers. Output is a proposal
report; nothing is rewritten without explicit confirmation via the
existing write tools (no auto-fix).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import math
import os
import re
import stat
import sys
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import yaml

from . import (
    access,
    contradiction_stance,
    indexes,
    logging_config,
    relation_registry,
    reserved_paths,
    semantic_language_registry,
    semantic_units,
    source_closure,
    temporal,
)
from . import entity_types as entity_types_module
from . import find as find_module
from . import vault as vault_module
from .kbdir import kb_dirname, kb_prefix
from .vault import (
    _mask_code_spans,
    content_hash,
    find_body_wikilinks,
    in_append_only_tree,
    kb_root,
    parse_frontmatter,
)

log = logging.getLogger(__name__)

ALL_CATEGORIES: tuple[str, ...] = (
    "broken_wikilink",
    "forward_reference",
    "orphan_entity",
    "unprocessed_source",
    "unresolved_source_citation",
    "index_drift",
    "tag_inconsistency",
    "frontmatter_compliance",
    "unregistered_project_key",
    "embedding_drift",
    "graph_drift",
    "reference_identity",
    "relevance_pairs_pending",
    "stale_review",
    "corpus_contradictions",
    "relation_debt",
    "governance_receipts",
    "bridge_review",
    "duplicated_sidecar",
    "semantic_recall_isolation",
    "unfinished_experiments",
    "prediction_window",
    "question_aging",
    "supersession_integrity",
    "entity_type_unregistered",
    "unreflected_outcomes",
)
OPTIONAL_CATEGORIES: tuple[str, ...] = (
    "relation_registry",
    "missing_sources",
    "derivation_double_counting",
    "semantic_contract_drift",
    "semantic_malformed_unit",
    "semantic_category_governance",
    "semantic_strict_schema_drift",
    "semantic_relation_disposition",
)
TYPED_SEMANTIC_CATEGORIES: tuple[str, ...] = (
    "semantic_malformed_unit",
    "semantic_category_governance",
    "semantic_strict_schema_drift",
    "semantic_relation_disposition",
)
# Epistemic LIFECYCLE queues that are OPT-IN: registered as selectable
# `attention` categories but deliberately kept OUT of its default union (see
# `attention.DEFAULT_ATTENTION_CATEGORIES`).
#
# The gate is BACKLOG PROFILE, not category kind. `prediction_window` is the same
# sort of lifecycle check and it IS in the default union, because the fields it
# reads (`check_by`, the `prediction` kind) shipped with the epistemic loop
# primitives and no vault can hold a grandfathered population of them. The fields
# `unfinished_experiments` reads (`started`, `duration`) predate the package
# rename, so a long-lived vault can genuinely hold dozens of long-closed windows,
# and dropping all of them onto the daily surface at upgrade time would evict the
# signal already there rather than add to it.
#
# That is the activation-manifest precedent applied where it bites: a new
# category surfaces grandfathered items as review CANDIDATES, never as blocking
# findings, and never by displacing a surface someone already relies on. Where
# there is no grandfathered population, the precedent has nothing to protect and
# opt-in only hides the queue from the people it exists for.
EPISTEMIC_REVIEW_CATEGORIES: tuple[str, ...] = (
    "unfinished_experiments",
    # Same exclusion, a different reason for it, and the difference matters.
    # `unfinished_experiments` is held back by a grandfathered POPULATION;
    # `question_aging` is held back because its trigger is a THRESHOLD THIS
    # SYSTEM CHOSE (`QUESTION_AGING_DAYS`) rather than one an author declared.
    # A queue that fires on a date a human wrote down has earned the daily
    # surface; a queue that fires on a number the product picked has not, and
    # admitting one would let a tuning decision quietly displace authored work.
    "question_aging",
)
_SEMANTIC_AUDIT_CATEGORIES = frozenset({"semantic_contract_drift", *TYPED_SEMANTIC_CATEGORIES})
_LEGACY_BACKLOG_CODE = "RELATION_DISPOSITION_MISSING"
DEFAULT_LEGACY_SAMPLE_LIMIT = 5
MAX_LEGACY_SAMPLE_LIMIT = 50

# Feedback-loop logs (written by the running service) + the golden query set,
# used by the relevance_pairs_pending check. Module-level so tests can
# monkeypatch them to point at an isolated fixture. `_RELEVANCE_LOGS_DIR` MUST
# route through the same `resolve_log_dir()` every other log file uses —
# `queries.jsonl`/`writes.jsonl` live under it (via `query_log.py`), and a
# hardcoded `<repo>/logs` guess here left them unfindable once
# EXOMEM_LOG_DIR-unset resolution stopped assuming a checkout (issue #552).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_RELEVANCE_LOGS_DIR = logging_config.resolve_log_dir()
_RELEVANCE_GOLDEN = _REPO_ROOT / "tests" / "golden" / "queries.yaml"

# Matches [[Target]] or [[Target|Alias]]. Target may contain '/' for paths.
WIKILINK_PATTERN = re.compile(r"\[\[([^\]\|\n]+?)(?:\|[^\]\n]*)?\]\]")

# When walking the full vault to build the wikilink-resolution set, skip these.
VAULT_WALK_SKIP_DIRS = frozenset(
    {
        ".obsidian",
        ".git",
        ".trash",
        "_attachments",
        "_archive",
        "_trash",
    }
)

# Counts row in index.md. Captures (label, optional-subcategory, count).
# Matches lines like:
#   - Sources: 3 (articles: 1, ...)
#   - Notes (research): 2
#   - Entities (person): 1
_COUNTS_ROW_PATTERN = re.compile(
    r"^- (Sources|Notes|Entities)(?:\s*\(([^)]+)\))?:\s*(\d+)\b",
    re.MULTILINE,
)


@dataclass
class AuditFinding:
    category: str       # one of ALL_CATEGORIES
    severity: str       # "info" | "warn" | "error"
    path: str           # vault-relative path of the affected page (or "index.md")
    detail: str         # one-line human description
    proposed_fix: str | None = None
    # Optional cluster/aging context, surfaced only when set (mirrors
    # find.Hit.signals' omit-when-empty convention so existing findings and
    # the test suite see no shape change). `paths` carries a multi-file group;
    # `meta` carries structured extras like age_days / age_bucket.
    paths: list[str] | None = None
    meta: dict | None = None
    #: Server-internal, never serialised (see `as_dict`, which whitelists).
    #: A family whose finding depends on OTHER pages an audience may or may not
    #: be allowed to see puts the primitives to re-derive it here, so a consumer
    #: holding a narrower audience rebuilds the finding through the family's own
    #: composer instead of forming a second opinion about what it means. Only
    #: `unreflected_outcomes` sets it; see `unreflected_component`.
    component: dict | None = None

    def as_dict(self) -> dict:
        out: dict = {
            "category": self.category,
            "severity": self.severity,
            "path": self.path,
            "detail": self.detail,
            "proposed_fix": self.proposed_fix,
        }
        if self.paths:
            out["paths"] = self.paths
        if self.meta:
            out["meta"] = self.meta
        return out


@dataclass
class AuditReport:
    findings: list[AuditFinding]
    summary: dict[str, int]  # category → count
    metadata: dict | None = None

    def as_dict(self) -> dict:
        value = {
            "findings": [f.as_dict() for f in self.findings],
            "summary": self.summary,
        }
        if self.metadata:
            value["metadata"] = self.metadata
        return value

    def as_public_dict(
        self,
        *,
        detail: Literal["actionable", "full"] = "actionable",
        legacy_sample_limit: int = DEFAULT_LEGACY_SAMPLE_LIMIT,
    ) -> dict:
        """Project raw diagnostic truth for action-first product consumers."""
        validate_presentation_controls(detail, legacy_sample_limit)
        upstream = _semantic_upstream_facts(self.metadata)
        if detail == "full":
            full_value = self.as_dict()
            full_value["detail"] = "full"
            full_value["presentation"] = {
                "grouped_legacy_backlog": False,
                "upstream_findings_complete": upstream["findings_complete"],
                "upstream_omitted_count": upstream["omitted_count"],
            }
            return full_value

        legacy = _unique_legacy_backlog_findings(self.findings)
        actionable = sorted(
            (finding for finding in self.findings if not _is_legacy_backlog(finding)),
            key=_actionable_finding_sort_key,
        )
        public_summary = dict(self.summary)
        public_summary.update(upstream["category_summary"])
        value: dict[str, Any] = {
            "detail": "actionable",
            "findings": [finding.as_dict() for finding in actionable],
            "summary": public_summary,
        }
        observed_count = upstream["summary"].get(_LEGACY_BACKLOG_CODE, len(legacy))
        if observed_count:
            samples = []
            for finding in legacy[:legacy_sample_limit]:
                sample = finding.as_dict()
                sample["raw_severity"] = sample["severity"]
                sample["severity"] = "info"
                sample["presentation"] = "legacy_backlog"
                samples.append(sample)
            value["legacy_backlog"] = {
                "code": _LEGACY_BACKLOG_CODE,
                "severity": "info",
                "kind": "legacy_backlog",
                "observed_count": observed_count,
                "observed_complete": upstream["observation_complete"],
                "available_sample_count": len(legacy),
                "sample_limit": legacy_sample_limit,
                "sample_omitted_count": max(0, observed_count - len(samples)),
                "upstream_findings_truncated": not upstream["findings_complete"],
                "upstream_omitted_count": upstream["omitted_count"],
                "samples": samples,
            }
        value["presentation"] = {
            "grouped_legacy_backlog": bool(observed_count),
            "actionable_count": len(actionable),
            "upstream_findings_complete": upstream["findings_complete"],
            "upstream_omitted_count": upstream["omitted_count"],
        }
        if self.metadata:
            value["metadata"] = self.metadata
        return value


def validate_presentation_controls(detail: str, legacy_sample_limit: int) -> None:
    if detail not in {"actionable", "full"}:
        raise ValueError("INVALID_AUDIT_DETAIL: detail must be 'actionable' or 'full'")
    if (
        isinstance(legacy_sample_limit, bool)
        or not isinstance(legacy_sample_limit, int)
        or not 0 <= legacy_sample_limit <= MAX_LEGACY_SAMPLE_LIMIT
    ):
        raise ValueError(
            "INVALID_AUDIT_SAMPLE_LIMIT: legacy_sample_limit must be an integer "
            f"from 0 to {MAX_LEGACY_SAMPLE_LIMIT}"
        )


def _semantic_upstream_facts(metadata: dict | None) -> dict[str, Any]:
    semantic = (metadata or {}).get("semantic_contract_drift") or {}
    omitted = semantic.get("omitted_counts") or {}
    truncation = semantic.get("truncation") or {}
    omitted_count = int(omitted.get("semantic_contract_findings") or 0)
    return {
        "summary": dict(semantic.get("semantic_contract_summary") or {}),
        "category_summary": dict(semantic.get("semantic_category_summary") or {}),
        "omitted_count": omitted_count,
        "observation_complete": bool(truncation.get("observation_complete", omitted_count == 0)),
        "findings_complete": bool(truncation.get("findings_complete", omitted_count == 0)),
    }


def _is_legacy_backlog(finding: AuditFinding) -> bool:
    meta = finding.meta or {}
    return bool(meta.get("code") == _LEGACY_BACKLOG_CODE and meta.get("grandfathered") is True)


def _finding_identity(finding: AuditFinding) -> str:
    meta = finding.meta or {}
    return json.dumps(
        meta.get("finding_key") or {},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _unique_legacy_backlog_findings(
    findings: list[AuditFinding],
) -> list[AuditFinding]:
    unique: dict[tuple[str, str], AuditFinding] = {}
    for finding in findings:
        if _is_legacy_backlog(finding):
            unique.setdefault((finding.path, _finding_identity(finding)), finding)
    return sorted(
        unique.values(),
        key=lambda finding: (
            finding.category,
            finding.path,
            _finding_identity(finding),
        ),
    )


def _actionable_finding_sort_key(finding: AuditFinding) -> tuple[str | int, ...]:
    meta = finding.meta or {}
    current = meta.get("activation") in {None, "current"}
    grandfathered = meta.get("grandfathered") is True
    if current and not grandfathered and finding.severity == "error":
        priority = 0
    elif finding.category in {
        "relation_registry",
        "semantic_malformed_unit",
        "semantic_category_governance",
    }:
        priority = 1
    else:
        priority = 2
    semantic_work_priority = 1 if finding.category == "relation_registry" else 0
    return (
        priority,
        semantic_work_priority,
        finding.category,
        str(meta.get("code") or ""),
        finding.path,
        _finding_identity(finding),
    )


def audit(
    vault_root: Path,
    *,
    categories: list[str] | None = None,
    today: dt.date | None = None,
    semantic_detail: Literal["actionable", "full"] = "actionable",
) -> AuditReport:
    """Scan the KB and return a structured findings report.

    `categories` filters which checks to run (default: all). Read-only.
    `today` is dependency-injectable for tests (used by unprocessed-source aging).
    """
    selected = set(categories) if categories else set(ALL_CATEGORIES)
    valid_categories = set(ALL_CATEGORIES) | set(OPTIONAL_CATEGORIES)
    invalid = selected - valid_categories
    if invalid:
        raise ValueError(
            f"unknown audit categories: {sorted(invalid)}. Valid: {sorted(valid_categories)}"
        )

    kb = kb_root(vault_root)
    pages = [] if selected <= _SEMANTIC_AUDIT_CATEGORIES else _parse_all(kb, vault_root)

    findings: list[AuditFinding] = []
    metadata: dict = {}
    link_categories = selected & {"broken_wikilink", "forward_reference"}
    if link_categories:
        findings.extend(_check_wikilinks(vault_root, pages, link_categories))
    if "orphan_entity" in selected:
        findings.extend(_check_orphan_entities(vault_root, pages))
    if "unprocessed_source" in selected:
        findings.extend(_check_unprocessed_sources(vault_root, pages, today=today))
    if "unresolved_source_citation" in selected:
        findings.extend(_check_unresolved_source_citations(vault_root, pages))
    if "index_drift" in selected:
        findings.extend(_check_index_drift(vault_root))
    if "tag_inconsistency" in selected:
        findings.extend(_check_tag_inconsistency(pages))
    if "frontmatter_compliance" in selected:
        findings.extend(_check_frontmatter_compliance(pages))
    if "entity_type_unregistered" in selected:
        findings.extend(_check_unregistered_entity_types(vault_root, pages))
    if "unregistered_project_key" in selected:
        findings.extend(_check_unregistered_project_keys(vault_root, pages))
    if "embedding_drift" in selected:
        findings.extend(_check_embedding_drift(vault_root))
    if "graph_drift" in selected:
        findings.extend(_check_graph_drift(vault_root))
    if "reference_identity" in selected:
        findings.extend(_check_reference_identity(vault_root))
    if "relevance_pairs_pending" in selected:
        findings.extend(_check_relevance_pairs_pending())
    if "stale_review" in selected:
        findings.extend(_check_stale_review(vault_root, pages, today=today))
    if "unfinished_experiments" in selected:
        findings.extend(_check_unfinished_experiments(vault_root, pages, today=today))
    if "prediction_window" in selected:
        findings.extend(_check_prediction_window(vault_root, pages, today=today))
    if "question_aging" in selected:
        findings.extend(_check_question_aging(vault_root, pages, today=today))
    if "unreflected_outcomes" in selected:
        outcome_findings, outcome_metadata = _check_unreflected_outcomes(vault_root)
        findings.extend(outcome_findings)
        if outcome_metadata:
            metadata["unreflected_outcomes"] = outcome_metadata
    if "supersession_integrity" in selected:
        findings.extend(_check_supersession_integrity(vault_root, pages))
    if "corpus_contradictions" in selected:
        findings.extend(_check_corpus_contradictions(vault_root, pages, today=today))
    if "relation_registry" in selected:
        findings.extend(_check_relation_registry(vault_root))
    if "relation_debt" in selected:
        findings.extend(_check_relation_debt(vault_root, pages))
    if "missing_sources" in selected:
        findings.extend(_check_missing_sources(vault_root, pages))
    if "derivation_double_counting" in selected:
        findings.extend(_check_derivation_double_counting(vault_root, pages))
    if "governance_receipts" in selected:
        findings.extend(_check_governance_receipts(vault_root))
    if "bridge_review" in selected:
        findings.extend(_check_bridge_review(vault_root, today=today))
    if "duplicated_sidecar" in selected:
        findings.extend(_check_duplicated_sidecars(vault_root))
    if "semantic_recall_isolation" in selected:
        findings.extend(_check_semantic_recall_isolation(vault_root))
    semantic_categories = selected & _SEMANTIC_AUDIT_CATEGORIES
    if semantic_categories:
        semantic_findings, semantic_metadata = _check_semantic_contract_drift(
            vault_root,
            categories=semantic_categories,
            detail=semantic_detail,
        )
        findings.extend(semantic_findings)
        metadata["semantic_contract_drift"] = semantic_metadata

    summary: dict[str, int] = {}
    for f in findings:
        summary[f.category] = summary.get(f.category, 0) + 1

    log.info(
        "audit complete: categories=%s findings=%d summary=%s",
        sorted(selected),
        len(findings),
        summary,
    )
    return AuditReport(
        findings=findings,
        summary=summary,
        metadata=metadata or None,
    )


def _check_bridge_review(
    vault_root: Path,
    *,
    today: dt.date | None,
) -> list[AuditFinding]:
    """Derive approval-bound bridge review work without persisting state."""
    from .governance import bridges, policy

    compiled = policy.load(vault_root)
    if compiled.empty or compiled.blocked:
        return []
    effective_today = today or dt.date.today()
    findings: list[AuditFinding] = []
    for grant in compiled.release_grants:
        signal = bridges.review_signal(
            Path(vault_root),
            grant,
            policy=compiled,
            today=effective_today,
        )
        if signal is None:
            continue
        partition = hashlib.sha256(f"{grant.id}\0{grant.to_audience}".encode()).hexdigest()[:24]
        findings.append(
            AuditFinding(
                category="bridge_review",
                severity="warn",
                path=grant.path,
                detail=f"Bridge review required: {signal.cause}.",
                proposed_fix=(
                    "Review the bridge and, when appropriate, separately commit "
                    "a new exact release approval."
                ),
                meta={
                    "bridge_audience": grant.to_audience,
                    "cause": signal.cause,
                    "review_date": signal.review_date,
                    "review_partition": partition,
                    "signal_version": signal.signal_version,
                },
            )
        )
    return sorted(
        findings,
        key=lambda finding: (
            finding.path,
            str((finding.meta or {}).get("review_partition") or ""),
        ),
    )


def _check_duplicated_sidecars(vault_root: Path) -> list[AuditFinding]:
    """Report media sidecars carrying nested copies of themselves.

    Sidecars are chunked and embedded whole, so an N-times duplicated document
    contributes N near-identical chunks. The distortion tracks how often a file
    happened to be reprocessed rather than anything about the file, so it
    arbitrarily suppresses the rest of the corpus in ranked results.
    """
    from . import sidecar_repair

    findings: list[AuditFinding] = []
    for sidecar in sidecar_repair.iter_media_sidecars(vault_root):
        try:
            content, _guard = vault_module.read_guarded_text(vault_root, sidecar)
        except (OSError, UnicodeError, vault_module.PathGuardError):
            continue
        damage = sidecar_repair.analyze(content, sidecar)
        if damage is None:
            continue
        rel = sidecar.relative_to(vault_root).as_posix()
        detail = f"{damage.depth} nested copies; {damage.duplicate_chars:,} duplicate chars"
        if damage.recovery_only:
            detail += "; extraction survives ONLY in a nested copy"
        elif damage.distinct_extractions > 1:
            detail += f"; {damage.distinct_extractions} differing extractions"
        findings.append(
            AuditFinding(
                category="duplicated_sidecar",
                severity="error" if damage.recovery_only else "warn",
                path=rel,
                detail=detail,
                proposed_fix=(
                    "maintain_memory(mode='fix') keeps the longest extraction and drops the nesting"
                ),
            )
        )
    return findings


# ---------------- check: semantic_recall_isolation ----------------

_SEMANTIC_ISOLATION_CENSUS_LIMIT = 256
_SEMANTIC_ISOLATION_CENSUS_FETCH = 64


@dataclass(frozen=True)
class _SemanticIsolationRow:
    component: str
    raw: str
    path: str | None
    edge_column: str | None = None
    missing: bool = False

    def as_dict(self) -> dict[str, str]:
        if self.path is not None:
            return {"component": self.component, "path": self.path}
        fingerprint = hashlib.sha256(self.raw.encode("utf-8", "replace")).hexdigest()[:16]
        return {
            "component": self.component,
            "path": f"<corrupt-sidecar-row:{fingerprint}>",
        }


@dataclass(frozen=True)
class SemanticRecallIsolationCensus:
    rows: tuple[_SemanticIsolationRow, ...]
    truncation: dict[str, int]
    continuation: dict[str, dict[str, str]]
    incomplete: dict[str, str]

    @property
    def safe_rows(self) -> tuple[_SemanticIsolationRow, ...]:
        return tuple(row for row in self.rows if row.path is not None and not row.missing)

    @property
    def missing_rows(self) -> tuple[_SemanticIsolationRow, ...]:
        return tuple(row for row in self.rows if row.path is not None and row.missing)

    @property
    def corrupt_rows(self) -> tuple[_SemanticIsolationRow, ...]:
        return tuple(row for row in self.rows if row.path is None)

    def safe_dicts(self) -> list[dict[str, str]]:
        return [row.as_dict() for row in self.safe_rows]

    def corrupt_dicts(self) -> list[dict[str, str]]:
        return [row.as_dict() for row in self.corrupt_rows]


def _safe_persisted_markdown_rel(value: object) -> str | None:
    """Normalize an untrusted persisted Markdown identity without traversal."""
    if not isinstance(value, str) or "\\" in value:
        return None
    path = PurePosixPath(value)
    if (
        not value
        or "\0" in value
        or (len(value) >= 2 and value[0].isalpha() and value[1] == ":")
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or not value.lower().endswith(".md")
    ):
        return None
    return path.as_posix()


def _no_follow_regular_markdown_path(vault_root: Path, rel: str) -> Path | None:
    """Return a regular leaf only after no-follow validation of every segment."""
    current = Path(vault_root)
    try:
        for index, part in enumerate(PurePosixPath(rel).parts):
            current /= part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or _is_reparse_point(info):
                return None
            if index < len(PurePosixPath(rel).parts) - 1:
                if not stat.S_ISDIR(info.st_mode):
                    return None
            elif not stat.S_ISREG(info.st_mode):
                return None
    except OSError:
        return None
    return current


def _is_reparse_point(info: os.stat_result) -> bool:
    """Platform-neutral seam for Windows reparse-point rejection."""
    return bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


@dataclass(frozen=True)
class _BoundSidecarRepair:
    fds: tuple[int, ...]
    path: Path
    checks: tuple[tuple[int, str, tuple[int, int], bool], ...]
    signatures: tuple[tuple[str, tuple[int, ...]], ...]
    windows_handles: tuple[int, ...] = ()
    windows_checks: tuple[tuple[Path, bool, tuple[int, int, int], int | None], ...] = ()

    def close(self) -> None:
        if self.windows_handles:
            from . import mutation_lock

            for handle in reversed(self.windows_handles):
                mutation_lock._windows_close_handle(handle)
        for fd in reversed(self.fds):
            os.close(fd)

    def entry_matches(self) -> bool:
        if self.windows_checks:
            from . import mutation_lock

            handles: list[int] = []
            try:
                for path, is_directory, windows_identity, parent_index in self.windows_checks:
                    handle = mutation_lock._windows_open_path(
                        path,
                        directory=is_directory,
                        access=0x80000000,  # GENERIC_READ
                        share=0x3,  # FILE_SHARE_READ | FILE_SHARE_WRITE
                    )
                    handles.append(handle)
                    if mutation_lock._windows_handle_identity(handle) != windows_identity:
                        return False
                    if parent_index is not None:
                        parent = mutation_lock._SecureDirectory(  # noqa: SLF001 - retained primitive
                            self.windows_checks[parent_index][0],
                            windows_handles=[handles[parent_index]],
                        )
                        if not mutation_lock._windows_child_is_in_directory(parent, handle):
                            return False
                return True
            except OSError:
                return False
            finally:
                for handle in reversed(handles):
                    mutation_lock._windows_close_handle(handle)
        for parent_fd, name, identity, is_directory in self.checks:
            try:
                info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except OSError:
                return False
            if (
                not (stat.S_ISDIR(info.st_mode) if is_directory else stat.S_ISREG(info.st_mode))
                or _is_reparse_point(info)
                or (info.st_dev, info.st_ino) != identity
            ):
                return False
        return True


_DARWIN_F_GETPATH = 50  # <sys/fcntl.h>
_DARWIN_MAXPATHLEN = 1024


@lru_cache(maxsize=1)
def _proc_fd_directory() -> Path | None:
    """The procfs descriptor directory, where the running kernel provides one."""
    candidate = Path("/proc/self/fd")
    try:
        return candidate if candidate.is_dir() else None
    except OSError:  # pragma: no cover - a hostile or absent /proc
        return None


def _names_pinned_inode(candidate: Path, pinned: os.stat_result) -> bool:
    """True when *candidate* still names the same inode the descriptor holds."""
    try:
        named = os.stat(candidate, follow_symlinks=False)
    except OSError:
        return False
    return (named.st_dev, named.st_ino) == (pinned.st_dev, pinned.st_ino)


def _pinned_descriptor_path(descriptor: int, *, writable: bool = False) -> Path | None:
    """Name the inode a descriptor holds, not the name it was opened under.

    Linux answers this with the magic symlink `/proc/self/fd/N`, and this
    module used to assume every POSIX host had one. macOS has no procfs, so
    the binding handed sqlite a path that could not exist: every bound census
    read reported `sidecar_unreadable` and every exact-row repair quietly did
    nothing, which made audit and reconcile repair inert on macOS rather than
    unavailable. Darwin's `F_GETPATH` answers the same question -- it reports
    where the pinned inode lives *now* -- so a sidecar replaced behind the
    descriptor is still reached through the descriptor and not through the
    name it no longer owns. Where neither exists the caller degrades to the
    declared `unsupported` state instead of inventing a path.
    """
    proc_fd = _proc_fd_directory()
    if proc_fd is not None:
        return proc_fd / str(descriptor)
    if sys.platform != "darwin":
        return None
    if writable:
        # A WRITE may not go through `F_GETPATH`. What it returns is an
        # ordinary name, and every caller hands it to sqlite, which resolves
        # it again at open time -- so a rename between this check and that
        # open redirects the write to whatever holds the name then. That is
        # the exact swap the pinning exists to defeat, and `entry_matches()`
        # notices it only afterwards, where it gates the *report* rather than
        # the write. The procfs branch above has no such gap: sqlite resolves
        # the magic symlink to wherever the pinned inode lives at open time,
        # so the write always lands on the inode.
        #
        # A hard link would name the inode and close the gap, but these
        # sidecars are WAL databases (`sidecar_store.apply_sidecar_pragmas`),
        # and a second name for one database gets its own `-wal`/`-shm` pair.
        # Two names, two write-ahead logs, one inode is a corruption hazard
        # strictly worse than the repair being unavailable.
        #
        # So Darwin binds for reading and declines to write, reaching the
        # `unsupported` state that exists to say exactly this. Reads stay
        # bound because a redirected read can only mis-classify -- it opens
        # `mode=ro`, and what it would leak back is the attacker's own file.
        return None
    pinned = os.fstat(descriptor)
    # `volfs` (`/.vol/<dev>/<ino>`) addresses the inode directly and looks like
    # the ideal answer, but it cannot serve one: every caller hands this path to
    # sqlite, which in WAL mode must create `-wal` and `-shm` siblings inside
    # `/.vol/<dev>` -- a synthetic read-only directory. Returning it turned an
    # inert repair into one that raised `EROFS`, so `F_GETPATH` is the only
    # usable branch here.
    import fcntl

    try:
        raw = fcntl.fcntl(descriptor, _DARWIN_F_GETPATH, bytes(_DARWIN_MAXPATHLEN))
    except (OSError, ValueError):
        return None
    resolved = raw.split(b"\x00", 1)[0]
    if not resolved:
        return None
    candidate = Path(os.fsdecode(resolved))
    # `F_GETPATH` answers from the vnode name cache, and a rename can leave
    # that stale: after a sidecar is replaced behind this descriptor it may
    # still report the old name, which now belongs to a *different* file. That
    # swap is the exact attack pinning the inode exists to defeat, so the path
    # is usable only while it still names the pinned inode. Where it does not,
    # the caller degrades to the declared `unsupported` state -- macOS can hold
    # the inode open but cannot re-open it by identity, so refusing is the only
    # honest answer. The procfs branch above needs no such check: its magic
    # symlink names the descriptor itself.
    return candidate if _names_pinned_inode(candidate, pinned) else None


def _sidecar_platform() -> Literal["posix", "windows", "unsupported"]:
    """Select the local no-follow binding primitive without mutating process state."""
    if os.name == "posix" and all(
        hasattr(os, flag) for flag in ("O_NOFOLLOW", "O_CLOEXEC", "O_DIRECTORY")
    ):
        return "posix"
    if os.name == "nt":
        return "windows"
    return "unsupported"


def _windows_handle_signature(handle: int) -> tuple[int, int, int, int, int]:
    """Return a stable identity and revision marker from one retained Windows handle."""
    import ctypes
    from ctypes import wintypes

    from . import mutation_lock

    class _FileInfo(ctypes.Structure):
        _fields_ = [
            ("attributes", wintypes.DWORD),
            ("creation_time", wintypes.FILETIME),
            ("access_time", wintypes.FILETIME),
            ("write_time", wintypes.FILETIME),
            ("volume_serial", wintypes.DWORD),
            ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD),
            ("links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        ]

    info = _FileInfo()
    kernel32 = mutation_lock._windows_library(ctypes, "kernel32")  # noqa: SLF001
    get_info = kernel32.GetFileInformationByHandle
    get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(_FileInfo)]
    get_info.restype = wintypes.BOOL
    if not get_info(handle, ctypes.byref(info)):
        raise OSError(
            mutation_lock._windows_last_error(ctypes),  # noqa: SLF001
            "cannot read retained Windows sidecar revision",
        )
    return (
        int(info.volume_serial),
        int(info.file_index_high),
        int(info.file_index_low),
        (int(info.size_high) << 32) | int(info.size_low),
        (int(info.write_time.dwHighDateTime) << 32) | int(info.write_time.dwLowDateTime),
    )


def _bind_posix_sidecar(path: Path, *, writable: bool) -> tuple[str, _BoundSidecarRepair | None]:
    """Pin a standard sidecar through no-follow vault-root and KB dirfds."""
    vault_root = path.parent.parent
    flags = os.O_CLOEXEC | os.O_NOFOLLOW
    fds: list[int] = []
    checks: list[tuple[int, str, tuple[int, int], bool]] = []
    signatures: list[tuple[str, tuple[int, ...]]] = []
    bound: _BoundSidecarRepair | None = None
    try:
        root_fd = os.open(vault_root, os.O_RDONLY | os.O_DIRECTORY | flags)
        fds.append(root_fd)
        root_info = os.fstat(root_fd)
        if not stat.S_ISDIR(root_info.st_mode) or _is_reparse_point(root_info):
            return "invalid", None
        kb_name = path.parent.name
        kb_fd = os.open(kb_name, os.O_RDONLY | os.O_DIRECTORY | flags, dir_fd=root_fd)
        fds.append(kb_fd)
        kb_info = os.fstat(kb_fd)
        if not stat.S_ISDIR(kb_info.st_mode) or _is_reparse_point(kb_info):
            return "invalid", None
        checks.append((root_fd, kb_name, (kb_info.st_dev, kb_info.st_ino), True))
        leaf_flags = (os.O_RDWR if writable else os.O_RDONLY) | flags
        leaf_fd = os.open(path.name, leaf_flags, dir_fd=kb_fd)
        fds.append(leaf_fd)
        leaf_info = os.fstat(leaf_fd)
        if not stat.S_ISREG(leaf_info.st_mode) or _is_reparse_point(leaf_info):
            return "invalid", None
        checks.append((kb_fd, path.name, (leaf_info.st_dev, leaf_info.st_ino), False))
        signatures.append(
            (
                path.name,
                (leaf_info.st_dev, leaf_info.st_ino, leaf_info.st_size, leaf_info.st_mtime_ns),
            )
        )
        for suffix in ("-wal", "-shm", "-journal"):
            name = path.name + suffix
            try:
                companion_fd = os.open(name, os.O_RDONLY | flags, dir_fd=kb_fd)
            except FileNotFoundError:
                continue
            fds.append(companion_fd)
            info = os.fstat(companion_fd)
            if not stat.S_ISREG(info.st_mode) or _is_reparse_point(info):
                return "invalid", None
            checks.append((kb_fd, name, (info.st_dev, info.st_ino), False))
            signatures.append((name, (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)))
        pinned = _pinned_descriptor_path(leaf_fd, writable=writable)
        if pinned is None:
            return "unsupported", None
        bound = _BoundSidecarRepair(tuple(fds), pinned, tuple(checks), tuple(signatures))
        return "regular", bound
    except FileNotFoundError:
        return "absent", None
    except OSError:
        return "unreadable", None
    finally:
        if bound is None:
            for fd in reversed(fds):
                os.close(fd)


def _bind_windows_sidecar(path: Path, *, writable: bool) -> tuple[str, _BoundSidecarRepair | None]:
    """Retain Windows no-delete handles for one standard SQLite sidecar."""
    from . import mutation_lock

    vault_root = path.parent.parent
    handles: list[int] = []
    checks: list[tuple[Path, bool, tuple[int, int, int], int | None]] = []
    signatures: list[tuple[str, tuple[int, ...]]] = []
    bound: _BoundSidecarRepair | None = None

    def open_entry(candidate: Path, *, directory: bool, parent_index: int | None) -> int:
        handle = mutation_lock._windows_open_path(
            candidate,
            directory=directory,
            access=0x80000000,  # GENERIC_READ
            share=0x3,  # FILE_SHARE_READ | FILE_SHARE_WRITE; deliberately no delete share
        )
        handles.append(handle)
        if parent_index is not None:
            parent = mutation_lock._SecureDirectory(  # noqa: SLF001 - retained primitive
                checks[parent_index][0], windows_handles=[handles[parent_index]]
            )
            if not mutation_lock._windows_child_is_in_directory(parent, handle):
                raise OSError("Windows sidecar entry escaped its retained directory")
        identity = mutation_lock._windows_handle_identity(handle)
        checks.append((candidate, directory, identity, parent_index))
        return handle

    try:
        open_entry(vault_root, directory=True, parent_index=None)
        open_entry(path.parent, directory=True, parent_index=0)
        leaf_handle = open_entry(path, directory=False, parent_index=1)
        signatures.append((path.name, _windows_handle_signature(leaf_handle)))
        for suffix in ("-wal", "-shm", "-journal"):
            companion = path.with_name(path.name + suffix)
            try:
                companion_handle = open_entry(companion, directory=False, parent_index=1)
            except FileNotFoundError:
                continue
            signatures.append((companion.name, _windows_handle_signature(companion_handle)))
        bound = _BoundSidecarRepair((), path, (), tuple(signatures), tuple(handles), tuple(checks))
        return "regular", bound
    except FileNotFoundError:
        return "absent", None
    except OSError:
        return "unreadable", None
    finally:
        if bound is None:
            for handle in reversed(handles):
                mutation_lock._windows_close_handle(handle)


def _bind_sidecar(path: Path, *, writable: bool) -> tuple[str, _BoundSidecarRepair | None]:
    """Bind one standard sidecar through the local no-follow platform primitive."""
    platform = _sidecar_platform()
    if platform == "posix":
        return _bind_posix_sidecar(path, writable=writable)
    if platform == "windows":
        return _bind_windows_sidecar(path, writable=writable)
    return "unsupported", None


def _bound_sidecar_repair(path: Path) -> _BoundSidecarRepair | None:
    """Bind an existing sidecar identity for an exact-row repair."""
    state, binding = _bind_sidecar(path, writable=True)
    return binding if state == "regular" else None


def _classify_semantic_isolation_row(
    vault_root: Path,
    component: str,
    raw: object,
    *,
    markdown_identity: object | None = None,
    edge_column: str | None = None,
) -> _SemanticIsolationRow | None:
    """Return persisted Markdown identities that are live but no longer admitted.

    Sidecars are untrusted input here.  Validate the persisted spelling before
    joining it to the vault root so an old/corrupt row can never make audit or
    reconcile traverse outside the vault.
    """
    from . import recall_policy

    if not isinstance(raw, str):
        return _SemanticIsolationRow(component, repr(raw), None, edge_column)
    value = raw if markdown_identity is None else markdown_identity
    rel = _safe_persisted_markdown_rel(value)
    if rel is None:
        return _SemanticIsolationRow(component, raw, None, edge_column)
    candidate = _no_follow_regular_markdown_path(vault_root, rel)
    if candidate is None:
        # A valid but missing identity remains ordinary missing-path cleanup;
        # a symlink/reparse leaf is never followed and its sidecar row is
        # handled only as an exact corrupt value.
        try:
            if not os.path.lexists(Path(vault_root) / rel):
                return _SemanticIsolationRow(component, raw, rel, edge_column, missing=True)
        except OSError:
            return None
        return _SemanticIsolationRow(component, raw, None, edge_column)
    if recall_policy.is_recall_candidate(vault_root, candidate):
        return None
    return _SemanticIsolationRow(component, raw, rel, edge_column)


def _sidecar_rows(
    vault_root: Path, path: Path, query: str, *, after: str, limit: int
) -> tuple[list[tuple[object, ...]], int, str | None, str | None]:
    """Read at most one bounded census page without creating a sidecar."""
    state, binding = _bind_sidecar(path, writable=False)
    if state == "absent":
        return [], 0, None, None
    if state == "unsupported":
        return [], 0, None, "sidecar_unsupported"
    if state != "regular" or binding is None:
        return [], 0, None, "sidecar_unreadable"
    import sqlite3

    try:
        conn = sqlite3.connect(f"{binding.path.as_uri()}?mode=ro", uri=True)
    except (OSError, sqlite3.Error):
        binding.close()
        return [], 0, None, "sidecar_unreadable"
    try:
        cursor = conn.execute(query, (after,))
        rows: list[tuple[object, ...]] = []
        while len(rows) < limit:
            batch = cursor.fetchmany(min(_SEMANTIC_ISOLATION_CENSUS_FETCH, limit - len(rows)))
            if not batch:
                break
            rows.extend(batch)
            if len(rows) >= limit:
                break
        return (
            rows,
            int(cursor.fetchone() is not None),
            str(rows[-1][0]) if rows else None,
            None,
        )
    except sqlite3.Error:
        return [], 0, None, "sidecar_schema_unreadable"
    finally:
        conn.close()
        binding.close()


def _sidecar_signature(vault_root: Path, path: Path) -> str | None:
    """Stable-enough revision marker for a bounded SQLite census cursor."""
    state, binding = _bind_sidecar(path, writable=False)
    if state == "absent":
        return hashlib.sha256(b"absent|absent|absent").hexdigest()
    if state != "regular" or binding is None:
        return None
    try:
        identities = dict(binding.signatures)
        pieces = [
            ":".join(map(str, identities.get(name, ("absent",))))
            for name in (path.name, path.name + "-wal", path.name + "-shm")
        ]
        return hashlib.sha256("|".join(pieces).encode("ascii")).hexdigest()
    finally:
        binding.close()


def _deferred_sidecar_signature(vault_root: Path, path: Path) -> str | None:
    """Read deferred generation through the same pinned sidecar binding."""
    from . import deferred_index

    state, binding = _bind_sidecar(path, writable=False)
    if state == "absent":
        return "semantic:0"
    if state != "regular" or binding is None:
        return None
    try:
        return deferred_index.semantic_isolation_signature(vault_root, connection_path=binding.path)
    finally:
        binding.close()


def semantic_recall_isolation_census(
    vault_root: Path,
    *,
    limit: int = _SEMANTIC_ISOLATION_CENSUS_LIMIT,
    after: dict[str, dict[str, str]] | None = None,
) -> SemanticRecallIsolationCensus:
    """Inventory live suppressed Markdown that survives in semantic sidecars.

    This deliberately inspects persisted state rather than the ordinary recall
    APIs: those APIs correctly fail closed, while reconcile needs a complete
    maintenance census even with embeddings, graph, claims, or CLIP disabled.
    """
    from . import claims, deferred_index, epistemic_graph, index_paths, lexstore

    cursors = after or {}
    sources = (
        (
            "lexical",
            lexstore.lexical_path(vault_root),
            "SELECT DISTINCT path FROM pages WHERE path > ? ORDER BY path",
        ),
        (
            "lexical_units",
            lexstore.lexical_path(vault_root),
            "SELECT DISTINCT parent_path FROM semantic_units WHERE parent_path > ? ORDER BY parent_path",
        ),
        (
            "vector",
            index_paths.sidecar_path(vault_root),
            "SELECT DISTINCT file_path FROM chunks WHERE file_path > ? ORDER BY file_path",
        ),
        (
            "vector_units",
            index_paths.sidecar_path(vault_root),
            "SELECT DISTINCT parent_path FROM semantic_unit_vectors WHERE parent_path > ? ORDER BY parent_path",
        ),
        (
            "graph",
            epistemic_graph.sidecar_path(vault_root),
            "SELECT DISTINCT path FROM graph_nodes WHERE path > ? ORDER BY path",
        ),
        (
            "claims",
            claims.sidecar_path(vault_root),
            "SELECT DISTINCT file_path FROM claims WHERE file_path > ? ORDER BY file_path",
        ),
        (
            "deferred_semantic",
            deferred_index.store_path(vault_root),
            "SELECT DISTINCT rel_path FROM semantic_upserts WHERE rel_path > ? ORDER BY rel_path",
        ),
    )
    rows: list[_SemanticIsolationRow] = []
    truncation: dict[str, int] = {}
    continuation: dict[str, dict[str, str]] = {}
    incomplete: dict[str, str] = {}
    for component, sidecar, query in sources:
        signature = (
            _deferred_sidecar_signature(vault_root, sidecar)
            if component == "deferred_semantic"
            else _sidecar_signature(vault_root, sidecar)
        )
        stored = cursors.get(component, {})
        cursor = stored.get("cursor", "") if stored.get("signature") == signature else ""
        values, truncated, last, failure = _sidecar_rows(
            vault_root, sidecar, query, after=cursor, limit=limit
        )
        truncation[component] = truncated
        if failure is not None:
            incomplete[component] = failure
        elif truncated and last is not None and signature is not None:
            continuation[component] = {"cursor": last, "signature": signature}
        for (value,) in values:
            row = _classify_semantic_isolation_row(vault_root, component, value)
            if row is not None:
                rows.append(row)

    # CLIP keys omit the terminal Markdown sidecar suffix.  Re-add it before
    # policy admission; a non-Markdown binary can never be a semantic finding.
    clip_sidecar = index_paths.clip_sidecar_path(vault_root)
    signature = _sidecar_signature(vault_root, clip_sidecar)
    stored = cursors.get("clip", {})
    values, truncated, last, failure = _sidecar_rows(
        vault_root,
        clip_sidecar,
        "SELECT DISTINCT file_path FROM images WHERE file_path > ? ORDER BY file_path",
        after=stored.get("cursor", "") if stored.get("signature") == signature else "",
        limit=limit,
    )
    truncation["clip"] = truncated
    if failure is not None:
        incomplete["clip"] = failure
    elif truncated and last is not None and signature is not None:
        continuation["clip"] = {"cursor": last, "signature": signature}
    for (key,) in values:
        if isinstance(key, str):
            row = _classify_semantic_isolation_row(
                vault_root, "clip", key, markdown_identity=f"{key}.md"
            )
            if row is not None:
                rows.append(row)

    graph_sidecar = epistemic_graph.sidecar_path(vault_root)
    signature = _sidecar_signature(vault_root, graph_sidecar)
    stored = cursors.get("graph_edges", {})
    values, truncated, last, failure = _sidecar_rows(
        vault_root,
        graph_sidecar,
        "SELECT edge_key, source_path, src_key, dst_key FROM graph_edges WHERE edge_key > ? ORDER BY edge_key",
        after=stored.get("cursor", "") if stored.get("signature") == signature else "",
        limit=limit,
    )
    truncation["graph_edges"] = truncated
    if failure is not None:
        incomplete["graph_edges"] = failure
    elif truncated and last is not None and signature is not None:
        continuation["graph_edges"] = {"cursor": last, "signature": signature}
    for _edge_key, source_path, src_key, dst_key in values:
        for raw, identity, edge_column in (
            (source_path, source_path, "source_path"),
            (
                src_key,
                src_key.removeprefix("file:")
                if isinstance(src_key, str) and src_key.startswith("file:")
                else None,
                "src_key",
            ),
            (
                dst_key,
                dst_key.removeprefix("file:")
                if isinstance(dst_key, str) and dst_key.startswith("file:")
                else None,
                "dst_key",
            ),
        ):
            if identity is None:
                continue
            row = _classify_semantic_isolation_row(
                vault_root, "graph_edges", raw, markdown_identity=identity, edge_column=edge_column
            )
            if row is not None:
                rows.append(row)
    unique = {(row.component, row.raw, row.path, row.edge_column, row.missing): row for row in rows}
    return SemanticRecallIsolationCensus(
        tuple(sorted(unique.values(), key=lambda row: (row.path or "", row.component, row.raw))),
        {component: count for component, count in truncation.items() if count},
        continuation,
        incomplete,
    )


def semantic_recall_isolation_drift(vault_root: Path) -> list[dict[str, str]]:
    """Compatibility projection of the bounded safe census."""
    return semantic_recall_isolation_census(vault_root).safe_dicts()


def purge_corrupt_semantic_recall_isolation_rows(
    vault_root: Path, rows: tuple[_SemanticIsolationRow, ...]
) -> dict[str, int]:
    """Purge quarantined values through each sidecar's exact-row model seam."""
    from . import claims, deferred_index, embeddings, epistemic_graph, index_paths, lexstore

    grouped: dict[str, set[str]] = {}
    graph_edges: dict[str, set[str]] = {}
    for row in rows:
        if row.path is not None:
            continue
        if row.component == "graph_edges":
            if row.edge_column in {"source_path", "src_key", "dst_key"}:
                graph_edges.setdefault(row.edge_column, set()).add(row.raw)
            continue
        grouped.setdefault(row.component, set()).add(row.raw)

    deleted: dict[str, int] = {}

    def purge(component: str, sidecar: Path, callback) -> None:
        binding = _bound_sidecar_repair(sidecar)
        if binding is None:
            return
        try:
            changed = callback(binding.path)
        except Exception:  # noqa: BLE001 - independent derived-sidecar cleanup
            log.warning("%s exact-row purge failed", component, exc_info=True)
            return
        finally:
            entry_matches = binding.entry_matches()
            binding.close()
        if changed and entry_matches:
            deleted[component] = int(changed)

    purge(
        "lexical",
        lexstore.lexical_path(vault_root),
        lambda connection_path: lexstore.purge_exact_persisted_rows(
            vault_root,
            sorted(grouped.get("lexical", set()) | grouped.get("lexical_units", set())),
            connection_path=connection_path,
        ),
    )
    purge(
        "vector",
        index_paths.sidecar_path(vault_root),
        lambda connection_path: embeddings.get_embedding_index(
            vault_root
        ).purge_exact_persisted_rows(
            sorted(grouped.get("vector", set()) | grouped.get("vector_units", set())),
            connection_path=connection_path,
        ),
    )
    purge(
        "claims",
        claims.sidecar_path(vault_root),
        lambda connection_path: claims.get_claim_index(vault_root).purge_exact_persisted_rows(
            sorted(grouped.get("claims", set())), connection_path=connection_path
        ),
    )
    purge(
        "deferred_semantic",
        deferred_index.store_path(vault_root),
        lambda connection_path: deferred_index.purge_exact_persisted_semantic_rows(
            vault_root,
            sorted(grouped.get("deferred_semantic", set())),
            connection_path=connection_path,
        ),
    )
    purge(
        "clip",
        index_paths.clip_sidecar_path(vault_root),
        lambda connection_path: embeddings.get_clip_index(vault_root).purge_exact_persisted_rows(
            sorted(grouped.get("clip", set())), connection_path=connection_path
        ),
    )
    graph_values = {column: sorted(values) for column, values in graph_edges.items()}
    purge(
        "graph",
        epistemic_graph.sidecar_path(vault_root),
        lambda connection_path: epistemic_graph.EpistemicGraphIndex(
            vault_root
        ).purge_exact_persisted_rows(
            sorted(grouped.get("graph", set())),
            graph_values,
            connection_path=connection_path,
        ),
    )
    return deleted


def _check_semantic_recall_isolation(vault_root: Path) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    census = semantic_recall_isolation_census(vault_root)
    for item in census.safe_dicts():
        component = item["component"]
        findings.append(
            AuditFinding(
                category="semantic_recall_isolation",
                severity="warn",
                path=item["path"],
                detail=(f"live suppressed Markdown remains in the {component} semantic sidecar"),
                proposed_fix="Run `reconcile` to purge the stale semantic row.",
                meta={"component": component},
            )
        )
    for item in census.corrupt_dicts():
        findings.append(
            AuditFinding(
                category="semantic_recall_isolation",
                severity="warn",
                path=item["path"],
                detail="corrupt persisted semantic identity is quarantined from path routing",
                proposed_fix="Run `reconcile` to purge the exact corrupt sidecar row.",
                meta={"component": item["component"], "state": "corrupt"},
            )
        )
    for row in census.missing_rows:
        findings.append(
            AuditFinding(
                category="semantic_recall_isolation",
                severity="warn",
                path=str(row.path),
                detail="missing Markdown remains in a semantic sidecar",
                proposed_fix="Run `reconcile` to purge stale derived rows.",
                meta={"component": row.component, "state": "missing"},
            )
        )
    for component, remaining in census.truncation.items():
        findings.append(
            AuditFinding(
                category="semantic_recall_isolation",
                severity="info",
                path=kb_prefix(),
                detail=f"{component} census capped; at least {remaining} persisted row remains",
                proposed_fix="Run `reconcile` again to continue bounded semantic cleanup.",
                meta={
                    "component": component,
                    "state": "truncated",
                    "remaining_at_least": remaining,
                },
            )
        )
    for component, code in census.incomplete.items():
        findings.append(
            AuditFinding(
                category="semantic_recall_isolation",
                severity="warn",
                path=kb_prefix(),
                detail="semantic sidecar isolation could not be proven",
                proposed_fix="Repair or rebuild the affected derived sidecar, then rerun audit.",
                meta={"component": component, "state": "incomplete", "code": code},
            )
        )
    return findings


def _check_governance_receipts(vault_root: Path) -> list[AuditFinding]:
    """Project receipt-chain evidence problems without touching its sidecar."""
    from .governance import receipts

    report = receipts.verify_chain(vault_root)
    return [
        AuditFinding(
            category="governance_receipts",
            severity="error",
            path=item["path"],
            detail=f"{item['code']}: {item['detail']}",
            proposed_fix="Run `maintain_memory` with mode `reconcile` after reviewing evidence.",
        )
        for item in report["issues"]
    ]


def _check_semantic_contract_drift(
    vault_root: Path,
    *,
    categories: set[str] | frozenset[str] | None = None,
    detail: Literal["actionable", "full"] = "actionable",
) -> tuple[list[AuditFinding], dict]:
    """Project the shared bounded posthoc result into audit findings."""
    from . import semantic_writes

    posthoc = semantic_writes.evaluate_posthoc_batch(
        vault_root,
        operation="audit",
    )
    batch = posthoc.as_dict() if detail == "actionable" else posthoc.as_dict(detail=detail)
    out: list[AuditFinding] = []
    selected = set(categories or {"semantic_contract_drift"})
    for item in batch["semantic_contract_findings"]:
        projected_categories = _selected_semantic_categories(item, selected)
        if not projected_categories:
            continue
        key = {
            "code": item["code"],
            "governed_element_identity": item["governed_element_identity"],
            "resolved_rule": item["resolved_rule"],
        }
        signal_version = hashlib.sha256(
            json.dumps(
                key,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()[:16]
        actions = item["actions"]
        for category in projected_categories:
            out.append(
                AuditFinding(
                    category=category,
                    severity="error" if item["severity"] == "error" else "warn",
                    path=item["path"],
                    detail=f"Semantic contract finding {item['code']} requires review.",
                    proposed_fix=(
                        ", ".join(actions)
                        if actions
                        else "Review the current semantic contract finding."
                    ),
                    meta={
                        "finding_key": key,
                        "signal_version": signal_version,
                        "code": item["code"],
                        "governed_element_identity": item["governed_element_identity"],
                        "resolved_rule": item["resolved_rule"],
                        "relation_disposition": item["relation_disposition"],
                        "actions": actions,
                        "activation": item["activation"],
                        "grandfathered": item["grandfathered"],
                    },
                )
            )
    metadata = {
        "omitted_counts": batch["omitted_counts"],
        "truncation": batch["truncation"],
    }
    if "semantic_contract_summary" in batch:
        metadata["semantic_contract_summary"] = batch["semantic_contract_summary"]
    category_summary = _complete_semantic_category_summary(posthoc, selected)
    if category_summary is not None:
        metadata["semantic_category_summary"] = category_summary
    return out, metadata


def semantic_finding_group(item: dict) -> str | None:
    """Return the stable typed audit grouping for one shared posthoc finding."""
    code = str(item.get("code") or "")
    resolved_rule = tuple(str(value) for value in (item.get("resolved_rule") or ()))
    namespace = resolved_rule[0] if resolved_rule else ""
    rule = resolved_rule[2] if len(resolved_rule) >= 3 else ""
    if code in {
        "RELATION_DISPOSITION_MISSING",
        "RELATION_DISPOSITION_STALE",
        "RELATION_TYPED_EDGE_ABSENT",
    }:
        return "semantic_relation_disposition"
    if rule == "syntax":
        return "semantic_malformed_unit"
    if namespace in {"categories", "kinds"} and (rule == "registry" or "REGISTRY" in code.upper()):
        return "semantic_category_governance"
    if code.startswith("CONTRACT_") and item.get("severity") == "error":
        return "semantic_strict_schema_drift"
    return None


def _selected_semantic_categories(
    item: dict,
    selected: set[str],
) -> tuple[str, ...]:
    categories: list[str] = []
    if "semantic_contract_drift" in selected:
        categories.append("semantic_contract_drift")
    typed_category = semantic_finding_group(item)
    if typed_category is not None and typed_category in selected:
        categories.append(typed_category)
    return tuple(categories)


def _complete_semantic_category_summary(
    posthoc: Any,
    selected: set[str],
) -> dict[str, int] | None:
    evaluations = getattr(posthoc, "evaluations", None)
    if evaluations is None:
        return None
    summary: dict[str, int] = {}
    for evaluation in evaluations:
        for finding in evaluation.contract_result.findings:
            item = {
                "code": finding.code,
                "severity": finding.severity,
                "resolved_rule": list(finding.resolved_rule),
            }
            for category in _selected_semantic_categories(item, selected):
                summary[category] = summary.get(category, 0) + 1
    return summary


# ---------------- vault walk ----------------


def _parse_all(kb: Path, vault_root: Path) -> list[find_module.ParsedPage]:
    """Walk the KB once, parse every .md, return ParsedPage objects."""
    pages: list[find_module.ParsedPage] = []
    for path in find_module._walk_md(kb):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        page = find_module._parse_page(path, mtime, vault_root)
        if page is not None:
            pages.append(page)
    return pages


def _page_signal_version(page: find_module.ParsedPage) -> str:
    """Stable content version for fingerprint-bound review decisions."""
    frontmatter = yaml.safe_dump(page.frontmatter, sort_keys=True, allow_unicode=True)
    return content_hash(frontmatter + "\n" + page.body)[:16]


def _check_unregistered_entity_types(
    vault_root: Path,
    pages: list[find_module.ParsedPage],
) -> list[AuditFinding]:
    """Surface authored entity kinds/folders absent from the active registry."""
    registry = entity_types_module.load_entity_types(vault_root)
    prefix = f"{kb_prefix()}Entities/"
    entities: list[tuple[find_module.ParsedPage, str]] = []
    for page in pages:
        if not page.rel_path.startswith(prefix):
            continue
        remainder = page.rel_path[len(prefix):]
        folder, separator, _tail = remainder.partition("/")
        if separator and folder:
            entities.append((page, folder))

    def definition_payload(
        definition: entity_types_module.EntityTypeDefinition,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "folder": definition.folder,
            "label": definition.label,
            "aliases": list(definition.aliases),
            "cue_nouns": list(definition.cue_nouns),
            "capture_guidance": definition.capture_guidance,
        }
        if definition.optional_frontmatter:
            payload["optional_frontmatter"] = list(definition.optional_frontmatter)
        if definition.parent is not None:
            payload["parent"] = definition.parent
        if definition.status != "active":
            payload["status"] = definition.status
        if definition.replaced_by is not None:
            payload["replaced_by"] = definition.replaced_by
        return payload

    current_extensions = {
        type_id: definition_payload(definition)
        for type_id, definition in registry.extensions.items()
    }
    occupied_folders = {
        definition.folder.casefold()
        for definition in (*registry.core.values(), *registry.extensions.values())
    }
    proposal_cache: dict[
        tuple[str, str],
        tuple[dict[str, Any] | None, dict[str, Any] | None, str | None],
    ] = {}

    def proposed_entry(
        type_id: str,
        source_folder: str,
        label: str,
        page_count: int,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
        cache_key = (type_id, source_folder.casefold())
        cached = proposal_cache.get(cache_key)
        if cached is not None:
            cached_entry, cached_proposal, reason = cached
            cached_result = dict(cached_entry) if cached_entry is not None else None
            if cached_result is not None:
                cached_result["page_count"] = page_count
            return cached_result, cached_proposal, reason

        owner = registry.by_folder.get(source_folder.casefold())
        parent = owner.id if owner is not None and owner.id in registry.core else None

        base = derived_folder(type_id)
        attempt = 0
        while attempt <= 1000:
            if attempt == 0:
                folder = source_folder
            elif attempt == 1:
                folder = base
            else:
                folder = f"{base}{attempt}"
            attempt += 1
            if folder.casefold() in occupied_folders:
                continue
            candidate_entry: dict[str, Any] = {
                "id": type_id,
                "folder": folder,
                "label": label,
                "aliases": [],
            }
            if parent is not None:
                candidate_entry["parent"] = parent
            definition = {key: value for key, value in candidate_entry.items() if key != "id"}
            definition["capture_guidance"] = (
                f"A stable {label.lower()} identity with reusable context."
            )
            proposal = {
                "schema_version": entity_types_module.EXTENSION_SCHEMA_VERSION,
                "entity_types": {**current_extensions, type_id: definition},
            }
            validation = entity_types_module.validate_proposal(proposal)
            if not validation:
                proposal_cache[cache_key] = (candidate_entry, proposal, None)
                result = dict(candidate_entry)
                result["page_count"] = page_count
                return result, proposal, None
            if all(finding["path"].endswith(f".{type_id}.folder") for finding in validation):
                continue
            reason = "; ".join(f"{finding['path']}: {finding['detail']}" for finding in validation)
            proposal_cache[cache_key] = (None, None, reason)
            return None, None, reason
        reason = "No collision-free entity folder could be derived."
        proposal_cache[cache_key] = (None, None, reason)
        return None, None, reason

    def derived_folder(type_id: str) -> str:
        label = type_id.replace("-", " ").title()
        if label.casefold().endswith("s"):
            return label
        return f"{label}s"

    folder_pages: dict[str, list[find_module.ParsedPage]] = {}
    folder_spelling: dict[str, str] = {}
    authored_type_counts: dict[str, int] = {}
    authored_type_folder_counts: dict[str, dict[str, int]] = {}
    authored_type_folder_spelling: dict[tuple[str, str], str] = {}
    unknown_entities: list[tuple[find_module.ParsedPage, str, str]] = []
    for page, folder in sorted(entities, key=lambda item: item[0].rel_path):
        key = folder.casefold()
        folder_pages.setdefault(key, []).append(page)
        folder_spelling.setdefault(key, folder)
        raw_type = page.frontmatter.get("entity_type")
        if isinstance(raw_type, str):
            type_id = entity_types_module.normalize_entity_token(raw_type)
            authored_type_counts[type_id] = authored_type_counts.get(type_id, 0) + 1
            if registry.resolve(raw_type) is None:
                unknown_entities.append((page, folder, type_id))
                folder_counts = authored_type_folder_counts.setdefault(type_id, {})
                folder_counts[key] = folder_counts.get(key, 0) + 1
                authored_type_folder_spelling.setdefault((type_id, key), folder)

    preferred_folder_by_type = {
        type_id: authored_type_folder_spelling[
            (
                type_id,
                min(
                    folder_counts,
                    key=lambda folder_key: (
                        -folder_counts[folder_key],
                        authored_type_folder_spelling[(type_id, folder_key)].casefold(),
                    ),
                ),
            )
        ]
        for type_id, folder_counts in authored_type_folder_counts.items()
    }

    carrier_path_by_type_folder: dict[tuple[str, str], str] = {}
    for page, folder, type_id in unknown_entities:
        carrier_path_by_type_folder.setdefault((type_id, folder.casefold()), page.rel_path)

    def proposal_meta(
        *,
        entry: dict[str, Any] | None,
        proposal: dict[str, Any] | None,
        reason: str | None,
        signal_version: str,
        carrier_path: str,
        finding_path: str,
    ) -> tuple[dict[str, Any], str]:
        meta: dict[str, Any] = {
            "proposed_entry": entry,
            "signal_version": signal_version,
        }
        if reason is not None:
            meta["reason"] = reason
        if proposal is None:
            return (
                meta,
                "Resolve the reported registry conflict or move the affected page(s).",
            )
        if finding_path == carrier_path:
            meta["proposal"] = proposal
            meta["expected_hash"] = registry.extension_hash
            return (
                meta,
                "Save `meta.proposal` — the full registry with this entry merged — "
                'via `schema_memory(operation="save-entity-types", '
                "proposal=meta.proposal, why=..., "
                "expected_hash=meta.expected_hash)`. Do not save "
                "`proposed_entry` on its own; it is a description, not a registry.",
            )
        meta["proposal_carrier"] = carrier_path
        return (
            meta,
            f"Use `meta.proposal` and `meta.expected_hash` from the carrying finding "
            f"at {carrier_path!r}. Do not save `proposed_entry` on its own; it is a "
            "description, not a registry.",
        )

    findings: list[AuditFinding] = []
    for page, folder, type_id in unknown_entities:
        raw_type = str(page.frontmatter["entity_type"])
        label = type_id.replace("-", " ").title()
        entry, proposal, reason = proposed_entry(
            type_id,
            preferred_folder_by_type[type_id],
            label,
            authored_type_counts[type_id],
        )
        meta, proposed_fix = proposal_meta(
            entry=entry,
            proposal=proposal,
            reason=reason,
            signal_version=_page_signal_version(page),
            carrier_path=carrier_path_by_type_folder[(type_id, folder.casefold())],
            finding_path=page.rel_path,
        )
        findings.append(
            AuditFinding(
                category="entity_type_unregistered",
                severity="warn",
                path=page.rel_path,
                detail=(f"Entity type {raw_type!r} is not in the active entity registry."),
                proposed_fix=proposed_fix,
                meta=meta,
            )
        )

    for key in sorted(folder_pages):
        members = sorted(folder_pages[key], key=lambda page: page.rel_path)
        if len(members) < 3 or registry.by_folder.get(key) is not None:
            continue
        # Page-level unknown-type findings already expose this folder. The
        # threshold signal is for registered-looking pages stored under a new
        # folder, so do not duplicate the same proposal on one attention pass.
        if any(
            isinstance(page.frontmatter.get("entity_type"), str)
            and registry.resolve(str(page.frontmatter["entity_type"])) is None
            for page in members
        ):
            continue
        folder = folder_spelling[key]
        paths = [page.rel_path for page in members]
        signal = content_hash("\n".join(paths))[:16]
        type_id = entity_types_module.normalize_entity_token(folder)
        entry, proposal, reason = proposed_entry(
            type_id,
            folder,
            folder.title(),
            len(paths),
        )
        meta, proposed_fix = proposal_meta(
            entry=entry,
            proposal=proposal,
            reason=reason,
            signal_version=signal,
            carrier_path=paths[0],
            finding_path=paths[0],
        )
        findings.append(
            AuditFinding(
                category="entity_type_unregistered",
                severity="info",
                path=paths[0],
                paths=paths,
                detail=(f"{len(paths)} entity pages live under unregistered folder {folder!r}."),
                proposed_fix=proposed_fix,
                meta=meta,
            )
        )
    return sorted(findings, key=lambda finding: (finding.path, finding.detail))


# ---------------- checks: broken_wikilink / forward_reference ----------------


def _check_wikilinks(
    vault_root: Path,
    pages: list[find_module.ParsedPage],
    selected: set[str],
) -> list[AuditFinding]:
    findings: list[AuditFinding] = []

    # Wikilinks in compiled KB notes may legitimately target curated sibling
    # folders outside Knowledge Base/ (read-only material), plus `_Schema/`.
    # SKILL.md rule 1 explicitly calls these out as link targets. Build the
    # existence set from the full vault so those don't false-positive.
    full_paths: set[str] = set()          # vault-relative, no .md, e.g. "Reference/Strategy"
    kb_stripped_paths: set[str] = set()   # KB-relative, no .md
    names_to_paths: dict[str, list[str]] = {}  # bare filename (no ext) → vault-rel paths
    titles_to_paths: dict[str, list[str]] = {}  # lower(frontmatter title) → paths
    for md_path in _walk_vault_md(vault_root):
        try:
            rel = md_path.relative_to(vault_root).as_posix()
        except ValueError:
            continue
        no_ext = rel.removesuffix(".md")
        full_paths.add(no_ext)
        kb_stripped_paths.add(no_ext.removeprefix(kb_prefix()))
        names_to_paths.setdefault(md_path.stem, []).append(no_ext)
        # Title fallback: lets `[[North-Led Content Manual]]` resolve to a
        # date-prefixed source whose frontmatter `title:` matches.
        try:
            text, _guard = vault_module.read_guarded_text(vault_root, md_path)
        except (OSError, UnicodeDecodeError, vault_module.PathGuardError):
            continue
        fm, _, _ = parse_frontmatter(text)
        title = fm.get("title") if isinstance(fm, dict) else None
        if isinstance(title, str) and title.strip():
            titles_to_paths.setdefault(title.strip().lower(), []).append(no_ext)

    for page in pages:
        # Skip wikilinks inside fenced code blocks and inline code spans —
        # `[[:space:]]` and similar regex/bash snippets aren't real links.
        body_masked = _mask_code_spans(page.body)
        for match in WIKILINK_PATTERN.finditer(body_masked):
            target = match.group(1).strip()
            if target.endswith("/"):
                # Folder hub link, not a page link.
                continue
            # Strip `#anchor` for resolution — anchors are intra-page jumps,
            # not file paths.
            target_for_resolve = target.split("#", 1)[0].strip()
            if not target_for_resolve:
                continue
            normalized = target_for_resolve.removeprefix(kb_prefix()).lstrip("/")
            if normalized in kb_stripped_paths:
                continue
            if target_for_resolve.lstrip("/") in full_paths:
                continue
            # Bare-name lookup: Obsidian resolves [[name]] by filename anywhere
            # in the vault. Only attempt if no path separator.
            ambiguous_stem = False
            ambiguous_title = False
            if "/" not in target_for_resolve:
                stem_matches = names_to_paths.get(target_for_resolve)
                if stem_matches and len(stem_matches) == 1:
                    continue
                ambiguous_stem = bool(stem_matches)
                # Title fallback. Only resolves when unambiguous; ambiguous
                # title matches stay flagged so the user can disambiguate.
                title_matches = titles_to_paths.get(target_for_resolve.lower())
                if not ambiguous_stem and title_matches and len(title_matches) == 1:
                    continue
                ambiguous_title = not ambiguous_stem and bool(title_matches)
            # Attachment links: Obsidian resolves a wikilink carrying an
            # explicit (non-.md) extension to the file on disk of any type
            # (e.g. [[.../scan.pdf]], [[Reference/diagram.png]]). The resolution
            # set above is .md-only and skips _attachments/, so such links
            # false-positived even when the file was present. Probe the
            # filesystem directly. Extension-less links stay note (.md) links,
            # matching Obsidian — a bare [[Foo]] never resolves to Foo.eml.
            suffix = Path(target_for_resolve).suffix.lower()
            if suffix and suffix != ".md":
                rel = target_for_resolve.lstrip("/")
                if _ordinary_file_exists(vault_root, vault_root / rel) or _ordinary_file_exists(
                    vault_root, vault_root / kb_dirname() / normalized
                ):
                    continue

            non_markdown_collision = not suffix and _has_non_markdown_collision(
                    vault_root, target_for_resolve.lstrip("/"), normalized
                )
            broken = bool(
                (suffix and suffix != ".md")
                or ambiguous_stem
                or ambiguous_title
                or non_markdown_collision
            )
            category = "broken_wikilink" if broken else "forward_reference"
            if category not in selected:
                continue

            immutable = in_append_only_tree(str(page.rel_path)) is not None
            if not broken:
                meta: dict[str, Any] = {"signal_version": _page_signal_version(page)}
                if immutable:
                    meta["immutable"] = True
                findings.append(
                    AuditFinding(
                    category="forward_reference",
                    severity="info",
                    path=str(page.rel_path),
                    detail=(
                        f"Wikilink [[{target}]] is a forward reference to a page "
                        "that does not exist yet"
                    ),
                    proposed_fix=(
                        "Create the referenced page when ready. If the target is a "
                        "typo or obsolete, correct or remove the link."
                    ),
                    meta=meta,
                    )
                )
                continue

            # A broken link inside an append-only tree (Sources/, Evidence/)
            # can't be repaired in place — the containing file is immutable.
            findings.append(
                AuditFinding(
                category="broken_wikilink",
                severity="info" if immutable else "warn",
                path=str(page.rel_path),
                detail=(
                    f"Wikilink [[{target}]] points to a file that doesn't exist"
                    + (" (append-only file — not repairable in place)" if immutable else "")
                ),
                proposed_fix=(
                    "Append-only file (Sources/Evidence): the link can't be edited "
                    "in place. Correct it in the source body desk-side, or accept it "
                    "as a stray reference in captured material."
                        if immutable
                        else "Update the link to the correct target, or remove if obsolete. "
                    "Common cause: target was renamed or moved without supersession."
                ),
                meta={"immutable": True} if immutable else None,
                )
            )
    return findings


def _check_unresolved_source_citations(
    vault_root: Path,
    pages: list[find_module.ParsedPage],
) -> list[AuditFinding]:
    """Report explicit compiled source claims through the writer's validator."""
    from . import record_governance

    root = Path(vault_root)
    authorize = record_governance.full_release_filter(root)
    resolver = find_module.writer_resolver_snapshot(root)
    findings: list[AuditFinding] = []
    for page in sorted(pages, key=lambda item: item.rel_path.encode("utf-8")):
        if page.page_type not in {
            "research-note",
            "insight",
            "failure",
            "pattern",
            "experiment",
            "production-log",
        } or not page.frontmatter.get("sources"):
            continue
        try:
            markdown, _guard = vault_module.read_guarded_text(root, page.path)
        except (OSError, UnicodeError, vault_module.PathGuardError):
            continue
        inspection = source_closure.inspect_source_closure(
            root,
            markdown,
            authorize_path=authorize,
            resolver=resolver,
        )
        if inspection.closed:
            continue
        details = inspection.public_details()
        identity = str(page.frontmatter.get("exomem_id") or page.rel_path)
        finding_material = json.dumps(
            [identity, *inspection.unresolved_values],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        details["finding_id"] = hashlib.sha256(finding_material.encode("utf-8")).hexdigest()
        count = details["unresolved_source_count"]
        findings.append(
            AuditFinding(
                category="unresolved_source_citation",
                severity="warn",
                path=page.rel_path,
                detail=(
                    f"{count} explicit source citation"
                    + (" does" if count == 1 else "s do")
                    + " not resolve to captured material."
                ),
                proposed_fix=(
                    "Capture the original material as governed Source or Evidence "
                    "and update the citation, or explicitly remove the unsupported "
                    "citation. Do not reconstruct an original from this derivative."
                ),
                meta=details,
            )
        )
    return findings


def _has_non_markdown_collision(vault_root: Path, vault_relative: str, kb_relative: str) -> bool:
    """Whether an extensionless note link names an existing non-note file."""
    for candidate in (
        vault_root / vault_relative,
        vault_root / kb_dirname() / kb_relative,
    ):
        try:
            parent_rel = candidate.parent.relative_to(vault_root).as_posix()
            siblings = reserved_paths.list_generic_tree(
                vault_root,
                parent_rel if parent_rel != "." else ".",
                recursive=False,
            )
        except (OSError, ValueError, reserved_paths.ReservedPathLeafError):
            continue
        for sibling in siblings:
            if (
                sibling.identity.kind == "file"
                and "/" not in sibling.relative_path
                and Path(sibling.relative_path).stem == candidate.name
                and Path(sibling.relative_path).suffix.lower() != ".md"
            ):
                return True
    return False


def _ordinary_file_exists(vault_root: Path, candidate: Path) -> bool:
    """Whether one direct attachment target is an ordinary retained file."""

    try:
        rel = candidate.relative_to(vault_root).as_posix()
        reserved_paths.inspect_generic_file(vault_root, rel)
        return True
    except (OSError, ValueError, reserved_paths.ReservedPathLeafError):
        return False


def _walk_vault_md(vault_root: Path):
    """Yield every .md path under the full vault, skipping config/cruft dirs.

    Used for wikilink resolution — broader than find._walk_md which scopes to
    Knowledge Base/ only. Compiled notes can link to curated parent trees
    (per SKILL.md rule 1), so we need a full-vault existence set.
    """
    yield from vault_module.walk_vault_md(vault_root)


# ---------------- check: orphan_entity ----------------


def _check_orphan_entities(
    vault_root: Path, pages: list[find_module.ParsedPage]
) -> list[AuditFinding]:
    findings: list[AuditFinding] = []

    # Collect every wikilink target referenced anywhere in the KB.
    referenced: set[str] = set()
    for page in pages:
        # Don't count self-references and don't count from inside Entities/index.md
        # (those are hub listings, not real "uses").
        if page.rel_path.endswith("/Entities/index.md") or page.rel_path == (
            f"{kb_prefix()}Entities/index.md"
        ):
            continue
        for match in WIKILINK_PATTERN.finditer(page.body):
            target = match.group(1).strip().removeprefix(kb_prefix()).lstrip("/")
            if target:
                referenced.add(target)
        # Frontmatter wikilinks (sources, related, supersedes, etc.) count too.
        for value in page.frontmatter.values():
            for link in _extract_wikilinks_from_value(value):
                referenced.add(link.removeprefix(kb_prefix()).lstrip("/"))

    for page in pages:
        if not page.rel_path.startswith(f"{kb_prefix()}Entities/"):
            continue
        if page.path.name == "index.md":
            continue
        self_key = _rel_kb_path_no_ext(page.path, vault_root)
        if self_key in referenced:
            continue
        findings.append(
            AuditFinding(
            category="orphan_entity",
            severity="info",
            path=page.rel_path,
            detail=f"Entity {self_key!r} has no inbound wikilinks in the KB",
            proposed_fix=(
                "Either link to it from a relevant page (research-note, insight, etc.) "
                "or archive it if no longer relevant."
            ),
            )
        )
    return findings


def _extract_wikilinks_from_value(value) -> list[str]:
    """Pull `[[...]]` strings out of a frontmatter value (string / list / nested)."""
    out: list[str] = []
    if isinstance(value, str):
        for m in WIKILINK_PATTERN.finditer(value):
            out.append(m.group(1).strip())
    elif isinstance(value, list):
        for item in value:
            out.extend(_extract_wikilinks_from_value(item))
    elif isinstance(value, dict):
        for v in value.values():
            out.extend(_extract_wikilinks_from_value(v))
    return out


# ---------------- check: unprocessed_source ----------------


def _check_unprocessed_sources(
    vault_root: Path,
    pages: list[find_module.ParsedPage],
    *,
    today: dt.date | None = None,
) -> list[AuditFinding]:
    """Flag sources with empty ingested_into, aged + triaged oldest-first.

    Adds age signal so the backlog can be drained by priority rather than
    treated as an undifferentiated pile: bucket fresh (<30d) / aging (30-90d) /
    stale (>90d), bump severity to `warn` once stale, sort oldest-first, and
    surface age in `meta` so a client can `propose_compilation` the worst rot
    first.
    """
    today = today or dt.date.today()
    rows: list[tuple[int, AuditFinding]] = []  # (age_days for sort, finding)
    for page in pages:
        if page.frontmatter.get("type") != "source":
            continue
        ingested = page.frontmatter.get("ingested_into")
        if not (ingested is None or (isinstance(ingested, list) and len(ingested) == 0)):
            continue

        captured = _parse_fm_date(
            page.frontmatter.get("captured") or page.frontmatter.get("created")
        )
        meta: dict = {"signal_version": _page_signal_version(page)}
        age_days: int | None = None
        if captured is not None:
            age_days = max(0, (today - captured).days)
            bucket = "fresh" if age_days < 30 else "aging" if age_days < 90 else "stale"
            meta.update(
                {
                    "age_days": age_days,
                    "age_bucket": bucket,
                    "captured": captured.isoformat(),
                }
            )
            severity = "warn" if bucket == "stale" else "info"
            age_phrase = f" ({age_days}d old, {bucket})"
        else:
            bucket = "unknown"
            severity = "info"
            age_phrase = " (capture date unknown)"

        rows.append(
            (
            age_days if age_days is not None else -1,
            AuditFinding(
                category="unprocessed_source",
                severity=severity,
                path=page.rel_path,
                detail=(
                    f"Source has no ingested_into entries — nothing compiled "
                    f"from it yet{age_phrase}"
                ),
                proposed_fix=(
                    "Call `propose_compilation(sources=[this])` for a draft note "
                    "skeleton, then compile via `note` (the back-ref updates "
                    "automatically). Otherwise mark archived or delete."
                ),
                meta=meta,
            ),
            )
        )

    # Oldest first — drain the worst rot first. Capture-unknown (-1) sinks last.
    rows.sort(key=lambda t: t[0], reverse=True)
    return [f for _, f in rows]


def _parse_fm_date(value) -> dt.date | None:
    """Coerce a frontmatter date value (yaml date, datetime, or ISO str) to date.

    Audit checks age pages in whole days — "unprocessed for N days", "not
    reviewed since" — so collapsing a timestamp to its day is the right answer
    here, not a loss. `temporal.parse` additionally accepts the quoted and
    space-separated spellings that `[:10]` prefix-slicing used to mangle.
    """
    moment = temporal.parse(value)
    return moment.day if moment is not None else None


# ---------------- check: index_drift ----------------


def _check_index_drift(vault_root: Path) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    kb = kb_root(vault_root)
    top_index = kb / "index.md"
    try:
        text, _guard = vault_module.read_guarded_text(vault_root, top_index)
    except (OSError, UnicodeDecodeError, vault_module.PathGuardError):
        return findings
    declared: dict[str, int] = {}
    for m in _COUNTS_ROW_PATTERN.finditer(text):
        label, subcat, count = m.group(1), (m.group(2) or "").strip().lower(), int(m.group(3))
        key = f"{label.lower()}:{subcat}" if subcat else label.lower()
        declared[key] = count

    # Actual counts.
    sources = indexes._count_sources(kb / "Sources")
    notes = indexes._count_notes(kb / "Notes")
    entities = indexes._count_entities(kb / "Entities")
    actual: dict[str, int] = {
        "sources": sum(sources.values()),
        "notes": sum(notes.values()),
        "entities": sum(entities.values()),
    }
    for type_key, n in notes.items():
        actual[f"notes:{type_key}"] = n
    for type_key, n in entities.items():
        actual[f"entities:{type_key}"] = n

    # Compare. Only flag drift for keys present in declared (the index defines what's tracked).
    for key, declared_count in declared.items():
        actual_count = actual.get(key)
        if actual_count is None:
            findings.append(
                AuditFinding(
                category="index_drift",
                severity="warn",
                path=f"{kb_prefix()}index.md",
                detail=(
                    f"Counts row {key!r} declared {declared_count} but the on-disk "
                    "folder doesn't exist"
                ),
                proposed_fix="Remove the row or create the missing folder.",
                )
            )
            continue
        if actual_count != declared_count:
            findings.append(
                AuditFinding(
                category="index_drift",
                severity="warn",
                path=f"{kb_prefix()}index.md",
                detail=(
                    f"Counts row {key!r} declared {declared_count}, actual is {actual_count}"
                ),
                proposed_fix=(
                    "Update the Counts line manually (or run an `audit --fix` once "
                    "auto-fix is supported)."
                ),
                )
            )
    return findings


# ---------------- check: tag_inconsistency ----------------


_TAG_NORMALIZE_PATTERN = re.compile(r"[\s_]+")


def _normalize_tag(tag: str) -> str:
    """Lowercase + collapse whitespace/underscores to dashes for cluster keying.

    `Warning-Letter-Incident`, `warning_letter_incident`, `warning  letter  incident`
    all normalize to `warning-letter-incident`.
    """
    return _TAG_NORMALIZE_PATTERN.sub("-", tag.strip().lower())


def _extract_tags(value) -> list[str]:
    """Pull tags out of a frontmatter `tags:` value (string, list, or nested)."""
    out: list[str] = []
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                out.append(item)
    return out


def _check_tag_inconsistency(
    pages: list[find_module.ParsedPage],
) -> list[AuditFinding]:
    """Detect variant clusters: distinct raw tags that normalize to the same
    key (e.g. `warning_letter_incident` vs `warning-letter-incident` vs
    `Warning-Letter-Incident`).

    Only mechanical drift (case + separator) is detected. Semantic
    near-duplicates like `workflow` vs `workflows` are NOT flagged — that
    needs human or LLM judgment.

    Singleton tags (used exactly once) are NOT flagged — too noisy in
    practice (a healthy KB has many genuinely-unique one-offs).

    Source pages are immutable per rule 2, so their tags can't be fixed in
    place. The finding's proposed_fix names the compiled-material rewrite path.
    """
    findings: list[AuditFinding] = []

    # raw_tag -> list of pages using it
    raw_to_pages: dict[str, list[str]] = {}
    for page in pages:
        for raw in _extract_tags(page.frontmatter.get("tags")):
            raw_to_pages.setdefault(raw, []).append(page.rel_path)

    # Group raw tags by normalized key.
    norm_to_raws: dict[str, list[str]] = {}
    for raw in raw_to_pages:
        norm_to_raws.setdefault(_normalize_tag(raw), []).append(raw)

    # Variant clusters: normalized keys with >1 raw variant.
    for raws in norm_to_raws.values():
        if len(raws) < 2:
            continue
        # Canonical = the most-used raw variant; ties broken by lex order.
        canonical = max(raws, key=lambda r: (len(raw_to_pages[r]), r))
        for raw in raws:
            if raw == canonical:
                continue
            using_pages = raw_to_pages[raw]
            findings.append(
                AuditFinding(
                category="tag_inconsistency",
                severity="info",
                path=using_pages[0],  # representative; full list in detail
                detail=(
                    f"Tag {raw!r} (used in {len(using_pages)} page(s)) is a variant "
                    f"of {canonical!r} (used in {len(raw_to_pages[canonical])} page(s)). "
                    f"Using pages: {using_pages}"
                ),
                proposed_fix=(
                    f"Normalize {raw!r} → {canonical!r}. For compiled material, "
                    "rewrite the tag via `replace`. Source pages are immutable per "
                    "SKILL.md rule 2; normalize forward via downstream compiled "
                    "pages that cite them."
                ),
                )
            )

    return findings


# ---------------- check: frontmatter_compliance ----------------


# Required fields per page-type (per `_Schema/references/frontmatter.md`).
# Optional / per-type-conditional fields aren't enforced — those depend on
# subtype and can't be validated without parser-level type intent.
_REQUIRED_FIELDS_BY_TYPE: dict[str, tuple[str, ...]] = {
    "source": ("type", "source_type", "captured"),
    "research-note": ("type", "project", "status", "created", "updated"),
    "insight": ("type", "status", "created", "updated"),
    "failure": ("type", "status", "created", "updated"),
    "pattern": ("type", "status", "created", "updated"),
    "experiment": ("type", "domain", "status", "created", "updated", "started", "duration"),
    "production-log": ("type", "medium", "status", "created", "updated"),
    "entity": ("type", "entity_type", "status", "created", "updated"),
}


def _check_frontmatter_compliance(
    pages: list[find_module.ParsedPage],
) -> list[AuditFinding]:
    """Surface per-page-type frontmatter problems.

    Three classes of finding:
    - Missing required field for the declared `type:`.
    - `tenant:` set on a non-Q page (the `tenant` field is Q-only).
    - Pattern page with singular `project:` instead of plural `projects:`
      (the convention for cross-project patterns).
    """
    findings: list[AuditFinding] = []
    for page in pages:
        fm = page.frontmatter
        excluded = vault_module.first_excluded_field(fm)
        if excluded is not None:
            field, _reason = excluded
            findings.append(
                AuditFinding(
                category="frontmatter_compliance",
                severity="warn",
                path=page.rel_path,
                detail=f"{field!r} is a schema-excluded frontmatter field.",
                proposed_fix=f"Remove the {field!r} frontmatter field.",
                )
            )
        page_type = fm.get("type")
        if not isinstance(page_type, str):
            continue
        if page_type == "collection":
            nested_excluded = vault_module.excluded_field_in_collection_frontmatter(fm)
            if nested_excluded is not None:
                field, _reason = nested_excluded
                item_schema = fm.get("item_schema")
                schema_fields = item_schema.get("fields") if isinstance(item_schema, dict) else None
                in_schema = isinstance(schema_fields, dict) and field in schema_fields
                if in_schema:
                    detail = (
                        f"Collection item_schema.fields declares schema-excluded field {field!r}."
                    )
                    proposed_fix = (
                        f"Delete {field!r} from every item first, then revise the "
                        "collection manifest to remove the field declaration."
                    )
                else:
                    # The note field lives in the immutable storage descriptor, so
                    # revise refuses with IMMUTABLE_COLLECTION_REPRESENTATION. The
                    # only route out is a new collection plus migration.
                    detail = (
                        f"Collection Markdown-log note field declares schema-excluded "
                        f"field {field!r}."
                    )
                    proposed_fix = (
                        f"Delete {field!r} from every item, then migrate to a new "
                        "collection whose note field uses a permitted name — the "
                        "storage descriptor is immutable, so revise cannot remove it."
                    )
                findings.append(
                    AuditFinding(
                    category="frontmatter_compliance",
                    severity="warn",
                    path=page.rel_path,
                    detail=detail,
                    proposed_fix=proposed_fix,
                    )
                )
        required = _REQUIRED_FIELDS_BY_TYPE.get(page_type)
        if required:
            missing = [k for k in required if not fm.get(k)]
            if missing:
                findings.append(
                    AuditFinding(
                    category="frontmatter_compliance",
                    severity="warn",
                    path=page.rel_path,
                    detail=(
                            f"{page_type!r} page missing required frontmatter field(s): {missing}"
                    ),
                    proposed_fix=(
                        "Add the missing field(s) via `set_frontmatter_field` "
                        "or `edit`. See `_Schema/references/frontmatter.md` "
                        "for the per-type required set."
                    ),
                    )
                )
        # tenant: is Q-only.
        if fm.get("tenant") and fm.get("project") != "q":
            findings.append(
                AuditFinding(
                category="frontmatter_compliance",
                severity="warn",
                path=page.rel_path,
                detail=(
                    f"`tenant: {fm['tenant']!r}` set but `project` is "
                    f"{fm.get('project')!r}, not 'q'. The tenant field is "
                    f"Q-only."
                ),
                proposed_fix=(
                    "Either set `project: q` (if this is a Q-tenant note) "
                    "or remove the `tenant:` field."
                ),
                )
            )
        # Patterns should use plural `projects:`, not singular `project:`.
        if page_type == "pattern" and fm.get("project") and not fm.get("projects"):
            findings.append(
                AuditFinding(
                category="frontmatter_compliance",
                severity="info",
                path=page.rel_path,
                detail=(
                    "pattern page uses singular `project:` instead of plural "
                    "`projects:` (the convention for cross-project patterns)."
                ),
                proposed_fix=(
                    "Rename to `projects: [<key>]` (plural list form) via "
                    "`set_frontmatter_field`."
                ),
                )
            )
    return findings


# ---------------- check: unregistered_project_key ----------------


def _check_unregistered_project_keys(
    vault_root: Path, pages: list[find_module.ParsedPage]
) -> list[AuditFinding]:
    """Flag frontmatter `project:` / `projects:` values not in the registry.

    Catches drift that bypasses `note`/`replace`/`set_frontmatter_field`'s
    auto-register (e.g. pre-typo-guard history, or values landed via the
    Tier 2 `create_file` escape hatch).
    """
    from . import project_keys as project_keys_module

    registry = project_keys_module.load_project_registry(vault_root)
    valid = set(registry.project_to_folder.keys())
    findings: list[AuditFinding] = []
    for page in pages:
        fm = page.frontmatter
        if not isinstance(fm, dict):
            continue
        seen: list[tuple[str, str]] = []  # (field, key)
        single = fm.get("project")
        if isinstance(single, str) and single:
            seen.append(("project", single))
        plural = fm.get("projects")
        if isinstance(plural, list):
            for v in plural:
                if isinstance(v, str) and v:
                    seen.append(("projects", v))
        for field, key in seen:
            if key in valid:
                continue
            findings.append(
                AuditFinding(
                category="unregistered_project_key",
                severity="warn",
                path=page.rel_path,
                detail=(
                    f"`{field}: {key!r}` not in _Schema/project-keys.yaml "
                    f"registry. Drift from a pre-guard write or a Tier 2 "
                    f"escape-hatch path."
                ),
                proposed_fix=(
                    f"If {key!r} is a typo, fix the frontmatter via "
                    f"`set_frontmatter_field` (the typo guard will surface "
                    f"the intended key). If it's a real new key, hand-add "
                    f"it to _Schema/project-keys.yaml."
                ),
                )
            )
    return findings


# ---------------- check: graph_drift ----------------


def _check_reference_identity(vault_root: Path) -> list[AuditFinding]:
    """Flag duplicate/malformed IDs and reference-sidecar drift."""
    from . import memory_refs

    findings: list[AuditFinding] = []
    for item in memory_refs.scan_issues(vault_root):
        kind = item["kind"]
        value = item["value"]
        findings.append(
            AuditFinding(
            category="reference_identity",
            severity="error",
            path=item["path"],
            detail=f"{kind} exomem_id: {value}",
            proposed_fix=(
                "Assign a unique UUID in `exomem_id` and run `maintain_memory` "
                "with mode `reconcile`."
            ),
            meta=item,
            )
        )
    for item in memory_refs.drift(vault_root):
        findings.append(
            AuditFinding(
            category="reference_identity",
            severity="info",
            path=item["path"],
            detail=item["reason"],
            proposed_fix="Run `maintain_memory` with mode `reconcile`.",
            meta=item,
            )
        )
    return findings


def _check_graph_drift(vault_root: Path) -> list[AuditFinding]:
    """Flag derived graph sidecar drift. Read-only and disabled-gate aware."""
    from . import epistemic_graph, recall_policy

    findings: list[AuditFinding] = []
    for item in epistemic_graph.graph_drift(vault_root):
        path = str(item.get("path") or kb_prefix())
        if path.lower().endswith(".md"):
            safe_path = _safe_persisted_markdown_rel(path)
            if safe_path is None:
                continue
            candidate = _no_follow_regular_markdown_path(vault_root, safe_path)
            if candidate is not None and not recall_policy.is_recall_candidate(
                vault_root, candidate
            ):
                continue
        reason = str(item.get("reason") or "graph drift")
        findings.append(
            AuditFinding(
            category="graph_drift",
            severity="info",
            path=path,
            detail=reason,
            proposed_fix="Run `reconcile` to refresh the derived graph sidecar.",
            meta=item,
            )
        )
    return findings


def _check_relation_registry(vault_root: Path) -> list[AuditFinding]:
    """Opt-in ontology-governance findings; never feeds default attention."""
    from . import memory_schema

    report = memory_schema.validate_relation_registry(vault_root)
    findings: list[AuditFinding] = []
    for item in report["findings"]:
        findings.append(
            AuditFinding(
            category="relation_registry",
            severity=str(item.get("severity") or "warning"),
            path=str(item.get("path") or kb_prefix()),
            detail=str(item.get("detail") or item.get("code") or "relation registry finding"),
            proposed_fix=(
                "Review corpus evidence with schema_memory(subject='relations') and save "
                "a complete, hash-guarded proposal."
            ),
            meta=item,
            )
        )
    return findings


# ---------------- check: embedding_drift ----------------


def _check_embedding_drift(vault_root: Path) -> list[AuditFinding]:
    """Flag embedding drift in three forms: (1) sidecar rows whose on-disk file
    mtime is newer than the row (external edit), (2) rows whose file is gone from
    disk, and (3) on-disk embeddable files with NO sidecar row at all — never
    embedded, e.g. created out-of-band in Obsidian / mobile / a filesystem write,
    which bypass the writer's embed hook.

    External edits/creates don't trigger the writer hooks, so the vector sidecar
    drifts silently. `reconcile` heals all three incrementally (it re-embeds
    whatever this flags); `audit_fix(rebuild_embeddings=True)` resolves them in
    one wipe-and-rebuild.
    """
    findings: list[AuditFinding] = []
    from . import index_paths

    sidecar = index_paths.sidecar_path(vault_root)
    if not sidecar.exists():
        return findings
    import sqlite3

    from . import recall_policy

    try:
        conn = sqlite3.connect(sidecar)
    except sqlite3.Error:
        return findings
    try:
        try:
            rows = conn.execute(
                "SELECT file_path, MAX(file_mtime) FROM chunks GROUP BY file_path"
            ).fetchall()
        except sqlite3.Error:
            return findings
    finally:
        conn.close()
    seen: set[str] = set()
    for rel_path, row_mtime in rows:
        safe_rel = _safe_persisted_markdown_rel(rel_path)
        if safe_rel is None or safe_rel in seen:
            continue
        seen.add(safe_rel)
        abs_path = _no_follow_regular_markdown_path(vault_root, safe_rel)
        if abs_path is None:
            try:
                missing = not os.path.lexists(vault_root.joinpath(*PurePosixPath(safe_rel).parts))
            except OSError:
                missing = False
            if not missing:
                continue
            # File removed in vault but still in sidecar: surface that too.
            findings.append(
                AuditFinding(
                category="embedding_drift",
                severity="info",
                path=safe_rel,
                detail="sidecar row for file no longer on disk",
                    proposed_fix=("Run `audit_fix(rebuild_embeddings=true)` to drop stale rows."),
                )
            )
            continue
        try:
            disk_mtime = abs_path.stat().st_mtime
        except OSError:
            continue
        if not recall_policy.is_recall_candidate(vault_root, abs_path):
            continue
        if disk_mtime > (row_mtime or 0) + 1.0:  # 1s slack for FS jitter
            findings.append(
                AuditFinding(
                category="embedding_drift",
                severity="info",
                path=safe_rel,
                detail=(
                    f"file mtime ({disk_mtime:.0f}) newer than sidecar "
                    f"({(row_mtime or 0):.0f}) — likely external edit."
                ),
                proposed_fix=(
                    "Run `reconcile` (or `audit_fix(rebuild_embeddings=true)`) to refresh."
                ),
                )
            )

    # Files on disk that were NEVER embedded — no sidecar row at all. The scan
    # above only compares existing rows, so out-of-band *creates* (Obsidian /
    # mobile / filesystem writes that bypass the writer's embed hook) stay
    # vector-invisible until caught here. Mirror the embedder's selection
    # (_index_walk + _is_embeddable_path + non-empty chunks) so we never flag a
    # file the rebuild itself would skip — that would be perpetual drift.
    #
    # LOCKSTEP with the index scope (EXOMEM_INDEX_SCOPE): under "vault" the
    # embedder covers the whole vault, so the never-embedded scan must too —
    # else out-of-KB creates would never be flagged and never get reconciled.
    # The "kb" branch is byte-identical to the historical KB-only scan.
    from . import embeddings as embeddings_module
    from . import index_paths

    scope = index_paths.index_scope()
    never_walk = index_paths.iter_index_markdown(vault_root)
    for md in never_walk:
        if not index_paths.is_embeddable_path(md):
            continue
        try:
            rel = md.resolve().relative_to(vault_root.resolve()).as_posix()
        except (ValueError, OSError):
            continue
        if rel in seen:
            continue
        if not recall_policy.is_recall_candidate(vault_root, md):
            continue
        # Vault scope walks trees the KB scan never reached; honor is_indexable
        # so an `excluded` out-of-KB subtree isn't flagged as perpetual drift
        # (the embedder skips it). KB scope keeps its historical behavior.
        if scope == "vault" and not access.is_indexable(vault_root, rel):
            continue
        page = find_module._CACHE.get(md, vault_root)
        if page is None or not embeddings_module.chunk_text(page.title, page.body):
            continue  # empty / no-chunk file — the embedder skips it too
        findings.append(
            AuditFinding(
            category="embedding_drift",
            severity="info",
            path=rel,
            detail="file has no sidecar row — never embedded (out-of-band create).",
            proposed_fix="Run `reconcile` to embed it incrementally.",
            )
        )
    return findings


# ---------------- check: relevance_pairs_pending ----------------


def _relevance_canon(path: str) -> str:
    """Delegates to the shared usage primitives (see `usage.py`)."""
    from . import usage

    return usage.canon(path)


def _relevance_read_jsonl(path: Path) -> list[dict]:
    """Delegates to the shared usage primitives (see `usage.py`)."""
    from . import usage

    return usage.read_jsonl(path)


def _relevance_golden_queries(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    except yaml.YAMLError:
        return set()
    return {e["query"].strip().lower() for e in raw if isinstance(e, dict) and e.get("query")}


def _check_relevance_pairs_pending(
    *,
    logs_dir: Path | None = None,
    golden_path: Path | None = None,
    window_seconds: float = 7200.0,
) -> list[AuditFinding]:
    """Surface real-usage (query -> cited_path) relevance signal not yet in the
    golden set — the retrieval feedback loop's unconfirmed backlog.

    A note/replace write that cites a path shortly after a find() which surfaced
    that path is a weak (query -> path) relevance label (see
    `scripts/derive_relevance_pairs.py`). When such a query isn't yet in
    `tests/golden/queries.yaml`, ranking has measurable signal nobody has
    confirmed. Pure log-join (model-free), so it's safe to run inside audit.

    Gated by `EXOMEM_DISABLE_RELEVANCE_CHECK` (set by the test suite) so the
    per-vault audit stays deterministic regardless of the repo-global logs.
    """
    if os.environ.get("EXOMEM_DISABLE_RELEVANCE_CHECK"):
        return []
    logs_dir = logs_dir or _RELEVANCE_LOGS_DIR
    golden_path = golden_path or _RELEVANCE_GOLDEN

    queries = _relevance_read_jsonl(logs_dir / "queries.jsonl")
    writes = _relevance_read_jsonl(logs_dir / "writes.jsonl")
    if not queries or not writes:
        return []

    existing = _relevance_golden_queries(golden_path)
    new_queries: set[str] = set()
    pairs_in_window = 0
    for w in writes:
        if w.get("tool") not in ("note", "replace"):
            continue
        try:
            w_ts = dt.datetime.fromisoformat(w.get("ts", ""))
        except (ValueError, TypeError):
            continue
        cited = {_relevance_canon(c) for c in (w.get("cited_sources") or []) if c}
        if not cited:
            continue
        for q in queries:
            try:
                q_ts = dt.datetime.fromisoformat(q.get("ts", ""))
            except (ValueError, TypeError):
                continue
            delta = (w_ts - q_ts).total_seconds()
            if not (0 <= delta <= window_seconds):
                continue
            ranked = {
                _relevance_canon(t.get("path", "")) for t in (q.get("top_k") or []) if t.get("path")
            }
            if cited & ranked:
                pairs_in_window += 1
                ql = (q.get("query") or "").strip()
                if ql and ql.lower() not in existing:
                    new_queries.add(ql)

    if not new_queries:
        return []
    return [
        AuditFinding(
        category="relevance_pairs_pending",
        severity="info",
        path="logs/queries.jsonl",
        detail=(
            f"{len(new_queries)} query/result pair(s) from real usage are not in "
            "the golden set yet — unconfirmed retrieval feedback signal."
        ),
        proposed_fix=(
            "Run `python scripts/derive_relevance_pairs.py` to review the "
            "proposed (query -> cited_path) labels, paste confirmed ones into "
            "tests/golden/queries.yaml, then re-run `scripts/eval_retrieval.py`."
        ),
        meta={"new_queries": len(new_queries), "pairs_in_window": pairs_in_window},
        )
    ]


# ---------------- check: relation_debt ----------------

_RELATION_DEBT_TYPES = frozenset(
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


def relation_debt_eligible(
    vault_root: Path,
    *,
    page_type: str | None,
    rel_path: str,
    status: str | None,
    tags: list[str] | tuple[str, ...] | set[str] | frozenset[str],
) -> bool:
    """Whether one page participates in the shared relation-debt predicate."""
    if page_type not in _RELATION_DEBT_TYPES:
        return False
    path = PurePosixPath(str(rel_path).replace("\\", "/"))
    if path.name in ("index.md", "log.md"):
        return False
    if status in ("superseded", "archived", "draft", "dropped"):
        return False
    if access.access_tier(vault_root, path.as_posix()) != access.TIER_READ_WRITE:
        return False
    stem = path.stem.lower()
    if any(stem.endswith(suffix) for suffix in _STALE_SKIP_SLUG_SUFFIXES):
        return False
    return not bool(_STALE_SKIP_TAGS & set(tags))


def _check_relation_debt(
    vault_root: Path,
    pages: list[find_module.ParsedPage],
) -> list[AuditFinding]:
    """Surface active compiled pages with no explicit outbound Markdown edges."""
    findings: list[AuditFinding] = []
    relations = relation_registry.load_registry(vault_root)
    language = semantic_language_registry.load_registry(vault_root)
    for page in pages:
        if not relation_debt_eligible(
            vault_root,
            page_type=page.page_type,
            rel_path=page.rel_path,
            status=page.status,
            tags=page.tags,
        ):
            continue

        document = semantic_units.parse_semantic_units(
            page.body,
            validate=False,
            language_registry=language,
            relation_registry=relations,
            page_type=page.page_type,
        )
        typed_count = len(document.note_relations) + sum(
            len(unit.relations) for unit in document.rich_units
        )
        body_link_count = sum(1 for _ in find_body_wikilinks(page.body))
        if typed_count or body_link_count:
            continue

        findings.append(
            AuditFinding(
                category="relation_debt",
                severity="info",
                path=page.rel_path,
                detail=(
                    "Active compiled page has no outbound Markdown connections; "
                    "its semantic neighbours are not visible as durable graph edges."
                ),
                proposed_fix=(
                    "Review `connect_memory(operation='suggest-relations')` or "
                    "`suggest-links`; accept only meaningful edges, writing note-level "
                    "relations under `## Relations`. Nothing is auto-written."
                ),
                meta={
                    "signal_version": _page_signal_version(page),
                    "typed_relations": typed_count,
                    "body_wikilinks": body_link_count,
                },
            )
        )

    return sorted(findings, key=lambda finding: finding.path)


# ---------------- check: missing_sources ----------------

# `_Schema/references/frontmatter.md` marks `sources:` required for these four
# compiled types. Deliberately NOT expressed through `_REQUIRED_FIELDS_BY_TYPE`:
# that table is `warn` severity (overstating a chronic, often-honest condition),
# it would swamp `frontmatter_compliance` — whose job is structural integrity —
# with hundreds of findings, and `audit_fix` iterates it to backfill inferable
# values. Provenance is exactly the field that must never be inferred.
_SOURCES_REQUIRED_TYPES = frozenset({"research-note", "insight", "failure", "pattern"})


def _check_missing_sources(
    vault_root: Path,
    pages: list[find_module.ParsedPage],
) -> list[AuditFinding]:
    """Surface active compiled pages that should cite provenance and cite none."""
    findings: list[AuditFinding] = []
    for page in pages:
        if page.page_type not in _SOURCES_REQUIRED_TYPES:
            continue
        if page.path.name in ("index.md", "log.md"):
            continue
        if page.status in ("superseded", "archived", "draft", "dropped"):
            continue
        if access.access_tier(vault_root, page.rel_path) != access.TIER_READ_WRITE:
            continue
        stem = page.path.stem.lower()
        if any(stem.endswith(suffix) for suffix in _STALE_SKIP_SLUG_SUFFIXES):
            continue
        if _STALE_SKIP_TAGS & set(page.tags):
            continue
        if page.frontmatter.get("sources"):
            continue

        findings.append(
            AuditFinding(
                category="missing_sources",
                severity="info",
                path=page.rel_path,
                detail=(
                    "Active compiled page cites no `sources:`; the raw material it "
                    "was drawn from is not recoverable from the page itself, and any "
                    "originating source still counts as unprocessed."
                ),
                proposed_fix=(
                    "If this came from live work with nothing captured, it is an "
                    "honest empty list — dismiss it. Otherwise cite the `Sources/` "
                    "page, which also appends this note to that source's "
                    "`ingested_into:`. Nothing is auto-written or inferred."
                ),
                meta={"signal_version": _page_signal_version(page)},
            )
        )

    return sorted(findings, key=lambda finding: finding.path)


# ---------------- check: derivation_double_counting ----------------

# Bounds on the `sources:` (`derived_from`) chain walk. Both are env-overridable
# so ops can tune without redeploying (same convention as `_stale_thresholds`).
# The depth cap bounds how many hops are followed from any single node; the edge
# cap bounds total work across the WHOLE audit pass (shared across every walk),
# protecting a vault with thousands of notes and dense wikilinks from an
# unbounded chain walk. Either cap being hit produces a dedicated `truncated`
# finding rather than silently under-reporting.
#
# The edge default is derived, not guessed: a mutation-testing pass measured
# this walk (no cross-walk closure reuse — every distinct `sources:`-bearing
# page gets its own independent bounded BFS) consuming ~10 edges per sourced
# page in a chain-shaped graph, exhausting a 2000 budget at ~200 sourced
# pages. For a ~5,000-file vault, assuming a generous (over-)estimate of up to
# half the corpus carrying `sources:` (2,500 sourced pages) at that same ~10
# edges/page rate: 2,500 * 10 = 25,000 edges needed; doubled for margin =
# 50,000. See design.md D2 for the full derivation and the stress-test that
# validated it.
_DERIVATION_DEFAULT_MAX_DEPTH = 12
_DERIVATION_DEFAULT_MAX_EDGES = 50_000

_DERIVATION_TRUNCATED_BY_DEPTH = "depth"
_DERIVATION_TRUNCATED_BY_EDGES = "edges"


def _derivation_traversal_limits() -> tuple[int, int]:
    """(max_depth, max_edges); bad/non-positive env values fall back to default."""

    def _int_env(name: str, default: int) -> int:
        raw = os.environ.get(name)
        if raw is None:
            return default
        try:
            value = int(raw)
        except ValueError:
            log.warning("invalid %s=%r; using %s", name, raw, default)
            return default
        return value if value > 0 else default

    return (
        _int_env("EXOMEM_DERIVATION_MAX_DEPTH", _DERIVATION_DEFAULT_MAX_DEPTH),
        _int_env("EXOMEM_DERIVATION_MAX_EDGES", _DERIVATION_DEFAULT_MAX_EDGES),
    )


@dataclass
class _DerivationBudget:
    """Total-edge cap shared across every walk in one audit pass."""

    remaining: int

    def take(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


@dataclass(frozen=True)
class _DerivationWalk:
    ancestors: frozenset[str]        # keys reachable from `start`, excluding `start`
    cycle_path: tuple[str, ...] | None  # start -> ... -> start, if `start` reaches itself
    truncated_reasons: frozenset[str]  # subset of {"depth", "edges"}; empty = complete


def _derivation_direct_sources(
    pages: list[find_module.ParsedPage],
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """(direct_sources, raw_by_key).

    `direct_sources`: canon(page) -> ordered, de-duplicated canon(source) keys
    from `sources:`. A page citing itself is kept (not filtered) so a trivial
    self-reference is caught by cycle detection rather than silently dropped
    from the graph.

    `raw_by_key`: canon key -> the first raw `sources:` wikilink text observed
    for it, so an UNRESOLVED target (no matching page) can still be rendered
    as a plausible vault-relative path in a finding rather than an internal
    lowercase canon key an agent cannot open.
    """
    direct: dict[str, list[str]] = {}
    raw_by_key: dict[str, str] = {}
    for page in pages:
        key = _relevance_canon(page.rel_path)
        seen: set[str] = set()
        targets: list[str] = []
        for link in _extract_wikilinks_from_value(page.frontmatter.get("sources")):
            target_key = _relevance_canon(link)
            if not target_key or target_key in seen:
                continue
            seen.add(target_key)
            targets.append(target_key)
            raw_by_key.setdefault(target_key, link)
        direct[key] = targets
    return direct, raw_by_key


def _derivation_best_effort_path(raw: str) -> str:
    """Reconstruct a plausible vault-relative rel_path for an unresolved
    `sources:` target. Every other `AuditFinding.path` (and every path inside
    `meta`) is a vault-relative rel_path an agent can open directly; falling
    back to the internal lowercase canon key instead would silently break
    that contract for exactly the pages a reviewer most needs to inspect.
    """
    cleaned = raw.strip().strip("/")
    if not cleaned:
        return raw
    if not cleaned.startswith(kb_prefix()):
        cleaned = kb_prefix() + cleaned
    if not cleaned.lower().endswith(".md"):
        cleaned = cleaned + ".md"
    return cleaned


def _bounded_ancestor_walk(
    direct_sources: dict[str, list[str]],
    start: str,
    *,
    max_depth: int,
    budget: _DerivationBudget,
) -> _DerivationWalk:
    """BFS from `start` over `derived_from` edges, bounded by depth + a shared edge
    budget. Always terminates: `seen` guards against revisiting a node regardless
    of cycles, and the budget bounds total edges expanded across the whole audit
    pass. Detects whether `start` is reachable from itself (a cycle) and
    reconstructs one witness path.
    """
    parent: dict[str, str] = {}
    seen = {start}
    ancestors: set[str] = set()
    frontier: deque[tuple[str, int]] = deque([(start, 0)])
    cycle_path: tuple[str, ...] | None = None
    truncated_reasons: set[str] = set()
    while frontier:
        node, depth = frontier.popleft()
        if depth >= max_depth:
            if direct_sources.get(node):
                truncated_reasons.add(_DERIVATION_TRUNCATED_BY_DEPTH)
            continue
        for target in direct_sources.get(node, ()):
            if not budget.take():
                truncated_reasons.add(_DERIVATION_TRUNCATED_BY_EDGES)
                break
            if target == start:
                if cycle_path is None:
                    path = [node]
                    cur = node
                    while cur != start:
                        cur = parent[cur]
                        path.append(cur)
                    cycle_path = tuple(reversed(path)) + (start,)
                ancestors.add(target)
                continue
            ancestors.add(target)
            if target not in seen:
                seen.add(target)
                parent[target] = node
                frontier.append((target, depth + 1))
    return _DerivationWalk(
        ancestors=frozenset(ancestors),
        cycle_path=cycle_path,
        truncated_reasons=frozenset(truncated_reasons),
    )


def _nearest_shared_roots(
    candidates: dict[str, set[str]],
    walk,
) -> dict[str, set[str]]:
    """Collapse one converging ancestral tail to its nearest node(s) only.

    A shared root `Y` is dropped when some OTHER candidate `X` can reach `Y`
    (i.e. `Y` is further upstream than `X`, discovered on the same tail) —
    keeping only the convergence point(s) closest to the citing page instead
    of emitting one finding per node in a multi-hop shared tail. Pairwise and
    order-independent, so it is correct regardless of candidate iteration
    order. If every candidate ends up mutually dominated (a cycle among the
    candidates themselves), keep one deterministic representative rather than
    silently emitting nothing for a genuine collapse.

    Known gap, deliberately not addressed: if the candidates form TWO (or
    more) disjoint mutually-dominating cyclic clusters — e.g. {P, Q} each
    reachable from the other, and separately {R, S} each reachable from the
    other, with no path between the two clusters — every candidate across
    BOTH clusters is "dominated by someone", so the single-survivor fallback
    picks one representative overall and silently drops the other cluster's
    distinct convergence point, not just redundant nodes on the same tail.
    This is exotic (it requires the ancestor graph itself to contain a cycle,
    which is separately reported as its own `warn`-severity `cycle` finding)
    and not worth the extra bookkeeping a per-cluster fallback would need.
    """
    keys = list(candidates)
    if len(keys) <= 1:
        return dict(candidates)
    dominated: set[str] = set()
    for x in keys:
        reachable_from_x = walk(x).ancestors
        for y in keys:
            if y != x and y in reachable_from_x:
                dominated.add(y)
    survivors = [k for k in keys if k not in dominated]
    if not survivors:
        survivors = [min(keys)]
    return {root: candidates[root] for root in survivors}


def _check_derivation_double_counting(
    vault_root: Path,
    pages: list[find_module.ParsedPage],
) -> list[AuditFinding]:
    """Walk `sources:` chains for support-collapse and circular derivation.

    Read-only and observe-before-enforce: reports findings only, never rewrites
    a relation, never demotes anything, never blocks a write. Severity is
    `warn` for a genuine cycle (a structural inconsistency) and `info` for a
    support-collapse candidate (a review candidate, not a defect) — never
    `error`.
    """
    direct_sources, raw_by_key = _derivation_direct_sources(pages)
    pages_by_canon = {_relevance_canon(page.rel_path): page for page in pages}
    max_depth, max_edges = _derivation_traversal_limits()
    budget = _DerivationBudget(max_edges)
    walk_cache: dict[str, _DerivationWalk] = {}
    truncated_reasons_seen: set[str] = set()
    findings: list[AuditFinding] = []
    cycles_reported: set[frozenset[str]] = set()

    def walk(key: str) -> _DerivationWalk:
        if key not in walk_cache:
            walk_cache[key] = _bounded_ancestor_walk(
                direct_sources, key, max_depth=max_depth, budget=budget
            )
        return walk_cache[key]

    def display_path(key: str) -> str:
        page = pages_by_canon.get(key)
        if page is not None:
            return page.rel_path
        raw = raw_by_key.get(key)
        return _derivation_best_effort_path(raw) if raw else key

    # -- circular derivation: any node with outgoing `sources:` edges may loop.
    for key in sorted(direct_sources):
        if not direct_sources[key]:
            continue
        result = walk(key)
        truncated_reasons_seen |= result.truncated_reasons
        if result.cycle_path is None:
            continue
        identity = frozenset(result.cycle_path)
        if identity in cycles_reported:
            continue
        cycles_reported.add(identity)
        origin_page = pages_by_canon.get(key)
        cycle_display = [display_path(k) for k in result.cycle_path]
        findings.append(
            AuditFinding(
                category="derivation_double_counting",
                severity="warn",
                path=display_path(key),
                detail=(
                    "Circular derivation: "
                    + " -> ".join(cycle_display)
                    + " — this `sources:` chain supports itself."
                ),
                proposed_fix=(
                    "Review the `sources:` chain for a mistaken back-reference and "
                    "remove or correct one entry to break the cycle. Nothing is "
                    "auto-written."
                ),
                meta={
                    "kind": "cycle",
                    "cycle": cycle_display,
                    "signal_version": (_page_signal_version(origin_page) if origin_page else None),
                },
            )
        )

    # -- support collapse: only from active, read-write, provenance-bearing
    # compiled pages citing two or more sources as (nominally) independent
    # support. Mirrors `_check_missing_sources`'s origination gate exactly,
    # including its hub/snapshot/slug-suffix common-hub damping — a hub or
    # snapshot page is EXPECTED to fan its `sources:` out from a shared root
    # and would otherwise dominate this queue with non-actionable noise.
    for page in pages:
        if page.page_type not in _SOURCES_REQUIRED_TYPES:
            continue
        if page.path.name in ("index.md", "log.md"):
            continue
        if page.status in ("superseded", "archived", "draft", "dropped"):
            continue
        if access.access_tier(vault_root, page.rel_path) != access.TIER_READ_WRITE:
            continue
        stem = page.path.stem.lower()
        if any(stem.endswith(suffix) for suffix in _STALE_SKIP_SLUG_SUFFIXES):
            continue
        if _STALE_SKIP_TAGS & set(page.tags):
            continue
        key = _relevance_canon(page.rel_path)
        directs = direct_sources.get(key, [])
        if len(directs) < 2:
            continue

        closures: dict[str, frozenset[str]] = {}
        page_reasons: set[str] = set()
        for source_key in directs:
            result = walk(source_key)
            closures[source_key] = frozenset({source_key}) | result.ancestors
            page_reasons |= result.truncated_reasons
        truncated_reasons_seen |= page_reasons

        collapse_roots: dict[str, set[str]] = {}
        for i, a in enumerate(directs):
            for b in directs[i + 1 :]:
                for shared_root in closures[a] & closures[b]:
                    collapse_roots.setdefault(shared_root, set()).update({a, b})
        # A page can never be its own shared ancestor — a back-citing source
        # placing `key` in its own closure is a cycle, reported separately.
        collapse_roots.pop(key, None)
        if not collapse_roots:
            continue
        # One converging tail (C <- D <- E, both A and B reaching all three)
        # is one situation, not one finding per node in the tail.
        collapse_roots = _nearest_shared_roots(collapse_roots, walk)

        for root in sorted(collapse_roots):
            via = sorted(collapse_roots[root])
            findings.append(
                AuditFinding(
                    category="derivation_double_counting",
                    severity="info",
                    path=page.rel_path,
                    detail=(
                        f"{len(via)} of this page's cited sources trace back to a "
                        f"shared ancestor ({display_path(root)}); citing them as "
                        "independent support double-counts that ancestor."
                    ),
                    proposed_fix=(
                        "Review whether these citations are genuinely independent "
                        "evidence or restate the same underlying source; if not, "
                        "cite the shared ancestor once. Nothing is auto-written."
                    ),
                    meta={
                        "kind": "support_collapse",
                        "shared_ancestor": display_path(root),
                        "via_sources": [display_path(v) for v in via],
                        "signal_version": _page_signal_version(page),
                        "truncated_reasons": sorted(page_reasons),
                    },
                )
            )

    if truncated_reasons_seen:
        reasons_label = " and ".join(sorted(truncated_reasons_seen))
        findings.append(
            AuditFinding(
                category="derivation_double_counting",
                severity="info",
                path=kb_prefix(),
                detail=(
                    f"derivation-chain traversal capped by {reasons_label} "
                    f"(depth<={max_depth}, edges<={max_edges}); some ancestor "
                    "chains may extend further than reported here."
                ),
                proposed_fix=(
                    "Re-run scoped to the densest chains, or raise "
                    "EXOMEM_DERIVATION_MAX_DEPTH / EXOMEM_DERIVATION_MAX_EDGES."
                ),
                meta={
                    "kind": "truncated",
                    "max_depth": max_depth,
                    "max_edges": max_edges,
                    "reasons": sorted(truncated_reasons_seen),
                },
            )
        )

    return sorted(
        findings, key=lambda finding: (finding.path, str((finding.meta or {}).get("kind") or ""))
    )


# ---------------- check: unfinished_experiments ----------------

# Out of rotation by the author's own declaration — archived or superseded is
# deliberately parked, a draft never started, a dropped one was abandoned on
# purpose, and a planned one has not begun. None of them owes a result, and a
# note the author explicitly dropped generating daily review work would be the
# system arguing with a decision already made.
#
# The set matches what `_check_relation_debt`, `activation.py`, and
# `semantic_contract.py` already treat as inactive; it previously claimed to
# mirror that discipline while omitting `dropped` and `planned`.
_EXPERIMENT_PARKED_STATUSES = frozenset({"archived", "superseded", "draft", "dropped", "planned"})

# `duration:` is free text by contract ("30 days", "2 weeks", "ongoing"), so the
# span parser is deliberately small and fails CLOSED: anything it does not
# recognise means "no finite window", which means "never flagged". A parser bug
# therefore produces silence, never a false accusation.
_DURATION_UNIT_DAYS: dict[str, int] = {
    "day": 1,
    "week": 7,
    "month": 30,
    "year": 365,
}
_DURATION_PATTERN = re.compile(r"^(\d+(?:\.\d+)?)\s*(day|week|month|year)s?$", re.IGNORECASE)


def _experiment_duration_days(value: Any) -> int | None:
    """Whole days for an experiment `duration:`, or None when it declares none.

    None means "no finite window", which no elapsed time can exceed. That is the
    honest reading of `ongoing`: an experiment that declares no deadline has not
    missed one. Flagging an unparseable duration would turn a field-shape
    question into an epistemic one — `frontmatter_compliance` owns field shape.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        days = int(value)
        return days if days >= 0 else None
    text = str(value).strip().strip('"').strip("'")
    if not text:
        return None
    if text.isdigit():
        return int(text)
    match = _DURATION_PATTERN.match(text)
    if match is None:
        return None
    count = float(match.group(1))
    return int(round(count * _DURATION_UNIT_DAYS[match.group(2).lower()]))


def _check_unfinished_experiments(
    vault_root: Path,
    pages: list[find_module.ParsedPage],
    *,
    today: dt.date | None = None,
) -> list[AuditFinding]:
    """Surface experiments whose declared window closed with no result recorded.

    The `experiment` page type has been fully authorable for a long time —
    `started`, `duration`, `concluded`, and a categorical `outcome:` — but until
    now nothing asked whether one ever finished, even though the shipped skill
    scaffold has advertised exactly this check to users since it shipped.

    The trigger is a missing `outcome:`, NOT `status: active`. `status` records
    that the experiment stopped; `outcome` records what it showed. An experiment
    marked `concluded` with no outcome is the purest instance of the thing this
    check exists to catch — the loop was closed administratively without anyone
    writing down the result — so keying on `status` would miss its best case.

    Measurement-only at `info`: nothing is concluded, archived, or inferred, and
    `find` ordering is untouched. Ordered oldest-first, because the context
    needed to write a result up decays with time.
    """
    today = today or dt.date.today()
    rows: list[tuple[int, str, AuditFinding]] = []
    for page in pages:
        if page.page_type != "experiment":
            continue
        if page.path.name in ("index.md", "log.md"):
            continue
        if (page.status or "") in _EXPERIMENT_PARKED_STATUSES:
            continue
        if access.access_tier(vault_root, page.rel_path) != access.TIER_READ_WRITE:
            continue

        started = _parse_fm_date(page.frontmatter.get("started"))
        if started is None:
            continue  # no start date → no window to have closed; don't invent one
        duration_days = _experiment_duration_days(page.frontmatter.get("duration"))
        if duration_days is None:
            continue  # open-ended by declaration — never overdue
        elapsed_days = (today - started).days
        if elapsed_days <= duration_days:
            continue  # still inside the window (the edge itself is inside it)
        if str(page.frontmatter.get("outcome") or "").strip():
            continue  # the result is recorded — the loop is closed

        overdue_days = elapsed_days - duration_days
        rows.append(
            (
            elapsed_days,
            page.rel_path,
            AuditFinding(
                category="unfinished_experiments",
                severity="info",
                path=page.rel_path,
                detail=(
                    f"Experiment window closed with no result recorded — started "
                    f"{started.isoformat()}, declared {duration_days}d, "
                    f"{elapsed_days}d elapsed ({overdue_days}d past the window) "
                    f"and no `outcome:`."
                ),
                proposed_fix=(
                    "Surfaced for REVIEW only — nothing is auto-concluded, "
                    "archived, or inferred. Write the result up (`status: "
                    "concluded` plus a categorical `outcome:`), extend "
                    "`duration:` if it is genuinely still running, or archive it."
                ),
                meta={
                    "signal_version": _page_signal_version(page),
                    "started": started.isoformat(),
                    "duration_days": duration_days,
                    "elapsed_days": elapsed_days,
                    "overdue_days": overdue_days,
                    "status": page.status,
                },
            ),
            )
        )

    # Oldest-first: elapsed age DESCENDING, path ascending as the deterministic
    # tiebreak. Age rather than overdue-ness, because a 30d experiment 300d late
    # has lost more of the context its write-up needs than a 300d one 30d late.
    rows.sort(key=lambda row: (-row[0], row[1]))
    return [finding for _, _, finding in rows]


# ---------------- check: prediction_window ----------------

# Authoring one of these on the unit is the signal that SOMEBODY ENGAGED with the
# prediction. Deliberately not a judgment of which way it went — concluding is
# `verdict`'s job; this queue only measures whether anyone looked.
_PREDICTION_RESOLVING_RELATIONS = frozenset({"supports", "contradicts", "resolves", "evidenced_by"})
# The prefilter that decides whether a page is worth parsing. It MUST NOT be
# narrower than `semantic_blocks.normalize_label`, which lowercases a metadata
# key and collapses `[\s-]+` to `_` — so `- Check By:`, `- check by:` and
# `- check-by:` all author a genuine governed `check_by` that `find` and the
# structured filters already see. A raw case-sensitive `"check_by" in body` test
# silently dropped all three before parsing, which for this queue is the worst
# failure available: the whole justification is that an unsurfaced obligation is
# one nobody meets, and a miss here is indistinguishable from "nothing is due".
_CHECK_BY_PREFILTER = re.compile(r"check[\s_-]*by", re.IGNORECASE)
# A unit inherits its page's standing (see the epistemic loop primitives), so a
# due prediction on a parked page is not outstanding work. Same inactive set the
# rest of the codebase uses — `dropped` and `planned` included, because a
# prediction on a note the author dropped is not an obligation anyone still owes.
_PREDICTION_PARKED_STATUSES = frozenset({"superseded", "archived", "draft", "dropped", "planned"})


def _check_prediction_window(
    vault_root: Path,
    pages: list[find_module.ParsedPage],
    *,
    today: dt.date | None = None,
) -> list[AuditFinding]:
    """Surface authored `check_by` dates that came due with nothing recorded.

    `check_by` exists to answer exactly one question — what is due? — and until
    now nothing asked it. The retrieval half already landed (`unit.check_by` is a
    typed date in the structured-filter registry, so a due-by query is answerable
    on request); this is the missing PUSH, so an outstanding obligation surfaces
    without the author having to remember to go looking for it.

    "Nothing recorded" is deliberately UNIT-LOCAL: no `verdict` on the unit, and
    no outbound relation authored ON THE UNIT ITSELF resolving to one of
    `_PREDICTION_RESOLVING_RELATIONS`. A relation elsewhere on the parent page
    does not clear it, and neither does an inbound edge from another note.

    That is forced, not lazy. `epistemic_graph` still strips the `#fragment` off a
    relation target, so an edge authored against `[[Note#unit-abc]]` lands on
    `Note`, not on unit `abc`. Building the predicate on inbound edges today would
    mean either page-level granularity — where ANY inbound `contradicts` silently
    clears EVERY due prediction on that page — or a second, private, fragment-aware
    traversal that would immediately disagree with the graph everyone else queries.
    Both are worse than the bounded false positive this leaves: a prediction
    resolved only from elsewhere keeps surfacing until someone records the verdict
    where the loop's own documentation says it belongs. Widening this once
    fragment targets resolve can only ever REMOVE findings, never add them.

    The trigger is the authored `check_by`, not `kind == "prediction"`: the parser
    reserves `check_by` for every governed kind, so a `## Hypothesis` carrying one
    has authored a real due date, and ignoring it would relocate this very bug one
    field over.

    Measurement-only at `info`: never judges whether the prediction held, never
    writes a verdict, never touches `find` ordering.
    """
    today = today or dt.date.today()
    relations = relation_registry.load_registry(vault_root)
    language = semantic_language_registry.load_registry(vault_root)

    rows: list[tuple[int, str, str, AuditFinding]] = []
    for page in pages:
        if page.path.name in ("index.md", "log.md"):
            continue
        if (page.status or "") in _PREDICTION_PARKED_STATUSES:
            continue
        if access.access_tier(vault_root, page.rel_path) != access.TIER_READ_WRITE:
            continue
        # Cheap prefilter: a page with no authored check date cannot match, so a
        # vault that has never used the primitive pays one regex scan and no
        # parse at all. Deliberately looser than the exact key — see
        # `_CHECK_BY_PREFILTER`; over-matching costs a parse, under-matching
        # loses a real obligation.
        if not _CHECK_BY_PREFILTER.search(page.body):
            continue

        document = semantic_units.parse_semantic_units(
            page.body,
            path=page.rel_path,
            validate=False,
            language_registry=language,
            relation_registry=relations,
            page_type=page.page_type,
        )
        for unit in document.rich_units:
            if not unit.check_by:
                continue
            due = _parse_fm_date(unit.check_by)
            if due is None or due > today:
                continue
            if unit.verdict:
                continue
            if any(
                relations.resolve(relation.kind, origin="semantic_relation").canonical
                in _PREDICTION_RESOLVING_RELATIONS
                for relation in unit.relations
            ):
                continue

            overdue_days = (today - due).days
            fingerprint = unit.fingerprint or ""
            label = unit.title or unit.anchor or unit.kind
            rows.append(
                (
                overdue_days,
                page.rel_path,
                fingerprint,
                AuditFinding(
                    category="prediction_window",
                    severity="info",
                    path=page.rel_path,
                    detail=(
                        f"Check window closed on {due.isoformat()} "
                        f"({overdue_days}d ago) for {label!r} with no verdict and "
                        f"no resolving relation recorded on the unit."
                    ),
                    proposed_fix=(
                        "Surfaced for REVIEW only — nothing is judged, written, or "
                        "expired. Record a categorical `verdict:` on the unit, "
                        "attach the evidence that settles it with a "
                        "`supports`/`contradicts`/`resolves`/`evidenced_by` "
                        "relation, or move `check_by` out if the question is still "
                        "genuinely open."
                    ),
                    meta={
                        # The UNIT's fingerprint, not the page's: an edit to this
                        # prediction must resurface it rather than inherit a
                        # dismissal recorded against different words.
                        "signal_version": fingerprint,
                        # Same value as the review partition, so a page holding
                        # several due predictions composes several INDEPENDENT
                        # review items instead of one whose dismissal would
                        # silently dispose of predictions nobody read.
                        "review_partition": fingerprint,
                        "unit_ref": unit.unit_ref,
                        "anchor": unit.anchor,
                        "kind": unit.kind,
                        "check_by": due.isoformat(),
                        "overdue_days": overdue_days,
                    },
                ),
                )
            )

    # Most-overdue-first. Unlike an experiment — where the decaying write-up
    # context makes raw age the right signal — `check_by` is an explicitly
    # authored commitment, so distance past the date the author chose IS the
    # urgency. Path then fingerprint keep it deterministic.
    rows.sort(key=lambda row: (-row[0], row[1], row[2]))
    return [finding for _, _, _, finding in rows]


# ---------------- check: unreflected_outcomes ----------------

#: A Planning item is OPEN while the vault still intends it: lifecycle `active`
#: and a status that has not settled. Both are authored values, read as authored;
#: an absent `lifecycle` reads as `active` because that is Planning's own capture
#: default for every kind (`planning.py:77,83`), not an inference made here.
#: Nothing reads a state out of dates, out of the events, or out of how long
#: anything took.
_SETTLED_PLAN_STATUSES = frozenset({"completed", "cancelled"})

#: How many joined record references one finding carries. The TOTAL is always
#: exact; the list is a sample, for the same reason the wire block caps `top`.
_UNREFLECTED_REF_LIMIT = 8


@dataclass(frozen=True)
class _OutcomeBinding:
    """One authored Records->Planning binding, both ends already resolved."""

    records: Any
    planning: Any
    join: dict[str, str]


def _release_filter(vault_root: Path) -> Any:
    """The Records release gate, answered once per path per pass.

    The plane does not move while one pass runs, and the same paths are asked
    about by discovery, by each adapter read and by serving. Without the memo the
    delta pays the full release walk three times for every file it touches.
    """
    from . import record_governance

    allowed = record_governance.full_release_filter(vault_root)
    cache: dict[str, bool] = {}

    def memoized(relative: str) -> bool:
        hit = cache.get(relative)
        if hit is None:
            hit = cache[relative] = allowed(relative)
        return hit

    return memoized


def _outcome_bindings(
    vault_root: Path, *, authorize: Any = None
) -> tuple[list[_OutcomeBinding], list[dict[str, Any]]]:
    """Every join-bearing binding, plus the ones that could not be evaluated.

    An unresolvable Planning reference is NOT a quiet skip: it is a binding the
    author declared and this pass could not check, so it comes back as its own
    state and the caller reports it. Treating it as "no findings" would hand back
    a clean bill for something nobody looked at.
    """
    from . import structured_collections as collections_module

    authorize = authorize or _release_filter(vault_root)
    try:
        manifests = list(
            collections_module.discover_collections(vault_root, authorize_path=authorize)
        )
    except collections_module.CollectionError:
        return [], []
    planning_by_id = {
        manifest.collection_id: manifest
        for manifest in manifests
        if manifest.semantic_profile == "planning"
    }
    planning_by_path = {
        manifest.path: manifest for manifest in manifests if manifest.semantic_profile == "planning"
    }
    bindings: list[_OutcomeBinding] = []
    unevaluated: list[dict[str, Any]] = []
    for manifest in manifests:
        if manifest.semantic_profile != "records":
            continue
        for link in manifest.links.plans:
            if not link.join:
                continue
            target = _resolve_planning_target(
                vault_root, link.reference, planning_by_id, planning_by_path
            )
            if target is None:
                unevaluated.append(
                    {
                        "collection": manifest.path,
                        "reference": str(link.reference),
                        "reason": "unresolved_planning_reference",
                    }
                )
                continue
            # A plan-side name the target does not declare cannot be read, so
            # the join can never match and the family would report a clean bill
            # for a binding nobody could evaluate. The plan side is deliberately
            # unchecked at AUTHORING time (the target may not exist yet, and
            # Records must not resolve Planning), which makes THIS the first
            # place the two ends are ever held together -- so it is where the
            # gap has to be named. Same state as an unresolvable reference: not
            # a finding, and never silence.
            undeclared = sorted(
                {
                    str(plan_field)
                    for plan_field in link.join.values()
                    if str(plan_field) not in target.schema.fields
                }
            )
            if undeclared:
                unevaluated.append(
                    {
                        "collection": manifest.path,
                        "reference": str(link.reference),
                        "reason": "undeclared_plan_field",
                        "fields": undeclared,
                    }
                )
                continue
            bindings.append(_OutcomeBinding(manifest, target, dict(link.join)))
    return bindings, unevaluated


def outcome_binding_index(vault_root: Path) -> dict[str, list[dict[str, Any]]]:
    """Every resolved binding, keyed BOTH ways, for the write path to consult.

    A full pass already walked the tree and resolved both ends, so it publishes
    the answer instead of making every subsequent write re-derive it. Keyed by
    the Records manifest path and by the Planning manifest path, because the two
    write sides ask opposite questions: "what did I just write into, and what
    plans does it feed?" and "who feeds this plan?".

    Persisted in the projection, so it is maintained exactly like the projection
    is: rebuilt by `reconcile`, extended by a delta that resolves a binding the
    index did not have yet, and never authoritative on its own.
    """
    index: dict[str, list[dict[str, Any]]] = {}
    for binding in _outcome_bindings(vault_root)[0]:
        row = {
            "records": binding.records.path,
            "planning": binding.planning.path,
            "join": dict(binding.join),
        }
        for key in (binding.records.path, binding.planning.path):
            rows = index.setdefault(key, [])
            if row not in rows:
                rows.append(row)
    return index


def declared_bindings(vault_root: Path, manifest: Any) -> list[dict[str, Any]]:
    """The bindings THIS Records manifest declares, resolved without a vault walk.

    The write path may not discover: `discover_collections` walks the governed
    tree and re-decides the release plane for every manifest it finds, which is
    9-21 ms an UNBOUND append was paying to learn it had nothing to do. A manifest
    already states whether it is bound -- `links.plans[].join` is right there in
    the bytes the writer just loaded -- so a collection nobody bound costs one
    attribute read.

    Resolution of the far end goes through the reference index, never the
    fallback scan, for the same reason. An id the index cannot answer is skipped
    rather than scanned for: the projection is maintained, and `reconcile` heals
    what a delta could not resolve.
    """
    from . import memory_refs
    from . import structured_collections as collections_module

    rows: list[dict[str, Any]] = []
    for link in getattr(getattr(manifest, "links", None), "plans", ()) or ():
        if not link.join:
            continue
        memory_id = memory_refs.parse_memory_ref(str(link.reference))
        target: str | None = None
        if memory_id:
            try:
                paths = memory_refs.ReferenceIndex(vault_root)._paths_for_id(memory_id)
            except Exception:  # noqa: BLE001 -- an unresolvable binding is skipped, never raised
                paths = []
            if len(paths) == 1:
                target = str(paths[0])
        elif str(link.reference).lower().startswith(("exomem://vault/", "exomem://source/")):
            try:
                target = str(
                    memory_refs.resolve_identifier_read_only(vault_root, str(link.reference))
                )
            except Exception:  # noqa: BLE001
                target = None
        if not target:
            continue
        normalized = target.replace("\\", "/").lstrip("/")
        try:
            planning = collections_module.load_manifest(vault_root, vault_root / normalized)
        except Exception:  # noqa: BLE001
            continue
        if planning.semantic_profile != "planning":
            continue
        if any(str(name) not in planning.schema.fields for name in link.join.values()):
            continue
        rows.append({"planning": normalized, "join": dict(link.join)})
    return rows


def open_plan_item(values: Any) -> bool:
    """Public alias: a Planning item the vault still intends (design D6)."""
    return _open_plan_item(values)


def join_key(names: list[str], values: Any) -> tuple[str, ...] | None:
    """Public alias: the exact join token tuple, or None when a side is empty."""
    return _join_key(names, values)


def _resolve_planning_target(
    vault_root: Path,
    reference: str,
    planning_by_id: dict[str, Any],
    planning_by_path: dict[str, Any],
) -> Any | None:
    """Resolve one opaque reference to a Planning manifest, or None.

    A collection's id IS its manifest page's `exomem_id`, so the common
    `exomem://memory/<id>` form is answered from the manifests already in hand.
    Anything else goes through the read-only resolver, which never creates or
    refreshes the sidecar for an audit pass.
    """
    from . import memory_refs

    memory_id = memory_refs.parse_memory_ref(str(reference))
    if memory_id and memory_id in planning_by_id:
        return planning_by_id[memory_id]
    try:
        resolved = memory_refs.resolve_identifier_read_only(vault_root, str(reference))
    except Exception:  # noqa: BLE001 -- an unresolvable reference is reported, not raised
        return None
    return planning_by_path.get(str(resolved).replace("\\", "/").lstrip("/"))


def _join_token(value: Any) -> str | None:
    """One comparable token, or None when this side carries no value at all.

    Exact, never fuzzy: identity is the manifest author's declaration, and a
    near-match here would silently invent a binding nobody wrote down.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _join_key(names: list[str], values: Any) -> tuple[str, ...] | None:
    tokens: list[str] = []
    for name in names:
        token = _join_token(values.get(name))
        if token is None:
            return None
        tokens.append(token)
    return tuple(tokens)


def _open_plan_item(values: Any) -> bool:
    lifecycle = str(values.get("lifecycle") or "active").strip().lower()
    status = str(values.get("status") or "").strip().lower()
    return lifecycle == "active" and status not in _SETTLED_PLAN_STATUSES


def _outcome_snapshot(vault_root: Path, manifest: Any, authorize: Any) -> Any | None:
    """One release-filtered adapter snapshot, or None when it cannot be read.

    The filter is the same one every other Planning read uses, so a withheld
    record or item is absent here exactly as it is absent from the served view.
    """
    from . import record_formats
    from . import structured_collections as collections_module

    try:
        return record_formats.load_adapter(vault_root, manifest, authorize_path=authorize).read()
    except (collections_module.CollectionError, OSError, ValueError):
        return None


def _unreflected_for_binding(
    vault_root: Path, binding: _OutcomeBinding, authorize: Any
) -> tuple[list[AuditFinding], set[str]]:
    """Findings for one bound pair, plus every plan-item path the pair covers.

    The second value is what a write-time delta must REPLACE: an item that no
    longer has a finding has to lose its stored entry, and only a pass that knows
    the full candidate set can tell "cleared" from "not looked at".
    """
    plan_snapshot = _outcome_snapshot(vault_root, binding.planning, authorize)
    record_snapshot = _outcome_snapshot(vault_root, binding.records, authorize)
    if plan_snapshot is None or record_snapshot is None:
        return [], set()
    record_fields = list(binding.join)
    plan_fields = [binding.join[name] for name in record_fields]
    grouped: dict[tuple[str, ...], list[Any]] = {}
    for record in record_snapshot.records:
        key = _join_key(record_fields, record.values)
        if key is not None:
            grouped.setdefault(key, []).append(record)
    findings: list[AuditFinding] = []
    covered: set[str] = set()
    for item in plan_snapshot.records:
        covered.add(item.source.path)
        if not _open_plan_item(item.values):
            continue
        key = _join_key(plan_fields, item.values)
        if key is None:
            continue
        matched = grouped.get(key)
        if matched:
            finding = _unreflected_finding(binding, item, matched)
            if finding is not None:
                findings.append(finding)
    return findings, covered


def outcome_component(
    records: Any, planning: Any, join: Mapping[str, str], item: Any
) -> dict[str, Any]:
    """The primitives one finding needs, minus the joined records themselves.

    Serialisable on purpose. The due-state projection stores it beside the joined
    `(path, key)` pairs so that a SERVE under a narrower audience can rebuild the
    finding from the records that audience may see -- through
    `unreflected_component` below, the same composer this pass uses -- instead of
    inventing a second opinion about what a partially-withheld finding means. The
    write-time delta stores it for the same reason: it can then refresh a
    renamed or re-statused item without re-reading the Records collection.
    """
    return {
        "records_title": str(records.title or ""),
        "records_collection": str(records.path),
        "records_collection_id": str(records.collection_id),
        "planning_collection": str(planning.path),
        "planning_collection_id": str(planning.collection_id),
        "join": dict(join),
        "item_path": str(item.source.path),
        "item_key": str(item.identity.key),
        "item_title": str(item.values.get("title") or item.identity.key),
        "item_status": str(item.values.get("status") or "open"),
    }


def unreflected_component(
    component: Mapping[str, Any], joined: Iterable[tuple[str, str]]
) -> AuditFinding | None:
    """One finding, composed from a component and the joined records in scope.

    THE composer for this family. `joined` is `(record_path, record_key)` pairs;
    an empty set is not a finding, because "an item with recorded events" is the
    whole claim. Every derived value -- the count in `detail`, the sample refs,
    the total, and the `signal_version` the fingerprint is built from -- is
    derived from the pairs handed in, so a caller that filtered them produces
    exactly what a fresh pass under the same visibility would.
    """
    from . import structured_collections as collections_module

    pairs = sorted({(str(path), str(key)) for path, key in joined})
    if not pairs:
        return None
    keys = sorted(key for _path, key in pairs)
    paths = sorted({path for path, _key in pairs})
    join = dict(component.get("join") or {})
    title = str(component.get("item_title") or "")
    status = str(component.get("item_status") or "open")
    records_title = str(component.get("records_title") or "")
    binding_text = ", ".join(f"{name}={join[name]}" for name in sorted(join))
    return AuditFinding(
        category="unreflected_outcomes",
        severity="info",
        path=str(component.get("item_path") or ""),
        detail=(
            f"{len(pairs)} recorded event(s) in {records_title!r} join to "
            f"the open item {title!r} (status {status}) on {binding_text}, and the "
            f"item has not been moved."
        ),
        proposed_fix=(
            "Surfaced for REVIEW only -- nothing is judged, written, or "
            "transitioned. Read the joined events and, if they settle the item, "
            'move it yourself with plan_memory(action="triage"); remove the '
            "manifest binding if these events are not about this work."
        ),
        paths=paths[:_UNREFLECTED_REF_LIMIT],
        meta={
            # The joined record identities, so a new event on the same item
            # changes the fingerprint and a dismissal binds to what was read.
            "signal_version": hashlib.sha256(
                json.dumps(
                    [str(component.get("item_key") or ""), keys], separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest()[:16],
            # One review item per plan item, even where a Markdown-log collection
            # keeps every item in one file.
            "review_partition": str(component.get("item_key") or ""),
            # Never resolved by time: the sentinel every dateless due-state
            # category publishes, so serving sorts it after real overdue work.
            "due_since": dt.date.min.isoformat(),
            "plan_item": collections_module.plan_ref(
                str(component.get("planning_collection_id") or ""),
                str(component.get("item_key") or ""),
            ),
            "plan_title": title,
            "plan_collection": str(component.get("planning_collection") or ""),
            "records_collection": str(component.get("records_collection") or ""),
            "joined_records": [
                collections_module.record_ref(
                    str(component.get("records_collection_id") or ""), key
                )
                for key in keys[:_UNREFLECTED_REF_LIMIT]
            ],
            "joined_total": len(keys),
            "binding": join,
        },
        component={**dict(component), "joined": [list(pair) for pair in pairs]},
    )


def _unreflected_finding(
    binding: _OutcomeBinding, item: Any, matched: list[Any]
) -> AuditFinding | None:
    return unreflected_component(
        outcome_component(binding.records, binding.planning, binding.join, item),
        ((record.source.path, record.identity.key) for record in matched),
    )


def _check_unreflected_outcomes(
    vault_root: Path,
) -> tuple[list[AuditFinding], dict[str, Any]]:
    """Open plan items that recorded events already joined to (design D6).

    Both halves of the fact are already in the vault: an event says a thing
    happened, an item says the vault still intends it. Nobody was comparing them,
    so an agent had to remember to, which is exactly the class of prompt this
    change removes. Measurement only -- the runtime never moves either side.
    """
    authorize = _release_filter(vault_root)
    bindings, unevaluated = _outcome_bindings(vault_root, authorize=authorize)
    findings: list[AuditFinding] = []
    for binding in bindings:
        pair_findings, _ = _unreflected_for_binding(vault_root, binding, authorize)
        findings.extend(pair_findings)
    findings.sort(key=lambda finding: (finding.path, str((finding.meta or {}).get("plan_item"))))
    return findings, ({"unevaluated": unevaluated} if unevaluated else {})


# ---------------- check: question_aging ----------------

#: How old a page's authored date has to be before an unanswered question on it
#: is worth raising. PROVISIONAL: unlike `check_by` (a date the author chose) and
#: `duration` (a window the author declared), nothing here was authored — the
#: number is the system's invention, and it is the whole reason this category is
#: registered but kept OUT of the default attention union. Tune it from observed
#: behaviour, not from taste, and remember that raising it only ever hides work.
QUESTION_AGING_DAYS = 30

#: The canonical registry-resolved category a governed question unit carries.
#: `## Open Question`, `## Open Questions` and an explicit `- category: question`
#: all normalise here (`semantic_language_registry` core category `question`),
#: so matching the resolved value rather than the authored heading is what makes
#: the check alias-proof.
_QUESTION_CATEGORY = "question"

#: Authoring one of these on the unit is the signal that SOMEBODY ANSWERED the
#: question. Deliberately the same set `_check_prediction_window` clears on, so
#: one resolution rule governs every unit-scoped due-state category instead of
#: two that drift apart. A question and a prediction are the same shape of open
#: loop; only the threshold that opens them differs.
_QUESTION_ANSWERING_RELATIONS = _PREDICTION_RESOLVING_RELATIONS

#: Cheap prefilter, deliberately looser than the exact heading — a page that has
#: never authored a question cannot match, so a vault that does not use the
#: primitive pays one regex scan and no parse. Same trade `_CHECK_BY_PREFILTER`
#: makes: over-matching costs a parse, under-matching loses a real obligation.
_QUESTION_PREFILTER = re.compile(r"question", re.IGNORECASE)

#: A unit inherits its page's standing, so a question on a parked page is not
#: outstanding work. Same inactive set the sibling lifecycle queues use.
_QUESTION_PARKED_STATUSES = _PREDICTION_PARKED_STATUSES


def _check_question_aging(
    vault_root: Path,
    pages: list[find_module.ParsedPage],
    *,
    today: dt.date | None = None,
) -> list[AuditFinding]:
    """Surface governed question units that have sat unanswered for a while.

    This is the softest of the four due-state consumers and it is deliberately
    labelled as such in its own finding text. `prediction_window` fires on a date
    the author wrote down and `unfinished_experiments` on a window the author
    declared; nobody ever declared that a question goes stale after
    `QUESTION_AGING_DAYS`. The threshold is the system's, so the finding reports a
    review CANDIDATE and never a defect, and the category stays out of the default
    attention union — the same backlog-profile discipline that keeps
    `unfinished_experiments` opt-in, applied to an invented threshold rather than
    to a grandfathered field.

    "Unanswered" is UNIT-LOCAL for the same forced reason the prediction predicate
    is: relation targets still lose their `#fragment`, so an inbound edge cannot
    address a unit, and a page-level test would let any answering relation
    anywhere on the page silently close every question on it. Only a `verdict` or
    an answering relation authored ON THE UNIT clears it.

    The age is the PAGE's authored date, because a unit has no date of its own.
    That is honest rather than approximate: a question written into a page is as
    old as the page said it was, and a page with no parseable date is skipped
    rather than assigned an invented one.
    """
    today = today or dt.date.today()
    relations = relation_registry.load_registry(vault_root)
    language = semantic_language_registry.load_registry(vault_root)

    rows: list[tuple[int, str, str, AuditFinding]] = []
    for page in pages:
        if page.path.name in ("index.md", "log.md"):
            continue
        if (page.status or "") in _QUESTION_PARKED_STATUSES:
            continue
        if access.access_tier(vault_root, page.rel_path) != access.TIER_READ_WRITE:
            continue
        if not _QUESTION_PREFILTER.search(page.body):
            continue

        authored = _parse_fm_date(
            page.frontmatter.get("created") or page.frontmatter.get("updated")
        )
        if authored is None:
            continue  # no authored date → no age to measure; never invent one
        age_days = (today - authored).days
        if age_days < QUESTION_AGING_DAYS:
            continue

        document = semantic_units.parse_semantic_units(
            page.body,
            path=page.rel_path,
            validate=False,
            language_registry=language,
            relation_registry=relations,
            page_type=page.page_type,
        )
        for unit in document.rich_units:
            if unit.category != _QUESTION_CATEGORY:
                continue
            if unit.verdict:
                continue
            if any(
                relations.resolve(relation.kind, origin="semantic_relation").canonical
                in _QUESTION_ANSWERING_RELATIONS
                for relation in unit.relations
            ):
                continue

            fingerprint = unit.fingerprint or ""
            label = unit.title or unit.anchor or unit.kind
            rows.append(
                (
                age_days,
                page.rel_path,
                fingerprint,
                AuditFinding(
                    category="question_aging",
                    severity="info",
                    path=page.rel_path,
                    detail=(
                        f"Open question {label!r} has stood unanswered for "
                        f"{age_days}d (page authored {authored.isoformat()}) with no "
                        f"verdict and no answering relation on the unit. A review "
                        f"candidate, never a defect — nothing here is overdue by "
                        f"anyone's declaration."
                    ),
                    proposed_fix=(
                        "Surfaced for REVIEW only, and on a threshold this system "
                        "chose rather than one you declared. Answer it by recording "
                        "a categorical `verdict:` on the unit or attaching what "
                        "settles it with a `supports`/`contradicts`/`resolves`/"
                        "`evidenced_by` relation, or leave it open — an open "
                        "question is a legitimate long-lived state."
                    ),
                    meta={
                        # The UNIT's fingerprint, as `prediction_window` uses:
                        # editing this question must resurface it rather than
                        # inherit a decision recorded against different words.
                        "signal_version": fingerprint,
                        "review_partition": fingerprint,
                        "unit_ref": unit.unit_ref,
                        "anchor": unit.anchor,
                        "kind": unit.kind,
                        "authored": authored.isoformat(),
                        "age_days": age_days,
                        "threshold_days": QUESTION_AGING_DAYS,
                        # The day this became a candidate. The due-state
                        # projection buckets on this rather than re-deriving it,
                        # so a day boundary is a date comparison and not a rescan.
                        "due_since": (
                            authored + dt.timedelta(days=QUESTION_AGING_DAYS)
                        ).isoformat(),
                    },
                ),
                )
            )

    # Oldest-first: the context a question needs to be answered decays with time,
    # so raw age is the right signal here (as with experiments) rather than
    # distance past a chosen date (as with predictions).
    rows.sort(key=lambda row: (-row[0], row[1], row[2]))
    return [finding for _, _, _, finding in rows]


# ---------------- check: supersession_integrity ----------------

#: The two authored supersession pointers. `superseded_by` runs old -> new,
#: `supersedes` runs new -> old; the protocol requires both halves, which is
#: exactly why a half-written supersession is detectable at all.
_SUPERSESSION_POINTERS = ("superseded_by", "supersedes")


def _supersession_link_target(raw: str) -> str:
    """The path a `supersedes`/`superseded_by` wikilink names, alias and anchor stripped."""
    text = str(raw or "").strip()
    if text.startswith("[[") and text.endswith("]]"):
        text = text[2:-2]
    return text.split("|", 1)[0].split("#", 1)[0].strip()


def _check_supersession_integrity(
    vault_root: Path,
    pages: list[find_module.ParsedPage],
) -> list[AuditFinding]:
    """Surface supersession pointers that do not describe a real, single-headed chain.

    Two defects, both in state a human authored, and neither inferred:

    1. A DANGLING POINTER — `supersedes` or `superseded_by` naming a page that does
       not resolve. The supersession protocol's whole promise is that a reader who
       lands on the old conclusion is led to the current one; a pointer into
       nothing is that promise broken silently, and the reader has no way to tell
       the difference between "not superseded" and "superseded, link rotted".

    2. A MULTI-HEADED CHAIN — a connected supersession component with more than one
       member that nothing supersedes. `replace` maintains a linear spine on
       purpose, so two live heads means the chain no longer has one current answer
       and every consumer of it (evolution timelines, `prefer_active` ranking, a
       human reading backwards) silently picks one.

    Unlike the three time-driven consumers this ships beside, nothing here is
    thresholded: the pointer either resolves or it does not, and the component
    either has one head or it does not. That is why these are `warn` DEFECTS while
    the others are `info` candidates, and why this category joins the default
    attention union while `question_aging` does not.

    Parked statuses are deliberately NOT excluded. A `superseded` page is precisely
    where a dangling forward pointer lives, so filtering the way the measurement
    queues do would blind the check to its main case.

    Measurement-only: nothing is repaired, relinked, or re-pointed.
    """
    known: set[str] = set()
    by_stem: dict[str, list[str]] = {}
    for page in pages:
        rel = page.rel_path
        known.add(rel)
        known.add(rel[:-3] if rel.endswith(".md") else rel)
        by_stem.setdefault(page.path.stem, []).append(rel)

    def _resolve(raw: str) -> str | None:
        target = _supersession_link_target(raw)
        if not target:
            return None
        for candidate in (target, f"{target}.md"):
            if candidate in known:
                return candidate if candidate.endswith(".md") else f"{candidate}.md"
        # A bare name is a legitimate Obsidian spelling; resolve it only when it
        # is UNAMBIGUOUS, because guessing between two stems would invent a chain.
        if "/" not in target:
            matches = by_stem.get(target) or by_stem.get(
                target[:-3] if target.endswith(".md") else target
            )
            if matches and len(matches) == 1:
                return matches[0]
        # Last resort: the page may live outside the walked set (a curated tree),
        # so believe the filesystem before calling a pointer broken.
        for candidate in (target, f"{target}.md"):
            if (vault_root / candidate).exists():
                return candidate if candidate.endswith(".md") else f"{candidate}.md"
        return None

    findings: list[AuditFinding] = []
    in_scope: list[find_module.ParsedPage] = []
    resolved_edges: list[tuple[str, str]] = []  # (older, newer)

    for page in pages:
        if page.path.name in ("index.md", "log.md"):
            continue
        if access.access_tier(vault_root, page.rel_path) != access.TIER_READ_WRITE:
            continue
        in_scope.append(page)
        for pointer in _SUPERSESSION_POINTERS:
            for raw in getattr(page, pointer):
                target = _resolve(raw)
                if target is None:
                    partition = content_hash(
                        f"dangling\0{pointer}\0{_supersession_link_target(raw)}"
                    )[:16]
                    findings.append(
                        AuditFinding(
                            category="supersession_integrity",
                            severity="warn",
                            path=page.rel_path,
                            detail=(
                                f"`{pointer}:` names "
                                f"{_supersession_link_target(raw)!r}, which does not "
                                f"resolve to a page. The supersession chain is broken "
                                f"here, so a reader cannot tell a page that was never "
                                f"superseded from one whose forward link rotted."
                            ),
                            proposed_fix=(
                                "Surfaced for REVIEW only — no pointer is repaired, "
                                "re-pointed, or removed. Fix the target path, restore "
                                "the missing page, or drop the pointer if the "
                                "supersession never happened."
                            ),
                            meta={
                                "signal_version": _page_signal_version(page),
                                "review_partition": partition,
                                "defect": "dangling_pointer",
                                "pointer": pointer,
                                "target": _supersession_link_target(raw),
                                "due_since": (
                                    _parse_fm_date(
                                        page.frontmatter.get("updated")
                                        or page.frontmatter.get("created")
                                    )
                                    or dt.date.min
                                ).isoformat(),
                            },
                        )
                    )
                    continue
                older, newer = (
                    (page.rel_path, target)
                    if pointer == "superseded_by"
                    else (target, page.rel_path)
                )
                if older != newer:
                    resolved_edges.append((older, newer))

    findings.extend(_multi_headed_chain_findings(in_scope, resolved_edges))
    # Deterministic: path, then the partition that distinguishes two defects on
    # one page. Nothing here has an age or an urgency to order by.
    findings.sort(key=lambda f: (f.path, str((f.meta or {}).get("review_partition"))))
    return findings


def _multi_headed_chain_findings(
    pages: list[find_module.ParsedPage],
    edges: list[tuple[str, str]],
) -> list[AuditFinding]:
    """One finding per supersession component carrying more than one current head."""
    if not edges:
        return []
    by_path = {page.rel_path: page for page in pages}
    parent: dict[str, str] = {}

    def _find(node: str) -> str:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def _union(left: str, right: str) -> None:
        left_root, right_root = _find(left), _find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    superseded: set[str] = set()
    for older, newer in edges:
        _union(older, newer)
        superseded.add(older)

    components: dict[str, set[str]] = {}
    for older, newer in edges:
        for node in (older, newer):
            components.setdefault(_find(node), set()).add(node)

    findings: list[AuditFinding] = []
    for members in components.values():
        heads = sorted(node for node in members if node not in superseded)
        if len(heads) < 2:
            continue
        # Anchor on the component's first member by path so the finding has one
        # stable home regardless of which head was written last.
        anchor = sorted(members)[0]
        page = by_path.get(anchor)
        findings.append(
            AuditFinding(
                category="supersession_integrity",
                severity="warn",
                path=anchor,
                paths=sorted(members),
                detail=(
                    f"This supersession chain has {len(heads)} current heads "
                    f"({', '.join(heads)}). A chain is meant to have exactly one "
                    f"current answer, so every reader and every consumer of it is "
                    f"silently picking between them."
                ),
                proposed_fix=(
                    "Surfaced for REVIEW only — nothing is merged, re-pointed, or "
                    "archived. Decide which head is current and supersede or archive "
                    "the other, or split the chain if the two are genuinely about "
                    "different things."
                ),
                meta={
                    "signal_version": (_page_signal_version(page) if page is not None else ""),
                    "review_partition": content_hash("multi_head\0" + "\0".join(heads))[:16],
                    "defect": "multi_headed_chain",
                    "heads": heads,
                    "members": sorted(members),
                    "due_since": (
                        (
                            _parse_fm_date(
                                page.frontmatter.get("updated") or page.frontmatter.get("created")
                            )
                            if page is not None
                            else None
                        )
                        or dt.date.min
                    ).isoformat(),
                },
            )
        )
    return findings


# ---------------- check: stale_review ----------------

# Staleness review targets living CONCLUSIONS only. Raw sources have their own
# `unprocessed_source` check, and a time-bounded `experiment` has its own
# `unfinished_experiments` lifecycle check — "is this still true?" is the wrong
# question for either. `production-log` is excluded for the same time-bounded
# reason, but honestly: it has NO lifecycle check yet. (The scaffold's
# "unfinished production lifecycles" entry is still an unbacked claim; closing it
# is filed as a named follow-up in
# `openspec/changes/close-experiment-lifecycle/design.md`.)
_STALE_REVIEW_TYPES = frozenset({"research-note", "insight", "pattern", "failure", "entity"})
# Convention-named hubs/snapshots are EXPECTED to drift (SKILL.md) — never flag.
_STALE_SKIP_SLUG_SUFFIXES = ("-architecture", "-snapshot", "-catalog-snapshot")
_STALE_SKIP_TAGS = frozenset({"hub", "snapshot"})


def _stale_thresholds() -> tuple[int, int, int]:
    """(min_age_days, max_inbound, max_access), env-overridable; bad values fall back.

    Tunable via EXOMEM_STALE_AGE_DAYS / _MAX_INBOUND / _MAX_ACCESS. These are
    gate edges, not weights — the check is a filter, never a score (no
    confidence concept; see SKILL.md rule 5).
    """

    def _int_env(name: str, default: int) -> int:
        raw = os.environ.get(name)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError:
            log.warning("invalid %s=%r; using %s", name, raw, default)
            return default

    return (
        _int_env("EXOMEM_STALE_AGE_DAYS", 365),
        _int_env("EXOMEM_STALE_MAX_INBOUND", 1),
        _int_env("EXOMEM_STALE_MAX_ACCESS", 1),
    )


def _inbound_degree(pages: list[find_module.ParsedPage]) -> dict[str, int]:
    """Inbound wikilink count per KB-relative target (canonicalised, no .md).

    Mirrors `_check_orphan_entities`' referenced-set scan (body + frontmatter
    wikilinks) but COUNTS, over the already-parsed in-memory pages — no second
    disk walk. A page contributes at most 1 to each target it links (dedup per
    source) and never to itself; the `Entities/index.md` hub listing is skipped
    so catalogue rows don't inflate degree.
    """
    counts: dict[str, int] = {}
    for page in pages:
        if (
            page.rel_path.endswith("/Entities/index.md")
            or page.rel_path == f"{kb_prefix()}Entities/index.md"
        ):
            continue
        self_key = _relevance_canon(page.rel_path)
        targets: set[str] = set()
        for match in WIKILINK_PATTERN.finditer(page.body):
            target = match.group(1).strip().removeprefix(kb_prefix()).lstrip("/")
            if target:
                targets.add(_relevance_canon(target))
        for value in page.frontmatter.values():
            for link in _extract_wikilinks_from_value(value):
                targets.add(_relevance_canon(link.removeprefix(kb_prefix()).lstrip("/")))
        for target in targets:
            if target == self_key:
                continue
            counts[target] = counts.get(target, 0) + 1
    return counts


def _stale_access_counts(logs_dir: Path | None = None) -> dict[str, int] | None:
    """How often each KB path was surfaced by `find` (its appearances across
    `top_k` in logs/queries.jsonl), keyed by canonicalised path.

    Returns None when the access signal is UNAVAILABLE — gated for tests
    (`EXOMEM_DISABLE_RELEVANCE_CHECK`, set by the suite so the per-vault audit is
    deterministic regardless of the repo-global log), or no/empty log. None lets
    the caller DROP the access conjunct rather than fabricate "zero access" from
    missing telemetry. Reuses the relevance-log reader.
    """
    if os.environ.get("EXOMEM_DISABLE_RELEVANCE_CHECK"):
        return None
    logs_dir = logs_dir or _RELEVANCE_LOGS_DIR
    queries = _relevance_read_jsonl(logs_dir / "queries.jsonl")
    if not queries:
        return None
    counts: dict[str, int] = {}
    for q in queries:
        for t in q.get("top_k") or []:
            p = t.get("path")
            if p:
                key = _relevance_canon(p)
                counts[key] = counts.get(key, 0) + 1
    return counts


def _stale_activation_params() -> tuple[float, float, float, float]:
    """(decay d, w_surfaced, w_read, w_cited) for the ACT-R dormancy sort.

    Env-overridable via EXOMEM_STALE_DECAY / _W_SURFACED / _W_READ / _W_CITED;
    bad values fall back. ACT-R canonical decay d=0.5; access weights order
    citation > read > surfacing. These weight the review-queue SORT only — they
    never touch the stale_review gate, and they never touch DEFAULT `find`
    ranking. (The opt-in `find(prefer_used=true)` boost is the sole, explicit
    exception: it consumes the same shared primitives via `usage.py` with its
    own RankingConfig weights — notably w_surfaced=0 — never these.)
    """

    def _float_env(name: str, default: float) -> float:
        raw = os.environ.get(name)
        if raw is None:
            return default
        try:
            return float(raw)
        except ValueError:
            log.warning("invalid %s=%r; using %s", name, raw, default)
            return default

    return (
        _float_env("EXOMEM_STALE_DECAY", 0.5),
        _float_env("EXOMEM_STALE_W_SURFACED", 1.0),
        _float_env("EXOMEM_STALE_W_READ", 2.0),
        _float_env("EXOMEM_STALE_W_CITED", 3.0),
    )


def _stale_access_events(
    logs_dir: Path | None = None, today: dt.date | None = None
) -> dict[str, list[tuple[float, float]]] | None:
    """Per-path weighted access events `(delta_days, weight)` for the ACT-R sort.

    Parallel to `_stale_access_counts`, but instead of a surfacing COUNT it
    returns, per canonicalised KB path, the access events feeding the base-level
    activation B = ln(Σ wⱼ·Δtⱼ^(−d)): find-surfacings (queries.jsonl top_k,
    weight w_surfaced), get-reads (reads.jsonl, w_read), and citations
    (writes.jsonl cited_sources, w_cited). delta_days = max((today - ts).days, 1)
    (floored at 1 to dodge the t^−d singularity).

    Returns None when the signal is UNAVAILABLE — gated for tests
    (`EXOMEM_DISABLE_RELEVANCE_CHECK`, set by the suite) or all three logs
    empty — so the caller FALLS BACK to the age-based sort rather than fabricate
    activation. Delegates to the shared primitives in `usage.py` (extracted
    from here verbatim; the parity test guards the move) with audit's own
    env-tunable weights and NO horizon.
    """
    from . import usage

    _, w_surfaced, w_read, w_cited = _stale_activation_params()
    return usage.access_events(
        logs_dir or _RELEVANCE_LOGS_DIR,
        today,
        w_surfaced=w_surfaced,
        w_read=w_read,
        w_cited=w_cited,
    )


def _activation(events: list[tuple[float, float]] | None, d: float) -> float | None:
    """ACT-R base-level activation B = ln(Σ wⱼ·Δtⱼ^(−d)) over weighted access
    events. Higher B = more recently/often accessed = LESS dormant. Returns None
    when there are no events (never accessed) — the caller sorts those to the TOP
    (most dormant). Delegates to `usage.activation`."""
    from . import usage

    return usage.activation(events, d)


def _check_stale_review(
    vault_root: Path,
    pages: list[find_module.ParsedPage],
    *,
    today: dt.date | None = None,
) -> list[AuditFinding]:
    """Surface review candidates: active compiled conclusions that are old AND
    rarely surfaced in `find` AND low inbound-link degree.

    A measurement-only REVIEW QUEUE — it never decays, down-ranks, moves, or
    hides anything (DEFAULT `find` ordering is unchanged; the opt-in
    `find(prefer_used=true)` boost is the sole explicit exception, and it
    lives in `usage.py`/`find.py`, not here); the reader judges keep /
    `replace` (supersede) / archive. All three signals are derived from what the
    KB already records (frontmatter dates, the wikilink graph, the query
    log) — no new sidecar. AND-gated as a filter, not a score (no confidence
    concept). When the access log is unavailable/gated, that conjunct is DROPPED
    (absence is "unknown", never a fabricated zero), so the gate is age AND
    low-inbound. Scope is governed by access tier (read-write) + a conclusion
    type, so it spans the whole writeable KB, not a fixed folder list, and auto-
    excludes the readonly curated trees, append-only Sources/Evidence, hubs/
    snapshots (expected to drift), superseded/archived, and index files.

    Ordering is most-dormant first via ACT-R base-level activation
    (B = ln(Σ wⱼ·Δtⱼ^(−d)) over weighted access events, ascending; never-accessed
    sorts to the top); falls back to oldest-first when the access signal is
    gated/absent. Activation is SORT-ONLY — it never changes who is flagged.
    """
    today = today or dt.date.today()
    min_age_days, max_inbound, max_access = _stale_thresholds()
    degree = _inbound_degree(pages)
    access_counts = _stale_access_counts()  # None when unavailable/gated
    events_map = _stale_access_events(today=today)  # None when unavailable/gated
    d, *_ = _stale_activation_params()

    rows: list[tuple[float, int, AuditFinding]] = []
    for page in pages:
        if page.page_type not in _STALE_REVIEW_TYPES:
            continue
        if page.path.name in ("index.md", "log.md"):
            continue
        if page.status in ("superseded", "archived", "draft"):
            continue
        if access.access_tier(vault_root, page.rel_path) != access.TIER_READ_WRITE:
            continue
        stem = page.path.stem.lower()
        if any(stem.endswith(suffix) for suffix in _STALE_SKIP_SLUG_SUFFIXES):
            continue
        if _STALE_SKIP_TAGS & set(page.tags):
            continue

        updated = _parse_fm_date(page.frontmatter.get("updated") or page.frontmatter.get("created"))
        if updated is None:
            continue  # no date → can't judge age; don't fabricate one
        age_days = max(0, (today - updated).days)
        if age_days < min_age_days:
            continue

        page_key = _relevance_canon(page.rel_path)
        inbound = degree.get(page_key, 0)
        if inbound > max_inbound:
            continue

        access_count = None if access_counts is None else access_counts.get(page_key, 0)
        if access_count is not None and access_count > max_access:
            continue

        bucket = "aging" if age_days < 2 * min_age_days else "stale"
        access_phrase = "" if access_count is None else f", surfaced {access_count}x in find"
        acts = _activation(events_map.get(page_key) if events_map else None, d)
        finding = AuditFinding(
            category="stale_review",
            severity="info",
            path=page.rel_path,
            detail=(
                f"Possibly stale — {age_days}d since updated, "
                f"{inbound} inbound link(s){access_phrase}. Still true?"
            ),
            proposed_fix=(
                "Surfaced for REVIEW only — not auto-decayed or down-ranked; "
                "`find` ordering is unchanged. Confirm still true (keep), "
                "`replace` (supersede) if newer understanding replaces it, or "
                "archive into a `_archive/` subfolder."
            ),
            meta={
                "signal_version": _page_signal_version(page),
                "age_days": age_days,
                "age_bucket": bucket,
                "inbound_count": inbound,
                "access_count": access_count,  # null when the signal is gated/absent
                "activation": round(acts, 4) if acts is not None else None,
                "access_observations": (len(events_map.get(page_key, [])) if events_map else None),
            },
        )
        # Most-dormant first: activation ASCENDING (never-accessed → -inf at the
        # top), age DESCENDING on ties (older first). Sort-only — the gate above
        # already decided who is flagged.
        sort_act = acts if acts is not None else float("-inf")
        rows.append((sort_act, age_days, finding))

    rows.sort(key=lambda r: (r[0], -r[1]))
    return [f for _, _, f in rows]


# ---------------- check: corpus_contradictions ----------------


def _contradiction_top_n() -> int:
    """Default cap on surfaced contradiction pairs (env EXOMEM_CONTRADICTION_TOP_N).

    Default 40. `0` or a negative value disables the cap (surface every in-band
    pair, no omitted-count summary finding). Bad values log + fall back. This
    caps only the SURFACED review list — it never changes what is measured.
    """
    raw = os.environ.get("EXOMEM_CONTRADICTION_TOP_N")
    if raw is None:
        return 40
    try:
        return int(raw)
    except ValueError:
        log.warning("invalid EXOMEM_CONTRADICTION_TOP_N=%r; using 40", raw)
        return 40


def _contradiction_w_dormancy() -> float:
    """Weight on the pair's ACT-R dormancy in the review priority
    (env EXOMEM_CONTRADICTION_W_DORMANCY).

    priority = cosine + w · pair_dormancy, where pair_dormancy ∈ [0, 1]. Default
    0.5: cosine occupies a narrow band (~[0.5, 0.93)) so a dormant pair earns up
    to +0.5, enough to lift a forgotten close pair over a fresher equally-close
    one while cosine still anchors the base order. Bad values log + fall back.
    Sort-only — it never changes who is eligible or `find` ranking.
    """
    raw = os.environ.get("EXOMEM_CONTRADICTION_W_DORMANCY")
    if raw is None:
        return 0.5
    try:
        return float(raw)
    except ValueError:
        log.warning("invalid EXOMEM_CONTRADICTION_W_DORMANCY=%r; using 0.5", raw)
        return 0.5


def _contradiction_family(rel_path: str) -> str | None:
    """Return the `Notes/Research/<X>` family segment of a KB path, else None.

    The architecture-cluster noise is same-family adjacency: many
    `Notes/Research/<X>/*-architecture` pairs that are expected to sit close. Two
    notes are 'same-family' when they share the `<X>` subfolder directly under
    `Notes/Research/`. Returns `"Notes/Research/<X>"` for such a path (after the
    leading `Knowledge Base/` is stripped) and None for anything outside that
    tree (or directly in it with no `<X>` subfolder).
    """
    stripped = rel_path.removeprefix(kb_prefix()).lstrip("/")
    parts = stripped.split("/")
    # parts = ["Notes", "Research", "<X>", ..., "file.md"] → need an <X> dir
    # before the filename, so at least 4 components.
    if len(parts) >= 4 and parts[0] == "Notes" and parts[1] == "Research":
        return "/".join(parts[:3])
    return None


def _pair_dormancy(
    rel_a: str,
    rel_b: str,
    events_map: dict[str, list[tuple[float, float]]] | None,
    d: float,
) -> float:
    """Most-forgotten endpoint's dormancy ∈ [0, 1] for a contradiction pair.

    Per note, reuse the `stale_review` ACT-R activation B = ln(Σ wⱼ·Δtⱼ^(−d)):
    never-accessed (no events) OR a gated/absent access signal → maximally
    dormant (1.0), never a fabricated "active"; otherwise squash via the logistic
    1/(1+e^B) so a highly-active note → ~0 and a barely-active note → ~1. The
    pair takes the MAX over its two notes — one forgotten endpoint is the review
    trigger ("did I forget I already concluded the opposite?").
    """

    def _one(rel: str) -> float:
        events = events_map.get(_relevance_canon(rel)) if events_map else None
        b = _activation(events, d)
        if b is None:
            return 1.0
        return 1.0 / (1.0 + math.exp(b))

    return max(_one(rel_a), _one(rel_b))


def _is_active_compiled_rw(vault_root: Path, page: find_module.ParsedPage) -> bool:
    """An active, read-write, COMPILED conclusion — the only pages a contradiction
    can actually be reconciled against (edit/replace/supersede). Mirrors the scope
    of `corpus_aware.detect_contradictions` + `_check_stale_review`: a compiled type
    (`find._COMPILED_TYPES`), not an index/log hub, not superseded/archived/draft,
    and in a writeable (read-write) tree (auto-excludes readonly curated trees,
    append-only Sources/Evidence, and excluded subtrees)."""
    if page.page_type not in find_module._COMPILED_TYPES:
        return False
    if page.path.name in ("index.md", "log.md"):
        return False
    if page.status in ("superseded", "archived", "draft"):
        return False
    if access.access_tier(vault_root, page.rel_path) != access.TIER_READ_WRITE:
        return False
    return True


def _claim_level_enabled() -> bool:
    """EXOMEM_CLAIM_LEVEL gate for the claim-level polarity enrichment. Isolated
    so a claims-import problem can't disable the whole sweep."""
    try:
        from . import claims

        return claims.claim_level_enabled()
    except Exception:  # noqa: BLE001
        return False


def _pair_polarity(vault_root: Path, a: str, b: str) -> dict | None:
    """Claim-level polarity for one flagged pair, or None (best-effort).

    Pulls each page's stored/live claim (`claims.claim_text_for_page`) and runs
    `claims.classify_polarity`. Returns `{label, score, method}` or None when a
    claim is missing / the check fails — the audit finding then degrades to the
    proximity-only detail. Never raises into the sweep.
    """
    try:
        from . import claims

        claim_a = claims.claim_text_for_page(vault_root, a)
        claim_b = claims.claim_text_for_page(vault_root, b)
        if not claim_a or not claim_b:
            return None
        res = claims.classify_polarity(claim_a, claim_b)
        return {"label": res.label, "score": res.score, "method": res.method}
    except Exception as e:  # noqa: BLE001
        log.debug("audit pair polarity failed for (%s, %s): %s", a, b, e)
        return None


_ASSERTED_FIX = (
    "Surfaced for REVIEW only — this is YOUR authored `contradicts` edge, not a "
    "server judgment about which side is right. Read both: `replace` (supersede) "
    "the stale one, `reconcile` them, or — if they are genuine rivals you intend "
    "to keep — record the competing-alternatives stance with `triage_memory` "
    "using `action='competing'`. Never auto-acted."
)


def _asserted_contradictions(
    eligible: dict[str, find_module.ParsedPage],
    pairs: list[tuple[str, str]],
) -> tuple[list[AuditFinding], set[tuple[str, str]]]:
    """Authored `contradicts` edges as contradiction findings, plus their pair keys.

    The strongest contradiction signal a vault carries is the one the author wrote
    down, and until this lane existed nothing consumed it. Unlike the proximity
    sweep this needs NO embeddings — it reads typed graph edges — so it survives a
    torch-less deploy and an `EXOMEM_DISABLE_EMBEDDINGS` run. Both endpoints must
    clear the same `_is_active_compiled_rw` bar the proximity sweep uses, because
    those are the only pages a contradiction can actually be reconciled against.

    Deliberately NOT capped by `EXOMEM_CONTRADICTION_TOP_N`: that cap exists for the
    combinatorial proximity sweep, and silently hiding an edge the user typed by hand
    would be a different thing entirely.
    """
    findings: list[AuditFinding] = []
    keys: set[tuple[str, str]] = set()
    for a, b in pairs:
        if a not in eligible or b not in eligible:
            continue
        keys.add((a, b))
        findings.append(
            AuditFinding(
            category="corpus_contradictions",
            severity="info",
            path=a,
            detail=(
                f"Authored `contradicts` edge with {b!r} — you asserted these "
                "conflict. Is the conflict still live, or has one side become the "
                "stale view?"
            ),
            proposed_fix=_ASSERTED_FIX,
            paths=[a, b],
            meta={
                # Distinct from the proximity signal version for the same pair, so
                # an asserted decision and a proximity decision can never collide.
                "signal_version": content_hash(
                    "asserted\n"
                    + _page_signal_version(eligible[a])
                    + "\n"
                    + _page_signal_version(eligible[b])
                )[:16],
                "provenance": "asserted",
                "relation_type": "contradicts",
            },
            )
        )
    return findings, keys


def _check_corpus_contradictions(
    vault_root: Path,
    pages: list[find_module.ParsedPage],
    *,
    today: dt.date | None = None,
) -> list[AuditFinding]:
    """The contradiction queue: authored conflicts first, then measured proximity.

    Two lanes, one category. Asserted entries come from authored `contradicts`
    graph edges and are emitted FIRST — `attention` treats emission order as
    intra-queue rank, so "ranked above proximity" needs no ranking code. Proximity
    entries come from the embedding-band sweep below and keep their existing
    priority, same-family demotion, and cap behaviour among themselves.

    A pair that is both authored and in band surfaces once, as asserted: the
    authored edge is strictly the stronger signal, and two rows for one decision
    would double the pair's RRF vote in `attention`.
    """
    pairs = contradiction_stance.asserted_pairs(vault_root)
    if not pairs and os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
        # Nothing authored and no sidecar lane to run: keep the pre-existing
        # torch-less fast path — one indexed graph query and out, without paying
        # the eligibility walk this category used to skip entirely.
        return []
    eligible: dict[str, find_module.ParsedPage] = {
        page.rel_path: page for page in pages if _is_active_compiled_rw(vault_root, page)
    }
    asserted, asserted_keys = _asserted_contradictions(eligible, pairs)
    return asserted + _proximity_contradictions(
        vault_root, eligible, today=today, exclude=asserted_keys
    )


def _proximity_contradictions(
    vault_root: Path,
    eligible: dict[str, find_module.ParsedPage],
    *,
    today: dt.date | None = None,
    exclude: set[tuple[str, str]] | None = None,
) -> list[AuditFinding]:
    """Corpus-wide contradiction sweep: surface PAIRS of active read-write compiled
    conclusions whose embeddings sit in the band `[floor, dup_threshold)`.

    The audit-time counterpart to `corpus_aware.detect_contradictions` (which fires
    on a single write): instead of one draft vs. the corpus, it sweeps every active
    read-write compiled conclusion against every other and reports the deduped,
    unordered file pairs whose max chunk-cosine lands just below the near-dup
    ceiling. That band is close enough to plausibly restate, refine, OR contradict —
    a PROXIMITY measurement, not a stance judgment (cosine can't separate "X works"
    from "X doesn't"), so each pair is surfaced for the reader to reconcile or
    supersede; nothing is ever auto-acted.

    Reuses the existing vector sidecar (`EmbeddingIndex.all_vectors()`, cached by
    the write generation) — it reads the chunk vectors already on disk and never re-encodes, so the
    sweep is O(eligible_files) matmuls over the sidecar matrix and needs no model
    (works on a CPU/offline box as long as a sidecar exists). The band edges are the
    same knobs the write-time check uses: floor = `corpus_aware._contradiction_floor`,
    ceiling = `corpus_aware._dup_threshold`. An inverted band (floor >= ceiling) is
    disabled. No-ops cleanly (returns []) when embeddings are disabled, the sidecar
    is empty, or numpy/embeddings are unimportable — the same gate the write-time
    check honors, so the fast test suite and torch-less deploys are unaffected.

    The surfaced pairs are ORDERED into a usable review queue (the raw sweep is
    flat cosine-descending and dominated by same-family architecture noise): each
    pair gets a review `priority = cosine + w · pair_dormancy`, where pair_dormancy
    is the most-forgotten endpoint's ACT-R dormancy (reusing the `stale_review`
    activation calc — a dormant note in a close pair is the "is this still true /
    did I forget I concluded the opposite" case). Same-family pairs (both notes in
    one `Notes/Research/<X>/` subfolder) are flagged and sorted last. The surfaced
    set is capped at `EXOMEM_CONTRADICTION_TOP_N` (default 40; `0` = uncapped) with
    an explicit omitted-count summary finding — never a silent truncation. This
    ORDERS/CAPS the review list only; it never mutates a note or touches `find`.
    """
    if os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
        return []
    from . import corpus_aware

    floor = corpus_aware._contradiction_floor()
    ceiling = corpus_aware._dup_threshold()
    if floor >= ceiling:
        log.warning(
            "contradiction floor (%s) >= dup ceiling (%s); corpus_contradictions "
            "sweep disabled this run",
            floor,
            ceiling,
        )
        return []
    try:
        import numpy as np

        from . import embeddings as embeddings_module
    except ImportError as e:  # numpy is core, but stay defensive
        log.debug("corpus_contradictions sweep unavailable (%s)", e)
        return []

    idx = embeddings_module.get_embedding_index(vault_root)
    metadata, matrix = idx.all_vectors()  # cached by the sidecar write generation
    if not metadata or matrix.shape[0] == 0:
        return []

    # Both endpoints of a flagged pair must be active read-write compiled — the
    # caller already applied that gate when building `eligible`.
    if len(eligible) < 2:
        return []
    excluded = exclude or set()

    rows_by_file: dict[str, list[int]] = {}
    for i, (fp, _cidx) in enumerate(metadata):
        rows_by_file.setdefault(fp, []).append(i)

    # max chunk-cosine per deduped unordered file pair, both endpoints eligible.
    pair_cos: dict[tuple[str, str], float] = {}
    for fp, _page in eligible.items():
        rows = rows_by_file.get(fp)
        if not rows:
            continue  # eligible page with no vectors yet (e.g. never embedded)
        sub = matrix[rows]                       # (m, D) this file's chunk vectors
        col_max = (sub @ matrix.T).max(axis=0)   # (N,) best cosine file→each chunk
        in_band = np.nonzero((col_max >= floor) & (col_max < ceiling))[0]
        for j in in_band:
            other_fp = metadata[int(j)][0]
            if other_fp == fp or other_fp not in eligible:
                continue
            score = float(col_max[int(j)])
            a, b = sorted((fp, other_fp))
            key = (a, b)
            # Already surfaced as an authored contradiction: the stronger signal
            # owns the pair, so it is not re-measured, re-counted, or re-capped.
            if key in excluded:
                continue
            if key not in pair_cos or score > pair_cos[key]:
                pair_cos[key] = score

    # Order into a usable review queue: priority = cosine + w · pair_dormancy,
    # same-family pairs demoted, then capped at top-N with an explicit count.
    # Dormancy reuses the stale_review ACT-R calc; gated/absent access → 1.0
    # (maximally dormant) so ordering degrades to cosine, never crashes.
    events_map = _stale_access_events(today=today)  # None when gated/unavailable
    d, *_ = _stale_activation_params()
    w_dormancy = _contradiction_w_dormancy()

    scored: list[tuple[bool, float, str, str, float, float]] = []
    for (a, b), cos in pair_cos.items():
        same_family = _contradiction_family(a) is not None and _contradiction_family(
            a
        ) == _contradiction_family(b)
        dormancy = _pair_dormancy(a, b, events_map, d)
        priority = cos + w_dormancy * dormancy
        scored.append((same_family, priority, a, b, cos, dormancy))

    # Cross-family first; within a bucket by priority desc; path tiebreak.
    scored.sort(key=lambda r: (r[0], -r[1], r[2], r[3]))

    top_n = _contradiction_top_n()
    capped = top_n > 0 and len(scored) > top_n
    shown = scored[:top_n] if capped else scored

    # Sharpen PROXIMITY → POLARITY on the surfaced (already-capped) pairs. Opt-in
    # via EXOMEM_CLAIM_LEVEL; off → `_pair_polarity` returns None so every finding
    # is byte-identical to baseline. Bounded by top_n (shown is already capped).
    claim_level = _claim_level_enabled()

    findings: list[AuditFinding] = []
    for same_family, priority, a, b, cos, dormancy in shown:
        family_note = (
            " Same-family adjacency (likely architecture-cluster noise) — demoted."
            if same_family
            else ""
        )
        polarity = _pair_polarity(vault_root, a, b) if claim_level else None
        polarity_note = (
            f" Claim-level check: likely {polarity['label'].upper()} (via {polarity['method']})."
            if polarity
            else ""
        )
        meta = {
            "signal_version": content_hash(
                _page_signal_version(eligible[a]) + "\n" + _page_signal_version(eligible[b])
            )[:16],
            "cosine": round(cos, 4),
            "priority": round(priority, 4),
            "dormancy": round(dormancy, 4),
            "same_family": same_family,
            "provenance": "proximity",
        }
        if polarity:
            meta["polarity"] = polarity["label"]
            meta["polarity_score"] = polarity["score"]
            meta["polarity_method"] = polarity["method"]
        findings.append(
            AuditFinding(
            category="corpus_contradictions",
            severity="info",
            path=a,
            detail=(
                f"Active conclusion overlaps active conclusion {b!r} "
                f"(cosine {round(cos, 4)}) — close enough to restate, refine, or "
                f"contradict. Do they conflict?{family_note}{polarity_note}"
            ),
            proposed_fix=(
                "Surfaced for REVIEW only — a proximity measurement, not an asserted "
                "contradiction. Read both: if they genuinely conflict, `replace` "
                "(supersede) the stale one or `reconcile` them; otherwise leave as-is. "
                "Never auto-acted."
            ),
            paths=[a, b],
            meta=meta,
            )
        )

    if capped:
        omitted = len(scored) - top_n
        findings.append(
            AuditFinding(
            category="corpus_contradictions",
            severity="info",
            path=kb_prefix(),
            detail=(
                f"{omitted} more lower-priority/same-family contradiction pair(s) "
                f"not shown (showing top {top_n} of {len(scored)}; raise "
                f"EXOMEM_CONTRADICTION_TOP_N or set it to 0 to see all)."
            ),
            proposed_fix=(
                "Work the surfaced pairs first; raise EXOMEM_CONTRADICTION_TOP_N "
                "(or set it to 0) to surface the remainder. Ordering/capping is "
                "measurement-only — nothing is mutated or auto-acted."
            ),
            meta={"truncated": omitted, "shown": top_n, "total": len(scored)},
            )
        )
    return findings


# ---------------- helpers ----------------


def _rel_kb_path_no_ext(absolute: Path, vault_root: Path) -> str:
    """Return KB-rooted path with .md stripped, e.g. 'Sources/Articles/foo'.

    Matches the form wikilinks use after the leading 'Knowledge Base/' is stripped.
    """
    rel = absolute.resolve().relative_to(vault_root.resolve())
    no_ext = rel.with_suffix("").as_posix()
    return no_ext.removeprefix(kb_prefix()).lstrip("/")
