"""Corpus-aware writes: let the existing graph + embeddings inform authoring.

Today the write path is corpus-blind — every wikilink and source is caller-
supplied, so the dense link graph and the embedding sidecar contribute nothing
at authoring time. This module closes that loop using ONLY the existing retrieval
stack (find() + EmbeddingIndex), no new dependency and no server-side LLM:

- `suggest_related()` — given a draft (title + body), return ranked EXISTING
  pages it should probably link to, preferring graph hubs, excluding itself and
  anything already linked. Reuses find() wholesale, so it inherits graceful
  BM25/keyword degradation when embeddings are unavailable.
- `detect_duplicates()` — flag existing pages whose content is near-identical to
  a draft (cosine over the sidecar), so a new entry doesn't silently duplicate an
  old one. A WARNING, never a block — append-only + supersession invariants mean
  the client decides (edit/replace/append), we just make the overlap visible.
- `detect_contradictions()` — flag existing ACTIVE COMPILED notes whose content
  sits in the band *just below* the dup threshold (`[floor, dup_threshold)`):
  close enough to plausibly restate, refine, OR contradict, but not a duplicate.
  This is PROXIMITY measurement, not a stance judgment — the cosine band can't
  tell agreement from contradiction, so the warning surfaces the tension and the
  reader judges (and supersedes if it's a real conflict). Shares one embedding
  pass with `detect_duplicates` so a write encodes the draft only once.

ALTITUDE: everything here is *surfaced* (returned as structured suggestions /
warnings) for the client LLM to act on — never auto-injected into a body. The
user makes the call, so visibility beats silent graph mutation.
"""

from __future__ import annotations

import logging
import math
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from . import call_spans
from .kbdir import kb_prefix
from .vault import content_hash

log = logging.getLogger(__name__)

# Tunable knobs — intuition-seeded like find.RankingConfig; revisit against the
# eval harness (scripts/eval_retrieval.py) once a golden set exists. Kept here
# as named constants so they're one-line greppable.
HUB_WEIGHT = 0.15  # weight on log1p(graph_in_degree) when re-ranking suggestions
DUP_THRESHOLD = 0.90  # default min doc-doc cosine for a near-dup; override via EXOMEM_DUP_THRESHOLD
CONTRADICTION_FLOOR = 0.82  # default lower edge of the contradiction band [floor, dup_threshold); override via EXOMEM_CONTRADICTION_FLOOR
RELATED_OVERFETCH = 3  # fetch limit * this from find(), then re-rank + trim

# Lead-body word budget for the synthesized "what is this about" query.
_QUERY_LEAD_WORDS = 400
_WRITE_ADVISORY_NAMESPACE = "write-advisory"
_WRITE_ADVISORY_KINDS = frozenset({"near-duplicate", "overlap"})
_WRITE_ADVISORY_REF_PREFIX = f"exomem://review/{_WRITE_ADVISORY_NAMESPACE}/"
# Coupled to mutation_terminal._MAX_WARNING_CHARS: identity must survive compact
# projection, whose generic projector truncates warning strings from the right.
_WRITE_ADVISORY_WARNING_CHARS = 300
_WRITE_ADVISORY_FINGERPRINT_RE = re.compile(r"[0-9a-f]{24}")


def _dup_threshold() -> float:
    """DUP_THRESHOLD, overridable at runtime via EXOMEM_DUP_THRESHOLD.

    Lower = more near-dup warnings (0.86 was the old, looser default); higher =
    stricter (e.g. 0.93). Resolved per call so the env is read live, not frozen
    at import. Bad values fall back to the default with a logged warning.
    """
    raw = os.environ.get("EXOMEM_DUP_THRESHOLD")
    if raw is None:
        return DUP_THRESHOLD
    try:
        return float(raw)
    except ValueError:
        log.warning("invalid EXOMEM_DUP_THRESHOLD=%r; using %s", raw, DUP_THRESHOLD)
        return DUP_THRESHOLD


def _contradiction_floor() -> float:
    """CONTRADICTION_FLOOR, overridable at runtime via EXOMEM_CONTRADICTION_FLOOR.

    The lower edge of the contradiction band `[floor, dup_threshold)`. Pages this
    close to a draft (but not near-identical) often restate / refine / contradict
    it. Resolved per call so the env is read live, not frozen at import. Bad
    values fall back to the default with a logged warning.
    """
    raw = os.environ.get("EXOMEM_CONTRADICTION_FLOOR")
    if raw is None:
        return CONTRADICTION_FLOOR
    try:
        return float(raw)
    except ValueError:
        log.warning(
            "invalid EXOMEM_CONTRADICTION_FLOOR=%r; using %s", raw, CONTRADICTION_FLOOR
        )
        return CONTRADICTION_FLOOR


@dataclass
class RelatedSuggestion:
    path: str
    title: str
    type: str | None
    why: str
    excerpt: str

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "title": self.title,
            "type": self.type,
            "why": self.why,
            "excerpt": self.excerpt,
        }


@dataclass
class DupCandidate:
    path: str
    title: str
    cosine: float

    def as_dict(self) -> dict:
        return {"path": self.path, "title": self.title, "cosine": self.cosine}


@dataclass(frozen=True)
class WriteAdvisoryIdentity:
    """Stable, triageable identity for one write-time corpus advisory."""

    kind: str
    review_id: str
    ref: str
    fingerprint: str


def _advisory_path(
    vault_root: Path,
    path: str,
    *,
    require_file: bool = True,
) -> str:
    clean = str(path or "").replace("\\", "/").lstrip("/")
    if not clean:
        raise ValueError("write advisory endpoint path is required")
    if not clean.lower().endswith(".md"):
        clean = f"{clean}.md"
    root = Path(vault_root)
    if not (root / clean).is_file() and not clean.startswith(kb_prefix()):
        clean = f"{kb_prefix()}{clean}"
    if require_file and not (root / clean).is_file():
        raise ValueError(f"write advisory endpoint is unreadable: {path}")
    return clean


def write_advisory_ref(review_id: str) -> str:
    """Render the dedicated public ref namespace for write advisories."""
    from . import review_state

    # Reuse the review-id validator without exposing a generic queue ref.
    review_state.review_ref(review_id)
    return f"{_WRITE_ADVISORY_REF_PREFIX}{review_id}"


def is_write_advisory_ref(value: str) -> bool:
    return str(value or "").strip().lower().startswith(_WRITE_ADVISORY_REF_PREFIX)


def parse_write_advisory_ref(value: str) -> str:
    from . import review_state

    raw = str(value or "").strip()
    if not raw.lower().startswith(_WRITE_ADVISORY_REF_PREFIX):
        raise ValueError(
            "INVALID_REVIEW_REFERENCE: expected "
            f"{_WRITE_ADVISORY_REF_PREFIX}<id>"
        )
    return review_state.parse_review_ref(
        f"{review_state.REVIEW_PREFIX}{raw[len(_WRITE_ADVISORY_REF_PREFIX):]}"
    )


def write_advisory_identity(
    vault_root: Path,
    *,
    kind: str,
    self_path: str,
    candidate: DupCandidate,
    refs: dict[str, str] | None = None,
    candidate_signal_version: str | None = None,
) -> WriteAdvisoryIdentity:
    """Derive one pair-stable review identity from refs and counterpart content.

    The payload intentionally mirrors queue review fingerprints: immutable endpoint
    refs plus the counterpart's normalized signal version, rather than ranking score,
    raw file bytes, write time, or the triggering page's just-written content.
    """
    from . import contradiction_stance, review_state

    if kind not in _WRITE_ADVISORY_KINDS:
        raise ValueError(f"unknown write advisory kind: {kind}")
    root = Path(vault_root)
    self_rel = _advisory_path(root, self_path, require_file=refs is None)
    candidate_rel = _advisory_path(root, candidate.path)
    if refs is None or self_rel not in refs or candidate_rel not in refs:
        refs = review_state.refs_for_paths(root, [self_rel, candidate_rel])
    counterpart_signal = candidate_signal_version
    if counterpart_signal is None:
        counterpart_signal = contradiction_stance.page_signal_version(root, candidate_rel)
    if counterpart_signal is None:
        raise ValueError(f"write advisory counterpart is unreadable: {candidate.path}")
    left_ref, right_ref = sorted((str(refs[self_rel]), str(refs[candidate_rel])))
    category = f"{_WRITE_ADVISORY_NAMESPACE}:{kind}"
    signal_version = content_hash(
        f"{category}\n{left_ref}\n{right_ref}\n{counterpart_signal}"
    )[:16]
    review_id = review_state.item_id(f"{category}:{left_ref}|{right_ref}")
    fingerprint = review_state.fingerprint(
        target_ref=left_ref,
        categories=[category],
        reasons=[{"category": category, "meta": {"signal_version": signal_version}}],
        related_refs=[right_ref],
    )
    return WriteAdvisoryIdentity(kind, review_id, write_advisory_ref(review_id), fingerprint)


def _render_write_advisory(kind: str, candidate: DupCandidate) -> str:
    return dup_warning(candidate) if kind == "near-duplicate" else overlap_warning(candidate)


def _render_identified_write_advisory(
    kind: str,
    candidate: DupCandidate,
    identity: WriteAdvisoryIdentity,
    quiet_offer: dict | None = None,
) -> str:
    suffix = f" [review: {identity.ref}; fingerprint: {identity.fingerprint}]"
    prose = _render_write_advisory(kind, candidate)
    offer_clause = ""
    if quiet_offer:
        offer_clause = (
            " [quiet offer: ref="
            f"{quiet_offer['ref']}; action=quiet; reason required]"
        )
    budget = _WRITE_ADVISORY_WARNING_CHARS - len(suffix)
    prose_budget = budget - len(offer_clause)
    if len(prose) > prose_budget:
        prose = prose[: max(0, prose_budget - 1)].rstrip() + "…"
    return prose + offer_clause + suffix


def emit_write_advisories(
    vault_root: Path,
    *,
    self_path: str,
    kind: str,
    candidates: list[DupCandidate],
    apply_declared_pair_filter: bool = False,
) -> list[str]:
    """Render one advisory class after portable-state suppression, failing open."""
    return emit_write_advisory_groups(
        vault_root,
        self_path=self_path,
        groups=[(kind, candidates)],
        apply_declared_pair_filter=apply_declared_pair_filter,
    )


@dataclass(frozen=True)
class EmittedWriteAdvisory:
    """One surfaced write advisory: its rendered warning and its identity.

    `identity` is None exactly on the fail-open paths, where the warning is
    the unidentified prose the write path has always emitted when advisory
    state could not be read. A consumer that must address the advisory later
    (rather than print it now) has to treat that as unaddressable.
    """

    kind: str
    candidate: DupCandidate
    warning: str
    identity: WriteAdvisoryIdentity | None
    counterpart_rel_path: str | None


def emit_write_advisory_groups(
    vault_root: Path,
    *,
    self_path: str,
    groups: list[tuple[str, list[DupCandidate]]],
    apply_declared_pair_filter: bool = False,
) -> list[str]:
    """Render all advisory classes with one ref batch and one review-state read."""
    return [
        emitted.warning
        for emitted in emitted_write_advisory_groups(
            vault_root,
            self_path=self_path,
            groups=groups,
            apply_declared_pair_filter=apply_declared_pair_filter,
        )
    ]


def emitted_write_advisory_groups(
    vault_root: Path,
    *,
    self_path: str,
    groups: list[tuple[str, list[DupCandidate]]],
    apply_declared_pair_filter: bool = False,
    record_surfacing: bool = True,
) -> list[EmittedWriteAdvisory]:
    """The structured form of `emit_write_advisory_groups`, same order and text.

    Deferred advisory work needs the review identity beside each warning, not
    just the rendered string. Sharing this one body keeps the deterministic
    suppression, family disposition, quiet offer, and first-surfaced ledger
    exactly as the synchronous write path performs them.

    `record_surfacing=False` computes without committing the once-only
    first-surfaced ledger (and therefore without arming a quiet offer, which
    the inline path arms only when that ledger write persisted). It exists for
    a consumer whose candidate set may still be refused after it is computed:
    the ledger measures when a signal reached somebody, and a refused set
    reached nobody. Such a consumer commits the stamp with
    `record_write_advisory_surfacing` once its result is durable.
    """
    from . import contradiction_stance, review_state

    for kind, _candidates in groups:
        if kind not in _WRITE_ADVISORY_KINDS:
            raise ValueError(f"unknown write advisory kind: {kind}")
    advisories = [
        (kind, candidate)
        for kind, candidates in groups
        for candidate in candidates
    ]
    if not advisories:
        return []

    root = Path(vault_root)
    declared_pair = (
        contradiction_stance.DeclaredPairFilter(root, self_path)
        if apply_declared_pair_filter
        else None
    )
    eligible: list[tuple[str, DupCandidate, str]] = []
    warnings: list[EmittedWriteAdvisory] = []
    for kind, candidate in advisories:
        try:
            if declared_pair is not None and declared_pair(candidate.path):
                continue
            eligible.append((kind, candidate, _advisory_path(root, candidate.path)))
        except Exception as error:  # noqa: BLE001 — advisory state must fail open
            log.debug("write advisory suppression failed open: %s", error)
            warnings.append(
                EmittedWriteAdvisory(
                    kind=kind,
                    candidate=candidate,
                    warning=_render_write_advisory(kind, candidate),
                    identity=None,
                    counterpart_rel_path=None,
                )
            )

    if not eligible:
        return warnings

    try:
        self_rel = _advisory_path(root, self_path, require_file=False)
        paths = [
            self_rel,
            *(candidate_rel for _kind, _candidate, candidate_rel in eligible),
        ]
        refs = review_state.refs_for_paths(root, paths)
        counterpart_paths = dict.fromkeys(
            candidate_rel for _kind, _candidate, candidate_rel in eligible
        )
        signals = {
            candidate_rel: contradiction_stance.page_signal_version(root, candidate_rel)
            for candidate_rel in counterpart_paths
        }
        if any(signal is None for signal in signals.values()):
            raise ValueError("one or more write advisory counterparts are unreadable")
        store = review_state.ReviewStateStore(root)
        payload = store.load()
        excluded_kinds = _excluded_advisory_kinds(payload)
        emitted: list[tuple[str, DupCandidate, str, WriteAdvisoryIdentity]] = []
        surfaced: list[tuple[str, str, str]] = []
        for kind, candidate, candidate_rel in eligible:
            if kind in excluded_kinds:
                # The user said this KIND of advisory is noise in this vault.
                # Read from the same payload the per-item suppression uses, so
                # a store this path cannot read fails open for both together.
                continue
            identity = write_advisory_identity(
                root,
                kind=kind,
                self_path=self_rel,
                candidate=candidate,
                refs=refs,
                candidate_signal_version=signals[candidate_rel],
            )
            state, _decision = store.effective_state(
                identity.review_id,
                identity.fingerprint,
                payload=payload,
            )
            if state in {"dismissed", "snoozed"}:
                continue
            emitted.append((kind, candidate, candidate_rel, identity))
            surfaced.append((identity.review_id, identity.fingerprint, kind))
        ledgered = (
            _record_surfaced_advisories(root, surfaced, known=payload)
            if record_surfacing
            else False
        )
        for kind, candidate, candidate_rel, identity in emitted:
            offer = None
            if ledgered:
                try:
                    offer = store.arm_quiet_offer(kind, known=payload)
                except Exception as error:  # noqa: BLE001 — optional state fails open
                    log.debug("write advisory quiet offer failed open: %s", error)
            warnings.append(
                EmittedWriteAdvisory(
                    kind=kind,
                    candidate=candidate,
                    warning=_render_identified_write_advisory(
                        kind, candidate, identity, offer
                    ),
                    identity=identity,
                    counterpart_rel_path=candidate_rel,
                )
            )
    except Exception as error:  # noqa: BLE001 — advisory state must fail open
        log.debug("write advisory suppression failed open: %s", error)
        warnings.extend(
            EmittedWriteAdvisory(
                kind=kind,
                candidate=candidate,
                warning=_render_write_advisory(kind, candidate),
                identity=None,
                counterpart_rel_path=candidate_rel,
            )
            for kind, candidate, candidate_rel in eligible
        )
    return warnings


def record_write_advisory_surfacing(
    vault_root: Path,
    emitted: list[EmittedWriteAdvisory],
    *,
    known: dict | None = None,
) -> bool:
    """Stamp the first surfacing for advisories that actually reached someone.

    The deferred half of `emitted_write_advisory_groups(record_surfacing=False)`.
    Unidentified fail-open advisories carry no review identity and so cannot be
    ledgered, exactly as inline. Fails open like every other advisory-state
    write: an unwritable ledger records the entry on a later surfacing.
    """
    entries = [
        (item.identity.review_id, item.identity.fingerprint, item.kind)
        for item in emitted
        if item.identity is not None
    ]
    return _record_surfaced_advisories(Path(vault_root), entries, known=known)


def _excluded_advisory_kinds(payload: dict) -> frozenset[str]:
    """Advisory kinds a family disposition silences on the write path.

    Named and separate so it is a mechanism a test can remove. It reads the same
    review-state payload the per-item suppression already loaded, which is what
    keeps the fail-open posture identical: a store this path cannot read raises
    out of the caller's `try` and every advisory is emitted unfiltered.
    """
    from . import review_state

    return frozenset(
        family
        for family in review_state.disposition_map(payload)
        if family in _WRITE_ADVISORY_KINDS
    )


def _record_surfaced_advisories(
    vault_root: Path, entries: list[tuple[str, str, str]], *, known: dict | None = None
) -> bool:
    """Stamp the first surfacing of advisories that were actually emitted.

    Not the suppressed ones and not the disposition-excluded ones: the ledger
    measures when a signal reached somebody, and one that was filtered out
    reached nobody.
    """
    if not entries:
        return False
    from . import review_state

    try:
        _stamps, persisted = review_state.record_surfaced(
            vault_root, entries, surface="write", known=known, return_success=True
        )
        return persisted
    except Exception as error:  # noqa: BLE001 — advisory state must fail open
        log.debug("first-surfaced ledger not recorded for advisories: %s", error)
        return False


def detected_overlap_advisory_groups(
    candidates: list[DupCandidate],
) -> list[tuple[str, list[DupCandidate]]]:
    """Group proximity candidates into their one warning signal class.

    A single branch, deliberately: the partition that used to split a
    `contradiction-band` group off needed a stance judgment, and the write path
    no longer makes one. The band is a PROXIMITY measurement, so every candidate
    in it is the overlap kind. Kept as a named seam because the callers pass
    groups, not kinds.
    """
    return [("overlap", list(candidates))]


def triage_write_advisory(
    vault_root: Path,
    *,
    ref: str,
    action: str,
    until: str | None = None,
    why: str | None = None,
    expected_fingerprint: str | None = None,
) -> dict:
    """Record a decision for a surfaced write advisory without creating a queue item."""
    from . import review_state

    normalized = str(action or "").strip().lower()
    if normalized not in {"dismiss", "snooze", "reopen"}:
        raise ValueError(
            "INVALID_REVIEW_ACTION: write advisories accept dismiss, snooze, or reopen"
        )
    if normalized == "dismiss" and not str(why or "").strip():
        raise ValueError("INVALID_REVIEW_ACTION: write-advisory dismiss requires `why`")
    if (
        normalized != "reopen"
        and expected_fingerprint is not None
        and not _WRITE_ADVISORY_FINGERPRINT_RE.fullmatch(expected_fingerprint)
    ):
        raise ValueError(
            "INVALID_REVIEW_FINGERPRINT: expected exactly 24 lowercase hex characters"
        )
    review_id = parse_write_advisory_ref(ref)
    store = review_state.ReviewStateStore(vault_root)
    payload = store.load()
    if normalized == "reopen":
        # Reopen clears every historical fingerprint for the stable pair identity.
        result = store.apply(
            review_id,
            expected_fingerprint or "",
            action="reopen",
            until=until,
            why=why,
        )
    else:
        if not expected_fingerprint:
            raise ValueError(
                "INVALID_REVIEW_ACTION: write-advisory dismiss/snooze requires "
                "the surfaced fingerprint"
            )
        result = store.apply(
            review_id,
            expected_fingerprint,
            action=normalized,
            until=until,
            why=why,
            family=review_state.surfaced_family(payload, review_id, expected_fingerprint),
        )
    result["ref"] = write_advisory_ref(review_id)
    return result


def _canon(path: str) -> str:
    """Comparable key across find paths (with .md), sources (no .md), wikilinks."""
    p = (path or "").strip().replace("\\", "/").split("#", 1)[0].strip()
    if p.lower().endswith(".md"):
        p = p[:-3]
    if p.startswith(kb_prefix()):
        p = p[len(kb_prefix()):]
    return p.lower()


def _why(hit) -> str:
    """One-line rationale assembled from the hit's ranking signals."""
    bits: list[str] = []
    if hit.vector_rank:
        bits.append(f"semantic #{hit.vector_rank}")
    if hit.bm25_rank:
        bits.append(f"keyword #{hit.bm25_rank}")
    if hit.graph_in_degree:
        hub = " (hub)" if hit.graph_in_degree >= 3 else ""
        bits.append(f"{hit.graph_in_degree} shared link(s){hub}")
    return ", ".join(bits) or "related"


def _is_relation_target_eligible(vault_root: Path, rel_path: str) -> bool:
    """False for out-of-KB, readonly, or excluded targets.

    A `relates_to` suggestion has to be actionable: the reader can review and
    wire it in via `note()`/`edit()`. Read-only, excluded, and out-of-KB
    material (e.g. `find()`'s scope="kb" auto-widen reaching sibling trees
    like `Handbooks/`/`Reference/`) fails that bar — you can't act on the
    edge, so surfacing it just pollutes the graph (audit finding 2-02).
    """
    from . import access, recall_policy

    normalized = (rel_path or "").replace("\\", "/").lstrip("/")
    if not normalized.casefold().startswith(kb_prefix().casefold()):
        return False
    if not recall_policy.is_recall_candidate(vault_root, vault_root / normalized):
        return False
    return access.access_tier(vault_root, rel_path) not in (
        access.TIER_READONLY,
        access.TIER_EXCLUDED,
    )


def suggest_related(
    vault_root: Path,
    *,
    title: str,
    body: str,
    self_path: str | None = None,
    existing_links: set[str] | None = None,
    limit: int = 8,
    scope: str = "kb",
) -> list[RelatedSuggestion]:
    """Rank existing pages a draft should link to. Reuses find(); never writes.

    Excludes the draft itself (`self_path`) and anything in `existing_links`
    (cited sources + wikilinks already in the body). Re-ranks find()'s order
    with a small log-scaled graph-in-degree bonus so well-connected hubs float
    up — linking a hub compounds more than linking a leaf. Also excludes any
    hit that isn't a governed, in-KB target (see `_is_relation_target_eligible`)
    — `find()`'s auto-widen is correct for retrieval but wrong for a
    suggested edge, since you can't act on a read-only/out-of-KB link.
    """
    from . import find as find_module

    lead = " ".join((body or "").split()[:_QUERY_LEAD_WORDS])
    query = f"{title}\n\n{lead}".strip() or (title or "").strip()
    if not query:
        return []

    self_canon = _canon(self_path) if self_path else None
    excluded = {_canon(e) for e in (existing_links or set())}

    try:
        hits = find_module.find(
            vault_root,
            query=query,
            limit=limit * RELATED_OVERFETCH,
            mode="hybrid",
            graph=True,
            scope=scope,
            prefer_compiled=True,
        )
    except Exception as e:  # noqa: BLE001 — suggestions are best-effort
        log.debug("suggest_related find() failed: %s", e)
        return []

    eligible = []
    for h in hits:
        hc = _canon(h.path)
        if self_canon and hc == self_canon:
            continue
        if hc in excluded:
            continue
        if not _is_relation_target_eligible(vault_root, h.path):
            continue
        eligible.append(h)

    # Re-rank: find's fused position (1/(i+1)) + hub bonus on graph_in_degree.
    def _score(i_h: tuple[int, object]) -> float:
        i, h = i_h
        return 1.0 / (i + 1) + HUB_WEIGHT * math.log1p(getattr(h, "graph_in_degree", 0) or 0)

    ranked = sorted(enumerate(eligible), key=_score, reverse=True)
    return [
        RelatedSuggestion(
            path=h.path, title=h.title, type=h.type, why=_why(h), excerpt=h.excerpt
        )
        for _, h in ranked[:limit]
    ]


def _best_cosine_per_file(
    vault_root: Path,
    *,
    title: str,
    body: str,
    k: int = 15,
    published_path: str | None = None,
) -> dict[str, float]:
    """Embed a draft (title+body) as PASSAGES and return the max cosine per
    existing file over the sidecar: ``{file_path: best_score}``.

    The shared core of `detect_duplicates` / `detect_contradictions`: one encode
    + search pass, so a single write can partition the same scores into the dup
    band and the contradiction band without embedding the draft twice (the
    encode is the expensive part). Embeds with `is_query=False` (doc-to-doc, not
    a query). Returns ``{}`` when embeddings are disabled, unimportable, or the
    sidecar is empty — the no-op contract both callers depend on, so the fast
    test suite and torch-less deploys are unaffected.

    `published_path` names a page whose rows the sidecar may already hold for
    these very chunk texts — a post-commit sweep passes the page its commit just
    embedded. A chunk whose exact text has a stored vector takes that vector
    instead of being encoded again: a vector is a function of its text, the
    same economy the write's own embedding pass applies. On a CPU-only cell the
    second encode of a just-written note was most of a 9-13 s sweep. Only the
    texts the page's rows lack are encoded, so a missing or stale row costs
    what it always did, and reuse never changes a score while the sidecar's
    one-model invariant holds.
    """
    if os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
        return {}
    # While the background warm-up is loading the model, embedding the draft
    # would BLOCK on the singleton lock (minutes on a first-ever download) —
    # and this runs inline on every add/note/edit and pack assembly. Skip the
    # sweep; {} is the same no-op contract used for torch-less deploys.
    from . import readiness
    if readiness.should_defer("embeddings"):
        return {}
    try:
        from . import embeddings

        # The advisory sweep runs inline on every governed edit and is where
        # the encode it pays actually lives, so it is timed here rather than at
        # the call sites: one definition, and every surface that reaches it --
        # add, note, edit, pack assembly -- is attributed the same way. The
        # fields are what the duration alone cannot say: on 0.84.1 an
        # `embeddings.encode` of 15.6 s with `count=1` sat entirely outside
        # `index.embeddings`, and this is the caller it belonged to.
        with call_spans.span("advisory.best_cosine", {}) as measured:
            chunks = embeddings.chunk_text(title, body)
            if not chunks:
                if measured is not None:
                    measured["texts"] = 0
                    measured["chars"] = 0
                return {}
            idx = embeddings.get_embedding_index(vault_root)
            stored = (
                embeddings._stored_text_vectors(idx, published_path)[0]
                if published_path
                else {}
            )
            # A text no row holds may still have been encoded by a sweep moments
            # ago -- an edit's previous generation of these same paragraphs.
            recalled = embeddings.recall_passage_vectors(
                chunk for chunk in dict.fromkeys(chunks) if chunk not in stored
            )
            published = stored
            if recalled:
                stored = {**stored, **recalled}
            encoded = [chunk for chunk in chunks if chunk not in stored]
            # With nothing to reuse the whole draft is encoded as it always was;
            # otherwise only the unique texts the stored rows lack.
            full_encode = len(encoded) == len(chunks)
            to_encode = chunks if full_encode else list(dict.fromkeys(encoded))
            if measured is not None:
                # `texts`/`chars` are what the encoder was handed, so on this
                # surface they still say what the sweep encoded.
                measured["texts"] = len(to_encode)
                measured["chars"] = sum(len(chunk) for chunk in to_encode)
                if published_path:
                    measured["reused"] = sum(1 for chunk in chunks if chunk in published)
                if recalled:
                    measured["recalled"] = sum(1 for chunk in chunks if chunk in recalled)
            if full_encode:
                vecs = embeddings.embed_texts(chunks, is_query=False)
                embeddings.remember_passage_vectors(chunks, vecs)
            else:
                lookup = {chunk: stored[chunk] for chunk in chunks if chunk in stored}
                if to_encode:
                    fresh = embeddings.embed_texts(to_encode, is_query=False)
                    embeddings.remember_passage_vectors(to_encode, fresh)
                    lookup.update(zip(to_encode, fresh, strict=True))
                vecs = [lookup[chunk] for chunk in chunks]
            best_per_file: dict[str, float] = {}
            # Every chunk in one pass over the matrix, asking eligibility only of
            # the pages that reach a chunk's top-k: a `search` per chunk re-read
            # the matrix and hydrated text this discards, and the allowed-path
            # set was a walk of the whole corpus on every write.
            admits = _index_admitter(vault_root)
            for hits in idx.search_many(vecs, k, admits=admits):
                for fp, _cidx, score in hits:
                    if fp not in best_per_file or score > best_per_file[fp]:
                        best_per_file[fp] = score
            return best_per_file
    except ImportError as e:
        log.debug("_best_cosine_per_file unavailable (%s)", e)
        return {}
    except Exception as e:  # noqa: BLE001 — best-effort
        log.debug("_best_cosine_per_file failed: %s", e)
        return {}


def _index_admitter(vault_root: Path):
    """Which sidecar pages the advisory may score: exactly those the index walk yields.

    Stray rows -- a page moved to `_trash/`, one deleted before its rows were
    purged, a Records item -- must never reach a warning (observed 2026-07-04:
    dup warnings flagging trash entries). The KB scope answers per page from
    that page's own ancestry; the vault scope has no per-page twin and still
    walks, once per sweep.
    """
    from . import index_paths

    admits = index_paths.index_markdown_admitter(vault_root)
    if admits is not None:
        return admits
    allowed = frozenset(
        rel
        for rel in (
            index_paths.rel_to_vault(vault_root, path)
            for path in index_paths.iter_index_markdown(vault_root)
        )
        if rel is not None
    )
    return allowed.__contains__


def pairwise_best_cosine_from_sidecar(
    vault_root: Path, pages: Iterable[tuple[str, list[str]]]
) -> tuple[dict[frozenset[str], float], set[str]]:
    """Best cosine between each pair of `pages`, from the sidecar's own rows -- no encode.

    The context pack only ever needs proximity AMONG the pages it packed, and
    every one of those pages was embedded when it was written. Re-encoding
    their bodies to get the same vectors back was the whole cost of a recall
    on a large vault (measured 2026-09-14: 32 to 50 s of `embeddings.encode`
    over 110 to 174 chunks per `ask_memory`, on 4,281 pages). So this reads the
    rows the embedding pass published and does one small pairwise product.

    `pages` are `(rel_path, chunks)` with the exact chunking the index stores
    (`embeddings._chunks_for_page`). Exactness is decided on stored chunk TEXT,
    as `published_generation_vectors` does: a page whose rows do not match its
    current chunks -- its embedding still deferred, or the page moved since --
    contributes no pairs and is absent from the returned covered set, and is
    never encoded here. Returns `({}, set())` when embeddings are disabled or
    the sidecar is unavailable, the same no-op contract as the encoding sweep.
    """
    if os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
        return {}, set()
    wanted = {rel: list(chunks) for rel, chunks in pages if chunks}
    if not wanted:
        return {}, set()
    try:
        import numpy as np

        from . import embeddings

        # Same span as the encoding sweep on purpose (see
        # `best_cosine_per_file_for_vectors`): `texts=0` is what says it
        # encoded nothing, and `pages` how many packed pages had exact rows.
        with call_spans.span("advisory.best_cosine", {}) as measured:
            idx = embeddings.get_embedding_index(vault_root)
            metadata, matrix = idx.all_vectors()
            rows_by_page: dict[str, list[tuple[int, int]]] = {}
            for row, (file_path, chunk_index) in enumerate(metadata):
                if file_path in wanted:
                    rows_by_page.setdefault(file_path, []).append((chunk_index, row))
            texts = idx._texts_for(
                [(fp, ci) for fp, rows in rows_by_page.items() for ci, _row in rows]
            )
            vectors: dict[str, np.ndarray] = {}
            for file_path, rows in rows_by_page.items():
                rows.sort()
                chunks = wanted[file_path]
                if [ci for ci, _row in rows] != list(range(len(chunks))):
                    continue
                if [texts.get((file_path, ci)) for ci, _row in rows] != chunks:
                    continue
                stacked = np.asarray([matrix[row] for _ci, row in rows], dtype=np.float32)
                norms = np.linalg.norm(stacked, axis=1, keepdims=True)
                vectors[file_path] = stacked / np.maximum(norms, 1e-12)
            if measured is not None:
                measured["texts"] = 0
                measured["pages"] = len(vectors)
                measured["vectors"] = sum(len(v) for v in vectors.values())
            best: dict[frozenset[str], float] = {}
            names = sorted(vectors)
            for i, a in enumerate(names):
                for b in names[i + 1 :]:
                    best[frozenset((a, b))] = float((vectors[a] @ vectors[b].T).max())
            return best, set(names)
    except ImportError as e:
        log.debug("pairwise_best_cosine_from_sidecar unavailable (%s)", e)
        return {}, set()
    except Exception as e:  # noqa: BLE001 -- best-effort, the pack degrades to no tension
        log.debug("pairwise_best_cosine_from_sidecar failed: %s", e)
        return {}, set()


def best_cosine_per_file_for_vectors(
    vault_root: Path,
    vectors,
    *,
    self_path: str | None = None,
    k: int = 15,
) -> dict[str, float]:
    """`_best_cosine_per_file` for vectors a caller already holds — no encode.

    Deferred advisory work reuses the exact vectors the embedding pass
    published for one generation, so the same page is never encoded twice.
    The target identity is excluded HERE rather than downstream, so a page
    cannot rank against itself or consume a `top_n` slot with a self-match.

    Returns ``{}`` on the same no-op contract as `_best_cosine_per_file`:
    embeddings disabled, sidecar empty or unreadable, or no vectors supplied.
    """
    if os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
        return {}
    try:
        from . import embeddings

        # The same span name as the encoding path, deliberately: both are "the
        # advisory's cosine sweep", and a diagnosis wants to compare them. What
        # separates them is the fields -- this one reports `texts=0`, because
        # it encodes nothing and reuses vectors the embedding pass published.
        with call_spans.span("advisory.best_cosine", {}) as measured:
            rows = list(vectors)
            if measured is not None:
                measured["texts"] = 0
                measured["vectors"] = len(rows)
            if not rows:
                return {}
            idx = embeddings.get_embedding_index(vault_root)
            self_canon = _canon(self_path) if self_path else None
            best_per_file: dict[str, float] = {}
            # The same scoring and eligibility as the inline sweep, so a
            # deferred advisory ranks exactly what the synchronous one would.
            admits = _index_admitter(vault_root)
            for hits in idx.search_many(rows, k, admits=admits):
                for fp, _cidx, score in hits:
                    if self_canon and _canon(fp) == self_canon:
                        continue
                    if fp not in best_per_file or score > best_per_file[fp]:
                        best_per_file[fp] = score
            return best_per_file
    except ImportError as e:
        log.debug("best_cosine_per_file_for_vectors unavailable (%s)", e)
        return {}
    except Exception as e:  # noqa: BLE001 — best-effort
        log.debug("best_cosine_per_file_for_vectors failed: %s", e)
        return {}


def _declared_pair_filter(vault_root: Path, self_path: str | None):
    """A candidate-level "already declared a rival pair with `self_path`?" predicate.

    Declared means a recorded competing-alternatives stance, an authored
    `contradicts` edge between the two pages, or both answering one question. In
    every case the proximity warning would only repeat what the author typed, and
    the stance is fingerprint-bound, so editing either rival brings the warning
    back. A draft with no page identity of its own (`self_path is None`) has no
    pair to declare, so it warns exactly as before.

    Applied INSIDE each detect loop rather than to the finished list: filtering
    afterwards would let an exempt rival consume a `top_n` slot, so three declared
    rivals ranked above a genuine near-duplicate would suppress the real warning.
    """
    from . import contradiction_stance

    return contradiction_stance.DeclaredPairFilter(vault_root, self_path)


def detect_duplicates(
    vault_root: Path,
    *,
    title: str,
    body: str,
    self_path: str | None = None,
    types_filter: list[str] | None = None,
    threshold: float | None = None,
    top_n: int = 3,
    precomputed: dict[str, float] | None = None,
) -> list[DupCandidate]:
    """Flag existing pages whose content is near-identical to a draft.

    Cosine-matches the draft against the existing sidecar (via
    `_best_cosine_per_file`) and returns at most `top_n` candidates at/above
    `threshold` (default resolved from `EXOMEM_DUP_THRESHOLD`, else
    `DUP_THRESHOLD`), optionally restricted to `types_filter` page types. Pass
    `precomputed` (a `_best_cosine_per_file` map) to reuse one embedding pass
    across the dup + contradiction checks on a single write. No-ops (returns [])
    when embeddings are disabled or the sidecar is empty.
    """
    if os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
        return []
    if threshold is None:
        threshold = _dup_threshold()
    best_per_file = (
        precomputed
        if precomputed is not None
        else _best_cosine_per_file(vault_root, title=title, body=body, k=top_n * 5)
    )
    if not best_per_file:
        return []

    from . import find as find_module
    from . import recall_policy

    self_canon = _canon(self_path) if self_path else None
    declared = _declared_pair_filter(vault_root, self_path)
    out: list[DupCandidate] = []
    for fp, score in sorted(best_per_file.items(), key=lambda t: -t[1]):
        if score < threshold:
            break  # sorted desc — nothing below threshold remains
        if self_canon and _canon(fp) == self_canon:
            continue
        if not recall_policy.is_recall_candidate(vault_root, vault_root / fp):
            continue
        page = find_module._CACHE.get(vault_root / fp, vault_root)
        if page is None:
            continue
        if types_filter and page.page_type not in types_filter:
            continue
        if declared(fp):
            continue  # already declared rivals — and it must not eat a `top_n` slot
        out.append(DupCandidate(path=fp, title=page.title, cosine=round(float(score), 4)))
        if len(out) >= top_n:
            break
    return out


def detect_contradictions(
    vault_root: Path,
    *,
    title: str,
    body: str,
    self_path: str | None = None,
    top_n: int = 3,
    precomputed: dict[str, float] | None = None,
) -> list[DupCandidate]:
    """Flag existing ACTIVE COMPILED notes in the band `[floor, dup_threshold)`.

    A page this close to the draft (but not near-identical) plausibly restates,
    refines, OR contradicts it. This is a PROXIMITY measurement, not a polarity
    judgment — embeddings can't separate "X works" from "X doesn't" — so the
    server surfaces the tension and the reader decides (and supersedes if it's a
    real conflict). Candidates are restricted to *active compiled conclusions in
    a writeable (read-write) tree* — the only things resolvable via edit/replace
    — so a raw source never trips this, and an `add` only flags genuine
    new-capture-vs-active-conclusion tension (never source-vs-source noise).

    `floor` is resolved from `EXOMEM_CONTRADICTION_FLOOR`, the `ceiling` from
    `EXOMEM_DUP_THRESHOLD`; an inverted band (floor >= ceiling) is logged and
    disabled (returns []). Pass `precomputed` to share one embedding pass with
    `detect_duplicates`. No-ops (returns []) when embeddings are disabled/empty.
    """
    if os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
        return []
    floor = _contradiction_floor()
    ceiling = _dup_threshold()
    if floor >= ceiling:
        log.warning(
            "EXOMEM_CONTRADICTION_FLOOR (%s) >= dup ceiling (%s); "
            "contradiction band disabled this call",
            floor, ceiling,
        )
        return []
    best_per_file = (
        precomputed
        if precomputed is not None
        else _best_cosine_per_file(vault_root, title=title, body=body, k=top_n * 5)
    )
    if not best_per_file:
        return []

    from . import access, recall_policy
    from . import find as find_module

    self_canon = _canon(self_path) if self_path else None
    declared = _declared_pair_filter(vault_root, self_path)
    out: list[DupCandidate] = []
    for fp, score in sorted(best_per_file.items(), key=lambda t: -t[1]):
        if score >= ceiling:
            continue  # a near-duplicate — detect_duplicates owns that band
        if score < floor:
            break  # sorted desc — nothing else reaches the band
        if self_canon and _canon(fp) == self_canon:
            continue
        if not recall_policy.is_recall_candidate(vault_root, vault_root / fp):
            continue
        page = find_module._CACHE.get(vault_root / fp, vault_root)
        if page is None:
            continue
        # Restrict to active compiled conclusions in a writeable tree: the only
        # candidates a contradiction can actually be reconciled against.
        if page.page_type not in find_module._COMPILED_TYPES:
            continue
        if page.status in ("superseded", "archived"):
            continue
        if access.access_tier(vault_root, page.rel_path) != access.TIER_READ_WRITE:
            continue
        if declared(fp):
            continue  # already declared rivals — and it must not eat a `top_n` slot
        out.append(DupCandidate(path=fp, title=page.title, cosine=round(float(score), 4)))
        if len(out) >= top_n:
            break
    return out


def dup_warning(candidate: DupCandidate) -> str:
    """Render a near-duplicate as a single warning string for a write result."""
    return (
        f"possible near-duplicate of [[{candidate.path}]] (cosine "
        f"{candidate.cosine}) — consider edit/replace/append instead of a new page"
    )


def overlap_warning(candidate: DupCandidate) -> str:
    """Render a band-overlap as a single honest warning for a write result.

    Deliberately NOT phrased as an asserted contradiction — the cosine band is a
    proximity measurement, not a stance judgment. It names contradiction as one
    possibility and hands the call to the reader (measure-don't-judge), pointing
    at supersession as the resolution if it IS a conflict.

    There is no polarity clause and no claim-level variant: write-time warning
    generation invokes no polarity classification at all, so this string is the
    same one on every path including the claim-level gate.
    """
    return (
        f"overlaps active note [[{candidate.path}]] (cosine {candidate.cosine}) "
        "— review: does this restate, refine, or contradict it? supersede the "
        "stale one if they conflict"
    )
