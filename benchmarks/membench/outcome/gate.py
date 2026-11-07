"""The held-out gate, and the labels every outcome report carries.

A held-out run is a comparative, decision-bearing run. Before its first session it
needs two things: the family's amendment acknowledged, and every held-out input file
equal to the digest the amendment receipt froze. A dev run needs neither and says so.
Only a gated held-out run by the frozen actor and judge is stamped as decision evidence.
"""

from __future__ import annotations

from pathlib import Path

from epistemic.amendments import require_family_released
from protocol.contracts import ContractIdentityError, working_amendment_receipts

from .isolation import ACTOR_EFFORT, ACTOR_MODEL, REPO_ROOT
from .judging import DECISION_JUDGE
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


def heldout_gate(
    suite: str, root: Path = RUN_ROOT, repo_root: Path = REPO_ROOT
) -> dict[str, object]:
    """Refuse a held-out run unless its family is released and its inputs match the receipt.

    Raises ``AmendmentAcknowledgmentPendingError`` while the amendment is unacknowledged,
    and ``HeldoutGateError`` for a missing or changed input. Returns the verified digests.
    """
    require_family_released(FAMILIES[suite], repo_root=repo_root)
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


def evidence_of(report: dict, stored: dict) -> str:
    """What a report is worth, from what the run recorded: ``instrument``, ``void`` or ``decision``.

    A decision report is a gated held-out run whose actor and judge are the frozen ones and whose
    decision no harness fault voided. Anything else is instrument evidence, whatever the split says.
    """
    if stored["split"] != "heldout" or not stored.get("gate"):
        return "instrument"
    if (stored["model"], stored["effort"]) != (ACTOR_MODEL, ACTOR_EFFORT):
        return "instrument"
    if report["actor"]["model_reported"] != [ACTOR_MODEL]:
        return "instrument"
    judge = report.get("judge")
    if judge is not None and (judge["backend"], judge["model"]) != DECISION_JUDGE:
        return "instrument"
    return "void" if report.get("decision", {}).get("status") == "void" else "decision"


def stamp(report: dict, stored: dict) -> dict:
    """Every report says what it is: an internal diagnostic, and instrument, void or decision evidence."""
    return {
        "label": LABEL,
        "evidence": evidence_of(report, stored),
        "split": stored["split"],
        **report,
    }
