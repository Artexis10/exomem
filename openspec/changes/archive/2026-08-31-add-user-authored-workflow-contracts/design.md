## Context

Exomem already has three adjacent mechanisms:

- a code-owned runtime authoring contract projected through `bootstrap()` and the shipped skill;
- corpus schema contracts under `_Schema/contracts/*.yaml`, used to validate recurring note structure;
- user-authored governance policy, which compiles natural-language intent into explicit, inspectable, reversible rules.

None is a user-authored behavioural contract. The current Planning/Records bootstrap instead contains a fixed software rule naming OpenSpec. That happens to fit the maintainer's workflow but cannot represent standalone Planning, another specification system, a non-software workflow, or different choices across projects.

The design must preserve Exomem's pure-substrate boundary. The server may parse, validate, resolve, fingerprint, and render contracts deterministically. It must not interpret conversational intent with a server-side model, execute user-supplied prompt text, or call companion tools. The agent interprets the conversation and supplies bounded context; governed leaves remain the only Exomem mutation path.

## Goals / Non-Goals

**Goals:**

- Make Planning fully useful with no companion tool and composable with any user-declared companion.
- Introduce a reusable contract-family architecture rather than a one-off OpenSpec preference.
- Keep canonical contract data strict and machine-readable while making every contract pleasant to inspect in Obsidian or another Markdown editor.
- Resolve behaviour deterministically from explicit user selection or project/domain/activity context, with provenance and ambiguity refusal.
- Close the loop from durable intent through external execution to observed Records and explicit Planning review or transition.
- Project one coherent contract across MCP, REST, CLI, bootstrap, installed skills, hosted cells, and portable vault export.

**Non-Goals:**

- No tool-specific adapter, webhook, polling, bidirectional synchronization, or external API credential storage.
- No server-side language model, prompt execution, free-form instruction field, or inferred companion ownership.
- No automatic Planning completion from Records, elapsed time, git state, or an external system.
- No migration or unification of existing corpus schema contracts or governance policies.
- No dashboard or contract editor UI in this change; the canonical Markdown and product command form the first interface.

## Decisions

### 1. A code-owned family registry with user-authored instances

Introduce an internal contract-family registry. A family implementation owns its schema version, parser, validator, resolver, deterministic renderer, and bounded projections. The first registered family is `workflow`. Registering a new family requires product code and an OpenSpec contract; a vault file cannot load code or invent executable semantics.

This provides the extension point suggested by the dogfood insight without prematurely forcing governance policy and corpus schema validation into one data model. Those mechanisms remain independent and may adopt the substrate later only through explicit migrations.

Alternative rejected: add `openspec_mode` to a global config file. It fixes one installation, cannot compose across domains, and bakes a companion product into Exomem.

### 2. Human-readable Markdown items with authoritative structured frontmatter

Workflow contracts live at:

`<configured kb_dirname>/_Schema/contracts/workflow/<human-readable title>.md`

`kb_dirname()` is the only root authority; the implementation must not spell `Knowledge Base`. Each item has a stable UUID `contract_id`, a unique human-selectable `key`, `type: workflow-contract`, `schema_version`, title, lifecycle, scope, Planning posture, companion declarations, capture posture, and transition posture in YAML frontmatter. The filename and title are presentation, never identity. New saves derive a natural-case filename with the existing portable title sanitizer and refuse a collision with another identity. A safe manual rename remains valid.

The body contains one delimited deterministic English rendering generated from the frontmatter. Authored Markdown outside the managed block is preserved byte-for-byte by update and refresh. Frontmatter remains authoritative; inspection reports presentation drift, and guarded save or refresh can rebuild only the managed block without reading values back from prose.

This follows the same human-owned structured-file pattern as readable Planning and Records items: perfect typed data for tools, useful prose for people, and no hidden database as canon.

Alternative rejected: one canonical YAML registry plus a separate generated document. Two files make direct editing and portable review clumsier and create an avoidable pairing problem.

### 3. A normative v1 schema with an open integration vocabulary

The canonical frontmatter has exactly this shape and field order:

```yaml
type: workflow-contract
contract_id: 6f1c2ec5-7f14-4ce8-a54e-f94c8c95c378
schema_version: 1
key: software-delivery
title: Software Delivery
lifecycle: active
scope:
  projects: [example-project]
  domains: [software]
  activities: [implementation]
planning:
  mode: companion
companions:
  - key: specification-tool
    name: Specification Tool
    owns: [software.requirements, software.acceptance-tasks]
capture:
  durable_intent: proactive
  observed_outcomes: proactive
planning_transition: propose-after-outcome
```

Unknown or missing fields are invalid. All three scope lists are present, individually unique, sorted in canonical output, and contain zero to 16 tokens. Dimensions are ANDed; values within a dimension are ORed. `planning.mode` is `standalone` or `companion`. `companions` is empty in standalone mode and contains one to eight entries sorted by key in companion mode. Companion keys are unique; each owns one to 16 unique, sorted ownership tokens, and one ownership token may appear in only one companion in the contract. Co-ownership is not represented in v1. Capture values are `explicit` or `proactive`, with proactive capped by the active prominence level. `planning_transition` is `explicit-only` or `propose-after-outcome`.

Project selectors reuse the canonical Exomem project-key grammar `^[a-z][a-z0-9-]{0,40}$`. Contract keys, companion keys, domain selectors, and activity selectors are 1–64 ASCII bytes matching `^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$`. Ownership tokens are 3–128 ASCII bytes matching `^[a-z][a-z0-9_-]{0,31}(?:\.[a-z][a-z0-9_-]{0,31})+$`. Titles and companion names are NFKC-normalized, trimmed, contain no control characters, and are 1–128 UTF-8 bytes. IDs use the lowercase canonical RFC 4122 UUID form. A file is at most 64 KiB. An exact scan is capped at 512 workflow files and 8 MiB; exhaustion refuses `WORKFLOW_CONTRACT_SCAN_LIMIT` without returning a total or partial candidate set. A completed scan returns at most 128 released summaries plus its exact released total and projection-truncation flag. Explanations are at most 4 KiB; findings and ambiguity candidates are capped at 32 and 16 respectively.

Canonical writing uses UTF-8, LF endings, `---` delimiters, the field order above, two-space YAML indentation, PyYAML safe dumping with `sort_keys=False`, `allow_unicode=True`, `default_flow_style=False`, and `width=4096`, followed by the managed presentation block. The fingerprint is SHA-256 over compact sorted-key JSON of the normalized semantic mapping; it excludes filename, path, authored body, generated presentation, and source byte hash. The source hash is separately SHA-256 over the exact file bytes.

The open vocabulary is how any integration can participate without Exomem shipping an adapter or pretending to understand the external artifact. Companion references on Planning/Records items remain opaque. Planning `execution.kind` adopts the same open key syntax; the existing values (`openspec`, `repository`, `issue`, `pull-request`, `release`, `deployment`, and `other`) remain valid data but none is privileged.

The following are code-owned invariants and cannot be overridden:

- Planning holds intended future state, prioritisation, commitment, horizon, and desired outcomes.
- Records holds observed events and current state.
- Governance and egress rules outrank every workflow contract.
- A contract cannot expose an unavailable tool, grant authority, inject instructions, or make external state true.
- No observation, elapsed time, or external pointer silently changes Planning.

Alternative rejected: let users write arbitrary `instructions:` prose. It is not deterministically validatable, creates a prompt-injection surface, and produces different behaviour across clients.

### 4. Deterministic resolution with explicit unknown context and refusal

The resolver accepts an exact `context` mapping whose allowed keys are `project`, `domain`, and `activity`. A missing key means **unknown**; an explicit null means **known absent**; a token means **known value**. It may also receive either one explicit saved contract key or one reviewed ephemeral proposal. It never receives the full conversation.

Resolution order is:

1. a user-selected saved contract or reviewed ephemeral proposal;
2. the unique active scoped contract with the greatest number of matched selector dimensions;
3. the unique active empty-scope default contract;
4. the immutable built-in standalone decision.

An active scoped contract is ruled out only by a known-absent or unequal known value. If an unruled contract still depends on an unknown context dimension, ordinary scoped/default resolution refuses with `WORKFLOW_CONTRACT_CONTEXT_INCOMPLETE`; it does not choose a broader contract. Explicit saved or reviewed ephemeral selection does not require its scope to match and therefore remains usable with partial context.

Every workflow file is envelope-parsed before resolution. An unreadable/unparseable file, unsupported version, duplicate active key/ID, or incomplete scan returns `WORKFLOW_CONTRACT_INVALID_INVENTORY`/`WORKFLOW_CONTRACT_SCAN_LIMIT` and blocks non-explicit resolution because applicability cannot be proven. Multiple active empty-scope defaults and equal-specificity winners return `WORKFLOW_CONTRACT_AMBIGUOUS`. Explicit lookup of an absent, archived, duplicated, or invalid selection returns `WORKFLOW_CONTRACT_NOT_FOUND`, `WORKFLOW_CONTRACT_INACTIVE`, `WORKFLOW_CONTRACT_DUPLICATE_IDENTITY`, or `WORKFLOW_CONTRACT_INVALID`. None silently falls back. An entirely absent valid contract set is not an error and resolves to standalone unless the semantic-migration sensor applies.

The result includes normalized context, source (`explicit`, `scoped`, `default`, or `builtin`), contract ID/key/title when applicable, canonical machine decision, contract fingerprint, source path/hash, fixed-template English explanation, and warnings. File order, mtime, title similarity, embeddings, and model judgment never participate.

Alternative rejected: merge every matching contract. Field-wise inheritance makes conflicts hard to explain and turns a simple routing decision into policy algebra. One winning contract plus the built-in invariant kernel is auditable.

### 5. `schema_memory` is the first product surface

Extend `schema_memory` with subject `workflow-contracts`. It remains one statically mixed/write-capable product command; current command metadata cannot expose selector-level tools. The command adds `context: object|null` and uses existing `operation`, `name`, `proposal`, `expected_hash`, and `why` fields. Existing `save: bool` is a legacy argument for other subjects: false is ignored for this subject and true refuses `WORKFLOW_CONTRACT_INVALID_ARGUMENTS`.

| operation | accepted workflow fields | leaf result |
|---|---|---|
| `inventory` | none | authorized summaries, authorized total, truncation, findings |
| `inspect` | `name` | normalized contract, authorized source provenance, presentation drift |
| `validate` | exactly one of `name` or `proposal` | validity, normalized proposal/fingerprint when valid, bounded findings |
| `resolve` | `context`, optional `name` (saved key or reserved `@standalone`) or `proposal`, never both | resolved decision/provenance or one stable bounded refusal |
| `preview` | `proposal`, optional `name` for update | exact target/content/fingerprint/current hash; no write |
| `save` | `proposal`, `why`, optional `name` plus `expected_hash` for update | standard mutation envelope and saved identity/hashes |
| `refresh` | `name`, `expected_hash`, `why` | standard mutation envelope; only managed presentation changes |

All cross-operation surplus or missing arguments refuse with `WORKFLOW_CONTRACT_INVALID_ARGUMENTS`. Read operations (`inventory`, `inspect`, `validate`, `resolve`, `preview`) take no writer lease and emit no mutation receipt. `save` and `refresh` take the ordinary writer lease and emit standard mutation/audit receipts. MCP annotations remain the static mixed/write-capable `schema_memory` annotation. The same registered selector classifier and implementation reach MCP, REST, and CLI; active profiles expose or omit the whole command, not a fictitious read-only subset.

Workflow semantic refusals use the stable codes defined in decision 4 plus `WORKFLOW_CONTRACT_INVALID_ARGUMENTS`, `WORKFLOW_CONTRACT_MIGRATION_REQUIRED`, `WORKFLOW_CONTRACT_MIGRATION_INDETERMINATE`, `WORKFLOW_CONTRACT_STALE`, and `WORKFLOW_CONTRACT_PATH_CONFLICT`. A resolved payload carries `resolved`, normalized context including known/unknown state, source class, canonical decision, schema/fingerprint, active-capability separation, fixed explanation, warnings, and released provenance.

Natural-language authoring remains agent-led: the agent drafts a structured proposal from the user's request, validates and shows the English preview, and saves only after the normal reviewed-write rule. The server does not infer workflow policy from corpus contents.

The product route registers a subject-specific, default-deny egress path and may not rely on `schema_memory`'s metadata-only exemption. It filters workflow files through the caller's release authority before inventory or resolution. An unreleased contract is physically absent for that caller: it changes neither result nor refusal and creates no existence oracle. Counts, ambiguity sets, scan totals, summaries, and winners are computed only over the released set. Only released winners receive identity/path/hash fields.

Alternative rejected: a new `workflow_memory` command. It is discoverable but expands an already broad tool surface for functionality that is structurally schema/configuration governance.

### 6. Bootstrap advertises; resolution happens at the moment of work

Bootstrap cannot know every task a session will later enter. Every profile carries the immutable workflow invariants and built-in fallback. When the active surface exports `schema_memory`, bootstrap also carries at most one released default summary and eight released scoped summaries in deterministic key order, the exact released total/projection-truncation metadata, and the exact resolution route. When the command is absent, bootstrap advertises no route and returns `resolution_available: false`; it may report built-in standalone only when the released inventory is empty and no migration marker applies, otherwise it reports fixed `workflow_resolution_unavailable` and disables contract-aware proactive routing. At the start of substantial work or when the user states durable intent, an agent with the route resolves current project/domain/activity values, explicitly marking known-absent dimensions, then inspects relevant Planning before adding or updating an item.

Compact bootstrap remains bounded: it carries the capped summaries, then names the inventory route. Full contract content remains in the canonical files and resolver output. Machine semantics and digest come from the code-owned family. The generic scaffold remains the repository's hand-authored skill source; parity tests pin its contract version, operation names, and invariant statements to the machine projection without generating it from a private vault or creating a second canonical skill.

### 7. Links-first coordination and a Records-backed feedback loop

In standalone mode, Planning may represent the complete durable hierarchy from outcome to work item. In companion mode, Planning still owns cross-domain intent and prioritisation, but declared execution artifacts stay in the companion system and Planning stores only opaque execution references and concise connective context.

When an observed outcome is sufficiently identified, the agent routes it to one compatible Records collection under the active capture posture and links it to the relevant plan when possible. A user statement that explicitly changes intent may drive the corresponding guarded Planning transition. Otherwise the contract may ask the agent to propose a transition; Records alone never changes Planning. Existing `unreflected_outcomes` and plan-progress review remain the deterministic surfacing mechanisms.

### 8. Session overrides are explicit, ephemeral proposals

An explicit user instruction in the current conversation may select a saved contract, request built-in standalone mode, or authorize a validated ephemeral proposal. Ephemeral proposals are resolved and fingerprinted but never written automatically. If the user indicates the choice should persist, the agent uses the ordinary preview-and-save path.

This makes the contract dynamic in the useful sense—resolved against current intent and scope—without making canonical policy mutate opportunistically.

## Risks / Trade-offs

- **Contract sprawl or overlap** → deterministic specificity, ambiguity refusal, bounded inventory, lifecycle, and audit findings rather than hidden priority ordering.
- **A malformed direct edit blocks useful routing** → fail only workflow resolution, surface an authorized exact file/finding through inventory/inspection, preserve all unrelated Exomem reads and writes, and never silently choose a broader contract.
- **Open ownership vocabulary drifts semantically** → validate namespaced tokens and treat them as declarations/pointers, not executable adapter capabilities. Reusable core tokens can be standardized later without closing the vocabulary.
- **Human rendering drifts from data** → frontmatter is sole authority; the generated block carries a version/fingerprint and is refreshable through guarded mutation.
- **A contract is mistaken for authorization** → bootstrap and every resolved result state that governance, tool availability, and external permissions remain separate and authoritative.
- **Proactive capture becomes noisy** → contract posture is capped by prominence, durable-intent predicates remain required, and explicit-only remains available per scope.
- **Compact bootstrap grows with personalisation** → eight-summary cap and released-only counts after a complete bounded scan; detailed resolution is a separate read-only call.
- **Older clients ignore the feature** → canonical Planning/Records files remain valid; contracts are additive `_Schema` files, and the generic bootstrap route teaches capable clients without requiring a plugin-specific hook.

## Migration Plan

1. Ship the parser/resolver/rendering substrate and optional workflow family with no contract files required.
2. Replace every normative hard-coded OpenSpec rule in canonical specs, runtime descriptions, validators, bootstrap, and scaffold with invariant + resolver guidance. Ship a generic, inactive OpenSpec example outside the active contract directory.
3. Treat this as a semantic migration even though existing Planning/Records files need no rewrite. Add product-owned `<kb_dirname>/_Schema/workflow-contract-migration.yaml` with exact `{schema_version: 1, review_required: <bool>}`. Before the first feature-aware scaffold refresh writes any managed file, it atomically creates this marker when absent: `review_required: true` if any managed scaffold sentinel existed at call entry (a known pre-feature vault), otherwise false for a fresh vault. An existing valid marker is preserved. A missing marker on a vault already containing managed sentinels is conservatively treated as required; an unreadable/invalid marker refuses `WORKFLOW_CONTRACT_MIGRATION_INDETERMINATE` and is never overwritten. This classification does not depend on mutable scaffold prose or a Planning scan.
4. When `review_required` is true and there is no active released workflow contract, bootstrap and inventory report `workflow_contract_migration_required`, and ordinary fallback refuses `WORKFLOW_CONTRACT_MIGRATION_REQUIRED`. A user bypasses it for the current session by resolving reserved `name: "@standalone"`, or durably satisfies it by saving a reviewed active standalone/companion contract. `@standalone` is outside the key grammar and cannot collide with a saved contract. A fresh marker with `review_required: false` permits zero-config built-in standalone.
5. Existing execution pointer values remain valid under the new open key syntax. Users who want an existing companion workflow create an explicit contract through the reviewed save path. The maintainer vault will dogfood an Exomem/OpenSpec/GitHub contract and update global agent instructions to resolve rather than hard-code it.
6. Rollback is data-preserving: older versions ignore the nested workflow directory, while the Markdown contracts remain inspectable. Re-upgrade restores resolution from the same canonical files.

## Deferred Decisions

- This delivery does not add a registry for recurring ownership tokens. Tokens remain syntactically governed and semantically open; a later change may add descriptions or aliases without changing resolution.
- The hosted portal editor is outside this delivery. Any later direct editor or form must call the same guarded save surface and keep the Markdown files canonical.
