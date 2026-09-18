"""The current-state resolver: what is true about an anchor right now.

This closes one specific failure the compiler exists to prevent: recommending a
resource that is no longer where the user can use it. Prose notes are written
once and stay written; observed state changes. So state is read Records-first —
the newest item in a collection that claims the anchor — then from the anchor
page's own status field, then from the newest active note in its neighbourhood.

Every entry NAMES its source, because "the sled is abroad, per a Records
observation dated 2026-09-10" and "the sled is abroad, per a note someone wrote
in March" are different claims and the agent must be able to tell them apart.
There is no lifecycle model here and no inference: three ordered lookups, each
one reading authored values only.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .working_set_index import normalize, terms_of

log = logging.getLogger(__name__)

RECORDS = "records"
PROFILE = "profile"
NOTE = "note"

#: Field names that state an observed status, most specific first.
_STATE_FIELDS: tuple[str, ...] = (
    "state",
    "status",
    "condition",
    "location",
    "value",
    "balance",
    "remaining",
)
#: Field names that date an observation, most specific first.
_DATE_FIELDS: tuple[str, ...] = ("observed_on", "occurred_on", "as_of", "date", "updated")

STATEMENT_MAX_CHARS = 200
#: Anchor kinds that HAVE a current state. A method or a precedent does not.
STATEFUL_KINDS = frozenset({"resource", "collection", "plan"})


def current_state_for(
    vault_root: Path,
    *,
    anchors: Sequence[Any],
    purpose: str | None = None,
) -> tuple[dict[str, Any], ...]:
    """Resolve each stateful anchor's current state, Records first."""
    root = Path(vault_root)
    manifests = _records_manifests(root)
    out: list[dict[str, Any]] = []
    for anchor in anchors:
        if getattr(anchor, "kind", "") not in STATEFUL_KINDS:
            continue
        entry = (
            _from_records(root, anchor, manifests, purpose=purpose)
            or _from_profile(root, anchor)
            or _from_neighbourhood(root, anchor)
        )
        if entry is not None:
            out.append(entry)
    return tuple(out)


def _records_manifests(vault_root: Path) -> tuple[Any, ...]:
    from . import structured_collections

    try:
        manifests = structured_collections.discover_collections(vault_root)
    except Exception:  # noqa: BLE001 - an unreadable manifest costs its state entry
        log.debug("current state: collection discovery failed", exc_info=True)
        return ()
    return tuple(
        manifest
        for manifest in manifests
        if str(getattr(manifest, "semantic_profile", "")) == "records"
    )


def _claiming_manifest(anchor: Any, manifests: Sequence[Any]) -> Any | None:
    """The collection that claims this anchor, through the existing claims router."""
    path = str(getattr(anchor, "path", "") or "")
    for manifest in manifests:
        if str(getattr(manifest, "path", "")) == path:
            return manifest
    if not manifests:
        return None
    from . import collection_claims, record_governance

    targets = []
    for manifest in manifests:
        claims = record_governance.effective_claims(manifest, None)
        if not claims:
            continue
        targets.append(
            collection_claims.RoutingTarget(
                collection=str(getattr(manifest, "path", "")),
                title=str(getattr(manifest, "title", "")),
                claims=claims,
                natural_key=tuple(getattr(getattr(manifest, "schema", None), "natural_key", ())),
            )
        )
    if not targets:
        return None
    decision = collection_claims.route(
        terms_of(str(getattr(anchor, "title", ""))), targets
    )
    if not isinstance(decision, Mapping):
        return None
    winner = str(decision.get("collection") or "")
    for manifest in manifests:
        if str(getattr(manifest, "path", "")) == winner:
            return manifest
    return None


def _from_records(
    vault_root: Path,
    anchor: Any,
    manifests: Sequence[Any],
    *,
    purpose: str | None,
) -> dict[str, Any] | None:
    manifest = _claiming_manifest(anchor, manifests)
    if manifest is None:
        return None
    from . import record_governance

    fields = tuple(getattr(getattr(manifest, "schema", None), "fields", {}) or ())
    date_column = next((name for name in _DATE_FIELDS if name in fields), None)
    try:
        result = record_governance.query_collection(
            vault_root,
            manifest,
            semantic_profile="records",
            sort_by=date_column,
            descending=True,
            limit=1,
        )
    except Exception:  # noqa: BLE001 - a refused or unreadable collection falls through
        log.debug("current state: records query failed", exc_info=True)
        return None
    del purpose  # the release plane decides disclosure; purpose rides the principal
    rows = list(getattr(result, "rows", ()) or ())
    if not rows:
        return None
    row = rows[0]
    if not isinstance(row, Mapping):
        return None
    statement = _statement_from(row, fields)
    if not statement:
        return None
    return {
        "anchor": _anchor_ref(anchor),
        "source": RECORDS,
        "as_of": str(row.get(date_column) or "") if date_column else "",
        "statement": statement,
    }


def _statement_from(row: Mapping[str, Any], fields: Sequence[str]) -> str:
    """Render the authored values, never a sentence the server invented."""
    for name in _STATE_FIELDS:
        value = row.get(name)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return f"{name}: {str(value).strip()}"[:STATEMENT_MAX_CHARS]
    parts = [
        f"{name}: {str(row[name]).strip()}"
        for name in fields
        if name in row
        and isinstance(row[name], (str, int, float))
        and str(row[name]).strip()
    ]
    return " · ".join(parts)[:STATEMENT_MAX_CHARS]


def _from_profile(vault_root: Path, anchor: Any) -> dict[str, Any] | None:
    from . import find_corpus

    rel = str(getattr(anchor, "path", "") or "")
    if not rel or not rel.endswith(".md"):
        return None
    page = find_corpus.CACHE.get(vault_root / rel, vault_root)
    if page is None:
        return None
    frontmatter = page.frontmatter if isinstance(page.frontmatter, dict) else {}
    for name in _STATE_FIELDS:
        value = frontmatter.get(name)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return {
                "anchor": _anchor_ref(anchor),
                "source": PROFILE,
                "as_of": str(frontmatter.get("updated") or ""),
                "statement": f"{name}: {str(value).strip()}"[:STATEMENT_MAX_CHARS],
            }
    return None


def _from_neighbourhood(vault_root: Path, anchor: Any) -> dict[str, Any] | None:
    from . import find_corpus

    best: tuple[str, str] | None = None
    for rel in sorted(getattr(anchor, "neighbourhood", ()) or ()):
        if not rel.endswith(".md"):
            continue
        page = find_corpus.CACHE.get(vault_root / rel, vault_root)
        if page is None:
            continue
        frontmatter = page.frontmatter if isinstance(page.frontmatter, dict) else {}
        if normalize(frontmatter.get("status") or "active") != "active":
            continue
        updated = str(frontmatter.get("updated") or "")
        title = str(frontmatter.get("title") or page.title or "").strip()
        if not title:
            continue
        if best is None or updated > best[0]:
            best = (updated, f"latest active note: {title}")
    if best is None:
        return None
    return {
        "anchor": _anchor_ref(anchor),
        "source": NOTE,
        "as_of": best[0],
        "statement": best[1][:STATEMENT_MAX_CHARS],
    }


def _anchor_ref(anchor: Any) -> str:
    return str(
        getattr(anchor, "ref", None)
        or getattr(anchor, "path", "")
        or getattr(anchor, "anchor_id", "")
    )
