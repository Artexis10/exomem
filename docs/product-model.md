# Exomem product model

Exomem is durable memory with sources, proof, history, and review for MCP-capable agents.

Built-in AI memory and Exomem should be complementary:

| Layer | Use it for | Do not use it for |
| --- | --- | --- |
| Built-in AI memory | Preferences, working rules, tone, routing instructions, small stable facts about how to help the user | Project history, research conclusions, legal/warranty records, source material, proof, supersession history |
| Exomem | Durable governed knowledge: sources, proof/evidence, decisions, records, compiled conclusions, review queues, and history | Ephemeral chat state or private assistant implementation preferences |

## Simple actions

Users and agents should think in verbs first. Exomem's internal page types still enforce governance, but normal prompts should not require the user to choose them.

| User action | What it means | Backed by |
| --- | --- | --- |
| Save | Keep raw material or a durable conclusion | `capture_source` for raw material; `remember` for compiled conclusions; `preserve_evidence` for proof |
| Adopt/import | Understand an existing vault safely before changing anything | `adopt_vault(mode="scan-only")`, then explicit copy/manifest/compile modes |
| Ask | Retrieve what the vault already knows with citations | `ask_memory`, then `read_memory`; `ask_memory(deep=true)` for bounded context |
| Prove | Show or preserve proof for a claim, case, warranty, dispute, record, or receipt | `preserve_evidence`, `transfer_artifact`, `review_memory(mode="provenance")` |
| Review | Surface stale, unprocessed, broken, or contradictory areas | `review_memory` |
| Update | Correct, edit, or supersede knowledge while preserving history | `edit_memory`, `replace_memory`, `maintain_memory` |
| Connect | Link related notes, entities, decisions, and sources | `connect_memory` |

## Existing vaults

An existing vault is not a migration problem by default. Exomem treats non-KB folders as read-only input and creates a governed `Knowledge Base/` layer beside them.

The adoption modes are explicit:

| Mode | Writes? | Purpose |
| --- | --- | --- |
| `scan-only` | No | Report structure, likely packs, governed vs read-only areas, and next actions |
| `save-manifest` | Yes, under `Knowledge Base/_Adoption/` only | Save the scan as a durable onboarding artifact |
| `copy-as-sources` | Yes, under `Knowledge Base/Sources/Imported/` only | Copy selected legacy text files with original path and SHA-256 provenance |
| `compile-selected` | Yes, under `Knowledge Base/Sources/Imported/` when legacy files need source copies | Copy selected legacy text files when needed, then return a reviewable compile plan; compiled notes are written later through `remember` |

There is no rewrite-in-place onboarding path in the normal product flow. `compile-selected` is a planning step, not auto-migration: it prepares governed sources and a note scaffold so the user or agent can deliberately compile with `remember`.

## Sources versus evidence

A Source is raw input captured so future notes can cite it. An article, meeting transcript, pasted conversation, or research excerpt is usually a Source.

Evidence is proof-bound. A receipt saved for a warranty case, a legal letter, an insurance document, a medical record, or a screenshot preserved for a claim belongs in Evidence. Evidence does not mean "important source"; it means "used as proof for a case, claim, record, or decision."

## Compared with note graph tools

A searchable graph of Markdown notes is useful, but it is not the whole product. Exomem adds governed writes, source-to-note provenance, evidence/proof handling, explicit supersession, review queues, multimodal extraction, and one command registry that exposes the same behavior through MCP, CLI, REST, and OpenAPI.
