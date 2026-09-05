# Design

## Loading boundary

SKILL.md is the entrypoint plus the shared contract. It names when each local
reference is required. An ordinary lookup needs no mutation, media or maintenance
procedure. References are read once when their operation becomes relevant, and
are not a second mandatory manual loaded on every invocation.

The live engagement envelope cannot be baked into a static skill: obtain compact
bootstrap when current policy/capabilities are missing, reuse it within the
connection, and refresh after a policy/adapter change. Use the active tool surface,
not the canonical full discovery digest, to determine availability. Tool discovery
syntax is harness-specific; the route itself is not.

## Contract preservation map

| Existing contract | Destination / always-visible guard |
|---|---|
| Sources/Evidence immutability, managed scope, honest provenance | entrypoint; writing and write-scope references |
| Prominence and durable landing classes | entrypoint summary; engagement reference |
| Action-class ceiling, disposition, confirmation and triage | complete existing envelope remains in entrypoint |
| Authoring grammar/version/digest/minimum-unit gate | complete canonical render_concise remains once in entrypoint and every standalone authoring workflow |
| Dedupe, first-write provenance, reviewed connections, page taxonomy | entrypoint pre-write loop; writing reference and existing detailed references |
| Planning intent vs observed Records, guarded transitions, collection ownership | entrypoint guard; planning-records reference |
| Mutation terminals, warnings and idempotent retry | entrypoint refusal/identity guard; mutation-results required before mutations |
| Recall scope, identity resolution, filters and diagnostics | entrypoint recall loop; recall reference |
| Stable IDs and human-readable citations | full durable-reference section remains in entrypoint |
| Media lane selection, file handles, upload/download, leaf escape hatches | operation-routing reference and existing operations reference |
| Structure reports, adoption preview, audit-only findings, no decay | vault-care reference and operation details |
| Opt-in governance and content/instruction boundary | entrypoint; governance reference |

Extraction preserves the detailed procedures. Internal references are made
relative to their new directory. Existing batch/standing capture waivers remain
subject to the live envelope; they do not raise entity/supersession/restructure
confirmation ceilings. No new approval flow is introduced.

## Distribution and compatibility

Keep the existing filesystem/zip/plugin package mechanisms: core references are
already bundled by all three. Named native installer clients are Claude and
Codex; explicit filesystem targets may serve other skill-capable clients. This
is portable guidance, not a guarantee every harness implements skills, local
resources, tool discovery or the same engagement hooks. Standalone workflows
retain their existing complete grammar so they do not depend on another package.
Every standalone authoring workflow also embeds the core entrypoint's small
portable operating-rules block (live capabilities/envelope, immutable scope,
first-write provenance/connections, same-identity retries). This earned
repetition makes isolated uploads safe without importing the whole core package.
Tests derive the shared block from the core source rather than restating it.
The separate hosted plugin is unchanged.

## Verification

Exercise route destinations in source and upload payloads, both native installer
clients, plugin synchronization, unchanged canonical authoring projection,
privacy, and existing surface contract assertions. Move an assertion to the
relevant routed procedure when teaching moves; never remove the behavioral
expectation. Compare entrypoint bytes and estimated tokens before/after separately
from all-reference size. Independently review routing scenarios and missing-resource
behavior; static strings alone do not establish behavioral equivalence.

## Measured validation

The core entrypoint is 5,082 comparative o200k tokens, down from 20,512
(75.2% less entrypoint input). Detailed references remain bundled; this is not a
75.2% reduction in every operation or in invoiced cost. The canonical semantic
projection remains intact. The scoped packaging, install, privacy, and operating
contract suite passed 167 tests; independent scenario review approved after
standalone retry/first-write and trigger-coverage corrections. Full corpus
validation runs in the repository's existing CI shards before merge.
