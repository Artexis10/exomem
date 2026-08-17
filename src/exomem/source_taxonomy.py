"""Open source-kind and subject-domain vocabularies, and the path they project to.

Two orthogonal axes classify a captured source: what the artifact **is**
(`source_kind`) and what it is **about** (`domain`). Both are open. Any value
that normalizes to a safe canonical key is accepted, including one this code has
never seen, and it auto-registers into
`Knowledge Base/_Schema/source-taxonomy.yaml` on first use — the same contract
`project_keys` already gives the project axis.

The semantic keys are authoritative; the directory layout is a *projection* of
them. A canonical machine key (`research-report`) is deliberately separate from
its display label (`Research report`) and from the path segment it projects to
(`Reports`), so the filesystem never becomes the classification model.

Safety is structural rather than a filter list: a path segment is only ever
derived from a key that already matched `_KEY_RE`, so nothing a caller supplies
can reach a path segment as path input. Two guards remain, because a valid slug
can still be a bad directory name — a filesystem-reserved device name, and a
segment that would collide case-insensitively with another key's.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType

import yaml
from slugify import slugify as _slugify

from .project_keys import _closest_existing_key, _title_case_slug
from .vault import (
    PathGuard,
    PlannedWrite,
    kb_root,
    read_guarded_text,
)

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

#: The low-confidence fallback. It means "could not be determined", never
#: "this code has no label for it".
FALLBACK_KIND = "other"

#: Canonical keys are hyphenated slugs, matching the project-key and tag
#: conventions rather than the underscore form `semantic_language_registry`
#: uses for semantic categories. Both are user-facing vault identifiers.
_KEY_RE = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
MAX_KEY_LENGTH = 41

#: At most `Sources/<Kind>/<Domain>/`. Deepening this is a contract change.
MAX_PROJECTION_DEPTH = 2

SOURCES_ROOT = "Sources"

_REGISTRY_FILENAME = "source-taxonomy.yaml"
_KIND_SECTION = "source_kinds"
_DOMAIN_SECTION = "domains"
_STATUSES = frozenset({"active", "deprecated"})

# Reserved on Windows regardless of case or extension. A perfectly valid slug
# such as `con` or `nul` would title-case into a directory that cannot be
# created or opened there, so it is refused rather than discovered at write
# time on one platform only.
_RESERVED_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{n}" for n in range(1, 10)}
    | {f"lpt{n}" for n in range(1, 10)}
)

_GENERIC_CATEGORY_DESCRIPTION = "captured material"


class TaxonomyError(ValueError):
    """Base class for every refusal this module raises."""


class InvalidTaxonomyValue(TaxonomyError):
    """A supplied value cannot become a safe canonical key."""

    def __init__(self, axis: str, raw: object, reason: str) -> None:
        self.axis = axis
        self.raw = raw
        self.reason = reason
        super().__init__(f"{axis} {raw!r} is not usable: {reason}")


class TaxonomyTypoError(TaxonomyError):
    """A new key looks like a typo of one already known.

    An LLM caller sees `close_match` in the message and re-calls with the
    existing key. A deliberately similar new key is introduced by hand-editing
    the registry, exactly as `ProjectKeyTypoError` documents for project keys.
    """

    def __init__(self, axis: str, key: str, close_match: str, distance: int) -> None:
        self.axis = axis
        self.key = key
        self.close_match = close_match
        self.distance = distance
        super().__init__(
            f"{axis} {key!r} looks like a typo of existing {axis} "
            f"{close_match!r} (edit distance {distance}). Use the existing key, "
            f"or hand-edit _Schema/{_REGISTRY_FILENAME} if this really is a "
            f"distinct new concept."
        )


class TaxonomyCollisionError(TaxonomyError):
    """Two distinct keys would project to the same directory."""

    def __init__(self, axis: str, key: str, other_key: str, segment: str) -> None:
        self.axis = axis
        self.key = key
        self.other_key = other_key
        self.segment = segment
        super().__init__(
            f"{axis} {key!r} would project to {segment!r}, which {other_key!r} "
            f"already uses (directory names collide case-insensitively). Give "
            f"one of them an explicit distinct path_label in "
            f"_Schema/{_REGISTRY_FILENAME}."
        )


@dataclass(frozen=True, slots=True)
class Definition:
    """One entry on one axis."""

    key: str
    label: str
    path_label: str
    description: str = ""
    aliases: tuple[str, ...] = ()
    status: str = "active"
    replaced_by: str | None = None
    builtin: bool = False
    requires_url: bool = False

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "key": self.key,
            "label": self.label,
            "path_label": self.path_label,
            "status": self.status,
        }
        if self.description:
            out["description"] = self.description
        if self.aliases:
            out["aliases"] = sorted(self.aliases)
        if self.replaced_by:
            out["replaced_by"] = self.replaced_by
        if self.builtin:
            out["builtin"] = True
        if self.requires_url:
            out["requires_url"] = True
        return out


@dataclass(frozen=True, slots=True)
class Resolution:
    """The outcome of resolving one supplied value on one axis.

    `status` is one of `builtin`, `alias`, `registered`, `unregistered`, or
    `deprecated`. **`unregistered` is a success**: the key is safe and is used
    as-is. That is what makes the vocabulary open.
    """

    axis: str
    raw: str
    key: str
    label: str
    path_label: str
    status: str
    definition: Definition | None = None
    replaced_by: str | None = None
    close_match: str | None = None

    @property
    def is_registered(self) -> bool:
        return self.status in {"builtin", "alias", "registered", "deprecated"}

    @property
    def requires_url(self) -> bool:
        return bool(self.definition and self.definition.requires_url)

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "axis": self.axis,
            "key": self.key,
            "label": self.label,
            "path_label": self.path_label,
            "status": self.status,
        }
        if self.replaced_by:
            out["replaced_by"] = self.replaced_by
        if self.close_match:
            out["close_match"] = self.close_match
        return out


# --------------------------------------------------------------------------
# Built-in vocabulary
#
# Deliberately generic and public-safe: no user, product, client, or vault
# identifier appears here, mirroring `semantic_language_registry`'s core
# category table and `project_keys._FALLBACK_PROJECTS`. The set is a useful
# default, never the permitted set.
#
# The six kinds Exomem shipped before this module keep their original
# `path_label`, so every existing capture routes byte-identically.
# --------------------------------------------------------------------------
_BUILTIN_KINDS: tuple[Definition, ...] = (
    Definition(
        key="article",
        label="Article",
        path_label="Articles",
        description="captured web or PDF content",
        aliases=("articles",),
        builtin=True,
        requires_url=True,
    ),
    Definition(
        key="session",
        label="Session",
        path_label="Sessions",
        description="pasted conversation transcripts",
        aliases=("sessions", "transcript", "transcripts"),
        builtin=True,
    ),
    Definition(
        key="book",
        label="Book",
        path_label="Books",
        description="book notes or excerpts",
        aliases=("books",),
        builtin=True,
    ),
    Definition(
        key="paper",
        label="Paper",
        path_label="Papers",
        description="academic papers",
        aliases=("papers",),
        builtin=True,
        requires_url=True,
    ),
    Definition(
        key="video",
        label="Video",
        path_label="Videos",
        description="captured video transcripts or notes",
        aliases=("videos",),
        builtin=True,
        requires_url=True,
    ),
    Definition(
        key="research-report",
        label="Research report",
        path_label="Reports",
        description="a compiled investigation or research report",
        aliases=("research-reports", "deep-research"),
        builtin=True,
    ),
    Definition(
        key="official-guidance",
        label="Official guidance",
        path_label="Official Guidance",
        description="guidance published by an authority",
        aliases=("guidance",),
        builtin=True,
    ),
    Definition(
        key="correspondence",
        label="Correspondence",
        path_label="Correspondence",
        description="letters, email, or message threads",
        aliases=("letter", "letters", "email", "emails"),
        builtin=True,
    ),
    Definition(
        key="invoice-receipt",
        label="Invoice or receipt",
        path_label="Invoices",
        description="invoices, receipts, and order confirmations",
        aliases=("invoice", "invoices", "receipt", "receipts"),
        builtin=True,
    ),
    Definition(
        key="contract-legal-document",
        label="Contract or legal document",
        path_label="Contracts",
        description="contracts and other legal instruments",
        aliases=("contract", "contracts", "legal-document", "legal-documents"),
        builtin=True,
    ),
    Definition(
        key="dataset-export",
        label="Dataset export",
        path_label="Datasets",
        description="structured data exports",
        aliases=("dataset", "datasets", "export", "exports"),
        builtin=True,
    ),
    Definition(
        key="manual-documentation",
        label="Manual or documentation",
        path_label="Manuals",
        description="product manuals and reference documentation",
        aliases=("manual", "manuals", "documentation"),
        builtin=True,
    ),
    Definition(
        key="webpage-snapshot",
        label="Webpage snapshot",
        path_label="Snapshots",
        description="point-in-time captures of a web page",
        aliases=("snapshot", "snapshots", "webpage"),
        builtin=True,
    ),
    Definition(
        key=FALLBACK_KIND,
        label="Other",
        path_label="Other",
        description="unclassified captures — a low-confidence fallback",
        builtin=True,
    ),
)

_BUILTIN_DOMAINS: tuple[Definition, ...] = (
    Definition(key="travel", label="Travel", path_label="Travel", builtin=True),
    Definition(key="health", label="Health", path_label="Health", builtin=True),
    Definition(key="finance", label="Finance", path_label="Finance", builtin=True),
    Definition(key="legal", label="Legal", path_label="Legal", builtin=True),
    Definition(key="equipment", label="Equipment", path_label="Equipment", builtin=True),
    Definition(key="software", label="Software", path_label="Software", builtin=True),
    Definition(
        key="photography", label="Photography", path_label="Photography", builtin=True
    ),
    Definition(key="media", label="Media", path_label="Media", builtin=True),
    Definition(key="food", label="Food", path_label="Food", builtin=True),
    Definition(key="research", label="Research", path_label="Research", builtin=True),
    Definition(key="business", label="Business", path_label="Business", builtin=True),
)


@lru_cache(maxsize=1)
def builtin_kinds() -> Mapping[str, Definition]:
    return MappingProxyType({item.key: item for item in _BUILTIN_KINDS})


@lru_cache(maxsize=1)
def builtin_domains() -> Mapping[str, Definition]:
    return MappingProxyType({item.key: item for item in _BUILTIN_DOMAINS})


def _alias_table(definitions: Iterable[Definition]) -> dict[str, str]:
    table: dict[str, str] = {}
    for definition in definitions:
        for alias in definition.aliases:
            table[alias] = definition.key
    return table


@lru_cache(maxsize=1)
def _builtin_kind_aliases() -> Mapping[str, str]:
    return MappingProxyType(_alias_table(_BUILTIN_KINDS))


@lru_cache(maxsize=1)
def _builtin_domain_aliases() -> Mapping[str, str]:
    return MappingProxyType(_alias_table(_BUILTIN_DOMAINS))


# --------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------
def normalize(raw: object, *, axis: str = "value") -> str:
    """Return the canonical key for `raw`, or raise `InvalidTaxonomyValue`.

    NFKC-fold, then run the value through the same slugifier the vault already
    uses for filenames, then require the result to match `_KEY_RE`. That single
    gate is what eliminates traversal segments, absolute and drive-qualified
    paths, network shares, embedded separators, dot segments, trailing dots and
    spaces, control characters, and pathological Unicode — none of them can
    survive it, so none can reach a path segment.
    """
    if not isinstance(raw, str):
        raise InvalidTaxonomyValue(axis, raw, "must be a string")
    folded = unicodedata.normalize("NFKC", raw).strip()
    if not folded:
        raise InvalidTaxonomyValue(axis, raw, "is empty")
    # `word_boundary` is off here: truncating a canonical identifier at a word
    # boundary would silently produce a *different* key. Over-length is refused
    # below instead, so the caller learns rather than being surprised.
    key = _slugify(folded, max_length=0, lowercase=True)
    if not key:
        raise InvalidTaxonomyValue(
            axis, raw, "contains no characters usable in a canonical key"
        )
    if len(key) > MAX_KEY_LENGTH:
        raise InvalidTaxonomyValue(
            axis,
            raw,
            f"normalizes to {len(key)} characters, over the "
            f"{MAX_KEY_LENGTH}-character canonical-key limit",
        )
    if not _KEY_RE.fullmatch(key):
        raise InvalidTaxonomyValue(
            axis, raw, f"normalizes to {key!r}, which does not match {_KEY_RE.pattern}"
        )
    if key in _RESERVED_DEVICE_NAMES:
        raise InvalidTaxonomyValue(
            axis,
            raw,
            f"normalizes to {key!r}, a filesystem-reserved device name that "
            f"cannot be used as a directory. Choose a more specific key",
        )
    return key


def _validate_path_label(axis: str, key: str, label: str) -> str:
    """Accept a registry-declared path segment only if it is a safe directory name."""
    cleaned = unicodedata.normalize("NFKC", str(label)).strip().strip(".")
    if not cleaned:
        raise InvalidTaxonomyValue(axis, label, f"path_label for {key!r} is empty")
    if any(char in cleaned for char in '<>:"/\\|?*') or any(
        ord(char) < 32 for char in cleaned
    ):
        raise InvalidTaxonomyValue(
            axis, label, f"path_label for {key!r} contains a reserved character"
        )
    if cleaned.split(".")[0].casefold() in _RESERVED_DEVICE_NAMES:
        raise InvalidTaxonomyValue(
            axis, label, f"path_label for {key!r} is a reserved device name"
        )
    return cleaned


def derive_label(key: str) -> str:
    """`marine-biology` -> `Marine biology`. Display, never a path segment."""
    words = key.split("-")
    return " ".join([words[0].capitalize(), *words[1:]])


def derive_path_label(key: str) -> str:
    """`marine-biology` -> `Marine Biology`, reusing the project-key convention."""
    return _title_case_slug(key)


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SourceTaxonomy:
    """A snapshot of both axes: built-ins overlaid by the vault registry."""

    schema_version: int = SCHEMA_VERSION
    kinds: Mapping[str, Definition] = field(default_factory=dict)
    domains: Mapping[str, Definition] = field(default_factory=dict)
    kind_aliases: Mapping[str, str] = field(default_factory=dict)
    domain_aliases: Mapping[str, str] = field(default_factory=dict)
    findings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("kinds", "domains", "kind_aliases", "domain_aliases"):
            object.__setattr__(
                self, name, MappingProxyType(dict(getattr(self, name)))
            )
        object.__setattr__(self, "findings", tuple(self.findings))

    # -- resolution --------------------------------------------------------
    def resolve_kind(self, raw: object) -> Resolution:
        """Resolve a source kind. Near-misses of a known key are refused."""
        resolution = self._resolve(
            "source_kind", raw, self.kinds, self.kind_aliases
        )
        if resolution.status == "unregistered":
            close = _closest_existing_key(resolution.key, list(self.kinds))
            if close is not None:
                raise TaxonomyTypoError("source_kind", resolution.key, *close)
        return resolution

    def resolve_domain(self, raw: object) -> Resolution:
        """Resolve a subject domain. A near-miss *warns* rather than refusing.

        Kinds are a small, mostly-hyphenated, product-seeded vocabulary where a
        one-character difference is nearly always a typo. Domains are an open
        single-word space where near-misses are frequently distinct real words —
        `wealth` beside `health`, `feed` beside `food`. Refusing those would
        make capture burdensome for no epistemic gain, so the close match is
        reported on the resolution and surfaced as a warning instead.
        """
        resolution = self._resolve("domain", raw, self.domains, self.domain_aliases)
        if resolution.status == "unregistered":
            close = _closest_existing_key(resolution.key, list(self.domains))
            if close is not None:
                return Resolution(
                    axis=resolution.axis,
                    raw=resolution.raw,
                    key=resolution.key,
                    label=resolution.label,
                    path_label=resolution.path_label,
                    status=resolution.status,
                    close_match=close[0],
                )
        return resolution

    def _resolve(
        self,
        axis: str,
        raw: object,
        definitions: Mapping[str, Definition],
        aliases: Mapping[str, str],
    ) -> Resolution:
        key = normalize(raw, axis=axis)
        canonical = aliases.get(key, key)
        definition = definitions.get(canonical)
        if definition is None:
            # Open vocabulary: an unknown but safe key is accepted as itself.
            return Resolution(
                axis=axis,
                raw=str(raw),
                key=key,
                label=derive_label(key),
                path_label=derive_path_label(key),
                status="unregistered",
            )
        if definition.status == "deprecated":
            status = "deprecated"
        elif canonical != key:
            status = "alias"
        elif definition.builtin:
            status = "builtin"
        else:
            status = "registered"
        return Resolution(
            axis=axis,
            raw=str(raw),
            key=canonical,
            label=definition.label,
            path_label=definition.path_label,
            status=status,
            definition=definition,
            replaced_by=definition.replaced_by,
        )

    # -- browsing ----------------------------------------------------------
    def kind_for_path_label(self, segment: str) -> Definition | None:
        wanted = segment.casefold()
        for definition in self.kinds.values():
            if definition.path_label.casefold() == wanted:
                return definition
        return None

    def category_description(self, segment: str) -> str:
        """Description for a top-level `Sources/<segment>/` folder."""
        definition = self.kind_for_path_label(segment)
        if definition is not None and definition.description:
            return definition.description
        return _GENERIC_CATEGORY_DESCRIPTION

    def category_descriptions(self) -> dict[str, str]:
        """`{path_label: description}` for every kind that declares one."""
        return {
            definition.path_label: definition.description
            for definition in sorted(self.kinds.values(), key=lambda item: item.key)
            if definition.description
        }


def registry_path(vault_root: Path) -> Path:
    return kb_root(Path(vault_root)) / "_Schema" / _REGISTRY_FILENAME


def core_taxonomy() -> SourceTaxonomy:
    """Built-ins only — the vocabulary before any vault registry is read."""
    return SourceTaxonomy(
        kinds=dict(builtin_kinds()),
        domains=dict(builtin_domains()),
        kind_aliases=dict(_builtin_kind_aliases()),
        domain_aliases=dict(_builtin_domain_aliases()),
    )


def load_taxonomy(vault_root: Path) -> SourceTaxonomy:
    """Read the vault registry and overlay it on the built-ins.

    Never raises on a malformed file: the built-ins alone are a working
    vocabulary, so a broken registry degrades to defaults with a finding rather
    than refusing every capture.
    """
    path = registry_path(vault_root)
    if not path.exists():
        return core_taxonomy()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning("%s unreadable (%s); using built-in source taxonomy", path, exc)
        return SourceTaxonomy(
            kinds=dict(builtin_kinds()),
            domains=dict(builtin_domains()),
            kind_aliases=dict(_builtin_kind_aliases()),
            domain_aliases=dict(_builtin_domain_aliases()),
            findings=(f"{_REGISTRY_FILENAME} is unreadable: {exc}",),
        )
    return taxonomy_from_data(data)


def taxonomy_from_data(data: object) -> SourceTaxonomy:
    findings: list[str] = []
    if not isinstance(data, dict):
        return SourceTaxonomy(
            kinds=dict(builtin_kinds()),
            domains=dict(builtin_domains()),
            kind_aliases=dict(_builtin_kind_aliases()),
            domain_aliases=dict(_builtin_domain_aliases()),
            findings=(f"{_REGISTRY_FILENAME} is not a mapping",),
        )
    kinds = _merge_section(
        "source_kind", builtin_kinds(), data.get(_KIND_SECTION), findings
    )
    domains = _merge_section(
        "domain", builtin_domains(), data.get(_DOMAIN_SECTION), findings
    )
    _report_segment_collisions("source_kind", kinds, findings)
    _report_segment_collisions("domain", domains, findings)
    return SourceTaxonomy(
        kinds=kinds,
        domains=domains,
        kind_aliases=_alias_table(kinds.values()),
        domain_aliases=_alias_table(domains.values()),
        findings=tuple(findings),
    )


def _merge_section(
    axis: str,
    builtins: Mapping[str, Definition],
    section: object,
    findings: list[str],
) -> dict[str, Definition]:
    merged = dict(builtins)
    if section is None:
        return merged
    if not isinstance(section, dict):
        findings.append(f"{axis} section is not a mapping; ignored")
        return merged
    for raw_key, entry in section.items():
        try:
            key = normalize(raw_key, axis=axis)
        except TaxonomyError as exc:
            findings.append(str(exc))
            continue
        if isinstance(entry, str):
            entry = {"label": entry}
        if entry is None:
            entry = {}
        if not isinstance(entry, dict):
            findings.append(f"{axis} {key!r} entry is not a mapping; ignored")
            continue
        existing = merged.get(key)
        try:
            merged[key] = _definition_from_entry(axis, key, entry, existing, findings)
        except TaxonomyError as exc:
            findings.append(str(exc))
    return merged


def _definition_from_entry(
    axis: str,
    key: str,
    entry: Mapping[str, object],
    existing: Definition | None,
    findings: list[str],
) -> Definition:
    label = str(entry.get("label") or "").strip()
    if not label:
        label = existing.label if existing else derive_label(key)
    raw_segment = entry.get("path_label")
    if raw_segment:
        path_label = _validate_path_label(axis, key, str(raw_segment))
    elif existing is not None:
        path_label = existing.path_label
    else:
        path_label = derive_path_label(key)
    status = str(entry.get("status") or "active").strip().casefold()
    if status not in _STATUSES:
        findings.append(f"{axis} {key!r} has unknown status {status!r}; using active")
        status = "active"
    aliases: list[str] = []
    for candidate in _string_list(entry.get("aliases")):
        try:
            aliases.append(normalize(candidate, axis=f"{axis} alias"))
        except TaxonomyError as exc:
            findings.append(str(exc))
    if existing is not None:
        aliases.extend(existing.aliases)
    description = str(entry.get("description") or "").strip() or (
        existing.description if existing else ""
    )
    requires_url = entry.get("requires_url")
    if requires_url is None:
        requires_url = bool(existing.requires_url) if existing else False
    return Definition(
        key=key,
        label=label,
        path_label=path_label,
        description=description,
        aliases=tuple(dict.fromkeys(alias for alias in aliases if alias != key)),
        status=status,
        replaced_by=(str(entry.get("replaced_by")).strip() or None)
        if entry.get("replaced_by")
        else (existing.replaced_by if existing else None),
        builtin=bool(existing.builtin) if existing else False,
        requires_url=bool(requires_url),
    )


def _string_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    return []


def _report_segment_collisions(
    axis: str, definitions: Mapping[str, Definition], findings: list[str]
) -> None:
    seen: dict[str, str] = {}
    for key in sorted(definitions):
        segment = definitions[key].path_label.casefold()
        if segment in seen and seen[segment] != key:
            findings.append(
                f"{axis} {key!r} and {seen[segment]!r} both project to "
                f"{definitions[key].path_label!r}"
            )
            continue
        seen.setdefault(segment, key)


# --------------------------------------------------------------------------
# Projection
# --------------------------------------------------------------------------
def source_segments(
    kind: Resolution, domain: Resolution | None = None
) -> tuple[str, ...]:
    """`Sources/<Kind>[/<Domain>]` as path segments.

    Every segment comes from a `Resolution`, whose `path_label` is either a
    registry value already checked by `_validate_path_label` or one derived from
    a key that matched `_KEY_RE`. Raw caller input never reaches here.
    """
    segments = [SOURCES_ROOT, kind.path_label]
    if domain is not None:
        segments.append(domain.path_label)
    depth = len(segments) - 1
    if depth > MAX_PROJECTION_DEPTH:  # pragma: no cover - structural guard
        raise TaxonomyError(
            f"source projection depth {depth} exceeds {MAX_PROJECTION_DEPTH}"
        )
    return tuple(segments)


def source_directory(
    vault_root: Path, kind: Resolution, domain: Resolution | None = None
) -> Path:
    """Absolute destination directory for a capture with this classification."""
    return kb_root(Path(vault_root)).joinpath(*source_segments(kind, domain))


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TaxonomyIntroduction:
    axis: str
    key: str
    label: str
    path_label: str


@dataclass(frozen=True, slots=True)
class TaxonomyPlan:
    taxonomy: SourceTaxonomy
    introductions: tuple[TaxonomyIntroduction, ...] = ()
    writes: tuple[PlannedWrite, ...] = ()

    @property
    def introduced_keys(self) -> tuple[str, ...]:
        return tuple(item.key for item in self.introductions)


_BOOTSTRAP_HEADER = f"""\
# Source-taxonomy vocabulary for captured sources.
#
# Two independent axes: `{_KIND_SECTION}` is what an artifact IS, `{_DOMAIN_SECTION}`
# is what it is ABOUT. Both are OPEN — any slug-shaped key is accepted and
# auto-registers here on first use. Exomem ships useful defaults in code; this
# file records what your vault added, and lets you override how any key is
# displayed or where it is filed.
#
# Per entry: label (display), path_label (the Sources/ directory segment),
# description, aliases, status (active|deprecated), replaced_by, requires_url.
schema_version: {SCHEMA_VERSION}
{_KIND_SECTION}: {{}}
{_DOMAIN_SECTION}: {{}}
"""


def plan_registrations(
    vault_root: Path,
    *,
    kind: Resolution | None = None,
    domain: Resolution | None = None,
) -> TaxonomyPlan:
    """Plan a folded registry update for any unregistered resolution.

    Returns an empty plan when nothing new is used, so the common case adds no
    write at all. When something is new the returned `PlannedWrite` is folded
    into the caller's existing atomic batch, exactly as `plan_project_keys`
    does, so a capture and its vocabulary land together or not at all.
    """
    root = Path(vault_root)
    path = registry_path(root)
    pending = [
        resolution
        for resolution in (kind, domain)
        if resolution is not None and resolution.status == "unregistered"
    ]
    taxonomy = load_taxonomy(root)
    if not pending:
        return TaxonomyPlan(taxonomy)

    try:
        text, guard = read_guarded_text(root, path)
        path_exists = True
    except FileNotFoundError:
        text = _BOOTSTRAP_HEADER
        guard = PathGuard.capture(
            root, path.relative_to(root).as_posix(), leaf_policy="absent"
        )
        path_exists = False

    introductions: list[TaxonomyIntroduction] = []
    definitions = {"source_kind": dict(taxonomy.kinds), "domain": dict(taxonomy.domains)}
    for resolution in pending:
        axis = resolution.axis
        known = definitions[axis]
        if resolution.key in known:
            continue
        _refuse_segment_collision(axis, resolution, known)
        introductions.append(
            TaxonomyIntroduction(
                axis=axis,
                key=resolution.key,
                label=resolution.label,
                path_label=resolution.path_label,
            )
        )
        known[resolution.key] = Definition(
            key=resolution.key,
            label=resolution.label,
            path_label=resolution.path_label,
        )

    if not introductions:
        return TaxonomyPlan(taxonomy)

    updated = _append_introductions(text, introductions)
    write = PlannedWrite(
        path,
        updated,
        create_only=not path_exists,
        guard=guard,
    )
    return TaxonomyPlan(
        taxonomy=SourceTaxonomy(
            kinds=definitions["source_kind"],
            domains=definitions["domain"],
            kind_aliases=_alias_table(definitions["source_kind"].values()),
            domain_aliases=_alias_table(definitions["domain"].values()),
            findings=taxonomy.findings,
        ),
        introductions=tuple(introductions),
        writes=(write,),
    )


def _refuse_segment_collision(
    axis: str, resolution: Resolution, known: Mapping[str, Definition]
) -> None:
    wanted = resolution.path_label.casefold()
    for key in sorted(known):
        if key != resolution.key and known[key].path_label.casefold() == wanted:
            raise TaxonomyCollisionError(
                axis, resolution.key, key, resolution.path_label
            )


def _append_introductions(
    text: str, introductions: Iterable[TaxonomyIntroduction]
) -> str:
    """Insert entries under their axis heading, keeping the file readable.

    An empty `{}` placeholder heading is rewritten to a block heading the first
    time that axis gains an entry.
    """
    if not text.endswith("\n"):
        text += "\n"
    for introduction in introductions:
        section = (
            _KIND_SECTION if introduction.axis == "source_kind" else _DOMAIN_SECTION
        )
        entry = (
            f"  # auto-registered by exomem\n"
            f"  {introduction.key}:\n"
            f"    label: {introduction.label}\n"
            f"    path_label: {introduction.path_label}\n"
        )
        text = _insert_in_section(text, section, entry)
    return text


def _insert_in_section(text: str, section: str, entry: str) -> str:
    empty_heading = f"{section}: {{}}\n"
    if empty_heading in text:
        return text.replace(empty_heading, f"{section}:\n{entry}", 1)
    heading = f"{section}:\n"
    index = text.find(heading)
    if index == -1:
        return f"{text}{heading}{entry}"
    insert_at = index + len(heading)
    return text[:insert_at] + entry + text[insert_at:]


def register(
    vault_root: Path,
    *,
    kind: str | None = None,
    domain: str | None = None,
) -> tuple[TaxonomyIntroduction, ...]:
    """Resolve and persist vocabulary outside a capture. Idempotent."""
    from .vault import batch_atomic_write

    taxonomy = load_taxonomy(vault_root)
    kind_resolution = taxonomy.resolve_kind(kind) if kind is not None else None
    domain_resolution = taxonomy.resolve_domain(domain) if domain is not None else None
    plan = plan_registrations(
        vault_root, kind=kind_resolution, domain=domain_resolution
    )
    if plan.writes:
        batch_atomic_write(list(plan.writes), vault_root=Path(vault_root))
    return plan.introductions
