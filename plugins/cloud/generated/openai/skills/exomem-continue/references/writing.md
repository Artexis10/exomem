# Writing and connecting compiled knowledge

Apply the live engagement envelope from SKILL.md before these workflows.
A batch or capture waiver never raises an action-class ceiling: entity merge,
supersession, and restructure execution still require the applicable confirmation;
a new entity after resolve-before-create follows `proactive_capture`.

## Entity resolution

Resolve entity candidates against the active entity registry and selected knowledge packs.
  Call `connect_memory(operation="resolve-entity", name=...)` first. If one active page
  matches, use `edit_note` for a small stable-fact correction or the canonical
  relation workflow for a new connection. If none matches, use
  `connect_memory(operation="create-entity")` only when the identity is stable,
  recurring, central to the conclusion, and useful beyond the current source.
  Pass known meaning as `relations=[{"kind": ..., "target": ...}]`;
  `connections` always writes the generic `relates_to`.
  An unregistered-type finding supplies `proposal` and `expected_hash`; save
  those exact values through the governed
  `schema_memory(operation="save-entity-types")` leaf with `why`, never by
  editing frontmatter around the registry rule.
  A single incidental mention, unresolved identity, or transient participant
  stays in source/note context.

### Names in another script

A turn written in one script reaches a page only through a name in that script:
a user who writes in Japanese says the katakana or kanji name, not the Latin
title. So when you write an entity's name in a script other than the one the
user used for it (a Latin title for a name they wrote in Japanese, or the
reverse), add a native-script alias: the user's own spelling.

- At capture: `connect_memory(operation="create-entity", aliases=[...])`.
- On an existing page: `edit_note` with `{"kind": "patch_frontmatter",
  "field": "aliases", "value": [...current, "<the user's spelling>"],
  "expected_hash": ...}` and a `why`; the list replaces the field, so keep the
  current aliases.

Use only a spelling the user wrote or the source states; never a
transliteration or translation you made up. An alias is a name the page
answers to, so one any other page already answers to (a note's title included)
is refused, at capture and on edit.

## Vocabulary consideration

Before saving durable knowledge, consider reuse, enrich, propose-new, generic, no-edge, or defer from the material and bounded review evidence. Resolve candidate entity
types and relation meanings before proposing a new one. Generic and no-edge are
truthful outcomes when no useful distinction is supported; there is no quota for
types or edges. A resolver returns definitions and evidence; its null selection
or proposal fields leave the judgment to the active agent. When a useful recurring
identity has no fitting type, author its definition and rationale, preserve the
existing extension definitions, and use `schema_memory(operation="save-entity-types")`
with the reviewed registry document and current extension hash. An empty match
does not require squeezing the identity into a core type or abandoning the type
proposal. Record the reviewed fingerprint, family, registry hashes, target
versions, outcome, rationale, and canonical choice through vocabulary triage.
That record is consideration, not execution or permission: v1 canonical writers
retain their existing confirmation rules. Do not treat a proposal, review decision,
or generic connection as authority to create an entity, type, or edge.

## Agent write loop

Use this loop whenever a durable conclusion should enter Exomem:

1. Partition durable candidates by retrieval question, subject/domain, episode,
   and epistemic role before choosing destinations. Reuse current relevant
   recall; call `search_memory` only for missing prior notes or sources.
2. Assess scope/title fit for each candidate home. An open or similar page has
   no automatic priority. Read selected exact units/pages only as needed; keep
   coherent details together and route independently reusable clusters separately.
   Preserve durable hypotheses with speaker/source attribution and uncertainty.
3. Identify the provenance: which governed `Sources/` or `Evidence/` pages this
   conclusion draws from. Those become `sources:` on the write call. Capture
   external material first: a URL, connector/message ID, remote file ID, working
   script, excerpt, or derivative summary is not itself a source citation. Use
   `capture_source` or the Evidence lane for the original, then cite the returned
   path or stable ref. If the original cannot be recovered, remove the unsupported
   claim explicitly; never recreate an "original" from the derivative. If the
   conclusion came from live work with nothing external captured, `sources: []`
   is honest and valid.
4. Draft the typed page at the right layer: `capture_source` for raw source, `create_note` for a compiled conclusion, `connect_memory` for entity/link work, `observe_memory` or `edit_note` for guarded corrections, `supersede_note` for supersession.
5. Reuse meaningful links established by the current search/read context. When
   connections remain unknown, use `connect_memory(operation="suggest-links")`
   on the draft; use `suggest-relations` when direction remains unresolved. Accept only links
   that genuinely clarify provenance or context, and write accepted note-level
   edges under `## Relations` as `- relation_type [[Target]]`. Carry accepted
   links into the *first* write; do not defer them to a follow-up `edit_note`.
6. Write, then inspect the result. The default committed response carries
   `warnings` (when the write warned) and an optional `structure_suggestion`.
   The fuller structural checklist — `write_feedback` with `sources.cited`,
   `links.body_wikilinks`, and `relations.relation_debt` — lives under
   `diagnostics` and needs `response_detail="full"`. Ask for it when provenance
   or connectivity is in doubt; if all three counts are zero and that is not
   honest, fix it before reporting. Corpus-aware `suggestions` are **off by
   default** because they cost a whole retrieval pass on the write path; pass
   `suggestions=true` (with `response_detail="full"`) when you want them.
   `write_feedback.suggestions.computed` tells you whether they ran.
7. If a near-duplicate warning fires, prefer `edit_note` or `supersede_note` over a parallel page. If suggestions are useful, add them with a follow-up `edit_note`.
8. If the write returned a `structure_suggestion`, handle it as below.
9. Report one line: `Saved -> <path>`.

## Closing a multi-step workflow

Group work by dependencies, not by one tool call per reasoning turn. Read
independent known pages together when the client supports parallel calls. Preserve
several artifacts with one `preserve_artifacts` call, then use the discovered
`process_media(paths=[...])` selector when available. Its ordered item results
acknowledge durable work; extraction finishes asynchronously. Do not claim to have
read extracted content before public status/read proves it exists.

Author related observations and accepted connections in the initial note where
possible. On an existing page, `edit_note` with `operation.kind="batch_replace"`
can combine known string changes into one guarded mutation. It is not an atomic
multi-page transaction. Keep conflicting canonical edits ordered; run independent
reads or artifact work concurrently only within the active client's capabilities.
When a compiled note cites a media sidecar that extraction can still change,
complete the relevant extraction before validating that dependent write. Do
source-independent tracker, background-note or unrelated capture work while it
runs. Never bypass a source-version guard or reuse a stale staged backlink.

Inspect each terminal and handle actionable warnings, but do not reread the page
after every committed edit or poll until every projection is current. Chain a
returned `after_hash` into the next supported `expected_hash`; use returned
`unit_ref` for exact semantic edits. Finish with a bounded check of the requested
state and citations. If the next operation explicitly needs current graph edges,
honor its freshness refusal; optional graph lag does not invalidate a successful
ordinary lookup.

**When a write says the page has outgrown its scope.** A compiled write may return
`structure_suggestion` — the runtime's observation that recurring durable material
on that page now sits outside what the page says it is about. It is advice, not an
instruction, and nothing has been moved.

Normally surface a `strong` one, in the user's own words: name the threads that have
grown up, say it looks like its own project or note now, and offer to organise it.
Never recite the reason codes or say "scope divergence" — that is internal
vocabulary. Prefer routing into an existing suitable destination over inventing a
new one, so search before proposing. Ask before restructuring anything unless the
user has already delegated curation. Do not raise the same recommendation twice in
one conversation. On a `moderate` one, use judgement: if mentioning it would be
bureaucracy rather than help, stay quiet.

**Comprehensive coverage, minimal expression.** Capturing at the landing is about
*timing*, not *volume* — it never means keep less. Minimality is a property of
*expression* — distillation, signal-density, no redundancy — never of *coverage*.
Don't drop context or detail because it "doesn't seem important": importance is
usually only legible in hindsight, and nothing here forces the tradeoff (no
retention decay, hybrid BM25+vector retrieval, append-only `Sources/`, no storage
limit). Default coverage to comprehensive; reserve concision for *how* a note is
written, not *what* it keeps. Torn between keeping a detail and dropping it? Keep
it — preserving raw detail keeps it recoverable; compiled notes should avoid redundant restatement.
Capture more, at the right layer: raw verbatim to `Sources/` liberally; compiled
notes stay distilled in form but never context-pruned.

## Descriptive vs analytical coverage

The KB serves two complementary purposes:

- **Descriptive coverage** — *describe what is.* Architecture hubs
  (`Notes/Research/<project>/<subsystem>-architecture`), point-in-time snapshots,
  concept entities. These let a future planner orient quickly.
- **Analytical coverage** — *extract reusable lessons.* Patterns, insights,
  failure modes, decisions. These compound across projects.

Both are first-class. When orienting a new area, descriptive hubs typically come
first; patterns and insights extract from the descriptive layer as second-order
knowledge.

**Boundary with a code repo.** For a software project the repository is the
source of truth for code, design, and decisions. KB coverage of it is the
cross-session/cross-project layer the repo can't hold — strategy, roadmap,
orientation, hard-won empirical findings — never a condensed changelog or a
restatement of what specs/commits already capture.

## Write discipline

These rules are non-negotiable.

1. **Read-only paths.** Never write to anything outside `Knowledge Base/`. Any
   sibling folders in the vault are inputs only. Compiled notes may **link to**
   them but never modify them.

2. **Sources and Evidence are append-only.** Once a file lands in `Sources/` or
   `Evidence/`, never edit its *content*. Corrections happen by adding a new
   source and superseding the old via a compiled note. Relocating a file *within*
   the same append-only tree (into a themed sub-folder) is allowed via
   `move_file`; crossing the boundary is forbidden.

3. **Propose before writing compiled material.** For `create_note`,
   `connect_memory` entity writes, and `supersede_note` (and any hand-edit of
   `_Schema/` files), show the proposed content (or diff) and wait for
   confirmation. The exception is `capture_source` (raw capture),
   `preserve_evidence` (raw evidence), and read-only recall/review operations.

    **Batch waiver:** you may approve a *scope* of multiple files upfront ("draft
    all Tier 1," "write all four hubs + concepts") rather than each individually.
    Write the batch, then summarise paths + count. The waiver is per-batch.

    **Standing waiver:** phrasing like "just write it," recorded preferences, or a
    stepping-stone reached in an autonomous session — draft, write, and report
    rather than pre-approve.

4. **Frontmatter is mandatory.** Every file written under `Knowledge Base/` must
   carry frontmatter conforming to `frontmatter.md`. Exceptions:
   `index.md`, `log.md`, and sub-folder `index.md` files. Non-markdown binaries
   carry frontmatter in a sidecar `.md` if one is needed.

5. **Compiled pages carry their connections.** A compiled note names in
   `sources:` every `Sources/` or `Evidence/` page it draws from, in the write
   call itself — not as a follow-up edit. Each entry makes the writer append this
   note's wikilink to that source's `ingested_into:`, which is what maintains the
   source→note graph; skip it and the source stays in the unprocessed backlog
   permanently even though you compiled it. A conclusion that builds on prior
   conclusions links them inline, and the ones carrying direction go under
   `## Relations` as typed edges (see § Linking discipline).

    **Honest zero is legitimate.** There is no minimum edge count and no quota. A
    note with no source and no prior art is a complete, valid note — write it and
    move on. This rule forbids *omitting a link you know about*, never *failing to
    find one*. Manufacturing an edge to look connected is worse than no edge.

6. **No `confidence` floats.** Trust is conveyed through citations and link
   counts, not numbers.

7. **Supersession over deletion.** When information is replaced, mark the old page
   `superseded`, link to the new one, and never delete. See
   `supersession.md`.

8. **Always update `index.md` and `log.md`.** Every write that creates or moves a
   page updates the top-level `index.md` (counts + Recent activity, cap-50),
   appends to `log.md`, refreshes the relevant sub-folder `index.md` counts, and
   appends the new artifact's wikilink to the originating source's `ingested_into:`
   frontmatter — the back-reference rule 5 depends on. Count tokens are
   auto-refreshed by the writer; hand-curated descriptions are preserved.

For the full read-only / writeable path map see `write-scope.md`.

## Page types

Eight page types under `Knowledge Base/`, each with a required frontmatter shape,
naming rule, and location. **Full per-type spec: `page-types.md`;
frontmatter: `frontmatter.md`.** The behaviorally-load-bearing
distinctions:

- **source** — raw input, `Sources/<Kind>/[<Domain>/]` projected from its
  `source_type` and optional `domain`. Two flavors (same frontmatter):
  *transcript* (content as-is) and *origination record* (a session-reasoning
  capture, `ingested_into:` listing what it produced).
- **research-note** — `Notes/Research/<scope>/`. Informal subtypes: *standard*;
  *hub* (orients a subsystem, links out; refresh on major ships); *snapshot*
  (point-in-time, drift OK, say "snapshot" in body).
- **insight** — cross-cutting lesson, `Notes/Insights/`.
- **failure** — failure mode, `Notes/Failures/`.
- **pattern** — reusable pattern, `Notes/Patterns/`. Use `projects:` (plural) when
  it spans projects.
- **experiment** — hypothesis + protocol + primary data, `Notes/Experiments/<domain>/`.
- **production-log** — creative artifact + production knowledge, `Notes/Productions/<medium>/`.
- **entity** — typed node under the folder resolved by the stable entity registry
  (People / Organizations / Concepts / Libraries / Decisions).

### Research scope keys

The `project` field on a research note is a slug-shaped key registered in
`_Schema/project-keys.yaml`. It's an **open set**, not a closed enum — pick the
most-specific scope first. A typical starter set:

- Products / projects: `project-alpha`, `project-beta` — one key per project.
- Domains: `work`, plus your own (`research`, `ops`, …).
- Cross-cutting: `personal` — anything not tied to a specific project or domain.

For **patterns** that apply across multiple projects, use `projects:` (plural
list) instead of `project:` (singular), e.g. `projects: [project-alpha, project-beta]`.

**Auto-registration of new project keys.** The `create_note`, `supersede_note`,
`edit_note` (frontmatter-patch), and `connect_memory` (decision-entity)
writers auto-append unknown
slug-shaped project keys to `_Schema/project-keys.yaml` and create the matching
`Notes/Research/<Folder>/` directory on first use — no manual YAML edit needed.
Pass `project_category` to bucket the new key (product / activity / domain /
cross-cutting); omitted, it lands `uncategorized`. A **typo guard** rejects new
keys within edit distance ≤2 of an existing key (`wrok` → "Did you mean
'work'?") so the registry stays clean.

### Experiment vs production-log

Easy to confuse (both time-bounded, date-prefixed, with outcomes).
**Experiment** = a hypothesis tested under a protocol with primary data
(`Notes/Experiments/`); ends in confirm/refute/qualify. **Production-log** = a
creative artifact + its production knowledge (`Notes/Productions/`); ends in
engagement metrics + reflection, and the value is the thing made. Quick test: set
out to *learn whether X is true* (experiment) or to *make a thing the world sees*
(production)?

## Workflow: typical add-then-compile session

1. **You paste raw material or ask to log something.**
2. **Skill calls `capture_source` to create a source file.** Classify it on two
   independent axes and let the location follow: `source_kind` is what the
   artifact **is** (`article`, `session`, `research-report`, `invoice-receipt`,
   `field-notebook`, …) and `domain` is what it is **about** (`travel`, `health`,
   `software`, …). Both are **open vocabularies** — name the label you actually
   mean even when Exomem has not seen it, and it registers itself. Add `projects`
   for the work the source serves; a source may serve several, and projects never
   change where it is filed.
   The path is a projection of that metadata, `Sources/<Kind>/[<Domain>/]` —
   e.g. `Sources/Reports/Travel/`, `Sources/Invoices/Equipment/`,
   `Sources/Articles/`. There is no catch-all kind: when no familiar label fits,
   name a new one. Filename: ISO-date + slug. Updates `Sources/index.md`.
   A capture may come back with a `structure_suggestion` of kind
   `source_classification_debt` when the vault holds sources nobody has given a
   kind. Offer once, in the user's own words, to sort them, then reclassify each.
   The display title is stored losslessly as Unicode in frontmatter and the H1.
   When a non-Latin title needs a readable portable filename, pass a separate
   explicit lowercase ASCII `slug`; never treat a transliterated filename as
   the page's title. Existing files are not renamed automatically.
3. **Compile under the live request and engagement policy.** Follow the
   [source compilation pass](#source-compilation-pass)
   for inventory, prior knowledge, central identities and semantic coverage.
   An optional `prepare_note` scaffold prepares one note; adjacent pages are
   unreviewed candidates, and opening excerpts do not cover the full source.
4. **Write reviewed outputs with `create_note`.** Carry original source references
   and grounded accepted relations in the first write. Follow the live confirmation
   ceiling and any existing batch authority.
5. **Inspect results and close the source compilation pass.** Report custody,
   extraction and citation linkage separately from semantic coverage. Counts do
   not establish completeness; unread, unavailable and uncompiled portions remain explicit.

When you approve a scope of multiple files upfront, the workflow collapses to a
single batch write (see Write discipline § 3, batch waiver).

## Source compilation pass

The active conversation agent owns this judgment under the request and live engagement policy.
Selected compilation needs no special user wording to trigger decomposition, central identity resolution or relation review.
Raw capture remains valid without compilation.

1. Read the available full source through its exposed routes; unread or unavailable portions remain explicit.
2. Inventory reusable themes and central durable referents; the inventory reflects meaning, without output quotas.
3. Retrieve prior conclusions, resolve central identities and inspect suitable filing homes; reuse and missing durable identities are distinguished.
4. Compile independently useful outputs into suitable existing or new homes; attribution, observations, hypotheses and measurements remain distinct.
   Keep coherent detail together and cite the preserved originals in the first write.
   Handle durable entities through the live resolve-before-create and vocabulary routes.
5. Review candidate relations against their meaning and evidence; author grounded specific relations, honest generic relations or no edge.
   Resolve registered meaning before authoring, and inspect relation feedback after each write.
   Generic connectivity does not establish typed specificity. Suggestions never establish meaning by themselves.
6. Check the whole inventory once against the committed outputs; report any unread, unavailable or uncompiled portion honestly.
   Custody, extraction and valid citations are separate from semantic coverage.
   One live citing derivative clears the citation backlog only; the server reports semantic compilation as `unassessed`.
   Do not add notes or edges to reach a count or repeat the same source warning indefinitely.

## Linking discipline

Link every compiled page to what it actually connects to — this is Write
discipline rule 5, restated with its mechanics. Linking is what turns the KB from
a junk drawer into a graph. The obligation is to record the connections you know
about, not to reach a count: a page with nothing to link to is finished, and a
fabricated edge is worse than none.

**Canonical wikilink form: full vault-rooted.** Every wikilink resolves cleanly
under the vault root with no prefix guessing:
`[[Knowledge Base/Entities/Concepts/Profile]]`. Link back to the originating
`Sources/` file via the `sources:` frontmatter list (mirrors the source's
`ingested_into:` list).

**Canonical note relation form: one directional edge per bullet under
`## Relations`.** Use a governed lower-snake-case relation and one wikilink:

```markdown
## Relations
- refines [[Knowledge Base/Notes/Insights/Earlier Conclusion]]
- depends_on [[Knowledge Base/Entities/Decisions/Architecture Decision]]
```

Use semantic-block metadata such as
`- relations: evidenced_by: [[Source]], contradicts: [[Earlier Finding]]` when
the edge belongs to a specific claim, finding, or piece of evidence. Ordinary
inline wikilinks remain useful generic `links_to` connections. Never turn a
semantic suggestion into a typed relation without reviewing its meaning.

**Relation labels are governed; categories and tags are not.** Categories are open
vocabulary — invent one whenever it fits. Relation labels come from a registry, so
an unregistered label (`- inspired_by [[X]]`) is *retained and surfaced* but does
not yet count as a graph edge, and does not connect the page. That is deliberate:
the label stays visible as review debt instead of being silently downgraded to a
generic link.

So the vocabulary is extensible, not fixed. Resolve before authoring with
`connect_memory(operation="resolve-relation")`, and reuse a specific truthful
registered relation when one fits. `relates_to` is honest only for a real generic
connection; no edge is better than invented meaning. For a durable recurring
distinction, use `schema_memory(subject="relations", operation="propose-relation")`,
review the complete delta and duplicate evidence, then persist that exact delta
with `operation="save-relations"`, its `expected_hash`, and `why`. Meaning is
immutable: corrections create a new canonical key and deprecate the old one.
Registered relations become traversable edges while clean aliases remain authoring
labels. A registry-marked generic relation establishes connectivity, not typed
specificity; an all-generic advisory asks for meaning review without forcing an edge.

**The writer normalizes on your behalf.** Exomem's writers run every wikilink
through `vault.normalize_wikilink()` before writing — bare names, KB-relative
paths, `.md` suffixes, and stale paths get rewritten to canonical full form. You
can write in any form; the on-disk file lands canonical.

If a wikilink target doesn't exist yet, prefer creating the entity stub via
**connect_memory** rather than leaving a dangling link. Dangling links accumulate
and surface in **review_memory(mode="audit")** as `broken_wikilink`.

When creating an entity that points at a **currently-evolving external artifact**
(a live spec, a code library, a service config), use **pointer-style** — summary
+ canonical-source pointer + connective tissue — not **mirror-style** (versions,
file inventories, command lines copied verbatim). Mirroring guarantees drift.

## When to ask vs. when to proceed

**Ask before:**
- Writing any compiled note, entity, experiment, production-log, supersession, or
  schema update.
- Choosing a page type when intent is ambiguous (research vs. insight vs.
  experiment vs. production-log).
- Choosing a scope under `Notes/Research/` when you haven't named one.
- Choosing a domain under `Notes/Experiments/` or medium under
  `Notes/Productions/` when not stated.
- Choosing whether a research-note is *standard*, *hub*, or *snapshot* — when the
  framing materially affects scope.
- Marking an existing page `superseded`.

**Proceed without asking:**
- Proactive `search_memory` for context (read-only).
- Capturing a clear stepping-stone conclusion whose type and scope are
  unambiguous — write under the standing waiver and report the path.
- `capture_source` and `preserve_evidence` operations — raw capture.
- `search_memory`, `read_memory`, `browse_memory`, and `review_memory` — read-only.
- `triage_memory` — writes only portable review state (Inbox, activation, and
  relation-queue identities are namespaced apart); it never edits a note.
- Updating `index.md`, `log.md`, and `ingested_into:` frontmatter after a
  confirmed write.
- Resolving obvious wikilink targets when the entity exists exactly.
- Continuing a previously-approved batch.

## Source citations and write-tool details

- **Citations.** Every non-empty `sources` entry must resolve to authorized
  governed Source or Evidence material. A URL, connector or remote file ID,
  working script or derivative summary is not the original: capture it first
  (`capture_source`, `preserve_evidence`, `preserve_artifacts`) and cite its
  governed path or stable ref. With no external source, pass an honest empty
  list. An unresolved citation returns `UNRESOLVED_SOURCE_CITATION` and writes
  nothing. Brackets and the leading `Knowledge Base/` are tolerated in paths.
- **Back-references.** Each cited source gets the new note's wikilink appended
  to its `ingested_into:` frontmatter, atomically with the citation. A live citing
  derivative clears the citation backlog only; semantic compilation remains unassessed.
  Omitting `sources` on a research-note,
  insight, failure or pattern only warns.
- **Edits.** A source-changing `edit_note` validates the complete final
  `sources` list and updates back-references atomically. An unrelated edit may
  leave a legacy unresolved citation; list that debt with
  `review_memory(mode="audit", categories=["unresolved_source_citation"])`.
- **Frontmatterless pages.** Body and section edits work on ordinary Markdown
  without synthesizing YAML. Frontmatter and take-row operations still require
  frontmatter.
- **Suggestions.** `create_note(suggestions=true)` adds a `suggestions` block of
  existing pages the note should probably link to (read it with
  `response_detail="full"`). It costs one retrieval pass over the corpus, so it
  is off by default; duplicate and overlap warnings run either way, and
  `write_feedback.suggestions.computed` says which happened.
- **Relation recovery.** When `RELATION_DISPOSITION_STALE` or
  `RELATION_DISPOSITION_MISSING` blocks an `edit_note`, replay the identical
  operation with `validate_only=true`, then commit with the returned
  `transition_token` and `relation_review_hash`, `relation_disposition="reviewed_none"`
  and a `relation_review_reason`. The hash is the relation review's, never the
  page content hash. The alternative is a typed `## Relations` entry
  (`- supports [[...]]`); Dataview `supports:: [[...]]` is not relation syntax.

### Correcting existing knowledge

When new evidence changes a durable conclusion, inspect each affected claim and its sources.
Distinguish what someone attested from what the evidence establishes.
Use the suitable existing home; a correction does not need another page by default.

1. Read the affected units and parent hashes; the exact propositions are visible.
2. Judge each proposition against the evidence; its support and uncertainty are explicit.
3. Update wording or author a verdict with guarded writers; anchored identities remain stable.
4. Add distinct corrective units and justified exact relations; readers can follow the correction.
5. Read the current units and bounded context; the requested state and citations are verified.

Preserve false historical propositions when their history matters, and mark their verdict.
A refuted claim keeps active standing and full rank as negative knowledge.
Contradiction and refutation do not imply supersession.
Use replacement only when another page should become the current answer, under the active engagement policy.

The semantic writer keeps prior Markdown in the correction's atomic transition.
`read_memory(include_history=true)` returns `revision_history` separately from the existing page operation log.
Add `unit_ref` to inspect one unit's revisions; use `history_limit` and `history_continuation` for bounded pages.
A result with `lookup: stale` could not check linked names yet; repeat the same call shortly, not the continuation, before you treat history as absent.
Entity and legacy corrections use their existing authorized edit owner.
Records corrections use `change_records(operation={"kind": "update", ...})` and its retained row history.
For observed-state corrections, follow the independent Records discovery route in
`references/planning-records.md`; Note recall alone does not search structured items.
A surface without the required edit owner reports that limit; it does not invent another home.

### Updating one unit with `observe_memory`

An update rebuilds the whole unit. `verdict`, `check_by` and `id` are
preserve-on-omit (pass an empty string to clear `verdict` or `check_by`), and
metadata rows the tool does not own are carried through. `tags`, `context` and
`relations` are replace-on-omit: omitting one clears it, so resend what should
stay. `verdict` is a categorical epistemic outcome (abandoned, confirmed,
inconclusive, qualified, refuted), never a confidence score. `update` and
`remove` need `expected_fingerprint` and `expected_hash`; `after_hash` from the
result is the next call's `expected_hash`.
