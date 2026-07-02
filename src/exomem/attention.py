"""The `attention` review surface — one ranked "what needs your review today" list.

Composes the three measurement-only epistemic queues that `audit` already produces —
`corpus_contradictions`, `stale_review`, `unprocessed_source` — into a single ranked
list. The composition is pure measurement: each queue already emits its findings in
intra-queue rank order, and this module fuses those ranks with Reciprocal Rank Fusion
(the same `fusion` utility `find` uses) and dedups by anchor path. No note content is
read, embedded, or compared here; nothing is mutated; `find` ordering is untouched. The
brain (Claude) decides what to do with each surfaced item.

The line: surfacing + deterministic rank arithmetic over already-computed measurements is
MEASUREMENT (in bounds, like `find`'s weighted RRF and the contradiction queue's dormancy
sort). Cross-item synthesis/judgment would be the brain's job and is deliberately absent.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import audit as audit_module
from . import fusion
from .audit import AuditFinding

# The queues this surface composes, in tiebreak-preference order (highest first):
# a self-contradiction is the most actionable signal, an unprocessed source the least.
ATTENTION_CATEGORIES: tuple[str, ...] = (
    "corpus_contradictions",
    "stale_review",
    "unprocessed_source",
)
_CATEGORY_ORDER: dict[str, int] = {c: i for i, c in enumerate(ATTENTION_CATEGORIES)}
_SEVERITY_RANK: dict[str, int] = {"info": 0, "warn": 1, "error": 2}
_SEVERITY_BY_RANK: dict[int, str] = {v: k for k, v in _SEVERITY_RANK.items()}
# Equal default weights — the order is then a clean rank-major interleave, except a note
# flagged by >1 queue accumulates votes and rises. Weights are a seam, not env-exposed.
_DEFAULT_WEIGHTS: dict[str, float] = {c: 1.0 for c in ATTENTION_CATEGORIES}
_RRF_K: int = 60  # the conventional default `fusion` and `find` use

_PROPOSED_FIX: str = (
    "Surfaced for REVIEW only — this ranking is a deterministic measurement, not a "
    "judgment that anything conflicts or is wrong. You decide per reason: keep / "
    "`replace` (supersede) / `reconcile` / `propose_compilation` / archive. Nothing is "
    "auto-acted; `find` ordering is unchanged."
)


@dataclass
class AttentionItem:
    path: str                 # the anchor note
    score: float              # fused RRF score (higher = more attention)
    severity: str             # max severity over the contributing reasons
    categories: list[str]     # queues that flagged this note, in preference order
    reasons: list[dict]       # one per contributing finding: {category, rank, detail, related_paths?, meta?}
    proposed_fix: str

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "score": self.score,
            "severity": self.severity,
            "categories": self.categories,
            "reasons": self.reasons,
            "proposed_fix": self.proposed_fix,
        }


@dataclass
class AttentionReport:
    items: list[AttentionItem]
    summary: dict[str, int]       # contributing-finding count per category (pre-dedup, pre-cap)
    shown: int
    total: int                    # distinct anchors after dedup, before the cap
    truncated: int                # anchors beyond `limit` not shown
    upstream_truncated: int       # contradiction pairs the upstream cap omitted (folded in)
    note: str | None

    def as_dict(self) -> dict:
        return {
            "items": [it.as_dict() for it in self.items],
            "summary": self.summary,
            "shown": self.shown,
            "total": self.total,
            "truncated": self.truncated,
            "upstream_truncated": self.upstream_truncated,
            "note": self.note,
        }


def _reason(category: str, rank: int, finding: AuditFinding) -> dict:
    """Build one reason dict from a contributing finding, preserving its pair + meta."""
    reason: dict = {"category": category, "rank": rank, "detail": finding.detail}
    if finding.paths:
        reason["related_paths"] = list(finding.paths)
    if finding.meta:
        reason["meta"] = finding.meta
    return reason


def _build_note(shown: int, total: int, truncated: int, upstream_truncated: int) -> str | None:
    """Explicit truncation note — never a silent cap (mirrors the contradiction queue)."""
    if truncated <= 0 and upstream_truncated <= 0:
        return None
    parts: list[str] = []
    if truncated > 0:
        parts.append(
            f"Showing top {shown} of {total} review items "
            f"({truncated} more not shown; raise `limit`)."
        )
    else:
        parts.append(f"Showing all {total} review item(s).")
    if upstream_truncated > 0:
        parts.append(
            f"(+{upstream_truncated} contradiction pair(s) capped upstream by "
            f"EXOMEM_CONTRADICTION_TOP_N; raise it to surface more.)"
        )
    return " ".join(parts)


def _rank(
    findings: list[AuditFinding],
    *,
    categories: set[str] | None = None,
    limit: int = 25,
    weights: dict[str, float] | None = None,
) -> AttentionReport:
    """Compose findings into one ranked, deduped review surface. Pure — no vault access.

    Fuse each finding's intra-queue rank (emission order == rank) via weighted RRF, dedup
    by anchor path (votes add → multi-flagged notes rise), drop+fold the contradiction
    queue's trailing summary finding, then cap at `limit` with an explicit count.
    """
    selected = set(ATTENTION_CATEGORIES) if categories is None else (
        set(categories) & set(ATTENTION_CATEGORIES)
    )
    weights = _DEFAULT_WEIGHTS if weights is None else weights

    per_cat: dict[str, list[AuditFinding]] = {c: [] for c in ATTENTION_CATEGORIES}
    upstream_truncated = 0
    for f in findings:
        if f.category not in selected:
            continue
        # The contradiction queue appends a trailing summary finding for the pairs it
        # capped upstream — not a reviewable item; fold its count, don't surface it.
        if f.category == "corpus_contradictions" and f.meta and "truncated" in f.meta:
            upstream_truncated += int(f.meta["truncated"])
            continue
        per_cat[f.category].append(f)

    # One best-first anchor-path list per populated category, plus aligned weights.
    result_lists: list[list[str]] = []
    weight_list: list[float] = []
    for c in ATTENTION_CATEGORIES:
        if c in selected and per_cat[c]:
            result_lists.append([f.path for f in per_cat[c]])
            weight_list.append(float(weights.get(c, 1.0)))

    # Reuse the house RRF for the scores; an anchor's score uses its best rank per list.
    fused = (
        fusion.reciprocal_rank_fusion_weighted(result_lists, weight_list, k=_RRF_K)
        if result_lists else []
    )
    scores: dict[str, float] = dict(fused)

    # All reasons (every contributing finding) + max severity per anchor path.
    reasons_by_path: dict[str, list[dict]] = {}
    severity_by_path: dict[str, int] = {}
    for c in ATTENTION_CATEGORIES:
        if c not in selected:
            continue
        for rank, f in enumerate(per_cat[c], start=1):
            reasons_by_path.setdefault(f.path, []).append(_reason(c, rank, f))
            severity_by_path[f.path] = max(
                severity_by_path.get(f.path, 0), _SEVERITY_RANK.get(f.severity, 0)
            )

    # Order: score desc, then category preference of the item's best reason, then path.
    ordered = sorted(
        scores,
        key=lambda p: (
            -scores[p],
            min(_CATEGORY_ORDER[r["category"]] for r in reasons_by_path[p]),
            p,
        ),
    )
    total = len(ordered)
    shown_paths = ordered[:limit] if (limit and limit > 0) else ordered

    items: list[AttentionItem] = []
    for p in shown_paths:
        reasons = sorted(
            reasons_by_path[p],
            key=lambda r: (r["rank"], _CATEGORY_ORDER[r["category"]]),
        )
        cats = sorted({r["category"] for r in reasons}, key=lambda c: _CATEGORY_ORDER[c])
        items.append(AttentionItem(
            path=p,
            score=round(scores[p], 6),
            severity=_SEVERITY_BY_RANK[severity_by_path[p]],
            categories=cats,
            reasons=reasons,
            proposed_fix=_PROPOSED_FIX,
        ))

    truncated = total - len(items)
    summary = {
        c: len(per_cat[c])
        for c in ATTENTION_CATEGORIES
        if c in selected and per_cat[c]
    }
    note = _build_note(len(items), total, truncated, upstream_truncated)
    return AttentionReport(
        items=items,
        summary=summary,
        shown=len(items),
        total=total,
        truncated=truncated,
        upstream_truncated=upstream_truncated,
        note=note,
    )


def attention(
    vault_root: Path,
    *,
    categories: list[str] | None = None,
    limit: int = 25,
    today=None,
) -> AttentionReport:
    """Compose the three epistemic queues into one ranked review surface. Read-only.

    Runs a single `audit` pass over the selected categories, then ranks/dedups via
    `_rank`. `today` is threaded through for deterministic ACT-R dormancy in tests.
    """
    resolved = set(ATTENTION_CATEGORIES) if not categories else set(categories)
    invalid = resolved - set(ATTENTION_CATEGORIES)
    if invalid:
        raise ValueError(
            f"unknown attention categories: {sorted(invalid)}. "
            f"Valid: {list(ATTENTION_CATEGORIES)}"
        )
    report = audit_module.audit(vault_root, categories=sorted(resolved), today=today)
    return _rank(report.findings, categories=resolved, limit=limit)
