## ADDED Requirements

### Requirement: Terminal Surface Renders Only Governed Command Output
The terminal review surface SHALL obtain every piece of vault data through
the existing product-command registry (the same commands the CLI exposes,
with their JSON envelopes) and SHALL NOT read vault files directly,
recompute ranking or severity, or re-derive epistemic state client-side. A
capability absent from the registry response SHALL render as absent rather
than being inferred.

#### Scenario: Data path is the command registry
- **WHEN** the surface renders a review queue, item workspace, or evolution
  chain
- **THEN** the rendered data originates from product-command responses and a
  governance-withheld item renders its withhold notice rather than content

### Requirement: Current Truth Is Never Conflated With History
Wherever a conclusion appears, the surface SHALL distinguish active from
superseded state, SHALL render the supersession/evolution chain with the
recorded rationale per transition, and MUST NOT present a superseded
conclusion as current.

#### Scenario: Superseded conclusion display
- **WHEN** an item whose page carries superseded status is rendered in any
  list or workspace
- **THEN** it is visibly marked superseded with a path to its successor

### Requirement: Queue Triage Preserves Fingerprint Binding
Queue views SHALL expose dismiss, snooze, and reopen exactly through the
existing triage command, binding each decision to the signal fingerprint the
queue item carried; the surface MUST NOT fabricate or reuse fingerprints
across items.

#### Scenario: Triage round-trip
- **WHEN** the user dismisses a queue item in the surface
- **THEN** the triage command is invoked with that item's reference and
  fingerprint and the queue re-renders from a fresh command response

### Requirement: Honest Trust Signals Only
The surface SHALL render only trust signals the substrate provides —
citations and sources, recency, contradiction/duplicate flags, inbound
connectivity, authority of cited sources — and MUST NOT display numeric
confidence scores or any invented certainty indicator.

#### Scenario: No invented scores
- **WHEN** any conclusion is rendered
- **THEN** no numeric confidence or certainty score appears anywhere in the
  surface

### Requirement: Retrieval Status Visibility
The surface SHALL display automatic-retrieval status per configured client:
whether the product hooks are installed, whether the most recent
retrieve-nudge fired or injected context, and current cooldown state, sourced
from hook installation state and recorded traces.

#### Scenario: Hooks not installed
- **WHEN** no hooks are installed for any client
- **THEN** the status panel states that seamless retrieval is off and names
  the install command

### Requirement: Destructive Operations Stay In The CLI
The surface MUST NOT execute supersession, deletion, merges, schema edits,
or governance policy changes; where such an action is contextually relevant
it SHALL surface the exact CLI command instead. Surface-initiated writes are
limited to triage, relation acceptance, and pack selection through their
existing governed commands, each with an honest preview where the underlying
command supports dry-run.

#### Scenario: Destructive action requested
- **WHEN** the user selects an affordance related to superseding or deleting
  a page
- **THEN** the surface displays the exact CLI command and performs no write

### Requirement: Terminal Robustness Contract
The surface SHALL restore the terminal (cursor visibility, modes) on every
exit path including exceptions and interrupts; a cancelled or failed action
SHALL return to the surface without terminating it; and the active context
(vault path, governance on/off, service state) SHALL remain permanently
visible while the surface is open.

#### Scenario: Interrupt during an action
- **WHEN** the user interrupts a running action with Ctrl-C
- **THEN** the action cancels, the surface remains usable, and the terminal
  is not left in a broken state
