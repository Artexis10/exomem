"""Adoption Studio agent contract: bounded work items and governed proposals.

An adoption run (``adoption_run.py``) turns a messy legacy tree into governed
Sources. This module is the *agent* half: it hands an agent a bounded, read-only
``work-item`` (measurements plus recorded content, zero judgment), accepts
structured ``propose`` submissions, surfaces them through the existing Epistemic
Inbox review verbs, and applies an approved proposal EXCLUSIVELY through a
pre-existing governed leaf (``remember`` / ``link`` / governed ``edit`` /
``replace_memory``).

Nothing here writes Markdown directly: ``propose`` writes exactly one file
(``proposals.json`` under the run directory, via the run store) and every applied
effect routes through a governed command with all its validation, logging, index
updates, and compare-and-swap intact. Identity and fingerprints reuse
``review_state`` verbatim, and adoption refs are namespaced under
``exomem://review/adoption/<id>`` so they never resolve to — and are never
resolved by — attention, activation, or relation items (the #198 isolation rule,
byte-for-byte the ``relation_queue`` pattern).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any

from . import context_refs, guards
from . import get_page as get_page_module
from . import link as link_module
from . import note as note_module
from . import relation_registry as relation_registry_module
from . import review_state as review_state_module
from . import vault as vault_module
from .adoption_run import AdoptionRunStore
from .entity_types import ENTITY_TYPE_IDS
from .kbdir import kb_dirname

ADOPTION_REVIEW_PREFIX = "exomem://review/adoption/"

PROPOSAL_KINDS = ("compilation", "entity", "relation", "reconciliation", "supersession")
_APPLIED_RUN_STATUSES = ("applied", "already-applied")
_CONTENT_MIN = 1
_CONTENT_MAX = 100_000

_WORK_ITEM_CONSTRAINTS = (
    "Your interpretation is explicit and provisional. You cannot write to the "
    "vault. Submit structured proposals via adoption_studio(action='propose'); "
    "each is validated, fingerprint-bound, reviewed by the user, and applied only "
    "through governed operations. Original files are never rewritten, moved, or "
    "deleted."
)

_PROPOSAL_KIND_SUMMARY = {
    "compilation": "sources (governed Sources paths) + title + note_type + content "
    "markdown; applied via remember",
    "entity": f"entity_type ({'|'.join(ENTITY_TYPE_IDS)}) + name + summary "
    "[+ slug, why_in_kb, tags, connections]; applied via create-entity",
    "relation": "from + to + relation_type (must exist in the relation registry); "
    "applied as a reviewed Relations bullet",
    "reconciliation": "subject_path + duplicate_of + resolution (relate|supersede) "
    "+ sub-kind fields",
    "supersession": "old_path + title + note_type + content; applied via replace "
    "(supersession chain)",
}


class AdoptionProposalError(Exception):
    """Structured failure: ``code`` machine-readable, ``reason`` human-readable.

    Exposes ``.code``/``.reason`` so ``commands.op_adoption_studio`` converts it
    to the house ``ValueError(f"{code}: {reason}")`` envelope exactly like
    ``AdoptionRunError``.
    """

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _clean(path: str | None) -> str:
    return str(path or "").replace("\\", "/").strip().lstrip("/")


def _finding(code: str, path: str, detail: str) -> dict:
    return {"code": code, "path": path, "detail": detail}


def _hash_file(root: Path, path: str) -> str | None:
    """Content hash of a governed page, or ``None`` when it is missing/unreadable.

    Uses the same whole-file ``vault.content_hash`` the edit drift-guard uses so a
    binding re-hash agrees byte-for-byte with ``get_page``'s ``content_hash``.
    """
    fpath = Path(root) / _clean(path)
    try:
        return vault_module.content_hash(fpath.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None


# --------------------------------------------------------------------------- #
# ref namespace (byte-for-byte the relation-queue pattern, #198 isolation)
# --------------------------------------------------------------------------- #
def is_adoption_ref(value: str) -> bool:
    return str(value or "").strip().startswith(ADOPTION_REVIEW_PREFIX)


def adoption_review_ref(review_id: str) -> str:
    clean = str(review_id or "").strip().lower()
    if len(clean) != 24 or any(char not in "0123456789abcdef" for char in clean):
        raise ValueError(
            f"INVALID_REVIEW_REFERENCE: invalid adoption review id {review_id!r}"
        )
    return f"{ADOPTION_REVIEW_PREFIX}{clean}"


def parse_adoption_review_ref(value: str) -> str:
    raw = str(value or "").strip()
    if not raw.startswith(ADOPTION_REVIEW_PREFIX):
        raise ValueError(
            f"INVALID_REVIEW_REFERENCE: expected {ADOPTION_REVIEW_PREFIX}<id>"
        )
    raw_id = raw[len(ADOPTION_REVIEW_PREFIX) :].lower()
    if len(raw_id) != 24 or any(char not in "0123456789abcdef" for char in raw_id):
        raise ValueError(
            f"INVALID_REVIEW_REFERENCE: invalid adoption review reference {value!r}"
        )
    return raw_id


# --------------------------------------------------------------------------- #
# identity + fingerprint (reuse review_state verbatim)
# --------------------------------------------------------------------------- #
def _proposal_id(kind: str, payload: dict) -> str:
    raw = _canonical_json({"kind": kind, "payload": payload})
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _review_id(run_id: str, proposal_id: str) -> str:
    return review_state_module.item_id(f"adoption:{run_id}:{proposal_id}")


def _refs_for(kind: str, payload: dict) -> tuple[str, list[str]]:
    """Stable (target_ref, related_refs) for the fingerprint's identity anchor.

    These depend only on the payload's identity fields (never on content), so the
    review item's identity is stable; resurfacing is driven solely by the
    ``signal_version`` (payload + live bound hashes).
    """
    if kind == "compilation":
        sources = [_clean(s) for s in (payload.get("sources") or [])]
        if sources:
            return context_refs.source_ref(sources[0]), [
                context_refs.source_ref(s) for s in sources[1:]
            ]
        return context_refs.vault_ref(f"compilation/{payload.get('title', '')}"), []
    if kind == "entity":
        return context_refs.vault_ref(f"Entities/{payload.get('name', '')}"), []
    if kind == "relation":
        return context_refs.vault_ref(_clean(payload.get("from"))), [
            context_refs.vault_ref(_clean(payload.get("to")))
        ]
    if kind == "supersession":
        return context_refs.vault_ref(_clean(payload.get("old_path"))), []
    if kind == "reconciliation":
        return context_refs.vault_ref(_clean(payload.get("subject_path"))), [
            context_refs.vault_ref(_clean(payload.get("duplicate_of")))
        ]
    return context_refs.vault_ref(kind), []


def _signal_version(payload: dict, bindings: dict) -> str:
    raw = _canonical_json(payload) + "|" + _canonical_json(bindings)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _proposal_fingerprint(payload: dict, bindings: dict, kind: str) -> str:
    target_ref, related_refs = _refs_for(kind, payload)
    return review_state_module.fingerprint(
        target_ref=target_ref,
        categories=[kind],
        reasons=[
            {"category": kind, "meta": {"signal_version": _signal_version(payload, bindings)}}
        ],
        related_refs=related_refs,
    )


# --------------------------------------------------------------------------- #
# bindings
# --------------------------------------------------------------------------- #
def _binding_source_paths(kind: str, payload: dict) -> list[str]:
    """Paths whose content is fingerprint-bound and re-hashed at apply.

    Compilation binds its governed Sources; supersession (and a
    reconciliation-supersede) binds the page it will replace. Relation and entity
    have no bound content — their drift guard is the governed edit's own
    ``expected_hash`` compare-and-swap, and identity, respectively.
    """
    if kind == "compilation":
        return [_clean(s) for s in (payload.get("sources") or [])]
    if kind == "supersession":
        return [_clean(payload.get("old_path"))] if payload.get("old_path") else []
    if kind == "reconciliation" and payload.get("resolution") == "supersede":
        return [_clean(payload.get("subject_path"))] if payload.get("subject_path") else []
    return []


def _live_bindings(root: Path, run: dict, kind: str, payload: dict) -> dict:
    """Bindings computed from live disk state: run fingerprint + per-source hash."""
    sources: dict[str, str] = {}
    for path in _binding_source_paths(kind, payload):
        if not path:
            continue
        digest = _hash_file(root, path)
        if digest is not None:
            sources[path] = digest
    return {"run_fingerprint": run.get("inventory_fingerprint", ""), "sources": sources}


def _recompute_fingerprint(root: Path, run: dict, proposal: dict) -> str:
    """Fingerprint from the STORED payload plus a LIVE re-hash of bound sources.

    This is what makes a dismissed proposal resurface: when a bound source is
    edited, its live hash changes, the ``signal_version`` changes, the fingerprint
    changes, and the review-state decision (keyed ``review_id:fingerprint``) no
    longer matches — the item is open again.
    """
    payload = proposal.get("payload") or {}
    kind = proposal.get("kind") or ""
    stored = proposal.get("bindings") or {}
    live_sources: dict[str, str] = {}
    for path in (stored.get("sources") or {}):
        digest = _hash_file(root, path)
        if digest is not None:
            live_sources[path] = digest
    bindings = {"run_fingerprint": stored.get("run_fingerprint", ""), "sources": live_sources}
    return _proposal_fingerprint(payload, bindings, kind)


def _rehash_bindings(root: Path, bindings: dict) -> list[dict]:
    """Rows whose bound content changed since submission (drift → REVIEW_ITEM_CHANGED)."""
    changed: list[dict] = []
    for path, expected in (bindings.get("sources") or {}).items():
        current = _hash_file(root, path)
        if current != expected:
            changed.append(
                {"path": path, "expected_sha256": expected, "current_sha256": current}
            )
    return changed


# --------------------------------------------------------------------------- #
# validation (phase 1 at propose; re-run live at apply-proposal)
# --------------------------------------------------------------------------- #
def _is_governed_source(path: str) -> bool:
    clean = _clean(path)
    return f"/{kb_dirname()}/Sources/" in f"/{clean}" or clean.startswith(
        f"{kb_dirname()}/Sources/"
    )


def _page_exists(root: Path, path: str) -> bool:
    try:
        get_page_module.get_page(Path(root), path=_clean(path))
    except get_page_module.GetError:
        return False
    except Exception:  # noqa: BLE001 - a malformed path is "does not exist" here
        return False
    return True


def _page_frontmatter(root: Path, path: str) -> dict | None:
    try:
        return get_page_module.get_page(Path(root), path=_clean(path)).frontmatter or {}
    except Exception:  # noqa: BLE001
        return None


def _validate_content(content: Any, findings: list[dict]) -> None:
    text = content if isinstance(content, str) else ""
    if len(text) < _CONTENT_MIN:
        findings.append(_finding("EMPTY_CONTENT", "content", "content is required (1+ chars)"))
        return
    if len(text) > _CONTENT_MAX:
        findings.append(
            _finding(
                "CONTENT_TOO_LARGE",
                "content",
                f"content is {len(text):,} chars (> {_CONTENT_MAX:,} limit)",
            )
        )
    try:
        guards.guard_text_content(text, tool="adoption_studio", field="content")
    except ValueError as exc:
        findings.append(_finding("BINARY_BLOB", "content", str(exc)))


def _validate_note_type(note_type: Any, project: Any, findings: list[dict]) -> None:
    if note_type not in note_module.NOTE_TYPES:
        findings.append(
            _finding(
                "INVALID_NOTE_TYPE",
                "note_type",
                f"note_type {note_type!r} is not one of {list(note_module.NOTE_TYPES)}",
            )
        )
    elif note_type == "research-note" and not project:
        findings.append(
            _finding("MISSING_PROJECT", "project", "research-note requires a project key")
        )


def _validate_relation_type(root: Path, relation_type: Any, findings: list[dict]) -> None:
    registry = relation_registry_module.load_registry(Path(root))
    resolution = registry.resolve(str(relation_type or ""))
    if resolution.definition is None:
        findings.append(
            _finding(
                "UNKNOWN_RELATION_TYPE",
                "relation_type",
                f"relation_type {relation_type!r} is not in the relation registry; "
                "agents cannot extend the registry",
            )
        )


def _validate_supersession_target(root: Path, old_path: Any, findings: list[dict]) -> None:
    clean = _clean(old_path)
    if not clean:
        findings.append(_finding("OLD_NOT_FOUND", "old_path", "old_path is required"))
        return
    if "/Sources/" in f"/{clean}" or "/Evidence/" in f"/{clean}":
        findings.append(
            _finding(
                "CANNOT_SUPERSEDE",
                "old_path",
                "Sources/ and Evidence/ are append-only and cannot be superseded",
            )
        )
    frontmatter = _page_frontmatter(root, clean)
    if frontmatter is None:
        findings.append(_finding("OLD_NOT_FOUND", "old_path", f"{clean} does not exist"))
    elif frontmatter.get("status") == "superseded":
        findings.append(
            _finding("ALREADY_SUPERSEDED", "old_path", f"{clean} is already superseded")
        )


def _validate(root: Path, kind: str, payload: dict, run: dict) -> list[dict]:
    """Full structural + registry/schema validation. Returns findings (empty = valid)."""
    findings: list[dict] = []
    if kind not in PROPOSAL_KINDS:
        findings.append(
            _finding("UNKNOWN_KIND", "kind", f"kind {kind!r} is not one of {list(PROPOSAL_KINDS)}")
        )
        return findings

    if kind == "compilation":
        _validate_note_type(payload.get("note_type"), payload.get("project"), findings)
        if not str(payload.get("title") or "").strip():
            findings.append(_finding("MISSING_TITLE", "title", "title is required"))
        sources = [_clean(s) for s in (payload.get("sources") or [])]
        if not sources:
            findings.append(_finding("MISSING_SOURCES", "sources", "at least one source is required"))
        for src in sources:
            if not _is_governed_source(src):
                findings.append(
                    _finding("NOT_A_SOURCE", src, f"{src} is not a governed Knowledge Base Source")
                )
            elif not _page_exists(root, src):
                findings.append(_finding("SOURCES_NOT_FOUND", src, f"{src} does not exist"))
        _validate_content(payload.get("content"), findings)

    elif kind == "entity":
        if payload.get("entity_type") not in link_module.ENTITY_TYPES:
            findings.append(
                _finding(
                    "INVALID_ENTITY_TYPE",
                    "entity_type",
                    f"entity_type {payload.get('entity_type')!r} is not one of "
                    f"{list(link_module.ENTITY_TYPES)}",
                )
            )
        if not str(payload.get("name") or "").strip():
            findings.append(_finding("MISSING_NAME", "name", "name is required"))
        if not str(payload.get("summary") or "").strip():
            findings.append(_finding("MISSING_SUMMARY", "summary", "summary is required"))

    elif kind == "relation":
        for endpoint in ("from", "to"):
            value = _clean(payload.get(endpoint))
            if not value:
                findings.append(_finding("MISSING_ENDPOINT", endpoint, f"{endpoint} is required"))
            elif not _page_exists(root, value):
                findings.append(_finding("ENDPOINT_NOT_FOUND", endpoint, f"{value} does not exist"))
        _validate_relation_type(root, payload.get("relation_type"), findings)

    elif kind == "supersession":
        _validate_supersession_target(root, payload.get("old_path"), findings)
        _validate_note_type(payload.get("note_type"), payload.get("project"), findings)
        if not str(payload.get("title") or "").strip():
            findings.append(_finding("MISSING_TITLE", "title", "title is required"))
        _validate_content(payload.get("content"), findings)

    elif kind == "reconciliation":
        resolution = payload.get("resolution")
        if resolution not in ("relate", "supersede"):
            findings.append(
                _finding(
                    "INVALID_RESOLUTION",
                    "resolution",
                    "resolution must be 'relate' or 'supersede'",
                )
            )
        subject = _clean(payload.get("subject_path"))
        duplicate = _clean(payload.get("duplicate_of"))
        if not subject or not _page_exists(root, subject):
            findings.append(_finding("SUBJECT_NOT_FOUND", "subject_path", f"{subject} does not exist"))
        if not duplicate or not _page_exists(root, duplicate):
            findings.append(
                _finding("DUPLICATE_NOT_FOUND", "duplicate_of", f"{duplicate} does not exist")
            )
        if resolution == "relate":
            _validate_relation_type(root, payload.get("relation_type") or "duplicates", findings)
        elif resolution == "supersede":
            _validate_supersession_target(root, subject, findings)
            _validate_note_type(payload.get("note_type"), payload.get("project"), findings)
            _validate_content(payload.get("content"), findings)

    return findings


# --------------------------------------------------------------------------- #
# work-item (read-only bounded context)
# --------------------------------------------------------------------------- #
def _extract_capture(text: str, max_chars: int) -> tuple[str, bool]:
    """Pull the ``## Capture`` fenced body out of an imported Source copy."""
    marker = "## Capture"
    idx = text.find(marker)
    body = text[idx + len(marker) :] if idx != -1 else text
    lines = [ln for ln in body.splitlines() if not ln.strip().startswith("```")]
    excerpt = "\n".join(lines).strip()
    truncated = len(excerpt) > max_chars
    return excerpt[:max_chars], truncated


def _existing_context(root: Path, imported_path: str, title: str, body: str) -> dict:
    from . import corpus_aware as corpus_aware_module

    related: list[dict] = []
    try:
        suggestions = corpus_aware_module.suggest_related(
            Path(root),
            title=title or imported_path,
            body=body,
            self_path=imported_path,
            limit=5,
            scope="kb",
        )
        for suggestion in suggestions[:5]:
            related.append(
                {
                    "path": suggestion.path,
                    "ref": context_refs.vault_ref(suggestion.path),
                    "title": suggestion.title,
                    "type": suggestion.type,
                }
            )
    except Exception:  # noqa: BLE001 - retrieval is best-effort context, never fatal
        related = []
    return {"for": imported_path, "related": related}


def _semantic_unit_pack(root: Path, imported_path: str, char_bound: int) -> dict:
    """The bound Source's semantic-unit context pack for a work item.

    Reuses the SAME pack constructor ``context-packs`` defines (``#242``) — never a
    reimplementation — so pack content matches the primary surface exactly. Read-only
    and deterministic. Bounded by the work item's server-clamped ``char_bound``; the
    pack's own per-unit/per-page item caps stay. An absent-or-unit-less source yields
    an explicit empty marker, never a silently missing field.
    """
    if not imported_path or not (root / imported_path).is_file():
        return {"units": [], "available": True}
    from . import context_pack as context_pack_module
    from .find_types import Hit

    hit = Hit(path=imported_path, type=None, scope=None, title="", updated="", excerpt="")
    pack = context_pack_module.assemble_pack(root, [hit], max_unit_total_chars=char_bound)
    entry = (pack.get("semantic_units") or {}).get(imported_path)
    if not entry or not entry.get("units"):
        return {"units": [], "available": True}
    return {"available": True, "parent": entry.get("parent"), "units": entry["units"]}


def work_item(
    root: Path,
    *,
    run_id: str,
    sources: list[str] | None = None,
    max_sources: int = 5,
    max_chars_per_source: int = 2000,
) -> dict:
    """Bounded, deterministic, read-only context pack for an adoption run.

    Measurements plus recorded content only — zero judgment. Writes nothing.
    """
    root = Path(root)
    # Server-side clamps keep the pack bounded regardless of caller input: a
    # negative char cap must not slice from the end, and a huge one must not
    # put a whole large import into an MCP response.
    max_sources = max(0, min(int(max_sources), 50))
    max_chars_per_source = max(1, min(int(max_chars_per_source), 20_000))
    store = AdoptionRunStore(root)
    run = _load_run(store, run_id)

    outcomes = run.get("outcomes") or {}
    plan_items = {it["original_path"]: it for it in (run.get("plan") or {}).get("items") or []}
    applied = [
        (op, o) for op, o in outcomes.items() if o.get("status") in _APPLIED_RUN_STATUSES
    ]
    applied.sort(key=lambda t: plan_items.get(t[0], {}).get("original_bytes", 0), reverse=True)

    if sources:
        wanted = {_clean(p) for p in sources}
        chosen = [(op, o) for op, o in applied if op in wanted]
    else:
        chosen = applied
    total = len(chosen)
    shown = chosen[: max(0, max_sources)]

    source_rows: list[dict] = []
    existing_context: list[dict] = []
    for op, outcome in shown:
        item = plan_items.get(op, {})
        title = item.get("title") or op
        imported_path = outcome.get("target_path") or item.get("target_path") or ""
        excerpt, truncated = "", False
        if imported_path and (root / imported_path).is_file():
            excerpt, truncated = _extract_capture(
                (root / imported_path).read_text(encoding="utf-8", errors="replace"),
                max_chars_per_source,
            )
        elif (root / op).is_file():
            raw = (root / op).read_text(encoding="utf-8", errors="replace")
            truncated = len(raw) > max_chars_per_source
            excerpt = raw[:max_chars_per_source]
        source_rows.append(
            {
                "original_path": op,
                "sha256": outcome.get("sha256") or item.get("original_sha256"),
                "bytes": item.get("original_bytes"),
                "title": title,
                "imported_path": imported_path,
                "source_ref": outcome.get("source_ref") or item.get("target_ref"),
                "excerpt": excerpt,
                "excerpt_truncated": truncated,
                "semantic_units": _semantic_unit_pack(
                    root, imported_path, max_chars_per_source
                ),
            }
        )
        if imported_path:
            existing_context.append(_existing_context(root, imported_path, title, excerpt))

    scan_summary = run.get("scan_summary") or {}
    return {
        "run_ref": run.get("run_ref"),
        "run_id": run.get("run_id"),
        "phase": run.get("phase"),
        "run_fingerprint": run.get("inventory_fingerprint"),
        "constraints": _WORK_ITEM_CONSTRAINTS,
        "sources": source_rows,
        "measurements": {
            "pack_suggestions": run.get("pack_suggestions") or [],
            "totals": scan_summary.get("totals") or {},
            "junk_counts": scan_summary.get("junk_counts") or {},
        },
        "existing_context": existing_context,
        "proposal_kinds": dict(_PROPOSAL_KIND_SUMMARY),
        "limits": {
            "max_sources": max_sources,
            "max_chars_per_source": max_chars_per_source,
            "shown": len(shown),
            "total": total,
            "truncated": max(0, total - len(shown)),
        },
    }


# --------------------------------------------------------------------------- #
# propose (writes ONLY proposals.json)
# --------------------------------------------------------------------------- #
def _load_run(store: AdoptionRunStore, run_id: str | None) -> dict:
    from .adoption_run import AdoptionRunError

    try:
        return store.load(run_id)
    except AdoptionRunError as exc:
        raise AdoptionProposalError(exc.code, exc.reason) from exc


def _contract_validation(root: Path, kind: str, payload: dict, *, why: str) -> dict:
    """Propose-time semantic-write-contract check for compilation/supersession.

    Runs the create path's ``validate_only`` phase with the payload mapped EXACTLY as
    ``_route_apply`` maps it at apply time, so a contract violation surfaces in the
    review queue instead of failing after approval. Returns a compact record:
    ``contract_findings`` (code/severity/detail per blocking finding) plus the
    ``committable_after_review``/``reviewed_none_required`` booleans, and an
    ``invalid_finding`` when the content carries a non-review blocker (the two-phase
    writer refuses those before any write — the existing ``invalid`` mechanism). The
    validate call is read-only: draft registration is ephemeral and writes nothing.
    """
    from . import commands as commands_module

    if kind == "compilation":
        op: Any = commands_module.op_remember
        base_kwargs: dict = {
            "content": payload.get("content") or "",
            "title": payload.get("title") or "",
            "note_type": payload.get("note_type") or "insight",
            "sources": [_clean(s) for s in (payload.get("sources") or [])],
            "tags": payload.get("tags"),
            "project": payload.get("project"),
            "suggestions": False,
        }
    else:  # supersession
        op = commands_module.op_replace_memory
        base_kwargs = {
            "old_path": _clean(payload.get("old_path")),
            "content": payload.get("content") or "",
            "title": payload.get("title") or "",
            "note_type": payload.get("note_type") or "insight",
            "reason": why,
            "sources": payload.get("sources"),
            "tags": payload.get("tags"),
            "project": payload.get("project"),
        }

    try:
        validation = op(root, validate_only=True, **base_kwargs)
    except Exception as exc:  # noqa: BLE001 - the writer refuses blockers here
        # Only a genuine semantic-contract block earns the CONTRACT_BLOCKED
        # framing. Non-contract validation failures (e.g. PROJECT_KEY_TYPO or
        # INVALID_SLUG, which the note path wraps into a plain ValueError whose
        # str() carries the code as a prefix) must NOT masquerade as contract
        # blocks. Prefer a structured ``code`` attribute; fall back to the code
        # string appearing in the wrapped message.
        is_contract_block = (
            getattr(exc, "code", None) == "SEMANTIC_CONTRACT_BLOCKED"
            or "SEMANTIC_CONTRACT_BLOCKED" in str(exc)
        )
        if is_contract_block:
            invalid_finding = _finding(
                "CONTRACT_BLOCKED",
                "content",
                f"the semantic write contract blocks this content: {exc}",
            )
        else:
            invalid_finding = _finding(
                "VALIDATION_FAILED",
                "content",
                f"proposal validation failed: {exc}",
            )
        return {
            "contract_findings": [],
            "committable_after_review": False,
            "reviewed_none_required": False,
            "invalid_finding": invalid_finding,
        }

    contract_result = validation.get("contract_result") or {}
    contract_findings = [
        {"code": f.get("code"), "severity": f.get("severity"), "detail": f.get("detail")}
        for f in (contract_result.get("blocking_findings") or [])
    ]
    invalid_finding = None
    if validation.get("has_non_review_blockers"):
        detail = contract_findings[0]["detail"] if contract_findings else "a blocking contract finding"
        invalid_finding = _finding("CONTRACT_BLOCKED", "content", str(detail))
    return {
        "contract_findings": contract_findings,
        "committable_after_review": bool(validation.get("committable_after_review")),
        "reviewed_none_required": bool(validation.get("reviewed_none_required")),
        "invalid_finding": invalid_finding,
    }


def propose(root: Path, *, run_id: str, proposals: list[dict]) -> dict:
    """Validate and persist structured proposals. Writes only ``proposals.json``."""
    root = Path(root)
    store = AdoptionRunStore(root)
    run = _load_run(store, run_id)
    run_fp = run.get("inventory_fingerprint", "")

    payload = store.load_proposals(run_id)
    existing = payload.get("proposals") or []
    by_id = {p.get("proposal_id"): p for p in existing}

    results: list[dict] = []
    for raw in proposals or []:
        if not isinstance(raw, dict):
            raise AdoptionProposalError("INVALID_PROPOSAL", "each proposal must be an object")
        kind = str(raw.get("kind") or "").strip()
        prop_payload = raw.get("payload") or {}
        why = str(raw.get("why") or "").strip()
        submitted_bindings = raw.get("bindings") or {}

        proposal_id = _proposal_id(kind, prop_payload)
        # Idempotent dedup: an identical resubmission returns the existing record.
        if proposal_id in by_id:
            prior = by_id[proposal_id]
            results.append(
                {
                    "proposal_id": proposal_id,
                    "ref": prior.get("ref"),
                    "fingerprint": prior.get("fingerprint"),
                    "kind": prior.get("kind"),
                    "status": prior.get("status"),
                    "findings": prior.get("findings") or [],
                    "contract_findings": prior.get("contract_findings") or [],
                    "reviewed_none_required": bool(prior.get("reviewed_none_required")),
                    "committable_after_review": bool(prior.get("committable_after_review")),
                    "deduplicated": True,
                }
            )
            continue

        findings = list(_validate(root, kind, prop_payload, run))
        if not why:
            findings.append(_finding("MISSING_WHY", "why", "a one-line `why` rationale is required"))
        submitted_run_fp = submitted_bindings.get("run_fingerprint")
        if submitted_run_fp is not None and submitted_run_fp != run_fp:
            findings.append(
                _finding(
                    "RUN_FINGERPRINT_MISMATCH",
                    "bindings.run_fingerprint",
                    "run_fingerprint does not match the run's current inventory; re-load "
                    "the work item",
                )
            )

        bindings = _live_bindings(root, run, kind, prop_payload)
        # A submitted source hash records what the agent actually read: if the
        # live file no longer matches it, refuse at submission instead of
        # silently rebinding the proposal to content the agent never saw.
        for spath, submitted_hash in (submitted_bindings.get("sources") or {}).items():
            live_hash = (bindings.get("sources") or {}).get(_clean(spath))
            if live_hash is not None and submitted_hash and submitted_hash != live_hash:
                findings.append(
                    _finding(
                        "SOURCE_CHANGED",
                        f"bindings.sources.{spath}",
                        "this source changed between the work-item read and "
                        "submission; re-load the work item and resubmit",
                    )
                )
        # Semantic write contract check for kinds that create/replace governed pages.
        # A blocking finding no reviewed disposition can clear invalidates now; a
        # relation-review gap rides the proposal (resolved at apply via reviewed-none).
        contract_findings: list[dict] = []
        reviewed_none_required = False
        committable_after_review = False
        if not findings and kind in ("compilation", "supersession"):
            contract = _contract_validation(root, kind, prop_payload, why=why)
            contract_findings = contract["contract_findings"]
            reviewed_none_required = contract["reviewed_none_required"]
            committable_after_review = contract["committable_after_review"]
            if contract["invalid_finding"] is not None:
                findings.append(contract["invalid_finding"])

        review_id = _review_id(run_id, proposal_id)
        fingerprint = _proposal_fingerprint(prop_payload, bindings, kind)
        status = "invalid" if findings else "proposed"
        record = {
            "proposal_id": proposal_id,
            "review_id": review_id,
            "ref": adoption_review_ref(review_id),
            "fingerprint": fingerprint,
            "kind": kind,
            "why": why or None,
            "payload": prop_payload,
            "bindings": bindings,
            "status": status,
            "findings": findings,
            "contract_findings": contract_findings,
            "reviewed_none_required": reviewed_none_required,
            "committable_after_review": committable_after_review,
            "submitted_at": _now_iso(),
            "applied": None,
        }
        existing.append(record)
        by_id[proposal_id] = record
        results.append(
            {
                "proposal_id": proposal_id,
                "ref": record["ref"],
                "fingerprint": fingerprint,
                "kind": kind,
                "status": status,
                "findings": findings,
                "contract_findings": contract_findings,
                "reviewed_none_required": reviewed_none_required,
                "committable_after_review": committable_after_review,
            }
        )

    payload["proposals"] = existing
    store.save_proposals(run_id, payload)
    return {
        "mode": "adoption",
        "mutated": True,
        "run_id": run_id,
        "run_ref": run.get("run_ref"),
        "proposals": results,
    }


# --------------------------------------------------------------------------- #
# review surfacing
# --------------------------------------------------------------------------- #
def _run_ids(store: AdoptionRunStore, run_id: str | None) -> list[str]:
    if run_id:
        return [run_id]
    ids: list[str] = []
    for row in store.list_runs():
        if row.get("phase") == "cancelled":
            continue
        rid = row.get("run_id")
        if rid:
            ids.append(rid)
    return ids


def _item_view(proposal: dict, fingerprint: str) -> dict:
    payload = proposal.get("payload") or {}
    return {
        "proposal_id": proposal.get("proposal_id"),
        "review_id": proposal.get("review_id"),
        "ref": proposal.get("ref"),
        "fingerprint": fingerprint,
        "kind": proposal.get("kind"),
        "why": proposal.get("why"),
        "title": payload.get("title") or payload.get("name"),
        "status": proposal.get("status"),
        "contract_findings": proposal.get("contract_findings") or [],
        "submitted_at": proposal.get("submitted_at"),
        "state": "open",
    }


def build_queue(
    root: Path,
    *,
    run_id: str | None = None,
    state: str = "open",
    limit: int = 25,
    today: dt.date | None = None,
) -> dict:
    """Per-run grouped adoption proposal queue with effective review state.

    Applied/invalid proposals are filtered (counted, never shown); dismissed and
    snoozed decisions are honored via ``ReviewStateStore.effective_state`` keyed on
    the live-recomputed fingerprint, so a bound-source edit resurfaces the item.
    """
    root = Path(root)
    store = AdoptionRunStore(root)
    review_store = review_state_module.ReviewStateStore(root)
    state_payload = review_store.load()

    filtered = {"decided": 0, "invalid": 0, "applied": 0}
    groups: list[dict] = []
    flat_items: list[dict] = []
    total = 0
    cap = max(0, int(limit))
    remaining = cap

    for rid in _run_ids(store, run_id):
        try:
            run = store.load(rid)
        except Exception:  # noqa: BLE001 - a corrupt run is simply skipped
            continue
        proposals = (store.load_proposals(rid).get("proposals")) or []
        items: list[dict] = []
        for proposal in proposals:
            status = proposal.get("status")
            if status == "invalid":
                filtered["invalid"] += 1
                continue
            if status == "applied":
                filtered["applied"] += 1
                continue
            total += 1
            fingerprint = _recompute_fingerprint(root, run, proposal)
            effective, _decision = review_store.effective_state(
                proposal.get("review_id") or "",
                fingerprint,
                today=today,
                payload=state_payload,
            )
            if state == "open" and effective != "open":
                filtered["decided"] += 1
                continue
            if state in ("dismissed", "snoozed") and effective != state:
                continue
            view = _item_view(proposal, fingerprint)
            view["state"] = effective
            items.append(view)
            flat_items.append(view)
        if items:
            # The limit is a GLOBAL result cap: spend the remaining budget in
            # run order rather than granting every group the full limit.
            take = items[:remaining] if cap else items
            if cap:
                remaining -= len(take)
            if take:
                groups.append(
                    {
                        "run_id": rid,
                        "run_ref": run.get("run_ref"),
                        "phase": run.get("phase"),
                        "items": take,
                    }
                )

    shown = sum(len(group["items"]) for group in groups)
    return {
        "mode": "adoption",
        "mutated": False,
        "runs": groups,
        # Flat mirror for callers that don't group per run; identical items.
        "items": flat_items[:cap] if cap else flat_items,
        "shown": shown,
        "total": total,
        "filtered": filtered,
    }


def _resolve(root: Path, ref: str) -> tuple[dict, dict, AdoptionRunStore]:
    """Locate the (run, proposal) a review ref points at across all runs."""
    root = Path(root)
    wanted = parse_adoption_review_ref(ref)
    store = AdoptionRunStore(root)
    for rid in _run_ids(store, None):
        try:
            run = store.load(rid)
        except Exception:  # noqa: BLE001
            continue
        for proposal in (store.load_proposals(rid).get("proposals")) or []:
            if proposal.get("review_id") == wanted:
                return run, proposal, store
    raise AdoptionProposalError(
        "REVIEW_ITEM_NOT_FOUND", f"no adoption proposal for {ref}"
    )


def triage(
    root: Path,
    *,
    ref: str,
    action: str,
    until: str | None = None,
    why: str | None = None,
    expected_fingerprint: str | None = None,
) -> dict:
    """Persist a fingerprint-bound dismiss/snooze/reopen for one proposal."""
    root = Path(root)
    run, proposal, _store = _resolve(root, ref)
    fingerprint = _recompute_fingerprint(root, run, proposal)
    if expected_fingerprint and fingerprint != expected_fingerprint:
        raise AdoptionProposalError(
            "REVIEW_ITEM_CHANGED",
            f"the adoption proposal signal changed; refresh the queue and inspect {ref} again",
        )
    result = review_state_module.ReviewStateStore(root).apply(
        proposal.get("review_id") or "",
        fingerprint,
        action=action,
        until=until,
        why=why,
    )
    result["ref"] = ref
    result["kind"] = proposal.get("kind")
    result["run_id"] = run.get("run_id")
    result["categories"] = [proposal.get("kind")]
    return result


def assemble_context(
    root: Path, *, ref: str, expected_fingerprint: str | None = None, **caps: Any
) -> dict:
    """Bounded deterministic context pack for one proposal incl. a live binding check."""
    root = Path(root)
    max_chars = max(
        1, min(int(caps.get("max_body_chars", caps.get("max_chars_per_source", 2000)) or 2000), 20_000)
    )
    run, proposal, _store = _resolve(root, ref)
    fingerprint = _recompute_fingerprint(root, run, proposal)
    stored_bindings = proposal.get("bindings") or {}
    binding_check = []
    for path, expected in (stored_bindings.get("sources") or {}).items():
        current = _hash_file(root, path)
        binding_check.append(
            {
                "path": path,
                "expected_sha256": expected,
                "current_sha256": current,
                "changed": current != expected,
            }
        )

    payload = proposal.get("payload") or {}
    target = None
    target_path = (
        payload.get("old_path") or payload.get("subject_path") or payload.get("from")
    )
    if target_path:
        try:
            page = get_page_module.get_page(root, path=_clean(target_path))
            target = {
                "path": page.path,
                "content_hash": page.content_hash,
                "excerpt": page.body[:max_chars],
                "excerpt_truncated": len(page.body) > max_chars,
            }
        except Exception:  # noqa: BLE001 - a missing target is reported, not fatal
            target = {"path": _clean(target_path), "content_hash": None, "missing": True}

    return {
        "mode": "adoption",
        "mutated": False,
        "ref": ref,
        "review_id": proposal.get("review_id"),
        "run_id": run.get("run_id"),
        "run_ref": run.get("run_ref"),
        "kind": proposal.get("kind"),
        "why": proposal.get("why"),
        "status": proposal.get("status"),
        "fingerprint": fingerprint,
        "expected_fingerprint": expected_fingerprint,
        "fingerprint_changed": bool(
            expected_fingerprint and fingerprint != expected_fingerprint
        ),
        "payload": payload,
        "findings": proposal.get("findings") or [],
        "contract_findings": proposal.get("contract_findings") or [],
        "reviewed_none_required": bool(proposal.get("reviewed_none_required")),
        "committable_after_review": bool(proposal.get("committable_after_review")),
        "binding_check": binding_check,
        "target": target,
    }


# --------------------------------------------------------------------------- #
# apply-proposal (routes EXCLUSIVELY through governed leaves)
# --------------------------------------------------------------------------- #
def apply_proposal(
    root: Path,
    *,
    ref: str,
    expected_fingerprint: str | None,
    why: str | None,
    expected_hash: str | None = None,
) -> dict:
    """Approve one proposal through a governed leaf after live re-validation.

    ``expected_fingerprint`` and ``why`` are REQUIRED (relation-queue's
    required-not-optional stance). Everything is re-validated live; a changed bound
    source refuses with ``REVIEW_ITEM_CHANGED`` and nothing is written; the
    governed op's own compare-and-swap fires last.
    """
    root = Path(root)
    if not why or not str(why).strip():
        raise AdoptionProposalError(
            "INVALID_APPLY", "apply-proposal requires an approver rationale (`why`)"
        )
    if not expected_fingerprint or not str(expected_fingerprint).strip():
        raise AdoptionProposalError(
            "INVALID_APPLY", "apply-proposal requires `expected_fingerprint` from the review"
        )

    run, proposal, store = _resolve(root, ref)
    if proposal.get("status") == "applied":
        return {"applied": True, "ref": ref, "already_applied": True, **(proposal.get("applied") or {})}
    if proposal.get("status") == "invalid":
        raise AdoptionProposalError(
            "PROPOSAL_INVALID", "this proposal failed validation and can never be applied"
        )
    if proposal.get("status") == "applying":
        raise AdoptionProposalError(
            "APPLY_IN_FLIGHT",
            "a previous approval of this proposal may have been interrupted "
            "mid-write; verify whether its result landed before retrying",
        )

    kind = proposal.get("kind") or ""
    payload = proposal.get("payload") or {}

    # Re-validate everything live.
    findings = _validate(root, kind, payload, run)
    if findings:
        first = findings[0]
        raise AdoptionProposalError(
            "REVIEW_ITEM_CHANGED",
            f"the proposal is no longer valid ({first['code']}: {first['detail']}); "
            f"refresh and inspect {ref} again",
        )

    fingerprint = _recompute_fingerprint(root, run, proposal)
    if fingerprint != expected_fingerprint:
        raise AdoptionProposalError(
            "REVIEW_ITEM_CHANGED",
            f"the proposal signal changed since review; refresh and inspect {ref} again",
        )
    changed = _rehash_bindings(root, proposal.get("bindings") or {})
    if changed:
        raise AdoptionProposalError(
            "REVIEW_ITEM_CHANGED",
            f"a bound source changed since submission ({changed[0]['path']}); "
            f"refresh and inspect {ref} again",
        )

    # Persist the transient `applying` marker BEFORE the governed mutation so a
    # crash between the mutation and its completion record stays visible and a
    # blind retry cannot duplicate the write (mirrors the run's `applying`).
    _persist_proposal(store, run["run_id"], {**proposal, "status": "applying"})
    try:
        result = _route_apply(
            root, kind, payload, why=str(why).strip(), expected_hash=expected_hash
        )
    except Exception:
        # A clean refusal (drift, CAS) wrote nothing: restore `proposed` so the
        # reviewer can refresh and retry.
        _persist_proposal(store, run["run_id"], proposal)
        raise

    proposal["status"] = "applied"
    proposal["applied"] = {
        "at": _now_iso(),
        "result_path": result.get("result_path"),
        "result_ref": result.get("result_ref"),
        "why": str(why).strip(),
    }
    _persist_proposal(store, run["run_id"], proposal)

    return {
        "applied": True,
        "mode": "adoption",
        "mutated": True,
        "ref": ref,
        "kind": kind,
        "run_id": run.get("run_id"),
        "result_path": result.get("result_path"),
        "result_ref": result.get("result_ref"),
        "why": str(why).strip(),
        "result": result.get("raw"),
    }


def _persist_proposal(store: Any, run_id: str, proposal: dict) -> None:
    """Replace one proposal record in proposals.json by proposal_id."""
    saved = store.load_proposals(run_id)
    for idx, existing in enumerate(saved.get("proposals") or []):
        if existing.get("proposal_id") == proposal.get("proposal_id"):
            saved["proposals"][idx] = proposal
            break
    store.save_proposals(run_id, saved)


def _bullet(relation_type: str, to_path: str) -> str:
    destination = _clean(to_path).removesuffix(".md")
    return f"- {relation_type} [[{destination}]]"


def _reviewed_creation(op: Any, root: Path, base_kwargs: dict, why: str) -> dict:
    """Two-phase governed creation honoring the semantic write contract.

    Validate first; when the contract requires an explicit relation review
    (an agent-compiled page rarely carries a typed relation yet), commit with
    the approver's reviewed-none disposition bound to the validated draft —
    the approval rationale IS the review reason, and the relation-debt queue
    resurfaces the page later. Non-review blockers propagate from the create.
    """
    validation = op(root, validate_only=True, **base_kwargs)
    review_kwargs: dict = {}
    if not validation.get("committable_without_review"):
        review_kwargs = {
            "relation_disposition": "reviewed_none",
            "relation_review_hash": validation.get("draft_hash"),
            "relation_review_reason": why,
        }
    return op(
        root,
        draft_id=validation.get("draft_id"),
        draft_hash=validation.get("draft_hash"),
        draft_token=validation.get("draft_token"),
        **review_kwargs,
        **base_kwargs,
    )


def _route_apply(
    root: Path, kind: str, payload: dict, *, why: str, expected_hash: str | None
) -> dict:
    """Dispatch to the ONE governed leaf that may realize this kind."""
    from . import commands as commands_module
    from . import edit as edit_module

    if kind == "compilation":
        result = _reviewed_creation(
            commands_module.op_remember,
            root,
            {
                "content": payload.get("content") or "",
                "title": payload.get("title") or "",
                "note_type": payload.get("note_type") or "insight",
                "sources": [_clean(s) for s in (payload.get("sources") or [])],
                "tags": payload.get("tags"),
                "project": payload.get("project"),
                "suggestions": False,
            },
            why,
        )
        return {"result_path": result.get("path"), "result_ref": result.get("ref"), "raw": result}

    if kind == "entity":
        result = commands_module.op_link(
            root,
            entity_type=payload.get("entity_type") or "",
            name=payload.get("name") or "",
            summary=payload.get("summary") or "",
            slug=payload.get("slug"),
            why_in_kb=payload.get("why_in_kb"),
            tags=payload.get("tags"),
            connections=payload.get("connections"),
            affiliation=payload.get("affiliation"),
            relationship=payload.get("relationship"),
            domain=payload.get("domain"),
            language=payload.get("language"),
            repo=payload.get("repo"),
            license=payload.get("license"),
            used_in=payload.get("used_in"),
            decided=payload.get("decided"),
            project=payload.get("project"),
            decision_status=payload.get("decision_status"),
        )
        return {"result_path": result.get("path"), "result_ref": result.get("ref"), "raw": result}

    if kind == "relation":
        return _apply_relation_edit(
            root,
            edit_module,
            from_path=_clean(payload.get("from")),
            to_path=_clean(payload.get("to")),
            relation_type=str(payload.get("relation_type") or ""),
            why=why,
            expected_hash=expected_hash,
        )

    if kind == "supersession":
        result = _reviewed_creation(
            commands_module.op_replace_memory,
            root,
            {
                "old_path": _clean(payload.get("old_path")),
                "content": payload.get("content") or "",
                "title": payload.get("title") or "",
                "note_type": payload.get("note_type") or "insight",
                "reason": why,
                "sources": payload.get("sources"),
                "tags": payload.get("tags"),
                "project": payload.get("project"),
            },
            why,
        )
        return {
            "result_path": result.get("new_path") or result.get("path"),
            "result_ref": result.get("new_ref"),
            "raw": result,
        }

    if kind == "reconciliation":
        if payload.get("resolution") == "relate":
            return _apply_relation_edit(
                root,
                edit_module,
                from_path=_clean(payload.get("subject_path")),
                to_path=_clean(payload.get("duplicate_of")),
                relation_type=str(payload.get("relation_type") or "duplicates"),
                why=why,
                expected_hash=expected_hash,
            )
        result = _reviewed_creation(
            commands_module.op_replace_memory,
            root,
            {
                "old_path": _clean(payload.get("subject_path")),
                "content": payload.get("content") or "",
                "title": payload.get("title") or "",
                "note_type": payload.get("note_type") or "insight",
                "reason": why,
                "sources": payload.get("sources"),
                "tags": payload.get("tags"),
                "project": payload.get("project"),
            },
            why,
        )
        return {
            "result_path": result.get("new_path") or result.get("path"),
            "result_ref": result.get("new_ref"),
            "raw": result,
        }

    raise AdoptionProposalError("UNKNOWN_KIND", f"cannot apply unknown kind {kind!r}")


def _apply_relation_edit(
    root: Path,
    edit_module: Any,
    *,
    from_path: str,
    to_path: str,
    relation_type: str,
    why: str,
    expected_hash: str | None,
) -> dict:
    """Author one reviewed Relations bullet via the governed edit (CAS = expected_hash).

    ``expected_hash`` is REQUIRED (mirrors ``relation_queue.accept``): the reviewer
    echoes the target page's ``content_hash`` and the edit's compare-and-swap
    refuses on drift (``STALE_EDIT``). ``create_missing_section`` adds ``##
    Relations`` when the page has none, exactly like the relation-queue accept.
    """
    if not expected_hash:
        raise AdoptionProposalError(
            "INVALID_APPLY",
            "applying a relation requires `expected_hash` from the target page",
        )
    bullet = _bullet(relation_type, to_path)
    try:
        result = edit_module.edit(
            root,
            path=from_path,
            why=why,
            heading="Relations",
            section_position="append",
            new_string=bullet,
            expected_hash=expected_hash,
            create_missing_section=True,
        )
    except edit_module.EditError as exc:
        raise AdoptionProposalError(exc.code, exc.reason) from exc
    payload = result.as_dict() if hasattr(result, "as_dict") else result
    return {"result_path": from_path, "result_ref": None, "raw": payload, "bullet": bullet}
