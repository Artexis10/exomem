"""Durable consideration state inside the existing review-state owner.

Only `decide` is an agent decision operation. Application transitions are
internal integration seams: callers must supply a freshly resolved item and
canonical writer/recovery evidence, never a terminal submitted by an agent.
This module does not authorize, invoke or repeat a content mutation.
"""

from __future__ import annotations

import copy
import datetime as dt
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import mutation_terminal, review_state
from .vocabulary_workflow import WorkItem, _hash, _text, _versions, make_item, validate_decision


def _key(item: WorkItem) -> str:
    return f"{item.ref}:{item.fingerprint}"


def _currency(item: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item[key] for key in ("fingerprint", "registry_hashes", "target_versions")}


def _view(section: dict[str, Any], ref: str) -> dict[str, Any]:
    stored = section["items"].get(ref)
    if not isinstance(stored, dict):
        raise ValueError("VOCABULARY_ITEM_NOT_FOUND: refresh vocabulary review")
    decision = section["decisions"].get(f"{ref}:{stored['fingerprint']}")
    if decision is None:
        # A canonical target edit changes the signal fingerprint. Retain its
        # bound application only for the exact expected resulting snapshot.
        matches = [
            record
            for record in section["decisions"].values()
            if record["item_ref"] == ref
            and record.get("application", {}).get("result_currency") == _currency(stored)
            and record["state"] in {"applying", "applied"}
        ]
        if len(matches) == 1:
            decision = matches[0]
    current = decision is None or _currency(decision) == _currency(stored)
    if decision and decision["state"] == "applied":
        current = current or decision["application"]["result_currency"] == _currency(stored)
    state = decision["state"] if decision else "pending"
    if not current and state not in {"deferred", "applying"}:
        state = "pending"
    return copy.deepcopy(
        {
            **stored,
            "state": state,
            "decision_currency": "current" if current else "refresh_required",
            "decision": decision,
            "receipts": decision.get("receipts", []) if decision else [],
        }
    )


def _current(section: dict[str, Any], item: WorkItem) -> dict[str, Any]:
    view = _view(section, item.ref)
    if (
        view["fingerprint"] != item.fingerprint
        or view["registry_hashes"] != dict(item.registry_hashes)
        or view["target_versions"] != dict(item.target_versions)
    ):
        raise ValueError("VOCABULARY_DECISION_STALE: refresh the current work item")
    return view


def _current_decision(section: dict[str, Any], item: WorkItem) -> dict[str, Any]:
    view = _current(section, item)
    if view["decision_currency"] != "current":
        raise ValueError(
            "VOCABULARY_DECISION_STALE: review the changed registry before application"
        )
    return view


class VocabularyState:
    def __init__(self, vault_root: Path):
        self.store = review_state.ReviewStateStore(vault_root)

    def _update(
        self,
        operation: Callable[[dict[str, Any]], Any],
        *,
        affected_refs: tuple[str, ...] | None = None,
    ) -> Any:
        # Use the owner's existing lock and publication gate; a parallel review
        # disposition must not be overwritten by a vocabulary update.
        with review_state._LOCK:
            payload = self.store.load()
            section = payload["vocabulary"]
            before = copy.deepcopy(section)
            result = operation(section)
            if section != before:
                ref = result.get("ref") if isinstance(result, Mapping) else None
                self.store._write(
                    payload,
                    vocabulary_refs=(ref,) if isinstance(ref, str) else affected_refs,
                    vocabulary_families=(),
                )
            return result

    def observe(self, item: WorkItem) -> dict[str, Any]:
        def update(section):
            section["items"][item.ref] = item.to_dict()
            return _view(section, item.ref)

        return self._update(update)

    def get(self, ref: str) -> dict[str, Any]:
        return _view(self.store.load()["vocabulary"], ref)

    def bound_operation(
        self, *, ref: str, fingerprint: str, operation_id: str
    ) -> dict[str, Any] | None:
        """Return the exact prior writer binding, without treating it as current."""
        record = self.store.load()["vocabulary"]["decisions"].get(f"{ref}:{fingerprint}")
        if not isinstance(record, Mapping):
            return None
        application = record.get("application")
        if not isinstance(application, Mapping):
            return None
        if application.get("operation_id") == operation_id:
            return copy.deepcopy({"record": record, "operation": application})
        operations = application.get("operations")
        if isinstance(operations, list):
            operation = next(
                (entry for entry in operations if entry.get("operation_id") == operation_id), None
            )
            if isinstance(operation, Mapping):
                return copy.deepcopy({"record": record, "operation": operation})
        return None

    def decide(self, item: WorkItem, payload: Mapping[str, Any], *, actor: str) -> dict[str, Any]:
        decision = validate_decision(item, payload)
        if not _text(actor):
            raise ValueError("VOCABULARY_DECISION_INVALID: attributable actor required")

        def update(section):
            current = _current(section, item)
            if current["decision"] and current["decision"]["state"] in {"applying", "applied"}:
                raise ValueError(
                    "VOCABULARY_APPLICATION_INVALID: reconcile the bound application first"
                )
            section["decisions"][_key(item)] = {
                **decision.to_dict(),
                "actor": actor,
                "state": decision.initial_state,
                "receipts": [],
                "updated_at": dt.datetime.now(dt.UTC).isoformat(),
            }
            return _view(section, item.ref)

        return self._update(update)

    def await_approval(self, item: WorkItem) -> dict[str, Any]:
        def update(section):
            current = _current_decision(section, item)
            if current["state"] not in {"proposed", "awaiting_approval"}:
                raise ValueError("VOCABULARY_APPLICATION_INVALID: a current proposal is required")
            section["decisions"][_key(item)]["state"] = "awaiting_approval"
            return _view(section, item.ref)

        return self._update(update)

    def begin_application(
        self,
        item: WorkItem,
        *,
        request_id: str,
        operation_id: str | None = None,
        expected_results: Mapping[str, str],
        choice: Mapping[str, Any],
        step: str | None = None,
    ) -> dict[str, Any]:
        versions = dict(_versions(expected_results))
        if not _text(request_id) or not versions:
            raise ValueError(
                "VOCABULARY_APPLICATION_INVALID: canonical identity and result binding required"
            )
        binding = {
            "request_id": request_id,
            **({"operation_id": operation_id} if operation_id is not None else {}),
            "expected_results": versions,
            "choice_digest": _hash(choice),
            "reviewed_currency": _currency(item.to_dict()),
            "result_currency": _currency(
                make_item(
                    family=item.family,
                    signal=item.signal,
                    targets={
                        ref: versions.get(ref, version) for ref, version in item.target_versions
                    },
                    evidence=item.evidence,
                    registry_hashes={
                        key: versions.get(f"registry:{key}", version)
                        for key, version in item.registry_hashes
                    },
                    projection_status=item.projection_status,
                    question=item.question,
                    logical_identity=item.logical_identity,
                    projection_currency=dict(item.projection_currency),
                ).to_dict()
            ),
        }

        def update(section):
            current = _current_decision(section, item)
            record = section["decisions"].get(_key(item))
            if not record or choice != record.get("choice"):
                raise ValueError(
                    "VOCABULARY_APPLICATION_INVALID: application must match the reviewed meaning"
                )
            if current["state"] not in {"proposed", "awaiting_approval", "applying"}:
                raise ValueError("VOCABULARY_APPLICATION_INVALID: a current proposal is required")
            if step is not None:
                if step not in {"registration", "edge"}:
                    raise ValueError("VOCABULARY_APPLICATION_INVALID: unknown relation application step")
                application = record.get("application")
                if application is None:
                    if step != "registration":
                        raise ValueError("VOCABULARY_APPLICATION_INVALID: relation registration is pending")
                    application = {"requires_edge": True, "operations": []}
                    record.update(state="applying", application=application)
                if not application.get("requires_edge"):
                    raise ValueError("VOCABULARY_APPLICATION_INVALID: operation identity is already bound")
                operations = application.setdefault("operations", [])
                existing = next(
                    (entry for entry in operations if entry.get("operation_id") == operation_id), None
                )
                if existing is not None:
                    if existing.get("request_id") != request_id or existing.get("step") != step:
                        raise ValueError("VOCABULARY_APPLICATION_INVALID: operation identity is already bound")
                    return _view(section, item.ref)
                if step == "edge" and not any(
                    entry.get("step") == "registration" and entry.get("state") == "committed"
                    for entry in operations
                ):
                    raise ValueError("VOCABULARY_APPLICATION_INVALID: relation registration is pending")
                operations.append({**binding, "step": step, "state": "bound"})
                return _view(section, item.ref)
            application = record.get("application")
            if application is not None and application != binding and not (
                isinstance(application, Mapping)
                and application.get("operation_id") == operation_id
                and application.get("request_id") == request_id
                and application.get("state") == "uncertain"
            ):
                raise ValueError(
                    "VOCABULARY_APPLICATION_INVALID: operation identity is already bound"
                )
            record.update(state="applying", application=binding)
            return _view(section, item.ref)

        return self._update(update)

    def record_committed_receipt(
        self,
        item: WorkItem,
        terminal: Mapping[str, Any],
        *,
        resulting_versions: Mapping[str, str],
        applied_choice: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Consume a canonical terminal at the trusted writer/recovery seam.

        The marker is shape validation, not cryptographic authentication. This
        method must never be exposed as a user-supplied receipt decision route.
        Result versions are read from canonical state by the integrating owner.
        """
        if (
            not isinstance(terminal, Mapping)
            or terminal.get("_terminal") != mutation_terminal._TERMINAL_MARKER
            or terminal.get("version") != mutation_terminal._TERMINAL_VERSION
            or terminal.get("state") != "committed"
            or terminal.get("ok") is not True
            or not _text(terminal.get("receipt_id"))
        ):
            raise ValueError("VOCABULARY_APPLICATION_INVALID: canonical committed receipt required")
        versions = dict(_versions(resulting_versions))

        def update(section):
            _current(section, item)
            records = [
                record
                for record in section["decisions"].values()
                if record["item_ref"] == item.ref
                and (
                    (record.get("application") or {}).get("request_id")
                    == terminal.get("request_id")
                    or any(
                        entry.get("request_id") == terminal.get("request_id")
                        for entry in (record.get("application") or {}).get("operations", [])
                    )
                )
            ]
            record = records[0] if len(records) == 1 else None
            binding = record.get("application") if record else None
            operations = binding.get("operations") if isinstance(binding, Mapping) else None
            if isinstance(operations, list):
                operation = next(
                    (entry for entry in operations if entry.get("request_id") == terminal.get("request_id")),
                    None,
                )
                if not isinstance(operation, dict) or (
                    operation.get("expected_results") != versions
                    or operation.get("choice_digest") != _hash(applied_choice)
                    or applied_choice != record.get("choice")
                    or operation.get("state") not in {"bound", "committed"}
                ):
                    raise ValueError("VOCABULARY_APPLICATION_INVALID: receipt or resulting state does not match")
                receipt_id = terminal["receipt_id"]
                if operation.get("receipts") not in (None, [], [receipt_id]):
                    raise ValueError("VOCABULARY_APPLICATION_INVALID: receipt or resulting state does not match")
                operation.update(state="committed", receipts=[receipt_id])
                record["receipts"] = [
                    entry["receipts"][0] for entry in operations if entry.get("receipts")
                ]
                if operation.get("step") == "edge" and any(
                    entry.get("step") == "registration" and entry.get("state") == "committed"
                    for entry in operations
                ):
                    record.update(state="applied", resulting_versions=versions)
                else:
                    record["state"] = "applying"
                return _view(section, item.ref)
            if binding and _currency(item.to_dict()) not in (
                binding["reviewed_currency"],
                binding["result_currency"],
            ):
                raise ValueError("VOCABULARY_DECISION_STALE: state differs from the bound result")
            if (
                not binding
                or record["state"] not in {"applying", "applied"}
                or binding["request_id"] != terminal.get("request_id")
                or binding["expected_results"] != versions
                or applied_choice != record.get("choice")
                or binding["choice_digest"] != _hash(applied_choice)
                or record["receipts"] not in ([], [terminal["receipt_id"]])
            ):
                raise ValueError(
                    "VOCABULARY_APPLICATION_INVALID: receipt or resulting state does not match"
                )
            record.update(
                state="applied", receipts=[terminal["receipt_id"]], resulting_versions=versions
            )
            return _view(section, item.ref)

        return self._update(update)

    def bind_resulting_versions(
        self, item: WorkItem, *, operation_id: str, resulting_versions: Mapping[str, str]
    ) -> None:
        """Writer-only bridge from a reader-derived postcondition to the receipt seam."""
        versions = dict(_versions(resulting_versions))

        def update(section):
            current = _current_decision(section, item)
            record = section["decisions"].get(_key(item))
            binding = record.get("application") if record else None
            operations = binding.get("operations") if isinstance(binding, Mapping) else None
            if isinstance(operations, list):
                operation = next(
                    (entry for entry in operations if entry.get("operation_id") == operation_id), None
                )
                if not isinstance(operation, dict):
                    raise ValueError("VOCABULARY_APPLICATION_INVALID: application is not bound")
                operation["expected_results"] = versions
                operation["result_currency"] = _currency(
                    make_item(
                        family=item.family,
                        signal=item.signal,
                        targets={ref: versions.get(ref, version) for ref, version in item.target_versions},
                        evidence=item.evidence,
                        registry_hashes={key: versions.get(f"registry:{key}", version) for key, version in item.registry_hashes},
                        projection_status=item.projection_status,
                        question=item.question,
                        logical_identity=item.logical_identity,
                        projection_currency=dict(item.projection_currency),
                    ).to_dict()
                )
                if operation.get("step") == "edge":
                    binding["result_currency"] = operation["result_currency"]
                return
            if (
                current["state"] != "applying"
                or not isinstance(binding, dict)
                or binding.get("operation_id") != operation_id
            ):
                raise ValueError("VOCABULARY_APPLICATION_INVALID: application is not bound")
            binding["expected_results"] = versions
            binding["result_currency"] = _currency(
                make_item(
                    family=item.family,
                    signal=item.signal,
                    targets={
                        ref: versions.get(ref, version) for ref, version in item.target_versions
                    },
                    evidence=item.evidence,
                    registry_hashes={
                        key: versions.get(f"registry:{key}", version)
                        for key, version in item.registry_hashes
                    },
                    projection_status=item.projection_status,
                    question=item.question,
                    logical_identity=item.logical_identity,
                    projection_currency=dict(item.projection_currency),
                ).to_dict()
            )

        self._update(update, affected_refs=(item.ref,))

    def record_application_uncertain(self, item: WorkItem, *, operation_id: str) -> None:
        """Retain a post-commit correlation failure without changing the mutation."""
        def update(section):
            record = section["decisions"].get(_key(item))
            application = record.get("application") if record else None
            if not isinstance(application, dict):
                return
            if application.get("operation_id") == operation_id:
                application["state"] = "uncertain"
            else:
                operations = application.get("operations")
                operation = (
                    next((entry for entry in operations if entry.get("operation_id") == operation_id), None)
                    if isinstance(operations, list)
                    else None
                )
                if not isinstance(operation, dict):
                    return
                operation["state"] = "uncertain"
            record["state"] = "applying"

        self._update(update, affected_refs=(item.ref,))

    def begin_curation_application(
        self,
        item: WorkItem,
        *,
        request_id: str,
        operation_id: str,
        run_id: str,
        plan_id: str,
        plan_fingerprint: str,
        choice: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Bind one curation request to its immutable plan, never its payload."""
        if not all(
            _text(value)
            for value in (request_id, operation_id, run_id, plan_id, plan_fingerprint)
        ):
            raise ValueError("VOCABULARY_APPLICATION_INVALID: sealed curation identity required")
        curation = {
            "run_id": run_id,
            "plan_id": plan_id,
            "plan_fingerprint": plan_fingerprint,
            "choice_digest": _hash(choice),
        }

        def update(section):
            current = _current_decision(section, item)
            record = section["decisions"].get(_key(item))
            if not record or record.get("choice") != dict(choice):
                raise ValueError("VOCABULARY_APPLICATION_INVALID: curation must match reviewed meaning")
            if current["state"] not in {"proposed", "awaiting_approval", "applying"}:
                raise ValueError("VOCABULARY_APPLICATION_INVALID: a current proposal is required")
            application = record.get("application")
            if application is None:
                application = {"curation": curation, "operations": []}
                record.update(state="applying", application=application)
            elif application.get("curation") != curation:
                raise ValueError("VOCABULARY_APPLICATION_INVALID: curation plan is already bound")
            operations = application.setdefault("operations", [])
            if not any(entry.get("operation_id") == operation_id for entry in operations):
                operations.append(
                    {"operation_id": operation_id, "request_id": request_id, "state": "bound"}
                )
            return _view(section, item.ref)

        return self._update(update)

    def record_curation_receipts(
        self,
        item: WorkItem,
        *,
        operation_id: str,
        phase: str,
        receipts: list[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Mirror CurationStore's already-verified receipts and phase only."""
        if phase not in {"executing", "partial", "completed"}:
            raise ValueError("VOCABULARY_APPLICATION_INVALID: invalid curation result phase")
        if not all(isinstance(entry, Mapping) and _text(entry.get("operation_id")) for entry in receipts):
            raise ValueError("VOCABULARY_APPLICATION_INVALID: canonical curation receipts required")

        def update(section):
            _current(section, item)
            record = section["decisions"].get(_key(item))
            application = record.get("application") if record else None
            operations = application.get("operations") if isinstance(application, Mapping) else None
            if not isinstance(operations, list):
                raise ValueError("VOCABULARY_APPLICATION_INVALID: curation application is not bound")
            current = next((entry for entry in operations if entry.get("operation_id") == operation_id), None)
            if not isinstance(current, dict):
                raise ValueError("VOCABULARY_APPLICATION_INVALID: curation operation is not bound")
            receipt_ids = list(dict.fromkeys(str(entry["operation_id"]) for entry in receipts))
            current.update(state="committed" if operation_id in receipt_ids else "bound", receipts=receipt_ids)
            if phase == "completed":
                record.update(state="applied", receipts=receipt_ids)
            else:
                record["state"] = "applying"
            return _view(section, item.ref)

        return self._update(update)

    def page(
        self,
        *,
        limit: int = 4,
        continuation: str | None = None,
        visible: Callable[[dict[str, Any]], bool] | None = None,
        state: str = "open",
    ) -> dict[str, Any]:
        from . import vocabulary_review_index

        return vocabulary_review_index.page(
            self.store.vault_root,
            review_state_path=self.store.path,
            limit=limit,
            continuation=continuation,
            state=state,
            visible=visible,
        )

    def rebuild_review_projection(
        self, *, read_limit: int | None = None
    ) -> dict[str, Any]:
        """Explicit maintenance seam for the derived bounded review projection."""
        from . import vocabulary_review_index
        from .vocabulary_notifications import reconcile_legacy

        notifications = reconcile_legacy(self.store.vault_root)
        with review_state._LOCK:
            payload = self.store.load(
                read_limit=review_state.recovery_read_limit() if read_limit is None else read_limit
            )
            report = vocabulary_review_index.rebuild(
                self.store.vault_root,
                review_state_path=self.store.path,
                payload=payload,
            )
        report["notifications"] = notifications
        return report
