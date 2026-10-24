## Decision

Keep `Command.name` as the execution identity and add a preferred public name to the existing registry.
The preferred name defaults to the execution identity. These six commands override it:

| Execution identity and compatible spelling | Preferred public name |
|---|---|
| activate_context | recall_context |
| ask_memory | search_memory |
| remember | create_note |
| edit_memory | edit_note |
| replace_memory | supersede_note |
| compile_source | prepare_note |

All other public names remain unchanged. This change preserves 33 operation boundaries.

## Dispatch and discovery

Both spellings resolve directly to the same command after surface, tier, and Cloud filtering.
Resolve names before credential rules, edit normalization, tracing, writer leases, mutation identity, and egress checks.
Keep implementation routes and durable receipt identities unchanged.

Use FastMCP's public `Transform.list_tools` hook to hide compatible registrations while preserving registered-name lookup.
Do not reuse the opt-in primitive compatibility surface, disable tools, or access private provider state for name resolution.
REST registers both paths but documents the preferred path. CLI accepts both spellings and preserves existing short-wrapper grammar.
Capability metadata recommends preferred names. Internal callability includes compatible spellings without recommending them.
Every old spelling remains callable until a later explicit contract change removes it; no automatic expiry or alias chains apply.

## Guidance and generated clients

Descriptions state the job, result, neighboring-tool distinction, and relevant side effects in plain language.
Preserve incident-backed remediation, semantic-authoring rules, guards, and portable minimum teaching.
Teach context reuse, search for remaining evidence gaps, selected-result reads, and existing combined capture guidance.
Change the owning scaffold and hook sources, then regenerate schemas, fingerprints, capabilities, skills, and current plugin trees.
Do not change frozen historical contracts or an externally attested digest without fresh external evidence.

## Reuse and cost

The registry, `bind_vault` exposed-name argument, middleware, and FastMCP transforms already provide the required seams.
The gap is a shared distinction between advertised names and stable callable identities.
The lasting cost is one preferred-name field, one shared resolver, and reserved compatibility spellings.
No new dependency, intent classifier, vocabulary constant, or benchmark framework is needed.

## Proof and delivery

Use isolated fixture state to prove both names through native MCP, Cloud-mode MCP, REST, and CLI.
Check discovery, guarded edits, authorization, restricted disclosure, Cloud exclusions, tier policy, and cross-spelling mutation replay.
Preserve activation inputs, task refresh, map/profile references, read continuation, and note summary/topics.
Measure complete wire bytes with the existing script and retain current budgets.
Reuse existing public-interface workflow infrastructure and record tool choices, retries, calls, and outcomes without inferring general model-quality gains.

One independent review covers the integrated batch and names C4, C6, security, and product-versus-proof weight.
Run affected checks during development and the required full corpus at delivery.
Source integration waits for the current release hold to lift and avoids any context-evaluation freeze window.
The release owner handles the later ordinary release, installation, and coherent package rollback.
External connector acceptance remains explicitly pending until observed through its supported refresh route.
