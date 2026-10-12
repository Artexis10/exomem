"""Borrow current indexes and point-read authority evidence without rebuilding."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from . import (
    epistemic_graph,
    find,
    freshness,
    memory_refs,
    relation_registry,
    semantic_contract,
    vault,
    vocabulary_effects,
)
from .kbdir import kb_dirname
from .vocabulary import instances
from .vocabulary_authority import AuthorityScope, VocabularyAuthorityUnavailable
from .vocabulary_workflow import _hash


@dataclass
class EvidenceBinding:
    root: Path
    classification: Any = None
    registry_digests: dict[str, str] = field(default_factory=dict)
    target_digests: dict[str, str] = field(default_factory=dict)
    scope_proofs: tuple[AuthorityScope, ...] = ()
    guards: list[Any] = field(default_factory=list)
    connections: list[Any] = field(default_factory=list)
    checkpoint: Any = None
    transitions: dict[tuple[str, str], vocabulary_effects.RegistryTransition] = field(default_factory=dict)
    selection_policy: str | None = None
    status_dependencies: set[tuple[str, str]] = field(default_factory=set)
    type_dependencies: set[tuple[str, str]] = field(default_factory=set)

    def recheck(self, *, phase: Literal["before", "after"] = "before") -> None:
        from . import lifecycle_statuses, note_types
        from .governance import policy

        for transition in self.transitions.values():
            transition.recheck(self.root, after=phase == "after")
        if self.selection_policy is not None and policy.load(self.root).fingerprint != self.selection_policy:
            raise VocabularyAuthorityUnavailable("instance selection policy changed during commit")
        if (not all(lifecycle_statuses.Basis(self.root).matches(dependency) for dependency in self.status_dependencies)
                or not all(note_types.Basis(self.root).matches(dependency) for dependency in self.type_dependencies)):
            raise VocabularyAuthorityUnavailable("page classification definitions changed during commit")
        for guard in self.guards:
            guard.recheck(self.root)
        if self.checkpoint is not None and freshness.live_recall_checkpoint(
            self.root, "vault"
        ) != self.checkpoint:
            raise VocabularyAuthorityUnavailable("authority evidence changed during commit")

    def close(self) -> None:
        for connection in self.connections:
            connection.close()
        self.connections.clear()


def classify(
    root: Path,
    images: tuple[vocabulary_effects.CanonicalWriteImage, ...],
    *,
    derived_roles: dict[str, str] | None = None,
) -> EvidenceBinding:
    binding = EvidenceBinding(root)
    indexed = {image.path: image for image in images}
    pages: dict[str, semantic_contract.SemanticPageState] = {}
    detached: dict[tuple[str, bool], semantic_contract.SemanticPageState] = {}

    def state_for(path: str, content: str, *, after: bool = False):
        return vocabulary_effects.staged_page_state(
            root, path, content, after=after, images=indexed, transitions=binding.transitions,
        )

    def bind_dependencies():
        from . import lifecycle_statuses, note_types, registry_history

        for state in (*pages.values(), *detached.values()):
            if state.status_dependency is not None:
                binding.status_dependencies.add(state.status_dependency)
            if state.type_basis is not None:
                binding.type_dependencies.add(state.type_basis.dependency)
        for kind, value in binding.status_dependencies:
            if kind == "instances:v1":
                for instance, _revision, _digest in json.loads(value):
                    spec = instances.select(root, lifecycle_statuses.SPEC, instance)
                    vocabulary_effects.capture_transition(root, spec, indexed, binding.transitions)
        if any(dependency[0] == "effective" for dependency in binding.type_dependencies):
            spec = instances.select(root, note_types.SPEC)
            vocabulary_effects.capture_transition(root, spec, indexed, binding.transitions)
        if binding.transitions:
            fingerprints = {transition.admission.policy_fingerprint for transition in binding.transitions.values()}
            if len(fingerprints) != 1:
                raise VocabularyAuthorityUnavailable("instance selection policy changed during classification")
            binding.selection_policy = next(iter(fingerprints))
            binding.registry_digests["selection_policy"] = binding.selection_policy
        for key, transition in binding.transitions.items():
            spec = transition.spec
            binding.registry_digests[json.dumps(key, separators=(",", ":"))] = _hash({
                "overlay": spec.overlay(root).relative_to(root).as_posix(),
                "history": registry_history.history_dir(root, spec.stem).relative_to(root).as_posix(),
                "binding_revision": spec.binding_revision,
                "content_hash": transition.before.content_hash,
                "effective_digest": transition.before.effective_digest,
            })
        binding.registry_digests["lifecycle_dependencies"] = _hash(sorted(binding.status_dependencies))
        binding.registry_digests["note_type_dependencies"] = _hash(sorted(binding.type_dependencies))

    def point(path: str):
        if path in pages:
            return pages[path]
        image = indexed.get(path)
        if image is not None and image.before is not None:
            content = image.before.decode("utf-8")
        else:
            content, guard = vault.read_guarded_text(root, root / path)
            if path not in indexed:
                binding.guards.append(guard)
        state = state_for(path, content)
        binding.target_digests[path] = state.source_hash
        pages[path] = state
        return state

    try:
        # A sealed derived auxiliary (a registry save's `log.md` entry) carries
        # code-owned bytes and no authored effect, so it needs no identity
        # evidence; classification excludes it the same way.
        markdown = [
            image
            for image in images
            if image.path.endswith(".md") and image.path not in (derived_roles or {})
        ]
        if not markdown:
            binding.classification = vocabulary_effects.classify_additive_effects(
                root, images, derived_roles=derived_roles or {}, registry_transitions=binding.transitions,
            )
            bind_dependencies()
            binding.recheck(phase="before")
            return binding

        binding.checkpoint = freshness.live_recall_checkpoint(root, "vault")
        if binding.checkpoint is None:
            raise VocabularyAuthorityUnavailable("current identity projection is warming")
        graph = epistemic_graph.EpistemicGraphIndex(root)._open_read_snapshot()
        references = memory_refs.ReferenceIndex(root)._current_readonly_connection()
        if graph is None or references is None:
            if graph is not None:
                graph.close()
            if references is not None:
                references.close()
            raise VocabularyAuthorityUnavailable("current identity projection is unavailable")
        binding.connections.extend((graph, references))
        references.execute("BEGIN")
        pending = memory_refs._pending_reference_projection(root)
        if pending is not None and not pending.empty:
            raise VocabularyAuthorityUnavailable("identity publication is warming")
        cached = find.recall_resolver_snapshot_at_checkpoint(root, binding.checkpoint)
        if cached is None:
            raise VocabularyAuthorityUnavailable("current endpoint resolver is warming")
        registry = relation_registry.core_registry()
        for image in markdown:
            for ordinal, raw in enumerate((image.before, image.after)):
                if raw is None:
                    continue
                detached[(image.path, bool(ordinal))] = state_for(
                    image.path, raw.decode("utf-8"), after=bool(ordinal),
                )
        empty = vault.WikilinkResolver.from_entries(root, ())
        entries: dict[str, str] = {}
        for state in detached.values():
            facts = semantic_contract._derive_relation_facts(
                root, {state.path: state}, empty, registry, complete_authored_effects=True,
            )
            for fact in facts:
                target = semantic_contract._target_parts(fact.raw_target)[0]
                identity = memory_refs.parse_memory_ref(target)
                if identity is not None:
                    rows = references.execute(
                        "SELECT path FROM identities WHERE exomem_id=? AND status='valid' LIMIT 2",
                        (identity,),
                    ).fetchall()
                    if len(rows) != 1:
                        continue
                    target_path = rows[0][0]
                else:
                    status, target_path, _, _ = semantic_contract._resolve_target(root, target, cached)
                    if status != "resolved" or target_path is None:
                        continue
                    target_path = target_path.split("#", 1)[0]
                if target_path in indexed and indexed[target_path].before is None:
                    continue
                target_state = point(target_path)
                entries[target_path] = target_state.title

        def paths_for_identity(identifier: str):
            return tuple(row[0] for row in references.execute(
                "SELECT path FROM identities WHERE exomem_id=? AND status='valid' LIMIT 2",
                (identifier,),
            ).fetchall())

        def identity_for_path(path: str):
            state = point(path)
            identifier = memory_refs.normalize_id(state.frontmatter.get("exomem_id"))
            if identifier is None or paths_for_identity(identifier) != (path,):
                return None
            return memory_refs.memory_ref(identifier)

        binding.classification = vocabulary_effects.classify_additive_effects(
            root, images, resolver_entries=entries.items(), identity_reader=identity_for_path,
            identity_paths_for_id=paths_for_identity, derived_roles=derived_roles or {},
            registry_transitions=binding.transitions, page_states=detached,
        )
        # A project scope is proven only by the two existing canonical endpoints
        # and the actual registered project key, never proposed entity metadata.
        project_path = f"{kb_dirname()}/_Schema/project-keys.yaml"
        try:
            project_text, guard = vault.read_guarded_text(root, root / project_path)
            project_data = yaml.safe_load(project_text)
            projects = project_data.get("projects", {}) if isinstance(project_data, dict) else {}
            if not isinstance(projects, dict):
                projects = {}
            if project_path not in indexed:
                binding.guards.append(guard)
        except (FileNotFoundError, yaml.YAMLError):
            projects = {}
            project_text = ""
        proofs = []
        for effect in binding.classification.effects:
            if effect.action != "edge.add":
                continue
            endpoints = [effect.details["source"], effect.details["target"]]
            endpoint_states = []
            for reference in endpoints:
                identifier = memory_refs.parse_memory_ref(reference)
                paths = paths_for_identity(identifier) if identifier is not None else ()
                if len(paths) != 1:
                    break
                endpoint_states.append(point(paths[0]))
            if len(endpoint_states) != 2:
                continue
            resulting_states = []
            for page in endpoint_states:
                staged_image = indexed.get(page.path)
                if staged_image is not None:
                    if staged_image.after is None:
                        break
                    resulting_states.append(detached[(page.path, True)])
                else:
                    resulting_states.append(page)
            if len(resulting_states) != 2:
                continue
            common = set(projects)
            for page in (*endpoint_states, *resulting_states):
                common.intersection_update(page.projects)
            for project in sorted(common):
                proofs.append(AuthorityScope.project_edge(
                    project_ref=project, source_ref=endpoints[0], target_ref=endpoints[1],
                    membership_digest=_hash({
                        "project_registry": project_text,
                        "endpoints": [(page.path, page.source_hash) for page in endpoint_states],
                        "resulting_endpoints": [
                            (page.path, page.source_hash) for page in resulting_states
                        ],
                    }),
                ))
        binding.scope_proofs = tuple(proofs)
        bind_dependencies()
        binding.recheck(phase="before")
        return binding
    except BaseException:
        binding.close()
        raise
