"""Bounded, guarded continuity evidence for destructive promotion undo.

History remains unverified on public reads. Only a complete retained chain can
bind an undo to its original subject; a gap leaves ordinary writes available.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import compiled_history, memory_refs, record_history, registry_history, vault


@dataclass(frozen=True)
class Proof:
    status: str
    fingerprint: str | None = None
    guards: tuple[vault.PathGuard | vault.DirectoryCensusGuard, ...] = ()


def _result(status: str, subject: Mapping[str, Any], guards: list, evidence: Any = None) -> Proof:
    fingerprint = vault.content_hash(json.dumps(
        {"subject": dict(subject), "evidence": evidence}, sort_keys=True, separators=(",", ":"),
    )) if status == "same" else None
    return Proof(status, fingerprint, tuple(guards))


def _hash(value: Any) -> bool:
    # SHA-256 has a fixed protocol alphabet and width, independent of vault vocabulary.
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _collect(
    root: Path, directory: str, suffix: str, admit: Callable[[str, str], None],
    *, include_internal_state: bool = False,
) -> tuple[list[tuple[str, str]], list]:
    """Account for every child, including unsafe names the history reader omits."""
    cap = compiled_history._scan_cap(record_history.PAGE_MAX)

    consumed_names: set[str] = set()

    def admitted(path: str) -> bool:
        name = Path(path).name
        # The census checks new names; only retained inputs need renewed disclosure.
        # Own batch additions and staging directories use its allowed_changes protocol.
        if name not in consumed_names:
            return True
        text = registry_history.read_kept(root, directory, name)
        if text is None:
            raise ValueError("history entry unavailable")
        admit(name, text)
        return True

    census = vault.DirectoryCensusGuard.capture(
        root, directory, max_entries=cap, admission=admitted,
        include_internal_state=include_internal_state,
    )
    names = registry_history.kept_names(root, directory, suffix=suffix)
    if names is None:
        if census.missing_paths:
            return [], [census]
        raise ValueError("history directory unavailable")
    if set(names) != {Path(entry.relative_path).name for entry in census.entries}:
        raise ValueError("history census contains an unaccounted entry")
    guards: list = [census]
    entries = []
    for name in names:
        text = registry_history.read_kept(root, directory, name)
        if text is None:
            raise ValueError("history entry unavailable")
        admit(name, text)
        consumed_names.add(name)
        guards.append(vault.PathGuard.capture(
            root, f"{directory}/{name}", leaf_policy="content",
            expected_content_hash=vault.content_hash(text),
        ))
        entries.append((name.removesuffix(suffix), text))
    for guard in guards:
        guard.recheck(root)
    return entries, guards


def _page(root: Path, path: str, expected: Any, guards: list) -> str | None:
    if memory_refs.parse_memory_ref(str(expected or "")) is None:
        raise ValueError("original page identity unavailable")
    try:
        absolute, _relative = vault.resolve_under_vault(root, path)
        source, guard = vault.read_guarded_text(root, absolute)
    except FileNotFoundError:
        guards.append(vault.PathGuard.capture(root, path, leaf_policy="absent"))
        return None
    registry_history._require_snapshot_admission(root, path, source.encode(), markdown=True)
    guards.append(guard)
    return source if memory_refs.ref_from_markdown(source) == expected else None


def _edge_presence(root: Path, source: str, subject: Mapping[str, Any], registry: Any) -> bool:
    from . import markdown_relations, promotions

    resolve = promotions._Resolver(root)
    unresolved = False
    for candidate in markdown_relations.scan_markdown_relations(source).candidates:
        if not (candidate.canonical and candidate.grammar_valid):
            continue
        canonical = registry.resolve(candidate.kind).canonical
        target = resolve(candidate.target)
        if canonical is None and (target is None or target == subject["target"]):
            unresolved = True  # A removed alias cannot prove that the original edge ended.
        if canonical != subject["predicate"]:
            continue
        if target == subject["target"]:
            return True
        unresolved |= target is None
    if unresolved:
        raise ValueError("historical edge target unavailable")
    return False


def page(root: Path, kind: str, subject: Mapping[str, Any]) -> Proof:
    """Prove entity identity or every retained state of one promoted edge."""
    from . import relation_registry

    guards: list = []
    try:
        path = str(subject["path"] if kind == "entity" else subject["subject"])
        expected = subject.get("ref") if kind == "entity" else subject.get("source_ref")
        current = _page(root, path, expected, guards)
        if current is None:
            return _result("ended", subject, guards)
        if kind == "entity":
            return _result("same", subject, guards, vault.content_hash(current))
        target = _page(root, str(subject["target"]), subject.get("target_ref"), guards)
        if target is None:
            return _result("ended", subject, guards)
        registry = relation_registry.load_for_page(root, path, vault.parse_frontmatter(current)[0])
        if registry is None:
            raise ValueError("relation registry unavailable")
        if not _edge_presence(root, current, subject, registry):
            return _result("ended", subject, guards)
        introduction = subject["introduction"]
        if introduction["source_ref"] != expected or not _hash(introduction["after_hash"]):
            raise ValueError("edge introduction unavailable")
        nodes: dict[str, tuple[str, str, str]] = {}

        def admit(name: str, text: str) -> None:
            entry = compiled_history._admitted_entry(root, text, path=path, parent_ref=expected)
            if entry is None:
                raise ValueError("history admission unavailable")
            transition = entry["transition_id"]
            if str(uuid.UUID(transition)) != transition or not _hash(entry["after_hash"]):
                raise ValueError("history transition invalid")
            moment = dt.datetime.fromisoformat(entry["at"].replace("Z", "+00:00"))
            if name != registry_history.version_id(moment, transition) + ".json":
                raise ValueError("history transition name invalid")

        entries, history_guards = _collect(
            root, compiled_history.history_dir(expected), ".json", admit, include_internal_state=True,
        )
        guards.extend(history_guards)
        for _version, text in entries:
            entry = json.loads(text)
            transition = entry["transition_id"]
            if transition in nodes:
                raise ValueError("duplicate history transition")
            nodes[transition] = (entry["before_hash"], entry["after_hash"], entry["prior"]["source"])
        outgoing: dict[str, list[str]] = {}
        incoming: dict[str, list[str]] = {}
        states = {vault.content_hash(current): current}
        for transition, (before, after, source) in nodes.items():
            states[before] = source
            if before == after:
                # Exact self-loops retain a real state, which still needs subject admission.
                continue
            outgoing.setdefault(before, []).append(transition)
            incoming.setdefault(after, []).append(transition)
        if any(len(edges) != 1 for edges in (*outgoing.values(), *incoming.values())):
            raise ValueError("history fork")
        roots = outgoing.keys() - incoming.keys()
        nontrivial = {key for key, (before, after, _source) in nodes.items() if before != after}
        endpoint = vault.content_hash(current)
        if nontrivial and len(roots) != 1:
            raise ValueError("history cycle or disconnected roots")
        first = next(iter(roots)) if nontrivial else endpoint
        chain: list[str] = []
        seen = {first}
        cursor = first
        while cursor in outgoing:
            transition = outgoing[cursor][0]
            chain.append(transition)
            cursor = nodes[transition][1]
            if cursor in seen:
                raise ValueError("history cycle")
            seen.add(cursor)
        if cursor != endpoint or set(chain) != nontrivial:
            raise ValueError("history does not connect to the current page")
        if any(before == after and before not in seen for before, after, _source in nodes.values()):
            raise ValueError("disconnected history self-loop")
        introduced = introduction.get("transition_id")
        if introduced is not None:
            if introduced not in chain or nodes[introduced][1] != introduction["after_hash"]:
                raise ValueError("introducing transition unavailable")
            hashes = [nodes[key][1] for key in chain[chain.index(introduced):]]
        else:
            if introduction.get("creation_ref") != expected or first != introduction["after_hash"]:
                raise ValueError("creation image is not the history root")
            hashes = [first, *(nodes[key][1] for key in chain)]
        for image_hash in hashes:
            image_source = states.get(image_hash)
            if image_source is None:
                raise ValueError("historical after image unavailable")
            if not _edge_presence(root, image_source, subject, registry):
                return _result("ended", subject, guards)
        return _result("same", subject, guards, [endpoint, sorted((key, before, after) for key, (before, after, _source) in nodes.items()), vault.content_hash(target)])
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError):
        return _result("unverified", subject, guards)


def registry(root: Path, subject: Mapping[str, Any], *, spec: Any = None) -> Proof:
    """Prove the reachable registry suffix from a retained introducing version."""
    from .vocabulary import contract, instances, registry_spec
    from .vocabulary import registry as registries

    guards: list = []
    try:
        spec = spec or instances.select(root, registry_spec(str(subject["registry"])), subject.get("scope"))
        if contract.admission_refusal(root, spec) is not None:
            raise ValueError("registry admission unavailable")
        if str(subject["registry"]) != spec.name or (subject.get("scope") or registries.PUBLIC_INSTANCE) != spec.instance_id:
            raise ValueError("registry subject mismatch")
        previous, guard = registry_history.read_previous(root, spec.overlay(root), max_bytes=registries.MAX_OVERLAY_BYTES)
        if guard is None:
            raise ValueError("registry source unavailable")
        guards.append(guard)
        current = previous if previous is not None else registry_history.NO_OVERRIDE
        endpoint = vault.content_hash(current)
        key = str(subject["key"])

        def active(source: str) -> bool:
            snapshot = registries._build(spec, source)
            if any(item.get("severity", "error") == "error" for item in snapshot.findings):
                raise ValueError("registry snapshot invalid")
            entry = snapshot.entries.get(key)
            # The registry schema fixes this status; the key remains registry data.
            return entry is not None and entry.status == "active"

        if not active(current):
            return _result("ended", subject, guards)

        def admit(name: str, text: str) -> None:
            registry_history._require_snapshot_admission(
                root, f"{directory}/{name}", text.encode(),
            )
            header, source = registry_history._split(text)
            if not header:
                raise ValueError("registry history invalid")
            active(source)
            before = header.get("before_source_hash")
            after = header.get("after_source_hash")
            if before is not None and (not _hash(before) or before != vault.content_hash(source)):
                raise ValueError("registry history before image mismatch")
            if after is not None and not _hash(after):
                raise ValueError("registry history after image invalid")

        directory = registry_history.history_dir(root, spec.stem).relative_to(root).as_posix()
        entries, history_guards = _collect(root, directory, ".yaml", admit)
        guards.extend(history_guards)
        nodes = {version: (*registry_history._split(text),) for version, text in entries}
        introduced = subject["introduced_version"]
        header, _source = nodes[introduced]
        first = subject["after_hash"]
        if not _hash(first) or header.get("after_source_hash") != first:
            raise ValueError("registry introducing version unavailable")
        outgoing: dict[str, list[tuple[str, str | None, str]]] = {}
        for version, (metadata, source) in nodes.items():
            before = vault.content_hash(source)
            after = metadata.get("after_source_hash")
            outgoing.setdefault(before, []).append((version, after, source))
        states = {endpoint: current}
        for _metadata, source in nodes.values():
            states[vault.content_hash(source)] = source
        cursor = first
        seen = set()
        consumed = []
        while True:
            if cursor in seen:
                raise ValueError("registry history cycle")
            seen.add(cursor)
            image = states.get(cursor)
            if image is None:
                raise ValueError("registry after image unavailable")
            next_nodes = []
            for version, after, _source in outgoing.get(cursor, []):
                consumed.append(version)
                if after is None:
                    raise ValueError("reachable legacy registry transition")
                if after != cursor:
                    next_nodes.append((version, after))
            if len(next_nodes) > 1:
                raise ValueError("registry history fork")
            if not next_nodes:
                if cursor != endpoint:
                    raise ValueError("registry suffix does not reach current overlay")
                break
            cursor = next_nodes[0][1]
        if any(not active(states[image_hash]) for image_hash in seen):
            return _result("ended", subject, guards)
        return _result("same", subject, guards, [endpoint, introduced, sorted(consumed)])
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError):
        return _result("unverified", subject, guards)


def subjects(root: Path, kind: str, items: list[dict[str, Any]]) -> list[Proof]:
    return [registry(root, item) if kind == "registry" else page(root, kind, item) for item in items]


def metadata(value: Any) -> dict[str, str]:
    """Validate the reserved undo metadata before a mutation candidate is built."""
    from . import promotions

    # nosemgrep: ep-word-set, ep-word-membership -- the reserved undo protocol fixes these fields.
    if not isinstance(value, Mapping) or set(value) != {"ref", "expected_fingerprint"}:
        raise ValueError("PROMOTION_SUBJECT_UNVERIFIED: invalid undo binding")
    if not isinstance(value["ref"], str) or not _hash(value["expected_fingerprint"]):
        raise ValueError("PROMOTION_SUBJECT_UNVERIFIED: invalid undo binding")
    promotions.parse_promotion_ref(value["ref"])
    return dict(value)


def fingerprint(proofs: list[Proof]) -> str:
    return vault.content_hash(json.dumps([proof.fingerprint for proof in proofs], separators=(",", ":")))


def require_same(proofs: list[Proof]) -> None:
    if any(proof.status == "unverified" for proof in proofs):
        raise ValueError("PROMOTION_SUBJECT_UNVERIFIED: original subject continuity is unavailable")
    if any(proof.status != "same" for proof in proofs):
        raise ValueError("PROMOTION_SUBJECT_CHANGED: the original promotion ended")
