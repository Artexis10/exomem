## Why

A 2026-11-01 adversarial audit of the governed vault found doors where a restricted caller can still tell a withheld item from a missing one. It also found governed calls that leave no receipt, and copied vaults whose receipt chains verify as valid with nothing to verify them against. The byte-level oracles restore an existing contract and need no spec change. Three gaps need a contract:

- The fixed refusal floor covers only direct reads. `browse_memory`, `compile_source`, `connect_memory` and `query_dataset` refuse a withheld path after work that a missing path skips, so their completion time discloses existence.
- A governed call that touches nothing returns no receipt. When the receipt store fails, calls that touch an item refuse and calls that touch nothing succeed. A broken store therefore turns search into a word-existence oracle, and those probes are never audited.
- Chain verification treats a missing machine-local anchor as valid. A vault copy with a consistently rewritten chain reports `valid: true`.

Non-owners can also read governance metadata that only the owner needs: every rule and grant id from `govern_memory list`, and mutation-lock contention from `coordination_status`.

## What Changes

- The restricted refusal floor F extends to `browse_memory`, `compile_source`, `connect_memory` and `query_dataset`, marked through the existing `restricted_refusal_floor` registry attribute. `list_inbound_links` answers a withheld target as a success, so it does the same work for a withheld target as for a missing one instead.
- A governed boundary with no disclosure outcome appends a receipt with an empty outcome list when the caller is restricted. An unrestricted owner keeps the receipt-free path.
- Chain verification reports, for each instance, whether a local anchor covers it, and counts the unanchored instances. `valid` keeps its meaning. The audit category reports each unanchored instance as a warning. Restore admission keeps its stricter anchored-only rule.
- `govern_memory list` returns rule and grant ids to the owner only. `coordination_status` returns mutation-lock contention to the owner only.

## Not Claimed

- An unanchored instance is not proven untampered. The chain of another machine's instance can only be checked for internal consistency until a cross-machine anchor exists.
- Busy-refusal error details still carry the last holder and the acquire count to every caller. This is reported as follow-up work.

## Impact

- Code: `src/exomem/commands.py`, `src/exomem/governance/egress.py`, `src/exomem/governance/receipts.py`, `src/exomem/governance/inspection.py`, `src/exomem/query_data.py`, `src/exomem/audit.py`.
- Specs: `get-payload-shape` gains one requirement. `disclosure-evidence` changes "Exactly-once decision emission at the final representation" and "Chain verification via audit". `governance-authoring` changes "Read-only inspection operations".
- Cost: a restricted refusal on the four doors waits up to F. A restricted or governed call with no outcome appends one small receipt.
