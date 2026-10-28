"""Classify staged vocabulary writes against their selected definitions.

The mutation terminal supplies the exact images it will commit under its
content lock. Operation-local snapshots interpret the complete batch before
and after publication; classification never writes a file.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

import yaml

from . import entity_types, memory_refs, relation_registry, semantic_contract, vault
from .kbdir import kb_dirname
from .semantic_contract import _derive_relation_facts
from .vocabulary import contract, instances
from .vocabulary import registry as vocabulary_registry

_SCHEMA_PREFIX = f"{kb_dirname()}/_Schema/"
_ALLOWED_ACTIONS = frozenset({"entity.create", "entity_type.add", "relation_type.add", "edge.add"})
MAX_IMAGE_BYTES = 1024 * 1024


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
    details: Mapping[str, str] = field(default_factory=dict)

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


def image_format_refusals(*, is_markdown: bool, size: int | None, ordinary: bool = True) -> tuple[str, ...]:
    """The closed structural-image format checks shared with transport admission."""
    reasons = []
    if size is not None and size > MAX_IMAGE_BYTES:
        reasons.append("image_too_large")
    if ordinary and not is_markdown:
        reasons.append("unsupported_file_change")
    return tuple(reasons)


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


@dataclass(frozen=True)
class RegistryTransition:
    """One selected registry's admitted snapshots for this complete operation."""

    spec: vocabulary_registry.RegistrySpec
    before: vocabulary_registry.Snapshot
    after: vocabulary_registry.Snapshot
    admission: contract.RegistryAdmission
    guard: Any = None
    image: CanonicalWriteImage | None = None

    def recheck(self, root: Path, *, after: bool = False) -> None:
        self.admission.recheck()
        if self.image is not None:
            if not after:
                instances.recheck(root, self.spec)
                current = vocabulary_registry.load(self.spec, root)
                if (current.content_hash, current.effective_digest) != (self.before.content_hash, self.before.effective_digest):
                    raise vocabulary_registry.RegistryError("REGISTRY_UNAVAILABLE: operation definitions changed")
            path = self.spec.overlay(root).relative_to(root).as_posix()
            expected = self.after if after else self.before
            raw_image = self.image.after if after else self.image.before
            raw_hash = self.image.after_sha256 if after else self.image.before_sha256
            if raw_image is None:
                current_guard = vault.PathGuard.capture(root, path, leaf_policy="absent")
                text = None
            else:
                raw, current_guard = vault.read_bounded_guarded_bytes(
                    root, path, limit=vocabulary_registry.MAX_OVERLAY_BYTES,
                    expected_hash=raw_hash,
                )
                text = raw.decode("utf-8")
            current = vocabulary_registry._build(self.spec, text)
            if (current.content_hash, current.effective_digest) != (expected.content_hash, expected.effective_digest):
                raise vocabulary_registry.RegistryError("REGISTRY_UNAVAILABLE: operation definitions changed")
            current_guard.recheck(root)
            # The batch writer also checks its installed inode and ancestor guards.
            return
        instances.recheck(root, self.spec)
        if self.guard is not None:
            self.guard.recheck(root)
        expected = self.after if after else self.before
        current = vocabulary_registry.load(self.spec, root)
        if (current.content_hash, current.effective_digest) != (expected.content_hash, expected.effective_digest):
            raise vocabulary_registry.RegistryError("REGISTRY_UNAVAILABLE: operation definitions changed")


def capture_transition(
    root: Path,
    spec: vocabulary_registry.RegistrySpec,
    images: Mapping[str, CanonicalWriteImage],
    transitions: dict[tuple[str, str], RegistryTransition],
) -> RegistryTransition:
    """Capture only a consumed selection; staged bytes never substitute for admission."""
    key = (spec.name, spec.instance_id)
    if key in transitions:
        return transitions[key]
    path = spec.overlay(root).relative_to(root).as_posix()
    image = images.get(path)
    guard = None
    if image is None:
        instances.recheck(root, spec)
        try:
            text, guard = vault.read_guarded_text(root, root / path)
        except FileNotFoundError:
            text = None
            guard = vault.PathGuard.capture(root, path, leaf_policy="absent")
        before = after = vocabulary_registry._build(spec, text)
        admission = contract.capture_admission(root, spec, text.encode("utf-8") if text is not None else None)
    else:
        admission = contract.capture_admission(root, spec, image.before)
        snapshots = []
        for raw in (image.before, image.after):
            if raw is not None and _yaml_image(image, raw) is None:
                raise vocabulary_registry.RegistryError("INVALID_REGISTRY_OVERLAY: staged registry is malformed")
            snapshots.append(vocabulary_registry._build(spec, raw.decode("utf-8") if raw is not None else None))
        before, after = snapshots
    if before.findings or after.findings:
        raise vocabulary_registry.RegistryError("INVALID_REGISTRY_OVERLAY: operation definitions are invalid")
    transition = RegistryTransition(spec, before, after, admission, guard, image)
    transitions[key] = transition
    return transition


def staged_page_state(
    root: Path, path: str, source: str, *, after: bool,
    images: Mapping[str, CanonicalWriteImage],
    transitions: dict[tuple[str, str], RegistryTransition],
) -> semantic_contract.SemanticPageState:
    """Enrich the maintained detached parser with this image's selected snapshots."""
    from . import lifecycle_statuses
    from .vocabulary import registry_spec

    state = semantic_contract._parse_page_state(root, path, source, complete_authored_effects=True)
    scope = instances.page_scope(root, state.path, dict(state.frontmatter))
    snapshots = {}
    binding = None
    for subject in semantic_contract._PAGE_DEFINITION_SUBJECTS:
        spec = instances.select(root, registry_spec(subject), scope)
        transition = capture_transition(root, spec, images, transitions)
        snapshots[subject] = transition.after if after else transition.before
        binding = spec.binding_revision
    definitions = instances.PageDefinitions(
        scope or instances.PUBLIC_INSTANCE, binding, snapshots, state.path, state.frontmatter,
    )
    return semantic_contract.enrich_page_state(
        root, state, lifecycle_statuses.Basis(root),
        page_definitions=definitions,
    )


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
    *,
    after: bool,
    images: Mapping[str, CanonicalWriteImage],
    transitions: dict[tuple[str, str], RegistryTransition],
) -> semantic_contract.SemanticPageState | None:
    text = _text(image, source)
    if text is None:
        return None
    try:
        return staged_page_state(
            root, image.path, text, after=after, images=images, transitions=transitions,
        )
    except (TypeError, ValueError, yaml.YAMLError):
        return None


def _fact_key(fact: semantic_contract.RelationFact) -> tuple[str, ...]:
    return (
        fact.authored_path,
        fact.logical_source_path,
        fact.logical_target_path,
        str(fact.canonical_relation),
        fact.origin,
        str(fact.authored_anchor),
        str(fact.registry_instance),
    )


def classify_additive_effects(
    vault_root: Path,
    images: Iterable[CanonicalWriteImage],
    *,
    resolver_entries: Iterable[tuple[str, str]] = (),
    identity_reader: Callable[[str], str | None] | None = None,
    identity_paths_for_id: Callable[[str], Iterable[str] | None] | None = None,
    derived_roles: Mapping[str, str] = MappingProxyType({}),
    registry_transitions: dict[tuple[str, str], RegistryTransition] | None = None,
    page_states: dict[tuple[str, bool], semantic_contract.SemanticPageState] | None = None,
) -> AdditiveEffectClassification:
    """Classify a complete staged batch using operation-local selected snapshots.

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
        reasons.extend(ClassificationReason(code, image.path) for code in image_format_refusals(
            is_markdown=image.path.endswith(".md"), size=max(len(image.before or b""), len(image.after or b"")),
            ordinary=False,
        ))
        if image.path in indexed:
            reasons.append(ClassificationReason("duplicate_image_path", image.path))
        indexed[image.path] = image
    derived_paths = set(derived_roles)
    for path, role in derived_roles.items():
        image = indexed.get(path)
        if image is None or image.role != role:
            reasons.append(ClassificationReason("derived_auxiliary_invalid", path))
    active = tuple(image for image in active if image.path not in derived_paths)
    transitions = registry_transitions if registry_transitions is not None else {}
    states = page_states if page_states is not None else {}
    project_keys_path = (root / kb_dirname() / "_Schema" / "project-keys.yaml").relative_to(root).as_posix()
    schema_paths = {project_keys_path}
    registry_images = []
    for image in active:
        try:
            # These two adapters implement the additive type actions; other subjects retain their own authority.
            for base, action in ((entity_types.SPEC, "entity_type.add"), (relation_registry.SPEC, "relation_type.add")):
                spec = instances.match_overlay(root, base, image.path, authoring=True)
                if spec is not None:
                    schema_paths.add(image.path)
                    transition = capture_transition(root, spec, indexed, transitions)
                    registry_images.append((image, transition, action))
                    break
        except (ValueError, OSError):
            reasons.append(ClassificationReason("registry_invalid", image.path))
    for image in active:
        if image.path.startswith(_SCHEMA_PREFIX) and image.path not in schema_paths:
            reasons.append(ClassificationReason("unknown_schema_change", image.path))
    if project_keys_path in indexed:
        reasons.append(ClassificationReason("project_keys_unsupported", project_keys_path))
    if reasons:
        return _result("unsupported", (), reasons, received)

    effects: list[Effect] = []
    for image, transition, action in registry_images:
        effects.extend(_registry_effects(transition.before.typed, transition.after.typed, action=action, path=image.path, reasons=reasons))
    if any(reason.code == "registry_invalid" for reason in reasons):
        return _result("unsupported", (), reasons, received)

    before_states: dict[str, semantic_contract.SemanticPageState] = {}
    after_states: dict[str, semantic_contract.SemanticPageState] = {}
    ordinary: list[CanonicalWriteImage] = []
    for image in active:
        if image.path in schema_paths:
            continue
        if not image.path.endswith(".md"):
            if image.before != image.after:
                reasons.extend(ClassificationReason(code, image.path) for code in image_format_refusals(
                    is_markdown=False, size=None,
                ))
            continue
        ordinary.append(image)
        if image.before is not None:
            state = states.get((image.path, False)) or _state_for(
                root, image, image.before, after=False, images=indexed, transitions=transitions,
            )
            if state is None:
                reasons.append(ClassificationReason("markdown_parse_invalid", image.path))
            else:
                before_states[image.path] = state
        if image.after is not None:
            state = states.get((image.path, True)) or _state_for(
                root, image, image.after, after=True, images=indexed, transitions=transitions,
            )
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
            assert new is not None and new.definitions is not None
            frontmatter = new.frontmatter
            title = frontmatter.get("title")
            entity_type = frontmatter.get("entity_type")
            identifier = memory_refs.normalize_id(frontmatter.get(memory_refs.ID_FIELD))
            try:
                # Folder validation consumes entity types; page parsing needs only relations and categories.
                spec = instances.select(root, entity_types.SPEC, new.definitions.instance_id)
                entities = capture_transition(root, spec, indexed, transitions).after.typed
            except (ValueError, OSError):
                reasons.append(ClassificationReason("registry_invalid", image.path))
                continue
            definition = entities.resolve(str(entity_type or ""))
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
        relation_registry.core_registry(),
        target_states=before_states,
        complete_authored_effects=True,
    )
    after_facts = _derive_relation_facts(
        root,
        after_states,
        resolver,
        relation_registry.core_registry(),
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
