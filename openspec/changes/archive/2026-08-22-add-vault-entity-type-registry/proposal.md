## Why

Exomem already lets each vault own its project, relation, and semantic-language registries, but entity types remain a closed five-value code registry. Vaults need to define recurring entity kinds such as places or products without a code release, while preserving deterministic validation, governed writes, and visible review of unregistered types.

## What Changes

- Add a vault-owned `_Schema/entity-types.yaml` extension registry layered beside the unchanged core entity types.
- Add validation, content-hash caching, optimistic guarded saves, observed-type deletion protection, deprecation, and one-core-parent roll-up semantics.
- Make entity creation, resolution, indexing, referent cues, knowledge-pack validation, adoption guidance, bootstrap output, and folder initialization consume the active vault registry.
- **BREAKING**: change the MCP schema for `entity_type` from a closed enum to a free stable-ID string validated at runtime, so vault-defined types are representable.
- Surface unregistered entity types as deterministic attention findings with ready registration proposals that resolve only when vault state changes.
- Document and package the governed extension workflow and regenerate capability/schema contracts through repository generators.

## Capabilities

### New Capabilities

- `entity-type-registry`: Vault-defined entity type loading, validation, guarded saving, extension folders, and unregistered-type findings.

### Modified Capabilities

- `command-surface`: Entity commands accept active vault-defined type IDs and expose the governed `save-entity-types` operation.
- `agent-bootstrap-contract`: Entity capture guidance and active type metadata reflect the vault-aware registry.
- `attention-queue`: Unregistered entity types surface as state-resolved attention findings without dismiss-to-silence behavior.
- `referent-resolution`: Cue nouns and candidate scoping include active vault-defined entity types.

## Impact

This affects entity registry/loading code, vault initialization, indexes, entity create/resolve flows, candidate and referent resolution, command schemas, bootstrap output, knowledge packs, adoption guidance, audit/attention review, generated capability contracts, and the packaged skill scaffold. No dependency is added.

Deliberately out of scope: automatic registration, type hierarchies beyond one core parent, renaming core types, migrating existing pages.
