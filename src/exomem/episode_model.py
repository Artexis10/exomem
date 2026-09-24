"""Pure episode coordination state; it performs no I/O, writes, or model calls.

VerifiedLeafOutcome is an internal adapter boundary, not authentication.  A
persistence adapter must authenticate its receipt, result and readback values.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from . import curation

_HEX = re.compile(r"^[0-9a-f]{64}$")
_DISPOSITIONS = {"routed", "no_capture", "uncertain", "rejected", "deferred", "awaiting_authority"}
_ROUTES = {
    "existing_page",
    "semantic_unit",
    "focused_note",
    "entity",
    "records",
    "planning",
    "experiment",
    "source",
    "evidence",
    "relation_only",
    "no_capture",
}
_ADAPTER_PENDING_ROUTES = {"source", "evidence", "records", "planning", "experiment"}
_NO_EFFECT_ROUTES = _ADAPTER_PENDING_ROUTES | {"no_capture"}
MAX_STATE_BYTES = 256 * 1024
MAX_INPUT_REVISIONS = 64
#: Refs of existing pages an input concerns, retained with it. Opaque here;
#: the recorder resolves and visibility-filters them before they arrive.
MAX_INPUT_ABOUT = 3
MAX_ATTESTATIONS = 64
MAX_EFFECT_HISTORY = 64
MAX_ATTEMPTS = 64


@dataclass
class EpisodeError(ValueError):
    code: str
    reason: str

    def __str__(self) -> str:
        return f"{self.code}: {self.reason}"


@dataclass(frozen=True)
class VerifiedLeafOutcome:
    candidate_id: str
    leaf_id: str
    operation_id: str
    effect_digest: str
    outcome: str
    receipt_digest: str
    result_digest: str | None = None
    readback_digest: str | None = None


def _fail(code: str, reason: str) -> EpisodeError:
    return EpisodeError(code, reason)


def _json(value: Any) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError, RecursionError) as error:
        raise _fail("EPISODE_JSON_INVALID", "value must be finite JSON") from error


def _copy(value: Any) -> Any:
    encoded = _json(value)
    if len(encoded.encode()) > MAX_STATE_BYTES:
        raise _fail("EPISODE_TOO_LARGE", "episode state exceeds its byte cap")
    return json.loads(encoded)


def _hash(label: str, *value: Any) -> str:
    return hashlib.sha256(_json([label, *value]).encode()).hexdigest()


def _string(value: Any, field: str, maximum: int = 2048) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise _fail("EPISODE_PROPOSAL_INVALID", f"{field} must be a bounded non-empty string")
    return value


def episode_id(key: str) -> str:
    return _hash("exomem-episode-v1", _string(key, "episode_key", 512))


def candidate_id(episode: str, key: str) -> str:
    if not _HEX.fullmatch(str(episode)):
        raise _fail("EPISODE_CANDIDATE_ID_INVALID", "episode identity is invalid")
    return _hash("exomem-episode-candidate-v1", episode, _string(key, "candidate_key", 160))


def leaf_id(candidate: str, key: str) -> str:
    if not _HEX.fullmatch(str(candidate)):
        raise _fail("EPISODE_LEAF_ID_INVALID", "candidate identity is invalid")
    return _hash("exomem-episode-leaf-v1", candidate, _string(key, "leaf_key", 160))


def _evidence(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) - {"reference", "excerpt", "digest", "about"}:
        raise _fail("EPISODE_EVIDENCE_INVALID", "evidence has unknown fields")
    value = dict(raw)
    if value.get("reference") is not None and (
        not isinstance(value["reference"], str)
        or not value["reference"].strip()
        or len(value["reference"]) > 2048
    ):
        raise _fail("EPISODE_EVIDENCE_INVALID", "reference is invalid")
    if value.get("excerpt") is not None and (
        not isinstance(value["excerpt"], str)
        or not value["excerpt"]
        or len(value["excerpt"].encode()) > 8192
    ):
        raise _fail("EPISODE_EVIDENCE_INVALID", "excerpt is invalid")
    if value.get("digest") is not None and (
        not isinstance(value["digest"], str) or not _HEX.fullmatch(value["digest"])
    ):
        raise _fail("EPISODE_EVIDENCE_INVALID", "digest is invalid")
    about = value.get("about")
    if about is not None and (
        not isinstance(about, (list, tuple))
        or len(about) > MAX_INPUT_ABOUT
        or len(set(about)) != len(about)
        or not all(isinstance(ref, str) and 0 < len(ref) <= 2048 for ref in about)
    ):
        raise _fail("EPISODE_EVIDENCE_INVALID", "about is invalid")
    keep = {
        key: value[key] for key in ("reference", "excerpt", "digest") if value.get(key) is not None
    }
    if not keep:
        raise _fail("EPISODE_EVIDENCE_INVALID", "evidence is empty")
    if about:
        keep["about"] = list(about)
    keep["recovery"] = "available" if {"reference", "excerpt"} & set(keep) else "unavailable"
    return keep


def _current(state: Mapping[str, Any]) -> int:
    return state["input_revisions"][-1]["revision"]


def _invalidate(state: dict[str, Any]) -> None:
    state["current_precommit"] = None
    state["covered_through_input_revision"] = None
    state["reviewed_through_input_revision"] = None
    state["complete"] = False
    state["pending_leaf_ids"] = _pending(state)


def start_episode(key: str, input_evidence: Mapping[str, Any]) -> dict[str, Any]:
    evidence = _evidence(input_evidence)
    return {
        "version": 1,
        "episode_id": episode_id(key),
        "input_revisions": [
            {"revision": 1, "evidence": evidence, "recovery": evidence["recovery"]}
        ],
        "candidates": [],
        "precommit_attestations": [],
        "postcommit_attestations": [],
        "current_precommit": None,
        "reviewed_through_input_revision": None,
        "covered_through_input_revision": None,
        "pending_leaf_ids": [],
        "complete": False,
    }


def append_input_revision(
    state: Mapping[str, Any], input_evidence: Mapping[str, Any]
) -> dict[str, Any]:
    result, evidence = _copy(state), _evidence(input_evidence)
    old = {
        key: value
        for key, value in result["input_revisions"][-1]["evidence"].items()
        if key != "recovery"
    }
    new = {key: value for key, value in evidence.items() if key != "recovery"}
    if _json(old) == _json(new):
        raise _fail("EPISODE_REVISION_UNCHANGED", "input evidence is byte-equivalent")
    if len(result["input_revisions"]) >= MAX_INPUT_REVISIONS:
        raise _fail("EPISODE_TOO_LARGE", "too many input revisions")
    result["input_revisions"].append(
        {"revision": _current(result) + 1, "evidence": evidence, "recovery": evidence["recovery"]}
    )
    _invalidate(result)
    return _copy(result)


def _candidate(state: dict[str, Any], identity: str) -> dict[str, Any]:
    for candidate in state["candidates"]:
        if candidate["candidate_id"] == identity:
            return candidate
    raise _fail("EPISODE_CANDIDATE_UNKNOWN", "candidate is not part of this episode")


def declare_candidate(state: Mapping[str, Any], key: str) -> dict[str, Any]:
    result, identity = _copy(state), candidate_id(state["episode_id"], key)
    if any(item["candidate_id"] == identity for item in result["candidates"]):
        raise _fail("EPISODE_CANDIDATE_DUPLICATE", "candidate key is already declared")
    if len(result["candidates"]) >= 64:
        raise _fail("EPISODE_TOO_LARGE", "too many candidates")
    result["candidates"].append(
        {
            "candidate_id": identity,
            "candidate_key": key,
            "proposal_revision": 0,
            "proposal": None,
            "disposition": None,
            "leaves": [],
        }
    )
    result["candidates"].sort(key=lambda item: item["candidate_id"])
    _invalidate(result)
    return _copy(result)


def _effect(kind: str, args: Mapping[str, Any]) -> str:
    return _hash("exomem-episode-effect-v1", kind, dict(args))


def _leaf(raw: Any, candidate: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != {"leaf_key", "effect_revision", "kind", "args"}:
        raise _fail("EPISODE_PROPOSAL_INVALID", "leaf has unknown fields")
    if type(raw["effect_revision"]) is not int or raw["effect_revision"] < 1:
        raise _fail("EPISODE_PROPOSAL_INVALID", "effect revision is invalid")
    try:
        step = curation.validate_forward_plan(
            {
                "version": 1,
                "title": "episode leaf",
                "steps": [{"step_id": "leaf", "kind": raw["kind"], "args": raw["args"]}],
            }
        )["steps"][0]
    except curation.CurationError as error:
        raise _fail(
            "EPISODE_PROPOSAL_INVALID", "leaf is not a supported curation effect"
        ) from error
    key = _string(raw["leaf_key"], "leaf_key", 160)
    return {
        "leaf_id": leaf_id(candidate, key),
        "leaf_key": key,
        "effect_revision": raw["effect_revision"],
        "kind": step["kind"],
        "args": step["args"],
        "effect_digest": _effect(step["kind"], step["args"]),
        "binding": None,
        "outcome": "pending",
        "attempts": 0,
        "attempt_history": [],
        "outcome_proof": None,
        "effect_history": [],
    }


def _proposal(raw: Any, candidate: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    fields = {"route", "target", "title", "alternatives", "evidence", "reason", "leaves"}
    if (
        not isinstance(raw, Mapping)
        or set(raw) - fields
        or raw.get("route") not in _ROUTES
        or raw.get("evidence") not in {"complete", "truncated", "missing"}
    ):
        raise _fail("EPISODE_PROPOSAL_INVALID", "proposal is invalid")
    if raw.get("target") is None and raw.get("title") is None:
        raise _fail("EPISODE_PROPOSAL_INVALID", "proposal needs target or title")
    alternatives = raw.get("alternatives")
    if not isinstance(alternatives, list) or len(alternatives) > 8:
        raise _fail("EPISODE_PROPOSAL_INVALID", "alternatives are invalid")
    alternatives = [
        {key: _string(item[key], key) for key in ("target", "scope", "version")}
        for item in alternatives
        if isinstance(item, Mapping) and set(item) == {"target", "scope", "version"}
    ]
    if len(alternatives) != len(raw["alternatives"]) or alternatives != sorted(
        alternatives, key=_json
    ):
        raise _fail("EPISODE_PROPOSAL_INVALID", "alternatives are not canonical")
    leaves = raw.get("leaves")
    if not isinstance(leaves, list) or len(leaves) > 16:
        raise _fail("EPISODE_PROPOSAL_INVALID", "leaves are invalid")
    if raw["route"] in _NO_EFFECT_ROUTES and leaves:
        raise _fail("EPISODE_PROPOSAL_INVALID", "route has no integrated curation leaf")
    if raw["route"] not in _NO_EFFECT_ROUTES and not leaves:
        raise _fail("EPISODE_PROPOSAL_INVALID", "route needs at least one curation leaf")
    leaves = sorted((_leaf(item, candidate) for item in leaves), key=lambda item: item["leaf_id"])
    if len({item["leaf_id"] for item in leaves}) != len(leaves):
        raise _fail("EPISODE_PROPOSAL_INVALID", "leaf keys are duplicated")
    if len({item["effect_digest"] for item in leaves}) != len(leaves):
        raise _fail(
            "EPISODE_DUPLICATE_EFFECT", "proposal gives one effect multiple leaf identities"
        )
    proposal = {
        "route": raw["route"],
        "alternatives": alternatives,
        "evidence": raw["evidence"],
        "reason": _string(raw.get("reason"), "reason", 1000),
    }
    for key, maximum in (("target", 2048), ("title", 500)):
        if raw.get(key) is not None:
            proposal[key] = _string(raw[key], key, maximum)
    return proposal, leaves


def _all_leaves(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [leaf for candidate in state["candidates"] for leaf in candidate["leaves"]]


def revise_proposal(
    state: Mapping[str, Any], candidate_identity: str, proposal: Mapping[str, Any]
) -> dict[str, Any]:
    result, candidate = _copy(state), None
    candidate = _candidate(result, candidate_identity)
    if any(item["outcome"] == "uncertain" for item in candidate["leaves"]):
        raise _fail("EPISODE_ATTEMPT_UNCERTAIN", "uncertain candidate proposal is frozen")
    normalized, leaves = _proposal(proposal, candidate_identity)
    prior = {item["leaf_id"]: item for item in candidate["leaves"]}
    for old in prior.values():
        new = next((item for item in leaves if item["leaf_id"] == old["leaf_id"]), None)
        if old["outcome"] == "committed" and (
            new is None or new["effect_digest"] != old["effect_digest"]
        ):
            raise _fail("EPISODE_ATTEMPTED_LEAF", "committed leaf cannot change or be removed")
        if (
            old["attempts"] or any(item["attempts"] for item in old["effect_history"])
        ) and new is None:
            raise _fail("EPISODE_ATTEMPTED_LEAF", "attempted leaf cannot be removed")
    for leaf in leaves:
        old = prior.get(leaf["leaf_id"])
        if old is None and leaf["effect_revision"] != 1:
            raise _fail("EPISODE_EFFECT_REVISION_REQUIRED", "a new effect starts at revision one")
        if old and old["effect_digest"] != leaf["effect_digest"]:
            if any(
                item["attempts"] and item["effect_digest"] == leaf["effect_digest"]
                for item in old["effect_history"]
            ):
                raise _fail(
                    "EPISODE_DUPLICATE_EFFECT",
                    "historically attempted effect retains its original operation",
                )
            if leaf["effect_revision"] != old["effect_revision"] + 1:
                raise _fail(
                    "EPISODE_EFFECT_REVISION_REQUIRED", "changed effect needs an explicit revision"
                )
            if old["outcome"] == "committed":
                raise _fail("EPISODE_ATTEMPTED_LEAF", "committed effect is frozen")
            if len(old["effect_history"]) >= MAX_EFFECT_HISTORY:
                raise _fail("EPISODE_TOO_LARGE", "too many effect revisions")
            leaf["effect_history"] = old["effect_history"] + [
                {
                    "effect_revision": old["effect_revision"],
                    "effect_digest": old["effect_digest"],
                    "binding": old["binding"],
                    "outcome": old["outcome"],
                    "outcome_proof": old["outcome_proof"],
                    "attempts": old["attempts"],
                    "attempt_history": old["attempt_history"],
                }
            ]
            if old["outcome"] == "proven_uncommitted":
                leaf["binding"] = None
                leaf["outcome"] = "pending"
                leaf["attempts"] = 0
                leaf["outcome_proof"] = None
        elif old:
            if leaf["effect_revision"] != old["effect_revision"]:
                raise _fail("EPISODE_PROPOSAL_INVALID", "unchanged effect cannot change revision")
            leaf.update(
                {
                    key: old[key]
                    for key in (
                        "binding",
                        "outcome",
                        "attempts",
                        "attempt_history",
                        "outcome_proof",
                        "effect_history",
                    )
                }
            )
        for owned in _all_leaves(result):
            if owned["leaf_id"] != leaf["leaf_id"] and (
                owned["effect_digest"] == leaf["effect_digest"]
                or any(
                    item["effect_digest"] == leaf["effect_digest"]
                    for item in owned["effect_history"]
                )
            ):
                raise _fail(
                    "EPISODE_DUPLICATE_EFFECT", "identical effect already belongs to another leaf"
                )
    if candidate["proposal"] is not None and _json(
        {"proposal": normalized, "leaves": leaves}
    ) == _json({"proposal": candidate["proposal"], "leaves": candidate["leaves"]}):
        raise _fail("EPISODE_PROPOSAL_UNCHANGED", "proposal is byte-equivalent")
    candidate["proposal_revision"] += 1
    candidate["proposal"], candidate["leaves"] = normalized, leaves
    _invalidate(result)
    return _copy(result)


def set_disposition(
    state: Mapping[str, Any], candidate_identity: str, disposition: str, reason: str
) -> dict[str, Any]:
    result, candidate = _copy(state), None
    candidate = _candidate(result, candidate_identity)
    if disposition not in _DISPOSITIONS:
        raise _fail("EPISODE_DISPOSITION_INVALID", "disposition is invalid")
    if any(leaf["outcome"] == "uncertain" for leaf in candidate["leaves"]):
        raise _fail("EPISODE_ATTEMPT_UNCERTAIN", "uncertain candidate cannot change")
    if disposition == "routed":
        proposal = candidate["proposal"]
        if proposal is None or proposal["route"] == "no_capture":
            raise _fail("EPISODE_DISPOSITION_INVALID", "routed candidate needs a capture proposal")
        if not candidate["leaves"] and proposal["route"] not in _ADAPTER_PENDING_ROUTES:
            raise _fail("EPISODE_DISPOSITION_INVALID", "routed candidate needs leaves")
    candidate["disposition"] = {
        "value": disposition,
        "reason": _string(reason, "reason", 1000),
        "input_revision": _current(result),
    }
    _invalidate(result)
    return _copy(result)


def _owned(state: dict[str, Any], candidate: str, leaf: str) -> dict[str, Any]:
    for item in _candidate(state, candidate)["leaves"]:
        if item["leaf_id"] == leaf:
            return item
    raise _fail("EPISODE_LEAF_UNKNOWN", "leaf does not belong to candidate")


def _binding(raw: Any, leaf: Mapping[str, Any]) -> dict[str, Any]:
    fields = {
        "sealed_plan",
        "run_id",
        "plan_id",
        "plan_fingerprint",
        "ordinal",
        "step_id",
        "operation_id",
    }
    if (
        not isinstance(raw, Mapping)
        or set(raw) != fields
        or not isinstance(raw["sealed_plan"], Mapping)
    ):
        raise _fail("EPISODE_BINDING_INVALID", "binding is invalid")
    sealed = raw["sealed_plan"]
    plan = curation.validate_forward_plan(
        {
            key: sealed[key]
            for key in ("version", "title", "steps", "entity_candidate")
            if key in sealed
        }
    )
    manifest, registries = sealed.get("binding_manifest"), sealed.get("registry_ids")
    if not isinstance(manifest, list) or not isinstance(registries, Mapping):
        raise _fail("EPISODE_BINDING_INVALID", "sealed plan evidence is incomplete")
    plan_id = curation.plan_id(
        {**plan, "binding_manifest": manifest, "registry_ids": dict(registries)}
    )
    if raw["plan_id"] != plan_id or raw["plan_fingerprint"] != curation.plan_fingerprint(
        plan, manifest, registries
    ):
        raise _fail("EPISODE_BINDING_INVALID", "sealed plan identity does not match evidence")
    if not isinstance(raw["run_id"], str) or not re.fullmatch(
        rf"cur-[0-9]{{8}}-{plan_id[:12]}", raw["run_id"]
    ):
        raise _fail("EPISODE_BINDING_INVALID", "run id does not match curation plan identity")
    if type(raw["ordinal"]) is not int or not 0 <= raw["ordinal"] < len(plan["steps"]):
        raise _fail("EPISODE_BINDING_INVALID", "ordinal is invalid")
    step = plan["steps"][raw["ordinal"]]
    if raw["step_id"] != step["step_id"]:
        raise _fail("EPISODE_BINDING_INVALID", "step does not match ordinal")
    if raw["operation_id"] != curation.operation_id(raw["plan_id"], raw["ordinal"], raw["step_id"]):
        raise _fail(
            "EPISODE_OPERATION_MISMATCH", "operation identity is not current curation identity"
        )
    if step["kind"] != leaf["kind"] or _effect(step["kind"], step["args"]) != leaf["effect_digest"]:
        raise _fail("EPISODE_EFFECT_MISMATCH", "step does not match leaf effect")
    return {key: raw[key] for key in fields if key != "sealed_plan"}


def bind_curation_leaf(
    state: Mapping[str, Any], candidate: str, leaf: str, binding: Mapping[str, Any]
) -> dict[str, Any]:
    result, item = _copy(state), None
    item = _owned(result, candidate, leaf)
    if item["attempts"]:
        raise _fail("EPISODE_ATTEMPTED_LEAF", "attempted leaf cannot be rebound")
    item["binding"] = _binding(binding, item)
    _invalidate(result)
    return _copy(result)


def mark_attempt_started(state: Mapping[str, Any], candidate: str, leaf: str) -> dict[str, Any]:
    result = _copy(state)
    if result["current_precommit"] is None:
        raise _fail("EPISODE_PRECOMMIT_REQUIRED", "attempt requires current precommit review")
    owner = _candidate(result, candidate)
    if owner["disposition"] is None or owner["disposition"]["value"] != "routed":
        raise _fail("EPISODE_DISPOSITION_INVALID", "only routed candidates may execute")
    if owner["proposal"] is None or owner["proposal"]["route"] in _NO_EFFECT_ROUTES:
        raise _fail("EPISODE_DISPOSITION_INVALID", "selected route has no executable effect")
    item = _owned(result, candidate, leaf)
    if item["binding"] is None:
        raise _fail("EPISODE_BINDING_REQUIRED", "attempt requires curation binding")
    if item["outcome"] not in {"pending", "proven_uncommitted"}:
        raise _fail("EPISODE_ATTEMPT_UNCERTAIN", "leaf needs reconciliation before retry")
    if item["attempts"] >= MAX_ATTEMPTS:
        raise _fail("EPISODE_TOO_LARGE", "too many attempts for this effect")
    item["attempts"] += 1
    item["outcome"], item["outcome_proof"] = "uncertain", None
    item["attempt_history"].append(
        {"attempt": item["attempts"], "outcome": "uncertain", "outcome_proof": None}
    )
    return _copy(result)


def reconcile_leaf(state: Mapping[str, Any], observation: VerifiedLeafOutcome) -> dict[str, Any]:
    if not isinstance(observation, VerifiedLeafOutcome):
        raise _fail("EPISODE_OUTCOME_TYPE_REQUIRED", "outcome must use typed adapter input")
    if observation.outcome not in {"committed", "proven_uncommitted"} or not _HEX.fullmatch(
        observation.receipt_digest
    ):
        raise _fail("EPISODE_OUTCOME_INVALID", "outcome is invalid")
    if observation.outcome == "committed" and (
        not _HEX.fullmatch(str(observation.result_digest))
        or not _HEX.fullmatch(str(observation.readback_digest))
    ):
        raise _fail(
            "EPISODE_OUTCOME_INVALID", "committed outcome needs result and readback identities"
        )
    result = _copy(state)
    item = _owned(result, observation.candidate_id, observation.leaf_id)
    if item["outcome"] != "uncertain" or item["binding"] is None:
        raise _fail("EPISODE_RECONCILIATION_INVALID", "only bound uncertain leaves reconcile")
    if (
        item["binding"]["operation_id"] != observation.operation_id
        or item["effect_digest"] != observation.effect_digest
    ):
        raise _fail("EPISODE_RECONCILIATION_MISMATCH", "outcome does not match leaf")
    item["outcome"] = observation.outcome
    item["outcome_proof"] = {
        "receipt_digest": observation.receipt_digest,
        "result_digest": observation.result_digest,
        "readback_digest": observation.readback_digest,
        "operation_id": observation.operation_id,
    }
    item["attempt_history"][-1] = {
        "attempt": item["attempts"],
        "outcome": item["outcome"],
        "outcome_proof": _copy(item["outcome_proof"]),
    }
    return _copy(result)


def _pending(state: Mapping[str, Any]) -> list[str]:
    result = []
    for candidate in state["candidates"]:
        disposition = candidate["disposition"]
        if disposition is None or disposition["value"] in {
            "uncertain",
            "deferred",
            "awaiting_authority",
        }:
            result.append(candidate["candidate_id"])
        elif disposition["value"] == "routed":
            if candidate["proposal"]["route"] in _ADAPTER_PENDING_ROUTES:
                result.append(candidate["candidate_id"])
            else:
                result.extend(
                    leaf["leaf_id"]
                    for leaf in candidate["leaves"]
                    if leaf["outcome"] != "committed"
                )
    return sorted(result)


def attest_precommit(state: Mapping[str, Any], input_revision: int) -> dict[str, Any]:
    result = _copy(state)
    if input_revision != _current(result):
        raise _fail("EPISODE_INPUT_REVISION_STALE", "precommit must attest current input")
    if result["input_revisions"][-1]["recovery"] != "available":
        raise _fail("EPISODE_RECOVERY_UNAVAILABLE", "digest-only input cannot become capture-ready")
    if len(result["precommit_attestations"]) >= MAX_ATTESTATIONS:
        raise _fail("EPISODE_TOO_LARGE", "too many precommit attestations")
    for candidate in result["candidates"]:
        disposition = candidate["disposition"]
        if disposition is None or disposition["input_revision"] != input_revision:
            raise _fail(
                "EPISODE_COVERAGE_INCOMPLETE", "every candidate needs a current disposition"
            )
        if disposition["value"] == "routed" and any(
            leaf["binding"] is None for leaf in candidate["leaves"]
        ):
            raise _fail("EPISODE_BINDING_REQUIRED", "routed leaves need curation bindings")
    attestation = {
        "input_revision": input_revision,
        "snapshot": _hash("exomem-episode-precommit-v1", input_revision, result["candidates"]),
    }
    result["precommit_attestations"].append(attestation)
    result["current_precommit"], result["pending_leaf_ids"] = (
        _copy(attestation),
        _copy(_pending(result)),
    )
    return _copy(result)


def attest_postcommit(
    state: Mapping[str, Any], input_revision: int, leaf_ids: Sequence[str]
) -> dict[str, Any]:
    result = _copy(state)
    if input_revision != _current(result) or result["current_precommit"] is None:
        raise _fail("EPISODE_PRECOMMIT_REQUIRED", "postcommit needs current precommit review")
    committed = {
        leaf["leaf_id"]: leaf for leaf in _all_leaves(result) if leaf["outcome"] == "committed"
    }
    if (
        isinstance(leaf_ids, (str, bytes))
        or len(leaf_ids) != len(set(leaf_ids))
        or set(leaf_ids) != set(committed)
    ):
        raise _fail("EPISODE_POSTCOMMIT_INVALID", "postcommit must attest known committed leaves")
    if len(result["postcommit_attestations"]) >= MAX_ATTESTATIONS:
        raise _fail("EPISODE_TOO_LARGE", "too many postcommit attestations")
    if any(
        not leaf["outcome_proof"]
        or not leaf["outcome_proof"]["result_digest"]
        or not leaf["outcome_proof"]["readback_digest"]
        for leaf in committed.values()
    ):
        raise _fail("EPISODE_POSTCOMMIT_INVALID", "committed leaf proof is incomplete")
    pending = _pending(result)
    result["pending_leaf_ids"], result["reviewed_through_input_revision"] = (
        _copy(pending),
        input_revision,
    )
    result["postcommit_attestations"].append(
        {"input_revision": input_revision, "leaf_ids": sorted(leaf_ids), "pending": _copy(pending)}
    )
    result["complete"] = not pending
    if not pending:
        result["covered_through_input_revision"] = input_revision
    return _copy(result)
