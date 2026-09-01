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
from . import graph_sync as graph_sync_module
from . import relation_registry, semantic_language_registry, semantic_units
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
    source_page: Any | None = None


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
    document = semantic_units.parse_semantic_units(
        page.body,
        validate=False,
        language_registry=semantic_language_registry.load_registry(vault_root),
        relation_registry=relation_registry.load_registry(vault_root),
        page_type=page.page_type,
    )
    authored: set[tuple[str, str]] = set()
    for relation in document.canonical_note_relations:
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
        # Raw bytes, not `read_text`: `content_hash` is defined as the sha256
        # of a file's full raw text and `get_page` hands out exactly that, so a
        # hash taken over the newline-normalized form disagrees with it on every
        # CRLF page -- and the caller echoes this one straight back into
        # `edit(expected_hash=...)`, which then refuses the write.
        return vault_module.content_hash(page.path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError):
        return ""


#: How far past the display budget candidate generation reaches, so that
#: classification has something to discard.
#:
#: `suggest_relations` truncates at the limit it is given, and the queue only
#: then drops the already-authored, placeholder-target and already-decided
#: candidates — so those drops used to come out of the display budget. A page
#: whose first `limit_per_page` candidates were all authored produced an empty
#: group, and the genuinely open candidates behind them never surfaced on any
#: read, however many times the queue was rebuilt.
#:
#: Bounded rather than unlimited because generation includes embedding
#: proximity, which is the expensive generator; the ceiling stops an adversarial
#: page from turning one queue read into a full-corpus scoring pass.
_CLASSIFICATION_HEADROOM = 4
_MAX_GENERATED_PER_PAGE = 64


def _page_candidates(
    vault_root: Path, page: Any, *, limit_per_page: int
) -> list[dict[str, Any]]:
    """Re-derive one source's deterministic candidate neighborhood.

    This is the decision-time compatibility leaf, not queue assembly.  It may
    inspect the one hinted page and bounded graph neighborhoods, but deliberately
    excludes embedding proximity.  Explicit per-page ``suggest_relations`` keeps
    that discovery method; relation review decisions never recompute it.
    """
    budget = max(0, int(limit_per_page))
    generated = [
        *epistemic_graph_module._structural_candidates(vault_root, page.rel_path),
        *epistemic_graph_module._wikilink_candidates(
            vault_root, page.body, page.rel_path
        ),
        *epistemic_graph_module._frontmatter_source_candidates(page),
        *epistemic_graph_module._shared_source_candidates(
            vault_root, page.rel_path
        ),
    ]
    return epistemic_graph_module._dedupe_candidates(generated)[
        : min(_MAX_GENERATED_PER_PAGE, budget * _CLASSIFICATION_HEADROOM)
    ]


def _page_for(vault_root: Path, rel_path: str) -> Any | None:
    """Re-read one page fresh from disk, for accept's live re-validation.

    Deliberately bypasses any process-level cache: accept must judge the
    candidate against the file's CURRENT state, not a read from earlier in
    this call (or an earlier request). Returns `None` if the page is gone.
    """
    try:
        path, _rel = vault_module.resolve_under_vault(
            Path(vault_root),
            str(rel_path or ""),
            must_exist=True,
            must_be_file=True,
            must_be_under_kb=True,
        )
    except vault_module.VaultPathError:
        return None
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
    """Assemble one bounded graph-native relation-acceptance queue."""
    vault_root = Path(vault_root)
    batch = epistemic_graph_module.EpistemicGraphIndex(
        vault_root
    ).relation_review_batch(
        limit_pages=limit_pages,
        limit_per_page=limit_per_page,
    )
    status = str(batch.get("status") or "warming")
    if status != "available":
        if not epistemic_graph_module.graph_enabled():
            status = "unavailable"
        else:
            sync_state = str(graph_sync_module.status(vault_root).get("state") or "")
            if sync_state == "recovery_required":
                status = "pending"
            elif sync_state == "unavailable":
                status = "unavailable"
            elif status not in {"warming", "pending", "unavailable"}:
                status = "warming"
        return {
            **batch,
            "status": status,
            "mode": "relation-queue",
            "mutated": False,
            "groups": [],
            "shown": 0,
            "pages_shown": 0,
            "retryable": True,
            "retry_after": "graph-current",
            "next_action": "retry-relation-queue",
        }

    groups: list[dict[str, Any]] = []
    for raw_group in batch.get("groups") or []:
        group = dict(raw_group)
        source_path = str(group.get("source_path") or group.get("path") or "")
        source_hash = str(
            group.get("source_content_hash") or group.get("content_hash") or ""
        )
        group["source_path"] = source_path
        group["source_content_hash"] = source_hash
        items: list[dict[str, Any]] = []
        for raw_item in group.get("items") or []:
            item = dict(raw_item)
            item["source_path"] = str(item.get("source_path") or source_path)
            item["source_content_hash"] = str(
                item.get("source_content_hash") or source_hash
            )
            items.append(item)
        group["items"] = items
        groups.append(group)
    return {
        **batch,
        "status": "available",
        "mode": "relation-queue",
        "mutated": False,
        "groups": groups,
        "retryable": False,
    }


def _refresh_required(ref: str) -> ValueError:
    return ValueError(
        "REVIEW_REFRESH_REQUIRED: the relation candidate is not present in the "
        f"bounded current queue prefix; refresh the queue and inspect {ref} again"
    )


def resolve_candidate(
    vault_root: Path,
    ref: str,
    *,
    source_path: str | None = None,
) -> ResolvedCandidate:
    """Resolve one current candidate from a hint or the bounded legacy prefix."""
    vault_root = Path(vault_root)
    wanted = parse_relation_review_ref(ref)
    if source_path is None:
        queue = build_queue(
            vault_root,
            limit_pages=_DEFAULT_LIMIT_PAGES,
            limit_per_page=_DEFAULT_LIMIT_PER_PAGE,
        )
        if queue.get("status") != "available":
            raise _refresh_required(ref)
        for group in queue.get("groups") or []:
            for item in group.get("items") or []:
                if str(item.get("review_id") or "") != wanted:
                    continue
                candidate = {
                    key: item.get(key)
                    for key in (
                        "from",
                        "to",
                        "relation_type",
                        "method",
                        "evidence",
                    )
                }
                return ResolvedCandidate(
                    review_id=str(item["review_id"]),
                    ref=str(item["ref"]),
                    fingerprint=str(item["fingerprint"]),
                    candidate=candidate,
                    target_ref=str(item["target_ref"]),
                )
        raise _refresh_required(ref)

    rel_path = epistemic_graph_module._with_md(str(source_path or ""))
    if not epistemic_graph_module.EpistemicGraphIndex(vault_root).available():
        raise _refresh_required(ref)
    page = _page_for(vault_root, rel_path)
    if page is None or not activation_module._eligible(vault_root, page):
        raise _refresh_required(ref)
    for candidate in _page_candidates(
        vault_root, page, limit_per_page=_DEFAULT_LIMIT_PER_PAGE
    ):
        if str(candidate.get("from") or page.rel_path) != rel_path:
            continue
        if _candidate_identity(candidate) != wanted:
            continue
        enriched = _enrich(vault_root, page, candidate)
        return ResolvedCandidate(
            review_id=enriched["review_id"],
            ref=enriched["ref"],
            fingerprint=enriched["fingerprint"],
            candidate=candidate,
            target_ref=enriched["target_ref"],
            source_page=page,
        )
    raise _refresh_required(ref)


def triage(
    vault_root: Path,
    *,
    ref: str,
    action: str,
    until: str | None = None,
    why: str | None = None,
    expected_fingerprint: str | None = None,
    source_path: str | None = None,
) -> dict[str, Any]:
    """Persist a fingerprint-bound dismiss/snooze/reopen for a relation candidate."""
    resolved = resolve_candidate(vault_root, ref, source_path=source_path)
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
    source_path: str | None = None,
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
    resolved = resolve_candidate(vault_root, ref, source_path=source_path)
    if resolved.fingerprint != expected_fingerprint:
        raise ValueError(
            "REVIEW_ITEM_CHANGED: the relation candidate signal changed; refresh "
            f"the queue and inspect {ref} again"
        )
    candidate = resolved.candidate
    page = resolved.source_page or _page_for(
        vault_root, str(candidate.get("from") or "")
    )
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
