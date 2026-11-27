# Writing and connecting compiled knowledge

Apply the live engagement envelope from SKILL.md before these workflows. A batch or capture waiver
never raises an action-class ceiling: entity merge, supersession and restructure execution still
need the applicable confirmation. A new entity after resolve-before-create follows
`proactive_capture`. Each typed tool lists its fields: `read_instructions(topic="create_note")`.

## Entity resolution

Resolve entity candidates against the active entity registry and the selected knowledge packs.

1. Call `inspect_memory(operation="connections.resolve_entity", arguments={"name": ...})` first.
2. If one active page matches, use `edit_note` for a small stable-fact correction, or write the
   relation that connects it.
3. If none matches, call `manage_memory(operation="connections.create_entity", arguments={"name": ...})`
   only when the identity is stable, and central or recurring, and useful beyond the current
   source. [Engagement](engagement.md) holds the eligibility rule.
4. Pass known meaning as `relations=[{"kind": ..., "target": ...}]`. `connections` writes plain links
   and states no meaning, so use a `relations` item to name a specific relation.
5. If the reply says the entity type is unregistered, save the type through [vocabulary](vocabulary.md).
   Never edit frontmatter around the registry rule.

### Names in another script

A turn written in one script reaches a page only through a name in that script. A user who writes in
Japanese says the katakana or kanji name, not the Latin title. So when you write an entity's name in a
script other than the one the user used for it, add a native-script alias: the user's own spelling.

- At capture: `manage_memory(operation="connections.create_entity", arguments={'aliases': [...], 'name': ...})`.
- On an existing page: `edit_note(action="set_fields", fields={"aliases": [...current, "<the user's spelling>"]}, why=...)`.
  The list replaces the field, so keep the current aliases.

Use only a spelling the user wrote or the source states. Never invent a transliteration or a
translation. An alias is a name the page answers to, so a name that any other page already answers to
(a note's title included) is refused, at capture and on edit.

## Vocabulary consideration

Before saving durable knowledge, consider reuse, enrich, propose-new, generic, no-edge, or defer from
the material and the bounded review evidence. Resolve the candidate entity types and relation meanings
before you propose a new one. Generic and no-edge are truthful outcomes when no useful distinction is
supported; there is no quota for types or edges. A resolver returns definitions and evidence, and its
null selection leaves the judgment to you. When a useful recurring identity has no fitting type, author
its definition and rationale and propose it ([vocabulary](vocabulary.md)). Never squeeze it into a core
type, and never abandon the proposal because the match came back empty.

Record the outcome and rationale through vocabulary triage. That record is consideration, not execution
or permission: canonical writers keep their existing confirmation rules. Do not treat a proposal, a
review decision or a generic connection as authority to create an entity, a type or an edge.

## Agent write loop

Use this loop whenever a durable conclusion should enter Evermatter.

1. Partition the durable candidates by retrieval question, subject, domain, episode and epistemic role
   before you choose destinations. Reuse current recall. Call `search_memory` only for missing prior
   notes or sources.
2. Assess scope and title fit for each candidate home. An open or similar page has no automatic
   priority. Read exact units or pages only as needed. Keep coherent details together and route
   independently reusable clusters separately. Keep durable hypotheses with their speaker, source and
   uncertainty.
3. Identify the provenance: which governed `Sources/` or `Evidence/` pages the conclusion draws from.
   They become `sources` on the write. Capture external material first. A URL, a connector or message
   id, a remote file id, a working script, an excerpt or a derivative summary is not a source citation.
   Use `save_source` or `save_evidence` for the original, then cite the returned path or ref. If the
   original cannot be recovered, remove the unsupported claim and say so; never recreate an
   "original" from the derivative. A conclusion from live work with nothing external to capture takes
   an honest empty `sources`.
4. Draft the typed page at the right layer: `save_source` for raw source, `create_note` for a compiled
   conclusion, `add_observation` or `edit_note` for a guarded correction, `supersede_note` for a
   replacement. [Page types](page-types.md) says which type fits.
5. Reuse the links that the current search or read context already established. When connections
   remain unknown, call `inspect_memory(operation="connections.suggest_links", arguments={"draft_title": ..., "draft_body": ...})`
   on the draft, or `connections.suggest_relations` when direction is unresolved. Accept only links
   that clarify provenance or context, and write accepted note-level edges under `## Relations` as
   `- relation_type [[Target]]`. Carry them in the first write, not in a follow-up `edit_note`.
6. Write, then read the result. A reply carries `warnings` when the write warned, and a
   `structure_suggestion` when the page needs a decision. Ask for `detail="full"` when provenance or
   connectivity is in doubt: it adds the sources cited, the body links and the relation debt. If all
   three are zero and that is not honest, fix it before you report. Link suggestions cost a retrieval
   pass, so they stay off unless you pass `suggest_links=true` to `create_note`.
7. If a near-duplicate warning fires, prefer `edit_note` or `supersede_note` over a parallel page.
8. If the write returned a `structure_suggestion`, answer it as below.
9. Report one line: `Saved -> <title>`.

## Closing a multi-step workflow

Group work by dependencies, not by one tool call per reasoning turn. Read independent known pages
together when the client allows parallel calls. Save several artifacts with one `save_evidence` call,
then queue extraction with `manage_memory(operation="media.process", arguments={"paths": [...]})`. The
ordered item results acknowledge the durable work, and extraction finishes asynchronously. Do not claim to
have read extracted content before a status or read call shows that it exists.

Author related observations and accepted connections in the initial note where possible. On an
existing page, `edit_note(action="replace_text", edits=[...])` combines known string changes into one
guarded mutation. It is not an atomic multi-page transaction, so keep conflicting edits ordered. When a
compiled note cites a media sidecar that extraction can still change, finish the extraction before you
validate the dependent write, and do source-independent work while it runs. Never bypass a
source-version guard or reuse a stale staged back-reference.

Read each terminal result and handle actionable warnings. Do not reread the page after every committed
edit, and do not poll until every projection is current. Pass the returned page `id` as `note` only when
the next `replace_body` must guard against a concurrent edit. Use the returned unit id as `observation`
for exact semantic edits. Finish with one bounded check of the requested state and citations. If the next
operation needs current graph edges, honor its freshness refusal; optional graph lag does not invalidate a
successful ordinary lookup.

### Answering a structure suggestion

A write can return a `structure_suggestion`. The runtime decides when to ask and you judge meaning. You
hold the page, so answer once, then carry on with the task. Do not recite field names to the user.

**`note_cohesion`: does the page answer more than one independent question?** The page has grown to a
size step. That does not say it is overloaded, because the runtime counts size and you judge meaning. Name
the questions.

- **One question.** Keep the page whole however long it is. Dismiss the item with
  `decide_review_item(review_item=<id>, decision="dismiss", why="intentional: <the one question>")`. The page
  stays quiet until its next size step.
- **Several questions, each with its own material.** Propose a split through curation
  ([vault care](vault-care.md)). Ask the user for exact confirmation of the previewed plan before you
  apply it. A remainder or new child that still reaches a size step gets its own item; answer those in the
  same pass.
- **A hub by design.** Add a breadth tag (`hub` or `snapshot`) with `edit_note(action="set_tags", tags=[...])`,
  resending the current tags. The page is then never asked. A page below the first step is not asked either.

Do not split a coherent note to make the question go away.

**`ownership`: does the page belong in its project?** The page was created in a project, its project key
or title just changed, or the daily rotation reached it. It does not say the page is misfiled. Apply the
ownership test that the question carries to the page in hand, answer before you reply to the user, and
leave the page unchanged while you answer.

- **It serves the project's goal.** Dismiss with `why="intentional: <the project goal this page serves>"`.
  The item stays quiet while the page keeps its project key, folder and title, however much the page grows.
- **It has its own goal, schedule or course of events.** Tell the user. On their yes, propose the move or
  split through curation. A write with a new `project` slug registers the project. Link the related
  projects' notes with a typed relation, and get the user's exact confirmation of the previewed plan.
- **You cannot tell.** If the user already decided this page's home and nothing since bears on ownership,
  the earlier decision stands: dismiss with a reason that starts `deferred:`, or make no call.

A page that is asked and also at a size step carries the ownership question first.

**`rename`: the title no longer matches the file name.** The suggestion holds the name that the title now
gives. The user's own retitle is the approval, so do not ask again. Preview the relocation with the arguments
the suggestion carries (`curation.relocate`, [vault care](vault-care.md)), tell the user the new name in one
line, and apply it. A rename keeps links working. When no suggestion comes back, the name still follows the
title, or the title did not change.

**`name_drift`: many pages carry stale names.** Pages made before titles named files, or retitled since,
keep a name that no longer follows their title. The review queue holds one item for the whole vault, and
`inspect_memory(operation="maintenance.name_drift")` lists the pages with the name each would get. Never
rename a page that has no usable title; the list marks those. Propose the next batch to the user as one
preview with the call the item carries, apply it only on their yes, and repeat per batch until the item
closes.

**`source_classification_debt`: the vault holds sources nobody gave a kind.** Offer once, in the user's own
words, to sort them, then reclassify each ([page types](page-types.md), source).

## Comprehensive coverage, minimal expression

Capturing at the landing is about timing, not volume: it never means keep less. Minimality is a property of
expression (distillation, signal density, no redundancy), never of coverage. Do not drop context or detail
because it does not seem important. Importance is usually legible only in hindsight, and nothing here forces
the tradeoff: there is no retention decay, retrieval is hybrid, `Sources/` is append-only, and storage has no
limit. Default coverage to comprehensive, and reserve concision for how a note is written, not what it keeps.
When you hesitate between keeping a detail and dropping it, keep it: raw detail stays recoverable. Put raw
verbatim material in `Sources/` liberally. Keep compiled notes distilled in form but never context-pruned.

## Descriptive and analytical coverage

The Knowledge Base serves two complementary purposes, and both are first-class.

- **Descriptive coverage** describes what is: architecture hubs, point-in-time snapshots and concept
  entities. They let a future planner orient quickly.
- **Analytical coverage** extracts reusable lessons: patterns, insights, failure modes and decisions. They
  compound across projects.

When you orient in a new area, descriptive hubs usually come first. Patterns and insights then extract from
the descriptive layer as second-order knowledge.

**Boundary with a code repository.** For a software project the repository is the source of truth for code,
design and decisions. Knowledge Base coverage is the cross-session, cross-project layer the repository cannot
hold: strategy, roadmap, orientation and hard-won empirical findings. It is never a condensed changelog or a
restatement of what specs and commits already capture.

## Write discipline

These rules are non-negotiable.

1. **Read-only paths.** Never write vault content outside `Knowledge Base/`. Sibling folders in the vault are
   inputs only: compiled notes may link to them but never modify them. Ordinary workspace files (the user's
   code, documents and project files) are not the vault. Write them with your own file tools when asked.
   `write-scope.md` holds the full path map.
2. **Sources and Evidence are append-only.** Once a file lands in `Sources/` or `Evidence/`, never edit its
   content. Correct it by adding a new source and superseding the old one through a compiled note. Moving a
   file within the same append-only tree is allowed through `manage_memory(operation="files.move", arguments={...})`.
   Crossing the boundary is forbidden.
3. **Use the active write authority.** Follow [engagement](engagement.md) and the current envelope for every
   write, including raw capture and vocabulary effects. Draft internally. Present content and wait only when
   the active authority or a named procedure requires it. An explicit request or approved batch keeps its
   existing scope, and neither lifts an action-class ceiling or grant.
4. **Compiled pages carry their connections.** A compiled note names in `sources` every `Sources/` or
   `Evidence/` page it draws from, in the write call itself and not as a follow-up edit. Each entry makes the
   server append this note's wikilink to that source's `ingested_into`, which maintains the source-to-note
   graph. Skip it, and the source stays in the unprocessed backlog even though you compiled it. A conclusion
   that builds on prior conclusions links them inline, and the links that carry direction go under
   `## Relations` as typed edges (see Linking discipline).

   **Honest zero is legitimate.** There is no minimum edge count and no quota. A note with no source and no
   prior art is a complete, valid note. This rule forbids omitting a link you know about, never failing to
   find one. Manufacturing an edge to look connected is worse than none.
5. **No `confidence` floats.** A claim carries no confidence score, and a degree of belief is never a
   frontmatter field. Citations and link counts convey trust. A degree of belief that its holder stated is a
   `## Belief` unit: see Recording a degree of belief in `page-types.md`.
6. **Supersession over deletion.** When information is replaced, supersede the old page and never delete it.
   `supersession.md` has the protocol.

The server writes the frontmatter, the canonical H1, `index.md`, `log.md` and the source back-references.
Never write them by hand. [Page types](page-types.md) defines what each field means.

## Research scope keys

A research note's `project` is a slug-shaped key registered in `_Schema/project-keys.yaml`. The set is open,
not a closed enum. Pick the most specific scope first. A typical starter set:

- Products and projects: `project-alpha`, `project-beta`, one key per project.
- Domains: `work`, plus your own (`research`, `ops`).
- Cross-cutting: `personal`, for anything not tied to a specific project or domain.

A pattern that applies across several projects takes `projects` (a list) instead of `project`.

A write with a new `project` or `projects` slug registers the key and creates the matching
`Notes/Research/<Folder>/` directory on first use, with no manual YAML edit. A typo guard refuses a new key
within edit distance 2 of an existing key, and its message names the near key, so the registry stays clean.

## Source compilation pass

The active conversation agent owns this judgment under the request and the live engagement policy. Selected
compilation needs no special user wording to trigger decomposition, central identity resolution or relation
review. Raw capture stays valid without compilation.

1. Read the available full source through its exposed routes. Unread or unavailable portions stay explicit.
2. Inventory the reusable themes and the central durable referents. The inventory reflects meaning, with no
   output quotas.
3. Retrieve prior conclusions, resolve central identities and inspect suitable filing homes. Keep reuse and
   missing durable identities distinct.
4. Compile independently useful outputs into suitable existing or new homes. Keep attribution, observations,
   hypotheses and measurements distinct, keep coherent detail together, and cite the preserved originals in
   the first write. Handle durable entities through resolve-before-create and the vocabulary route.
5. Review candidate relations against their meaning and evidence. Author a grounded specific relation, a
   proposed new relation type, a plain link as the last resort, or no edge. Resolve registered meaning
   before you author, and read the relation feedback after each write. Generic connectivity does not
   establish typed specificity, and a suggestion never establishes meaning by itself.
6. Check the whole inventory once against the committed outputs, and report any unread, unavailable or
   uncompiled portion. Custody, extraction and valid citations are separate from semantic coverage. One live
   citing derivative clears the citation backlog only, and the server reports semantic compilation as
   `unassessed`. Do not add notes or edges to reach a count, and do not repeat the same source warning
   indefinitely.

### Several recordings and documents

When the sources are a set of recordings and documents, such as a seminar series or a case file, run the pass
above with these additions.

1. Inventory every source and check its extraction before you read it: the processing state, the language
   fields in the sidecar (`asr_language`, `asr_language_probability`, `asr_language_source`,
   `asr_language_mode`), and faults such as a wrong language or one line repeated. A `multilingual` mode
   means the engine read the recording in several languages, so `asr_language` is then not a wrong-language
   fault. Reprocess a faulty extraction with
   `manage_memory(operation="media.reprocess", arguments={"path": ..., "language": <code>})` before you rely on it.
2. Build a timeline. Order recordings by capture time, and mark recorder splits (one session in several
   files), overlaps and duplicates, so each session counts once.
3. Read transcripts by time range and documents by page. Cite `[m:ss]` ranges and `[page N]` markers, not
   whole files.
4. Give each claim one attribution class. The open observation category names the class, such as
   `source_asserted`, `verified`, `user_position` or `question`. The `(context)` slot holds the citation, as in
   `- [verified] <claim> (<primary source and section>)`.
   - Source-asserted: a speaker or document says it.
   - Independently verified: checked against a primary source, which the `(context)` cites.
   - The user's own positions: record only what the user states. Where one is missing, leave a marked slot
     such as `(user position: to confirm)`. Never infer it.
   - Open questions: record what remains unresolved.
5. Check legal, scientific and numeric claims against primary sources, such as the statute text, the paper or
   the dataset, before you mark them verified. An unchecked claim stays source-asserted.
6. Keep a coverage ledger with one row per source: the ranges or pages read, the outputs that cite it, and
   what stays unread or unavailable.
7. Finish with an audit of the ledger against the committed outputs. Report sources with no output, unread
   ranges and unverified claims as gaps.

## Linking discipline

Link every compiled page to what it actually connects to. This is Write discipline rule 4 with its mechanics.
Linking turns the Knowledge Base from a junk drawer into a graph. The obligation is to record the connections
you know about, not to reach a count. A page with nothing to link to is finished, and a fabricated edge is
worse than none.

**Wikilink form.** Write full vault-rooted links, such as `[[Knowledge Base/Entities/Concepts/Profile]]`. The
server normalizes any other form, so write what you have. Link back to the originating `Sources/` file through
the `sources` list.

**Relation form: one directional edge per bullet under `## Relations`.** Use a governed lower-snake-case
relation and one wikilink:

```markdown
## Relations
- refines [[Knowledge Base/Notes/Insights/Earlier Conclusion]]
- depends_on [[Knowledge Base/Entities/Decisions/Architecture Decision]]
```

Use semantic-block metadata such as `- relations: evidenced_by: [[Source]], contradicts: [[Earlier Finding]]`
when the edge belongs to a specific claim, finding or piece of evidence. An inline wikilink is only a plain
link. When the note uses the page it links (the evidence it rests on, the decision it backs, a person who took
part, the plan it carries out), also write that relation under `## Relations`. Keep the inline link alone only
when the two pages just share a subject. Never turn a semantic suggestion into a typed relation without
reviewing its meaning.

**Relation labels are governed; categories and tags are not.** Categories are open vocabulary, so invent one
whenever it fits. Relation labels come from a registry. The server keeps an unregistered label
(`- inspired_by [[X]]`) and surfaces it, but it does not count as a graph edge and does not connect the page.
That is deliberate: the label stays visible as review debt instead of silently turning into a generic link.

The vocabulary is extensible, not fixed.

1. Resolve before you author: `inspect_memory(operation="connections.resolve_relation")`. Reuse a specific
   truthful registered relation when one fits.
2. A write that adds the generic relation is refused. When no relation fits, propose a new relation type,
   write a plain link as the last resort, or write no edge. No edge beats invented meaning.
3. For a durable recurring distinction, propose it through [vocabulary](vocabulary.md). Meaning is immutable:
   a correction creates a new canonical key and deprecates the old one.

Registered relations become traversable edges, and clean aliases stay authoring labels. A registry-marked
generic relation that an older page already holds establishes connectivity, not typed specificity. An
all-generic advisory asks for a meaning review without forcing an edge.

If a wikilink target does not exist yet, create the entity page instead of leaving a dangling link. Dangling
links accumulate and surface in `inspect_memory(operation="maintenance.audit")` as `broken_wikilink`.

When an entity points at a currently evolving external artifact (a live spec, a code library, a service
config), write it pointer-style: a summary, the canonical-source pointer and connective tissue. Mirror-style
copies (versions, file inventories, command lines) guarantee drift.

## When to ask and when to proceed

Use the live [engagement](engagement.md), envelope class and disposition for the actual action and caller.
Off or advisory permits no agent-initiated write. An explicit user request or approved batch follows its
existing scope and required guards. When the user approves a scope of several files up front, the work
collapses into one batch write under that authority. A capture preference never lifts a ceiling, a grant, a
scope limit or a named procedural hold.

Ask for confirmation when the active authority or a named procedure requires it. Keep confirmation for
restructure (including supersession, entity merge and substantial curation) and for accepting a queued
relation. A permitted additive write needs no extra confirmation because it creates a note, an entity or a
registry entry. Raw capture also follows the live authority: first-use labels can require registry writes.

Ask about type, scope, domain, medium or page framing only when a material choice remains ambiguous after you
check the source, the current context and the registries. An unspecified field alone does not require a
question.

Draft and commit through the normal governed writer when authorized, and present content only when required.
Reuse resolved identities and existing meaningful connections, read the mutation result, and report the
committed paths. Read-only recall and review follow the live retrieval policy.

## Source citations and write details

- **Citations.** Every non-empty `sources` entry must resolve to authorized governed Source or Evidence
  material. A URL, a connector or remote file id, a working script or a derivative summary is not the
  original: capture it first (`save_source`, `save_evidence`) and cite its governed path or stable ref. With no
  external source, pass an honest empty list. An unresolved citation writes nothing. Brackets and the leading
  `Knowledge Base/` are tolerated in paths.
- **Back-references.** Each cited source gets the new note's wikilink in its `ingested_into`, atomically with
  the citation. Omitting `sources` on a research-note, insight, failure or pattern only warns.
- **Edits.** A source-changing `edit_note` validates the complete final `sources` list and updates
  back-references atomically. An unrelated edit may leave a legacy unresolved citation; list that debt with
  `inspect_memory(operation="maintenance.audit", arguments={"categories": ["unresolved_source_citation"]})`.
- **Frontmatterless pages.** Body and section edits work on ordinary Markdown without synthesizing YAML.
  Frontmatter and take-row operations still require frontmatter.
- **Relation recovery.** When an `edit_note` is blocked for a stale or missing relation decision, send the
  same call again with an `unlinked_reason`, and that one call commits. A previewed edit (`preview=true`)
  commits when you send the same call again without `preview`. The decision then binds the page, not its
  bytes: later edits ask nothing while the page stays unconnected, and an edit that removes the last
  connection is still refused. The alternative is a typed `## Relations` entry (`- supports [[...]]`), or a
  plain link (`- links_to [[...]]`) when the page only shares its subject with another page. Use
  `unlinked_reason` only when no page relates. Dataview `supports:: [[...]]` is not relation syntax. A refused
  generic relation takes no disposition: repeat the call with a listed specific relation, a proposed relation
  type or a plain link ([mutation-results](mutation-results.md)).

## Correcting existing knowledge

When new evidence changes a durable conclusion, inspect each affected claim and its sources. Distinguish what
someone attested from what the evidence establishes. Use the suitable existing home; a correction does not
need another page by default. Reconcile each affected current proposition and its explanation with the
revised scope. A later exception does not complete a correction while other current wording still overstates
it.

1. Read the affected units and their parent hashes, so the exact propositions are visible.
2. Judge each proposition against the evidence, with its support and uncertainty explicit.
3. Update the wording, or author a verdict, with the guarded writers. Anchored identities stay stable.
4. Add distinct corrective units and justified exact relations, so readers can follow the correction.
5. Read the current units and bounded context, and verify the requested state and citations.

Preserve false historical propositions when their history matters, and mark their verdict. A refuted claim
keeps active standing and full rank as negative knowledge. Contradiction and refutation do not imply
supersession. Use replacement only when another page should become the current answer, under the active
engagement policy.

The semantic writer keeps the prior Markdown in the correction's atomic transition.
`read_page(page=..., part="history")` returns the revision history separately from the page operation log.
Entity and legacy corrections use their existing authorized edit owner. Records corrections use `edit_record`
and its retained row history. For observed-state corrections, follow the Records discovery route in
[planning and records](planning-records.md): note recall alone does not search structured items. A surface
without the required edit owner reports that limit and does not invent another home.

### Updating one unit with `edit_note`

An update rebuilds the whole unit. `verdict`, `check_by` and `as_of` persist when you omit them, and an empty
string clears one. The unit id always persists, and metadata rows the tool does not own carry through.
`tags`, `context` and `links` are replace-on-omit: omitting one clears it, so resend what should stay. A link in
a body or in a unit reads and writes as `[[Title]]`, and the vault stores the page's path form. `verdict` is a
categorical epistemic outcome, a judgment in words and never a confidence score. `inspect_memory(operation="schema.infer",
arguments={"subject": "categories"})` lists the accepted verdicts under `verdicts`. `add_observation` sets it on
a new unit. A belief unit's `links` are replace-on-omit too: an update that omits `assesses` drops the claim the
belief assesses, so resend it. Recording a degree of belief in `page-types.md` gives the unit's form.
`edit_note(action="edit_observation", observation=<unit id>)` and `action="remove_observation"` check the unit
version and refuse a stale id with the current one.
