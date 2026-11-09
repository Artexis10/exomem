"""Governance for open categories and extensible semantic kinds.

The registry never assigns semantics to an unknown category. It preserves
portable rich-block kinds while allowing explicitly reviewed, vault-owned
category aliases and custom rich headings.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

import yaml

from . import semantic_kinds, semantic_labels
from .kbdir import kb_dirname
from .semantic_labels import LabelDefinition, LabelRegistry
from .vocabulary import registry as vocabulary_registry

SCHEMA_VERSION = 1
# The kinds' entry in `_label_cache`, which the label registries key by their section.
_KINDS_CACHE_KEY = "kinds"
_STATUSES = frozenset({"active", "deprecated"})
_SEPARATORS_RE = re.compile(r"[\s_-]+")
_DEFINITION_FIELDS = frozenset(
    {"description", "aliases", "status", "replaced_by", "scope"}
)
_KIND_DEFINITION_FIELDS = _DEFINITION_FIELDS | {"heading_aliases", "traits", "labels"}


@dataclass(frozen=True, slots=True)
class RegistryFinding(Mapping[str, str]):
    """Deeply immutable, mapping-compatible registry validation finding."""

    code: str
    path: str
    span: str
    severity: str
    detail: str

    def __getitem__(self, key: str) -> str:
        if key not in {"code", "path", "span", "severity", "detail"}:
            raise KeyError(key)
        return getattr(self, key)

    def __iter__(self) -> Iterator[str]:
        return iter(("code", "path", "span", "severity", "detail"))

    def __len__(self) -> int:
        return 5

    def as_dict(self) -> dict[str, str]:
        return {key: self[key] for key in self}


@dataclass(frozen=True, slots=True)
class CategoryDefinition:
    key: str
    description: str
    aliases: tuple[str, ...] = ()
    projects: frozenset[str] = frozenset()
    page_types: frozenset[str] = frozenset()
    status: str = "active"
    replaced_by: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return _definition_dict(self)


@dataclass(frozen=True, slots=True)
class KindDefinition:
    key: str
    description: str
    aliases: tuple[str, ...] = ()
    heading_aliases: tuple[str, ...] = ()
    projects: frozenset[str] = frozenset()
    page_types: frozenset[str] = frozenset()
    status: str = "active"
    replaced_by: str | None = None
    core: bool = False
    traits: frozenset[str] = frozenset()
    # Each (language tag, label) pair, sorted. A label is a heading alias in that language.
    labels: tuple[tuple[str, str], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        out = _definition_dict(self)
        if self.labels:
            out["labels"] = dict(self.labels)
        if self.heading_aliases:
            out["heading_aliases"] = sorted(self.heading_aliases)
        if self.traits:
            out["traits"] = sorted(self.traits)
        if self.core:
            out["core"] = True
        return out


@dataclass(frozen=True, slots=True)
class LabelResolution:
    raw: str
    key: str
    resolved: str | None
    status: str
    definition: CategoryDefinition | KindDefinition | None
    replacement: str | None = None
    findings: tuple[RegistryFinding, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "key": self.key,
            "resolved": self.resolved,
            "status": self.status,
            "definition": self.definition.as_dict() if self.definition else None,
            "replacement": self.replacement,
            "findings": [item.as_dict() for item in self.findings],
        }


class LanguageRegistryView(Protocol):
    """Resolution surface accepted by the semantic-unit parser."""

    @property
    def findings(self) -> Iterable[Mapping[str, str]]: ...

    def resolve_heading(
        self,
        raw: str,
        *,
        project: str | None = None,
        page_type: str | None = None,
    ) -> LabelResolution: ...

    def resolve_category(
        self,
        raw: str,
        *,
        project: str | None = None,
        page_type: str | None = None,
    ) -> LabelResolution: ...

    def resolve_label(self, registry: LabelRegistry, raw: str) -> str | None: ...

    def label_keys(self, registry: LabelRegistry) -> tuple[str, ...]: ...

    def has(self, kind: str, trait: str) -> bool: ...

    def kinds_with(self, trait: str) -> frozenset[str]: ...


@dataclass(frozen=True, slots=True)
class UnitQueryPlan:
    """Resolved unit axes with shared core and instance-qualified extension identity.

    The label sets are the finite raw labels, in this adapter's normalization
    domain, that could interpret to a requested value in some admitted
    instance: authored categories, headings whose kind a category falls back
    to, and headings for requested kinds. They only propose candidates;
    `matches` decides each one against the authoring page's own meaning.
    """

    categories: frozenset[str] | None
    kinds: frozenset[str] | None
    instance_id: str
    core_categories: frozenset[str]
    core_kinds: frozenset[str]
    category_labels: frozenset[str] = frozenset()
    category_kind_labels: frozenset[str] = frozenset()
    kind_labels: frozenset[str] = frozenset()

    def matches(self, category: str, kind: str, instance_id: str | None) -> bool:
        return (
            (self.categories is None or category in self.categories
             and (category in self.core_categories or instance_id == self.instance_id))
            and (self.kinds is None or kind in self.kinds
                 and (kind in self.core_kinds or instance_id == self.instance_id))
        )

    def predicate(self, category: str, kind: str) -> tuple[str, list[str]]:
        """SQL proposing candidates over stored raw-label columns `category` and `kind`."""
        parts: list[str] = []
        params: list[str] = []
        if self.categories is not None:
            labels, fallback = sorted(self.category_labels), sorted(self.category_kind_labels)
            # A rich block without a recognized category falls back to its kind.
            parts.append(
                f"({category} IN ({','.join('?' for _ in labels)}) "
                f"OR {kind} IN ({','.join('?' for _ in fallback)}))"
            )
            params.extend((*labels, *fallback))
        if self.kinds is not None:
            labels = sorted(self.kind_labels)
            parts.append(f"{kind} IN ({','.join('?' for _ in labels)})")
            params.extend(labels)
        return ("(" + " AND ".join(parts) + ")" if parts else "1"), params


def _heading_labels(registry: SemanticLanguageRegistry, kinds: frozenset[str]) -> set[str]:
    """Raw heading labels this adapter can recognize as one of `kinds`.

    Scope is ignored on purpose: applicability belongs to each page's own
    interpretation, so discovery stays a superset.
    """
    core_aliases = _core_heading_aliases()
    labels = set(kinds)
    for label in (*registry.core_kinds, *core_aliases):
        if core_aliases.get(label, label) in kinds and core_aliases.get(label, label) in registry.core_kinds:
            labels.add(label)
    for label in (*registry.kinds, *registry.heading_aliases):
        if registry.heading_aliases.get(label, label) in kinds:
            labels.add(label)
    return labels | registry.kind_heading_labels(kinds)


def _category_labels(registry: SemanticLanguageRegistry, categories: frozenset[str]) -> set[str]:
    labels = set(categories)
    for aliases in (registry.core_category_aliases, registry.category_aliases):
        labels.update(label for label, canonical in aliases.items() if canonical in categories)
    return labels


def unit_query_plan(
    registry: SemanticLanguageRegistry, *, categories=None, kinds=None,
    instance_id: str = "public", admitted: tuple[SemanticLanguageRegistry, ...] = (),
) -> UnitQueryPlan:
    """Use the typed adapter's aliases and canonical keys for every unit query owner.

    `admitted` holds the other instances this caller may interpret: a shared
    core value can be authored through their aliases too.
    """
    def resolve(values, resolver):
        if values is None:
            return None
        return frozenset((result.resolved or result.key) for value in values
                         for result in (resolver(value),))

    resolved_categories = resolve(categories, registry.resolve_category)
    resolved_kinds = resolve(kinds, registry.resolve_kind)
    core_categories = frozenset(registry.core_categories)
    core_kinds = frozenset(registry.core_kinds)
    category_labels: set[str] = set()
    category_kind_labels: set[str] = set()
    kind_labels: set[str] = set()
    for adapter in (registry, *admitted):
        own = adapter is registry
        if resolved_categories is not None:
            wanted = resolved_categories if own else resolved_categories & core_categories
            category_labels |= _category_labels(adapter, wanted)
            # A rich block without a recognized category falls back to its kind.
            category_kind_labels |= _heading_labels(
                adapter, resolved_categories if own else resolved_categories & core_kinds,
            )
        if resolved_kinds is not None:
            kind_labels |= _heading_labels(adapter, resolved_kinds if own else resolved_kinds & core_kinds)
    return UnitQueryPlan(
        resolved_categories, resolved_kinds, instance_id, core_categories, core_kinds,
        frozenset(category_labels), frozenset(category_kind_labels), frozenset(kind_labels),
    )


@dataclass(frozen=True, slots=True)
class SemanticLanguageRegistry:
    schema_version: int
    content_hash: str
    core_kinds: Mapping[str, KindDefinition]
    categories: Mapping[str, CategoryDefinition] = field(default_factory=dict)
    kinds: Mapping[str, KindDefinition] = field(default_factory=dict)
    category_aliases: Mapping[str, str] = field(default_factory=dict)
    kind_aliases: Mapping[str, str] = field(default_factory=dict)
    heading_aliases: Mapping[str, str] = field(default_factory=dict)
    findings: tuple[RegistryFinding, ...] = ()
    core_categories: Mapping[str, CategoryDefinition] = field(default_factory=dict)
    core_category_aliases: Mapping[str, str] = field(default_factory=dict)
    # The entries of each label registry (`semantic_labels.REGISTRIES`), by section: the shipped
    # ones, and the vault's as it wrote them.
    core_label_entries: Mapping[str, Mapping[str, LabelDefinition]] = field(default_factory=dict)
    label_entries: Mapping[str, Mapping[str, LabelDefinition]] = field(default_factory=dict)
    # The labels a vault wrote under a shipped kind's key, which carry nothing else: key to its
    # (language tag, label) pairs. A vault kind keeps its labels in its own definition.
    kind_label_entries: Mapping[str, tuple[tuple[str, str], ...]] = field(default_factory=dict)
    # Each label registry's effective entries and its folded-spelling index, built on first
    # use. The registry is immutable and one instance serves a whole pass, so a unit row
    # resolves by one lookup instead of a rebuild. The kinds are one more entry under
    # `KINDS_SECTION`, indexed by heading form.
    _label_cache: dict[str, tuple[dict[str, LabelDefinition], dict[str, str]]] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        for name in (
            "core_kinds",
            "categories",
            "kinds",
            "category_aliases",
            "kind_aliases",
            "heading_aliases",
            "core_categories",
            "core_category_aliases",
            "kind_label_entries",
        ):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))
        for name in ("core_label_entries", "label_entries"):
            object.__setattr__(
                self,
                name,
                MappingProxyType(
                    {
                        section: MappingProxyType(dict(entries))
                        for section, entries in getattr(self, name).items()
                    }
                ),
            )
        object.__setattr__(self, "findings", tuple(self.findings))

    def _has_error_findings(self) -> bool:
        return any(finding["severity"] == "error" for finding in self.findings)

    def traits_of(self, kind: str) -> frozenset[str]:
        """The traits of the kind `kind`, a resolved key. An unregistered key has none.

        Scope and status do not matter: a deprecated or scoped kind keeps its traits, because
        its units still exist and still behave as the kind does.
        """
        definition = self.core_kinds.get(kind) or self.kinds.get(kind)
        return definition.traits if definition is not None else frozenset()

    def has(self, kind: str, trait: str) -> bool:
        """Whether units of `kind` have `trait`; a name outside `KIND_TRAITS` raises."""
        return semantic_kinds.require_trait(trait) in self.traits_of(kind)

    def kinds_with(self, trait: str) -> frozenset[str]:
        """Every core kind and every vault kind that has `trait`, whatever its status or scope.

        A name outside `KIND_TRAITS` raises instead of answering with an empty set."""
        semantic_kinds.require_trait(trait)
        return frozenset(
            key
            for definitions in (self.core_kinds, self.kinds)
            for key, definition in definitions.items()
            if trait in definition.traits
        )

    def label_definitions(self, registry: LabelRegistry) -> dict[str, LabelDefinition]:
        """The entries of `registry` a reader may use, by key: the shipped ones, then the vault's.

        A vault entry for a shipped key carries only labels; each replaces the shipped label in
        its language. The vault's own entries are unavailable while the registry has an error
        finding, as its kinds are; the shipped entries always resolve.
        """
        return dict(self._labels(registry)[0])

    def label_keys(self, registry: LabelRegistry) -> tuple[str, ...]:
        """The keys a unit row of `registry` may hold, sorted."""
        return tuple(self._labels(registry)[0])

    def resolve_label(self, registry: LabelRegistry, raw: str) -> str | None:
        """The key that `raw` spells in `registry`, as its key or a label in any language, or None.

        The comparison is NFKC-normalised and casefolded, so the matched value is always a key.
        """
        return self._labels(registry)[1].get(semantic_labels.fold(raw))

    def _labels(
        self, registry: LabelRegistry
    ) -> tuple[dict[str, LabelDefinition], dict[str, str]]:
        cached = self._label_cache.get(registry.section)
        if cached is not None:
            return cached
        shipped = self.core_label_entries.get(registry.section, {})
        definitions = dict(shipped)
        if not self._has_error_findings():
            for key, entry in self.label_entries.get(registry.section, {}).items():
                effective = _effective_label(shipped.get(key), entry)
                if effective is not None:
                    definitions[key] = effective
        ordered = dict(sorted(definitions.items()))
        index = {
            semantic_labels.fold(spelling): key
            for key, definition in ordered.items()
            for spelling in definition.spellings
        }
        self._label_cache[registry.section] = (ordered, index)
        return ordered, index

    def kind_label_definitions(self) -> dict[str, LabelDefinition]:
        """Every kind with the labels it answers to, by key: the shipped kinds, then the vault's.

        A vault entry under a shipped key replaces the shipped label in each language it names,
        and the vault's own kinds are unavailable while the registry has an error finding.
        """
        return dict(self._kind_labels()[0])

    def kind_heading_labels(self, kinds: frozenset[str]) -> set[str]:
        """The heading forms, in this registry's normalization, whose label names one of `kinds`."""
        return {label for label, key in self._kind_labels()[1].items() if key in kinds}

    def _kind_labels(self) -> tuple[dict[str, LabelDefinition], dict[str, str]]:
        cached = self._label_cache.get(_KINDS_CACHE_KEY)
        if cached is not None:
            return cached
        definitions = {
            key: LabelDefinition(key=key, labels=kind.labels, description=kind.description, core=True)
            for key, kind in self.core_kinds.items()
        }
        if not self._has_error_findings():
            for key, written in self.kind_label_entries.items():
                if key in definitions:
                    merged = {**dict(definitions[key].labels), **dict(written)}
                    definitions[key] = replace(definitions[key], labels=tuple(sorted(merged.items())))
            for key, kind in self.kinds.items():
                definitions[key] = LabelDefinition(
                    key=key, labels=kind.labels, description=kind.description
                )
        index: dict[str, str] = {}
        for key, definition in definitions.items():
            for _language, text in definition.labels:
                index.setdefault(normalize_label(text), key)
        self._label_cache[_KINDS_CACHE_KEY] = (definitions, index)
        return definitions, index

    def _label_resolution(self, raw: str, key: str, *, heading: bool) -> LabelResolution | None:
        """The resolution of a label that a vault gave a shipped kind, else None.

        A compact kind has no heading, so a label never makes a `##` heading one.
        """
        kind = self._kind_labels()[1].get(key)
        definition = self.core_kinds.get(kind) if kind is not None else None
        if definition is None or (heading and kind in semantic_kinds.compact_kinds()):
            return None
        return LabelResolution(str(raw), key, definition.key, "alias", definition)

    def as_dict(self) -> dict[str, Any]:
        """Return deterministic semantic content (the byte hash is transport metadata)."""
        return {
            "schema_version": self.schema_version,
            "core_kinds": {
                key: self.core_kinds[key].as_dict() for key in sorted(self.core_kinds)
            },
            "core_categories": {
                key: self.core_categories[key].as_dict()
                for key in sorted(self.core_categories)
            },
            "categories": {
                key: self.categories[key].as_dict() for key in sorted(self.categories)
            },
            "kinds": {key: self.kinds[key].as_dict() for key in sorted(self.kinds)},
            "kind_label_entries": {
                key: dict(labels) for key, labels in sorted(self.kind_label_entries.items())
            },
            "category_aliases": dict(sorted(self.category_aliases.items())),
            "kind_aliases": dict(sorted(self.kind_aliases.items())),
            "heading_aliases": dict(sorted(self.heading_aliases.items())),
            "findings": [item.as_dict() for item in self.findings],
        }

    def resolve_category(
        self,
        raw: str,
        *,
        project: str | None = None,
        page_type: str | None = None,
    ) -> LabelResolution:
        key = normalize_label(raw)
        if self._has_error_findings():
            return LabelResolution(
                raw=str(raw),
                key=key,
                resolved=key,
                status="registry_invalid",
                definition=None,
                findings=self.findings,
            )
        core_canonical = self.core_category_aliases.get(key, key)
        core_definition = self.core_categories.get(core_canonical)
        if core_definition is not None:
            status = "alias" if core_canonical != key else "core"
            return LabelResolution(
                str(raw), key, core_canonical, status, core_definition
            )
        canonical = self.category_aliases.get(key, key)
        definition = self.categories.get(canonical)
        if definition is None:
            return LabelResolution(str(raw), key, key, "unregistered", None)
        scope_findings = _scope_findings(
            "categories", canonical, definition, project=project, page_type=page_type
        )
        if scope_findings:
            return LabelResolution(
                str(raw),
                key,
                key,
                "scope_violation",
                definition,
                definition.replaced_by,
                scope_findings,
            )
        status = _resolution_status(key, canonical, definition.status)
        return LabelResolution(
            str(raw), canonical if key == canonical else key, canonical, status, definition,
            definition.replaced_by,
        )

    def resolve_kind(
        self,
        raw: str,
        *,
        project: str | None = None,
        page_type: str | None = None,
    ) -> LabelResolution:
        key = normalize_label(raw)
        core_canonical = _core_heading_aliases().get(key, key)
        core_definition = self.core_kinds.get(core_canonical)
        if core_definition is not None:
            status = "alias" if core_canonical != key else "core"
            return LabelResolution(
                str(raw), key, core_canonical, status, core_definition
            )
        if self._has_error_findings():
            return LabelResolution(
                str(raw), key, None, "registry_invalid", None, findings=self.findings
            )
        labelled = self._label_resolution(raw, key, heading=False)
        if labelled is not None:
            return labelled
        canonical = self.kind_aliases.get(key) or self._kind_labels()[1].get(key, key)
        return self._resolve_custom_kind(
            raw, key, canonical, project=project, page_type=page_type
        )

    def resolve_heading(
        self,
        raw: str,
        *,
        project: str | None = None,
        page_type: str | None = None,
    ) -> LabelResolution:
        """Resolve rich headings; a compact kind, such as ``observation``, stays inert."""
        key = normalize_label(raw)
        core_canonical = _core_heading_aliases().get(key, key)
        core_definition = self.core_kinds.get(core_canonical)
        if core_definition is not None and core_canonical not in semantic_kinds.compact_kinds():
            status = "alias" if core_canonical != key else "core"
            return LabelResolution(
                str(raw), key, core_canonical, status, core_definition
            )
        if self._has_error_findings():
            return LabelResolution(
                str(raw), key, None, "registry_invalid", None, findings=self.findings
            )
        labelled = self._label_resolution(raw, key, heading=True)
        if labelled is not None:
            return labelled
        canonical = self.heading_aliases.get(key) or self._kind_labels()[1].get(key, key)
        return self._resolve_custom_kind(
            raw, key, canonical, project=project, page_type=page_type
        )

    def _resolve_custom_kind(
        self,
        raw: str,
        key: str,
        canonical: str,
        *,
        project: str | None,
        page_type: str | None,
    ) -> LabelResolution:
        definition = self.kinds.get(canonical)
        if definition is None:
            return LabelResolution(str(raw), key, None, "unregistered", None)
        scope_findings = _scope_findings(
            "kinds", canonical, definition, project=project, page_type=page_type
        )
        if scope_findings:
            return LabelResolution(
                str(raw),
                key,
                None,
                "scope_violation",
                definition,
                definition.replaced_by,
                scope_findings,
            )
        status = _resolution_status(key, canonical, definition.status)
        return LabelResolution(
            str(raw), key, canonical, status, definition, definition.replaced_by
        )


@dataclass(frozen=True, slots=True)
class AttachedProjectsLanguageRegistry:
    """Resolve a page label against every project attached to that page."""

    registry: SemanticLanguageRegistry
    projects: tuple[str, ...]

    @property
    def findings(self) -> tuple[RegistryFinding, ...]:
        return self.registry.findings

    def resolve_heading(
        self,
        raw: str,
        *,
        project: str | None = None,
        page_type: str | None = None,
    ) -> LabelResolution:
        return self._resolve(self.registry.resolve_heading, raw, page_type=page_type)

    def resolve_kind(
        self,
        raw: str,
        *,
        project: str | None = None,
        page_type: str | None = None,
    ) -> LabelResolution:
        return self._resolve(self.registry.resolve_kind, raw, page_type=page_type)

    def resolve_category(
        self,
        raw: str,
        *,
        project: str | None = None,
        page_type: str | None = None,
    ) -> LabelResolution:
        return self._resolve(self.registry.resolve_category, raw, page_type=page_type)

    def resolve_label(self, registry: LabelRegistry, raw: str) -> str | None:
        """Label registries have no project or page-type scope, so the shared registry answers."""
        return self.registry.resolve_label(registry, raw)

    def label_keys(self, registry: LabelRegistry) -> tuple[str, ...]:
        return self.registry.label_keys(registry)

    def has(self, kind: str, trait: str) -> bool:
        return self.registry.has(kind, trait)

    def kinds_with(self, trait: str) -> frozenset[str]:
        return self.registry.kinds_with(trait)

    def _resolve(
        self,
        resolver: Callable[..., LabelResolution],
        raw: str,
        *,
        page_type: str | None,
    ) -> LabelResolution:
        resolutions = tuple(
            resolver(raw, project=project, page_type=page_type)
            for project in self.projects or (None,)
        )
        return next(
            (
                resolution
                for resolution in resolutions
                if resolution.status != "scope_violation"
            ),
            resolutions[0],
        )


def for_attached_projects(
    registry: SemanticLanguageRegistry,
    projects: tuple[str, ...],
) -> AttachedProjectsLanguageRegistry:
    """Return the shared language view used by writes and derived indexes."""
    return AttachedProjectsLanguageRegistry(registry, tuple(sorted(set(projects))))


def normalize_label(raw: str) -> str:
    # semantic_blocks.normalize_label is the older lower()-only form that stored
    # keys use; see the note there before unifying the two.
    label = str(raw or "").strip().rstrip(":").strip()
    normalized = unicodedata.normalize("NFKC", label).casefold()
    return _SEPARATORS_RE.sub("_", normalized).strip("_")


def registry_path(vault_root: Path) -> Path:
    return Path(vault_root) / kb_dirname() / "_Schema" / "semantic-language-registry.yaml"


@lru_cache(maxsize=1)
def core_registry() -> SemanticLanguageRegistry:
    core_kinds: dict[str, KindDefinition] = {
        key: KindDefinition(
            key=key, description=kind.description, core=True, traits=kind.traits,
            labels=kind.labels,
        )
        for key, kind in sorted(semantic_kinds.pack().items())
    }
    return SemanticLanguageRegistry(
        SCHEMA_VERSION,
        "none",
        core_kinds,
        core_categories=_core_categories(),
        core_category_aliases=_core_category_aliases(),
        core_label_entries=_core_label_entries(),
    )


def load_registry(
    vault_root: Path | None = None,
    *,
    proposal: Any | None = None,
    registry_scope: str | None = None,
) -> SemanticLanguageRegistry:
    core = core_registry()
    if proposal is not None:
        raw = yaml.safe_dump(proposal, allow_unicode=True, sort_keys=True)
        return _parse_registry_data(proposal, _content_hash(raw), core)
    if vault_root is None:
        return core
    from .vocabulary import instances

    return vocabulary_registry.load(instances.select(Path(vault_root), CATEGORY_SPEC, registry_scope), Path(vault_root)).typed


def load_admitted(
    vault_root: Path, registry_scope: str | None = None
) -> SemanticLanguageRegistry | None:
    """The registry of one instance, or None when this caller cannot admit it.

    As `relation_registry.load_admitted`, None is one answer for an instance that does not
    exist, a binding that is ambiguous and an overlay the caller may not read.
    """
    from .vocabulary import contract, instances

    root = Path(vault_root)
    try:
        selected = instances.select(root, CATEGORY_SPEC, registry_scope)
        if contract.admission_refusal(root, selected) is not None:
            return None
        return vocabulary_registry.load(selected, root).typed
    except vocabulary_registry.RegistryError:
        return None


def load_for_page(
    vault_root: Path, rel_path: str, frontmatter: Mapping[str, Any]
) -> SemanticLanguageRegistry:
    """The registry the page's instance defines; core meanings when this caller cannot admit it."""
    from .vocabulary import instances

    try:
        scope = instances.page_scope(Path(vault_root), rel_path, dict(frontmatter))
    except vocabulary_registry.RegistryError:
        return core_registry()
    registry = load_admitted(vault_root, scope)
    return registry if registry is not None else core_registry()


def clear_cache() -> None:
    """Drop every vault's cached semantic-language snapshot."""
    vocabulary_registry.invalidate_registry(CATEGORY_SPEC.name)


def _parse_overlay_text(raw: str | None, digest: str) -> SemanticLanguageRegistry:
    core = core_registry()
    if raw is None:
        return core
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        return SemanticLanguageRegistry(
            SCHEMA_VERSION,
            digest,
            core.core_kinds,
            findings=(_finding("invalid_yaml", "registry", str(exc)),),
            core_categories=core.core_categories,
            core_category_aliases=core.core_category_aliases,
            core_label_entries=core.core_label_entries,
        )
    return _parse_registry_data(data, digest, core)


def validate_proposal(proposal: Any) -> list[dict[str, str]]:
    raw = yaml.safe_dump(proposal, allow_unicode=True, sort_keys=True)
    registry = _parse_registry_data(proposal, _content_hash(raw), core_registry())
    return [finding.as_dict() for finding in registry.findings]


def empty_proposal() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "categories": {}, "kinds": {}}


def registry_proposal(registry: SemanticLanguageRegistry) -> dict[str, Any]:
    """Serialize the complete reviewed document without derived core metadata.

    A label-registry section such as `verdicts` appears only when the vault has entries in it,
    so a document without that section reads and writes as it always did; an absent section and
    an empty one mean the same.
    """
    # A labels-only entry under a shipped kind's key sits among the vault's own kinds, as written.
    kinds = {
        **{key: _definition_proposal(definition) for key, definition in registry.kinds.items()},
        **{key: {"labels": dict(labels)} for key, labels in registry.kind_label_entries.items()},
    }
    proposal: dict[str, Any] = {
        "schema_version": registry.schema_version,
        "categories": {
            key: _definition_proposal(registry.categories[key])
            for key in sorted(registry.categories)
        },
        "kinds": dict(sorted(kinds.items())),
    }
    for labels in semantic_labels.REGISTRIES:
        entries = registry.label_entries.get(labels.section, {})
        if entries:
            proposal[labels.section] = {
                key: _label_proposal(definition) for key, definition in sorted(entries.items())
            }
    return proposal


def _label_proposal(definition: LabelDefinition) -> dict[str, Any]:
    """A vault label-registry entry as the vault wrote it."""
    out: dict[str, Any] = {}
    if definition.labels:
        out["labels"] = dict(definition.labels)
    if definition.description:
        out["description"] = definition.description
    if definition.members:
        out["members"] = {name: list(values) for name, values in definition.members}
    if definition.display:
        out["display"] = list(definition.display)
    return out


def dropped_label_findings(
    current: SemanticLanguageRegistry, candidate: SemanticLanguageRegistry
) -> list[tuple[LabelRegistry, RegistryFinding]]:
    """An error for each label registry where `candidate` loses a vault key that `current`
    saved, with the registry it names.

    A page that uses a saved verdict, level, modality or mode stops parsing once a save drops
    it. A save may still add an entry or change a label, so only a lost key is refused. A shipped
    key still resolves without its vault entry, so removing an entry that only labels a shipped
    key is no loss. No page stores a key of a registry that is not `on_pages`, such as a
    dimension, so a vault may remove its own entry there; a filter that still names it is refused
    with the keys that exist. Validation and the save both read these findings.
    """
    findings = []
    for labels in semantic_labels.REGISTRIES:
        if not labels.on_pages:
            continue
        section = labels.section
        dropped = sorted(
            set(current.label_entries.get(section, {}))
            - set(candidate.label_entries.get(section, {}))
            - set(current.core_label_entries.get(section, {}))
        )
        if dropped:
            finding = _finding(
                f"{section}_dropped",
                section,
                f"this save would drop the vault {labels.section_words} "
                f"{', '.join(dropped)}; keep them in the proposal's `{section}` section",
            )
            findings.append((labels, finding))
    return findings


def save_registry(
    vault_root: Path,
    proposal: dict[str, Any],
    *,
    expected_hash: str | None = None,
    why: str | None = None,
    operation: str = "infer",
) -> dict[str, Any]:
    """Atomically save one reviewed, complete semantic-language document, keeping history."""
    if not isinstance(proposal, dict) or not {"categories", "kinds"} <= set(proposal):
        raise ValueError(
            "INCOMPLETE_SEMANTIC_LANGUAGE_PROPOSAL: "
            "save requires categories and kinds in one reviewed document"
        )
    registry = load_registry(proposal=proposal)
    errors = [item for item in registry.findings if item["severity"] == "error"]
    if errors:
        raise ValueError(
            "INVALID_SEMANTIC_LANGUAGE_REGISTRY: "
            f"{[item.as_dict() for item in errors]!r}"
        )

    from .vocabulary import instances

    spec = instances.select(Path(vault_root), CATEGORY_SPEC, authoring=True)
    path = spec.overlay(Path(vault_root))
    inspected = vocabulary_registry.load(spec, Path(vault_root))
    current_hash: str | None = None
    if inspected.overlay_text is not None:
        current_hash = inspected.content_hash
        if expected_hash is None:
            raise ValueError(
                "SEMANTIC_LANGUAGE_REGISTRY_EXISTS: provide current expected_hash"
            )
        if expected_hash != current_hash:
            raise ValueError(
                "STALE_SEMANTIC_LANGUAGE_REGISTRY: "
                "expected_hash does not match current hash"
            )
    elif expected_hash is not None:
        raise ValueError(
            "SEMANTIC_LANGUAGE_REGISTRY_MISSING: "
            "expected_hash was provided but the registry does not exist"
        )

    rendered = yaml.safe_dump(
        registry_proposal(registry),
        allow_unicode=True,
        sort_keys=True,
    )
    history = vocabulary_registry.commit(
        spec,
        Path(vault_root),
        rendered,
        operation=operation,
        why=why,
        before_hash=current_hash or vocabulary_registry.NO_OVERLAY_HASH,
        previous=inspected.overlay_text,
        added=vocabulary_registry.added_keys(
            inspected.entries,
            spec.adapter.entries(registry),
        ),
    )
    return {
        "path": path.relative_to(vault_root).as_posix(),
        "content_hash": _content_hash(rendered),
        "previous_hash": current_hash,
        "created": current_hash is None,
        "history": history,
    }


def _parse_registry_data(
    data: Any,
    digest: str,
    core: SemanticLanguageRegistry,
) -> SemanticLanguageRegistry:
    findings: list[RegistryFinding] = []
    if not isinstance(data, dict):
        return SemanticLanguageRegistry(
            SCHEMA_VERSION,
            digest,
            core.core_kinds,
            findings=(_finding("invalid_registry", "registry", "must be an object"),),
            core_categories=core.core_categories,
            core_category_aliases=core.core_category_aliases,
            core_label_entries=core.core_label_entries,
        )
    unknown_root_fields = set(data) - {
        "schema_version",
        "categories",
        "kinds",
        *(labels.section for labels in semantic_labels.REGISTRIES),
    }
    for key in sorted(unknown_root_fields, key=str):
        findings.append(_finding("unknown_field", str(key), "unknown registry field"))
    schema_version = data.get("schema_version")
    if type(schema_version) is not int or schema_version != SCHEMA_VERSION:
        findings.append(
            _finding("invalid_version", "schema_version", f"must be {SCHEMA_VERSION}")
        )

    raw_categories = data.get("categories", {})
    if not isinstance(raw_categories, dict):
        findings.append(_finding("invalid_categories", "categories", "must be an object"))
        raw_categories = {}
    raw_kinds = data.get("kinds", {})
    if not isinstance(raw_kinds, dict):
        findings.append(_finding("invalid_kinds", "kinds", "must be an object"))
        raw_kinds = {}

    kind_label_entries, raw_kinds = _split_kind_labels(raw_kinds, core.core_kinds, findings)

    label_entries = {}
    for labels in semantic_labels.REGISTRIES:
        raw_entries = data.get(labels.section, {})
        if not isinstance(raw_entries, dict):
            findings.append(
                _finding(f"invalid_{labels.section}", labels.section, "must be an object")
            )
            raw_entries = {}
        label_entries[labels.section] = _parse_label_entries(
            labels,
            raw_entries,
            core.core_label_entries.get(labels.section, {}),
            findings,
        )

    categories = _parse_definitions(
        raw_categories, "categories", CategoryDefinition, _DEFINITION_FIELDS, findings
    )
    kinds = _parse_definitions(
        raw_kinds, "kinds", KindDefinition, _KIND_DEFINITION_FIELDS, findings
    )

    category_aliases = _build_aliases(
        categories, "categories", core_labels={}, findings=findings
    )
    core_labels = {key: key for key in core.core_kinds}
    core_labels.update(_core_heading_aliases())
    kind_aliases = _build_aliases(
        kinds, "kinds", core_labels=core_labels, findings=findings
    )
    heading_aliases = _build_heading_aliases(
        kinds, core_labels=core_labels, kind_aliases=kind_aliases, findings=findings
    )
    _check_kind_labels(kinds, kind_label_entries, core.core_kinds, findings)

    _validate_replacements(
        categories,
        "categories",
        findings,
        external_active=frozenset(core.core_categories),
    )
    _validate_replacements(kinds, "kinds", findings)
    findings.extend(_replacement_cycle_findings(categories, "categories"))
    findings.extend(_replacement_cycle_findings(kinds, "kinds"))
    _core_category_shadow_findings(categories, findings)
    _check_dimension_members(
        label_entries.get(semantic_labels.DIMENSIONS.section, {}),
        (*core.core_kinds, *kinds),
        findings,
    )
    sorted_findings = tuple(
        sorted(findings, key=lambda item: (item["path"], item["code"], item["detail"]))
    )
    return SemanticLanguageRegistry(
        SCHEMA_VERSION,
        digest,
        core.core_kinds,
        categories,
        kinds,
        category_aliases,
        kind_aliases,
        heading_aliases,
        sorted_findings,
        core_categories=core.core_categories,
        core_category_aliases=core.core_category_aliases,
        core_label_entries=core.core_label_entries,
        label_entries=label_entries,
        kind_label_entries=kind_label_entries,
    )


def _split_kind_labels(
    raw_kinds: dict[Any, Any],
    shipped: Mapping[str, KindDefinition],
    findings: list[RegistryFinding],
) -> tuple[dict[str, tuple[tuple[str, str], ...]], dict[Any, Any]]:
    """The labels a vault wrote under a shipped kind's key, and the other kind entries.

    An entry under a shipped key that carries anything but `labels` stays among the others,
    which report it as a collision with the shipped kind, as they always did.
    """
    labelled: dict[str, tuple[tuple[str, str], ...]] = {}
    others: dict[Any, Any] = {}
    for raw_key, value in raw_kinds.items():
        if raw_key in shipped and isinstance(value, dict) and set(value) == {"labels"}:
            labelled[raw_key] = _read_labels(value["labels"], f"kinds.{raw_key}", findings)
        else:
            others[raw_key] = value
    return labelled, others


def _check_kind_labels(
    kinds: Mapping[str, KindDefinition],
    written_for_shipped: Mapping[str, tuple[tuple[str, str], ...]],
    shipped: Mapping[str, KindDefinition],
    findings: list[RegistryFinding],
) -> None:
    """Refuse a vault label that another kind answers to, or that no heading can spell.

    A label is a heading, so it collides with another kind's key, alias, heading alias or label
    in the form a heading resolves under, and it needs the shape a heading alias has. A label
    never collides with a category: kinds and categories are separate namespaces, as a heading
    alias already is. The shipped spellings the vault keeps own their kind first, so a finding
    always points at something the vault wrote.
    """
    owners: dict[str, str] = {}
    for key, kind in shipped.items():
        replaced = {language for language, _text in written_for_shipped.get(key, ())}
        kept = (text for language, text in kind.labels if language not in replaced)
        for spelling in (key, *kept):
            owners[normalize_label(spelling)] = key
    owners.update(_core_heading_aliases())
    for key, kind in kinds.items():
        for spelling in (key, *kind.aliases, *kind.heading_aliases):
            owners.setdefault(spelling, key)
    written = [
        (f"kinds.{key}.labels.{language}", key, text)
        for key, labels in (*written_for_shipped.items(), *((k, d.labels) for k, d in kinds.items()))
        for language, text in labels
    ]
    for where, key, text in sorted(written):
        form = normalize_label(text)
        if not _valid_label(form):
            findings.append(_finding(
                "invalid_label",
                where,
                f"{text!r} must start with a letter and hold only letters, digits, spaces, "
                "hyphens and underscores, as a heading does",
            ))
            continue
        owner = owners.setdefault(form, key)
        if owner != key:
            findings.append(_finding(
                "kind_label_collision",
                where,
                f"{text!r} on {key!r} is already the key, an alias or a label of {owner!r}",
            ))


def kind_changes_beyond_labels(
    current: SemanticLanguageRegistry, candidate: SemanticLanguageRegistry
) -> bool:
    """Whether `candidate` changes a vault kind in any way but its labels.

    Adding or changing a label, and adding or removing a labels-only entry under a shipped
    kind's key, are the one change that a reviewed categories save makes to the kinds. Such an
    entry is no vault kind, so it is not among `kinds` here.
    """

    def without_labels(registry: SemanticLanguageRegistry) -> dict[str, dict[str, Any]]:
        return {
            key: {name: value for name, value in _definition_proposal(kind).items() if name != "labels"}
            for key, kind in registry.kinds.items()
        }

    return without_labels(current) != without_labels(candidate)


def _parse_label_entries(
    labels: LabelRegistry,
    raw: dict[Any, Any],
    shipped: Mapping[str, LabelDefinition],
    findings: list[RegistryFinding],
) -> dict[str, LabelDefinition]:
    """The vault's entries of one label registry as it wrote them. A new key needs a
    description; a shipped key takes labels only."""
    entries: dict[str, LabelDefinition] = {}
    for raw_key, value in sorted(raw.items(), key=lambda item: str(item[0])):
        key = str(raw_key)
        span = f"{labels.section}.{key}"
        core = shipped.get(key)
        if core is None and (
            not semantic_labels.KEY_RE.match(key) or len(key) > semantic_labels.MAX_SPELLING
        ):
            findings.append(
                _finding("invalid_key", span, "must be lower-case letters, digits and underscores")
            )
            continue
        if not isinstance(value, dict):
            findings.append(_finding("invalid_definition", span, "must be an object"))
            continue
        for unknown in sorted(set(value) - labels.entry_fields, key=str):
            findings.append(
                _finding("unknown_field", f"{span}.{unknown}", "unknown definition field")
            )
        entry_labels = _read_labels(value.get("labels", {}), span, findings)
        members, display = (
            _parse_members(labels, key, span, value, findings) if labels.has_members else ((), ())
        )
        if core is not None:
            # Kept as written, so a save preserves the entry; `_effective_label` ignores it.
            raw_description = value.get("description")
            description = "" if raw_description is None else str(raw_description).strip()
            fixed = [
                name
                for name, written in (
                    ("description", description), ("members", members), ("display", display),
                )
                if written
            ]
            if fixed:
                findings.append(
                    _finding(
                        f"core_{labels.noun}_shadowed",
                        f"{span}.{fixed[0]}",
                        f"{key!r} is a shipped {labels.entry_words} with a fixed "
                        f"{', '.join(fixed)}, so this entry is ignored; remove "
                        f"{'it' if len(fixed) == 1 else 'them'} to use its labels",
                        severity="warning",
                    )
                )
        else:
            description = value.get("description")
            if not isinstance(description, str) or not description.strip():
                findings.append(
                    _finding("missing_description", f"{span}.description", "is required")
                )
                continue
            description = description.strip()
            if labels.has_members and not members:
                findings.append(
                    _finding(
                        "missing_members",
                        f"{span}.members",
                        f"is required: name families, kinds or keys that make a unit belong to "
                        f"the {labels.entry_words}",
                    )
                )
                continue
        entries[key] = LabelDefinition(
            key=key, labels=entry_labels, description=description, core=core is not None,
            members=members, display=display,
        )
    _check_label_spellings(labels, shipped, entries, findings)
    return entries


def _parse_members(
    labels: LabelRegistry,
    key: str,
    span: str,
    value: Mapping[Any, Any],
    findings: list[RegistryFinding],
) -> tuple[tuple[tuple[str, tuple[str, ...]], ...], tuple[str, ...]]:
    """The member lists and display behaviours one vault dimension wrote, with a finding for each
    that is malformed or that the closed display set does not declare.

    A name's existence as a family, kind or key is `_check_dimension_members`, which needs the
    registries this document sits beside.
    """
    members: tuple[tuple[str, tuple[str, ...]], ...] = ()
    if "members" in value:
        read = semantic_labels.read_members(value["members"])
        if read is None:
            findings.append(
                _finding(
                    "invalid_members",
                    f"{span}.members",
                    "must map `families`, `kinds` or `keys` to a list of keys",
                )
            )
        else:
            members = read[0]
            findings.extend(
                _finding("invalid_members", f"{span}.members.{name}", problem)
                for name, problem in read[1]
            )
    display: tuple[str, ...] = ()
    if "display" in value:
        shown = semantic_labels.read_display(value["display"])
        if shown is None:
            findings.append(
                _finding("invalid_display", f"{span}.display", "must be a list of display behaviours")
            )
        else:
            display = shown[0]
            findings.extend(
                _finding(
                    "undeclared_display_behaviour",
                    f"{span}.display",
                    f"{labels.entry_words} {key!r} names the display behaviour {behaviour!r}, "
                    "which the registry does not declare; use "
                    + ", ".join(sorted(semantic_labels.DISPLAY_BEHAVIOURS)),
                )
                for behaviour in shown[1]
            )
    return members, display


def _check_dimension_members(
    entries: Mapping[str, LabelDefinition],
    kinds: Iterable[str],
    findings: list[RegistryFinding],
) -> None:
    """Refuse a vault dimension member that names no family, kind or governed key, with a finding
    that names the dimension and the member.

    The families are the relation pack's: a vault extension inherits its parent's family, so no
    vault adds one. The kinds are the shipped ones and the vault's own in this document.
    """
    from . import relation_registry, semantic_blocks

    known = {
        "families": (relation_registry.core_registry().core_families, "relation family"),
        "kinds": (frozenset(kinds) | {semantic_labels.ALL_KINDS}, "unit kind"),
        "keys": (frozenset(semantic_blocks.GOVERNED_UNIT_METADATA_KEYS), "governed unit-metadata key"),
    }
    for key, entry in sorted(entries.items()):
        if entry.core:
            continue
        for list_name, values in entry.members:
            exists, noun = known[list_name]
            for value in values:
                if value in exists:
                    continue
                findings.append(
                    _finding(
                        "unknown_dimension_member",
                        f"dimensions.{key}.members.{list_name}",
                        f"dimension {key!r} names the {noun} {value!r}, which does not exist; "
                        + _member_remedy(list_name, exists),
                    )
                )


def _member_remedy(list_name: str, exists: Iterable[str]) -> str:
    """What to write instead: the families and keys that exist, which are few; the kinds by
    where they live, because a vault may hold many."""
    if list_name == "kinds":
        return "name a shipped kind or a kind in this registry's `kinds` section"
    return "use one of " + ", ".join(sorted(exists))


def _read_labels(
    raw: Any, span: str, findings: list[RegistryFinding]
) -> tuple[tuple[str, str], ...]:
    """The valid (language tag, label) pairs of the entry at `span`, and a finding for each
    invalid one. A vault kind and a label registry's entry read their labels through here."""
    read = semantic_labels.read_labels(raw)
    if read is None:
        findings.append(
            _finding(
                "invalid_labels",
                f"{span}.labels",
                "must map a language tag to a label, such as `en: ...`",
            )
        )
        return ()
    labels, problems = read
    findings.extend(
        _finding("invalid_label", f"{span}.labels.{language}", problem)
        for language, problem in problems
    )
    return labels


def _effective_label(
    shipped: LabelDefinition | None, entry: LabelDefinition
) -> LabelDefinition | None:
    """The entry a vault entry gives: a new one as written, or a shipped one whose labels the
    vault's replace, language by language.

    An entry that rewrites a shipped entry's description, members or display gives None: it is
    ignored, as a category that shadows a shipped one is, and its warning says so.
    """
    if shipped is None:
        return entry
    if entry.description or entry.members or entry.display:
        return None
    merged = {**dict(shipped.labels), **dict(entry.labels)}
    return LabelDefinition(
        key=entry.key,
        labels=tuple(sorted(merged.items())),
        description=shipped.description,
        core=True,
        members=shipped.members,
        display=shipped.display,
    )


def _check_label_spellings(
    labels: LabelRegistry,
    shipped: Mapping[str, LabelDefinition],
    entries: Mapping[str, LabelDefinition],
    findings: list[RegistryFinding],
) -> None:
    """Refuse a vault key or label that another entry of the same registry answers to, in any
    language, or that a row beside a probability would read as a probability.

    The shipped spellings the vault keeps own their entry first, so a finding always points at
    something the vault wrote.
    """
    effective = {
        key: entry
        for key, entry in entries.items()
        if _effective_label(shipped.get(key), entry) is not None
    }
    owners: dict[str, str] = {}
    for key, definition in shipped.items():
        entry = effective.get(key)
        replaced = {language for language, _text in entry.labels} if entry else set()
        kept = (text for language, text in definition.labels if language not in replaced)
        for spelling in (key, *kept):
            owners[semantic_labels.fold(spelling)] = key
    for key, entry in sorted(effective.items()):
        span = f"{labels.section}.{key}"
        written = [] if entry.core else [(span, key)]
        written += [(f"{span}.labels.{language}", text) for language, text in entry.labels]
        for where, spelling in written:
            if labels.beside_probabilities and (
                semantic_labels.is_probability(spelling.strip())
                or semantic_labels.PERCENT_RE.match(spelling.strip())
            ):
                findings.append(_finding(
                    f"{labels.noun}_label_probability",
                    where,
                    f"{spelling!r} reads as a probability, which the same row takes; name the "
                    f"{labels.entry_words} in words",
                ))
                continue
            folded = semantic_labels.fold(spelling)
            if owners.setdefault(folded, key) != key:
                findings.append(
                    _finding(
                        f"{labels.noun}_label_collision",
                        where,
                        f"{spelling!r} is already the key or label of {owners[folded]!r}",
                    )
                )


def _parse_definitions(
    raw_definitions: dict[Any, Any],
    namespace: str,
    definition_type: type[CategoryDefinition] | type[KindDefinition],
    allowed_fields: frozenset[str],
    findings: list[RegistryFinding],
) -> dict[str, CategoryDefinition] | dict[str, KindDefinition]:
    definitions: dict[str, CategoryDefinition] | dict[str, KindDefinition] = {}
    canonical_sources: dict[str, str] = {}
    for raw_key, value in raw_definitions.items():
        source_key = str(raw_key)
        key = normalize_label(source_key)
        span = f"{namespace}.{source_key}"
        if source_key != key or not _valid_label(key):
            findings.append(
                _finding(
                    "invalid_key",
                    span,
                    "must be a canonical NFKC/casefold semantic label",
                )
            )
        prior_source = canonical_sources.get(key)
        if prior_source is not None and prior_source != source_key:
            findings.append(
                _finding(
                    "canonical_collision",
                    span,
                    f"normalizes to existing canonical key {key!r}",
                )
            )
        canonical_sources[key] = source_key
        if namespace == "kinds" and key in _all_core_kind_labels():
            findings.append(
                _finding(
                    "canonical_collision", span, "collides with a portable built-in kind"
                )
            )
        if not isinstance(value, dict):
            findings.append(_finding("invalid_definition", span, "must be an object"))
            continue
        for unknown in sorted(set(value) - allowed_fields, key=str):
            findings.append(
                _finding("unknown_field", f"{span}.{unknown}", "unknown definition field")
            )
        raw_description = value.get("description")
        if raw_description is None:
            findings.append(
                _finding("missing_description", f"{span}.description", "is required")
            )
            description = ""
        elif not isinstance(raw_description, str):
            findings.append(
                _finding(
                    "invalid_type", f"{span}.description", "must be a string"
                )
            )
            description = ""
        else:
            description = raw_description.strip()
        if isinstance(raw_description, str) and not description:
            findings.append(
                _finding("missing_description", f"{span}.description", "is required")
            )
        raw_status = value.get("status", "active")
        if not isinstance(raw_status, str):
            findings.append(
                _finding("invalid_type", f"{span}.status", "must be a string")
            )
            status = "active"
        else:
            status = raw_status
        if isinstance(raw_status, str) and status not in _STATUSES:
            findings.append(
                _finding(
                    "invalid_status",
                    f"{span}.status",
                    f"must be one of {sorted(_STATUSES)}",
                )
            )
        scope = value.get("scope", {})
        if not isinstance(scope, dict):
            findings.append(
                _finding("invalid_scope", f"{span}.scope", "must be an object")
            )
            scope = {}
        elif set(scope) - {"projects", "page_types"}:
            findings.append(
                _finding(
                    "invalid_scope",
                    f"{span}.scope",
                    "only projects and page_types are allowed",
                )
            )
        aliases = tuple(
            normalize_label(item)
            for item in _strings(value.get("aliases", []), f"{span}.aliases", findings)
        )
        projects = frozenset(
            _strings(scope.get("projects", []), f"{span}.scope.projects", findings)
        )
        page_types = frozenset(
            _strings(scope.get("page_types", []), f"{span}.scope.page_types", findings)
        )
        raw_replaced_by = value.get("replaced_by")
        if raw_replaced_by not in (None, "") and not isinstance(
            raw_replaced_by, str
        ):
            findings.append(
                _finding(
                    "invalid_type", f"{span}.replaced_by", "must be a string or null"
                )
            )
            replaced_by = None
        else:
            replaced_by = _optional(raw_replaced_by)
        common: dict[str, Any] = {
            "key": key,
            "description": description,
            "aliases": aliases,
            "projects": projects,
            "page_types": page_types,
            "status": status,
            "replaced_by": replaced_by,
        }
        if definition_type is KindDefinition:
            common["heading_aliases"] = tuple(
                normalize_label(item)
                for item in _strings(
                    value.get("heading_aliases", []),
                    f"{span}.heading_aliases",
                    findings,
                )
            )
            common["traits"] = _kind_traits(value.get("traits", []), span, findings)
            common["labels"] = _read_labels(value.get("labels", {}), span, findings)
        definitions[key] = definition_type(**common)
    return definitions


def _build_aliases(
    definitions: Mapping[str, CategoryDefinition | KindDefinition],
    namespace: str,
    *,
    core_labels: Mapping[str, str],
    findings: list[RegistryFinding],
) -> dict[str, str]:
    aliases: dict[str, str] = {}
    canonical = set(definitions)
    for key in sorted(definitions):
        for alias in definitions[key].aliases:
            span = f"{namespace}.{key}.aliases"
            if not _valid_label(alias):
                findings.append(_finding("invalid_alias", span, f"invalid alias {alias!r}"))
            if alias in core_labels or alias in canonical:
                findings.append(
                    _finding("alias_collision", span, f"alias {alias!r} collides")
                )
                continue
            prior = aliases.get(alias)
            if prior is not None and prior != key:
                findings.append(
                    _finding(
                        "alias_conflict",
                        span,
                        f"alias {alias!r} maps to both {prior!r} and {key!r}",
                    )
                )
                continue
            aliases[alias] = key
    return aliases


def _build_heading_aliases(
    kinds: Mapping[str, KindDefinition],
    *,
    core_labels: Mapping[str, str],
    kind_aliases: Mapping[str, str],
    findings: list[RegistryFinding],
) -> dict[str, str]:
    aliases: dict[str, str] = {}
    canonical = set(kinds)
    for key in sorted(kinds):
        aliases[key] = key
        for alias in kinds[key].heading_aliases:
            span = f"kinds.{key}.heading_aliases"
            if not _valid_label(alias):
                findings.append(_finding("invalid_alias", span, f"invalid alias {alias!r}"))
            if alias in core_labels or alias in canonical:
                findings.append(
                    _finding("alias_collision", span, f"alias {alias!r} collides")
                )
                continue
            prior = aliases.get(alias) or kind_aliases.get(alias)
            if prior is not None and prior != key:
                findings.append(
                    _finding(
                        "alias_conflict",
                        span,
                        f"alias {alias!r} maps to both {prior!r} and {key!r}",
                    )
                )
                continue
            aliases[alias] = key
    return aliases


def _validate_replacements(
    definitions: Mapping[str, CategoryDefinition | KindDefinition],
    namespace: str,
    findings: list[RegistryFinding],
    *,
    external_active: frozenset[str] = frozenset(),
) -> None:
    for key in sorted(definitions):
        definition = definitions[key]
        span = f"{namespace}.{key}.replaced_by"
        replacement = definition.replaced_by
        if definition.status == "deprecated" and not replacement:
            findings.append(
                _finding(
                    "missing_replacement", span, "deprecated definitions require a replacement"
                )
            )
            continue
        if definition.status != "deprecated" and replacement:
            findings.append(
                _finding(
                    "invalid_replacement",
                    span,
                    "only deprecated definitions may declare a replacement",
                )
            )
        if not replacement:
            continue
        if replacement != normalize_label(replacement):
            findings.append(
                _finding(
                    "invalid_replacement",
                    span,
                    "must name an active canonical key in the same namespace",
                )
            )
            continue
        if replacement in external_active:
            continue
        if replacement not in definitions:
            findings.append(
                _finding(
                    "invalid_replacement",
                    span,
                    "must name an active canonical key in the same namespace",
                )
            )
            continue
        target = definitions[replacement]
        if target.status == "deprecated":
            findings.append(
                _finding("invalid_replacement", span, "replacement must be active")
            )


def _replacement_cycle_findings(
    definitions: Mapping[str, CategoryDefinition | KindDefinition], namespace: str
) -> list[RegistryFinding]:
    findings: list[RegistryFinding] = []
    reported: set[frozenset[str]] = set()
    for start in sorted(definitions):
        order: list[str] = []
        positions: dict[str, int] = {}
        current: str | None = start
        while current in definitions and current not in positions:
            positions[current] = len(order)
            order.append(current)
            current = definitions[current].replaced_by
        if current is None or current not in positions:
            continue
        cycle = order[positions[current] :]
        cycle_key = frozenset(cycle)
        if cycle_key in reported:
            continue
        reported.add(cycle_key)
        findings.append(
            _finding(
                "replacement_cycle",
                f"{namespace}.{start}.replaced_by",
                f"replacement cycle is not allowed: {' -> '.join(cycle + [cycle[0]])}",
            )
        )
    return findings


def _scope_findings(
    namespace: str,
    canonical: str,
    definition: CategoryDefinition | KindDefinition,
    *,
    project: str | None,
    page_type: str | None,
) -> tuple[RegistryFinding, ...]:
    findings: list[RegistryFinding] = []
    for field_name, allowed, actual in (
        ("project", definition.projects, project),
        ("page_type", definition.page_types, page_type),
    ):
        if allowed and actual not in allowed:
            findings.append(
                _finding(
                    "scope_violation",
                    f"{namespace}.{canonical}.{field_name}",
                    f"{actual!r} is outside {sorted(allowed)!r}",
                    severity="warning",
                )
            )
    return tuple(
        sorted(findings, key=lambda item: (item["path"], item["code"], item["detail"]))
    )


def _resolution_status(key: str, canonical: str, status: str) -> str:
    if status == "deprecated":
        return "deprecated"
    return "alias" if key != canonical else "extension"


def _definition_dict(definition: CategoryDefinition | KindDefinition) -> dict[str, Any]:
    out: dict[str, Any] = {
        "key": definition.key,
        "description": definition.description,
        "status": definition.status,
    }
    if definition.aliases:
        out["aliases"] = sorted(definition.aliases)
    if definition.projects:
        out["projects"] = sorted(definition.projects)
    if definition.page_types:
        out["page_types"] = sorted(definition.page_types)
    if definition.replaced_by:
        out["replaced_by"] = definition.replaced_by
    return out


def _definition_proposal(
    definition: CategoryDefinition | KindDefinition,
) -> dict[str, Any]:
    out: dict[str, Any] = {"description": definition.description}
    if definition.aliases:
        out["aliases"] = sorted(definition.aliases)
    if isinstance(definition, KindDefinition) and definition.labels:
        out["labels"] = dict(definition.labels)
    if isinstance(definition, KindDefinition) and definition.heading_aliases:
        out["heading_aliases"] = sorted(definition.heading_aliases)
    if isinstance(definition, KindDefinition) and definition.traits:
        out["traits"] = sorted(definition.traits)
    if definition.status != "active":
        out["status"] = definition.status
    if definition.replaced_by is not None:
        out["replaced_by"] = definition.replaced_by
    scope: dict[str, list[str]] = {}
    if definition.projects:
        scope["projects"] = sorted(definition.projects)
    if definition.page_types:
        scope["page_types"] = sorted(definition.page_types)
    if scope:
        out["scope"] = scope
    return out


def _kind_traits(raw: Any, span: str, findings: list[RegistryFinding]) -> frozenset[str]:
    """The traits a vault kind declares; one outside `KIND_TRAITS` is an error naming the rest."""
    names = _strings(raw, f"{span}.traits", findings)
    unknown = sorted(set(names) - semantic_kinds.KIND_TRAITS)
    if unknown:
        findings.append(
            _finding(
                "unknown_trait",
                f"{span}.traits",
                f"{unknown} are not kind traits; use any of {sorted(semantic_kinds.KIND_TRAITS)}",
            )
        )
    return frozenset(names) - set(unknown)


def _strings(value: Any, path: str, findings: list[RegistryFinding]) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        findings.append(_finding("invalid_list", path, "must be a list of strings"))
        return []
    return list(dict.fromkeys(item.strip() for item in value if item.strip()))


def _optional(value: Any) -> str | None:
    return str(value).strip() if value not in (None, "") else None


def _valid_label(label: str) -> bool:
    if not label or len(label) > 64 or not label[0].isalpha():
        return False
    return all(char.isalpha() or char.isdigit() or char == "_" for char in label)


# The sixteen code-owned core categories and their exact built-in alias table.
# Descriptions are deliberately generic and public-safe; no vault identifiers.
_CORE_CATEGORY_DEFINITIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("decision", "A choice that was made together with its rationale.", ("decisions",)),
    ("fact", "A verified statement taken to be true.", ("facts",)),
    ("finding", "Something learned from investigation or observation.", ("findings",)),
    ("insight", "A non-obvious realization or interpretation.", ("insights",)),
    ("constraint", "A limit or boundary that must be respected.", ("constraints",)),
    ("requirement", "Something that must be satisfied.", ("requirements",)),
    ("assumption", "Something taken as given without proof.", ("assumptions",)),
    ("risk", "A possible future problem and its potential impact.", ("risks",)),
    ("problem", "A described issue that needs resolution.", ("problems",)),
    (
        "question",
        "An open question awaiting an answer.",
        ("questions", "open_question", "open_questions"),
    ),
    ("action", "A task or next step to carry out.", ("actions",)),
    ("technique", "A reusable method or approach.", ("techniques",)),
    ("preference", "A stated inclination or choice of style.", ("preferences",)),
    ("code", "A code snippet or code-level detail.", ()),
    ("design", "A design description, plan, or approach.", ("designs",)),
    (
        "config",
        "A configuration value or setting.",
        ("configs", "configuration", "configurations"),
    ),
)


def _core_label_entries() -> dict[str, dict[str, LabelDefinition]]:
    return {labels.section: semantic_labels.pack(labels) for labels in semantic_labels.REGISTRIES}


@lru_cache(maxsize=1)
def _core_categories() -> dict[str, CategoryDefinition]:
    return {
        key: CategoryDefinition(key=key, description=description, aliases=aliases)
        for key, description, aliases in _CORE_CATEGORY_DEFINITIONS
    }


@lru_cache(maxsize=1)
def _core_category_aliases() -> dict[str, str]:
    aliases: dict[str, str] = {}
    for key, definition in _core_categories().items():
        for alias in definition.aliases:
            aliases[alias] = key
    return aliases


@lru_cache(maxsize=1)
def _all_core_category_labels() -> frozenset[str]:
    return frozenset((*_core_categories(), *_core_category_aliases()))


def _core_category_shadow_findings(
    categories: Mapping[str, CategoryDefinition],
    findings: list[RegistryFinding],
) -> None:
    """Warn (never error) when an extension category shadows a core key/alias.

    The colliding entry is byte-preserved in ``categories`` for serialization but
    core resolution wins, so the extension entry is inert. The advisory warning
    guides migration without invalidating the registry.
    """
    core_labels = _all_core_category_labels()
    for key in sorted(categories):
        definition = categories[key]
        if key in core_labels:
            findings.append(
                _finding(
                    "core_category_shadowed",
                    f"categories.{key}",
                    f"category {key!r} shadows a built-in core category and is ignored",
                    severity="warning",
                )
            )
        for alias in sorted(set(definition.aliases)):
            if alias in core_labels:
                findings.append(
                    _finding(
                        "core_category_shadowed",
                        f"categories.{key}.aliases",
                        f"alias {alias!r} shadows a built-in core category and is ignored",
                        severity="warning",
                    )
                )


@lru_cache(maxsize=1)
def _core_heading_aliases() -> dict[str, str]:
    return {
        normalize_label(alias): canonical
        for alias, canonical in semantic_kinds.heading_aliases().items()
    }


@lru_cache(maxsize=1)
def _all_core_kind_labels() -> frozenset[str]:
    return frozenset((*core_registry().core_kinds, *_core_heading_aliases()))


def _finding(
    code: str, path: str, detail: str, *, severity: str = "error"
) -> RegistryFinding:
    return RegistryFinding(
        code=code,
        path=path,
        span=path,
        severity=severity,
        detail=detail,
    )


def _content_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# The registry adapter (`add-vocabulary-registries`)
#
# The semantic categories are one registry over the `categories` section of
# `_Schema/semantic-language-registry.yaml`; its custom kinds share the file
# and its history but are not a vocabulary registry in this slice.
# --------------------------------------------------------------------------- #


class _CategoryAdapter:
    def parse(self, text: str | None, digest: str) -> SemanticLanguageRegistry:
        return _parse_overlay_text(text, digest)

    def parse_document(self, document: Mapping[str, Any]) -> SemanticLanguageRegistry:
        return load_registry(proposal=dict(document))

    def entries(self, typed: SemanticLanguageRegistry) -> dict[str, vocabulary_registry.Entry]:
        out: dict[str, vocabulary_registry.Entry] = {}
        for origin, definitions in (("pack", typed.core_categories), ("vault", typed.categories)):
            for key, definition in definitions.items():
                scope = {
                    name: sorted(getattr(definition, name))
                    for name in ("projects", "page_types")
                    if getattr(definition, name)
                }
                out[key] = vocabulary_registry.Entry(
                    key=key,
                    description=definition.description,
                    aliases=definition.aliases,
                    status=definition.status,
                    replaced_by=definition.replaced_by,
                    attributes=MappingProxyType({"scope": scope} if scope else {}),
                    origin=origin,
                )
        return out

    def findings(self, typed: SemanticLanguageRegistry) -> tuple[dict[str, str], ...]:
        return tuple(item.as_dict() for item in typed.findings)

    def document(self, text: str | None) -> dict[str, Any]:
        if text is None:
            return empty_proposal()
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise vocabulary_registry.RegistryError(
                "INVALID_REGISTRY_OVERLAY: semantic-language-registry.yaml does not parse; fix "
                f"it or restore a kept version ({exc})"
            ) from exc
        if not isinstance(data, dict):
            raise vocabulary_registry.RegistryError(
                "INVALID_REGISTRY_OVERLAY: semantic-language-registry.yaml is not a mapping; "
                "fix it or restore a kept version"
            )
        data.setdefault("schema_version", SCHEMA_VERSION)
        for section in ("categories", "kinds"):
            value = data.get(section)
            data[section] = dict(value) if isinstance(value, dict) else {}
        return data

    def put(
        self,
        document: dict[str, Any],
        key: str,
        entry: vocabulary_registry.Entry,
        *,
        existing: bool,
    ) -> None:
        if key in _core_categories():
            raise vocabulary_registry.RegistryError(
                f"PACK_ENTRY_FIXED: {key!r} is a core semantic category"
            )
        row: dict[str, Any] = {"description": entry.description}
        if entry.aliases:
            row["aliases"] = sorted(normalize_label(alias) for alias in entry.aliases)
        if entry.status != "active":
            row["status"] = entry.status
        if entry.replaced_by:
            row["replaced_by"] = entry.replaced_by
        scope = entry.attributes.get("scope")
        if scope:
            row["scope"] = {name: sorted(values) for name, values in dict(scope).items()}
        document["categories"][key] = row

    def remove(self, document: dict[str, Any], key: str) -> None:
        document["categories"].pop(key, None)

    def render(self, document: Mapping[str, Any]) -> str:
        return yaml.safe_dump(dict(document), allow_unicode=True, sort_keys=True)

    def normalize_key(self, raw: str) -> str:
        key = normalize_label(raw)
        if not _valid_label(key):
            raise vocabulary_registry.RegistryError(
                f"INVALID_REGISTRY_KEY: {raw!r} is not a semantic category label"
            )
        return key


def _category_usage(vault_root: Path, snapshot: vocabulary_registry.Snapshot) -> Any:
    from .vocabulary import usage

    return usage.categories(vault_root, snapshot)


CATEGORY_SPEC = vocabulary_registry.RegistrySpec(
    name="categories",
    stem="semantic-language-registry",
    overlay=registry_path,
    adapter=_CategoryAdapter(),
    fields=frozenset({"description", "aliases", "status", "replaced_by", "attributes"}),
    attributes=frozenset({"scope"}),
    cap=512,
    usage=_category_usage,
)
