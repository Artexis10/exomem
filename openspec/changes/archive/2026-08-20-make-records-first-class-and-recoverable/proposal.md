## Why

Records are technically callable but still easy for agents to miss, and the current manifest lifecycle can accept a collection that immediate inspection rejects without providing an audited repair path. The release process then compounds the problem by proving tool visibility rather than the complete deployed Records workflow.

## What Changes

- Make `record` a first-class agent action for inferred observed state: measurements, sessions, transactions, maintenance, symptoms, inventory changes, and other durable events route to Records even when the user does not name Records or explicitly ask to save.
- Make compact bootstrap and MCP discovery salient and action-specific, with Records routing before large semantic-authoring detail and a tested size/position budget.
- Make saved-view and manifest validation eager and shared so a manifest accepted by `validate` is also accepted by `create`, `inspect`, and saved-view query resolution.
- Extend `record_memory` with read-only revision validation plus guarded, audited `revise` and `rebaseline` actions for existing collection manifests.
- Preserve direct human editing: out-of-band manifest edits remain canonical, inspectable audit gaps, while `rebaseline` records the discontinuity before establishing a new checkpoint.
- Replace fixture-first Records release proof with a complete installed-wheel lifecycle and require structured disposable-vault live MCP evidence before a changed Records surface can be promoted.
- Supersede connector promotion evidence that proves only schema visibility or tool callability.
- Deliver lifecycle capability through an additive `hosted-alpha-agent-v2` candidate/profile with reader floor 2; retain `hosted-alpha-agent-v1` membership, identity, clients, locks, and registered evidence unchanged.
- Keep mutation-boundary availability work in the existing `rebuild-graph-without-blocking-writes` change; this change consumes its acceptance result but does not duplicate its implementation.

## Capabilities

### New Capabilities

- `records-release-acceptance`: define deterministic installed-wheel and disposable live-MCP proof, natural-language agent selection checks, structured evidence, and connector-promotion gating for Records-affecting changes.

### Modified Capabilities

- `records`: make observed-state routing inferred and proactive while preserving the boundaries with Sources, Evidence, Notes, Planning, and Review.
- `structured-collections`: require eager saved-view parity and provide guarded audited manifest revision and explicit rebaseline after out-of-band edits.
- `command-surface`: add revision lifecycle actions, action-specific arguments, read/write classification, and generated-surface parity to the single `record_memory` command.
- `agent-bootstrap-contract`: expose `record` as a salient beginner/front-door action with bounded routing guidance ahead of detailed authoring material.
- `product-e2e`: exercise collection authoring and recovery through the installed wheel instead of pre-writing the collection fixture.

## Impact

- Affects Records parsing, governance, audit publication, command registration, bootstrap projection, scaffold guidance, installed-wheel E2E, live acceptance tooling, connector promotion metadata, generated MCP/REST/CLI schemas, and capability documentation.
- Adds fields and selector values to the public `record_memory` schema; existing seven-action callers remain compatible.
- Adds no hidden database, storage migration, server-side reasoning model, or automatic interpretation of observed values.
- Requires coordinated delivery with `rebuild-graph-without-blocking-writes` before claiming the reported reconcile-blocking incident is fully resolved.
- The existing active `add-hosted-client-plugins` change owns the corresponding additive v2 Hosted profile/candidate, platform-promotion extension, and enforcement artifacts; this change defines the Records proof it consumes without creating a second delta over that not-yet-canonical capability. V1 is not a lifecycle deployment target and is never silently mutated.
