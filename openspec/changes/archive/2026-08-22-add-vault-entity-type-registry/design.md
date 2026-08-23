## Context

The core entity registry is five frozen definitions in `entity_types.py`. Every downstream consumer currently assumes that closed set, even though project keys, relation vocabulary, and semantic language are already extended by vault-owned schema files. This change must preserve the core compatibility symbols while making runtime behavior derive from a validated core-plus-extension registry. Reads must soft-fail invalid extension entries into findings, writes must remain governed and stale-safe, and referent resolution must retain its existing deterministic and bounded behavior.

## Goals / Non-Goals

**Goals:**

- Load valid vault-defined entity types beside the unchanged core registry.
- Give every named consumer the same active registry and invalidate caches by registry identity.
- Expose one validate-first, optimistic governed save leaf and refuse deletion of observed types.
- Detect unregistered entity state deterministically and surface it through attention without automatic mutation or dismiss-to-silence state.
- Preserve generated MCP/REST/CLI parity and the existing referent benchmark floors.

**Non-Goals:**

- Automatic type registration or page migration.
- Renaming, removing, or extending the definitions of core types.
- Hierarchies deeper than an optional roll-up to one core parent.
- Model-backed inference, confidence scoring, or any server-side semantic judgment.

## Decisions

### Registry composition and compatibility

`EntityTypeRegistry` is an immutable snapshot containing the core version, extension content hash, core and extension definitions, validation findings, and resolution indexes. `load_entity_types(vault_root, proposal=...)` parses the extension mapping independently so one invalid entry cannot suppress valid siblings. Active lookup indexes include valid active core and extension definitions; deprecated definitions remain represented but are excluded from active IDs and ordinary resolution. Existing module constants remain core-only compatibility symbols.

Alternatives considered and rejected: replacing the existing constants would break callers that rely on their closed core meaning; merging invalid entries and raising on first failure would make read paths brittle; silently dropping invalid entries would hide governance debt.

### Validation and governed saving

The extension file mirrors the relation-registry pattern: deterministic YAML, content-hash cache keys, findings shaped by the shared registry convention, validate-first proposal handling, guarded atomic writes, and explicit stale/existing-file errors. IDs and aliases are compared through the existing entity normalization. Folders are one safe path segment and casefold-unique. A parent, when supplied, names a core type only. `save_registry` compares proposal IDs with `observed_ids` and refuses an observed deletion with `OBSERVED_ENTITY_TYPE_DELETION`; deprecation is the supported removal path.

Alternatives considered and rejected: direct file edits would bypass shared validation and optimistic concurrency; permitting arbitrary parents would introduce an unrequested hierarchy; deleting unused-looking types based only on schema state would ignore authored pages.

### Runtime consumers and cache identity

Every named consumer loads the registry for its vault. Folder/index structures and referent cue maps are cached by `(core_version, extension_hash)`, in addition to the vault/corpus freshness keys already needed by those subsystems. Initialization creates extension folders only when an extension file is present; entity creation lazily creates the selected extension folder. Tool schemas accept strings and runtime validation reports `ENTITY_TYPE_UNKNOWN` with active IDs.

Vault-aware `default_entity_types` validation is a forward hook for user-authored knowledge packs. Pack admission currently loads built-in product packs only, so there is no production custom-pack caller to receive `vault_root` yet; the validation seam is ready when that admission path exists.

Alternatives considered and rejected: enumerating values in the static MCP schema would require schema regeneration on every user vault change and cannot represent per-vault state; unconditional init-time folder creation would materialize unused extension schema; uncached reparsing would violate the latency contract.

### The predicate

An `entity_type_unregistered` finding exists when either predicate is true for current vault state:

1. An `Entities/**` Markdown page declares an `entity_type` that does not resolve to an active registry ID.
2. At least three pages are present beneath an immediate `Entities/<Folder>` directory whose folder does not resolve to an active registry type.

The finding is deterministic, carries the affected path/count and a ready `proposed_entry`, and is composed into attention as audit state. Attention deliberately adds a `decision` key to every reason: ordinary reasons carry the recorded action while state-resolved-only reasons carry `null`. It has no portable dismissal state: registering the type or moving/correcting the pages makes the predicate false.

Alternatives considered and rejected: a two-page threshold is too eager for incidental staging; dismissing the finding would conceal unchanged authored state; automatic registration would turn a measurement into an unreviewed mutation.

### Validation against the real case

A synthetic extension named `place` uses folder `Places`, label `Place`, aliases such as `location`, and cue nouns such as `venue`, with optional parent `concept`. Loading it adds `place` beside all five core IDs; entity creation accepts `place` and lazily creates `Entities/Places`; indexes and referent resolution enumerate that folder; bootstrap and adoption guidance list `place`; a knowledge pack may name it. Before registration, three authored pages in `Entities/Places` produce one attention finding whose proposal matches the same entry. After a guarded save, the finding disappears without triage state and the same pages become ordinary registered entities.

## Risks / Trade-offs

- [Registry parsing adds latency to hot paths] → Hash and cache the immutable snapshot; enforce cold and warm latency tests at 50 extension types.
- [Different consumers could resolve different type sets] → Route every named consumer through the same loader and use registry identity in derived caches.
- [Alias or folder ambiguity could misroute entities] → Validate collisions across IDs, aliases, labels, and folders before indexing; preserve invalid entries as findings only.
- [Generated surface drift could be hand-edited incorrectly] → Regenerate pins and capabilities only through repository generators and keep fidelity tests in the focused shard.
- [Attention could become a hidden mutation/nudge] → Reuse deterministic audit composition, write nothing, and resolve only from current vault state.

## Migration Plan

Ship the loader and migrated consumers with no extension file required. Existing vaults therefore retain the core-only behavior and folders. Vaults opt in by using the governed `save-entity-types` leaf; initialization or first entity creation then materializes extension folders. Rollback consists of running the prior binary: extension files and pages remain ordinary user-owned Markdown/YAML and core behavior continues, but extension types are no longer active until the new version is restored.

## Open Questions

None. The extension shape, validation rules, parent semantics, attention predicate, and generated surface change are settled by the task brief.
