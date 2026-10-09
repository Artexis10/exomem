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

Every correction appends where the source was and how it was classified to
`reclassified_from`, oldest first, so `revert` can restore it.

It never decides a classification. `propose` reports what is deterministically
observable and declines when the evidence supports nothing, because presenting a
plausible guess for approval is how a fallback becomes permanent.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import move_file as move_file_module
from . import record_formats, semantic_writes, source_taxonomy, vocabulary_resolution
from .kbdir import kb_dirname
from .vault import (
    InboundLink,
    PlannedWrite,
    VaultPathError,
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
RECORD_FIELDS = ("reclassified", "reclassified_from", "reclassified_reason")
#: Each entry records `path`, `kind` and `domain`, oldest first. The field and
#: its entry keys are fixed by the source page format, not by any vocabulary.
HISTORY_FIELD = "reclassified_from"

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
    #: whose links name the source by its path.
    rewritten: tuple[str, ...] = ()
    #: Append-only pages the caller may see that the move leaves byte-identical,
    #: grouped by the link form they use (`basename` or `path`).
    append_only_unchanged: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: Why the move would be refused, each naming only a page the caller may see.
    refusals: tuple[Mapping[str, str], ...] = ()

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
                "append_only_unchanged": {
                    form: list(paths) for form, paths in self.append_only_unchanged.items()
                },
            },
            "refusals": [dict(refusal) for refusal in self.refusals],
            **({"refusal_scope": _REFUSAL_SCOPE} if self.destination is not None else {}),
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
    warnings: tuple[str, ...] = field(default_factory=tuple)
    # One line per source kind or domain this correction registered on first use.
    vocabulary_receipt: tuple[str, ...] = field(default_factory=tuple)

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
        if self.warnings:
            value["warnings"] = list(self.warnings)
        if self.vocabulary_receipt:
            value["vocabulary_receipt"] = list(self.vocabulary_receipt)
        return value


def _sources_root() -> str:
    return f"{kb_dirname()}/{source_taxonomy.SOURCES_ROOT}"


def _require_source(vault_root: Path, path: str) -> tuple[str, Path]:
    """Resolve `path` to a source page, refusing anything else."""
    rel = str(path).replace("\\", "/").strip().lstrip("/")
    if not rel:
        raise ReclassifyError("PATH_REQUIRED", "supply the source page to reclassify.")
    if not rel.startswith(f"{kb_dirname()}/"):
        rel = f"{kb_dirname()}/{rel}"
    if not rel.lower().endswith(".md"):
        rel = f"{rel}.md"
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

    # A source the caller may not see is answered exactly as a missing one.
    if not absolute.is_file() or egress.write_target_withheld(vault_root, rel):
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
    """The source's recorded history as list entries, oldest first.

    A legacy scalar recorded only the latest previous path, so it reads as one
    entry whose kind and domain are unknown.
    """
    value = front.get(HISTORY_FIELD)
    if value is None:
        return []
    if isinstance(value, str):
        return [{"path": value}] if value.strip() else []
    if isinstance(value, list):
        return list(value)
    raise ReclassifyError(
        "INVALID_HISTORY",
        f"`{HISTORY_FIELD}` is neither a previous path nor a list of entries; "
        "correct the field before reclassifying this source.",
    )


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
        "the latest history entry records a previous path but not its kind and "
        "domain, as an entry carried over from a legacy `reclassified_from` "
        "path does; reclassify the source explicitly instead.",
    )


def _rewrite_classification(
    text: str,
    *,
    kind: str | None,
    domain: str | None,
    history: list[Any] | None,
    reason: str,
    today: dt.date,
) -> str:
    """Return `text` with only the classification and record fields changed.

    Each field is patched where the YAML parser places it, so every other byte
    of the page is kept, and the body is proven unchanged before anything is
    written. A None kind or domain removes the field, which is how a revert
    restores a source that recorded none. `history=None` leaves the history
    as it is; an empty history removes the field.
    """
    changes: dict[str, Any] = {}
    deletes: list[str] = []
    for key, value in (("source_type", kind), ("domain", domain)):
        if value is None:
            deletes.append(key)
        else:
            changes[key] = value
    changes["reclassified"] = today.isoformat()
    if history:
        changes[HISTORY_FIELD] = history
    elif history is not None:
        deletes.append(HISTORY_FIELD)
    changes["reclassified_reason"] = reason
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


def _destination(vault_root: Path, rel: str, kind, domain) -> str:
    segments = source_taxonomy.source_segments(kind, domain)
    return "/".join((kb_dirname(), *segments, rel.rsplit("/", 1)[-1]))


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
    destination = "/".join((kb_dirname(), *binding.segments, rel.rsplit("/", 1)[-1]))
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
            "recaps recorded with episode_memory: a recap cannot be reclassified, "
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
    rel, absolute = _require_source(vault_root, path)
    text, _ = read_guarded_text(vault_root, absolute)
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
    relocation_required = False
    if proposed_kind is not None or proposed_domain is not None:
        try:
            _, _, destination = _projection(
                vault_root,
                rel,
                taxonomy,
                proposed_kind or current_kind,
                effective_domain,
                supplied=domain is not None,
            )
            relocation_required = destination != rel
        except (source_taxonomy.TaxonomyError, vocabulary_resolution.VocabularyResolutionError):
            destination = None

    inbound = _visible_inbound(vault_root, rel)
    outcome = (
        _referrer_outcome(vault_root, rel, text, destination, inbound)
        if destination is not None
        else _ReferrerOutcome()
    )
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
        append_only_unchanged=outcome.append_only_unchanged,
        refusals=outcome.refusals,
    )


@dataclass(frozen=True)
class _ReferrerOutcome:
    rewritten: tuple[str, ...] = ()
    append_only_unchanged: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    refusals: tuple[Mapping[str, str], ...] = ()


def _visible_inbound(vault_root: Path, rel: str) -> list[InboundLink]:
    """Inbound links over the caller's view, as in a vault without the pages
    withheld from it: a writer other than the owner never learns of a link
    from a page it may not see."""
    from .governance import egress

    visible = egress.visible_page_filter(vault_root)
    return [
        hit
        for hit in find_inbound_wikilinks(vault_root, rel, visible=visible)
        if visible is None or visible(hit.path)
    ]


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
) -> _ReferrerOutcome:
    """What moving the source to `destination` does to each page that links it.

    It reads the outcome from the same plan the move executes. `inbound` is the
    caller's view, so every page named here is one the caller may see, and the
    report matches a vault without the pages withheld from it.
    """
    from .governance import egress

    relocating = destination != rel
    forms: dict[str, set[str]] = {}
    for hit in inbound:
        forms.setdefault(hit.path, set()).add(_link_form(hit.raw_target))
    rewritten: list[str] = []
    unchanged: dict[str, list[str]] = {}
    refusals: list[dict[str, str]] = []
    for item in move_file_module.plan_inbound_rewrites(vault_root, rel, destination, inbound):
        changes = item.changes if relocating else 0
        if changes and item.append_only:
            refusals.append({
                "code": "APPEND_ONLY",
                "path": item.path,
                "reason": (
                    f"{item.path} in append-only {item.append_only}/ links the source "
                    "by its path, and that link would dangle; the source has to stay "
                    "where it is until history-aware resolution exists"
                ),
            })
        elif changes:
            rewritten.append(item.path)
        elif item.append_only:
            for form in sorted(forms.get(item.path, ())):
                unchanged.setdefault(form, []).append(item.path)
    if relocating:
        _, self_links = semantic_writes.rewrite_wikilinks_for_move(text, rel, destination)
        if self_links:
            refusals.append({
                "code": "APPEND_ONLY",
                "path": rel,
                "reason": "the source links itself by its path, and its own bytes cannot change",
            })
        visible = egress.visible_page_filter(vault_root)
        if (vault_root / destination).exists() and (visible is None or visible(destination)):
            refusals.append({
                "code": "DEST_EXISTS",
                "path": destination,
                "reason": f"another page already occupies {destination}",
            })
    return _ReferrerOutcome(
        rewritten=tuple(rewritten),
        append_only_unchanged={form: tuple(paths) for form, paths in sorted(unchanged.items())},
        refusals=tuple(refusals),
    )


def _write_correction(
    vault_root: Path,
    rel: str,
    absolute: Path,
    text: str,
    destination: str,
    transform: Callable[[str], str],
    *,
    today: dt.date,
    extra_writes: tuple[PlannedWrite, ...] = (),
) -> tuple[str, int]:
    """Commit a correction and return the source's final path and rewrite count.

    A relocation goes through `move_file`, which moves the source, its companion
    and every reference in one transaction. The preview's refusals are raised
    first, so a correction refuses for exactly the reasons its preview gave.
    """
    if destination == rel:
        batch_atomic_write(
            [PlannedWrite(path=absolute, content=transform(text)), *extra_writes],
            vault_root=vault_root,
        )
        return rel, 0
    outcome = _referrer_outcome(
        vault_root, rel, text, destination, _visible_inbound(vault_root, rel)
    )
    if outcome.refusals:
        refusal = outcome.refusals[0]
        raise ReclassifyError(refusal["code"], refusal["reason"])
    try:
        result = move_file_module.move_file(
            vault_root,
            old_path=rel,
            new_path=destination,
            update_wikilinks=True,
            today=today,
            content_transform=transform,
            extra_writes=extra_writes,
        )
    except move_file_module.MoveFileError as error:
        raise ReclassifyError(error.code, error.reason) from error
    return result.new_path, result.wikilinks_updated


def reclassify(
    vault_root: Path,
    *,
    path: str,
    source_kind: str | None = None,
    domain: str | None = None,
    reason: str | None = None,
    today: dt.date | None = None,
) -> ReclassifyResult:
    """Correct a captured source's classification and relocate it to match.

    The source's location, kind and domain before the correction are appended
    to its history, unless the correction changes none of them.
    """
    vault_root = Path(vault_root)
    rel, absolute = _require_source(vault_root, path)
    if source_kind is None and domain is None:
        raise ReclassifyError(
            "NO_CHANGE_REQUESTED",
            "supply source_kind, domain, or both. Reclassification is not a "
            "general file move: a correction with nothing to correct would "
            "relocate a source for no recorded reason.",
        )
    clean_reason = _clean_reason(reason)
    today = today or dt.date.today()

    text, _ = read_guarded_text(vault_root, absolute)
    front, _, _ = parse_frontmatter(text)
    current_kind, current_domain = _current_classification(text)
    history = _history(front)

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
    corrected_domain = domain_resolution.key if domain_resolution else None
    previous = {
        "path": rel,
        "kind": _recorded(front, "source_type"),
        "domain": _recorded(front, "domain"),
    }
    changed = (rel, previous["kind"], previous["domain"]) != (
        destination,
        kind_resolution.key,
        corrected_domain,
    )

    def transform(current: str) -> str:
        return _rewrite_classification(
            current,
            kind=kind_resolution.key,
            domain=corrected_domain,
            history=[*history, previous] if changed else None,
            reason=clean_reason,
            today=today,
        )

    final_path, references_updated = _write_correction(
        vault_root,
        rel,
        absolute,
        text,
        destination,
        transform,
        today=today,
        extra_writes=tuple(plan.writes),
    )
    return ReclassifyResult(
        old_path=rel,
        new_path=final_path,
        kind=kind_resolution.key,
        domain=corrected_domain,
        reason=clean_reason,
        relocated=destination != rel,
        references_updated=references_updated,
        warnings=_introduction_warnings(plan),
        vocabulary_receipt=tuple(plan.receipt()),
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
    removes the history entry it restores. It restores a recorded `other`
    kind, because the entry states what the source was rather than choosing a
    new kind. It refuses an entry that records no kind and domain, and a
    previous location that another page now occupies.
    """
    vault_root = Path(vault_root)
    rel, absolute = _require_source(vault_root, path)
    clean_reason = _clean_reason(reason)
    today = today or dt.date.today()

    text, _ = read_guarded_text(vault_root, absolute)
    front, _, _ = parse_frontmatter(text)
    history = _history(front)
    if not history:
        raise ReclassifyError(
            "NO_HISTORY", f"{rel} records no earlier classification to restore."
        )
    recorded_path, kind, domain = _restorable(history[-1])
    destination = _previous_location(vault_root, recorded_path)
    current_kind, _ = _current_classification(text)
    _refuse_episode_kind(current_kind, kind)
    if destination != rel and (vault_root / destination).exists():
        raise ReclassifyError(
            "PREVIOUS_PATH_OCCUPIED",
            f"another page now occupies {destination}, so the source cannot "
            "return there; move that page or reclassify the source explicitly.",
        )

    def transform(current: str) -> str:
        return _rewrite_classification(
            current,
            kind=kind,
            domain=domain,
            history=history[:-1],
            reason=clean_reason,
            today=today,
        )

    final_path, references_updated = _write_correction(
        vault_root, rel, absolute, text, destination, transform, today=today
    )
    return ReclassifyResult(
        old_path=rel,
        new_path=final_path,
        kind=kind,
        domain=domain,
        reason=clean_reason,
        relocated=destination != rel,
        references_updated=references_updated,
    )
