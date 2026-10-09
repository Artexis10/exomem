## Why

Exomem fixes its customer note types in code. About twelve modules copy the six compiled types as literal sets, and six maps copy their `Notes/<Folder>` destinations. A vault cannot add a note type, and each new consumer copies one more set.

The word "compiled" also hides four different sets. Ranking, claims and contradiction review use seven types, including `entity`. The semantic contract uses six. Graph endpoints use the six and `entity` for another purpose, and stale review uses five. One `compiled` flag cannot replace them without a change in behaviour.

The vocabulary programme makes every category set a registry with a shipped pack and a vault overlay. This change delivers its note-type slices, S4a and S4b.

## What Changes

S4a, the read side:

- Add the note-type registry on the shared vocabulary substrate: a shipped pack, the `_Schema/note-types.yaml` overlay and `schema_memory(subject="note-types")`.
- Give each type a closed note-type role (`compiled`, `entity`, `source` or `evidence`) and three closed attributes: `folder`, `time_bounded` and `sources`.
- Replace each literal type set with a predicate over these values. The shipped pack reproduces every current set exactly.
- Let `ranking_config` map roles to its existing ranking knobs, and delete the duplicate ranking constants and aliases.
- Teach the role rule in the semantic authoring contract, and list the shipped compiled types from the pack. Remove the "`COMPILED_TYPES` contains exactly" sentence from the skills.
- Bind parsed catalogue rows to the registry's effective digest, so a save or restore reaches the next operation.
- Report a page whose type the registry does not define as unregistered debt, and never rewrite its bytes.

S4b, the write side and promotion:

- Promote a type under a parent that supplies its role and flags.
- Move the typed writer's folders, partitions, stems, required fields, statuses, sections and typed fields onto the registry. Create the index entry for a new folder.
- Add one generic `fields` parameter to `remember` and `replace_memory` on the current tool surfaces, including hosted v5, and never on released profiles.
- Remove the type-key debt that S4a leaves, and unify type normalisation as a deliberate, proven change.

## Capabilities

### New Capabilities

- `note-type-registry`: governed note-type definitions, closed roles and attributes, consumer predicates, admission, ranking knobs, debt, published guidance, promotion and typed creation.

### Modified Capabilities

- `semantic-write-contract`: compiled intent reads the note-type role and the registered folders, not a fixed type list. Observe-memory refusals name the role, not a private constant.
- `attention-queue`: support-collapse review covers the types whose registry entry requires sources.

## Impact

- Dependency: `add-lifecycle-status-registry` (S3) merges first. Both changes edit the semantic authoring contract and its version, and S4a reuses the S3 operation basis. S4a rebases on S3 and refreshes its modified requirement against the synchronized S3 text.
- Code, S4a: a new `note_types` adapter and pack, and predicate calls in about 24 modules. Among them are `ranking_config`, `find_policy`, `find`, `semantic_contract`, `semantic_authoring`, `lexstore`, `activation` and `audit`.
- Code, S4b: `note`, `commands`, `indexes`, `init`, `create_file`, `adoption_proposals`, `compile_proposal`, `claims`, `audit_fix`, `find_types`, `vault`, `knowledge_packs` and the capture clients.
- Published surface: S4a adds no tool parameter. It regenerates the skill scaffold, the plugin copies, the cloud plugin and the live hosted candidate. S4b adds one parameter to the current surfaces, including hosted v5. Released hosted profiles v1 to v4 keep their descriptors, schemas and authoring contract.
- Vaults: the upgrade rewrites no page and no overlay. An unchanged vault resolves every type as before.
