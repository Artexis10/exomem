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
record the correction, asserts the body is byte-identical rather than trusting
it, and reuses `move_file` for the relocation and inbound-reference rewriting
instead of reimplementing either.

It never decides a classification. `propose` reports what is deterministically
observable and declines when the evidence supports nothing, because presenting a
plausible guess for approval is how a fallback becomes permanent.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import move_file as move_file_module
from . import source_taxonomy
from .kbdir import kb_dirname
from .vault import (
    VaultPathError,
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

_MAX_REASON_CHARS = 400


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
        }


@dataclass(frozen=True)
class ReclassifyResult:
    old_path: str
    new_path: str
    kind: str
    domain: str | None
    reason: str
    relocated: bool = False
    references_updated: int = 0
    warnings: tuple[str, ...] = field(default_factory=tuple)

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
    if not absolute.is_file():
        raise ReclassifyError("NOT_FOUND", f"no source page at {rel}.")
    return rel, absolute


def _current_classification(text: str) -> tuple[str, str | None]:
    front, _, _ = parse_frontmatter(text)
    kind = front.get("source_type")
    domain = front.get("domain")
    return (
        str(kind) if isinstance(kind, str) and kind else source_taxonomy.FALLBACK_KIND,
        str(domain) if isinstance(domain, str) and domain else None,
    )


def _body_of(text: str) -> str:
    _, body, _ = parse_frontmatter(text)
    return body


def _scalar_pattern(key: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(key)}:[^\n]*$", re.MULTILINE)


def _set_scalar(front_text: str, key: str, value: str, *, after: str) -> str:
    """Set `key: value` in frontmatter text, inserting after `after` if absent.

    Surgical for the same reason `note.py` rewrites `ingested_into:` by hand: a
    parse-and-redump round trip would reformat and reorder every other field on
    an append-only page whose bytes are supposed to stay recognisable.
    """
    line = f"{key}: {value}"
    pattern = _scalar_pattern(key)
    if pattern.search(front_text):
        return pattern.sub(lambda _: line, front_text, count=1)
    anchor = _scalar_pattern(after).search(front_text)
    if anchor:
        return front_text[: anchor.end()] + "\n" + line + front_text[anchor.end() :]
    return front_text.rstrip("\n") + "\n" + line


def _drop_scalar(front_text: str, key: str) -> str:
    return re.sub(rf"^{re.escape(key)}:[^\n]*\n?", "", front_text, count=1, flags=re.MULTILINE)


def _rewrite_classification(
    text: str,
    *,
    kind: str,
    domain: str | None,
    previous_path: str | None,
    reason: str,
    today: dt.date,
) -> str:
    """Return `text` with only the classification and record fields changed."""
    if not text.startswith("---\n"):
        raise ReclassifyError(
            "NO_FRONTMATTER", "the source has no frontmatter block to correct."
        )
    end = text.find("\n---", 4)
    if end == -1:
        raise ReclassifyError(
            "NO_FRONTMATTER", "the source's frontmatter block is unterminated."
        )
    front_text = text[4:end]
    rest = text[end:]

    front_text = _set_scalar(front_text, "source_type", kind, after="title")
    if domain is None:
        front_text = _drop_scalar(front_text, "domain")
    else:
        front_text = _set_scalar(front_text, "domain", domain, after="source_type")
    front_text = _set_scalar(
        front_text, "reclassified", today.isoformat(), after="captured"
    )
    if previous_path is not None:
        front_text = _set_scalar(
            front_text, "reclassified_from", previous_path, after="reclassified"
        )
    front_text = _set_scalar(
        front_text,
        "reclassified_reason",
        _quote(reason),
        after="reclassified_from" if previous_path is not None else "reclassified",
    )
    return "---\n" + front_text + rest


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


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


def propose(vault_root: Path, path: str) -> ReclassifyProposal:
    """Report what a correction would do, without writing anything.

    Evidence is restricted to what is deterministically observable: the domain
    segment already in the source's location, whether it records an origin URL,
    and its existing metadata. That is usually enough to propose a domain and
    rarely enough to propose a kind — and when it supports no kind this reports
    none rather than offering the fallback, which is the failure the open
    vocabulary exists to remove.
    """
    vault_root = Path(vault_root)
    rel, absolute = _require_source(vault_root, path)
    text, _ = read_guarded_text(vault_root, absolute)
    front, _, _ = parse_frontmatter(text)
    current_kind, current_domain = _current_classification(text)

    taxonomy = source_taxonomy.load_taxonomy(vault_root)
    proposed_domain: str | None = None
    domain_evidence: list[str] = []
    if current_domain is None:
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
    if current_kind == source_taxonomy.FALLBACK_KIND:
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

    effective_domain = proposed_domain or current_domain
    destination: str | None = None
    relocation_required = False
    if proposed_kind is not None or proposed_domain is not None:
        try:
            kind_resolution = taxonomy.resolve_kind(proposed_kind or current_kind)
            domain_resolution = (
                taxonomy.resolve_domain(effective_domain) if effective_domain else None
            )
            destination = _destination(vault_root, rel, kind_resolution, domain_resolution)
            relocation_required = destination != rel
        except source_taxonomy.TaxonomyError:
            destination = None

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
        references=len(find_inbound_wikilinks(vault_root, rel)),
    )


def reclassify(
    vault_root: Path,
    *,
    path: str,
    source_kind: str | None = None,
    domain: str | None = None,
    reason: str | None = None,
    today: dt.date | None = None,
) -> ReclassifyResult:
    """Correct a captured source's classification and relocate it to match."""
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
    current_kind, current_domain = _current_classification(text)
    original_body = _body_of(text)

    taxonomy = source_taxonomy.load_taxonomy(vault_root)
    try:
        kind_resolution = taxonomy.resolve_kind(source_kind or current_kind)
        effective_domain = domain if domain is not None else current_domain
        domain_resolution = (
            taxonomy.resolve_domain(effective_domain) if effective_domain else None
        )
    except source_taxonomy.TaxonomyError as error:
        raise ReclassifyError("INVALID_CLASSIFICATION", str(error)) from error

    plan = source_taxonomy.plan_registrations(
        vault_root, kind=kind_resolution, domain=domain_resolution
    )
    destination = _destination(vault_root, rel, kind_resolution, domain_resolution)
    relocating = destination != rel

    def transform(current: str) -> str:
        return _rewrite_classification(
            current,
            kind=kind_resolution.key,
            domain=domain_resolution.key if domain_resolution else None,
            previous_path=rel if relocating else None,
            reason=clean_reason,
            today=today,
        )

    if relocating:
        result = move_file_module.move_file(
            vault_root,
            old_path=rel,
            new_path=destination,
            update_wikilinks=True,
            today=today,
            content_transform=transform,
            extra_writes=tuple(plan.writes),
        )
        references_updated = result.wikilinks_updated
        final_path = result.new_path
    else:
        from .vault import PlannedWrite, batch_atomic_write

        updated = transform(text)
        batch_atomic_write(
            [PlannedWrite(path=absolute, content=updated), *plan.writes],
            vault_root=vault_root,
        )
        references_updated = 0
        final_path = rel

    committed, _ = read_guarded_text(vault_root, vault_root / final_path)
    if _body_of(committed) != original_body:
        raise ReclassifyError(
            "BODY_CHANGED",
            "reclassification altered the source body, which it must never do.",
        )

    return ReclassifyResult(
        old_path=rel,
        new_path=final_path,
        kind=kind_resolution.key,
        domain=domain_resolution.key if domain_resolution else None,
        reason=clean_reason,
        relocated=relocating,
        references_updated=references_updated,
        warnings=tuple(plan.introductions),
    )
