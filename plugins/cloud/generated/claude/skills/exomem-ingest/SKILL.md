---
name: exomem-ingest
description: Ingest an external article, PDF, pasted note, dataset, image, audio, or video into Exomem while preserving raw evidence before compiling conclusions.
metadata:
  skill_contract: e97d49b3db8be67154cf8daf230013c8679c6b4832764dda4d6e05526ad68859
  version: "0.1.0"
---

# exomem-ingest

## Purpose
Turn external artifacts into preserved evidence plus useful compiled memory.

## When to use
Use when the user asks to ingest, add, import, process, or preserve an external source or artifact.

## Portable operating rules

Before the first operation, inspect the exposed bootstrap schema. If it lacks `skill_contract`, obtain `bootstrap(operation={'kind': 'instructions', 'profile': 'compact'})` directly. Otherwise obtain `bootstrap(operation={"kind": "instructions", "profile": "session", "skill_contract": <metadata.skill_contract>})` if current policy or capabilities are missing; honor `engagement.envelope` and `available_product_tools`. Reuse returned state until policy, connection, adapter, or returned vault configuration/registry state changes. If the server rejects the session profile or argument, obtain `bootstrap(operation={'kind': 'instructions', 'profile': 'compact'})` once. `session_profile_unavailable` marks a compact fallback; follow its returned rules. Use the harness's supported discovery mechanism and load only the tools needed now. If the applicable local procedure cannot be read, obtain the portable compact contract; do not improvise a write.

Do not invoke tools absent from `available_product_tools`; use the live capability list even when a bundled workflow mentions a withheld operation.

Use `bootstrap/change_settings` set/clear only for an explicit user request to change saved engagement.
Recall, capture, installation and missing hooks use the existing preference;
none authorizes changing it. Inspect first when the user requests a change.

Sources/Evidence are immutable, and content outside the managed Knowledge Base
is read-only. Before a compiled write: reuse current relevant search/read results,
check for duplicates, and include known source references and reviewed connections
in the first write. Use `read_connections(operation={'kind': 'suggest-links'})` when useful
connections are still unknown, not to recheck links already established in context.
Honor the live confirmation ceiling;
a workflow or standing capture preference does not grant restructure authority.

Inspect mutation results before reporting success. On `success: false`, follow
the structured error. For warming, busy, pending, or
`MUTATION_COMMITTED_ACKNOWLEDGEMENT_UNCERTAIN`, preserve the same mutation identity
and unchanged payload; wait/reconcile/retry only as instructed, never with a new
identity after an uncertain commit.

## Workflow
1. Identify the artifact type: text, article, PDF, dataset, image, audio, video, or mixed media.
2. Preserve raw material with `capture_source`: `content` for authored text, `files` for original attachments. Original bytes remain intact.
   Never retype an original file. If its handle is unavailable, follow the Source upload route that the exposed tools serve.
   Use `preserve_evidence` or `preserve_artifacts` for proof-bearing material.
   Pass `source_kind` and `domain` on a `capture_source` call — both are open
   vocabularies, so name the label that fits even if it is unfamiliar. A kind is
   required, and there is no catch-all.
   For a whole folder or collection of existing files, use `import_memory` instead of
   capturing one by one. A folder outside the vault is staged first: run
   `exomem import <folder>` and start on the path it prints, because `start` refuses an
   absolute path. (Exomem Cloud takes `exomem cloud import <folder>` instead.) Then set `operation.kind` to `start`
   with a `collection` name, `select` folders (with a `source_kind` and `domain` per folder rule where known),
   `plan`, `apply`, then `finish`. Repeat `plan` or `apply` with the returned `continuation`
   until none returns, and report the `coverage` from `finish`, including what was refused,
   what is still pending and how many notes were created. For a large folder, a full disk
   or a staged copy to discard, follow [importing](references/importing.md).
   A folder of the person's own notes can also become notes. Ask which folders hold them;
   never guess from file names. Give that folder the rule
   `{path, adopt_as: "note", note_type, project?, domain?}`, with a `note_type` that
   `read_schema(operation={'kind': 'inspect', 'subject': 'note-types'})` lists as compiled, and
   `project` or `domain` where that type needs one. Each Markdown file in the folder keeps
   its Source and also gets a draft note that cites it and keeps its original body. Other
   files stay Sources. A draft is not live and does not clear its Source citation backlog. Promote it
   after review with the usual writers; a live citation still does not prove semantic coverage.
3. Inspect available original media or extracted content; record the scope actually read.
   Media processing is automatic. Use `read_media/change_media` for reconciliation, status, retry or reprocess,
   and `read_media` or the exposed source-read route for inspection. Pending extraction
   does not prevent using an available original. Extraction completion says nothing
   about semantic compilation. Respect source-version guards on dependent writes.
4. When preparing compiled knowledge, request `compile_guidance=true` or use `prepare_note`; the optional scaffold is available.
   A proposal prepares one note, not a source-wide semantic plan. Its opening excerpt
   covers only the opening; use the returned read route for the full available source.
   Adjacent pages and `suggested_connections` are unreviewed candidates, never accepted edges.
5. For selected compilation, follow the [source compilation pass](references/writing.md#source-compilation-pass); one inventory check closes the work.
   Raw capture may end after custody when compilation is outside the request and live policy.

## Vocabulary

Read [vocabulary](references/vocabulary.md) before changing a registry.
If the vocabulary reference is missing, inspect the exposed bootstrap schema.
Use `bootstrap(operation={'kind': 'instructions', 'profile': 'compact', 'section': 'vocabulary'})`, or
`bootstrap(operation={'kind': 'instructions', 'profile': 'full'})` when `section` is unavailable. Follow the returned
inspect routes and `save` contract. Reuse or alias before proposing justified
new meaning; save with the current hash and `why`. Report saved versus queued
and the returned restore route. Use only exposed operations.

## Relation governance
Resolve typed meaning with `resolve-relation` before authoring. Reuse a specific
truthful match. Keep `relates_to` for a real generic link, with a stated reason when it
joins an entity and a specific relation fits (`GENERIC_RELATION_NEEDS_REASON` lists
them), or choose no edge. Durable recurring
meaning is proposal-first through `propose-relation` and hash-guarded
`save-relations`; corrections use a new canonical key and deprecate the old one.

## Output contract
Report preserved originals, compiled outputs and the scope actually inspected.
Distinguish custody, extraction, citation linkage and the agent's semantic coverage judgment.
Name unread, unavailable or uncompiled material without claiming that counts prove completion.

## Save rules
Raw artifacts stay in `Sources/` or `Evidence/`. Distilled conclusions go in typed compiled notes with source links.

## Mistakes to avoid
Do not skip raw preservation. Do not paste large raw artifacts into compiled notes. Do not claim media content was inspected unless an extraction, frame, transcript, OCR, or artifact view was actually used.

<!-- exomem-semantic-authoring:v10 sha256:8329b80b17d7dbdbc975c0921795e0f5a3265d93bcdc23f3963ea57842a7ba95 -->
## Semantic authoring contract

Choose homes by future use and accumulated content, not mentions or broad scope. Keep coherent long notes; reuse or create a home for independent material.

Live compiled creates, replacements and transitions to live need a valid nonempty semantic unit; pending pages may remain unit-free. Either compact or rich form satisfies the minimum; compact is preferred, and a valid rich unit does not need a duplicate compact restatement.

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
- Rich: write `## <Governed Kind>` with optional leading metadata `- category: <open category>`, `- id: <stable-id>`, `- tags: <comma-separated tags>`, `- context: <context>`, `- relations: <relation-type>: [[Target]]`, `- verdict: <epistemic outcome>`, `- check_by: <ISO revisit date>`. Metadata rows are optional and leading; the canonical writer emits category, id, tags, context, relations, verdict and check_by; category defaults to the governed kind when omitted. Accepted metadata order is flexible while rows remain leading. After optional leading metadata, add a blank line and a substantive Markdown body. Typed unit relations require the rich form. A `#anchor` target addresses a unit.
- Epistemic corrections: Inspect affected claims and evidence. Distinguish attributed accounts from established facts. Correct suitable existing units with guarded writers, justified verdicts and exact links; verify current state. Refutation alone changes neither lifecycle nor rank. Writers retain prior interpretations; request bootstrap with profile=full for history guidance. Keep an anchored unit's identity when its wording changes. Preserve a false historical proposition and author its verdict; add a distinct corrective unit when useful. Refutation and contradiction do not imply supersession or reduce rank. The semantic writer retains prior interpretations atomically. Read revision_history with read_memory(include_history=true), optionally for unit_ref, using history_limit and history_continuation. When it carries lookup=stale, repeat the same call shortly instead of reading absence or paging on. Existing history remains the page's operation log. Entities and legacy pages use their existing edit owner; Records use guarded row corrections and history. Corrections need no dated child, special user nudge, or confirmation beyond the active engagement policy.
- Rich boundary: A heading at level N owns content until the next non-fenced heading at level N or shallower; deeper headings remain in its body. `empty_rich_unit` means a recognized rich heading has no substantive body; Add substantive body content or remove the empty recognized heading.
- Exact applicability: obtain `bootstrap(operation={'kind': 'instructions', 'profile': 'compact', 'section': 'authoring'})` and read `semantic_authoring.minimum_semantic_unit` for current structural applicability and the status-class rule. If the exposed bootstrap schema lacks `section`, use `bootstrap(operation={'kind': 'instructions', 'profile': 'full'})` instead; released profiles reject section arguments. Apply `compiled_intent(after_state) = canonical_compiled_destination(path) OR the normalized type's note-type role is compiled`. Shipped compiled types and their default folders: `research-note` in `Notes/Research`, `insight` in `Notes/Insights`, `failure` in `Notes/Failures`, `pattern` in `Notes/Patterns`, `experiment` in `Notes/Experiments`, `production-log` in `Notes/Productions`. More may be registered; `read_schema(operation={'kind': 'inspect', 'subject': 'note-types'})` lists the live set. Reject missing, invalid, or mismatched compiled frontmatter before evaluating the minimum-unit predicate. A page in a type folder needs a compiled type; a compiled type may sit in any Notes folder, and a compiled page outside Notes is refused. Folders are defaults. `create_note` files research notes in their `project` home. Other compiled notes use the first `topics` entry with a home. `read_schema(operation={'kind': 'inspect', 'subject': 'homes'})` lists homes. The draft's `placement` gives the folder, basis and call to make a topic a home. The minimum predicate applies when the path and normalized compiled type structurally match; the result is writable managed Markdown in the governed subtree; the result is outside Sources, Evidence, and trash; no activation exclusion applies; the admitted effective status class is live. Check live creates, replacements and transitions to live; pending compiled notes may remain unit-free until activation.
- Existing active pages: A post-activation compliant page cannot lose its final valid semantic unit.
- Exempt content: arbitrary non-compiled Markdown, dataset cards, Evidence artifacts, hubs, indexes, logs, non-Markdown files, schema and admin artifacts, snapshots, Sources, templates, trash.
- Routes: use `create_note` for a new compiled note, `supersede_note` for a replacement, `change_observations` for one unit, and `edit_note` for a small edit or activation. Tier 2 change_files create, overwrite, and append receive the same semantic precommit contract on the complete resulting compiled Markdown; prefer create_note or supersede_note when their typed route fits.
- Findings: `missing_semantic_unit` means an applicable live-class compiled result has no valid non-empty unit; `empty_rich_unit` means a recognized rich heading has no substantive body. Add substantive body content or remove the empty recognized heading.
- Compact remediation: Add `## Observations` and `- [operating constraint] Keep retries bounded #reliability`.
- Rich remediation: Alternatively add `## Decision`, a blank line, and a substantive body.
- Semantic-unit coverage and relation-review disposition are independent obligations.
- Validation review: Validation evaluates the proposed result without saving content or review state; the request includes relation assessment. Inspect the current page and candidate evidence. If no truthful relation applies, revalidate with relation_disposition="reviewed_none" and a bounded evidence-based relation_review_reason. Missing edges prove nothing; preserve typed relations and metadata. Report insufficient evidence or unresolved material choices. Commit needs separate write authority and returned guards.
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
