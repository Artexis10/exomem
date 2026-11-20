"""Exact Source dependencies for file-backed record effects and their retained history."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import memory_refs, provenance, source_custody, vault

# These bound a persisted evidence format, not a content vocabulary.
EVENT_VERSION = 3
HISTORY_VERSION = 2
MAX_BINDINGS = 512
MAX_PAYLOAD_REFERENCES = MAX_BINDINGS // 2
PRODUCER = source_custody.OwnerLocator(owner="file_records", locator="record-history", version="2")
# The strict event schema fixes these fields; Source identities remain producer data.
METADATA_FIELDS = frozenset({"dependency_operation_id", "dependency_complete", "dependency_bindings"})
# The persistence protocol distinguishes a new primary from its retained before-image.
PAYLOADS = ("current", "prior")


def valid_metadata(value: Mapping[str, Any]) -> bool:
    operation = value.get("dependency_operation_id")
    bindings = value.get("dependency_bindings")
    if (not isinstance(operation, str) or not 0 < len(operation) <= 1024
            or type(value.get("dependency_complete")) is not bool
            or not isinstance(bindings, list) or len(bindings) > MAX_BINDINGS):
        return False
    seen = set()
    for binding in bindings:
        if not isinstance(binding, dict) or set(binding) != {"source_ref", "epoch_id", "payload"}:
            return False
        reference, epoch, payload = binding["source_ref"], binding["epoch_id"], binding["payload"]
        identity = memory_refs.parse_memory_ref(reference) if isinstance(reference, str) else None
        if (identity is None or memory_refs.memory_ref(identity) != reference or payload not in PAYLOADS
                or (reference, payload) in seen):
            return False
        if epoch is not None and (not isinstance(epoch, str) or memory_refs.normalize_id(epoch) != epoch):
            return False
        seen.add((reference, payload))
    return True


@dataclass(frozen=True)
class Facts:
    references: tuple[str, ...] = ()
    complete: bool = True


@dataclass(frozen=True)
class Effect:
    current: Facts
    prior: Facts = Facts()


def facts(*bodies: str | None, explicit: Any = None, prepared: Any = None) -> Facts:
    """Read declared retained inputs, including carriers unchanged by this write."""
    references, complete = [], True
    for body in bodies:
        document = provenance.parse_origin(body or "", managed=True)
        if document.payload is not None:
            references.extend(binding["reference"].partition("#")[0]
                              for binding in document.payload["inputs"].values())
        complete = complete and document.status != "unassessed"
    if explicit is not None:
        if explicit.canonical_ref is None:
            complete = False
        else:
            references.append(explicit.canonical_ref)
    if prepared is not None:
        references.extend(prepared.source_references)
    unique = tuple(dict.fromkeys(references))
    return Facts(unique[:MAX_PAYLOAD_REFERENCES], complete and len(unique) <= MAX_PAYLOAD_REFERENCES)


@dataclass
class FileDependencies:
    operation_id: str
    effects: Mapping[str, Effect]
    admissions: tuple[source_custody.DependencyAdmission, ...]
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def bindings(self, transition: str, *, payload: str | None = None) -> dict[str, Any]:
        epochs = {admission.source_ref: admission.epoch_id for admission in self.admissions}
        effect = self.effects[transition]
        selected = {name: getattr(effect, name) for name in PAYLOADS if payload is None or name == payload}
        return {
            "dependency_operation_id": self.operation_id,
            "dependency_complete": all(facts.complete for facts in selected.values()),
            "dependency_bindings": [{"source_ref": reference, "epoch_id": epochs.get(reference), "payload": name}
                                    for name, facts in selected.items() for reference in facts.references],
        }

    def event(self, prefix: str, event: dict[str, Any]) -> dict[str, Any]:
        bound = {**event, "version": EVENT_VERSION, **self.bindings(event["transition_id"])}
        self.events.append((prefix, bound))
        return bound

    def audit_body(self, prefix: str, body: str) -> str:
        if not body.startswith(prefix):
            raise ValueError("dependency audit has another protocol prefix")
        event = self.event(prefix, json.loads(body[len(prefix):]))
        return prefix + json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

    def history(self, write: vault.PlannedWrite, transition: str) -> vault.PlannedWrite:
        if not write.content:
            return write  # A same-payload marker retains no body.
        entry = json.loads(write.content)
        # This entry owns only prior bytes. A failed replacement cannot transfer its inputs here.
        entry.update(version=HISTORY_VERSION, **self.bindings(transition, payload="prior"))
        return vault.PlannedWrite(write.path, json.dumps(entry, ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":")) + "\n", create_only=write.create_only,
                                  guard=write.guard, expected_hash=write.expected_hash)

    def begin(self, root: Path) -> None:
        source_custody.begin_dependency_effect(root, self.admissions)

    def observe(self, root: Path, writes: Sequence[vault.PlannedWrite]) -> None:
        """A current primary plus its exact audit, or retained history, proves a file effect.

        This checks only this bounded operation's targets. Missing or partially rolled-back
        publication stays uncertain; neither an exception nor SQL rollback proves cleanup.
        """
        if not self.admissions:
            return
        expected = {event["transition_id"]: event for _prefix, event in self.events}
        audit_paths: dict[str, set[Path]] = {}
        history_paths: dict[str, set[Path]] = {}
        by_path = {write.path: write for write in writes}
        prefixes = tuple(dict.fromkeys(prefix for prefix, _event in self.events))
        for write in writes:
            for line in write.content.splitlines():
                prefix = next((prefix for prefix in prefixes if line.startswith(prefix)), None)
                if prefix is None:
                    continue
                try:
                    event = json.loads(line[len(prefix):])
                except ValueError:
                    continue
                if isinstance(event, dict) and event == expected.get(event.get("transition_id")):
                    audit_paths.setdefault(event["transition_id"], set()).add(write.path)
            if write.path.suffix == ".json" and write.content:
                try:
                    entry = json.loads(write.content)
                except ValueError:
                    continue
                transition = entry.get("transition_id") if isinstance(entry, dict) else None
                if transition in expected and entry.get("version") == HISTORY_VERSION and all(
                        entry.get(key) == value for key, value in self.bindings(transition, payload="prior").items()):
                    history_paths.setdefault(transition, set()).add(write.path)
        needed = {path for paths in (*audit_paths.values(), *history_paths.values()) for path in paths}
        needed.update(root / event["canonical_path"] for event in expected.values())
        matching = set()
        for path in needed:
            write = by_path.get(path)
            if write is None:
                continue
            try:
                text, guard = vault.read_guarded_text(root, path)
                exact = vault.content_hash(text) == vault.content_hash(write.content)
                guard.recheck(root)
            except (OSError, UnicodeError, vault.PathGuardError):
                continue
            if exact:
                matching.add(path)
        admitted = {admission.source_ref: admission for admission in self.admissions}
        for transition, event in expected.items():
            prior_published = bool(history_paths.get(transition, set()) & matching)
            current_published = (root / event["canonical_path"] in matching
                                 and bool(audit_paths.get(transition, set()) & matching))
            if not prior_published and not current_published:
                continue
            witness = source_custody.OwnerLocator(owner=PRODUCER.owner, locator=f"{self.operation_id}:{transition}",
                version=vault.content_hash(json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))))
            effect = self.effects[transition]
            references = (*(effect.current.references if current_published else ()),
                          *(effect.prior.references if prior_published else ()))
            for reference in dict.fromkeys(references):
                admission = admitted.get(reference)
                if admission is not None:
                    source_custody.observe_dependency(root, admission, witness=witness)


def prepare(root: Path, *, operation_id: str, effects: Mapping[str, Effect], preview: bool = False) -> FileDependencies:
    references = tuple(dict.fromkeys(reference for effect in effects.values() for payload in PAYLOADS
                                    for reference in getattr(effect, payload).references))
    admissions = () if preview else source_custody.reserve_dependencies(
        root, references, producer=PRODUCER, operation_id=operation_id, checkpoint=f"before:{operation_id}")
    return FileDependencies(operation_id, effects, admissions)
