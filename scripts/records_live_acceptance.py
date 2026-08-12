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
from datetime import UTC, datetime
from typing import Any, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_READ_ONLY_ACTIONS = frozenset({"describe", "validate", "inspect", "query"})
_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "deployment",
        "release",
        "surface",
        "run",
        "vault",
        "identity",
        "client_contract",
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
        surface_digest: str,
        vault_purpose: str,
        reset_epoch: str,
        principal_hmac_sha256: str,
        audience_hmac_sha256: str,
        client_contract: tuple[str, str, str, str],
        required_actions: frozenset[str],
        required_prompt_cases: Mapping[str, str],
        graph_proof_digest: str,
    ) -> None:
        self.deployment_sha256 = deployment_sha256
        self.package = package
        self.release_version = release_version
        self.surface_digest = surface_digest
        self.vault_purpose = vault_purpose
        self.reset_epoch = reset_epoch
        self.principal_hmac_sha256 = principal_hmac_sha256
        self.audience_hmac_sha256 = audience_hmac_sha256
        self.client_contract = client_contract
        self.required_actions = required_actions
        self.required_prompt_cases = dict(required_prompt_cases)
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

    def invoke(self, *, action: str, timeout_seconds: float) -> Mapping[str, Any]: ...

    def readback(self, *, timeout_seconds: float) -> Mapping[str, Any]: ...


class HttpOAuthRecordsTransport:
    """Small, concrete HTTP/OAuth transport; endpoint paths remain deploy-configurable."""

    def __init__(
        self,
        endpoint: str,
        bearer_token: str,
        *,
        reset_path: str = "/acceptance/reset",
        invoke_path: str = "/mcp",
        readback_path: str = "/api/record_memory",
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
        self._readback_path = readback_path
        self._action_requests = {key: dict(value) for key, value in (action_requests or {}).items()}

    def reset(self, *, purpose: str, timeout_seconds: float) -> Mapping[str, Any]:
        return self._post(self._reset_path, {"purpose": purpose}, timeout_seconds)

    def invoke(self, *, action: str, timeout_seconds: float) -> Mapping[str, Any]:
        payload = dict(self._action_requests.get(action, {}))
        payload.setdefault("action", action)
        return self._post(self._invoke_path, payload, timeout_seconds)

    def readback(self, *, timeout_seconds: float) -> Mapping[str, Any]:
        return self._post(self._readback_path, {"action": "inspect"}, timeout_seconds)

    def _post(self, path: str, payload: Mapping[str, Any], timeout_seconds: float) -> Mapping[str, Any]:
        request = Request(
            self._endpoint + path,
            data=canonical_json(payload),
            headers={
                "Authorization": f"Bearer {self._bearer_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:  # nosec B310: HTTPS is enforced above
                decoded = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RecordsEvidenceError(f"live HTTP/OAuth request failed: {type(exc).__name__}") from exc
        if not isinstance(decoded, Mapping):
            raise RecordsEvidenceError("live HTTP/OAuth response must be an object")
        return decoded


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
            outcome = _string(response.get("outcome"), f"{action} outcome")
            if outcome not in {"completed", "committed"}:
                raise RecordsEvidenceError(f"{action} did not reach an accepted terminal")
            if outcome == "committed":
                if before is None:
                    raise RecordsEvidenceError(f"read-only action {action} returned a committed terminal")
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
        return {
            "vault": {"purpose": self._reset_purpose, "reset_epoch": epoch},
            "actions": actions,
            "mutations": mutations,
        }


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def validate_records_live_evidence(
    evidence: Mapping[str, Any], *, expected: RecordsEvidenceExpectation, now: str | datetime
) -> bytes:
    """Validate only structured, content-free unsigned facts against trusted bindings."""

    _closed_object(evidence, _TOP_LEVEL_FIELDS, "evidence")
    if evidence["schema_version"] != 1:
        raise RecordsEvidenceError("unsupported Records evidence schema version")
    _validate_binding(evidence, expected, _as_datetime(now))
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


def _validate_binding(evidence: Mapping[str, Any], expected: RecordsEvidenceExpectation, now: datetime) -> None:
    deployment = _object(evidence["deployment"], "deployment")
    _closed_object(deployment, {"sha256"}, "deployment")
    _same_digest(deployment["sha256"], expected.deployment_sha256, "deployment")
    release = _object(evidence["release"], "release")
    _closed_object(release, {"package", "version"}, "release")
    _same_string(release["package"], expected.package, "release package")
    _same_string(release["version"], expected.release_version, "release version")
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
    if timestamp > now or expiry <= now or expiry <= timestamp:
        raise RecordsEvidenceError("Records evidence is expired or has an invalid lifetime")
    vault = _object(evidence["vault"], "vault")
    _closed_object(vault, {"purpose", "reset_epoch"}, "vault")
    _same_string(vault["purpose"], expected.vault_purpose, "vault purpose")
    _same_string(vault["reset_epoch"], expected.reset_epoch, "reset epoch")
    identity = _object(evidence["identity"], "identity")
    _closed_object(identity, {"principal_hmac_sha256", "audience_hmac_sha256"}, "identity")
    _same_digest(identity["principal_hmac_sha256"], expected.principal_hmac_sha256, "principal")
    _same_digest(identity["audience_hmac_sha256"], expected.audience_hmac_sha256, "audience")
    contract = _object(evidence["client_contract"], "client contract")
    _closed_object(contract, {"client", "client_version", "model_version", "system_contract_version"}, "client contract")
    if tuple(contract[key] for key in ("client", "client_version", "model_version", "system_contract_version")) != expected.client_contract:
        raise RecordsEvidenceError("Records evidence client contract does not match the candidate")
    _validate_actions(evidence["actions"], expected.required_actions)
    _validate_mutations(evidence["mutations"], evidence["actions"])
    restart = _object(evidence["restart"], "restart")
    _closed_object(restart, {"outcome", "readback_sha256"}, "restart")
    if restart["outcome"] != "completed":
        raise RecordsEvidenceError("restart did not complete")
    _require_digest(restart["readback_sha256"], "restart readback")
    _validate_prompt_cases(evidence["prompt_cases"], expected.required_prompt_cases)
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
        if row["outcome"] not in {"completed", "committed"} or action in actual:
            raise RecordsEvidenceError("actions must have unique successful terminals")
        actual.add(action)
    if actual != expected:
        raise RecordsEvidenceError("Records evidence does not contain the required actions exactly")


def _validate_mutations(value: Any, actions: Any) -> None:
    if not isinstance(value, list):
        raise RecordsEvidenceError("mutations must be a list")
    committed = {row["action"] for row in actions if isinstance(row, Mapping) and row.get("outcome") == "committed"}
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


def _validate_prompt_cases(value: Any, expected: Mapping[str, str]) -> None:
    if not isinstance(value, list):
        raise RecordsEvidenceError("prompt cases must be a list")
    actual: dict[str, str] = {}
    for item in value:
        row = _object(item, "prompt case")
        _closed_object(row, {"id", "sha256"}, "prompt case")
        identifier = _string(row["id"], "prompt case id")
        digest = row["sha256"]
        _require_digest(digest, "prompt case digest")
        if identifier in actual:
            raise RecordsEvidenceError("prompt cases must have unique identifiers")
        actual[identifier] = digest
    if actual != dict(expected):
        raise RecordsEvidenceError("Records evidence does not contain the required prompt cases exactly")


def _readback_digest(value: Mapping[str, Any]) -> str:
    # Hash the full independently fetched object, then discard it immediately.
    return hashlib.sha256(canonical_json(value)).hexdigest()


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
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    transport = HttpOAuthRecordsTransport(args.endpoint, args.bearer_token or "")
    runner = RecordsLiveAcceptanceRunner(
        transport,
        timeout_seconds=args.timeout_seconds,
        expected_actions=args.actions,
        reset_purpose=args.purpose,
    )
    facts = runner.run()
    facts["run"] = {
        "nonce": secrets.token_urlsafe(24),
        "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    print(canonical_json(facts).decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
