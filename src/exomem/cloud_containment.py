"""The cell's containment checks against a compromised controller, in report mode.

contain-cloud-controller-compromise D1 and D4: a cell refuses a request that
carries no gateway proof, and refuses an export, import or restore that carries
no Substrate grant or that names a destination the grant does not. This release
only reports what it would refuse and serves exactly as before. A later release
changes `ENFORCE`; the cell image's minimum-release floor then stops a rollback.

Every event is cataloged in `log_events.EVENT_CATALOG` and content-free: a
reason token, the cell ID and, for an operation, its ID. It never carries a
token, a claim, a URL or a tenant's content.
"""

from __future__ import annotations

import logging
import re
import sys
from enum import StrEnum

from . import cloud_cell
from .log_events import KeyValueFormatter, log_event

#: Report mode (False) or enforce mode (True). This is a constant of the image,
#: never an environment value, flag or rendered argument, so nothing that
#: `cellctl` writes can select the mode. Each check takes an `enforce`
#: parameter that defaults to it, so a test exercises the enforce branch by
#: argument and the switch to enforce is this one line.
ENFORCE = False

REQUEST_EVENT = "cloud_request_credential"
OPERATION_EVENT = "cloud_operation_check"

log = logging.getLogger(__name__)


class Credential(StrEnum):
    """Which credential a request used, from the bearer's validity and the proof's alone (D1).

    A cell without the proof key reports `bearer_only` for a valid bearer, so the
    query "no bearer-only requests" covers it. The `proof` field says why.
    """

    BOTH = "both"
    BEARER_ONLY = "bearer_only"
    PROOF_ONLY = "proof_only"
    NEITHER = "neither"


class Proof(StrEnum):
    """What the cell found of the gateway proof, so that a forged one differs from a missing one."""

    VALID = "valid"
    INVALID = "invalid"
    ABSENT = "absent"
    UNCONFIGURED = "unconfigured"


class Verdict(StrEnum):
    """What a check did: let the request pass, would refuse it (report mode) or refused it (enforce mode)."""

    PASS = "pass"
    WOULD_REFUSE = "would_refuse"
    REFUSED = "refused"


def refusal_verdict(enforce: bool) -> Verdict:
    return Verdict.REFUSED if enforce else Verdict.WOULD_REFUSE


#: What an event logs for an ID that is not in its canonical form.
INVALID_ID = "invalid"


def emit(event: str, verdict: Verdict, *, cell_id: object, operation_id: object = None, **fields: str) -> None:
    """Log one containment event. A check that passes logs at INFO; one that refuses logs at WARNING.

    The cell ID comes from a Job's environment and the operation ID from the controller's
    argument, and not every caller checks them. This one path therefore logs an ID only in
    its canonical form, so a newline or a `key=value` pair in it cannot forge an event line.
    """
    fields["cell_id"] = (
        cell_id if isinstance(cell_id, str) and re.fullmatch(cloud_cell.CELL_ID_PATTERN, cell_id) else INVALID_ID)
    if operation_id is not None:
        fields["operation_id"] = cloud_cell.canonical_app_id(operation_id) or INVALID_ID
    log_event(log, logging.INFO if verdict is Verdict.PASS else logging.WARNING, event,
              fields={**fields, "verdict": verdict})


def judge_request(*, bearer_valid: bool, proof: Proof, cell_id: object, enforce: bool = ENFORCE) -> Verdict:
    """Log the credential an MCP request used and return the proof gate's verdict.

    The gate passes only on a valid proof. Whether the bearer is also valid
    changes the credential that the event names, never the verdict: the bearer
    check itself stays with the server's own authentication.
    """
    if proof is Proof.VALID:
        credential = Credential.BOTH if bearer_valid else Credential.PROOF_ONLY
    else:
        credential = Credential.BEARER_ONLY if bearer_valid else Credential.NEITHER
    verdict = Verdict.PASS if proof is Proof.VALID else refusal_verdict(enforce)
    emit(REQUEST_EVENT, verdict, credential=credential, proof=proof, cell_id=cell_id)
    return verdict


class _StderrHandler(logging.StreamHandler):
    """Writes to the `sys.stderr` of the moment, so a redirected stream is honoured."""

    def __init__(self) -> None:
        logging.Handler.__init__(self)

    @property
    def stream(self):
        return sys.stderr


def log_to_stderr() -> None:
    """Send this module's events to the pod log of a Job, in the one `key=value` rendering.

    A serving cell writes them to its JSONL log. A Job configures no logging,
    and its termination message is only for the result, so it adds a handler.
    The logger still propagates: a Job's root logger has no handler, so nothing prints twice.
    """
    if log.handlers:
        return
    handler = _StderrHandler()
    handler.setFormatter(KeyValueFormatter())
    log.addHandler(handler)
    log.setLevel(logging.INFO)
