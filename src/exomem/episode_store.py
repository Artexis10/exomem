"""Internal episode history owned by the existing curation persistence boundary.

Only trusted owners may call this module, after authorizing retention/disclosure
of all supplied evidence and intent. It supplies no public API, permission grant,
evidence resolver or executor. Reconstructed coverage is historical, not current.
Accepted events preserve verified commitments without replaying mutable readback.
The hash chain detects corruption; it is not authentication against a vault owner.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import curation
from . import episode_model as model
from .episode_reconciliation import reconcile_curation_leaf
from .vault import (
    BatchWriteError,
    ContentHashMismatchError,
    CreateOnlyConflict,
    PlannedWrite,
    batch_atomic_write,
)
from .writer_lease import active_manager

MAX_TRANSITIONS = 512
MAX_JOURNAL_BYTES = curation.MAX_PLAN_BYTES * 4
_FIELDS = {
    "append_input_revision": {"input_evidence"},
    "declare_candidate": {"key"},
    "revise_proposal": {"candidate", "proposal"},
    "set_disposition": {"candidate", "disposition", "reason"},
    "bind_curation_leaf": {
        "candidate",
        "leaf",
        "run_id",
        "plan_id",
        "plan_fingerprint",
        "ordinal",
    },
    "attest_precommit": {"input_revision"},
    "mark_attempt_started": {"candidate", "leaf"},
    "reconcile_curation_leaf": {"candidate", "leaf"},
    "attest_postcommit": {"input_revision", "leaf_ids"},
}


def _error(code: str, reason: str) -> model.EpisodeError:
    return model.EpisodeError(code, reason)


def _validate_args(action: Any, args: Any) -> None:
    if (
        not isinstance(action, str)
        or action not in _FIELDS
        or not isinstance(args, dict)
        or set(args) != _FIELDS[action]
    ):
        raise _error("EPISODE_TRANSITION_INVALID", "transition fields are invalid")
    for field in ("input_revision", "ordinal"):
        if field in args and type(args[field]) is not int:
            raise _error("EPISODE_TRANSITION_INVALID", "revision and ordinal must be integers")


def _apply_event(state: Mapping[str, Any], event: Mapping[str, Any]) -> dict[str, Any]:
    """Replay accepted finite transitions without consulting mutable external state."""
    action, args, evidence = event["action"], event["args"], event["evidence"]
    _validate_args(action, args)
    evidence_fields = {
        "bind_curation_leaf": {"sealed_plan"},
        "reconcile_curation_leaf": {"outcome"},
    }.get(action, set())
    if not isinstance(evidence, dict) or set(evidence) != evidence_fields:
        raise _error("EPISODE_TRANSITION_INVALID", "accepted evidence fields are invalid")
    if action == "append_input_revision":
        return model.append_input_revision(state, args["input_evidence"])
    if action == "declare_candidate":
        return model.declare_candidate(state, args["key"])
    if action == "revise_proposal":
        return model.revise_proposal(state, args["candidate"], args["proposal"])
    if action == "set_disposition":
        return model.set_disposition(state, args["candidate"], args["disposition"], args["reason"])
    if action == "bind_curation_leaf":
        plan = evidence["sealed_plan"]
        ordinal = args["ordinal"]
        if not 0 <= ordinal < len(plan["steps"]):
            raise _error("EPISODE_BINDING_INVALID", "ordinal is invalid")
        step = plan["steps"][ordinal]
        binding = {key: args[key] for key in ("run_id", "plan_id", "plan_fingerprint", "ordinal")}
        binding.update(
            sealed_plan=plan,
            step_id=step["step_id"],
            operation_id=curation.operation_id(args["plan_id"], ordinal, step["step_id"]),
        )
        return model.bind_curation_leaf(state, args["candidate"], args["leaf"], binding)
    if action == "attest_precommit":
        return model.attest_precommit(state, args["input_revision"])
    if action == "mark_attempt_started":
        return model.mark_attempt_started(state, args["candidate"], args["leaf"])
    if action == "reconcile_curation_leaf":
        outcome = model.VerifiedLeafOutcome(**evidence["outcome"])
        if (outcome.candidate_id, outcome.leaf_id, outcome.outcome) != (
            args["candidate"],
            args["leaf"],
            "committed",
        ):
            raise _error("EPISODE_TRANSITION_INVALID", "accepted outcome differs from command")
        return model.reconcile_leaf(state, outcome)
    return model.attest_postcommit(state, args["input_revision"], args["leaf_ids"])


class EpisodeStore:
    """Persist bounded accepted history; callers never supply materialized state."""

    def __init__(self, vault_root: Path):
        self.vault_root = Path(vault_root)
        self.curation = curation.CurationStore(self.vault_root)
        self.root = self.curation.root.parent / "episodes"

    def path(self, identity: str) -> Path:
        if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
            raise _error("EPISODE_ID_INVALID", "episode identity is invalid")
        return self.root / f"{identity}.json"

    def _guard(self):
        return active_manager().consistency_guard(
            self.vault_root, operation="episode-store", holder_kind="command"
        )

    @staticmethod
    def _encoded(journal: Mapping[str, Any]) -> str:
        encoded = model._json(journal)
        if len(encoded.encode()) > MAX_JOURNAL_BYTES:
            raise _error("EPISODE_TOO_LARGE", "episode journal exceeds its byte cap")
        return encoded

    def _reconstruct(self, identity: str, journal: Any) -> dict[str, Any]:
        try:
            if (
                not isinstance(journal, dict)
                or set(journal)
                != {"version", "episode_key", "input_evidence", "root_hash", "transitions"}
                or type(journal["version"]) is not int
                or journal["version"] != 1
                or not isinstance(journal["transitions"], list)
                or len(journal["transitions"]) > MAX_TRANSITIONS
            ):
                raise ValueError("invalid envelope")
            self._encoded(journal)
            state = model.start_episode(journal["episode_key"], journal["input_evidence"])
            if state["episode_id"] != identity:
                raise ValueError("wrong episode")
            digest = model._hash(
                "exomem-episode-journal-v1",
                {
                    key: value
                    for key, value in journal.items()
                    if key not in {"transitions", "root_hash"}
                },
            )
            if journal["root_hash"] != digest:
                raise ValueError("invalid original evidence digest")
            for revision, event in enumerate(journal["transitions"], start=2):
                if (
                    not isinstance(event, dict)
                    or set(event)
                    != {"revision", "previous_hash", "hash", "action", "args", "evidence"}
                    or type(event["revision"]) is not int
                    or event["revision"] != revision
                    or event["previous_hash"] != digest
                ):
                    raise ValueError("invalid event chain")
                digest = model._hash(
                    "exomem-episode-event-v1",
                    {key: value for key, value in event.items() if key != "hash"},
                )
                if digest != event["hash"]:
                    raise ValueError("invalid event digest")
                state = _apply_event(state, event)
            return {
                "revision": len(journal["transitions"]) + 1,
                "journal_digest": digest,
                "state": state,
                "coverage_current": "unchecked",
            }
        except (KeyError, TypeError, ValueError, IndexError, RecursionError) as error:
            raise _error("EPISODE_JOURNAL_INVALID", "episode history is invalid") from error

    def _load(self, identity: str) -> tuple[dict[str, Any], dict[str, Any]]:
        journal = self.curation._read_json(self.path(identity))
        return journal, self._reconstruct(identity, journal)

    def read(self, identity: str) -> dict[str, Any]:
        """Return historical state; no current evidence/disclosure claim is made."""
        with self._guard():
            return self._load(identity)[1]

    def _write(self, identity: str, journal: Mapping[str, Any], prior: Any = None) -> None:
        path = self.path(identity)
        self.curation._assert_safe(path)
        encoded = self._encoded(journal)
        try:
            batch_atomic_write(
                [
                    PlannedWrite(
                        path=path, content=encoded, create_only=prior is None, expected_hash=prior
                    )
                ],
                vault_root=self.vault_root,
                # Operational JSON is not an admitted knowledge/index input.
                post_commit_fanout=False,
            )
        except (
            BatchWriteError,
            ContentHashMismatchError,
            CreateOnlyConflict,
            OSError,
            ValueError,
        ) as error:
            raise _error(
                "EPISODE_STORE_WRITE_FAILED", "episode history write was refused"
            ) from error

    def create(self, key: str, input_evidence: Mapping[str, Any]) -> dict[str, Any]:
        """Retain already-authorized minimal evidence without resetting an episode."""
        initial = model.start_episode(key, input_evidence)
        evidence = {
            k: v for k, v in initial["input_revisions"][0]["evidence"].items() if k != "recovery"
        }
        identity = initial["episode_id"]
        journal = {"version": 1, "episode_key": key, "input_evidence": evidence, "transitions": []}
        journal["root_hash"] = model._hash(
            "exomem-episode-journal-v1",
            {key: value for key, value in journal.items() if key != "transitions"},
        )
        with self._guard():
            try:
                stored, state = self._load(identity)
            except curation.CurationError as error:
                if error.code != "CURATION_RUN_NOT_FOUND":
                    raise
            else:
                if stored["episode_key"] != key or stored["input_evidence"] != evidence:
                    raise _error("EPISODE_IDENTITY_COLLISION", "original episode evidence differs")
                return state
            state = self._reconstruct(identity, journal)
            self._write(identity, journal)
            return state

    def _accepted_evidence(
        self, state: dict[str, Any], action: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        if action == "bind_curation_leaf":
            plan = self.curation.load_plan(args["run_id"])
            identities = self.curation.identities(args["run_id"])
            if identities != (args["plan_id"], args["plan_fingerprint"]):
                raise _error("EPISODE_BINDING_INVALID", "stored curation identities differ")
            return {"sealed_plan": plan}
        if action == "reconcile_curation_leaf":
            verified = reconcile_curation_leaf(
                self.vault_root, state, args["candidate"], args["leaf"]
            )
            leaf = model._owned(verified, args["candidate"], args["leaf"])
            proof = leaf["outcome_proof"]
            return {
                "outcome": asdict(
                    model.VerifiedLeafOutcome(
                        candidate_id=args["candidate"],
                        leaf_id=args["leaf"],
                        effect_digest=leaf["effect_digest"],
                        outcome="committed",
                        **proof,
                    )
                )
            }
        if action == "attest_postcommit":
            for candidate in state["candidates"]:
                for leaf in candidate["leaves"]:
                    if leaf["outcome"] != "committed":
                        continue
                    check = model._copy(state)
                    original = model._owned(check, candidate["candidate_id"], leaf["leaf_id"])
                    original["outcome"], original["outcome_proof"] = "uncertain", None
                    verified = reconcile_curation_leaf(
                        self.vault_root, check, candidate["candidate_id"], leaf["leaf_id"]
                    )
                    current = model._owned(verified, candidate["candidate_id"], leaf["leaf_id"])
                    if current["outcome_proof"] != leaf["outcome_proof"]:
                        raise _error("EPISODE_OUTCOME_UNCERTAIN", "current commit proof differs")
        return {}

    def transition(
        self,
        identity: str,
        *,
        expected_revision: int,
        expected_digest: str,
        action: str,
        args: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Accept a finite command under CAS, then persist its validated result event.

        No command executes a curation effect. An owner must first durably record
        mark_attempt_started, then separately call the existing authorized writer.
        """
        args = model._copy(args)
        _validate_args(action, args)
        with self._guard():
            journal, current = self._load(identity)
            if (
                type(expected_revision) is not int
                or expected_revision != current["revision"]
                or expected_digest != current["journal_digest"]
            ):
                raise _error("EPISODE_REVISION_CONFLICT", "episode revision or digest changed")
            if action == "reconcile_curation_leaf":
                leaf = model._owned(current["state"], args["candidate"], args["leaf"])
                if leaf["outcome"] == "committed":
                    return current
            if len(journal["transitions"]) >= MAX_TRANSITIONS:
                raise _error("EPISODE_TOO_LARGE", "episode transition cap reached")
            prior = curation._digest(journal)
            try:
                evidence = self._accepted_evidence(current["state"], action, args)
                event = {
                    "revision": current["revision"] + 1,
                    "previous_hash": current["journal_digest"],
                    "action": action,
                    "args": args,
                    "evidence": evidence,
                }
                _apply_event(current["state"], event)
            except (KeyError, TypeError, IndexError) as error:
                raise _error(
                    "EPISODE_TRANSITION_INVALID", "transition values are invalid"
                ) from error
            event["hash"] = model._hash("exomem-episode-event-v1", event)
            journal["transitions"].append(event)
            result = self._reconstruct(identity, journal)
            self._write(identity, journal, prior)
            return result
