"""Pure, fail-closed classification of staged vocabulary write images.

This is deliberately a byte-image classifier.  It neither reads a corpus nor
writes a file; the mutation terminal supplies the exact images it will later
commit under its content lock.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

import yaml

from . import entity_types, memory_refs, relation_registry, semantic_contract, vault
from .kbdir import kb_dirname
from .semantic_contract import _derive_relation_facts

_SCHEMA_PREFIX = f"{kb_dirname()}/_Schema/"
_ALLOWED_ACTIONS = frozenset({"entity.create", "entity_type.add", "relation_type.add", "edge.add"})
_MAX_IMAGE_BYTES = 1024 * 1024


def _sha256(content: bytes | None) -> str | None:
    return hashlib.sha256(content).hexdigest() if content is not None else None


def _canonical_path(path: str) -> str:
    if not isinstance(path, str) or not path or "\\" in path or "\x00" in path:
        raise ValueError("VOCABULARY_EFFECT_PATH_INVALID")
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise ValueError("VOCABULARY_EFFECT_PATH_INVALID")
    return parsed.as_posix()


@dataclass(frozen=True, slots=True)
class CanonicalWriteImage:
    """One exact planned file transition, normalized to vault-relative POSIX."""

    path: str
    before: bytes | None
    after: bytes | None
    before_sha256: str | None = None
    after_sha256: str | None = None
    role: str | None = None

    def __post_init__(self) -> None:
        canonical = _canonical_path(self.path)
        if self.before is not None and not isinstance(self.before, bytes):
            raise ValueError("VOCABULARY_EFFECT_BYTES_REQUIRED")
        if self.after is not None and not isinstance(self.after, bytes):
            raise ValueError("VOCABULARY_EFFECT_BYTES_REQUIRED")
        before_hash = _sha256(self.before)
        after_hash = _sha256(self.after)
        if self.before_sha256 not in {None, before_hash} or self.after_sha256 not in {None, after_hash}:
            raise ValueError("VOCABULARY_EFFECT_HASH_MISMATCH")
        object.__setattr__(self, "path", canonical)
        object.__setattr__(self, "before_sha256", before_hash)
        object.__setattr__(self, "after_sha256", after_hash)


@dataclass(frozen=True, slots=True)
class Effect:
    action: str
    path: str
    key: str | None = None
    details: Mapping[str, str] = MappingProxyType({})

    def __post_init__(self) -> None:
        if self.action not in _ALLOWED_ACTIONS:
            raise ValueError("VOCABULARY_EFFECT_ACTION_INVALID")
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))

    def as_dict(self) -> dict[str, Any]:
        return {"action": self.action, "path": self.path, "key": self.key, "details": dict(self.details)}


@dataclass(frozen=True, slots=True)
class ClassificationReason:
    code: str
    path: str
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class AdditiveEffectClassification:
    """The reviewable structural delta; this is never a write authorization."""

    state: str
    effects: tuple[Effect, ...]
    reasons: tuple[ClassificationReason, ...]
    digest: str


def _text(image: CanonicalWriteImage, value: bytes | None) -> str | None:
    if value is None:
        return None
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _yaml_image(image: CanonicalWriteImage, value: bytes | None) -> dict[str, Any] | None:
    text = _text(image, value)
    if text is None:
        return None
    try:
        node = yaml.compose(text)
    except yaml.YAMLError:
        return None
    if not _unique_yaml_keys(node):
        return None
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _unique_yaml_keys(node: yaml.Node | None) -> bool:
    if isinstance(node, yaml.MappingNode):
        keys: set[str] = set()
        for key, value in node.value:
            if not isinstance(key, yaml.ScalarNode) or key.value in keys:
                return False
            keys.add(key.value)
            if not _unique_yaml_keys(value):
                return False
    elif isinstance(node, yaml.SequenceNode):
        return all(_unique_yaml_keys(value) for value in node.value)
    return True


def _registry_from_image(
    image: CanonicalWriteImage | None,
    value: bytes | None,
    *,
    kind: str,
    vault_root: Path,
):
    if image is None:
        return (
            entity_types.load_entity_types(vault_root)
            if kind == "entity"
            else relation_registry.load_registry(vault_root)
        )
    if value is None:
        return entity_types.core_registry() if kind == "entity" else relation_registry.core_registry()
    data = _yaml_image(image, value)
    if data is None:
        return None
    return entity_types.load_entity_types(proposal=data) if kind == "entity" else relation_registry.load_registry(proposal=data)


def _registry_effects(
    before: Any,
    after: Any,
    *,
    action: str,
    path: str,
    reasons: list[ClassificationReason],
) -> list[Effect]:
    if before is None or after is None or before.findings or after.findings:
        reasons.append(ClassificationReason("registry_invalid", path))
        return []
    before_defs = before.extensions
    after_defs = after.extensions
    changed = sorted(key for key in before_defs if key not in after_defs or before_defs[key] != after_defs[key])
    if changed:
        reasons.extend(ClassificationReason(f"{action.removesuffix('.add')}_definition_changed", path, key) for key in changed)
    return [Effect(action, path, key=key) for key in sorted(set(after_defs) - set(before_defs))]


def _entity_path_is_canonical(path: str, definition: entity_types.EntityTypeDefinition) -> bool:
    parsed = PurePosixPath(path)
    return (
        parsed.parent.as_posix() == f"{kb_dirname()}/Entities/{definition.folder}"
        and parsed.suffix == ".md"
        and bool(parsed.stem)
    )


def _state_for(
    root: Path,
    image: CanonicalWriteImage,
    source: bytes,
    registry: relation_registry.RelationRegistry,
) -> semantic_contract.SemanticPageState | None:
    text = _text(image, source)
    if text is None:
        return None
    try:
        return semantic_contract.build_page_state(
            root,
            image.path,
            text,
            relation_registry=registry,
            complete_authored_effects=True,
        )
    except (TypeError, ValueError, yaml.YAMLError):
        return None


def _fact_key(fact: semantic_contract.RelationFact) -> tuple[str, str, str, str, str, str]:
    return (
        fact.authored_path,
        fact.logical_source_path,
        fact.logical_target_path,
        str(fact.canonical_relation),
        fact.origin,
        str(fact.authored_anchor),
    )


def classify_additive_effects(
    vault_root: Path,
    images: Iterable[CanonicalWriteImage],
    *,
    resolver_entries: Iterable[tuple[str, str]] = (),
    identity_reader: Callable[[str], str | None] | None = None,
    identity_paths_for_id: Callable[[str], Iterable[str] | None] | None = None,
    derived_roles: Mapping[str, str] = MappingProxyType({}),
) -> AdditiveEffectClassification:
    """Classify a complete staged batch without touching mutable vault content.

    ``resolver_entries`` and ``identity_reader`` are authoritative point-index
    snapshots supplied by the caller.  This function will not cold-build either
    index: unresolved endpoints are retained as unavailable exact work.
    """
    root = Path(vault_root)
    received = tuple(images)
    active = tuple(image for image in received if image.before != image.after)
    reasons: list[ClassificationReason] = []
    indexed: dict[str, CanonicalWriteImage] = {}
    for image in active:
        if not isinstance(image, CanonicalWriteImage):
            raise ValueError("VOCABULARY_EFFECT_IMAGE_INVALID")
        if max(len(image.before or b""), len(image.after or b"")) > _MAX_IMAGE_BYTES:
            reasons.append(ClassificationReason("image_too_large", image.path))
        if image.path in indexed:
            reasons.append(ClassificationReason("duplicate_image_path", image.path))
        indexed[image.path] = image
    derived_paths = set(derived_roles)
    for path, role in derived_roles.items():
        image = indexed.get(path)
        if image is None or image.role != role:
            reasons.append(ClassificationReason("derived_auxiliary_invalid", path))
    active = tuple(image for image in active if image.path not in derived_paths)
    entity_registry_path = entity_types.extension_registry_path(root).relative_to(root).as_posix()
    relation_registry_path = relation_registry.extension_registry_path(root).relative_to(root).as_posix()
    project_keys_path = (root / kb_dirname() / "_Schema" / "project-keys.yaml").relative_to(root).as_posix()
    schema_paths = {entity_registry_path, relation_registry_path, project_keys_path}
    for image in active:
        if image.path.startswith(_SCHEMA_PREFIX) and image.path not in schema_paths:
            reasons.append(ClassificationReason("unknown_schema_change", image.path))
    if project_keys_path in indexed:
        reasons.append(ClassificationReason("project_keys_unsupported", project_keys_path))
    if reasons:
        return _result("unsupported", (), reasons, received)

    entity_image = indexed.get(entity_registry_path)
    relation_image = indexed.get(relation_registry_path)
    before_entities = _registry_from_image(entity_image, entity_image.before if entity_image else None, kind="entity", vault_root=root)
    after_entities = _registry_from_image(entity_image, entity_image.after if entity_image else None, kind="entity", vault_root=root)
    before_relations = _registry_from_image(relation_image, relation_image.before if relation_image else None, kind="relation", vault_root=root)
    after_relations = _registry_from_image(relation_image, relation_image.after if relation_image else None, kind="relation", vault_root=root)

    effects: list[Effect] = []
    if entity_image is not None:
        effects.extend(_registry_effects(before_entities, after_entities, action="entity_type.add", path=entity_image.path, reasons=reasons))
    if relation_image is not None:
        effects.extend(_registry_effects(before_relations, after_relations, action="relation_type.add", path=relation_image.path, reasons=reasons))
    if any(reason.code == "registry_invalid" for reason in reasons):
        return _result("unsupported", (), reasons, received)
    assert after_entities is not None and after_relations is not None

    before_states: dict[str, semantic_contract.SemanticPageState] = {}
    after_states: dict[str, semantic_contract.SemanticPageState] = {}
    ordinary: list[CanonicalWriteImage] = []
    for image in active:
        if image.path in schema_paths:
            continue
        if not image.path.endswith(".md"):
            if image.before != image.after:
                reasons.append(ClassificationReason("unsupported_file_change", image.path))
            continue
        ordinary.append(image)
        if image.before is not None:
            state = _state_for(root, image, image.before, before_relations)
            if state is None:
                reasons.append(ClassificationReason("markdown_parse_invalid", image.path))
            else:
                before_states[image.path] = state
        if image.after is not None:
            state = _state_for(root, image, image.after, after_relations)
            if state is None:
                reasons.append(ClassificationReason("markdown_parse_invalid", image.path))
            else:
                after_states[image.path] = state
    if any(reason.code == "registry_invalid" for reason in reasons):
        return _result("unsupported", (), reasons, received)

    ids: dict[str, list[str]] = {}
    for path, state in after_states.items():
        identifier = memory_refs.normalize_id(state.frontmatter.get(memory_refs.ID_FIELD))
        if identifier is not None:
            ids.setdefault(identifier, []).append(path)
    for identifier, paths in ids.items():
        if len(paths) > 1:
            reasons.append(ClassificationReason("duplicate_batch_identity", paths[0], identifier))
    if any(
        reason.code
        in {"duplicate_batch_identity", "markdown_parse_invalid", "unsupported_file_change"}
        for reason in reasons
    ):
        return _result("unsupported", (), reasons, received)

    for image in ordinary:
        old = before_states.get(image.path)
        new = after_states.get(image.path)
        old_is_entity = old is not None and old.page_type == "entity"
        new_is_entity = new is not None and new.page_type == "entity"
        if old_is_entity and image.before != image.after:
            reasons.append(ClassificationReason("existing_entity_changed", image.path))
            continue
        if old is not None and not old_is_entity and new_is_entity:
            reasons.append(ClassificationReason("existing_page_converted_to_entity", image.path))
            continue
        if new_is_entity and old is None:
            frontmatter = new.frontmatter
            title = frontmatter.get("title")
            entity_type = frontmatter.get("entity_type")
            identifier = memory_refs.normalize_id(frontmatter.get(memory_refs.ID_FIELD))
            definition = after_entities.resolve(str(entity_type or ""))
            if not isinstance(title, str) or identifier is None or definition is None or new.status != "active":
                reasons.append(ClassificationReason("new_entity_invalid", image.path))
            elif not _entity_path_is_canonical(image.path, definition):
                reasons.append(ClassificationReason("new_entity_noncanonical_path", image.path))
            else:
                if identity_paths_for_id is None:
                    reasons.append(ClassificationReason("identity_uniqueness_unavailable", image.path))
                    continue
                try:
                    existing_paths = identity_paths_for_id(identifier)
                except Exception:  # noqa: BLE001 - point index availability is an input contract
                    existing_paths = None
                if existing_paths is None:
                    reasons.append(ClassificationReason("identity_uniqueness_unavailable", image.path))
                    continue
                if any(path != image.path for path in existing_paths):
                    reasons.append(ClassificationReason("identity_already_owned", image.path, identifier))
                    continue
                effects.append(Effect("entity.create", image.path, key=memory_refs.memory_ref(identifier)))
        elif old is None and image.after is None:
            reasons.append(ClassificationReason("delete_without_before", image.path))
    if any(reason.code == "identity_uniqueness_unavailable" for reason in reasons):
        return _result("unavailable", (), reasons, received)

    entries: dict[str, str] = {}
    ambiguous_entries: set[str] = set()
    for path, title in resolver_entries:
        previous_title = entries.get(path)
        if previous_title is not None and previous_title != title:
            ambiguous_entries.add(path)
        entries[path] = title
    for state in after_states.values():
        entries[state.path] = state.title
        ambiguous_entries.discard(state.path)
    if ambiguous_entries:
        return _result("unavailable", (), [ClassificationReason("resolver_snapshot_ambiguous", path) for path in sorted(ambiguous_entries)], received)
    resolver = vault.WikilinkResolver.from_entries(root, entries.items())
    before_facts = _derive_relation_facts(
        root,
        before_states,
        resolver,
        before_relations,
        target_states=before_states,
        complete_authored_effects=True,
    )
    after_facts = _derive_relation_facts(
        root,
        after_states,
        resolver,
        after_relations,
        target_states=after_states,
        complete_authored_effects=True,
    )
    previous = {_fact_key(fact) for fact in before_facts}
    current = {_fact_key(fact): fact for fact in after_facts}
    removed = previous - set(current)
    if removed:
        reasons.extend(
            ClassificationReason("edge_removed_or_changed", path)
            for path, *_rest in sorted(removed)
        )

    def ref_for(path: str) -> str | None:
        state = after_states.get(path)
        if state is not None:
            identifier = memory_refs.normalize_id(state.frontmatter.get(memory_refs.ID_FIELD))
            if identifier is not None:
                return memory_refs.memory_ref(identifier)
        return identity_reader(path) if identity_reader is not None else None

    unavailable: list[ClassificationReason] = []
    for key, fact in sorted(current.items()):
        if key in previous:
            continue
        if fact.canonical_relation is None or fact.registry_status in {"unregistered", "deprecated", "scope_violation"}:
            unavailable.append(ClassificationReason("edge_not_currently_registered", fact.authored_path))
            continue
        source = ref_for(fact.logical_source_path)
        target = ref_for(fact.logical_target_path)
        if source is None or target is None or fact.target_status != "resolved":
            unavailable.append(ClassificationReason("edge_endpoint_unavailable", fact.authored_path))
            continue
        effects.append(Effect("edge.add", fact.authored_path, key=fact.identity, details={"source": source, "target": target, "relation": fact.canonical_relation}))
    if unavailable:
        return _result("unavailable", (), unavailable, received)
    if reasons:
        return _result("mixed" if effects else "non_additive", effects, reasons, received)
    return _result("reviewed", effects, (), received)


def _result(
    state: str,
    effects: Iterable[Effect],
    reasons: Iterable[ClassificationReason],
    images: Iterable[CanonicalWriteImage],
) -> AdditiveEffectClassification:
    ordered_effects = tuple(sorted(effects, key=lambda item: (item.action, item.path, item.key or "")))
    ordered_reasons = tuple(sorted(reasons, key=lambda item: (item.code, item.path, item.detail)))
    payload = {
        "state": state,
        "images": [
            {"path": item.path, "before_sha256": item.before_sha256, "after_sha256": item.after_sha256, "role": item.role}
            for item in sorted(images, key=lambda item: item.path)
        ],
        "effects": [item.as_dict() for item in ordered_effects],
        "reasons": [item.as_dict() for item in ordered_reasons],
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
    return AdditiveEffectClassification(state, ordered_effects, ordered_reasons, digest)
