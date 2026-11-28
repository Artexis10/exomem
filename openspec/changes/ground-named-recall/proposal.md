# Ground recall in names the user supplies

## Why

A turn can name an ordinary note and a peripheral in the same request. The peripheral can take the turn slice while more incidental text ranks above the note in task discovery.

## What Changes

- Give a released note's existing title evidence precedence over incidental matches in the default task results.

## Capabilities

### New Capabilities

### Modified Capabilities

- `context-activation` gives directly named compiled notes precedence in the default task block.

## Impact

Task discovery reuses its current candidate pool and release checks. The projection orders the released results before it spends the task block's room.
