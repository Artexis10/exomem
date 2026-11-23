"""Correct a captured source's classification without breaking its provenance.

Classification is a judgement made at capture time, often before the answer is
knowable, and until now it was permanent: `edit.py` refuses every write into
`Sources/`, so a source captured under the wrong kind stayed wrong forever.

Two established positions make the correction path defensible rather than a hole
in the append-only rule. `move_file` already treats a Sources-to-Evidence move as
a *reclassification* of the same raw item, requiring a stated reason precisely
because the capture-time judgement can turn out wrong. And `note.py` already
mutates an append-only source's frontmatter, appending to `ingested_into:`
whenever a compiled note cites it. Rule 2 protects the body.

So this module changes exactly the classification fields plus the fields that
record the correction, patching each where the YAML parser places it, proves
the body byte-identical before anything is written, and reuses `move_file` for
the relocation and inbound-reference rewriting instead of reimplementing either.

Every correction appends where the source was and how it was classified to its
lineage, `moved_from`, oldest first, so `revert` can restore it; a legacy
`reclassified_from` value reads as older entries and keeps its bytes. The separate
`classification_history` keeps events and reasons even after a reversal.

It never decides a classification. `propose` reports what is deterministically
observable and declines when the evidence supports nothing, because presenting a
plausible guess for approval is how a fallback becomes permanent.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from types import MappingProxyType
from typing import Any, Literal

from . import (
    access,
    file_watcher,
    foreground_priority,
    freshness,
    originals,
    page_lineage,
    path_mentions,
    pointer_write,
    recall_policy,
    record_formats,
    request_budget,
    semantic_writes,
    source_pointers,
    source_taxonomy,
    vault,
    vocabulary_resolution,
)
from . import move_file as move_file_module
from .cli_ops import OpError
from .governance import authorization_session_authority, bridges, egress, lifecycle, policy, store
from .governance.authorization_session_lifecycle import AuthorizationSessionUnavailable
from .governance.principal import RequestPrincipal, effective_principal
from .kbdir import managed_form, managed_join, managed_split
from .vault import (
    InboundLink,
    PathGuard,
    PathGuardError,
    PlannedWrite,
    VaultPathError,
    admit_write_target,
    batch_atomic_write,
    find_inbound_wikilinks,
    parse_frontmatter,
    read_guarded_text,
    resolve_under_vault,
)

log = logging.getLogger(__name__)

#: Frontmatter keys this module is allowed to write. Anything else on a source
#: is provenance and must survive a correction untouched.
CLASSIFICATION_FIELDS = ("source_type", "domain")
RECORD_FIELDS = (
    "reclassified", "reclassified_from", "reclassified_reason", "classification_history",
)
#: The legacy history key. Its entries record `path`, `kind` and `domain`, oldest
#: first, and read as lineage older than every `moved_from` entry (`page_lineage`).
#: The field and its entry keys are fixed by the source page format, not by any vocabulary.
HISTORY_FIELD = page_lineage.LEGACY_FIELD
#: These fields and event actions are closed Source-format tokens, not vocabulary.
EVENTS_FIELD = "classification_history"
#: The optional history-entry and event key that records the pointer fields a repair
#: changed, each with the value it recorded before (None when the field was absent).
POINTER_KEY = "pointer"

#: The form a prose mention of the source's path is reported under, beside the link forms.
MENTION_FORM = "mention"
_MAX_REASON_CHARS = 400
#: The preview judges references and the destination, not the semantic
#: contract of the pages a move rewrites, so it says so beside its refusals.
_REFUSAL_SCOPE = (
    "covers inbound references and the destination; the semantic contract of "
    "rewritten pages is checked when the correction applies"
)
_FENCE_LINE = re.compile(r"---\r?\n")


@dataclass
class ReclassifyError(Exception):
    code: str
    reason: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.code}: {self.reason}"


@dataclass(frozen=True)
class ReclassifyProposal:
    """What a correction would do, and the evidence behind each proposed value."""

    path: str
    current_kind: str
    current_domain: str | None
    proposed_kind: str | None = None
    proposed_domain: str | None = None
    kind_evidence: tuple[str, ...] = ()
    domain_evidence: tuple[str, ...] = ()
    destination: str | None = None
    relocation_required: bool = False
    references: int = 0
    #: Pages the caller may see that the move would rewrite: mutable pages
    #: whose links or prose name the source by its path.
    rewritten: tuple[str, ...] = ()
    #: Append-only pages the caller may see whose links to the source the move repoints
    #: through the governed pointer write; the commit passes `preview_digest` to approve them.
    pointer_repointed: tuple[str, ...] = ()
    #: Append-only pages the caller may see that the move leaves byte-identical,
    #: grouped by the link form they use (`basename` or `path`), or `mention` when the page
    #: only cites the source's path in prose.
    append_only_unchanged: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: Why the move would be refused, each naming only a page the caller may see.
    refusals: tuple[Mapping[str, str], ...] = ()
    #: What the preview could not check, such as path mentions the catalogue could not answer.
    warnings: tuple[str, ...] = ()
    #: The digest a correction that repoints append-only pages passes; None when it would refuse.
    preview_digest: str | None = None

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "current_kind": self.current_kind,
            "current_domain": self.current_domain,
            "proposed_kind": self.proposed_kind,
            "proposed_domain": self.proposed_domain,
            "kind_evidence": list(self.kind_evidence),
            "domain_evidence": list(self.domain_evidence),
            "destination": self.destination,
            "relocation_required": self.relocation_required,
            "references": self.references,
            "referrers": {
                "rewritten": list(self.rewritten),
                "pointer_repointed": list(self.pointer_repointed),
                "append_only_unchanged": {
                    form: list(paths) for form, paths in self.append_only_unchanged.items()
                },
            },
            "refusals": [dict(refusal) for refusal in self.refusals],
            **({"refusal_scope": _REFUSAL_SCOPE} if self.destination is not None else {}),
            **({"warnings": list(self.warnings)} if self.warnings else {}),
            **({"preview_digest": self.preview_digest} if self.preview_digest else {}),
        }


@dataclass(frozen=True)
class ReclassifyResult:
    old_path: str
    new_path: str
    #: None when a revert restores a source that recorded no kind.
    kind: str | None
    domain: str | None
    reason: str
    relocated: bool = False
    references_updated: int = 0
    #: Prose mentions of the source's path that the relocation renamed in page bodies.
    path_mentions_updated: int = 0
    warnings: tuple[str, ...] = field(default_factory=tuple)
    # One line per source kind or domain this correction registered on first use.
    vocabulary_receipt: tuple[str, ...] = field(default_factory=tuple)
    #: The pointer fields this correction set or restored.
    repointed: tuple[str, ...] = field(default_factory=tuple)
    #: The append-only pages, title and path, whose links to the source its relocation
    #: repointed or restored.
    pointer_repointed: tuple[Mapping[str, str], ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        value: dict[str, object] = {
            "old_path": self.old_path,
            "path": self.new_path,
            "source_type": self.kind,
            "domain": self.domain,
            "reason": self.reason,
            "relocated": self.relocated,
            "references_updated": self.references_updated,
        }
        if self.path_mentions_updated:
            value["path_mentions_updated"] = self.path_mentions_updated
        if self.warnings:
            value["warnings"] = list(self.warnings)
        if self.vocabulary_receipt:
            value["vocabulary_receipt"] = list(self.vocabulary_receipt)
        if self.repointed:
            value["repointed"] = list(self.repointed)
        if self.pointer_repointed:
            value["pointer_repointed"] = [dict(page) for page in self.pointer_repointed]
        return value


def _sources_root() -> str:
    return managed_join(source_taxonomy.SOURCES_ROOT)


def _require_source(vault_root: Path, path: str) -> tuple[str, Path]:
    """Resolve `path` to a source page, refusing anything else."""
    rel = str(path).replace("\\", "/").strip().lstrip("/")
    if not rel:
        raise ReclassifyError("PATH_REQUIRED", "supply the source page to reclassify.")
    if managed_split(rel) is None:
        rel = managed_join(rel)
    if not rel.lower().endswith(".md"):
        rel = f"{rel}.md"
    try:
        admit_write_target(vault_root, rel)
    except VaultPathError as refusal:
        raise ReclassifyError(refusal.code, refusal.reason) from None
    prefix = f"{_sources_root()}/"
    if not rel.startswith(prefix):
        raise ReclassifyError(
            "NOT_A_SOURCE",
            f"{rel} is not under {_sources_root()}/. Only captured sources carry a "
            f"source kind and a domain; compiled notes are corrected with edit or "
            f"replace.",
        )
    try:
        absolute, rel = resolve_under_vault(vault_root, rel)
    except VaultPathError as error:
        raise ReclassifyError("INVALID_PATH", error.reason) from error
    except Exception as error:  # noqa: BLE001 - reported as a refusal, not a crash
        raise ReclassifyError("INVALID_PATH", str(error)) from error
    from .governance import egress
    from .vault_store import read as store_read

    # A source the caller may not see is answered exactly as a missing one.
    if not store_read.holds_file(vault_root, rel) or egress.write_target_withheld(vault_root, rel):
        raise ReclassifyError("NOT_FOUND", f"no source page at {rel}.")
    return rel, absolute


def _recorded(front: Mapping[str, Any], key: str) -> str | None:
    """A classification field as the page states it; None when it states none."""
    value = front.get(key)
    return value if isinstance(value, str) and value else None


def _current_classification(text: str) -> tuple[str, str | None]:
    front, _, _ = parse_frontmatter(text)
    return (
        _recorded(front, "source_type") or source_taxonomy.UNCLASSIFIED_KIND,
        _recorded(front, "domain"),
    )


def _body_of(text: str) -> str:
    _, body, _ = parse_frontmatter(text)
    return body


def _history(front: Mapping[str, Any]) -> list[Any]:
    """The source's lineage entries, oldest first: legacy `reclassified_from`, then `moved_from`.

    A legacy scalar recorded only the latest previous path, so it reads as one
    entry whose kind and domain are unknown.
    """
    legacy = front.get(HISTORY_FIELD)
    if legacy is not None and not isinstance(legacy, (str, list)):
        raise ReclassifyError(
            "INVALID_HISTORY",
            f"`{HISTORY_FIELD}` is neither a previous path nor a list of entries; "
            "correct the field before reclassifying this source.",
        )
    moved = front.get(page_lineage.LINEAGE_FIELD)
    if moved is not None and not isinstance(moved, list):
        raise ReclassifyError(
            "INVALID_HISTORY",
            f"`{page_lineage.LINEAGE_FIELD}` is not a list of entries; "
            "correct the field before reclassifying this source.",
        )
    return page_lineage.entries(front)


def _restorable(entry: Any) -> tuple[str, str | None, str | None]:
    """The path, kind and domain a history entry records.

    An entry that does not record all three cannot be restored: a revert never
    invents the classification that a legacy path-only entry left out.
    """
    if isinstance(entry, dict) and isinstance(entry.get("path"), str):
        if "kind" in entry and "domain" in entry:
            kind, domain = entry["kind"], entry["domain"]
            if all(value is None or isinstance(value, str) for value in (kind, domain)):
                return entry["path"], kind or None, domain or None
    raise ReclassifyError(
        "HISTORY_CLASSIFICATION_UNKNOWN",
        "the latest lineage entry records a previous path but not its kind and "
        "domain, as a plain move or a legacy `reclassified_from` path records it; "
        "reclassify the source explicitly instead.",
    )


def _classification_events(
    front: Mapping[str, Any],
    *,
    path: str,
    destination: str,
    kind: str | None,
    domain: str | None,
    reason: str,
    operation: Literal["reclassify", "revert"],
    pointer: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Append a retained event without interpreting the legacy undo stack.

    A legacy reason can describe a no-op or a reversal, so it cannot safely be
    attributed to an undo entry. Keep the available fields together, once.
    """
    if EVENTS_FIELD in front:
        record = front[EVENTS_FIELD]
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("events"), list)
            or any(not isinstance(event, dict) for event in record["events"])
        ):
            raise ReclassifyError(
                "INVALID_HISTORY", "classification_history must contain a list of event records."
            )
        record = dict(record)
    else:
        record = {"events": []}
        # These are the existing correction fields; retain their values, not a reconstruction.
        legacy = {
            key: front[key]
            for key in ("reclassified", HISTORY_FIELD, "reclassified_reason")
            if key in front
        }
        if legacy:
            record["legacy"] = legacy
    event = {
        "operation": operation,
        # The declared transform runs twice; allocate time before constructing it.
        "at": dt.datetime.now(dt.UTC).isoformat(),
        "reason": reason,
        "before": {
            "path": path,
            "kind": _recorded(front, "source_type"),
            "domain": _recorded(front, "domain"),
        },
        "after": {"path": destination, "kind": kind, "domain": domain},
    }
    if pointer:
        event[POINTER_KEY] = dict(pointer)
    return {**record, "coverage": "retained-only", "events": [*record["events"], event]}


def _rewrite_classification(
    text: str,
    *,
    kind: str | None,
    domain: str | None,
    classification_events: dict[str, Any],
    reason: str,
    today: dt.date,
    pointer: Mapping[str, str | None] | None = None,
    lineage: Callable[[str], str] | None = None,
) -> str:
    """Return `text` with only the classification and record fields changed.

    Each field is patched where the YAML parser places it, so every other byte
    of the page is kept, and the body is proven unchanged before anything is
    written. A kind or domain equal to the one the page states is left as it is. A
    None kind or domain removes the field, which is how a revert restores a source
    that recorded none. A legacy `reclassified_from` keeps its bytes. `pointer` sets
    each pointer field to its path, or removes it where the path is None. `lineage`
    first appends or removes the page's own lineage entry, for a correction in place.
    """
    changes: dict[str, Any] = {}
    deletes: list[str] = []
    if lineage is not None:
        try:
            text = lineage(text)
        except page_lineage.LineageError as error:
            raise ReclassifyError(error.code, error.reason) from error
    front, _, _ = parse_frontmatter(text)
    for key, value in (("source_type", kind), ("domain", domain)):
        if value == _recorded(front, key):
            # A field the correction leaves as stated keeps its bytes, a list domain included.
            continue
        if value is None:
            deletes.append(key)
        else:
            changes[key] = value
    for key, value in (pointer or {}).items():
        if value is None:
            deletes.append(key)
        else:
            changes[key] = value
    changes["reclassified"] = today.isoformat()
    changes["reclassified_reason"] = reason
    changes[EVENTS_FIELD] = classification_events
    try:
        updated = record_formats.splice_markdown_frontmatter(
            text, changes, delete_fields=tuple(deletes)
        )
    except record_formats.FrontmatterSpliceError as error:
        if error.code != "NO_FRONTMATTER":
            raise ReclassifyError(error.code, f"the source's {error.reason}.") from error
        if _FENCE_LINE.match(text):
            raise ReclassifyError(
                "NO_FRONTMATTER", "the source's frontmatter block is unterminated."
            ) from error
        raise ReclassifyError(
            "NO_FRONTMATTER", "the source has no frontmatter block to correct."
        ) from error
    if _body_of(updated) != _body_of(text):
        raise ReclassifyError(
            "BODY_CHANGED",
            "reclassification would alter the source body, which it must never do.",
        )
    return updated


def _clean_reason(reason: str | None) -> str:
    text = (reason or "").strip()
    if not text:
        raise ReclassifyError(
            "REASON_REQUIRED",
            "reclassifying a captured source restates a judgement and rewrites "
            "every reference to it; supply `reason` naming why the original "
            "classification was wrong.",
        )
    return " ".join(text.split())[:_MAX_REASON_CHARS]


def _plan_pointer(
    vault_root: Path, rel: str, front: Mapping[str, Any], artifact_path: str
) -> tuple[dict[str, str], dict[str, str | None]]:
    """The pointer fields a repair sets, and the value each recorded before.

    Every structured pointer the card has is set to the target, so none keeps naming a
    stale path; a card with none gets `evidence_file`, the general artifact pointer. A
    card that follows its bytes by name (a descriptor, or a card at its file's companion
    path) is refused: the repair there is to move the file back beside the card.
    """
    target = artifact_path.replace("\\", "/").strip().lstrip("/")
    if target:
        target = managed_form(target)
    found = source_pointers.pointers(front)
    if any(source_pointers.bound(rel, pointer.field, pointer.path) for pointer in found):
        raise ReclassifyError(
            "POINTER_BOUND",
            "the source follows its file by name (a descriptor, or a card beside its file); "
            "move the file back beside the source with move_file instead of repointing it.",
        )
    refusal = source_pointers.target_refusal(vault_root, front, target)
    if refusal is not None:
        raise ReclassifyError(*refusal)
    fields = [pointer.field for pointer in found] or [originals.SCALAR_POINTERS[0]]
    recorded = {key: _recorded(front, key) for key in fields}
    changes = {key: target for key in fields if recorded[key] != target}
    return changes, {key: recorded[key] for key in changes}


def _restorable_pointer(entry: Any) -> dict[str, str | None]:
    """The pointer fields a history entry restores; empty when the entry repaired none.

    Only the scalar pointer fields are restorable, so an edited history cannot make a
    revert write any other field.
    """
    recorded = entry.get(POINTER_KEY) if isinstance(entry, dict) else None
    if recorded is None:
        return {}
    if not isinstance(recorded, dict) or any(
        key not in originals.SCALAR_POINTERS or not (value is None or isinstance(value, str))
        for key, value in recorded.items()
    ):
        raise ReclassifyError(
            "HISTORY_POINTER_INVALID",
            f"the latest history entry's `{POINTER_KEY}` is not a map of pointer fields to "
            "their earlier paths; correct the entry before reverting.",
        )
    return dict(recorded)


def _destination(vault_root: Path, rel: str, kind, domain) -> str:
    segments = source_taxonomy.source_segments(kind, domain)
    return managed_join("/".join((*segments, rel.rsplit("/", 1)[-1])))


def _projection(
    vault_root: Path,
    rel: str,
    taxonomy: source_taxonomy.SourceTaxonomy,
    kind: str,
    domain: str | None,
    *,
    supplied: bool = True,
):  # noqa: ANN202 - (kind, domain, destination)
    """Resolve a correction through the capture's own domain seam.

    A domain projects through the strict snapshot and reuses the one existing
    equivalent folder, so restating a source's domain never moves it into a
    case-only sibling of the folder it already lives in.

    A malformed registry refuses only a domain the caller supplied. A kind-only
    correction carries the source's existing domain, so it falls back to the
    lenient registry, as it did before the strict seam.
    """
    if not domain:
        kind_resolution = taxonomy.resolve_kind(kind)
        return kind_resolution, None, _destination(vault_root, rel, kind_resolution, None)
    try:
        binding = vocabulary_resolution.resolve_source_domain(
            vault_root, kind=kind, domain=domain
        )
    except vocabulary_resolution.VocabularyResolutionError as error:
        if supplied or error.code != "INVALID_DOMAIN_TAXONOMY":
            raise
        kind_resolution = taxonomy.resolve_kind(kind)
        domain_resolution = taxonomy.resolve_domain(domain)
        return (
            kind_resolution,
            domain_resolution,
            _destination(vault_root, rel, kind_resolution, domain_resolution),
        )
    destination = managed_join("/".join((*binding.segments, rel.rsplit("/", 1)[-1])))
    return binding.kind, binding.domain, destination


def _refuse_episode_kind(current_kind: str, target_kind: str | None) -> None:
    """A recap is only what `episode_memory` recorded and bound, in one folder.

    Nothing is reclassified into the kind, and a recap is not reclassified out
    of it or into a domain subfolder: its revisions are found by one listing
    of `Sources/Episodes/`, so a moved recap is one that listing no longer
    sees, and the next record would leave two live revisions.
    """
    if source_taxonomy.EPISODE_KIND in (current_kind, target_kind):
        raise ReclassifyError(
            "EPISODE_KIND_RESERVED",
            f"the {source_taxonomy.EPISODE_KIND!r} kind is reserved for conversation "
            "recaps recorded with record_episode: a recap cannot be reclassified, "
            "and nothing else can become one",
        )


def _refuse_unchosen_kind(target_kind: str | None) -> None:
    """A correction names what the source IS; it never files one as unclassified."""
    if target_kind in source_taxonomy.UNCHOSEN_KINDS:
        raise ReclassifyError(
            "SOURCE_KIND_REQUIRED",
            f"{target_kind!r} records that no kind was chosen; reclassify to the kind "
            "the source actually is, an existing one or a new slug",
        )


def _introduction_warnings(plan: source_taxonomy.TaxonomyPlan) -> tuple[str, ...]:
    """Say so when a correction introduces vocabulary the vault had not seen.

    Same wording as capture (`add._vocabulary_warnings`), because a correction
    that quietly registers a mistyped kind is the same failure as a capture that
    does -- and the correction path is the one where a typo is *more* likely,
    since the caller is naming a value it just decided on.
    """
    return tuple(
        f"NEW_{introduction.axis.upper()}: registered {introduction.key!r} "
        f"(files under Sources/{introduction.path_label}/). Edit "
        f"_Schema/source-taxonomy.yaml to rename or relabel it."
        for introduction in plan.introductions
    )


def propose(
    vault_root: Path,
    path: str,
    *,
    source_kind: str | None = None,
    domain: str | None = None,
    candidate_limit: int | None = None,
    paged: bool = False,
) -> ReclassifyProposal:
    """Report what a correction would do, without writing anything.

    With no values supplied this reports what the vault itself can observe: the
    domain segment already in the source's location, whether it records an origin
    URL, and its existing metadata. That is usually enough to propose a domain and
    rarely enough to propose a kind — and when it supports no kind this reports
    none rather than offering the fallback, which is the failure the open
    vocabulary exists to remove.

    A caller that has already decided passes its own `source_kind`/`domain` and
    gets that correction previewed instead: the destination it would project to,
    the pages whose links it would rewrite, the append-only pages it leaves
    unchanged, and why it would be refused. Deciding what an artifact IS means
    reading it, so this is the normal path — the agent judges, and the preview is
    what it shows the user before anything is written. Supplied values are
    resolved through the same taxonomy rules the correction itself applies, so a
    value that would be refused is refused here rather than after the user has
    approved it. The report names only pages the caller may see.
    """
    vault_root = Path(vault_root)
    who = effective_principal()
    surface = who.surface
    owner = managed_preview_stop(vault_root, who)
    check = request_budget.current_checkpoint()
    served = surface == "rest" or (surface == "mcp" and request_budget.current() is not None)
    if served and (owner is None) != (check is None):
        # The transport selected whether to manage this preview before the leaf.
        raise preview_warming()
    if owner is None:
        if paged:
            raise preview_warming()
        return _propose(vault_root, path, source_kind=source_kind, domain=domain)

    try:
        admission = preview_release_policy(vault_root, who)
        visible = _preview_referrer_view(vault_root, who, admission.erasure.paths,
                                         bounded=candidate_limit is not None)
        with nullcontext() if paged else foreground_priority.cancellable(owner):
            foreground_priority.check_cancelled()
            result = _propose(vault_root, path, source_kind=source_kind, domain=domain, owner=owner,
                              candidate_limit=candidate_limit, paged=paged, visible=visible)
            foreground_priority.check_cancelled()
            if (managed_preview_stop(vault_root, who) is not owner
                    or preview_release_policy(vault_root, who) != admission):
                raise preview_warming()
            return result
    except foreground_priority.BulkCancelled:
        raise preview_warming() from None


def _propose(
    vault_root: Path, path: str, *, source_kind: str | None, domain: str | None,
    owner: Event | None = None,
    candidate_limit: int | None = None, paged: bool = False,
    visible: Callable[[str], bool] | None = None,
) -> ReclassifyProposal:
    rel, absolute = _require_source(vault_root, path)
    if visible is not None and not visible(rel):
        raise ReclassifyError("SOURCE_NOT_FOUND", "source does not exist")
    held_sink: _HeldSink | None = _HELD_EVIDENCE.get() if paged else None
    read_text = _read_inline_text if candidate_limit is not None else read_guarded_text
    text, source_guard = (
        _witnessed_read(read_text, held_sink, vault_root, absolute)
        if held_sink is not None
        else read_text(vault_root, absolute)
    )
    front, _, _ = parse_frontmatter(text)
    current_kind, current_domain = _current_classification(text)

    taxonomy = source_taxonomy.load_taxonomy(vault_root)
    proposed_domain: str | None = None
    domain_evidence: list[str] = []
    if domain is not None:
        proposed_domain = taxonomy.resolve_domain(domain).key
        domain_evidence.append("supplied by the caller")
    elif current_domain is None:
        # `Sources/<Kind>/<Domain>/<file>.md` — the segment under the kind is a
        # domain the vault already asserted by filing the page there.
        parts = rel.split("/")
        if len(parts) == 5:
            try:
                resolved = taxonomy.resolve_domain(parts[3])
            except source_taxonomy.TaxonomyError:
                resolved = None
            if resolved is not None:
                proposed_domain = resolved.key
                domain_evidence.append(
                    f"filed under the {parts[3]!r} folder, which resolves to the "
                    f"{resolved.key!r} domain"
                )

    proposed_kind: str | None = None
    kind_evidence: list[str] = []
    if source_kind is not None:
        proposed_kind = taxonomy.resolve_kind(source_kind).key
        kind_evidence.append("supplied by the caller")
    elif current_kind in source_taxonomy.UNCHOSEN_KINDS:
        # Deliberately no title or content heuristics. A kind guessed from a
        # filename reads as authoritative once approved, and a wrong kind is
        # exactly the debt this operation exists to clear.
        url = front.get("url")
        if isinstance(url, str) and url.strip():
            kind_evidence.append(
                f"records an origin URL ({url.strip()[:80]}), so the material came "
                f"from a retrievable web artifact"
            )
        else:
            kind_evidence.append("records no origin URL")
        kind_evidence.append(
            "no kind is proposed: the observable metadata does not establish what "
            "this artifact is, which is a judgement the caller has to make"
        )

    if source_kind is not None or domain is not None:
        _refuse_episode_kind(current_kind, proposed_kind)
    if source_kind:
        _refuse_unchosen_kind(proposed_kind)
    effective_domain = proposed_domain or current_domain
    destination: str | None = None
    classification = None
    relocation_required = False
    if proposed_kind is not None or proposed_domain is not None:
        try:
            kind, resolved_domain, destination = _projection(
                vault_root,
                rel,
                taxonomy,
                proposed_kind or current_kind,
                effective_domain,
                supplied=domain is not None,
            )
            classification = (kind.key, resolved_domain.key if resolved_domain else None)
            relocation_required = destination != rel
        except (source_taxonomy.TaxonomyError, vocabulary_resolution.VocabularyResolutionError):
            destination = None

    generation = _preview_generation(vault_root) if owner is not None else None
    read_hashes = {rel: source_guard.expected_content_hash} if owner is not None else None
    inbound = (
        _visible_inbound(vault_root, rel) if owner is None
        else _managed_preview_inbound(vault_root, rel, owner, read_hashes=read_hashes,
                                     candidate_limit=candidate_limit, visible=visible)
    )
    outcome = (
        _referrer_outcome(vault_root, rel, text, destination, inbound, read_hashes=read_hashes,
                          candidate_limit=candidate_limit,
                          paged_classification=classification if paged else None,
                          visible=visible, held_sink=held_sink)
        if destination is not None
        else _ReferrerOutcome()
    )
    if owner is not None and read_hashes is not None:
        foreground_priority.check_cancelled()
        try:
            source_guard.recheck(vault_root)
        except PathGuardError:
            raise preview_warming() from None
        current_inbound = _managed_preview_inbound(vault_root, rel, owner, read_hashes=read_hashes,
                                                   candidate_limit=candidate_limit, visible=visible)
        if current_inbound != inbound or _preview_generation(vault_root) != generation:
            raise preview_warming()
    return ReclassifyProposal(
        path=rel,
        current_kind=current_kind,
        current_domain=current_domain,
        proposed_kind=proposed_kind,
        proposed_domain=proposed_domain,
        kind_evidence=tuple(kind_evidence),
        domain_evidence=tuple(domain_evidence),
        destination=destination,
        relocation_required=relocation_required,
        references=len(inbound),
        rewritten=outcome.rewritten,
        pointer_repointed=outcome.repointed,
        append_only_unchanged=outcome.append_only_unchanged,
        refusals=outcome.refusals,
        warnings=() if outcome.mentions_checked else (path_mentions.NOT_CHECKED,),
        preview_digest=(
            (outcome.paged_digest if paged else
             pointer_write.preview_digest_from_hashes({rel: destination}, outcome.repoint_hashes))
            if destination is not None and destination != rel and not outcome.refusals
            else None
        ),
    )


#: The `manage_memory_file` call whose digest approves a correction's repoints of append-only
#: referrers.
_PREVIEW_CALL = MappingProxyType({"operation": "propose-reclassification"})


def is_preview_call(command: str, arguments: Mapping[str, object]) -> bool:
    """Select the read-only preview operation from the command schema."""
    return (
        command == "manage_memory_file"
        and arguments.get("operation") == _PREVIEW_CALL["operation"]
    )


def managed_preview_stop(vault_root: Path, who: RequestPrincipal) -> Event | None:
    """A live managed observer can prove the unrestricted owner's preview."""
    # MCP and REST are the served protocol surfaces defined by RequestPrincipal.
    if who.surface not in ("mcp", "rest"):
        return None
    stop = file_watcher.observing_inbound_recovery_stop(vault_root)
    if stop is None:
        return None
    request_budget.checkpoint()
    allowed = egress.unrestricted_content_access(vault_root, who)
    request_budget.checkpoint()
    return stop if allowed else None


@dataclass(frozen=True)
class PreviewReleaseBasis:
    policy_fingerprint: str
    access_fingerprint: str
    session_stamp: tuple[str | None, tuple[str, ...]] | None
    complete: bool


@dataclass(frozen=True)
class PreviewReleaseAuthority:
    basis: PreviewReleaseBasis
    erasure: lifecycle.TombstoneSnapshot


def preview_release_basis(vault_root: Path, who: RequestPrincipal) -> PreviewReleaseBasis:
    """Read current actor and policy scope independently of lifecycle history."""
    compiled = policy.load(vault_root)
    exact_access = access.publication_policy_exclusions(vault_root)
    if exact_access is None:
        raise preview_warming()
    access_identity, excludes_paths = exact_access
    complete = not (
        compiled.blocked or compiled.conflicted or excludes_paths
        or any(rule.audience == who.audience_id and rule.options.get("suspended") is not True
               for rule in compiled.rules)
    )
    session = who.verified_authorization_session
    session_stamp = None
    if session is not None and not compiled.empty:
        try:
            with closing(
                store.open_authorization_session_read_connection(vault_root)
            ) as connection:
                session_stamp = authorization_session_authority.session_release_stamp(
                    connection, context=session, audience=who.audience_id, now=int(time.time()),
                )
        except AuthorizationSessionUnavailable:
            raise preview_warming() from None
    return PreviewReleaseBasis(compiled.fingerprint, access_identity, session_stamp, complete)


def preview_release_authority(vault_root: Path, who: RequestPrincipal) -> PreviewReleaseAuthority:
    basis = preview_release_basis(vault_root, who)
    try:
        erasure = lifecycle.tombstone_snapshot(vault_root)
    except request_budget.ReadBudgetExhausted:
        stop = file_watcher.observing_inbound_recovery_stop(vault_root)
        if stop is not None:
            from .find import schedule_tombstone_proof_warm

            schedule_tombstone_proof_warm(vault_root, stop=stop)
        raise
    request_budget.checkpoint()
    if lifecycle.FAIL_CLOSED_TOMBSTONE in erasure.paths:
        raise preview_warming()
    return PreviewReleaseAuthority(basis, erasure)


def preview_release_policy(vault_root: Path, who: RequestPrincipal) -> PreviewReleaseAuthority:
    """Admit a managed report only when the caller can approve its full write plan."""
    authority = preview_release_authority(vault_root, who)
    if not authority.basis.complete:
        raise OpError(
            "RECLASSIFICATION_PREVIEW_UNAVAILABLE",
            "The current policy does not authorize a complete managed reference report.",
        )
    return authority


def _preview_referrer_view(
    vault_root: Path, who: RequestPrincipal, tombstones: frozenset[str], *,
    bounded: bool = False,
    failure: Callable[[], Exception] | None = None,
) -> Callable[[str], bool]:
    def unavailable() -> None:
        raise (failure or preview_warming)()

    def large() -> None:
        raise PreviewNeedsJob()

    def read_inline_page(path: str) -> bytes:
        try:
            return _read_inline_text(vault_root, vault_root.resolve() / path)[0].encode()
        except (OSError, PathGuardError, UnicodeError):
            raise (failure or preview_warming)() from None

    read_page = read_inline_page if bounded else None
    keep = egress.release_walk_filter(vault_root, principal=who, tombstones=tombstones,
                                     read_page=read_page, on_unavailable=unavailable)
    if keep is None:
        keep = egress.content_permits_filter(
            vault_root, who, read_page=read_page, on_unavailable=unavailable
        )

    def visible(path: str) -> bool:
        request_budget.checkpoint()
        if lifecycle.is_tombstoned_in(tombstones, path):
            return False
        with bridges.preview_read_bound(
            _INLINE_MARKDOWN_BYTES if bounded else None, unavailable, large
        ):
            return keep(path)

    return visible


def _preview_generation(vault_root: Path) -> tuple:
    """Bind both the literal inbound index and the projected prose candidates."""
    generation = freshness.preview_reference_generation(vault_root)
    if generation is None:
        raise preview_warming()
    return generation, recall_policy.recall_policy_identity(vault_root)


@dataclass(frozen=True)
class _ReferrerOutcome:
    rewritten: tuple[str, ...] = ()
    repointed: tuple[str, ...] = ()
    #: Keep only the pointer proofs behind `repointed`, independent of page body size.
    repoint_hashes: tuple[tuple[str, str, str], ...] = ()
    append_only_unchanged: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    refusals: tuple[Mapping[str, str], ...] = ()
    #: False when the catalogue could not say which pages cite the source's path in prose.
    mentions_checked: bool = True
    paged_digest: str | None = None


@dataclass(frozen=True)
class _Written:
    """Where a committed correction left the source, and what its relocation rewrote."""

    path: str
    references_updated: int = 0
    path_mentions_updated: int = 0
    warnings: tuple[str, ...] = ()
    pointer_repointed: tuple[Mapping[str, str], ...] = ()


def _visible_inbound(vault_root: Path, rel: str) -> list[InboundLink]:
    """Inbound links over the caller's view, as in a vault without the pages
    withheld from it: a writer other than the owner never learns of a link
    from a page it may not see."""
    visible = egress.visible_page_filter(vault_root)
    return [
        hit
        for hit in find_inbound_wikilinks(vault_root, rel, visible=visible, lineage=False)
        if visible is None or visible(hit.path)
    ]


def _managed_preview_inbound(
    vault_root: Path, rel: str, owner: Event, *,
    read_hashes: Mapping[str, str | None] | None = None,
    candidate_limit: int | None = None, visible: Callable[[str], bool] | None = None,
) -> list[InboundLink]:
    from . import find

    try:
        inbound = vault.cached_inbound_wikilinks(
            vault_root, rel, visible=visible, read_hashes=read_hashes,
            candidate_limit=candidate_limit,
        )
    except vault.InboundCandidatesExceeded:
        raise PreviewNeedsJob() from None
    if file_watcher.observing_inbound_recovery_stop(vault_root) is owner:
        if inbound is not None:
            return inbound
        find.schedule_inbound_rebuild(vault_root, stop=owner)
    raise preview_warming()


def preview_warming() -> OpError:
    """The fact-free retry shared by an incomplete or expired preview."""
    return OpError(
        "RECLASSIFICATION_PREVIEW_WARMING",
        "The source's references are still being checked. Retry the same preview shortly.",
        details={"retry_after_ms": 1000},
    )


def preview_unavailable() -> OpError:
    return OpError(
        "RECLASSIFICATION_PREVIEW_UNAVAILABLE",
        "The current search backend cannot check prose references to the source.",
        remediation="Use a server with the maintained lexical index to preview the move.",
    )


class PreviewNeedsJob(Exception):
    """Current evidence exceeds the interactive admission bound."""


# Keep one large parse out of the interactive request even when it has few candidates.
_INLINE_MARKDOWN_BYTES = 1024 * 1024
_PAGED_APPROVAL_PREFIX = "reclassification-report-v1:"


def _read_inline_text(root: Path, path: Path) -> tuple[str, PathGuard]:
    if path.lstat().st_size > _INLINE_MARKDOWN_BYTES:
        raise PreviewNeedsJob()
    absolute_root = root.resolve()
    rel = path.relative_to(absolute_root).as_posix()
    raw, guard = vault.read_bounded_guarded_bytes(absolute_root, rel, limit=_INLINE_MARKDOWN_BYTES)
    return raw.decode("utf-8"), guard


class _HeldSink(dict):
    """path -> release record, plus the build's identity context, computed once on first use."""

    context_ready = False
    context_usable = False
    compiled = None
    identity = None


#: A private preview job's held-evidence sink, set only inside its own thread. Every other
#: preview sees ``None`` and pays no capture cost.
_HELD_EVIDENCE: ContextVar[_HeldSink | None] = ContextVar(
    "reclassify_source_held_evidence", default=None,
)


@contextmanager
def held_evidence_sink() -> Iterator[dict[str, dict[str, Any]]]:
    """Collect per-row release evidence for the duration of one preview build."""
    sink: _HeldSink = _HeldSink()
    token = _HELD_EVIDENCE.set(sink)
    try:
        yield sink
    finally:
        sink.compiled = None
        _HELD_EVIDENCE.reset(token)


def _load_held_context(sink: _HeldSink, vault_root: Path, who: RequestPrincipal) -> None:
    """Load the row-independent release identity once; a failed load records nothing all build."""
    if sink.context_ready:
        return
    sink.context_ready = True
    try:
        compiled = policy.load(vault_root)
        tombstones = lifecycle.tombstoned_paths(vault_root)
        declared_purpose = egress._declared_purpose(vault_root, who, None)
        identity = egress._frozen_release_identity(egress._release_admission_identity(
            vault_root, compiled, who, declared_purpose, egress._grants_hash(compiled), tombstones,
        ))
    except Exception:  # noqa: BLE001 - no provable identity means every row takes the full path
        return
    if identity is not None:
        sink.compiled = compiled
        sink.identity = identity
        sink.context_usable = True


def _stat_witness(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _record_held_row(
    vault_root: Path,
    sink: _HeldSink,
    who: RequestPrincipal,
    rel: str,
    text: str,
    guard: PathGuard,
    witness: tuple[int, int, int, int, int],
) -> None:
    """Record one simple governed row's release evidence while its bytes are still in hand.

    A row is simple when its permit depends on no other file: no container-parent binding and no
    bridge contribution. Anything unclear leaves no record and takes the full release path. Only
    scalars are stored — the page text dies with this frame.
    """
    from . import document_containers
    from .governance import connector_boundary

    if not rel.lower().endswith(".md") or sink.compiled is None:
        return
    # Only a file's content guard carries a stat witness; a store-guarded page cannot.
    if guard.leaf_policy != "content" or guard.expected_content_hash is None:
        return
    raw = text.encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != guard.expected_content_hash:
        return
    if document_containers.parent_binding_for_path(vault_root, rel, content=raw) is not None:
        return
    if bridges.maybe_bridge(raw) or any(
            rel == grant.path or any(rel == dependency.path for dependency in grant.bridge_of)
            for grant in sink.compiled.release_grants):
        return
    scopes = connector_boundary._membership_sets(
        vault_root, rel, sink.compiled, content=raw, who=who,
    )
    sink[rel] = {
        "path": rel,
        "sha256": digest,
        "size": len(raw),
        "witness": list(witness),
        "scopes": [sorted(scope) for scope in scopes],
        "identity": sink.identity,
    }


def _witnessed_read(
    reader: Callable[[Path, Path], tuple[str, PathGuard]],
    sink: _HeldSink,
    root: Path,
    page: Path,
) -> tuple[str, PathGuard]:
    """Read a page and record its release evidence only when both stat witnesses agree."""
    try:
        resolved = page.resolve()
        before = os.stat(resolved)
    except OSError:
        return reader(root, page)
    text, guard = reader(root, page)
    try:
        rel = resolved.relative_to(root.resolve()).as_posix()
        after = os.stat(resolved)
    except (OSError, ValueError):
        return text, guard
    if _stat_witness(before) == _stat_witness(after):
        try:
            who = effective_principal()
            _load_held_context(sink, root, who)
            if sink.context_usable:
                _record_held_row(root, sink, who, rel, text, guard, _stat_witness(before))
        except Exception:  # noqa: BLE001 - one unprovable row must not lose the others
            pass
    return text, guard


class _PagedPlanProof:
    """Bind the Source and the ordered effects that produce every row of a paged report."""

    def __init__(
        self, path: str, destination: str, text: str, inbound: Sequence[InboundLink],
        classification: tuple[str | None, str | None],
    ) -> None:
        self.moves = {path: destination}
        self._hash = hashlib.sha256(_PAGED_APPROVAL_PREFIX.encode())
        self._add([path, destination, vault.content_hash(text), classification])
        for hit in sorted((link.path, link.line_number, link.raw_target) for link in inbound):
            self._add(hit)

    def _add(self, value: Any) -> None:
        self._hash.update(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())
        self._hash.update(b"\n")

    def examine(
        self, item: move_file_module.InboundRewrite, *, visible: bool, writable: bool
    ) -> None:
        self._add([
            item.path, item.guard.expected_content_hash, vault.content_hash(item.content),
            item.changes, item.mentions, item.left_mentions, item.append_only, item.refusal,
            pointer_write.preview_digest(self.moves, (item.repoint,))
            if item.repoint is not None else None,
            visible, writable,
        ])

    def digest(self) -> str:
        return _PAGED_APPROVAL_PREFIX + self._hash.hexdigest()


def _link_form(raw_target: str) -> str:
    """`path` when a wikilink names a folder, else `basename`.

    Wikilink syntax fixes this split, and it is the one that decides whether a
    link still resolves once its target moves and keeps its name.
    """
    return "path" if "/" in raw_target.split("#", 1)[0] else "basename"


def _referrer_outcome(
    vault_root: Path,
    rel: str,
    text: str,
    destination: str,
    inbound: list[InboundLink],
    *,
    undo: bool = False,
    read_hashes: dict[str, str | None] | None = None,
    candidate_limit: int | None = None,
    paged_classification: tuple[str | None, str | None] | None = None,
    visible: Callable[[str], bool] | None = None,
    held_sink: _HeldSink | None = None,
) -> _ReferrerOutcome:
    """What moving the source to `destination` does to each page that links it.

    It reads the outcome from the same plan the move executes. `inbound` is the
    caller's view, so every page named here is one the caller may see, and the
    report matches a vault without the pages withheld from it. An `undo` is a revert's
    move, which restores append-only pages from their records.
    """
    from .governance import connector_boundary, egress
    from .vault_store import read as store_read

    def borrow_resolver() -> vault.WikilinkResolver:
        from .find import borrow_preview_resolver

        resolver = borrow_preview_resolver(vault_root)
        if resolver is None:
            raise preview_warming()
        return resolver

    relocating = destination != rel
    writable_page = connector_boundary.effect_admission(vault_root)
    paired = move_file_module.paired_move(vault_root, rel, destination) if relocating else None
    if visible is None:
        visible = egress.visible_page_filter(vault_root)
    lookup = None
    if relocating:
        targets = [rel, *([paired[0]] if paired else [])]
        if read_hashes is None:
            lookup = path_mentions.find_pages(vault_root, targets)
        else:
            lookup = path_mentions.find_pages(vault_root, targets, current_published=True,
                                             candidate_limit=candidate_limit)
            if lookup.overflow:
                raise PreviewNeedsJob()
            if lookup.unsupported:
                raise preview_unavailable()
            if not lookup.complete:
                raise preview_warming()
    named_in = [
        page
        for page in (lookup.pages if lookup is not None else ())
        if visible is None or visible(page)
    ]
    forms: dict[str, set[str]] = {}
    for hit in inbound:
        forms.setdefault(hit.path, set()).add(_link_form(hit.raw_target))
    rewritten: list[str] = []
    repointed: list[str] = []
    repoint_hashes: list[tuple[str, str, str]] = []
    unchanged: dict[str, list[str]] = {}
    refusals: list[dict[str, str]] = []
    proof = (
        _PagedPlanProof(rel, destination, text, inbound, paged_classification)
        if paged_classification is not None else None
    )
    iter_reader: Callable[[Path, Path], tuple[str, PathGuard]] | None = (
        _read_inline_text if candidate_limit is not None else None
    )
    if held_sink is not None:
        base_reader = iter_reader or read_guarded_text

        def _held_iter_reader(root: Path, page: Path) -> tuple[str, PathGuard]:
            return _witnessed_read(base_reader, held_sink, root, page)

        iter_reader = _held_iter_reader

    plans = move_file_module.iter_inbound_rewrites(
        vault_root, rel, destination, inbound, named_in=named_in, paired=paired, undo=undo,
        check=foreground_priority.check_cancelled if read_hashes is not None else None,
        resolver_factory=borrow_resolver if read_hashes is not None else None,
        read_text=iter_reader,
    )
    for item in plans:
        if read_hashes is not None:
            read_hashes[item.path] = item.guard.expected_content_hash
        writable = writable_page(item.path)
        if proof is not None:
            proof.examine(item, visible=visible is None or visible(item.path), writable=writable)
        if not writable:
            continue  # the move leaves a page its mover may not write unchanged
        changes = item.changes if relocating else 0
        if relocating and item.left_mentions:
            # A prose mention in an immutable body keeps its bytes; it never refuses the move.
            unchanged.setdefault(MENTION_FORM, []).append(item.path)
        if changes and item.refusal is not None:
            code, reason = item.refusal
            refusals.append({"code": code, "path": item.path, "reason": reason})
        elif changes and item.repoint is not None:
            repointed.append(item.path)
            if proof is None:
                repoint_hashes.append(
                    (item.repoint.path, item.repoint.before_hash, item.repoint.after_hash)
                )
        elif changes or item.mentions:
            rewritten.append(item.path)
        elif item.append_only:
            for form in sorted(forms.get(item.path, ())):
                unchanged.setdefault(form, []).append(item.path)
    if read_hashes is not None:
        planned_paths = forms.keys() | set(named_in)
        if not planned_paths.issubset(read_hashes):
            raise preview_warming()
    if relocating:
        _, self_links = semantic_writes.rewrite_wikilinks_for_move(text, rel, destination)
        if self_links:
            refusals.append({
                "code": "APPEND_ONLY",
                "path": rel,
                "reason": "the source links itself by its path, and its own bytes cannot change",
            })
        occupied = store_read.holds_file(vault_root, destination)
        if occupied and (visible is None or visible(destination)):
            refusals.append({
                "code": "DEST_EXISTS",
                "path": destination,
                "reason": f"another page already occupies {destination}",
            })
    if held_sink is not None:
        row_paths = {
            rel, *rewritten, *repointed,
            *(path for paths in unchanged.values() for path in paths),
            *(refusal["path"] for refusal in refusals),
        }
        for recorded in set(held_sink) - row_paths:
            del held_sink[recorded]
    return _ReferrerOutcome(
        rewritten=tuple(rewritten),
        repointed=tuple(repointed),
        repoint_hashes=tuple(repoint_hashes),
        append_only_unchanged={form: tuple(paths) for form, paths in sorted(unchanged.items())},
        refusals=tuple(refusals),
        mentions_checked=lookup is None or lookup.complete,
        paged_digest=proof.digest() if proof is not None else None,
    )


def _write_correction(
    vault_root: Path,
    rel: str,
    absolute: Path,
    text: str,
    destination: str,
    transform: Callable[[str], str],
    *,
    source_guard: PathGuard,
    today: dt.date,
    extra_writes: tuple[PlannedWrite, ...] = (),
    _validate_prepared_bindings: Callable[[], None] | None = None,
    lineage_entry: Mapping[str, Any] | None = None,
    undo_lineage: bool = False,
    preview_digest: str | None = None,
    classification: tuple[str | None, str | None] = (None, None),
) -> _Written:
    """Commit a correction and return where the source is and what the relocation rewrote.

    A relocation goes through `move_file`, which moves the source, its companion
    and every reference in one transaction, and appends `lineage_entry` to the
    source's lineage or, with `undo_lineage`, removes the entry naming the
    destination. The preview's refusals are raised first, so a correction
    refuses for exactly the reasons its preview gave. The move repoints append-only
    referrers only with the preview's `preview_digest`, and its lineage entry names the
    move's operation, by which an undo finds their pointer-write records and restores each.
    """
    paged = preview_digest is not None and preview_digest.startswith(_PAGED_APPROVAL_PREFIX)
    who = effective_principal()
    if paged and (destination == rel or not egress.unrestricted_content_access(vault_root, who)):
        raise _stale_paged_approval()
    if destination == rel:
        batch_atomic_write(
            [
                PlannedWrite(path=absolute, content=transform(text), guard=source_guard),
                *extra_writes,
            ],
            vault_root=vault_root,
            _validate_prepared_bindings=_validate_prepared_bindings,
        )
        return _Written(rel)
    if not paged:
        outcome = _referrer_outcome(
            vault_root, rel, text, destination, _visible_inbound(vault_root, rel), undo=undo_lineage
        )
        if outcome.refusals:
            refusal = outcome.refusals[0]
            raise ReclassifyError(refusal["code"], refusal["reason"])

    def current_reference_generation() -> tuple:
        generation = freshness.preview_reference_generation(vault_root)
        if generation is None or generation[1] is None or generation[3] is not None:
            raise _stale_paged_approval()
        try:
            release = preview_release_policy(vault_root, who)
        except OpError:
            raise _stale_paged_approval() from None
        return generation, release

    approved_generation = current_reference_generation() if paged else None
    approved_view = (
        _preview_referrer_view(
            vault_root, who, approved_generation[1].erasure.paths,
            failure=_stale_paged_approval
        )
        if approved_generation is not None else None
    )
    if approved_view is not None and not approved_view(rel):
        raise _stale_paged_approval()

    def verify_referrers(inbound, named_in, plans, visible, writable, complete):
        candidates = {hit.path for hit in inbound}.union(named_in)
        planned = {rel, *(item.path for item in plans)}
        if not complete or not candidates.issubset(planned):
            raise _stale_paged_approval()
        proof = _PagedPlanProof(rel, destination, text, inbound, classification)
        for item in plans:
            proof.examine(
                item, visible=visible is None or visible(item.path),
                writable=writable(item.path)
            )
        if proof.digest() != preview_digest:
            raise _stale_paged_approval()
        guards = (source_guard, *(item.guard for item in plans))

        def recheck():
            current_generation, current_authority = current_reference_generation()
            if (current_generation, current_authority) != approved_generation:
                raise _stale_paged_approval()
            still_visible = _preview_referrer_view(vault_root, who, current_authority.erasure.paths,
                                                  failure=_stale_paged_approval)
            if not still_visible(rel) or any(not still_visible(item.path) for item in plans):
                raise _stale_paged_approval()
            try:
                for guard in guards:
                    guard.recheck(vault_root)
            except (OSError, PathGuardError):
                raise _stale_paged_approval() from None
            if current_reference_generation() != approved_generation:
                raise _stale_paged_approval()

        return recheck

    def bound_transform(current: str) -> str:
        # Refuse stale history instead of overwriting an external metadata edit.
        if current != text:
            raise ReclassifyError(
                "SOURCE_CHANGED", "the source changed after it was read; read it again and retry."
            )
        return transform(current)

    try:
        result = move_file_module.move_file(
            vault_root,
            old_path=rel,
            new_path=destination,
            update_wikilinks=True,
            today=today,
            content_transform=bound_transform,
            extra_writes=extra_writes,
            _validate_prepared_bindings=_validate_prepared_bindings,
            _referrer_proof=(
                move_file_module.ReferrerProof(approved_view, verify_referrers)
                if approved_view is not None else None
            ),
            lineage_entry=lineage_entry,
            undo_lineage=undo_lineage,
            preview_digest=preview_digest,
            record_repoints=not undo_lineage,
            _preview_call=_PREVIEW_CALL,
        )
    except move_file_module.MoveFileError as error:
        raise ReclassifyError(error.code, error.reason) from error
    # The move's warnings carry the append-only mentions it kept and a lookup it could not make.
    return _Written(
        result.new_path, result.wikilinks_updated, result.path_mentions_updated,
        tuple(result.warnings), tuple(result.pointer_repointed),
    )


def _stale_paged_approval() -> ReclassifyError:
    return ReclassifyError(
        "RECLASSIFICATION_PREVIEW_STALE",
        "The current correction does not match its paged preview."
        " Preview it again before applying.",
    )


def reclassify(
    vault_root: Path,
    *,
    path: str,
    source_kind: str | None = None,
    domain: str | None = None,
    artifact_path: str | None = None,
    reason: str | None = None,
    today: dt.date | None = None,
    preview_digest: str | None = None,
) -> ReclassifyResult:
    """Correct a captured source's classification and relocate it to match.

    The source's location, kind and domain before the correction are appended
    to its history, unless the correction changes none of them. A relocation that
    repoints append-only referrers needs the `preview_digest` of `propose`.

    `artifact_path` repairs the pointer to the source's raw file in the same step. It
    must name an existing file that fits the identity the source records, and the
    earlier pointer values join the history entry so `revert` restores them. With no
    kind or domain, the source stays where it is.
    """
    vault_root = Path(vault_root)
    rel, absolute = _require_source(vault_root, path)
    pointer_only = source_kind is None and domain is None
    if pointer_only and artifact_path is None:
        raise ReclassifyError(
            "NO_CHANGE_REQUESTED",
            "supply source_kind, domain, artifact_path, or any of them. Reclassification "
            "is not a general file move: a correction with nothing to correct would "
            "relocate a source for no recorded reason.",
        )
    clean_reason = _clean_reason(reason)
    today = today or dt.date.today()

    text, source_guard = read_guarded_text(vault_root, absolute)
    front, _, _ = parse_frontmatter(text)
    current_kind, current_domain = _current_classification(text)
    _history(front)  # a malformed lineage refuses before anything is planned

    pointer_changes, pointer_previous = (
        _plan_pointer(vault_root, rel, front, artifact_path)
        if artifact_path is not None
        else ({}, {})
    )
    previous: dict[str, Any] = {
        "path": rel,
        "kind": _recorded(front, "source_type"),
        "domain": _recorded(front, "domain"),
    }
    plan: source_taxonomy.TaxonomyPlan | None = None
    if pointer_only:
        # A pointer repair leaves the classification, and so the location, as stated.
        _refuse_episode_kind(current_kind, None)
        kind_key, corrected_domain, destination = previous["kind"], previous["domain"], rel
    else:
        taxonomy = source_taxonomy.load_taxonomy(vault_root)
        try:
            effective_domain = domain if domain is not None else current_domain
            kind_resolution, domain_resolution, destination = _projection(
                vault_root,
                rel,
                taxonomy,
                source_kind or current_kind,
                effective_domain,
                supplied=domain is not None,
            )
        except source_taxonomy.TaxonomyError as error:
            raise ReclassifyError("INVALID_CLASSIFICATION", str(error)) from error
        except vocabulary_resolution.VocabularyResolutionError as error:
            raise ReclassifyError(
                error.code, vocabulary_resolution.registry_refusal_reason(vault_root, error)
            ) from error
        _refuse_episode_kind(current_kind, kind_resolution.key)
        if source_kind:
            _refuse_unchosen_kind(kind_resolution.key)
        plan = source_taxonomy.plan_registrations(
            vault_root, kind=kind_resolution, domain=domain_resolution
        )
        kind_key = kind_resolution.key
        corrected_domain = domain_resolution.key if domain_resolution else None
    changed = bool(pointer_changes) or (rel, previous["kind"], previous["domain"]) != (
        destination,
        kind_key,
        corrected_domain,
    )
    if pointer_changes:
        previous[POINTER_KEY] = pointer_previous
    events = _classification_events(
        front, path=rel, destination=destination, kind=kind_key,
        domain=corrected_domain, reason=clean_reason, operation="reclassify",
        pointer={"before": pointer_previous, "after": pointer_changes} if pointer_changes else None,
    )

    # The lineage entry records where the source was and how it was classified. A relocation's
    # move appends it; a correction in place appends it here, naming the path it keeps.
    entry_extra = {key: value for key, value in previous.items() if key != page_lineage.PATH_KEY}
    in_place_entry = (
        page_lineage.append_transform(rel, entry_extra)
        if changed and destination == rel
        else None
    )

    def transform(current: str) -> str:
        return _rewrite_classification(
            current,
            kind=kind_key,
            domain=corrected_domain,
            classification_events=events,
            reason=clean_reason,
            today=today,
            pointer=pointer_changes,
            lineage=in_place_entry,
        )

    written = _write_correction(
        vault_root,
        rel,
        absolute,
        text,
        destination,
        transform,
        today=today,
        source_guard=source_guard,
        extra_writes=tuple(plan.writes) if plan else (),
        _validate_prepared_bindings=(
            (lambda: plan.recheck_bindings(vault_root)) if plan else None
        ),
        lineage_entry=entry_extra if changed and destination != rel else None,
        preview_digest=preview_digest,
        classification=(kind_key, corrected_domain),
    )
    return ReclassifyResult(
        old_path=rel,
        new_path=written.path,
        kind=kind_key,
        domain=corrected_domain,
        reason=clean_reason,
        relocated=destination != rel,
        references_updated=written.references_updated,
        path_mentions_updated=written.path_mentions_updated,
        pointer_repointed=written.pointer_repointed,
        warnings=(*(_introduction_warnings(plan) if plan else ()), *written.warnings),
        vocabulary_receipt=tuple(plan.receipt()) if plan else (),
        repointed=tuple(pointer_changes),
    )


def _previous_location(vault_root: Path, recorded: str) -> str:
    """The location a history entry records, refused unless it is a source page."""
    rel = recorded.replace("\\", "/").strip().lstrip("/")
    if (
        ".." in rel.split("/")
        or not rel.startswith(f"{_sources_root()}/")
        or not rel.lower().endswith(".md")
    ):
        raise ReclassifyError(
            "INVALID_HISTORY",
            f"the latest history entry names {recorded!r}, which is not a source "
            f"page under {_sources_root()}/.",
        )
    try:
        _, rel = resolve_under_vault(vault_root, rel)
    except VaultPathError as error:
        raise ReclassifyError("INVALID_HISTORY", error.reason) from error
    return rel


def revert(
    vault_root: Path,
    *,
    path: str,
    reason: str | None = None,
    today: dt.date | None = None,
) -> ReclassifyResult:
    """Restore the source's latest recorded location, kind and domain.

    The revert runs through the same guarded relocation as a correction and
    removes the lineage entry it restores. A restricted caller's revert also records
    the path it leaves, so a page withheld from it that links that path still finds the
    source (`move_file`). It restores a recorded `other`
    kind, because the entry states what the source was rather than choosing a
    new kind. It refuses an entry that records no kind and domain, such as a
    later plain move's or a legacy path-only entry, and a previous location
    that another page now occupies.
    """
    vault_root = Path(vault_root)
    rel, absolute = _require_source(vault_root, path)
    clean_reason = _clean_reason(reason)
    today = today or dt.date.today()

    text, source_guard = read_guarded_text(vault_root, absolute)
    front, _, _ = parse_frontmatter(text)
    history = _history(front)
    if not history:
        raise ReclassifyError(
            "NO_HISTORY", f"{rel} records no earlier classification to restore."
        )
    recorded_path, kind, domain = _restorable(history[-1])
    restored_pointer = _restorable_pointer(history[-1])
    destination = _previous_location(vault_root, recorded_path)
    current_kind, _ = _current_classification(text)
    _refuse_episode_kind(current_kind, kind)
    from .vault_store import read as store_read

    # The store's answer where it serves the path: a store move leaves the file it vacated.
    if destination != rel and store_read.holds_file(vault_root, destination):
        raise ReclassifyError(
            "PREVIOUS_PATH_OCCUPIED",
            f"another page now occupies {destination}, so the source cannot "
            "return there; move that page or reclassify the source explicitly.",
        )
    events = _classification_events(
        front, path=rel, destination=destination, kind=kind,
        domain=domain, reason=clean_reason, operation="revert",
        pointer=(
            {
                "before": {key: _recorded(front, key) for key in restored_pointer},
                "after": restored_pointer,
            }
            if restored_pointer
            else None
        ),
    )

    def transform(current: str) -> str:
        # An undo removes the entry it restores and appends none; a relocation's move removes it.
        return _rewrite_classification(
            current,
            kind=kind,
            domain=domain,
            classification_events=events,
            reason=clean_reason,
            today=today,
            pointer=restored_pointer,
            lineage=page_lineage.undo_transform(rel) if destination == rel else None,
        )

    written = _write_correction(
        vault_root, rel, absolute, text, destination, transform,
        today=today, source_guard=source_guard, undo_lineage=destination != rel,
    )
    return ReclassifyResult(
        old_path=rel,
        new_path=written.path,
        kind=kind,
        domain=domain,
        reason=clean_reason,
        relocated=destination != rel,
        references_updated=written.references_updated,
        path_mentions_updated=written.path_mentions_updated,
        pointer_repointed=written.pointer_repointed,
        warnings=written.warnings,
        repointed=tuple(restored_pointer),
    )
