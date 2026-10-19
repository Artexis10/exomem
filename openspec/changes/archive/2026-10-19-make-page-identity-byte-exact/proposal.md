# Proposal

## Why

Text-mode reads can normalize newlines before hashing a page. Different subsystems then disagree about the identity of the same stored bytes, blocking valid writes and derived visibility.

## What Changes

- Use stored-byte SHA-256 for page identity, optimistic guards, and derived custody.
- Preserve line endings when adding an identity to an existing page.
- Keep deliberate semantic fingerprints separate from canonical page identity.
- Refresh derived hashes through existing recovery without rewriting canonical pages.

## Capabilities

### Modified Capabilities

- `transactional-vault-writes`: Require consistent byte identity across readers, write guards, and derived consumers.

## Impact

The change affects page reads in mutation preparation, media sidecars, semantic parent state, and identity backfill. It adds no dependency, model, flag, or automatic canonical migration.
