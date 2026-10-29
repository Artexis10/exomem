# Proposal

## Why

Source reclassification keeps an undo stack, but later corrections overwrite reasons and reversals remove prior entries.
Readers need the retained classification record after undo, without changing captured content or the existing operations.

## What Changes

- Retain classification events beside the existing undo stack, including unchanged classifications and reversals.
- Preserve available legacy history without inventing missing reasons or their attribution.
- Bind each correction to its original source snapshot so concurrent metadata edits survive.
- Expose the record through existing governed frontmatter reads.

## Capabilities

### Modified Capabilities

- `command-surface`: Source corrections retain their classification events and refuse stale snapshots.

## Impact

The change affects Source reclassification and its existing transaction and read boundaries.
It adds no command, model, dependency, history resolver, automatic migration, or live Source operation.
Transport changes and installation remain separate delivery responsibilities.
