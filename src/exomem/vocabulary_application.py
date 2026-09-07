"""Bind a reviewed vocabulary choice to one canonical writer invocation.

Correlation fields are deliberately not authority: this module re-reads the
durable decision, checks the exact supported writer shape, and accepts a
completion only through the writer-owned terminal wrapper below.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .vocabulary_state import VocabularyState
from .vocabulary_workflow import Evidence, WorkItem, _hash, make_item

_SEAL = object()


@dataclass(frozen=True)
class ApplicationBinding:
    item: WorkItem
    choice: dict[str, Any]
    request_id: str
    operation_id: str
    curation: tuple[str, str, str] | None = None
    relation_step: str | None = None
    replay: bool = False


@dataclass(frozen=True)
class _WriterTerminal:
    value: Mapping[str, Any]
    seal: object


def _item(stored: Mapping[str, Any]) -> WorkItem:
    return make_item(
        family=stored["family"],
        signal=stored["signal"],
        targets=stored["target_versions"],
        evidence=[Evidence(**entry) for entry in stored["evidence"]],
        registry_hashes=stored["registry_hashes"],
        projection_status=stored["projection"]["status"],
        continuation=stored.get("continuation"),
        paths=stored.get("paths"),
        question=stored.get("question"),
        logical_identity=stored.get("logical_identity"),
        projection_currency=stored["projection"].get("currency", {}),
    )


def _metadata(kwargs: Mapping[str, Any]) -> tuple[str, str]:
    ref = kwargs.get("vocabulary_ref")
    fingerprint = kwargs.get("vocabulary_fingerprint")
    if not isinstance(ref, str) or not ref or not isinstance(fingerprint, str) or not fingerprint:
        raise ValueError("VOCABULARY_APPLICATION_INVALID: vocabulary correlation pair is required")
    return ref, fingerprint


def _matches(
    vault_root,
    command: str,
    kwargs: Mapping[str, Any],
    item: WorkItem,
    choice: Mapping[str, Any],
    decision_outcome: str,
) -> bool:
    operation = kwargs.get("operation")
    canonical = choice.get("canonical")
    definition = choice.get("definition")
    if command == "connect_memory" and operation == "create-entity":
        return (
            item.family == "entity-instance/v1"
            and decision_outcome == "propose-new"
            and isinstance(definition, Mapping)
            and all(
            kwargs.get(key) == definition.get(key) for key in ("entity_type", "name", "summary")
            )
        )
    if command == "connect_memory" and operation == "accept-relation":
        if item.family != "relation-type/v1" or not isinstance(canonical, str):
            return False
        from . import relation_queue

        try:
            resolved = relation_queue.resolve_candidate(vault_root, str(kwargs.get("ref") or ""))
            selected = relation_queue.selected_relation(
                vault_root,
                resolved.candidate,
                kwargs.get("requested_relation"),
                source_page=getattr(resolved, "source_page", None),
            )
        except (OSError, ValueError):
            return False
        candidate = selected
        paths = set(item.paths)
        path_hints = dict(item.paths)
        reviewed = {path_hints.get(ref, ref) for ref, _version in item.target_versions}
        return (
            kwargs.get("expected_fingerprint") == resolved.fingerprint
            and candidate.get("relation_type") == canonical
            and {candidate.get("from"), candidate.get("to")} <= reviewed
            and bool(paths or reviewed)
        )
    if command == "schema_memory" and operation == "save-entity-types":
        proposal = kwargs.get("proposal")
        return (
            item.family == "entity-type/v1"
            and decision_outcome == "propose-new"
            and isinstance(definition, Mapping)
            and isinstance(canonical, str)
            and bool(canonical)
            and isinstance(proposal, Mapping)
            and isinstance(proposal.get("entity_types"), Mapping)
            and proposal["entity_types"].get(canonical) == definition
        )
    if command == "schema_memory" and operation == "save-relations":
        proposal = kwargs.get("proposal")
        return (
            item.family == "relation-type/v1"
            and decision_outcome == "propose-new"
            and isinstance(definition, Mapping)
            and isinstance(canonical, str)
            and bool(canonical)
            and kwargs.get("subject") == "relations"
            and isinstance(proposal, Mapping)
            and isinstance(proposal.get("upsert"), Mapping)
            and proposal["upsert"].get(canonical) == definition
        )
    return False


def _curation_binding(vault_root, kwargs: Mapping[str, Any], item: WorkItem, choice: Mapping[str, Any]):
    if kwargs.get("mode") != "curation" or kwargs.get("curation_action") not in {"apply", "resume"}:
        return None
    from .curation import CurationStore, work_item

    run_id = kwargs.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return None
    store = CurationStore(vault_root)
    plan_id, fingerprint = store.identities(run_id)
    if kwargs.get("plan_id") != plan_id:
        return None
    if kwargs.get("curation_action") == "apply" and kwargs.get("expected_plan_fingerprint") != fingerprint:
        return None
    plan = store.load_plan(run_id)
    candidate = plan.get("entity_candidate")
    if (
        not isinstance(candidate, Mapping)
        or not isinstance(item.logical_identity, str)
        or candidate.get("review_ref") != item.logical_identity
    ):
        return None
    if kwargs.get("curation_action") == "apply":
        try:
            current = work_item(vault_root, review_ref=item.logical_identity).get(
                "entity_candidate"
            )
        except (OSError, ValueError):
            return None
        if not isinstance(current, Mapping) or (
            candidate.get("review_fingerprint") != current.get("review_fingerprint")
        ):
            return None
    steps = plan.get("steps")
    definition = choice.get("definition")
    if item.family != "entity-instance/v1" or not isinstance(steps, list):
        return None
    if candidate.get("candidate_state") == "promotion":
        args = steps[0].get("args") if len(steps) == 1 and isinstance(steps[0], Mapping) else None
        if not isinstance(args, Mapping) or not isinstance(definition, Mapping):
            return None
        if steps[0].get("kind") != "create-entity" or any(
            args.get(key) != definition.get(key) for key in ("entity_type", "name", "summary")
        ):
            return None
    elif candidate.get("candidate_state") == "hydration":
        if choice.get("canonical") not in set(candidate.get("target_refs") or []):
            return None
    else:
        return None
    return run_id, plan_id, fingerprint


def bind(
    vault_root,
    *,
    command: str,
    kwargs: Mapping[str, Any],
    idempotency_key: str,
    command_digest: str,
    principal: str,
    request_id: str | None = None,
    canonical_terminal: Mapping[str, Any] | None = None,
) -> ApplicationBinding:
    """Return a private binding only for an exact, current reviewed choice."""
    ref, fingerprint = _metadata(kwargs)
    if not all(isinstance(value, str) and value for value in (command, idempotency_key, command_digest, principal)):
        raise ValueError("VOCABULARY_APPLICATION_INVALID: canonical operation identity is required")
    operation_id = _hash({"domain": "vocabulary-application/v1", "principal": principal, "key": idempotency_key, "command": command_digest})
    store = VocabularyState(vault_root)
    existing = store.bound_operation(ref=ref, fingerprint=fingerprint, operation_id=operation_id)
    view = store.get(ref)
    item = _item(view)
    decision = view.get("decision")
    if existing is not None and (
        existing["record"].get("state") == "applied"
        or existing["operation"].get("state") == "committed"
    ):
        choice = existing["record"].get("choice")
        request = existing["operation"].get("request_id")
        if not isinstance(choice, dict) or not isinstance(request, str) or not request:
            raise ValueError("VOCABULARY_APPLICATION_INVALID: stored application is incomplete")
        return ApplicationBinding(
            item=item,
            choice=choice,
            request_id=request,
            operation_id=operation_id,
            replay=True,
        )
    if (
        existing is not None
        and existing["operation"].get("state") in {None, "bound", "uncertain"}
        and isinstance(canonical_terminal, Mapping)
        and canonical_terminal.get("state") == "committed"
        and isinstance(canonical_terminal.get("receipt_id"), str)
        and canonical_terminal["receipt_id"]
    ):
        choice = existing["record"].get("choice")
        request = existing["operation"].get("request_id")
        application = existing["record"].get("application")
        stored_curation = application.get("curation") if isinstance(application, Mapping) else None
        curation = (
            (stored_curation["run_id"], stored_curation["plan_id"], stored_curation["plan_fingerprint"])
            if isinstance(stored_curation, Mapping)
            and all(isinstance(stored_curation.get(key), str) and stored_curation[key] for key in (
                "run_id", "plan_id", "plan_fingerprint"
            ))
            else None
        )
        if not isinstance(choice, dict) or not isinstance(request, str) or not request:
            raise ValueError("VOCABULARY_APPLICATION_INVALID: stored application is incomplete")
        return ApplicationBinding(
            item=item,
            choice=choice,
            request_id=request,
            operation_id=operation_id,
            curation=curation,
            relation_step=existing["operation"].get("step"),
        )
    if item.fingerprint != fingerprint or view.get("decision_currency") != "current" or not isinstance(decision, Mapping):
        raise ValueError("VOCABULARY_DECISION_STALE: refresh vocabulary review before application")
    choice = decision.get("choice")
    if decision.get("state") not in {"proposed", "awaiting_approval", "applying", "applied"} or not isinstance(choice, dict):
        raise ValueError("VOCABULARY_APPLICATION_INVALID: a current proposal is required")
    curation = _curation_binding(vault_root, kwargs, item, choice) if command == "maintain_memory" else None
    relation_step = (
        "registration"
        if item.family == "relation-type/v1"
        and decision.get("outcome") == "propose-new"
        and len(item.target_versions) == 2
        and command == "schema_memory"
        and kwargs.get("operation") == "save-relations"
        else "edge"
        if item.family == "relation-type/v1"
        and decision.get("outcome") == "propose-new"
        and len(item.target_versions) == 2
        and command == "connect_memory"
        and kwargs.get("operation") == "accept-relation"
        else None
    )
    if curation is None:
        from . import vocabulary_review

        try:
            current_item, _pages = vocabulary_review._fresh(vault_root, view)
        except (OSError, ValueError) as exc:
            raise ValueError("VOCABULARY_DECISION_STALE: refresh vocabulary review before application") from exc
        current_currency = {
            key: current_item.to_dict()[key]
            for key in ("fingerprint", "registry_hashes", "target_versions")
        }
        registration = (
            next(
                (
                    entry
                    for entry in (decision.get("application") or {}).get("operations", [])
                    if entry.get("step") == "registration" and entry.get("state") == "committed"
                ),
                None,
            )
            if relation_step == "edge"
            else None
        )
        stale = (
            current_item.fingerprint != item.fingerprint
            or current_item.registry_hashes != item.registry_hashes
            or current_item.target_versions != item.target_versions
        )
        if stale and not (
            isinstance(registration, Mapping) and registration.get("result_currency") == current_currency
        ):
            store.observe(current_item)
            raise ValueError("VOCABULARY_DECISION_STALE: refresh vocabulary review before application")
    if curation is None and not _matches(
        vault_root, command, kwargs, item, choice, str(decision.get("outcome"))
    ):
        raise ValueError("VOCABULARY_APPLICATION_INVALID: command does not implement the reviewed choice")
    request = request_id or operation_id
    if decision.get("state") == "applied":
        application = decision.get("application")
        if not isinstance(application, Mapping) or application.get("operation_id") != operation_id:
            raise ValueError("VOCABULARY_APPLICATION_INVALID: operation identity is already bound")
        return ApplicationBinding(item=item, choice=choice, request_id=str(application["request_id"]), operation_id=operation_id, curation=curation, relation_step=relation_step)
    if existing is not None:
        request = existing["operation"].get("request_id")
        if not isinstance(request, str) or not request:
            raise ValueError("VOCABULARY_APPLICATION_INVALID: stored application is incomplete")
    if curation is None:
        store.begin_application(
            item,
            request_id=request,
            operation_id=operation_id,
            expected_results=dict(item.target_versions),
            choice=choice,
            step=relation_step,
        )
    else:
        store.begin_curation_application(
            item,
            request_id=request,
            operation_id=operation_id,
            run_id=curation[0],
            plan_id=curation[1],
            plan_fingerprint=curation[2],
            choice=choice,
        )
    return ApplicationBinding(item=item, choice=choice, request_id=request, operation_id=operation_id, curation=curation, relation_step=relation_step)


def _writer_terminal(value: Mapping[str, Any]) -> _WriterTerminal:
    """Writer lease only: make an unforgeable-in-band terminal capability."""
    return _WriterTerminal(value=value, seal=_SEAL)


def _resulting_versions(vault_root, binding: ApplicationBinding, terminal: Mapping[str, Any]) -> dict[str, str]:
    """Read the canonical postcondition; never trust caller result fields."""
    from . import get_page, vocabulary_review

    if binding.item.family in {"entity-type/v1", "relation-type/v1"}:
        leaf = terminal.get("leaf_result")
        if binding.item.family == "relation-type/v1" and isinstance(leaf, Mapping) and all(
            isinstance(leaf.get(key), str) and leaf[key]
            for key in ("from", "to", "relation_type")
        ):
            source = leaf.get("from") if isinstance(leaf, Mapping) else None
            target = leaf.get("to") if isinstance(leaf, Mapping) else None
            relation = leaf.get("relation_type") if isinstance(leaf, Mapping) else None
            if not all(isinstance(value, str) and value for value in (source, target, relation)):
                raise ValueError("VOCABULARY_APPLICATION_INVALID: accepted relation postcondition absent")
            page = get_page.get_page(vault_root, path=source)
            bullet = f"- {relation} [[{target.removesuffix('.md')}]]"
            if bullet not in page.body:
                raise ValueError("VOCABULARY_APPLICATION_INVALID: accepted relation is not canonical")
            paths = dict(binding.item.paths)
            resulting = dict(binding.item.target_versions)
            for ref in resulting:
                if paths.get(ref, ref) == source:
                    resulting[ref] = page.content_hash
            return resulting
        registries = vocabulary_review.registry_hashes(vault_root)
        key = "entity_types" if binding.item.family == "entity-type/v1" else "relations"
        return {f"registry:{key}": registries[key]}
    leaf = terminal.get("leaf_result")
    path = leaf.get("path") if isinstance(leaf, Mapping) else None
    if not isinstance(path, str) or not path:
        raise ValueError("VOCABULARY_APPLICATION_INVALID: entity writer returned no canonical path")
    page = get_page.get_page(vault_root, path=path)
    definition = binding.choice["definition"]
    if (
        page.frontmatter.get("type") != "entity"
        or page.frontmatter.get("entity_type") != definition["entity_type"]
        or page.frontmatter.get("title") != definition["name"]
    ):
        raise ValueError("VOCABULARY_APPLICATION_INVALID: canonical entity postcondition differs")
    return dict(binding.item.target_versions) | {path: page.content_hash}


def commit(vault_root, binding: ApplicationBinding, terminal: object) -> dict[str, Any]:
    if not isinstance(terminal, _WriterTerminal) or terminal.seal is not _SEAL:
        raise ValueError("VOCABULARY_APPLICATION_INVALID: writer-sealed terminal required")
    if binding.replay:
        return VocabularyState(vault_root).get(binding.item.ref)
    if binding.curation is not None:
        from .curation import CurationStore

        reconstructed = CurationStore(vault_root).reconstruct(binding.curation[0])
        return VocabularyState(vault_root).record_curation_receipts(
            binding.item,
            operation_id=binding.operation_id,
            phase=str(reconstructed.get("phase")),
            receipts=list(reconstructed.get("receipts") or []),
        )
    store = VocabularyState(vault_root)
    value = {**terminal.value, "request_id": binding.request_id}
    resulting_versions = _resulting_versions(vault_root, binding, value)
    store.bind_resulting_versions(
        binding.item, operation_id=binding.operation_id, resulting_versions=resulting_versions
    )
    return store.record_committed_receipt(
        binding.item,
        value,
        resulting_versions=resulting_versions,
        applied_choice=binding.choice,
    )
