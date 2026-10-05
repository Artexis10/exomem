# Design

## Context

Three surfaces exist today and they disagree about what Exomem is.

| layer | count | where it is exposed |
|---|---|---|
| canonical leaves | — | internal |
| product commands (`PRODUCT_COMMANDS`) | 29 | `cli`, `mcp`, `rest` |
| product actions (`_SIMPLE_ACTIONS`) | 9 | CLI-family only (`product_invoke.py`, `surface="cli"`) |
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
| `plan_memory` | `plan` | primary route | `main` added a ninth action for it while this branch was open; see decision 1 |
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

**A — action tools (recommended target).** Ten MCP tools: the nine actions plus `bootstrap`. Route selection becomes an `operation`/route parameter. Twenty-nine choices become nine. Largest simplification and the closest reading of the intent; also the largest redesign, and mega-tools with mode parameters are a known way to make schemas harder for a model to use well, so schema quality is the risk to manage.

**B — keep 29, fix parity only.** Complete the catalog, expose it through `bootstrap`, make hosted equal local. Immediate, low risk, and closes the capability hole — but delivers no tool reduction, so it does not answer the ask.

**C — tier by `product_surface`.** The field already classifies commands `primary` (21) and `advanced` (8). Expose primary as tools, keep advanced reachable, use the catalog as the entry map. Moderate reduction, no schema redesign, parity complete.

### Recommendation: stage it, A as the destination

The capability hole and the consolidation are different problems with different urgency. A hosted customer cannot adopt or maintain **today**; that is a live defect. Tool-count reduction is a design improvement that deserves schema care.

1. **Complete the catalog** to 29/29 and add the coverage gate. Fixes the silent-drift class.
2. **Ship `hosted-alpha-agent-v4` as the complete product surface**, tier 2 included. Closes the capability hole; parity becomes structural.
3. **Then land option A** as its own change, with the schema work done properly rather than rushed alongside a promotion.

Steps 1 and 2 belong to this change. Step 3 is the follow-on this change makes safe, because after step 1 there is a total mapping to consolidate *over*.

## Why hosted parity is a rule, not a list

v1's membership was correct for a closed alpha and became wrong the moment the tier was paid. A hand-maintained list drifts by default: every new command is absent from hosted until someone remembers. Inverting it — the profile *is* the product surface, exclusions are the exception and must be justified — makes the default correct and the exception visible. Four commands qualify today. `transfer_artifact` and `adopt_vault` are actively intercepted (`HOSTED_TRANSFER_INTERCEPT_REQUIRED`, `HOSTED_IMPORT_INTERCEPT_REQUIRED`) and reachable through gateway flows, so exposing them as tools would surface an error rather than a capability. `process_media` and `read_media` need dependencies the hosted image does not carry -- its Dockerfile stage installs only the `embeddings-onnx` extra and gates the build on `torch` being absent -- under a `media` feature grant no alpha cell holds, so they would publish as tools that refuse on every cell.

Tier 2 is included. `docs/capabilities.md` describes it as advanced governance administration and file/data operations; every one of those operates inside the calling tenant's own vault, which is the same blast radius a local user already has. Withholding it buys no safety — the same argument `widen-hosted-epistemic-surface` made for supersession.

## Capability preservation is a gate, not a promise

Two assertions, both cheap, both would have caught the current state:

- **Catalog coverage**: every `PRODUCT_COMMANDS` entry is reachable from at least one action route or `advanced` entry. Failed at 18/29 when this change opened; now enforced.
- **Hosted parity**: the hosted profile equals the complete product surface minus a registry of exclusions, each carrying a reason string. Adding a command without deciding its hosted status fails the build.

Both belong beside the existing surface-contract tests.

## Risks

Consolidation changes the tool names customers first meet, so it cannot ride along with a promotion that is already blocked. Legacy names stay resolvable for one release — the precedent is `_ADDITIONAL_CALLABLE_NAMES`, which still recognises the primitives `PRODUCT_COMMANDS` folded away.

Tool names appear in skill prose validated by `validate_skill_text`, and the shipped scaffold must stay generic (`tests/test_scaffold_no_leak.py`). Renaming without updating prose fails those gates — which is the intended behaviour.

Nothing here is observable end to end until substrate PR #144 deploys: `ask_memory` and `connect_memory` publish an `x-fastmcp-wrap-result` schema the gateway never applied, so every hosted tool call has returned `CELL_RESPONSE_INVALID`. Every tool except those two is currently inferred-correct from its schema and has never been observed working through the gateway.

## Decisions

**1. `plan_memory` — decided the other way, by `main`, while this branch was open.** The reasoning here was that planning argues for reachability rather than a top-level verb, so `plan_memory` would sit under `remember` as an `advanced` entry and the action count would stay at eight. `main` then shipped a ninth action, `plan`, routing to `plan_memory` directly. That answers the question, and the answer stands: the catalog now names nine actions, `plan_memory` is `plan`'s primary route, and the `advanced` entry under `remember` was removed as redundant rather than kept as a second name for the same command.

This is worth stating rather than quietly conforming to, because it is the one decision in this change that a reader would otherwise find contradicted by the code.

**2. `bootstrap` stays a separate tool under option A.** It is the call that returns the catalog, so it cannot be an operation *within* the catalog without a chicken-and-egg for any agent that has not called it yet. This is why it is the single exemption in the coverage gate rather than an oversight.

**3. Tier classification is unchanged.** Tier is a statement about how advanced a command is, not about where it is hosted. Reclassifying `read_media` and `query_dataset` out of tier 2 would change CLI and REST behaviour for every existing operator to solve a hosted-only problem — and it solves nothing, because once hosted exposes tier 2 both commands are available anyway. If they are later judged to be ordinary retrieval, that is its own change with its own reason.

## Budget consequence

Completing the catalog added 183 bytes to compact bootstrap, taking the merged payload to 61,480 against a 61,400 ceiling `main` already sat 103 bytes under — 80 over.

The ceiling did not move. Six of the added entries named a command that is already another action's primary route, and the catalog names those actions, so the entries said nothing the payload did not already say. Removing them landed compact at 61,376, 24 bytes under, with coverage unchanged because the gate counts routes and `advanced` together. The rule is now that `advanced` carries commands no route reaches. The arithmetic is in `COMPACT_BYTE_CEILING`'s docstring, measured on the final tree.

## Status of the prerequisite

The catalog is complete: routes and `advanced` entries now reach 28 of 29 product commands, `bootstrap` being the exemption above. `test_simple_action_catalog_reaches_every_product_command` asserts it and fails against the pre-change source naming all ten previously unreachable commands, so the gate discriminates rather than passing by construction.
