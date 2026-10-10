# Design

## Context

The programme brief asks that ordinary work turns durable referents into entities and typed edges, and that later activation uses them.
The no-nudge architecture assigns the work: deterministic sensors measure, frozen verifiers label into queues, the active conversation agent decides, and carriers deliver state on every response.
The relation audit adds that `relates_to` is honest when nothing more specific is evidenced, that specific predicates are offered cheaply, and that relation count is never a target.

Evidence pointers below are verified against `origin/main` at `42c807797`.

## Owner decisions (decided 2026-10-10)

- **O1. Outcome.** Entity, category, relationship and type creation is first class and fully dynamic, and a promotion reaches all future interactions.
  The owner's complaint: "It only did when I told it to do it."
- **O2. Authority.** Agents create entity types, relation types, categories, entities and typed edges without confirmation when the new type fits a registered parent family.
  Every creation carries provenance (the originating write or episode), surfaces once in the next session's counters, and reverts in one call.
  This replaces the v1 confirm-first rule for these classes.
  Whatever survives of the opt-in v2 grants and T13 owner control (#1124) must not gate the normal owner's promotion.
- **O3. Architecture.** The active conversation agent is the only decider.
  Sensors, verifiers and carriers serve it.
  Code holds no word list or regex that decides meaning (C6), and vocabulary lives in registries (C4).
- **O4. Display.** Humans and agents see the label, for example `has_pet (a kind of owns)`, never `vault.has_pet`.
  The namespaced key stays the machine identity.
  A label is qualified only when two namespaces share it.
- **O5. Acceptance.** A rich conversational turn about a person's pet, health product and diet, replayed with no corrective nudge, yields the pet as an entity with the right type, an ownership edge to the person, and later activation in a fresh session that resolves and uses them.
  Negative twins (incidental names, one-off mentions) stay unpromoted, with a declared false-positive ceiling of zero on twins.
  The run uses the real compiler and capture pipeline, not hand-built packets.

## Goals / Non-Goals

**Goals:** one declaration channel on the ordinary write path; owner promotion without confirmation, with provenance, surfacing and revert; specific relations offered and writable in one call; activation over typed entity families; one display label.

**Non-goals:** server-side referent detection in prose, server choice of a type or predicate, recurrence-based promotion before origin accounting, new core predicates unless the owner answers Q2 with (a), and a bulk curation pass over existing vaults.

## Decisions

### 1. The decider's channel (T1)

**Evidence.**
- Entity creation is first class: `connect_memory` `resolve-entity` and `create-entity` (`link.py:582`), with `identity_decision` for shared names (`link.py:143-159`, close-memory-loop ruling R2).
- Episode `prepare` seals curation steps of kind `create-entity` and `accept-relation`; `resume` runs them only when `EXOMEM_EPISODE_WORKFLOW` is set (`episode_workflow.py:1-45`, `curation.py:44-53`).
  It needs four or more calls and a feature switch, so it cannot be the per-write channel.
- `create-entity` writes every connection as `relates_to` (`link.py:386-391`).
- `accept-relation` is bound to a queue candidate fingerprint (`commands.py:9705`), so no ad hoc typed-edge leaf exists.
- The server does not detect a first mention in prose, by design (`capture_sweep.py:301-303`).

**Decision.**
1. Add one optional `referents` argument, with one shared schema, to `remember`, `observe_memory`, `edit_memory` and `capture_source`.
   The slot on the write call is the structural prompt: the agent sees it every time it writes, and no server text has to nudge it.
2. A declaration list holds at most 8 entries.
   Each entry has these fields:
   - `name` (required, at most 120 characters): the name as the write states it.
   - `evidence` (required, at most 200 characters): a verbatim span of the text that this write commits.
     The server checks only that the span occurs in the committed text after the writer's Unicode normalization.
   - `type` (optional): an entity-type key, alias or label from the live registry.
   - `ref` (optional): the stable ref of an entity that the agent already resolved.
   - `summary` (optional, at most 300 characters) and `aliases` (optional, at most 8): used only for a creation; a creation needs `summary`.
   - `relations` (optional, at most 3): each `{predicate, subject}` or `{predicate, object}`.
     The declared referent is the other endpoint.
     The value names another referent in the same list or a stable ref.
   - `identity_decision` (optional): the existing `create-entity` contract.
3. A malformed argument refuses the whole call before any write, like any other argument error.
   A semantic outcome never refuses the primary write.
4. After the primary write commits, the server runs each declaration through existing leaves:
   - It resolves `ref` when given; otherwise it runs the `resolve-entity` resolution, within the type's entity family when a `type` is given.
   - `reused`: exactly one visible match. Nothing is written for the entity.
   - `created`: no match, a registered `type` and a `summary`. The `create-entity` leaf runs.
   - `needs_decision`: a shared name. The outcome carries the candidates and the `candidate_fingerprint`, as `create-entity` returns them today.
   - `needs_type`: no match, and no `type` or no `summary`. Nothing is written. The outcome carries the `create-entity` call built from the agent's own fields.
   - `type_unknown`: the `type` is not registered. Nothing is written. The outcome carries the `schema_memory` inspect and propose routes.
   - `unresolved`: the `ref` is not a visible entity. A withheld ref gives the same outcome as a missing one.
5. Relations run after both endpoints exist.
   An edge whose subject is created in the same declaration renders in that creation as a typed connection.
   Every other edge runs the `add-relation` leaf (decision 3).
6. The compact envelope gains a bounded `referents` block: one row per declaration with its outcome, ref, label and relation outcomes, and at most 3 candidates per row.
7. The write's operation identity binds every referent leaf.
   An identical retry replays the recorded outcomes and creates nothing twice.
   A crash after the primary commit leaves the declarations `pending` in the receipt, and the identical retry completes them once.
8. Episode proposals use the same two leaf kinds, so the episode path and the write path share one implementation.

**Teaching.** The compact core gains one capture line of at most 160 bytes, registered in `CORE_RULES` (`tests/test_bootstrap_core.py:104`).
On this base the core measures 14,037 bytes at the default level and 14,383 bytes at `maximal` on `claude-code`, against the 15,000-byte ceiling (`tests/test_bootstrap_compact_budget.py:37`).
The 512-byte default band leaves 451 spendable bytes, and the 256-byte `maximal` floor leaves 361.
Measured with `op_bootstrap(profile="compact")` over the source at `42c807797`.
The full contract goes into `bootstrap(section="vocabulary")`, `references/vocabulary.md` and the four tool descriptions.

**The server never** detects a referent in prose, chooses a type or predicate, or creates an entity that no declaration names.
The `write-time-identity-candidates` rule "the server SHALL never create an Entity" keeps its meaning: detection still creates nothing.

**Rejected.** A separate `declare-referents` operation costs an extra call and loses the structural prompt.
Making episode `prepare` the only door keeps the four-call cost and the switch.
Inline type creation inside a declaration would duplicate the registry save; `schema_memory save` stays the one vocabulary door.

### 2. Authority: owner promotion is proactive capture (O2)

**Evidence.**
- On 2026-10-07 the owner ruled that agents promote and humans revert: a v1 owner's registry save takes effect at once, and restore is the revert (`add-vocabulary-registries` design decision 7; `activate-agent-led-vocabulary-evolution` design decision 7).
- Ruling R1 of 2026-09-28 put additive entity creation under `proactive_capture` (close-memory-loop design, "Identity batch 1 rulings").
- The served contract contradicts both: `commands.py:1313-1317` serves "Decision not permission; v1 writers retain confirmation", while `references/vocabulary.md` says "A saved registry change takes effect immediately".
- The `complete-recurring-entity-lifecycle` envelope delta still classes an unknown-kind type save as confirm-required `restructure_execution`.
- v2 is an explicit opt-in vault activation with no default grants (`activate-agent-led-vocabulary-evolution` design decision 4).
  T13 is open PR #1124, which adds native owner approvals and activation.

**Decision.**
1. When the caller resolves to the vault owner and v2 is not activated, these promotions are `proactive_capture`:
   - an entity-type or semantic-category save;
   - a relation-extension save;
   - an entity creation (unchanged from R1);
   - a typed edge that the agent authors through `add-relation`, a referent relation or a `## Relations` bullet.

   They follow the served disposition, which is silent at `balanced` and `maximal`.
2. Provenance, surfacing and revert (decision 5) replace the confirmation question.
3. A relation extension fits a registered parent family by construction: the registry requires a core parent with the same family (close-memory-loop task 5.12).
   An entity type with a `parent` fits.
   A parentless vault type starts a new family; the working rule is that it promotes the same way, marked `new_family` in its notice (open question Q1).
4. `link_acceptance` keeps covering acceptance of a relation that the server's queue suggested.
   An edge that the agent authors from its own evidence is not an acceptance (open question Q3).
5. `restructure_execution` is unchanged: merge, supersession, deletion, meaning changes, alias edits on existing entries and deprecation.
6. A resolved non-owner keeps the 2026-10-07 route: its registry save becomes a pending item for the owner.
   Its entity and edge creation follow its existing write scope; a declaration never widens it.
7. In a v2-activated vault every referent leaf passes the v2 effect classifier, because the declaration calls the same leaf functions.
   No migration, upgrade or default activates v2.
8. When T13 lands, it keeps v2 opt-in and routes no v1 owner promotion to approval.
9. The served authority rule and the scaffold's `proactive_capture` row change to state this decision.

### 3. Specific relations (T2)

**Evidence.**
- `link.py:391` renders every `create-entity` connection as `relates_to`.
- `source_kinds` and `target_kinds` exist on relation definitions (`relation_registry.py:55-71`), but they scope node kinds: callers pass a page type or `file`/`unresolved` (`relation_registry.py:163-168`, `relation_queue.py:119`, `epistemic_graph.py:10762-10763`).
  No entity-family endpoint declaration exists.
- Code branches on the `relates_to` key at `mutation_terminal.py:1402`, `vocabulary_signals.py:71` and `relation_census.py:60` (C4 debt).
- The compact envelope already carries `relation_advisory` (`mutation_terminal.py:1975`).

**Decision.**
1. `create-entity` `connections` accept `{target, relation}` items beside strings.
   A string keeps the generic relation for compatibility.
   An unknown predicate on a direct call refuses the creation with the registry finding, and nothing is written.
2. Add the additive leaf `connect_memory(operation="add-relation", path, requested_relation, target, expected_hash, why)`.
   It uses existing parameters only.
   It appends one bullet to the subject page's `## Relations` through the edit writer, under the page's content-hash guard, and writes a log entry.
   It refuses an unregistered or deprecated predicate with the resolution findings and the propose route.
   It returns `exists` for an edge that is already present.
   A withheld target refuses exactly as a missing one.
   It is also a curation step kind, with an `edit` compensation.
3. Add three relation attributes as registry data:
   - `generic: true` marks the core pack's generic relation.
     Extensions do not inherit it, and the core pack holds exactly one active generic relation.
     It replaces the `relates_to` literals on the paths this change touches.
   - `endpoints: {subject: [...], object: [...]}` lists entity families from the entity-type registry, or the closed token `any_entity`.
     The core pack declares endpoints for its entity predicates; the exact families are pack data.
     An extension inherits its parent's endpoints, and a narrower declaration must stay inside them.
   - `label` (decision 6).
4. Endpoints never refuse a write.
   A mismatch adds a non-blocking `endpoint_mismatch` finding to the receipt.
   A refusal would block a truthful edge whenever the declared families are too narrow, and the agent and the graph would pay for it.
5. When a committed write authors the generic relation between two entity pages, `relation_advisory` carries `specific_options`.
   These are at most 4 active predicates whose declared endpoints contain both endpoint families, each with its label, description and `add-relation` route.
   A predicate with no endpoints is never offered.
   The generic edge stays committed; the offer refuses nothing.
   The existing write-advisory fingerprints dismiss it.
   The work is two page-family lookups and one in-memory registry pass, with no corpus scan.
6. Relation-vocabulary growth stays the proposal loop: an unknown predicate returns the propose and save routes, and an owner's save takes effect at once (decision 2).

### 4. Activation follows typed entity families (T3)

**Evidence.**
- The built-in traversal profiles are a dict in code (`traversal_profiles.py:143-180`), which is C4 debt.
  The vault overlay `_Schema/traversal-profiles.yaml` already extends them as data.
- Activation applies one constant profile, `GRAPH_TRAVERSAL_PROFILE = "epistemic"`, to every resolved anchor at depth 2 (`working_set.py:79`, `working_set.py:352-365`, `working_set.py:1193-1207`).
- `epistemic` excludes the ownership, membership and location families (`traversal_profiles.py:149-157`, `relation_allowed` at `traversal_profiles.py:252-262`).
- Anchor neighbourhoods are untyped wikilinks (`working_set_index.py:1648-1688`).
- An extension already traverses through its parent's family (`traversal_profiles.py:262`, close-memory-loop task 5.12).

**Decision.**
1. Move the built-in profiles into `src/exomem/vocabulary/packs/core/traversal-profiles.yaml` with the same names, families and bounds.
   A parity check runs until the code dict is deleted.
   The vault overlay keeps its extend-only grammar.
2. Add the built-in profile `entity`.
   Its families are the core families that relate entities: ownership, membership, location, operation, supply, production, composition, use and entity.
3. A pack profile may declare `activation_anchor_kinds` from the closed `ANCHOR_KINDS` set (`working_set_index.py:132`).
   Activation selects the profile that declares the anchor's kind.
   `epistemic` declares every other kind, so non-entity anchors behave as before.
   No profile key remains in code.
4. Kinship and affiliation have no core family today.
   A new family joins the `entity` profile by one pack edit (open question Q2).
5. Bounds stay unchanged: resolved anchors only, `GRAPH_MAX_NODES` and `GRAPH_MAX_EDGES`, and no lane for a partial anchor.

### 5. Surfacing, provenance and revert (T4)

**Evidence.**
- The due-state carrier serves categories on bootstrap, mutating responses and recall (`due_state.py:107-123`), with emission keyed by session, audience and vault (`due_state.py:200-205`).
- The first-surfaced ledger stamps delivered items (`review_state.py:1145-1215`, `attention.py:843`).
- Family dispositions, batch-once emission and egress before counting already apply to every category.
- Per-edge provenance today is the authoring write's log entry (`tests/test_operator_site_cohort.py:70-103`).
- Registry history records the operation, reason and principal hash of each save (`add-vocabulary-registries` design decisions 3 and 6).
- `manage_memory_file` delete moves a page to `_trash` and recovers it (`commands.py:5894-5930`).
- `edit_memory` `replace_string` carries an `expected_hash` guard (`edit_operations.py:39-59`).

**Decision.**
1. Provenance reuses existing records:
   - An entity creation or typed edge writes a log entry on its page that names the originating write's path and operation id, the evidence span, and the episode key when an episode leaf ran it.
   - A registry save keeps its history header and its `why`.
2. Add the due-state category `recent_promotions`.
   It holds one entry per promotion: a registry addition, an entity creation, or a typed edge between two entity pages, whichever leaf wrote it.
   An edge from a page that is not an entity is part of that page and is not a promotion.
3. An entry records the session that created it.
   That session never counts it.
   Every other session counts it until its first delivery stamps the first-surfaced ledger; then the entry settles.
   A revert or a dismissal also settles it.
   A withheld promotion contributes nothing to a restricted audience's count.
4. Each entry's item context carries one revert call:
   - A registry addition reverts by `schema_memory restore` to the version before the save.
     When later saves exist, the route lists the keys that the restore would also remove (`also_removes`).
   - An entity creation reverts by `manage_memory_file` delete with `confirm` and the originating page in `expected_dead_inbound`.
     The page moves to `_trash` and stays recoverable.
   - A typed edge reverts by `edit_memory` `replace_string` that removes its bullet, guarded by the page hash.
5. A revert runs on the user's request.
   Deletion keeps its confirm parameter, and the user's request is that confirmation.
6. History and logs keep the promotion and its revert.

### 6. One display label (T5)

**Evidence.**
- `RelationDefinition` has no `label` (`relation_registry.py:55-71`), but the generic registry entry does (`vocabulary/registry.py:67-79`).
- A new extension already stores its clean authoring label as its first alias (`relation_registry.py:340-342`), and `extension_key_for_label` adds the `vault.` namespace (`relation_registry.py:571-573`).
- Keys are emitted verbatim by `relation_registry.py:76`, `relation_vocabulary.py:214-229`, `epistemic_graph.py:328`, `find_types.py:288` and `find_types.py:359`, `relation_census.py`, the relation advisory in `mutation_terminal.py`, and Studio `app.v5.js:307,450,708`.

**Decision.**
1. A relation entry carries `label` as registry data.
   When it is absent, a core relation's label is its key, and an extension's label is the segment after its namespace.
2. One formatter, `relation_registry.display_label(registry, key)`, renders:
   - a core relation as its label;
   - an extension as `<label> (a kind of <parent label>)`;
   - a label that two active entries share with its namespace added;
   - a deprecated entry with `(deprecated)` added.
3. Every producer adds `relation_label` beside its existing key field.
   Prose renderings use the label: the hook packet, advisories, referent outcomes and census text.
   Every existing key field keeps the key.
4. The title-first presentation rule extends to relations: say the label, and pass the key, label or alias in calls.
   A label resolves as an alias, so an agent never has to type a namespaced key.
5. Studio is a separate task with its own PR under the frontend procedure.

### 7. Order against the programme (T6)

This change ships before close-memory-loop task 4.3.
Origin accounting is a dependency for recurrence-based promotion, where the server must know that two mentions come from independent originals.
An agent's first-mention declaration needs no origin count: the decider has the evidence, and the declaration records it.
The slice therefore delivers owner promotion, typed edges, offers, surfacing, revert and typed activation without 4.3.
Recurrence-based candidates keep their current two-page rule until task 4.5 switches them to independent origins.

| Existing task | Relation to this change |
| --- | --- |
| close-memory-loop 3.4 | Related. The referents capture line sits in the guidance that 3.4 owns. 3.4's fixture partition proof stays open. |
| close-memory-loop 4.3, 4.3a-c | Not a dependency. Needed only for recurrence-based promotion. |
| close-memory-loop 4.5 | Unchanged. It still depends on 4.3. |
| close-memory-loop 5.3 | Narrowed. The owner's promotion no longer depends on it; it remains the v2 delegate path. |
| close-memory-loop 6.3 | Contributes. Task 6.2 here adds cross-kind topology tests; 6.3 stays open. |
| close-memory-loop 6.4 | Depends on this change for the private pet-and-owner replay. Task 7.1 here is its synthetic counterpart. |
| close-memory-loop 6.6 | Reuses the replay harness that task 7.1 builds. |
| close-memory-loop 6.17, 6.18 | Independent. Task 6.2 keeps their tests and the same +10% latency bound. |
| agent-led vocabulary 2.1 | Complements. The write-time offer needs no origins; the two-origin generic-pair signal stays with 2.1 after 4.3. |
| agent-led vocabulary 4.1, 4.3, 5.1, 5.5 | Unchanged v2 work; no longer on the owner's path. |
| agent-led vocabulary 7.1-7.4 | Superseded for the owner's vault by decision 2. Kept open only if a v2 delegate needs vault-wide `edge.add`. |
| add-vocabulary-registries 5.1 (S5) | Compatible. The new relation attributes follow S5's rule that behaviour keys on declared attributes. |
| add-vocabulary-registries 9.1 (S9) | Complements. The offer shares the relation-advisory slot; variant nudges stay with S9. |
| complete-recurring-entity-lifecycle 7.5 | Before archive, its envelope scenario "Unknown kind registration remains confirmed restructuring" follows decision 2. |

### 8. Proof plan (T7)

The workflow that this change alters is ordinary capture followed by later activation.
The highest-level check is the end-to-end replay in task 7.1.
Each test below names the failure that only it catches.

- **End-to-end replay (CI).** A synthetic corpus in product shape holds an invented person entity and earlier notes.
  A scripted decider sends the rich turn's calls through the real doors: one type save, then a note with `referents` for the dog, its supplement and its diet.
  The real publication pipeline runs, and a fresh session calls `activate_context`.
  It catches a break anywhere between declaration, creation, graph publication and activation.
  Negative twins carry an incidental name and a one-off mention in the body without a declaration.
  They prove that nothing undeclared becomes an entity, an edge or a notice.
- **Agent-track acceptance.** A real ordinary agent replays the same turn with no nudge, in a hookless and a hooked arm, one to three runs each.
  Only this run proves that the decider uses the channel unprompted.
  Pass: the pet is an entity with the right type, the ownership edge exists, a fresh-session answer uses them, and twin false positives are zero.
  Results are reported per run, with no aggregate.
- **Authority.** A resolved non-owner's declaration with an unknown type changes no registry, and its creation stays inside its write scope.
  A v2-activated vault without a grant refuses the referent leaf while the primary write commits.
  This catches the channel becoming an authority bypass.
- **Disclosure twin.** One restricted caller, one input, two vaults: with a withheld entity of the same name at another path, and without it.
  Referent outcomes, the relation advisory, notices and a later activation must be byte-identical.
  This catches promotion exposing a withheld referent.
  A withheld page at the same path still triggers the entity writer's occupancy refusal (`link.py:232-248`); the test pins that the refusal names no path (open question Q4).
- **Revert.** Each promotion kind is reverted by its one call, and history keeps the record.
  This catches a stale or incomplete revert route.
- **Surfacing once.** A promotion is absent from its creating session, counted once in the next one, and settled after delivery, revert or dismissal.
  This catches a notice that nags or never appears.
- **Typed activation.** An invented person anchor reaches an entity two typed hops away only through the `entity` profile, and a non-entity anchor's packet is unchanged.
  This catches the profile selection regressing either side.

## Risks / Trade-offs

- An agent may declare an incidental name → declaration needs an evidence span, the twins measure false positives, and revert is one call.
- Four tool schemas grow → one shared schema and one pin move; the ChatGPT refresh is an operator step after release.
- Notices could become noise → one entry per promotion, settled after one delivery, and family dispositions quiet them.
- Endpoint data can be too narrow or too wide → offers only, never refusals, and the pack data changes without code.
- A restore can remove later keys → the revert route names them before the user runs it.

## Migration Plan

1. Ship the authority text, registry attributes and display label. An unchanged vault resolves as before.
2. Ship typed edges, the offer and the declaration channel, then surfacing and revert.
3. Ship the profile pack with its parity check, then the `entity` profile.
4. Rollback: older releases ignore the new optional registry fields and the new due-state category, and pages keep their bytes.

## Rulings (owner delegate, 2026-10-10)

- **R1. A parentless entity type** promotes at once. Its notice carries `new_family`, it is surfaced first in the next session, and it reverts in one call. Asking first would put a question in the O5 path.
- **R2. Kinship and affiliation:** this change adds a symmetric kinship relation and an affiliation relation to the core pack, as pack data. Task 2.4.
- **R3. Queue acceptance:** `link_acceptance` keeps its confirm ceiling for queue suggestions in this slice. The agent's own declared edges do not need it.
- **R4. Path occupancy for restricted writers:** suffixed filenames for restricted writers, delivered with the connector-ceiling work of `add-governed-vault-consolidation`.
- **R5. More writers:** measure the four writers first. `record_memory`, `replace_memory` and `episode_memory record` follow on evidence.
