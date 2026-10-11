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


def validate_stored_result(row: Mapping[str, object]) -> dict[str, object] | None:
    """Validate a stored observation; an absent key means historical unknown."""
    if "activation_result" not in row:
        return None
    observation = project_block(row["activation_result"])
    if observation is None or observation != row["activation_result"]:
        raise ValueError("invalid ledger activation result")
    if row.get("tool") != "activate_context" or row.get("outcome") == "error":
        raise ValueError("unexpected ledger activation result")
    observed_outcome = "abstained" if observation["abstained"] else "ok"
    if row.get("outcome") in {"ok", "abstained"} and row["outcome"] != observed_outcome:
        raise ValueError("ledger activation result disagrees with outcome")
    return observation


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
