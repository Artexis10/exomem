## ADDED Requirements

### Requirement: Records changes carry deterministic installed-wheel proof

Every pull request that changes Records parsing, lifecycle actions, audit behavior, routing metadata, bootstrap placement, generated Records schemas, or connector promotion rules SHALL run the complete installed-wheel Records lifecycle in a fresh temporary vault. The proof SHALL use real stdio MCP dispatch, SHALL fail within bounded time rather than hang, and SHALL cover every affected action rather than infer coverage from tool discovery.

#### Scenario: Tool visibility is insufficient
- **WHEN** a changed Records action appears in `tools/list` but the installed lifecycle does not execute it successfully
- **THEN** the Records acceptance gate fails

#### Scenario: Full affected lifecycle passes locally
- **WHEN** a Records-affecting pull request runs the installed-wheel gate
- **THEN** the report names every required action and each action has a successful asserted outcome against the disposable vault

### Requirement: Deployed Records promotion requires disposable live MCP evidence

A changed Records surface SHALL NOT be promoted as connector-registered until a dedicated disposable HTTP/OAuth vault completes the full applicable lifecycle against the deployed release. The runner MAY produce unsigned content-free facts, but promotion SHALL require those facts inside the existing operator-signed live-evidence envelope and SHALL verify that signature with operator-held trust configuration rather than a key supplied by the evidence.

The exact closed evidence contract SHALL bind deployment SHA, release/package identities, canonical MCP surface digest, a run nonce, timestamp and expiry, disposable-vault purpose and reset epoch, principal and audience HMACs, exact client/model/system-contract versions, required action coverage, fixed prompt-case identifiers and hashes, restart result, per-mutation request/receipt identifiers and committed terminal outcomes, independent before/after readback hashes, and the coordinated graph-availability proof digest. Evidence from another digest, release, vault purpose, reset, principal, audience, or client contract; expired evidence; unverified readback; extra fields; or an incomplete action/case set SHALL refuse promotion. Exact byte-identical signed replay against the unchanged candidate SHALL be an idempotent no-op returning the same promotion result.

#### Scenario: Current deployed lifecycle authorizes promotion
- **WHEN** the disposable live runner completes every required action and both agent-selection clients against the deployed release and current surface digest
- **THEN** a promotion PR may record that exact structured evidence and clear the pending connector state

#### Scenario: Stale or prose-only evidence refuses promotion
- **WHEN** promotion metadata contains only free-form verification prose, tool-callability claims, unsigned runner output, or signed evidence for a different release, surface digest, run/reset identity, or client contract
- **THEN** the connector guardrail fails and the surface remains pending

#### Scenario: Mutation claims require independent readback
- **WHEN** a signed case claims a committed Records mutation but its receipt/request correlation or independently re-read before/after state does not prove the expected change
- **THEN** promotion refuses even if the client reported success

#### Scenario: Exact signed replay is idempotent
- **WHEN** the same byte-identical signed evidence and expected candidate digest are submitted again after promotion
- **THEN** the verifier returns the existing result without a second acceptance or record mutation
- **AND** evidence from an older reset epoch still refuses

### Requirement: Codex and Claude infer Records without prompt naming

The disposable live acceptance SHALL run fixed natural-language cases through Codex and Claude Code. Existing-collection cases SHALL state durable observed events without the words “save”, “log”, “record”, or “Records”; each client SHALL select the compatible Records collection and produce the expected guarded mutation. No-collection cases SHALL produce a Records collection proposal and SHALL NOT create a schema or write the observation into another knowledge layer.

#### Scenario: Existing collection is selected proactively
- **WHEN** a fixed prompt states an attributable observed measurement with no explicit persistence verb and exactly one compatible collection exists
- **THEN** each named client invokes the Records route, commits one item, and reports the mutation

#### Scenario: Empty vault produces a Records proposal
- **WHEN** a fixed prompt states durable observed data but the disposable vault has no compatible collection
- **THEN** each named client identifies Records as the destination, obtains the authoring contract, and proposes a collection without silently creating it

### Requirement: Graph rebuild cannot invalidate Records acceptance

Final Records live acceptance SHALL consume current evidence from the exact `graph-rebuild-availability` contract proving canonical batch plus versioned checkpoint publication, boundary release before join, checkpoint-aware single-flight, crash recovery, and committed derived-failure terminals. A timed-out or still-running rebuild SHALL remain observable and SHALL NOT leave later canonical mutations blocked behind an abandoned-looking global hold.

#### Scenario: Second canonical batch enters during rebuild
- **WHEN** one mutation has published its canonical batch and is awaiting a deliberately held off-boundary rebuild, and a second valid Records mutation begins
- **THEN** the second mutation enters the vault boundary and publishes its canonical batch without waiting for the rebuild work
- **AND** both callers may subsequently await and resolve from the same per-vault rebuild result

#### Scenario: Missing graph evidence blocks final promotion
- **WHEN** Records lifecycle evidence is green but the coordinated graph-rebuild availability evidence is absent for the release
- **THEN** the release cannot claim the reported reconcile-blocking incident fully resolved and connector promotion remains pending
