"""The `attention` review surface — one ranked "what needs your review today" list.

Composes the four measurement-only epistemic queues that `audit` already produces —
`corpus_contradictions`, `stale_review`, `unprocessed_source`, `relation_debt` — into a single ranked
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

from . import activation as activation_module
from . import audit as audit_module
from . import fusion
from . import review_state as review_state_module
from .audit import AuditFinding

# The queues this surface composes, in tiebreak-preference order (highest first):
# a self-contradiction is the most actionable signal, an unprocessed source the least.
ATTENTION_CATEGORIES: tuple[str, ...] = (
    "corpus_contradictions",
    "stale_review",
    "unprocessed_source",
    "relation_debt",
)
_SEVERITY_RANK: dict[str, int] = {"info": 0, "warn": 1, "error": 2}
_SEVERITY_BY_RANK: dict[int, str] = {v: k for k, v in _SEVERITY_RANK.items()}
_RRF_K: int = 60  # the conventional default `fusion` and `find` use

_PROPOSED_FIX: str = (
    "Surfaced for REVIEW only — this ranking is a deterministic measurement, not a "
    "judgment that anything conflicts or is wrong. You decide per reason: keep / "
    "`replace` (supersede) / `reconcile` / `propose_compilation` / "
    "`connect_memory` / archive. Nothing is "
    "auto-acted; `find` ordering is unchanged."
)

_ACTIVATION_FIX: str = (
    "Surfaced for REVIEW only. Coverage and ranking measure explicit Markdown "
    "structure; they do not judge truth or quality. Follow a reason's `next_actions` "
    "only after review. Nothing is auto-written or auto-registered."
)


@dataclass
class AttentionItem:
    path: str                 # the anchor note
    score: float              # fused RRF score (higher = more attention)
    severity: str             # max severity over the contributing reasons
    categories: list[str]     # queues that flagged this note, in preference order
    reasons: list[dict]       # one per contributing finding: {category, rank, detail, related_paths?, meta?}
    proposed_fix: str
    item_id: str | None = None
    ref: str | None = None
    target_ref: str | None = None
    related_refs: list[str] | None = None
    fingerprint: str | None = None
    state: str | None = None
    state_detail: dict | None = None

    def as_dict(self) -> dict:
        out = {
            "path": self.path,
            "score": self.score,
            "severity": self.severity,
            "categories": self.categories,
            "reasons": self.reasons,
            "proposed_fix": self.proposed_fix,
        }
        if self.item_id is not None:
            out.update(
                {
                    "item_id": self.item_id,
                    "ref": self.ref,
                    "target_ref": self.target_ref,
                    "related_refs": self.related_refs or [],
                    "fingerprint": self.fingerprint,
                    "state": self.state or "open",
                }
            )
            if self.state_detail is not None:
                out["state_detail"] = self.state_detail
        return out


@dataclass
class AttentionReport:
    items: list[AttentionItem]
    summary: dict[str, int]       # contributing-finding count per category (pre-dedup, pre-cap)
    shown: int
    total: int                    # distinct anchors after dedup, before the cap
    truncated: int                # anchors beyond `limit` not shown
    upstream_truncated: int       # contradiction pairs the upstream cap omitted (folded in)
    note: str | None
    all_total: int | None = None
    state_summary: dict[str, int] | None = None
    coverage: dict[str, int] | None = None

    def as_dict(self) -> dict:
        out = {
            "items": [it.as_dict() for it in self.items],
            "summary": self.summary,
            "shown": self.shown,
            "total": self.total,
            "truncated": self.truncated,
            "upstream_truncated": self.upstream_truncated,
            "note": self.note,
        }
        if self.all_total is not None:
            out["all_total"] = self.all_total
            out["state_summary"] = self.state_summary or {}
        if self.coverage is not None:
            out["coverage"] = self.coverage
        return out


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
    category_order: tuple[str, ...] = ATTENTION_CATEGORIES,
    proposed_fix: str = _PROPOSED_FIX,
) -> AttentionReport:
    """Compose findings into one ranked, deduped review surface. Pure — no vault access.

    Fuse each finding's intra-queue rank (emission order == rank) via weighted RRF, dedup
    by anchor path (votes add → multi-flagged notes rise), drop+fold the contradiction
    queue's trailing summary finding, then cap at `limit` with an explicit count.
    """
    selected = set(category_order) if categories is None else (
        set(categories) & set(category_order)
    )
    weights = ({c: 1.0 for c in category_order} if weights is None else weights)
    category_rank = {category: rank for rank, category in enumerate(category_order)}

    per_cat: dict[str, list[AuditFinding]] = {c: [] for c in category_order}
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
    for c in category_order:
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
    for c in category_order:
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
            min(category_rank[r["category"]] for r in reasons_by_path[p]),
            p,
        ),
    )
    total = len(ordered)
    shown_paths = ordered[:limit] if (limit and limit > 0) else ordered

    items: list[AttentionItem] = []
    for p in shown_paths:
        reasons = sorted(
            reasons_by_path[p],
            key=lambda r: (r["rank"], category_rank[r["category"]]),
        )
        cats = sorted({r["category"] for r in reasons}, key=lambda c: category_rank[c])
        items.append(AttentionItem(
            path=p,
            score=round(scores[p], 6),
            severity=_SEVERITY_BY_RANK[severity_by_path[p]],
            categories=cats,
            reasons=reasons,
            proposed_fix=proposed_fix,
        ))

    truncated = total - len(items)
    summary = {
        c: len(per_cat[c])
        for c in category_order
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
    state: str = "open",
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
    state = str(state or "open").strip().lower()
    if state not in review_state_module.VALID_VIEWS:
        raise ValueError(
            f"INVALID_REVIEW_STATE: state must be one of "
            f"{sorted(review_state_module.VALID_VIEWS)}"
        )
    report = audit_module.audit(vault_root, categories=sorted(resolved), today=today)
    ranked = _rank(report.findings, categories=resolved, limit=0)
    return _apply_review_state(
        vault_root,
        ranked,
        state=state,
        limit=limit,
        today=today,
    )


def activation(
    vault_root: Path,
    *,
    categories: list[str] | None = None,
    limit: int = 25,
    today=None,
    state: str = "open",
) -> AttentionReport:
    """Rank deterministic existing-corpus activation measurements. Read-only."""
    resolved = (
        set(activation_module.ACTIVATION_CATEGORIES)
        if not categories
        else set(categories)
    )
    invalid = resolved - set(activation_module.ACTIVATION_CATEGORIES)
    if invalid:
        raise ValueError(
            f"unknown activation categories: {sorted(invalid)}. "
            f"Valid: {list(activation_module.ACTIVATION_CATEGORIES)}"
        )
    state = str(state or "open").strip().lower()
    if state not in review_state_module.VALID_VIEWS:
        raise ValueError(
            f"INVALID_REVIEW_STATE: state must be one of "
            f"{sorted(review_state_module.VALID_VIEWS)}"
        )
    scan = activation_module.scan(vault_root)
    ranked = _rank(
        scan.findings,
        categories=resolved,
        limit=0,
        category_order=activation_module.ACTIVATION_CATEGORIES,
        proposed_fix=_ACTIVATION_FIX,
    )
    ranked.coverage = scan.coverage
    return _apply_review_state(vault_root, ranked, state=state, limit=limit, today=today)


def item_by_ref(
    vault_root: Path,
    reference: str,
    *,
    expected_fingerprint: str | None = None,
    today=None,
) -> AttentionItem:
    """Resolve one current review item by its stable review reference."""
    wanted = review_state_module.parse_review_ref(reference)
    matches: list[AttentionItem] = []
    for report in (
        attention(vault_root, limit=0, state="all", today=today),
        activation(vault_root, limit=0, state="all", today=today),
    ):
        for item in report.items:
            if item.item_id == wanted:
                if expected_fingerprint and item.fingerprint == expected_fingerprint:
                    return item
                matches.append(item)
    if matches:
        return matches[0]
    raise ValueError(f"REVIEW_ITEM_NOT_FOUND: no current review item for {reference}")


def _apply_review_state(
    vault_root: Path,
    report: AttentionReport,
    *,
    state: str,
    limit: int,
    today=None,
) -> AttentionReport:
    all_paths: list[str] = []
    for item in report.items:
        all_paths.append(item.path)
        for reason in item.reasons:
            all_paths.extend(reason.get("related_paths") or [])
    refs = review_state_module.refs_for_paths(vault_root, all_paths)
    store = review_state_module.ReviewStateStore(vault_root)
    state_payload = store.load()
    state_summary = {"open": 0, "snoozed": 0, "dismissed": 0}

    for item in report.items:
        target_ref = refs[item.path]
        review_id = review_state_module.item_id(target_ref)
        related_paths = sorted(
            {
                path
                for reason in item.reasons
                for path in (reason.get("related_paths") or [])
                if path != item.path
            }
        )
        related_refs = [refs[path] for path in related_paths if path in refs]
        signal_fingerprint = review_state_module.fingerprint(
            target_ref=target_ref,
            categories=item.categories,
            reasons=item.reasons,
            related_refs=related_refs,
        )
        effective, decision = store.effective_state(
            review_id,
            signal_fingerprint,
            today=today,
            payload=state_payload,
        )
        item.item_id = review_id
        item.ref = review_state_module.review_ref(review_id)
        item.target_ref = target_ref
        item.related_refs = related_refs
        item.fingerprint = signal_fingerprint
        item.state = effective
        item.state_detail = decision.as_dict() if decision is not None else None
        state_summary[effective] += 1

    if state == "all":
        eligible = list(report.items)
    else:
        eligible = [item for item in report.items if item.state == state]
    shown_items = eligible[:limit] if limit > 0 else eligible
    total = len(eligible)
    truncated = total - len(shown_items)
    note = _build_note(
        len(shown_items),
        total,
        truncated,
        report.upstream_truncated,
    )
    return AttentionReport(
        items=shown_items,
        summary=report.summary,
        shown=len(shown_items),
        total=total,
        truncated=truncated,
        upstream_truncated=report.upstream_truncated,
        note=note,
        all_total=len(report.items),
        state_summary=state_summary,
        coverage=report.coverage,
    )
