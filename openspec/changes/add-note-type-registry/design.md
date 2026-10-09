# Design

## Context

A note type is the `type:` value of a governed page. The product writes or reads ten: six compiled types, `entity`, `source`, `evidence` and `collection`. Code fixes them in literal sets: twelve copies of the six compiled types, five sets that add `entity`, and six maps from type to folder. The sets serve different purposes, so their members differ.

The vocabulary substrate supplies the loader, packs, overlays, effective digests, governed saves, history and restore. The status registry (S3) adds an operation basis that admits an overlay before it classifies a page. S3 also moves the authoring contract to version 6. This change builds on both.

## Goals / Non-Goals

**Goals:** every note-type set becomes a predicate over closed attributes of one registry. An unchanged vault resolves every type as before. A vault can register a compiled type with its own folder in S4a, and promote one under a parent in S4b.

**Non-goals:** entity types, source kinds and statuses keep their own registries. S4a changes no write path, tool parameter or page. Collections, Records and Planning keep their own type protocols. S6 owns slug suffixes, hub and snapshot tags, and the research-folder demotion.

## Decisions

### 1. Roles and attributes

An entry may declare a note-type role: `compiled`, `entity`, `source` or `evidence`. The registry schema fixes this closed set, and code branches on a role value, never on a type key. An entry without a role, such as the shipped `collection`, matches no role predicate. `evidence` is a role of its own, because ranking penalizes sources and keeps evidence neutral.

Two closed attributes join S4a, because the read-side audit sets need them. `time_bounded: bool` marks a compiled type whose pages record one bounded period. `sources: required | optional` marks a type whose pages must cite their sources. An entry that omits them takes `false` and `optional`.

`folder` joins S4a as an immutable attribute of a compiled type. It names one `Notes/<Name>` folder, because the destination resolver matches that single segment. It is unique among compiled types, because the write gate maps each folder back to one type. The semantic contract reads its destinations from the registry in S4a, and the S4b writer uses the same attribute.

Docs, refusals and guidance say "note-type role", because artifact, context and semantic roles already use the word "role".

The predicates reproduce today's sets exactly for the shipped pack. The pack sets `time_bounded` on `experiment` and `production-log`. It sets `sources: required` on `research-note`, `insight`, `failure` and `pattern`. It gives the six compiled types the folders `Notes/Research`, `Notes/Insights`, `Notes/Failures`, `Notes/Patterns`, `Notes/Experiments` and `Notes/Productions`.

| Set | Consumers | Predicate | Shipped members |
|---|---|---|---|
| A | rank boost, claim scope, contradiction candidates | role `compiled` or `entity` | six compiled types, `entity` |
| B | semantic contract, compiled-page activation, observe, bridges, source closure, curation, Dreamer hydration, structure promotion | role `compiled` | six compiled types |
| C | governed graph endpoint, relation debt, stable identity | role `compiled` or `entity` | six compiled types, `entity` |
| C+ | connectable relation target | role `compiled`, `entity` or `source` | set C, `source` |
| D | stale review | role `compiled` and not `time_bounded`, or role `entity` | `research-note`, `insight`, `failure`, `pattern`, `entity` |
| E | missing sources, support collapse | `sources: required` | `research-note`, `insight`, `failure`, `pattern` |
| F | sink exemption in episode reports | none in S4a; stays a key comparison | `entity`, `production-log` |
| G | source rank penalty; sweep applicability with A | role `source` | `source` |
| H | raw and append-only pages | role `source` or `evidence` | `source`, `evidence` |

Five type-key comparisons do not reduce to these values. S4a leaves them in place and reports them as C4 debt, and S4b removes each one:

- the sink exemption of set F;
- the experiment-only lifecycle in audit, artifact-role state and due state;
- the experiment-owned `outcome` field;
- the knowledge-pack primitives list;
- the singular `project` finding for `pattern`.

Single-key comparisons on `entity`, `source`, `evidence` and `collection` outside sets A to H are also old C4 debt. S4b moves each one to a role or attribute predicate.

### 2. Registry shape

`note_types.py` follows the status adapter: one `RegistrySpec`, the adapter, an operation basis with admission, and the role predicates. `vocabulary.registry_specs()` registers it as subject `note-types`. The pack is `vocabulary/packs/core/note-types.yaml`. The overlay is `_Schema/note-types.yaml`, in the generic entry grammar from the start, because no legacy file exists.

`role`, `folder` and `time_bounded` are immutable in place, because pages and derived rows already rely on them. In S4b, a promotion names a parent and inherits its role and flags.

Admission follows S3 exactly. A caller who may not read the overlay classifies against the shipped pack only. So an overlay never shadows, removes, redirects or changes a shipped type, and an extension key never collides with a shipped key. An operation that depends on an unavailable definition reports unavailable rather than guessing. Ranking is a soft signal, so a type whose role is unavailable ranks neutral.

Read-side selection (eligibility, claim scope, contradiction candidates, audit sets) classifies against the shipped pack only for such a caller, so a withheld type matches no predicate there, exactly as an unknown type did before the registry. It never becomes unregistered debt for that caller. The semantic write gate, observe and Tier-2 identity checks need the definition to choose a finding, so they report `NOTE_TYPE_DEFINITION_UNAVAILABLE` and refuse.

Shared state that no caller reads except through a per-caller serve reads the owner's view: the activation census, the stored graph and artifact-role eligibility bits, the claim store, bridge validation and the catalogue identity.

The pack holds every type that the product writes or reads, so a product page never becomes debt or depends on an overlay. After admitted resolution, a type that the registry does not define matches no predicate, as today. The audit reports its pages as unregistered debt.

### 3. Type normalisation

Sites normalise the type value differently today. Activation casefolds and strips it; ranking, bridges, curation, source closure and stable identity compare the raw string. S4a keeps each site's normalisation before the lookup, and the lookup adds none of its own. S4b unifies normalisation as a deliberate behaviour change with its own proof.

### 4. Ranking

`ranking_config` owns the map from note-type role to ranking knob. `compiled` and `entity` take `compiled_boost`, `source` takes `source_penalty`, and `evidence` takes 1.0. S4a deletes `find_policy.COMPILED_BOOST`, `SOURCE_PENALTY` and `SUPERSEDED_PENALTY` and the alias layer in `find`. No entry carries its own multiplier, because the bounded multiplier pass needs every factor between the declared knob values.

### 5. Contract text and skills

The semantic authoring contract states the role rule: `compiled_intent = canonical destination OR the type's note-type role is compiled`. It lists the shipped compiled types and their folders, which it renders from the pack at import, so the list has one source. The skills rendered from it also say that a vault may register more types and that `schema_memory(subject="note-types", operation="inspect")` serves the live set. The contract stays vault-independent, because its digest addresses fixed content.

The note-type registry sets `served_by="authoring"`, so the bootstrap `vocabulary` summary leaves it out. The authoring contract already serves the shipped compiled types and folders, and search guidance the ranking types; a summary row would repeat them in the budgeted reference payload.

The skills lose the sentence "`COMPILED_TYPES` contains exactly". S4a owns that part of programme task 2.2. The contract version moves once more after S3, from 6 to 7. A one-off script or edit updates the 10 scaffold files that quote the concise contract. Then `package-skills --sync-plugin`, `cloud-plugin.py build` and `hosted-plugin.py regenerate` refresh the copies; the regeneration touches only v5.

`lexstore.catalog_semantic_identity` adds the registry's effective digest, so a save or restore invalidates the parsed catalogue rows. Bootstrap `search_guidance.compiled_types` lists five types while `find` boosts seven. S4a renders that list from the rank predicate over the shipped pack, beside the pointer to the live set.

### 6. Dead and duplicate code

S4a deletes `semantic_writes._COMPILED_TYPES`, because both of its branches return the same value. Each other literal copy becomes a predicate call. The typed writer's `note.NOTE_TYPES` takes the shipped pack's compiled types until S4b, because that writer cannot yet place a vault-defined type. SQL consumers expand a role into its type keys for each query, because derived stores hold the type key and have no role column.

### 7. Sequencing and proof

S4a starts implementation after S3 merges, because both change the contract version and S4a reuses the S3 basis. The existing suites pass unchanged. Tests that read a deleted private constant move to the registry API in the same commit.

One workflow test drives the registry through an overlay entry with role `compiled` and its own folder. `find` boosts its page, the write gate requires a semantic unit on it, and activation treats it as eligible. A restore then removes the entry, and its pages keep their bytes as registry debt.

S4b is a separate task group in this change, delivered later. It covers the write side, promotion and the generic `fields` parameter. It also covers index entries for new folders, partition and stem kinds, the deferred debt and unified normalisation. Its proof is programme task 4.3.

## Frozen surfaces

- Released hosted profiles v1 to v4 serve the frozen version 5 authoring contract. Its bytes and digest stay unchanged, and the live rebuild keeps its nine top-level field names.
- Their `remember` and `replace_memory` descriptors, schemas and ordered parameters stay pinned. The S4b `fields` parameter reaches the current surfaces and hosted v5 only.
- The hand-authored v1 to v4 files keep the bytes that the immutability manifest pins. The marketplace fixture check reads the shipped pack, never a vault overlay.
- A vault-defined type never appears in a released profile's bootstrap or contract text.
- Generated v5 and directory artifacts change only through `hosted-plugin.py regenerate`, and `hosted-plugin.py check` passes.

## Risks / Trade-offs

- A vault type cannot enter the content-addressed contract. → The contract states the role rule and the shipped list. Its digest changes once, and the catalogue identity, digest pin and skill stamps move with it.
- A shared lookup can change the result for a mixed-case type such as `Insight`. → Each site keeps its normalisation (decision 3), and S4b changes that with its own proof.
- The notes index counts only its fixed folders, so it omits the pages of a vault-defined type. → S4a accepts this gap, because it writes no page itself. S4b creates the index entry for a new folder.
- The write gate maps each folder to one type. → A compiled folder is one `Notes/<Name>` segment, unique among compiled types and immutable.
- Graph and catalogue rows hold only the type key. → SQL consumers expand roles per query, and the catalogue identity includes the registry digest.
- A per-entry multiplier makes the bounded ranking pass inexact. → Roles map only to the existing knobs.
- A live type list would add bytes per type to the `authoring` and `routing` bootstrap sections, which have fixed ceilings. → Both list the shipped pack only, and `schema_memory` inspect serves the live set. The contract drops its duplicate `compiled_types` list, which pays for the two types search guidance gains, so the budget tests run unchanged.
- A restricted writer cannot write a page that needs an unadmitted type or folder. → S3 accepts the same cost, and independently admitted operations stay available.

## S4a / S4b split

**S4a, read side.** S4a adds the registry, pack, admission and predicates. It moves every read-side set and the semantic gate's destinations onto them. It also delivers the ranking map, contract text, skills, catalogue identity and unregistered debt. It changes no write path, tool parameter or page.

**S4b, write side and promotion.** S4b adds promotion through `schema_memory` and the `fields` parameter. It moves the typed writer's folders, partitions, stems, required fields, statuses, sections, claim sections and typed fields onto the registry. It adds index entries for new folders, removes the deferred debt and unifies normalisation. S4b records its decisions for partition kinds, stem kinds and the `fields` shape here before implementation starts.

## Migration Plan

1. Merge S3, rebase S4a, and refresh the modified requirements against the synchronized canonical text.
2. Ship S4a. An unchanged vault resolves as before, and the upgrade rewrites no file.
3. To roll back, restore the previous release. It ignores `_Schema/note-types.yaml` and returns to the fixed sets; the overlay and its history stay for later recovery.
