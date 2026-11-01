## Context

`pad-restricted-direct-read-refusals` added one floor constant F, a `restricted_refusal_floor` attribute on registry commands, and a dispatcher wrapper that pads only refusals for a restricted caller. Receipts are per-machine hash chains in the vault, and each machine keeps the head of its own instance in a machine-local sidecar.

## Decisions

### D1. Reuse the floor; calibrate it on the new doors

The four doors take the existing attribute; the dispatcher and the surfaces need no change. F stays one constant. The withheld refusal work of each door is measured after its own early refusal lands (for example `connect_memory suggest-links` now refuses at the seed page before ranking). If a door's warm withheld refusal work exceeds F, F rises for every door, because a per-door floor would let the door's floor value itself say which door ran.

### D2. Inbound links pad their empty outcome

`list_inbound_links` deliberately answers a missing target with an empty success, and answers a withheld target the same way. Deciding a withheld target reads the page, so its work depends on existence and on page size, and it cannot be made equal to a missing target's work without weakening the decision. For a restricted caller, the door therefore completes the empty outcome of a withheld or a missing target no sooner than F, through the same deferred wait that the surfaces apply to refusals. A visible target is not padded, because the caller already knows that it exists. A restricted caller never receives the echoed `target`, which the terminal filter would strip only for a withheld target.

### D3. Empty-outcome receipts only where governance applies

The receipt schema already admits `outcomes: []`. Only a restricted caller can be withheld anything, so only its probes can turn a receipt-store fault into an existence oracle. The rule is: append when the caller is restricted. Appending for the owner as well, for example whenever the policy is non-empty, was rejected: it closes no oracle, writes a receipt for every owner zero-hit read, and makes owner reads such as `coordination_status` refuse during a receipt-store fault. The decision reads only the caller and the bound writer, so it loads no policy.

### D4. Report anchor state; do not invalidate unanchored chains

Treating a missing anchor as invalid was rejected. Each machine anchors only its own instance, so every synced multi-machine vault holds instances without a local anchor. Import also sets the sidecar aside. Invalidation would block custody cloning, legacy-v3 publication and operation recovery on those vaults, all of which require `valid`. Verification therefore keeps `valid` for internal consistency and anchor agreement, and adds `anchored` per instance and `unanchored_instances` overall, so a copied chain reads "consistent, unanchored" rather than a plain pass.

### D5. Metadata is owner-only

A non-owner needs neither the ids of rules and grants it cannot inspect nor the lock's contention counters. Both are returned to the owner only, by the same owner test that `inspection.list` already uses for scopes and the fingerprint.
