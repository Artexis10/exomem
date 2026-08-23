"""The `attention` review surface — one ranked "what needs your review today" list.

Composes the seven default measurement-only queues that `audit` already produces —
`bridge_review`, `prediction_window`, `supersession_integrity`,
`corpus_contradictions`, `stale_review`,
`unprocessed_source`, and `relation_debt` — into a single ranked list while retaining
opt-in registered semantic and epistemic-lifecycle categories. The composition is pure
measurement: each queue already emits its findings
in intra-queue rank order, and this module fuses those ranks with Reciprocal Rank Fusion
(the same `fusion` utility `find` uses) and dedups by anchor path. No note content is
read, embedded, or compared here; nothing is mutated; `find` ordering is untouched. The
caller decides what to do with each surfaced item.

The line: surfacing + deterministic rank arithmetic over already-computed measurements is
MEASUREMENT (in bounds, like `find`'s weighted RRF and the contradiction queue's dormancy
sort). Cross-item synthesis/judgment would be the brain's job and is deliberately absent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from . import activation as activation_module
from . import audit as audit_module
from . import contradiction_stance as contradiction_stance_module
from . import fusion
from . import review_state as review_state_module
from .audit import AuditFinding

log = logging.getLogger(__name__)

# The default queues in deterministic tiebreak-preference order (highest first).
#
# The load-bearing rule is only about the top two: `bridge_review` and
# `prediction_window` fire on DATES A HUMAN WROTE DOWN — a governance review date
# and an epistemic check date — so an authored deadline outranks every queue that
# infers its candidates. Ranking a guess above a promise would be the wrong way
# round. `bridge_review` leads because its commitment is owed to another
# audience, where a check date is owed to yourself.
#
# `supersession_integrity` sits third for a reason of the same kind rather than
# by seniority: it is the only DEFECT queue in the union. The two above it report
# an authored obligation that has come due, which is work; a dangling supersession
# pointer or a two-headed chain reports state that is already WRONG, and a reader
# who fixes one is repairing the record rather than deciding something. It ranks
# below the dated queues because a broken pointer does not expire while a check
# date does, and above the inferential ones because nothing about it is inferred.
#
# Below that line the order is HISTORICAL, not principled: `unprocessed_source`
# and `relation_debt` are deterministic scans over authored state and are in that
# sense less inferential than `corpus_contradictions` above them. Do not read a
# gradient into it, and do not use one to justify inserting a queue mid-list.
DEFAULT_ATTENTION_CATEGORIES: tuple[str, ...] = (
    "bridge_review",
    "prediction_window",
    "supersession_integrity",
    "corpus_contradictions",
    "stale_review",
    "unprocessed_source",
    "relation_debt",
    # Last, and deliberately not mid-list: this queue fires on a binding a person
    # authored in a manifest, which is the `prediction_window` argument for being
    # in the default union at all, but it is the newest and the least-evidenced of
    # them. A vault that declares no binding never sees it.
    "unreflected_outcomes",
)
# Registered — selectable via `categories` — but deliberately NOT default,
# because these read old fields that a long-lived vault can already hold a large
# backlog of. See `audit.EPISTEMIC_REVIEW_CATEGORIES` for why their sibling
# `prediction_window` sits in the default union above instead.
ATTENTION_CATEGORIES: tuple[str, ...] = (
    *DEFAULT_ATTENTION_CATEGORIES,
    *audit_module.TYPED_SEMANTIC_CATEGORIES,
    *audit_module.EPISTEMIC_REVIEW_CATEGORIES,
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
    # Component fingerprints of signals that share this item's identity but are
    # NOT folded into `categories`/`fingerprint` above, because they come from
    # registered-but-opt-in queues the default surface does not show. Populated
    # only by `item_by_ref`, never projected onto the wire (`as_dict`), and used
    # solely so a triage decision quiets everything the ref actually names.
    triage_components: list[str] | None = None
    # The family disposition that put this item on an EXPLICITLY requested
    # surface it would otherwise have left. Present only where it explains the
    # item's presence, so an absence never has to be read as "normal".
    disposition: str | None = None
    # When this signal first reached a served surface, from the first-surfaced
    # ledger. Absent until the ledger holds a record — it is never backfilled.
    first_surfaced_at: str | None = None

    def as_dict(self) -> dict:
        out = {
            "path": self.path,
            "score": self.score,
            "severity": self.severity,
            "categories": self.categories,
            "reasons": self.reasons,
            "proposed_fix": self.proposed_fix,
        }
        if self.disposition is not None:
            out["disposition"] = self.disposition
        if self.first_surfaced_at is not None:
            out["first_surfaced_at"] = self.first_surfaced_at
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

    def _anchor(finding: AuditFinding) -> str:
        partition = str((finding.meta or {}).get("review_partition") or "")
        return f"{finding.path}\0{partition}" if partition else finding.path
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
            result_lists.append([_anchor(f) for f in per_cat[c]])
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
    display_path: dict[str, str] = {}
    for c in category_order:
        if c not in selected:
            continue
        for rank, f in enumerate(per_cat[c], start=1):
            anchor = _anchor(f)
            display_path[anchor] = f.path
            reasons_by_path.setdefault(anchor, []).append(_reason(c, rank, f))
            severity_by_path[anchor] = max(
                severity_by_path.get(anchor, 0), _SEVERITY_RANK.get(f.severity, 0)
            )

    # Fold each path's UNPARTITIONED signals into that path's partitioned items.
    #
    # A partitioned finding anchors on `path\0partition` and shares no key with
    # the same page's page-level findings, so without this the note takes TWO
    # rows of the surface and its RRF votes never sum — breaking the additivity
    # property that is the whole point of composing queues. That was latent
    # while `bridge_review` was the only partitioned queue (it anchors on
    # governance grant paths, which no page-level queue flags); `prediction_window`
    # made it routine, because predictions live on exactly the insight and
    # research-note pages `stale_review` and `relation_debt` already flag.
    #
    # Folding rather than dropping the partition keeps both properties: one row
    # per note when it has one due prediction, and still N independently
    # triageable rows when it has N — each carrying the page's context and its
    # vote. `_apply_review_state` derives identity from the single partition on
    # an item's reasons, and a folded page-level reason carries none, so review
    # identities are unaffected.
    partitioned_by_path: dict[str, list[str]] = {}
    for anchor in scores:
        base, separator, partition = anchor.partition("\0")
        if separator and partition:
            partitioned_by_path.setdefault(base, []).append(anchor)
    for base, anchors in partitioned_by_path.items():
        if base not in scores:
            continue  # no page-level signal on this path — nothing to fold
        base_score = scores.pop(base)
        base_reasons = reasons_by_path.pop(base, [])
        base_severity = severity_by_path.pop(base, 0)
        display_path.pop(base, None)
        for anchor in anchors:
            scores[anchor] += base_score
            reasons_by_path[anchor].extend(base_reasons)
            severity_by_path[anchor] = max(severity_by_path[anchor], base_severity)

    # Order: score desc, then category preference of the item's best reason, then path.
    ordered = sorted(
        scores,
        key=lambda p: (
            -scores[p],
            min(category_rank[r["category"]] for r in reasons_by_path[p]),
            display_path[p],
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
            path=display_path[p],
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
    record_surfacing: bool = True,
) -> AttentionReport:
    """Compose the selected epistemic queues into one ranked review surface. Read-only.

    `record_surfacing=False` is for callers that list in order to LOOK SOMETHING
    UP rather than to show a review surface — `item_by_ref` resolves one
    reference by scanning every queue at `state="all"`, and stamping that scan
    would record a first surfacing for every item in the vault on a request that
    shows exactly one. The first-surfaced ledger measures what reached a person.

    Runs a single `audit` pass over the selected categories, then ranks/dedups via
    `_rank`. `today` is threaded through for deterministic ACT-R dormancy in tests.
    Defaults to `DEFAULT_ATTENTION_CATEGORIES`; the opt-in registered categories
    in `ATTENTION_CATEGORIES` are reachable only by naming them.
    """
    resolved = set(DEFAULT_ATTENTION_CATEGORIES) if not categories else set(categories)
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
    state_payload = _review_state_payload(vault_root)
    excluded, annotations = _excluded_families(
        state_payload,
        requested=(set(categories) if categories else None),
        state=state,
    )
    report = audit_module.audit(vault_root, categories=sorted(resolved), today=today)
    # BEFORE fusion, deliberately. Dropping an excluded family's reasons at the
    # report edge would leave its RRF votes in the scores, so an item flagged
    # only by a quiet family would still occupy a row with no reason on it, and
    # a doubly-flagged item would keep a rank it earned from a signal the user
    # asked not to hear about.
    findings = [f for f in report.findings if f.category not in excluded]
    ranked = _rank(findings, categories=resolved, limit=0)
    return _apply_review_state(
        vault_root,
        ranked,
        state=state,
        limit=limit,
        today=today,
        payload=state_payload,
        annotations=annotations,
        record_surfacing=record_surfacing,
    )


def _review_state_payload(vault_root: Path) -> dict:
    """The review state. Raises `REVIEW_STATE_INVALID` rather than answering empty.

    Loaded once per request so the disposition filter, the state view and the
    ledger annotation all read the same snapshot. It deliberately does NOT
    swallow the failure: this payload is what tells attention which items were
    dismissed, and an unreadable store answered as an empty one reports every
    standing dismissal in the vault as open — the precise regression this slice
    exists to prevent, delivered silently. The LEDGER write is best-effort
    (losing a measurement is cheap); the DECISION read is not, and never was.
    """
    return review_state_module.ReviewStateStore(vault_root).load()


def _excluded_families(
    payload: dict,
    *,
    requested: set[str] | None,
    state: str,
) -> tuple[frozenset[str], dict[str, str]]:
    """``(families to drop, family -> disposition to annotate)`` for one request.

    Named and separate so it is a mechanism a test can remove: patching it to
    return nothing must put every quieted family straight back on the surface,
    which is what makes the filter's presence provable rather than asserted.

    The asymmetry between the default union and an explicit request is the whole
    point of `quiet` versus prominence `off`. The daily surface honours the
    decision without argument; asking for the category by name is asking anyway,
    and the answer says so with an annotation rather than pretending the family
    is clean.
    """
    dispositions = review_state_module.disposition_map(payload)
    if not dispositions:
        return frozenset(), {}
    explicit = requested is not None
    excluded: set[str] = set()
    annotated: dict[str, str] = {}
    for family, disposition in dispositions.items():
        if explicit and family in requested:
            if disposition == "quiet" or state == "all":
                annotated[family] = disposition
                continue
        excluded.add(family)
    return frozenset(excluded), annotated


def activation(
    vault_root: Path,
    *,
    categories: list[str] | None = None,
    limit: int = 25,
    today=None,
    state: str = "open",
    record_surfacing: bool = True,
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
    # Activation is a SERVED review surface, not an internal measurement: it
    # ranks, it is reachable through `review_memory`, and it shares
    # `_apply_review_state` — so it also shares the ledger write. A disposition
    # therefore binds it exactly as it binds attention, and for the same two
    # reasons: a silenced family must not come back on another surface, and the
    # ledger must not record a first surfacing for a signal nobody was shown.
    # Filtering before `_rank` for the same reason attention does: an excluded
    # family's votes would otherwise still move a wanted item's position.
    state_payload = _review_state_payload(vault_root)
    excluded, annotations = _excluded_families(
        state_payload,
        requested=(set(categories) if categories else None),
        state=state,
    )
    scan = activation_module.scan(vault_root)
    findings = [f for f in scan.findings if f.category not in excluded]
    ranked = _rank(
        findings,
        categories=resolved,
        limit=0,
        category_order=activation_module.ACTIVATION_CATEGORIES,
        proposed_fix=_ACTIVATION_FIX,
    )
    ranked.coverage = scan.coverage
    return _apply_review_state(
        vault_root,
        ranked,
        state=state,
        limit=limit,
        today=today,
        identity_namespace="activation",
        payload=state_payload,
        annotations=annotations,
        record_surfacing=record_surfacing,
    )


#: What a ref may be resolved against when the default union does not hold it.
#:
#: The default union plus the registered-but-opt-in epistemic queues — exactly the
#: categories a due-state count can hand a reference out for, and deliberately NOT
#: `audit.ALL_CATEGORIES`, which is full of expensive structural checks that were
#: never review items. Being opt-in is a statement about what belongs on the daily
#: surface; it was never meant to be a statement about what a user is allowed to
#: put down.
_TRIAGEABLE_CATEGORIES: tuple[str, ...] = (
    *DEFAULT_ATTENTION_CATEGORIES,
    *audit_module.EPISTEMIC_REVIEW_CATEGORIES,
)


def _item_by_ref_fallback(
    vault_root: Path, wanted: str, *, today=None
) -> AttentionItem | None:
    """Resolve one ref over the default union PLUS the opt-in epistemic queues.

    Separate and named so it is a mechanism a test can remove.

    Two distinct failures live behind one ref, and both are this queue's problem:

    1. The default union holds NO item at that identity — a `question_aging` item,
       whose partition puts it on an id nothing else occupies. `triage_memory`
       raised `REVIEW_ITEM_NOT_FOUND`.
    2. The default union holds a DIFFERENT item at that identity — an
       `unfinished_experiments` page with no wikilinks also earns `relation_debt`,
       which carries no partition, so both land on the bare `target_ref` id. Triage
       "succeeded" and dismissed the relation-debt signal instead, leaving the
       count exactly where it was and quietly putting down something else.

    The second is why this is consulted even when the default union hits: the
    caller keeps the default item (identity untouched, see `item_by_ref`) and only
    borrows the component fingerprints it could not see.
    """
    report = attention(
        vault_root,
        categories=list(_TRIAGEABLE_CATEGORIES),
        limit=0,
        state="all",
        today=today,
        # A lookup, not a served surface: nothing here is shown to anybody.
        record_surfacing=False,
    )
    for item in report.items:
        if item.item_id == wanted:
            return item
    return None


def item_by_ref(
    vault_root: Path,
    reference: str,
    *,
    expected_fingerprint: str | None = None,
    today=None,
) -> AttentionItem:
    """Resolve one current review item by its stable review reference.

    The default union is searched first, then activation, and only then the opt-in
    epistemic queues. That order is the contract, not an optimisation: a counter
    that hands out a reference an agent is told to act on must hand out one the
    triage surface can resolve, and a `question_aging` or `unfinished_experiments`
    item was reachable by no path at all — read on every carrier, impossible to
    dismiss. Widening the search only after a miss buys that without moving a
    single existing item's identity.
    """
    wanted = review_state_module.parse_review_ref(reference)
    found: AttentionItem | None = None
    for resolver in (attention, activation):
        # A scan to resolve ONE reference. Stamping it would record a first
        # surfacing for every item in the vault on a request that shows one.
        report = resolver(
            vault_root, limit=0, state="all", today=today, record_surfacing=False
        )
        for item in report.items:
            if item.item_id == wanted:
                found = item
                break
        if found is not None:
            break

    wider = _item_by_ref_fallback(vault_root, wanted, today=today)
    if found is None:
        if wider is None:
            raise ValueError(
                f"REVIEW_ITEM_NOT_FOUND: no current review item for {reference}"
            )
        return wider

    # The default union answered, so its item is the answer — same `item_id`,
    # same `fingerprint`, same `categories` as before this fallback existed. The
    # wider view only contributes the component identities the default surface
    # cannot see, so a decision on this ref also quiets the opt-in signal that
    # shares it. One extra pass on an explicit, infrequent triage call.
    if wider is not None and wider is not found:
        extra = [
            value
            for value in review_state_module.component_fingerprints(vault_root, wider)
            if value != found.fingerprint
        ]
        if extra:
            found.triage_components = extra
    return found


def _stamp_first_surfaced(
    vault_root: Path, items: list[AttentionItem], *, known: dict | None = None
) -> None:
    """Record, and then annotate, the first time each RETURNED item was shown.

    Only the items this report actually returns: an item the state view filtered
    out, or one an excluded family removed before fusion, was never composed
    onto a served surface, and a ledger that recorded it would be measuring the
    runtime's internals rather than what a person saw.

    Egress is consulted first, and separately from the report. Attention itself
    is not the disclosure boundary — the governance plane projects this payload
    after it is returned — so without this the ledger would record a page the
    requesting audience is about to be told nothing about. It fails CLOSED:
    a release plane that cannot decide records nothing, because a ledger entry
    is cheap to miss and impossible to unsee.
    """
    if not items:
        return
    entries = _recordable(vault_root, items)
    if not entries:
        return
    stamps = review_state_module.record_surfaced(
        vault_root, entries, surface="review", known=known
    )
    for item in items:
        value = stamps.get(f"{item.item_id}:{item.fingerprint}")
        if value:
            item.first_surfaced_at = value


def _recordable(vault_root: Path, items: list[AttentionItem]) -> list[tuple[str, str]]:
    """The `(item_id, fingerprint)` pairs this audience may have a ledger row for.

    The egress consult runs inside its OWN disclosure boundary, mirroring
    `due_state.block_for_write` and for the same reason: `release_walk_filter`
    records one decision per page it judges, and judging them inside an
    enclosing collector would attach N pages the caller never touched to that
    caller's governance receipt. A write's receipt would then list every item
    on the review surface. Those decisions are real and are collected; they
    belong to this ledger write rather than to whatever ran around it.
    """
    from .governance import egress as egress_module

    with egress_module.disclosure_boundary(Path(vault_root), "review_ledger"):
        keep = _egress_keep(vault_root)
        return [
            (item.item_id, item.fingerprint)
            for item in items
            if item.item_id
            and item.fingerprint
            and (keep is None or keep(item.path))
        ]


def _egress_keep(vault_root: Path):
    """The release filter for this audience, or `None` when nothing to record.

    `None` means "record nothing", which is the fail-closed direction. An
    unconfigured vault takes the release plane's own empty-policy fast path and
    returns a filter that keeps everything, so the ordinary case costs one call.
    """
    try:
        from .governance import egress as egress_module

        return egress_module.release_walk_filter(Path(vault_root))
    except Exception:  # noqa: BLE001 — an undecidable release plane records nothing
        log.debug("release filter unavailable; not recording first-surfaced", exc_info=True)
        return lambda _path: False


def _apply_review_state(
    vault_root: Path,
    report: AttentionReport,
    *,
    state: str,
    limit: int,
    today=None,
    identity_namespace: str | None = None,
    payload: dict | None = None,
    annotations: dict[str, str] | None = None,
    record_surfacing: bool = True,
) -> AttentionReport:
    all_paths: list[str] = []
    for item in report.items:
        all_paths.append(item.path)
        for reason in item.reasons:
            all_paths.extend(reason.get("related_paths") or [])
    refs = review_state_module.refs_for_paths(vault_root, all_paths)
    store = review_state_module.ReviewStateStore(vault_root)
    state_payload = payload if payload is not None else store.load()
    annotations = annotations or {}
    state_summary = {state: 0 for state in review_state_module.VALID_STATES}

    for item in report.items:
        target_ref = refs[item.path]
        identity = (
            f"{identity_namespace}:{target_ref}"
            if identity_namespace
            else target_ref
        )
        partitions = {
            str((reason.get("meta") or {}).get("review_partition") or "")
            for reason in item.reasons
            if (reason.get("meta") or {}).get("review_partition")
        }
        if len(partitions) == 1:
            identity = f"{identity}:{next(iter(partitions))}"
        review_id = review_state_module.item_id(identity)
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
        if effective == "open":
            # No item-level decision APPLIES (none recorded, or a snooze that has
            # since lapsed), so a competing-alternatives stance recorded on the
            # contradiction pair itself still governs. The item-level record is
            # checked first because it is the more specific decision about this
            # exact signal composite.
            # Annotating here also makes a partially-stanced item legible: a reader
            # sees which reason is already dispositioned, and the `pair_ref` that
            # addresses it, instead of an ordinary-looking open item.
            stances = contradiction_stance_module.annotate_reasons(
                vault_root,
                item.reasons,
                store=store,
                payload=state_payload,
                refs=refs,
            )
            # EVERY conflict on the anchor must be dispositioned before the item is:
            # one un-stanced rival — a newly drifted pair, say — is still open
            # review work, so the item honestly reopens.
            if stances and all(stance is not None for stance in stances.values()):
                effective = "competing"
                decision = stances[min(stances)]
        item.item_id = review_id
        item.ref = review_state_module.review_ref(review_id)
        item.target_ref = target_ref
        item.related_refs = related_refs
        item.fingerprint = signal_fingerprint
        item.state = effective
        item.state_detail = decision.as_dict() if decision is not None else None
        for category in item.categories:
            if category in annotations:
                item.disposition = annotations[category]
                break
        state_summary[effective] += 1

    if state == "all":
        eligible = list(report.items)
    else:
        eligible = [item for item in report.items if item.state == state]
    shown_items = eligible[:limit] if limit > 0 else eligible
    if record_surfacing:
        _stamp_first_surfaced(vault_root, shown_items, known=payload)
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
