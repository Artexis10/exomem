## MODIFIED Requirements

### Requirement: Codex and Claude infer Records without prompt naming

The disposable live acceptance SHALL run fixed natural-language cases through Codex and Claude Code. Existing-collection cases SHALL state durable observed events without the words “save”, “log”, “record” or “Records”, and each client SHALL select the compatible collection and produce the expected guarded mutation. No-collection cases SHALL end in a proposal under `advisory` capture or a bounded creation under `silent` capture, and never in a write to another knowledge layer.

#### Scenario: Existing collection is selected proactively
- **WHEN** a fixed prompt states an attributable observed measurement with no explicit persistence verb and exactly one compatible collection exists
- **THEN** each named client invokes the Records route, commits one item, and reports the mutation

#### Scenario: Empty vault produces a Records proposal
- **WHEN** a fixed prompt states durable observed data, the disposable vault has no compatible collection, and the effective `proactive_capture` is `advisory`
- **THEN** each named client identifies Records as the destination, obtains the authoring contract, and proposes a collection without silently creating it

#### Scenario: Empty vault under silent capture gets a bounded collection
- **WHEN** a fixed prompt states recurring observed data, the disposable vault has no compatible collection, and the effective `proactive_capture` is `silent`
- **THEN** each named client creates one minimal Records collection with `automatic: true` within the server's bounds, commits the observation to it, and reports both
