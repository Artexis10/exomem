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
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from datetime import date
from typing import Any

from .collection_store.preview import bound_writer, canonical_read, selected_projection_writer
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


#: Anchor kinds whose current state is a page, not a collection row: the
#: settled facts of an entity or hub live in the current-state page its own page
#: DECLARES. A project anchor has no page of its own, so it declares none.
CANONICAL_KINDS = frozenset({"project", "entity", "hub"})
CANONICAL = "canonical_page"
#: Frontmatter key on the anchor's own page that names its current-state page.
#: The only way a page becomes canonical: never "the newest page with a fact".
CURRENT_PAGE_FIELD = "current_state_page"
CANONICAL_CATEGORIES = ("fact", "config")
CANONICAL_UNIT_LIMIT = 64


def bounded_statement(text: str, limit: int = STATEMENT_MAX_CHARS) -> str:
    """`text` cut to at most `limit` characters, never inside a `[[link]]`.

    A cut inside a link leaves part of a page name with no closing brackets,
    which the egress guard cannot read as a link any more, so a withheld page's
    name would be served. The cut moves back to before the link instead.
    """
    if len(text) <= limit:
        return text
    cut = text[:limit]
    opened = cut.rfind("[[")
    if opened != -1 and cut.find("]]", opened) == -1:
        return cut[:opened].rstrip()
    return cut


def _named_current_page(
    vault_root: Path, anchor: Any, *, visible: Callable[[str], bool] | None = None
) -> str:
    """The neighbourhood page the anchor's own page declares current, if any."""
    from . import find_corpus

    rel = str(getattr(anchor, "path", "") or "")
    if not rel.endswith(".md"):
        return ""
    page = find_corpus.CACHE.get(vault_root / rel, vault_root)
    frontmatter = getattr(page, "frontmatter", None)
    named = frontmatter.get(CURRENT_PAGE_FIELD) if isinstance(frontmatter, dict) else None
    if not isinstance(named, str):
        return ""
    target = normalize(named.strip().strip("[]").split("|")[0].split("#")[0].removesuffix(".md"))
    if not target:
        return ""
    matches = []
    for path in sorted(getattr(anchor, "neighbourhood", ()) or ()):
        if visible is not None and not visible(path):
            continue
        stem = path.removesuffix(".md")
        if target in (normalize(stem), normalize(stem.rsplit("/", 1)[-1])):
            matches.append(path)
    # A bare name shared by two pages names neither unambiguously. The author
    # can use the full path to declare which page holds current state.
    return matches[0] if len(matches) == 1 else ""


def _from_canonical_page(
    vault_root: Path, anchor: Any, *, visible: Callable[[str], bool] | None = None
) -> dict[str, Any] | None:
    """The leading current-state unit of the page the anchor declares current.

    The entry carries the page's `path`, so the egress guard decides it like any
    other page reference, and the unit's OWN authored time as `as_of`; a unit
    that authors none is labelled with its page's time instead.
    """
    named = _named_current_page(vault_root, anchor, visible=visible)
    if not named:
        return None
    from . import find as find_module
    from . import ranking_config, structured_filters
    from . import working_set_currency

    try:
        snapshot = find_module.FreshnessSnapshot(vault_root)
        plan = structured_filters.compile_filter(
            None,
            shortcuts=structured_filters.FilterShortcuts(categories=CANONICAL_CATEGORIES),
        )
        hits = find_module._find_semantic_units(
            vault_root, query="", limit=CANONICAL_UNIT_LIMIT, scope="kb", plan=plan,
            snapshot=snapshot, prefer_active=True, config=ranking_config.DEFAULT_RANKING,
            mode="keyword", degraded_out=None, failed_out=None,
            allowed_parent_paths={named},
            recall_checkpoint=snapshot.recall_checkpoint("kb"), repair=False,
            max_catalog_candidates=CANONICAL_UNIT_LIMIT,
        )
    except Exception:  # noqa: BLE001 - an unreadable unit index costs this entry only
        log.debug("current state: canonical page lookup failed", exc_info=True)
        return None
    from . import working_set

    # A unit the reader's egress guard would remove never leads: the next one
    # is the current state, as on a page without it (`working_set.hits_in_view`).
    hits = working_set.hits_in_view(
        [
            hit
            for hit in hits
            if str(getattr(hit, "parent_path", "") or "") == named
            and not getattr(hit, "parent_superseded_by", None)
        ],
        visible,
    )
    if not hits:
        return None

    def _line(hit: Any) -> int:
        span = getattr(hit, "source_span", None) or {}
        return int(span.get("start_line", 0) or 0)

    lead = min(hits, key=_line)
    content = str(getattr(lead, "content", "") or getattr(lead, "excerpt", "") or "").strip()
    if not content:
        return None
    entry = {
        "anchor": _anchor_ref(anchor),
        "source": CANONICAL,
        "path": named,
        "as_of": working_set_currency.own_time(getattr(lead, "context", None)),
        "statement": bounded_statement(content),
    }
    page_updated = str(getattr(lead, "parent_updated", "") or "")
    if not entry["as_of"] and page_updated:
        entry["page_updated"] = page_updated
    return entry


@canonical_read
def current_state_for(
    vault_root: Path,
    *,
    anchors: Sequence[Any],
    purpose: str | None = None,
    visible: Callable[[str], bool] | None = None,
    index_generation: int | None = None,
    index_token: tuple[int, int, int] | None = None,
    state_fields: Sequence[str] | None = None,
    date_fields: Sequence[str] | None = None,
) -> tuple[dict[str, Any], ...]:
    """Resolve each stateful anchor's current state, Records first.

    `visible` filters canonical candidates before ambiguity resolution and
    neighbourhood pages before choosing the newest note. `None` retains the
    owner's unrestricted view. Each entry carries its source page's path for
    the release guard.

    `index_generation` names the activation index generation whose published
    manifests this lookup may route with, and `index_token` the sidecar that
    issued it — a generation means nothing without the sidecar it came from. Routing is evidence and may be that
    stale; the collection it routes to is then read from disk again before any
    governed query runs against it. Left unset — a caller with no index at all
    — the manifests are discovered directly, exactly as they always were.

    `state_fields`/`date_fields` default to the vault's effective
    activation-conventions registry (`make-activation-conventions-vault-
    owned`), read here when either is omitted — the shipped values equal the
    module's former `_STATE_FIELDS`/`_DATE_FIELDS` constants exactly, so a
    vault with no override reads state precisely as before.
    """
    root = Path(vault_root)
    if state_fields is None or date_fields is None:
        from . import activation_conventions

        conventions = activation_conventions.load_conventions(root).conventions
        if state_fields is None:
            state_fields = conventions.state_fields
        if date_fields is None:
            date_fields = conventions.date_fields
    state_fields = tuple(state_fields)
    date_fields = tuple(date_fields)
    manifests = _records_manifests(root, index_generation, index_token)
    out: list[dict[str, Any]] = []
    for anchor in anchors:
        if getattr(anchor, "kind", "") in CANONICAL_KINDS:
            entry = _from_canonical_page(root, anchor, visible=visible)
            if entry is not None:
                out.append(entry)
            continue
        if getattr(anchor, "kind", "") not in STATEFUL_KINDS:
            continue
        entry = (
            _from_records(
                root, anchor, manifests, purpose=purpose, state_fields=state_fields, date_fields=date_fields
            )
            or _from_profile(root, anchor, state_fields=state_fields)
            or _from_neighbourhood(root, anchor, visible=visible)
        )
        if entry is not None:
            out.append(entry)
    return temper_weak_entries(out, anchors)


#: Evidence that names an anchor by shared WORDS alone. An anchor whose every
#: evidence kind is in this set resolved on vocabulary, which two unrelated
#: pages can share; its state is not vouched for by anything else.
WEAK_EVIDENCE = frozenset(
    {"lexical_overlap", "rare_term", "recency", "category_match", "usage_prior", "retrieval"}
)
#: A state older than this, on a weakly-resolved anchor, is held back rather
#: than served as current. Younger state is served labelled with its age.
WEAK_STATE_MAX_AGE_DAYS = 30


def temper_weak_entries(
    entries: Sequence[Mapping[str, Any]],
    anchors: Sequence[Any],
    *,
    today: "date | None" = None,
) -> tuple[dict[str, Any], ...]:
    """Hold back or age-label current state from weakly-resolved anchors.

    Strongly-resolved anchors (an alias, a claim, a vector band, corroboration,
    the agent's own pick) are untouched. A weak one's state older than
    `WEAK_STATE_MAX_AGE_DAYS` is dropped; recent state says how it was resolved
    and how old it is; undated state is served saying how it was resolved.
    """
    from datetime import date as _date

    now = today or _date.today()
    weak = {
        _anchor_ref(anchor)
        for anchor in anchors
        if (evidence := frozenset(getattr(anchor, "evidence", ()) or ()))
        and evidence <= WEAK_EVIDENCE
    }
    out: list[dict[str, Any]] = []
    for entry in entries:
        if entry.get("anchor") not in weak:
            out.append(dict(entry))
            continue
        as_of = str(entry.get("as_of") or "")
        if not as_of:
            # Undated is not old: most authored state carries no date of its
            # own. It is served, saying how it was resolved; a page time it
            # already carries (`page_updated`) stays beside it.
            out.append({**entry, "resolved_by": "lexical"})
            continue
        try:
            age = (now - _date.fromisoformat(as_of[:10])).days
        except ValueError:
            continue
        if age > WEAK_STATE_MAX_AGE_DAYS:
            continue
        out.append({**entry, "resolved_by": "lexical", "age_days": max(age, 0)})
    return tuple(out)


def _records_manifests(
    vault_root: Path,
    index_generation: int | None,
    index_token: tuple[int, int, int] | None = None,
) -> tuple[Any, ...]:
    """The Records manifests this lookup may ROUTE with — never govern with.

    Served from the activation index's published set when the caller named a
    generation, which is what keeps the request path off the filesystem: the
    sweep that produced it ran during the index update. Everything read from
    here is a claim used to decide WHICH collection a stateful anchor belongs
    to; the collection's governance is read fresh in `_governing_manifest`
    before a single record is queried.
    """
    if index_generation is not None and bound_writer(vault_root) is None:
        from . import working_set_index

        try:
            return working_set_index.records_manifests(
                vault_root, index_generation, token=index_token
            )
        except Exception:  # noqa: BLE001 - an unreadable manifest costs its state entry
            log.debug("current state: collection discovery failed", exc_info=True)
            return ()
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


def _governing_manifest(vault_root: Path, routed: Any) -> Any | None:
    """Re-read the routed collection's own manifest, for the governed query.

    The routing above may be as stale as the index; this may not. A manifest is
    a governance document — its profile, its schema, its policies decide what a
    query may return — so the collection that is about to be queried is read
    from disk on THIS request, not taken from a set discovered whenever the
    index last ran. One guarded read of one manifest (typically zero to two per
    request), never a sweep.

    Fails closed for its own collection only: a manifest that has vanished, no
    longer parses, or no longer declares the Records profile yields no
    current-state entry for that collection, and the packet still serves.

    There are deliberately TWO guards for this and neither may be removed on
    the strength of the other: `record_governance.query_collection` also
    re-resolves the collection from its path before querying it. This one
    exists so the freshness is a property of the caller that needs it rather
    than a coincidence of what its callee happens to do today, and so a profile
    change is refused before the query rather than inside it.
    """
    rel = str(getattr(routed, "path", "") or "")
    if not rel:
        return None
    from . import structured_collections

    try:
        manifest = structured_collections.load_manifest(vault_root, rel)
    except Exception:  # noqa: BLE001 - an unreadable manifest costs its state entry
        log.debug("current state: collection manifest could not be re-read", exc_info=True)
        return None
    if str(getattr(manifest, "semantic_profile", "")) != "records":
        return None
    return manifest


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
    state_fields: Sequence[str] = _STATE_FIELDS,
    date_fields: Sequence[str] = _DATE_FIELDS,
) -> dict[str, Any] | None:
    routed = _claiming_manifest(anchor, manifests)
    if routed is None:
        return None
    # The routing decision above may be stale; what is governed below may not.
    manifest = _governing_manifest(vault_root, routed)
    if manifest is None:
        return None
    from . import record_governance

    fields = tuple(getattr(getattr(manifest, "schema", None), "fields", {}) or ())
    date_column = next((name for name in date_fields if name in fields), None)
    try:
        result = record_governance.query_collection(
            vault_root,
            manifest,
            semantic_profile="records",
            sort_by=date_column,
            descending=True,
            limit=1,
            # Bound the work: govern only the one row this lookup returns,
            # never every record in the collection, and never build link
            # governance's vault-wide candidate index to do it -- a bare
            # title or memory-reference link then resolves exactly as it
            # already does whenever that index is incomplete (withheld), so
            # this can only withhold more than the ordinary eager path,
            # never disclose more. See record_formats.query_collection's
            # `late_link_projection` docstring for the exact contract.
            late_link_projection=True,
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
    statement = _statement_from(row, fields, state_fields=state_fields)
    if not statement:
        return None
    return {
        "anchor": _anchor_ref(anchor),
        "source": RECORDS,
        "path": str(manifest.path),
        "as_of": str(row.get(date_column) or "") if date_column else "",
        "statement": statement,
    }


def _statement_from(
    row: Mapping[str, Any], fields: Sequence[str], *, state_fields: Sequence[str] = _STATE_FIELDS
) -> str:
    """Render the authored values, never a sentence the server invented."""
    for name in state_fields:
        value = row.get(name)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return bounded_statement(f"{name}: {str(value).strip()}")
    parts = [
        f"{name}: {str(row[name]).strip()}"
        for name in fields
        if name in row
        and isinstance(row[name], (str, int, float))
        and str(row[name]).strip()
    ]
    return bounded_statement(" · ".join(parts))


def _from_profile(
    vault_root: Path, anchor: Any, *, state_fields: Sequence[str] = _STATE_FIELDS
) -> dict[str, Any] | None:
    rel = str(getattr(anchor, "path", "") or "")
    if not rel or not rel.endswith(".md"):
        return None
    profile = _profile_data(vault_root, rel)
    if profile is None:
        return None
    frontmatter, _ = profile
    for name in state_fields:
        value = frontmatter.get(name)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return {
                "anchor": _anchor_ref(anchor),
                "source": PROFILE,
                "path": rel,
                "as_of": str(frontmatter.get("updated") or ""),
                "statement": bounded_statement(f"{name}: {str(value).strip()}"),
            }
    return None


def _from_neighbourhood(
    vault_root: Path, anchor: Any, *, visible: Callable[[str], bool] | None = None
) -> dict[str, Any] | None:
    best: tuple[str, str, str] | None = None
    for rel in sorted(getattr(anchor, "neighbourhood", ()) or ()):
        if not rel.endswith(".md") or (visible is not None and not visible(rel)):
            continue
        profile = _profile_data(vault_root, rel)
        if profile is None:
            continue
        frontmatter, page_title = profile
        if normalize(frontmatter.get("status") or "active") != "active":
            continue
        updated = str(frontmatter.get("updated") or "")
        title = str(frontmatter.get("title") or page_title or "").strip()
        if not title:
            continue
        if best is None or updated > best[0]:
            best = (updated, f"latest active note: {title}", rel)
    if best is None:
        return None
    return {
        "anchor": _anchor_ref(anchor),
        "source": NOTE,
        "path": best[2],
        "as_of": best[0],
        "statement": bounded_statement(best[1]),
    }


def _profile_data(vault_root: Path, rel: str) -> tuple[Mapping[str, Any], str] | None:
    """Read collection-owned profiles canonically; ordinary knowledge stays Markdown."""
    from . import find_corpus, recall_policy

    writer = selected_projection_writer(vault_root, rel)
    if writer is not None and recall_policy.is_structured_only_path(vault_root, rel):
        from . import record_formats, structured_collections

        with writer.read_snapshot():
            identity = writer.connection.execute(
                "SELECT collection_id,item_key FROM items WHERE view_path=?", (rel,),
            ).fetchone()
            if identity is None:
                return None
            try:
                manifest = structured_collections.load_manifest(vault_root, identity[0])
                snapshot = record_formats.load_adapter(vault_root, manifest).read()
            except structured_collections.CollectionError:
                return None
            record = next((record for record in snapshot.records
                           if record.identity.key == identity[1]), None)
            if record is None:
                return None
            return record.values, str(record.values.get("title") or "")
    page = find_corpus.CACHE.get(vault_root / rel, vault_root)
    if page is None:
        return None
    return page.frontmatter if isinstance(page.frontmatter, dict) else {}, page.title


def _anchor_ref(anchor: Any) -> str:
    return str(
        getattr(anchor, "ref", None)
        or getattr(anchor, "path", "")
        or getattr(anchor, "anchor_id", "")
    )
