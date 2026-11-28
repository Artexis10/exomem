# Proposal

## Why

A dated observation written without a kind lands after the last section of its note. People looking through the observation list can miss it.

A note with a custom replacement kind also makes a compact observation harder to correct. An ordinary correction asks the caller to select between the shipped kind and the custom one.

## What Changes

- Save implicitly dated observations as rich blocks within the note's `## Observations` section. Create that section when the note has none.
- Put compact lines above child headings so each dated block has a clear end. Content edits preserve the block's place when its text still fits there.
- When a compact observation gains a date, move it to the dated part of the same section and keep its anchor.
- Use the shipped replacement kind when correcting a compact observation without a kind. An explicit request can still select a custom kind.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `semantic-write-contract` defines where a dated observation is saved, how later writes preserve the section, and how a compact correction chooses its kind.

## Impact

The existing governed observation writer chooses the placement for MCP, REST and CLI. The current Markdown grammar reads the nested blocks. The rollout applies to new or edited units.
