# Design

## Context

Three surfaces exist today and they disagree about what Exomem is.

| layer | count | where it is exposed |
|---|---|---|
| canonical leaves | — | internal |
| product commands (`PRODUCT_COMMANDS`) | 29 | `cli`, `mcp`, `rest` |
| product actions (`_SIMPLE_ACTIONS`) | 8 | CLI-family only (`product_invoke.py`, `surface="cli"`) |
| hosted agent profile v1 | 13 | hosted MCP |

The consolidation this change wants was already built — it is the action layer — and agents never received it. The parity hole was already detectable — `simple_action_catalog()` marks `adopt`, `maintain` and `record` unavailable under v1 — and nothing asserted on it.

## Goals and non-goals

**Goals.** Fewer things an agent must choose between. No capability lost. Hosted equal to local unless a specific technical reason says otherwise, with that reason recorded. Parity that cannot silently regress.

**Non-goals.** Changing any command's behaviour or schema. Mutating a published hosted profile. Media enablement. Bridging transfer/adopt to their gateway flows.

## The blocker nobody has stated

The action catalog is **not capability-complete**. Routes and `advanced` entries together reach 18 of 29 commands. Unreachable from any action:

`bootstrap`, `plan_memory`, `coordination_status`, `preserve_artifacts`, `process_media`, `read_media`, `query_dataset`, `adoption_studio`, `govern_memory`, `schema_memory`, `manage_memory_file`

Promoting the action layer to *the* agent surface while it covers 18 of 29 would drop eleven capabilities — precisely the outcome this change forbids. Completing the catalog is therefore a prerequisite, not a nicety, and it is the first substantive task.

### Proposed assignment of the eleven

| command | action | placement | rationale |
|---|---|---|---|
| `bootstrap` | — | own tool | The entry point that returns the catalog; it cannot be behind the catalog |
| `plan_memory` | `remember` | advanced | Stating intent is a durable write; see open question 1 |
| `coordination_status` | `review` | advanced | Read-only status, same intent family as audit |
| `preserve_artifacts` | `capture` | advanced | Sits beside `preserve_evidence` and `transfer_artifact` |
| `process_media` | `capture` | advanced | Media ingestion is raw-material capture |
| `read_media` | `ask` | advanced | Reading an artifact by reference |
| `query_dataset` | `ask` | advanced | Structured retrieval is retrieval |
| `adoption_studio` | `adopt` | advanced | Already the review half of adoption |
| `govern_memory` | `maintain` | advanced | Governance administration |
| `schema_memory` | `maintain` | advanced | Schema administration |
| `manage_memory_file` | `maintain` | advanced | File and data operations |

The catalog already filters `advanced` by `available_tools`, so these placements degrade correctly on a surface that withholds a command — which is exactly the mechanism that exposed the hole.

## Options for the agent-facing surface

**A — action tools (recommended target).** Nine MCP tools: the eight actions plus `bootstrap`. Route selection becomes an `operation`/route parameter. Twenty-nine choices become nine. Largest simplification and the closest reading of the intent; also the largest redesign, and mega-tools with mode parameters are a known way to make schemas harder for a model to use well, so schema quality is the risk to manage.

**B — keep 29, fix parity only.** Complete the catalog, expose it through `bootstrap`, make hosted equal local. Immediate, low risk, and closes the capability hole — but delivers no tool reduction, so it does not answer the ask.

**C — tier by `product_surface`.** The field already classifies commands `primary` (21) and `advanced` (8). Expose primary as tools, keep advanced reachable, use the catalog as the entry map. Moderate reduction, no schema redesign, parity complete.

### Recommendation: stage it, A as the destination

The capability hole and the consolidation are different problems with different urgency. A hosted customer cannot adopt or maintain **today**; that is a live defect. Tool-count reduction is a design improvement that deserves schema care.

1. **Complete the catalog** to 29/29 and add the coverage gate. Fixes the silent-drift class.
2. **Ship `hosted-alpha-agent-v4` as the complete product surface**, tier 2 included. Closes the capability hole; parity becomes structural.
3. **Then land option A** as its own change, with the schema work done properly rather than rushed alongside a promotion.

Steps 1 and 2 belong to this change. Step 3 is the follow-on this change makes safe, because after step 1 there is a total mapping to consolidate *over*.

## Why hosted parity is a rule, not a list

v1's membership was correct for a closed alpha and became wrong the moment the tier was paid. A hand-maintained list drifts by default: every new command is absent from hosted until someone remembers. Inverting it — the profile *is* the product surface, exclusions are the exception and must be justified — makes the default correct and the exception visible. `transfer_artifact` and `adopt_vault` are the only commands qualifying today; both are actively intercepted (`HOSTED_TRANSFER_INTERCEPT_REQUIRED`, `HOSTED_IMPORT_INTERCEPT_REQUIRED`) and reachable through gateway flows, so exposing them as tools would surface an error rather than a capability.

Tier 2 is included. `docs/capabilities.md` describes it as advanced governance administration and file/data operations; every one of those operates inside the calling tenant's own vault, which is the same blast radius a local user already has. Withholding it buys no safety — the same argument `widen-hosted-epistemic-surface` made for supersession.

## Capability preservation is a gate, not a promise

Two assertions, both cheap, both would have caught the current state:

- **Catalog coverage**: every `PRODUCT_COMMANDS` entry is reachable from at least one action route or `advanced` entry. Fails today at 18/29.
- **Hosted parity**: the hosted profile equals the complete product surface minus a registry of exclusions, each carrying a reason string. Adding a command without deciding its hosted status fails the build.

Both belong beside the existing surface-contract tests.

## Risks

Consolidation changes the tool names customers first meet, so it cannot ride along with a promotion that is already blocked. Legacy names stay resolvable for one release — the precedent is `_ADDITIONAL_CALLABLE_NAMES`, which still recognises the primitives `PRODUCT_COMMANDS` folded away.

Tool names appear in skill prose validated by `validate_skill_text`, and the shipped scaffold must stay generic (`tests/test_scaffold_no_leak.py`). Renaming without updating prose fails those gates — which is the intended behaviour.

Nothing here is observable end to end until substrate PR #144 deploys: `ask_memory` and `connect_memory` publish an `x-fastmcp-wrap-result` schema the gateway never applied, so every hosted tool call has returned `CELL_RESPONSE_INVALID`. Every tool except those two is currently inferred-correct from its schema and has never been observed working through the gateway.

## Open questions

1. Does `plan_memory` deserve a ninth action (`plan`) rather than sitting under `remember`? Stating intent before acting is arguably its own intent, and `widen-hosted-epistemic-surface` treated planning as a first-class part of the epistemic loop.
2. Under option A, does `bootstrap` remain a separate tool or become the catalog response itself?
3. Should `read_media` and `query_dataset` stay tier 2 once hosted exposes tier 2, or be reclassified as ordinary retrieval?
