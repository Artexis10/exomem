"""Run disposable HTTP/OAuth Records acceptance and render unsigned facts.

This tool deliberately does not sign or publish evidence.  Its output is an
input to the operator-held promotion verifier after a deployed acceptance run.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from exomem import mutation_terminal

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_READ_ONLY_ACTIONS = frozenset({"describe", "validate", "inspect", "query"})
_MUTATING_ACTIONS = frozenset({"create", "append", "update", "revise", "rebaseline"})
_REQUIRED_ACTIONS = _READ_ONLY_ACTIONS | _MUTATING_ACTIONS
_MAX_HTTP_RESPONSE_BYTES = 1_048_576
_MAX_SSE_EVENTS = 64
_COMPACT_MUTATION_OUTER = frozenset(
    {
        "ok",
        "state",
        "terminal",
        "status",
        "mutated",
        "paths",
        "request_id",
        "receipt_id",
        "warnings_count",
    }
)
_COMPACT_MUTATION_OPTIONAL = frozenset(
    {
        "idempotency_key",
        "graph_sync",
        "graph_sync_code",
        "graph_sync_checkpoint",
        "graph_sync_remediation",
        # Present only when the leaf actually warned; absent on receipt recovery,
        # which reports a count with no retained texts.
        "warnings",
        # Advisory structural feedback on a compiled-note write. Absent unless the
        # written page shows recurring durable material outside its declared scope,
        # and never carried by a Records mutation.
        "structure_suggestion",
    }
)
_COMPACT_V1_RECEIPT = frozenset(
    {
        "operation",
        "collection_id",
        "item_key",
        "before_item_hash",
        "after_item_hash",
        "before_container_hash",
        "after_container_hash",
        "affected_paths",
        "payload_hash",
        "outcome",
        "audit_correlation",
    }
)
_COMPACT_V2_RECEIPT = frozenset(
    {
        "_record_receipt",
        "receipt_version",
        "operation",
        "collection_id",
        "item_key",
        "before_item_hash",
        "after_item_hash",
        "before_manifest_hash",
        "after_manifest_hash",
        "before_container_hash",
        "after_container_hash",
        "affected_paths",
        "payload_hash",
        "outcome",
        "audit_correlation",
        "continuity",
        "acknowledged_gap_codes",
        "gap_fingerprint",
        "checkpoint_snapshot_hash",
        "minimum_reader_version",
    }
)
_READ_MUTATION_INDICATORS = frozenset(
    {
        "_record_receipt",
        "receipt_version",
        "operation",
        "item_key",
        "before_item_hash",
        "after_item_hash",
        "before_manifest_hash",
        "after_manifest_hash",
        "before_container_hash",
        "after_container_hash",
        "affected_paths",
        "payload_hash",
        "outcome",
        "audit_correlation",
        "continuity",
        "acknowledged_gap_codes",
        "gap_fingerprint",
        "checkpoint_snapshot_hash",
        "minimum_reader_version",
        "request_id",
        "receipt_id",
        "idempotency_key",
        "terminal",
        "state",
        "warnings_count",
        "paths",
        "path",
    }
)
_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "deployment",
        "release",
        "surface",
        "run",
        "vault",
        "identity",
        "client_contracts",
        "actions",
        "mutations",
        "restart",
        "prompt_cases",
        "graph_availability",
    }
)


class RecordsEvidenceError(ValueError):
    """The content-free, closed Records evidence contract was not satisfied."""


class RecordsEvidenceExpectation:
    """Trusted candidate bindings supplied by the promotion operator, never evidence."""

    def __init__(
        self,
        *,
        deployment_sha256: str,
        package: str,
        release_version: str,
        profile: str | None = None,
        minimum_records_reader_version: int | None = None,
        surface_digest: str,
        vault_purpose: str,
        reset_epoch: str,
        principal_hmac_sha256: str,
        audience_hmac_sha256: str,
        client_contracts: Mapping[str, tuple[str, str, str, str]],
        required_actions: frozenset[str],
        required_prompt_cases: Mapping[str, str],
        required_prompt_case_results: Mapping[str, tuple[str, str, str, bool]] | None = None,
        graph_proof_digest: str,
    ) -> None:
        self.deployment_sha256 = deployment_sha256
        self.package = package
        self.release_version = release_version
        self.profile = profile
        self.minimum_records_reader_version = minimum_records_reader_version
        self.surface_digest = surface_digest
        self.vault_purpose = vault_purpose
        self.reset_epoch = reset_epoch
        self.principal_hmac_sha256 = principal_hmac_sha256
        self.audience_hmac_sha256 = audience_hmac_sha256
        self.client_contracts = dict(client_contracts)
        self.required_actions = required_actions
        self.required_prompt_cases = dict(required_prompt_cases)
        self.required_prompt_case_results = (
            dict(required_prompt_case_results) if required_prompt_case_results is not None else None
        )
        self.graph_proof_digest = graph_proof_digest


class RecordsAcceptanceLedger:
    """In-memory idempotency seam for the promotion verifier's signed replay path."""

    def __init__(self) -> None:
        self._accepted: dict[str, tuple[str, dict[str, Any]]] = {}

    def accept(
        self,
        *,
        candidate_digest: str,
        envelope: Mapping[str, Any],
        expected: RecordsEvidenceExpectation,
        operator_secret: str,
        now: str | datetime,
    ) -> dict[str, Any]:
        _require_digest(candidate_digest, "candidate digest")
        evidence = validate_operator_signed_records_evidence(
            envelope,
            expected=expected,
            operator_secret=operator_secret,
            now=now,
        )
        evidence_digest = hashlib.sha256(evidence).hexdigest()
        known = self._accepted.get(evidence_digest)
        if known is not None:
            previous_candidate, result = known
            if not hmac.compare_digest(previous_candidate, candidate_digest):
                raise RecordsEvidenceError("evidence was accepted for a different candidate")
            return result
        result = {
            "accepted": True,
            "candidate_digest": candidate_digest,
            "evidence_digest": evidence_digest,
        }
        self._accepted[evidence_digest] = (candidate_digest, result)
        return result


class RecordsLiveTransport(Protocol):
    def reset(self, *, purpose: str, timeout_seconds: float) -> Mapping[str, Any]: ...

    def invoke(
        self, *, action: str, timeout_seconds: float, arguments: Mapping[str, Any] | None = None
    ) -> Mapping[str, Any]: ...

    def readback(self, *, collection: str | None = None, timeout_seconds: float) -> Mapping[str, Any]: ...

    def direct_edit(self, *, collection: str, timeout_seconds: float) -> Mapping[str, Any]: ...

    def precreate(self, *, manifest_path: str, timeout_seconds: float) -> Mapping[str, Any]: ...

    def restart(self, *, timeout_seconds: float) -> Mapping[str, Any]: ...


class HttpOAuthRecordsTransport:
    """Small, concrete HTTP/OAuth transport; endpoint paths remain deploy-configurable."""

    def __init__(
        self,
        endpoint: str,
        bearer_token: str,
        *,
        reset_path: str | None = None,
        invoke_path: str = "/mcp",
        restart_path: str | None = None,
        direct_edit_path: str | None = None,
        precreate_path: str | None = None,
        action_requests: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        if not endpoint.startswith("https://"):
            raise RecordsEvidenceError("live acceptance endpoint must use HTTPS")
        if not bearer_token:
            raise RecordsEvidenceError("OAuth bearer token is required")
        self._endpoint = endpoint.rstrip("/")
        self._bearer_token = bearer_token
        self._reset_path = reset_path
        self._invoke_path = invoke_path
        self._restart_path = restart_path
        self._direct_edit_path = direct_edit_path
        self._precreate_path = precreate_path
        self._action_requests = {key: dict(value) for key, value in (action_requests or {}).items()}

    def reset(self, *, purpose: str, timeout_seconds: float) -> Mapping[str, Any]:
        return self._control_post(self._reset_path, {"purpose": purpose}, timeout_seconds, "reset")

    def invoke(
        self, *, action: str, timeout_seconds: float, arguments: Mapping[str, Any] | None = None
    ) -> Mapping[str, Any]:
        request_arguments = dict(self._action_requests.get(action, {}))
        request_arguments.update(arguments or {})
        request_arguments.setdefault("action", action)
        return self._mcp_call("record_memory", request_arguments, timeout_seconds)

    def readback(self, *, collection: str | None = None, timeout_seconds: float) -> Mapping[str, Any]:
        arguments: dict[str, Any] = {"action": "inspect"}
        if collection is not None:
            arguments["collection"] = collection
        return self._mcp_call("record_memory", arguments, timeout_seconds)

    def direct_edit(self, *, collection: str, timeout_seconds: float) -> Mapping[str, Any]:
        return self._control_post(
            self._direct_edit_path, {"collection": collection}, timeout_seconds, "direct-edit"
        )

    def precreate(self, *, manifest_path: str, timeout_seconds: float) -> Mapping[str, Any]:
        return self._control_post(
            self._precreate_path,
            {"manifest_path": manifest_path},
            timeout_seconds,
            "pre-create state",
        )

    def restart(self, *, timeout_seconds: float) -> Mapping[str, Any]:
        return self._control_post(self._restart_path, {}, timeout_seconds, "restart")

    def _control_post(
        self, path: str | None, payload: Mapping[str, Any], timeout_seconds: float, operation: str
    ) -> Mapping[str, Any]:
        if not path:
            raise RecordsEvidenceError(f"live acceptance {operation} control endpoint is required")
        return self._post(path, payload, timeout_seconds)

    def _mcp_call(
        self, name: str, arguments: Mapping[str, Any], timeout_seconds: float
    ) -> Mapping[str, Any]:
        response = self._post(
            self._invoke_path,
            {
                "jsonrpc": "2.0",
                "id": secrets.token_hex(12),
                "method": "tools/call",
                "params": {"name": name, "arguments": dict(arguments)},
            },
            timeout_seconds,
        )
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise RecordsEvidenceError("live MCP response has no result object")
        if result.get("isError") is True:
            raise RecordsEvidenceError("live MCP tool call returned an error")
        structured = result.get("structuredContent")
        if isinstance(structured, Mapping):
            return structured
        content = result.get("content")
        if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], Mapping):
            raise RecordsEvidenceError("live MCP response has no domain result")
        item = content[0]
        if item.get("type") != "text" or not isinstance(item.get("text"), str):
            raise RecordsEvidenceError("live MCP response has no JSON domain result")
        try:
            decoded = json.loads(item["text"])
        except json.JSONDecodeError as exc:
            raise RecordsEvidenceError("live MCP response has invalid JSON domain result") from exc
        if not isinstance(decoded, Mapping):
            raise RecordsEvidenceError("live MCP response domain result must be an object")
        return decoded

    def _post(self, path: str, payload: Mapping[str, Any], timeout_seconds: float) -> Mapping[str, Any]:
        request = Request(
            self._endpoint + path,
            data=canonical_json(payload),
            headers={
                "Authorization": f"Bearer {self._bearer_token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:  # nosec B310: HTTPS is enforced above
                body = response.read(_MAX_HTTP_RESPONSE_BYTES + 1)
                content_type = _content_type(response)
        except (HTTPError, URLError, TimeoutError) as exc:
            raise RecordsEvidenceError(f"live HTTP/OAuth request failed: {type(exc).__name__}") from exc
        if len(body) > _MAX_HTTP_RESPONSE_BYTES:
            raise RecordsEvidenceError("live HTTP/OAuth response exceeds size limit")
        expected_id = payload.get("id") if payload.get("jsonrpc") == "2.0" else None
        if expected_id is not None and not isinstance(expected_id, str):
            raise RecordsEvidenceError("live MCP request id must be a string")
        return _decode_http_response(body, content_type, expected_id)


def _content_type(response: Any) -> str:
    headers = getattr(response, "headers", None)
    if headers is None:
        raise RecordsEvidenceError("live HTTP/OAuth response has no Content-Type")
    if hasattr(headers, "get_content_type"):
        value = headers.get_content_type()
    else:
        value = headers.get("Content-Type")
    if not isinstance(value, str):
        raise RecordsEvidenceError("live HTTP/OAuth response has no Content-Type")
    return value.split(";", 1)[0].strip().lower()


def _decode_http_response(body: bytes, content_type: str, expected_id: str | None) -> Mapping[str, Any]:
    if content_type == "application/json":
        return _decode_json_rpc_envelope(_decode_json_object(body), expected_id)
    if content_type != "text/event-stream":
        raise RecordsEvidenceError("live HTTP/OAuth response has unsupported Content-Type")
    return _decode_sse_json_rpc_envelope(body, expected_id)


def _decode_json_object(body: bytes) -> Mapping[str, Any]:
    try:
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecordsEvidenceError(f"live HTTP/OAuth response is not JSON: {type(exc).__name__}") from exc
    if not isinstance(decoded, Mapping):
        raise RecordsEvidenceError("live HTTP/OAuth response must be an object")
    return decoded


def _decode_json_rpc_envelope(decoded: Mapping[str, Any], expected_id: str | None) -> Mapping[str, Any]:
    if expected_id is None:
        return decoded
    if decoded.get("jsonrpc") != "2.0" or decoded.get("id") != expected_id:
        raise RecordsEvidenceError("live MCP response does not match request id")
    if "error" in decoded:
        raise RecordsEvidenceError("live MCP response contains an error")
    if not isinstance(decoded.get("result"), Mapping):
        raise RecordsEvidenceError("live MCP response has no result object")
    return decoded


def _decode_sse_json_rpc_envelope(body: bytes, expected_id: str | None) -> Mapping[str, Any]:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RecordsEvidenceError("live SSE response is not UTF-8") from exc
    if not (text.endswith("\n\n") or text.endswith("\r\n\r\n")):
        raise RecordsEvidenceError("live SSE response is truncated")

    events = re.split(r"\r?\n\r?\n", text)[:-1]
    if len(events) > _MAX_SSE_EVENTS:
        raise RecordsEvidenceError("live SSE response exceeds event limit")
    envelopes: list[Mapping[str, Any]] = []
    for event in events:
        data: list[str] = []
        for line in event.splitlines():
            if line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
            elif line.startswith(("event:", "id:", "retry:", ":")):
                continue
            else:
                raise RecordsEvidenceError("live SSE response has malformed framing")
        if data:
            envelopes.append(_decode_json_rpc_envelope(_decode_json_object("\n".join(data).encode()), expected_id))
    if len(envelopes) != 1:
        raise RecordsEvidenceError("live SSE response must contain exactly one JSON-RPC response")
    return envelopes[0]


class RecordsLiveAcceptanceRunner:
    """Run a deterministic reset/action/readback sequence through any live transport."""

    def __init__(
        self,
        transport: RecordsLiveTransport,
        *,
        timeout_seconds: float,
        expected_actions: Sequence[str],
        reset_purpose: str,
        expected_reset_epoch: str | None = None,
    ) -> None:
        if not 0 < timeout_seconds <= 60:
            raise RecordsEvidenceError("timeout must be between 0 and 60 seconds")
        if not reset_purpose or not expected_actions or len(set(expected_actions)) != len(expected_actions):
            raise RecordsEvidenceError("acceptance actions and reset purpose must be unique and non-empty")
        self._transport = transport
        self._timeout_seconds = timeout_seconds
        self._expected_actions = tuple(expected_actions)
        self._reset_purpose = reset_purpose
        self._expected_reset_epoch = expected_reset_epoch

    def run(self) -> dict[str, Any]:
        if set(self._expected_actions) == _REQUIRED_ACTIONS:
            return self._run_full_lifecycle()
        reset = self._transport.reset(
            purpose=self._reset_purpose, timeout_seconds=self._timeout_seconds
        )
        epoch = _string(reset.get("reset_epoch"), "reset epoch")
        if self._expected_reset_epoch is not None and not hmac.compare_digest(
            epoch, self._expected_reset_epoch
        ):
            raise RecordsEvidenceError("live reset epoch does not match the expected disposable vault")

        actions: list[dict[str, str]] = []
        mutations: list[dict[str, str]] = []
        for action in self._expected_actions:
            # The public selector identifies read-only actions.  Capture state
            # before every other action so a committed terminal has independent
            # before/after HTTP readback rather than a client-reported claim.
            before = (
                None
                if action in _READ_ONLY_ACTIONS
                else _readback_digest(self._transport.readback(timeout_seconds=self._timeout_seconds))
            )
            response = self._transport.invoke(action=action, timeout_seconds=self._timeout_seconds)
            outcome = _validated_action_terminal(action, response)
            if action in _MUTATING_ACTIONS:
                if before is None:
                    raise RecordsEvidenceError(f"mutating action {action} has no before readback")
                after = _readback_digest(self._transport.readback(timeout_seconds=self._timeout_seconds))
                request_id = _string(response.get("request_id"), f"{action} request id")
                receipt_id = _string(response.get("receipt_id"), f"{action} receipt id")
                if hmac.compare_digest(before, after):
                    raise RecordsEvidenceError(f"{action} has no independently observable readback change")
                mutations.append(
                    {
                        "action": action,
                        "request_id": request_id,
                        "receipt_id": receipt_id,
                        "terminal_outcome": "committed",
                        "before_readback_sha256": before,
                        "after_readback_sha256": after,
                    }
                )
            actions.append({"action": action, "outcome": outcome})
        restart = self._transport.restart(timeout_seconds=self._timeout_seconds)
        if not _completed_control_response(restart):
            raise RecordsEvidenceError("live restart did not complete")
        return {
            "vault": {"purpose": self._reset_purpose, "reset_epoch": epoch},
            "actions": actions,
            "mutations": mutations,
            "restart": {
                "outcome": "completed",
                "readback_sha256": _readback_digest(
                    self._transport.readback(timeout_seconds=self._timeout_seconds)
                ),
            },
        }

    def _run_full_lifecycle(self) -> dict[str, Any]:
        reset = self._transport.reset(
            purpose=self._reset_purpose, timeout_seconds=self._timeout_seconds
        )
        epoch = _string(reset.get("reset_epoch"), "reset epoch")
        if self._expected_reset_epoch is not None and not hmac.compare_digest(epoch, self._expected_reset_epoch):
            raise RecordsEvidenceError("live reset epoch does not match the expected disposable vault")

        actions: list[dict[str, str]] = []
        mutations: list[dict[str, str]] = []

        describe = self._invoke_read("describe", actions)
        example = _object(_object(describe.get("examples"), "describe examples").get("minimal"), "minimal example")
        manifest_path = _string(example.get("manifest_path"), "minimal manifest path")
        manifest_text = _string(example.get("manifest_text"), "minimal manifest text")
        self._invoke_read(
            "validate",
            actions,
            {"manifest_path": manifest_path, "manifest_text": manifest_text, "scaffold": True},
        )

        before_create = _absence_digest(
            self._transport.precreate(manifest_path=manifest_path, timeout_seconds=self._timeout_seconds),
            manifest_path,
        )
        self._invoke_mutation(
            "create",
            actions,
            mutations,
            before_create,
            {"manifest_path": manifest_path, "manifest_text": manifest_text, "scaffold": True},
            readback_collection=manifest_path,
        )
        state = self._targeted_state(manifest_path)
        collection = _collection_id(state)
        self._invoke_read("inspect", actions, {"collection": collection})

        before_append = _readback_digest(state)
        append = self._invoke_mutation(
            "append",
            actions,
            mutations,
            before_append,
            {
                "collection": collection,
                "item": example.get("append_item"),
                "expected_container_hash": _lifecycle_guards(state)["expected_container_hash"],
            },
        )
        append_receipt = _terminal_receipt(append)
        item_key = _string(append_receipt.get("item_key"), "appended item key")
        state = self._targeted_state(collection)
        self._invoke_read("query", actions, {"collection": collection})
        current_item_version = _string(append_receipt.get("after_item_hash"), "appended item version")
        before_update = _readback_digest(state)
        self._invoke_mutation(
            "update",
            actions,
            mutations,
            before_update,
            {
                "collection": collection,
                "item_key": item_key,
                "expected_container_hash": _lifecycle_guards(state)["expected_container_hash"],
                "expected_item_version": current_item_version,
            },
        )
        state = self._targeted_state(collection)
        self._invoke_read(
            "validate",
            actions,
            {"collection": collection, "manifest_text": manifest_text},
            record=False,
        )
        before_revise = _readback_digest(state)
        self._invoke_mutation(
            "revise",
            actions,
            mutations,
            before_revise,
            {
                "collection": collection,
                "manifest_text": manifest_text,
                "expected_manifest_hash": _lifecycle_guards(state)["expected_manifest_hash"],
                "expected_container_hash": _lifecycle_guards(state)["expected_container_hash"],
            },
        )
        state = self._targeted_state(collection)
        drift = self._transport.direct_edit(collection=collection, timeout_seconds=self._timeout_seconds)
        if drift.get("status") != "committed" or drift.get("mutated") is not True:
            raise RecordsEvidenceError("live direct-edit control did not create Records drift")
        drifted = self._targeted_state(collection)
        self._invoke_read("inspect", actions, {"collection": collection}, record=False)
        gap_codes = _gap_codes(drifted)
        if not gap_codes:
            raise RecordsEvidenceError("live direct-edit control did not expose Records gaps")
        before_rebaseline = _readback_digest(drifted)
        self._invoke_mutation(
            "rebaseline",
            actions,
            mutations,
            before_rebaseline,
            {
                "collection": collection,
                "expected_manifest_hash": _lifecycle_guards(drifted)["expected_manifest_hash"],
                "expected_container_hash": _lifecycle_guards(drifted)["expected_container_hash"],
                "acknowledged_gap_codes": gap_codes,
            },
        )
        restart = self._transport.restart(timeout_seconds=self._timeout_seconds)
        if not _completed_control_response(restart):
            raise RecordsEvidenceError("live restart did not complete")
        return {
            "vault": {"purpose": self._reset_purpose, "reset_epoch": epoch},
            "actions": actions,
            "mutations": mutations,
            "restart": {
                "outcome": "completed",
                "readback_sha256": _readback_digest(self._targeted_state(collection)),
            },
        }

    def _invoke_read(
        self,
        action: str,
        actions: list[dict[str, str]],
        arguments: Mapping[str, Any] | None = None,
        *,
        record: bool = True,
    ) -> Mapping[str, Any]:
        response = self._transport.invoke(
            action=action, timeout_seconds=self._timeout_seconds, arguments=arguments
        )
        _validated_action_terminal(action, response)
        if record:
            actions.append({"action": action, "outcome": "completed"})
        return response

    def _invoke_mutation(
        self,
        action: str,
        actions: list[dict[str, str]],
        mutations: list[dict[str, str]],
        before: str,
        arguments: Mapping[str, Any],
        *,
        readback_collection: str | None = None,
    ) -> Mapping[str, Any]:
        response = self._transport.invoke(
            action=action, timeout_seconds=self._timeout_seconds, arguments=arguments
        )
        _validated_action_terminal(action, response)
        collection = readback_collection or arguments.get("collection")
        after = _readback_digest(
            self._transport.readback(
                collection=collection if isinstance(collection, str) else None,
                timeout_seconds=self._timeout_seconds,
            )
        )
        if hmac.compare_digest(before, after):
            raise RecordsEvidenceError(f"{action} has no independently observable readback change")
        actions.append({"action": action, "outcome": "committed"})
        mutations.append(
            {
                "action": action,
                "request_id": _string(response.get("request_id"), f"{action} request id"),
                "receipt_id": _string(response.get("receipt_id"), f"{action} receipt id"),
                "terminal_outcome": "committed",
                "before_readback_sha256": before,
                "after_readback_sha256": after,
            }
        )
        return response

    def _targeted_state(self, collection: str) -> Mapping[str, Any]:
        state = self._transport.readback(collection=collection, timeout_seconds=self._timeout_seconds)
        _readback_digest(state)
        _collection_id(state)
        _lifecycle_guards(state)
        return state


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _validated_action_terminal(action: str, response: Mapping[str, Any]) -> str:
    """Validate real record_memory domain data; evidence terminals are derived here."""

    _reject_unsuccessful_response(response, action)
    if action in _READ_ONLY_ACTIONS:
        _reject_read_mutation_terminal(response, action)
        if action == "describe":
            examples = response.get("examples")
            if (
                response.get("contract_version") != 1
                or response.get("records_action_constraint") != {"semantic_profile": "records"}
                or not isinstance(examples, Mapping)
                or not isinstance(examples.get("minimal"), Mapping)
                or set(examples["minimal"]) != {"manifest_path", "manifest_text", "append_item"}
            ):
                raise RecordsEvidenceError("describe did not return the Records contract")
        elif action == "validate":
            if response.get("valid") is not True or not isinstance(response.get("normalized_contract"), Mapping):
                raise RecordsEvidenceError("validate did not return a valid Records contract")
        elif action == "inspect":
            try:
                _collection_id(response)
                _lifecycle_guards(response)
                _readback_digest(response)
            except RecordsEvidenceError as exc:
                raise RecordsEvidenceError("inspect did not return Records state") from exc
            if response.get("kind") != "collection" or response.get("report_only") is not True:
                raise RecordsEvidenceError("inspect did not return Records state")
        elif action == "query":
            if (
                not isinstance(response.get("rows"), list)
                or response.get("derived") is not True
                or type(response.get("returned")) is not int
                or response["returned"] != len(response["rows"])
                or type(response.get("total_matched")) is not int
                or type(response.get("truncated")) is not bool
                or not isinstance(response.get("collection_id"), str)
                or not _DIGEST.fullmatch(str(response.get("snapshot", "")))
            ):
                raise RecordsEvidenceError("query did not return projected Records rows")
        return "completed"
    _validate_compact_records_mutation(action, response)
    return "committed"


def _reject_unsuccessful_response(response: Mapping[str, Any], action: str) -> None:
    if (
        response.get("ok") is False
        or "error" in response
        or response.get("withheld") is True
        or response.get("valid") is False
        or response.get("status") in {"failed", "rejected", "replayed"}
        or response.get("isError") is True
    ):
        raise RecordsEvidenceError(f"{action} response is unsuccessful")


def _reject_read_mutation_terminal(response: Mapping[str, Any], action: str) -> None:
    if (
        response.get("status") in {"committed", "replayed", "failed"}
        or "mutated" in response
        or bool(set(response) & _READ_MUTATION_INDICATORS)
    ):
        raise RecordsEvidenceError(f"{action} response contains a mutation terminal")


def _validate_compact_records_mutation(action: str, response: Mapping[str, Any]) -> None:
    receipt_fields = _COMPACT_V1_RECEIPT if action in {"create", "append", "update"} else _COMPACT_V2_RECEIPT
    required = _COMPACT_MUTATION_OUTER | receipt_fields
    _closed_compact_object(
        response,
        required=required,
        allowed=required | _COMPACT_MUTATION_OPTIONAL,
        name=f"{action} compact Records mutation",
    )
    if (
        response["ok"] is not True
        or response["state"] != "committed"
        or response["terminal"] is not True
        or response["status"] != "committed"
        or response["mutated"] is not True
        or response["operation"] != action
        or response["outcome"] != "committed"
    ):
        raise RecordsEvidenceError(f"{action} did not commit a Records mutation")
    _string(response["request_id"], f"{action} request id")
    _string(response["receipt_id"], f"{action} receipt id")
    if "idempotency_key" in response:
        _string(response["idempotency_key"], f"{action} idempotency key")
    if type(response["warnings_count"]) is not int or response["warnings_count"] < 0:
        raise RecordsEvidenceError(f"{action} warnings count is invalid")
    paths = response["paths"]
    affected_paths = response["affected_paths"]
    if (
        not isinstance(paths, list)
        or not 1 <= len(paths) <= 16
        or not all(isinstance(path, str) and 0 < len(path) <= 1024 for path in paths)
        or paths != affected_paths
    ):
        raise RecordsEvidenceError(f"{action} compact paths are invalid")
    _validate_compact_graph_outcome(action, response)
    receipt = _terminal_receipt(response)
    if action in {"create", "append", "update"}:
        if not _valid_compact_v1_records_receipt(receipt, action):
            raise RecordsEvidenceError(f"{action} did not return a valid compact Records receipt")
    elif not mutation_terminal.valid_record_receipt(receipt):
        raise RecordsEvidenceError(f"{action} did not return a valid Records receipt")


def _closed_compact_object(
    value: Mapping[str, Any], *, required: frozenset[str], allowed: frozenset[str], name: str
) -> None:
    fields = set(value)
    missing = sorted(required - fields)
    unknown = sorted(fields - allowed)
    if missing or unknown:
        parts = []
        if missing:
            parts.append("missing fields: " + ", ".join(missing))
        if unknown:
            parts.append("unknown fields: " + ", ".join(unknown))
        raise RecordsEvidenceError(f"{name} has " + "; ".join(parts))


def _validate_compact_graph_outcome(action: str, response: Mapping[str, Any]) -> None:
    graph_fields = {"graph_sync_code", "graph_sync_checkpoint", "graph_sync_remediation"}
    present = set(response) & ({"graph_sync"} | graph_fields)
    if not present:
        return
    # `pending` is the fourth outcome (#576/#588): the canonical bytes are
    # committed and the registered derived-graph rebuild has not converged yet.
    # This script is not in CI, so a stale vocabulary here fails only when
    # someone runs it against a real build -- exactly the gap that let the
    # bounded write look like an acceptance regression.
    if response.get("graph_sync") not in mutation_terminal.GRAPH_SYNC_OUTCOMES:
        raise RecordsEvidenceError(f"{action} graph outcome is invalid")
    if "graph_sync_code" in response:
        _string(response["graph_sync_code"], f"{action} graph code")
    if "graph_sync_checkpoint" in response:
        _require_digest(response["graph_sync_checkpoint"], f"{action} graph checkpoint")
    if "graph_sync_remediation" in response:
        _string(response["graph_sync_remediation"], f"{action} graph remediation")


def _valid_compact_v1_records_receipt(receipt: Mapping[str, Any], action: str) -> bool:
    if set(receipt) != _COMPACT_V1_RECEIPT or receipt.get("operation") != action:
        return False
    if not _normalized_uuid(receipt.get("collection_id")) or receipt.get("outcome") != "committed":
        return False
    paths = receipt.get("affected_paths")
    correlation = receipt.get("audit_correlation")
    if not (
        isinstance(paths, list)
        and 1 <= len(paths) <= 16
        and all(isinstance(path, str) and 0 < len(path) <= 1024 for path in paths)
        and isinstance(correlation, str)
        and bool(re.fullmatch(r"[0-9a-f]{24}", correlation))
    ):
        return False
    if action == "create":
        return (
            receipt.get("item_key") is None
            and receipt.get("before_item_hash") is None
            and (receipt.get("after_item_hash") is None or _is_digest(receipt.get("after_item_hash")))
            and receipt.get("before_container_hash") is None
            and receipt.get("payload_hash") is None
            and _is_digest(receipt.get("after_container_hash"))
        )
    if not _normalized_uuid(receipt.get("item_key")):
        return False
    if action == "append":
        return (
            receipt.get("before_item_hash") is None
            and _is_digest(receipt.get("after_item_hash"))
            and _is_digest(receipt.get("before_container_hash"))
            and _is_digest(receipt.get("after_container_hash"))
            and _is_digest(receipt.get("payload_hash"))
        )
    return (
        _is_digest(receipt.get("before_item_hash"))
        and _is_digest(receipt.get("after_item_hash"))
        and _is_digest(receipt.get("before_container_hash"))
        and _is_digest(receipt.get("after_container_hash"))
        and receipt.get("payload_hash") is None
    )


def _normalized_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _is_digest(value: Any) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _terminal_receipt(response: Mapping[str, Any]) -> Mapping[str, Any]:
    leaf = response.get("leaf_result")
    if isinstance(leaf, Mapping):
        return leaf
    fields = {
        "_record_receipt", "receipt_version", "operation", "collection_id", "item_key",
        "before_item_hash", "after_item_hash", "before_manifest_hash", "after_manifest_hash",
        "before_container_hash", "after_container_hash", "affected_paths", "payload_hash",
        "outcome", "audit_correlation", "continuity", "acknowledged_gap_codes",
        "gap_fingerprint", "checkpoint_snapshot_hash", "minimum_reader_version",
    }
    return {key: response[key] for key in fields if key in response}


def _completed_control_response(response: Mapping[str, Any]) -> bool:
    return response.get("outcome") == "completed" or (
        response.get("status") in {"committed", "replayed"} and response.get("mutated") is False
    )


def validate_records_live_evidence(
    evidence: Mapping[str, Any], *, expected: RecordsEvidenceExpectation, now: str | datetime,
    require_fresh: bool = True,
) -> bytes:
    """Validate only structured, content-free unsigned facts against trusted bindings."""

    _closed_object(evidence, _TOP_LEVEL_FIELDS, "evidence")
    if evidence["schema_version"] != 1:
        raise RecordsEvidenceError("unsupported Records evidence schema version")
    _validate_binding(evidence, expected, _as_datetime(now), require_fresh=require_fresh)
    return canonical_json(evidence)


def validate_operator_signed_records_evidence(
    envelope: Mapping[str, Any],
    *,
    expected: RecordsEvidenceExpectation,
    operator_secret: str,
    now: str | datetime,
) -> bytes:
    """Verify an operator-held signature around live runner facts without minting one."""

    _closed_object(envelope, {"facts", "operator_signature"}, "signed Records evidence")
    facts = _object(envelope["facts"], "signed Records facts")
    signature = envelope["operator_signature"]
    _require_digest(signature, "operator signature")
    if not operator_secret:
        raise RecordsEvidenceError("operator trust configuration is required")
    expected_signature = hmac.new(
        operator_secret.encode("utf-8"), canonical_json(facts), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature, expected_signature):
        raise RecordsEvidenceError("operator signature does not verify")
    validate_records_live_evidence(facts, expected=expected, now=now)
    return canonical_json(envelope)


def _validate_binding(
    evidence: Mapping[str, Any], expected: RecordsEvidenceExpectation, now: datetime, *, require_fresh: bool
) -> None:
    deployment = _object(evidence["deployment"], "deployment")
    _closed_object(deployment, {"sha256"}, "deployment")
    _same_digest(deployment["sha256"], expected.deployment_sha256, "deployment")
    release = _object(evidence["release"], "release")
    release_fields = {"package", "version"}
    if expected.profile is not None:
        release_fields |= {"profile", "minimum_records_reader_version"}
    _closed_object(release, release_fields, "release")
    _same_string(release["package"], expected.package, "release package")
    _same_string(release["version"], expected.release_version, "release version")
    if expected.profile is not None:
        _same_string(release["profile"], expected.profile, "release profile")
        if release["minimum_records_reader_version"] != expected.minimum_records_reader_version:
            raise RecordsEvidenceError("Records evidence reader floor does not match the candidate")
    surface = _object(evidence["surface"], "surface")
    _closed_object(surface, {"mcp_digest"}, "surface")
    _same_digest(surface["mcp_digest"], expected.surface_digest, "surface")
    run = _object(evidence["run"], "run")
    _closed_object(run, {"nonce", "timestamp", "expires_at"}, "run")
    nonce = _string(run["nonce"], "run nonce")
    if len(nonce) < 16:
        raise RecordsEvidenceError("run nonce is too short")
    timestamp = _as_datetime(run["timestamp"])
    expiry = _as_datetime(run["expires_at"])
    if expiry <= timestamp or (require_fresh and (timestamp > now or expiry <= now)):
        raise RecordsEvidenceError("Records evidence is expired or has an invalid lifetime")
    vault = _object(evidence["vault"], "vault")
    _closed_object(vault, {"purpose", "reset_epoch"}, "vault")
    _same_string(vault["purpose"], expected.vault_purpose, "vault purpose")
    _same_string(vault["reset_epoch"], expected.reset_epoch, "reset epoch")
    identity = _object(evidence["identity"], "identity")
    _closed_object(identity, {"principal_hmac_sha256", "audience_hmac_sha256"}, "identity")
    _same_digest(identity["principal_hmac_sha256"], expected.principal_hmac_sha256, "principal")
    _same_digest(identity["audience_hmac_sha256"], expected.audience_hmac_sha256, "audience")
    contracts = _object(evidence["client_contracts"], "client contracts")
    if set(contracts) != set(expected.client_contracts):
        raise RecordsEvidenceError("Records evidence client contracts do not match the candidate")
    for client, expected_contract in expected.client_contracts.items():
        contract = _object(contracts[client], "client contract")
        _closed_object(
            contract,
            {"client", "client_version", "model_version", "system_contract_version"},
            "client contract",
        )
        if tuple(contract[key] for key in ("client", "client_version", "model_version", "system_contract_version")) != expected_contract:
            raise RecordsEvidenceError("Records evidence client contracts do not match the candidate")
    _validate_actions(evidence["actions"], expected.required_actions)
    _validate_mutations(evidence["mutations"], expected.required_actions)
    restart = _object(evidence["restart"], "restart")
    _closed_object(restart, {"outcome", "readback_sha256"}, "restart")
    if restart["outcome"] != "completed":
        raise RecordsEvidenceError("restart did not complete")
    _require_digest(restart["readback_sha256"], "restart readback")
    _validate_prompt_cases(
        evidence["prompt_cases"], expected.required_prompt_cases, expected.required_prompt_case_results
    )
    graph = _object(evidence["graph_availability"], "graph availability")
    _closed_object(graph, {"proof_digest"}, "graph availability")
    _same_digest(graph["proof_digest"], expected.graph_proof_digest, "graph availability")


def _validate_actions(value: Any, expected: frozenset[str]) -> None:
    if not isinstance(value, list) or not value:
        raise RecordsEvidenceError("actions must be a non-empty list")
    actual: set[str] = set()
    for item in value:
        row = _object(item, "action")
        _closed_object(row, {"action", "outcome"}, "action")
        action = _string(row["action"], "action name")
        required_outcome = "completed" if action in _READ_ONLY_ACTIONS else "committed"
        if row["outcome"] != required_outcome or action in actual:
            raise RecordsEvidenceError("actions must have exact action terminals")
        actual.add(action)
    if actual != expected:
        raise RecordsEvidenceError("Records evidence does not contain the required actions exactly")


def _validate_mutations(value: Any, required_actions: frozenset[str]) -> None:
    if not isinstance(value, list):
        raise RecordsEvidenceError("mutations must be a list")
    committed = required_actions & _MUTATING_ACTIONS
    actual: set[str] = set()
    request_ids: set[str] = set()
    receipt_ids: set[str] = set()
    for item in value:
        row = _object(item, "mutation")
        _closed_object(row, {"action", "request_id", "receipt_id", "terminal_outcome", "before_readback_sha256", "after_readback_sha256"}, "mutation")
        action = _string(row["action"], "mutation action")
        request_id = _string(row["request_id"], "mutation request id")
        receipt_id = _string(row["receipt_id"], "mutation receipt id")
        if action not in committed or action in actual or request_id in request_ids or receipt_id in receipt_ids:
            raise RecordsEvidenceError("mutation request/receipt correlation is invalid")
        if row["terminal_outcome"] != "committed":
            raise RecordsEvidenceError("mutation terminal outcome must be committed")
        before = row["before_readback_sha256"]
        after = row["after_readback_sha256"]
        _require_digest(before, "before readback")
        _require_digest(after, "after readback")
        if hmac.compare_digest(before, after):
            raise RecordsEvidenceError("mutation readback does not prove a state change")
        actual.add(action)
        request_ids.add(request_id)
        receipt_ids.add(receipt_id)
    if actual != committed:
        raise RecordsEvidenceError("every committed action requires independent readback")


def _validate_prompt_cases(
    value: Any,
    expected: Mapping[str, str],
    required_results: Mapping[str, tuple[str, str, str, bool]] | None,
) -> None:
    if not isinstance(value, list):
        raise RecordsEvidenceError("prompt cases must be a list")
    actual: dict[str, str] = {}
    for item in value:
        row = _object(item, "prompt case")
        fields = {"id", "sha256"}
        if required_results is not None:
            fields |= {"client", "action", "outcome", "mutation"}
        _closed_object(row, fields, "prompt case")
        identifier = _string(row["id"], "prompt case id")
        digest = row["sha256"]
        _require_digest(digest, "prompt case digest")
        if identifier in actual:
            raise RecordsEvidenceError("prompt cases must have unique identifiers")
        actual[identifier] = digest
        if required_results is not None:
            required = required_results.get(identifier)
            if required is None or (
                row["client"], row["action"], row["outcome"], row["mutation"]
            ) != required:
                raise RecordsEvidenceError("prompt case result does not match the committed selection case")
    if actual != dict(expected):
        raise RecordsEvidenceError("Records evidence does not contain the required prompt cases exactly")


def _readback_digest(value: Mapping[str, Any]) -> str:
    """Hash only one targeted collection's content-free state projection."""

    collection_id = _collection_id(value)
    snapshot = value.get("snapshot")
    _require_digest(snapshot, "readback snapshot")
    guards = _lifecycle_guards(value)
    projection: dict[str, Any] = {
        "collection_id": collection_id,
        "snapshot": snapshot,
        "lifecycle_guards": guards,
    }
    audit = value.get("audit")
    if audit is not None:
        audit_object = _object(audit, "readback audit")
        gaps = audit_object.get("gaps")
        if (
            audit_object.get("status") not in {"baseline", "ok", "gap", "acknowledged_gap", "history_incomplete"}
            or not isinstance(gaps, list)
            or not all(isinstance(code, str) for code in gaps)
        ):
            raise RecordsEvidenceError("readback audit gaps are invalid")
        projection["gap_codes"] = sorted(set(gaps))
    return hashlib.sha256(canonical_json(projection)).hexdigest()


def _collection_id(value: Mapping[str, Any]) -> str:
    contract = _object(value.get("contract"), "readback contract")
    return _string(contract.get("collection_id"), "readback collection id")


def _lifecycle_guards(value: Mapping[str, Any]) -> dict[str, str]:
    guards = _object(value.get("lifecycle_guards"), "readback lifecycle guards")
    _closed_object(guards, {"expected_manifest_hash", "expected_container_hash"}, "readback lifecycle guards")
    result = dict(guards)
    _require_digest(result["expected_manifest_hash"], "expected manifest hash")
    _require_digest(result["expected_container_hash"], "expected container hash")
    return result


def _absence_digest(value: Mapping[str, Any], manifest_path: str) -> str:
    _closed_object(value, {"manifest_path", "absent", "state_sha256"}, "pre-create state")
    _same_string(value["manifest_path"], manifest_path, "pre-create manifest path")
    if value["absent"] is not True:
        raise RecordsEvidenceError("pre-create state does not prove absence")
    _require_digest(value["state_sha256"], "pre-create state")
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _gap_codes(value: Mapping[str, Any]) -> list[str]:
    audit = _object(value.get("audit"), "readback audit")
    gaps = audit.get("gaps")
    if (
        audit.get("status") not in {"gap", "history_incomplete"}
        or not isinstance(gaps, list)
        or not all(isinstance(code, str) and code for code in gaps)
    ):
        raise RecordsEvidenceError("readback audit gaps are invalid")
    return sorted(set(gaps))


def _closed_object(value: Mapping[str, Any], allowed: set[str] | frozenset[str], name: str) -> None:
    fields = set(value)
    if fields != set(allowed):
        missing = sorted(set(allowed) - fields)
        unknown = sorted(fields - set(allowed))
        parts = []
        if missing:
            parts.append("missing fields: " + ", ".join(missing))
        if unknown:
            parts.append("unknown fields: " + ", ".join(unknown))
        raise RecordsEvidenceError(f"{name} has " + "; ".join(parts))


def _object(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RecordsEvidenceError(f"{name} must be an object")
    return value


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise RecordsEvidenceError(f"{name} must be a non-empty string")
    return value


def _require_digest(value: Any, name: str) -> None:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise RecordsEvidenceError(f"{name} must be a lowercase SHA-256 digest")


def _same_digest(value: Any, expected: str, name: str) -> None:
    _require_digest(value, name)
    _require_digest(expected, f"expected {name}")
    if not hmac.compare_digest(value, expected):
        raise RecordsEvidenceError(f"Records evidence {name} does not match the candidate")


def _same_string(value: Any, expected: str, name: str) -> None:
    if not isinstance(value, str) or not hmac.compare_digest(value, expected):
        raise RecordsEvidenceError(f"Records evidence {name} does not match the candidate")


def _as_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise RecordsEvidenceError("timestamp must be UTC")
        return value.astimezone(UTC)
    if not isinstance(value, str) or not _UTC_TIMESTAMP.fullmatch(value):
        raise RecordsEvidenceError("timestamp must be UTC RFC3339 seconds")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True, help="deployed HTTPS endpoint")
    parser.add_argument("--bearer-token", default=os.environ.get("EXOMEM_LIVE_ACCEPTANCE_TOKEN"))
    parser.add_argument("--purpose", default="records-live-acceptance")
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--action", action="append", required=True, dest="actions")
    parser.add_argument("--action-requests", type=Path, required=True)
    parser.add_argument("--reset-path", required=True)
    parser.add_argument("--restart-path", required=True)
    parser.add_argument("--direct-edit-path", required=True)
    parser.add_argument("--precreate-path", required=True)
    parser.add_argument("--deployment-sha256", required=True)
    parser.add_argument("--release-version", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--minimum-records-reader-version", type=int, required=True)
    parser.add_argument("--surface-digest", required=True)
    parser.add_argument("--reset-epoch", required=True)
    parser.add_argument("--principal-hmac-sha256", required=True)
    parser.add_argument("--audience-hmac-sha256", required=True)
    parser.add_argument("--client-contracts", type=Path, required=True)
    parser.add_argument("--prompt-cases", type=Path, required=True)
    parser.add_argument("--graph-proof-digest", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    if set(args.actions) != _REQUIRED_ACTIONS or len(args.actions) != len(_REQUIRED_ACTIONS):
        raise RecordsEvidenceError("live acceptance requires every Records action exactly once")
    action_requests = json.loads(args.action_requests.read_text(encoding="utf-8"))
    prompt_cases = json.loads(args.prompt_cases.read_text(encoding="utf-8"))
    client_contracts = json.loads(args.client_contracts.read_text(encoding="utf-8"))
    if (
        not isinstance(action_requests, Mapping)
        or not isinstance(prompt_cases, Mapping)
        or not isinstance(client_contracts, Mapping)
    ):
        raise RecordsEvidenceError("live acceptance inputs must be JSON objects")
    prompt_digests: dict[str, str] = {}
    prompt_results: dict[str, tuple[str, str, str, bool]] = {}
    prompt_rows: list[dict[str, Any]] = []
    for identifier, value in prompt_cases.items():
        if not isinstance(identifier, str):
            raise RecordsEvidenceError("prompt case identifiers must be strings")
        if isinstance(value, str):
            prompt_digests[identifier] = value
            prompt_rows.append({"id": identifier, "sha256": value})
            continue
        if not isinstance(value, Mapping) or set(value) != {"sha256", "client", "action", "outcome", "mutation"}:
            raise RecordsEvidenceError("prompt case result must have exact content-free fields")
        digest = value["sha256"]
        client, action, outcome, mutation = (
            value["client"], value["action"], value["outcome"], value["mutation"]
        )
        if not isinstance(digest, str) or not all(isinstance(item, str) for item in (client, action, outcome)) or not isinstance(mutation, bool):
            raise RecordsEvidenceError("prompt case result has invalid values")
        prompt_digests[identifier] = digest
        prompt_results[identifier] = (client, action, outcome, mutation)
        prompt_rows.append({"id": identifier, "sha256": digest, **dict(value)})
    if prompt_results and len(prompt_results) != len(prompt_digests):
        raise RecordsEvidenceError("prompt cases must consistently include result bindings")
    transport = HttpOAuthRecordsTransport(
        args.endpoint,
        args.bearer_token or "",
        reset_path=args.reset_path,
        restart_path=args.restart_path,
        direct_edit_path=args.direct_edit_path,
        precreate_path=args.precreate_path,
        action_requests=action_requests,
    )
    runner = RecordsLiveAcceptanceRunner(
        transport,
        timeout_seconds=args.timeout_seconds,
        expected_actions=args.actions,
        reset_purpose=args.purpose,
        expected_reset_epoch=args.reset_epoch,
    )
    facts = runner.run()
    now = datetime.now(UTC)
    facts.update({
        "schema_version": 1,
        "deployment": {"sha256": args.deployment_sha256},
        "release": {
            "package": "exomem",
            "version": args.release_version,
            "profile": args.profile,
            "minimum_records_reader_version": args.minimum_records_reader_version,
        },
        "surface": {"mcp_digest": args.surface_digest},
        "identity": {
            "principal_hmac_sha256": args.principal_hmac_sha256,
            "audience_hmac_sha256": args.audience_hmac_sha256,
        },
        "client_contracts": dict(client_contracts),
        "prompt_cases": prompt_rows,
        "graph_availability": {"proof_digest": args.graph_proof_digest},
    })
    facts["run"] = {
        "nonce": secrets.token_urlsafe(24),
        "timestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": (now + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    expected = RecordsEvidenceExpectation(
        deployment_sha256=args.deployment_sha256,
        package="exomem",
        release_version=args.release_version,
        profile=args.profile,
        minimum_records_reader_version=args.minimum_records_reader_version,
        surface_digest=args.surface_digest,
        vault_purpose=args.purpose,
        reset_epoch=args.reset_epoch,
        principal_hmac_sha256=args.principal_hmac_sha256,
        audience_hmac_sha256=args.audience_hmac_sha256,
        client_contracts={
            client: tuple(contract[key] for key in ("client", "client_version", "model_version", "system_contract_version"))
            for client, contract in client_contracts.items()
            if isinstance(client, str) and isinstance(contract, Mapping)
        },
        required_actions=_REQUIRED_ACTIONS,
        required_prompt_cases=prompt_digests,
        required_prompt_case_results=prompt_results or None,
        graph_proof_digest=args.graph_proof_digest,
    )
    validate_records_live_evidence(facts, expected=expected, now=now)
    print(canonical_json(facts).decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
