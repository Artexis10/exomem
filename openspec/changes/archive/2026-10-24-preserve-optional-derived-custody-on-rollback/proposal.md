# Proposal

## Why

A settled receipt can still be replayed after an application rollback.
The previous reader rejects a retained catalogue component, so an empty work queue does not establish rollback compatibility.

## What Changes

- Distinguish components that an application can read from components that it executes.
- Preserve the documented optional catalogue component across replay and rollback without claiming its completion.
- Qualify a separately versioned earlier-application artifact before selecting it as an undo target.
- Keep canonical mutations, receipt history, content proofs and the existing managed handoff unchanged.
- Keep catalogue execution absent in the compatibility artifact; add no model, packet-mode change or live setting.

## Capabilities

### New Capabilities

- `derived-receipt-compatibility`: Preserve optional derived custody when application versions have different execution capabilities.

### Modified Capabilities

None. Existing mutation, acknowledgement and managed-handoff guarantees remain required.

## Impact

The compatibility surface includes `derived_receipts.py`, `derived_drain.py`, `writer_lease.py`, `index_sync.py` and `pending_recall.py`.
The delivery needs an earlier-application artifact with a distinct version, source identity and official producer receipt.
It does not activate maps, change canonical storage, convert receipt state or authorize a service outage.
The current feature release can qualify its source and artifacts independently; its installation needs a qualified undo target.
