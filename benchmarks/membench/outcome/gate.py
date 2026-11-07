"""The held-out gate, and the labels every outcome report carries.

A held-out run is a comparative, decision-bearing run. Before its first session it
needs two things: the family's amendment acknowledged, and every held-out input file
equal to the digest the amendment receipt froze. A dev run needs neither and says so.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from epistemic.amendments import require_family_released
from protocol.contracts import ContractIdentityError, working_amendment_receipts

from .isolation import REPO_ROOT
from .workspace import RUN_ROOT

#: The pre-registered family each suite is filed under.
FAMILIES = {"clbench": "f35", "lab": "f36", "planted": "f37"}
#: Where each suite's inputs sit under the run root; the receipt names files by these paths.
PREFIXES = {
    "clbench": "upstream/clbench/",
    "lab": "cases/lab/",
    "planted": "cases/planted/",
}
#: The input a decision cannot run without, per suite. The receipt must freeze each of them.
REQUIRED = {
    "clbench": ("upstream/clbench/data/database_exploration/questions.json",),
    "lab": ("cases/lab/heldout.json",),
    "planted": ("cases/planted/heldout.jsonl",),
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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


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
        actual = sha256_file(path)
        if actual != expected:
            raise HeldoutGateError(
                f"{path} differs from the receipt: expected {expected}, found {actual}"
            )
        verified[relative] = actual
    return {"family": FAMILIES[suite], "verified_sha256": verified}


def stamp(report: dict, split: str) -> dict:
    """Every report says what it is: an internal diagnostic, and instrument or decision evidence."""
    return {
        "label": LABEL,
        "evidence": "decision" if split == "heldout" else "instrument",
        "split": split,
        **report,
    }
