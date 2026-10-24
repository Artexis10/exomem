## Context

See proposal.md for the problem and scope.
The shared registry builds 33 product tools across MCP, REST, and CLI.
Invocation classification already separates reads from mutations; MCP discovery publishes one conservative annotation set for each complete tool.
Public titles currently come from command spelling, with one adoption-specific exception.

The existing naming change supplies six preferred names and compatible dispatch.
This change builds on that contract without another rename campaign or an operation split.
The existing selector registry and classifiers remain the authorities for supported operations and their effects.

## Goals / Non-Goals

The design makes each tool's job, required inputs, effect, result, and recovery route understandable at its point of use.
It also makes user-facing titles useful when a host displays them.
The design preserves capabilities, call shapes, response contracts, safeguards, and context continuity.

Host clients own permission prompts and activity rendering.
This change neither promises fewer prompts nor marks mixed tools read-only.
Provider review concerns affect that distribution channel, not ordinary releases for other clients.
This change adds no model, classifier, workflow engine, dependency, or heavyweight optional path.

## Decisions

### 1. Keep the current task families

Keep all 33 tools and the six preferred names.
Records describe observed state; Planning describes intended state; Notes hold compiled conclusions; Sources and Evidence preserve originals.
Those distinctions remain visible in the descriptions and examples.

A full operation projection increases the public catalogue and requires a separate compatibility and filtering design.
A selective read/write split creates narrower permission boundaries but does not establish provider acceptance or better tool selection.
Keep these alternatives available if manual review or observed host behavior identifies a concrete requirement.

### 2. Own public titles in the command registry

Add an optional public title to Command and pass it from _PRODUCT_SPEC metadata to the existing annotation builder.
Keep the generic name-derived fallback for compatibility with non-product commands.
Replace the adoption-specific branch with metadata on its registry entry.
Titles describe the whole tool, including mixed effects; they never pretend to describe a particular invocation.

The following table covers every current product tool and gives the proposed title and review focus.
Descriptions retain any necessary distinctions that cannot fit in a short title.

The integrated registry still contains all 33 families and the six compatible name pairs.
Reconciliation found no operation, parameter, selector, or effect-coverage change.
Existing provenance, semantic-authoring, continuity, and refusal guidance remains part of the reviewed surface.

| Tool | Proposed public title | Review focus |
|---|---|---|
| bootstrap | Read memory instructions and settings | Reuse held policy; request detail only when needed. |
| recall_context | Recall context for this task | Preserve session, workspace, recent excerpts, maps, and task refresh. |
| search_memory | Search saved knowledge | Explain scope, remaining evidence gaps, filters, and partial results. |
| read_memory | Read a saved page or view | Distinguish a page, semantic unit, history page, and continuation. |
| browse_memory | Browse memory folders | Explain structure discovery without implying exhaustive recall. |
| viewer_map | View connections in memory | Explain bounded graph coverage and its limits. |
| create_note | Create a knowledge note | Distinguish conclusions from originals; retain provenance and validation guidance. |
| edit_note | Edit a knowledge note | Explain typed edits, current guards, preview, and recovery. |
| supersede_note | Supersede a knowledge note | Preserve history and explain the difference from a correction. |
| prepare_note | Prepare a sourced note | State that preparation saves nothing; reuse an existing capture proposal. |
| observe_memory | Check or edit a note entry | Explain semantic units, validation, and replacement of omitted fields. |
| episode_memory | Save or manage a session recap | Explain read actions, durable capture, stable keys, and revisions. |
| capture_source | Save original source material | Distinguish text, file handles, provenance, and optional preparation guidance. |
| preserve_evidence | Preserve original evidence text | Explain append-only storage and committed versus repeated preservation. |
| preserve_artifacts | Preserve original evidence files | Explain handle transport, per-file outcomes, and partial failure. |
| transfer_artifact | Prepare a file transfer | Distinguish issuing a handoff from completing a transfer. |
| process_media | Check or process media | Distinguish status reads, processing, retry, and deferred extraction. |
| read_media | Read video frames | State the supported media type and bounded sampling. |
| query_dataset | Query a data file | Explain the supported formats and returned provenance. |
| record_memory | Read and update records | Explain collection discovery, queries, held items, imports, and guarded changes. |
| plan_memory | Read and update plans | Separate recall from a change of intent; retain exact mutation identity. |
| connect_memory | Find or create knowledge connections | Distinguish suggestions, entity resolution, entity creation, and accepted relations. |
| review_memory | Review memory or submit a review question | Distinguish read-only review from durable question submission. |
| review_item_context | Read a review item's context | Explain fingerprint binding, bounded evidence, and continuation. |
| triage_memory | Manage review decisions and attention | Explain decisions, snoozing, quieting, and reserved setting references. |
| configure_memory | Read or change memory settings | Retain explicit intent, revision checks, and the distinction from authority. |
| schema_memory | Inspect or change memory schemas | Explain subject-specific inputs and conditional saves. |
| govern_memory | Inspect or manage access policies | Separate inspection, proposals, grants, and guarded policy changes. |
| maintain_memory | Check or repair the knowledge base | Explain preview defaults, writes, restrictions, and recovery routes. |
| manage_memory_file | Read or change managed files | Explain typed-tool preference, file effects, and destructive flags. |
| adopt_vault | Scan or import an existing vault | Explain scan defaults, governed copies, and preservation of originals. |
| adoption_studio | Review and import existing material | Explain run state, exact preview/apply, proposals, and original preservation. |
| coordination_status | Check the memory writer's status | Limit the claim to writer coordination health. |

### 3. Explain effects and inputs at the decision point

Front-load each description with its job, when to choose it, and material effects.
For a selector, explain which branches read, write, or depend on a flag.
Use the existing operation registry and classifier when checking those claims; add no parallel effect catalogue.
Keep guards and incident-backed recovery details reachable without loading a complete manual.

For Records, show inspect/query for recall and the returned UUID, item version, and container hash for an authorized correction.
For Planning recall, allow multiple matching items; a mutation must select exactly one item and retain its guards.
Ordinary recall can return admitted collection manifests; vault scope does not admit raw Records or Planning bodies.

For schema administration, distinguish inspection and previews from guarded saves and restores.
General inference remains read-only unless its existing save flag requests a mutation.
No description may weaken a permission or change a runtime default.

Review large schemas for confusing names and operation-specific requirements.
Remove redundant prose, not supported parameters or capabilities.
Keep dynamic note types, fields, vocabularies, and statuses in their existing registries.

### 4. Give each instruction layer one job

SERVER_INSTRUCTIONS describes Exomem's scope, policy reuse, bounded context, maps, and evidence boundaries.
It preserves the maps clause and teaches no unconditional collection of conversation history.
The activation tool retains session, workspace, and optional recent excerpts with their current bounded, purpose-specific behavior.
Bootstrap returns current policy and available routes; clients reuse it while current.
Tool descriptions teach selection and effects; skill references teach detailed procedures.
Hooks point back to those contracts and do not create a second policy or automatic write authority.

Audit loading and fallback behavior as well as text.
A stale skill digest must remain a visible fallback rather than appearing to load the requested session profile.
This batch changes teaching around an existing fallback; it adds no refusal or new bootstrap requirement.

The hand-authored source is src/exomem/_scaffold/_Schema/.
Hook sources are src/exomem/_hooks/; package_skills and install_hook deploy them.
Current plugin trees and archives remain generated outputs.

### 5. Use outcome proof and reuse existing coverage

The highest-level check is one isolated walkthrough through discovery and real product calls.
It covers a Records query/correction, Planning query/transition, schema inspect/save, and adoption status/preview/apply.
Reference state exists before the actor runs; final persisted state and traces establish the outcome.
Read calls remain lease-free, mutations keep guards and receipts, and denied or stale calls leave state intact.

Compare the same tool schemas, annotations, instructions, and relevant workflow outcomes before and after.
Use the existing public-interface acceptance infrastructure and reuse unaffected naming and mutation proof.
Run scoped annotation, schema, bootstrap, hook, privacy, and generator checks during development.
Run the full required corpus once the integrated batch is ready.
Keep existing byte ceilings; do not add a framework or tests that pin prose.

Inspect a supported isolated host's reference and candidate activity with DevTools when a rendered comparison is available.
Record host version, displayed title, prompt behavior, and actual effect.
Otherwise report rendered behavior as unverified, even when wire metadata is correct.
No production-connected preview may perform test writes.

A claim about routing, calls, latency, or prompt reduction needs a separate comparable measurement.
This plan claims clearer metadata and preserved behavior, not an unmeasured model-quality gain.
Metered evaluation requires its existing budget authority.

## Risks / Trade-offs

- Mixed tools retain broad discovery hints → explain the full capability and distinguish invocation authorization from presentation.
- Hosts can ignore public titles → verify the supported host and report other rendering as unknown.
- Shortening can remove essential teaching → check realistic workflows and preserve guards, provenance, and recovery routes.
- Guidance can drift from selectors → check against the shared registry and classifiers during integrated review.
- Provider rules can reject grouped operations → retain manual-review evidence and report acceptance as pending until observed.

## Migration Plan

1. Integrate the naming change and qualified guidance corrections, preserving the existing delivery batch and current main changes.
2. Update metadata and owning instruction sources, then regenerate current surfaces from the exact installed candidate.
3. Complete the independent integrated review and required CI, with a scoped recheck for each correction.
4. Release through the ordinary owner, preserving any Compiler freeze boundary and the coherent package rollback.
5. Verify the installed surface and compatibility, then synchronize and archive completed changes through OpenSpec.

Cloud deployment requires its explicit authorization.
One operator rescan of the final deployed surface can also close the existing selector-enum delivery tail.
Prepare manual-review material when needed; preparation is not submission or publication.

## Source comparison

The maintained registry, adapters, and generators already supply the needed seams; the lasting addition is explicit title metadata.
The design follows purposeful tools and small, relevant context while preserving application authority outside prose.
It deliberately keeps grouped operations pending manual review and retains purpose-specific context inputs.

Primary references: [tool design](https://www.anthropic.com/research/writing-tools-for-agents), [context engineering](https://www.anthropic.com/research/effective-context-engineering-for-ai-agents), [containment](https://www.anthropic.com/research/how-we-contain-claude), and [current plugin requirements](https://developers.openai.com/plugins/plugin-guidelines).
The plugin requirements request separate exposed operations and disallow prior-turn arrays; this design does not claim those concerns are resolved.
