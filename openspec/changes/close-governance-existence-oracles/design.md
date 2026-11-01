## Context

`pad-restricted-direct-read-refusals` added one floor constant F, a `restricted_refusal_floor` attribute on registry commands, and a dispatcher wrapper that pads only refusals for a restricted caller. Receipts are per-machine hash chains in the vault, and each machine keeps the head of its own instance in a machine-local sidecar.

## Decisions

### D1. Reuse the floor; calibrate it on the new doors

The four doors take the existing attribute; the dispatcher and the surfaces need no change. F stays one constant. The withheld refusal work of each door is measured after its own early refusal lands (for example `connect_memory suggest-links` now refuses at the seed page before ranking). If a door's warm withheld refusal work exceeds F, F rises for every door, because a per-door floor would let the door's floor value itself say which door ran.

### D2. Inbound links equalise work instead of padding

`list_inbound_links` deliberately answers a missing target with an empty success, and answers a withheld target the same way. The floor pads only refusals, so it cannot cover this door. The withheld and missing branches run the same visibility work, and a restricted caller never receives the echoed `target`, which the terminal filter would strip only for a withheld target.

### D3. Empty-outcome receipts only where governance applies

The receipt schema already admits `outcomes: []`. Appending one for every call would put a receipt write on the owner's ungoverned fast path, which governs nothing. The rule is: append when the caller is restricted or the compiled policy is non-empty. A broken store then refuses every governed call alike.

### D4. Report anchor state; do not invalidate unanchored chains

Treating a missing anchor as invalid was rejected. Each machine anchors only its own instance, so every synced multi-machine vault holds instances without a local anchor. Import also sets the sidecar aside. Invalidation would block custody cloning, legacy-v3 publication and operation recovery on those vaults, all of which require `valid`. Verification therefore keeps `valid` for internal consistency and anchor agreement, and adds `anchored` per instance and `unanchored_instances` overall, so a copied chain reads "consistent, unanchored" rather than a plain pass.

### D5. Metadata is owner-only

A non-owner needs neither the ids of rules and grants it cannot inspect nor the lock's contention counters. Both are returned to the owner only, by the same owner test that `inspection.list` already uses for scopes and the fingerprint.
