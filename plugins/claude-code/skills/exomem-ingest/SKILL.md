---
name: exomem-ingest
description: Ingest an external article, PDF, pasted note, dataset, image, audio, or video into Exomem while preserving raw evidence before compiling conclusions.
version: 0.1.0
---

# exomem-ingest

## Purpose
Turn external artifacts into preserved evidence plus useful compiled memory.

## When to use
Use when the user asks to ingest, add, import, process, or preserve an external source or artifact.

## Workflow
1. Identify the artifact type: text, article, PDF, dataset, image, audio, video, or mixed media.
2. Preserve the raw source first with `capture_source`, `preserve_evidence`, or `transfer_artifact`.
3. Media processing is automatic; use `process_media` for immediate reconciliation, actionable status, or retry, then inspect via `read_media`, extracted text/OCR/transcripts, or media-aware `ask_memory`.
4. If the source is worth distilling, use `compile_source` for planning and `remember` for the compiled note.
5. Link related prior notes with `connect_memory(operation="suggest-links")`.

<!-- exomem-semantic-authoring:v4 sha256:837b03b15c3d83f6c6eeb50771f4eaa04e4beaaae0f7d54be249be40ce7685f7 -->
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
- Portable categories: Choose exactly one primary category: prefer a meaningful epistemic or operational role and put the domain in tags, but if the role would only be a generic fact, finding, or observation and the domain is the durable lens, use a domain category instead. The category vocabulary is open: these core keys are a shared starting point, not a closed list. When no core key is a good primary fit, author a new meaningful category rather than forcing an ill-fitting one. Use exactly one primary category; kind is the governed form, tags are secondary facets, and relations are typed edges. The rich form's category defaults to its kind, so `category: decision` is redundant when the kind is Decision. Create multiple distinct semantic observations and typed relations when the source genuinely supports them, but never multiply units or relations to satisfy a quota and never duplicate the same fact. Core keys are `action`, `assumption`, `code`, `config`, `constraint`, `decision`, `design`, `fact`, `finding`, `insight`, `preference`, `problem`, `question`, `requirement`, `risk`, `technique`. Core aliases are `actions` → `action`, `assumptions` → `assumption`, `configs` → `config`, `configuration` → `config`, `configurations` → `config`, `constraints` → `constraint`, `decisions` → `decision`, `designs` → `design`, `facts` → `fact`, `findings` → `finding`, `insights` → `insight`, `open_question` → `question`, `open_questions` → `question`, `preferences` → `preference`, `problems` → `problem`, `questions` → `question`, `requirements` → `requirement`, `risks` → `risk`, `techniques` → `technique`. Role example: `- [decision] Relocate to a coastal city next spring #life ^relocation`. Domain example: `- [nutrition] Evening protein improves adherence #experiment ^evening-protein`. Breadth example (life, finance, legal/travel, and career alongside one retained code line):

```markdown
- [constraint] Keep retry windows bounded #code ^retry-windows
- [risk] Variable-rate mortgage payments could spike #finance ^mortgage-rate-risk
- [question] Does the destination require a visa before travel #legal #travel ^visa-requirement
- [career] Weigh a sabbatical before the next promotion cycle #growth ^sabbatical-timing
```

Rich example:

```markdown
## Decision
- id: commit-to-morning-training
- tags: health
- relations: supports: [[Knowledge Base/Notes/Health/Morning training]]

Commit to a fixed 6am training block on weekdays so consistency compounds and health stays the durable lens for this decision.
```

## Output contract
Report the stored source/evidence path, any compiled note path, and what remains unprocessed.

## Save rules
Raw artifacts stay in `Sources/` or `Evidence/`. Distilled conclusions go in typed compiled notes with source links.

## Mistakes to avoid
Do not skip raw preservation. Do not paste large raw artifacts into compiled notes. Do not claim media content was inspected unless an extraction, frame, transcript, OCR, or artifact view was actually used.
