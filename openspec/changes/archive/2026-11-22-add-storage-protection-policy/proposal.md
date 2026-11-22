# Proposal

## Why

Exomem records exact replicas and detects damaged copies. An owner still has to compare that evidence against a storage goal and request copies by store. Eviction also has no vault-wide way to require enough independent recoverable copies.

## What Changes

- Add an optional owner policy for minimum verified copies, minimum distinct failure domains, and copy spending by configured storage allowance.
- Give the existing media worker bounded work to add and verify missing copies.
- Require the remaining verified custody to meet the policy before an owner-authorized eviction. Managed placement keeps the last verified recoverable copy.
- Report an unmet or unknown policy with the evidence gap and last observation. Vaults without a policy continue on the existing paths.

## Capabilities

### New Capabilities

- `original-protection-policy`: Owner storage goals, additive repair, and verified custody at removal.

## Impact

The media worker and replica ledger share the existing binding, key export, fixity, and queue evidence. The media status surface reports progress. Existing providers and backup workflows keep their present roles. The change runs without a model and defaults off.
