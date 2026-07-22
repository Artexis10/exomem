---
name: exomem-continue
description: Resume prior project or session context from Exomem when the user wants to continue, pick something back up, or remember what was happening on a topic.
version: 0.1.0
---

# exomem-continue

## Purpose
Resume prior context without guessing from chat history alone.

## When to use
Use when the user asks to continue, resume, pick up a project, recover prior state, or asks what they were doing on a topic.

## Workflow
1. Search first with `ask_memory(detail="compact", rerank=false)`, preferring active compiled notes and project-relevant terms from the prompt.
2. Use `ask_memory(deep=true)` when several hits matter or the state needs synthesis.
3. Read only the top relevant pages with `read_memory` when excerpts are not enough.
4. Prefer recent active compiled notes, then linked sources/evidence when provenance is needed.
5. Summarize current state, decisions already made, blockers, and likely next actions.

<!-- exomem-semantic-authoring:v2 sha256:b5fd73be05d4d07cf37c941625f8fe5e09a1ae2bc6f9e2db43392583e9eb2d5f -->
## Semantic authoring contract

Every new, replaced, or activated active compiled note needs at least one valid, non-empty semantic unit. Either compact or rich form satisfies the minimum; compact is preferred, and a valid rich unit does not need a duplicate compact restatement.

Semantic roles:

- Category: One primary open-vocabulary label describes what a unit is about; rich category defaults to its governed kind unless explicitly overridden.
- Tag: Zero or more optional secondary retrieval labels refine lookup and never replace category or determine kind.
- Kind: The governed semantic form: compact units always use `observation`; rich units use their recognized heading kind.

Compact grammar: `- [category] content #tags (context) ^anchor`. Parse valid compact observations anywhere outside fenced code blocks. Exomem writers use `-` under the canonical `## Observations` section. Parser bullet markers are `-`, `*`, `+`; the canonical marker is `-`. Parse from the end by taking anchor, then context, then trailing tags; the authored display order remains tags, context, anchor. Category uses open vocabulary.

- Compact category: the unit's one primary open-vocabulary subject or domain label. After trimming, use 1-64 Unicode code points; begin with a Unicode letter; then use only Unicode letters or digits, spaces, `_`, or `-`. Apply Unicode NFKC and casefold, then collapse runs of spaces, `_`, and `-` to one `_`. Registry alias resolution is separate from authored canonicalization; an unseen valid category needs no registry write.
- Compact content: the unit's substantive observation. Use non-empty content that remains on one Markdown line. Escaped parentheses, embedded hashes, and non-trailing tag-like text remain content.
- Compact tags: zero or more optional secondary retrieval labels; tags do not replace the primary category or governed kind. Write `#slug`. Use 1-64 Unicode letters or digits, `_`, `-`, or `/`; begin with a letter or digit; do not use empty path segments or a trailing `/`. Use one contiguous trailing run after content and before optional context and anchor.
- Compact context: one optional authored qualifier for the observation. Write `(<context>)`. Use one balanced, unescaped parenthesized suffix preceded by whitespace.
- Compact anchor: one optional stable authored unit identifier. Write `^anchor`. Use 1-64 ASCII letters, digits, or hyphens and begin and end alphanumeric. Place it at the end of the line.
- Compact exclusions: observation-shaped rows inside fenced code blocks; task labels `[ ]`, `[x]`, `[X]`, and `[-]`; reserved or punctuation-bearing bracket labels outside category grammar. Compact units do not carry typed unit relations; use a canonical note-level relation or the rich form.
- Rich: write `## <Governed Kind>` with optional leading metadata `- category: <open category>`, `- id: <stable-id>`, `- tags: <comma-separated tags>`, `- context: <context>`, `- relations: <relation-type>: [[Target]]`. Metadata rows are optional and leading; the canonical writer emits category, id, tags, context, then relations; category defaults to the governed kind when omitted. Accepted metadata order is flexible while rows remain leading. After optional leading metadata, add a blank line and a substantive Markdown body. Typed unit relations require the rich form.
- Rich boundary: A heading at level N owns content until the next non-fenced heading at level N or shallower; deeper headings remain in its body. `empty_rich_unit` means a recognized rich heading has no substantive body; Add substantive body content or remove the empty recognized heading.
- Exact applicability: `compiled_intent(after_state) = canonical_compiled_destination(path) OR normalized_type in COMPILED_TYPES`. `COMPILED_TYPES` contains exactly `experiment`, `failure`, `insight`, `pattern`, `production-log`, `research-note`, with canonical destinations `experiment` → `Notes/Experiments`, `failure` → `Notes/Failures`, `insight` → `Notes/Insights`, `pattern` → `Notes/Patterns`, `production-log` → `Notes/Productions`, `research-note` → `Notes/Research`. Reject missing, invalid, or mismatched compiled frontmatter before evaluating the minimum-unit predicate. The minimum predicate applies when the path and normalized compiled type structurally match; the result is writable managed Markdown in the governed subtree; the result is outside Sources, Evidence, and trash; no activation exclusion applies; the resolved lifecycle is active. Inactive lifecycle values are `archived`, `draft`, `dropped`, `planned`, `superseded`. Check new active creates, replacements, and inactive-to-active transitions; inactive drafts may remain unit-free until activation.
- Existing active pages: A post-activation compliant page cannot lose its final valid semantic unit.
- Exempt content: arbitrary non-compiled Markdown, dataset cards, Evidence artifacts, hubs, indexes, logs, non-Markdown files, schema and admin artifacts, snapshots, Sources, templates, trash.
- Routes: use `remember` for a new compiled note, `replace_memory` for a replacement, `observe_memory` for one unit, and `edit_memory` for a small edit or activation. Tier 2 manage_memory_file create, overwrite, and append receive the same semantic precommit contract on the complete resulting compiled Markdown; prefer remember or replace_memory when their typed route fits.
- Findings: `missing_semantic_unit` means an applicable active compiled result has no valid non-empty unit; `empty_rich_unit` means a recognized rich heading has no substantive body. Add substantive body content or remove the empty recognized heading.
- Compact remediation: Add `## Observations` and `- [operating constraint] Keep retries bounded #reliability`.
- Rich remediation: Alternatively add `## Decision`, a blank line, and a substantive body.
- Semantic-unit coverage and relation-review disposition are independent obligations.

## Output contract
Return a compact continuation brief with cited Exomem pages and a short next-action list. Say when the search missed or was thin.

## Save rules
Save only if the resumed work produces a new durable decision, solved problem, failure, pattern, or research finding. Use `remember`, `edit_memory`, or `replace_memory` as appropriate.

## Mistakes to avoid
Do not invent prior context from memory. Do not treat a scoped miss as absence. Do not dump long note bodies when a state brief is enough.
