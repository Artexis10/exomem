## Context

See proposal.md for motivation and capability ownership. The shipped relation resolver already exposes core definitions, extension candidates, aliases, direction and registry currency. Registry saves already validate and publish typed graph state. Entity types are extensible, but ordinary-text identity evidence, hydration and parent-family traversal are planned in `complete-recurring-entity-lifecycle`, not proven shipped by the presence of its planning artifacts.

`mutation_terminal.py` currently supplies relation advice for unknown labels and excludes `relates_to`. This is a useful validation advisory, not a complete consideration loop. The v1 delegation envelope also explicitly lacks standing authority for entity creation and type changes. Both gaps need named contracts; a prompt-only change cannot close them.

## Goals / Non-Goals

**Goals:** add one bounded evidence/decision protocol above the existing family-specific resolvers and writers; make the active agent responsible for meaning; make user delegation independently enforceable; expose enough traceable state to distinguish advice from actual adoption.

**Non-goals:** a new ontology engine, arbitrary schema execution, an internal reasoning LLM, automatic semantic deduplication, relationship quotas, source rewriting, bulk backfill, or a universal permission setting. Existing category/source taxonomy openness is not replaced by mandatory registry review. The observability, resource-management and vault-consolidation stacks are not dependencies of this plan.

## Decisions

### 1. Shared protocol, existing tool families

Add a family-neutral vocabulary work-item layer over existing resolvers, not a new top-level catch-all tool. A versioned family descriptor declares evidence acquisition, resolver, proposal validator, canonical writer, supported decisions, authority actions and persistence currency. It is registered by product code. Vault data can supply definitions within a family, never modules or permissions.

Proposed adapter mapping (new variants must be added to the canonical command manifest and generated surfaces together):

| Intent | Surface | Effect |
| --- | --- | --- |
| List considerations | `review_memory(mode="vocabulary")` | Bounded queue, availability and continuation |
| Inspect one item | Existing review-context read by item ref | Definitions, targets, evidence, hashes and permitted next operations |
| Resolve a meaning | Existing `connect_memory` entity/relation resolvers; entity-type resolution under `schema_memory` | Read-only candidates; no semantic auto-selection |
| Record a decision | `triage_memory(action="decide-vocabulary", ...)` with a typed decision payload | Fingerprint-bound review state only |
| Propose or register a type | Existing `schema_memory` family operations | Canonical validation and guarded save |
| Create an entity or edge | Existing `connect_memory` operations | Canonical additive leaf under current authority |
| Request/inspect authority | Existing governance request/status command family, new finite additive variants | Pending request or non-secret status, never agent self-approval |

The decision payload contains item ref/fingerprint, family, registry hashes, target versions, outcome, rationale and selected/proposed canonical identity. It cannot contain an arbitrary command to execute. Application remains a separate canonical operation; receipt reconciliation advances the work item. Reuse the existing relation resolver unchanged where possible. Add entity-type evidence parity rather than forcing type registration through a relation-shaped payload.

Alternative rejected: a single `evolve_memory` tool that resolves, reasons and writes in one call. It obscures which part the agent decided and makes replay and authority harder to inspect. A generic protocol does not require a generic mutation endpoint.

### 2. Evidence triggers and ordinary-session cadence

Use current-write parsed context plus the existing indexed review/recurrence projections. Initially connect unknown labels, structurally eligible generic relation opportunities and entity-lifecycle evidence into one queue. Generic opportunities require an indexed resolved pair or canonical generic edge with at least two independent supporting origins; copy-equivalent origins count once. Preserve the raw supporting contexts and the provenance of the independence determination. If that projection cannot establish independence, report unavailable instead of guessing. Explicit agent-submitted meaning questions are independently eligible. Neither keyword matching nor a classifier may turn a context into a server-authored supplier/equivalence judgment. Entity candidate detection is supplied by its owning change; do not duplicate or weaken its evidence threshold here. The agent also has a pre-write duty to consider identities and meaning from the conversation because an index cannot see unsaved conversation.

The default ordinary path returns at most one compact advisory per committed write and at most four work items per review page; each compact advisory is at most 1 KiB and contains references rather than full definitions. The agent consumes relevant pending work at a durable capture boundary, records a disposition, and avoids surfacing the same unchanged item twice in a conversation. Explicit review is paginated and can continue beyond these response budgets; these are resource bounds, not limits on retained knowledge or eventual work.

The durable review ledger remains the decision owner. A derived projection in the existing deferred-index database serves actionable review through a fixed candidate window; decision history remains available through `state="all"`. A publication fence binds the projection to the canonical ledger. Missing or stale projection state requires explicit reconciliation, with no synchronous whole-ledger fallback on review. Private principal-bound rotation advances successive bounded passes; public continuation contains only an opaque handle for already-visible buffered rows and rechecks their decision currency and disclosure. Coverage is always non-exhaustive: a bounded empty pass cannot prove absence while also hiding withheld queue topology. Conversation notice deduplication uses point lookups in the same derived store and bounded expiry cleanup under the existing surfaced-ledger retention horizon. This bounds request work without limiting retained decisions or session count.

Fingerprint logical evidence, canonical target identities and relevant meaning versions rather than raw mtime. Registry hash still guards a commit; an unrelated registry edit requires refreshing that guard, not treating unchanged semantics as fresh unsolicited advice. Deduplicate work by logical identity. Resolve by observed resulting state, including additive promotion where source material remains intact, to avoid the already-observed permanently loud advisory failure.

Keep workflow consideration dispositions separate from linked integrity findings. In particular, `entity_type_unregistered` is non-quietable and resolves only through changed registry/page state under its canonical spec. Suppressing an optional workflow notification must never suppress that audit/attention row or claim the defect fixed.

Put derived queue/index state under the resolved machine-local state root. Reuse the portable review decision ledger for user/agent dispositions and distinguish their origins. No new state database belongs in authored KB folders. Explicit broad discovery remains default-off, bounded and resumable. Optional guidance failure preserves the note commit and reports unavailable; bounded review projection recovery reconstructs missing considerations from committed state without replaying the content write.

Alternative rejected: warn on every `relates_to`, or require every note to invent a specific edge. That measures conformity, not knowledge quality, and trains agents to ignore the channel.

### 3. Decision lifecycle and graph publication

Use `pending -> proposed -> awaiting_approval -> applying -> applied` for changes; `resolved_without_mutation` and `deferred` are legitimate alternate outcomes. Transitions bind evidence fingerprints, target versions and registry hashes. An agent rationale is an attributable decision, not proof of semantic correctness. Server-enforceable facts are identity, shape, collisions, permission, version currency and committed state.

An applied item records canonical receipts; a proposed item never does. Registration and graph publication remain distinct: saved registry state can be durable while typed recall is warming. Reuse the shipped graph epoch/rebind/recovery contract and preserve authored raw labels/history. Multi-step registration-plus-entity/edge work is not advertised as atomic. Existing receipts cover individual leaves; the governed curation lane owns ordered previews, interrupted runs and compensation. A no-mutation decision closes only the reviewed evidence version.

### 4. Versioned additive authority, not a relaxed restructuring switch

Introduce v2 as an explicit vault activation recorded outside authored content, with a minimum supported authority runtime and no default grants. The v1 envelope remains unchanged. Activation is vault-owned server policy: old clients cannot choose v1 as a downgrade in a v2 vault. Preserve `restructure_execution` confirmation for all non-additive and mixed-plan effects.

The minimum contract must survive loss of the vocabulary database and its local activation marker. Carry `vocabulary_authority_floor=2` in an exact, authenticated version-2 external authorization control record. Version-1 control records retain their existing bytes, MAC and attestation semantics and imply floor 1. The external owner/deployment controller publishes the new floor and its matching serving-membership successor before activation; ordinary agent credentials cannot raise or lower it. Floor 2 with missing, corrupt or mismatched vocabulary state refuses content mutation. Floor 1 permits the ordinary v1 path only when no vocabulary authority artifacts exist. Renewal preserves the floor, and custody republishing refuses a floor decrease or an identity substitution involving floor 2. The trusted activation and session-control paths must remain usable while floor 2 is published but local activation is incomplete, without admitting content writes.

Authority records name contract version, issuer, authorizing user, agent/principal audience, logical vault, action set, resolved scope, expiry, generation and status. First actions are `entity.create`, `entity_type.add`, `relation_type.add`, and `edge.add`. There is no all-future-actions wildcard. Type additions are global registry effects and require vault-wide authority; project edge grants cannot include them implicitly. New entities have no canonical project membership yet, so initial standing `entity.create` grants are also vault-wide; narrower creation uses exact approval of the new destination/payload. A project label is never its own scope proof. Existing-fact replacement, alias changes to existing definitions, deprecation, merges, supersession and deletion remain outside this first additive set.

Requesting permission through MCP is not granting permission. Reuse the existing authorization request and owner-control machinery, but add an explicit user-approval ceremony: the trusted control surface shows the exact payload/effects and accepts the user's authenticated decision through a control capability not possessed by the ordinary agent principal. Ordinary tool credentials can create pending requests and inspect non-secret status only. Personal installations without that distinction offer advice and pending requests, not a fictional machine-verified approval. The exact surface must pass the existing principal/session/custody contract; do not implement an independent bearer grammar or let `why`/`confirmed=true` serve as consent.

Use exact-action approvals for ask-each and bounded expiring grants for delegation. Exact approval binds the full canonical payload and reviewed versions. Standing grants authorize only currently resolved matching effects, not stale previews. Resolve project membership from canonical target state and check both edge endpoints; cross-scope or unresolved objects need separate approval. Recheck under the leaf's mutation boundary. Serialize revoke versus commit with a generation check; committed-before-revoke is history, uncommitted-after-revoke is refused. Reads of existing receipts retain current disclosure checks without reexecuting or requiring a replacement grant.

An exact approval is one-shot, not a payload-shaped standing grant. Bind and durably reserve it to one canonical operation identity before writing effects. Its first committed receipt spends and links that authorization; same-identity receipt replay is not a second execution. Proven pre-commit failure permits the same unchanged retry while approval is valid; uncertainty keeps the reservation until recovery. A new identity always needs a new exact approval, including recreation of a later-deleted object. Reuse the existing receipt/journal protocol for this ordering rather than claim atomicity across a filesystem write and an independent database.

Effect classification must cover low-level/generic file writers and imports as well as friendly tools. Typed tools alone are not an enforcement boundary. Mark activated state with runtime admission constraints; rollback to a runtime without the gate must stop structural writes rather than interpret unknown configuration permissively. Do not enable v2 until the control channel, leaf coverage and admission/rollback fence have independent security evidence.

Compare the complete before/after result, not just the declared operation name. Full entity-registry saves can combine a new type with edits to existing definitions. Classify each addition, alteration and removal; authorize all effects independently and reject the whole atomic payload if any effect lacks authority. Do not prune unauthorized fields and commit the remainder, since that is a different reviewed payload.

Two existing entity-writer paths are required mixed-effect fixtures: `connections` adds authored `relates_to` edges, and decision creation can auto-register a project key. The former requires edge authority as well as entity authority; the latter is outside the first additive family set and requires its separately owned authority or a refused write. Neither is incidental index hygiene. New command variants must also be classified in the existing total credential/owner-control matrix; no adapter may infer authority from an unclassified route.

Alternative rejected: silently turn `restructure_execution` into silent mode, or trust the agent to attest consent. That would grant far more than additive graph work and is incompatible with the current contract.

### 5. Implementation ownership and staged delivery

This change may deliver its consideration protocol and guidance while v1 remains in force. It does not need standing grants to begin fixing the no-nudge gap. Treat scoped authority as a later gated tranche, not a prerequisite for useful advice.

| Owning change | Contract supplied | Integration boundary |
| --- | --- | --- |
| Shipped relation vocabulary | Resolution, registry save and graph currency | Call existing leaves, preserve semantics |
| `complete-recurring-entity-lifecycle` | Ordinary-text identity evidence, hydration, type families | Consume its evidence and traversal APIs; do not duplicate its 37-task plan |
| `add-governed-curation-lane` | Reviewed multi-step execution/recovery | Use for combined plans; single additive leaves do not invent a plan runner |
| This change | Common queue/decisions, ordinary agent cadence, additive authority | Own shared adapters, bootstrap and authority guards |

Before integration, amend the two active predecessor plans' confirm-only language to be explicitly v1, with an explicit dependency on this change for v2. Until that reconciliation and code evidence exist, those plans remain confirm-only and no task here may claim their work implemented. Preserve their scenario requirements and frozen hosted contributions; evolve the current candidate, never v1-v4 release artifacts. Completing this change requires the integration tasks, not only a newly available endpoint.

### 6. Acceptance measures meaning and use

Add pure protocol/state tests before adapters, then end-to-end cases for real entity enrichment, genuinely useful new entity and relation types, overlap resolved by an agent, collision refusal, generic/no-edge, unavailable evidence, stale review, replay, revocation and cross-vault isolation. Test generic-writer bypass and an older-client downgrade explicitly. A synthetic future-family adapter proves that the protocol is reusable but cannot inherit grants or invoke undeclared behaviour.

Run a small, reviewed ordinary-domain acceptance cohort without vocabulary hints. Preserve input, delivered considerations, agent decisions, tool traces and retrieval/traversal results. Include negative cases and compare against the current generic-only/missed-enrichment behaviour. Success is the supported outcome per case; counts of new labels or edges are descriptive, not a pass criterion. Live KB writes require explicit scope approval or a valid live grant; never seed private registries to manufacture a success metric.

### 7. Native owner integration

This delivery completes the standalone installation first. Substrate already
owns Hosted Exomem Home, public accounts, browser sessions and cell routing.
Hosted owner approval must extend that existing surface and reuse its account
session; it must not register the native GitHub ceremony or a second dashboard.
Shared exact previews, authority records and enforcement stay in Exomem. Hosted
decision attestation and floor publication remain explicit integration work.

The standalone installation reuses its pinned GitHub identity verifier for a
separate browser owner ceremony. A verified provider callback establishes
identity; only a subsequent CSRF-protected submission accepts the displayed
intent. MCP clients, client registration, ordinary scopes and agent sessions
cannot request owner purpose. Server-held transactions distinguish the owner
flow, use short-lived HttpOnly cookies, and bind owner identity, vault, action,
exact intent digest, expiry and a one-shot ceremony ID. The agent receiving
authority remains the original audience; browser identity never replaces it.

One canonical intent builder supplies both preview and execution. Prepared
values are deeply immutable, preview never invokes the consent callback, and
execution rereads current facts. Pending exact requests retain the complete
canonical before/after write images captured before staging cleanup. Decoding
must reproduce the classifier's existing digest, including paths, hashes,
roles and effects. Human review displays actual definitions and page changes.
Legacy hash-only requests remain inspectable, but exact approval requires a
fresh complete proposal. Preview resource bounds refuse oversized proposals
without truncation; they impose no limit on retained knowledge.

Resolve authority artifacts centrally from an explicitly configured durable
external directory when present. Admission, activation, custody transitions,
republishing and recovery use the same resolver and inspect legacy colocated
artifacts and SQLite sidecars. Ambiguous placement or missing activated storage
refuses writes; there is no silent move or fallback to v1.

Initial setup has its own finite plan because authorization sessions do not
exist yet. When governance policy is absent, explicitly review a canonical
scope named `Existing access`, with `paths: ["**"]` and
`default_deny: false`, preserving existing disclosure and transport boundaries.
Create no rules or grants. Commit accepted policy through canonical governance
authoring, initialize the ordinary store, then compose the existing
`prepare_forward_migration`, `stage_forward_migration` and
`commit_forward_migration` coordinator. Show and bind the migration plan digest.
The browser never runs migration inside the serving process. It records an
accepted, expiring setup request in durable external control storage and returns
`maintenance_required`. A local maintenance runner verifies that acceptance,
resolves the exact service/interpreter/state binding, stops only that service,
and proves its captured worker and listener are stopped using the existing
service-manager helpers. It rechecks the accepted plan digest, executes the
coordinator with its verified backup and membership publication, and restarts
the same service only after proving a serviceable state. Partial or uncertain
migration remains stopped with its recovery checkpoint. The runner does not
upgrade packages or drain indexes. Automatic launch requires a separate
user-manager unit; a child in the stopped service's cgroup is insufficient.
The initial implementation may expose the accepted request through a local
operator command, with that maintenance step stated explicitly in the UI.
Do not weaken policy validation, pre-provision never-enrolled
custody, or call test-only schema migration shortcuts.

Prospective custody paths are prepared in a separate process. Merely configuring
missing custody files changes admission, so the serving environment stays
unconfigured until initial policy is committed and the accepted migration has
succeeded. The maintenance review binds the actual service unit, interpreter,
runtime version and exact environment source. After migration the runner
persists only the four approved custody settings before restarting.

An accepted review acquires a durable `started_at` when execution begins before
expiry. Expired recovery additionally requires exact on-disk partial or completed
publication evidence; an applying row or inert staged plan alone is insufficient.
Recovery completes only that original transition, using actual current time for
serving admission. A completed ledger result and its approved environment
publication are independently resumable. If the deployment step finishes after
consent expires, a new authority activation requires a fresh owner review.

Activation first prepares and displays the exact floor-2 deployment successor
without publishing it. Only after owner acceptance does deployment serialization
publish the authenticated control and matching serving membership. The accepted
setup binds the owner, vault and deployment transition across subsequent
session opening; a final session-bound activation continuation revalidates the
actual published state. Activate with no grants, then review bounded grants or
exact actions separately. Store no agent bearer in prepared continuations.
Hosted adapters may supply the same trusted decision contract from their own
owner controller. Live setup remains an actual owner ceremony, never an agent
assertion of consent.

Activation explicitly includes ongoing mechanical renewal of the same
installation's signed serving proof. Completion records that permission
atomically with the original owner review. A local lifecycle worker verifies
the retained owner and attachment, renews before membership expiry and repairs
overnight expiry before ordinary workers start. Renewal preserves keys,
identity, authority floor and permissions; it performs no semantic reasoning.
Every publication retains its exact recovery checkpoint. Expired partial
renewal finishes the original signed target before creating a fresh successor,
and only actual-current custody can be admitted for serving.

## Risks / Trade-offs

- Advisory saturation → fingerprinted decisions, resolution from actual state, one compact item per write and explicit pagination.
- Missed unsaved meaning → portable pre-write agent responsibility complements server evidence; no claim that a deterministic detector understands every conversation.
- Registry-wide effects under narrow grants → separate global type-addition actions and scope rejection tests.
- Approval channel is not genuinely user-controlled → delegation stays unavailable until trusted-control evidence passes; no caller-controlled consent flag.
- Generic writers bypass typed gates → classify complete resulting effects at shared leaves and exercise alternate-route tests.
- Dependency plans drift → integration/wording reconciliation is an explicit task; no archive while required integrations remain unfinished.
- State recovery or optional guidance adds write latency → point lookups only on the write path, typed unavailable results, bounded rebuild off the core commit path.

## Migration Plan

1. Ship the family protocol, read/decision surfaces and bounded guidance with v1 permissions intact. New review projections are rebuildable and old clients can ignore the additional terminal field.
2. Integrate entity-lifecycle evidence and curation receipts when their acceptance is demonstrated; synchronize overlapping OpenSpec wording before enabling those paths.
3. Ship but do not activate v2 authority. Validate trusted approval, generic/typed leaf gates, revoke/commit ordering and deployment downgrade refusal across supported surfaces.
4. Let a user deliberately activate one vault and create narrowly scoped expiring grants through trusted control. Existing registries and notes remain unchanged. Test revocation and rejected out-of-scope work before widening the pilot.
5. Roll back workflow presentation independently of authority. Once a vault has activated v2, any runtime serving structural writes must preserve its gates; an incompatible rollback is a refused deployment, not silent v1 fallback. Preserve receipts and dispositions throughout.
