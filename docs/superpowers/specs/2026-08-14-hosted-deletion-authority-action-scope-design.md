# Hosted deletion authority action scope

## Problem

Lifecycle maintenance rows (`quiesce` and `seal`) and a deletion row can share a
tenant and external operation ID. `DeletionClaimAuthority` previously loaded an
operation using only that pair, so database row order could select a completed
maintenance row before the active `destroy` claim. The deletion worker then
treated its own claim as unavailable and retried with `deletion-lock-busy`.

## Design

Keep the existing authority data flow and locking order: read the database
clock, lock the tenant fence, then lock the one candidate operation. The query
itself requires the deletion worker's explicit action set (`discard` and
`destroy`) plus the caller's fence, a live `claimed` state, a claim token, and
an unexpired lease. `scalar_one_or_none()` makes an unexpected second live
deletion claim fail closed rather than selecting an arbitrary row. The existing
post-query guards remain as defence in depth.

## Alternatives rejected

Making external operation IDs globally unique across actions would change the
durable contract and require a migration for valid lifecycle histories. Adding
ordering to the ambiguous query would still make authority depend on an
incidental row choice. Retrying after a maintenance-row match leaves the worker
in the same permanent pending loop. Scoping only to destructive actions still
permits a final `discard` to shadow a live `destroy`. The active-claim predicate
is the smallest least-privilege boundary that selects the authoritative
deletion claim.

## Verification

Repository-level authority tests create final `quiesce`, `seal`, and `discard`
rows plus a claimed `destroy` row with one tenant/external operation pair, and
require authority acquisition to accept the destroy claim. A companion test
verifies that a non-deletion row alone is never accepted.
