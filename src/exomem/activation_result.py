"""Content-free observations of the final activation packet."""

from collections.abc import Mapping
from dataclasses import dataclass

# The call-ledger specification fixes these protocol values, not vault vocabulary.
AMBIGUOUS = "ambiguous"
UNRESOLVED = "unresolved"
INDEX_WARMING = "index_warming"
UNAVAILABLE = "unavailable"
DISABLED = "disabled"
WITHHELD = "withheld"
REASONS = frozenset({AMBIGUOUS, UNRESOLVED, INDEX_WARMING, UNAVAILABLE, DISABLED, WITHHELD})


def project_block(value: object) -> dict[str, object] | None:
    """Copy only strict booleans and a fixed reason into the ledger shape."""
    if not isinstance(value, Mapping):
        return None
    abstained, limited = value.get("abstained"), value.get("budget_limited")
    if type(abstained) is not bool or type(limited) is not bool:
        return None
    reason = value.get("reason")
    return {
        "abstained": abstained,
        "reason": reason if type(reason) is str and reason in REASONS else None,
        "budget_limited": limited,
    }


@dataclass
class Observation:
    """One request's shared cell; never retains the packet or its material."""

    abstained: bool | None = None
    result: dict[str, object] | None = None

    def capture(self, packet: object) -> None:
        if not isinstance(packet, Mapping):
            return
        flag = packet.get("abstained")
        self.abstained = flag if type(flag) is bool else None
        abstention = packet.get("abstention")
        budget = packet.get("request_budget")
        limited = (
            False if "request_budget" not in packet
            else budget.get("applied") if isinstance(budget, Mapping) else None
        )
        self.result = project_block({
            "abstained": self.abstained,
            "reason": abstention.get("reason") if isinstance(abstention, Mapping) else None,
            "budget_limited": limited,
        })
