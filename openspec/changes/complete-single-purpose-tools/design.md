## Context

See proposal.md for scope. The pilot already normalizes typed operations before authorization and retains grouped execution identities. Its renderer supports one command per public tool. Completing the original per-command split would produce about 50 ordinary tools. Related-job consolidation instead produces a design inventory of 39 ordinary tools and 40 Cloud tools.

## Goals / Non-Goals

Preserve every admitted capability, credential rule, response contract and retry identity while keeping the public catalogue small. Reuse the existing renderer and FastMCP discovery transform. Do not add another dispatcher per tool, change leaf semantics, enable excluded Cloud operations, or change recall inputs.

## Decisions

### One public job can contain several existing execution commands

Group public operations by the job an agent performs. Each exact typed variant selects one existing command and its arguments before authorization. Keep the source command, field constraints, aliases and effect in declarative metadata. Generate schema branches and invocation resolution from that metadata.

Simple per-command pairs avoid shared routing, but add roughly ten tools. Shared routing earns its cost by retaining the smaller catalogue without deleting capability. It does not authorize new effects.

### Inventory

The ordinary inventory contains the following tools. These counts are design expectations until generated discovery proves them.

| Public tools | Count | Related operations |
| --- | ---: | --- |
| `bootstrap` | 1 | Instructions, settings inspection and writer status |
| `recall_context`, `search_memory`, `read_memory` | 3 | Context, targeted recall, pages, datasets and views, including the complete graph view |
| `browse_memory` | 1 | Folder listing, trash inspection and file reclassification preview |
| `create_note`, `edit_note`, `supersede_note`, `prepare_note` | 4 | Note authoring; preparation includes sourced drafts and observation validation |
| `capture_source`, `preserve_evidence`, `preserve_artifacts` | 3 | Original sources, evidence text and evidence files |
| `triage_memory`, `read_media`, `transfer_artifact` | 3 | Review actions, media frames or status, file transfer preparation |
| `read_records`, `change_records`, `read_plans`, `change_plans` | 4 | The pilot surface |
| `read_review` | 1 | Review lists and item context; question submission belongs to `triage_memory` |
| `read_connections`, `change_connections` | 2 | Connection inspection and governed changes |
| `read_schema`, `change_schema` | 2 | Schema inspection and guarded changes |
| `read_governance`, `change_governance`, `grant_session_access`, `change_session_access`, `manage_authorization_session` | 5 | Distinct inspection, owner, token-session, self-session and session-lifecycle contracts |
| `change_files`, `change_observations`, `change_settings` | 3 | File, semantic-unit and preference changes |
| `read_episode`, `record_episode` | 2 | Recap inspection and persisted workflow changes |
| `change_media` | 1 | Media processing and retry |
| `read_adoption`, `change_adoption` | 2 | Legacy adoption and Adoption Studio through one related job |
| `check_memory`, `repair_memory` | 2 | Maintenance previews and admitted repairs |

Cloud adds `observe_transfer` and `transfer_memory`, and excludes `transfer_artifact`, for 40 tools. Video exclusion removes only the video variant of `read_media`; media status keeps that tool listed. Other exclusions apply per variant.

### Keep discovery separate from executable registrations

Keep one executable registration per grouped command. The discovery transform publishes the combined public schemas without replacing those executable registrations. FastMCP supports separate list and lookup transforms; the existing compatibility transform already uses this boundary.

An explicit nested operation object selects the typed shape. Flat compatibility arguments retain their previous route. Malformed or mixed nested and flat inputs refuse; they never fall back to legacy interpretation. This avoids replacing a flat executable with a nested handler under the same name.

Resolve the operation kind to its target command before validating its fields and credentials. A valid kind with invalid fields receives that command's refusal contract. Carry the selected command and normalized authorization selectors through that refusal; do not restore the raw nested arguments. An unknown kind has no execution target: return a public invalid-operation refusal and record no invented target identity. Never select the first contributor's credentials or projector merely to construct a refusal.

A public tool with one contributor can retain its established domain refusal code without attaching an execution target. This preserves the pilot's error contract. A shared tool with no unique source uses the generic invalid-operation code. Neither path enters authorization with an undefined selector.

Validate unique executable spellings separately from public routes. Each public tool and public kind selects one command; literal alternatives can select different effects within that command. Derive discovery, resolution and capability names from the same filtered contributors, without a second routing table.

### Literal constraints and field aliases share one source

Represent scalar or nested literal conditions in operation entries. Distinguish booleans from integers, null from omission, and omission from an explicit default. Preserve the caller's omitted fields when flattening. Schema alternatives, runtime matching, spelling, inverse spelling and coverage read the same constraints.

Use declared wire-to-leaf aliases where a data field conflicts with `operation.kind`. Observation `unit_kind` and file `file_kind` map to their existing leaf `kind` field. Public kinds may differ from grouped selectors when related commands would otherwise collide.

### Effects follow current classifiers

Maintenance uses its existing dry-run, apply and curation rules. Curation proposals persist workflow state and remain writes. Governance companion previews and session status also remain write-classified. Do not classify by an operation's English name.

Generate vocabulary subjects from `registry_specs()` and governance authorization families from `OPERATION_SPECS`. Preserve each variant's credential presence rule from the transport registry. Inspection variants can require or optionally accept the same session credential; session opening forbids it while status requires it. Do not turn those variant rules into a tool-wide requirement. Do not copy vault vocabulary into code. Closed API selectors and literal field formats are protocol data and require a code reason where a list is not obvious.

### Preserve complete capabilities during consolidation

The global graph route retains drawing limits, coverage, counts and its original result; a context catalogue map cannot replace it. Settings and writer-status routes call their existing commands directly. Writer status must not build bootstrap first during an outage. Media reads retain native image results.

Keep `preserve_artifacts` as a separate single-purpose tool with its existing top-level `files` and file-input metadata. The gateway admits that exact operation and binds artifact grants to it. Combining it with text evidence would require a gateway release and a conditional file-input schema. This cost does not earn one fewer name.

Instead, expose dataset queries as a typed `read_memory` variant that normalizes to `query_dataset`. It reads a selected memory object, like the page and view variants. Keep the source command's tier-2 admission, permissions, provenance, filters and exact output. A listed `read_memory` must not expose dataset access when tier 2 is disabled.

Keep adoption schema changes after the containing collection-adoption merge. Preserve its folder rules, cursors, diagnostics and wrappers. Convert the other families while that dependency remains open.

### Reuse the existing experiment runner

Extend the tool-selection harness rather than introduce another runner or telemetry service. Use harder workflows across the converted families, with source-derived end-state keys authored outside measured sessions. Keep grouped and nested arms on the same bytes while measuring.

Start with free contract checks and a small complete development round. Freeze the decision protocol and fresh held-out cases before the decision run. Reuse the approved non-inferiority floor of 0.10 and inspect every wrong write. Report per-case paired differences and intervals. An interval containing zero means no change. Remove the grouped baseline option only after the decision run.

## Risks / Trade-offs

- Shared names can collide with legacy registrations. Publish schemas through discovery and retain one grouped executable.
- Incorrect route selection can change authorization or retry identity. Compare normalized calls at the boundary and exercise cross-spelling replay.
- Schema expansion can offset fewer tool names. Measure complete wire bytes and input tokens; claim no efficiency gain without evidence.
- A consolidated name can hide an unrelated job. Keep exact typed variants, clear descriptions and complete-workflow evaluation.
- Host and provider access can limit a measurement. Record unavailable hosts and continue independent work without new logins or paid API spend.

## Migration Plan

1. Build the renderer foundation and verify that the pilot schemas remain unchanged.
2. Add shared public variants and regenerate the active guidance and packages.
3. Integrate the merged adoption contract and run the paired decision check.
4. Review the exact integrated batch, pass full CI and merge the ready PR.
5. Let the release owner publish and qualify the ordinary containing release.
6. Keep Cloud rollout and rescan pending the operator's existing authority boundary.

The grouped call contracts remain available for compatibility. A source rollback reverts the public rendering batch; it does not rewrite stored knowledge. Hosted rollout and undo use the release owner's operational procedure.
