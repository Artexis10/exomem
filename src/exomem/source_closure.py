"""Authorization-safe closure for explicit compiled-note source claims."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from . import find as find_module
from . import memory_refs, vault
from .kbdir import kb_prefix

PUBLIC_UNRESOLVED_LIMIT = 8
PUBLIC_SOURCE_VALUE_BYTES = 256
UNRESOLVED_CODE = "UNRESOLVED_SOURCE_CITATION"
UNRESOLVED_MESSAGE = "one or more explicit sources do not resolve to captured material"
UNRESOLVED_REMEDIATION = (
    "Capture the original material as governed Source or Evidence, then retry the "
    "unchanged derived write with that governed reference; otherwise remove the "
    "unsupported citation explicitly."
)

_COMPILED_TYPES = frozenset(
    {
        "research-note",
        "insight",
        "failure",
        "pattern",
        "experiment",
        "production-log",
    }
)
_EXTERNAL_LOCATOR = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ResolvedSource:
    supplied_value: str
    path: str
    source: str
    guard: vault.PathGuard
    supports_backref: bool


@dataclass(frozen=True, slots=True)
class SourceClosureInspection:
    asserted_values: tuple[str, ...]
    resolved: tuple[ResolvedSource, ...]
    unresolved_values: tuple[str, ...]

    @property
    def closed(self) -> bool:
        return not self.unresolved_values

    @property
    def resolved_paths(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.resolved)

    def public_details(self) -> dict[str, Any]:
        retained = self.unresolved_values[:PUBLIC_UNRESOLVED_LIMIT]
        return {
            "unresolved_sources": [_bounded_value(value) for value in retained],
            "unresolved_source_count": len(self.unresolved_values),
            "unresolved_sources_truncated": len(retained) < len(self.unresolved_values),
        }


@dataclass(frozen=True, slots=True)
class SourceClosurePlan:
    required: bool
    inspection: SourceClosureInspection
    prior_inspection: SourceClosureInspection
    backref_writes: tuple[vault.PlannedWrite, ...]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class SourceClosureViolation(Exception):
    code: str
    reason: str
    details: dict[str, Any]


def _fingerprint(
    inspection: SourceClosureInspection,
    writes: tuple[vault.PlannedWrite, ...],
) -> str:
    digest = sha256()
    for value in inspection.asserted_values:
        digest.update(b"value\0" + value.encode("utf-8", errors="replace") + b"\0")
    for item in inspection.resolved:
        digest.update(b"path\0" + item.path.encode("utf-8") + b"\0")
        digest.update(vault.content_hash(item.source).encode("ascii"))
    for write in writes:
        digest.update(write.path.as_posix().encode("utf-8") + b"\0")
        digest.update(vault.content_hash(write.content).encode("ascii"))
    return digest.hexdigest()


def _bounded_value(value: str) -> str:
    raw = value.encode("utf-8", errors="replace")
    if len(raw) <= PUBLIC_SOURCE_VALUE_BYTES:
        return raw.decode("utf-8")
    suffix = "…"
    prefix = raw[: PUBLIC_SOURCE_VALUE_BYTES - len(suffix.encode("utf-8"))]
    return prefix.decode("utf-8", errors="ignore") + suffix


def _source_values(markdown: str) -> tuple[str, ...]:
    frontmatter, _body, _frontmatter_text = vault.parse_frontmatter(markdown)
    if frontmatter.get("type") not in _COMPILED_TYPES:
        return ()
    raw = frontmatter.get("sources")
    if raw is None or raw == "":
        return ()
    values = raw if isinstance(raw, list | tuple) else [raw]
    return tuple(_target(str(value)) for value in values if _target(str(value)))


def _target(value: str) -> str:
    cleaned = value.strip()
    if cleaned.startswith("[[") and cleaned.endswith("]]"):
        cleaned = cleaned[2:-2].strip()
    cleaned = cleaned.split("|", 1)[0].strip()
    return cleaned.split("#", 1)[0].strip()


def source_claims(markdown: str) -> tuple[str, ...]:
    """Return normalized explicit source values from a compiled page."""
    return _source_values(markdown)


def _default_authorizer(root: Path) -> Callable[[str], bool]:
    from . import record_governance

    return record_governance.full_release_filter(root)


def _eligible_path(path: str) -> bool:
    relative = path.removeprefix(kb_prefix())
    return relative.startswith("Sources/") or relative.startswith("Evidence/")


def _resolve_one(
    root: Path,
    supplied: str,
    *,
    resolver: vault.WikilinkResolver,
    authorize_path: Callable[[str], bool],
) -> ResolvedSource | None:
    target = _target(supplied)
    if not target:
        return None
    try:
        if target.lower().startswith(memory_refs.REF_PREFIX):
            relative = memory_refs.resolve_identifier_read_only(root, target)
        else:
            if _EXTERNAL_LOCATOR.match(target):
                return None
            canonical, warning = vault.normalize_wikilink(
                target,
                root,
                resolver=resolver,
                strict=True,
            )
            if warning:
                return None
            relative = canonical.split("#", 1)[0].removesuffix(".md") + ".md"
    except (
        memory_refs.ReferenceError,
        vault.UnresolvedWikilinkError,
        vault.AmbiguousWikilinkError,
        OSError,
        UnicodeError,
        ValueError,
    ):
        return None
    relative = relative.replace("\\", "/").lstrip("/")
    if not _eligible_path(relative) or not authorize_path(relative):
        return None
    try:
        source, guard = vault.read_guarded_text(root, root / relative)
    except (OSError, UnicodeError, vault.PathGuardError):
        return None
    frontmatter, _body, frontmatter_text = vault.parse_frontmatter(source)
    if frontmatter_text is None or frontmatter.get("type") not in {"source", "evidence"}:
        return None
    return ResolvedSource(
        supplied,
        relative,
        source,
        guard,
        "ingested_into" in frontmatter,
    )


def inspect_source_closure(
    vault_root: Path,
    markdown: str,
    *,
    authorize_path: Callable[[str], bool] | None = None,
    resolver: vault.WikilinkResolver | None = None,
) -> SourceClosureInspection:
    """Resolve final explicit source claims without exposing failed candidates."""
    root = Path(vault_root)
    values = _source_values(markdown)
    if not values:
        return SourceClosureInspection((), (), ())
    authorize = authorize_path or _default_authorizer(root)
    resolver = resolver or find_module.writer_resolver_snapshot(root)
    resolved: list[ResolvedSource] = []
    unresolved: list[str] = []
    for supplied in values:
        item = _resolve_one(
            root,
            supplied,
            resolver=resolver,
            authorize_path=authorize,
        )
        if item is None:
            unresolved.append(supplied)
        else:
            resolved.append(item)
    return SourceClosureInspection(values, tuple(resolved), tuple(unresolved))


def _append_backref(source: str, wikilink: str) -> str:
    """Surgically add one governed derived-page link to `ingested_into`."""
    if wikilink in source:
        return source
    inline = re.search(r"(?m)^(ingested_into:\s*)\[([^\n]*)\]\s*$", source)
    if inline is not None:
        existing = inline.group(2).strip()
        value = f'"{wikilink}"'
        replacement = (
            inline.group(1) + f"[{existing}, {value}]"
            if existing
            else inline.group(1) + f"[{value}]"
        )
        return source[: inline.start()] + replacement + source[inline.end() :]
    block = re.search(r"(?m)^ingested_into:\s*$", source)
    if block is None:
        return source
    line_end = source.find("\n", block.end())
    insertion = f'\n  - "{wikilink}"'
    if line_end == -1:
        return source + insertion
    return source[:line_end] + insertion + source[line_end:]


def _remove_backref(source: str, wikilink: str) -> str:
    escaped = re.escape(wikilink)
    updated = re.sub(
        rf'(?m)^\s*-\s*["\']?{escaped}["\']?\s*\n?',
        "",
        source,
    )
    inline = re.search(r"(?m)^(ingested_into:\s*)\[([^\n]*)\]\s*$", updated)
    if inline is None:
        return updated
    entries = [item.strip() for item in inline.group(2).split(",") if item.strip()]
    retained = [item for item in entries if item.strip("\"'") != wikilink]
    replacement = inline.group(1) + "[" + ", ".join(retained) + "]"
    return updated[: inline.start()] + replacement + updated[inline.end() :]


def _backref_writes(
    root: Path,
    *,
    inspection: SourceClosureInspection,
    prior: SourceClosureInspection,
    destination: str,
) -> tuple[vault.PlannedWrite, ...]:
    if not inspection.closed:
        return ()
    target = destination.removesuffix(".md")
    rendered = vault.render_wikilink_target(target, root)
    wikilink = f"[[{rendered}]]"
    final_by_path = {item.path: item for item in inspection.resolved}
    prior_by_path = {item.path: item for item in prior.resolved}
    writes: list[vault.PlannedWrite] = []
    for path in sorted(set(final_by_path) | set(prior_by_path)):
        item = final_by_path.get(path) or prior_by_path[path]
        updated = item.source
        if path in final_by_path and item.supports_backref:
            updated = _append_backref(updated, wikilink)
        elif path not in final_by_path and item.supports_backref:
            updated = _remove_backref(updated, wikilink)
        # A no-op guarded write is intentional for captured material without a
        # supported back-reference field: it rechecks the source version in the
        # same atomic batch as the derived page.
        writes.append(vault.PlannedWrite(root / path, updated, guard=item.guard))
    return tuple(writes)


def prepare_source_closure(
    vault_root: Path,
    final_markdown: str,
    *,
    destination: str,
    prior_markdown: str | None = None,
    required: bool = True,
    authorize_path: Callable[[str], bool] | None = None,
) -> SourceClosurePlan:
    """Prepare deterministic guarded back-references before writer authority."""
    root = Path(vault_root)
    if not required:
        empty = SourceClosureInspection((), (), ())
        return SourceClosurePlan(False, empty, empty, (), _fingerprint(empty, ()))
    inspection = inspect_source_closure(
        root,
        final_markdown,
        authorize_path=authorize_path,
    )
    prior = (
        inspect_source_closure(root, prior_markdown, authorize_path=authorize_path)
        if prior_markdown is not None
        else SourceClosureInspection((), (), ())
    )
    writes = _backref_writes(
        root,
        inspection=inspection,
        prior=prior,
        destination=destination,
    )
    return SourceClosurePlan(
        True,
        inspection,
        prior,
        writes,
        _fingerprint(inspection, writes),
    )


def enforce_source_closure(
    vault_root: Path,
    final_markdown: str,
    *,
    destination: str,
    prepared: SourceClosurePlan,
    prior_markdown: str | None = None,
    authorize_path: Callable[[str], bool] | None = None,
) -> None:
    """Re-resolve under writer authority and reject unresolved or stale plans."""
    if not prepared.required:
        return
    current = prepare_source_closure(
        vault_root,
        final_markdown,
        destination=destination,
        prior_markdown=prior_markdown,
        required=True,
        authorize_path=authorize_path,
    )
    if not current.inspection.closed:
        raise SourceClosureViolation(
            UNRESOLVED_CODE,
            UNRESOLVED_MESSAGE,
            current.inspection.public_details(),
        )
    if current.fingerprint != prepared.fingerprint:
        raise SourceClosureViolation(
            "STALE_SEMANTIC_WRITE",
            "a cited source changed after source closure; retry the operation",
            {},
        )


def merge_backref_writes(
    existing: tuple[vault.PlannedWrite, ...] | list[vault.PlannedWrite],
    source_writes: tuple[vault.PlannedWrite, ...],
) -> tuple[vault.PlannedWrite, ...]:
    """Join source writes to an auxiliary batch without ambiguous duplicates."""
    merged = list(existing)
    by_path = {write.path: write for write in merged}
    for write in source_writes:
        prior = by_path.get(write.path)
        if prior is not None:
            if prior.content != write.content:
                raise SourceClosureViolation(
                    "STALE_SEMANTIC_WRITE",
                    "a cited source also has a conflicting planned update",
                    {},
                )
            continue
        by_path[write.path] = write
        merged.append(write)
    return tuple(merged)


__all__ = [
    "PUBLIC_UNRESOLVED_LIMIT",
    "ResolvedSource",
    "SourceClosureInspection",
    "SourceClosurePlan",
    "SourceClosureViolation",
    "UNRESOLVED_CODE",
    "UNRESOLVED_MESSAGE",
    "UNRESOLVED_REMEDIATION",
    "enforce_source_closure",
    "inspect_source_closure",
    "merge_backref_writes",
    "prepare_source_closure",
    "source_claims",
]
