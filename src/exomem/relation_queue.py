"""Batched, fingerprint-guarded relation-acceptance queue.

Assembles deterministic `suggest_relations` candidates across the
activation-eligible corpus into a read-only review batch. Nothing here mutates
the vault: candidates are proposed, filtered at read time, and only a separate
governed accept (`accept`) or triage decision writes anything.

Identity and fingerprints reuse the existing review-state machinery
(`review_state`) so decisions key on `review_id:signal_fingerprint` exactly like
the activation and attention queues. Relation refs are namespaced under
`exomem://review/relation/<id>` so they never resolve — and are never resolved
by — activation or attention items (the #198 isolation rule).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import activation as activation_module
from . import epistemic_graph as epistemic_graph_module
from . import find as find_module
from . import markdown_relations
from . import review_state as review_state_module
from . import vault as vault_module
from .vault import kb_root

RELATION_REVIEW_PREFIX = "exomem://review/relation/"
_DEFAULT_LIMIT_PAGES = 50
_DEFAULT_LIMIT_PER_PAGE = 10


@dataclass(frozen=True)
class ResolvedCandidate:
    review_id: str
    ref: str
    fingerprint: str
    candidate: dict[str, Any]
    target_ref: str


def is_relation_ref(value: str) -> bool:
    return str(value or "").strip().startswith(RELATION_REVIEW_PREFIX)


def relation_review_ref(review_id: str) -> str:
    clean = str(review_id or "").strip().lower()
    if len(clean) != 24 or any(char not in "0123456789abcdef" for char in clean):
        raise ValueError(
            f"INVALID_REVIEW_REFERENCE: invalid relation review id {review_id!r}"
        )
    return f"{RELATION_REVIEW_PREFIX}{clean}"


def parse_relation_review_ref(value: str) -> str:
    raw = str(value or "").strip()
    if not raw.startswith(RELATION_REVIEW_PREFIX):
        raise ValueError(
            f"INVALID_REVIEW_REFERENCE: expected {RELATION_REVIEW_PREFIX}<id>"
        )
    raw_id = raw[len(RELATION_REVIEW_PREFIX) :].lower()
    if len(raw_id) != 24 or any(char not in "0123456789abcdef" for char in raw_id):
        raise ValueError(
            f"INVALID_REVIEW_REFERENCE: invalid relation review reference {value!r}"
        )
    return raw_id


def _candidate_identity(candidate: dict[str, Any]) -> str:
    payload = "|".join(
        str(candidate.get(key) or "")
        for key in ("from", "to", "relation_type", "method")
    )
    return review_state_module.item_id(f"relation:{payload}")


def _bullet(candidate: dict[str, Any]) -> str:
    relation = candidate.get("relation_type") or "relates_to"
    destination = str(candidate.get("to") or "").removesuffix(".md")
    return f"- {relation} [[{destination}]]"


def _evidence_signal_version(page: Any, candidate: dict[str, Any]) -> str:
    """A version string that changes whenever the candidate's evidence does.

    `review_state.fingerprint()` uses `meta.signal_version` verbatim as the
    reason's version WHENEVER it is supplied, ignoring `detail` entirely (see
    `review_state.fingerprint`). Folding in only the source page's own
    `activation._signal_version` (as the activation/attention queues do,
    since their findings are entirely about the source page) would miss
    candidate methods whose evidence is driven by a DIFFERENT page or the
    corpus index — `shared_sources` (a neighbour page's edge), and
    `embedding_proximity` (a corpus-wide cosine score) — so an edit to that
    OTHER page would never resurface a dismissed candidate. Hashing the
    source page's signal version together with the serialized evidence (and
    the candidate's own to/method/relation_type) means any of those changes
    is reflected, regardless of which page produced them.
    """
    payload = {
        "page_signal_version": activation_module._signal_version(page),
        "method": str(candidate.get("method") or ""),
        "relation_type": str(candidate.get("relation_type") or ""),
        "to": str(candidate.get("to") or ""),
        "evidence": candidate.get("evidence") or {},
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return vault_module.content_hash(encoded)[:16]


def _candidate_fingerprint(
    candidate: dict[str, Any],
    *,
    from_ref: str,
    to_ref: str,
    signal_version: str,
) -> str:
    reason = {
        "category": str(candidate.get("method") or ""),
        "detail": json.dumps(candidate.get("evidence") or {}, sort_keys=True),
        "related_paths": [str(candidate.get("to") or "")],
        "meta": {"signal_version": signal_version},
    }
    return review_state_module.fingerprint(
        target_ref=from_ref,
        categories=[str(candidate.get("relation_type") or "")],
        reasons=[reason],
        related_refs=[to_ref],
    )


def _authored_targets(page: Any, vault_root: Path) -> set[tuple[str, str]]:
    """Set of `(relation_type, target.md)` already authored under ``## Relations``."""
    document = markdown_relations.parse_markdown_relations(page.body)
    authored: set[tuple[str, str]] = set()
    for relation in document.canonical_relations:
        try:
            canonical, warning = vault_module.normalize_wikilink(
                relation.target, vault_root, strict=False
            )
        except Exception:  # noqa: BLE001 - malformed authored links are ignored
            continue
        if warning:
            continue
        authored.add((relation.kind, epistemic_graph_module._with_md(canonical)))
    return authored


def _is_placeholder_target(vault_root: Path, target: str) -> bool:
    return not (Path(vault_root) / epistemic_graph_module._with_md(target)).is_file()


def _eligible_pages(vault_root: Path) -> list[Any]:
    kb = kb_root(vault_root)
    if not kb.is_dir():
        return []
    pages: list[Any] = []
    for path in find_module._walk_md(kb):
        try:
            page = find_module._parse_page(path, path.stat().st_mtime, vault_root)
        except OSError:
            continue
        if page is None or not activation_module._eligible(vault_root, page):
            continue
        pages.append(page)
    return pages


def _ordered_pages(vault_root: Path, scan: Any) -> list[Any]:
    """Eligible pages ordered by activation rank, then path (deterministic)."""
    rank: dict[str, int] = {}
    for index, finding in enumerate(scan.findings):
        rank.setdefault(finding.path, index)
    pages = _eligible_pages(vault_root)
    return sorted(
        pages,
        key=lambda page: (rank.get(page.rel_path, len(scan.findings)), page.rel_path),
    )


def _page_content_hash(page: Any) -> str:
    try:
        return vault_module.content_hash(page.path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return ""


def _page_candidates(
    vault_root: Path, page: Any, *, limit_per_page: int
) -> list[dict[str, Any]]:
    proposal = epistemic_graph_module.suggest_relations(
        vault_root, path=page.rel_path, limit=limit_per_page
    )
    return list(proposal.get("candidates") or [])


def _page_for(vault_root: Path, rel_path: str) -> Any | None:
    """Re-read one page fresh from disk, for accept's live re-validation.

    Deliberately bypasses any process-level cache: accept must judge the
    candidate against the file's CURRENT state, not a read from earlier in
    this call (or an earlier request). Returns `None` if the page is gone.
    """
    path = Path(vault_root) / str(rel_path or "")
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return find_module._parse_page(path, mtime, Path(vault_root))


def _enrich(vault_root: Path, page: Any, candidate: dict[str, Any]) -> dict[str, Any]:
    from_path = str(candidate.get("from") or page.rel_path)
    to_path = str(candidate.get("to") or "")
    refs = review_state_module.refs_for_paths(vault_root, [from_path, to_path])
    review_id = _candidate_identity(candidate)
    fingerprint = _candidate_fingerprint(
        candidate,
        from_ref=refs.get(from_path, from_path),
        to_ref=refs.get(to_path, to_path),
        signal_version=_evidence_signal_version(page, candidate),
    )
    return {
        "review_id": review_id,
        "ref": relation_review_ref(review_id),
        "fingerprint": fingerprint,
        "from": from_path,
        "to": to_path,
        "relation_type": candidate.get("relation_type"),
        "method": candidate.get("method"),
        "evidence": candidate.get("evidence") or {},
        "bullet": _bullet(candidate),
        "target_ref": refs.get(from_path, from_path),
        "state": "open",
    }


def _classify_candidate(
    vault_root: Path,
    page: Any,
    candidate: dict[str, Any],
    *,
    store: review_state_module.ReviewStateStore,
    state_payload: dict[str, Any],
    authored: set[tuple[str, str]] | None = None,
    today=None,
) -> tuple[str | None, dict[str, Any] | None]:
    """Apply the three read-time eligibility filters to one candidate.

    Shared by `build_queue` (batch read) and `accept` (single-candidate
    re-validation immediately before writing) so the two paths can never
    disagree about what counts as an open, acceptable candidate.

    Returns `(reason, enriched)`: `reason` is `None` when the candidate is
    open, else one of "authored_edge", "placeholder_target", "decided".
    `enriched` (the review-identity-bearing item) is computed only once the
    cheap authored/placeholder checks pass; it is `None` when filtered by
    either of those, since nothing downstream needs it.
    """
    if authored is None:
        authored = _authored_targets(page, vault_root)
    relation_type = str(candidate.get("relation_type") or "")
    to_path = str(candidate.get("to") or "")
    if (relation_type, epistemic_graph_module._with_md(to_path)) in authored:
        return "authored_edge", None
    if _is_placeholder_target(vault_root, to_path):
        return "placeholder_target", None
    enriched = _enrich(vault_root, page, candidate)
    effective, _decision = store.effective_state(
        enriched["review_id"],
        enriched["fingerprint"],
        today=today,
        payload=state_payload,
    )
    if effective != "open":
        return "decided", enriched
    return None, enriched


def build_queue(
    vault_root: Path,
    *,
    limit_pages: int = _DEFAULT_LIMIT_PAGES,
    limit_per_page: int = _DEFAULT_LIMIT_PER_PAGE,
    today=None,
) -> dict[str, Any]:
    """Assemble the deterministic, read-only relation-acceptance queue.

    Generation is lazy: candidate generation (`suggest_relations` per page,
    which can invoke embedding-proximity scoring) STOPS as soon as
    `limit_pages` groups with open items have been collected, so a small
    `limit_pages` never pays full-corpus generation cost. Because of that,
    per-call totals (`pages_scanned`, `filtered`, the `relation_*` coverage
    counters) describe only the scanned prefix, not the whole corpus — the
    activation denominators in `coverage` (from the cheap, model-free
    `activation.scan`) stay full-corpus as before, but `pages_truncated` /
    `pages_unscanned` / `coverage["relation_scan_complete"]` say explicitly
    when the relation-specific counts are partial rather than implying a
    full-corpus total that was never computed.
    """
    vault_root = Path(vault_root)
    scan = activation_module.scan(vault_root)
    store = review_state_module.ReviewStateStore(vault_root)
    state_payload = store.load()
    cap = max(0, int(limit_pages))

    filtered = {"authored_edge": 0, "placeholder_target": 0, "decided": 0}
    groups: list[dict[str, Any]] = []
    pages_scanned = 0
    scan_complete = True

    for page in _ordered_pages(vault_root, scan):
        if len(groups) >= cap:
            scan_complete = False
            break
        pages_scanned += 1
        authored = _authored_targets(page, vault_root)
        items: list[dict[str, Any]] = []
        for candidate in _page_candidates(
            vault_root, page, limit_per_page=limit_per_page
        ):
            reason, enriched = _classify_candidate(
                vault_root,
                page,
                candidate,
                store=store,
                state_payload=state_payload,
                authored=authored,
                today=today,
            )
            if reason is not None:
                filtered[reason] += 1
                continue
            items.append(enriched)
        if items:
            groups.append(
                {
                    "path": page.rel_path,
                    "title": page.title,
                    "content_hash": _page_content_hash(page),
                    "items": items,
                }
            )

    eligible_pages_total = int(scan.coverage.get("eligible_pages", 0))
    shown_items = sum(len(group["items"]) for group in groups)

    coverage = dict(scan.coverage)
    coverage["relation_pages_scanned"] = pages_scanned
    coverage["relation_candidate_pages_found"] = len(groups)
    coverage["relation_candidates_found"] = shown_items
    coverage["relation_scan_complete"] = scan_complete

    return {
        "mode": "relation-queue",
        "mutated": False,
        "groups": groups,
        "shown": shown_items,
        "pages_shown": len(groups),
        "pages_scanned": pages_scanned,
        "pages_truncated": not scan_complete,
        "pages_unscanned": max(0, eligible_pages_total - pages_scanned),
        "filtered": filtered,
        "coverage": coverage,
    }


def resolve_candidate(vault_root: Path, ref: str) -> ResolvedCandidate:
    """Re-derive a queue candidate from the live signal by its relation ref."""
    vault_root = Path(vault_root)
    wanted = parse_relation_review_ref(ref)
    scan = activation_module.scan(vault_root)
    for page in _ordered_pages(vault_root, scan):
        for candidate in _page_candidates(
            vault_root, page, limit_per_page=_DEFAULT_LIMIT_PER_PAGE
        ):
            if _candidate_identity(candidate) != wanted:
                continue
            enriched = _enrich(vault_root, page, candidate)
            return ResolvedCandidate(
                review_id=enriched["review_id"],
                ref=enriched["ref"],
                fingerprint=enriched["fingerprint"],
                candidate=candidate,
                target_ref=enriched["target_ref"],
            )
    raise ValueError(
        f"REVIEW_ITEM_NOT_FOUND: no current relation candidate for {ref}"
    )


def triage(
    vault_root: Path,
    *,
    ref: str,
    action: str,
    until: str | None = None,
    why: str | None = None,
    expected_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Persist a fingerprint-bound dismiss/snooze/reopen for a relation candidate."""
    resolved = resolve_candidate(vault_root, ref)
    if expected_fingerprint and resolved.fingerprint != expected_fingerprint:
        raise ValueError(
            "REVIEW_ITEM_CHANGED: the relation candidate signal changed; refresh "
            f"the queue and inspect {ref} again"
        )
    result = review_state_module.ReviewStateStore(vault_root).apply(
        resolved.review_id,
        resolved.fingerprint,
        action=action,
        until=until,
        why=why,
    )
    result["ref"] = ref
    result["path"] = resolved.candidate.get("from")
    result["target_ref"] = resolved.target_ref
    result["categories"] = [resolved.candidate.get("relation_type")]
    return result


def accept(
    vault_root: Path,
    *,
    ref: str,
    expected_hash: str | None,
    why: str | None,
    expected_fingerprint: str | None = None,
    edit_memory: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    """Governed server-side accept: validate signal + hash, then author the bullet.

    ``edit_memory`` is injected (the registry's ``op_edit_memory``) so the write
    is byte-identical in effect to the Studio single-proposal path.

    ``expected_fingerprint`` is REQUIRED, not merely checked when present: the
    spec requires accept to validate the candidate's fingerprint against the
    live signal, and an optional-by-omission check is skippable by any caller
    that simply doesn't send it.
    """
    if not why or not str(why).strip():
        raise ValueError(
            "INVALID_ACCEPT: accept-relation requires an audit reason (`why`)"
        )
    if not expected_hash:
        raise ValueError(
            "INVALID_ACCEPT: accept-relation requires `expected_hash` from the target page"
        )
    if not expected_fingerprint:
        raise ValueError(
            "INVALID_ACCEPT: accept-relation requires `expected_fingerprint` from the queue read"
        )
    vault_root = Path(vault_root)
    resolved = resolve_candidate(vault_root, ref)
    if resolved.fingerprint != expected_fingerprint:
        raise ValueError(
            "REVIEW_ITEM_CHANGED: the relation candidate signal changed; refresh "
            f"the queue and inspect {ref} again"
        )
    candidate = resolved.candidate
    page = _page_for(vault_root, str(candidate.get("from") or ""))
    if page is None:
        raise ValueError(
            "REVIEW_ITEM_CHANGED: the relation candidate's source page no longer "
            f"exists; refresh the queue and inspect {ref} again"
        )
    store = review_state_module.ReviewStateStore(vault_root)
    reason, _enriched = _classify_candidate(
        vault_root, page, candidate, store=store, state_payload=store.load()
    )
    if reason is not None:
        raise ValueError(
            "REVIEW_ITEM_CHANGED: the relation candidate is no longer eligible "
            f"({reason}); refresh the queue and inspect {ref} again"
        )
    bullet = _bullet(candidate)
    edit_result = edit_memory(
        vault_root,
        path=str(candidate.get("from") or ""),
        why=why,
        heading="Relations",
        section_position="append",
        new_string=bullet,
        expected_hash=expected_hash,
    )
    return {
        "accepted": True,
        "ref": ref,
        "path": candidate.get("from"),
        "from": candidate.get("from"),
        "to": candidate.get("to"),
        "relation_type": candidate.get("relation_type"),
        "method": candidate.get("method"),
        "fingerprint": resolved.fingerprint,
        "bullet": bullet,
        "edit": edit_result,
    }
