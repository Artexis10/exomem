## Why

`add-governed-curation-lane` ships content curation. Its relocation steps
(`move`, `delete`, `recover`) are accepted in the plan schema but fail closed
with `CURATION_RENAME_HISTORY_UNPROVABLE` at proposal, preview, and apply,
because the held-filesystem ABI exposes no durable monotonic parent-namespace
generation token. Without one, a stable file identity plus a post-crash
placement hash cannot distinguish a completed rename from a crash followed by an
external inverse rename, so curation must not publish or authorize a rename
transition on that base.

That refusal is the correct v1 behavior, and the v1 change now says so. This
change carries the prepared-relocation protocol that would lift the refusal, so
the canonical spec never promises an unbuilt protocol when
`add-governed-curation-lane` archives.

## What Changes

- Add the prepared-relocation protocol: ordered create-only relocation candidate
  and authorization records published and durably flushed before any rename,
  binding the immutable plan, approval, operation, and preparation identities.
- Add durable monotonic parent-namespace generation tokens for both retained
  parents, captured under a capability probe, restart-comparable and
  non-repeating within a filesystem epoch, and refuse before candidate
  publication when unsupported.
- Add exact-target recovery that adopts the bound desired placement, rolls
  forward only the missing bound suffix, and issues no executor rename.
- Add the ambiguity and unprovable-history blocking rules
  (`CURATION_OUTCOME_UNCERTAIN`, `CURATION_RENAME_HISTORY_UNPROVABLE`) as
  protocol outcomes rather than a blanket pre-dispatch refusal.
- Replace the v1 blanket relocation refusal only when the protocol is proven end
  to end; until then the v1 fail-closed requirement stands.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `governed-curation-lane`: relocation steps gain a prepared-relocation
  execution and recovery protocol in place of the v1 fail-closed refusal.

## Impact

`src/exomem/curation.py` relocation preparation and execution, the held
filesystem ABI in `src/exomem/reserved_paths.py` and `src/exomem/move_file.py`,
and the governed run store's transition records. No public command schema, tool
contract, or content-step behavior changes. Requires a held-filesystem
capability that does not exist today, so this change cannot start until that
ABI work lands.
