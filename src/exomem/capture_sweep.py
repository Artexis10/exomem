"""The capture-sweep advisory: one bounded episode-completeness pass after a write.

Proactive capture on a hookless client is object-local. The agent writes the one
object it was thinking about, the turn ends, and the adjacent durable facts of the
same episode -- an operational quirk of a supplier, an outcome, a facet of a
recurring entity -- are lost until the user notices and asks.

Why this is a write-time advisory and not an audit family. Every family in
`audit.ALL_CATEGORIES` is a deterministic predicate over authored vault state,
with a fingerprint and a resolution condition. "The conversation held more durable
facts than were written" has neither: the evidence is not in the vault, and
nothing the user later writes can prove the episode was finished. It is also
unobservable to a family, because the server has no conversation window on a
hookless client -- remote HTTP is served `stateless_http=True`, deliberately, so
there is not even a per-conversation session id, and `due_state.emission_key`
already records that by degrading to one process key. So the signal rides the one
deterministic moment the substrate does have: the response to a durable write,
the same class of carrier as `structure_suggestion`.

The agent is the sole semantic decider. This module produces a bounded prompt and
two bounded hints; it never judges what is durable and never writes anything.

Three properties are worth stating because they differ from the due-state carrier
this seam shares:

**The ledger records writes, not deliveries.** `due_state` records `mark_emitted`
at the terminal, because its ledger answers "has this caller been TOLD this?".
This ledger answers "has this caller WRITTEN recently?", which is a fact about the
caller rather than about what reached them. A write that happened is a write that
happened whether or not its response carried an advisory, so it is recorded here,
at production. An advisory lost to a `legacy` detail costs the caller one
advisory and leaves the boundary honest.

**It is in memory and is not persisted**, by the rationale `due_state` records for
`_EMISSION`: "persisting it would make a server restart change what an agent is
told." A restart re-arms each caller once, which costs one advisory; a durable
file deciding what an agent is told costs more than that.

**The classes it names are examples, never an enumeration.** An agent reads a
closed list inside a capture contract as a boundary, which would make this
carrier narrow capture rather than widen it -- the opposite of the point.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, NamedTuple

log = logging.getLogger(__name__)

#: How long a caller must have been quiet for the next durable write to count as
#: the start of an episode. A module constant with NO environment knob: a tunable
#: here would be one more thing an operator can set wrong and no reviewer can see,
#: and the number is a product decision rather than a deployment one.
QUIET_SECONDS = 1800

BOUNDARY_QUIET_INTERVAL = "quiet-interval"
#: The closed boundary vocabulary. The terminal re-validates against its own copy.
BOUNDARIES = frozenset({BOUNDARY_QUIET_INTERVAL})

MAX_WRITTEN_RECENTLY = 8
MAX_UNPAGED_MENTIONS = 5
MAX_MENTION_CHARS = 64

#: Bounds on the write-time `entity_candidate` block
#: (`write-time-identity-candidates` spec, design D5): at most three
#: identities, each with at most eight linking pages. `MAX_DEPENDENCY_ROWS` is
#: how many rows the graph's link-dependency index returns are evaluated for
#: eligibility per name -- a wide fan-out costs a bounded read, never an
#: unbounded one.
MAX_CANDIDATE_IDENTITIES = 3
MAX_CANDIDATE_LINKING_PAGES = 8
MAX_DEPENDENCY_ROWS = 16

#: The one fixed sentence of guidance the `entity_candidate` block itself
#: carries (`write-time-identity-candidates` spec, task 2.4; design D2's
#: "resolve before you create and hydrate an existing Entity before you make
#: a second one"). Block-level, not per identity: it reaches exactly the
#: client that has no skill and no hook, at the moment it holds the block.
ENTITY_CANDIDATE_GUIDANCE = (
    "Resolve before you create an Entity, and hydrate an existing Entity "
    "before you make a second one."
)
#: Bounded rather than unbounded: a long-lived server must not accumulate one
#: entry per caller it has ever seen. Evicting the least recently written costs
#: at worst one extra advisory for a caller who had gone quiet anyway.
LEDGER_CAP = 512

#: The ask. One sentence, and deliberately about reuse value rather than about a
#: list of categories -- see the module docstring on enumerations.
#:
#: The closing clause is load-bearing rather than decorative. `rule` is the ONLY
#: prose on the wire: `consider` is a bare list with nothing around it, so an
#: agent that reads this block and not this repository has nothing else telling
#: it the list is open. Without the clause the block presents an enumeration,
#: which is precisely the hard boundary this carrier exists not to draw.
RULE = (
    "one bounded pass over the recent exchange for anything that would materially "
    "improve a later decision, lookup, repeated task, comparison or continuation; "
    "dedupe against written_recently and existing memory; stay silent when nothing "
    "qualifies. The classes in `consider` are examples, not a closed set"
)

#: Examples, not an enumeration. `rule` says so and every prose carrier says so.
CONSIDER: tuple[str, ...] = (
    "conclusion",
    "outcome or state change",
    "stable preference",
    "method or parameter",
    "entity facet",
    "operational or vendor quirk",
    "evidence worth preserving",
    "relation",
    "planning implication",
    "record implication",
)

#: Injected so a 1,800-second interval is testable. Monotonic, because the
#: boundary is an elapsed duration and a wall clock that steps backwards would
#: re-arm a caller who had just written.
_clock = time.monotonic

_LOCK = threading.Lock()
#: `key -> the monotonic time of that key's last durable write`, oldest first.
_LAST_WRITE: dict[tuple[str, str, str], float] = {}
#: `key -> that key's recent write references, newest first`, same eviction.
_RECENT: dict[tuple[str, str, str], list[str]] = {}

#: The fallback scope for stdio and the CLI, where the transport supplies no
#: identity at all. One key per process lifetime, which is exactly the scope of a
#: stdio conversation -- the same reasoning as `due_state._PROCESS_SESSION_KEY`.
_PROCESS_KEY = f"process:{uuid.uuid4().hex}"

#: `mcp_retry_scope()` tiers stable enough to key a ledger on. `session:` is not
#: stable under `stateless_http=True`, and `None` is not a scope at all.
_STABLE_SCOPE_PREFIXES = ("principal:", "bearer:")

#: Every reference this substrate puts on the wire is an `exomem://` URI.
_REF_SCHEME = "exomem://"
_MAX_REF_CHARS = 256


class _RegistryPage(NamedTuple):
    """The two attributes `entity_recurrence.registry_index` reads off a page.

    An adapter rather than a reimplementation: the registry predicate lives in
    one place and this supplies it the shape it already expects, from pages the
    write preflight had already parsed.
    """

    rel_path: str
    frontmatter: Any


def ledger_key(vault_root: Path | None = None) -> tuple[str, str, str] | None:
    """`(scope, client, vault)`, or None when this caller must not be keyed.

    Four tiers, and each is a different answer rather than a degraded one:

    - `principal:<hash>` over HTTP is a verified OAuth subject, stable across a
      token refresh. The intended key.
    - `bearer:<hash>` over HTTP is a hash of the credential itself, so a refresh
      mints a new key and re-arms the caller once. Accepted, with that cost.
    - `session:<id>` or no scope over HTTP is not stable under
      `stateless_http=True`. The answer is None -- emit nothing -- rather than
      collapsing every caller into one bucket, which would let one principal's
      write silence another's.
    - Outside any MCP call, identity is `None` by design and the process lifetime
      IS the conversation, so the process key applies.

    The vault is in the key because one process can serve more than one of them.
    Never raises: an unavailable transport is simply not a keyable caller.
    """
    try:
        from .command_surface import mcp_caller_identity, mcp_retry_scope

        identity = mcp_caller_identity()
        transport = identity.get("transport")
        client = identity.get("client_name") or ""
        scope = mcp_retry_scope()
    except Exception:  # noqa: BLE001 -- an unreadable caller is not an advisory
        log.debug("capture-sweep caller identity unavailable", exc_info=True)
        return None
    vault = str(vault_root or "")
    if transport is None or transport == "stdio":
        return (_PROCESS_KEY, "", vault)
    if (
        transport == "http"
        and isinstance(scope, str)
        and scope.startswith(_STABLE_SCOPE_PREFIXES)
    ):
        return (scope, client, vault)
    return None


def boundary(key: tuple[str, str, str] | None, now: float) -> str | None:
    """The boundary this write sits on, or None. Records nothing.

    A first-ever write from a key qualifies: a caller who has never written is a
    caller who has been quiet, and the alternative -- staying silent until the
    second write -- would miss exactly the single-object episode this exists for.
    """
    if key is None:
        return None
    with _LOCK:
        last = _LAST_WRITE.get(key)
    if last is None or (now - last) >= QUIET_SECONDS:
        return BOUNDARY_QUIET_INTERVAL
    return None


def record_write(
    key: tuple[str, str, str] | None, now: float, *, ref: str | None = None
) -> None:
    """Record one successful durable write against this key.

    Called for EVERY successful durable write, emitting or not: the interval is
    measured from the latest write, not from the latest advisory.
    """
    if key is None:
        return
    with _LOCK:
        _record_locked(key, now, ref if _valid_ref(ref) else None)


def _record_locked(key: tuple[str, str, str], now: float, reference: str | None) -> None:
    """The ledger write itself. Callers hold `_LOCK`; this never takes it."""
    if key not in _LAST_WRITE and len(_LAST_WRITE) >= LEDGER_CAP:
        oldest = next(iter(_LAST_WRITE))
        _LAST_WRITE.pop(oldest, None)
        _RECENT.pop(oldest, None)
    # Re-inserted rather than updated in place, so iteration order stays
    # least-recently-written first and the eviction above is honest.
    _LAST_WRITE.pop(key, None)
    _LAST_WRITE[key] = now
    if reference is not None:
        recent = _RECENT.setdefault(key, [])
        if reference in recent:
            recent.remove(reference)
        recent.insert(0, reference)
        del recent[MAX_WRITTEN_RECENTLY:]


def check_and_record(
    key: tuple[str, str, str] | None, now: float, *, ref: str | None = None
) -> str | None:
    """Decide the boundary AND record the write, under one lock acquisition.

    `boundary` then `record_write` is two acquisitions with a gap between them,
    and two threads that both read the ledger before either writes it both see a
    quiet interval and both emit. That is the one duplicate this carrier's whole
    governance exists to prevent, so the seam uses this rather than the pair.

    The two primitives stay public because they are separately meaningful and
    separately testable -- `boundary` records nothing, which is what makes it
    safe to ask twice in a test -- but no production path may call them in
    sequence.
    """
    if key is None:
        return None
    reference = ref if _valid_ref(ref) else None
    with _LOCK:
        last = _LAST_WRITE.get(key)
        found = (
            BOUNDARY_QUIET_INTERVAL
            if last is None or (now - last) >= QUIET_SECONDS
            else None
        )
        _record_locked(key, now, reference)
    return found


def recent_writes(key: tuple[str, str, str] | None) -> list[str]:
    """This key's recent write references, newest first."""
    if key is None:
        return []
    with _LOCK:
        return list(_RECENT.get(key, ()))


def hints(page_state: Any, corpus: Any = None) -> dict[str, list[str]]:
    """Bounded, best-effort names the committed page reaches for without a page.

    One digest-cached registry file read and nothing else. The wikilinks were
    parsed by the preflight and resolution runs against the corpus context that
    same preflight already built, so no writer lease is held and no vault-wide
    read happens here; the single file is the entity-type extension registry,
    which `entity_types.load_entity_types` opens and then serves from a digest
    cache, and which `audit`'s own recurrence sweep opens for the same reason.

    Nothing is promised. A first mention in plain prose with no wikilink is not
    detected, and detecting it would need exactly the semantic judgement this
    design refuses to put on the server. The block is useful without it.
    """
    if corpus is None:
        return {}
    try:
        links = tuple(getattr(page_state, "body_wikilinks", ()) or ())
        if not links:
            return {}
        from . import entity_recurrence, semantic_contract

        registry = _registry_index(corpus)
        mentions: list[str] = []
        seen: set[str] = set()
        for raw_target, _line in links:
            link = entity_recurrence.parse_link(raw_target)
            if link is None or not 0 < len(link.name) <= MAX_MENTION_CHARS:
                continue
            identity = entity_recurrence.identity_key(link.name)
            if not identity or identity in seen:
                continue
            seen.add(identity)
            resolution = semantic_contract._resolve_reference_wikilink_from_context(
                corpus, raw_target
            )
            if resolution.status != "unresolved":
                continue
            if registry is not None and registry.resolves(identity):
                continue
            mentions.append(link.name)
        if not mentions:
            return {}
        return {"unpaged_mentions": mentions[:MAX_UNPAGED_MENTIONS]}
    except Exception:  # noqa: BLE001 -- a hint never breaks a commit or a block
        log.debug("capture-sweep hint extraction failed (non-fatal)", exc_info=True)
        return {}


def entity_candidate(
    vault_root: Path | None,
    *,
    page_state: Any = None,
    corpus: Any = None,
    previous_page_state: Any = None,
) -> dict[str, Any] | None:
    """The write-time `entity_candidate` block this write may carry, or None.

    (`write-time-identity-candidates` spec; design D5.) Computed beside
    `hints()`, reusing its link parsing, `identity_key`, and registry
    resolution: a bare name is a candidate identity only when it is unresolved
    (no page, checked through the same `semantic_contract` resolution `hints`
    uses), unregistered (no active Entity title or alias), and NEW to this
    write -- not already linked by this exact page before this edit, checked
    against `previous_page_state` (the page's pre-write state; `None` for a
    creation, where every link is new by construction). That last check is
    load-bearing rather than a courtesy: the graph's dependency row for this
    page can already reflect this exact commit by the time this runs, so
    "was this page already among the returned sources" cannot distinguish a
    first-time link from a no-op re-edit.

    For each new name, the graph's link-dependency index
    (`EpistemicGraphIndex.dependency_sources_for_bare_name`) answers "which
    pages link this exact bare spelling" without a vault walk. This write's
    own page is evaluated for eligibility like any other returned row (never
    assumed present or absent), and separately from up to `MAX_DEPENDENCY_ROWS`
    of the OTHER returned sources, each run through the family's page-level
    exclusions -- ineligible evidence, navigation pages, the `Entities/`
    subtree, a page whose own title or stem is the name -- via the page state
    `corpus.pages` already holds. The block fires only on the exact transition
    one eligible page -> two: exactly one other eligible page, and this write's
    own page eligible too. Zero other eligible pages is still only one page
    total after this write; two or more is already at or past the gate the
    `entity_recurrence` audit family owns.

    A `structural_suggestions` owner-off disposition, an active mutation
    batch, or anything but an `available` graph status withholds the block
    silently -- the family remains the authority either way. Never raises: a
    fault here costs the caller a candidate, never the write.
    """
    if vault_root is None or page_state is None or corpus is None:
        return None
    try:
        from . import envelope

        if envelope.active().get("structural_suggestions") == "off":
            return None
        if _batch_active(vault_root):
            return None
        links = tuple(getattr(page_state, "body_wikilinks", ()) or ())
        if not links:
            return None
        from . import entity_recurrence, epistemic_graph, semantic_contract

        registry = _registry_index(corpus)
        self_path = str(getattr(page_state, "path", "") or "")
        previous_identities = _linked_identities(previous_page_state)
        candidates: list[tuple[str, entity_recurrence.Wikilink]] = []
        seen: set[str] = set()
        for raw_target, _line in links:
            link = entity_recurrence.parse_link(raw_target)
            if link is None or not 0 < len(link.name) <= MAX_MENTION_CHARS:
                continue
            identity = entity_recurrence.identity_key(link.name)
            if not identity or identity in seen:
                continue
            seen.add(identity)
            if identity in previous_identities:
                # Already linked before this write: no new transition, however
                # the graph's own dependency row for this page reads right now.
                continue
            resolution = semantic_contract._resolve_reference_wikilink_from_context(
                corpus, raw_target
            )
            if resolution.status != "unresolved":
                continue
            if registry is not None and registry.resolves(identity):
                continue
            candidates.append((identity, link))
        if not candidates:
            return None

        root = Path(vault_root)
        graph_index = epistemic_graph.EpistemicGraphIndex(root)
        identities: list[dict[str, Any]] = []
        for identity, link in candidates:
            if len(identities) >= MAX_CANDIDATE_IDENTITIES:
                break
            result = graph_index.dependency_sources_for_bare_name(link.name)
            if result.status != "available":
                continue
            if link.suffix and _attachment_exists(root, link.target):
                continue
            # This write's own page may already be among the returned sources
            # -- the dependency index can be updated synchronously with the
            # canonical commit -- so it is evaluated for eligibility exactly
            # like any other row rather than assumed present or absent.
            others = sorted(source for source in result.sources if source != self_path)
            eligible_others = [
                source
                for source in others[:MAX_DEPENDENCY_ROWS]
                if _linking_page_eligible(source, corpus=corpus, root=root, identity=identity)
            ]
            if len(eligible_others) != 1:
                continue
            # This write's own page state is already in hand -- passed
            # directly rather than resolved through `corpus.pages`, which for
            # a creation may not yet carry the just-created page at all.
            if not _eligible_page(self_path, page_state, root=root, identity=identity):
                continue
            pages = tuple(sorted({self_path, *eligible_others}))[:MAX_CANDIDATE_LINKING_PAGES]
            identities.append(
                {
                    "name": link.name,
                    "pages": list(pages),
                    "near_matches": (
                        [dict(match) for match in registry.near_matches(identity)]
                        if registry is not None
                        else []
                    ),
                    "routes": ["resolve-entity", "create-entity"],
                }
            )
        if not identities:
            return None
        return {"identities": identities, "guidance": ENTITY_CANDIDATE_GUIDANCE}
    except Exception:  # noqa: BLE001 -- a candidate never breaks a commit or a block
        log.debug("entity-candidate advisory failed (non-fatal)", exc_info=True)
        return None


def _linked_identities(page_state: Any) -> frozenset[str]:
    """The `identity_key` of every bare wikilink one page state's body carries.

    Used to tell "this write's link is new" from "this page already linked
    it": comparing identities rather than raw target text, so a display
    alias, heading anchor or `.md` suffix change on an unchanged link is still
    the SAME identity and never looks new.
    """
    if page_state is None:
        return frozenset()
    from . import entity_recurrence

    links = getattr(page_state, "body_wikilinks", ()) or ()
    found: set[str] = set()
    for raw_target, _line in links:
        link = entity_recurrence.parse_link(raw_target)
        if link is None:
            continue
        identity = entity_recurrence.identity_key(link.name)
        if identity:
            found.add(identity)
    return frozenset(found)


def _eligible_page(source: str, page: Any, *, root: Path, identity: str) -> bool:
    """Whether one page, already resolved to its state, is eligible evidence.

    Mirrors the `entity_recurrence` wikilink lane's own exclusions: ineligible
    evidence (retired status, excluded access tier), a navigation page, the
    `Entities/` subtree, and a page whose own title or filename stem is the
    identity. `page` is `None` when no state could be found for `source`, in
    which case it is never eligible -- a page this advisory cannot see is a
    page it must not name.
    """
    if page is None:
        return False
    from . import access
    from .entity_recurrence import counts_as_evidence, entities_prefix, identity_key
    from .find_corpus import NAVIGATION_BASENAMES

    if Path(source).name in NAVIGATION_BASENAMES:
        return False
    if source.startswith(entities_prefix()):
        return False
    if identity_key(str(getattr(page, "title", "") or "")) == identity:
        return False
    if identity_key(Path(source).stem) == identity:
        return False
    return counts_as_evidence(page, indexable=access.is_indexable(root, source))


def _linking_page_eligible(
    source: str, *, corpus: Any, root: Path, identity: str
) -> bool:
    """`_eligible_page`, resolving `source`'s state from `corpus.pages`.

    For every OTHER page the dependency index named: the raw dependency row
    carries no page state of its own, so this write's already-built corpus is
    where that state comes from.
    """
    pages = getattr(corpus, "pages", None) or {}
    page = pages.get(source) if hasattr(pages, "get") else None
    return _eligible_page(source, page, root=root, identity=identity)


def _attachment_exists(vault_root: Path, target: str) -> bool:
    """Whether an ordinary file, not a page, stands at a suffixed target.

    The same two spellings `audit._check_wikilinks`' probe checks -- vault-
    rooted and KB-relative -- so this agrees with the audit family on what an
    attachment is.
    """
    from .kbdir import kb_dirname, kb_prefix

    try:
        normalized = target.removeprefix(kb_prefix()).lstrip("/")
        return _ordinary_file_exists(vault_root, vault_root / target.lstrip("/")) or (
            _ordinary_file_exists(vault_root, vault_root / kb_dirname() / normalized)
        )
    except Exception:  # noqa: BLE001 -- a probe failure is never an attachment
        return False


def _ordinary_file_exists(vault_root: Path, candidate: Path) -> bool:
    from . import reserved_paths

    try:
        rel = candidate.relative_to(vault_root).as_posix()
        reserved_paths.inspect_generic_file(vault_root, rel)
        return True
    except (OSError, ValueError, reserved_paths.ReservedPathLeafError):
        return False


def block(
    vault_root: Path | None = None,
    *,
    page_state: Any = None,
    corpus: Any = None,
    ref: str | None = None,
) -> dict[str, Any] | None:
    """The advisory this write may carry, or None. Records the write either way.

    Inside a `due_state.batch_scope` this produces nothing AND records nothing:
    a multi-write product command is one episode boundary, and its own carrier
    decides once after the scope exits (`commands._carrying_capture_sweep`).
    Recording here would let the first of twelve writes consume the interval the
    batch's terminal is about to ask about.
    """
    try:
        if _batch_active(vault_root):
            return None
        key = ledger_key(vault_root)
        if key is None:
            return None
        now = float(_clock())
        # Decided and recorded together: two acquisitions would let two
        # concurrent writes on one key both see a quiet interval and both emit.
        found = check_and_record(key, now, ref=ref)
        if found is None or not _proactive_capture_permitted():
            return None
        payload: dict[str, Any] = {
            "boundary": found,
            "rule": RULE,
            "consider": list(CONSIDER),
        }
        written = recent_writes(key)
        if written:
            payload["written_recently"] = written
        mentions = hints(page_state, corpus).get("unpaged_mentions") or []
        if mentions:
            payload["unpaged_mentions"] = mentions
        return payload
    except Exception:  # noqa: BLE001 -- an advisory never breaks a commit
        log.debug("capture-sweep advisory failed (non-fatal)", exc_info=True)
        return None


def reset_state() -> None:
    """Forget every caller. For tests and for a deliberate session reset."""
    with _LOCK:
        _LAST_WRITE.clear()
        _RECENT.clear()


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------


def _valid_ref(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith(_REF_SCHEME)
        and len(value) <= _MAX_REF_CHARS
    )


def _batch_active(vault_root: Path | None) -> bool:
    try:
        from . import due_state

        return due_state.batch_active(vault_root)
    except Exception:  # noqa: BLE001
        log.debug("capture-sweep batch scope unreadable", exc_info=True)
        return False


def _proactive_capture_permitted() -> bool:
    """Whether the resolved delegation envelope permits proactive capture.

    Read through `envelope.active()`, which resolves the level through
    `prominence.resolve()` and then applies an operator's stored override -- so a
    per-client prominence default or an explicit `proactive_capture: off` both
    apply here without this module owning a second copy of the table.

    Fails CLOSED: an advisory nobody can govern is not delivered.
    """
    try:
        from . import envelope

        return envelope.active().get("proactive_capture") != "off"
    except Exception:  # noqa: BLE001 -- an ungovernable advisory is not delivered
        log.debug("capture-sweep envelope unavailable; staying silent", exc_info=True)
        return False


def _registry_index(corpus: Any) -> Any:
    """The entity registry's resolution surface, from pages already in memory.

    Reused rather than reimplemented: `entity_recurrence.registry_index` owns the
    predicate for what an active registry entity is, and this only adapts the
    corpus's page states to the two attributes it reads. `load_entity_types`
    opens one digest-cached file and nothing else.
    """
    try:
        from . import entity_recurrence, entity_types

        pages = getattr(corpus, "pages", None) or {}
        registry = entity_types.load_entity_types(getattr(corpus, "vault_root", None))
        return entity_recurrence.registry_index(
            (_RegistryPage(state.path, state.frontmatter) for state in pages.values()),
            entity_types=registry,
        )
    except Exception:  # noqa: BLE001 -- a missing registry only widens the hint
        log.debug("capture-sweep registry index unavailable", exc_info=True)
        return None
