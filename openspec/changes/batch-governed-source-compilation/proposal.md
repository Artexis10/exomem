# Proposal

## Why

Grouped Source compilation still previews and commits each resulting note in separate calls. A batch could reduce those round trips without changing semantic authorship.

The measured five-Source case already completes in eight application calls. It does not establish that a new API pays its maintenance cost. This proposal defines the candidate and the comparison that must precede implementation.

## What Changes

- First compare one shared compilation context with the existing context-per-group workflow, using the current tools.
- If a writer batch is justified, add an optional batch form to the existing compiled-note creation operation.
- Preserve each note's preview, relation decision, source closure, authorization and terminal receipt.
- Return one outcome per item, including partial and unknown outcomes. Reconcile interrupted items before retrying them.
- Keep source grouping and note content with the calling agent. Add no server model, queue, worker or background compilation.
- Preserve existing single-note calls. Batch use is explicit; an unavailable batch leaves the existing workflow usable.

## Capabilities

### New Capabilities

- `governed-compilation-batches`: optional grouped transport of independently governed compiled-note creations.

### Modified Capabilities

None. Existing semantic, lineage, authorization and mutation requirements continue to apply to each note.

## Impact

The candidate would affect `commands.op_remember`, the shared note leaf, mutation receipts and the generated MCP, REST and CLI surfaces. It would add typed item envelopes and recovery tests, without replacing the atomic writer.

This delivery contains planning artifacts only. Implementation, a public tool name and an activation release remain unselected. The first-class bulk-import delivery does not depend on this proposal.
