## ADDED Requirements

### Requirement: Bulk-operation leaves are due-state carriers

Mutating invocations of the bulk-operation leaves — `adopt_vault` mutating
modes, `adoption_studio` mutating actions, `maintain_memory` fix and
reconcile modes, `preserve_artifacts`, and `process_media` mutating
operations — SHALL carry the bounded advisory due-state block under the same
carrier contract and emission governance as page writes: the block is served
through the release plane, validated and bounded, never a key a client
branches on for operation outcome, emission is recorded once per delivered
block, family dispositions apply, and an unreadable review state yields no
block while the operation still completes. One invocation SHALL be one batch
scope: at most one block, at the end of the invocation, under the unchanged
change-only rule. Read-only invocations of the same leaves SHALL NOT carry
the block. The leaves SHALL reuse the shared due-state helpers rather than
re-deriving any of it, and tool input schemas SHALL NOT change.

#### Scenario: A bulk apply reports accumulation once, at the end

- **WHEN** an `adoption_studio` apply commits many governed writes whose
  deltas change the due-state counts
- **THEN** the invocation's response carries at most one `due_state` block,
  reflecting the projection after the batch
- **AND** the operation outcome keys are byte-identical to a projection-free
  response apart from the advisory block

#### Scenario: A read-only invocation stays clean

- **WHEN** `adopt_vault` runs in scan-only mode
- **THEN** the response carries no `due_state` block

#### Scenario: Unreadable review state never blocks the bulk operation

- **WHEN** the review state cannot be read while a `process_media` processing
  invocation completes
- **THEN** the invocation completes with its existing terminal unchanged and
  no `due_state` block
