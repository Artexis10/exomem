"""Recent promotions: the notice that an agent grew the vault's structure.

A promotion is a registry addition, an entity creation, or a typed edge between
two entity pages. Each is recorded as a source fact in the `promotions` section
of the review-state store, one entry per originating write. The due-state
carrier serves the open entries as the `recent_promotions` category; a
conversation other than the creator's sees an entry once, and each entry
reverts in one call (`promotion_revert.py`).

The ledger holds facts, not decisions. A settlement (first delivery to another
conversation, a dismissal, or the promotion no longer holding) is an ordinary
automatic or manual review decision on the entry's stable identity, so snooze,
dismiss, family dispositions and stickiness are the existing review rules.

Everything here is best effort on the write side: a failed record never fails
the write that caused it.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import markdown_relations
from . import review_state as review_state_module

log = logging.getLogger(__name__)

FAMILY = "recent_promotions"
REF_PREFIX = f"{review_state_module.REVIEW_PREFIX}{FAMILY}/"
#: Rows older than this leave the ledger on the next write. PROVISIONAL: tune
#: from how long a notice stays unseen, never from preference.
RETENTION_DAYS = 30
#: One entry names at most this many subjects; the rest are counted, not listed.
MAX_SUBJECTS = 500


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #


def promotion_ref(promotion_id: str) -> str:
    review_state_module.review_ref(promotion_id)
    return f"{REF_PREFIX}{promotion_id}"


def is_promotion_ref(value: str) -> bool:
    return str(value or "").strip().lower().startswith(REF_PREFIX)


def parse_promotion_ref(value: str) -> str:
    raw = str(value or "").strip()
    if not raw.lower().startswith(REF_PREFIX):
        raise ValueError(f"INVALID_REVIEW_REFERENCE: expected {REF_PREFIX}<id>")
    return review_state_module.parse_review_ref(
        f"{review_state_module.REVIEW_PREFIX}{raw[len(REF_PREFIX):]}"
    )


def identity_fingerprint(promotion_id: str) -> str:
    """The entry's review fingerprint: a function of its identity and nothing else.

    It never folds in a content hash, a dependant or a subject, so a busy page
    cannot re-raise a dismissed entry. The read-time `revert_fingerprint` is the
    separate value that moves with the page.
    """
    return review_state_module.component_fingerprint(
        target_ref=promotion_ref(promotion_id),
        reason={"category": FAMILY, "meta": {"signal_version": "promotion"}},
        related_refs=[],
    )


def interactive_session() -> str:
    """The conversation session of the current MCP call, or "" outside one.

    A hook process, a CLI call and a REST call run outside an MCP call and so
    have no session. A stdio MCP client has one and counts as interactive.
    """
    try:
        from .command_surface import mcp_caller_identity

        value = mcp_caller_identity().get("session_id")
    except Exception:  # noqa: BLE001 - no identity means no session
        return ""
    return f"session:{value}" if value else ""


# --------------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------------- #


def origin_clause(origin: Mapping[str, Any]) -> str:
    """The sentence a log entry carries for the write that led to a creation.

    An origin the caller could not read is "unknown" in one fixed form, so a
    withheld page and a missing one leave the same bytes.
    """
    path = origin.get("path")
    if not path:
        return "Origin: unknown."
    operation = origin.get("operation_id")
    return f"Origin: {path}." + (f" Operation: {operation}." if operation else "")


def write_token() -> str:
    """The identity of the write in progress: its request id, or a fresh one outside any request.

    One entry per (write, kind): the request id folds a bulk writer's subjects
    into one entry, and a write without one (a CLI call) is its own write.
    """
    from .writer_lease import active_mutation_request_id

    return active_mutation_request_id() or uuid.uuid4().hex


def record(
    vault_root: Path,
    kind: str,
    subjects: list[dict[str, Any]],
    *,
    new_family: bool = False,
    write: str | None = None,
    origin: Mapping[str, Any] | None = None,
) -> str | None:
    """Add `subjects` to this write's entry of `kind`; the entry's id, or None.

    `write` names the originating write (default: the write in progress) and
    `origin` the page whose content led to it, when that page is readable. The
    entry keeps both so a revert can tell the promotion's own edges and its
    originating link from everything else. Never raises.
    """
    if not subjects:
        return None
    try:
        write = write or write_token()
        promotion_id = review_state_module.item_id(f"{FAMILY}:{kind}:{write}")
        creator = interactive_session()
        now = dt.datetime.now(dt.UTC)
        cutoff = (now - dt.timedelta(days=RETENTION_DAYS)).date().isoformat()

        def edit(ledger: dict[str, Any]) -> None:
            entry = ledger.get(promotion_id)
            if not isinstance(entry, dict):
                entry = {
                    "kind": kind,
                    "creator": creator,
                    "created": now.date().isoformat(),
                    "created_at": now.isoformat(),
                    "write": write,
                    "new_family": False,
                    "omitted": 0,
                    "subjects": [],
                }
                if origin and origin.get("path"):
                    entry["origin"] = {"path": str(origin["path"])}
                ledger[promotion_id] = entry
            known = {json.dumps(item, sort_keys=True) for item in entry["subjects"]}
            for subject in subjects:
                encoded = json.dumps(subject, sort_keys=True)
                if encoded in known:
                    continue
                if len(entry["subjects"]) >= MAX_SUBJECTS:
                    entry["omitted"] = int(entry.get("omitted") or 0) + 1
                    continue
                entry["subjects"].append(subject)
                known.add(encoded)
            entry["new_family"] = bool(entry.get("new_family")) or new_family
            for stale in [
                key for key, row in ledger.items()
                if isinstance(row, dict) and str(row.get("created") or "") < cutoff
            ]:
                ledger.pop(stale, None)

        review_state_module.ReviewStateStore(Path(vault_root)).edit_promotions(edit)
        return promotion_id
    except Exception:  # noqa: BLE001 - a ledger fault never fails the write that caused it
        log.debug("promotion not recorded", exc_info=True)
        return None


class _Resolver:
    """Resolve wikilink targets to vault paths, building the vault index once."""

    def __init__(self, vault_root: Path) -> None:
        self._root = Path(vault_root)
        self._resolver: Any = None

    def _factory(self) -> Any:
        from . import vault as vault_module

        if self._resolver is None:
            self._resolver = vault_module.WikilinkResolver(self._root)
        return self._resolver

    def __call__(self, raw: str) -> str | None:
        from . import vault as vault_module

        try:
            canonical, warning = vault_module.normalize_wikilink(
                raw, self._root, resolver_factory=self._factory, strict=False
            )
        except Exception:  # noqa: BLE001 - an unresolvable link is no edge
            return None
        if warning or not canonical:
            return None
        path = canonical.split("#", 1)[0]
        return path if path.endswith(".md") else f"{path}.md"


def authored_edges(
    vault_root: Path, text: str, resolve: Callable[[str], str | None] | None = None
) -> list[tuple[int, str, str]]:
    """`(line index, predicate, target path)` per `## Relations` bullet that resolves.

    The scan is the relation grammar's own; a bullet whose target does not
    resolve to a page is not an edge.
    """
    resolve = resolve or _Resolver(vault_root)
    edges: list[tuple[int, str, str]] = []
    for candidate in markdown_relations.scan_markdown_relations(text).candidates:
        if not (candidate.canonical and candidate.grammar_valid):
            continue
        target = resolve(candidate.target)
        if target is not None:
            edges.append((candidate.line - 1, candidate.kind, target))
    return edges


def _is_entity(frontmatter: Mapping[str, Any]) -> bool:
    # The entity page type is the structural role the entity writer fixes, not a
    # vocabulary the vault defines (see `note_types` role "entity").
    return str(frontmatter.get("type") or "") == "entity"


def record_page_write(
    vault_root: Path,
    path: str,
    *,
    before_source: str | None,
    after_source: str,
    origin: Mapping[str, Any] | None = None,
    corpus: Any = None,
    source_state: Any = None,
    transition_id: str | None = None,
) -> None:
    """Record an entity creation and the typed edges an entity page gained.

    `before_source` is None for a creation. An edge counts only when its source
    page and its target page are both entities. Never raises.
    """
    try:
        from . import memory_refs
        from .vault import parse_frontmatter

        if not _is_entity(parse_frontmatter(after_source)[0]):
            return
        write = write_token()
        if before_source is None:
            record(
                vault_root, "entity",
                [{"path": path, "ref": memory_refs.ref_from_markdown(after_source)}],
                write=write, origin=origin,
            )
        if corpus is None:
            return  # An unbound post-commit reread cannot establish an edge's incarnation.
        from . import semantic_contract
        from .vault import content_hash

        source_ref = memory_refs.ref_from_markdown(after_source)
        if source_ref is None:
            return

        def resolve(raw: str) -> str | None:
            return semantic_contract._resolve_reference_wikilink_from_context(corpus, raw).path

        registry = (
            semantic_contract._authored_relations(source_state, corpus.registry)
            if source_state is not None else corpus.registry_for(path)
        )

        def edges(source: str) -> set[tuple[str, str]]:
            return {
                (canonical, target)
                for _line, kind, target in authored_edges(vault_root, source, resolve)
                if (canonical := registry.resolve(kind).canonical) is not None
            }

        before = edges(before_source) if before_source is not None else set()
        gained = sorted(edges(after_source) - before)
        introduction = {"source_ref": source_ref, "after_hash": content_hash(after_source)}
        if before_source is None:
            introduction["creation_ref"] = source_ref
        elif transition_id is not None:
            introduction["transition_id"] = transition_id
        else:
            return
        subjects = []
        for canonical, target in gained:
            state = corpus.pages.get(target)
            if state is None or not _is_entity(state.frontmatter):
                continue
            target_id = memory_refs.normalize_id(state.frontmatter.get(memory_refs.ID_FIELD))
            if target_id is None:
                continue
            subjects.append({
                "subject": path, "predicate": canonical, "target": target,
                "source_ref": source_ref, "target_ref": memory_refs.memory_ref(target_id),
                "introduction": introduction,
            })
        record(vault_root, "edge", subjects, write=write, origin=origin)
    except Exception:  # noqa: BLE001 - recording is best effort
        log.debug("promotion not recorded for %s", path, exc_info=True)


def record_registry(
    vault_root: Path, spec: Any, added: tuple[str, ...], rendered: str, *, version: str,
) -> None:
    """Record the keys one registry save added; `rendered` is the overlay it wrote. Never raises."""
    try:
        if not added:
            return
        from .vocabulary.registry import PUBLIC_INSTANCE, _build

        entries = _build(spec, rendered).entries

        scope = {} if spec.instance_id == PUBLIC_INSTANCE else {"scope": spec.instance_id}
        from .vault import content_hash

        subjects = [{
            "registry": spec.name, "key": key, **scope,
            "introduced_version": version, "after_hash": content_hash(rendered),
        } for key in added]
        # A root of its own family: the registry declares parents and this key has none.
        new_family = "parent" in spec.fields and any(
            getattr(entries.get(key), "parent", True) is None for key in added
        )
        record(vault_root, "registry", subjects, new_family=new_family)
    except Exception:  # noqa: BLE001 - recording is best effort
        log.debug("registry promotion not recorded", exc_info=True)


# --------------------------------------------------------------------------- #
# Does a subject still hold?
# --------------------------------------------------------------------------- #


def _safe_page(vault_root: Path, path: str) -> Path | None:
    candidate = Path(path)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    return Path(vault_root) / candidate


def edge_present(
    vault_root: Path, subject: str, predicate: str, target: str,
    resolve: Callable[[str], str | None] | None = None,
) -> bool:
    from . import relation_registry
    from .vault import PathGuardError, parse_frontmatter, read_guarded_text

    page = _safe_page(vault_root, subject)
    if page is None:
        return False
    try:
        text, _guard = read_guarded_text(vault_root, page)
    except (OSError, UnicodeDecodeError, PathGuardError):
        return False
    registry = relation_registry.load_for_page(vault_root, subject, parse_frontmatter(text)[0])
    # An unregistered label keeps its literal identity for ordinary authorized removal.
    canonical = registry.resolve(predicate).canonical or predicate
    return any(
        (registry.resolve(kind).canonical or kind) == canonical and resolved == target
        for _line, kind, resolved in authored_edges(vault_root, text, resolve)
    )


def page_visible(vault_root: Path, path: str) -> bool:
    """Whether the writer may see this page. A release plane that cannot decide says no.

    The owner of an ungoverned vault keeps the unfiltered answer, as `delete_file` does.
    """
    from .governance import egress as egress_module

    try:
        keep = egress_module.restricted_release_filter(Path(vault_root))
    except Exception:  # noqa: BLE001 - what cannot be decided is withheld
        return False
    return keep is None or bool(keep(path))


def subject_holds(
    vault_root: Path, kind: str, subject: Mapping[str, Any],
    resolve: Callable[[str], str | None] | None = None,
) -> bool:
    from . import promotion_proof

    proof = (
        promotion_proof.registry(Path(vault_root), subject)
        if kind == "registry" else promotion_proof.page(Path(vault_root), kind, subject)
    )
    # Unknown continuity leaves a readable notice; only proved absence settles it.
    return proof.status != "ended"


def subject_pages(kind: str, subject: Mapping[str, Any]) -> list[str]:
    """The pages a subject touches, for the audience filter. A registry key has none."""
    if kind == "entity":
        return [str(subject.get("path") or "")]
    if kind == "edge":
        return [str(subject.get("subject") or ""), str(subject.get("target") or "")]
    return []


def subject_visible(
    vault_root: Path, kind: str, subject: Mapping[str, Any],
    keep: Callable[[str], bool] | None,
) -> bool:
    """Admit the original subject before disclosing its stored identity binding."""
    from .governance import egress

    if kind == "registry":
        from . import registry_history
        from .vocabulary import contract, instances, registry_spec

        try:
            spec = instances.select(
                vault_root, registry_spec(str(subject["registry"])), subject.get("scope"),
            )
            if contract.admission_refusal(vault_root, spec) is not None:
                return False
            if egress.caller_restricted(vault_root):
                # Recycled storage cannot disclose a prior instance's private key.
                version = str(subject.get("introduced_version") or "")
                if not registry_history.VERSION_RE.fullmatch(version):
                    return False
                directory = registry_history.history_dir(vault_root, spec.stem).relative_to(vault_root).as_posix()
                return registry_history.read_kept(
                    vault_root, directory, f"{version}.yaml",
                    _admit_raw=registry_history._require_snapshot_admission,
                ) is not None
            return True
        except Exception:  # noqa: BLE001 - an unavailable instance is withheld
            return False
    if not all(keep is None or keep(page) for page in subject_pages(kind, subject)):
        return False
    if egress.caller_restricted(vault_root):
        from . import promotion_proof

        try:
            # A public replacement never releases the old page's private reference.
            if kind == "entity" and subject.get("ref") is not None:
                return promotion_proof._page(vault_root, subject["path"], subject["ref"], []) is not None
            if kind == "edge" and subject.get("source_ref") is not None:
                return all(promotion_proof._page(vault_root, path, ref, []) is not None for path, ref in (
                    (subject["subject"], subject["source_ref"]),
                    (subject["target"], subject.get("target_ref")),
                ))
        except (OSError, UnicodeError, ValueError, TypeError):
            return False
    return True


# --------------------------------------------------------------------------- #
# Serving
# --------------------------------------------------------------------------- #

_VIEW: dict[str, tuple[tuple[int, int, int], dict[str, Any]]] = {}


def _view(vault_root: Path) -> dict[str, Any] | None:
    """The review state, reread only when its file changed. None when unreadable."""
    path = review_state_module.state_path(Path(vault_root))
    try:
        info = path.stat()
        token = (info.st_ino, info.st_mtime_ns, info.st_size)
    except OSError:
        return review_state_module.empty_state()
    key = str(path)
    hit = _VIEW.get(key)
    if hit is not None and hit[0] == token:
        return hit[1]
    try:
        payload = review_state_module.ReviewStateStore(Path(vault_root)).load()
    except ValueError:
        return None
    if len(_VIEW) >= 8:
        _VIEW.pop(next(iter(_VIEW)), None)
    _VIEW[key] = (token, payload)
    return payload


def _entries(payload: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    ledger = payload.get("promotions")
    if not isinstance(ledger, dict):
        return []
    return [
        (key, row) for key, row in ledger.items()
        if isinstance(row, dict) and isinstance(row.get("subjects"), list)
    ]


def ledger(vault_root: Path) -> dict[str, dict[str, Any]]:
    """Every entry of the ledger by id; empty when the review state cannot be read."""
    payload = _view(vault_root)
    return dict(_entries(payload)) if payload else {}


def find(vault_root: Path, ref: str) -> tuple[str, dict[str, Any]]:
    """The entry for `ref` only when this caller may see all its subjects.

    An entry that never existed and one every page of which is withheld raise
    the same `REVIEW_ITEM_NOT_FOUND`, so the refusal says nothing about a page.
    """
    promotion_id = parse_promotion_ref(ref)
    payload = _view(vault_root)
    entry = dict(payload.get("promotions", {}).get(promotion_id) or {}) if payload else {}
    kind = str(entry.get("kind") or "")
    subjects = entry.get("subjects") or []
    # A notice is one write: a partial view must not let a client settle unseen subjects.
    if not subjects or not all(
        subject_visible(vault_root, kind, subject, lambda page: page_visible(vault_root, page))
        for subject in subjects
    ):
        raise ValueError(f"REVIEW_ITEM_NOT_FOUND: no recent promotion for {ref}")
    return promotion_id, entry


def settle(vault_root: Path, promotion_id: str, why: str) -> None:
    """Settle an entry as an automatic dismissal on its stable identity. Never raises."""
    try:
        review_state_module.ReviewStateStore(Path(vault_root)).apply(
            promotion_id, identity_fingerprint(promotion_id), action="dismiss", why=why,
            origin=review_state_module.AUTOMATIC, family=FAMILY,
        )
    except Exception:  # noqa: BLE001 - a settlement that fails leaves the entry open
        log.debug("promotion not settled", exc_info=True)


def served_rows(
    vault_root: Path,
    *,
    principal: Any = None,
    purpose: str | None = None,
    today: dt.date | None = None,
) -> list[dict[str, Any]]:
    """The open entries this audience may be told about, new families first.

    The order of operations is the egress contract: drop what this conversation
    created or already settled, drop a promotion this audience may not see, and
    only then count. A promotion that no longer holds settles for everyone.
    """
    payload = _view(vault_root)
    if payload is None or FAMILY in review_state_module.disposition_map(payload):
        return []
    entries = _entries(payload)
    if not entries:
        return []
    store = review_state_module.ReviewStateStore(Path(vault_root))
    today = today or dt.datetime.now(dt.UTC).date()
    me = interactive_session()
    open_entries: list[tuple[str, dict[str, Any]]] = []
    for promotion_id, entry in entries:
        if me and entry.get("creator") == me:
            continue
        state, _decision = store.effective_state(
            promotion_id, identity_fingerprint(promotion_id), today=today, payload=payload
        )
        if state == "open":
            open_entries.append((promotion_id, entry))
    if not open_entries:
        return []
    from .governance import egress as egress_module
    from .governance.principal import effective_principal, request_scope

    try:
        keep = egress_module.release_walk_filter(
            Path(vault_root), principal=principal, purpose=purpose
        )
    except Exception:  # noqa: BLE001 - a release plane that cannot decide serves nothing
        log.debug("release filter unavailable; serving no promotions", exc_info=True)
        return []
    resolve = _Resolver(vault_root)
    rows: list[dict[str, Any]] = []
    for promotion_id, entry in open_entries:
        kind = str(entry.get("kind") or "")
        with request_scope((principal or effective_principal()).with_purpose(purpose)):
            visible = [
                s for s in entry["subjects"] if subject_visible(vault_root, kind, s, keep)
            ]
        if len(visible) != len(entry["subjects"]) or not visible:
            continue
        holding = [s for s in visible if subject_holds(vault_root, kind, s, resolve)]
        if not holding:
            settle(vault_root, promotion_id, "settled by state: the promotion no longer holds")
            continue
        rows.append({
            "category": FAMILY,
            "ref": promotion_ref(promotion_id),
            "due_since": str(entry.get("created") or today.isoformat()),
            "fingerprint": identity_fingerprint(promotion_id),
            "path": "",
            **({"new_family": True} if entry.get("new_family") else {}),
        })
    rows.sort(key=lambda row: (not row.get("new_family"), row["due_since"], row["ref"]))
    return rows


def settle_delivered(vault_root: Path, delivered: list[Mapping[str, Any]]) -> None:
    """Settle the entries a block put in front of an interactive conversation.

    Only an MCP session settles, and never the one that created the entry.
    """
    me = interactive_session()
    if not me:
        return
    payload = _view(vault_root)
    if payload is None:
        return
    entries = dict(_entries(payload))
    for row in delivered:
        if row.get("category") != FAMILY:
            continue
        promotion_id = str(row.get("ref") or "").rsplit("/", 1)[-1]
        entry = entries.get(promotion_id)
        if entry is not None and entry.get("creator") != me:
            try:
                find(vault_root, promotion_ref(promotion_id))
            except ValueError:
                continue
            settle(vault_root, promotion_id, "settled by delivery to another conversation")


# --------------------------------------------------------------------------- #
# Triage
# --------------------------------------------------------------------------- #


def triage(
    vault_root: Path,
    *,
    ref: str,
    action: str,
    until: str | None = None,
    why: str | None = None,
    expected_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Dismiss, snooze or reopen one promotion notice. No revert lands here."""
    normalized = str(action or "").strip().lower()
    # nosemgrep: ep-word-membership -- the promotion triage protocol defines these action tokens.
    if normalized not in {"dismiss", "snooze", "reopen"}:
        raise ValueError(
            "INVALID_REVIEW_ACTION: a recent promotion accepts dismiss, snooze, or reopen"
        )
    promotion_id, _entry = find(vault_root, ref)
    if normalized != "reopen" and not expected_fingerprint:
        raise ValueError(
            "INVALID_REVIEW_ACTION: a recent promotion's dismiss or snooze requires the "
            "surfaced fingerprint"
        )
    if expected_fingerprint and expected_fingerprint != identity_fingerprint(promotion_id):
        raise ValueError(
            f"STALE_REVIEW_ITEM: {ref} no longer carries that fingerprint; read the item again"
        )
    result = review_state_module.ReviewStateStore(Path(vault_root)).apply(
        promotion_id, identity_fingerprint(promotion_id), action=normalized, until=until,
        why=why, family=None if normalized == "reopen" else FAMILY,
    )
    result["ref"] = promotion_ref(promotion_id)
    return result
