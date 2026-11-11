"""The held-out gate, and the labels every outcome report carries.

A held-out run is a comparative, decision-bearing run. Before its first session it
needs two things: the family's amendment acknowledged, and every held-out input file
equal to the digest the amendment receipt froze. A dev run needs neither and says so.
Only a gated held-out run by the frozen actor and judge is stamped as decision evidence.
"""

from __future__ import annotations

from pathlib import Path

from epistemic.amendments import require_family_released
from protocol.contracts import (
    AmendmentAcknowledgmentPendingError,
    ContractIdentityError,
    working_amendment_receipts,
)

from . import billing
from .isolation import ACTOR_EFFORT, ACTOR_MODEL, REPO_ROOT
from .judging import DECISION_JUDGE
from .record import checkout_state
from .workspace import RUN_ROOT, sha256_of

#: The pre-registered family each suite is filed under.
FAMILIES = {"clbench": "f35", "lab": "f36", "planted": "f37"}
#: Where each suite's inputs sit under the run root; the receipt names files by these paths.
PREFIXES = {
    "clbench": "upstream/clbench/",
    "lab": "cases/lab/",
    "planted": "cases/planted/",
}
#: CL-Bench's two databases come from a dataset without a revision pin: the receipt's digests freeze them.
CLBENCH_DATABASES = (
    "upstream/clbench/data/database_exploration/products.db",
    "upstream/clbench/data/database_exploration/products_drifted.db",
)
#: The inputs a decision cannot run without, per suite: every file the amendment freezes. The
#: receipt must freeze each of them, and a held-out run checks each against its digest.
REQUIRED = {
    "clbench": (
        "upstream/clbench/data/database_exploration/questions.json",
        "upstream/clbench/data/database_exploration/questions_post_drift.json",
        *CLBENCH_DATABASES,
    ),
    "lab": ("cases/lab/heldout.json",),
    "planted": ("cases/planted/heldout.jsonl", "cases/planted/distractors.jsonl"),
}
#: The amendment sequence this harness implements: it reads the decisions by that amendment's arms,
#: scale and judge, so no run is a decision until that receipt is acknowledged.
HARNESS_AMENDMENT = 10
#: The sequence-11 billing amendment releases held-out runs and Claude judges paid
#: from API credits as decision evidence. It releases no gate: a held-out run still
#: waits on HARNESS_AMENDMENT.
BILLING_AMENDMENT = 11
LABEL = "internal diagnostic"


class HeldoutGateError(RuntimeError):
    """A held-out run was refused; the message names what to fix."""


def frozen_digests(suite: str, repo_root: Path = REPO_ROOT) -> dict[str, str]:
    """The receipt's ``fixture_sha256`` entries under the suite's path prefix."""
    try:
        receipts = working_amendment_receipts(repo_root)
    except (ContractIdentityError, OSError) as error:
        raise HeldoutGateError(f"the amendment receipts cannot be read: {error}") from error
    frozen: dict[str, str] = {}
    for receipt in receipts:
        for path, digest in (receipt.fixture_sha256 or {}).items():
            if not path.startswith(PREFIXES[suite]):
                continue
            if frozen.setdefault(path, digest) != digest:
                raise HeldoutGateError(f"two receipts freeze {path} with different digests")
    missing = [path for path in REQUIRED[suite] if path not in frozen]
    if missing:
        raise HeldoutGateError(
            f"no receipt freezes {', '.join(missing)}; the {FAMILIES[suite]} amendment "
            f"must record its digest before a held-out {suite} run"
        )
    return frozen


def _amendment_acknowledged(sequence: int, repo_root: Path) -> bool:
    try:
        receipts = working_amendment_receipts(repo_root)
    except (ContractIdentityError, OSError):
        return False
    return any(
        receipt.sequence == sequence and receipt.acknowledgment_status == "acknowledged"
        for receipt in receipts
    )


def harness_released(repo_root: Path = REPO_ROOT) -> bool:
    """Whether the amendment this harness implements is acknowledged. Receipts that cannot be read say no."""
    return _amendment_acknowledged(HARNESS_AMENDMENT, repo_root)


def billing_released(repo_root: Path = REPO_ROOT) -> bool:
    """Whether the billing amendment is acknowledged. Receipts that cannot be read say no."""
    return _amendment_acknowledged(BILLING_AMENDMENT, repo_root)


def heldout_gate(
    suite: str, root: Path = RUN_ROOT, repo_root: Path = REPO_ROOT
) -> dict[str, object]:
    """Refuse a held-out run unless its family is released and its inputs match the receipt.

    Raises ``AmendmentAcknowledgmentPendingError`` while the family's amendment or the harness's
    amendment (``HARNESS_AMENDMENT``) is unacknowledged,
    and ``HeldoutGateError`` for a missing or changed input, or a product checkout with uncommitted
    changes: a decision run names a revision, and a dirty checkout is not one. Returns the verified digests.
    """
    if checkout_state(repo_root)["product_dirty"]:
        raise HeldoutGateError(
            f"the product checkout at {repo_root} has uncommitted changes; a decision run needs a clean checkout"
        )
    require_family_released(FAMILIES[suite], repo_root=repo_root)
    if not harness_released(repo_root):
        raise AmendmentAcknowledgmentPendingError(
            f"amendment sequence {HARNESS_AMENDMENT} founder acknowledgment is pending; "
            f"this harness implements it, so a held-out {suite} run is not a decision until then"
        )
    verified: dict[str, str] = {}
    for relative, expected in sorted(frozen_digests(suite, repo_root).items()):
        path = root / relative
        if not path.is_file():
            raise HeldoutGateError(f"{path} is missing; the receipt freezes digest {expected}")
        actual = sha256_of(path)
        if actual != expected:
            raise HeldoutGateError(
                f"{path} differs from the receipt: expected {expected}, found {actual}"
            )
        verified[relative] = actual
    return {"family": FAMILIES[suite], "verified_sha256": verified}


def _decision_billing(declared: str | None, repo_root: Path) -> bool:
    """Whether a billing declaration may stand behind decision evidence.

    The subscription may decide under sequence 10 alone; credits may decide only once sequence 11
    is acknowledged. A name outside ``billing.KEY_SOURCES``, or none at all, is never decision billing.
    """
    return (
        declared == billing.DEFAULT
        or declared in billing.KEY_SOURCES
        and billing_released(repo_root)
    )


def evidence_of(report: dict, stored: dict, repo_root: Path = REPO_ROOT) -> str:
    """What a report is worth, from what the run recorded: ``instrument``, ``void``, ``incomplete`` or ``decision``.

    A decision report is a gated held-out run whose actor and judge are the frozen ones and whose
    decision no harness fault voided and nothing leaves incomplete, billed to the subscription or — once the
    sequence-11 amendment is acknowledged — to API credits, while the harness's amendment is
    acknowledged. Anything else is instrument evidence, whatever the split says.
    """
    if stored["split"] != "heldout" or not stored.get("gate"):
        return "instrument"
    if (stored["model"], stored["effort"]) != (ACTOR_MODEL, ACTOR_EFFORT):
        return "instrument"
    if report["actor"]["model_reported"] != [ACTOR_MODEL]:
        return "instrument"
    # The preregistration pays decision runs from the subscription or, after sequence 11, from API credits.
    if not _decision_billing(stored.get("billing", billing.DEFAULT), repo_root):
        return "instrument"
    judge = report.get("judge")
    if judge is not None and (judge["backend"], judge["model"]) != DECISION_JUDGE:
        return "instrument"
    if (
        judge is not None
        and judge["backend"] == "claude"
        and not _decision_billing(judge.get("billing"), repo_root)
    ):
        return "instrument"
    status = report.get("decision", {}).get("status")
    if status in ("void", "incomplete"):
        return status
    return "decision" if harness_released(repo_root) else "instrument"


def stamp(report: dict, stored: dict) -> dict:
    """Every report says what it is: an internal diagnostic, and instrument, void, incomplete or decision evidence."""
    return {
        "label": LABEL,
        "evidence": evidence_of(report, stored),
        "split": stored["split"],
        **report,
    }
